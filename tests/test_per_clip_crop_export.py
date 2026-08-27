"""Per-clip crop export foundation (Tab 2I-B).

`Clip.crop_rect` / `Clip.crop_preset` are committed domain state that the
GUI does not write yet. These tests pin down how the exporter must read
them once it does:

* legacy `ExportJob.crop` behaviour is untouched while every clip is
  uncropped;
* one effective per-clip crop switches the export into per-clip mode,
  where the legacy global crop no longer leaks onto uncropped clips;
* whatever the mix of crops and source resolutions, every visual segment
  still normalizes into exactly one concat canvas.
"""

from pathlib import Path
import unittest

from cove_video_editor.clip import Clip, MediaAsset
from cove_video_editor.exporter import (
    AudioTrack,
    ExportJob,
    ExportWorker,
    effective_clip_crop_pixels,
    has_per_clip_crop,
    resolve_target_size,
)


SRC = Path(__file__).resolve().parents[1] / "src" / "cove_video_editor"


def _asset(
    name: str,
    *,
    has_audio: bool = True,
    kind: str = "video",
    width: int = 1920,
    height: int = 1080,
) -> MediaAsset:
    return MediaAsset(
        path=Path(name),
        duration=4.0,
        width=width,
        height=height,
        fps=30.0,
        has_audio=has_audio,
        kind=kind,
    )


def _clip(asset: MediaAsset, *, start: float = 0.0, crop=None, preset=None) -> Clip:
    c = Clip(asset, timeline_start=start)
    c.src_end = 2.0
    if crop is not None:
        c.crop_rect = crop
    if preset is not None:
        c.crop_preset = preset
    return c


def _normalized_preset_rect(target_aspect: float, sw: int, sh: int):
    """Centered normalized rect for a pixel aspect lock - the same mapping
    CropOverlay uses (`target_aspect / source_aspect`), reproduced here so
    these tests exercise realistic UI-produced rectangles without pulling
    Qt into an exporter test."""
    ratio = target_aspect / (sw / sh)
    if ratio <= 1.0:
        w, h = ratio, 1.0
    else:
        w, h = 1.0, (sw / sh) / target_aspect
    return ((1.0 - w) / 2.0, (1.0 - h) / 2.0, w, h)


def _filter_complex(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def _segment_chain(graph: str, index: int) -> str:
    """The one filtergraph statement producing [v<index>]."""
    return next(p for p in graph.split(";") if p.endswith(f"[v{index}]"))


def _graph_for(job: ExportJob) -> str:
    return _filter_complex(ExportWorker(job)._build_command())


def _job(clips, **kw) -> ExportJob:
    kw.setdefault("fmt_key", "MP4 (H.264 + AAC)")
    return ExportJob(clips=clips, output=Path("out.mp4"), **kw)


# --- Group A - legacy parity regression guards -------------------------


class LegacyGlobalCropParityTests(unittest.TestCase):
    """GREEN guards: current global-crop behaviour, pinned before per-clip
    mode existed so Tab 2I-B cannot drift it."""

    def test_a1_no_crop_at_all_emits_no_crop_filter(self) -> None:
        clips = [_clip(_asset("a.mp4")), _clip(_asset("b.mp4"), start=2.0)]
        graph = _graph_for(_job(clips))
        self.assertNotIn("crop=", graph)
        for i in (0, 1):
            chain = _segment_chain(graph, i)
            self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", chain)

    def test_a2_legacy_global_crop_filter_unchanged(self) -> None:
        clips = [_clip(_asset("a.mp4"))]
        graph = _graph_for(_job(clips, crop=(10, 20, 640, 480)))
        chain = _segment_chain(graph, 0)
        self.assertIn("crop=640:480:10:20", chain)
        self.assertLess(chain.index("crop="), chain.index("scale="))

    def test_a3_legacy_global_crop_applies_to_every_visual_clip(self) -> None:
        clips = [
            _clip(_asset("a.mp4")),
            _clip(_asset("b.mp4", width=1366, height=768), start=2.0),
            _clip(_asset("c.png", has_audio=False, kind="image",
                         width=800, height=600), start=4.0),
        ]
        graph = _graph_for(_job(clips, crop=(10, 20, 640, 480)))
        for i in (0, 1, 2):
            self.assertIn("crop=640:480:10:20", _segment_chain(graph, i))

    def test_a4_legacy_global_crop_still_outranks_explicit_resolution(self) -> None:
        clips = [_clip(_asset("a.mp4"))]
        self.assertEqual(
            resolve_target_size(clips, (10, 20, 640, 480), 1080, 1920), (640, 480)
        )
        graph = _graph_for(_job(clips, crop=(10, 20, 640, 480), width=1080, height=1920))
        chain = _segment_chain(graph, 0)
        self.assertIn("scale=640:480:force_original_aspect_ratio=decrease"
                      ":force_divisible_by=2", chain)
        self.assertIn("pad=640:480:(ow-iw)/2:(oh-ih)/2:color=black", chain)


# --- Group B - per-clip mode detection ---------------------------------


class PerClipModeDetectionTests(unittest.TestCase):
    def test_b1_one_effective_crop_activates_per_clip_mode(self) -> None:
        clips = [
            _clip(_asset("a.mp4"), crop=_normalized_preset_rect(9 / 16, 1920, 1080)),
            _clip(_asset("b.mp4"), start=2.0),
        ]
        self.assertTrue(has_per_clip_crop(clips))

    def test_b2_all_none_stays_legacy_mode(self) -> None:
        clips = [_clip(_asset("a.mp4")), _clip(_asset("b.mp4"), start=2.0)]
        self.assertFalse(has_per_clip_crop(clips))
        # And the legacy global crop is still applied to every clip.
        graph = _graph_for(_job(clips, crop=(0, 0, 640, 480)))
        for i in (0, 1):
            self.assertIn("crop=640:480:0:0", _segment_chain(graph, i))

    def test_b3_explicit_full_frame_tuple_does_not_activate(self) -> None:
        clips = [_clip(_asset("a.mp4"), crop=(0.0, 0.0, 1.0, 1.0))]
        self.assertFalse(has_per_clip_crop(clips))
        self.assertIsNone(effective_clip_crop_pixels(clips[0]))

    def test_b4_full_frame_clip_stays_uncropped_in_per_clip_mode(self) -> None:
        clips = [
            _clip(_asset("a.mp4"), crop=(0.0, 0.0, 1.0, 1.0)),
            _clip(_asset("b.mp4"), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1920, 1080)),
        ]
        self.assertTrue(has_per_clip_crop(clips))
        # Legacy crop is present but must not leak onto the uncropped clip
        # once per-clip mode is active.
        graph = _graph_for(_job(clips, crop=(10, 20, 640, 480), width=1280, height=720))
        self.assertNotIn("crop=", _segment_chain(graph, 0))
        self.assertIn("crop=608:1080:656:0", _segment_chain(graph, 1))
        self.assertNotIn("crop=640:480:10:20", graph)


# --- Group C - normalized to pixel conversion --------------------------


class NormalizedCropConversionTests(unittest.TestCase):
    def test_c1_portrait_crop_on_hd_source(self) -> None:
        c = _clip(_asset("a.mp4"), crop=_normalized_preset_rect(9 / 16, 1920, 1080))
        self.assertEqual(effective_clip_crop_pixels(c), (656, 0, 608, 1080))

    def test_c2_square_crop_on_hd_source(self) -> None:
        c = _clip(_asset("a.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080))
        self.assertEqual(effective_clip_crop_pixels(c), (420, 0, 1080, 1080))

    def test_c3_portrait_crop_on_wxga_source(self) -> None:
        c = _clip(_asset("b.mp4", width=1366, height=768),
                  crop=_normalized_preset_rect(9 / 16, 1366, 768))
        self.assertEqual(effective_clip_crop_pixels(c), (466, 0, 432, 768))

    def test_c4_crop_stays_in_bounds_and_even(self) -> None:
        cases = [
            ((0.0, 0.0, 1.0, 1.0), 1921, 1081),      # odd source, full frame
            ((0.9, 0.9, 0.5, 0.5), 1920, 1080),      # rect running past the edge
            ((0.333, 0.111, 0.777, 0.555), 1366, 768),
        ]
        for rect, sw, sh in cases:
            with self.subTest(rect=rect, src=(sw, sh)):
                c = _clip(_asset("x.mp4", width=sw, height=sh), crop=rect)
                px = effective_clip_crop_pixels(c)
                if px is None:
                    continue
                x, y, w, h = px
                self.assertEqual((x % 2, y % 2, w % 2, h % 2), (0, 0, 0, 0))
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)
                self.assertLessEqual(x + w, sw)
                self.assertLessEqual(y + h, sh)

    def test_c5_full_frame_tuple_is_no_effective_crop(self) -> None:
        c = _clip(_asset("a.mp4"), crop=(0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(effective_clip_crop_pixels(c))

    def test_c5b_pixel_equivalent_full_frame_is_no_effective_crop(self) -> None:
        """Float noise in a near-full rectangle must not manufacture a
        no-op crop filter, and must not flip the export into per-clip mode
        and discard the legacy global crop."""
        near_full = (0.0, 0.0, 0.9999999999, 1.0)
        c = _clip(_asset("a.mp4"), crop=near_full)
        self.assertIsNone(effective_clip_crop_pixels(c))
        self.assertFalse(has_per_clip_crop([c]))
        # Legacy precedence therefore survives untouched.
        self.assertEqual(
            resolve_target_size([c], (0, 0, 640, 480), 1280, 720), (640, 480)
        )
        graph = _graph_for(_job([c], crop=(0, 0, 640, 480), width=1280, height=720))
        chain = _segment_chain(graph, 0)
        self.assertIn("crop=640:480:0:0", chain)
        self.assertNotIn("crop=1920:1080", chain)

    def test_c5c_odd_source_full_frame_is_no_effective_crop(self) -> None:
        # The widest even crop of a 1921px source is 1920px - full
        # coverage, not a crop.
        c = _clip(_asset("odd.mp4", width=1921, height=1081),
                  crop=(0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(effective_clip_crop_pixels(c))
        self.assertFalse(has_per_clip_crop([c]))

    def test_c6_default_clip_has_no_effective_crop(self) -> None:
        c = _clip(_asset("a.mp4"))
        self.assertIsNone(c.crop_rect)
        self.assertIsNone(effective_clip_crop_pixels(c))

    def test_c7_crop_preset_is_not_consulted(self) -> None:
        """A Free custom crop exports exactly like a preset crop."""
        rect = _normalized_preset_rect(1.0, 1920, 1080)
        free = _clip(_asset("a.mp4"), crop=rect, preset="Free (Custom)")
        preset = _clip(_asset("a.mp4"), crop=rect,
                       preset="1:1 (Square / Instagram)")
        self.assertEqual(
            effective_clip_crop_pixels(free), effective_clip_crop_pixels(preset)
        )

    def test_c8_invalid_source_dimensions_yield_no_crop(self) -> None:
        c = _clip(_asset("a.mp4", width=0, height=0),
                  crop=(0.1, 0.1, 0.5, 0.5))
        self.assertIsNone(effective_clip_crop_pixels(c))


# --- Group D - per-segment filters -------------------------------------


class PerSegmentFilterTests(unittest.TestCase):
    def _graph(self) -> str:
        clips = [
            _clip(_asset("a.mp4")),
            _clip(_asset("b.mp4"), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1920, 1080)),
        ]
        # A legacy global crop is deliberately present: once per-clip mode
        # is active it must not reach the uncropped clip.
        return _graph_for(_job(clips, crop=(10, 20, 640, 480),
                               width=1280, height=720))

    def test_d1_uncropped_clip_has_no_crop_filter(self) -> None:
        self.assertNotIn("crop=", _segment_chain(self._graph(), 0))

    def test_d2_cropped_clip_carries_its_own_crop(self) -> None:
        self.assertIn("crop=608:1080:656:0", _segment_chain(self._graph(), 1))

    def test_d3_both_segments_share_one_canvas(self) -> None:
        graph = self._graph()
        for i in (0, 1):
            chain = _segment_chain(graph, i)
            self.assertIn("scale=1280:720:force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", chain)
            self.assertIn("pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black", chain)
            self.assertIn("setsar=1", chain)
            self.assertIn("format=yuv420p", chain)


# --- Group E - two different crops -------------------------------------


class TwoDifferentCropsTests(unittest.TestCase):
    def _graph(self) -> str:
        clips = [
            _clip(_asset("a.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080)),
            _clip(_asset("b.mp4", width=1366, height=768), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1366, 768)),
        ]
        return _graph_for(_job(clips, crop=(10, 20, 640, 480),
                               width=1280, height=720))

    def test_e1_each_clip_gets_its_own_crop(self) -> None:
        graph = self._graph()
        self.assertIn("crop=1080:1080:420:0", _segment_chain(graph, 0))
        self.assertIn("crop=432:768:466:0", _segment_chain(graph, 1))

    def test_e2_legacy_crop_does_not_leak(self) -> None:
        self.assertNotIn("crop=640:480:10:20", self._graph())

    def test_e3_concat_geometry_is_identical(self) -> None:
        graph = self._graph()
        for i in (0, 1):
            chain = _segment_chain(graph, i)
            self.assertIn("scale=1280:720:force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", chain)
            self.assertIn("pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black", chain)
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vc][ac]", graph)


# --- Group F - target size precedence ----------------------------------


class TargetSizePrecedenceTests(unittest.TestCase):
    def test_f1_explicit_resolution_wins_in_per_clip_mode(self) -> None:
        clips = [
            _clip(_asset("a.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080)),
            _clip(_asset("b.mp4"), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1920, 1080)),
        ]
        self.assertEqual(resolve_target_size(clips, None, 1920, 1080), (1920, 1080))
        # ...and it still wins over a stale legacy global crop.
        self.assertEqual(
            resolve_target_size(clips, (0, 0, 640, 480), 1920, 1080), (1920, 1080)
        )

    def test_f2_first_clip_crop_defines_fallback_target(self) -> None:
        clips = [
            _clip(_asset("a.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080)),
            _clip(_asset("b.mp4", width=1366, height=768), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1366, 768)),
        ]
        self.assertEqual(resolve_target_size(clips, None, None, None), (1080, 1080))

    def test_f3_uncropped_first_clip_defines_fallback_target(self) -> None:
        clips = [
            _clip(_asset("a.mp4")),
            _clip(_asset("b.mp4"), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1920, 1080)),
        ]
        self.assertEqual(resolve_target_size(clips, None, None, None), (1920, 1080))

    def test_f4_timeline_order_decides_the_fallback(self) -> None:
        """Swapping the timeline order swaps the derived canvas - the
        fallback is the *first visual clip*, never the largest, the last,
        or the selected one."""
        square = _normalized_preset_rect(1.0, 1920, 1080)
        portrait = _normalized_preset_rect(9 / 16, 1920, 1080)

        a = _clip(_asset("a.mp4"), start=0.0, crop=square)
        b = _clip(_asset("b.mp4"), start=2.0, crop=portrait)
        graph = _graph_for(_job([b, a]))  # list order deliberately reversed
        self.assertIn("scale=1080:1080:", _segment_chain(graph, 0))
        self.assertIn("scale=1080:1080:", _segment_chain(graph, 1))

        a2 = _clip(_asset("a.mp4"), start=2.0, crop=square)
        b2 = _clip(_asset("b.mp4"), start=0.0, crop=portrait)
        graph2 = _graph_for(_job([a2, b2]))
        self.assertIn("scale=608:1080:", _segment_chain(graph2, 0))
        self.assertIn("scale=608:1080:", _segment_chain(graph2, 1))

    def test_f5_legacy_precedence_is_untouched(self) -> None:
        clips = [_clip(_asset("a.mp4"))]
        self.assertEqual(resolve_target_size(clips, None, None, None), (1920, 1080))
        self.assertEqual(resolve_target_size(clips, None, 1280, 720), (1280, 720))
        self.assertEqual(
            resolve_target_size(clips, (0, 0, 640, 480), 1280, 720), (640, 480)
        )
        self.assertEqual(resolve_target_size([], None, None, None), (1280, 720))

    def test_f6_odd_effective_crop_target_is_forced_even(self) -> None:
        # 0.5 of a 1921-wide source rounds to an odd pixel width before the
        # even-snap; the derived canvas must still come out even.
        clips = [_clip(_asset("odd.mp4", width=1921, height=1081),
                       crop=(0.0, 0.0, 0.5, 0.5))]
        w, h = resolve_target_size(clips, None, None, None)
        self.assertEqual((w % 2, h % 2), (0, 0))
        self.assertEqual((w, h), effective_clip_crop_pixels(clips[0])[2:])


# --- Group G - image plus video ----------------------------------------


class ImageAndVideoCropTests(unittest.TestCase):
    def _clips(self) -> list[Clip]:
        return [
            _clip(_asset("card.png", has_audio=False, kind="image",
                         width=1600, height=1200),
                  crop=_normalized_preset_rect(1.0, 1600, 1200)),
            _clip(_asset("b.mp4"), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1920, 1080)),
        ]

    def test_g1_image_clip_gets_its_crop_filter(self) -> None:
        graph = _graph_for(_job(self._clips(), width=1280, height=720))
        chain = _segment_chain(graph, 0)
        # 1:1 of 1600x1200 -> 1200x1200 at x=200.
        self.assertIn("crop=1200:1200:200:0", chain)
        self.assertIn("setpts=PTS-STARTPTS", chain)
        self.assertNotIn("trim=start=", chain)

    def test_g2_video_clip_keeps_its_own_different_crop(self) -> None:
        graph = _graph_for(_job(self._clips(), width=1280, height=720))
        self.assertIn("crop=608:1080:656:0", _segment_chain(graph, 1))

    def test_g3_image_and_video_reach_the_same_canvas(self) -> None:
        graph = _graph_for(_job(self._clips(), width=1280, height=720))
        for i in (0, 1):
            chain = _segment_chain(graph, i)
            self.assertIn("scale=1280:720:force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", chain)
            self.assertIn("pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black", chain)
            self.assertIn("setsar=1", chain)
            self.assertIn("format=yuv420p", chain)

    def test_g4_image_crop_uses_the_image_asset_dimensions(self) -> None:
        img = _clip(_asset("card.png", has_audio=False, kind="image",
                           width=800, height=600),
                    crop=(0.0, 0.0, 0.5, 0.5))
        self.assertEqual(effective_clip_crop_pixels(img), (0, 0, 400, 300))
        graph = _graph_for(_job([img], width=1280, height=720))
        self.assertIn("crop=400:300:0:0", _segment_chain(graph, 0))

    def test_g5_image_only_timeline_derives_target_from_image_crop(self) -> None:
        img = _clip(_asset("card.png", has_audio=False, kind="image",
                           width=800, height=600),
                    crop=(0.0, 0.0, 0.5, 0.5))
        self.assertEqual(resolve_target_size([img], None, None, None), (400, 300))


# --- Group H - mixed source resolutions --------------------------------


class MixedSourceResolutionTests(unittest.TestCase):
    def _clips(self) -> list[Clip]:
        return [
            _clip(_asset("hd.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080)),
            _clip(_asset("wxga.mp4", width=1366, height=768), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1366, 768)),
            _clip(_asset("uhd.mp4", width=3840, height=2160), start=4.0),
        ]

    def test_h1_every_segment_converges_on_one_canvas(self) -> None:
        graph = _graph_for(_job(self._clips()))
        for i in (0, 1, 2):
            chain = _segment_chain(graph, i)
            self.assertIn("scale=1080:1080:force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", chain)
            self.assertIn("pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=black", chain)

    def test_h2_no_sar_or_pixel_format_divergence(self) -> None:
        graph = _graph_for(_job(self._clips()))
        for i in (0, 1, 2):
            chain = _segment_chain(graph, i)
            self.assertIn("setsar=1", chain)
            self.assertIn("format=yuv420p", chain)

    def test_h3_uncropped_third_clip_stays_uncropped(self) -> None:
        graph = _graph_for(_job(self._clips()))
        self.assertNotIn("crop=", _segment_chain(graph, 2))

    def test_h4_gap_segments_are_never_cropped(self) -> None:
        clips = [
            _clip(_asset("a.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080)),
            _clip(_asset("b.mp4"), start=8.0),
        ]
        graph = _graph_for(_job(clips, width=1280, height=720))
        gap = _segment_chain(graph, 1)
        self.assertIn("color=c=black:s=1280x720", gap)
        self.assertNotIn("crop=", gap)
        self.assertIn("setsar=1", gap)
        self.assertIn("format=yuv420p", gap)


# --- Group I - export invariants ---------------------------------------


class ExportInvariantTests(unittest.TestCase):
    def _chains(self) -> list[str]:
        clips = [
            _clip(_asset("a.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080)),
            _clip(_asset("card.png", has_audio=False, kind="image",
                         width=1600, height=1200), start=2.0,
                  crop=_normalized_preset_rect(9 / 16, 1600, 1200)),
        ]
        graph = _graph_for(_job(clips, width=1280, height=720))
        return [_segment_chain(graph, 0), _segment_chain(graph, 1)]

    def test_i1_crop_precedes_scale_and_pad(self) -> None:
        for chain in self._chains():
            self.assertLess(chain.index("crop="), chain.index("scale="))
            self.assertLess(chain.index("scale="), chain.index("pad="))

    def test_i2_normalization_chain_order_is_preserved(self) -> None:
        for chain in self._chains():
            self.assertLess(chain.index("pad="), chain.index("setsar=1"))
            self.assertLess(chain.index("setsar=1"), chain.index("format=yuv420p"))

    def test_i3_force_divisible_by_two_survives(self) -> None:
        for chain in self._chains():
            self.assertIn("force_original_aspect_ratio=decrease"
                          ":force_divisible_by=2", chain)

    def test_i4_speed_setpts_still_follows_normalization(self) -> None:
        c = _clip(_asset("a.mp4"), crop=_normalized_preset_rect(1.0, 1920, 1080))
        c.speed = 2.0
        chain = _segment_chain(_graph_for(_job([c], width=1280, height=720)), 0)
        self.assertLess(chain.index("crop="), chain.index("scale="))
        self.assertLess(chain.index("setsar=1"), chain.index("setpts=0.50000*PTS"))
        self.assertLess(chain.index("setpts=0.50000*PTS"),
                        chain.index("format=yuv420p"))


# --- Group J - hardware command structure ------------------------------


class HardwareCommandStructureTests(unittest.TestCase):
    """Per-clip crop is a filtergraph concern only; encoder selection and
    args must be byte-identical with and without crops."""

    def _cmd(self, *, cropped: bool, pref: str) -> list[str]:
        rect = _normalized_preset_rect(9 / 16, 1920, 1080) if cropped else None
        clips = [_clip(_asset("a.mp4"), crop=rect),
                 _clip(_asset("b.mp4"), start=2.0)]
        return ExportWorker(
            _job(clips, width=1280, height=720, encoder_pref=pref)
        )._build_command()

    def _encoder_tail(self, cmd: list[str]) -> list[str]:
        return cmd[cmd.index("-filter_complex") + 2:]

    def test_j1_cpu_command_differs_only_in_the_filtergraph(self) -> None:
        plain = self._cmd(cropped=False, pref="cpu")
        cropped = self._cmd(cropped=True, pref="cpu")
        self.assertEqual(self._encoder_tail(plain), self._encoder_tail(cropped))
        self.assertIn("libx264", cropped)
        self.assertNotIn("crop=", _filter_complex(plain))
        self.assertIn("crop=608:1080:656:0", _filter_complex(cropped))

    def test_j2_nvenc_command_differs_only_in_the_filtergraph(self) -> None:
        import cove_video_editor.ffmpeg_utils as ff
        orig = ff.nvenc_available
        ff.nvenc_available = lambda codec: True
        try:
            plain = self._cmd(cropped=False, pref="nvenc")
            cropped = self._cmd(cropped=True, pref="nvenc")
        finally:
            ff.nvenc_available = orig
        self.assertEqual(self._encoder_tail(plain), self._encoder_tail(cropped))
        self.assertIn("h264_nvenc", cropped)

    def test_j3_amf_command_differs_only_in_the_filtergraph(self) -> None:
        import cove_video_editor.ffmpeg_utils as ff
        orig = ff.amf_available
        ff.amf_available = lambda codec: True
        try:
            plain = self._cmd(cropped=False, pref="amf")
            cropped = self._cmd(cropped=True, pref="amf")
        finally:
            ff.amf_available = orig
        self.assertEqual(self._encoder_tail(plain), self._encoder_tail(cropped))
        self.assertIn("h264_amf", cropped)


# --- Group K - audio / region regression -------------------------------


class AudioAndRegionTests(unittest.TestCase):
    def test_k1_audio_only_export_ignores_crop_state(self) -> None:
        clips = [_clip(_asset("a.mp4"),
                       crop=_normalized_preset_rect(9 / 16, 1920, 1080))]
        job = ExportJob(clips=clips, output=Path("out.wav"),
                        fmt_key="WAV (audio only)")
        cmd = ExportWorker(job)._build_command()
        graph = _filter_complex(cmd)
        self.assertNotIn("crop=", graph)
        self.assertNotIn("scale=", graph)
        self.assertNotIn("-c:v", cmd)

    def test_k2_region_export_keeps_its_output_side_trim(self) -> None:
        clips = [_clip(_asset("a.mp4"),
                       crop=_normalized_preset_rect(9 / 16, 1920, 1080)),
                 _clip(_asset("b.mp4"), start=2.0)]
        cmd = ExportWorker(
            _job(clips, width=1280, height=720, region_start=0.5, region_end=1.5)
        )._build_command()
        self.assertIn("-ss", cmd)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "0.500")
        self.assertEqual(cmd[cmd.index("-t") + 1], "1.000")
        self.assertIn("crop=608:1080:656:0", _filter_complex(cmd))

    def test_k3_added_audio_tracks_are_unaffected_by_crop(self) -> None:
        clips = [_clip(_asset("a.mp4"),
                       crop=_normalized_preset_rect(9 / 16, 1920, 1080))]
        track = AudioTrack(path=Path("added.mp3"), volume=0.5, duration=2.0)
        graph = _graph_for(_job(clips, width=1280, height=720, audio_tracks=[track]))
        self.assertIn("volume=0.500[extra_a0]", graph)
        self.assertIn("amix=inputs=2", graph)


# --- Groups L / M - structural scope ------------------------------------


class StructuralScopeTests(unittest.TestCase):
    def test_l1_app_does_not_consume_committed_crop_state(self) -> None:
        """The GUI still exports through the legacy global crop; per-clip
        export stays dormant until the crop lifecycle slice lands."""
        app_src = (SRC / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("crop_rect", app_src)
        self.assertIn("crop=self._crop_pixels()", app_src)

    def test_m1_rejected_canvas_fit_architecture_is_absent(self) -> None:
        for name in ("exporter.py", "clip.py", "crop_overlay.py", "app.py"):
            src = (SRC / name).read_text(encoding="utf-8")
            for symbol in ("crop_fit_mode", "canvas_fit", "canvas_aspect",
                           "CROP_FIT_MODES", "set_fit_mode", "fit_mode"):
                with self.subTest(file=name, symbol=symbol):
                    self.assertNotIn(symbol, src)


if __name__ == "__main__":
    unittest.main()
