"""Tab 2C: Zoom to Fit.

A small orientation/recovery command for long timelines: size the zoom so
the whole sequence fits the visible track width and send the viewport back
to the start. It is not a "maximise the clip" command - it only ever zooms
out, so invoking it on an already-fitting project leaves the user's zoom
alone.

The Qt pieces run on the ``offscreen`` platform plugin so widget geometry
(and therefore ``_track_rect()``/``scroll_max_px()``) is real rather than
mocked.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor.clip import AddedAudio, Clip, MediaAsset  # noqa: E402
from cove_video_editor.timeline_widget import (  # noqa: E402
    MAX_PPS, MIN_PPS, SCROLL_TAIL_PAD_S, TimelineWidget,
    fit_pixels_per_second,
)


_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _clip(start: float, length: float, name: str = "a.mp4") -> Clip:
    return Clip(
        asset=MediaAsset(path=Path(name), duration=max(600.0, length),
                         width=1920, height=1080, fps=30.0, has_audio=True),
        timeline_start=start, src_start=0.0, src_end=length,
    )


def _widget(clips: list[Clip], *, pps: float = 40.0,
            size: tuple[int, int] = (800, 300)) -> TimelineWidget:
    w = TimelineWidget()
    w.resize(*size)
    w.set_pixels_per_second(pps)
    w.set_clips(clips)
    return w


# ---- Group G: the fit calculation ------------------------------------------


class FitPixelsPerSecondTests(unittest.TestCase):
    """Pure math, deliberately split out of the widget so the policy can be
    tested without geometry."""

    def test_g1_a_project_wider_than_the_viewport_zooms_out(self) -> None:
        # 100s of content needs 4000px at 40 px/s; the track has 800.
        got = fit_pixels_per_second(100.0, 800, 40.0)

        self.assertIsNotNone(got)
        self.assertLess(got, 40.0)
        self.assertAlmostEqual(got, 800 / (100.0 + SCROLL_TAIL_PAD_S), places=9)

    def test_g1b_the_fit_makes_the_whole_extent_land_inside_the_track(self) -> None:
        got = fit_pixels_per_second(100.0, 800, 40.0)

        self.assertLessEqual((100.0 + SCROLL_TAIL_PAD_S) * got, 800 + 1e-6)

    def test_g2_an_already_fitting_project_does_not_zoom_in(self) -> None:
        """Orientation command, not a magnifier: a 4s clip in an 800px track
        would "fit" at 132 px/s, but the user's 40 px/s is kept."""
        got = fit_pixels_per_second(4.0, 800, 40.0)

        self.assertEqual(got, 40.0)

    def test_g3_an_empty_project_has_nothing_to_fit(self) -> None:
        self.assertIsNone(fit_pixels_per_second(0.0, 800, 40.0))

    def test_g4_a_very_short_duration_does_not_divide_by_zero(self) -> None:
        got = fit_pixels_per_second(1e-9, 800, 40.0)

        self.assertEqual(got, 40.0)

    def test_g4b_a_zero_width_viewport_has_nothing_to_fit(self) -> None:
        self.assertIsNone(fit_pixels_per_second(600.0, 0, 40.0))

    def test_g5_the_minimum_zoom_is_never_breached(self) -> None:
        """A project too long to fit even at MIN_PPS takes the legal minimum
        and stays scrollable rather than producing an illegal zoom."""
        got = fit_pixels_per_second(100_000.0, 800, 40.0)

        self.assertEqual(got, MIN_PPS)

    def test_g6_the_maximum_zoom_is_never_breached(self) -> None:
        got = fit_pixels_per_second(0.001, 800, MAX_PPS * 10)

        self.assertLessEqual(got, MAX_PPS)

    def test_g7_a_wider_viewport_fits_at_a_larger_scale(self) -> None:
        narrow = fit_pixels_per_second(100.0, 1200, 40.0)
        wide = fit_pixels_per_second(100.0, 2400, 40.0)

        self.assertLess(narrow, wide)


# ---- Group H: widget integration -------------------------------------------


class ZoomToFitWidgetTests(unittest.TestCase):
    def test_h1_a_long_sequence_fits_and_the_view_returns_to_the_start(self) -> None:
        w = _widget([_clip(0.0, 100.0)])
        self.addCleanup(w.close)
        w.set_scroll_x(w.scroll_max_px())
        self.assertGreater(w.scroll_max_px(), 0, "fixture must be scrollable")

        w.zoom_to_fit()

        expected = fit_pixels_per_second(
            w._total_length(), w._track_rect().width(), 40.0)
        self.assertAlmostEqual(w.pixels_per_second(), expected, places=6)
        self.assertEqual(w._scroll_x, 0)
        self.assertEqual(w.scroll_max_px(), 0,
                         "the whole extent must fit, leaving nothing to scroll")

    def test_h1b_the_scrollbar_range_is_republished(self) -> None:
        w = _widget([_clip(0.0, 100.0)])
        self.addCleanup(w.close)
        seen: list[int] = []
        w.scrollValueChanged.connect(seen.append)

        w.zoom_to_fit()

        self.assertTrue(seen, "viewport state must be republished")
        self.assertEqual(seen[-1], 0)

    def test_h2_added_audio_beyond_the_video_is_included_in_the_fit(self) -> None:
        """`_total_length()` is the authoritative extent and counts added
        audio, so a music bed running past the last clip must still fit."""
        w = _widget([_clip(0.0, 4.0)])
        self.addCleanup(w.close)
        w.set_added_audios([
            AddedAudio(path=Path("long.mp3"), duration=100.0, rate=48000,
                       offset=0.0, lane=1, peaks=[0.5] * 64),
        ])

        w.zoom_to_fit()

        extent = w._total_length()
        self.assertGreater(extent, 90.0, "added audio must drive the extent")
        self.assertLessEqual(
            (extent + SCROLL_TAIL_PAD_S) * w.pixels_per_second(),
            w._track_rect().width() + 1e-6,
        )

    def test_h3_an_empty_timeline_is_safe(self) -> None:
        w = _widget([])
        self.addCleanup(w.close)
        before = w.pixels_per_second()

        w.zoom_to_fit()

        self.assertEqual(w.pixels_per_second(), before)
        self.assertEqual(w._scroll_x, 0)

    def test_h3b_a_short_project_keeps_the_current_zoom(self) -> None:
        w = _widget([_clip(0.0, 2.0)])
        self.addCleanup(w.close)

        w.zoom_to_fit()

        self.assertEqual(w.pixels_per_second(), 40.0)
        self.assertEqual(w._scroll_x, 0)

    def test_h4_the_tab_2a_scroll_clamp_still_holds_after_a_fit(self) -> None:
        for clips in ([], [_clip(0.0, 2.0)], [_clip(0.0, 600.0)],
                      [_clip(0.0, 100_000.0)]):
            with self.subTest(n=len(clips)):
                w = _widget(clips)
                self.addCleanup(w.close)
                w.set_scroll_x(w.scroll_max_px())

                w.zoom_to_fit()

                self.assertGreaterEqual(w._scroll_x, 0)
                self.assertLessEqual(w._scroll_x, w.scroll_max_px())

    def test_h4b_a_project_too_long_to_fit_lands_on_the_legal_minimum(self) -> None:
        """Documented bounded case: the sequence still does not fit, so the
        zoom takes MIN_PPS, the view goes to zero and it stays scrollable."""
        w = _widget([_clip(0.0, 100_000.0)])
        self.addCleanup(w.close)

        w.zoom_to_fit()

        self.assertEqual(w.pixels_per_second(), MIN_PPS)
        self.assertEqual(w._scroll_x, 0)
        self.assertGreater(w.scroll_max_px(), 0)

    def test_h5_the_fit_uses_the_current_viewport_width_not_a_stale_one(self) -> None:
        w = _widget([_clip(0.0, 100.0)], size=(1200, 300))
        self.addCleanup(w.close)
        w.zoom_to_fit()
        narrow = w.pixels_per_second()

        w.resize(2400, 300)
        w.set_pixels_per_second(40.0)
        w.zoom_to_fit()

        self.assertGreater(w.pixels_per_second(), narrow)

    def test_h6_zoom_to_fit_emits_the_zoom_change_for_the_slider(self) -> None:
        w = _widget([_clip(0.0, 100.0)])
        self.addCleanup(w.close)
        seen: list[float] = []
        w.pixelsPerSecondChanged.connect(seen.append)

        w.zoom_to_fit()

        self.assertEqual(seen, [w.pixels_per_second()])


if __name__ == "__main__":
    unittest.main()
