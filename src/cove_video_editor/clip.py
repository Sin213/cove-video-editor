from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtGui import QImage, QPixmap


@dataclass(slots=True)
class AddedAudio:
    """One audio file placed on one of the audio tracks.

    Multiple entries can coexist on either track; each has its own timeline
    `offset` and plays for its natural `duration` (no looping). `lane`
    controls which audio row it renders on: 0 = Audio Track 1 (sits next to
    clip audio), 1 = Audio Track 2 (dedicated overlay lane).

    `src_start` / `src_end` trim the audible portion within the file, like
    `Clip.src_start` / `Clip.src_end`. When both are 0 the full file plays.

    `volume` is a per-item linear gain (1.0 = 100%, 0.0 = silent, 2.0 = 200%)
    applied on top of the global added-audio gain.
    """
    path: Path
    duration: float = 0.0
    rate: int = 0
    peaks: list[float] = field(default_factory=list)
    offset: float = 0.0
    lane: int = 1
    src_start: float = 0.0
    src_end: float = 0.0
    volume: float = 1.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        if self.src_end <= 0:
            self.src_end = self.duration

    @property
    def src_span(self) -> float:
        return max(0.001, self.src_end - self.src_start)

    @property
    def timeline_length(self) -> float:
        return self.src_span

    @property
    def timeline_end(self) -> float:
        return self.offset + self.timeline_length

    def src_for_timeline(self, t_timeline: float) -> float:
        t = max(self.offset, min(self.timeline_end, t_timeline))
        return self.src_start + (t - self.offset)

    def clone(self) -> AddedAudio:
        a = AddedAudio(
            path=self.path, duration=self.duration, rate=self.rate,
            peaks=list(self.peaks), offset=self.offset, lane=self.lane,
            src_start=self.src_start, src_end=self.src_end,
            volume=self.volume,
        )
        a.id = self.id
        return a


@dataclass(slots=True)
class MediaAsset:
    """A file in the clip bin. Independent of any timeline placement.

    Image assets carry ``kind="image"``, ``duration`` as their default
    still-card display length (user-editable via the clip properties
    dialog), ``has_audio=False`` and ``fps=0``. Their ``thumb`` is the
    image itself scaled down.
    """
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    kind: str = "video"        # "video" | "audio" | "image"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    thumb: QImage | None = None


# Default duration (seconds) for a still-image clip when first dropped on
# the timeline. Users can stretch/trim it up to IMAGE_ASSET_DURATION_CAP
# via the properties dialog.
DEFAULT_IMAGE_DURATION = 5.0
# Upper bound on how long a single image clip can span — ffmpeg happily
# loops an image for hours but a single still card longer than ten
# minutes is probably user error.
IMAGE_ASSET_DURATION_CAP = 600.0


@dataclass(slots=True)
class SubtitleTrack:
    """Burn-in subtitle entry — a user-supplied SRT/VTT file plus the style
    applied when the exporter invokes ffmpeg's ``subtitles=`` filter.

    Styling maps 1:1 to libass ``force_style``. ``position="bottom"`` uses
    the libass default alignment (2); ``"top"`` uses alignment 8.

    ``cues`` is populated on import by ``parse_sub_cues`` and drives the
    live overlay on the preview view — the same data libass sees on export.
    """

    path: Path
    font_family: str = "Arial"
    font_size: int = 36
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline: int = 2
    position: str = "bottom"   # "bottom" or "top"
    active: bool = False
    # Manual/auto sync nudge (positive = cues show later). Applied to
    # both the live preview and the exported burn-in.
    offset_ms: int = 0
    # List of (start_seconds, end_seconds, text).
    cues: list[tuple[float, float, str]] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def clone(self) -> SubtitleTrack:
        s = SubtitleTrack(
            path=self.path, font_family=self.font_family, font_size=self.font_size,
            primary_color=self.primary_color, outline_color=self.outline_color,
            outline=self.outline, position=self.position, active=self.active,
            offset_ms=self.offset_ms, cues=list(self.cues),
        )
        s.id = self.id
        return s

    def cue_at(self, t: float) -> str:
        """Return the cue text(s) covering timeline time `t`.

        Multiple cues can overlap in an SRT (a second cue starts before
        the first one ends). libass stacks them on export, so the live
        preview joins them with a newline in file order to match — a
        single-line return would silently drop one of the stacked cues,
        which is why the editor showed one line while the exported file
        showed two."""
        shifted = t - self.offset_ms / 1000.0
        matches = [text for start, end, text in self.cues if start <= shifted < end]
        return "\n".join(matches)


# ---- SRT / VTT cue parsing ------------------------------------------------

import re as _re  # imported inside module; avoids a top-level `re` cost

_SUB_TS_RE = _re.compile(r"(\d+):(\d+):(\d+)[,\.](\d+)")
_SUB_RANGE_RE = _re.compile(
    r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)",
)
_SUB_TAG_RE = _re.compile(r"<[^>]+>|\{[^}]+\}")


def parse_sub_cues(path: Path) -> list[tuple[float, float, str]]:
    """Best-effort SRT/VTT cue parser. Returns a list of
    ``(start_seconds, end_seconds, text)``. Unknown formats / read errors
    produce an empty list so the rest of the app keeps working (ffmpeg
    still handles .ass / .ssa on export even when the live preview is
    empty)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Strip a WebVTT header (`WEBVTT\n\n`).
    if raw.startswith("WEBVTT"):
        parts = raw.split("\n\n", 1)
        raw = parts[1] if len(parts) > 1 else ""
    cues: list[tuple[float, float, str]] = []
    for block in _re.split(r"\n\n+", raw.strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        ts_idx = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if ts_idx < 0:
            continue
        m = _SUB_RANGE_RE.search(lines[ts_idx])
        if not m:
            continue
        start = _parse_sub_ts(m.group(1))
        end = _parse_sub_ts(m.group(2))
        if end <= start:
            continue
        text_lines = lines[ts_idx + 1:]
        text = "\n".join(text_lines).strip()
        text = _SUB_TAG_RE.sub("", text)
        if text:
            cues.append((start, end, text))
    return cues


def _parse_sub_ts(s: str) -> float:
    m = _SUB_TS_RE.match(s)
    if not m:
        return 0.0
    h, mm, ss, frac = m.group(1), m.group(2), m.group(3), m.group(4)
    ms = int(frac.ljust(3, "0")[:3])
    return int(h) * 3600 + int(mm) * 60 + int(ss) + ms / 1000.0


@dataclass(slots=True)
class Clip:
    """One clip placed on the timeline.

    `src_start` / `src_end` are in the asset's time domain (seconds).
    `timeline_start` is the absolute position on the sequence timeline.
    `speed` multiplies playback speed — 0.5 = half, 2.0 = double.
    """

    asset: MediaAsset
    timeline_start: float = 0.0
    src_start: float = 0.0
    src_end: float = 0.0
    speed: float = 1.0
    muted: bool = False
    audio_volume: float = 1.0
    linked_audio: bool = True
    audio_offset: float = 0.0
    # True when the user deleted the clip's audio track; the video stays on
    # the timeline but no waveform is shown and the audio is silent. The
    # chain chip toggles this back to False when the user wants to restore.
    audio_removed: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    thumbs: list[QImage] = field(default_factory=list)
    thumb_pixmaps: list[QPixmap] = field(default_factory=list)
    waveform_peaks: list[float] = field(default_factory=list)
    waveform_rate: int = 0

    def __post_init__(self) -> None:
        if self.src_end <= 0:
            self.src_end = self.asset.duration

    @property
    def path(self) -> Path:
        return self.asset.path

    @property
    def src_span(self) -> float:
        return max(0.001, self.src_end - self.src_start)

    @property
    def timeline_length(self) -> float:
        return self.src_span / max(0.01, self.speed)

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.timeline_length

    def clone(self) -> Clip:
        c = Clip(
            asset=self.asset, timeline_start=self.timeline_start,
            src_start=self.src_start, src_end=self.src_end, speed=self.speed,
            muted=self.muted, audio_volume=self.audio_volume,
            linked_audio=self.linked_audio,
            audio_offset=self.audio_offset, audio_removed=self.audio_removed,
        )
        c.thumbs = list(self.thumbs)
        c.thumb_pixmaps = list(self.thumb_pixmaps)
        c.waveform_peaks = self.waveform_peaks
        c.waveform_rate = self.waveform_rate
        return c

    def src_for_timeline(self, t_timeline: float) -> float:
        """Given a timeline second that falls within this clip, return the
        corresponding source second."""
        t = max(self.timeline_start, min(self.timeline_end, t_timeline))
        return self.src_start + (t - self.timeline_start) * self.speed


# ---- Region operations on the list of clips -------------------------------

def sort_clips(clips: list[Clip]) -> list[Clip]:
    return sorted(clips, key=lambda c: (c.timeline_start, c.id))


def sequence_length(clips: list[Clip]) -> float:
    if not clips:
        return 0.0
    return max(c.timeline_end for c in clips)


def clip_at_timeline(clips: list[Clip], t: float) -> Clip | None:
    for c in sort_clips(clips):
        if c.timeline_start <= t < c.timeline_end:
            return c
    # fall back to last clip if t is exactly at the end
    if clips:
        last = max(clips, key=lambda c: c.timeline_end)
        if abs(t - last.timeline_end) < 1e-3:
            return last
    return None


def split_clip(clip: Clip, t_timeline: float) -> Clip | None:
    """Split `clip` at timeline time `t`. Returns the new right-hand piece
    (the original is trimmed in place), or None if `t` is outside the clip."""
    if t_timeline <= clip.timeline_start + 0.01 or t_timeline >= clip.timeline_end - 0.01:
        return None
    src_t = clip.src_for_timeline(t_timeline)
    right = clip.clone()
    right.id = uuid.uuid4().hex[:8]
    right.src_start = src_t
    right.src_end = clip.src_end
    right.timeline_start = t_timeline
    clip.src_end = src_t
    return right


def split_added_audio(audio: AddedAudio, t_timeline: float) -> AddedAudio | None:
    """Split `audio` at timeline time `t`. Returns the new right-hand piece
    (the original is trimmed in place), or None if `t` is outside the clip."""
    if t_timeline <= audio.offset + 0.01 or t_timeline >= audio.timeline_end - 0.01:
        return None
    src_t = audio.src_for_timeline(t_timeline)
    right = audio.clone()
    right.id = uuid.uuid4().hex[:8]
    right.src_start = src_t
    right.src_end = audio.src_end
    right.offset = t_timeline
    audio.src_end = src_t
    return right


def delete_region(clips: list[Clip], start: float, end: float) -> list[Clip]:
    """Ripple-delete the [start, end) slice from the timeline.

    - Clips fully inside the region → dropped.
    - Clips partially overlapping → trimmed to the surviving side(s); clips
      spanning the whole region are split into two.
    - Clips after the region → shifted left by (end-start).
    """
    if end <= start:
        return clips
    dur = end - start
    out: list[Clip] = []
    for c in sort_clips(clips):
        cs, ce = c.timeline_start, c.timeline_end
        if ce <= start:
            out.append(c)
        elif cs >= end:
            c.timeline_start -= dur
            out.append(c)
        else:
            # some overlap
            if cs < start and ce > end:
                # split: keep left part, then right part shifted
                left = c
                right = left.clone()
                right.id = uuid.uuid4().hex[:8]
                # left keeps its timeline_start, trim its src_end to start
                left_src_end = left.src_for_timeline(start)
                # right starts at (end) on the timeline, but after shift by dur
                # we want it continuing from the original right segment
                right_src_start = left.src_for_timeline(end)
                left.src_end = left_src_end
                right.src_start = right_src_start
                right.timeline_start = start  # because everything after is shifted -dur; original right started at `end` → end-dur = start
                out.append(left)
                out.append(right)
            elif cs < start <= ce:
                # trim right side off
                c.src_end = c.src_for_timeline(start)
                out.append(c)
            elif cs < end <= ce:
                # trim left side off and shift
                c.src_start = c.src_for_timeline(end)
                c.timeline_start = start  # end - dur == start
                out.append(c)
            # fully inside → drop
    return sort_clips(out)


def keep_only_region(clips: list[Clip], start: float, end: float) -> list[Clip]:
    """Trim the sequence to only what's inside [start, end), shifting the
    kept content so it starts at t=0 on the timeline."""
    if end <= start:
        return clips
    out: list[Clip] = []
    for c in sort_clips(clips):
        cs, ce = c.timeline_start, c.timeline_end
        if ce <= start or cs >= end:
            continue
        new_src_start = c.src_start
        new_src_end = c.src_end
        new_ts = c.timeline_start
        if cs < start:
            new_src_start = c.src_for_timeline(start)
            new_ts = start
        if ce > end:
            new_src_end = c.src_for_timeline(end)
        c.src_start = new_src_start
        c.src_end = new_src_end
        c.timeline_start = new_ts - start
        out.append(c)
    return sort_clips(out)


def snap_to_next_boundary(clips: list[Clip], t: float, tolerance: float = 0.15) -> float:
    """Snap timeline time `t` to a nearby clip edge if within tolerance."""
    candidates = [0.0]
    for c in clips:
        candidates.append(c.timeline_start)
        candidates.append(c.timeline_end)
    best = t
    best_d = tolerance
    for cand in candidates:
        d = abs(cand - t)
        if d < best_d:
            best_d = d
            best = cand
    return best
