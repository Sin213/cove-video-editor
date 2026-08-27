from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


class FFmpegMissingError(RuntimeError):
    pass


if os.name == "nt":
    _CREATE_NO_WINDOW = 0x08000000
    _SUBPROCESS_KWARGS: dict = {"creationflags": _CREATE_NO_WINDOW}
else:
    _SUBPROCESS_KWARGS = {}


def _bundle_dirs() -> list[Path]:
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(Path(__file__).resolve().parent.parent.parent / "assets" / "bin")
    return dirs


def _find_binary(name: str) -> str | None:
    exe = f"{name}.exe" if os.name == "nt" else name
    for d in _bundle_dirs():
        candidate = d / exe
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def require_ffmpeg() -> str:
    path = _find_binary("ffmpeg")
    if not path:
        raise FFmpegMissingError("ffmpeg not found")
    return path


def require_ffprobe() -> str:
    path = _find_binary("ffprobe")
    if not path:
        raise FFmpegMissingError("ffprobe not found")
    return path


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(video: Path) -> VideoInfo:
    cmd = [
        require_ffprobe(),
        "-v", "error",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate:format=duration",
        "-of", "json",
        str(video),
    ]
    out = subprocess.check_output(cmd, text=True, **_SUBPROCESS_KWARGS)
    data = json.loads(out)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError("no video stream")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(data["format"]["duration"])
    num, den = v["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return VideoInfo(
        duration=duration,
        width=int(v["width"]),
        height=int(v["height"]),
        fps=fps,
        has_audio=has_audio,
    )


def probe_audio_duration(audio: Path) -> float:
    cmd = [
        require_ffprobe(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio),
    ]
    out = subprocess.check_output(cmd, text=True, **_SUBPROCESS_KWARGS).strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def extract_thumbnail(video: Path, time: float, out: Path, height: int = 80) -> None:
    cmd = [
        require_ffmpeg(),
        "-y",
        "-ss", f"{time:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale=-2:{height}",
        "-q:v", "5",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_SUBPROCESS_KWARGS)


def extract_frame_full(video: Path, time: float, out: Path, quality: int = 2) -> None:
    """Full-resolution single-frame extract (`quality` 2 is near-lossless JPEG)."""
    cmd = [
        require_ffmpeg(),
        "-y",
        "-ss", f"{time:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-q:v", str(quality),
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_SUBPROCESS_KWARGS)


# ---- Hardware encoder capability (NVENC / AMF) ----------------------------
#
# Being listed by `ffmpeg -encoders` is not evidence a hardware encoder can
# be used: a build can advertise `h264_nvenc` on a machine with no NVIDIA
# driver, no device, or no runtime library, and only fail when the export
# is already underway. So availability is decided by actually initializing
# the encoder on a tiny synthetic clip, and the answer is cached for the
# life of the process (GPU/driver state can change between launches, so it
# is never written to disk).

# Bounded so a wedged driver can never hang a caller. The listing is a
# pure text dump; the init probe encodes 0.2s of 320x240 black.
ENCODER_LIST_TIMEOUT = 10
ENCODER_PROBE_TIMEOUT = 20

# Internal preference domain, stable across releases and QSettings.
ENCODER_PREFS = ("auto", "cpu", "nvenc", "amf")
ENCODER_PREF_DEFAULT = "auto"

# UI labels, in display order. Index order matches ENCODER_PREFS.
ENCODER_OPTIONS = [
    "Automatic (GPU if available)",
    "CPU (x264 / x265)",
    "NVIDIA GPU (NVENC)",
    "AMD GPU (AMF)",
]
ENCODER_KEY_MAP = dict(zip(ENCODER_OPTIONS, ENCODER_PREFS))

# NVENC / AMF encoder names Cove can select, per family.
NVENC_ENCODERS = ("h264_nvenc", "hevc_nvenc")
AMF_ENCODERS = ("h264_amf", "hevc_amf")
# Every hardware encoder worth probing, in probe order.
HARDWARE_ENCODERS = NVENC_ENCODERS + AMF_ENCODERS


def build_export_video_encoder_args(
    encoder: str,
    fps: int | None = None,
) -> list[str]:
    """The video-encoder argument group only: codec + rate control (+ fps).

    Nothing here touches inputs, the filtergraph, scaling, crop, SAR, pixel
    format, audio or duration - those stay exactly as they were, which is
    what keeps a hardware export visually identical to a CPU one.

    Rate control per family, all constant-quality so the user-facing
    quality intent survives the encoder switch:
      * x264 / x265: the existing -crf / -preset medium pair.
      * NVENC: -rc vbr with -cq and -b:v 0 (CRF has no meaning there).
      * AMF: -rc cqp with per-frame-type QPs. AMF has no generic -qp
        option, so a bare -qp is silently dropped by ffmpeg ("AVOption qp
        ... has not been used for any stream") and the export falls back
        to encoder defaults; -qp_i / -qp_p (/ -qp_b on H.264) are the
        options AMF actually consumes in cqp mode.
      * anything else (vp9, mpeg4, gif): codec only, as before.

    This also builds the capability probe's encoder arguments, so a probe
    can never pass with settings the real export does not use.
    """
    args: list[str] = ["-c:v", encoder]
    if encoder == "libx264":
        args += ["-crf", "20", "-preset", "medium"]
    elif encoder == "libx265":
        args += ["-crf", "24", "-preset", "medium"]
    elif encoder == "h264_nvenc":
        args += ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "22", "-b:v", "0"]
    elif encoder == "hevc_nvenc":
        args += ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "26", "-b:v", "0"]
    elif encoder == "h264_amf":
        args += ["-quality", "balanced", "-usage", "transcoding", "-rc", "cqp",
                 "-qp_i", "23", "-qp_p", "23", "-qp_b", "23"]
    elif encoder == "hevc_amf":
        # hevc_amf exposes no B-frame QP.
        args += ["-quality", "balanced", "-usage", "transcoding", "-rc", "cqp",
                 "-qp_i", "27", "-qp_p", "27"]
    if fps:
        args += ["-r", str(fps)]
    return args

# One lock owns *all* probe lifecycle state: the current generation, the
# abort watermark, the registry of live probe children, the per-encoder
# coalescing lock table, and both caches. Lifecycle state is what decides
# whether a cache write is legal, so splitting the two across separate
# locks left a window in which a generation could be aborted between "this
# result is still wanted" and "this result is now process-wide knowledge".
# With one owner, eligibility and commit are a single transaction.
#
# It may only ever be held across cheap state moves - int/bool/dict/set
# work. Never a subprocess call, never `require_ffmpeg`, never Qt, and
# never a second lock. Nothing here needs re-entrancy; if it ever appears
# to, the critical section has grown something that does not belong in it.
_lifecycle_lock = threading.Lock()
_encoder_cache: dict[str, bool] = {}
_probe_locks: dict[str, threading.Lock] = {}
_encoder_listing: str | None = None

# Probe children currently running, so a shutting-down caller can end them
# instead of waiting out the timeout.
_active_probe_procs: set = set()
# Probing is owned by a generation: every probe captures the generation it
# started in, and an abort marks that generation aborted *permanently*.
# Re-arming activates a newer generation rather than un-aborting the old
# one, so a probe parked past the shutdown grace can never have its result
# adopted by the window that came after it.
#
# Generations only increase and an abort always targets the current one,
# so the aborted generations are a prefix: one watermark describes them
# exactly, and the bookkeeping cannot grow without bound.
_probe_generation = 0
_aborted_through = -1


def _generation_live(generation: int) -> bool:
    """May `generation` still publish results?

    The caller **must** already hold `_lifecycle_lock`. This takes no lock
    and calls nothing, so a commit can test eligibility and write in the
    same critical section.
    """
    return generation > _aborted_through


def abort_hardware_probes() -> None:
    """Kill every probe subprocess running now and abort this generation.

    `QThread.quit()` cannot interrupt a blocking wait on a child process,
    so the only way to make an in-flight probe return promptly at shutdown
    is to end the child. A killed probe simply reports "unavailable".
    """
    global _aborted_through
    # The watermark moves and the children are snapshotted together, then
    # the lock is dropped: killing a child is a process call and must never
    # happen while lifecycle state is held.
    with _lifecycle_lock:
        _aborted_through = max(_aborted_through, _probe_generation)
        procs = list(_active_probe_procs)
    for proc in procs:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - already gone is fine
            pass


def clear_hardware_probe_abort() -> None:
    """Re-arm probing for a new probe session.

    Starts a *newer* generation when the current one was aborted. The old
    generation stays aborted forever, so anything still finishing from it
    is discarded rather than adopted here. A clean generation is left
    alone so ordinary startup does not churn the counter.
    """
    global _probe_generation
    with _lifecycle_lock:
        if not _generation_live(_probe_generation):
            _probe_generation = _aborted_through + 1


def current_probe_generation() -> int:
    """Identity of the generation new probes will belong to."""
    with _lifecycle_lock:
        return _probe_generation


def hardware_probes_aborted(generation: int | None = None) -> bool:
    """Was `generation` (default: the current one) aborted?"""
    with _lifecycle_lock:
        gen = _probe_generation if generation is None else generation
        return not _generation_live(gen)


class _ProbeAborted(subprocess.SubprocessError):
    """Shutdown asked for an abort before this stage could start."""


def _run_probe(
    cmd: list[str], timeout: float, generation: int
) -> subprocess.CompletedProcess:
    """Run a probe command, bounded, and killable while it runs.

    Returns the same `CompletedProcess` shape `subprocess.run` would, and
    raises `TimeoutExpired` on the bound, so callers treat both paths
    exactly as before.
    """
    # Nothing new is worth starting once this probe's own generation has
    # been aborted - checked per generation, so a newer session re-arming
    # cannot resurrect an older one's remaining stages.
    if hardware_probes_aborted(generation):
        raise _ProbeAborted()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, **_SUBPROCESS_KWARGS,
    )
    # Registration and the abort check happen under the same lock, so a
    # child spawned in the window between the caller's cancellation check
    # and this registration is still killed: either `abort_hardware_probes`
    # sees it in the set, or this sees the flag it set.
    with _lifecycle_lock:
        _active_probe_procs.add(proc)
        aborted = not _generation_live(generation)
    if aborted:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - already gone is fine
            pass
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        with _lifecycle_lock:
            _active_probe_procs.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def normalize_encoder_pref(value: object) -> str:
    """Map any stored/passed preference onto the internal domain.

    Anything unknown - a stale QSettings value, a preference written by a
    newer build, a non-string - resolves to ``"auto"`` rather than raising.
    """
    if isinstance(value, str) and value in ENCODER_PREFS:
        return value
    return ENCODER_PREF_DEFAULT


def reset_hardware_encoder_cache() -> None:
    """Drop every cached capability result and generation state (tests).

    One critical section, so a test never observes a half-reset process.
    The live-child registry is deliberately *not* touched: production
    drains it on every path, and clearing it here would hide a leak rather
    than fix one. It also keeps this free of any process call, which the
    lifecycle lock does not permit.
    """
    global _encoder_listing, _probe_generation, _aborted_through
    with _lifecycle_lock:
        _encoder_cache.clear()
        _probe_locks.clear()
        _encoder_listing = None
        _probe_generation = 0
        _aborted_through = -1


def _cached_listing() -> str | None:
    """The shared encoder listing, or `None` when it was never fetched."""
    with _lifecycle_lock:
        return _encoder_listing


def _commit_listing(generation: int, listing: str) -> None:
    """Publish `listing` if `generation` may still speak for this process.

    Eligibility and the write are one critical section, so no abort can
    land between them. First valid writer wins: a redundant later probe
    cannot clobber a listing this process already settled on.
    """
    global _encoder_listing
    with _lifecycle_lock:
        if not _generation_live(generation):
            return
        if _encoder_listing is None:
            _encoder_listing = listing


def _cached_encoder(encoder: str) -> bool | None:
    """Cached availability for `encoder`, or `None` when never probed.

    `False` is a legitimate cached capability, so "not cached" needs its
    own value rather than being folded into it.
    """
    with _lifecycle_lock:
        return _encoder_cache.get(encoder)


def _commit_encoder_result(generation: int, encoder: str, result: bool) -> None:
    """Publish a probe result if `generation` is still live.

    This is the only place availability results become process-wide, and
    the test and the write share one critical section. An aborted probe
    still *answers* its caller "unavailable"; it just never writes that
    shutdown artifact down for anyone else.
    """
    with _lifecycle_lock:
        if not _generation_live(generation):
            return
        _encoder_cache[encoder] = result


def _coalescing_lock(encoder: str) -> threading.Lock:
    """The per-encoder lock that makes concurrent callers probe once.

    The lifecycle lock owns the *table*, never the locks inside it, and is
    released before the returned lock is taken. The only nesting that can
    exist is therefore coalescing -> lifecycle, never the reverse.
    """
    with _lifecycle_lock:
        return _probe_locks.setdefault(encoder, threading.Lock())


def _ffmpeg_encoder_listing(bin_path: str, generation: int) -> str:
    """`ffmpeg -encoders` output, fetched at most once per process."""
    cached = _cached_listing()
    if cached is not None:
        return cached
    try:
        proc = _run_probe(
            [bin_path, "-hide_banner", "-encoders"], ENCODER_LIST_TIMEOUT,
            generation,
        )
        listing = proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        listing = ""
    # Same reasoning as the capability cache: an empty listing produced by
    # an aborted generation must not become this process's answer. The
    # fetch happened outside the lifecycle lock; only the commit takes it.
    _commit_listing(generation, listing)
    return listing


def _probe_hardware_encoder(encoder: str, generation: int) -> bool:
    """Actually initialize `encoder` on a synthetic source, bounded.

    The probe uses the *same* encoder arguments the real export builds, so
    a build that initializes the encoder but rejects one of Cove's rate
    control options is reported unavailable here rather than failing
    mid-export.

    Produces no user-visible file (null muxer to the platform null device)
    and reads no project media. Any failure - missing binary, missing
    driver, timeout, non-zero exit - is an unavailable answer, never an
    exception.
    """
    try:
        bin_path = require_ffmpeg()
    except Exception:  # noqa: BLE001 - a missing binary is "unavailable"
        return False
    if encoder not in _ffmpeg_encoder_listing(bin_path, generation):
        return False
    try:
        proc = _run_probe(
            [bin_path, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=0.2"]
            + build_export_video_encoder_args(encoder)
            + ["-f", "null", os.devnull],
            ENCODER_PROBE_TIMEOUT,
            generation,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def hardware_encoder_available(encoder: str) -> bool:
    """Cached answer to "can this machine really encode with `encoder`?".

    Double-checked locking with a per-encoder lock: concurrent callers for
    the same encoder run the process work exactly once and the loser waits
    for the result instead of launching a second ffmpeg. Different
    encoders never block each other, and a negative answer is cached just
    like a positive one.

    The probe is owned by the generation it started in. Only a result from
    a generation that was never aborted may be cached, so a probe left over
    from a closed window cannot hand its shutdown artifact to the window
    that re-armed probing after it.
    """
    cached = _cached_encoder(encoder)
    if cached is not None:
        return cached
    with _coalescing_lock(encoder):
        cached = _cached_encoder(encoder)
        if cached is not None:
            return cached
        generation = current_probe_generation()
        # The probe runs with no lifecycle state held - a 20s ffmpeg bound
        # must never become a 20s lifecycle stall - and only the commit
        # re-enters, where eligibility and the write are indivisible.
        result = _probe_hardware_encoder(encoder, generation)
        _commit_encoder_result(generation, encoder, result)
        return result


def nvenc_available(encoder: str = "h264_nvenc") -> bool:
    return hardware_encoder_available(encoder)


def amf_available(encoder: str = "h264_amf") -> bool:
    return hardware_encoder_available(encoder)


# ---- Export pipeline ------------------------------------------------------

# Output containers + codec choices. Each entry is:
#   (display name, file extension, video codec, audio codec, extra args)
# "copy" is used only when no filters touch the stream (fast path).
# `nvenc_codec` / `amf_codec` name the hardware equivalent of `vcodec` for
# the formats that have one; a format without them always encodes on the
# CPU no matter what the user picked.
EXPORT_FORMATS: dict[str, dict] = {
    "MP4 (H.264 + AAC)":   {"ext": "mp4",  "vcodec": "libx264",  "acodec": "aac",         "nvenc_codec": "h264_nvenc", "amf_codec": "h264_amf", "extra": ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]},
    "MP4 (H.265 + AAC)":   {"ext": "mp4",  "vcodec": "libx265",  "acodec": "aac",         "nvenc_codec": "hevc_nvenc", "amf_codec": "hevc_amf", "extra": ["-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-movflags", "+faststart"]},
    "MKV (H.264 + AAC)":   {"ext": "mkv",  "vcodec": "libx264",  "acodec": "aac",         "nvenc_codec": "h264_nvenc", "amf_codec": "h264_amf", "extra": ["-pix_fmt", "yuv420p"]},
    "WebM (VP9 + Opus)":   {"ext": "webm", "vcodec": "libvpx-vp9", "acodec": "libopus",   "extra": ["-b:v", "0", "-crf", "32", "-row-mt", "1"]},
    "MOV (H.264 + AAC)":   {"ext": "mov",  "vcodec": "libx264",  "acodec": "aac",         "nvenc_codec": "h264_nvenc", "amf_codec": "h264_amf", "extra": ["-pix_fmt", "yuv420p"]},
    "AVI (MPEG-4 + MP3)":  {"ext": "avi",  "vcodec": "mpeg4",    "acodec": "libmp3lame",  "extra": ["-qscale:v", "4", "-ar", "44100", "-ac", "2"]},
    "GIF (animation)":     {"ext": "gif",  "vcodec": "gif",      "acodec": None,           "extra": []},
    "MP3 (audio only)":    {"ext": "mp3",  "vcodec": None,       "acodec": "libmp3lame",   "extra": ["-q:a", "2"]},
    "WAV (audio only)":    {"ext": "wav",  "vcodec": None,       "acodec": "pcm_s16le",    "extra": []},
    "Opus (audio only)":   {"ext": "opus", "vcodec": None,       "acodec": "libopus",      "extra": ["-b:a", "128k"]},
    "FLAC (audio only)":   {"ext": "flac", "vcodec": None,       "acodec": "flac",         "extra": []},
    "OGG (audio only)":    {"ext": "ogg",  "vcodec": None,       "acodec": "libvorbis",    "extra": ["-q:a", "5"]},
    "AAC (audio only)":    {"ext": "m4a",  "vcodec": None,       "acodec": "aac",          "extra": ["-b:a", "192k", "-movflags", "+faststart"]},
}


def escape_filter_arg(value: str) -> str:
    # Escape characters special to ffmpeg's filtergraph parser for filenames
    # passed as filter options (e.g. the subtitles= source path).
    return (
        value.replace("\\", "\\\\")
             .replace(":", "\\:")
             .replace("'", "\\\\'")
             .replace(",", "\\,")
    )


