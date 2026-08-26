"""Tab 2B: timeline edge auto-scroll while a time-axis drag is in progress.

Long moves used to need drag / drop / scroll / drag again. Holding the pointer
inside a narrow band at the left or right edge of the visible track area now
pans the viewport on a timer, and the active drag is recalculated against each
new viewport position so the dragged object keeps following timeline time.

Qt runs on the ``offscreen`` platform plugin so widget geometry - and therefore
``_track_rect()`` and ``scroll_max_px()`` - is real rather than mocked. No test
waits on wall-clock timer intervals: the tick handler is driven directly.
"""
from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor.clip import AddedAudio, Clip, MediaAsset  # noqa: E402
from cove_video_editor import timeline_widget as tw  # noqa: E402
from cove_video_editor.timeline_widget import TimelineWidget  # noqa: E402


_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# ---- fixtures --------------------------------------------------------------

# Widget geometry used by every fixture below. `_track_rect()` is
# QRect(LEFT_PAD, 0, W - LEFT_PAD - RIGHT_PAD, H), so with W = 800 the visible
# track spans widget x in [8, 791] and is 784 px wide.
WIDGET_W = 800
WIDGET_H = 300
VIEW_LEFT = tw.LEFT_PAD                                  # 8
VIEW_W = WIDGET_W - tw.LEFT_PAD - tw.RIGHT_PAD           # 784

X_CENTER = VIEW_LEFT + VIEW_W // 2                       # 400 - no edge zone
X_RIGHT_ZONE = VIEW_LEFT + VIEW_W - 12                   # 780 - inside right zone
X_LEFT_ZONE = VIEW_LEFT + 12                             # 20  - inside left zone

Y_RULER = 10
Y_VIDEO = tw.RULER_H + tw.TRACK_GAP + 20                 # 54 - on the video track
Y_LANE1 = (tw.RULER_H + tw.TRACK_GAP + tw.VIDEO_TRACK_H
           + tw.TRACK_GAP + tw.AUDIO_LANE_0_H + tw.TRACK_GAP + 20)  # lane 1


def _asset(duration: float = 900.0, name: str = "a.mp4") -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=duration, width=1920, height=1080,
        fps=30.0, has_audio=True,
    )


def _clip(start: float, length: float, *, src_start: float = 0.0,
          name: str = "a.mp4", **kwargs) -> Clip:
    return Clip(
        asset=_asset(max(900.0, src_start + length + 300.0), name),
        timeline_start=start, src_start=src_start, src_end=src_start + length,
        **kwargs,
    )


def _mouse(kind, pos: QPointF, *, mods=Qt.NoModifier) -> QMouseEvent:
    return QMouseEvent(kind, pos, pos, Qt.LeftButton, Qt.LeftButton, mods)


def _widget(clips: list[Clip], *, pps: float = 40.0) -> TimelineWidget:
    w = TimelineWidget()
    w.resize(WIDGET_W, WIDGET_H)
    w.set_pixels_per_second(pps)
    w.set_clips(clips)
    return w


def _press(w: TimelineWidget, x: int, y: int, *, mods=Qt.NoModifier) -> None:
    w.mousePressEvent(_mouse(QEvent.MouseButtonPress, QPointF(x, y), mods=mods))


def _move(w: TimelineWidget, x: int, y: int) -> None:
    w.mouseMoveEvent(_mouse(QEvent.MouseMove, QPointF(x, y)))


def _release(w: TimelineWidget, x: int, y: int) -> None:
    w.mouseReleaseEvent(_mouse(QEvent.MouseButtonRelease, QPointF(x, y)))


# ---- Group A: pure edge-delta calculation ---------------------------------


class EdgeScrollDeltaTests(unittest.TestCase):
    """`edge_scroll_delta(rel_x, viewport_width)` is pure: viewport-relative
    pointer x in, signed px-per-tick out. Negative scrolls left."""

    def test_a1_pointer_in_the_middle_does_not_scroll(self) -> None:
        self.assertEqual(tw.edge_scroll_delta(VIEW_W // 2, VIEW_W), 0)
        self.assertEqual(tw.edge_scroll_delta(tw.EDGE_SCROLL_ZONE_PX, VIEW_W), 0)
        self.assertEqual(
            tw.edge_scroll_delta(VIEW_W - tw.EDGE_SCROLL_ZONE_PX, VIEW_W), 0,
        )

    def test_a2_pointer_in_the_left_zone_scrolls_left(self) -> None:
        self.assertLess(tw.edge_scroll_delta(12, VIEW_W), 0)

    def test_a3_pointer_in_the_right_zone_scrolls_right(self) -> None:
        self.assertGreater(tw.edge_scroll_delta(VIEW_W - 12, VIEW_W), 0)

    def test_a4_equal_penetration_is_symmetric(self) -> None:
        for depth in (1, 10, 22, 43):
            left = tw.edge_scroll_delta(tw.EDGE_SCROLL_ZONE_PX - depth, VIEW_W)
            right = tw.edge_scroll_delta(
                VIEW_W - tw.EDGE_SCROLL_ZONE_PX + depth, VIEW_W,
            )
            self.assertEqual(abs(left), abs(right), f"depth={depth}")
            self.assertLess(left, 0)
            self.assertGreater(right, 0)

    def test_a5_deeper_penetration_scrolls_at_least_as_fast(self) -> None:
        shallow = abs(tw.edge_scroll_delta(tw.EDGE_SCROLL_ZONE_PX - 1, VIEW_W))
        mid = abs(tw.edge_scroll_delta(tw.EDGE_SCROLL_ZONE_PX // 2, VIEW_W))
        deep = abs(tw.edge_scroll_delta(0, VIEW_W))
        self.assertLessEqual(shallow, mid)
        self.assertLess(mid, deep)

    def test_a6_far_outside_the_viewport_is_capped(self) -> None:
        for rel_x in (-1, -500, VIEW_W + 1, VIEW_W + 5000):
            self.assertLessEqual(
                abs(tw.edge_scroll_delta(rel_x, VIEW_W)),
                tw.EDGE_SCROLL_MAX_PX, f"rel_x={rel_x}",
            )

    def test_a7_narrow_viewport_zones_cannot_overlap(self) -> None:
        # Viewport narrower than two full zones: the zone shrinks to half the
        # viewport so no pointer position can command both directions.
        narrow = 60
        seen_left = seen_right = False
        for rel_x in range(-10, narrow + 10):
            d = tw.edge_scroll_delta(rel_x, narrow)
            self.assertLessEqual(abs(d), tw.EDGE_SCROLL_MAX_PX)
            if rel_x < narrow // 2:
                self.assertLessEqual(d, 0, f"rel_x={rel_x}")
                seen_left = seen_left or d < 0
            elif rel_x > narrow // 2:
                self.assertGreaterEqual(d, 0, f"rel_x={rel_x}")
                seen_right = seen_right or d > 0
        self.assertTrue(seen_left and seen_right)
        self.assertEqual(tw.edge_scroll_delta(narrow // 2, narrow), 0)

    def test_a8_degenerate_viewport_never_scrolls(self) -> None:
        for width in (0, -5, 1):
            self.assertEqual(tw.edge_scroll_delta(0, width), 0, f"w={width}")


# ---- Group B: arm / disarm -------------------------------------------------


class ArmDisarmTests(unittest.TestCase):
    """The timer only runs while a supported drag is holding the pointer in an
    edge zone with room left to scroll in that direction."""

    def _dragging_clip(self, *, scroll_x: int = 0) -> TimelineWidget:
        w = _widget([_clip(0.0, 600.0)])
        w.set_scroll_x(scroll_x)
        _press(w, X_CENTER, Y_VIDEO)
        self.assertEqual(w._drag.mode, "move_clip")
        return w

    def test_b1_drag_into_the_right_zone_arms_the_timer(self) -> None:
        w = self._dragging_clip()
        self.assertFalse(w._autoscroll_timer.isActive())
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())

    def test_b2_returning_to_the_centre_disarms_immediately(self) -> None:
        w = self._dragging_clip()
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        _move(w, X_CENTER, Y_VIDEO)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_b3_drag_into_the_left_zone_arms_when_scrolled_in(self) -> None:
        w = self._dragging_clip(scroll_x=4000)
        _move(w, X_LEFT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())

    def test_b4_mouse_release_stops_the_timer(self) -> None:
        w = self._dragging_clip()
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        _release(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_b5_cancelling_the_drag_stops_the_timer(self) -> None:
        w = self._dragging_clip()
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        w._end_drag()
        self.assertFalse(w._autoscroll_timer.isActive())
        self.assertEqual(w._drag.mode, "")

    def test_b6_left_bound_does_not_leave_a_useless_timer_running(self) -> None:
        w = self._dragging_clip(scroll_x=0)
        self.assertEqual(w._scroll_x, 0)
        _move(w, X_LEFT_ZONE, Y_VIDEO)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_b7_right_bound_does_not_leave_a_useless_timer_running(self) -> None:
        w = self._dragging_clip()
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        w.set_scroll_x(w.scroll_max_px())
        w._update_edge_autoscroll()
        self.assertEqual(w._edge_scroll_step(), 0)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_b8_hovering_an_edge_without_a_drag_never_arms(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertEqual(w._drag.mode, "")
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_b9_content_narrower_than_the_viewport_never_arms(self) -> None:
        w = _widget([_clip(0.0, 2.0)], pps=40.0)
        self.assertEqual(w.scroll_max_px(), 0)
        _press(w, 20, Y_VIDEO)
        # Set the pointer without replaying the drag: this is the "no room in
        # either direction" predicate, not the clip-move that would itself
        # extend the sequence and create room.
        w._drag.moved = True
        w._drag_pos = QPoint(X_RIGHT_ZONE, Y_VIDEO)
        self.assertEqual(w._edge_scroll_step(), 0)
        w._update_edge_autoscroll()
        self.assertFalse(w._autoscroll_timer.isActive())
        w._drag_pos = QPoint(X_LEFT_ZONE, Y_VIDEO)
        self.assertEqual(w._edge_scroll_step(), 0)

    def test_b12_near_edge_jitter_below_the_click_slop_never_arms(self) -> None:
        # Auto-scroll defers to the widget's existing click-slop threshold: a
        # near-edge click with a couple of pixels of jitter must neither pan
        # the view nor - through the tick replay - become an edit.
        w = _widget([_clip(0.0, 600.0)])
        before = (w._scroll_x, w._clips[0].timeline_start)
        _press(w, X_RIGHT_ZONE, Y_VIDEO)
        _move(w, X_RIGHT_ZONE + 3, Y_VIDEO + 2)
        self.assertFalse(w._drag.moved)
        self.assertEqual(w._edge_scroll_step(), 0)
        self.assertFalse(w._autoscroll_timer.isActive())
        w._edge_autoscroll_tick()
        self.assertEqual((w._scroll_x, w._clips[0].timeline_start), before)

    def test_b12b_crossing_the_click_slop_arms_normally(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_RIGHT_ZONE, Y_VIDEO)
        _move(w, X_RIGHT_ZONE - (tw.CLICK_SLOP_PX + 1), Y_VIDEO)
        self.assertTrue(w._drag.moved)
        self.assertTrue(w._autoscroll_timer.isActive())

    def test_b13_right_click_during_a_drag_disarms(self) -> None:
        # `QMenu.exec()` spins a nested loop; the timer must not keep editing
        # behind the menu. The drag itself survives.
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        # `_show_context_menu()` returns None and blocks on `QMenu.exec()`;
        # stub only that call so the right-button branch itself is exercised.
        # The disarm happens before the menu is shown, so ordering is covered.
        with unittest.mock.patch.object(
            TimelineWidget, "_show_context_menu", return_value=None,
        ) as shown:
            w.mousePressEvent(QMouseEvent(
                QEvent.MouseButtonPress,
                QPointF(X_RIGHT_ZONE, Y_VIDEO), QPointF(X_RIGHT_ZONE, Y_VIDEO),
                Qt.RightButton, Qt.RightButton, Qt.NoModifier,
            ))
        self.assertEqual(shown.call_count, 1)
        self.assertFalse(w._autoscroll_timer.isActive())
        self.assertIsNone(w._drag_pos)
        self.assertEqual(w._drag.mode, "move_clip")

    def test_b10_vertical_track_resize_drag_never_arms(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        gap_y = w._va_divider_rect().center().y()
        _press(w, X_CENTER, gap_y)
        self.assertEqual(w._drag.mode, "resize_tracks")
        _move(w, X_RIGHT_ZONE, gap_y + 4)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_b11_playhead_seek_drag_never_arms(self) -> None:
        # Seeking already pans the view through `_ensure_visible()`; a second
        # scroll source would compound.
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_CENTER, Y_RULER)
        self.assertEqual(w._drag.mode, "seek")
        _move(w, X_RIGHT_ZONE, Y_RULER)
        self.assertFalse(w._autoscroll_timer.isActive())


# ---- Group C: one timer step ----------------------------------------------


class TimerStepTests(unittest.TestCase):

    def _armed_right(self) -> TimelineWidget:
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        return w

    def test_c1_a_right_tick_scrolls_right_within_bounds(self) -> None:
        w = self._armed_right()
        before = w._scroll_x
        w._edge_autoscroll_tick()
        self.assertGreater(w._scroll_x, before)
        self.assertLessEqual(w._scroll_x, w.scroll_max_px())

    def test_c2_a_left_tick_scrolls_left_within_bounds(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        w.set_scroll_x(4000)
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_LEFT_ZONE, Y_VIDEO)
        before = w._scroll_x
        w._edge_autoscroll_tick()
        self.assertLess(w._scroll_x, before)
        self.assertGreaterEqual(w._scroll_x, 0)

    def test_c3_a_tick_reapplies_the_active_drag_at_the_new_viewport(self) -> None:
        w = self._armed_right()
        c = w._clips[0]
        grab = w._drag.grab_offset_s
        before_start = c.timeline_start
        w._edge_autoscroll_tick()
        # Pointer never moved, but the timeline time under it did.
        expected = w._x_to_time(X_RIGHT_ZONE) - grab
        self.assertGreater(c.timeline_start, before_start)
        self.assertAlmostEqual(c.timeline_start, expected, places=6)

    def _armed_trim_l(self) -> TimelineWidget:
        """A trim-left drag held in the right edge zone. Unlike a clip move,
        trimming anchors the clip's right edge, so the sequence length - and
        therefore `scroll_max_px()` - stays put and the bound is reachable."""
        c = _clip(100.0, 100.0, src_start=50.0)
        w = _widget([c])
        w.select_clip(c.id)
        w.set_scroll_x(int(100.0 * 40.0 + tw.LEFT_PAD - X_RIGHT_ZONE))
        # Grab the handle just inside HANDLE_W, then move onto it: the drag
        # must cross CLICK_SLOP_PX before auto-scroll may arm.
        _press(w, X_RIGHT_ZONE - 8, Y_VIDEO)
        self.assertEqual(w._drag.mode, "trim_l")
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        return w

    def test_c4_a_tick_that_reaches_the_bound_disarms(self) -> None:
        w = self._armed_trim_l()
        limit = w.scroll_max_px()
        w.set_scroll_x(limit - 1)
        w._edge_autoscroll_tick()
        self.assertEqual(w._scroll_x, limit)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_c5_a_tick_with_no_room_mutates_nothing(self) -> None:
        w = self._armed_right()
        w.set_scroll_x(w.scroll_max_px())
        c = w._clips[0]
        before = (w._scroll_x, c.timeline_start, c.src_start, c.src_end)
        w._edge_autoscroll_tick()
        self.assertEqual(
            (w._scroll_x, c.timeline_start, c.src_start, c.src_end), before,
        )
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_c6_scroll_stays_clamped_across_many_ticks(self) -> None:
        w = self._armed_trim_l()
        for _ in range(400):
            w._edge_autoscroll_tick()
            self.assertGreaterEqual(w._scroll_x, 0)
            self.assertLessEqual(w._scroll_x, w.scroll_max_px())
            if not w._autoscroll_timer.isActive():
                break
        else:
            self.fail("auto-scroll never reached the right bound")


# ---- Group D: visual clip drag continuity ---------------------------------


class ClipDragContinuityTests(unittest.TestCase):

    def test_d1_rightward_edge_drag_keeps_tracking_the_pointer(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        c = w._clips[0]
        _press(w, X_CENTER, Y_VIDEO)
        grab = w._drag.grab_offset_s
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        for _ in range(5):
            w._edge_autoscroll_tick()
            self.assertAlmostEqual(
                c.timeline_start, w._x_to_time(X_RIGHT_ZONE) - grab, places=6,
            )
        self.assertGreater(w._scroll_x, 0)

    def test_d2_leftward_edge_drag_keeps_tracking_the_pointer(self) -> None:
        # The clip starts well away from t=0 so the leftward drag is not
        # pinned by the `max(0.0, ...)` floor in the move path.
        w = _widget([_clip(100.0, 400.0)])
        c = w._clips[0]
        w.set_scroll_x(6000)
        _press(w, X_CENTER, Y_VIDEO)
        grab = w._drag.grab_offset_s
        _move(w, X_LEFT_ZONE, Y_VIDEO)
        start_scroll = w._scroll_x
        for _ in range(5):
            w._edge_autoscroll_tick()
            self.assertAlmostEqual(
                c.timeline_start, w._x_to_time(X_LEFT_ZONE) - grab, places=6,
            )
        self.assertLess(w._scroll_x, start_scroll)

    def test_d3_a_long_edge_drag_is_still_one_logical_edit(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        moves: list[tuple[str, float]] = []
        w.clipMoved.connect(lambda cid, t: moves.append((cid, t)))
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        for _ in range(20):
            w._edge_autoscroll_tick()
        self.assertEqual(moves, [], "ticks must not commit the move")
        _release(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertEqual(len(moves), 1, "exactly one committed move")

    def test_d4_autoscroll_does_not_ripple_other_clips(self) -> None:
        # Focused Tab 2A guard: nothing in the auto-scroll plumbing emulates
        # Move to Start. Untouched clips keep their positions during the drag.
        others = [_clip(700.0, 20.0, name="b.mp4"), _clip(800.0, 20.0, name="c.mp4")]
        w = _widget([_clip(0.0, 600.0)] + others)
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        for _ in range(10):
            w._edge_autoscroll_tick()
        self.assertEqual([c.timeline_start for c in others], [700.0, 800.0])


# ---- Group E: AddedAudio drag ---------------------------------------------


def _added(offset: float = 0.0, duration: float = 400.0,
           lane: int = 1, volume: float = 1.4) -> AddedAudio:
    # `peaks` must be non-empty: `_hit_added_audio()` skips tiles without a
    # drawn waveform, so an empty-peaks fixture would not be draggable at all.
    return AddedAudio(path=Path("bed.mp3"), duration=duration, rate=48000,
                      peaks=[0.5] * 128, offset=offset, lane=lane,
                      volume=volume)


class AddedAudioDragTests(unittest.TestCase):

    def _dragging_added(self) -> tuple[TimelineWidget, AddedAudio, float]:
        w = _widget([_clip(0.0, 600.0)])
        w.set_added_audios([_added(offset=0.0)])
        # `set_added_audios()` clones, so assert against the widget's entry.
        audio = w._added_audios[0]
        _press(w, X_CENTER, Y_LANE1)
        self.assertEqual(w._drag.mode, "move_added")
        return w, audio, w._drag.grab_offset_s

    def test_e1_added_audio_edge_drag_arms_and_scrolls(self) -> None:
        w, _audio, _grab = self._dragging_added()
        _move(w, X_RIGHT_ZONE, Y_LANE1)
        self.assertTrue(w._autoscroll_timer.isActive())
        before = w._scroll_x
        w._edge_autoscroll_tick()
        self.assertGreater(w._scroll_x, before)

    def test_e2_added_audio_offset_follows_the_pointer(self) -> None:
        w, audio, grab = self._dragging_added()
        _move(w, X_RIGHT_ZONE, Y_LANE1)
        for _ in range(5):
            w._edge_autoscroll_tick()
            self.assertAlmostEqual(
                audio.offset, w._x_to_time(X_RIGHT_ZONE) - grab, places=6,
            )

    def test_e3_volume_and_source_metadata_are_untouched(self) -> None:
        w, audio, _grab = self._dragging_added()
        before = (audio.volume, audio.src_start, audio.src_end,
                  audio.duration, audio.lane)
        _move(w, X_RIGHT_ZONE, Y_LANE1)
        for _ in range(5):
            w._edge_autoscroll_tick()
        self.assertEqual(
            (audio.volume, audio.src_start, audio.src_end,
             audio.duration, audio.lane), before,
        )

    def test_e4_no_visual_clip_is_moved(self) -> None:
        w, _audio, _grab = self._dragging_added()
        c = w._clips[0]
        _move(w, X_RIGHT_ZONE, Y_LANE1)
        for _ in range(5):
            w._edge_autoscroll_tick()
        self.assertEqual(c.timeline_start, 0.0)


# ---- Group F: trim handles ------------------------------------------------


class TrimHandleTests(unittest.TestCase):
    """A clip at t=100s..200s, scrolled so a chosen handle sits in the right
    edge zone. Trimming must keep its existing semantics while the view pans."""

    def _trim_widget(self, handle_t: float) -> tuple[TimelineWidget, Clip]:
        c = _clip(100.0, 100.0, src_start=50.0)
        w = _widget([c])
        w.select_clip(c.id)
        # Put the requested handle under X_RIGHT_ZONE.
        w.set_scroll_x(int(handle_t * 40.0 + tw.LEFT_PAD - X_RIGHT_ZONE))
        return w, c

    def test_f1_trim_left_near_the_edge_scrolls_and_tracks(self) -> None:
        w, c = self._trim_widget(100.0)
        # Grab the handle just inside HANDLE_W, then move onto it: the drag
        # must cross CLICK_SLOP_PX before auto-scroll may arm.
        _press(w, X_RIGHT_ZONE - 8, Y_VIDEO)
        self.assertEqual(w._drag.mode, "trim_l")
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        before_src, before_end = c.src_start, c.timeline_end
        w._edge_autoscroll_tick()
        self.assertGreater(c.src_start, before_src)
        self.assertAlmostEqual(
            c.src_start, 50.0 + (w._x_to_time(X_RIGHT_ZONE) - 100.0), places=6,
        )
        # trim_l anchors the right edge - unchanged Tab 2A/earlier semantics.
        self.assertAlmostEqual(c.timeline_end, before_end, places=6)

    def _trim_r_widget(self) -> tuple[TimelineWidget, Clip]:
        """Clip A at 100s..400s with its right handle parked in the *left*
        edge zone. `_apply_trim("trim_r")` clamps to `min(timeline_end, t)`,
        so a right trim only ever shortens - the assisted direction is left.
        Clip B keeps the sequence long enough to scroll that far in."""
        a = _clip(100.0, 300.0, src_start=50.0)
        b = _clip(900.0, 20.0, name="b.mp4")
        w = _widget([a, b])
        w.select_clip(a.id)
        w.set_scroll_x(int(400.0 * 40.0 + tw.LEFT_PAD - X_LEFT_ZONE))
        return w, a

    def test_f2_trim_right_near_the_edge_scrolls_and_tracks(self) -> None:
        w, c = self._trim_r_widget()
        _press(w, X_LEFT_ZONE - 8, Y_VIDEO)
        self.assertEqual(w._drag.mode, "trim_r")
        _move(w, X_LEFT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        before_src_end, before_start = c.src_end, c.timeline_start
        w._edge_autoscroll_tick()
        self.assertLess(c.src_end, before_src_end)
        self.assertAlmostEqual(
            c.src_end, 50.0 + (w._x_to_time(X_LEFT_ZONE) - 100.0), places=6,
        )
        # trim_r never moves the clip's start.
        self.assertAlmostEqual(c.timeline_start, before_start, places=6)

    def test_f3_trim_never_commits_a_range_change_on_a_tick(self) -> None:
        w, c = self._trim_r_widget()
        ranges: list[str] = []
        w.rangeChanged.connect(lambda cid, a, b: ranges.append(cid))
        _press(w, X_LEFT_ZONE - 8, Y_VIDEO)
        _move(w, X_LEFT_ZONE, Y_VIDEO)
        for _ in range(10):
            w._edge_autoscroll_tick()
        self.assertEqual(ranges, [])
        _release(w, X_LEFT_ZONE, Y_VIDEO)
        self.assertEqual(ranges, [c.id])

    def test_f4_trim_left_leaves_the_leading_gap_alone(self) -> None:
        w, c = self._trim_widget(100.0)
        _press(w, X_RIGHT_ZONE - 8, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        for _ in range(10):
            w._edge_autoscroll_tick()
        self.assertGreater(c.timeline_start, 0.0,
                           "auto-scroll must not emulate Move to Start")

    def test_f5_added_audio_trim_near_the_edge_scrolls(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        w.set_added_audios([_added(offset=100.0, duration=400.0)])
        audio = w._added_audios[0]
        w._added_audio_selected_id = audio.id
        w.set_scroll_x(int(100.0 * 40.0 + tw.LEFT_PAD - X_RIGHT_ZONE))
        _press(w, X_RIGHT_ZONE - 8, Y_LANE1)
        self.assertEqual(w._drag.mode, "trim_added_l")
        _move(w, X_RIGHT_ZONE, Y_LANE1)
        self.assertTrue(w._autoscroll_timer.isActive())
        before = (w._scroll_x, audio.src_start)
        w._edge_autoscroll_tick()
        self.assertGreater(w._scroll_x, before[0])
        self.assertGreater(audio.src_start, before[1])
        self.assertEqual(audio.volume, 1.4)


# ---- Group G: region / selection drag -------------------------------------


class SelectionDragTests(unittest.TestCase):

    def _selecting(self) -> TimelineWidget:
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_CENTER, Y_VIDEO, mods=Qt.ShiftModifier)
        self.assertEqual(w._drag.mode, "select")
        return w

    def test_g1_selection_drag_near_the_edge_scrolls(self) -> None:
        w = self._selecting()
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        before = w._scroll_x
        w._edge_autoscroll_tick()
        self.assertGreater(w._scroll_x, before)

    def test_g2_selection_endpoint_follows_the_new_visible_time(self) -> None:
        w = self._selecting()
        anchor = w._drag.anchor_t
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        for _ in range(5):
            w._edge_autoscroll_tick()
            self.assertAlmostEqual(w.selection()[0], anchor, places=6)
            self.assertAlmostEqual(
                w.selection()[1], w._x_to_time(X_RIGHT_ZONE), places=6,
            )

    def test_g3_selection_writes_stay_on_the_centralised_path(self) -> None:
        w = self._selecting()
        calls: list[tuple[float, float]] = []
        real = w._set_selection_span

        def spy(a: float, b: float) -> None:
            calls.append((a, b))
            real(a, b)

        w._set_selection_span = spy  # type: ignore[method-assign]
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        calls.clear()
        w._edge_autoscroll_tick()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(w._scroll_x, w.scroll_max_px())


# ---- Group H: timer lifecycle ---------------------------------------------


class TimerLifecycleTests(unittest.TestCase):

    def test_h1_finishing_a_drag_leaves_the_timer_inactive(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        w._edge_autoscroll_tick()
        _release(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertFalse(w._autoscroll_timer.isActive())
        self.assertIsNone(w._drag_pos)

    def test_h2_a_second_drag_inherits_no_stale_pointer_or_direction(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        w._edge_autoscroll_tick()
        _release(w, X_RIGHT_ZONE, Y_VIDEO)

        scroll_after_first = w._scroll_x
        _press(w, X_CENTER, Y_VIDEO)
        self.assertIsNone(w._drag_pos)
        self.assertFalse(w._autoscroll_timer.isActive())
        # A tick that leaks through before any movement must do nothing.
        w._edge_autoscroll_tick()
        self.assertEqual(w._scroll_x, scroll_after_first)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_h3_cancelling_clears_the_remembered_pointer(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertEqual(w._drag_pos, QPoint(X_RIGHT_ZONE, Y_VIDEO))
        w._end_drag()
        self.assertIsNone(w._drag_pos)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_h4_hiding_the_widget_mid_drag_stops_the_timer(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        w.show()
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertTrue(w._autoscroll_timer.isActive())
        w.hide()
        self.assertFalse(w._autoscroll_timer.isActive())
        self.assertIsNone(w._drag_pos)

    def test_h4b_hiding_mid_drag_does_not_abandon_the_edit(self) -> None:
        # Hiding disarms auto-scroll only. The drag survives, so the release
        # still commits through the pre-existing path - no half-applied clip
        # move without overlap resolution or a controller update.
        w = _widget([_clip(0.0, 600.0)])
        moves: list[tuple[str, float]] = []
        w.clipMoved.connect(lambda cid, t: moves.append((cid, t)))
        w.show()
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        w._edge_autoscroll_tick()
        moved_to = w._clips[0].timeline_start
        w.hide()
        self.assertEqual(w._drag.mode, "move_clip")
        self.assertEqual(w._clips[0].timeline_start, moved_to)
        _release(w, X_RIGHT_ZONE, Y_VIDEO)
        self.assertEqual(len(moves), 1)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_h5_the_timer_is_a_single_shared_instance(self) -> None:
        w = _widget([_clip(0.0, 600.0)])
        timer = w._autoscroll_timer
        self.assertEqual(timer.interval(), tw.EDGE_SCROLL_INTERVAL_MS)
        for _ in range(3):
            _press(w, X_CENTER, Y_VIDEO)
            _move(w, X_RIGHT_ZONE, Y_VIDEO)
            _move(w, X_CENTER, Y_VIDEO)
            _release(w, X_CENTER, Y_VIDEO)
        self.assertIs(w._autoscroll_timer, timer)
        self.assertFalse(timer.isActive())


# ---- Group I: orphaned drag (mouse-grab loss / window deactivation) --------


def _send(w: TimelineWidget, etype: QEvent.Type) -> None:
    QApplication.instance().sendEvent(w, QEvent(etype))


ORPHAN_EVENTS = (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate)


class OrphanedDragTests(unittest.TestCase):
    """Qt does not guarantee a `mouseReleaseEvent()` once the implicit mouse
    grab is lost (a popup steals it, the window deactivates). Without a hook
    the auto-scroll timer would keep running against a stale drag.

    Chosen semantics: finalize in place - keep whatever the live drag has
    already applied, terminate the interaction, emit no commit signal (so no
    new undo snapshot), and never roll back.
    """

    def _dragging(self) -> tuple[TimelineWidget, Clip, float]:
        w = _widget([_clip(0.0, 600.0)])
        c = w._clips[0]
        _press(w, X_CENTER, Y_VIDEO)
        _move(w, X_RIGHT_ZONE, Y_VIDEO)
        w._edge_autoscroll_tick()
        self.assertTrue(w._autoscroll_timer.isActive())
        self.assertGreater(c.timeline_start, 0.0)
        return w, c, c.timeline_start

    def test_i1_ungrab_mouse_terminates_the_drag_in_place(self) -> None:
        w, c, applied = self._dragging()
        _send(w, QEvent.Type.UngrabMouse)
        self.assertFalse(w._autoscroll_timer.isActive())
        self.assertEqual(w._drag.mode, "")
        self.assertIsNone(w._drag_pos)
        self.assertEqual(c.timeline_start, applied, "position must be kept")

    def test_i2_window_deactivate_terminates_the_drag_in_place(self) -> None:
        w, c, applied = self._dragging()
        _send(w, QEvent.Type.WindowDeactivate)
        self.assertFalse(w._autoscroll_timer.isActive())
        self.assertEqual(w._drag.mode, "")
        self.assertIsNone(w._drag_pos)
        self.assertEqual(c.timeline_start, applied)

    def test_i3_orphan_termination_commits_exactly_once(self) -> None:
        # An orphaned drag finishes exactly as a release would: one commit
        # signal, therefore exactly the one app-side `_snapshot()` the drag
        # itself deserves - and no extra orphan-cleanup step.
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w, _c, _applied = self._dragging()
                fired: list[str] = []
                w.clipMoved.connect(lambda cid, t: fired.append("clipMoved"))
                w.rangeChanged.connect(lambda *a: fired.append("rangeChanged"))
                w.audioOffsetChanged.connect(lambda *a: fired.append("audioOffset"))
                w.addedAudioOffsetChanged.connect(
                    lambda *a: fired.append("addedAudioOffset"))
                _send(w, etype)
                self.assertEqual(fired, ["clipMoved"])

    def test_i4_orphan_before_any_movement_terminates_cleanly(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w = _widget([_clip(0.0, 600.0)])
                fired: list[str] = []
                w.clipMoved.connect(lambda cid, t: fired.append("clipMoved"))
                _press(w, X_CENTER, Y_VIDEO)
                before = w._clips[0].timeline_start
                _send(w, etype)
                self.assertEqual(w._drag.mode, "")
                self.assertIsNone(w._drag_pos)
                self.assertFalse(w._autoscroll_timer.isActive())
                self.assertEqual(w._clips[0].timeline_start, before)
                self.assertEqual(fired, [])

    def test_i5_a_later_release_is_a_no_op(self) -> None:
        # Cleanup is idempotent: a release that still arrives afterwards must
        # not re-apply, re-commit, or restart anything - and must not raise.
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w, c, applied = self._dragging()
                fired: list[str] = []
                w.clipMoved.connect(lambda cid, t: fired.append("clipMoved"))
                _send(w, etype)
                _send(w, etype)  # idempotent on repeat
                _release(w, X_RIGHT_ZONE, Y_VIDEO)
                # One commit total: the orphan's. The repeat event and the
                # late release must not add a second.
                self.assertEqual(fired, ["clipMoved"])
                self.assertEqual(c.timeline_start, applied)
                self.assertFalse(w._autoscroll_timer.isActive())
                self.assertEqual(w._drag.mode, "")

    def test_i6_orphan_events_while_idle_touch_nothing(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w = _widget([_clip(0.0, 600.0)])
                w.set_scroll_x(2000)
                before = (w._scroll_x, w._clips[0].timeline_start,
                          w.selection(), w.playhead())
                _send(w, etype)
                self.assertEqual(
                    (w._scroll_x, w._clips[0].timeline_start,
                     w.selection(), w.playhead()), before,
                )
                self.assertFalse(w._autoscroll_timer.isActive())

    def test_i7_a_drag_after_an_orphan_starts_clean(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w, c, _applied = self._dragging()
                _send(w, etype)

                moves: list[float] = []
                w.clipMoved.connect(lambda cid, t: moves.append(t))
                _press(w, X_CENTER, Y_VIDEO)
                self.assertIsNone(w._drag_pos)
                self.assertFalse(w._autoscroll_timer.isActive())
                self.assertEqual(w._drag.mode, "move_clip")
                _move(w, X_LEFT_ZONE, Y_VIDEO)
                # Direction is recomputed from the new pointer, not inherited.
                self.assertLess(w._edge_scroll_step(), 0)
                _release(w, X_LEFT_ZONE, Y_VIDEO)
                self.assertEqual(len(moves), 1)
                self.assertFalse(w._autoscroll_timer.isActive())


# ---- Group J: orphan finalization runs the normal release semantics -------


class OrphanFinalizationTests(unittest.TestCase):
    """An abnormal termination must finish the drag exactly as a mouse release
    would: same mode-specific commit, same sorting/overlap normalization, same
    single undo snapshot. Anything less silently drops the edit - added audio
    especially, since the widget holds clones the app never sees otherwise."""

    # --- visual clips (shared objects with the app model) ---

    def test_j1_orphan_clip_move_commits_and_normalizes(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                a = _clip(0.0, 100.0, name="a.mp4")
                b = _clip(120.0, 100.0, name="b.mp4")
                w = _widget([a, b])
                moves: list[tuple[str, float]] = []
                w.clipMoved.connect(lambda cid, t: moves.append((cid, t)))
                # Drag A rightward until it overlaps B.
                _press(w, w._time_to_x(10.0), Y_VIDEO)
                self.assertEqual(w._drag.clip_id, a.id)
                _move(w, w._time_to_x(140.0), Y_VIDEO)
                self.assertGreater(a.timeline_start, 0.0)
                _send(w, etype)
                self.assertEqual(len(moves), 1)
                # Overlap resolution ran, exactly as on release.
                self.assertFalse(
                    a.timeline_start < b.timeline_end
                    and b.timeline_start < a.timeline_end,
                    "orphan finalization must resolve overlaps",
                )
                # And the list was re-sorted.
                starts = [c.timeline_start for c in w._clips]
                self.assertEqual(starts, sorted(starts))

    def test_j2_orphan_clip_trim_commits_its_range(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                c = _clip(100.0, 100.0, src_start=50.0)
                w = _widget([c])
                w.select_clip(c.id)
                w.set_scroll_x(int(100.0 * 40.0 + tw.LEFT_PAD - X_RIGHT_ZONE))
                ranges: list[tuple[str, float, float]] = []
                w.rangeChanged.connect(
                    lambda cid, s, e: ranges.append((cid, s, e)))
                _press(w, X_RIGHT_ZONE - 8, Y_VIDEO)
                self.assertEqual(w._drag.mode, "trim_l")
                _move(w, X_RIGHT_ZONE, Y_VIDEO)
                w._edge_autoscroll_tick()
                applied = (c.src_start, c.src_end)
                _send(w, etype)
                self.assertEqual(ranges, [(c.id, applied[0], applied[1])])
                self.assertEqual((c.src_start, c.src_end), applied)

    # --- added audio (widget holds clones; only the signal reaches the app) ---

    def _app_backed_audio(
        self, offset: float = 0.0,
    ) -> tuple[TimelineWidget, list[AddedAudio]]:
        """Widget plus a stand-in app model wired the way `app.py` wires it:
        `_on_added_audio_offset_changed` / `_on_added_audio_range_changed`
        copy the widget's clone values back onto the app's own objects."""
        app_model = [_added(offset=offset, duration=400.0)]
        w = _widget([_clip(0.0, 600.0)])
        w.set_added_audios(app_model)

        def on_offset(audio_id: str, offset: float) -> None:
            for a in app_model:
                if a.id == audio_id:
                    a.offset = max(0.0, float(offset))

        def on_range(audio_id: str) -> None:
            tl = next((a for a in w._added_audios if a.id == audio_id), None)
            for a in app_model:
                if a.id == audio_id and tl is not None:
                    a.src_start, a.src_end, a.offset = (
                        tl.src_start, tl.src_end, tl.offset)

        w.addedAudioOffsetChanged.connect(on_offset)
        w.addedAudioRangeChanged.connect(on_range)
        return w, app_model

    def test_j3_orphan_added_audio_move_survives_a_refresh(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w, app_model = self._app_backed_audio()
                _press(w, X_CENTER, Y_LANE1)
                self.assertEqual(w._drag.mode, "move_added")
                _move(w, X_CENTER + 200, Y_LANE1)
                applied = w._added_audios[0].offset
                self.assertGreater(applied, 0.0)

                _send(w, etype)
                self.assertAlmostEqual(app_model[0].offset, applied, places=6)
                # The real test: a refresh re-clones from the app model.
                w.set_added_audios(app_model)
                self.assertAlmostEqual(
                    w._added_audios[0].offset, applied, places=6,
                    msg="added-audio move must not be lost on refresh",
                )

    def test_j4_orphan_added_audio_trim_survives_a_refresh(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w, app_model = self._app_backed_audio(offset=100.0)
                w._added_audio_selected_id = w._added_audios[0].id
                w.set_scroll_x(int(100.0 * 40.0 + tw.LEFT_PAD - X_RIGHT_ZONE))
                _press(w, X_RIGHT_ZONE - 8, Y_LANE1)
                self.assertEqual(w._drag.mode, "trim_added_l")
                _move(w, X_RIGHT_ZONE, Y_LANE1)
                w._edge_autoscroll_tick()
                applied = w._added_audios[0].src_start
                self.assertGreater(applied, 0.0)

                _send(w, etype)
                self.assertAlmostEqual(app_model[0].src_start, applied, places=6)
                w.set_added_audios(app_model)
                self.assertAlmostEqual(
                    w._added_audios[0].src_start, applied, places=6,
                    msg="added-audio trim must not be lost on refresh",
                )

    def test_j5_orphan_added_audio_keeps_volume_and_lane(self) -> None:
        w, app_model = self._app_backed_audio()
        _press(w, X_CENTER, Y_LANE1)
        _move(w, X_CENTER + 200, Y_LANE1)
        _send(w, QEvent.Type.UngrabMouse)
        w.set_added_audios(app_model)
        self.assertEqual(app_model[0].volume, 1.4)
        self.assertEqual(app_model[0].lane, 1)
        self.assertEqual(w._clips[0].timeline_start, 0.0)

    # --- one snapshot, and release parity ---

    def test_j6_orphan_creates_exactly_one_snapshot_worth_of_commits(self) -> None:
        # `_snapshot()` is driven by these signals app-side, so counting them
        # is the widget-level proxy for "exactly one undo step".
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w, app_model = self._app_backed_audio()
                fired: list[str] = []
                w.addedAudioOffsetChanged.connect(lambda *a: fired.append("off"))
                w.addedAudioRangeChanged.connect(lambda *a: fired.append("rng"))
                _press(w, X_CENTER, Y_LANE1)
                _move(w, X_RIGHT_ZONE, Y_LANE1)
                for _ in range(5):
                    w._edge_autoscroll_tick()
                _send(w, etype)
                _send(w, etype)
                _release(w, X_RIGHT_ZONE, Y_LANE1)
                self.assertEqual(fired, ["off"])

    def test_j7_ordinary_release_is_unchanged(self) -> None:
        # The extraction must not alter the normal path: still exactly one
        # commit, still overlap-resolved and sorted.
        a = _clip(0.0, 100.0, name="a.mp4")
        b = _clip(120.0, 100.0, name="b.mp4")
        w = _widget([a, b])
        moves: list[tuple[str, float]] = []
        w.clipMoved.connect(lambda cid, t: moves.append((cid, t)))
        _press(w, w._time_to_x(10.0), Y_VIDEO)
        _move(w, w._time_to_x(140.0), Y_VIDEO)
        _release(w, w._time_to_x(140.0), Y_VIDEO)
        self.assertEqual(len(moves), 1)
        self.assertFalse(
            a.timeline_start < b.timeline_end
            and b.timeline_start < a.timeline_end,
        )
        starts = [c.timeline_start for c in w._clips]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(w._drag.mode, "")
        self.assertIsNone(w._drag_pos)
        self.assertFalse(w._autoscroll_timer.isActive())

    def test_j8_orphan_leaves_timer_and_drag_state_cleared(self) -> None:
        for etype in ORPHAN_EVENTS:
            with self.subTest(event=etype):
                w, app_model = self._app_backed_audio()
                _press(w, X_CENTER, Y_LANE1)
                _move(w, X_RIGHT_ZONE, Y_LANE1)
                self.assertTrue(w._autoscroll_timer.isActive())
                _send(w, etype)
                self.assertFalse(w._autoscroll_timer.isActive())
                self.assertEqual(w._drag.mode, "")
                self.assertIsNone(w._drag_pos)


if __name__ == "__main__":
    unittest.main()
