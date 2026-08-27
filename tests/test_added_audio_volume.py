"""Per-item volume and explicit mute on AddedAudio.

Tab 2C shipped the per-item `volume` gain (default, clone and split
preservation). Tab 2G layers an independent `muted` override on top:

    effective gain = 0.0 if muted else volume

`muted` never rewrites `volume`, and `volume == 0.0` is deliberately *not*
the same state as `muted is True` - the first shows a ``0%`` timeline
badge, the second shows ``Muted``.

Qt pieces run on the ``offscreen`` platform plugin so widget geometry is
real rather than mocked. MainWindow construction suppresses the real
NVENC/AMF probe: it spawns ffmpeg children that outlive the window and
leak into ``ffmpeg_utils._active_probe_procs``, and nothing here depends
on encoder capabilities.
"""
from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import timeline_widget as tw  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402
from cove_video_editor.clip import (  # noqa: E402
    AddedAudio, Clip, MediaAsset, split_added_audio,
)
from cove_video_editor.timeline_widget import (  # noqa: E402
    TimelineWidget, added_audio_volume_badge,
)


_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _added(**kwargs) -> AddedAudio:
    opts = dict(path=Path("added.mp3"), duration=10.0, rate=48000, offset=2.0, lane=1)
    opts.update(kwargs)
    return AddedAudio(**opts)


def _hittable(**kwargs) -> AddedAudio:
    """An added-audio item the widget can actually hit-test: `peaks` is what
    makes the block paint and respond to clicks."""
    opts = dict(offset=0.0, duration=30.0, peaks=[0.5] * 64)
    opts.update(kwargs)
    return _added(**opts)


class _FakeAudioOutput:
    """Stands in for one QAudioOutput. Mirrors the real getter/setter pair
    production uses, so a passing test constrains the same call."""

    def __init__(self) -> None:
        self._volume = 1.0

    def setVolume(self, v: float) -> None:  # noqa: N802 - Qt naming
        self._volume = float(v)

    def volume(self) -> float:
        return self._volume


def _clip(name: str = "a.mp4") -> Clip:
    asset = MediaAsset(
        path=Path(name), duration=600.0, width=1920, height=1080,
        fps=30.0, has_audio=True,
    )
    return Clip(asset=asset, timeline_start=0.0, src_start=0.0, src_end=10.0)


def _window(audios: list[AddedAudio] | None = None, *,
            with_clip: bool = True) -> MainWindow:
    with unittest.mock.patch.object(
        MainWindow, "_start_encoder_probe", lambda self: None,
    ):
        w = MainWindow()
    if with_clip:
        w._clips = [_clip()]
        w.timeline.set_clips(w._clips)
    if audios:
        w._added_audios = list(audios)
        w._refresh_added_audio_display()
    return w


def _fake_outputs(w: MainWindow) -> dict[str, _FakeAudioOutput]:
    """Swap the live QAudioOutputs for recording fakes, keyed by audio id."""
    fakes = {a.id: _FakeAudioOutput() for a in w._added_audios}
    w._added_outputs.clear()
    w._added_outputs.update(fakes)
    return fakes


# ---- Group A: domain defaults ----------------------------------------------


class AddedAudioVolumeModelTests(unittest.TestCase):
    def test_new_added_audio_defaults_to_full_volume(self) -> None:
        self.assertEqual(_added().volume, 1.0)

    def test_added_audio_accepts_custom_volume(self) -> None:
        self.assertEqual(_added(volume=0.25).volume, 0.25)

    def test_clone_preserves_half_volume(self) -> None:
        self.assertEqual(_added(volume=0.5).clone().volume, 0.5)

    def test_clone_preserves_boosted_volume(self) -> None:
        self.assertEqual(_added(volume=1.5).clone().volume, 1.5)

    def test_clone_preserves_existing_fields(self) -> None:
        a = _added(volume=0.75, src_start=1.0, src_end=8.0, peaks=[0.1, 0.2])
        c = a.clone()

        self.assertEqual(c.path, a.path)
        self.assertEqual(c.id, a.id)
        self.assertEqual(c.duration, a.duration)
        self.assertEqual(c.rate, a.rate)
        self.assertEqual(c.peaks, a.peaks)
        self.assertEqual(c.offset, a.offset)
        self.assertEqual(c.lane, a.lane)
        self.assertEqual(c.src_start, a.src_start)
        self.assertEqual(c.src_end, a.src_end)

    def test_split_preserves_volume_on_first_half(self) -> None:
        a = _added(volume=0.5)
        right = split_added_audio(a, 6.0)

        self.assertIsNotNone(right)
        self.assertEqual(a.volume, 0.5)

    def test_split_preserves_volume_on_second_half(self) -> None:
        a = _added(volume=0.5)
        right = split_added_audio(a, 6.0)

        self.assertIsNotNone(right)
        self.assertEqual(right.volume, 0.5)

    def test_split_preserves_zero_volume_on_both_halves(self) -> None:
        a = _added(volume=0.0)
        right = split_added_audio(a, 6.0)

        self.assertIsNotNone(right)
        self.assertEqual(a.volume, 0.0)
        self.assertEqual(right.volume, 0.0)

    # A1
    def test_a1_default_added_audio_is_unmuted_at_full_volume(self) -> None:
        a = _added()

        self.assertEqual(a.volume, 1.0)
        self.assertIs(a.muted, False)

    # A2
    def test_a2_volume_and_mute_are_retained_independently(self) -> None:
        a = _added(volume=0.5, muted=True)

        self.assertEqual(a.volume, 0.5)
        self.assertIs(a.muted, True)

    # A3
    def test_a3_zero_volume_is_not_an_implicit_mute(self) -> None:
        a = _added(volume=0.0, muted=False)

        self.assertEqual(a.volume, 0.0)
        self.assertIs(a.muted, False)


# ---- Group B: badge semantics ----------------------------------------------


class AddedAudioBadgeTextTests(unittest.TestCase):
    # B1
    def test_b1_default_volume_has_no_badge(self) -> None:
        self.assertIsNone(added_audio_volume_badge(_added()))

    # B2
    def test_b2_half_volume_reads_as_percent(self) -> None:
        self.assertEqual(added_audio_volume_badge(_added(volume=0.5)), "50%")

    # B3
    def test_b3_boosted_volume_reads_as_percent(self) -> None:
        self.assertEqual(added_audio_volume_badge(_added(volume=1.25)), "125%")

    # B4
    def test_b4_zero_volume_reads_as_zero_percent_not_muted(self) -> None:
        self.assertEqual(added_audio_volume_badge(_added(volume=0.0)), "0%")

    # B5
    def test_b5_mute_wins_over_a_non_default_volume(self) -> None:
        self.assertEqual(
            added_audio_volume_badge(_added(volume=0.5, muted=True)), "Muted",
        )

    # B6
    def test_b6_mute_wins_over_the_default_volume(self) -> None:
        self.assertEqual(
            added_audio_volume_badge(_added(volume=1.0, muted=True)), "Muted",
        )

    # B7
    def test_b7_rounding_noise_around_full_volume_shows_no_badge(self) -> None:
        for v in (0.995, 1.0, 1.005):
            with self.subTest(volume=v):
                self.assertIsNone(added_audio_volume_badge(_added(volume=v)))


# ---- Group C: mute mutation + undo -----------------------------------------


class AddedAudioMuteMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio = _hittable(volume=0.5)
        self.win = _window([self.audio])
        self.addCleanup(self.win.close)
        _fake_outputs(self.win)

    def _live(self) -> AddedAudio:
        """The authoritative item, re-resolved: undo replaces the objects."""
        return next(a for a in self.win._added_audios if a.id == self.audio.id)

    # C1
    def test_c1_mute_sets_the_flag_and_keeps_the_stored_volume(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)

        self.assertIs(self._live().muted, True)
        self.assertEqual(self._live().volume, 0.5)

    # C2
    def test_c2_undo_restores_unmuted_at_the_same_volume(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)

        self.win._undo()

        self.assertIs(self._live().muted, False)
        self.assertEqual(self._live().volume, 0.5)

    # C3
    def test_c3_redo_restores_muted_at_the_same_volume(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)
        self.win._undo()

        self.win._redo()

        self.assertIs(self._live().muted, True)
        self.assertEqual(self._live().volume, 0.5)

    # C4
    def test_c4_unmute_keeps_the_stored_volume(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)

        self.win._on_added_audio_mute_requested(self.audio.id)

        self.assertIs(self._live().muted, False)
        self.assertEqual(self._live().volume, 0.5)

    # C5
    def test_c5_one_toggle_pushes_exactly_one_undo_entry(self) -> None:
        before = len(self.win._undo_stack)

        self.win._on_added_audio_mute_requested(self.audio.id)

        self.assertEqual(len(self.win._undo_stack), before + 1)

    def test_c5b_unmute_pushes_exactly_one_more_undo_entry(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)
        before = len(self.win._undo_stack)

        self.win._on_added_audio_mute_requested(self.audio.id)

        self.assertEqual(len(self.win._undo_stack), before + 1)

    def test_c6_an_unknown_audio_id_changes_nothing(self) -> None:
        before = len(self.win._undo_stack)

        self.win._on_added_audio_mute_requested("nope")

        self.assertEqual(len(self.win._undo_stack), before)
        self.assertIs(self._live().muted, False)


# ---- Group D: volume/mute independence -------------------------------------


class AddedAudioVolumeMuteIndependenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio = _hittable(volume=0.5)
        self.win = _window([self.audio])
        self.addCleanup(self.win.close)
        _fake_outputs(self.win)

    def _live(self) -> AddedAudio:
        return next(a for a in self.win._added_audios if a.id == self.audio.id)

    def _set_volume(self, pct: int) -> None:
        """Drive the real volume action, scripting only its modal dialog."""
        with unittest.mock.patch.object(
            app_mod.QInputDialog, "getInt",
            staticmethod(lambda *a, **k: (pct, True)),
        ):
            self.win._on_added_audio_volume_requested(self.audio.id)

    # D1
    def test_d1_changing_volume_while_muted_keeps_the_mute(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)

        self._set_volume(75)

        self.assertEqual(self._live().volume, 0.75)
        self.assertIs(self._live().muted, True)

    # D2
    def test_d2_unmuting_afterwards_exposes_the_edited_volume(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)
        self._set_volume(75)

        self.win._on_added_audio_mute_requested(self.audio.id)

        self.assertEqual(self._live().volume, 0.75)
        self.assertIs(self._live().muted, False)

    # D3
    def test_d3_setting_volume_to_zero_does_not_mute(self) -> None:
        self._set_volume(0)

        self.assertEqual(self._live().volume, 0.0)
        self.assertIs(self._live().muted, False)

    def test_d3b_setting_volume_to_zero_while_muted_stays_muted(self) -> None:
        self.win._on_added_audio_mute_requested(self.audio.id)

        self._set_volume(0)

        self.assertEqual(self._live().volume, 0.0)
        self.assertIs(self._live().muted, True)


# ---- Group E: preview ------------------------------------------------------


class AddedAudioPreviewGainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = _hittable(volume=0.5)
        self.b = _hittable(path=Path("b.mp3"), volume=1.2)
        self.win = _window([self.a, self.b])
        self.addCleanup(self.win.close)
        self.outs = _fake_outputs(self.win)

    def _live(self, audio: AddedAudio) -> AddedAudio:
        return next(a for a in self.win._added_audios if a.id == audio.id)

    # E1
    def test_e1_unmuted_item_uses_the_existing_gain_formula(self) -> None:
        self.win._update_audio_volumes()

        self.assertAlmostEqual(self.outs[self.a.id].volume(), 0.5)

    # E2
    def test_e2_muted_item_is_silent(self) -> None:
        self.win._on_added_audio_mute_requested(self.a.id)

        self.assertEqual(self.outs[self.a.id].volume(), 0.0)

    # E3
    def test_e3_unmute_returns_to_the_stored_gain_not_full(self) -> None:
        self.win._on_added_audio_mute_requested(self.a.id)

        self.win._on_added_audio_mute_requested(self.a.id)

        self.assertAlmostEqual(self.outs[self.a.id].volume(), 0.5)

    # E4
    def test_e4_muting_one_item_leaves_the_other_alone(self) -> None:
        self.win._on_added_audio_mute_requested(self.a.id)

        self.assertEqual(self.outs[self.a.id].volume(), 0.0)
        # 120% clamps to the QAudioOutput ceiling, exactly as before.
        self.assertAlmostEqual(self.outs[self.b.id].volume(), 1.0)
        self.assertIs(self._live(self.b).muted, False)

    # E5
    def test_e5_master_gain_cannot_override_mute(self) -> None:
        for gain in (0.4, 1.0, 2.5):
            with self.subTest(master_gain=gain):
                self.win.audio_gain.setValue(gain)
                self._live(self.a).muted = True

                self.win._update_audio_volumes()

                self.assertEqual(self.outs[self.a.id].volume(), 0.0)

    def test_e5b_master_gain_still_scales_an_unmuted_item(self) -> None:
        self.win.audio_gain.setValue(0.5)

        self.win._update_audio_volumes()

        self.assertAlmostEqual(self.outs[self.a.id].volume(), 0.25)


# ---- Group F: export -------------------------------------------------------


class AddedAudioExportGainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = _hittable(volume=0.5, offset=1.5, src_start=0.25, src_end=8.0)
        self.b = _hittable(path=Path("b.mp3"), volume=1.0, offset=0.0)
        self.win = _window([self.a, self.b])
        self.addCleanup(self.win.close)
        _fake_outputs(self.win)

    def _tracks(self):
        return self.win._build_added_audio_tracks()

    def _live(self, audio: AddedAudio) -> AddedAudio:
        return next(a for a in self.win._added_audios if a.id == audio.id)

    # F1
    def test_f1_unmuted_track_keeps_the_baseline_volume_product(self) -> None:
        self.win.audio_gain.setValue(1.5)

        self.assertAlmostEqual(self._tracks()[0].volume, 0.75)

    # F2
    def test_f2_muted_track_exports_at_zero_gain(self) -> None:
        self.win._on_added_audio_mute_requested(self.a.id)

        self.assertEqual(self._tracks()[0].volume, 0.0)

    def test_f2b_master_gain_cannot_rescue_a_muted_track(self) -> None:
        self.win.audio_gain.setValue(2.0)
        self.win._on_added_audio_mute_requested(self.a.id)

        self.assertEqual(self._tracks()[0].volume, 0.0)

    # F3
    def test_f3_building_the_export_does_not_mutate_the_model(self) -> None:
        self.win._on_added_audio_mute_requested(self.a.id)

        self._tracks()

        self.assertEqual(self._live(self.a).volume, 0.5)
        self.assertIs(self._live(self.a).muted, True)

    # F4
    def test_f4_only_the_muted_track_loses_its_gain(self) -> None:
        self.win._on_added_audio_mute_requested(self.a.id)

        tracks = self._tracks()

        self.assertEqual(tracks[0].volume, 0.0)
        self.assertAlmostEqual(tracks[1].volume, 1.0)

    # F5
    def test_f5_muting_leaves_placement_and_replacement_untouched(self) -> None:
        self.win.audio_replace_cb.setChecked(True)
        before = self._tracks()[0]

        self.win._on_added_audio_mute_requested(self.a.id)
        after = self._tracks()[0]

        self.assertEqual(after.replace, before.replace)
        self.assertEqual(after.offset, before.offset)
        self.assertEqual(after.duration, before.duration)
        self.assertEqual(after.src_start, before.src_start)
        self.assertEqual(after.original_volume, before.original_volume)
        self.assertIs(after.replace, True)


# ---- Group G: context-menu request -----------------------------------------


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


class AddedAudioMuteMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = TimelineWidget()
        self.addCleanup(self.w.close)
        self.w.resize(900, 320)
        self.w.set_pixels_per_second(20.0)
        self.w.set_added_audios([_hittable(volume=0.5)])
        # `set_added_audios` clones, so the widget's own copy is the object
        # its menu and painting actually see.
        self.audio = self.w._added_audios[0]
        self.mute_requests: list[str] = []
        self.volume_requests: list[str] = []
        self.w.addedAudioMuteRequested.connect(self.mute_requests.append)
        self.w.addedAudioVolumeRequested.connect(self.volume_requests.append)
        _MenuProbe.labels = []
        _MenuProbe.choose = ""
        patcher = unittest.mock.patch.object(tw, "QMenu", _MenuProbe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open(self, choose: str = "") -> list[str]:
        pos = self.w._added_audio_tile_rect(self.audio).center()
        _MenuProbe.choose = choose
        self.w._show_context_menu(pos, pos)
        return _MenuProbe.labels

    # G1
    def test_g1_unmuted_audio_offers_mute(self) -> None:
        labels = self._open()

        self.assertIn("Mute Audio", labels)
        self.assertNotIn("Unmute Audio", labels)

    # G2
    def test_g2_muted_audio_offers_unmute(self) -> None:
        self.audio.muted = True

        labels = self._open()

        self.assertIn("Unmute Audio", labels)
        self.assertNotIn("Mute Audio", labels)

    # G3
    def test_g3_choosing_mute_emits_the_request_without_mutating(self) -> None:
        self._open(choose="Mute Audio")

        self.assertEqual(self.mute_requests, [self.audio.id])
        # The widget must not own the model: MainWindow snapshots and writes.
        self.assertIs(self.audio.muted, False)

    def test_g3b_choosing_unmute_emits_the_same_request(self) -> None:
        self.audio.muted = True

        self._open(choose="Unmute Audio")

        self.assertEqual(self.mute_requests, [self.audio.id])
        self.assertIs(self.audio.muted, True)

    # G4
    def test_g4_the_existing_audio_actions_are_untouched(self) -> None:
        labels = self._open()

        for wanted in ("Volume...", "Replace original audio",
                       "Remove This Audio Clip"):
            self.assertIn(wanted, labels)

    def test_g4b_volume_action_still_dispatches(self) -> None:
        self._open(choose="Volume...")

        self.assertEqual(self.volume_requests, [self.audio.id])
        self.assertEqual(self.mute_requests, [])


# ---- Group H: timeline painting integration --------------------------------


class AddedAudioBadgePaintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = TimelineWidget()
        self.addCleanup(self.w.close)
        self.w.resize(900, 320)
        self.w.set_pixels_per_second(20.0)
        self.w.set_added_audios([_hittable(volume=1.0)])
        self.audio = self.w._added_audios[0]

    def _repaint(self) -> None:
        from PySide6.QtGui import QPixmap

        pm = QPixmap(self.w.size())
        self.w.render(pm)

    # H1
    def test_h1_default_item_paints_without_a_badge_decision(self) -> None:
        self.assertIsNone(added_audio_volume_badge(self.audio))
        self._repaint()

    # H2
    def test_h2_non_default_volume_paints_a_percent_badge(self) -> None:
        self.audio.volume = 0.5

        self.assertEqual(added_audio_volume_badge(self.audio), "50%")
        self._repaint()

    # H3
    def test_h3_muted_item_paints_the_muted_badge(self) -> None:
        self.audio.muted = True

        self.assertEqual(added_audio_volume_badge(self.audio), "Muted")
        self._repaint()

    # H4
    def test_h4_badge_does_not_change_tile_geometry(self) -> None:
        before = self.w._added_audio_tile_rect(self.audio)

        self.audio.volume = 0.5
        self.audio.muted = True
        self._repaint()

        self.assertEqual(self.w._added_audio_tile_rect(self.audio), before)

    # H5
    def test_h5_badge_does_not_change_hit_testing(self) -> None:
        tile = self.w._added_audio_tile_rect(self.audio)
        corner = tile.topRight() + tw.QPoint(-6, 6)
        before = self.w._hit_added_audio(corner, lane=self.audio.lane)

        self.audio.muted = True
        self._repaint()

        after = self.w._hit_added_audio(corner, lane=self.audio.lane)
        self.assertIs(before, self.audio)
        self.assertIs(after, self.audio)

    def test_h6_a_tile_too_narrow_for_a_badge_still_paints(self) -> None:
        self.audio.muted = True
        self.w.set_pixels_per_second(0.05)

        self._repaint()

        self.assertEqual(added_audio_volume_badge(self.audio), "Muted")


# ---- Group I: clone / snapshot ---------------------------------------------


class AddedAudioMuteSnapshotTests(unittest.TestCase):
    # I1
    def test_i1_clone_carries_mute_and_volume_together(self) -> None:
        c = _added(volume=0.4, muted=True).clone()

        self.assertIs(c.muted, True)
        self.assertEqual(c.volume, 0.4)

    def test_i1b_clone_carries_an_unmuted_zero_volume_item(self) -> None:
        c = _added(volume=0.0, muted=False).clone()

        self.assertIs(c.muted, False)
        self.assertEqual(c.volume, 0.0)

    def test_i1c_split_carries_mute_to_the_new_right_hand_piece(self) -> None:
        a = _added(volume=0.4, muted=True)

        right = split_added_audio(a, 6.0)

        self.assertIsNotNone(right)
        self.assertIs(right.muted, True)
        self.assertEqual(right.volume, 0.4)

    # I2
    def test_i2_snapshot_restore_returns_mute_and_volume_independently(self) -> None:
        audio = _hittable(volume=0.4)
        win = _window([audio])
        self.addCleanup(win.close)
        _fake_outputs(win)

        snap = win._current_state_snap()
        live = next(a for a in win._added_audios if a.id == audio.id)
        live.muted = True
        live.volume = 1.0
        win._apply_state(snap)

        restored = next(a for a in win._added_audios if a.id == audio.id)
        self.assertIs(restored.muted, False)
        self.assertEqual(restored.volume, 0.4)


# ---- Group J: structural exclusions ----------------------------------------


class MuteStaysOutOfTheTransportAndClipLayerTests(unittest.TestCase):
    """Tab 2G must not reintroduce the stale PR #9 transport / per-clip volume
    architecture. These are GREEN regression guards, not new behaviour."""

    def test_j1_no_transport_volume_widgets_exist(self) -> None:
        win = _window()
        self.addCleanup(win.close)

        for name in ("vol_slider", "vol_label", "vol_mute_btn"):
            with self.subTest(widget=name):
                self.assertFalse(hasattr(win, name))

    def test_j2_timeline_has_no_per_clip_audio_volume_signal(self) -> None:
        w = TimelineWidget()
        self.addCleanup(w.close)

        self.assertFalse(hasattr(w, "clipAudioVolumeChanged"))
        self.assertFalse(hasattr(w, "addedAudioVolumeChanged"))

    def test_j3_mute_flows_through_a_request_signal_not_a_model_write(self) -> None:
        w = TimelineWidget()
        self.addCleanup(w.close)

        self.assertTrue(hasattr(w, "addedAudioMuteRequested"))


if __name__ == "__main__":
    unittest.main()
