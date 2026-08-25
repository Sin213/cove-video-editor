"""Tab 2A: timeline scroll-range clamping + ripple Move to Start / Playhead.

Covers the long-form editing recovery path: a user who trims the head off the
first clip is left with an empty leading region, and the horizontal viewport
could sit past the end of a shrunken sequence. Both are fixed here.

The Qt pieces run on the ``offscreen`` platform plugin so the widget geometry
(and therefore ``scroll_max_px()``) is real rather than mocked.
"""
from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor.app import MainWindow, plan_ripple_move  # noqa: E402
from cove_video_editor.clip import AddedAudio, Clip, MediaAsset, sort_clips  # noqa: E402
from cove_video_editor import timeline_widget as tw  # noqa: E402
from cove_video_editor.timeline_widget import TimelineWidget  # noqa: E402


_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _asset(duration: float = 600.0, name: str = "a.mp4") -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=duration, width=1920, height=1080,
        fps=30.0, has_audio=True,
    )


def _clip(start: float, length: float, *, src_start: float = 0.0,
          name: str = "a.mp4", **kwargs) -> Clip:
    """A clip occupying ``[start, start + length)`` on the timeline."""
    return Clip(
        asset=_asset(max(600.0, src_start + length), name),
        timeline_start=start, src_start=src_start, src_end=src_start + length,
        **kwargs,
    )


def _mouse(kind, pos: QPointF, *, mods=Qt.NoModifier) -> QMouseEvent:
    return QMouseEvent(kind, pos, pos, Qt.LeftButton, Qt.LeftButton, mods)


def _widget(clips: list[Clip], *, pps: float = 40.0) -> TimelineWidget:
    w = TimelineWidget()
    w.resize(800, 300)
    w.set_pixels_per_second(pps)
    w.set_clips(clips)
    return w


# ---- Group A: scroll clamp -------------------------------------------------


class ScrollClampTests(unittest.TestCase):
    """``_scroll_x`` must always satisfy ``0 <= _scroll_x <= scroll_max_px()``."""

    def _long(self) -> TimelineWidget:
        w = _widget([_clip(0.0, 600.0)])
        self.assertGreater(w.scroll_max_px(), 0,
                           "fixture must actually be scrollable")
        return w

    def test_a1_negative_scroll_clamps_to_zero(self) -> None:
        w = self._long()

        w.set_scroll_x(-500)

        self.assertEqual(w._scroll_x, 0)

    def test_a2_scroll_beyond_max_clamps_to_max(self) -> None:
        w = self._long()
        max_px = w.scroll_max_px()

        w.set_scroll_x(max_px + 10_000)

        self.assertEqual(w._scroll_x, max_px)

    def test_a3_valid_scroll_is_left_alone(self) -> None:
        w = self._long()
        mid = w.scroll_max_px() // 2

        w.set_scroll_x(mid)

        self.assertEqual(w._scroll_x, mid)

    def test_a4_shrinking_the_sequence_repairs_stale_scroll(self) -> None:
        """The reported bug: after content shrinks the viewport must not stay
        past the new end without any further wheel/drag interaction."""
        w = self._long()
        w.set_scroll_x(w.scroll_max_px())
        self.assertGreater(w._scroll_x, 0)

        # Republish the model with a much shorter sequence (trim / delete /
        # undo / ripple move all land here).
        w.set_clips([_clip(0.0, 4.0)])

        self.assertLessEqual(w._scroll_x, w.scroll_max_px())

    def test_a4b_removing_the_longest_added_audio_repairs_stale_scroll(self) -> None:
        """Added audio counts towards the timeline length too, so dropping the
        longest item is another way for the content to shrink underneath a
        scrolled viewport."""
        w = _widget([_clip(0.0, 4.0)])
        w.set_added_audios([
            AddedAudio(path=Path("long.mp3"), duration=600.0, rate=48000,
                       offset=0.0, lane=1, peaks=[0.5] * 64),
        ])
        w.set_scroll_x(w.scroll_max_px())
        self.assertGreater(w._scroll_x, 0)

        w.set_added_audios([])

        self.assertLessEqual(w._scroll_x, w.scroll_max_px())

    def test_a4c_clearing_an_overlong_selection_repairs_stale_scroll(self) -> None:
        """A region dragged past the end of the content extends the scrollable
        range; clearing it shrinks the range back."""
        w = _widget([_clip(0.0, 4.0)])
        w.set_selection_range(0.0, 600.0)
        w.set_scroll_x(w.scroll_max_px())
        self.assertGreater(w._scroll_x, 0)

        w.clear_selection()

        self.assertLessEqual(w._scroll_x, w.scroll_max_px())

    def test_a4d_mouse_drag_shrinking_a_selection_repairs_stale_scroll(self) -> None:
        """The interactive path, not just the programmatic setter: drag a
        region far past the content, scroll to the end, then drag the region
        back. The scroll offset must stay legal mid-drag."""
        w = _widget([_clip(0.0, 4.0)])
        press = QPointF(float(tw.LEFT_PAD + 5), float(w._video_rect().top() + 4))
        w.mousePressEvent(_mouse(QEvent.MouseButtonPress, press,
                                 mods=Qt.ShiftModifier))
        far_x = tw.LEFT_PAD + int(600.0 * w.pixels_per_second())
        w.mouseMoveEvent(_mouse(QEvent.MouseMove, QPointF(float(far_x), press.y())))
        w.set_scroll_x(w.scroll_max_px())
        self.assertGreater(w._scroll_x, 0)

        # Drag back to the anchor: the region collapses and content shrinks.
        w.mouseMoveEvent(_mouse(QEvent.MouseMove, press))

        self.assertLessEqual(w._scroll_x, w.scroll_max_px())

    def test_a5_content_narrower_than_viewport_has_zero_scroll(self) -> None:
        w = _widget([_clip(0.0, 1.0)])

        self.assertEqual(w.scroll_max_px(), 0)
        w.set_scroll_x(500)
        self.assertEqual(w._scroll_x, 0)


# ---- ripple-move fixtures --------------------------------------------------


def _abc() -> list[Clip]:
    """The reference sequence used by most ripple cases:

    ``A`` 0..10, ``B`` 10..15, ``C`` 15..23 - contiguous, no gaps.
    """
    return [
        _clip(0.0, 10.0, name="A.mp4"),
        _clip(10.0, 5.0, name="B.mp4"),
        _clip(15.0, 8.0, name="C.mp4"),
    ]


def _by_name(clips: list[Clip], name: str) -> Clip:
    return next(c for c in clips if c.asset.path.name == f"{name}.mp4")


def _layout(clips: list[Clip]) -> list[tuple[str, float]]:
    """``[(name, timeline_start), ...]`` in timeline order."""
    return [
        (c.asset.path.name.removesuffix(".mp4"), c.timeline_start)
        for c in sort_clips(clips)
    ]


class RippleCase(unittest.TestCase):
    """Shared helpers for the pure-planner groups."""

    def move(self, clips: list[Clip], name: str,
             target: float) -> list[tuple[str, float]]:
        """Plan and apply a ripple move, returning the resulting layout."""
        plan = plan_ripple_move(clips, _by_name(clips, name).id, target)
        self.assertIsNotNone(plan, "expected a real move, got a no-op")
        for c in clips:
            self.assertIn(c.id, plan, "plan must place every visual clip")
            c.timeline_start = plan[c.id]
        self.assert_no_overlap(clips)
        return _layout(clips)

    def assert_no_overlap(self, clips: list[Clip]) -> None:
        ordered = sort_clips(clips)
        for prev, nxt in zip(ordered, ordered[1:]):
            self.assertLessEqual(
                prev.timeline_end, nxt.timeline_start + 1e-9,
                f"{prev.asset.path.name} overlaps {nxt.asset.path.name}",
            )
        for c in ordered:
            self.assertGreaterEqual(c.timeline_start, 0.0)


# ---- Group B: Move to Start ------------------------------------------------


class MoveToStartTests(RippleCase):
    def test_b1_trimmed_first_clip_moves_to_zero_keeping_its_trim(self) -> None:
        """The reported Windows bug: head-trimming the only clip leaves a
        leading gap; Move to Start closes it without re-trimming."""
        c = _clip(12.0, 10.0, src_start=3.0, name="A.mp4")
        clips = [c]

        self.move(clips, "A", 0.0)

        self.assertEqual(c.timeline_start, 0.0)
        self.assertEqual(c.src_start, 3.0)
        self.assertEqual(c.src_end, 13.0)
        self.assertEqual(c.timeline_length, 10.0)

    def test_b2_clip_already_at_start_is_a_no_op(self) -> None:
        clips = _abc()

        plan = plan_ripple_move(clips, _by_name(clips, "A").id, 0.0)

        self.assertIsNone(plan)
        self.assertEqual(_layout(clips), [("A", 0.0), ("B", 10.0), ("C", 15.0)])

    def test_b3_middle_clip_to_start_reorders_and_stays_contiguous(self) -> None:
        clips = _abc()

        self.assertEqual(
            self.move(clips, "B", 0.0),
            [("B", 0.0), ("A", 5.0), ("C", 15.0)],
        )

    def test_b4_final_clip_to_start_reorders_and_stays_contiguous(self) -> None:
        clips = _abc()

        self.assertEqual(
            self.move(clips, "C", 0.0),
            [("C", 0.0), ("A", 8.0), ("B", 18.0)],
        )

    def test_b5_gaps_between_non_moving_clips_are_preserved(self) -> None:
        """Documented gap semantics: clips that do not move keep their spacing
        relative to each other. Only two gaps change - the one the moved clip
        vacated closes, and one of exactly its length opens at the target."""
        clips = [
            _clip(0.0, 10.0, name="A.mp4"),    # 0..10, then a 10s gap
            _clip(20.0, 5.0, name="B.mp4"),    # 20..25, then a 15s gap
            _clip(40.0, 8.0, name="C.mp4"),    # 40..48
        ]

        self.assertEqual(
            self.move(clips, "C", 0.0),
            [("C", 0.0), ("A", 8.0), ("B", 28.0)],
        )
        # A ends at 18, B starts at 28: the original 10s A/B gap survived.
        self.assertEqual(
            _by_name(clips, "B").timeline_start - _by_name(clips, "A").timeline_end,
            10.0,
        )


# ---- Group C: Move to Playhead ---------------------------------------------


class MoveToPlayheadTests(RippleCase):
    def test_c1_playhead_on_an_exact_boundary_lands_exactly(self) -> None:
        clips = _abc()

        self.assertEqual(
            self.move(clips, "C", 10.0),
            [("A", 0.0), ("C", 10.0), ("B", 18.0)],
        )

    def test_c2_playhead_inside_another_clip_snaps_to_a_legal_boundary(self) -> None:
        """Playhead at 18.0 sits inside C (15..23). Dropping A there verbatim
        would overlap, so the target resolves to the nearest cut point."""
        clips = _abc()

        self.assertEqual(
            self.move(clips, "A", 18.0),
            [("B", 0.0), ("C", 5.0), ("A", 13.0)],
        )

    def test_c3_nearest_boundary_wins_when_distances_differ(self) -> None:
        # Boundaries available to B are {0.0, 10.0, 18.0}.
        far = _abc()
        self.assertEqual(
            self.move(far, "B", 16.0)[-1], ("B", 18.0),
            "16.0 is 2.0 from 18.0 but 6.0 from 10.0",
        )

        near = _abc()
        self.assertEqual(
            self.move(near, "B", 4.0)[0], ("B", 0.0),
            "4.0 is 4.0 from 0.0 but 6.0 from 10.0",
        )

    def test_c4_equal_distance_tie_resolves_to_the_earlier_boundary(self) -> None:
        """Tie rule: when two cut points are exactly equidistant, the earlier
        (smaller) timeline position wins. Deterministic, never iteration
        order dependent."""
        # C's boundaries are {0.0, 10.0, 15.0}; 5.0 is 5.0 from both 0.0 and 10.0.
        clips = _abc()
        self.assertEqual(
            self.move(clips, "C", 5.0),
            [("C", 0.0), ("A", 8.0), ("B", 18.0)],
        )

        # A's boundaries are {0.0, 5.0, 13.0}; 9.0 is 4.0 from both 5.0 and 13.0.
        other = _abc()
        self.assertEqual(
            self.move(other, "A", 9.0),
            [("B", 0.0), ("A", 5.0), ("C", 15.0)],
        )

    def test_c5_negative_target_never_produces_a_negative_start(self) -> None:
        """The widget clamps the playhead at 0.0, so this is defensive
        normalization inside the helper rather than a reachable UI state."""
        clips = _abc()

        self.assertEqual(
            self.move(clips, "C", -5.0),
            [("C", 0.0), ("A", 8.0), ("B", 18.0)],
        )

    def test_c6_target_past_the_end_moves_the_clip_to_last_position(self) -> None:
        clips = _abc()

        self.assertEqual(
            self.move(clips, "A", 23.0),
            [("B", 0.0), ("C", 5.0), ("A", 13.0)],
        )

    def test_c6b_unknown_clip_id_is_a_no_op(self) -> None:
        clips = _abc()

        self.assertIsNone(plan_ripple_move(clips, "nope", 0.0))


# ---- MainWindow integration harness ----------------------------------------


def _window(clips: list[Clip],
            audios: list[AddedAudio] | None = None) -> MainWindow:
    w = MainWindow()
    w._clips = clips
    if audios:
        w._added_audios = list(audios)
        w._refresh_added_audio_display()
    w.timeline.set_clips(w._clips)
    return w


def _audio_state(audios: list[AddedAudio]) -> list[tuple]:
    return [
        (a.id, a.path, a.offset, a.src_start, a.src_end, a.duration,
         a.volume, a.lane)
        for a in audios
    ]


# ---- Group D: undo / model integrity ---------------------------------------


class RippleUndoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clips = _abc()
        self.win = _window(self.clips)
        self.addCleanup(self.win.close)
        self.moved = _by_name(self.clips, "C")

    def test_d1_a_real_move_pushes_exactly_one_undo_entry(self) -> None:
        self.assertEqual(len(self.win._undo_stack), 0)

        self.win._on_clip_move_to_start(self.moved.id)

        self.assertEqual(len(self.win._undo_stack), 1)

    def test_d1b_a_no_op_move_pushes_no_undo_entry(self) -> None:
        self.win._on_clip_move_to_start(_by_name(self.clips, "A").id)

        self.assertEqual(len(self.win._undo_stack), 0)
        self.assertEqual(_layout(self.win._clips),
                         [("A", 0.0), ("B", 10.0), ("C", 15.0)])

    def test_d2_undo_restores_the_exact_prior_layout(self) -> None:
        before = _layout(self.win._clips)

        self.win._on_clip_move_to_start(self.moved.id)
        self.assertNotEqual(_layout(self.win._clips), before)
        self.win._undo()

        self.assertEqual(_layout(self.win._clips), before)

    def test_d3_redo_restores_the_moved_layout(self) -> None:
        self.win._on_clip_move_to_start(self.moved.id)
        after = _layout(self.win._clips)
        self.win._undo()

        self.win._redo()

        self.assertEqual(_layout(self.win._clips), after)

    def test_d4_moved_clip_keeps_every_property_but_its_position(self) -> None:
        self.moved.src_start = 2.0
        self.moved.src_end = 10.0
        self.moved.audio_volume = 0.35
        self.moved.muted = True
        self.moved.speed = 1.0
        before = (self.moved.src_start, self.moved.src_end, self.moved.speed,
                  self.moved.audio_volume, self.moved.muted,
                  self.moved.timeline_length, self.moved.id)

        self.win._on_clip_move_to_start(self.moved.id)

        self.assertEqual(self.moved.timeline_start, 0.0)
        self.assertEqual(
            (self.moved.src_start, self.moved.src_end, self.moved.speed,
             self.moved.audio_volume, self.moved.muted,
             self.moved.timeline_length, self.moved.id),
            before,
        )

    def test_d4b_detached_clip_audio_keeps_its_absolute_position(self) -> None:
        """Unlinking a clip's audio pins it to absolute timeline time, the way
        dragging a clip already treats it. A ripple must not slide it."""
        for c in self.clips:
            c.linked_audio = False
            c.audio_offset = 2.0
        before = {c.id: c.timeline_start + c.audio_offset for c in self.clips}

        self.win._on_clip_move_to_start(self.moved.id)

        self.assertEqual(_layout(self.win._clips),
                         [("C", 0.0), ("A", 8.0), ("B", 18.0)])
        for c in self.win._clips:
            self.assertAlmostEqual(c.timeline_start + c.audio_offset,
                                   before[c.id], places=9)

    def test_d4c_linked_clip_audio_rides_along_unchanged(self) -> None:
        for c in self.clips:
            self.assertTrue(c.linked_audio)
            c.audio_offset = 1.5

        self.win._on_clip_move_to_start(self.moved.id)

        for c in self.win._clips:
            self.assertEqual(c.audio_offset, 1.5)

    def test_d5_playhead_is_a_target_not_a_thing_that_moves(self) -> None:
        self.win.timeline.set_playhead(12.0, emit=False)
        before = self.win.timeline.playhead()

        self.win._on_clip_move_to_playhead(self.moved.id)

        self.assertEqual(self.win.timeline.playhead(), before)
        self.assertEqual(self.moved.timeline_start, 10.0)

    def test_d6_the_moved_clip_stays_selected(self) -> None:
        self.win.timeline.select_clip(_by_name(self.clips, "A").id)

        self.win._on_clip_move_to_start(self.moved.id)

        self.assertEqual(self.win.timeline.selected_id(), self.moved.id)


# ---- Group E: AddedAudio independence --------------------------------------


class AddedAudioAnchoringTests(unittest.TestCase):
    """AddedAudio is an independent absolute-time layer in this slice: a
    visual ripple move must not touch it."""

    def setUp(self) -> None:
        self.clips = _abc()
        self.audios = [
            AddedAudio(path=Path("music.mp3"), duration=30.0, rate=48000,
                       offset=4.0, lane=1, src_start=1.0, src_end=12.0,
                       volume=0.4),
            AddedAudio(path=Path("vo.wav"), duration=8.0, rate=48000,
                       offset=18.0, lane=0, volume=1.25),
        ]
        self.win = _window(self.clips, self.audios)
        self.addCleanup(self.win.close)
        self.before = _audio_state(self.win._added_audios)

    def test_e1_move_to_start_leaves_added_audio_untouched(self) -> None:
        self.win._on_clip_move_to_start(_by_name(self.clips, "C").id)

        self.assertEqual(_audio_state(self.win._added_audios), self.before)

    def test_e2_move_to_playhead_leaves_added_audio_untouched(self) -> None:
        self.win.timeline.set_playhead(10.0, emit=False)

        self.win._on_clip_move_to_playhead(_by_name(self.clips, "C").id)

        self.assertEqual(_audio_state(self.win._added_audios), self.before)

    def test_e3_added_audio_survives_undo_and_redo_of_a_move(self) -> None:
        self.win._on_clip_move_to_start(_by_name(self.clips, "C").id)
        self.win._undo()
        self.assertEqual(_audio_state(self.win._added_audios), self.before)

        self.win._redo()

        self.assertEqual(_audio_state(self.win._added_audios), self.before)


# ---- Group F: context-menu wiring ------------------------------------------


class _MenuProbe(tw.QMenu):
    """A real QMenu whose modal ``exec`` is replaced by a scripted choice, so
    the menu is built by production code but never blocks on an event loop."""

    labels: list[str] = []
    choose: str = ""

    def exec(self, *_args):  # noqa: ANN002, ANN201
        type(self).labels = [a.text() for a in self.actions()]
        if not type(self).choose:
            return None
        return next(
            (a for a in self.actions() if a.text() == type(self).choose), None,
        )


class ContextMenuWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clips = _abc()
        self.w = TimelineWidget()
        self.w.resize(900, 320)
        self.w.set_pixels_per_second(20.0)
        self.w.set_clips(self.clips)
        self.w.set_added_audios([
            # `peaks` is what makes the block hit-testable in the widget.
            AddedAudio(path=Path("music.mp3"), duration=30.0, rate=48000,
                       offset=0.0, lane=1, peaks=[0.5] * 64),
        ])
        self.emitted_start: list[str] = []
        self.emitted_playhead: list[str] = []
        self.emitted_volume: list[str] = []
        self.w.clipMoveToStartRequested.connect(self.emitted_start.append)
        self.w.clipMoveToPlayheadRequested.connect(self.emitted_playhead.append)
        self.w.clipDoubleClicked.connect(self.emitted_volume.append)
        _MenuProbe.labels = []
        _MenuProbe.choose = ""
        patcher = unittest.mock.patch.object(tw, "QMenu", _MenuProbe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open_on_clip(self, name: str, choose: str = "") -> list[str]:
        c = _by_name(self.clips, name)
        pos = self.w._clip_rect(c, self.w._video_rect()).center()
        _MenuProbe.choose = choose
        self.w._show_context_menu(pos, pos)
        return _MenuProbe.labels

    def test_f1_right_clicked_clip_offers_both_move_actions(self) -> None:
        labels = self._open_on_clip("B")

        self.assertIn("Move to Start", labels)
        self.assertIn("Move to Playhead", labels)

    def test_f2_volume_action_from_c28873a_is_still_offered(self) -> None:
        labels = self._open_on_clip("B")

        self.assertIn("Volume...", labels)

    def test_f3_volume_action_still_dispatches(self) -> None:
        self._open_on_clip("B", choose="Volume...")

        self.assertEqual(self.emitted_volume, [_by_name(self.clips, "B").id])

    def test_f4_move_to_start_dispatches_the_right_clicked_clip(self) -> None:
        # A stale selection must not hijack the action.
        self.w.select_clip(_by_name(self.clips, "A").id)

        self._open_on_clip("C", choose="Move to Start")

        self.assertEqual(self.emitted_start, [_by_name(self.clips, "C").id])
        self.assertEqual(self.emitted_playhead, [])

    def test_f5_move_to_playhead_dispatches_the_right_clicked_clip(self) -> None:
        self.w.select_clip(_by_name(self.clips, "A").id)

        self._open_on_clip("B", choose="Move to Playhead")

        self.assertEqual(self.emitted_playhead, [_by_name(self.clips, "B").id])
        self.assertEqual(self.emitted_start, [])

    def test_f6_added_audio_lane_does_not_offer_the_move_actions(self) -> None:
        pos = self.w._audio_lane_rect(1).center()
        _MenuProbe.choose = ""

        self.w._show_context_menu(pos, pos)

        self.assertNotIn("Move to Start", _MenuProbe.labels)
        self.assertNotIn("Move to Playhead", _MenuProbe.labels)
        # The added-audio menu from c28873a is otherwise untouched.
        self.assertIn("Remove This Audio Clip", _MenuProbe.labels)
        self.assertIn("Volume...", _MenuProbe.labels)
        self.assertIn("Replace original audio", _MenuProbe.labels)


if __name__ == "__main__":
    unittest.main()
