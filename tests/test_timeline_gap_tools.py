"""Tab 2C: Close Gap Left + Ripple Delete.

Two explicit long-form gap-cleanup actions layered on the Tab 2A ripple
model. Both close visual time and translate the suffix of the sequence;
neither introduces a general ripple-editing mode, and neither touches the
AddedAudio layer, which stays anchored to absolute timeline time.

The Qt pieces run on the ``offscreen`` platform plugin so widget geometry
is real rather than mocked.
"""
from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor.app import (  # noqa: E402
    MainWindow, plan_close_gap_left, plan_ripple_delete,
)
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


def _by_name(clips: list[Clip], name: str) -> Clip:
    return next(c for c in clips if c.asset.path.name == f"{name}.mp4")


def _layout(clips: list[Clip]) -> list[tuple[str, float]]:
    """``[(name, timeline_start), ...]`` in timeline order."""
    return [
        (c.asset.path.name.removesuffix(".mp4"), c.timeline_start)
        for c in sort_clips(clips)
    ]


def _gapped() -> list[Clip]:
    """``A`` 0..10, ``B`` 15..20, ``C`` 25..30 - a deliberate 5s gap on each
    side of ``B``. The reference fixture for gap-preservation cases."""
    return [
        _clip(0.0, 10.0, name="A.mp4"),
        _clip(15.0, 5.0, name="B.mp4"),
        _clip(25.0, 5.0, name="C.mp4"),
    ]


def _contiguous() -> list[Clip]:
    """``A`` 0..10, ``B`` 10..15, ``C`` 15..25 - no gaps at all."""
    return [
        _clip(0.0, 10.0, name="A.mp4"),
        _clip(10.0, 5.0, name="B.mp4"),
        _clip(15.0, 10.0, name="C.mp4"),
    ]


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


def _props(c: Clip) -> tuple:
    return (c.id, c.src_start, c.src_end, c.timeline_length, c.speed,
            c.audio_volume, c.muted, c.linked_audio)


class _NoOverlapMixin:
    def assert_no_overlap(self, clips: list[Clip]) -> None:
        ordered = sort_clips(clips)
        for a, b in zip(ordered, ordered[1:]):
            self.assertLessEqual(
                a.timeline_end, b.timeline_start + 1e-9,
                f"{a.asset.path.name} overlaps {b.asset.path.name}",
            )


# ---- Group A: Close Gap Left ------------------------------------------------


class CloseGapLeftPlanTests(unittest.TestCase, _NoOverlapMixin):
    """Pure position math: close the one gap immediately left of a clip and
    translate that clip plus everything after it by exactly that amount."""

    def _apply(self, clips: list[Clip], name: str) -> list[tuple[str, float]]:
        plan = plan_close_gap_left(clips, _by_name(clips, name).id)
        self.assertIsNotNone(plan, "expected a real gap close, got a no-op")
        for c in clips:
            self.assertIn(c.id, plan, "plan must place every visual clip")
            c.timeline_start = plan[c.id]
        self.assert_no_overlap(clips)
        return _layout(clips)

    def test_a1_first_clip_with_a_leading_gap_moves_to_zero(self) -> None:
        clips = [
            _clip(12.0, 10.0, name="A.mp4"),
            _clip(22.0, 5.0, name="B.mp4"),
        ]

        self.assertEqual(self._apply(clips, "A"),
                         [("A", 0.0), ("B", 10.0)])

    def test_a2_middle_gap_closes_and_translates_the_suffix(self) -> None:
        clips = _gapped()

        self.assertEqual(self._apply(clips, "B"),
                         [("A", 0.0), ("B", 10.0), ("C", 20.0)])

    def test_a3_a_later_deliberate_gap_keeps_its_duration(self) -> None:
        clips = _gapped()
        before = (_by_name(clips, "C").timeline_start
                  - _by_name(clips, "B").timeline_end)

        self._apply(clips, "B")

        after = (_by_name(clips, "C").timeline_start
                 - _by_name(clips, "B").timeline_end)
        self.assertEqual(before, 5.0)
        self.assertEqual(after, 5.0)

    def test_a3b_earlier_clips_are_left_alone(self) -> None:
        clips = _gapped()

        self._apply(clips, "C")

        self.assertEqual(_layout(clips),
                         [("A", 0.0), ("B", 15.0), ("C", 20.0)])

    def test_a4_no_gap_is_a_no_op(self) -> None:
        clips = _contiguous()

        self.assertIsNone(
            plan_close_gap_left(clips, _by_name(clips, "B").id))

    def test_a5_first_clip_already_at_zero_is_a_no_op(self) -> None:
        clips = _contiguous()

        self.assertIsNone(
            plan_close_gap_left(clips, _by_name(clips, "A").id))

    def test_a5b_sub_tolerance_float_noise_is_not_a_gap(self) -> None:
        clips = _contiguous()
        _by_name(clips, "B").timeline_start += 1e-9

        self.assertIsNone(
            plan_close_gap_left(clips, _by_name(clips, "B").id))

    def test_a5c_unknown_clip_id_is_a_no_op(self) -> None:
        self.assertIsNone(plan_close_gap_left(_gapped(), "nope"))


class CloseGapLeftWindowTests(unittest.TestCase, _NoOverlapMixin):
    def setUp(self) -> None:
        self.clips = _gapped()
        self.win = _window(self.clips)
        self.addCleanup(self.win.close)

    def test_a2b_action_closes_the_gap_on_the_model(self) -> None:
        self.win._on_clip_close_gap_left(_by_name(self.clips, "B").id)

        self.assertEqual(_layout(self.win._clips),
                         [("A", 0.0), ("B", 10.0), ("C", 20.0)])
        self.assert_no_overlap(self.win._clips)

    def test_a4b_no_gap_leaves_the_model_and_undo_stack_alone(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)
        before = _layout(win._clips)

        win._on_clip_close_gap_left(_by_name(win._clips, "B").id)

        self.assertEqual(_layout(win._clips), before)
        self.assertEqual(len(win._undo_stack), 0)

    def test_a5d_first_clip_at_zero_leaves_the_undo_stack_alone(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)

        win._on_clip_close_gap_left(_by_name(win._clips, "A").id)

        self.assertEqual(len(win._undo_stack), 0)

    def test_a6_source_and_trim_properties_survive_the_move(self) -> None:
        moved = _by_name(self.clips, "B")
        moved.src_start = 2.0
        moved.src_end = 7.0
        moved.audio_volume = 0.35
        moved.muted = True
        before = _props(moved)

        self.win._on_clip_close_gap_left(moved.id)

        self.assertEqual(moved.timeline_start, 10.0)
        self.assertEqual(_props(moved), before)

    def test_a7_detached_clip_audio_keeps_its_absolute_position(self) -> None:
        """Tab 2A pins unlinked clip audio to absolute timeline time; a gap
        close must compensate ``audio_offset`` rather than drag it along."""
        for c in self.clips:
            c.linked_audio = False
            c.audio_offset = 2.0
        before = {c.id: c.timeline_start + c.audio_offset for c in self.clips}

        self.win._on_clip_close_gap_left(_by_name(self.clips, "B").id)

        for c in self.win._clips:
            self.assertAlmostEqual(c.timeline_start + c.audio_offset,
                                   before[c.id], places=9)

    def test_a7b_linked_clip_audio_rides_along_unchanged(self) -> None:
        for c in self.clips:
            self.assertTrue(c.linked_audio)
            c.audio_offset = 1.5

        self.win._on_clip_close_gap_left(_by_name(self.clips, "B").id)

        for c in self.win._clips:
            self.assertEqual(c.audio_offset, 1.5)

    def test_a8_added_audio_is_untouched(self) -> None:
        win = _window(_gapped(), [
            AddedAudio(path=Path("music.mp3"), duration=30.0, rate=48000,
                       offset=4.0, lane=1, src_start=1.0, src_end=12.0,
                       volume=0.4),
            AddedAudio(path=Path("vo.wav"), duration=8.0, rate=48000,
                       offset=18.0, lane=0, volume=1.25),
        ])
        self.addCleanup(win.close)
        before = _audio_state(win._added_audios)

        win._on_clip_close_gap_left(_by_name(win._clips, "B").id)

        self.assertEqual(_audio_state(win._added_audios), before)

    def test_a9_the_clip_stays_selected(self) -> None:
        self.win.timeline.select_clip(_by_name(self.clips, "A").id)
        target = _by_name(self.clips, "B")

        self.win._on_clip_close_gap_left(target.id)

        self.assertEqual(self.win.timeline.selected_id(), target.id)


# ---- Group B: Ripple Delete, basics ----------------------------------------


class RippleDeletePlanTests(unittest.TestCase, _NoOverlapMixin):
    """Pure position math. The documented rule: the deleted clip's own
    occupied duration is removed from the timeline, so every clip that
    started after it translates left by exactly that duration. Nothing else
    about the layout is renegotiated."""

    def _apply(self, clips: list[Clip], name: str) -> list[tuple[str, float]]:
        target = _by_name(clips, name)
        plan = plan_ripple_delete(clips, target.id)
        self.assertIsNotNone(plan, "expected a real delete plan")
        self.assertNotIn(target.id, plan, "deleted clip must not be placed")
        remaining = [c for c in clips if c.id != target.id]
        for c in remaining:
            self.assertIn(c.id, plan, "plan must place every surviving clip")
            c.timeline_start = plan[c.id]
        self.assert_no_overlap(remaining)
        return _layout(remaining)

    def test_b1_contiguous_middle_clip_pulls_the_rest_left(self) -> None:
        clips = _contiguous()

        self.assertEqual(self._apply(clips, "B"),
                         [("A", 0.0), ("C", 10.0)])

    def test_b2_deleting_the_last_clip_moves_nothing(self) -> None:
        clips = _contiguous()

        self.assertEqual(self._apply(clips, "C"),
                         [("A", 0.0), ("B", 10.0)])

    def test_b3_deleting_the_first_clip_shifts_the_rest_by_its_duration(self) -> None:
        clips = _contiguous()

        self.assertEqual(self._apply(clips, "A"),
                         [("B", 0.0), ("C", 5.0)])

    def test_b4_deleting_the_only_clip_leaves_an_empty_plan(self) -> None:
        clips = [_clip(4.0, 10.0, name="A.mp4")]

        plan = plan_ripple_delete(clips, clips[0].id)

        self.assertIsNotNone(plan)
        self.assertEqual(plan, {})

    def test_b5_unknown_clip_id_is_a_no_op(self) -> None:
        self.assertIsNone(plan_ripple_delete(_contiguous(), "nope"))


# ---- Group C: Ripple Delete with pre-existing gaps -------------------------


class RippleDeleteGapTests(unittest.TestCase, _NoOverlapMixin):
    """Unrelated gaps translate; they are never collapsed."""

    def _apply(self, clips: list[Clip], name: str) -> list[tuple[str, float]]:
        target = _by_name(clips, name)
        plan = plan_ripple_delete(clips, target.id)
        remaining = [c for c in clips if c.id != target.id]
        for c in remaining:
            c.timeline_start = plan[c.id]
        self.assert_no_overlap(remaining)
        return _layout(remaining)

    def test_c1_a_gap_before_the_deleted_clip_is_not_swallowed(self) -> None:
        """``A`` 0..10, ``B`` 15..20, ``C`` 25..30 with ``B`` ripple-deleted.
        Only ``B``'s own 5s span goes away, so ``C`` lands at 25 - 5 = 20 and
        the leading 5s gap in front of ``B`` stays as timeline space."""
        clips = _gapped()

        self.assertEqual(self._apply(clips, "B"),
                         [("A", 0.0), ("C", 20.0)])

    def test_c2_a_gap_after_the_deleted_clip_keeps_its_duration(self) -> None:
        clips = _gapped()
        before = (_by_name(clips, "C").timeline_start
                  - _by_name(clips, "B").timeline_end)

        self._apply(clips, "B")

        # The B→C gap becomes the A→C gap after translation; its size, and
        # the total space that used to sit between A and C, are unchanged.
        self.assertEqual(before, 5.0)
        self.assertEqual(_by_name(clips, "C").timeline_start
                         - _by_name(clips, "A").timeline_end, 10.0)

    def test_c3_multiple_later_gaps_translate_uniformly(self) -> None:
        clips = [
            _clip(0.0, 10.0, name="A.mp4"),
            _clip(10.0, 5.0, name="B.mp4"),
            _clip(20.0, 5.0, name="C.mp4"),
            _clip(40.0, 5.0, name="D.mp4"),
        ]

        self.assertEqual(self._apply(clips, "B"),
                         [("A", 0.0), ("C", 15.0), ("D", 35.0)])
        # C→D gap was 15s before and stays 15s.
        self.assertEqual(_by_name(clips, "D").timeline_start
                         - _by_name(clips, "C").timeline_end, 15.0)

    def test_c4_deleting_a_first_clip_with_a_leading_gap(self) -> None:
        """The leading gap is not part of the removed region, so it survives
        as the new leading gap, minus nothing."""
        clips = [
            _clip(12.0, 10.0, name="A.mp4"),
            _clip(30.0, 5.0, name="B.mp4"),
        ]

        self.assertEqual(self._apply(clips, "A"), [("B", 20.0)])


class RippleDeleteWindowTests(unittest.TestCase, _NoOverlapMixin):
    def setUp(self) -> None:
        self.clips = _contiguous()
        self.win = _window(self.clips)
        self.addCleanup(self.win.close)

    def _ids(self) -> list[str]:
        return [c.asset.path.name.removesuffix(".mp4")
                for c in sort_clips(self.win._clips)]

    def test_b1b_action_removes_the_clip_and_closes_its_span(self) -> None:
        self.win._on_clip_ripple_delete(_by_name(self.clips, "B").id)

        self.assertEqual(_layout(self.win._clips),
                         [("A", 0.0), ("C", 10.0)])
        self.assert_no_overlap(self.win._clips)

    def test_b2b_deleting_the_last_clip_leaves_earlier_clips_put(self) -> None:
        self.win._on_clip_ripple_delete(_by_name(self.clips, "C").id)

        self.assertEqual(_layout(self.win._clips),
                         [("A", 0.0), ("B", 10.0)])

    def test_b2c_deleting_the_last_clip_reconciles_a_far_right_viewport(self) -> None:
        """Regression: the viewport can sit past the end of the shrunken
        sequence, which Tab 2A's clamp has to repair without further input."""
        win = _window([_clip(0.0, 600.0, name="A.mp4"),
                       _clip(600.0, 600.0, name="B.mp4")])
        self.addCleanup(win.close)
        win.timeline.resize(800, 300)
        win.timeline.set_pixels_per_second(40.0)
        win.timeline.set_scroll_x(win.timeline.scroll_max_px())
        self.assertGreater(win.timeline._scroll_x, 0)

        win._on_clip_ripple_delete(_by_name(win._clips, "B").id)

        self.assertLessEqual(win.timeline._scroll_x,
                             win.timeline.scroll_max_px())
        self.assertGreaterEqual(win.timeline._scroll_x, 0)

    def test_b4b_deleting_the_only_clip_empties_the_timeline_safely(self) -> None:
        win = _window([_clip(4.0, 10.0, name="A.mp4")])
        self.addCleanup(win.close)

        win._on_clip_ripple_delete(win._clips[0].id)

        self.assertEqual(win._clips, [])
        self.assertEqual(win._preview_clip_id, "")
        self.assertEqual(win.timeline.selected_id(), "")

    def test_c4b_added_audio_is_untouched_by_a_ripple_delete(self) -> None:
        win = _window(_gapped(), [
            AddedAudio(path=Path("music.mp3"), duration=30.0, rate=48000,
                       offset=4.0, lane=1, src_start=1.0, src_end=12.0,
                       volume=0.4),
            AddedAudio(path=Path("vo.wav"), duration=8.0, rate=48000,
                       offset=18.0, lane=0, volume=1.25),
        ])
        self.addCleanup(win.close)
        before = _audio_state(win._added_audios)

        win._on_clip_ripple_delete(_by_name(win._clips, "B").id)

        self.assertEqual(_audio_state(win._added_audios), before)

    def test_c5_detached_clip_audio_keeps_its_absolute_position(self) -> None:
        for c in self.clips:
            c.linked_audio = False
            c.audio_offset = 2.0
        before = {c.id: c.timeline_start + c.audio_offset for c in self.clips}

        self.win._on_clip_ripple_delete(_by_name(self.clips, "B").id)

        for c in self.win._clips:
            self.assertAlmostEqual(c.timeline_start + c.audio_offset,
                                   before[c.id], places=9)

    def test_c6_unknown_clip_id_changes_nothing(self) -> None:
        before = _layout(self.win._clips)

        self.win._on_clip_ripple_delete("nope")

        self.assertEqual(_layout(self.win._clips), before)
        self.assertEqual(len(self.win._undo_stack), 0)


# ---- Group D: ordinary Delete stays distinct -------------------------------


class DeleteVersusRippleDeleteTests(unittest.TestCase):
    """Ordinary Delete leaves the vacated time as a gap. Ripple Delete closes
    it. Converting one into the other would be a regression."""

    def test_d1_ordinary_delete_leaves_the_gap(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)

        win._delete_clip_by_id(_by_name(win._clips, "B").id)

        self.assertEqual(_layout(win._clips), [("A", 0.0), ("C", 15.0)])

    def test_d2_ripple_delete_closes_the_gap(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)

        win._on_clip_ripple_delete(_by_name(win._clips, "B").id)

        self.assertEqual(_layout(win._clips), [("A", 0.0), ("C", 10.0)])

    def test_d3_the_two_actions_disagree_on_the_same_arrangement(self) -> None:
        plain = _window(_contiguous())
        self.addCleanup(plain.close)
        ripple = _window(_contiguous())
        self.addCleanup(ripple.close)

        plain._delete_clip_by_id(_by_name(plain._clips, "B").id)
        ripple._on_clip_ripple_delete(_by_name(ripple._clips, "B").id)

        self.assertNotEqual(_layout(plain._clips), _layout(ripple._clips))

    def test_d4_the_clip_delete_signal_still_routes_to_ordinary_delete(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)

        win._on_clip_delete_requested(_by_name(win._clips, "B").id)

        self.assertEqual(_layout(win._clips), [("A", 0.0), ("C", 15.0)])


# ---- Group E: undo / redo --------------------------------------------------


class GapToolUndoTests(unittest.TestCase):
    def test_e1_close_gap_left_pushes_exactly_one_undo_entry(self) -> None:
        win = _window(_gapped())
        self.addCleanup(win.close)
        before = _layout(win._clips)

        win._on_clip_close_gap_left(_by_name(win._clips, "B").id)

        self.assertEqual(len(win._undo_stack), 1)
        win._undo()
        self.assertEqual(_layout(win._clips), before)

    def test_e2_ripple_delete_pushes_exactly_one_undo_entry(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)
        before = _layout(win._clips)

        win._on_clip_ripple_delete(_by_name(win._clips, "B").id)

        self.assertEqual(len(win._undo_stack), 1)

        win._undo()

        self.assertEqual(_layout(win._clips), before)
        self.assertEqual(len(win._clips), 3)

    def test_e2b_undo_restores_the_deleted_clip_properties(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)
        target = _by_name(win._clips, "B")
        target.src_start = 2.0
        target.src_end = 7.0
        target.audio_volume = 0.35
        target.muted = True
        # `Clip.clone()` deliberately remints ids, so undo restores the clip
        # under a fresh id across the whole app. Compare everything else.
        before = _props(target)[1:]

        win._on_clip_ripple_delete(target.id)
        win._undo()

        self.assertEqual(_props(_by_name(win._clips, "B"))[1:], before)

    def test_e2c_redo_reapplies_the_ripple_delete(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)

        win._on_clip_ripple_delete(_by_name(win._clips, "B").id)
        after = _layout(win._clips)
        win._undo()
        win._redo()

        self.assertEqual(_layout(win._clips), after)

    def test_e3_a_no_op_close_gap_pushes_no_undo_entry(self) -> None:
        win = _window(_contiguous())
        self.addCleanup(win.close)

        win._on_clip_close_gap_left(_by_name(win._clips, "B").id)

        self.assertEqual(len(win._undo_stack), 0)
        self.assertEqual(len(win._redo_stack), 0)

    def test_e4_added_audio_survives_undo_and_redo_of_both_actions(self) -> None:
        audios = [
            AddedAudio(path=Path("music.mp3"), duration=30.0, rate=48000,
                       offset=4.0, lane=1, src_start=1.0, src_end=12.0,
                       volume=0.4),
        ]
        win = _window(_gapped(), audios)
        self.addCleanup(win.close)
        before = _audio_state(win._added_audios)

        win._on_clip_close_gap_left(_by_name(win._clips, "B").id)
        win._on_clip_ripple_delete(_by_name(win._clips, "B").id)
        self.assertEqual(_audio_state(win._added_audios), before)
        win._undo()
        self.assertEqual(_audio_state(win._added_audios), before)
        win._redo()

        self.assertEqual(_audio_state(win._added_audios), before)


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


class GapToolContextMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clips = _contiguous()
        self.w = TimelineWidget()
        self.addCleanup(self.w.close)
        self.w.resize(900, 320)
        self.w.set_pixels_per_second(20.0)
        self.w.set_clips(self.clips)
        self.w.set_added_audios([
            # `peaks` is what makes the block hit-testable in the widget.
            AddedAudio(path=Path("music.mp3"), duration=30.0, rate=48000,
                       offset=0.0, lane=1, peaks=[0.5] * 64),
        ])
        self.emitted_gap: list[str] = []
        self.emitted_ripple: list[str] = []
        self.emitted_start: list[str] = []
        self.w.clipCloseGapLeftRequested.connect(self.emitted_gap.append)
        self.w.clipRippleDeleteRequested.connect(self.emitted_ripple.append)
        self.w.clipMoveToStartRequested.connect(self.emitted_start.append)
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

    def test_f1_clip_menu_offers_the_2a_and_2c_actions_together(self) -> None:
        labels = self._open_on_clip("B")

        for wanted in ("Move to Start", "Move to Playhead", "Volume...",
                       "Close Gap Left", "Ripple Delete"):
            self.assertIn(wanted, labels)

    def test_f2_close_gap_dispatches_the_right_clicked_clip(self) -> None:
        # A stale selection must not hijack the action.
        self.w.select_clip(_by_name(self.clips, "A").id)

        self._open_on_clip("C", choose="Close Gap Left")

        self.assertEqual(self.emitted_gap, [_by_name(self.clips, "C").id])
        self.assertEqual(self.emitted_ripple, [])

    def test_f3_ripple_delete_dispatches_the_right_clicked_clip(self) -> None:
        self.w.select_clip(_by_name(self.clips, "A").id)

        self._open_on_clip("B", choose="Ripple Delete")

        self.assertEqual(self.emitted_ripple, [_by_name(self.clips, "B").id])
        self.assertEqual(self.emitted_gap, [])

    def test_f4_move_to_start_still_dispatches(self) -> None:
        self._open_on_clip("B", choose="Move to Start")

        self.assertEqual(self.emitted_start, [_by_name(self.clips, "B").id])

    def test_f5_added_audio_lane_does_not_offer_the_visual_only_actions(self) -> None:
        pos = self.w._audio_lane_rect(1).center()
        _MenuProbe.choose = ""

        self.w._show_context_menu(pos, pos)

        self.assertNotIn("Close Gap Left", _MenuProbe.labels)
        self.assertNotIn("Ripple Delete", _MenuProbe.labels)
        self.assertIn("Remove This Audio Clip", _MenuProbe.labels)


if __name__ == "__main__":
    unittest.main()
