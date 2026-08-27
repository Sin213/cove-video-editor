from __future__ import annotations

import collections
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from . import ffmpeg_utils as ff
from .clip import Clip, SubtitleTrack, sequence_length, sort_clips
# The encoder argument group lives next to the format table because the
# capability probe builds its command from it too; re-exported here since
# this is where export command construction lives.
from .ffmpeg_utils import build_export_video_encoder_args  # noqa: F401


if os.name == "nt":
    _CREATE_NO_WINDOW = 0x08000000
    _POPEN_KWARGS: dict = {"creationflags": _CREATE_NO_WINDOW}
else:
    _POPEN_KWARGS = {}


_FILTER_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class AudioTrack:
    path: Path
    replace: bool = False
    volume: float = 1.0
    original_volume: float = 1.0
    offset: float = 0.0          # timeline seconds where the track starts
    duration: float = 0.0        # natural length; 0 means "use full input"
    src_start: float = 0.0       # source-file start in seconds (trim)


@dataclass
class ExportJob:
    clips: list[Clip]
    output: Path
    fmt_key: str
    crop: tuple[int, int, int, int] | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    # List of added-audio tracks; each placed at its own offset and mixed
    # with the clip audio (or replacing it if `replace` is true on all
    # tracks; the final flag wins).
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    # Optional region restriction — if set, only [region_start, region_end)
    # of the timeline is exported (via output-side -ss / -t on the final map).
    region_start: float | None = None
    region_end: float | None = None
    # Optional burn-in subtitle track. When present, the active
    # SubtitleTrack is applied to the concat'd video output via the
    # `subtitles=` filter before final mapping.
    subtitles: SubtitleTrack | None = None
    # Which video encoder family to use: "auto" (hardware when it really
    # works, else CPU), "cpu", "nvenc" or "amf". Callers that predate the
    # setting - and every audio-only job - keep the safe default.
    encoder_pref: str = "auto"

    @property
    def total_timeline(self) -> float:
        if self.clips:
            return sequence_length(self.clips)
        return _audio_only_duration(self.audio_tracks) if self.audio_tracks else 0.0


class ExportWorker(QObject):
    progress = Signal(int)
    eta = Signal(float)
    log = Signal(str)
    finished = Signal(Path)
    failed = Signal(str)

    def __init__(self, job: ExportJob) -> None:
        super().__init__()
        self._job = job
        self._cancelled = False
        self._proc: subprocess.Popen | None = None
        self._started_wall: float = 0.0
        self._eta_smoothed: float | None = None
        self._tmp_dir: Path | None = None

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def run(self) -> None:
        self._started_wall = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="cove-subs-") as _tmp:
            self._tmp_dir = Path(_tmp)
            try:
                cmd = self._build_command()
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc))
                return
            self.log.emit("$ " + " ".join(cmd))
            try:
                self._execute(cmd)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc))
                return
            finally:
                self._tmp_dir = None
        if self._cancelled:
            self.failed.emit("Cancelled")
            return
        self.finished.emit(self._job.output)

    def _resolve_subtitle_path(self, sub: SubtitleTrack, tgt_w: int, tgt_h: int) -> Path:
        """Return the path ffmpeg's ``subtitles=`` filter should load.

        We always materialize a temp ASS file with ``PlayResX/PlayResY``
        matching the output resolution. That anchors libass's coordinate
        system to the output video so ``Fontsize=N`` renders N pixels
        tall — 1:1 with Cove's preview overlay. The previous path loaded
        the raw SRT; libass then converted it to ASS using its default
        PlayResY=288, which scaled any font up by ``out_h / 288`` and
        produced burn-ins 2–3× larger than the preview.

        Cues are written with the user's sync offset already applied so
        the live preview, sync dialog, and burn-in all stay in lockstep.
        """
        assert self._tmp_dir is not None, "_resolve_subtitle_path called outside run()"
        out = self._tmp_dir / f"{sub.id}.ass"
        out.write_text(_render_ass(sub, tgt_w, tgt_h), encoding="utf-8")
        return out

    # --- build --------------------------------------------------------

    def _build_command(self) -> list[str]:
        job = self._job
        clips = sort_clips(job.clips)
        spec = ff.EXPORT_FORMATS.get(job.fmt_key)
        if spec is None:
            raise RuntimeError(f"unknown format {job.fmt_key!r}")

        is_audio_only = spec["vcodec"] is None
        needs_audio = spec["acodec"] is not None

        # Video/Project export still needs at least one clip. Audio-only
        # export may run on standalone added-audio tracks with no clips.
        if not clips and not (is_audio_only and job.audio_tracks):
            raise RuntimeError("no clips to export")

        # Build the list of segments on the timeline: either a clip, or a gap
        # (black + silent). Gaps between clips are filled with `color` /
        # `anullsrc` sources so concat matches. With no clips, this yields a
        # single silent gap spanning the added-audio duration.
        timeline_end = sequence_length(clips) if clips else _audio_only_duration(job.audio_tracks)
        segments = _segments_with_gaps(clips, timeline_end)

        tgt_w, tgt_h = resolve_target_size(clips, job.crop, job.width, job.height)

        cmd: list[str] = [ff.require_ffmpeg(), "-nostdin", "-y", "-hide_banner",
                          "-progress", "pipe:1", "-nostats", "-loglevel", "error"]

        # one -i per real clip; gaps are synthesized inside filter_complex.
        # Image clips need `-loop 1 -framerate 30 -t dur` before `-i` so
        # ffmpeg produces a finite video stream of the right length.
        clip_inputs: dict[str, int] = {}
        for c in clips:
            clip_inputs[c.id] = len(clip_inputs)
            if c.asset.kind == "image":
                img_dur = max(0.1, c.src_end - c.src_start) / max(0.01, c.speed)
                cmd += [
                    "-loop", "1",
                    "-framerate", "30",
                    "-t", f"{img_dur:.3f}",
                    "-i", str(c.path),
                ]
            else:
                cmd += ["-i", str(c.path)]

        # One -i per added-audio track. Parallel list of ffmpeg input indices
        # so the filter graph can reference them.
        add_track_indices: list[int] = []
        for track in job.audio_tracks:
            add_track_indices.append(len(clip_inputs) + len(add_track_indices))
            cmd += ["-i", str(track.path)]

        filter_complex, v_label, a_label = self._build_filtergraph(
            segments, clip_inputs, add_track_indices,
            tgt_w=tgt_w, tgt_h=tgt_h,
            is_audio_only=is_audio_only, needs_audio=needs_audio,
            acodec=spec.get("acodec"),
        )
        cmd += ["-filter_complex", filter_complex]

        if not is_audio_only:
            cmd += ["-map", f"[{v_label}]"]
        if needs_audio and a_label is not None:
            cmd += ["-map", f"[{a_label}]"]

        # region export (output-side trim — cheap and precise)
        if job.region_start is not None and job.region_end is not None:
            cmd += [
                "-ss", f"{max(0.0, job.region_start):.3f}",
                "-t",  f"{max(0.01, job.region_end - job.region_start):.3f}",
            ]

        if spec["vcodec"]:
            # Encoder choice is resolved here, outside the filtergraph: the
            # visual normalization above is identical no matter which
            # encoder ends up consuming it.
            encoder = resolve_export_video_encoder(spec, job.encoder_pref)
            cmd += build_export_video_encoder_args(encoder, fps=job.fps)
        if needs_audio and spec["acodec"]:
            cmd += ["-c:a", spec["acodec"]]
            if spec["acodec"] == "aac":
                cmd += ["-b:a", "192k"]
        cmd += list(spec.get("extra", []))
        cmd.append(str(job.output))
        return cmd

    def _build_filtergraph(
        self,
        segments: list[tuple[str, float, float, Clip | None]],
        clip_inputs: dict[str, int],
        add_track_indices: list[int],
        *, tgt_w: int, tgt_h: int,
        is_audio_only: bool, needs_audio: bool,
        acodec: str | None = None,
    ) -> tuple[str, str, str | None]:
        job = self._job
        parts: list[str] = []
        v_labels: list[str] = []
        a_labels: list[str] = []
        # libmp3lame works best with 44100 Hz fltp; force that in silence sources.
        if acodec == "libmp3lame":
            _null_sr = 44100
            _null_aformat = ",aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
        else:
            _null_sr = 48000
            _null_aformat = ""

        for i, (kind, seg_start, seg_end, clip) in enumerate(segments):
            seg_dur = max(0.01, seg_end - seg_start)
            if kind == "clip":
                c = clip
                assert c is not None
                inp = clip_inputs[c.id]
                is_image = c.asset.kind == "image"
                if not is_audio_only:
                    if is_image:
                        # Image input is already the right length (`-t`),
                        # so trim/setpts is unnecessary — just normalize
                        # pts to 0 and run through crop/scale/pad.
                        vchain = ["setpts=PTS-STARTPTS"]
                        if job.crop:
                            x, y, w, h = job.crop
                            vchain.append(f"crop={w}:{h}:{x}:{y}")
                        vchain.append(
                            f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease"
                            ":force_divisible_by=2,"
                            f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                            "setsar=1"
                        )
                        # yuv420p normalizes the pixel format so concat with
                        # neighbouring video clips doesn't fail when the
                        # source image is RGBA/RGB24.
                        vchain.append("format=yuv420p")
                    else:
                        vchain = [f"trim=start={c.src_start:.3f}:end={c.src_end:.3f}",
                                  "setpts=PTS-STARTPTS"]
                        if job.crop:
                            x, y, w, h = job.crop
                            vchain.append(f"crop={w}:{h}:{x}:{y}")
                        vchain.append(
                            f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease"
                            ":force_divisible_by=2,"
                            f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                            "setsar=1"
                        )
                        if abs(c.speed - 1.0) > 1e-6:
                            vchain.append(f"setpts={1.0/c.speed:.5f}*PTS")
                        # Mixed-resolution sources can reach concat with matching
                        # dimensions but different SAR/pix_fmt; setsar=1 above and
                        # yuv420p here keep every visual branch identical.
                        vchain.append("format=yuv420p")
                    parts.append(f"[{inp}:v]" + ",".join(vchain) + f"[v{i}]")
                    v_labels.append(f"v{i}")
                if needs_audio:
                    # Image clips never contribute audio.
                    if (
                        not is_image and c.asset.has_audio and not c.muted
                        and c.linked_audio and not c.audio_removed
                    ):
                        achain = [f"atrim=start={c.src_start:.3f}:end={c.src_end:.3f}",
                                  "asetpts=PTS-STARTPTS"]
                        if abs(c.speed - 1.0) > 1e-6:
                            achain.append(f"atempo={_atempo_chain(c.speed)}")
                        volume = max(0.0, min(2.0, c.audio_volume))
                        if abs(volume - 1.0) > 1e-6:
                            achain.append(f"volume={volume:.3f}")
                        parts.append(f"[{inp}:a]" + ",".join(achain) + f"[a{i}]")
                    else:
                        parts.append(
                            f"anullsrc=channel_layout=stereo:sample_rate={_null_sr},"
                            f"atrim=duration={seg_dur:.3f},asetpts=PTS-STARTPTS"
                            f"{_null_aformat}[a{i}]"
                        )
                    a_labels.append(f"a{i}")
            else:  # gap
                if not is_audio_only:
                    parts.append(
                        f"color=c=black:s={tgt_w}x{tgt_h}:d={seg_dur:.3f}:r=30,"
                        f"setsar=1,format=yuv420p[v{i}]"
                    )
                    v_labels.append(f"v{i}")
                if needs_audio:
                    parts.append(
                        f"anullsrc=channel_layout=stereo:sample_rate={_null_sr},"
                        f"atrim=duration={seg_dur:.3f},asetpts=PTS-STARTPTS"
                        f"{_null_aformat}[a{i}]"
                    )
                    a_labels.append(f"a{i}")

        # concat across all segments
        n = len(segments)
        if n == 0:
            raise RuntimeError("empty timeline")

        v_out: str | None = None
        a_out: str | None = None
        if not is_audio_only:
            if len(v_labels) != n:
                raise RuntimeError(
                    f"internal export error: expected {n} video labels, got {len(v_labels)}"
                )
            if needs_audio:
                if len(a_labels) != n:
                    raise RuntimeError(
                        f"internal export error: expected {n} audio labels, got {len(a_labels)}"
                    )
                # Concat expects inputs interleaved per segment: v0,a0,v1,a1,...
                # NOT all-video-then-all-audio (which causes type-mismatch errors).
                interleaved = [lbl for pair in zip(v_labels, a_labels) for lbl in pair]
                parts.append(
                    f"{_join_filter_labels(interleaved)}"
                    f"concat=n={n}:v=1:a=1[vc][ac]"
                )
                v_out, a_out = "vc", "ac"
            else:
                parts.append(f"{_join_filter_labels(v_labels)}concat=n={n}:v=1:a=0[vc]")
                v_out = "vc"
        else:
            if len(a_labels) != n:
                raise RuntimeError(
                    f"internal export error: expected {n} audio labels, got {len(a_labels)}"
                )
            parts.append(f"{_join_filter_labels(a_labels)}concat=n={n}:v=0:a=1[ac]")
            a_out = "ac"

        # Added-audio tracks: each placed at its offset, plays for its own
        # duration (padded with silence), then mixed with the clip audio.
        if add_track_indices and needs_audio:
            total = (
                max(0.01, sequence_length(job.clips))
                if job.clips else _audio_only_duration(job.audio_tracks)
            )
            extra_labels: list[str] = []
            replace_any = False
            orig_volume = 1.0
            for i, track_idx in enumerate(add_track_indices):
                track = job.audio_tracks[i]
                offset = max(0.0, track.offset)
                natural_dur = track.duration if track.duration > 0 else total
                end_t = min(total, offset + natural_dur)
                play_dur = max(0.01, end_t - offset)
                pre_ms = int(round(offset * 1000))
                delay_stage = (
                    f"adelay={pre_ms}:all=1," if pre_ms > 0 else ""
                )
                label = f"extra_a{i}"
                trim_start = max(0.0, track.src_start)
                trim_end = trim_start + play_dur
                parts.append(
                    f"[{track_idx}:a]"
                    f"atrim=start={trim_start:.3f}:end={trim_end:.3f},"
                    f"asetpts=PTS-STARTPTS,"
                    f"{delay_stage}"
                    f"apad=whole_dur={total:.3f},"
                    f"volume={track.volume:.3f}[{label}]"
                )
                extra_labels.append(label)
                if track.replace:
                    replace_any = True
                orig_volume = track.original_volume

            if len(extra_labels) == 1:
                mixed_extra = extra_labels[0]
            else:
                joined = "".join(f"[{lbl}]" for lbl in extra_labels)
                parts.append(
                    f"{joined}amix=inputs={len(extra_labels)}:"
                    f"duration=longest:dropout_transition=0[extra_mix]"
                )
                mixed_extra = "extra_mix"

            if replace_any or a_out is None:
                a_out = mixed_extra
            else:
                parts.append(
                    f"[{a_out}]volume={orig_volume:.3f}[orig_a];"
                    f"[orig_a][{mixed_extra}]amix=inputs=2:"
                    f"duration=longest:dropout_transition=0[mix_a]"
                )
                a_out = "mix_a"

        # Burn-in subtitles last so they appear on the final frame
        # regardless of what the video went through. We build a fresh ASS
        # file with PlayResX/PlayResY matching the output, which means
        # every Fontsize / Outline / MarginV value we bake in is in
        # output pixels — the same sizing Cove's preview overlay uses.
        # No `force_style` needed since the ASS already carries the
        # resolved style block.
        if job.subtitles is not None and not is_audio_only and v_out:
            sub_source = self._resolve_subtitle_path(job.subtitles, tgt_w, tgt_h)
            sub_path = ff.escape_filter_arg(str(sub_source))
            parts.append(
                f"[{v_out}]subtitles='{sub_path}':"
                f"original_size={tgt_w}x{tgt_h}[v_sub]"
            )
            v_out = "v_sub"

        return ";".join(parts), v_out or "", a_out

    # --- run ----------------------------------------------------------

    def _execute(self, cmd: list[str]) -> None:
        job = self._job
        if job.region_start is not None and job.region_end is not None:
            total = max(0.01, job.region_end - job.region_start)
        else:
            total = max(0.01, job.total_timeline)
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            **_POPEN_KWARGS,
        )
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        stderr_lines: collections.deque[str] = collections.deque(maxlen=200)

        def _drain_stderr() -> None:
            for line in self._proc.stderr:
                line = line.rstrip()
                if line:
                    stderr_lines.append(line)
                    self.log.emit(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        for line in self._proc.stdout:
            if self._cancelled:
                self._proc.terminate()
                break
            line = line.strip()
            if not line:
                continue
            key, _, value = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
                t = int(value) / 1_000_000
                pct = min(1.0, max(0.0, t / total))
                self.progress.emit(int(pct * 100))
                self._update_eta(pct * 100)
            elif key == "progress" and value == "end":
                self.progress.emit(100)
                break

        rc = self._proc.wait()
        stderr_thread.join(timeout=5)
        if rc != 0 and not self._cancelled:
            err = "\n".join(stderr_lines).strip()
            raise RuntimeError(f"ffmpeg exited {rc}: {err[-600:]}")

    def _update_eta(self, overall_pct: float) -> None:
        if overall_pct < 2.0:
            return
        elapsed = time.monotonic() - self._started_wall
        if elapsed < 0.5:
            return
        eta_raw = max(0.0, elapsed * (100.0 - overall_pct) / overall_pct)
        if self._eta_smoothed is None:
            self._eta_smoothed = eta_raw
        else:
            alpha = 0.35
            self._eta_smoothed = alpha * eta_raw + (1 - alpha) * self._eta_smoothed
        self.eta.emit(self._eta_smoothed)


def resolve_target_size(
    clips: list[Clip],
    crop: tuple[int, int, int, int] | None,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    """Output size policy: honor crop, else an explicit width+height pair,
    else the first real (non-gap) visual clip, else 1280x720. Final
    dimensions are forced even so encoders accept them."""
    first_real = next((c for c in clips if c.asset.width > 0), None)
    if crop:
        _, _, tgt_w, tgt_h = crop
    elif width and height:
        tgt_w, tgt_h = width, height
    elif first_real:
        tgt_w, tgt_h = first_real.asset.width, first_real.asset.height
    else:
        tgt_w, tgt_h = 1280, 720
    return tgt_w - tgt_w % 2, tgt_h - tgt_h % 2


def _audio_only_duration(audio_tracks: list[AudioTrack]) -> float:
    """Timeline length for a no-video-clip audio-only export, derived from
    each added-audio track's placement (offset + span) — the same
    offset/duration semantics used to place tracks during mixing.

    With no video clips there is no timeline length to fall back on, so a
    track left at its ``duration <= 0`` ("use full input") default has no
    resolvable end time here — fail fast instead of guessing a length.
    """
    ends = []
    for t in audio_tracks:
        if t.duration <= 0:
            raise RuntimeError(
                "cannot determine audio-only export duration: added-audio "
                "track has no explicit duration and there are no video "
                "clips to derive the timeline length from"
            )
        ends.append(max(0.0, t.offset) + t.duration)
    return max(0.01, max(ends))


def _segments_with_gaps(clips: list[Clip], end: float) -> list[tuple[str, float, float, Clip | None]]:
    """Return [(kind, seg_start, seg_end, clip|None)] covering [0, end)."""
    out: list[tuple[str, float, float, Clip | None]] = []
    cursor = 0.0
    for c in sort_clips(clips):
        if c.timeline_start > cursor + 1e-3:
            out.append(("gap", cursor, c.timeline_start, None))
        out.append(("clip", c.timeline_start, c.timeline_end, c))
        cursor = c.timeline_end
    if end > cursor + 1e-3:
        out.append(("gap", cursor, end, None))
    return out


def _join_filter_labels(labels: list[str]) -> str:
    """Return ffmpeg link labels, accepting only generated filter labels."""
    for label in labels:
        if _FILTER_LABEL_RE.fullmatch(label) is None:
            raise RuntimeError(f"internal export error: invalid concat label {label!r}")
    return "".join(f"[{label}]" for label in labels)


def _render_ass(sub: SubtitleTrack, out_w: int, out_h: int) -> str:
    """Serialize a SubtitleTrack to a full ASS script with PlayRes matching
    the output video. Applies the sync offset and bakes in Fontname,
    Fontsize (output pixels), PrimaryColour, OutlineColour, Outline,
    Alignment, and a bottom/top MarginV sized to 6% of the video height
    — same safe margin the preview overlay uses."""
    primary = _hex_to_libass(sub.primary_color)
    outline_c = _hex_to_libass(sub.outline_color)
    alignment = 8 if sub.position == "top" else 2
    # ASS is comma-separated; strip anything that would break style parsing.
    font_name = (sub.font_family or "Arial").replace(",", " ").replace(":", " ")
    font_size = max(8, int(sub.font_size))
    outline_w = max(0, int(sub.outline))
    margin_v = max(4, int(round(out_h * 0.06)))

    # ASS Style format (libass reference):
    #   Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,
    #   OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,
    #   ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,
    #   Alignment, MarginL, MarginR, MarginV, Encoding
    style_fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    style_row = (
        f"Style: Default,{font_name},{font_size},{primary},&H000000FF,"
        f"{outline_c},&H00000000,-1,0,0,0,100,100,0,0,1,{outline_w},0,"
        f"{alignment},20,20,{margin_v},1"
    )

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        f"PlayResX: {out_w}",
        f"PlayResY: {out_h}",
        "",
        "[V4+ Styles]",
        style_fmt,
        style_row,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    offset_s = sub.offset_ms / 1000.0
    for start, end, text in sub.cues:
        s = max(0.0, start + offset_s)
        e = max(s + 0.01, end + offset_s)
        # `,` separates Dialogue fields — the Text field is last so commas
        # inside Text are fine. `{` / `}` toggle libass override codes so
        # we escape them to stop a stray `{` in the caption from eating
        # the rest of the line. `\N` is a hard break; SRT uses `\n`.
        txt = (
            text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}")
                .replace("\r", "")
                .replace("\n", "\\N")
        )
        lines.append(
            f"Dialogue: 0,{_format_ass_ts(s)},{_format_ass_ts(e)},Default,,0,0,0,,{txt}"
        )

    return "\n".join(lines) + "\n"


def _format_ass_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - (h * 3600 + m * 60)
    return f"{h}:{m:02d}:{s:05.2f}"


def _format_srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _hex_to_libass(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    # Alpha 00 = fully opaque in libass.
    return f"&H00{b.upper()}{g.upper()}{r.upper()}&"


def _atempo_chain(speed: float) -> str:
    s = max(0.01, speed)
    chain: list[float] = []
    while s < 0.5:
        chain.append(0.5)
        s /= 0.5
    while s > 2.0:
        chain.append(2.0)
        s /= 2.0
    chain.append(s)
    return ",atempo=".join(f"{v:.4f}" for v in chain)


def resolve_export_video_encoder(spec: dict, pref: str) -> str | None:
    """Map (format spec, user preference) onto one concrete video encoder.

    Audio-only formats have no video encoder at all and never trigger a
    hardware probe. "cpu" is honoured without probing either. "auto"
    prefers NVENC, then AMF, then the CPU encoder. An explicit hardware
    choice that is not genuinely usable on this machine falls back to the
    CPU encoder for the format - never to the other vendor - so a stale
    preference carried over from another machine still exports.
    """
    cpu_codec = spec["vcodec"]
    if cpu_codec is None:
        return None
    pref = ff.normalize_encoder_pref(pref)
    if pref == "cpu":
        return cpu_codec
    nvenc_codec = spec.get("nvenc_codec")
    amf_codec = spec.get("amf_codec")
    if pref == "nvenc":
        if nvenc_codec and ff.nvenc_available(nvenc_codec):
            return nvenc_codec
        return cpu_codec
    if pref == "amf":
        if amf_codec and ff.amf_available(amf_codec):
            return amf_codec
        return cpu_codec
    if nvenc_codec and ff.nvenc_available(nvenc_codec):
        return nvenc_codec
    if amf_codec and ff.amf_available(amf_codec):
        return amf_codec
    return cpu_codec


def start_export(job: ExportJob) -> tuple[QThread, ExportWorker]:
    thread = QThread()
    worker = ExportWorker(job)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker
