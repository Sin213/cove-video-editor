"""Tab 2J: the confirmed-crop preview visualization.

Tab 2I-C committed ``Clip.crop_rect`` / ``Clip.crop_preset`` through a
draft lifecycle but the preview went back to a bare full-frame picture the
moment the editor closed. This slice adds the read-only counterpart:

    while no crop editor is open, the preview keeps showing the whole
    source frame and paints a dark matte over everything the committed
    crop will discard, a thin boundary on the crop itself, and a compact
    ratio pill inside it.

The dominant risk is *ownership*. The pill must describe the clip the
preview is actually displaying, which during playback is not necessarily
the selected clip - ``MainWindow._preview_clip_id`` is the only authority.
Showing the selected clip's crop over a different clip's picture would be
a confident lie about framing, so an unresolvable preview source hides the
indicator instead of guessing.

Geometry is asserted against the real ``QGraphicsView`` transform and the
paint output is inspected as real pixels on a real ``QImage``: a matte
that covers the wrong pixels is invisible to any state-only assertion.
Qt runs on the ``offscreen`` platform, and the background NVENC/AMF probe
is suppressed for every window - it spawns ffmpeg children that outlive
the window, and no crop visual depends on encoder capabilities.
"""
from __future__ import annotations

import inspect
import os
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor.app import MainWindow, VideoView, crop_badge_label  # noqa: E402
from cove_video_editor.clip import Clip, MediaAsset  # noqa: E402


_app: QApplication | None = None

FREE = "Free (Custom)"
TIKTOK = "9:16 (TikTok / Reels / Shorts)"
SQUARE = "1:1 (Square / Instagram)"
LANDSCAPE = "16:9 (Landscape / YouTube)"

WHITE = 255
# rgba(0, 0, 0, 175) composited over white -> 255 * (1 - 175/255) = 80.
MATTE = 80


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --- helpers -----------------------------------------------------------


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


def _view(src_w: int = 1920, src_h: int = 1080,
          px_w: int = 640, px_h: int = 360) -> VideoView:
    """A standalone VideoView fitted to a known source size.

    Shown, because ``fitInView`` reads the *viewport* size and a hidden
    QGraphicsView keeps a stale default one - geometry asserted against
    an unrealised layout would not describe anything the user can see.
    """
    v = VideoView()
    v.resize(px_w, px_h)
    v.show()
    QApplication.processEvents()
    v.set_native_size(src_w, src_h)
    return v


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
        w._set_preview_clip(clips[select if select is not None else 0])
    w._update_controls_enabled()
    return w


def _preview(w, clip: Clip) -> None:
    """Move the preview to ``clip`` without touching the selection."""
    w._set_preview_clip(clip)


def _render_foreground(view: VideoView) -> QImage:
    """Paint only the view's foreground layer onto an opaque white image.

    White is not a colour the indicator draws, so every altered pixel is
    attributable to the confirmed-crop visualization rather than to the
    video item, the scene background or the widget stylesheet.
    """
    size = view.viewport().size()
    img = QImage(size.width(), size.height(), QImage.Format_ARGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setTransform(view.viewportTransform())
    view.drawForeground(p, view.sceneRect())
    p.end()
    return img


def _lum(img: QImage, x: float, y: float) -> int:
    return QColor(img.pixel(int(x), int(y))).red()


class _Geom:
    def assert_inside(self, inner: QRectF, outer: QRectF, msg: str = "") -> None:
        eps = 0.001
        self.assertGreaterEqual(inner.left(), outer.left() - eps, msg)
        self.assertGreaterEqual(inner.top(), outer.top() - eps, msg)
        self.assertLessEqual(inner.right(), outer.right() + eps, msg)
        self.assertLessEqual(inner.bottom(), outer.bottom() + eps, msg)


# --- Group A: indicator presentation state -----------------------------


class IndicatorStateTests(unittest.TestCase):
    def test_a1_new_view_has_no_confirmed_crop(self) -> None:
        """The default - no arguments, no wiring - must be 'no indicator'."""
        v = _view()
        self.assertIsNone(v.crop_indicator_rect())
        self.assertEqual(v.crop_indicator_label(), "")
        self.assertIsNone(v.crop_indicator_geometry())

    def test_a2_effective_crop_is_stored_as_given(self) -> None:
        v = _view()
        v.set_crop_indicator((0.2, 0.1, 0.5, 0.6), "9:16")
        self.assertEqual(v.crop_indicator_rect(), (0.2, 0.1, 0.5, 0.6))
        self.assertEqual(v.crop_indicator_label(), "9:16")

    def test_a3_none_clears_the_indicator(self) -> None:
        v = _view()
        v.set_crop_indicator((0.2, 0.1, 0.5, 0.6), "9:16")
        v.set_crop_indicator(None)
        self.assertIsNone(v.crop_indicator_rect())
        self.assertEqual(v.crop_indicator_label(), "")

    def test_a4_full_frame_tuple_is_inactive(self) -> None:
        """(0,0,1,1) is 'no crop' even though the UI canonicalizes it away."""
        v = _view()
        v.set_crop_indicator((0.0, 0.0, 1.0, 1.0), "16:9")
        self.assertIsNone(v.crop_indicator_rect())
        self.assertEqual(v.crop_indicator_label(), "")
        self.assertIsNone(v.crop_indicator_geometry())

    def test_a5_reassigning_identical_state_does_not_repaint(self) -> None:
        v = _view()
        v.set_crop_indicator((0.2, 0.1, 0.5, 0.6), "9:16")
        with unittest.mock.patch.object(v.viewport(), "update") as upd:
            v.set_crop_indicator((0.2, 0.1, 0.5, 0.6), "9:16")
            self.assertEqual(upd.call_count, 0)
            v.set_crop_indicator((0.2, 0.1, 0.5, 0.7), "9:16")
            self.assertEqual(upd.call_count, 1)

    def test_a5b_default_label_is_empty(self) -> None:
        v = _view()
        v.set_crop_indicator((0.25, 0.0, 0.5, 1.0))
        self.assertEqual(v.crop_indicator_label(), "")


# --- Group B: normalized crop -> viewport geometry ---------------------


class GeometryTests(unittest.TestCase, _Geom):
    def test_b1_source_rect_maps_to_the_fitted_video_area(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.25, 0.0, 0.5, 1.0), "9:16")
        src, _ = v.crop_indicator_geometry()
        self.assertAlmostEqual(src.width() / src.height(), 1920 / 1080, places=3)
        self.assert_inside(src, QRectF(v.viewport().rect()).adjusted(-1, -1, 1, 1))

    def test_b2_centered_square_crop_maps_centered(self) -> None:
        v = _view(1920, 1080, 640, 360)
        w = 1080 / 1920
        v.set_crop_indicator(((1 - w) / 2, 0.0, w, 1.0), "1:1")
        src, crop = v.crop_indicator_geometry()
        self.assertAlmostEqual(crop.width() / src.width(), w, places=6)
        self.assertAlmostEqual(crop.height(), src.height(), places=6)
        self.assertAlmostEqual(crop.center().x(), src.center().x(), places=6)
        self.assertAlmostEqual(crop.width() / crop.height(), 1.0, places=3)

    def test_b3_nine_sixteen_crop_maps_at_the_right_aspect(self) -> None:
        v = _view(1920, 1080, 640, 360)
        w = (9 / 16) / (1920 / 1080)
        v.set_crop_indicator(((1 - w) / 2, 0.0, w, 1.0), "9:16")
        _, crop = v.crop_indicator_geometry()
        self.assertAlmostEqual(crop.width() / crop.height(), 9 / 16, places=3)

    def test_b4_off_center_custom_crop_maps_proportionally(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.1, 0.2, 0.3, 0.4), "Crop")
        src, crop = v.crop_indicator_geometry()
        self.assertAlmostEqual(
            (crop.left() - src.left()) / src.width(), 0.1, places=6)
        self.assertAlmostEqual(
            (crop.top() - src.top()) / src.height(), 0.2, places=6)
        self.assertAlmostEqual(crop.width() / src.width(), 0.3, places=6)
        self.assertAlmostEqual(crop.height() / src.height(), 0.4, places=6)

    def test_b5_geometry_follows_the_view_transform_after_resize(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.1, 0.2, 0.3, 0.4), "Crop")
        _, small = v.crop_indicator_geometry()
        v.resize(1280, 720)
        QApplication.processEvents()
        src, big = v.crop_indicator_geometry()
        self.assertGreater(big.width(), small.width() * 1.5)
        self.assertAlmostEqual(big.width() / src.width(), 0.3, places=6)

    def test_b6_crop_geometry_never_escapes_the_source_rect(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.9, 0.9, 0.5, 0.5), "Crop")
        src, crop = v.crop_indicator_geometry()
        self.assert_inside(crop, src, "crop must stay inside the source frame")


# --- Group C: the outside matte ----------------------------------------


class MatteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.v = _view(1920, 1080, 640, 360)
        self.v.set_crop_indicator((0.3, 0.25, 0.4, 0.5), "Crop")
        self.src, self.crop = self.v.crop_indicator_geometry()
        self.img = _render_foreground(self.v)

    def _at(self, x: float, y: float) -> int:
        return _lum(self.img, x, y)

    def test_c1_crop_interior_keeps_full_brightness(self) -> None:
        c = self.crop.center()
        self.assertEqual(self._at(c.x(), c.y()), WHITE)

    def test_c2_above_the_crop_is_matted(self) -> None:
        self.assertEqual(
            self._at(self.crop.center().x(),
                     (self.src.top() + self.crop.top()) / 2), MATTE)

    def test_c3_below_the_crop_is_matted(self) -> None:
        self.assertEqual(
            self._at(self.crop.center().x(),
                     (self.crop.bottom() + self.src.bottom()) / 2), MATTE)

    def test_c4_left_of_the_crop_is_matted(self) -> None:
        self.assertEqual(
            self._at((self.src.left() + self.crop.left()) / 2,
                     self.crop.center().y()), MATTE)

    def test_c5_right_of_the_crop_is_matted(self) -> None:
        self.assertEqual(
            self._at((self.crop.right() + self.src.right()) / 2,
                     self.crop.center().y()), MATTE)

    def test_c6_matte_regions_tile_the_source_minus_the_crop(self) -> None:
        regions = app_mod._crop_matte_regions(self.src, self.crop)
        area = sum(r.width() * r.height() for r in regions)
        expected = (self.src.width() * self.src.height()
                    - self.crop.width() * self.crop.height())
        self.assertAlmostEqual(area, expected, places=6)
        for r in regions:
            self.assertFalse(r.intersects(self.crop))
            self.assertTrue(self.src.contains(r) or self.src == r)

    def test_c7_matte_stays_off_the_letterbox_outside_the_source(self) -> None:
        """A tall viewport letterboxes 16:9 source; bars stay untouched."""
        v = _view(1920, 1080, 400, 600)
        v.set_crop_indicator((0.3, 0.25, 0.4, 0.5), "Crop")
        src, _ = v.crop_indicator_geometry()
        img = _render_foreground(v)
        self.assertGreater(src.top(), 4.0, "expected a letterboxed layout")
        self.assertEqual(_lum(img, src.center().x(), src.top() / 2), WHITE)


# --- Group D: the crop boundary ----------------------------------------


class BorderTests(unittest.TestCase):
    def _border_lum(self, v: VideoView) -> int:
        _, crop = v.crop_indicator_geometry()
        img = _render_foreground(v)
        return _lum(img, crop.center().x(), crop.top())

    def test_d1_an_effective_crop_draws_a_boundary(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.3, 0.25, 0.4, 0.5), "Crop")
        self.assertNotIn(self._border_lum(v), (WHITE, MATTE))

    def test_d2_no_crop_draws_nothing_at_all(self) -> None:
        v = _view(1920, 1080, 640, 360)
        img = _render_foreground(v)
        for x, y in ((5, 5), (320, 180), (600, 340)):
            self.assertEqual(_lum(img, x, y), WHITE)

    def test_d3_boundary_pen_is_cosmetic_and_thin(self) -> None:
        self.assertTrue(app_mod._CROP_BORDER_PEN.isCosmetic())
        self.assertLessEqual(app_mod._CROP_BORDER_PEN.widthF(), 2.0)

    def test_d4_zooming_does_not_inflate_the_boundary(self) -> None:
        def thickness(px_w: int, px_h: int) -> int:
            v = _view(1920, 1080, px_w, px_h)
            v.set_crop_indicator((0.3, 0.25, 0.4, 0.5), "Crop")
            _, crop = v.crop_indicator_geometry()
            img = _render_foreground(v)
            x = int(crop.center().x())
            return sum(
                1 for y in range(int(crop.top()) - 4, int(crop.top()) + 5)
                if _lum(img, x, y) not in (WHITE, MATTE)
            )

        self.assertEqual(thickness(640, 360), thickness(1600, 900))


# --- Group E: the compact pill label -----------------------------------


class PillTextTests(unittest.TestCase):
    def test_e1_nine_sixteen_preset_reads_nine_sixteen(self) -> None:
        self.assertEqual(crop_badge_label(TIKTOK), "9:16")

    def test_e2_square_preset_reads_one_one(self) -> None:
        self.assertEqual(crop_badge_label(SQUARE), "1:1")

    def test_e3_free_custom_crop_reads_crop(self) -> None:
        """Not 'Active' - the toolbar's word - and not an invented ratio."""
        self.assertEqual(crop_badge_label(FREE), "Crop")

    def test_e4_label_never_carries_pixel_dimensions(self) -> None:
        for preset in (TIKTOK, SQUARE, LANDSCAPE, FREE, ""):
            label = crop_badge_label(preset)
            self.assertNotRegex(label, r"\d{3,}")
            self.assertNotIn("x", label.lower())
            self.assertNotIn("×", label)

    def test_e5_label_drops_the_social_platform_suffix(self) -> None:
        for preset in (TIKTOK, SQUARE, LANDSCAPE):
            label = crop_badge_label(preset)
            self.assertNotIn("(", label)
            self.assertLessEqual(len(label), 5)

    def test_e6_unknown_preset_falls_back_to_crop(self) -> None:
        for preset in ("", "bogus", "9:16 but not really"):
            self.assertEqual(crop_badge_label(preset), "Crop")


# --- Group F: pill visibility ------------------------------------------


class PillVisibilityTests(unittest.TestCase):
    def test_f1_a_roomy_crop_shows_the_pill(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.2, 0.1, 0.6, 0.8), "9:16")
        _, crop = v.crop_indicator_geometry()
        self.assertIsNotNone(v.crop_pill_rect(crop))

    def test_f2_a_tiny_crop_suppresses_the_pill_but_keeps_matte(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.45, 0.45, 0.03, 0.03), "9:16")
        src, crop = v.crop_indicator_geometry()
        self.assertIsNone(v.crop_pill_rect(crop))
        img = _render_foreground(v)
        self.assertEqual(
            _lum(img, crop.center().x(), (src.top() + crop.top()) / 2), MATTE)
        self.assertNotIn(
            _lum(img, crop.center().x(), crop.top()), (WHITE, MATTE))

    def test_f3_a_visible_pill_stays_inside_the_crop(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.2, 0.1, 0.6, 0.8), "9:16")
        _, crop = v.crop_indicator_geometry()
        pill = v.crop_pill_rect(crop)
        eps = 0.001
        self.assertGreaterEqual(pill.left(), crop.left() - eps)
        self.assertGreaterEqual(pill.top(), crop.top() - eps)
        self.assertLessEqual(pill.right(), crop.right() + eps)
        self.assertLessEqual(pill.bottom(), crop.bottom() + eps)

    def test_f4_no_label_means_no_pill(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.2, 0.1, 0.6, 0.8))
        _, crop = v.crop_indicator_geometry()
        self.assertIsNone(v.crop_pill_rect(crop))


# --- Group G: edit-mode gating -----------------------------------------


class EditGatingTests(unittest.TestCase):
    def test_g1_confirmed_crop_on_the_previewed_clip_is_shown(self) -> None:
        c = _clip(_asset(), crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        w = _win([c])
        w._sync_crop_indicator()
        self.assertEqual(w.video_view.crop_indicator_rect(), (0.25, 0.0, 0.5, 1.0))
        self.assertEqual(w.video_view.crop_indicator_label(), "9:16")
        w.close()

    def test_g2_opening_the_editor_hides_the_confirmed_indicator(self) -> None:
        c = _clip(_asset(), crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        w = _win([c])
        w.show()
        w._sync_crop_indicator()
        w.crop_btn.setChecked(True)
        self.assertIsNone(w.video_view.crop_indicator_rect())
        self.assertTrue(w.crop_overlay.isVisible())
        w.close()

    def test_g3_escape_restores_the_committed_visualization(self) -> None:
        c = _clip(_asset(), crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_overlay.set_normalized_rect(QRectF(0.1, 0.1, 0.5, 0.5))
        w._finish_crop_edit(commit=False)
        self.assertEqual(w.video_view.crop_indicator_rect(), (0.25, 0.0, 0.5, 1.0))
        self.assertEqual(w.video_view.crop_indicator_label(), "9:16")
        w.close()

    def test_g4_confirm_shows_the_newly_committed_crop(self) -> None:
        c = _clip(_asset(), crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(SQUARE)
        w._on_crop_confirm_clicked()
        self.assertEqual(w.video_view.crop_indicator_rect(), c.crop_rect)
        self.assertEqual(w.video_view.crop_indicator_label(), "1:1")
        w.close()

    def test_g5_reset_then_confirm_clears_the_visualization(self) -> None:
        c = _clip(_asset(), crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        w = _win([c])
        w.crop_btn.setChecked(True)
        w._on_crop_reset()
        w._on_crop_confirm_clicked()
        self.assertIsNone(c.crop_rect)
        self.assertIsNone(w.video_view.crop_indicator_rect())
        self.assertEqual(w.crop_btn.text(), "Crop")
        w.close()


# --- Group H: the indicator belongs to the previewed clip --------------


class PreviewOwnershipTests(unittest.TestCase):
    def test_h1_preview_and_selection_agree(self) -> None:
        a = _clip(_asset("a.mp4"), crop_rect=(0.25, 0.0, 0.5, 1.0),
                  crop_preset=TIKTOK)
        w = _win([a])
        self.assertEqual(w.video_view.crop_indicator_rect(), a.crop_rect)
        w.close()

    def test_h2_preview_moves_to_b_while_a_stays_selected(self) -> None:
        """The critical detector: selection is not the indicator's owner."""
        a = _clip(_asset("a.mp4"), crop_rect=(0.25, 0.0, 0.5, 1.0),
                  crop_preset=TIKTOK)
        b = _clip(_asset("b.mp4"), start=10.0,
                  crop_rect=(0.2, 0.2, 0.6, 0.6), crop_preset=SQUARE)
        w = _win([a, b], select=0)
        _preview(w, b)
        self.assertEqual(w.timeline.selected_id(), a.id)
        self.assertEqual(w.video_view.crop_indicator_rect(), b.crop_rect)
        self.assertEqual(w.video_view.crop_indicator_label(), "1:1")
        w.close()

    def test_h3_previewing_an_uncropped_clip_clears_the_indicator(self) -> None:
        a = _clip(_asset("a.mp4"), crop_rect=(0.25, 0.0, 0.5, 1.0),
                  crop_preset=TIKTOK)
        b = _clip(_asset("b.mp4"), start=10.0)
        w = _win([a, b], select=0)
        _preview(w, b)
        self.assertIsNone(w.video_view.crop_indicator_rect())
        self.assertEqual(w.video_view.crop_indicator_label(), "")
        w.close()

    def test_h4_moving_on_to_a_third_crop_updates_the_indicator(self) -> None:
        a = _clip(_asset("a.mp4"), crop_rect=(0.25, 0.0, 0.5, 1.0),
                  crop_preset=TIKTOK)
        b = _clip(_asset("b.mp4"), start=10.0)
        c = _clip(_asset("c.mp4"), start=20.0,
                  crop_rect=(0.2, 0.2, 0.6, 0.6), crop_preset=SQUARE)
        w = _win([a, b, c], select=0)
        _preview(w, b)
        _preview(w, c)
        self.assertEqual(w.video_view.crop_indicator_rect(), c.crop_rect)
        self.assertEqual(w.video_view.crop_indicator_label(), "1:1")
        w.close()

    def test_h5_no_resolvable_preview_clip_hides_the_indicator(self) -> None:
        """A playhead in a gap shows no clip, so it must show no crop."""
        a = _clip(_asset("a.mp4"), start=0.0, length=10.0,
                  crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        far = _clip(_asset("b.mp4"), start=30.0, length=10.0)
        w = _win([a, far], select=0)
        w._sync_crop_indicator()
        self.assertIsNotNone(w.video_view.crop_indicator_rect())
        w.timeline.set_playhead(20.0, emit=False)
        w._drive_main_player_from_playhead()
        self.assertIsNone(w.video_view.crop_indicator_rect())
        w.close()


# --- Group I: toolbar (selection) vs preview (display) -----------------


class ToolbarSeparationTests(unittest.TestCase):
    def test_i1_toolbar_follows_selection_while_pill_follows_preview(self) -> None:
        a = _clip(_asset("a.mp4"), crop_rect=(0.25, 0.0, 0.5, 1.0),
                  crop_preset=TIKTOK)
        b = _clip(_asset("b.mp4"), start=10.0,
                  crop_rect=(0.2, 0.2, 0.6, 0.6), crop_preset=SQUARE)
        w = _win([a, b], select=0)
        _preview(w, b)
        w._update_crop_button_status()
        self.assertEqual(w.crop_btn.text(), "Crop (9:16)")
        self.assertEqual(w.video_view.crop_indicator_label(), "1:1")
        w.close()

    def test_i2_selected_crop_survives_an_uncropped_preview(self) -> None:
        a = _clip(_asset("a.mp4"), crop_rect=(0.25, 0.0, 0.5, 1.0),
                  crop_preset=TIKTOK)
        b = _clip(_asset("b.mp4"), start=10.0)
        w = _win([a, b], select=0)
        _preview(w, b)
        w._update_crop_button_status()
        self.assertEqual(w.crop_btn.text(), "Crop (9:16)")
        self.assertIsNone(w.video_view.crop_indicator_rect())
        w.close()


# --- Group J: undo / redo ----------------------------------------------


class UndoRedoTests(unittest.TestCase):
    def test_j1_confirming_a_crop_shows_the_indicator(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_crop_confirm_clicked()
        self.assertIsNotNone(w.video_view.crop_indicator_rect())
        self.assertEqual(w.video_view.crop_indicator_label(), "9:16")
        w.close()

    def test_j2_undo_restores_the_uncropped_visualization(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_crop_confirm_clicked()
        w._undo()
        self.assertIsNone(w.video_view.crop_indicator_rect())
        w.close()

    def test_j3_redo_brings_the_indicator_back(self) -> None:
        c = _clip(_asset())
        w = _win([c])
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_crop_confirm_clicked()
        w._undo()
        w._redo()
        self.assertEqual(w.video_view.crop_indicator_label(), "9:16")
        self.assertIsNotNone(w.video_view.crop_indicator_rect())
        w.close()

    def test_j4_undo_never_paints_a_non_previewed_clip_crop(self) -> None:
        a = _clip(_asset("a.mp4"))
        b = _clip(_asset("b.mp4"), start=10.0)
        w = _win([a, b], select=0)
        w.crop_btn.setChecked(True)
        w.crop_aspect_combo.setCurrentText(TIKTOK)
        w._on_crop_confirm_clicked()
        w._undo()
        previewed = w._current_preview_clip()
        expected = None if previewed is None else previewed.crop_rect
        self.assertEqual(w.video_view.crop_indicator_rect(), expected)
        w.close()


# --- Group K: image / video parity -------------------------------------


class ImageVideoParityTests(unittest.TestCase):
    def _image_win(self):
        img = _asset("still.png", 1600, 1200, kind="image")
        c = _clip(img, crop_rect=(0.2, 0.2, 0.6, 0.6), crop_preset=SQUARE)
        w = _win([c])
        w._image_pixmaps[img.id] = QPixmap(1600, 1200)
        w._set_preview_clip(c)
        return w, c

    def test_k1_video_clip_draws_the_indicator(self) -> None:
        c = _clip(_asset(), crop_rect=(0.2, 0.2, 0.6, 0.6), crop_preset=SQUARE)
        w = _win([c])
        self.assertIsNotNone(w.video_view.crop_indicator_geometry())
        w.close()

    def test_k2_image_clip_draws_the_indicator(self) -> None:
        w, _ = self._image_win()
        self.assertEqual(w.video_view.crop_indicator_rect(), (0.2, 0.2, 0.6, 0.6))
        self.assertIsNotNone(w.video_view.crop_indicator_geometry())
        w.close()

    def test_k3_same_normalized_crop_gives_the_same_relative_framing(self) -> None:
        def framing(kind: str, w_px: int, h_px: int):
            v = _view(w_px, h_px, 640, 360)
            v.set_crop_indicator((0.2, 0.2, 0.6, 0.6), "1:1")
            src, crop = v.crop_indicator_geometry()
            return (crop.width() / src.width(), crop.height() / src.height())

        vid = framing("video", 1920, 1080)
        img = framing("image", 1600, 1200)
        self.assertAlmostEqual(vid[0], img[0], places=9)
        self.assertAlmostEqual(vid[1], img[1], places=9)

    def test_k4_switching_video_image_video_leaves_no_stale_state(self) -> None:
        vid = _clip(_asset("a.mp4"), crop_rect=(0.25, 0.0, 0.5, 1.0),
                    crop_preset=TIKTOK)
        img_asset = _asset("still.png", 1600, 1200, kind="image")
        still = _clip(img_asset, start=10.0)
        w = _win([vid, still], select=0)
        w._image_pixmaps[img_asset.id] = QPixmap(1600, 1200)
        self.assertEqual(w.video_view.crop_indicator_label(), "9:16")
        _preview(w, still)
        self.assertIsNone(w.video_view.crop_indicator_rect())
        _preview(w, vid)
        self.assertEqual(w.video_view.crop_indicator_rect(), vid.crop_rect)
        w.close()


# --- Group L: playback transitions -------------------------------------


class PlaybackTransitionTests(unittest.TestCase):
    def _abc(self):
        a = _clip(_asset("a.mp4"), start=0.0, length=10.0,
                  crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        b = _clip(_asset("b.mp4"), start=10.0, length=10.0)
        c = _clip(_asset("c.mp4"), start=20.0, length=10.0,
                  crop_rect=(0.2, 0.2, 0.6, 0.6), crop_preset=SQUARE)
        return _win([a, b, c], select=0), a, b, c

    def _seek(self, w, t: float) -> None:
        w.timeline.set_playhead(t, emit=False)
        w._drive_main_player_from_playhead()

    def test_l1_a_to_b_clears_the_indicator(self) -> None:
        w, a, b, c = self._abc()
        self._seek(w, 2.0)
        self.assertEqual(w.video_view.crop_indicator_label(), "9:16")
        self._seek(w, 12.0)
        self.assertIsNone(w.video_view.crop_indicator_rect())
        w.close()

    def test_l2_b_to_c_shows_the_new_indicator(self) -> None:
        w, a, b, c = self._abc()
        self._seek(w, 12.0)
        self._seek(w, 22.0)
        self.assertEqual(w.video_view.crop_indicator_rect(), c.crop_rect)
        self.assertEqual(w.video_view.crop_indicator_label(), "1:1")
        w.close()

    def test_l3_repeated_transitions_retain_no_stale_state(self) -> None:
        w, a, b, c = self._abc()
        for _ in range(3):
            self._seek(w, 2.0)
            self.assertEqual(w.video_view.crop_indicator_rect(), a.crop_rect)
            self._seek(w, 12.0)
            self.assertIsNone(w.video_view.crop_indicator_rect())
            self._seek(w, 22.0)
            self.assertEqual(w.video_view.crop_indicator_rect(), c.crop_rect)
        w.close()

    def test_l4_syncing_the_indicator_touches_no_player_state(self) -> None:
        w, a, b, c = self._abc()
        player = w.player
        with unittest.mock.patch.object(player, "play") as play, \
             unittest.mock.patch.object(player, "pause") as pause, \
             unittest.mock.patch.object(player, "stop") as stop, \
             unittest.mock.patch.object(player, "setPosition") as seek, \
             unittest.mock.patch.object(player, "setSource") as src:
            w._sync_crop_indicator()
            w.video_view.set_crop_indicator(None)
            w.video_view.set_crop_indicator((0.2, 0.2, 0.6, 0.6), "1:1")
            for mock in (play, pause, stop, seek, src):
                self.assertEqual(mock.call_count, 0)
        w.close()


# --- Group M: the preview geometry is untouched ------------------------


class SceneGeometryTests(unittest.TestCase):
    def test_m1_indicator_activation_changes_no_scene_geometry(self) -> None:
        v = _view(1920, 1080, 640, 360)
        before = (
            QRectF(v.sceneRect()),
            v.video_item.pos(), QRectF(v.video_item.boundingRect()),
            v.pixmap_item.pos(),
        )
        v.set_crop_indicator((0.25, 0.0, 0.5, 1.0), "9:16")
        after = (
            QRectF(v.sceneRect()),
            v.video_item.pos(), QRectF(v.video_item.boundingRect()),
            v.pixmap_item.pos(),
        )
        self.assertEqual(before, after)

    def test_m2_painting_the_indicator_changes_no_scene_geometry(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.25, 0.0, 0.5, 1.0), "9:16")
        before = QRectF(v.sceneRect())
        _render_foreground(v)
        self.assertEqual(before, QRectF(v.sceneRect()))

    def test_m3_mainwindow_sync_changes_no_scene_geometry(self) -> None:
        c = _clip(_asset(), crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        w = _win([c])
        before = QRectF(w.video_view.sceneRect())
        w._sync_crop_indicator()
        self.assertEqual(before, QRectF(w.video_view.sceneRect()))
        self.assertEqual(
            (w.video_view.sceneRect().width(), w.video_view.sceneRect().height()),
            (1920.0, 1080.0),
        )
        w.close()


# --- Group N: painter hygiene ------------------------------------------


class PainterStateTests(unittest.TestCase):
    def test_n1_draw_foreground_restores_painter_state(self) -> None:
        v = _view(1920, 1080, 640, 360)
        v.set_crop_indicator((0.25, 0.0, 0.5, 1.0), "9:16")
        img = QImage(640, 360, QImage.Format_ARGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setTransform(v.viewportTransform())
        pen = QPen(QColor("#ff00ff"), 7)
        p.setPen(pen)
        p.setBrush(QColor("#00ff00"))
        p.setOpacity(0.5)
        font = QFont(p.font())
        font.setPixelSize(31)
        p.setFont(font)
        before = (p.transform(), p.pen().color().name(), p.pen().widthF(),
                  p.brush().color().name(), p.opacity(), p.font().pixelSize())
        v.drawForeground(p, v.sceneRect())
        after = (p.transform(), p.pen().color().name(), p.pen().widthF(),
                 p.brush().color().name(), p.opacity(), p.font().pixelSize())
        p.end()
        self.assertEqual(before, after)


# --- Group O: paint stays a hot path -----------------------------------


class HotPathTests(unittest.TestCase):
    def _paint_source(self) -> str:
        return "\n".join(
            inspect.getsource(fn) for fn in (
                VideoView.drawForeground,
                VideoView.crop_indicator_geometry,
                VideoView.crop_pill_rect,
                app_mod._crop_matte_regions,
            )
        )

    def test_o1_paint_never_scans_the_clip_model(self) -> None:
        src = self._paint_source()
        for token in ("_clips", "MainWindow", "Clip(", "clip_at_timeline"):
            self.assertNotIn(token, src)

    def test_o2_paint_never_parses_the_preset_registry(self) -> None:
        src = self._paint_source()
        for token in ("CROP_ASPECT_PRESETS", "compact_preset_label",
                      "crop_badge_label", "crop_preset"):
            self.assertNotIn(token, src)

    def test_o3_paint_does_no_io(self) -> None:
        src = self._paint_source()
        for token in ("QSettings", "open(", "subprocess", "ffmpeg", "Path("):
            self.assertNotIn(token, src)

    def test_o4_repeated_paints_do_not_mutate_state(self) -> None:
        c = _clip(_asset(), crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=TIKTOK)
        w = _win([c])
        before = (c.crop_rect, c.crop_preset, w._crop_edit_clip_id,
                  w._preview_clip_id, w.video_view.crop_indicator_rect())
        for _ in range(5):
            _render_foreground(w.video_view)
        after = (c.crop_rect, c.crop_preset, w._crop_edit_clip_id,
                 w._preview_clip_id, w.video_view.crop_indicator_rect())
        self.assertEqual(before, after)
        w.close()


# --- Group P: full-frame and degenerate safety -------------------------


class SafetyTests(unittest.TestCase):
    def test_p1_none_paints_nothing(self) -> None:
        v = _view()
        v.set_crop_indicator(None)
        self.assertEqual(_lum(_render_foreground(v), 320, 180), WHITE)

    def test_p2_full_frame_paints_nothing(self) -> None:
        v = _view()
        v.set_crop_indicator((0.0, 0.0, 1.0, 1.0), "16:9")
        self.assertEqual(_lum(_render_foreground(v), 320, 180), WHITE)

    def test_p3_degenerate_rects_are_ignored_without_crashing(self) -> None:
        bad = [
            (0.2, 0.2, 0.0, 0.5),
            (0.2, 0.2, 0.5, 0.0),
            (0.2, 0.2, -0.5, 0.5),
            (0.2, 0.2, 0.5, -0.5),
            (float("nan"), 0.2, 0.5, 0.5),
            (0.2, 0.2, float("inf"), 0.5),
            (2.0, 2.0, 0.5, 0.5),
        ]
        for rect in bad:
            with self.subTest(rect=rect):
                v = _view()
                v.set_crop_indicator(rect, "9:16")
                self.assertIsNone(v.crop_indicator_geometry())
                self.assertEqual(_lum(_render_foreground(v), 320, 180), WHITE)

    def test_p4_no_source_size_hides_the_indicator(self) -> None:
        v = _view()
        v.set_native_size(0, 0)
        v.set_crop_indicator((0.25, 0.0, 0.5, 1.0), "9:16")
        self.assertIsNone(v.crop_indicator_geometry())


# --- Group Q: structural exclusions ------------------------------------


class StructuralTests(unittest.TestCase):
    def test_q1_video_view_stores_no_model_reference(self) -> None:
        v = _view()
        v.set_crop_indicator((0.25, 0.0, 0.5, 1.0), "9:16")
        for value in vars(v).values():
            self.assertNotIsInstance(value, Clip)
            self.assertNotIsInstance(value, MainWindow)

    def test_q2_no_canvas_fit_symbols_were_introduced(self) -> None:
        src = inspect.getsource(app_mod)
        for token in ("CROP_FIT_MODES", "crop_fit_mode", "canvas_fit",
                      "canvas_aspect", "CROP ACTIVE"):
            self.assertNotIn(token, src)

    def test_q3_indicator_code_never_touches_the_scene_rect(self) -> None:
        src = "\n".join(
            inspect.getsource(fn) for fn in (
                VideoView.set_crop_indicator,
                VideoView.drawForeground,
                VideoView.crop_indicator_geometry,
                MainWindow._sync_crop_indicator,
            )
        )
        self.assertNotIn("setSceneRect", src)


if __name__ == "__main__":
    unittest.main()
