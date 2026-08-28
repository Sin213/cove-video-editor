"""Tab 2F: crop aspect-ratio presets, aspect-locked resizing, Fit to canvas.

The crop rectangle stays in normalized 0..1 *source* coordinates, so a
desired pixel aspect ratio cannot be applied to the normalized width and
height directly. The normalized ratio is always::

    norm_ratio = target_pixel_aspect / source_pixel_aspect

Every geometry assertion below therefore checks the *effective pixel*
aspect ratio (``rect.width() * src_w`` over ``rect.height() * src_h``)
rather than the raw normalized numbers.

Qt pieces run on the ``offscreen`` platform plugin so widget geometry is
real rather than mocked.
"""
from __future__ import annotations

import dataclasses
import os
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from cove_video_editor import crop_overlay  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402
from cove_video_editor.clip import Clip, MediaAsset  # noqa: E402
from cove_video_editor.crop_overlay import (  # noqa: E402
    CROP_ASPECT_PRESETS, MIN_NORMALIZED, CropOverlay,
)
from cove_video_editor.exporter import ExportJob  # noqa: E402


_app: QApplication | None = None

TOL = 1e-6

# The seven approved preset labels, in the order the selector shows them.
PRESET_LABELS = [
    "Free (Custom)",
    "16:9 (Landscape / YouTube)",
    "9:16 (TikTok / Reels / Shorts)",
    "1:1 (Square / Instagram)",
    "4:5 (Portrait / Social)",
    "4:3 (Standard / Classic)",
    "21:9 (Cinematic / Ultrawide)",
]


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _overlay(src_w: int, src_h: int) -> CropOverlay:
    """An overlay sized like the real preview, told about a real source."""
    o = CropOverlay()
    o.resize(800, 450)
    o.set_video_aspect(src_w / src_h)
    return o


def _pixel_aspect(r: QRectF, src_w: int, src_h: int) -> float:
    return (r.width() * src_w) / (r.height() * src_h)


class _GeometryAsserts:
    """Shared assertions about a normalized crop rect."""

    def assert_in_bounds(self, r: QRectF) -> None:
        self.assertGreaterEqual(r.left(), -TOL, f"left out of bounds: {r}")
        self.assertGreaterEqual(r.top(), -TOL, f"top out of bounds: {r}")
        self.assertLessEqual(r.right(), 1.0 + TOL, f"right out of bounds: {r}")
        self.assertLessEqual(r.bottom(), 1.0 + TOL, f"bottom out of bounds: {r}")
        self.assertGreater(r.width(), 0.0)
        self.assertGreater(r.height(), 0.0)

    def assert_pixel_aspect(self, r: QRectF, src_w: int, src_h: int,
                            target: float, places: int = 6) -> None:
        self.assertAlmostEqual(
            _pixel_aspect(r, src_w, src_h), target, places=places,
            msg=f"effective pixel aspect wrong for {r}",
        )

    def assert_max_area_centered(self, r: QRectF, src_w: int, src_h: int,
                                 target: float) -> None:
        """Largest rect at ``target`` pixel aspect, centered in the source.

        Maximum area at a fixed aspect means at least one normalized axis
        is saturated at 1.0; the *other* axis must then be centered.
        """
        self.assert_in_bounds(r)
        self.assert_pixel_aspect(r, src_w, src_h, target)
        saturated_w = abs(r.width() - 1.0) < 1e-9
        saturated_h = abs(r.height() - 1.0) < 1e-9
        self.assertTrue(
            saturated_w or saturated_h,
            f"not maximum area - neither axis fills the source: {r}",
        )
        if not saturated_w:
            self.assertAlmostEqual(r.left(), (1.0 - r.width()) / 2.0, places=9)
        if not saturated_h:
            self.assertAlmostEqual(r.top(), (1.0 - r.height()) / 2.0, places=9)


# ---- Group A: preset registry ----------------------------------------------


class TestPresetRegistry(unittest.TestCase, _GeometryAsserts):
    def test_exact_seven_presets_in_order(self) -> None:
        self.assertEqual(list(CROP_ASPECT_PRESETS.keys()), PRESET_LABELS)

    def test_preset_ratio_values(self) -> None:
        expected = {
            "Free (Custom)": None,
            "16:9 (Landscape / YouTube)": 16 / 9,
            "9:16 (TikTok / Reels / Shorts)": 9 / 16,
            "1:1 (Square / Instagram)": 1.0,
            "4:5 (Portrait / Social)": 4 / 5,
            "4:3 (Standard / Classic)": 4 / 3,
            "21:9 (Cinematic / Ultrawide)": 21 / 9,
        }
        for label, ratio in expected.items():
            with self.subTest(preset=label):
                if ratio is None:
                    self.assertIsNone(CROP_ASPECT_PRESETS[label])
                else:
                    self.assertAlmostEqual(
                        CROP_ASPECT_PRESETS[label], ratio, places=12,
                    )

    def test_default_aspect_lock_is_none(self) -> None:
        o = CropOverlay()
        self.assertIsNone(o.aspect_ratio_preset())

    def test_default_rect_is_full_frame(self) -> None:
        o = CropOverlay()
        self.assertEqual(o.normalized_rect(), QRectF(0.0, 0.0, 1.0, 1.0))


# ---- Group B: maximum-area preset geometry ---------------------------------


class TestMaxAreaPresetGeometry(unittest.TestCase, _GeometryAsserts):
    def test_b1_landscape_source_to_9x16(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(9 / 16, "9:16 (TikTok / Reels / Shorts)")
        r = o.normalized_rect()
        self.assert_max_area_centered(r, 1920, 1080, 9 / 16)
        self.assertAlmostEqual(r.height(), 1.0, places=9)
        self.assertAlmostEqual(r.width(), 81 / 256, places=9)
        self.assertAlmostEqual(r.left(), (1.0 - 81 / 256) / 2.0, places=9)
        self.assertAlmostEqual(r.top(), 0.0, places=9)

    def test_b2_landscape_source_to_1x1(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(1.0, "1:1 (Square / Instagram)")
        r = o.normalized_rect()
        self.assert_max_area_centered(r, 1920, 1080, 1.0)
        self.assertAlmostEqual(r.height(), 1.0, places=9)
        self.assertAlmostEqual(r.width(), 9 / 16, places=9)
        self.assertAlmostEqual(r.left(), (1.0 - 9 / 16) / 2.0, places=9)

    def test_b3_landscape_source_to_4x5(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(4 / 5, "4:5 (Portrait / Social)")
        r = o.normalized_rect()
        self.assert_max_area_centered(r, 1920, 1080, 4 / 5)
        self.assertAlmostEqual(r.height(), 1.0, places=9)
        self.assertAlmostEqual(r.width(), 0.45, places=9)

    def test_b4_landscape_source_to_4x3(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(4 / 3, "4:3 (Standard / Classic)")
        r = o.normalized_rect()
        self.assert_max_area_centered(r, 1920, 1080, 4 / 3)
        self.assertAlmostEqual(r.height(), 1.0, places=9)
        self.assertAlmostEqual(r.width(), 0.75, places=9)

    def test_b5_landscape_source_to_21x9(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(21 / 9, "21:9 (Cinematic / Ultrawide)")
        r = o.normalized_rect()
        self.assert_max_area_centered(r, 1920, 1080, 21 / 9)
        # Wider than the source: width saturates, height shrinks instead.
        self.assertAlmostEqual(r.width(), 1.0, places=9)
        self.assertAlmostEqual(r.height(), (16 / 9) / (21 / 9), places=9)
        self.assertAlmostEqual(r.left(), 0.0, places=9)

    def test_b6_portrait_source_to_16x9(self) -> None:
        o = _overlay(1080, 1920)
        o.set_aspect_ratio_preset(16 / 9, "16:9 (Landscape / YouTube)")
        r = o.normalized_rect()
        self.assert_max_area_centered(r, 1080, 1920, 16 / 9)
        self.assertAlmostEqual(r.width(), 1.0, places=9)
        self.assertAlmostEqual(r.height(), 81 / 256, places=9)
        self.assertAlmostEqual(r.top(), (1.0 - 81 / 256) / 2.0, places=9)
        self.assertAlmostEqual(r.left(), 0.0, places=9)

    def test_b7_matching_source_and_preset_fills_frame(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(16 / 9, "16:9 (Landscape / YouTube)")
        r = o.normalized_rect()
        self.assert_max_area_centered(r, 1920, 1080, 16 / 9)
        self.assertAlmostEqual(r.left(), 0.0, places=9)
        self.assertAlmostEqual(r.top(), 0.0, places=9)
        self.assertAlmostEqual(r.width(), 1.0, places=9)
        self.assertAlmostEqual(r.height(), 1.0, places=9)

    def test_preset_lock_is_queryable(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(9 / 16, "9:16 (TikTok / Reels / Shorts)")
        self.assertAlmostEqual(o.aspect_ratio_preset(), 9 / 16, places=12)

    def test_preset_emits_crop_changed(self) -> None:
        o = _overlay(1920, 1080)
        seen: list[QRectF] = []
        o.cropChanged.connect(seen.append)
        o.set_aspect_ratio_preset(1.0, "1:1 (Square / Instagram)")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], o.normalized_rect())


# ---- Group G: source-aspect changes ----------------------------------------


class TestSourceAspectChange(unittest.TestCase, _GeometryAsserts):
    def test_g1_locked_preset_recomputes_for_new_source_aspect(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(9 / 16, "9:16 (TikTok / Reels / Shorts)")
        self.assert_pixel_aspect(o.normalized_rect(), 1920, 1080, 9 / 16)

        for src_w, src_h in ((1440, 1080), (1080, 1920)):
            with self.subTest(source=(src_w, src_h)):
                o.set_video_aspect(src_w / src_h)
                r = o.normalized_rect()
                self.assertAlmostEqual(o.aspect_ratio_preset(), 9 / 16, places=12)
                self.assert_max_area_centered(r, src_w, src_h, 9 / 16)

    def test_g3_same_source_aspect_does_not_refit_a_locked_crop(self) -> None:
        """Re-announcing the *same* source is synchronisation, not a change.

        The app re-syncs the aspect on preset changes, crop toggles and
        preview switches; refitting there would silently throw away a crop
        the user had moved or resized.
        """
        o = _overlay(*(1920, 1080))
        o.set_aspect_ratio_preset(9 / 16, "9:16 (TikTok / Reels / Shorts)")
        edited = QRectF(0.10, 0.05, 0.20, 0.632)
        o.set_normalized_rect(edited)
        before = o.normalized_rect()
        o.set_video_aspect(1920 / 1080)
        self.assertEqual(o.normalized_rect(), before)
        self.assertAlmostEqual(o.aspect_ratio_preset(), 9 / 16, places=12)

    def test_g4_extreme_source_keeps_the_exact_target_ratio(self) -> None:
        """The minimum size must not silently rewrite the requested ratio."""
        for src_w, src_h, target, label in (
            (4096, 256, 9 / 16, "9:16"),
            (256, 4096, 21 / 9, "21:9"),
        ):
            with self.subTest(source=(src_w, src_h), preset=label):
                o = _overlay(src_w, src_h)
                o.set_aspect_ratio_preset(target, label)
                r = o.normalized_rect()
                self.assert_max_area_centered(r, src_w, src_h, target)

    def test_g4_extreme_source_drag_stays_on_ratio_and_in_bounds(self) -> None:
        o = _overlay(4096, 256)
        o.set_aspect_ratio_preset(9 / 16, "9:16")
        for handle in ("br", "tl", "r", "b"):
            with self.subTest(handle=handle):
                o.set_aspect_ratio_preset(9 / 16, "9:16")
                dx, dy = INWARD[handle]
                after = _drag(o, handle, dx, dy)
                self.assert_in_bounds(after)
                self.assert_pixel_aspect(after, 4096, 256, 9 / 16, places=4)

    def test_g2_free_mode_source_change_creates_no_lock(self) -> None:
        o = _overlay(1920, 1080)
        o.set_normalized_rect(QRectF(0.2, 0.3, 0.5, 0.4))
        before = o.normalized_rect()
        o.set_video_aspect(1080 / 1920)
        self.assertIsNone(o.aspect_ratio_preset())
        self.assertEqual(o.normalized_rect(), before)


# ---- Aspect badge ----------------------------------------------------------


class TestAspectBadge(unittest.TestCase):
    """The on-canvas ratio pill only exists while a preset is locked."""

    def test_badge_hidden_in_free_mode(self) -> None:
        o = _overlay(1920, 1080)
        self.assertIsNone(o.aspect_badge_text())
        o.set_aspect_ratio_preset(None, "Free (Custom)")
        self.assertIsNone(o.aspect_badge_text())

    def test_badge_shows_compact_ratio_for_each_preset(self) -> None:
        expected = {
            "16:9 (Landscape / YouTube)": "16:9",
            "9:16 (TikTok / Reels / Shorts)": "9:16",
            "1:1 (Square / Instagram)": "1:1",
            "4:5 (Portrait / Social)": "4:5",
            "4:3 (Standard / Classic)": "4:3",
            "21:9 (Cinematic / Ultrawide)": "21:9",
        }
        o = _overlay(1920, 1080)
        for label, tag in expected.items():
            with self.subTest(preset=label):
                o.set_aspect_ratio_preset(CROP_ASPECT_PRESETS[label], label)
                self.assertEqual(o.aspect_badge_text(), tag)

    def test_badge_cleared_by_reset(self) -> None:
        o = _overlay(1920, 1080)
        o.set_aspect_ratio_preset(1.0, "1:1 (Square / Instagram)")
        o.reset()
        self.assertIsNone(o.aspect_badge_text())


# ---- Group C: Free / Reset / Fit to canvas ---------------------------------


class TestFreeResetAndFit(unittest.TestCase, _GeometryAsserts):
    SRC = (1920, 1080)

    def _locked_9x16(self) -> CropOverlay:
        o = _overlay(*self.SRC)
        o.set_aspect_ratio_preset(9 / 16, "9:16 (TikTok / Reels / Shorts)")
        return o

    def test_c1_free_clears_the_lock(self) -> None:
        o = self._locked_9x16()
        self.assertIsNotNone(o.aspect_ratio_preset())
        o.set_aspect_ratio_preset(None, "Free (Custom)")
        self.assertIsNone(o.aspect_ratio_preset())

    def test_c2_free_preserves_the_current_rect(self) -> None:
        o = self._locked_9x16()
        custom = QRectF(0.22, 0.13, 0.31, 0.44)
        o.set_normalized_rect(custom)
        before = o.normalized_rect()
        o.set_aspect_ratio_preset(None, "Free (Custom)")
        self.assertEqual(
            o.normalized_rect(), before,
            "selecting Free is not a reset - the rect must survive",
        )

    def test_c3_reset_clears_lock_and_restores_full_frame(self) -> None:
        o = self._locked_9x16()
        o.reset()
        self.assertIsNone(o.aspect_ratio_preset())
        self.assertEqual(o.normalized_rect(), QRectF(0.0, 0.0, 1.0, 1.0))

    def test_c3_reset_leaves_later_drags_unconstrained(self) -> None:
        o = self._locked_9x16()
        o.reset()
        before = o.normalized_rect()
        after = _drag(o, "r", -80.0, 0.0)
        self.assertAlmostEqual(
            after.height(), before.height(), places=9,
            msg="a stale aspect lock survived reset",
        )

    def test_c4_fit_to_canvas_maximizes_the_locked_ratio(self) -> None:
        o = self._locked_9x16()
        o.set_normalized_rect(QRectF(0.2, 0.2, 0.2, 0.35))
        o.fit_to_canvas()
        r = o.normalized_rect()
        self.assert_max_area_centered(r, *self.SRC, 9 / 16)
        self.assertAlmostEqual(r.height(), 1.0, places=9)
        self.assertAlmostEqual(r.width(), 81 / 256, places=9)
        self.assertAlmostEqual(r.left(), (1.0 - 81 / 256) / 2.0, places=9)
        self.assertAlmostEqual(o.aspect_ratio_preset(), 9 / 16, places=12)

    def test_c4_fit_to_canvas_after_a_locked_drag(self) -> None:
        o = self._locked_9x16()
        _drag(o, "br", -120.0, -60.0)
        _drag(o, "move", 30.0, 10.0)
        self.assertNotEqual(o.normalized_rect(), o._max_area_rect(9 / 16))
        o.fit_to_canvas()
        self.assert_max_area_centered(o.normalized_rect(), *self.SRC, 9 / 16)

    def test_c5_fit_to_canvas_in_free_mode_fills_the_frame(self) -> None:
        o = _overlay(*self.SRC)
        o.set_normalized_rect(QRectF(0.13, 0.27, 0.4, 0.5))
        o.fit_to_canvas()
        self.assertEqual(o.normalized_rect(), QRectF(0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(o.aspect_ratio_preset())

    def test_c5_fit_to_canvas_emits_crop_changed(self) -> None:
        o = self._locked_9x16()
        o.set_normalized_rect(QRectF(0.2, 0.2, 0.2, 0.35))
        seen: list[QRectF] = []
        o.cropChanged.connect(seen.append)
        o.fit_to_canvas()
        self.assertEqual(seen, [o.normalized_rect()])


# ---- Groups D/E/F: dragging -------------------------------------------------


# ``_overlay`` sizes the widget 800x450, and a 16:9 source fills it exactly,
# so one normalized unit is 800px across and 450px down.
PX_X = 800.0
PX_Y = 450.0

# Handle -> widget-space delta that drags that handle toward the rect centre.
INWARD = {
    "br": (-80.0, -45.0),
    "tl": (+80.0, +45.0),
    "tr": (-80.0, +45.0),
    "bl": (+80.0, -45.0),
    "r": (-80.0, 0.0),
    "l": (+80.0, 0.0),
    "b": (0.0, -45.0),
    "t": (0.0, +45.0),
}

# Handle -> the two rect edges that must not move while it is dragged.
ANCHORS = {
    "br": ("left", "top"),
    "tl": ("right", "bottom"),
    "tr": ("left", "bottom"),
    "bl": ("right", "top"),
    "r": ("left",),
    "l": ("right",),
    "b": ("top",),
    "t": ("bottom",),
}


def _drag(o: CropOverlay, handle: str, dx_px: float, dy_px: float) -> QRectF:
    """Press ``handle`` at its real widget position and drag it by a delta."""
    c = o._crop_rect_widget()
    start = (
        QPointF(c.center()) if handle == "move" else o._handle_centers(c)[handle]
    )
    o._drag_target = handle
    o._drag_start_widget = QPointF(start)
    o._drag_start_rect = QRectF(o.normalized_rect())
    o._apply_drag(QPointF(start.x() + dx_px, start.y() + dy_px))
    o._drag_target = None
    return o.normalized_rect()


def _edge(r: QRectF, name: str) -> float:
    return {
        "left": r.left(), "right": r.right(),
        "top": r.top(), "bottom": r.bottom(),
    }[name]


class TestLockedCornerDrags(unittest.TestCase, _GeometryAsserts):
    """Group D - all four corners keep the locked ratio, anchored opposite."""

    SRC = (1920, 1080)
    TARGET = 1.0  # 1:1, which leaves slack on the horizontal axis.

    def _locked(self) -> CropOverlay:
        o = _overlay(*self.SRC)
        o.set_aspect_ratio_preset(self.TARGET, "1:1 (Square / Instagram)")
        return o

    def _assert_anchored(self, handle: str, before: QRectF, after: QRectF) -> None:
        for name in ANCHORS[handle]:
            self.assertAlmostEqual(
                _edge(after, name), _edge(before, name), places=9,
                msg=f"{handle} drag moved the anchored {name} edge",
            )

    def test_d_corner_shrink_then_grow_preserves_ratio(self) -> None:
        for handle in ("tl", "tr", "bl", "br"):
            with self.subTest(handle=handle, phase="shrink"):
                o = self._locked()
                before = o.normalized_rect()
                dx, dy = INWARD[handle]
                after = _drag(o, handle, dx, dy)
                self.assert_in_bounds(after)
                self.assert_pixel_aspect(after, *self.SRC, self.TARGET)
                self._assert_anchored(handle, before, after)
                self.assertLess(
                    after.width(), before.width(),
                    f"{handle} inward drag did not shrink the box",
                )

            with self.subTest(handle=handle, phase="grow"):
                shrunk = o.normalized_rect()
                grown = _drag(o, handle, -dx / 2.0, -dy / 2.0)
                self.assert_in_bounds(grown)
                self.assert_pixel_aspect(grown, *self.SRC, self.TARGET)
                self._assert_anchored(handle, shrunk, grown)
                self.assertGreater(
                    grown.width(), shrunk.width(),
                    f"{handle} outward drag did not grow the box",
                )


class TestLockedEdgeDrags(unittest.TestCase, _GeometryAsserts):
    """Group E - edge handles stay live while locked and compensate."""

    SRC = (1920, 1080)
    TARGET = 1.0

    def _locked(self) -> CropOverlay:
        o = _overlay(*self.SRC)
        o.set_aspect_ratio_preset(self.TARGET, "1:1 (Square / Instagram)")
        return o

    def _centre(self, r: QRectF) -> tuple[float, float]:
        return ((r.left() + r.right()) / 2.0, (r.top() + r.bottom()) / 2.0)

    def test_e_edge_drags_preserve_ratio_and_orthogonal_centre(self) -> None:
        for handle in ("l", "r", "t", "b"):
            horizontal = handle in ("l", "r")
            o = self._locked()
            before = o.normalized_rect()
            dx, dy = INWARD[handle]

            with self.subTest(handle=handle, phase="shrink"):
                after = _drag(o, handle, dx, dy)
                self.assert_in_bounds(after)
                self.assert_pixel_aspect(after, *self.SRC, self.TARGET)
                for name in ANCHORS[handle]:
                    self.assertAlmostEqual(
                        _edge(after, name), _edge(before, name), places=9,
                        msg=f"{handle} drag moved the anchored {name} edge",
                    )
                if horizontal:
                    self.assertLess(after.width(), before.width())
                    # The compensating axis has to move too.
                    self.assertLess(after.height(), before.height())
                    self.assertAlmostEqual(
                        self._centre(after)[1], self._centre(before)[1],
                        places=9, msg="vertical centre drifted",
                    )
                else:
                    self.assertLess(after.height(), before.height())
                    self.assertLess(after.width(), before.width())
                    self.assertAlmostEqual(
                        self._centre(after)[0], self._centre(before)[0],
                        places=9, msg="horizontal centre drifted",
                    )

            with self.subTest(handle=handle, phase="grow"):
                shrunk = o.normalized_rect()
                grown = _drag(o, handle, -dx / 2.0, -dy / 2.0)
                self.assert_in_bounds(grown)
                self.assert_pixel_aspect(grown, *self.SRC, self.TARGET)
                if horizontal:
                    self.assertGreater(grown.width(), shrunk.width())
                else:
                    self.assertGreater(grown.height(), shrunk.height())


class TestLockedMoveMinimumAndFreeRegression(unittest.TestCase, _GeometryAsserts):
    """Group F - moving, the minimum size, and free-mode drag behaviour."""

    SRC = (1920, 1080)

    def _locked_9x16(self) -> CropOverlay:
        o = _overlay(*self.SRC)
        o.set_aspect_ratio_preset(9 / 16, "9:16 (TikTok / Reels / Shorts)")
        return o

    def test_f1_move_preserves_dimensions(self) -> None:
        o = self._locked_9x16()
        # Shrink first so there is room to move on both axes.
        _drag(o, "br", -80.0, -45.0)
        before = o.normalized_rect()
        after = _drag(o, "move", 40.0, 20.0)
        self.assertAlmostEqual(after.width(), before.width(), places=9)
        self.assertAlmostEqual(after.height(), before.height(), places=9)
        self.assert_pixel_aspect(after, *self.SRC, 9 / 16)
        self.assertAlmostEqual(after.left(), before.left() + 40.0 / PX_X, places=9)
        self.assertAlmostEqual(after.top(), before.top() + 20.0 / PX_Y, places=9)

    def test_f2_move_clamps_inside_source(self) -> None:
        o = self._locked_9x16()
        _drag(o, "br", -80.0, -45.0)
        before = o.normalized_rect()
        for dx_px, dy_px in ((-4000.0, -4000.0), (4000.0, 4000.0)):
            with self.subTest(delta=(dx_px, dy_px)):
                o.set_normalized_rect(before)
                after = _drag(o, "move", dx_px, dy_px)
                self.assert_in_bounds(after)
                self.assertAlmostEqual(after.width(), before.width(), places=9)
                self.assertAlmostEqual(after.height(), before.height(), places=9)

    def test_f3_aggressive_shrink_respects_minimum_and_ratio(self) -> None:
        o = self._locked_9x16()
        after = _drag(o, "br", -4000.0, -4000.0)
        self.assert_in_bounds(after)
        self.assert_pixel_aspect(after, *self.SRC, 9 / 16)
        self.assertGreaterEqual(after.width(), MIN_NORMALIZED - TOL)
        self.assertGreaterEqual(after.height(), MIN_NORMALIZED - TOL)
        self.assertAlmostEqual(
            min(after.width(), after.height()), MIN_NORMALIZED, places=9,
            msg="locked crop should shrink to exactly the shared minimum",
        )

    def test_f4_free_mode_edge_drag_ignores_the_former_preset(self) -> None:
        o = self._locked_9x16()
        o.set_aspect_ratio_preset(None, "Free (Custom)")
        before = o.normalized_rect()
        after = _drag(o, "r", -80.0, 0.0)
        self.assertAlmostEqual(
            after.height(), before.height(), places=9,
            msg="free-mode horizontal drag must not touch the height",
        )
        self.assertAlmostEqual(after.width(), before.width() - 80.0 / PX_X, places=9)
        self.assertAlmostEqual(after.top(), before.top(), places=9)

    def test_f4_free_mode_vertical_drag_is_independent(self) -> None:
        o = _overlay(*self.SRC)
        o.set_normalized_rect(QRectF(0.2, 0.2, 0.6, 0.6))
        before = o.normalized_rect()
        after = _drag(o, "b", 0.0, -45.0)
        self.assertAlmostEqual(after.width(), before.width(), places=9)
        self.assertAlmostEqual(after.height(), before.height() - 45.0 / PX_Y, places=9)


# ---- Group H: app wiring ----------------------------------------------------


def _win(src_w: int = 1920, src_h: int = 1080, *, with_clip: bool = True):
    """A MainWindow with one selected visual clip (or none at all).

    The real background NVENC/AMF probe is suppressed: it spawns ffmpeg
    children that outlive these windows and leak into
    ``ffmpeg_utils._active_probe_procs``. Nothing about crop geometry
    depends on encoder capabilities, so the probe is pure interference.
    """
    with unittest.mock.patch.object(
        MainWindow, "_start_encoder_probe", lambda self: None,
    ):
        w = MainWindow()
    if with_clip:
        asset = MediaAsset(
            path=Path("a.mp4"), duration=600.0, width=src_w, height=src_h,
            fps=30.0, has_audio=True,
        )
        clip = Clip(
            asset=asset, timeline_start=0.0, src_start=0.0, src_end=10.0,
        )
        w._clips = [clip]
        w.timeline.set_clips(w._clips)
        w.timeline.select_clip(clip.id)
    return w


class TestCropControlWiring(unittest.TestCase, _GeometryAsserts):
    SRC = (1920, 1080)

    def test_h1_aspect_combo_exists(self) -> None:
        w = _win()
        self.assertIsInstance(w.crop_aspect_combo, QComboBox)

    def test_h2_combo_lists_the_seven_presets_in_order(self) -> None:
        w = _win()
        items = [
            w.crop_aspect_combo.itemText(i)
            for i in range(w.crop_aspect_combo.count())
        ]
        self.assertEqual(items, PRESET_LABELS)

    def test_h3_combo_defaults_to_free(self) -> None:
        w = _win()
        self.assertEqual(w.crop_aspect_combo.currentText(), "Free (Custom)")
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())

    def test_h4_fit_button_exists_and_is_labelled(self) -> None:
        w = _win()
        self.assertEqual(w.crop_fit_btn.text(), "Fit to canvas")

    def test_h5_crop_controls_hidden_while_crop_is_off(self) -> None:
        w = _win()
        self.assertFalse(w.crop_btn.isChecked())
        for name in ("crop_aspect_combo", "crop_fit_btn", "crop_reset_btn"):
            with self.subTest(control=name):
                self.assertFalse(getattr(w, name).isVisible())

    def test_h6_crop_controls_shown_while_crop_is_on(self) -> None:
        w = _win()
        w.show()
        w.crop_btn.setChecked(True)
        self.assertTrue(w.crop_btn.isChecked())
        for name in ("crop_aspect_combo", "crop_fit_btn", "crop_reset_btn"):
            with self.subTest(control=name):
                self.assertTrue(getattr(w, name).isVisible())
        w.crop_btn.setChecked(False)
        for name in ("crop_aspect_combo", "crop_fit_btn", "crop_reset_btn"):
            with self.subTest(control=name, crop="off"):
                self.assertFalse(getattr(w, name).isVisible())
        w.close()

    def test_h7_selecting_a_preset_locks_and_fits_the_overlay(self) -> None:
        w = _win()
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("9:16 (TikTok / Reels / Shorts)")
        self.assertAlmostEqual(
            w.crop_overlay.aspect_ratio_preset(), 9 / 16, places=12,
        )
        self.assert_max_area_centered(
            w.crop_overlay.normalized_rect(), *self.SRC, 9 / 16,
        )

    def test_h7_preset_picks_up_the_selected_clip_aspect(self) -> None:
        w = _win(1080, 1920)
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("16:9 (Landscape / YouTube)")
        self.assert_max_area_centered(
            w.crop_overlay.normalized_rect(), 1080, 1920, 16 / 9,
        )

    def test_h8_fit_button_maximizes_the_locked_crop(self) -> None:
        w = _win()
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("1:1 (Square / Instagram)")
        w.crop_overlay.set_normalized_rect(QRectF(0.3, 0.3, 0.2, 0.2))
        w.crop_fit_btn.click()
        self.assert_max_area_centered(
            w.crop_overlay.normalized_rect(), *self.SRC, 1.0,
        )

    def test_h8_fit_button_in_free_mode_fills_the_frame(self) -> None:
        w = _win()
        w.crop_btn.setChecked(True)
        w.crop_overlay.set_normalized_rect(QRectF(0.3, 0.3, 0.2, 0.2))
        w.crop_fit_btn.click()
        self.assertEqual(
            w.crop_overlay.normalized_rect(), QRectF(0.0, 0.0, 1.0, 1.0),
        )
        self.assertEqual(w.crop_aspect_combo.currentText(), "Free (Custom)")
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())

    def test_h9_reset_returns_combo_to_free_and_clears_the_overlay(self) -> None:
        w = _win()
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("21:9 (Cinematic / Ultrawide)")
        w.crop_reset_btn.click()
        self.assertEqual(w.crop_aspect_combo.currentText(), "Free (Custom)")
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())
        self.assertEqual(
            w.crop_overlay.normalized_rect(), QRectF(0.0, 0.0, 1.0, 1.0),
        )

    def test_h9_choosing_free_keeps_an_edited_locked_crop(self) -> None:
        """The app re-syncs the source aspect first; that must not reset."""
        w = _win()
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("1:1 (Square / Instagram)")
        edited = QRectF(0.05, 0.10, 0.30, 0.5333)
        w.crop_overlay.set_normalized_rect(edited)
        before = w.crop_overlay.normalized_rect()
        w.crop_aspect_combo.setCurrentText("Free (Custom)")
        self.assertEqual(w.crop_overlay.normalized_rect(), before)
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())

    def test_h6_a_confirmed_crop_survives_a_toggle_cycle(self) -> None:
        """Retargeted by Tab 2I-C.

        Toggling Crop off is now Cancel, so an *unconfirmed* edit is
        supposed to be discarded on the way out. The invariant worth
        keeping is the one the user actually cares about: a crop they
        confirmed comes back byte-for-byte on the next toggle cycle.
        """
        w = _win()
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("1:1 (Square / Instagram)")
        edited = QRectF(0.05, 0.10, 0.30, 0.5333)
        w.crop_overlay.set_normalized_rect(edited)
        before = w.crop_overlay.normalized_rect()
        w.crop_confirm_btn.click()
        w.crop_btn.setChecked(True)
        self.assertEqual(w.crop_overlay.normalized_rect(), before)
        self.assertAlmostEqual(
            w.crop_overlay.aspect_ratio_preset(), 1.0, places=12,
        )

    def test_h6_an_unconfirmed_crop_is_discarded_by_a_toggle_cycle(self) -> None:
        w = _win()
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("1:1 (Square / Instagram)")
        w.crop_overlay.set_normalized_rect(QRectF(0.05, 0.10, 0.30, 0.5333))
        w.crop_btn.setChecked(False)
        w.crop_btn.setChecked(True)
        self.assertEqual(
            w.crop_overlay.normalized_rect(), QRectF(0.0, 0.0, 1.0, 1.0),
        )
        self.assertIsNone(w._clips[0].crop_rect)

    def test_h10_no_selected_clip_does_not_crash(self) -> None:
        w = _win(with_clip=False)
        w.crop_aspect_combo.setCurrentText("4:5 (Portrait / Social)")
        w.crop_fit_btn.click()
        w.crop_reset_btn.click()
        self.assertEqual(w.crop_aspect_combo.currentText(), "Free (Custom)")
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())

    def test_crop_entry_on_an_uncropped_clip_starts_full_frame_free(self) -> None:
        """Retargeted by Tab 2I-C.

        Crop entry used to seed a transient 10% inset because the overlay
        was global scratch state with nothing to restore from. Entry is
        now scoped to the selected clip's *committed* crop, and an
        uncropped clip has none, so the session starts at the whole frame
        in Free. The surviving Tab 2F invariant - Free entry carries no
        aspect lock - is asserted below.
        """
        w = _win()
        w.crop_btn.setChecked(True)
        self.assertEqual(
            w.crop_overlay.normalized_rect(), QRectF(0.0, 0.0, 1.0, 1.0),
        )
        self.assertIsNone(w.crop_overlay.aspect_ratio_preset())
        self.assertEqual(w.crop_aspect_combo.currentText(), "Free (Custom)")

    def test_crop_entry_applies_the_committed_preset(self) -> None:
        """Retargeted by Tab 2I-C.

        Entry used to re-apply whatever the selector happened to hold.
        The clip's committed preset is the authority now, and leftover
        selector state must not leak into a clip that never had it.
        """
        w = _win()
        w.crop_aspect_combo.blockSignals(True)
        w.crop_aspect_combo.setCurrentText("21:9 (Cinematic / Ultrawide)")
        w.crop_aspect_combo.blockSignals(False)
        clip = w._clips[0]
        clip.crop_rect = (0.2, 0.0, 0.6, 1.0)
        clip.crop_preset = "1:1 (Square / Instagram)"
        w.crop_btn.setChecked(True)
        self.assertAlmostEqual(
            w.crop_overlay.aspect_ratio_preset(), 1.0, places=12,
        )
        self.assertEqual(
            w.crop_aspect_combo.currentText(), "1:1 (Square / Instagram)",
        )
        # The stored rectangle wins over the preset's centred default.
        self.assertEqual(
            w.crop_overlay.normalized_rect(), QRectF(0.2, 0.0, 0.6, 1.0),
        )


# ---- Group I: the existing export crop path --------------------------------


class TestExportCropPath(unittest.TestCase):
    def test_i_preset_feeds_the_existing_pixel_crop_helper(self) -> None:
        w = _win(1920, 1080)
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText("9:16 (TikTok / Reels / Shorts)")

        crop = w._crop_pixels()
        self.assertIsNotNone(crop, "a preset crop must reach the export path")
        x, y, cw, ch = crop

        self.assertEqual(cw % 2, 0)
        self.assertEqual(ch % 2, 0)
        self.assertEqual(ch, 1080, "full height on a 16:9 source")
        # Centred within the integer/even rounding the helper already applies.
        self.assertLessEqual(abs((1920 - (x + cw)) - x), 2)
        self.assertAlmostEqual(cw / ch, 9 / 16, delta=0.002)

    def test_i_free_full_frame_crop_stays_none(self) -> None:
        w = _win(1920, 1080)
        w.crop_btn.setChecked(True)
        w.crop_reset_btn.click()
        self.assertIsNone(w._crop_pixels())

    def test_i_export_job_needs_no_preset_metadata(self) -> None:
        names = {f.name for f in dataclasses.fields(ExportJob)}
        for forbidden in (
            "canvas_fit", "canvas_aspect", "crop_preset",
            "crop_aspect", "fit_mode",
        ):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, names)
        self.assertIn("crop", names)


# ---- Group J: structural exclusion -----------------------------------------


FORBIDDEN_TOKENS = (
    "CROP_FIT_MODES", "canvas_fit", "canvas_aspect", "set_fit_mode", "fit_mode",
)

TAB_2F_PRODUCTION = ("app.py", "crop_overlay.py")


class TestNoCanvasFitModeFeature(unittest.TestCase):
    """Tab 2F must not drag in the deferred canvas Fit/Fill/Stretch work."""

    def _src_dir(self) -> Path:
        return Path(crop_overlay.__file__).resolve().parent

    def test_j_production_files_define_no_fit_mode_feature(self) -> None:
        for name in TAB_2F_PRODUCTION:
            text = (self._src_dir() / name).read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS:
                with self.subTest(file=name, token=token):
                    self.assertNotIn(token, text)

    def test_j_overlay_exposes_no_fit_mode_api(self) -> None:
        o = CropOverlay()
        for attr in ("set_fit_mode", "fit_mode", "canvas_fit", "canvas_aspect"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(o, attr))

    def test_j_no_per_clip_crop_model(self) -> None:
        asset = MediaAsset(
            path=Path("a.mp4"), duration=1.0, width=1920, height=1080,
            fps=30.0, has_audio=False,
        )
        c = Clip(asset=asset, timeline_start=0.0, src_start=0.0, src_end=1.0)
        # Tab 2I-A gives Clip committed `crop_rect` / `crop_preset` fields, so
        # this guard now covers only the rejected canvas-fit concept. The
        # committed-crop contract itself lives in tests/test_crop_clip_state.py.
        for attr in ("crop_fit_mode", "canvas_fit", "canvas_aspect"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(c, attr))

    def test_j_exporter_needs_no_crop_preset_knowledge(self) -> None:
        """The exporter only ever sees the existing pixel crop tuple.

        Patch scope for exporter.py / ffmpeg_utils.py is enforced by review,
        not by asserting on the developer's working tree.
        """
        src = Path(crop_overlay.__file__).resolve().parent
        for name in ("exporter.py", "ffmpeg_utils.py"):
            text = (src / name).read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS + ("CROP_ASPECT_PRESETS",):
                with self.subTest(file=name, token=token):
                    self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
