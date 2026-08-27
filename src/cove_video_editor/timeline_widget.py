"""Multi-track NLE timeline widget.

Layout (top to bottom):
    Ruler            — time ticks + labels, scrubbable playhead
    Video track      — clip blocks (thumbnails), trim handles, drag-to-move
    Audio track      — waveform strip (drawn from each clip's ``waveform`` pixmap)

Interaction:
    - Click on ruler → move playhead
    - Click-drag on ruler (or empty area) → select a time region
    - Click on a clip → select it; drag → move it along the timeline
    - Drag the left/right edge of the selected clip → trim that side
    - Right-click on a selection → context menu (Delete / Crop-to-selection /
      Export)
    - Mouse wheel on the ruler → zoom in/out around cursor
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import (
    QEvent, QPoint, QPointF, QRect, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import QMenu, QWidget

from . import theme
from .clip import AddedAudio, Clip, sequence_length, sort_clips
from .clip_bin import ASSET_MIME


RULER_H = 24
TRACK_GAP = 10
VIDEO_TRACK_H = 70
# Lane 0 is Audio Track 1 — shares timing with the video track's embedded
# audio. Lane N>=1 are extra "added-audio" lanes (sound FX, music, VO).
# Lane count is now unbounded; these are just the seed heights.
AUDIO_LANE_DEFAULT_H = 48
AUDIO_LANE_0_H = 56
VIDEO_TRACK_H_MIN = 40
VIDEO_TRACK_H_MAX = 260
AUDIO_TRACK_H_MIN = 36
AUDIO_TRACK_H_MAX = 220
LEFT_PAD = 8
RIGHT_PAD = 8
HANDLE_W = 8
MIN_PPS = 5.0
MAX_PPS = 800.0
# Trailing breathing room past the end of the content, in seconds. The
# scroll range has always granted this, so Zoom to Fit fits it too rather
# than inventing a second, different margin.
SCROLL_TAIL_PAD_S = 2.0
CLICK_SLOP_PX = 5
CHAIN_BTN_W = 22
CHAIN_BTN_H = 18
CHAIN_BTN_MARGIN = 4

# ---- edge auto-scroll ------------------------------------------------------
# Holding a time-axis drag inside a narrow band at either end of the visible
# track pans the viewport, so a long move no longer needs drop / scroll / drag
# again. Values are per-tick pixel amounts at EDGE_SCROLL_INTERVAL_MS.
EDGE_SCROLL_ZONE_PX = 44
EDGE_SCROLL_INTERVAL_MS = 16
EDGE_SCROLL_MIN_PX = 2
EDGE_SCROLL_MAX_PX = 24


def edge_scroll_delta(rel_x: int, viewport_width: int) -> int:
    """Signed horizontal scroll for one tick, or 0 outside the edge zones.

    ``rel_x`` is the pointer x *relative to the left of the visible track*, so
    it may go negative (or past ``viewport_width``) while the drag still holds
    the mouse grab. Speed ramps linearly from ``EDGE_SCROLL_MIN_PX`` at the
    inner lip of the zone to ``EDGE_SCROLL_MAX_PX`` at the viewport edge and is
    capped there, so a pointer parked far outside cannot run away.

    On a viewport too narrow for two full zones each zone shrinks to half the
    width, which keeps the two branches mutually exclusive.
    """
    if viewport_width < 2:
        return 0
    zone = min(EDGE_SCROLL_ZONE_PX, viewport_width // 2)
    if zone <= 0:
        return 0
    if rel_x < zone:
        direction, penetration = -1, zone - rel_x
    elif rel_x > viewport_width - zone:
        direction, penetration = 1, rel_x - (viewport_width - zone)
    else:
        return 0
    frac = min(1.0, max(0.0, penetration / zone))
    speed = EDGE_SCROLL_MIN_PX + (EDGE_SCROLL_MAX_PX - EDGE_SCROLL_MIN_PX) * frac
    return direction * int(round(speed))


# ---- added-audio volume / mute badge ---------------------------------------
# A tile only earns a badge when its audio state is *not* the default, so an
# untouched timeline stays clean. Volumes within VOLUME_BADGE_EPS of 100% are
# treated as default: the volume dialog works in whole percent, and rounding
# noise must not produce a permanent "100%" sticker.
VOLUME_BADGE_EPS = 0.01
BADGE_H = 14
BADGE_PAD_X = 8
BADGE_MARGIN = 4
BADGE_BG_ALPHA = 210


def added_audio_volume_badge(audio: AddedAudio) -> str | None:
    """The badge text for one added-audio item, or None when it is at its
    default state. An explicit mute wins over the stored volume, which is
    what keeps `volume == 0.0` (``"0%"``) readable as a distinct state from
    an actual mute (``"Muted"``)."""
    if audio.muted:
        return "Muted"
    if abs(audio.volume - 1.0) > VOLUME_BADGE_EPS:
        return f"{int(round(audio.volume * 100))}%"
    return None


@dataclass
class _Drag:
    mode: str = ""                    # "seek", "select", "move_clip", "trim_l", "trim_r", "move_audio", "resize_tracks", "trim_added_l", "trim_added_r"
    clip_id: str = ""
    grab_offset_s: float = 0.0        # clip drag: t_clip_start at grab time
    original_clip_start: float = 0.0
    anchor_t: float = 0.0             # region-select anchor (seconds)
    press_x: int = 0                  # px position of the mouse press
    press_y: int = 0
    start_video_h: int = 0
    start_audio_h: int = 0
    moved: bool = False               # flips true once we exceed CLICK_SLOP_PX


class TimelineWidget(QWidget):
    rangeChanged = Signal(str, float, float)            # clip id, src_start, src_end
    clipMoved = Signal(str, float)                      # clip id, new timeline_start
    clipSelected = Signal(str)                          # clip id ("" = none)
    playheadMoved = Signal(float)                       # timeline seconds
    selectionChanged = Signal(float, float)             # selection start/end (seconds)
    regionDeleteRequested = Signal(float, float)
    regionCropRequested = Signal(float, float)
    regionExportRequested = Signal(float, float)
    splitAtPlayheadRequested = Signal()
    addedAudioDropped = Signal(str, float, int)           # path, drop_t, lane
    videoFileDropped = Signal(str, float)                 # OS video file, drop_t
    assetDroppedOnTimeline = Signal(str, float, int)      # asset id, drop_t, lane
    addedAudioReplaceToggled = Signal(bool)
    addedAudioOffsetChanged = Signal(str, float)          # audio id, offset
    addedAudioDeleteRequested = Signal(str)               # audio id ("" → clear all)
    addedAudioRangeChanged = Signal(str)                  # audio id — src_start/src_end edited
    addedAudioVolumeRequested = Signal(str)               # audio id — edit per-item volume
    addedAudioMuteRequested = Signal(str)                 # audio id - toggle per-item mute
    clipDoubleClicked = Signal(str)               # clip id
    audioLinkToggled = Signal(str)                # clip id
    clipDeleteRequested = Signal(str)             # clip id
    clipMoveToStartRequested = Signal(str)        # clip id - ripple to t=0
    clipMoveToPlayheadRequested = Signal(str)     # clip id - ripple to playhead
    clipCloseGapLeftRequested = Signal(str)       # clip id - close the gap in front
    clipRippleDeleteRequested = Signal(str)       # clip id - delete + close its span
    audioOffsetChanged = Signal(str, float)       # clip id, new audio_offset (seconds)
    clipAudioRemoveRequested = Signal(str)        # clip id — delete clip's audio only
    scrollRangeChanged = Signal(int, int)         # scroll_max, page (in px)
    scrollValueChanged = Signal(int)              # current scroll_x (in px)
    pixelsPerSecondChanged = Signal(float)        # emitted when zoom level changes

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self._video_h: int = VIDEO_TRACK_H
        # Dynamic list of audio lane heights. Index 0 = Audio Track 1 (shares
        # timing with clip audio); subsequent entries are extra overlay lanes.
        # Starts with two lanes to preserve the 1.x default layout.
        self._audio_lane_heights: list[int] = [AUDIO_LANE_0_H, AUDIO_LANE_DEFAULT_H]
        self._refresh_min_height()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)

        self._clips: list[Clip] = []
        self._selected_id: str = ""
        self._selected_audio_clip_id: str = ""
        self._playhead: float = 0.0
        self._sel_start: float = 0.0
        self._sel_end: float = 0.0
        self._pps: float = 40.0           # pixels per second
        self._scroll_x: int = 0           # horizontal scroll offset in px
        self._drag = _Drag()
        self._added_audios: list[AddedAudio] = []
        self._added_audio_replace: bool = False
        self._added_audio_selected_id: str = ""

        # Edge auto-scroll: one timer for the widget's whole lifetime, one
        # connection, and the last *widget-local* pointer position so a tick
        # can replay the active drag against the panned viewport.
        self._drag_pos: QPoint | None = None
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(EDGE_SCROLL_INTERVAL_MS)
        self._autoscroll_timer.timeout.connect(self._edge_autoscroll_tick)

    # ---- audio-lane management ----------------------------------------

    def num_audio_lanes(self) -> int:
        return len(self._audio_lane_heights)

    def add_audio_lane(self) -> int:
        """Append a new empty audio lane; return its index."""
        self._audio_lane_heights.append(AUDIO_LANE_DEFAULT_H)
        self._refresh_min_height()
        self.update()
        return len(self._audio_lane_heights) - 1

    def remove_audio_lane(self, lane: int) -> bool:
        """Drop lane `lane` if it's not lane 0 and holds no added audio."""
        if lane <= 0 or lane >= len(self._audio_lane_heights):
            return False
        if any(a.lane == lane for a in self._added_audios):
            return False
        # Slide everything above down one slot so lane indices stay packed.
        self._audio_lane_heights.pop(lane)
        for a in self._added_audios:
            if a.lane > lane:
                a.lane -= 1
        # Restore the invariant that there's still one empty trailing lane.
        self._maintain_trailing_empty_lane()
        self.update()
        return True

    def _refresh_min_height(self) -> None:
        audio_total = sum(self._audio_lane_heights) + TRACK_GAP * len(self._audio_lane_heights)
        total = (
            RULER_H + TRACK_GAP + self._video_h + audio_total + 4
        )
        self.setMinimumHeight(total)

    # ---- public API ---------------------------------------------------

    def set_clips(self, clips: list[Clip]) -> None:
        self._clips = sort_clips(clips)
        if self._selected_id and not any(c.id == self._selected_id for c in self._clips):
            self._selected_id = ""
        self._publish_scroll_range()
        self.update()

    def selected_id(self) -> str:
        return self._selected_id

    def selection(self) -> tuple[float, float]:
        if self._sel_end <= self._sel_start:
            return (0.0, 0.0)
        return (self._sel_start, self._sel_end)

    def clear_selection(self) -> None:
        if self._sel_end > self._sel_start:
            self._set_selection_span(0.0, 0.0)
            self.selectionChanged.emit(0.0, 0.0)
            self.update()

    def playhead(self) -> float:
        return self._playhead

    def set_playhead(self, t: float, emit: bool = True) -> None:
        self._playhead = max(0.0, min(self._total_length(), t))
        if emit:
            self.playheadMoved.emit(self._playhead)
        self._ensure_visible(self._playhead)
        self.update()

    def select_clip(self, clip_id: str) -> None:
        if clip_id != self._selected_id:
            self._selected_id = clip_id
            if clip_id:
                self._added_audio_selected_id = ""
                self._selected_audio_clip_id = ""
            self.clipSelected.emit(clip_id)
            self.update()

    def set_pixels_per_second(self, pps: float) -> None:
        new_pps = max(MIN_PPS, min(MAX_PPS, pps))
        if abs(new_pps - self._pps) < 1e-3:
            return
        self._pps = new_pps
        self.pixelsPerSecondChanged.emit(self._pps)
        self._publish_scroll_range()
        self.update()

    def pixels_per_second(self) -> float:
        return self._pps

    def zoom_to_fit(self) -> None:
        """Size the zoom so the whole timeline extent fits the visible track
        width, then send the viewport back to the start.

        An orientation/recovery command for long edits, so it only ever
        zooms out: a sequence already narrower than the viewport keeps the
        user's current zoom instead of being blown up to fill the space.
        `_total_length()` stays the single authoritative definition of the
        extent, so added audio running past the last clip is included, and
        the scroll write goes through the Tab 2A clamp.
        """
        target = fit_pixels_per_second(
            self._total_length(), self._track_rect().width(), self._pps,
        )
        if target is not None:
            self.set_pixels_per_second(target)
        self.set_scroll_x(0)
        self._publish_scroll_range()
        self.update()

    # Expose zoom bounds so the app's zoom bar maps to the same range.
    PPS_MIN = MIN_PPS
    PPS_MAX = MAX_PPS

    def set_added_audios(self, audios: list[AddedAudio]) -> None:
        """Replace the list of added-audio entries. The timeline clones each
        entry so later edits on the app side don't silently affect painting.

        Lanes are grown / trimmed so there's always exactly one empty lane
        at the bottom — drop into it and a fresh empty one slides in.
        """
        self._added_audios = [a.clone() for a in audios]
        self._maintain_trailing_empty_lane()
        if self._added_audio_selected_id and not any(
            a.id == self._added_audio_selected_id for a in self._added_audios
        ):
            self._added_audio_selected_id = ""
        # Added audio counts towards `_total_length()`, so removing or
        # shortening the longest item shrinks the scrollable range.
        self._publish_scroll_range()
        self.update()

    def _maintain_trailing_empty_lane(self) -> None:
        """Ensure the audio-lane stack is ``[lane 0 (clip audio)] + [used
        extra lanes] + [exactly one empty trailing lane]``.

        Lanes beyond the highest-used one collapse to a single empty lane;
        if the highest-used lane is already the last lane, a fresh empty
        one gets appended so the user always has a drop target visible."""
        if self._added_audios:
            highest_used = max(a.lane for a in self._added_audios)
        else:
            highest_used = 0
        # We always want at least lane 0 + one empty lane for drops. If the
        # user is actively using lane N, we want N+1 trailing empty on top
        # of the N+1 occupied lanes.
        required = max(2, highest_used + 2)
        while len(self._audio_lane_heights) < required:
            self._audio_lane_heights.append(AUDIO_LANE_DEFAULT_H)
        # Collapse trailing empties beyond the one we need.
        while len(self._audio_lane_heights) > required:
            last = len(self._audio_lane_heights) - 1
            if any(a.lane == last for a in self._added_audios):
                break
            self._audio_lane_heights.pop()
        self._refresh_min_height()

    def added_audios(self) -> list[AddedAudio]:
        return list(self._added_audios)

    def set_added_audio_replace(self, replace: bool) -> None:
        self._added_audio_replace = bool(replace)

    def set_selection_range(self, start: float, end: float) -> None:
        self._set_selection_span(start, end)
        self.selectionChanged.emit(self._sel_start, self._sel_end)
        self.update()

    def _set_selection_span(self, start: float, end: float) -> None:
        """Write the selection span and republish the scroll range.

        Selection endpoints feed `_total_length()`, so a region dragged past
        the content extends the scrollable range and collapsing it shrinks
        the range again. Routing every write through here keeps
        `_scroll_x` inside `[0, scroll_max_px()]` without a clamp in each
        mouse handler."""
        self._sel_start = max(0.0, min(start, end))
        self._sel_end = max(self._sel_start, end)
        self._publish_scroll_range()

    def set_scroll_x(self, x: int) -> None:
        self._scroll_x = self._clamped_scroll_x(x)
        self.update()

    def _clamped_scroll_x(self, x: int) -> int:
        """Fold a candidate offset into the only legal range,
        ``0 <= scroll <= scroll_max_px()``. Single source of truth for the
        upper bound so no caller has to recompute the track width."""
        return max(0, min(int(x), self.scroll_max_px()))

    def scroll_max_px(self) -> int:
        total = max(self._total_length() + SCROLL_TAIL_PAD_S, 0.1)
        track_w = max(1, self._track_rect().width())
        max_x = max(0, int(total * self._pps) - track_w)
        return max_x

    # ---- geometry -----------------------------------------------------

    def _total_length(self) -> float:
        added_end = max(
            (a.timeline_end for a in self._added_audios),
            default=0.0,
        )
        return max(
            sequence_length(self._clips), added_end,
            self._playhead, self._sel_end,
        )

    def _track_rect(self) -> QRect:
        return QRect(LEFT_PAD, 0, max(1, self.width() - LEFT_PAD - RIGHT_PAD),
                     self.height())

    def _ruler_rect(self) -> QRect:
        tr = self._track_rect()
        return QRect(tr.left(), 0, tr.width(), RULER_H)

    def _video_rect(self) -> QRect:
        tr = self._track_rect()
        return QRect(tr.left(), RULER_H + TRACK_GAP, tr.width(), self._video_h)

    def _audio_lane_rect(self, lane: int) -> QRect:
        """Rect for audio lane `lane` (0-indexed)."""
        tr = self._track_rect()
        top = RULER_H + TRACK_GAP + self._video_h + TRACK_GAP
        for i in range(lane):
            top += self._audio_lane_heights[i] + TRACK_GAP
        h = self._audio_lane_heights[lane] if 0 <= lane < len(self._audio_lane_heights) else AUDIO_LANE_DEFAULT_H
        return QRect(tr.left(), top, tr.width(), h)

    def _audio_rect(self) -> QRect:
        """Shortcut for lane 0 — kept as a property used by many callers."""
        return self._audio_lane_rect(0)

    def _last_audio_rect(self) -> QRect:
        return self._audio_lane_rect(max(0, len(self._audio_lane_heights) - 1))

    def _va_divider_rect(self) -> QRect:
        """Drag handle between the video and lane 0."""
        vr = self._video_rect()
        return QRect(vr.left(), vr.bottom() + 1, vr.width(), TRACK_GAP - 2)

    def _time_to_x(self, t: float) -> int:
        return int(LEFT_PAD + t * self._pps - self._scroll_x)

    def _x_to_time(self, x: int) -> float:
        return max(0.0, (x - LEFT_PAD + self._scroll_x) / max(0.01, self._pps))

    def _clip_rect(self, c: Clip, track: QRect) -> QRect:
        x0 = self._time_to_x(c.timeline_start)
        x1 = self._time_to_x(c.timeline_end)
        return QRect(x0, track.top(), max(2, x1 - x0), track.height())

    def _ensure_visible(self, t: float) -> None:
        x = self._time_to_x(t)
        tr = self._track_rect()
        if x < tr.left() + 16:
            self._scroll_x = max(0, int((t * self._pps) - 16))
        elif x > tr.right() - 16:
            self._scroll_x = int((t * self._pps) - tr.width() + 16)
        self._publish_scroll_range()

    def _publish_scroll_range(self) -> None:
        # Content shrinks behind the viewport on trim / delete / undo / ripple
        # move, which can leave `_scroll_x` past the new end. Re-clamp here,
        # since every model republish and geometry change funnels through this
        # method, so the beginning of the timeline is reachable again without
        # the user first having to wheel or drag the view back into range.
        self._scroll_x = self._clamped_scroll_x(self._scroll_x)
        self.scrollRangeChanged.emit(self.scroll_max_px(), self._track_rect().width())
        self.scrollValueChanged.emit(self._scroll_x)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._publish_scroll_range()

    # ---- painting -----------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.fillRect(self.rect(), theme.C_RULER_BG)

        rr = self._ruler_rect()
        vr = self._video_rect()
        last_r = self._last_audio_rect()

        self._paint_ruler(p, rr)
        self._paint_video_track(p, vr)
        # Lane 0 renders clip-audio blocks + lane-0 added audio. Higher lanes
        # are pure overlay lanes.
        self._paint_audio_track(p, self._audio_lane_rect(0))
        for lane in range(1, len(self._audio_lane_heights)):
            self._paint_added_audio_track(p, self._audio_lane_rect(lane), lane)
        self._paint_chain_chips(p, vr)
        self._paint_divider(p)

        # Selection band drawn ON TOP so it tints the track content.
        if self._sel_end > self._sel_start:
            sx = self._time_to_x(self._sel_start)
            ex = self._time_to_x(self._sel_end)
            band = QRect(sx, rr.top(), max(1, ex - sx), last_r.bottom() - rr.top())
            # Teal selection tint, matches the accent scheme.
            p.fillRect(band, QColor(94, 234, 212, 40))
            pen = QPen(QColor(94, 234, 212, 210))
            pen.setWidth(2)
            p.setPen(pen)
            p.drawLine(sx, rr.top(), sx, last_r.bottom())
            p.drawLine(ex, rr.top(), ex, last_r.bottom())

        # playhead on top
        ph_x = self._time_to_x(self._playhead)
        if rr.left() - 4 <= ph_x <= rr.right() + 4:
            # Soft teal glow behind the playhead for the design's look.
            glow = QColor(94, 234, 212, 70)
            p.setPen(QPen(glow, 3))
            p.drawLine(ph_x, rr.top(), ph_x, last_r.bottom())
            p.setPen(QPen(theme.C_ACCENT, 1))
            p.drawLine(ph_x, rr.top(), ph_x, last_r.bottom())
            p.setBrush(theme.C_ACCENT)
            p.setPen(Qt.NoPen)
            p.drawPolygon([
                QPoint(ph_x - 6, rr.top()),
                QPoint(ph_x + 6, rr.top()),
                QPoint(ph_x,     rr.top() + 10),
            ])

        p.end()

    def _paint_ruler(self, p: QPainter, rr: QRect) -> None:
        p.fillRect(rr, theme.C_RULER_BG)
        p.setPen(theme.C_BORDER)
        p.drawLine(rr.bottomLeft(), rr.bottomRight())

        step = _choose_tick_step(self._pps)
        total = max(1.0, self._total_length() + 2.0)
        f = p.font(); f.setPointSize(8); p.setFont(f)
        t = 0.0
        while t < total + step:
            x = self._time_to_x(t)
            if rr.left() - 2 <= x <= rr.right() + 2:
                p.setPen(theme.C_BORDER_HI)
                p.drawLine(x, rr.bottom() - 6, x, rr.bottom())
                p.setPen(theme.C_TEXT_3)
                p.drawText(QPoint(x + 3, rr.bottom() - 6), _fmt_tick(t))
            t += step

    def _paint_video_track(self, p: QPainter, vr: QRect) -> None:
        p.fillRect(vr, theme.C_TRACK_BG)
        if not self._clips:
            p.setPen(theme.C_TEXT_3)
            f = p.font(); f.setItalic(True); p.setFont(f)
            p.drawText(
                vr, Qt.AlignCenter,
                "Drag and drop video or image clips here",
            )
            f.setItalic(False); p.setFont(f)
            return
        for c in self._clips:
            r = self._clip_rect(c, vr)
            if r.right() < vr.left() or r.left() > vr.right():
                continue
            self._paint_clip_video_body(p, c, r)

    def _paint_clip_video_body(self, p: QPainter, c: Clip, r: QRect) -> None:
        clip_rect = r.intersected(self._video_rect())
        if clip_rect.isEmpty():
            return

        # Background — dim teal-blue matching the design's video clip gradient.
        p.fillRect(clip_rect, theme.C_VIDEO_CLIP_B)

        # Aspect-correct thumb tiling. The slot fractions are mapped into
        # [src_start/dur, src_end/dur] so trimmed clips show the frames that
        # are actually in the clip, not frames from outside the trim window.
        if c.thumb_pixmaps:
            aspect = c.asset.width / max(1, c.asset.height)
            thumb_w = max(30, int(round(r.height() * aspect)))
            slots = max(1, r.width() // thumb_w)
            tile_w = r.width() / slots
            dur = max(0.001, c.asset.duration)
            start_frac = max(0.0, min(1.0, c.src_start / dur))
            end_frac = max(start_frac, min(1.0, c.src_end / dur))
            n = len(c.thumb_pixmaps)
            for i in range(slots):
                tgt = QRect(
                    int(round(r.left() + i * tile_w)), r.top(),
                    int(round(tile_w)) + 1, r.height(),
                )
                vis = tgt.intersected(clip_rect)
                if vis.isEmpty():
                    continue
                local_frac = (i + 0.5) / slots
                src_frac = start_frac + local_frac * (end_frac - start_frac)
                idx = min(n - 1, max(0, int(src_frac * n)))
                thumb = c.thumb_pixmaps[idx]
                scaled = thumb.scaled(
                    tgt.width(), tgt.height(),
                    Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                )
                sx = max(0, (scaled.width() - tgt.width()) // 2) + (vis.left() - tgt.left())
                sy = max(0, (scaled.height() - tgt.height()) // 2)
                p.drawPixmap(
                    vis, scaled,
                    QRect(sx, sy, vis.width(), vis.height()),
                )
        elif c.asset.thumb is not None and not c.asset.thumb.isNull():
            # Thumb worker hasn't finished yet — show the single asset thumb
            # stretched across the clip so it's obvious the clip is there.
            pm = QPixmap.fromImage(c.asset.thumb)
            scaled = pm.scaled(
                clip_rect.width(), clip_rect.height(),
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
            )
            sx = max(0, (scaled.width() - clip_rect.width()) // 2)
            sy = max(0, (scaled.height() - clip_rect.height()) // 2)
            p.drawPixmap(
                clip_rect, scaled,
                QRect(sx, sy, clip_rect.width(), clip_rect.height()),
            )

        # Selection → border only (no clip body tint, so region-select tint
        # stays clearly distinguishable from "this clip is selected").
        is_selected = c.id == self._selected_id
        color = theme.C_ACCENT if is_selected else theme.C_VIDEO_CLIP_BD
        pen = QPen(color)
        pen.setWidth(2 if is_selected else 1)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(clip_rect.adjusted(0, 0, -1, -1))

        if is_selected:
            left = QRect(r.left(), r.top(), HANDLE_W, r.height()).intersected(self._video_rect())
            right = QRect(r.right() - HANDLE_W, r.top(), HANDLE_W, r.height()).intersected(self._video_rect())
            handle_fill = QColor(94, 234, 212, 170)
            p.fillRect(left, handle_fill)
            p.fillRect(right, handle_fill)

    def _chain_button_rect(self, clip_rect: QRect) -> QRect:
        """Chain chip sits in the gap between the video and audio tracks,
        anchored to the clip's left edge (so it visually joins the two)."""
        vr = self._video_rect()
        ar = self._audio_rect()
        mid = (vr.bottom() + ar.top()) // 2
        return QRect(
            clip_rect.left() + CHAIN_BTN_MARGIN,
            mid - CHAIN_BTN_H // 2,
            CHAIN_BTN_W, CHAIN_BTN_H,
        )

    def _paint_chain_icon(self, p: QPainter, rect: QRect, linked: bool) -> None:
        # Rounded chip background
        p.setPen(QPen(theme.C_BORDER_HI, 1))
        p.setBrush(QColor(10, 16, 19, 230))
        p.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 5, 5)

        color = theme.C_ACCENT if linked else theme.C_DANGER
        pen = QPen(color, 1.6)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        # Two rounded link rectangles overlapping slightly in the middle.
        cx = rect.center().x()
        cy = rect.center().y()
        half_w = 6
        half_h = 4
        left_link = QRect(cx - half_w - 2, cy - half_h, half_w * 2, half_h * 2)
        right_link = QRect(cx - half_w + 4, cy - half_h, half_w * 2, half_h * 2)
        p.drawRoundedRect(left_link, 3, 3)
        p.drawRoundedRect(right_link, 3, 3)

        if not linked:
            # Diagonal break stroke
            p.setPen(QPen(theme.C_DANGER, 2))
            p.drawLine(rect.left() + 3, rect.bottom() - 3,
                       rect.right() - 3, rect.top() + 3)

    def _paint_divider(self, p: QPainter) -> None:
        gap = self._va_divider_rect()
        if gap.height() <= 0:
            return
        # Two faint horizontal grip lines centered in the gap.
        p.setPen(QPen(theme.C_BORDER_HI, 1))
        cy = gap.center().y()
        left = gap.left() + 30
        right = gap.right() - 30
        p.drawLine(left, cy - 1, right, cy - 1)
        p.drawLine(left, cy + 1, right, cy + 1)

    def _paint_chain_chips(self, p: QPainter, vr: QRect) -> None:
        """Draw a chain chip in the gap for every clip that has audio."""
        for c in self._clips:
            if not c.asset.has_audio:
                continue
            r = self._clip_rect(c, vr)
            if r.right() < vr.left() or r.left() > vr.right():
                continue
            btn = self._chain_button_rect(r)
            self._paint_chain_icon(p, btn, c.linked_audio)

    def _paint_audio_track(self, p: QPainter, ar: QRect) -> None:
        p.fillRect(ar, theme.C_TRACK_BG)
        for c in self._clips:
            r_vid = self._clip_rect(c, self._video_rect())
            # Audio shifts horizontally by audio_offset (in seconds) when the
            # user has unlinked and dragged it — linked clips always have
            # audio_offset=0.
            offset_px = int(round(c.audio_offset * self._pps))
            r = QRect(r_vid.left() + offset_px, ar.top(), r_vid.width(), ar.height())
            r_vis = r.intersected(ar)
            if r_vis.isEmpty():
                continue

            if not c.asset.has_audio or c.audio_removed:
                p.fillRect(r_vis, QColor("#0a1013"))
                p.setPen(theme.C_TEXT_3)
                label = (
                    "(audio deleted — chain chip to restore)"
                    if c.audio_removed else "no audio"
                )
                p.drawText(r_vis, Qt.AlignCenter, label)
                continue

            # Linked and unlinked clip audio share the same visual
            # treatment — only the chain chip differentiates the two. The
            # selection outline colour still communicates what's selected.
            p.fillRect(r_vis, theme.C_VIDEO_CLIP_B)

            if c.waveform_peaks and c.waveform_rate > 0:
                self._draw_clip_waveform(p, c, r, r_vis, theme.C_ACCENT)

            audio_selected = c.id == self._selected_audio_clip_id
            if audio_selected:
                color = theme.C_WARN  # dragged/selected unlinked audio
                width = 2
            elif c.id == self._selected_id:
                color = theme.C_ACCENT
                width = 2
            else:
                color = theme.C_VIDEO_CLIP_BD
                width = 1
            pen = QPen(color)
            pen.setWidth(width)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(r_vis.adjusted(0, 0, -1, -1))

        # Lane-0 added-audio entries live on Track 1 alongside clip audio.
        self._paint_added_audio_entries(p, ar, lane=0)

    def _paint_added_audio_track(self, p: QPainter, ar: QRect, lane: int) -> None:
        p.fillRect(ar, theme.C_TRACK_BG)
        if not any(a.lane == lane for a in self._added_audios):
            p.setPen(theme.C_TEXT_3)
            f = p.font(); f.setItalic(True); p.setFont(f)
            p.drawText(
                ar, Qt.AlignCenter,
                f"Drag audio here for Audio Track {lane + 1} (sound effects, music, etc.)",
            )
            f.setItalic(False); p.setFont(f)
            return
        self._paint_added_audio_entries(p, ar, lane=lane)

    def _paint_added_audio_entries(self, p: QPainter, track_rect: QRect, lane: int) -> None:
        for audio in self._added_audios:
            if audio.lane != lane or audio.duration <= 0:
                continue
            tile = self._added_audio_tile_rect(audio)
            tile_vis = tile.intersected(track_rect)
            if tile_vis.isEmpty():
                continue
            p.fillRect(tile_vis, theme.C_AUDIO_CLIP_B)
            if audio.peaks and audio.rate > 0:
                self._draw_added_audio_chunk(
                    p, audio.peaks, audio.rate,
                    tile, tile_vis, audio.src_span, theme.C_WARN,
                    src_start=audio.src_start,
                )
            self._draw_added_audio_badge(p, audio, tile_vis)
            selected = audio.id == self._added_audio_selected_id
            if selected:
                pen = QPen(theme.C_WARN)
                pen.setWidth(2)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(tile_vis.adjusted(0, 0, -1, -1))
                hw = HANDLE_W
                handle_color = QColor(theme.C_WARN)
                handle_color.setAlpha(100)
                p.fillRect(QRect(tile_vis.left(), tile_vis.top(), hw, tile_vis.height()), handle_color)
                p.fillRect(QRect(tile_vis.right() - hw, tile_vis.top(), hw, tile_vis.height()), handle_color)
            else:
                pen = QPen(theme.C_AUDIO_CLIP_BD)
                pen.setWidth(1)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(tile_vis.adjusted(0, 0, -1, -1))

    def _draw_added_audio_badge(
        self, p: QPainter, audio: AddedAudio, tile_vis: QRect,
    ) -> None:
        """Stamp the volume / mute state into the tile's upper-right corner.

        Purely decorative: the badge is laid out from `tile_vis`, never fed
        back into it, so tile geometry, hit testing and selection rects are
        untouched. A tile too narrow to hold the badge is left bare rather
        than having it overflow into the neighbouring one."""
        text = added_audio_volume_badge(audio)
        if text is None:
            return
        p.save()
        f = p.font(); f.setBold(True); p.setFont(f)
        bw = p.fontMetrics().horizontalAdvance(text) + BADGE_PAD_X
        bh = BADGE_H
        if tile_vis.width() < bw + 2 * BADGE_MARGIN or tile_vis.height() < bh + 2:
            p.restore()
            return
        rect = QRect(
            tile_vis.right() - bw - BADGE_MARGIN, tile_vis.top() + BADGE_MARGIN,
            bw, bh,
        )
        bg = QColor(theme.C_BG)
        bg.setAlpha(BADGE_BG_ALPHA)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 3, 3)
        p.setBrush(Qt.NoBrush)
        p.setPen(theme.C_DANGER if audio.muted else theme.C_WARN)
        p.drawText(rect, Qt.AlignCenter, text)
        p.restore()

    def _draw_clip_waveform(
        self, p: QPainter, c: Clip, r: QRect, r_vis: QRect, color: QColor,
    ) -> None:
        """Build a filled polygon from `c.waveform_peaks` for the pixel columns
        of `r_vis`. The waveform stays crisp at any zoom because we resample
        from peak data every paint instead of scaling a bitmap."""
        peaks = c.waveform_peaks
        rate = c.waveform_rate
        if not peaks or rate <= 0:
            return

        cy = r.center().y()
        half_h = max(1, r.height() // 2 - 2)

        # seconds of source audio covered by one screen pixel
        src_per_px = max(c.speed / max(0.01, self._pps), 1.0 / rate)
        samples_per_px = src_per_px * rate
        half_window_samples = max(1, int(round(samples_per_px / 2)))
        interp = samples_per_px < 2.0
        n_peaks = len(peaks)

        # For each visible column, look up the peak from the clip's source
        # time, bounded to the clip's trimmed source window.
        src_start = c.src_start
        src_end = c.src_end
        left_px = r.left()
        pps = self._pps
        speed = c.speed

        top_pts: list[QPointF] = []
        bot_pts: list[QPointF] = []
        for x in range(r_vis.left(), r_vis.right() + 1):
            # column x → source time (relative to the video start, then shifted
            # back by audio_offset since the rect is already offset)
            px_into_rect = x - left_px
            src_t = src_start + (px_into_rect / pps) * speed
            if src_t < src_start or src_t > src_end:
                continue
            if interp:
                raw_idx = src_t * rate
                i0 = int(raw_idx)
                if i0 < 0:
                    continue
                if i0 >= n_peaks - 1:
                    pk = peaks[n_peaks - 1]
                else:
                    f = raw_idx - i0
                    pk = peaks[i0] * (1.0 - f) + peaks[i0 + 1] * f
            else:
                idx = int(src_t * rate)
                a = max(0, idx - half_window_samples)
                b = min(n_peaks, idx + half_window_samples + 1)
                if b <= a:
                    continue
                pk = max(peaks[a:b])
            y = pk * half_h
            top_pts.append(QPointF(x + 0.5, cy - y))
            bot_pts.append(QPointF(x + 0.5, cy + y))

        if not top_pts:
            return
        polygon = QPolygonF(top_pts + list(reversed(bot_pts)))
        path = QPainterPath()
        path.addPolygon(polygon)
        path.closeSubpath()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.fillPath(path, color)
        # centerline for a polished look
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1))
        p.drawLine(r_vis.left(), cy, r_vis.right(), cy)

    def _draw_added_audio_chunk(
        self, p: QPainter, peaks: list[float], rate: int,
        chunk: QRect, chunk_vis: QRect, chunk_seconds: float,
        color: QColor, *, src_start: float = 0.0,
    ) -> None:
        """Draw one loop iteration of the added audio. The chunk's left edge
        maps to source second `src_start`; its right edge to
        `src_start + chunk_seconds`."""
        n = len(peaks)
        if n == 0 or rate <= 0 or chunk.width() <= 0 or chunk_seconds <= 0:
            return
        cy = chunk.center().y()
        half_h = max(1, chunk.height() // 2 - 2)
        pps = max(0.01, self._pps)
        src_per_px = 1.0 / pps
        samples_per_px = src_per_px * rate
        # Interpolate between adjacent peaks when zoomed in enough that each
        # pixel covers less than ~2 samples — otherwise use max-in-window.
        interp = samples_per_px < 2.0
        half_window = max(1, int(round(samples_per_px / 2)))
        top_pts: list[QPointF] = []
        bot_pts: list[QPointF] = []
        for x in range(chunk_vis.left(), chunk_vis.right() + 1):
            audio_t = src_start + (x - chunk.left()) / pps
            if audio_t < src_start or audio_t > src_start + chunk_seconds:
                continue
            if interp:
                raw_idx = audio_t * rate
                i0 = int(raw_idx)
                if i0 < 0:
                    continue
                if i0 >= n - 1:
                    pk = peaks[n - 1]
                else:
                    f = raw_idx - i0
                    pk = peaks[i0] * (1.0 - f) + peaks[i0 + 1] * f
            else:
                idx = int(audio_t * rate)
                a = max(0, idx - half_window)
                b = min(n, idx + half_window + 1)
                if b <= a:
                    continue
                pk = max(peaks[a:b])
            y = pk * half_h
            top_pts.append(QPointF(x + 0.5, cy - y))
            bot_pts.append(QPointF(x + 0.5, cy + y))
        if not top_pts:
            return
        polygon = QPolygonF(top_pts + list(reversed(bot_pts)))
        path = QPainterPath()
        path.addPolygon(polygon)
        path.closeSubpath()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.fillPath(path, color)
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1))
        p.drawLine(chunk_vis.left(), cy, chunk_vis.right(), cy)

    # ---- mouse --------------------------------------------------------

    _PLAYHEAD_SLOP_PX = 4

    def _is_over_playhead(self, pos: QPoint) -> bool:
        return abs(pos.x() - self._time_to_x(self._playhead)) <= self._PLAYHEAD_SLOP_PX

    def _audio_clip_rect(self, c: Clip) -> QRect:
        """Rect for a clip's audio block on the audio track (honors offset)."""
        vr = self._video_rect()
        ar = self._audio_rect()
        r_vid = self._clip_rect(c, vr)
        offset_px = int(round(c.audio_offset * self._pps))
        return QRect(r_vid.left() + offset_px, ar.top(), r_vid.width(), ar.height())

    def _added_audio_tile_rect(self, audio: AddedAudio) -> QRect:
        """Rect for a single added-audio entry on its lane."""
        lane = max(0, min(len(self._audio_lane_heights) - 1, audio.lane))
        lane_rect = self._audio_lane_rect(lane)
        x0 = self._time_to_x(audio.offset)
        x1 = self._time_to_x(audio.offset + max(0.01, audio.timeline_length))
        return QRect(x0, lane_rect.top(), max(2, x1 - x0), lane_rect.height())

    def _hit_added_audio(self, pos: QPoint, lane: int | None = None) -> AddedAudio | None:
        """Topmost added-audio entry under `pos`, iterating newest-first so
        later additions win on overlap. If `lane` is None, search all lanes."""
        for audio in reversed(self._added_audios):
            if lane is not None and audio.lane != lane:
                continue
            if audio.duration <= 0 or not audio.peaks:
                continue
            if self._added_audio_tile_rect(audio).contains(pos):
                return audio
        return None

    def _lane_for_y(self, y: int) -> int:
        """Return the audio lane index whose rect covers `y`. When `y` is
        below the last lane, return the last lane — dragging below the
        stack doesn't blindly create a lane (the app has an explicit
        "+ Audio Track" button for that)."""
        for lane in range(len(self._audio_lane_heights)):
            r = self._audio_lane_rect(lane)
            if r.top() <= y <= r.bottom():
                return lane
        return max(0, len(self._audio_lane_heights) - 1)

    def _hit_test(self, pos: QPoint) -> _Drag:
        rr = self._ruler_rect()
        vr = self._video_rect()
        gap = self._va_divider_rect()

        # Chain chip lives in the gap — check it before the generic gap drag.
        for c in self._clips:
            if not c.asset.has_audio:
                continue
            r = self._clip_rect(c, vr)
            if self._chain_button_rect(r).contains(pos):
                return _Drag(mode="chain", clip_id=c.id)

        # Video/audio gap → drag to resize tracks
        if gap.contains(pos):
            return _Drag(mode="resize_tracks")

        if rr.contains(pos):
            # Clicking anywhere on the ruler — including the red playhead
            # handle itself — starts a seek drag.
            return _Drag(mode="seek")

        if vr.contains(pos):
            sel = next((c for c in self._clips if c.id == self._selected_id), None)
            if sel is not None:
                r = self._clip_rect(sel, vr)
                if abs(pos.x() - r.left()) <= HANDLE_W:
                    return _Drag(mode="trim_l", clip_id=sel.id)
                if abs(pos.x() - r.right()) <= HANDLE_W:
                    return _Drag(mode="trim_r", clip_id=sel.id)
            if self._is_over_playhead(pos):
                return _Drag(mode="playhead_region")
            for c in self._clips:
                r = self._clip_rect(c, vr)
                if r.contains(pos):
                    return _Drag(mode="move_clip", clip_id=c.id)
            return _Drag(mode="select")

        # Walk each audio lane in order.
        for lane in range(len(self._audio_lane_heights)):
            lane_rect = self._audio_lane_rect(lane)
            if not lane_rect.contains(pos):
                continue
            if self._is_over_playhead(pos):
                return _Drag(mode="playhead_region")
            sel_audio = next(
                (a for a in self._added_audios
                 if a.id == self._added_audio_selected_id and a.lane == lane),
                None,
            )
            if sel_audio is not None:
                ar = self._added_audio_tile_rect(sel_audio)
                if abs(pos.x() - ar.left()) <= HANDLE_W:
                    return _Drag(mode="trim_added_l", clip_id=sel_audio.id)
                if abs(pos.x() - ar.right()) <= HANDLE_W:
                    return _Drag(mode="trim_added_r", clip_id=sel_audio.id)
            hit_audio = self._hit_added_audio(pos, lane=lane)
            if hit_audio is not None:
                return _Drag(mode="select_added", clip_id=hit_audio.id)
            if lane == 0:
                # Lane 0 also carries clip audio blocks.
                for c in self._clips:
                    if not c.asset.has_audio or c.audio_removed:
                        continue
                    rect = self._audio_clip_rect(c)
                    if rect.contains(pos):
                        if not c.linked_audio:
                            return _Drag(mode="move_audio", clip_id=c.id)
                        return _Drag(mode="move_clip", clip_id=c.id)
            return _Drag(mode="select")

        return _Drag(mode="select")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if event.button() == Qt.RightButton:
            # `QMenu.exec()` spins a nested event loop, which would keep
            # servicing auto-scroll ticks - and so keep editing - behind the
            # menu. Disarm first; the drag itself is untouched.
            self._stop_edge_autoscroll()
            self._show_context_menu(event.globalPosition().toPoint(), pos)
            return
        if event.button() != Qt.LeftButton:
            return

        # A fresh gesture never inherits the previous one's pointer or
        # auto-scroll direction.
        self._autoscroll_timer.stop()
        self._drag_pos = None

        shift_held = bool(event.modifiers() & Qt.ShiftModifier)
        hit = self._hit_test(pos)
        hit.press_x = pos.x()
        hit.press_y = pos.y()

        # Any press that isn't a click on the added-audio tile clears the
        # added-audio selection highlight.
        if hit.mode != "select_added" and self._added_audio_selected_id:
            self._added_audio_selected_id = ""
            self.update()
        # Clicking anywhere other than an unlinked clip's audio block clears
        # the audio-only selection.
        if hit.mode != "move_audio" and self._selected_audio_clip_id:
            self._selected_audio_clip_id = ""
            self.update()

        # Any left-click that isn't starting a new region selection (shift-
        # drag or playhead-region drag) clears the existing blue highlight.
        region_modes = ("playhead_region",)
        if (
            not shift_held
            and hit.mode not in region_modes
            and self._sel_end > self._sel_start
        ):
            self.clear_selection()

        # Shift anywhere → region-select regardless of what's under the cursor.
        if shift_held and hit.mode != "seek":
            t = self._x_to_time(pos.x())
            self._drag = _Drag(mode="select", anchor_t=t,
                               press_x=pos.x(), press_y=pos.y())
            self._set_selection_span(t, t)
            self.update()
            return

        if hit.mode == "seek":
            self.clear_selection()
            self.set_playhead(self._x_to_time(pos.x()))
            self._drag = hit
            return
        if hit.mode == "playhead_region":
            t = self._playhead
            self._drag = _Drag(mode="select", anchor_t=t,
                               press_x=pos.x(), press_y=pos.y())
            self._set_selection_span(t, t)
            self.update()
            return
        if hit.mode == "chain":
            self.audioLinkToggled.emit(hit.clip_id)
            self._drag = _Drag()
            return
        if hit.mode == "select_added":
            # Click an added-audio tile → select it and arm a drag so the
            # user can reposition it along the timeline.
            audio = next(
                (a for a in self._added_audios if a.id == hit.clip_id), None,
            )
            if audio is None:
                self._drag = _Drag()
                return
            self._added_audio_selected_id = audio.id
            if self._selected_id:
                self._selected_id = ""
            self.clipSelected.emit("")
            self.clear_selection()
            self._drag = _Drag(
                mode="move_added",
                clip_id=audio.id,
                grab_offset_s=self._x_to_time(pos.x()) - audio.offset,
                press_x=pos.x(), press_y=pos.y(),
            )
            self.update()
            return
        if hit.mode == "resize_tracks":
            lane_0_h = self._audio_lane_heights[0] if self._audio_lane_heights else AUDIO_LANE_0_H
            self._drag = _Drag(
                mode="resize_tracks", press_x=pos.x(), press_y=pos.y(),
                start_video_h=self._video_h, start_audio_h=lane_0_h,
            )
            return
        if hit.mode in ("trim_l", "trim_r"):
            self._drag = hit
            return
        if hit.mode in ("trim_added_l", "trim_added_r"):
            self._drag = hit
            return
        if hit.mode == "move_audio":
            c = self._find(hit.clip_id)
            if c is not None:
                # Select just the audio block, not the whole clip — Delete
                # then removes the audio without touching the video.
                self._selected_audio_clip_id = c.id
                if self._selected_id:
                    self._selected_id = ""
                    self.clipSelected.emit("")
                self.clear_selection()
                self._drag = _Drag(
                    mode="move_audio", clip_id=c.id,
                    grab_offset_s=self._x_to_time(pos.x()) - (c.timeline_start + c.audio_offset),
                    press_x=pos.x(), press_y=pos.y(),
                )
                self.update()
                return
        if hit.mode == "move_clip":
            c = self._find(hit.clip_id)
            if c is not None:
                if c.id != self._selected_id:
                    self.select_clip(c.id)
                self._drag = _Drag(
                    mode="move_clip", clip_id=c.id,
                    original_clip_start=c.timeline_start,
                    grab_offset_s=self._x_to_time(pos.x()) - c.timeline_start,
                    press_x=pos.x(), press_y=pos.y(),
                )
                return

        # empty area drag → region select
        t = self._x_to_time(pos.x())
        self._drag = _Drag(mode="select", anchor_t=t,
                           press_x=pos.x(), press_y=pos.y())
        self._set_selection_span(t, t)
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        # Double click on a clip → emit for properties dialog.
        for c in self._clips:
            r = self._clip_rect(c, self._video_rect())
            if r.contains(pos):
                self.select_clip(c.id)
                self.clipDoubleClicked.emit(c.id)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if not self._drag.mode:
            hit = self._hit_test(pos)
            self.setCursor({
                "seek": Qt.PointingHandCursor,
                "select": Qt.IBeamCursor,
                "trim_l": Qt.SplitHCursor, "trim_r": Qt.SplitHCursor,
                "trim_added_l": Qt.SplitHCursor, "trim_added_r": Qt.SplitHCursor,
                "move_clip": Qt.SizeAllCursor,
                "move_audio": Qt.SizeHorCursor,
                "playhead_region": Qt.SizeHorCursor,
                "resize_tracks": Qt.SplitVCursor,
                "chain": Qt.PointingHandCursor,
                "select_added": Qt.SizeAllCursor,
            }.get(hit.mode, Qt.ArrowCursor))
            return

        if (abs(pos.x() - self._drag.press_x) > CLICK_SLOP_PX
                or abs(pos.y() - self._drag.press_y) > CLICK_SLOP_PX):
            self._drag.moved = True

        self._drag_pos = pos
        self._apply_drag_at(pos)
        self._update_edge_autoscroll()

    # Time-axis drags that edge auto-scroll assists. "seek" is deliberately
    # absent: `set_playhead()` already pans the view via `_ensure_visible()`,
    # so a second scroll source would compound. "resize_tracks" is vertical.
    _EDGE_SCROLL_MODES = frozenset({
        "select", "move_clip", "move_audio", "move_added",
        "trim_l", "trim_r", "trim_added_l", "trim_added_r",
    })

    def _apply_drag_at(self, pos: QPoint) -> None:
        """Recalculate the active drag for a widget-local pointer position.

        The single authoritative drag-application path: real pointer movement
        and edge auto-scroll ticks both land here, so a tick that only changed
        `_scroll_x` still moves the dragged object to the timeline time now
        sitting under the (physically stationary) pointer."""
        t = self._x_to_time(pos.x())
        if self._drag.mode == "seek":
            self.set_playhead(t)
        elif self._drag.mode == "select":
            a = self._drag.anchor_t
            self._set_selection_span(min(a, t), max(a, t))
            self.update()
        elif self._drag.mode in ("trim_l", "trim_r"):
            self._apply_trim(self._drag.mode, t)
        elif self._drag.mode in ("trim_added_l", "trim_added_r"):
            self._apply_added_audio_trim(self._drag.mode, t)
        elif self._drag.mode == "move_clip":
            if self._drag.moved:
                new_start = max(0.0, t - self._drag.grab_offset_s)
                c = self._find(self._drag.clip_id)
                if c is not None:
                    new_start = self._snap_clip_start(c, new_start)
                    # When the audio is unlinked, keep its absolute timeline
                    # position fixed by compensating audio_offset against the
                    # new video start. Linked audio rides along as before.
                    if not c.linked_audio:
                        old_audio_abs = c.timeline_start + c.audio_offset
                        c.audio_offset = old_audio_abs - new_start
                    c.timeline_start = new_start
                    self.update()
        elif self._drag.mode == "move_audio":
            c = self._find(self._drag.clip_id)
            if c is not None and self._drag.moved:
                new_audio_start = max(0.0, t - self._drag.grab_offset_s)
                new_audio_start = self._snap_audio_start(
                    new_audio_start, c.timeline_length,
                    ignore_clip_id=c.id,
                )
                c.audio_offset = new_audio_start - c.timeline_start
                self.update()
        elif self._drag.mode == "move_added":
            if self._drag.moved:
                audio = next(
                    (a for a in self._added_audios if a.id == self._drag.clip_id),
                    None,
                )
                if audio is not None:
                    new_offset = max(0.0, t - self._drag.grab_offset_s)
                    audio.offset = self._snap_audio_start(
                        new_offset, audio.timeline_length, ignore_audio_id=audio.id,
                    )
                    # Vertical drag switches between Audio Track 1 and 2.
                    new_lane = self._lane_for_y(pos.y())
                    if new_lane != audio.lane:
                        audio.lane = new_lane
                    self.update()
        elif self._drag.mode == "resize_tracks":
            dy = pos.y() - self._drag.press_y
            new_vh = max(VIDEO_TRACK_H_MIN,
                         min(VIDEO_TRACK_H_MAX, self._drag.start_video_h + dy))
            new_ah = max(AUDIO_TRACK_H_MIN,
                         min(AUDIO_TRACK_H_MAX, self._drag.start_audio_h - dy))
            lane_0_h = self._audio_lane_heights[0] if self._audio_lane_heights else AUDIO_LANE_0_H
            if new_vh != self._video_h or new_ah != lane_0_h:
                self._video_h = new_vh
                if self._audio_lane_heights:
                    self._audio_lane_heights[0] = new_ah
                self._refresh_min_height()
                self.update()

    # ---- edge auto-scroll ---------------------------------------------

    def _edge_scroll_step(self) -> int:
        """Per-tick scroll for the current drag; 0 when auto-scroll must not
        run - no supported drag, no remembered pointer, pointer away from the
        edges, or no room left to scroll that way."""
        if self._drag.mode not in self._EDGE_SCROLL_MODES:
            return 0
        if not self._drag.moved:
            # Respect the existing click-slop threshold: jitter inside a click
            # near an edge must not pan the view (and, via the replay, turn
            # itself into a real edit).
            return 0
        pos = self._drag_pos
        if pos is None:
            return 0
        tr = self._track_rect()
        delta = edge_scroll_delta(pos.x() - tr.left(), tr.width())
        if delta < 0 and self._scroll_x <= 0:
            return 0
        if delta > 0 and self._scroll_x >= self.scroll_max_px():
            return 0
        return delta

    def _update_edge_autoscroll(self) -> None:
        """Single arm/disarm decision point, re-evaluated after every pointer
        move and every tick."""
        if self._edge_scroll_step() != 0:
            if not self._autoscroll_timer.isActive():
                self._autoscroll_timer.start()
        else:
            self._autoscroll_timer.stop()

    def _edge_autoscroll_tick(self) -> None:
        step = self._edge_scroll_step()
        if step == 0:
            self._autoscroll_timer.stop()
            return
        before = self._scroll_x
        # Route through the Tab 2A clamp: `_publish_scroll_range()` folds the
        # candidate back into [0, scroll_max_px()] and notifies the scrollbar.
        # `scroll_max_px()` is recomputed there, so a resize or a range change
        # mid-drag can never be outrun by a stale maximum.
        self._scroll_x = self._clamped_scroll_x(self._scroll_x + step)
        self._publish_scroll_range()
        if self._scroll_x == before:
            self._autoscroll_timer.stop()
            return
        pos = self._drag_pos
        if pos is not None:
            self._apply_drag_at(pos)
        self.update()
        self._update_edge_autoscroll()

    def _stop_edge_autoscroll(self) -> None:
        """Disarm auto-scroll and forget the pointer, without touching the
        drag itself. Auto-scroll is viewport state only."""
        self._autoscroll_timer.stop()
        self._drag_pos = None

    def _end_drag(self) -> None:
        """Single teardown for every drag exit path."""
        self._stop_edge_autoscroll()
        self._drag = _Drag()

    #: Input-stream endings that are not a mouse release. Qt only guarantees
    #: `mouseReleaseEvent()` while the implicit mouse grab is held, so once it
    #: is lost (a popup steals it, the window deactivates) the drag is orphaned
    #: and nothing else would ever stop the auto-scroll timer.
    _ORPHAN_DRAG_EVENTS = (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate)

    def _abandon_orphaned_drag(self) -> None:
        """Finish a drag whose input stream stopped without a release.

        Terminating is not enough: the widget owns *clones* of the added-audio
        entries, so an added-audio move or trim that never emits its commit
        signal is silently discarded the next time the app republishes. The
        orphan therefore runs the same `_finalize_drag()` the release would
        have run - same commit, same normalization, same single snapshot -
        and only then tears the drag down. Nothing is rolled back.

        Idempotent: `_finalize_drag()` is a no-op once the drag is cleared, so
        a release that still arrives afterwards commits nothing further.
        """
        if not self._drag.mode:
            return
        self._finalize_drag()
        self._end_drag()

    def event(self, event) -> bool:  # noqa: ANN001
        if event.type() in self._ORPHAN_DRAG_EVENTS:
            self._abandon_orphaned_drag()
        return super().event(event)

    def hideEvent(self, event) -> None:  # noqa: ANN001
        # Nothing may keep ticking behind a hidden widget. The drag itself is
        # deliberately left alone: tearing it down here would abandon an
        # in-progress edit without the commit `mouseReleaseEvent()` performs.
        self._stop_edge_autoscroll()
        super().hideEvent(event)

    def _finalize_drag(self) -> None:
        """Commit the active drag with its mode-specific release semantics.

        The single authoritative finalization path, shared by an ordinary
        `mouseReleaseEvent()` and by an abnormal termination (`UngrabMouse` /
        `WindowDeactivate`). It emits exactly the one commit signal that drag
        earns - which is what drives the app's single `_snapshot()` - and runs
        the same normalization (overlap resolution, re-sort, trailing lane).

        No-op once `_drag.mode` is empty, so a release arriving after an
        orphan has already been finalized commits nothing a second time."""
        mode = self._drag.mode
        if mode in ("trim_l", "trim_r"):
            c = self._find(self._drag.clip_id)
            if c is not None:
                self.rangeChanged.emit(c.id, c.src_start, c.src_end)
        elif mode in ("trim_added_l", "trim_added_r"):
            audio = next(
                (a for a in self._added_audios if a.id == self._drag.clip_id),
                None,
            )
            if audio is not None:
                self.addedAudioRangeChanged.emit(audio.id)
        elif mode == "move_clip":
            c = self._find(self._drag.clip_id)
            if c is not None and self._drag.moved:
                self._resolve_overlaps(c)
                self.clipMoved.emit(c.id, c.timeline_start)
                self._clips = sort_clips(self._clips)
                self.update()
            # A bare click on a clip just leaves it selected — no region span.
        elif mode == "move_audio":
            c = self._find(self._drag.clip_id)
            if c is not None and self._drag.moved:
                self.audioOffsetChanged.emit(c.id, c.audio_offset)
        elif mode == "move_added":
            if self._drag.moved:
                audio = next(
                    (a for a in self._added_audios if a.id == self._drag.clip_id),
                    None,
                )
                if audio is not None:
                    self.addedAudioOffsetChanged.emit(audio.id, audio.offset)
                    # The lane may have just been populated — grow a fresh
                    # empty lane below it so the user has a drop target.
                    self._maintain_trailing_empty_lane()
        elif mode == "select":
            if self._sel_end > self._sel_start + 0.01:
                self.selectionChanged.emit(self._sel_start, self._sel_end)
            else:
                # bare click on empty area → clear any old selection
                self.clear_selection()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._finalize_drag()
        self._end_drag()

    def wheelEvent(self, event: QWheelEvent) -> None:
        # Plain wheel zooms horizontally around the cursor; shift+wheel scrolls.
        # Track resizing is drag-only via the video/audio divider.
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ShiftModifier:
            step_px = 80 if delta > 0 else -80
            self._scroll_x = max(0, min(self.scroll_max_px(), self._scroll_x - step_px))
            self._publish_scroll_range()
            self.update()
            return

        factor = 1.2 if delta > 0 else (1 / 1.2)
        pivot_t = self._x_to_time(int(event.position().x()))
        new_pps = max(MIN_PPS, min(MAX_PPS, self._pps * factor))
        if new_pps != self._pps:
            old_screen_x = int(event.position().x())
            self._pps = new_pps
            self._scroll_x = max(0, int(pivot_t * self._pps - (old_screen_x - LEFT_PAD)))
            self.pixelsPerSecondChanged.emit(self._pps)
            self._publish_scroll_range()
            self.update()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if self._sel_end > self._sel_start:
                self.regionDeleteRequested.emit(self._sel_start, self._sel_end)
                event.accept()
                return
            if self._added_audio_selected_id:
                audio_id = self._added_audio_selected_id
                self._added_audio_selected_id = ""
                self.addedAudioDeleteRequested.emit(audio_id)
                event.accept()
                return
            if self._selected_audio_clip_id:
                cid = self._selected_audio_clip_id
                self._selected_audio_clip_id = ""
                self.clipAudioRemoveRequested.emit(cid)
                event.accept()
                return
            if self._selected_id:
                self.clipDeleteRequested.emit(self._selected_id)
                event.accept()
                return
        if event.key() == Qt.Key_S and event.modifiers() == Qt.NoModifier:
            self.splitAtPlayheadRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- helpers ------------------------------------------------------

    def _apply_trim(self, mode: str, t_timeline: float) -> None:
        c = self._find(self._drag.clip_id)
        if c is None:
            return
        min_gap = max(0.05, c.asset.duration * 0.005)
        if mode == "trim_l":
            # trim the source start; keep the right edge anchored on the timeline
            anchor_end_t = c.timeline_end
            new_src_start = max(0.0, c.src_for_timeline(max(c.timeline_start, t_timeline)))
            # don't let the start cross the end minus a gap
            new_src_start = min(new_src_start, c.src_end - min_gap)
            c.src_start = new_src_start
            c.timeline_start = anchor_end_t - c.timeline_length
            c.timeline_start = max(0.0, c.timeline_start)
        else:
            new_src_end = c.src_for_timeline(min(c.timeline_end, t_timeline))
            c.src_end = max(c.src_start + min_gap, min(c.asset.duration, new_src_end))
        self.update()

    def _apply_added_audio_trim(self, mode: str, t_timeline: float) -> None:
        audio = next(
            (a for a in self._added_audios if a.id == self._drag.clip_id),
            None,
        )
        if audio is None:
            return
        min_gap = max(0.05, audio.duration * 0.005)
        if mode == "trim_added_l":
            anchor_end_t = audio.timeline_end
            new_src = audio.src_start + (t_timeline - audio.offset)
            new_src = max(0.0, min(new_src, audio.src_end - min_gap))
            audio.src_start = new_src
            new_offset = anchor_end_t - audio.timeline_length
            if new_offset < 0.0:
                audio.src_start = audio.src_end - anchor_end_t
                audio.src_start = max(0.0, audio.src_start)
                new_offset = 0.0
            audio.offset = new_offset
        else:
            new_src_end = audio.src_start + (t_timeline - audio.offset)
            audio.src_end = max(audio.src_start + min_gap, min(audio.duration, new_src_end))
        self.update()

    def _snap_clip_start(self, moved: Clip, new_start: float) -> float:
        """Snap moved clip's start/end to other clip edges, t=0, and the
        playhead within 10px."""
        return self._snap_to_candidates(
            new_start, moved.timeline_length,
            self._timeline_snap_candidates(ignore_clip_id=moved.id),
        )

    def _snap_audio_start(
        self, new_start: float, length: float, *,
        ignore_audio_id: str = "", ignore_clip_id: str = "",
    ) -> float:
        """Snap a dragged audio block's start/end to clip/audio edges, t=0,
        and the playhead."""
        return self._snap_to_candidates(
            new_start, length,
            self._timeline_snap_candidates(
                ignore_clip_id=ignore_clip_id,
                ignore_audio_id=ignore_audio_id,
            ),
        )

    def _timeline_snap_candidates(
        self, *, ignore_clip_id: str = "", ignore_audio_id: str = "",
    ) -> list[float]:
        candidates: list[float] = [0.0, self._playhead]
        for c in self._clips:
            if c.id == ignore_clip_id:
                continue
            candidates.append(c.timeline_start)
            candidates.append(c.timeline_end)
            # Also consider the clip's audio placement when it's been
            # detached — that's where its audio edges physically sit.
            if not c.linked_audio and c.asset.has_audio and not c.audio_removed:
                candidates.append(c.timeline_start + c.audio_offset)
                candidates.append(c.timeline_end + c.audio_offset)
        for a in self._added_audios:
            if a.id == ignore_audio_id:
                continue
            candidates.append(a.offset)
            candidates.append(a.timeline_end)
        return candidates

    def _snap_to_candidates(
        self, new_start: float, length: float, candidates: list[float],
    ) -> float:
        tolerance_s = 10.0 / max(1.0, self._pps)
        best_start = new_start
        best_d = tolerance_s
        for cand in candidates:
            # Snap both the start and end of the moved block.
            for anchor in (cand, cand - length):
                d = abs(anchor - new_start)
                if d < best_d:
                    best_d = d
                    best_start = anchor
        return max(0.0, best_start)

    def _resolve_overlaps(self, moved: Clip) -> None:
        # Simple policy: push other clips aside. If `moved` overlaps with any
        # existing clip, shift `moved` to the nearest non-overlapping spot
        # (prefer the right side of the overlap).
        others = [c for c in self._clips if c.id != moved.id]
        others.sort(key=lambda c: c.timeline_start)
        # find any colliding clip
        for oc in others:
            if moved.timeline_end <= oc.timeline_start or moved.timeline_start >= oc.timeline_end:
                continue
            # collision: snap to the right of this clip
            moved.timeline_start = oc.timeline_end
            # re-check by restarting the loop
            return self._resolve_overlaps(moved)

    def _show_context_menu(self, global_pos: QPoint, local_pos: QPoint) -> None:
        has_selection = self._sel_end > self._sel_start
        # Resolve which audio lane was clicked (if any) so the menu can
        # offer lane-specific actions without duplicate code.
        clicked_lane: int | None = None
        for lane in range(len(self._audio_lane_heights)):
            if self._audio_lane_rect(lane).contains(local_pos):
                clicked_lane = lane
                break
        clicked_audio = (
            self._hit_added_audio(local_pos, lane=clicked_lane)
            if clicked_lane is not None else None
        )
        clicked_clip = None
        if self._video_rect().contains(local_pos):
            for c in self._clips:
                if self._clip_rect(c, self._video_rect()).contains(local_pos):
                    clicked_clip = c
                    break
        menu = QMenu(self)
        if has_selection:
            menu.addAction("Delete Selected Region")
            menu.addAction("Crop to Selected Region")
            menu.addAction("Export Selected Region…")
        else:
            menu.addAction("(no region selected)").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Split at Playhead")
        replace_action = None
        remove_this_action = None
        remove_all_action = None
        add_lane_action = None
        remove_lane_action = None
        clip_volume_action = None
        audio_volume_action = None
        audio_mute_action = None
        move_start_action = None
        move_playhead_action = None
        close_gap_action = None
        ripple_delete_action = None
        if clicked_clip is not None:
            menu.addSeparator()
            # Zero-drag repositioning: the way back from a leading gap left
            # by head-trimming, without dragging across a long sequence.
            move_start_action = menu.addAction("Move to Start")
            move_playhead_action = menu.addAction("Move to Playhead")
            # Explicit gap cleanup on the clip that was actually right-clicked.
            close_gap_action = menu.addAction("Close Gap Left")
            # Distinct from the plain delete on the Delete key, which leaves
            # the vacated span behind as a gap.
            ripple_delete_action = menu.addAction("Ripple Delete")
            clip_volume_action = menu.addAction("Volume...")
        if clicked_audio is not None:
            menu.addSeparator()
            replace_action = menu.addAction("Replace original audio")
            replace_action.setCheckable(True)
            replace_action.setChecked(self._added_audio_replace)
            audio_volume_action = menu.addAction("Volume...")
            # Mute is a separate override from the stored volume, so it reads
            # as its own toggle rather than a "0%" entry in the volume dialog.
            audio_mute_action = menu.addAction(
                "Unmute Audio" if clicked_audio.muted else "Mute Audio",
            )
            remove_this_action = menu.addAction("Remove This Audio Clip")
            if len(self._added_audios) > 1:
                remove_all_action = menu.addAction("Remove All Added Audio")
        if clicked_lane is not None:
            menu.addSeparator()
            add_lane_action = menu.addAction("Add Audio Track")
            # Can remove a lane only when it's empty and isn't the base lane.
            can_remove = (
                clicked_lane > 0
                and not any(a.lane == clicked_lane for a in self._added_audios)
                and len(self._audio_lane_heights) > 1
            )
            if can_remove:
                remove_lane_action = menu.addAction(
                    f"Remove Audio Track {clicked_lane + 1}",
                )
        chosen = menu.exec(global_pos)
        if not chosen:
            return
        text = chosen.text()
        if text == "Split at Playhead":
            self.splitAtPlayheadRequested.emit()
            return
        if move_start_action is not None and chosen is move_start_action:
            self.clipMoveToStartRequested.emit(clicked_clip.id)
            return
        if move_playhead_action is not None and chosen is move_playhead_action:
            self.clipMoveToPlayheadRequested.emit(clicked_clip.id)
            return
        if close_gap_action is not None and chosen is close_gap_action:
            self.clipCloseGapLeftRequested.emit(clicked_clip.id)
            return
        if ripple_delete_action is not None and chosen is ripple_delete_action:
            self.clipRippleDeleteRequested.emit(clicked_clip.id)
            return
        if clip_volume_action is not None and chosen is clip_volume_action:
            self.clipDoubleClicked.emit(clicked_clip.id)
            return
        if audio_volume_action is not None and chosen is audio_volume_action:
            self.addedAudioVolumeRequested.emit(clicked_audio.id)
            return
        if audio_mute_action is not None and chosen is audio_mute_action:
            # The widget owns no model state: MainWindow snapshots for undo
            # and writes the authoritative item.
            self.addedAudioMuteRequested.emit(clicked_audio.id)
            return
        if add_lane_action is not None and chosen is add_lane_action:
            self.add_audio_lane()
            return
        if remove_lane_action is not None and chosen is remove_lane_action:
            self.remove_audio_lane(clicked_lane)
            return
        if remove_this_action is not None and chosen is remove_this_action:
            self.addedAudioDeleteRequested.emit(clicked_audio.id)
            return
        if remove_all_action is not None and chosen is remove_all_action:
            self.addedAudioDeleteRequested.emit("")
            return
        if replace_action is not None and chosen is replace_action:
            self._added_audio_replace = replace_action.isChecked()
            self.addedAudioReplaceToggled.emit(self._added_audio_replace)
            return
        if not has_selection:
            return
        if text.startswith("Delete"):
            self.regionDeleteRequested.emit(self._sel_start, self._sel_end)
        elif text.startswith("Crop"):
            self.regionCropRequested.emit(self._sel_start, self._sel_end)
        elif text.startswith("Export"):
            self.regionExportRequested.emit(self._sel_start, self._sel_end)

    # ---- drag & drop of media -----------------------------------------

    _AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
    _VIDEO_EXTS = {
        ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
        ".mpg", ".mpeg", ".wmv",
    }

    def _accepts_drop(self, event) -> bool:  # noqa: ANN001
        md = event.mimeData()
        if md.hasFormat(ASSET_MIME):
            return True
        if self._drop_audio_path(event) is not None:
            return True
        if self._drop_video_path(event) is not None:
            return True
        return False

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._accepts_drop(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._accepts_drop(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        md = event.mimeData()
        drop_x = int(event.position().x())
        drop_y = int(event.position().y())
        drop_t = max(0.0, self._x_to_time(drop_x))
        lane = self._lane_for_y(drop_y)
        if md.hasFormat(ASSET_MIME):
            asset_id = bytes(md.data(ASSET_MIME)).decode()
            if asset_id:
                event.acceptProposedAction()
                self.assetDroppedOnTimeline.emit(asset_id, drop_t, lane)
                return
        audio_path = self._drop_audio_path(event)
        if audio_path is not None:
            event.acceptProposedAction()
            self.addedAudioDropped.emit(audio_path, drop_t, lane)
            return
        video_path = self._drop_video_path(event)
        if video_path is not None:
            event.acceptProposedAction()
            self.videoFileDropped.emit(video_path, drop_t)

    def _drop_audio_path(self, event) -> str | None:  # noqa: ANN001
        return self._first_path_with_ext(event, self._AUDIO_EXTS)

    def _drop_video_path(self, event) -> str | None:  # noqa: ANN001
        return self._first_path_with_ext(event, self._VIDEO_EXTS)

    def _first_path_with_ext(self, event, exts: set[str]) -> str | None:  # noqa: ANN001
        md = event.mimeData()
        if not md.hasUrls():
            return None
        for u in md.urls():
            p = u.toLocalFile()
            if p and any(p.lower().endswith(ext) for ext in exts):
                return p
        return None

    def _find(self, clip_id: str) -> Clip | None:
        return next((c for c in self._clips if c.id == clip_id), None)

    def sizeHint(self) -> QSize:
        audio_total = sum(self._audio_lane_heights) + TRACK_GAP * len(self._audio_lane_heights)
        return QSize(900, RULER_H + TRACK_GAP + self._video_h + audio_total + 4)


def fit_pixels_per_second(
    total_length: float,
    track_width: int,
    current_pps: float,
) -> float | None:
    """Return the zoom that makes `total_length` seconds (plus the trailing
    pad the scroll range already grants) fit `track_width` pixels, or None
    when there is nothing to fit.

    Pure math, split out of the widget so the policy is testable without
    geometry. Two rules beyond the division:

    * it never zooms *in* - `min(current_pps, ...)` keeps this an
      orientation command rather than a "maximise the clip" one, so a short
      project is left at the zoom the user chose;
    * it never leaves the existing zoom bounds. A project too long to fit
      even at `MIN_PPS` lands on that legal minimum and stays scrollable.
    """
    if total_length <= 0.0 or track_width <= 0:
        return None
    raw = track_width / (total_length + SCROLL_TAIL_PAD_S)
    return max(MIN_PPS, min(MAX_PPS, min(current_pps, raw)))


def _choose_tick_step(pps: float) -> float:
    """Return a reasonable tick step (seconds) based on the current zoom."""
    for step in (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600):
        if step * pps >= 70:
            return step
    return 600


def _fmt_tick(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m = int(seconds // 60)
    s = seconds - m * 60
    if seconds < 60:
        return f"{s:.2f}s" if s < 10 else f"{s:.1f}s"
    return f"{m}:{int(s):02d}"
