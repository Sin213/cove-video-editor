"""Rounded frameless window chrome (Tab 2H).

Cove's main window is frameless with its own titlebar. This slice gives it
rounded *outer* corners without touching any of that:

    Windows   -> ask the DWM for a round corner preference (attr 33) and a
                 border colour (attr 34). Native only; no Qt mask.
    other     -> clip the top-level widget to a rounded-rect mask, cleared
                 while maximized / fullscreen and recomputed on resize.

Native calls are exercised through a seam (`_dwm_set_window_attribute`) so
the Windows path is provable on Linux with a recorder that matches the real
ctypes call shape. Mask *decisions* are tested against an unbound-method
host with controlled geometry/state (no window-manager timing); mask
*behaviour* is tested against a real offscreen MainWindow.

MainWindow construction suppresses the real NVENC/AMF probe: it spawns
ffmpeg children that outlive the window and leak into
``ffmpeg_utils._active_probe_procs``, and nothing here depends on encoder
capabilities.
"""
from __future__ import annotations

import ctypes
import os
import sys
import unittest
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import theme  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _window() -> MainWindow:
    with unittest.mock.patch.object(
        MainWindow, "_start_encoder_probe", lambda self: None,
    ):
        return MainWindow()


class _DwmRecorder:
    """Stands in for ``DwmSetWindowAttribute``.

    Mirrors the real call shape - ``(hwnd, attr, pvalue, cbsize)`` - and
    reads the value back out of the pointer, so a passing test constrains
    the attribute id, the payload *and* the declared size. Returns S_OK
    (0) like the real API does on success.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int, int]] = []

    def __call__(self, hwnd, attr, pvalue, cbsize):  # noqa: ANN001
        self.calls.append(
            (int(hwnd.value or 0), int(attr), pvalue.contents.value, int(cbsize)),
        )
        return 0

    def value_for(self, attr: int) -> int:
        return next(c[2] for c in self.calls if c[1] == attr)

    def size_for(self, attr: int) -> int:
        return next(c[3] for c in self.calls if c[1] == attr)

    @property
    def attrs(self) -> list[int]:
        return [c[1] for c in self.calls]


class _MaskHost:
    """Minimal ``MainWindow`` stand-in for ``_update_corner_mask``.

    Records the mask operations instead of asking a compositor, so the
    maximized / fullscreen / degenerate-geometry decisions are testable
    without window-manager timing.
    """

    def __init__(self, w: int = 1200, h: int = 800, *,
                 maximized: bool = False, fullscreen: bool = False) -> None:
        self._rect = QRect(0, 0, w, h)
        self._max = maximized
        self._full = fullscreen
        self.masks: list = []
        self.cleared = 0

    def rect(self) -> QRect:
        return self._rect

    def isMaximized(self) -> bool:  # noqa: N802 - Qt naming
        return self._max

    def isFullScreen(self) -> bool:  # noqa: N802 - Qt naming
        return self._full

    def setMask(self, region) -> None:  # noqa: N802, ANN001 - Qt naming
        self.masks.append(region)

    def clearMask(self) -> None:  # noqa: N802 - Qt naming
        self.cleared += 1


def _mask(host: _MaskHost):
    MainWindow._update_corner_mask(host)
    return host


# ---- Group A: platform branching -------------------------------------------


class PlatformBranchTests(unittest.TestCase):
    def test_a1_non_windows_makes_no_native_call(self) -> None:
        """The DWM path is Windows-only: on Linux the seam is never even
        reached, so no ctypes call can happen."""
        seam = unittest.mock.Mock(side_effect=AssertionError("seam reached"))
        host = unittest.mock.Mock()
        host._dwm_set_window_attribute = seam
        with unittest.mock.patch.object(sys, "platform", "linux"):
            MainWindow._apply_windows_chrome(host)
        seam.assert_not_called()

    def test_a2_windows_does_not_use_the_qt_mask(self) -> None:
        """Windows rounds natively; applying a Qt mask as well would fight
        the DWM, so the fallback must no-op there."""
        host = _MaskHost()
        with unittest.mock.patch.object(sys, "platform", "win32"):
            _mask(host)
        self.assertEqual(host.masks, [])
        self.assertEqual(host.cleared, 0)

    def test_a3_native_failure_does_not_escape(self) -> None:
        """A missing dwmapi / unsupported attribute is cosmetic; it must
        never propagate out of the chrome method."""
        host = unittest.mock.Mock()
        host._dwm_set_window_attribute.side_effect = OSError("no dwmapi")
        with unittest.mock.patch.object(sys, "platform", "win32"):
            MainWindow._apply_windows_chrome(host)  # must not raise

    def test_a3_failing_dwm_call_does_not_escape(self) -> None:
        """The seam resolving fine but the call itself blowing up is the
        Windows-10 case; also contained."""
        host = unittest.mock.Mock()
        host._dwm_set_window_attribute.return_value = unittest.mock.Mock(
            side_effect=OSError("unsupported attribute"),
        )
        host.winId.return_value = 4242
        with unittest.mock.patch.object(sys, "platform", "win32"):
            MainWindow._apply_windows_chrome(host)  # must not raise


# ---- Group B: COLORREF ------------------------------------------------------


class ColorrefTests(unittest.TestCase):
    def test_b1_hex_converts_to_bgr_colorref(self) -> None:
        """Windows COLORREF is 0x00BBGGRR, not RGB."""
        self.assertEqual(app_mod._colorref_from_hex("#27353d"), 0x003D3527)

    def test_b1_channel_isolation(self) -> None:
        self.assertEqual(app_mod._colorref_from_hex("#ff0000"), 0x000000FF)
        self.assertEqual(app_mod._colorref_from_hex("#00ff00"), 0x0000FF00)
        self.assertEqual(app_mod._colorref_from_hex("#0000ff"), 0x00FF0000)

    def test_b2_border_colour_comes_from_the_live_theme(self) -> None:
        """The DWM border must track ``theme.BORDER_HI``, not a literal
        copied from an older palette."""
        rec = _DwmRecorder()
        host = unittest.mock.Mock()
        host._dwm_set_window_attribute.return_value = rec
        host.winId.return_value = 4242
        with unittest.mock.patch.object(sys, "platform", "win32"):
            MainWindow._apply_windows_chrome(host)
        self.assertEqual(
            rec.value_for(34), app_mod._colorref_from_hex(theme.BORDER_HI),
        )


# ---- Group C: restored non-Windows mask ------------------------------------


class RestoredMaskTests(unittest.TestCase):
    def test_c1_restored_window_gets_a_rounded_mask(self) -> None:
        w = _window()
        self.addCleanup(w.deleteLater)
        w.resize(1200, 800)
        w.show()
        QApplication.processEvents()
        mask = w.mask()
        self.assertFalse(mask.isEmpty())
        self.assertEqual(mask.boundingRect().size(), w.rect().size())

    def test_c2_corners_excluded_interior_included(self) -> None:
        w = _window()
        self.addCleanup(w.deleteLater)
        w.resize(1200, 800)
        w.show()
        QApplication.processEvents()
        mask = w.mask()
        self.assertTrue(mask.contains(QPoint(600, 400)))
        for corner in (
            QPoint(0, 0),
            QPoint(w.width() - 1, 0),
            QPoint(0, w.height() - 1),
            QPoint(w.width() - 1, w.height() - 1),
        ):
            with self.subTest(corner=(corner.x(), corner.y())):
                self.assertFalse(mask.contains(corner))

    def test_c3_default_radius_is_modest(self) -> None:
        """Default (no override anywhere) - a radius large enough to eat
        titlebar controls would be a regression."""
        self.assertEqual(app_mod.WINDOW_CORNER_RADIUS, 10)

    def test_c4_degenerate_geometry_is_a_no_op(self) -> None:
        for w, h in ((0, 800), (1200, 0), (0, 0), (-5, 700)):
            with self.subTest(size=(w, h)):
                host = _mask(_MaskHost(w, h))
                self.assertEqual(host.masks, [])
                self.assertEqual(host.cleared, 0)


# ---- Group D: resize --------------------------------------------------------


class ResizeMaskTests(unittest.TestCase):
    def test_d1_mask_tracks_the_new_size(self) -> None:
        w = _window()
        self.addCleanup(w.deleteLater)
        w.resize(1200, 800)
        w.show()
        QApplication.processEvents()
        self.assertEqual(w.mask().boundingRect().size(), w.rect().size())
        w.resize(1340, 760)
        QApplication.processEvents()
        self.assertEqual(w.mask().boundingRect().width(), 1340)
        self.assertEqual(w.mask().boundingRect().height(), 760)

    def test_d2_size_grip_still_pinned_after_resize(self) -> None:
        """Pre-existing resizeEvent duty; the mask work is additive."""
        w = _window()
        self.addCleanup(w.deleteLater)
        w.show()
        grip = w._size_grip
        w.resize(1200, 800)
        QApplication.processEvents()
        before = grip.pos()
        w.resize(1340, 760)
        QApplication.processEvents()
        self.assertNotEqual(grip.pos(), before)
        self._assert_pinned(grip, w)

    def _assert_pinned(self, grip, w) -> None:  # noqa: ANN001
        """Bottom-right within a couple of px - production offsets by the
        grip's sizeHint, which is not its fixed size."""
        self.assertAlmostEqual(grip.x() + grip.width(), w.width(), delta=4)
        self.assertAlmostEqual(grip.y() + grip.height(), w.height(), delta=4)


# ---- Group E: maximize / restore -------------------------------------------


class MaximizeMaskTests(unittest.TestCase):
    def test_e1_maximized_clears_the_mask(self) -> None:
        host = _mask(_MaskHost(maximized=True))
        self.assertEqual(host.masks, [])
        self.assertEqual(host.cleared, 1)

    def test_e2_restore_reapplies_the_mask(self) -> None:
        host = _MaskHost(maximized=True)
        _mask(host)
        host._max = False
        _mask(host)
        self.assertEqual(len(host.masks), 1)
        self.assertFalse(host.masks[0].isEmpty())

    def test_e3_real_window_round_trip(self) -> None:
        w = _window()
        self.addCleanup(w.deleteLater)
        w.resize(1200, 800)
        w.show()
        QApplication.processEvents()
        self.assertFalse(w.mask().isEmpty())
        w.showMaximized()
        QApplication.processEvents()
        if not w.isMaximized():
            self.skipTest("offscreen platform did not report a maximized state")
        self.assertTrue(w.mask().isEmpty())
        w.showNormal()
        QApplication.processEvents()
        self.assertFalse(w.mask().isEmpty())


# ---- Group F: fullscreen / restore -----------------------------------------


class FullscreenMaskTests(unittest.TestCase):
    def test_f1_fullscreen_clears_the_mask(self) -> None:
        host = _mask(_MaskHost(fullscreen=True))
        self.assertEqual(host.masks, [])
        self.assertEqual(host.cleared, 1)

    def test_f2_leaving_fullscreen_reapplies(self) -> None:
        host = _MaskHost(fullscreen=True)
        _mask(host)
        host._full = False
        _mask(host)
        self.assertEqual(len(host.masks), 1)
        self.assertFalse(host.masks[0].isEmpty())


# ---- Group G: show / one-time native apply ---------------------------------


class NativeApplyTests(unittest.TestCase):
    def test_g1_sets_corner_preference_and_border(self) -> None:
        rec = _DwmRecorder()
        host = unittest.mock.Mock()
        host._dwm_set_window_attribute.return_value = rec
        host.winId.return_value = 4242
        with unittest.mock.patch.object(sys, "platform", "win32"):
            MainWindow._apply_windows_chrome(host)
        self.assertEqual(rec.attrs, [33, 34])
        self.assertEqual(rec.value_for(33), 2)  # DWMWCP_ROUND
        self.assertEqual(rec.size_for(33), ctypes.sizeof(ctypes.c_int))
        self.assertEqual(rec.size_for(34), ctypes.sizeof(ctypes.c_uint))
        self.assertEqual({c[0] for c in rec.calls}, {4242})

    def test_g1b_no_native_call_without_a_handle(self) -> None:
        """winId() of 0 means no HWND yet - calling the DWM with it is
        meaningless."""
        rec = _DwmRecorder()
        host = unittest.mock.Mock()
        host._dwm_set_window_attribute.return_value = rec
        host.winId.return_value = 0
        with unittest.mock.patch.object(sys, "platform", "win32"):
            MainWindow._apply_windows_chrome(host)
        self.assertEqual(rec.calls, [])

    def test_g2_applied_on_show_not_construction_and_only_once(self) -> None:
        with unittest.mock.patch.object(
            MainWindow, "_apply_windows_chrome", autospec=True,
        ) as native:
            w = _window()
            self.addCleanup(w.deleteLater)
            self.assertEqual(native.call_count, 0)
            w.show()
            QApplication.processEvents()
            self.assertEqual(native.call_count, 1)
            w.resize(1340, 760)
            w.hide()
            w.show()
            QApplication.processEvents()
            self.assertEqual(native.call_count, 1)

    def test_g3_native_failure_leaves_the_window_usable(self) -> None:
        """A blowing-up DWM must not stop the window from showing."""
        w = _window()
        self.addCleanup(w.deleteLater)
        with unittest.mock.patch.object(
            MainWindow, "_dwm_set_window_attribute",
            side_effect=OSError("no dwmapi"),
        ), unittest.mock.patch.object(sys, "platform", "win32"):
            w.show()
            QApplication.processEvents()
        self.assertTrue(w.isVisible())


# ---- Group H: event integration --------------------------------------------


class EventIntegrationTests(unittest.TestCase):
    def test_h1_resize_event_still_moves_the_grip(self) -> None:
        w = _window()
        self.addCleanup(w.deleteLater)
        w.show()
        w.resize(1250, 780)
        QApplication.processEvents()
        grip = w._size_grip
        self.assertAlmostEqual(grip.x() + grip.width(), 1250, delta=4)
        self.assertAlmostEqual(grip.y() + grip.height(), 780, delta=4)

    def test_h2_change_event_still_syncs_the_titlebar(self) -> None:
        w = _window()
        self.addCleanup(w.deleteLater)
        w.show()
        QApplication.processEvents()
        w.showMaximized()
        QApplication.processEvents()
        if not w.isMaximized():
            self.skipTest("offscreen platform did not report a maximized state")
        self.assertEqual(w.titlebar.max_btn._kind, "restore")
        w.showNormal()
        QApplication.processEvents()
        self.assertEqual(w.titlebar.max_btn._kind, "max")

    def test_h3_toggle_maximize_still_round_trips(self) -> None:
        w = _window()
        self.addCleanup(w.deleteLater)
        w.show()
        QApplication.processEvents()
        w._toggle_maximize()
        QApplication.processEvents()
        if not w.isMaximized():
            self.skipTest("offscreen platform did not report a maximized state")
        w._toggle_maximize()
        QApplication.processEvents()
        self.assertFalse(w.isMaximized())


# ---- Group I: QSS outer border ---------------------------------------------


class WindowBorderQssTests(unittest.TestCase):
    def test_i1_outer_window_border_is_thin(self) -> None:
        qss = theme.build_qss()
        self.assertIn(f"border: 1px solid {theme.WINDOW_EDGE};", qss)
        self.assertNotIn(f"border: 4px solid {theme.WINDOW_EDGE};", qss)

    def test_i2_panel_borders_unchanged(self) -> None:
        qss = theme.build_qss()
        self.assertIn("#CovePanel {", qss)
        panel = qss.split("#CovePanel {", 1)[1].split("}", 1)[0]
        self.assertIn(f"border: 1px solid {theme.BORDER};", panel)
        self.assertIn("border-radius: 12px;", panel)


# ---- Group J: structural exclusion ------------------------------------------


class StructuralExclusionTests(unittest.TestCase):
    def test_j1_no_duplicate_media_import_helpers(self) -> None:
        """Tab 2D already ported browse / folder import. Each symbol must
        exist exactly once."""
        import inspect
        src = inspect.getsource(app_mod)
        for name in ("_expand_media_paths", "_on_browse_requested",
                     "_confirm_bulk_import"):
            with self.subTest(symbol=name):
                self.assertEqual(src.count(f"def {name}"), 1)


if __name__ == "__main__":
    unittest.main()
