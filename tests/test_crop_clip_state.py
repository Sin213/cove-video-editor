"""Tab 2I-A: per-clip committed crop state on the domain model.

``Clip`` gains two dormant fields - ``crop_rect`` (normalized source-space
``(x, y, w, h)`` or ``None`` for "no crop") and ``crop_preset`` (the opaque
display key from the crop overlay's preset registry). Nothing in the UI or
the exporter reads them yet; this slice only proves the domain model owns,
clones, splits and snapshots them correctly.

The model must stay free of Qt geometry types so clones, equality and the
future exporter handoff remain plain-data operations.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

from cove_video_editor.clip import Clip, MediaAsset, split_clip  # noqa: E402


FREE = "Free (Custom)"
TIKTOK = "9:16 (TikTok / Reels / Shorts)"
SQUARE = "1:1 (Square / Instagram)"


def _asset(duration: float = 600.0, name: str = "a.mp4") -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=duration, width=1920, height=1080,
        fps=30.0, has_audio=True,
    )


def _clip(start: float = 0.0, length: float = 10.0, *, src_start: float = 0.0,
          **kwargs) -> Clip:
    """A clip occupying ``[start, start + length)`` - the same construction
    shape existing timeline tests use."""
    return Clip(
        asset=_asset(), timeline_start=start, src_start=src_start,
        src_end=src_start + length, **kwargs,
    )


class CropDefaultsTests(unittest.TestCase):
    """Group A - a clip built the way current code builds one is uncropped."""

    def test_a1_default_clip_has_no_committed_crop(self) -> None:
        c = _clip()
        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)

    def test_a2_existing_constructor_invocation_still_valid(self) -> None:
        """No new positional/required argument: the representative call used
        across the existing suite keeps working untouched."""
        c = Clip(
            asset=_asset(), timeline_start=2.0, src_start=1.0, src_end=5.0,
            speed=2.0, muted=True, audio_volume=0.5, linked_audio=False,
            audio_offset=0.25, audio_removed=True,
        )
        self.assertEqual(c.timeline_start, 2.0)
        self.assertEqual(c.src_end, 5.0)
        self.assertEqual(c.speed, 2.0)
        self.assertTrue(c.muted)
        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)


    def test_a3_positional_constructor_slots_are_unshifted(self) -> None:
        """The crop fields must not displace any pre-existing positional slot.

        A caller passing through the historical ``id`` position must still get
        an id, not a string silently stored as a crop rect.
        """
        c = Clip(_asset(), 2.0, 1.0, 5.0, 2.0, True, 0.5, False, 0.25, True,
                 "deadbeef")
        self.assertEqual(c.id, "deadbeef")
        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)


class CropExplicitStateTests(unittest.TestCase):
    """Group B - explicitly supplied crop state is stored verbatim."""

    def test_b1_free_form_rect_stored_exactly(self) -> None:
        c = _clip(crop_rect=(0.1, 0.2, 0.5, 0.6), crop_preset=FREE)
        self.assertEqual(c.crop_rect, (0.1, 0.2, 0.5, 0.6))
        self.assertEqual(c.crop_preset, FREE)

    def test_b2_preset_rect_stored_exactly(self) -> None:
        c = _clip(crop_rect=(0.359375, 0.0, 0.28125, 1.0), crop_preset=TIKTOK)
        self.assertEqual(c.crop_rect, (0.359375, 0.0, 0.28125, 1.0))
        self.assertEqual(c.crop_preset, TIKTOK)

    def test_b3_crop_rect_is_plain_data_not_a_qt_type(self) -> None:
        c = _clip(crop_rect=(0.0, 0.25, 1.0, 0.5), crop_preset=FREE)
        self.assertIsInstance(c.crop_rect, tuple)
        self.assertEqual(len(c.crop_rect), 4)
        self.assertTrue(all(isinstance(v, float) for v in c.crop_rect))
        self.assertEqual(type(c.crop_rect).__module__, "builtins")


class CropZeroVersusNoneTests(unittest.TestCase):
    """Group C - ``None`` and a full-frame tuple are distinguishable, and the
    model coerces neither. Canonicalizing full frame to ``None`` is the future
    UI commit layer's job, not the dataclass's."""

    def test_c1_none_is_preserved_not_expanded_to_full_frame(self) -> None:
        self.assertIsNone(_clip(crop_rect=None).crop_rect)

    def test_c2_explicit_full_frame_tuple_is_preserved_not_collapsed(self) -> None:
        c = _clip(crop_rect=(0.0, 0.0, 1.0, 1.0))
        self.assertIsNotNone(c.crop_rect)
        self.assertEqual(c.crop_rect, (0.0, 0.0, 1.0, 1.0))

    def test_c3_none_and_full_frame_are_distinct_states(self) -> None:
        self.assertNotEqual(
            _clip(crop_rect=None).crop_rect,
            _clip(crop_rect=(0.0, 0.0, 1.0, 1.0)).crop_rect,
        )


class CropCloneTests(unittest.TestCase):
    """Group D - ``Clip.clone()`` is the authoritative copy route used by
    split, ripple delete and the undo/redo snapshots, so it must carry
    committed crop state."""

    def test_d1_default_clip_clone_stays_uncropped(self) -> None:
        c = _clip().clone()
        self.assertIsNone(c.crop_rect)
        self.assertEqual(c.crop_preset, FREE)

    def test_d2_clone_preserves_exact_crop_state(self) -> None:
        src = _clip(crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=SQUARE)
        clone = src.clone()
        self.assertEqual(clone.crop_rect, (0.25, 0.0, 0.5, 1.0))
        self.assertEqual(clone.crop_preset, SQUARE)

    def test_d3_mutating_clone_crop_state_leaves_source_untouched(self) -> None:
        src = _clip(crop_rect=(0.25, 0.0, 0.5, 1.0), crop_preset=SQUARE)
        clone = src.clone()
        clone.crop_rect = None
        clone.crop_preset = FREE
        self.assertEqual(src.crop_rect, (0.25, 0.0, 0.5, 1.0))
        self.assertEqual(src.crop_preset, SQUARE)

    def test_d4_unrelated_clip_fields_still_clone(self) -> None:
        """Spot guard that adding crop fields did not disturb the existing
        clone contract for the fields the timeline and exporter rely on."""
        src = _clip(2.0, 4.0, speed=2.0, muted=True, audio_volume=0.4,
                    linked_audio=False, audio_offset=0.5, audio_removed=True)
        src.waveform_rate = 48000
        clone = src.clone()
        for name in ("asset", "timeline_start", "src_start", "src_end", "speed",
                     "muted", "audio_volume", "linked_audio", "audio_offset",
                     "audio_removed", "waveform_rate"):
            self.assertEqual(getattr(clone, name), getattr(src, name), name)


class CropSplitTests(unittest.TestCase):
    """Group E - crop is spatial and time-invariant, so both halves of a split
    represent the same source framing and inherit the committed crop."""

    def test_e1_split_halves_inherit_crop_state(self) -> None:
        left = _clip(0.0, 10.0, crop_rect=(0.1, 0.2, 0.5, 0.6),
                     crop_preset=TIKTOK)
        right = split_clip(left, 5.0)
        self.assertIsNotNone(right)
        self.assertEqual(left.crop_rect, (0.1, 0.2, 0.5, 0.6))
        self.assertEqual(left.crop_preset, TIKTOK)
        self.assertEqual(right.crop_rect, (0.1, 0.2, 0.5, 0.6))
        self.assertEqual(right.crop_preset, TIKTOK)

    def test_e2_split_of_uncropped_clip_stays_uncropped(self) -> None:
        left = _clip(0.0, 10.0)
        right = split_clip(left, 5.0)
        self.assertIsNotNone(right)
        for half in (left, right):
            self.assertIsNone(half.crop_rect)
            self.assertEqual(half.crop_preset, FREE)

    def test_e3_split_halves_hold_crop_state_independently(self) -> None:
        left = _clip(0.0, 10.0, crop_rect=(0.1, 0.2, 0.5, 0.6),
                     crop_preset=TIKTOK)
        right = split_clip(left, 5.0)
        right.crop_rect = (0.0, 0.0, 0.25, 0.25)
        right.crop_preset = SQUARE
        self.assertEqual(left.crop_rect, (0.1, 0.2, 0.5, 0.6))
        self.assertEqual(left.crop_preset, TIKTOK)


class CropSnapshotCompatibilityTests(unittest.TestCase):
    """Group F - undo/redo snapshots clone every clip on capture and again on
    restore, so committed crop state must survive that round trip. Proven at
    the object level plus a structural check that the app's snapshot route is
    still the clone route (no MainWindow, no encoder probe)."""

    def test_f1_snapshot_round_trip_preserves_crop_state(self) -> None:
        clips = [_clip(0.0, 5.0, crop_rect=(0.1, 0.2, 0.5, 0.6),
                       crop_preset=TIKTOK),
                 _clip(5.0, 5.0)]
        snap = [c.clone() for c in clips]        # _current_state_snap
        restored = [c.clone() for c in snap]     # _apply_state
        self.assertEqual(restored[0].crop_rect, (0.1, 0.2, 0.5, 0.6))
        self.assertEqual(restored[0].crop_preset, TIKTOK)
        self.assertIsNone(restored[1].crop_rect)
        self.assertEqual(restored[1].crop_preset, FREE)

    def test_f2_app_snapshot_route_still_uses_clip_clone(self) -> None:
        import inspect

        from cove_video_editor.app import MainWindow

        snap_src = inspect.getsource(MainWindow._current_state_snap)
        apply_src = inspect.getsource(MainWindow._apply_state)
        self.assertIn("c.clone() for c in self._clips", snap_src)
        self.assertIn('c.clone() for c in snap["clips"]', apply_src)


class CropDormancyGuardTests(unittest.TestCase):
    """Group G - Tab 2I-A is a domain foundation only: the committed fields
    exist but nothing consumes them, and the rejected canvas-fit architecture
    stays out."""

    @staticmethod
    def _source(module: str) -> str:
        import cove_video_editor

        return (Path(cove_video_editor.__file__).parent / module).read_text(
            encoding="utf-8")

    def test_g1_uncropped_clip_behaves_exactly_as_before(self) -> None:
        c = _clip(2.0, 4.0, src_start=1.0)
        self.assertEqual(c.timeline_length, 4.0)
        self.assertEqual(c.timeline_end, 6.0)
        self.assertEqual(c.src_span, 4.0)
        self.assertEqual(c.src_for_timeline(4.0), 3.0)
        self.assertIsNone(c.crop_rect)

    def test_g2_only_the_commit_layer_touches_committed_crop_state(self) -> None:
        """Retargeted by Tab 2I-C.

        Tab 2I-A was a dormant foundation, so *no* UI module was allowed
        to read the committed fields. The crop lifecycle slice makes
        ``app.py`` the commit layer, which is exactly where that write
        belongs. The still-valid half of the guard is that nothing else
        joins it: ``CropOverlay`` owns the draft and emits intent only,
        and the timeline draws clips without knowing about crop at all.
        """
        for module in ("crop_overlay.py", "timeline_widget.py"):
            src = self._source(module)
            self.assertNotIn(".crop_rect", src, module)
            self.assertNotIn(".crop_preset", src, module)

    def test_g2_the_commit_layer_writes_both_committed_fields(self) -> None:
        import inspect

        from cove_video_editor.app import MainWindow

        src = inspect.getsource(MainWindow._finish_crop_edit)
        self.assertIn("crop_rect", src)
        self.assertIn("crop_preset", src)

    def test_g3_ffmpeg_utils_does_not_read_committed_crop_state(self) -> None:
        """Tab 2I-B deliberately taught ``exporter.py`` to read
        ``crop_rect``, so it is no longer covered here. ``ffmpeg_utils.py``
        stays crop-unaware: it owns encoder/format concerns, and crop is a
        filtergraph concern that belongs to the exporter.

        ``crop_preset`` remains editor-only metadata that no export path
        may consult - geometry comes from ``crop_rect`` alone, so a Free
        custom crop exports exactly like a preset crop."""
        src = self._source("ffmpeg_utils.py")
        self.assertNotIn(".crop_rect", src)
        self.assertNotIn(".crop_preset", src)
        self.assertNotIn(".crop_preset", self._source("exporter.py"))

    def test_g4_rejected_canvas_fit_architecture_is_absent(self) -> None:
        self.assertFalse(hasattr(_clip(), "crop_fit_mode"))
        clip_src = self._source("clip.py")
        for symbol in ("crop_fit_mode", "canvas_fit", "canvas_aspect",
                       "crop_aspect_lock", "crop_draft"):
            self.assertNotIn(symbol, clip_src, symbol)


class CropSerializationAuditTests(unittest.TestCase):
    """Group H - the audit's original result was "no serializer exists, so
    the new crop fields need no migration", with a guard demanding that any
    serializer which later appeared must handle crop state explicitly.

    Tab 2N introduced exactly one: ``project_io.py``. The guard therefore
    changes shape rather than disappearing - it now pins the serializer to
    that single module and requires it to name both crop fields. A second
    serialization path, or one that quietly dropped ``crop_rect`` /
    ``crop_preset``, would still be caught here."""

    #: The one module allowed to turn models into a stored document.
    SERIALIZER = "project_io.py"

    def test_h1_project_io_is_the_only_serialization_path(self) -> None:
        import cove_video_editor

        pkg = Path(cove_video_editor.__file__).parent
        offenders = []
        for path in sorted(pkg.glob("*.py")):
            if path.name == self.SERIALIZER:
                continue
            src = path.read_text(encoding="utf-8")
            for marker in ("dataclasses.asdict", "asdict(", "json.dump",
                           "pickle.dump"):
                if marker in src:
                    offenders.append(f"{path.name}: {marker}")
        self.assertEqual(offenders, [])

    def test_h2_the_serializer_handles_crop_state_explicitly(self) -> None:
        import cove_video_editor

        src = (Path(cove_video_editor.__file__).parent
               / self.SERIALIZER).read_text(encoding="utf-8")
        self.assertIn("crop_rect", src)
        self.assertIn("crop_preset", src)

    def test_h3_no_unsafe_deserialization_path_exists(self) -> None:
        import cove_video_editor

        pkg = Path(cove_video_editor.__file__).parent
        offenders = []
        for path in sorted(pkg.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for marker in ("pickle", "marshal", "shelve", "yaml.load"):
                if marker in src:
                    offenders.append(f"{path.name}: {marker}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
