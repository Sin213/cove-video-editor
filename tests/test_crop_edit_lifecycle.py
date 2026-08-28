"""Tab 2I-C: the crop edit lifecycle - per-clip draft / Confirm / Cancel.

Tab 2I-A gave ``Clip`` committed ``crop_rect`` / ``crop_preset`` fields and
Tab 2I-B taught the exporter to consume them. Neither slice let the editor
*write* them. This slice does, and the dominant concern is a single state
machine:

    a crop edit is a temporary draft owned by one specific Clip until
    Confirm commits it; Cancel discards the draft and leaves the
    pre-edit committed state exactly as it was.

The draft lives entirely in ``CropOverlay``. Nothing touches ``Clip`` until
Confirm. The edit session is pinned to a stable clip *id* rather than a
``Clip`` object because undo/redo snapshots replace clip instances with
clones, and a raw object reference would commit into a detached orphan.

Qt runs on the ``offscreen`` platform so widget geometry, focus and key
delivery are real rather than mocked. The background NVENC/AMF probe is
suppressed for every window: it spawns ffmpeg children that outlive the
window and leak into ``ffmpeg_utils._active_probe_procs``, and no crop
behaviour depends on encoder capabilities.
"""
from __future__ import annotations

import inspect
import os
import re
import unittest
import unittest.mock
import warnings
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import crop_overlay as crop_mod  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402
from cove_video_editor.clip import Clip, MediaAsset  # noqa: E402
from cove_video_editor.crop_overlay import (  # noqa: E402
    CROP_ASPECT_PRESETS, HIT_PAD, CropOverlay,
)
from cove_video_editor.exporter import (  # noqa: E402
    ExportWorker, effective_clip_crop_pixels, has_per_clip_crop,
)


_app: QApplication | None = None

FREE = "Free (Custom)"
TIKTOK = "9:16 (TikTok / Reels / Shorts)"
SQUARE = "1:1 (Square / Instagram)"
LANDSCAPE = "16:9 (Landscape / YouTube)"

SRC_DIR = Path(crop_mod.__file__).resolve().parent


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --- helpers -----------------------------------------------------------


def _preset_rect(
    target_aspect: float, src_w: int, src_h: int,
) -> tuple[float, float, float, float]:
    """The centered maximum-area normalized rect at ``target_aspect``.

    Mirrors ``CropOverlay._max_area_rect`` so expectations are derived,
    not transcribed as magic numbers.
    """
    norm = target_aspect / (src_w / src_h)
    if norm <= 1.0:
        w, h = norm, 1.0
    else:
        w, h = 1.0, 1.0 / norm
    return ((1.0 - w) / 2.0, (1.0 - h) / 2.0, w, h)


def _asset(
    name: str = "a.mp4", w: int = 1920, h: int = 1080, kind: str = "video",
) -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=600.0, width=w, height=h, fps=30.0,
        has_audio=(kind == "video"), kind=kind,
    )


def _clip(asset: MediaAsset, start: float = 0.0, length: float = 10.0,
          **kw) -> Clip:
    return Clip(
        asset=asset, timeline_start=start, src_start=0.0, src_end=length, **kw
    )


def _win(clips: list[Clip] | None = None, *, select: int | None = 0):
    """A MainWindow holding ``clips`` with one of them selected."""
    with unittest.mock.patch.object(
        MainWindow, "_start_encoder_probe", lambda self: None,
    ):
        w = MainWindow()
    if clips:
        w._clips = list(clips)
        for c in clips:
            w._assets[c.asset.id] = c.asset
        w.timeline.set_clips(w._clips)
        if select is not None:
            w.timeline.select_clip(clips[select].id)
    w._update_controls_enabled()
    return w


def _activate(w) -> None:
    """Show ``w`` and make it the active window.

    The offscreen platform never activates a window on its own, and Qt
    refuses keyboard focus to widgets in an inactive window, so focus
    routing cannot be tested without this. ``setActiveWindow`` is the only
    call that works headless - ``QWindow.requestActivate()`` is a no-op
    here - and its deprecation warning is test-harness noise.
    """
    w.show()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        QApplication.setActiveWindow(w)


def _overlay(src_w: int = 1920, src_h: int = 1080,
             px_w: int = 640, px_h: int = 360) -> CropOverlay:
    o = CropOverlay()
    o.set_video_aspect(src_w / src_h)
    o.resize(px_w, px_h)
    return o


def _dbl_click(o: CropOverlay, x: float, y: float) -> None:
    ev = QMouseEvent(
        QEvent.MouseButtonDblClick, QPointF(x, y), QPointF(x, y),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
    )
    o.mouseDoubleClickEvent(ev)


class _RectAsserts:
    def assert_rect(self, got, expect, msg: str = "") -> None:
        """Compare a QRectF or 4-tuple against a 4-tuple, componentwise.

        Deliberately not a ``subTest``: a rect mismatch has to fail the
        test outright, otherwise a RED/GREEN classification pass reads a
        broken restore as a passing test.
        """
        if isinstance(got, QRectF):
            got = (got.x(), got.y(), got.width(), got.height())
        self.assertIsNotNone(got, msg or "expected a rect, got None")
        for i, name in enumerate(("x", "y", "w", "h")):
            self.assertAlmostEqual(
                got[i], expect[i], places=9,
                msg=f"{msg or 'rect'} component {name}: "
                    f"got {tuple(got)!r}, want {tuple(expect)!r}",
            )


# --- Group A: starting an edit session ---------------------------------


class StartEditSessionTests(unittest.TestCase, _RectAsserts):
    def test_a1_uncropped_clip_opens_a_full_frame_free_session(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        _activate(w)
        w.crop_btn.setChecked(True)

        self.assertEqual(w._crop_edit_clip_id, c.id)
        self.assertIsNone(w._crop_edit_start_rect)
        self.assertEqual(w._crop_edit_start_preset, FREE)
        self.assert_rect(w.crop_overlay.normalized_rect(), (0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())
        self.assertEqual(w.crop_overlay.preset_name(), FREE)
        self.assertTrue(w.crop_overlay.isVisible())
        self.assertTrue(w.crop_confirm_btn.isVisible())
        w.close()

    def test_a2_committed_preset_crop_reopens_at_its_exact_rect(self) -> None:
        """A moved 9:16 crop must come back where the user left it.

        Re-applying the preset lock re-centres a maximum-area rectangle, so
        restoring the lock and then the stored rect in the wrong order
        silently discards the stored position.
        """
        rect = _preset_rect(9 / 16, 1920, 1080)
        moved = (0.0, 0.0, rect[2], rect[3])
        c = _clip(_asset(), crop_rect=moved, crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)

        self.assertEqual(w._crop_edit_clip_id, c.id)
        self.assertEqual(w._crop_edit_start_rect, moved)
        self.assertEqual(w._crop_edit_start_preset, TIKTOK)
        self.assert_rect(w.crop_overlay.normalized_rect(), moved)
        self.assertAlmostEqual(
            w.crop_overlay.aspect_ratio_preset(), 9 / 16, places=12,
        )
        self.assertEqual(w.crop_aspect_combo.currentText(), TIKTOK)
        w.close()

    def test_a3_committed_custom_crop_reopens_free_at_its_exact_rect(self) -> None:
        custom = (0.12, 0.34, 0.4, 0.25)
        c = _clip(_asset(), crop_rect=custom, crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)

        self.assert_rect(w.crop_overlay.normalized_rect(), custom)
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())
        self.assertEqual(w.crop_aspect_combo.currentText(), FREE)
        w.close()

    def test_a3_malformed_preset_without_a_rect_opens_full_frame_free(self) -> None:
        """``crop_rect=None`` means no effective crop whatever the preset says.

        Opening the editor must not launder that metadata into the model.
        """
        c = _clip(_asset(), crop_rect=None, crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)

        self.assert_rect(w.crop_overlay.normalized_rect(), (0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())
        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, TIKTOK)
        w.close()

    def test_a4_no_selected_clip_cannot_start_an_edit(self) -> None:
        c = _clip(_asset())
        w = _win([c], select=None)
        w.timeline.select_clip("")
        w.crop_btn.setChecked(True)

        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_overlay.isVisible())
        self.assertFalse(w.crop_confirm_btn.isVisible())
        self.assertIsNone(c.crop_rect)
        w.close()

    def test_a4_audio_kind_clip_cannot_own_a_crop(self) -> None:
        c = _clip(_asset("m.mp3", 0, 0, kind="audio"))
        w = _win([c])
        w.crop_btn.setChecked(True)

        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, "")
        w.close()

    def test_a4_image_clip_may_own_a_crop(self) -> None:
        c = _clip(_asset("p.png", 800, 600, kind="image"))
        w = _win([c])
        w.crop_btn.setChecked(True)

        self.assertEqual(w._crop_edit_clip_id, c.id)
        w.close()


# --- Group B: the draft never writes the model -------------------------


class DraftIsNonMutatingTests(unittest.TestCase, _RectAsserts):
    def setUp(self) -> None:
        self.committed = _preset_rect(9 / 16, 1920, 1080)
        self.clip = _clip(_asset(), crop_rect=self.committed,
                          crop_preset=TIKTOK)
        self.w = _win([self.clip])
        self.w.crop_btn.setChecked(True)
        self.depth = len(self.w._undo_stack)

    def tearDown(self) -> None:
        self.w.close()

    def _assert_committed_untouched(self) -> None:
        self.assertEqual(self.clip.crop_rect, self.committed)
        self.assertEqual(self.clip.crop_preset, TIKTOK)
        self.assertEqual(len(self.w._undo_stack), self.depth)

    def test_b1_dragging_the_box_does_not_commit(self) -> None:
        self.w.crop_overlay.set_normalized_rect(QRectF(0.2, 0.1, 0.3, 0.4))
        self._assert_committed_untouched()

    def test_b2_choosing_a_preset_does_not_commit(self) -> None:
        self.w.crop_aspect_combo.setCurrentText(SQUARE)
        self._assert_committed_untouched()

    def test_b3_fit_to_canvas_does_not_commit(self) -> None:
        self.w.crop_fit_btn.click()
        self._assert_committed_untouched()

    def test_b4_reset_does_not_commit(self) -> None:
        self.w.crop_reset_btn.click()
        self._assert_committed_untouched()

    def test_b5_opening_the_editor_does_not_commit(self) -> None:
        self._assert_committed_untouched()

    def test_b6_a_full_draft_edit_sequence_does_not_commit(self) -> None:
        self.w.crop_aspect_combo.setCurrentText(SQUARE)
        self.w.crop_fit_btn.click()
        self.w.crop_overlay.set_normalized_rect(QRectF(0.05, 0.05, 0.5, 0.5))
        self.w.crop_reset_btn.click()
        self._assert_committed_untouched()


# --- Group C: Confirm ---------------------------------------------------


class ConfirmTests(unittest.TestCase, _RectAsserts):
    def test_c1_confirming_a_preset_crop_commits_rect_and_preset(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        _activate(w)
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        drafted = w.crop_overlay.normalized_rect()
        w.crop_confirm_btn.click()

        self.assert_rect(c.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assert_rect(c.crop_rect,
                         (drafted.x(), drafted.y(),
                          drafted.width(), drafted.height()))
        self.assertEqual(c.crop_preset, TIKTOK)
        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_confirm_btn.isVisible())
        self.assertFalse(w.crop_overlay.isVisible())
        self.assertEqual(w.crop_btn.text(), "Crop (9:16)")
        w.close()

    def test_c2_confirming_a_custom_free_crop_commits_free(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_overlay.set_normalized_rect(QRectF(0.1, 0.2, 0.5, 0.6))
        w.crop_confirm_btn.click()

        self.assert_rect(c.crop_rect, (0.1, 0.2, 0.5, 0.6))
        self.assertEqual(c.crop_preset, FREE)
        self.assertEqual(w.crop_btn.text(), "Crop (Active)")
        w.close()

    def test_c3_confirming_a_full_frame_draft_canonicalizes_to_none(self) -> None:
        c = _clip(_asset(), crop_rect=(0.1, 0.2, 0.5, 0.6), crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_overlay.set_normalized_rect(QRectF(0.0, 0.0, 1.0, 1.0))
        w.crop_confirm_btn.click()

        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)
        self.assertEqual(w.crop_btn.text(), "Crop")
        w.close()

    def test_c3_a_preset_that_fills_the_frame_also_canonicalizes(self) -> None:
        """16:9 on a 16:9 source is the whole frame - not a crop."""
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(LANDSCAPE)
        self.assert_rect(w.crop_overlay.normalized_rect(), (0.0, 0.0, 1.0, 1.0))
        w.crop_confirm_btn.click()

        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)
        w.close()

    def test_c4_confirming_an_unchanged_crop_adds_no_undo_entry(self) -> None:
        committed = _preset_rect(9 / 16, 1920, 1080)
        c = _clip(_asset(), crop_rect=committed, crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)
        depth = len(w._undo_stack)
        w.crop_confirm_btn.click()

        self.assertEqual(len(w._undo_stack), depth)
        self.assertEqual(c.crop_rect, committed)
        self.assertEqual(c.crop_preset, TIKTOK)
        w.close()

    def test_c4_confirming_an_untouched_uncropped_clip_adds_no_undo(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        depth = len(w._undo_stack)
        w.crop_confirm_btn.click()

        self.assertEqual(len(w._undo_stack), depth)
        self.assertIsNone(c.crop_rect)
        w.close()

    def test_c5_one_changed_confirm_is_exactly_one_undo_transition(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_overlay.set_normalized_rect(QRectF(0.0, 0.0, 0.3, 1.0))
        depth = len(w._undo_stack)
        w.crop_confirm_btn.click()

        self.assertEqual(len(w._undo_stack), depth + 1)
        w.close()


# --- Group D: Escape cancels -------------------------------------------


class EscapeCancelTests(unittest.TestCase, _RectAsserts):
    def test_d1_escape_on_an_uncropped_clip_leaves_it_uncropped(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_escape_pressed()

        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)
        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertEqual(w.crop_btn.text(), "Crop")
        w.close()

    def test_d2_escape_restores_an_altered_preset_crop(self) -> None:
        committed = _preset_rect(9 / 16, 1920, 1080)
        c = _clip(_asset(), crop_rect=committed, crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_overlay.set_normalized_rect(QRectF(0.0, 0.0, 0.2, 0.2))
        w._on_escape_pressed()

        self.assertEqual(c.crop_rect, committed)
        self.assertEqual(c.crop_preset, TIKTOK)
        w.close()

    def test_d3_escape_after_a_preset_change_restores_the_custom_crop(self) -> None:
        custom = (0.11, 0.22, 0.44, 0.33)
        c = _clip(_asset(), crop_rect=custom, crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(SQUARE)
        w._on_escape_pressed()

        self.assertEqual(c.crop_rect, custom)
        self.assertEqual(c.crop_preset, FREE)
        w.close()

    def test_d3_reopening_after_escape_shows_the_committed_rect(self) -> None:
        custom = (0.11, 0.22, 0.44, 0.33)
        c = _clip(_asset(), crop_rect=custom, crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(SQUARE)
        w._on_escape_pressed()
        w.crop_btn.setChecked(True)

        self.assert_rect(w.crop_overlay.normalized_rect(), custom)
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())
        w.close()

    def test_d4_cancel_creates_no_snapshot(self) -> None:
        c = _clip(_asset(), crop_rect=(0.1, 0.1, 0.5, 0.5), crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        depth = len(w._undo_stack)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_reset_btn.click()
        w._on_escape_pressed()

        self.assertEqual(len(w._undo_stack), depth)
        w.close()

    def test_d5_escape_outside_crop_mode_is_still_a_no_op(self) -> None:
        c = _clip(_asset(), crop_rect=(0.1, 0.1, 0.5, 0.5), crop_preset=FREE)
        w = _win([c])
        depth = len(w._undo_stack)
        w._on_escape_pressed()

        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(c.crop_rect, (0.1, 0.1, 0.5, 0.5))
        self.assertEqual(len(w._undo_stack), depth)
        w.close()


# --- Group E: turning the Crop button off cancels ----------------------


class CropButtonOffCancelTests(unittest.TestCase, _RectAsserts):
    def test_e1_manual_untoggle_discards_the_draft(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        depth = len(w._undo_stack)
        w.crop_btn.click()          # user turns Crop off

        self.assertFalse(w.crop_btn.isChecked())
        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)
        self.assertEqual(len(w._undo_stack), depth)
        w.close()

    def test_e2_an_existing_committed_crop_survives_an_untoggle(self) -> None:
        committed = _preset_rect(1.0, 1920, 1080)
        c = _clip(_asset(), crop_rect=committed, crop_preset=SQUARE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_btn.click()

        self.assertEqual(c.crop_rect, committed)
        self.assertEqual(c.crop_preset, SQUARE)
        self.assertEqual(w.crop_btn.text(), "Crop (1:1)")
        w.close()

    def test_e3_programmatic_uncheck_during_confirm_does_not_cancel(self) -> None:
        """Confirm has to leave crop mode, and leaving crop mode cancels.

        Without a finalization guard the button-off handler runs *after*
        the commit and rolls it straight back out.
        """
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_confirm_btn.click()

        self.assert_rect(c.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assertEqual(c.crop_preset, TIKTOK)
        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, "")
        w.close()

    def test_e3_confirm_does_not_double_cancel_or_double_snapshot(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        depth = len(w._undo_stack)
        w.crop_confirm_btn.click()
        self.assertEqual(len(w._undo_stack), depth + 1)
        # A second Confirm click with no session must be inert.
        w.crop_confirm_btn.click()
        self.assertEqual(len(w._undo_stack), depth + 1)
        self.assert_rect(c.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        w.close()


# --- Group F: Reset is draft-only --------------------------------------


class ResetSemanticsTests(unittest.TestCase, _RectAsserts):
    def setUp(self) -> None:
        self.committed = _preset_rect(9 / 16, 1920, 1080)
        self.clip = _clip(_asset(), crop_rect=self.committed,
                          crop_preset=TIKTOK)
        self.w = _win([self.clip])

    def tearDown(self) -> None:
        self.w.close()

    def test_f1_reset_clears_the_overlay_but_not_the_clip(self) -> None:
        self.w.crop_btn.setChecked(True)
        self.w.crop_reset_btn.click()

        self.assert_rect(self.w.crop_overlay.normalized_rect(),
                         (0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(self.w.crop_overlay.aspect_ratio_preset())
        self.assertEqual(self.w.crop_aspect_combo.currentText(), FREE)
        self.assertEqual(self.clip.crop_rect, self.committed)
        self.assertEqual(self.clip.crop_preset, TIKTOK)

    def test_f2_reset_then_escape_keeps_the_committed_crop(self) -> None:
        self.w.crop_btn.setChecked(True)
        depth = len(self.w._undo_stack)
        self.w.crop_reset_btn.click()
        self.w._on_escape_pressed()

        self.assertEqual(self.clip.crop_rect, self.committed)
        self.assertEqual(self.clip.crop_preset, TIKTOK)
        self.assertEqual(len(self.w._undo_stack), depth)
        self.assertEqual(self.w.crop_btn.text(), "Crop (9:16)")

    def test_f2_reset_then_escape_then_reopen_restores_the_rect(self) -> None:
        self.w.crop_btn.setChecked(True)
        self.w.crop_reset_btn.click()
        self.w._on_escape_pressed()
        self.w.crop_btn.setChecked(True)

        self.assert_rect(self.w.crop_overlay.normalized_rect(), self.committed)
        self.assertAlmostEqual(
            self.w.crop_overlay.aspect_ratio_preset(), 9 / 16, places=12,
        )

    def test_f3_reset_then_confirm_clears_the_committed_crop(self) -> None:
        self.w.crop_btn.setChecked(True)
        depth = len(self.w._undo_stack)
        self.w.crop_reset_btn.click()
        self.w.crop_confirm_btn.click()

        self.assertIsNone(self.clip.crop_rect)
        self.assertEqual(self.clip.crop_preset, FREE)
        self.assertEqual(len(self.w._undo_stack), depth + 1)
        self.assertEqual(self.w.crop_btn.text(), "Crop")

    def test_f4_undo_after_reset_confirm_restores_the_original(self) -> None:
        self.w.crop_btn.setChecked(True)
        self.w.crop_reset_btn.click()
        self.w.crop_confirm_btn.click()
        self.w._undo()

        restored = self.w._clips[0]
        self.assert_rect(restored.crop_rect, self.committed)
        self.assertEqual(restored.crop_preset, TIKTOK)
        self.assertEqual(self.w.crop_btn.text(), "Crop (9:16)")

    def test_f4_redo_clears_the_crop_again(self) -> None:
        self.w.crop_btn.setChecked(True)
        self.w.crop_reset_btn.click()
        self.w.crop_confirm_btn.click()
        self.w._undo()
        self.w._redo()

        self.assertIsNone(self.w._clips[0].crop_rect)
        self.assertEqual(self.w._clips[0].crop_preset, FREE)
        self.assertEqual(self.w.crop_btn.text(), "Crop")


# --- Group G: preset / fit cancel --------------------------------------


class PresetAndFitCancelTests(unittest.TestCase, _RectAsserts):
    def test_g1_preset_change_then_escape_keeps_the_old_preset(self) -> None:
        committed = _preset_rect(1.0, 1920, 1080)
        c = _clip(_asset(), crop_rect=committed, crop_preset=SQUARE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_escape_pressed()

        self.assertEqual(c.crop_rect, committed)
        self.assertEqual(c.crop_preset, SQUARE)
        self.assertEqual(w.crop_btn.text(), "Crop (1:1)")
        w.close()

    def test_g2_fit_to_canvas_then_escape_keeps_the_exact_rect(self) -> None:
        custom = (0.3, 0.3, 0.2, 0.2)
        c = _clip(_asset(), crop_rect=custom, crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_fit_btn.click()
        self.assert_rect(w.crop_overlay.normalized_rect(), (0.0, 0.0, 1.0, 1.0))
        w._on_escape_pressed()

        self.assertEqual(c.crop_rect, custom)
        w.crop_btn.setChecked(True)
        self.assert_rect(w.crop_overlay.normalized_rect(), custom)
        w.close()

    def test_g3_preset_and_fit_never_snapshot(self) -> None:
        c = _clip(_asset(), crop_rect=(0.3, 0.3, 0.2, 0.2), crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        depth = len(w._undo_stack)
        for step in (
            lambda: w.crop_aspect_combo.setCurrentText(TIKTOK),
            lambda: w.crop_fit_btn.click(),
            lambda: w.crop_aspect_combo.setCurrentText(SQUARE),
            lambda: w.crop_reset_btn.click(),
        ):
            step()
            with self.subTest(step=step):
                self.assertEqual(len(w._undo_stack), depth)
        w.close()


# --- Group H: clip selection --------------------------------------------


class SelectionLifecycleTests(unittest.TestCase, _RectAsserts):
    def _two_clips(self, b_crop=None, b_preset=FREE):
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        b = _clip(_asset("b.mp4"), 10.0, 10.0,
                  crop_rect=b_crop, crop_preset=b_preset)
        return a, b

    def test_h1_selecting_another_clip_auto_confirms_the_draft(self) -> None:
        a, b = self._two_clips()
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        depth = len(w._undo_stack)
        w.timeline.select_clip(b.id)

        self.assert_rect(a.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assertEqual(a.crop_preset, TIKTOK)
        self.assertIsNone(b.crop_rect)
        self.assertEqual(b.crop_preset, FREE)
        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertEqual(w.timeline.selected_id(), b.id)
        self.assertEqual(len(w._undo_stack), depth + 1)
        w.close()

    def test_h2_the_toolbar_label_follows_the_newly_selected_clip(self) -> None:
        b_rect = _preset_rect(1.0, 1920, 1080)
        a, b = self._two_clips(b_crop=b_rect, b_preset=SQUARE)
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.timeline.select_clip(b.id)

        self.assertEqual(w.crop_btn.text(), "Crop (1:1)")
        w.close()

    def test_h3_reselecting_the_first_clip_reopens_its_own_crop(self) -> None:
        b_rect = _preset_rect(1.0, 1920, 1080)
        a, b = self._two_clips(b_crop=b_rect, b_preset=SQUARE)
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_overlay.set_normalized_rect(QRectF(0.0, 0.0, 0.3, 1.0))
        w.timeline.select_clip(b.id)
        w.timeline.select_clip(a.id)
        w.crop_btn.setChecked(True)

        self.assert_rect(w.crop_overlay.normalized_rect(), (0.0, 0.0, 0.3, 1.0))
        self.assertEqual(w.crop_aspect_combo.currentText(), TIKTOK)
        self.assertAlmostEqual(
            w.crop_overlay.aspect_ratio_preset(), 9 / 16, places=12,
        )
        w.close()

    def test_h4_reselecting_the_edit_owner_does_not_churn(self) -> None:
        a, b = self._two_clips()
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        depth = len(w._undo_stack)
        drafted = w.crop_overlay.normalized_rect()

        w._on_clip_selected(a.id)

        self.assertTrue(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, a.id)
        self.assertEqual(len(w._undo_stack), depth)
        self.assertIsNone(a.crop_rect)
        self.assert_rect(w.crop_overlay.normalized_rect(),
                         (drafted.x(), drafted.y(),
                          drafted.width(), drafted.height()))
        w.close()

    def test_h5_auto_confirm_resolves_the_session_owner_not_the_selection(
        self,
    ) -> None:
        """``TimelineWidget`` updates ``_selected_id`` *before* it emits.

        A Confirm implementation that asks "which clip is selected?" at
        callback time therefore writes A's draft straight into B.
        """
        a, b = self._two_clips()
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)

        # Reproduce the real signal ordering exactly.
        w.timeline._selected_id = b.id
        w._on_clip_selected(b.id)

        self.assert_rect(a.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assertEqual(a.crop_preset, TIKTOK)
        self.assertIsNone(b.crop_rect)
        self.assertEqual(b.crop_preset, FREE)
        w.close()

    def test_h5_selecting_nothing_still_confirms_the_owner(self) -> None:
        a, b = self._two_clips()
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.timeline._selected_id = ""
        w._on_clip_selected("")

        self.assert_rect(a.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w.crop_btn.text(), "Crop")
        w.close()


# --- Group I: deletion and state replacement ----------------------------


class DeletionAndStateReplacementTests(unittest.TestCase, _RectAsserts):
    def test_i1_deleting_the_edit_owner_terminates_the_session(self) -> None:
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        b = _clip(_asset("b.mp4"), 10.0, 10.0)
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._delete_clip_by_id(a.id)

        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertIsNone(a.crop_rect, "a deleted clip must not be committed to")
        self.assertIsNone(b.crop_rect)
        w.close()

    def test_i1_deleting_another_clip_leaves_the_session_alone(self) -> None:
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        b = _clip(_asset("b.mp4"), 10.0, 10.0)
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._delete_clip_by_id(b.id)

        self.assertEqual(w._crop_edit_clip_id, a.id)
        self.assertTrue(w.crop_btn.isChecked())
        w.close()

    def test_i1_deleting_the_owner_asset_terminates_the_session(self) -> None:
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        w = _win([a])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_asset_delete_requested(a.asset.id)

        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertIsNone(a.crop_rect)
        w.close()

    def test_i2_undo_cancels_the_draft_before_replacing_state(self) -> None:
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        w = _win([a])
        w._snapshot()                       # something to undo
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._undo()

        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertIsNone(w._clips[0].crop_rect,
                          "Undo must not auto-confirm the draft")
        w.close()

    def test_i3_redo_cancels_the_draft_before_replacing_state(self) -> None:
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        w = _win([a])
        w._snapshot()
        w._undo()
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._redo()

        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertIsNone(w._clips[0].crop_rect)
        w.close()

    def test_i4_region_delete_terminates_an_active_session(self) -> None:
        """Region edits replace the whole clip list without a selection event.

        ``TimelineWidget.set_clips()`` silently drops a selected id that no
        longer resolves and emits nothing, so no selection callback closes
        crop mode. Left alone the overlay stays open over a clip that no
        longer exists and Confirm quietly throws the draft away.
        """
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        b = _clip(_asset("b.mp4"), 10.0, 10.0)
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        # Covers A entirely so `delete_region` drops it rather than trimming.
        w._on_region_delete(0.0, 10.5)

        self.assertNotIn(a.id, [c.id for c in w._clips], "owner was removed")
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertFalse(w.crop_overlay.isVisible())
        self.assertIsNone(a.crop_rect, "a removed clip must not be committed to")
        self.assertIsNone(b.crop_rect, "the draft must not land on another clip")
        w.close()

    def test_i4_region_crop_terminates_an_active_session(self) -> None:
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        b = _clip(_asset("b.mp4"), 10.0, 10.0)
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_region_crop(10.0, 20.0)

        self.assertNotIn(a.id, [c.id for c in w._clips], "owner was removed")
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertIsNone(a.crop_rect)
        self.assertIsNone(b.crop_rect)
        w.close()

    def test_i4_region_delete_sparing_the_owner_still_closes_the_editor(
        self,
    ) -> None:
        """A region edit reshapes the timeline under the draft, so the
        session ends either way rather than leaving a box sized against
        geometry that just moved."""
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        b = _clip(_asset("b.mp4"), 10.0, 10.0)
        w = _win([a, b])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_region_delete(10.0, 20.5)

        self.assertIn(a.id, [c.id for c in w._clips])
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())
        self.assertIsNone(a.crop_rect, "the draft is discarded, not committed")
        w.close()

    def test_i4_a_session_never_outlives_the_clip_objects_it_owns(self) -> None:
        """``_apply_state`` swaps in clones; a live session would be stale."""
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        w = _win([a])
        w._snapshot()
        w.crop_btn.setChecked(True)
        before = w._clips[0]
        w._undo()

        self.assertIsNot(w._clips[0], before)
        self.assertEqual(w._crop_edit_clip_id, "")
        w.close()


# --- Group J: undo / redo after Confirm ---------------------------------


class UndoRedoAfterConfirmTests(unittest.TestCase, _RectAsserts):
    def test_j1_undo_removes_a_confirmed_crop(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_confirm_btn.click()
        w._undo()

        self.assertIsNone(w._clips[0].crop_rect)
        self.assertEqual(w._clips[0].crop_preset, FREE)
        w.close()

    def test_j2_redo_restores_a_confirmed_crop(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_confirm_btn.click()
        w._undo()
        w._redo()

        self.assert_rect(w._clips[0].crop_rect,
                         _preset_rect(9 / 16, 1920, 1080))
        self.assertEqual(w._clips[0].crop_preset, TIKTOK)
        w.close()

    def test_j3_the_toolbar_label_tracks_undo_and_redo(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_confirm_btn.click()
        self.assertEqual(w.crop_btn.text(), "Crop (9:16)")
        w._undo()
        self.assertEqual(w.crop_btn.text(), "Crop")
        w._redo()
        self.assertEqual(w.crop_btn.text(), "Crop (9:16)")
        w.close()

    def test_j5_undo_leaves_a_resolvable_selection(self) -> None:
        """Snapshot restore must not strand the selection.

        ``Clip.clone()`` mints a fresh id, so the snapshot's stored
        ``selected_id`` can never resolve against the restored list. Left
        dangling it disables every clip-scoped tool, and the Crop button
        can neither report status nor open an edit.
        """
        a = _clip(_asset("a.mp4"), 0.0, 10.0)
        b = _clip(_asset("b.mp4"), 10.0, 10.0)
        w = _win([a, b], select=1)
        w._snapshot()
        w._undo()

        self.assertIsNotNone(w._selected_clip(),
                             "undo must leave a selection that resolves")
        self.assertEqual(w._selected_clip().id, w._clips[1].id,
                         "the same position stays selected")
        self.assertIsNotNone(w._crop_eligible_clip(),
                             "Crop must still be openable after undo")
        w.close()

    def test_j4_undo_restores_the_exact_previous_crop(self) -> None:
        old = _preset_rect(9 / 16, 1920, 1080)
        c = _clip(_asset(), crop_rect=old, crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(SQUARE)
        w.crop_confirm_btn.click()
        self.assert_rect(w._clips[0].crop_rect, _preset_rect(1.0, 1920, 1080))
        w._undo()

        self.assert_rect(w._clips[0].crop_rect, old)
        self.assertEqual(w._clips[0].crop_preset, TIKTOK)
        w.close()


# --- Group K: export while editing --------------------------------------


class _ExportSeam:
    """Drive ``_on_export_clicked`` up to (not into) the encoder."""

    def _export(self, w, tmp: Path):
        captured: dict[str, object] = {}

        def _fake_start_export(job):
            captured["job"] = job
            raise _StopExport

        with unittest.mock.patch.object(
            QFileDialog, "getSaveFileName",
            staticmethod(lambda *a, **k: (str(tmp / "out.mp4"), "")),
        ), unittest.mock.patch.object(
            app_mod, "start_export", _fake_start_export,
        ):
            try:
                w._on_export_clicked()
            except _StopExport:
                pass
        return captured.get("job")


class _StopExport(Exception):
    """Stops the export flow the instant a real job would have launched."""


class ExportWhileEditingTests(unittest.TestCase, _RectAsserts, _ExportSeam):
    def setUp(self) -> None:
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp"))

    def test_k1_export_auto_confirms_the_draft_first(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        job = self._export(w, self._tmp)

        self.assertIsNotNone(job, "export must reach job construction")
        self.assert_rect(c.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assertEqual(c.crop_preset, TIKTOK)
        self.assertFalse(w.crop_btn.isChecked())
        self.assertEqual(w._crop_edit_clip_id, "")
        w.close()

    def test_k2_the_job_carries_the_per_clip_crop_not_the_legacy_global(
        self,
    ) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        job = self._export(w, self._tmp)

        self.assertIsNone(
            job.crop, "the stale global overlay crop must not leak into export",
        )
        self.assertTrue(has_per_clip_crop(job.clips))
        expect = effective_clip_crop_pixels(job.clips[0])
        self.assertIsNotNone(expect)
        x, y, cw, ch = expect
        cmd = ExportWorker(job)._build_command()
        graph = " ".join(cmd)
        self.assertIn(f"crop={cw}:{ch}:{x}:{y}", graph)
        w.close()

    def test_k3_export_auto_confirm_creates_one_undo_entry(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        depth = len(w._undo_stack)
        self._export(w, self._tmp)

        self.assertEqual(len(w._undo_stack), depth + 1)
        w.close()

    def test_k4_a_full_frame_draft_exports_uncropped(self) -> None:
        c = _clip(_asset(), crop_rect=(0.2, 0.2, 0.5, 0.5), crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_reset_btn.click()
        job = self._export(w, self._tmp)

        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)
        self.assertIsNone(job.crop)
        self.assertFalse(has_per_clip_crop(job.clips))
        cmd = ExportWorker(job)._build_command()
        self.assertNotIn("crop=", " ".join(cmd))
        w.close()

    def test_k4_export_without_a_crop_session_is_unchanged(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        job = self._export(w, self._tmp)

        self.assertIsNone(job.crop)
        self.assertIsNone(c.crop_rect)
        w.close()


# --- Group L: Enter / Return ownership ----------------------------------


class EnterReturnFocusTests(unittest.TestCase, _RectAsserts):
    def test_l1_return_confirms_while_the_overlay_has_focus(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        _activate(w)
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        self.assertTrue(w.crop_overlay.hasFocus())
        QTest.keyClick(w.crop_overlay, Qt.Key_Return)

        self.assert_rect(c.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assertFalse(w.crop_btn.isChecked())
        w.close()

    def test_l2_enter_confirms_too(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        _activate(w)
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        QTest.keyClick(w.crop_overlay, Qt.Key_Enter)

        self.assert_rect(c.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        w.close()

    def test_l3_return_in_the_timecode_box_does_not_confirm_crop(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        _activate(w)
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.timecode_edit.setFocus(Qt.OtherFocusReason)
        self.assertTrue(w.timecode_edit.hasFocus())
        w.timecode_edit.setText("0:00:02.000")
        QTest.keyClick(w.timecode_edit, Qt.Key_Return)

        self.assertAlmostEqual(w.timeline.playhead(), 2.0, places=3,
                               msg="existing timecode Return behaviour")
        self.assertIsNone(c.crop_rect, "timecode Return must not confirm crop")
        self.assertTrue(w.crop_btn.isChecked())
        w.close()

    def test_l4_no_global_return_or_enter_shortcut_exists(self) -> None:
        src = (SRC_DIR / "app.py").read_text(encoding="utf-8")
        for token in ("Key_Return", "Key_Enter"):
            with self.subTest(token=token):
                self.assertNotIn(token, src)

    def test_l4_the_overlay_accepts_keyboard_focus(self) -> None:
        o = _overlay()
        self.assertNotEqual(o.focusPolicy(), Qt.NoFocus)


# --- Group M: double-click confirm --------------------------------------


class DoubleClickConfirmTests(unittest.TestCase, _RectAsserts):
    def _inset_overlay(self) -> CropOverlay:
        o = _overlay()
        o.set_normalized_rect(QRectF(0.25, 0.25, 0.5, 0.5))
        return o

    def test_m1_double_click_inside_the_crop_body_requests_confirm(self) -> None:
        o = self._inset_overlay()
        seen: list[int] = []
        o.confirmRequested.connect(lambda: seen.append(1))
        _dbl_click(o, 320.0, 180.0)
        self.assertEqual(len(seen), 1)

    def test_m2_double_click_outside_the_crop_box_does_not_confirm(self) -> None:
        o = self._inset_overlay()
        seen: list[int] = []
        o.confirmRequested.connect(lambda: seen.append(1))
        _dbl_click(o, 10.0, 10.0)
        self.assertEqual(seen, [])

    def test_m3_double_click_on_a_resize_handle_does_not_confirm(self) -> None:
        o = self._inset_overlay()
        seen: list[int] = []
        o.confirmRequested.connect(lambda: seen.append(1))
        # Top-left handle centre of the 0.25..0.75 box in a 640x360 widget.
        _dbl_click(o, 160.0, 90.0)
        self.assertEqual(seen, [])
        # And just inside its hit pad, still on the handle.
        _dbl_click(o, 160.0 + HIT_PAD - 1, 90.0 + HIT_PAD - 1)
        self.assertEqual(seen, [])

    def test_m4_double_click_while_dragging_does_not_confirm(self) -> None:
        o = self._inset_overlay()
        seen: list[int] = []
        o.confirmRequested.connect(lambda: seen.append(1))
        o._drag_target = "move"
        o._drag_start_widget = QPointF(320.0, 180.0)
        o._drag_start_rect = o.normalized_rect()
        _dbl_click(o, 320.0, 180.0)
        self.assertEqual(seen, [])

    def test_m1_single_click_drag_behaviour_is_unchanged(self) -> None:
        o = self._inset_overlay()
        before = o.normalized_rect()
        press = QMouseEvent(
            QEvent.MouseButtonPress, QPointF(320.0, 180.0),
            QPointF(320.0, 180.0), Qt.LeftButton, Qt.LeftButton,
            Qt.NoModifier,
        )
        o.mousePressEvent(press)
        self.assertEqual(o._drag_target, "move")
        o._apply_drag(QPointF(340.0, 180.0))
        self.assertGreater(o.normalized_rect().x(), before.x())

    def test_m1_overlay_confirm_intent_reaches_the_window(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w.crop_overlay.confirmRequested.emit()

        self.assert_rect(c.crop_rect, _preset_rect(9 / 16, 1920, 1080))
        self.assertFalse(w.crop_btn.isChecked())
        w.close()

    def test_m1_overlay_never_mutates_a_clip_itself(self) -> None:
        src = (SRC_DIR / "crop_overlay.py").read_text(encoding="utf-8")
        for token in (".crop_rect", ".crop_preset", "Clip"):
            with self.subTest(token=token):
                self.assertNotIn(token, src)


# --- Group N: toolbar status --------------------------------------------


LABEL_RE = re.compile(r"^Crop(?: \((?:\d+:\d+|Active)\))?$")


class ToolbarStatusTests(unittest.TestCase):
    def test_n1_no_selected_clip_shows_the_plain_label(self) -> None:
        w = _win([])
        self.assertEqual(w.crop_btn.text(), "Crop")
        w.close()

    def test_n1_an_uncropped_selected_clip_shows_the_plain_label(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        self.assertEqual(w.crop_btn.text(), "Crop")
        w.close()

    def test_n2_a_committed_preset_shows_its_compact_ratio(self) -> None:
        cases = {
            TIKTOK: "Crop (9:16)",
            SQUARE: "Crop (1:1)",
            LANDSCAPE: "Crop (16:9)",
            "4:5 (Portrait / Social)": "Crop (4:5)",
            "21:9 (Cinematic / Ultrawide)": "Crop (21:9)",
        }
        for preset, expect in cases.items():
            with self.subTest(preset=preset):
                c = _clip(_asset(), crop_rect=(0.1, 0.1, 0.5, 0.5),
                          crop_preset=preset)
                w = _win([c])
                self.assertEqual(w.crop_btn.text(), expect)
                w.close()

    def test_n3_a_committed_custom_crop_shows_active(self) -> None:
        c = _clip(_asset(), crop_rect=(0.1, 0.1, 0.5, 0.5), crop_preset=FREE)
        w = _win([c])
        self.assertEqual(w.crop_btn.text(), "Crop (Active)")
        w.close()

    def test_n4_selecting_an_uncropped_clip_clears_the_status(self) -> None:
        a = _clip(_asset("a.mp4"), 0.0, 10.0,
                  crop_rect=(0.1, 0.1, 0.5, 0.5), crop_preset=TIKTOK)
        b = _clip(_asset("b.mp4"), 10.0, 10.0)
        w = _win([a, b])
        self.assertEqual(w.crop_btn.text(), "Crop (9:16)")
        w.timeline.select_clip(b.id)
        self.assertEqual(w.crop_btn.text(), "Crop")
        w.timeline.select_clip(a.id)
        self.assertEqual(w.crop_btn.text(), "Crop (9:16)")
        w.close()

    def test_n6_the_label_never_shows_pixel_dimensions(self) -> None:
        for preset in (FREE, TIKTOK, SQUARE):
            with self.subTest(preset=preset):
                c = _clip(_asset(), crop_rect=(0.1, 0.1, 0.5, 0.5),
                          crop_preset=preset)
                w = _win([c])
                self.assertRegex(w.crop_btn.text(), LABEL_RE)
                w.close()

    def test_n6_compact_labels_come_from_the_existing_registry(self) -> None:
        for preset, ratio in CROP_ASPECT_PRESETS.items():
            with self.subTest(preset=preset):
                got = crop_mod.compact_preset_label(preset)
                if ratio is None:
                    self.assertEqual(got, "Active")
                else:
                    self.assertEqual(got, preset.split(" ")[0])

    def test_n6_an_unknown_preset_key_degrades_to_active(self) -> None:
        self.assertEqual(crop_mod.compact_preset_label("nonsense"), "Active")


# --- Group O: playback is untouched -------------------------------------


class PlaybackInvariantTests(unittest.TestCase):
    def _counted(self, w):
        """Patch every playback verb on both players and count calls."""
        patches, calls = [], []
        for player in (w.player, w.clip_audio_player):
            for name in ("play", "pause", "setPosition"):
                p = unittest.mock.patch.object(
                    player, name,
                    side_effect=lambda *a, _n=name: calls.append(_n),
                )
                p.start()
                patches.append(p)
        return patches, calls

    def test_o1_confirm_touches_no_playback_verb(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        patches, calls = self._counted(w)
        try:
            w.crop_confirm_btn.click()
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(calls, [])
        self.assertIsNotNone(c.crop_rect)
        w.close()

    def test_o2_cancel_touches_no_playback_verb(self) -> None:
        c = _clip(_asset(), crop_rect=(0.1, 0.1, 0.5, 0.5), crop_preset=FREE)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        patches, calls = self._counted(w)
        try:
            w._on_escape_pressed()
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(calls, [])
        w.close()

    def test_o3_opening_crop_touches_no_playback_verb(self) -> None:
        c = _clip(_asset(), crop_rect=(0.1, 0.1, 0.5, 0.5), crop_preset=FREE)
        w = _win([c])
        patches, calls = self._counted(w)
        try:
            w.crop_btn.setChecked(True)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(calls, [])
        w.close()

    def test_o3_no_lifecycle_method_calls_a_playback_verb(self) -> None:
        names = [
            n for n in dir(MainWindow)
            if n.startswith(("_on_crop", "_crop_", "_begin_crop",
                             "_finish_crop", "_commit_crop"))
        ]
        self.assertTrue(names, "crop lifecycle methods must exist")
        for name in names:
            member = getattr(MainWindow, name)
            if not callable(member):
                continue
            src = inspect.getsource(member)
            for verb in (".play(", ".pause(", ".setPosition("):
                with self.subTest(method=name, verb=verb):
                    self.assertNotIn(verb, src)


# --- Groups P / Q: structural scope and retired guards ------------------


REJECTED = (
    "crop_fit_mode", "canvas_fit", "canvas_aspect", "CROP_FIT_MODES",
    "set_fit_mode", "fit_mode",
)


class StructuralScopeTests(unittest.TestCase):
    @staticmethod
    def _src(name: str) -> str:
        return (SRC_DIR / name).read_text(encoding="utf-8")

    def test_p1_no_rejected_canvas_fit_architecture(self) -> None:
        for name in ("app.py", "crop_overlay.py", "clip.py", "exporter.py"):
            for token in REJECTED:
                with self.subTest(file=name, token=token):
                    self.assertNotIn(token, self._src(name))

    def test_p2_the_domain_model_stays_out_of_the_lifecycle(self) -> None:
        clip_src = self._src("clip.py")
        for token in ("crop_draft", "crop_edit", "confirmRequested",
                      "CropOverlay", "crop_aspect_lock", "crop_ratio"):
            with self.subTest(token=token):
                self.assertNotIn(token, clip_src)

    def test_p2_clip_exposes_only_the_2i_a_committed_fields(self) -> None:
        c = _clip(_asset())
        for attr in ("crop_draft", "crop_edit_id", "crop_aspect_lock",
                     "crop_ratio", "crop_fit_mode"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(c, attr))
        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)

    def test_p3_the_exporter_stays_out_of_the_lifecycle(self) -> None:
        exp_src = self._src("exporter.py")
        for token in ("crop_edit", "crop_btn", "crop_overlay", "CropOverlay",
                      "confirmRequested", ".crop_preset"):
            with self.subTest(token=token):
                self.assertNotIn(token, exp_src)

    def test_p3_ffmpeg_utils_stays_crop_unaware(self) -> None:
        ff_src = self._src("ffmpeg_utils.py")
        for token in (".crop_rect", ".crop_preset", "crop_edit",
                      "CROP_ASPECT_PRESETS"):
            with self.subTest(token=token):
                self.assertNotIn(token, ff_src)

    def test_p4_no_preview_canvas_geometry_is_used_to_show_crop(self) -> None:
        """Crop must never be represented by zooming the preview."""
        for name in [n for n in dir(MainWindow)
                     if n.startswith(("_on_crop", "_crop_", "_begin_crop",
                                      "_finish_crop", "_commit_crop"))]:
            member = getattr(MainWindow, name)
            if not callable(member):
                continue
            src = inspect.getsource(member)
            for token in ("sceneRect", "update_canvas", "set_native_size"):
                with self.subTest(method=name, token=token):
                    self.assertNotIn(token, src)

    def test_p4_the_overlay_adds_no_canvas_geometry_api(self) -> None:
        o = CropOverlay()
        for attr in ("update_canvas", "set_fit_mode", "canvas_fit",
                     "canvas_aspect", "sceneRect"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(o, attr))

    def test_q1_the_legacy_global_crop_field_still_exists(self) -> None:
        """``ExportJob.crop`` stays; this slice only stops feeding it."""
        app_src = self._src("app.py")
        self.assertIn("crop=self._crop_pixels()", app_src)


if __name__ == "__main__":
    unittest.main()
