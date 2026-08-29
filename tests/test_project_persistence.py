"""Tab 2N: the .coveproj save / open foundation.

The dominant goal is recoverability: a user saves the current editing
session, closes Cove, reopens the project and gets the same *editable*
timeline back. Everything here is asserted through the real model classes
(``MediaAsset`` / ``Clip`` / ``AddedAudio`` / ``SubtitleTrack``) and a real
``MainWindow`` - a parallel dict-shaped "project model" would prove the
serializer round-trips itself rather than the app.

Two properties carry the real risk and get the most coverage:

* **Transactional open.** A malformed, unsupported, structurally invalid
  or media-missing project must leave the current session *byte for byte*
  as it was. Half-loading is the failure mode that loses a user's work,
  so every rejection path is asserted against a populated project A.
* **Non-destructive save.** A failure at the write/commit seam must leave
  the previous project file loadable. The save goes through ``QSaveFile``,
  so the tests force a real ``commit()`` failure rather than mocking the
  whole write away.

Media files are zero-byte temp files: load-time validation only asks
whether the referenced source still exists, and every piece of metadata
the models need is inside the project document. That keeps the suite free
of ffmpeg and deterministic.

Qt runs on the ``offscreen`` platform and the background NVENC/AMF probe
is suppressed for every window - it spawns ffmpeg children that outlive
the window and no persistence behaviour depends on encoder capabilities.
"""
from __future__ import annotations

import gc
import json
import os
import tempfile
import threading
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (  # noqa: E402
    QObject,
    QSaveFile,
    Qt,
    QThread,
    QUrl,
    Signal,
)
import shiboken6  # noqa: E402

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import project_io  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402
from cove_video_editor.clip import (  # noqa: E402
    IMAGE_ASSET_DURATION_CAP,
    AddedAudio,
    Clip,
    MediaAsset,
    SubtitleTrack,
)

_app: QApplication | None = None

FREE = "Free (Custom)"
TIKTOK = "9:16 (TikTok / Reels / Shorts)"
SQUARE = "1:1 (Square / Instagram)"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --- helpers -----------------------------------------------------------


class _TempTree(unittest.TestCase):
    """Base class giving each test its own temp directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def media(self, name: str) -> Path:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        return p

    def proj(self, name: str = "p.coveproj") -> Path:
        return self.tmp / name


def _asset(path: Path, *, kind: str = "video", w: int = 1920, h: int = 1080,
           duration: float = 600.0, fps: float = 30.0,
           has_audio: bool = True) -> MediaAsset:
    return MediaAsset(
        path=path, duration=duration, width=w, height=h, fps=fps,
        has_audio=has_audio, kind=kind,
    )


def _state(**kw) -> project_io.ProjectState:
    base = dict(
        assets=[], clips=[], added_audios=[], subtitles=[],
        replace_added_audio=False, added_gain=1.0, original_gain=1.0,
    )
    base.update(kw)
    return project_io.ProjectState(**base)


# Captured before `_WinCase` starts patching, so a test can put the real
# implementation back for the one case that needs it.
_REAL_KICK_OFF_THUMBS = MainWindow._kick_off_thumbs


class _FakeAnalysisWorker(QObject):
    """A ``ThumbnailWorker`` stand-in with the production success shape.

    Same two signals, same ``cancel()``, same "``run`` on the worker
    thread" contract - but it emits only when the test says so, which is
    what makes queued-callback ordering assertable. Doing nothing in
    ``run`` leaves the thread sitting in its event loop exactly like a
    worker still generating frames.
    """

    finished = Signal(str, list)
    failed = Signal(str, str)

    def __init__(self, clip_id: str) -> None:
        super().__init__()
        self._id = clip_id
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def run(self) -> None:
        pass


class _FakeWaveWorker(QObject):
    """``WaveformWorker``'s signal shape, for the result-path guard."""

    finished = Signal(str, list, int)
    failed = Signal(str, str)

    def cancel(self) -> None:
        pass


def _fake_start_thumbnails(clip_id, video, duration, count=24):  # noqa: ANN001, ANN202
    """Mirrors ``thumbnails.start_thumbnails`` wiring exactly."""
    thread = QThread()
    worker = _FakeAnalysisWorker(clip_id)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker


class _WinCase(_TempTree):
    """Temp tree plus MainWindow lifecycle.

    The media-analysis workers are off for the whole test: they are
    cosmetic (thumbnail strips, waveform peaks), each one spawns an
    ffmpeg child, and the persistence contract is about model state, so
    letting real ffmpeg processes outlive a test would only make the
    suite slow and flaky.
    """

    def setUp(self) -> None:
        super().setUp()
        for name, repl in (
            ("_start_encoder_probe", lambda self: None),
            ("_kick_off_thumbs", lambda self, clip: None),
            ("_kick_off_waveform", lambda self, clip: None),
            ("_kick_off_added_waveform", lambda self, aid, path: None),
        ):
            p = unittest.mock.patch.object(MainWindow, name, repl)
            p.start()
            self.addCleanup(p.stop)
        # Save/open failures report through a modal, same as an import or
        # export failure. Captured rather than suppressed so the error
        # tests can assert on what the user was actually told.
        warn = unittest.mock.patch.object(
            app_mod.QMessageBox, "warning", return_value=None)
        self.warned = warn.start()
        self.addCleanup(warn.stop)
        # `_SURVIVING_MEDIA_THREADS` is module state by design, so leave
        # it exactly as it was found: a survivor left running would leak
        # into the next test's assertions and, worse, outlive the suite.
        self.addCleanup(self._drain_surviving_threads)

    @staticmethod
    def _drain_surviving_threads() -> None:
        for thread, _worker in list(app_mod._SURVIVING_MEDIA_THREADS):
            thread.quit()
            thread.wait(5000)
        for _ in range(20):
            QApplication.processEvents()
        app_mod._SURVIVING_MEDIA_THREADS.clear()

    def win(self) -> MainWindow:
        w = MainWindow()
        self.addCleanup(self._dispose, w)
        return w

    @staticmethod
    def _dispose(w: MainWindow) -> None:
        """Release a window's media objects before dropping it.

        These cases build more ``MainWindow``s than any other module, and
        each added-audio entry brings its own ``QMediaPlayer`` pointed at
        a fixture file. Stopping and detaching them keeps the number of
        live decoder backends bounded to one test at a time instead of
        the whole module.
        """
        if w._play_timer.isActive():
            w._play_timer.stop()
        for player in (w.player, w.clip_audio_player,
                       *w._added_players.values()):
            player.stop()
            player.setSource(QUrl())
        for audio_id in list(w._added_players):
            w._destroy_added_player(audio_id)
        w.close()

    def populated(self, w: MainWindow) -> dict:
        """Give ``w`` a representative session: two edited video clips (one
        cropped), one image, one added-audio entry and one subtitle."""
        v1 = _asset(self.media("one.mp4"))
        v2 = _asset(self.media("two.mp4"), w=1280, h=720, fps=25.0)
        img = _asset(self.media("still.png"), kind="image", w=800, h=600,
                     fps=0.0, has_audio=False, duration=600.0)
        for a in (v1, v2, img):
            w._assets[a.id] = a
            w.clip_bin.add_asset(a)
        c1 = Clip(asset=v1, timeline_start=0.0, src_start=2.5, src_end=9.0,
                  speed=1.5, muted=True, audio_volume=0.4,
                  linked_audio=False, audio_offset=-0.25,
                  crop_rect=(0.1, 0.2, 0.5, 0.6), crop_preset=TIKTOK)
        c2 = Clip(asset=v2, timeline_start=12.0, src_start=1.0, src_end=4.0,
                  audio_removed=True, crop_preset=SQUARE)
        c3 = Clip(asset=img, timeline_start=20.0, src_start=0.0, src_end=5.0)
        w._clips = [c1, c2, c3]
        w.timeline.set_clips(w._clips)
        aud = AddedAudio(path=self.media("bed.mp3"), duration=30.0, rate=44100,
                         offset=3.0, lane=0, src_start=1.0, src_end=11.0,
                         volume=0.75, muted=True)
        w._added_audios = [aud]
        w._create_added_player(aud)
        w._refresh_added_audio_display()
        sub = SubtitleTrack(path=self.media("cap.srt"), font_size=44,
                            primary_color="#FF0000", position="top",
                            active=True, offset_ms=250)
        w._subs = [sub]
        w.clip_bin.add_sub(sub.id, sub.path.name, str(sub.path), True)
        w.audio_replace_cb.setChecked(True)
        w.audio_gain.setValue(1.4)
        w.orig_gain.setValue(0.6)
        return {"clips": [c1, c2, c3], "audio": aud, "sub": sub,
                "assets": [v1, v2, img]}


CLIP_FIELDS = (
    "timeline_start", "src_start", "src_end", "speed", "muted",
    "audio_volume", "linked_audio", "audio_offset", "audio_removed",
    "crop_rect", "crop_preset", "id",
)
AUDIO_FIELDS = (
    "duration", "rate", "offset", "lane", "src_start", "src_end",
    "volume", "muted", "id",
)
ASSET_FIELDS = ("duration", "width", "height", "fps", "has_audio", "kind", "id")
SUB_FIELDS = (
    "font_family", "font_size", "primary_color", "outline_color", "outline",
    "position", "active", "offset_ms", "id",
)


def _assert_clip_equal(case: unittest.TestCase, got: Clip, want: Clip) -> None:
    case.assertEqual(got.asset.path, want.asset.path)
    for f in CLIP_FIELDS:
        case.assertEqual(getattr(got, f), getattr(want, f), f)


def _round_trip(state: project_io.ProjectState) -> project_io.ProjectState:
    doc = project_io.serialize_project(state)
    return project_io.deserialize_project(json.loads(json.dumps(doc)))


# --- Group A: schema ---------------------------------------------------


class SchemaTests(_TempTree):
    def _doc(self) -> dict:
        return project_io.serialize_project(_state())

    def test_a1_valid_v1_document_accepted(self) -> None:
        doc = self._doc()
        self.assertEqual(doc["format"], "cove-video-editor-project")
        self.assertEqual(doc["version"], 1)
        state = project_io.deserialize_project(doc)
        self.assertEqual(state.clips, [])

    def test_a2_format_marker_required(self) -> None:
        for bad in ({}, {"version": 1}, {"format": "something-else", "version": 1}):
            with self.subTest(doc=bad):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(bad)

    def test_a3_version_required(self) -> None:
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(
                {"format": "cove-video-editor-project"})

    def test_a4_unsupported_version_rejected(self) -> None:
        for bad in (0, 2, 99, -1):
            with self.subTest(version=bad):
                doc = self._doc()
                doc["version"] = bad
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(doc)

    def test_a4_non_integer_version_rejected(self) -> None:
        for bad in ("1", 1.0, True, None):
            with self.subTest(version=bad):
                doc = self._doc()
                doc["version"] = bad
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(doc)

    def test_a5_malformed_json_rejected(self) -> None:
        p = self.proj()
        p.write_text("{not json", encoding="utf-8")
        with self.assertRaises(project_io.ProjectError):
            project_io.load_project(p)

    def test_a5_missing_file_rejected(self) -> None:
        with self.assertRaises(project_io.ProjectError):
            project_io.load_project(self.tmp / "nope.coveproj")

    def test_a6_wrong_top_level_type_rejected(self) -> None:
        for bad in ([], "x", 3, None):
            with self.subTest(doc=bad):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(bad)

    def test_a6_wrong_field_types_rejected(self) -> None:
        cases = {
            "assets not a list": {"assets": {}},
            "clips not a list": {"clips": "x"},
            "added_audio not a list": {"added_audio": 3},
            "subtitles not a list": {"subtitles": 0},
            "asset not a mapping": {"assets": ["x"]},
            "clip not a mapping": {"clips": [7]},
            "audio_mix not a mapping": {"audio_mix": []},
        }
        for name, patch in cases.items():
            with self.subTest(name):
                doc = self._doc()
                doc.update(patch)
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(doc)

    def test_a6_clip_numeric_fields_must_be_numeric(self) -> None:
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(
            _state(assets=[a], clips=[Clip(asset=a, src_end=5.0)]))
        for field in ("timeline_start", "src_start", "src_end", "speed"):
            with self.subTest(field):
                bad = json.loads(json.dumps(doc))
                bad["clips"][0][field] = "soon"
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(bad)

    def test_a6_clip_asset_reference_must_resolve(self) -> None:
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(
            _state(assets=[a], clips=[Clip(asset=a, src_end=5.0)]))
        doc["clips"][0]["asset_id"] = "deadbeef"
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(doc)

    def test_a6_duplicate_ids_rejected(self) -> None:
        """Clip, added-audio and subtitle ids are used as unique keys for
        worker, player and selection state, so an ambiguous document has
        to be refused during validation rather than half-applied."""
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_end=3.0), Clip(asset=a, src_end=4.0)],
            added_audios=[
                AddedAudio(path=self.media("x.mp3"), duration=5.0),
                AddedAudio(path=self.media("y.mp3"), duration=5.0),
            ],
            subtitles=[
                SubtitleTrack(path=self.media("a.srt")),
                SubtitleTrack(path=self.media("b.srt")),
            ],
        ))
        # The un-mutated document must load: these ids are distinct.
        project_io.deserialize_project(json.loads(json.dumps(doc)))
        for key in ("clips", "added_audio", "subtitles", "assets"):
            with self.subTest(key):
                bad = json.loads(json.dumps(doc))
                if key == "assets":
                    bad["assets"].append(dict(bad["assets"][0]))
                else:
                    bad[key][1]["id"] = bad[key][0]["id"]
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(bad)

    def test_a6_non_finite_numbers_rejected(self) -> None:
        """Python's JSON decoder happily produces ``NaN`` / ``Infinity``.
        An infinite timeline position survives loading and only explodes
        later, when timeline geometry converts it to an int - by which
        point the previous session is already gone."""
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_end=5.0, crop_rect=(0.0, 0.0, 1.0, 1.0))],
            added_audios=[AddedAudio(path=self.media("x.mp3"), duration=5.0)],
        ))
        raw = json.dumps(doc)
        targets = (
            ("clips", 0, "timeline_start"),
            ("clips", 0, "src_end"),
            ("clips", 0, "speed"),
            ("clips", 0, "audio_volume"),
            ("added_audio", 0, "offset"),
            ("added_audio", 0, "volume"),
        )
        for literal in ("NaN", "Infinity", "-Infinity"):
            for key, idx, field in targets:
                with self.subTest(literal=literal, field=f"{key}.{field}"):
                    bad = json.loads(raw)
                    bad[key][idx][field] = float(literal)
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(bad)
            with self.subTest(literal=literal, field="crop_rect"):
                bad = json.loads(raw)
                bad["clips"][0]["crop_rect"][2] = float(literal)
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(bad)
            with self.subTest(literal=literal, field="audio_mix.added_gain"):
                bad = json.loads(raw)
                bad["audio_mix"]["added_gain"] = float(literal)
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(bad)

    def test_a6_non_finite_numbers_rejected_from_a_real_file(self) -> None:
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(
            _state(assets=[a], clips=[Clip(asset=a, src_end=5.0)]))
        doc["clips"][0]["timeline_start"] = "__INF__"
        p = self.proj()
        p.write_text(json.dumps(doc).replace('"__INF__"', "Infinity"),
                     encoding="utf-8")
        with self.assertRaises(project_io.ProjectError):
            project_io.load_project(p)

    def test_saving_a_non_finite_value_fails_instead_of_writing_it(
        self,
    ) -> None:
        """A document containing ``Infinity`` is not loadable by anything
        strict, so writing one is worse than refusing the save."""
        a = _asset(self.media("v.mp4"))
        clip = Clip(asset=a, src_end=5.0)
        clip.timeline_start = float("inf")
        p = self.proj()
        with self.assertRaises(project_io.ProjectError):
            project_io.save_project(p, _state(assets=[a], clips=[clip]))
        self.assertFalse(p.exists())

    def test_a6_out_of_range_times_rejected_at_the_boundary(self) -> None:
        """The ceiling is a real boundary, not a blanket refusal: the
        largest allowed value must still load."""
        a = _asset(self.media("v.mp4"))
        cap = project_io.MAX_TIME_SECONDS
        doc = project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_end=5.0)],
            added_audios=[AddedAudio(path=self.media("x.mp3"), duration=5.0)],
        ))
        raw = json.dumps(doc)

        at_cap = json.loads(raw)
        at_cap["clips"][0]["timeline_start"] = cap
        self.assertEqual(
            project_io.deserialize_project(at_cap).clips[0].timeline_start,
            cap)

        fields = (
            ("clips", "timeline_start"), ("clips", "src_start"),
            ("clips", "src_end"), ("clips", "audio_offset"),
            ("added_audio", "offset"), ("added_audio", "src_start"),
            ("added_audio", "src_end"), ("added_audio", "duration"),
        )
        for key, field in fields:
            for value in (cap * 1.001, -cap * 1.001, 1e308):
                with self.subTest(field=f"{key}.{field}", value=value):
                    bad = json.loads(raw)
                    bad[key][0][field] = value
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(bad)

    def test_a6_out_of_range_asset_duration_rejected(self) -> None:
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(_state(assets=[a]))
        doc["assets"][0]["duration"] = 1e308
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(doc)

    def _audio_doc(self, lane: object) -> dict:
        doc = project_io.serialize_project(_state(
            added_audios=[AddedAudio(path=self.media("x.mp3"), duration=5.0)]))
        doc["added_audio"][0]["lane"] = lane
        return doc

    def test_a6_lane_boundaries_accepted(self) -> None:
        """The cap is a real boundary: the lowest lane, an ordinary lane
        and the highest allowed lane all still load."""
        cap = project_io.MAX_AUDIO_LANE
        for lane in (0, 1, cap):
            with self.subTest(lane=lane):
                state = project_io.deserialize_project(self._audio_doc(lane))
                self.assertEqual(state.added_audios[0].lane, lane)

    def test_a6_out_of_range_lane_rejected(self) -> None:
        """An unbounded lane is not a display quirk: the timeline grows
        its lane-height list to `lane + 2` entries, so a corrupt value
        allocates without limit - and it does so after the outgoing
        session has already been replaced."""
        cap = project_io.MAX_AUDIO_LANE
        for lane in (cap + 1, 1_000_000_000, -1, -1_000_000_000):
            with self.subTest(lane=lane):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(self._audio_doc(lane))

    def test_a6_lane_wrong_type_rejected(self) -> None:
        for lane in ("1", 1.5, True, None, [1]):
            with self.subTest(lane=lane):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(self._audio_doc(lane))

    def test_a6_lane_cap_is_deliberately_small(self) -> None:
        """A cap large enough to still allocate unusably is not a fix."""
        self.assertIsInstance(project_io.MAX_AUDIO_LANE, int)
        self.assertGreaterEqual(project_io.MAX_AUDIO_LANE, 2)
        self.assertLessEqual(project_io.MAX_AUDIO_LANE, 256)

    def _clip_doc(self, **fields: object) -> dict:
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(
            _state(assets=[a], clips=[Clip(asset=a, src_end=5.0)]))
        doc["clips"][0].update(fields)
        return doc

    def test_a6_clip_speed_range_accepted(self) -> None:
        """The bounds are the properties dialog's own
        ``setRange(0.25, 4.0)``, so every speed the editor can produce
        must survive."""
        for speed in (project_io.MIN_CLIP_SPEED, 0.5, 1.0, 2.0,
                      project_io.MAX_CLIP_SPEED):
            with self.subTest(speed=speed):
                state = project_io.deserialize_project(
                    self._clip_doc(speed=speed))
                self.assertEqual(state.clips[0].speed, speed)

    def test_a6_invalid_clip_speed_rejected(self) -> None:
        """Zero is the sharp one: the exporter builds
        ``setpts={1.0/speed}*PTS``. A non-positive speed also defeats the
        time bound, because `timeline_length` divides by
        ``max(0.01, speed)`` and can then exceed MAX_TIME_SECONDS."""
        for speed in (0.0, -1.0, -0.5, 0.24, 4.01, 1e6):
            with self.subTest(speed=speed):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(self._clip_doc(speed=speed))

    def test_a6_crop_rect_invariant_accepted(self) -> None:
        """Every rectangle the crop lifecycle can commit, including the
        exact full frame and an edge-flush rect, must still load."""
        for rect in ([0.0, 0.0, 1.0, 1.0], [0.1, 0.2, 0.5, 0.6],
                     [0.21875, 0.0, 0.5625, 1.0], [0.5, 0.5, 0.5, 0.5]):
            with self.subTest(rect=rect):
                state = project_io.deserialize_project(
                    self._clip_doc(crop_rect=rect))
                self.assertEqual(state.clips[0].crop_rect, tuple(rect))

    def test_a6_crop_rect_outside_the_frame_rejected(self) -> None:
        """`Clip.crop_rect` documents a normalized invariant; preview and
        export disagree about what an out-of-domain rect means, so it is
        refused rather than silently reinterpreted."""
        bad = (
            [-0.1, 0.0, 0.5, 0.5],    # negative origin
            [0.0, -0.1, 0.5, 0.5],
            [0.0, 0.0, 0.0, 0.5],     # zero extent
            [0.0, 0.0, 0.5, 0.0],
            [0.0, 0.0, -0.5, 0.5],    # negative extent
            [0.0, 0.0, 1.5, 0.5],     # extent past the frame
            [0.0, 0.0, 0.5, 1.5],
            [0.6, 0.0, 0.5, 0.5],     # x + w past the right edge
            [0.0, 0.6, 0.5, 0.5],     # y + h past the bottom edge
        )
        for rect in bad:
            with self.subTest(rect=rect):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(
                        self._clip_doc(crop_rect=rect))

    def test_a6_crop_rect_shape_validated(self) -> None:
        a = _asset(self.media("v.mp4"))
        doc = project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_end=5.0, crop_rect=(0.0, 0.0, 1.0, 1.0))],
        ))
        for bad in ([0.0, 0.0, 1.0], [0.0, 0.0, 1.0, 1.0, 1.0], "0,0,1,1",
                    [0.0, 0.0, "1", 1.0]):
            with self.subTest(rect=bad):
                doc2 = json.loads(json.dumps(doc))
                doc2["clips"][0]["crop_rect"] = bad
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(doc2)

    def test_written_document_is_utf8_json(self) -> None:
        p = self.proj("ünïcode.coveproj")
        project_io.save_project(p, _state())
        raw = p.read_bytes()
        doc = json.loads(raw.decode("utf-8"))
        self.assertEqual(doc["format"], "cove-video-editor-project")


# --- Group B: empty project -------------------------------------------


class EmptyProjectTests(_WinCase):
    def test_b1_empty_session_saves(self) -> None:
        w = self.win()
        p = self.proj()
        self.assertTrue(w._save_project_to(p))
        self.assertTrue(p.is_file())

    def test_b2_empty_project_opens(self) -> None:
        w = self.win()
        p = self.proj()
        w._save_project_to(p)
        w2 = self.win()
        self.assertTrue(w2._open_project_path(p))
        self.assertEqual(w2._clips, [])
        self.assertEqual(w2._added_audios, [])
        self.assertEqual(w2._assets, {})

    def test_b3_round_trip_remains_empty(self) -> None:
        state = _round_trip(_state())
        self.assertEqual(
            (state.assets, state.clips, state.added_audios, state.subtitles),
            ([], [], [], []))


# --- Group C: visual clip round-trip -----------------------------------


class ClipRoundTripTests(_TempTree):
    def test_c_every_editable_clip_field_survives(self) -> None:
        a = _asset(self.media("v.mp4"))
        want = Clip(
            asset=a, timeline_start=3.25, src_start=1.5, src_end=8.75,
            speed=0.5, muted=True, audio_volume=0.35, linked_audio=False,
            audio_offset=-0.4, audio_removed=True,
            crop_rect=(0.125, 0.25, 0.5, 0.5), crop_preset=TIKTOK,
        )
        got = _round_trip(_state(assets=[a], clips=[want])).clips[0]
        self.assertIsInstance(got, Clip)
        _assert_clip_equal(self, got, want)

    def test_c_confirmed_crop_none_stays_none(self) -> None:
        a = _asset(self.media("v.mp4"))
        want = Clip(asset=a, src_end=4.0)
        self.assertIsNone(want.crop_rect)
        got = _round_trip(_state(assets=[a], clips=[want])).clips[0]
        self.assertIsNone(got.crop_rect)
        self.assertEqual(got.crop_preset, FREE)

    def test_c_crop_rect_is_a_plain_tuple(self) -> None:
        a = _asset(self.media("v.mp4"))
        got = _round_trip(_state(
            assets=[a],
            clips=[Clip(asset=a, src_end=4.0, crop_rect=(0.1, 0.2, 0.3, 0.4))],
        )).clips[0]
        self.assertIsInstance(got.crop_rect, tuple)
        self.assertEqual(got.crop_rect, (0.1, 0.2, 0.3, 0.4))

    def test_c_clip_defaults_survive_untouched(self) -> None:
        """The default construction path is the common case: a clip built
        with nothing but an asset must come back byte-identical."""
        a = _asset(self.media("v.mp4"))
        want = Clip(asset=a)
        got = _round_trip(_state(assets=[a], clips=[want])).clips[0]
        _assert_clip_equal(self, got, want)
        self.assertEqual(got.src_end, a.duration)

    def test_c_asset_metadata_survives(self) -> None:
        a = _asset(self.media("v.mp4"), w=3840, h=2160, fps=59.94,
                   duration=12.5)
        got = _round_trip(_state(assets=[a])).assets[0]
        self.assertIsInstance(got, MediaAsset)
        self.assertEqual(got.path, a.path)
        for f in ASSET_FIELDS:
            self.assertEqual(getattr(got, f), getattr(a, f), f)

    def test_c_clip_shares_the_deserialized_asset_object(self) -> None:
        a = _asset(self.media("v.mp4"))
        state = _round_trip(_state(assets=[a], clips=[Clip(asset=a)]))
        self.assertIs(state.clips[0].asset, state.assets[0])


# --- Group D: multiple clips -------------------------------------------


class MultipleClipTests(_TempTree):
    def test_d_count_order_identity_and_edits_survive(self) -> None:
        a1 = _asset(self.media("a.mp4"))
        a2 = _asset(self.media("b.mp4"), w=1280, h=720)
        a3 = _asset(self.media("c.mp4"))
        clips = [
            Clip(asset=a1, timeline_start=0.0, src_start=0.0, src_end=4.0,
                 crop_preset=SQUARE, crop_rect=(0.0, 0.1, 0.8, 0.8)),
            Clip(asset=a2, timeline_start=4.0, src_start=2.0, src_end=6.0,
                 speed=2.0),
            Clip(asset=a3, timeline_start=11.0, src_start=1.0, src_end=3.0,
                 muted=True, audio_volume=0.1),
        ]
        got = _round_trip(_state(assets=[a1, a2, a3], clips=clips)).clips
        self.assertEqual(len(got), 3)
        self.assertEqual([c.asset.path.name for c in got],
                         ["a.mp4", "b.mp4", "c.mp4"])
        self.assertEqual([c.timeline_start for c in got], [0.0, 4.0, 11.0])
        for g, w in zip(got, clips):
            _assert_clip_equal(self, g, w)

    def test_d_two_clips_on_one_asset_stay_independent(self) -> None:
        a = _asset(self.media("a.mp4"))
        clips = [
            Clip(asset=a, timeline_start=0.0, src_start=0.0, src_end=3.0,
                 crop_rect=(0.0, 0.0, 0.5, 0.5), crop_preset=SQUARE),
            Clip(asset=a, timeline_start=3.0, src_start=9.0, src_end=13.0,
                 crop_rect=None, speed=1.25),
        ]
        got = _round_trip(_state(assets=[a], clips=clips)).clips
        self.assertIs(got[0].asset, got[1].asset)
        self.assertEqual(got[0].crop_rect, (0.0, 0.0, 0.5, 0.5))
        self.assertIsNone(got[1].crop_rect)
        self.assertEqual(got[1].speed, 1.25)
        self.assertNotEqual(got[0].id, got[1].id)


# --- Group E: added audio ----------------------------------------------


class AddedAudioTests(_TempTree):
    def test_e_every_editable_field_survives(self) -> None:
        want = AddedAudio(
            path=self.media("bed.mp3"), duration=42.0, rate=48000,
            offset=6.5, lane=0, src_start=2.0, src_end=17.5,
            volume=0.65, muted=True,
        )
        got = _round_trip(_state(added_audios=[want])).added_audios[0]
        self.assertIsInstance(got, AddedAudio)
        self.assertEqual(got.path, want.path)
        for f in AUDIO_FIELDS:
            self.assertEqual(getattr(got, f), getattr(want, f), f)

    def test_e_defaults_survive_untouched(self) -> None:
        want = AddedAudio(path=self.media("bed.mp3"), duration=9.0)
        got = _round_trip(_state(added_audios=[want])).added_audios[0]
        for f in AUDIO_FIELDS:
            self.assertEqual(getattr(got, f), getattr(want, f), f)
        self.assertFalse(got.muted)
        self.assertEqual(got.volume, 1.0)
        self.assertEqual(got.lane, 1)

    def test_e_muted_is_independent_of_zero_volume(self) -> None:
        """``muted`` and ``volume == 0`` are different states in the model
        (unmuting restores the stored gain), so they must not collapse."""
        silent = AddedAudio(path=self.media("a.mp3"), duration=5.0,
                            volume=0.0, muted=False)
        muted = AddedAudio(path=self.media("b.mp3"), duration=5.0,
                           volume=0.8, muted=True)
        got = _round_trip(_state(added_audios=[silent, muted])).added_audios
        self.assertEqual((got[0].volume, got[0].muted), (0.0, False))
        self.assertEqual((got[1].volume, got[1].muted), (0.8, True))

    def test_e_order_and_count_survive(self) -> None:
        items = [
            AddedAudio(path=self.media(f"{i}.mp3"), duration=5.0,
                       offset=float(i * 5), lane=i % 2)
            for i in range(4)
        ]
        got = _round_trip(_state(added_audios=items)).added_audios
        self.assertEqual([a.path.name for a in got],
                         [a.path.name for a in items])
        self.assertEqual([a.lane for a in got], [a.lane for a in items])


# --- Group F: media / source bin ---------------------------------------


class MediaBinTests(_WinCase):
    """``MainWindow._assets`` is authoritative imported-media state that is
    *not* derivable from the timeline: a bin entry with no clip on the
    timeline is a normal state after browse-import (``_import_paths`` is
    called with ``append_to_timeline=False``). So Group F applies."""

    def test_f_bin_only_asset_survives_round_trip(self) -> None:
        w = self.win()
        a = _asset(self.media("unused.mp4"))
        w._assets[a.id] = a
        w.clip_bin.add_asset(a)
        p = self.proj()
        self.assertTrue(w._save_project_to(p))

        w2 = self.win()
        self.assertTrue(w2._open_project_path(p))
        self.assertEqual(list(w2._assets), [a.id])
        got = w2._assets[a.id]
        self.assertEqual(got.path, a.path)
        for f in ASSET_FIELDS:
            self.assertEqual(getattr(got, f), getattr(a, f), f)
        self.assertEqual(w2._clips, [])
        self.assertEqual(w2.clip_bin.video_list.count(), 1)

    def test_f_bin_widget_is_rebuilt_not_appended(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj()
        w._save_project_to(p)
        self.assertEqual(w.clip_bin.video_list.count(), 2)
        w._open_project_path(p)
        self.assertEqual(w.clip_bin.video_list.count(), 2)
        self.assertEqual(w.clip_bin.image_list.count(), 1)
        self.assertEqual(w.clip_bin.subs_list.count(), 1)

    def test_f_subtitle_library_survives(self) -> None:
        w = self.win()
        want = self.populated(w)["sub"]
        p = self.proj()
        w._save_project_to(p)
        w2 = self.win()
        w2._open_project_path(p)
        self.assertEqual(len(w2._subs), 1)
        got = w2._subs[0]
        self.assertEqual(got.path, want.path)
        for f in SUB_FIELDS:
            self.assertEqual(getattr(got, f), getattr(want, f), f)

    def test_f_audio_mix_settings_survive(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj()
        w._save_project_to(p)
        w2 = self.win()
        w2._open_project_path(p)
        self.assertTrue(w2.audio_replace_cb.isChecked())
        self.assertAlmostEqual(w2.audio_gain.value(), 1.4)
        self.assertAlmostEqual(w2.orig_gain.value(), 0.6)


# --- Group G: transient exclusion --------------------------------------


class TransientExclusionTests(_WinCase):
    def _doc(self) -> dict:
        w = self.win()
        self.populated(w)
        w.timeline.select_clip(w._clips[0].id)
        w.timeline.set_playhead(7.5, emit=False)
        w._set_last_export_output(self.media("prev-export.mp4"))
        w._region_export_range = (1.0, 2.0)
        p = self.proj()
        w._save_project_to(p)
        return json.loads(p.read_text(encoding="utf-8"))

    def test_g_no_transient_keys_anywhere_in_the_document(self) -> None:
        doc = self._doc()
        banned = {
            "selected_id", "selected_index", "selection", "selected",
            "playhead", "playing", "playback_position", "preview_clip_id",
            "export_progress", "progress", "export_process", "export_temp",
            "temp", "last_export_output", "show_in_folder", "show_folder",
            "crop_draft", "crop_edit", "undo", "redo", "undo_stack",
            "redo_stack", "status", "log", "encoder_caps", "geometry",
            "region_export_range", "cancelled",
        }
        seen: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                seen.update(node.keys())
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(doc)
        self.assertEqual(seen & banned, set())

    def test_g_previous_export_path_is_not_serialized(self) -> None:
        """Semantic, not a key check: the export target must not appear as
        a value either."""
        doc = self._doc()
        values: list[object] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            else:
                values.append(node)

        walk(doc)
        self.assertNotIn(str(self.tmp / "prev-export.mp4"), values)
        self.assertNotIn(7.5, values)

    def test_g_derived_caches_are_not_serialized(self) -> None:
        doc = self._doc()
        for clip in doc["clips"]:
            self.assertEqual(
                set(clip) & {"thumbs", "thumb_pixmaps", "waveform_peaks",
                             "waveform_rate"}, set())
        for audio in doc["added_audio"]:
            self.assertNotIn("peaks", audio)
        for sub in doc["subtitles"]:
            self.assertNotIn("cues", sub)

    def test_g_top_level_keys_are_exactly_the_schema(self) -> None:
        doc = self._doc()
        self.assertEqual(
            set(doc),
            {"format", "version", "assets", "clips", "added_audio",
             "subtitles", "audio_mix"})


# --- Group H: transactional failed open --------------------------------


def _fingerprint(w: MainWindow) -> dict:
    return {
        "clips": [
            (c.asset.path, *(getattr(c, f) for f in CLIP_FIELDS))
            for c in w._clips
        ],
        "audio": [
            (a.path, *(getattr(a, f) for f in AUDIO_FIELDS))
            for a in w._added_audios
        ],
        "assets": sorted(
            (a.path, *(getattr(a, f) for f in ASSET_FIELDS))
            for a in w._assets.values()
        ),
        "subs": [
            (s.path, *(getattr(s, f) for f in SUB_FIELDS)) for s in w._subs
        ],
        "mix": (w.audio_replace_cb.isChecked(), w.audio_gain.value(),
                w.orig_gain.value()),
        "bin": (w.clip_bin.video_list.count(), w.clip_bin.audio_list.count(),
                w.clip_bin.image_list.count(), w.clip_bin.subs_list.count()),
    }


class TransactionalOpenTests(_WinCase):
    def setUp(self) -> None:
        super().setUp()
        self.w = self.win()
        self.populated(self.w)
        self.before = _fingerprint(self.w)
        self.project_a = self.proj("a.coveproj")
        self.assertTrue(self.w._save_project_to(self.project_a))

    def _assert_unchanged(self) -> None:
        self.assertEqual(_fingerprint(self.w), self.before)
        self.assertEqual(self.w._current_project_path, self.project_a)

    def test_h_malformed_json_leaves_project_a_intact(self) -> None:
        bad = self.proj("bad.coveproj")
        bad.write_text('{"format": "cove-video-editor-project", ',
                       encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_unsupported_version_leaves_project_a_intact(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["version"] = 99
        bad = self.proj("v99.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_wrong_format_marker_leaves_project_a_intact(self) -> None:
        bad = self.proj("other.coveproj")
        bad.write_text(json.dumps({"format": "some-other-app", "version": 1}),
                       encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_invalid_model_data_leaves_project_a_intact(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["clips"][1]["timeline_start"] = "later"
        bad = self.proj("badclip.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_dangling_asset_reference_leaves_project_a_intact(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["assets"] = doc["assets"][:1]
        bad = self.proj("dangling.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_duplicate_clip_ids_leave_project_a_intact(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["clips"][1]["id"] = doc["clips"][0]["id"]
        bad = self.proj("dupe.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_infinite_clip_position_leaves_project_a_intact(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["clips"][1]["timeline_start"] = "__INF__"
        bad = self.proj("inf.coveproj")
        bad.write_text(json.dumps(doc).replace('"__INF__"', "Infinity"),
                       encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_absurd_clip_position_leaves_project_a_intact(self) -> None:
        """`1e308` is finite and passes every type check, but timeline
        geometry computes `int(total * pixels_per_second)` - which
        overflows to infinity and raises. That has to be caught inside
        `load_project`, not after the session has been dismantled."""
        for value in (1e308, -1e308, 1e30):
            with self.subTest(value=value):
                doc = json.loads(self.project_a.read_text(encoding="utf-8"))
                doc["clips"][1]["timeline_start"] = value
                bad = self.proj(f"huge{value}.coveproj")
                bad.write_text(json.dumps(doc), encoding="utf-8")
                self.assertFalse(self.w._open_project_path(bad))
                self._assert_unchanged()

    def test_h_absurd_added_audio_offset_leaves_project_a_intact(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["added_audio"][0]["offset"] = 1e308
        bad = self.proj("hugeaudio.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(self.w._open_project_path(bad))
        self._assert_unchanged()

    def test_h_huge_audio_lane_leaves_project_a_intact(self) -> None:
        """Transactional, not merely rejected: the refusal has to land
        before `_apply_project_state`, because the allocation the lane
        drives happens inside it."""
        for lane in (1_000_000_000, project_io.MAX_AUDIO_LANE + 1, -1):
            with self.subTest(lane=lane):
                doc = json.loads(self.project_a.read_text(encoding="utf-8"))
                doc["added_audio"][0]["lane"] = lane
                bad = self.proj(f"lane{lane}.coveproj")
                bad.write_text(json.dumps(doc), encoding="utf-8")
                with unittest.mock.patch.object(
                    MainWindow, "_apply_project_state",
                ) as apply_:
                    self.assertFalse(self.w._open_project_path(bad))
                apply_.assert_not_called()
                self._assert_unchanged()

    def test_h_invalid_speed_leaves_project_a_intact(self) -> None:
        for speed in (0.0, -2.0, 1e6):
            with self.subTest(speed=speed):
                doc = json.loads(self.project_a.read_text(encoding="utf-8"))
                doc["clips"][0]["speed"] = speed
                bad = self.proj(f"speed{speed}.coveproj")
                bad.write_text(json.dumps(doc), encoding="utf-8")
                with unittest.mock.patch.object(
                    MainWindow, "_apply_project_state",
                ) as apply_:
                    self.assertFalse(self.w._open_project_path(bad))
                apply_.assert_not_called()
                self._assert_unchanged()

    def test_h_invalid_crop_rect_leaves_project_a_intact(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["clips"][0]["crop_rect"] = [0.9, 0.0, 0.5, 0.5]
        bad = self.proj("crop.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        with unittest.mock.patch.object(
            MainWindow, "_apply_project_state",
        ) as apply_:
            self.assertFalse(self.w._open_project_path(bad))
        apply_.assert_not_called()
        self._assert_unchanged()

    def test_h_missing_clip_media_leaves_project_a_intact(self) -> None:
        moved = self.tmp / "two.mp4"
        moved.rename(self.tmp / "two.mp4.moved")
        self.assertFalse(self.w._open_project_path(self.project_a))
        self._assert_unchanged()

    def test_h_missing_added_audio_media_leaves_project_a_intact(self) -> None:
        (self.tmp / "bed.mp3").unlink()
        self.assertFalse(self.w._open_project_path(self.project_a))
        self._assert_unchanged()

    def test_h_missing_subtitle_media_leaves_project_a_intact(self) -> None:
        (self.tmp / "cap.srt").unlink()
        self.assertFalse(self.w._open_project_path(self.project_a))
        self._assert_unchanged()

    def test_h_missing_media_is_named_in_the_error(self) -> None:
        (self.tmp / "bed.mp3").unlink()
        self.assertFalse(self.w._open_project_path(self.project_a))
        self.warned.assert_called_once()
        self.assertIn("bed.mp3", str(self.warned.call_args))

    def test_h_failed_open_does_not_touch_the_media_bin_widget(self) -> None:
        bad = self.proj("bad.coveproj")
        bad.write_text("{", encoding="utf-8")
        self.w._open_project_path(bad)
        self.assertEqual(self.w.clip_bin.video_list.count(), 2)
        self.assertEqual(self.w.clip_bin.image_list.count(), 1)

    def test_h_failed_open_costs_no_model_construction_commit(self) -> None:
        """A rejected load must never reach the commit seam - not once,
        not partially. Asserting the call count constrains the mechanism,
        not just the surviving state."""
        bad = self.proj("bad.coveproj")
        bad.write_text("{", encoding="utf-8")
        with unittest.mock.patch.object(
            MainWindow, "_apply_project_state",
        ) as apply_:
            self.w._open_project_path(bad)
        apply_.assert_not_called()


# --- Group I: successful replacement -----------------------------------


class ReplacementTests(_WinCase):
    def test_i_project_b_completely_replaces_project_a(self) -> None:
        a_win = self.win()
        self.populated(a_win)
        project_a = self.proj("a.coveproj")
        a_win._save_project_to(project_a)

        b_win = self.win()
        v = _asset(self.media("solo.mp4"))
        b_win._assets[v.id] = v
        b_win.clip_bin.add_asset(v)
        only = Clip(asset=v, timeline_start=1.0, src_start=0.0, src_end=2.0)
        b_win._clips = [only]
        b_win.timeline.set_clips(b_win._clips)
        project_b = self.proj("b.coveproj")
        b_win._save_project_to(project_b)

        self.assertTrue(a_win._open_project_path(project_b))
        self.assertEqual(len(a_win._clips), 1)
        self.assertEqual(a_win._clips[0].asset.path.name, "solo.mp4")
        self.assertEqual(list(a_win._assets.values())[0].path.name, "solo.mp4")
        self.assertEqual(len(a_win._assets), 1)
        self.assertEqual(a_win._added_audios, [])
        self.assertEqual(a_win._subs, [])
        self.assertEqual(a_win._added_players, {})
        self.assertEqual(a_win.clip_bin.video_list.count(), 1)
        self.assertEqual(a_win.clip_bin.image_list.count(), 0)
        self.assertEqual(a_win.clip_bin.subs_list.count(), 0)
        self.assertFalse(a_win.audio_replace_cb.isChecked())
        self.assertEqual(a_win.timeline.added_audios(), [])
        self.assertEqual(len(a_win.timeline._clips), 1)
        self.assertEqual(a_win._current_project_path, project_b)


# --- Group J: save failure preserves the prior project -----------------


class _FailingSaveFile(QSaveFile):
    """A real ``QSaveFile`` whose commit fails, standing in for a full
    disk or a lost mount at the exact moment the rename would happen."""

    def commit(self) -> bool:  # noqa: D102
        self.cancelWriting()
        super().commit()
        return False


class SaveFailureTests(_WinCase):
    def test_j_failed_commit_leaves_the_prior_project_loadable(self) -> None:
        w = self.win()
        first = self.populated(w)
        p = self.proj()
        self.assertTrue(w._save_project_to(p))
        original = p.read_bytes()

        # A materially different session that must NOT reach disk.
        w._clips = [first["clips"][0]]
        w._clips[0].crop_rect = None
        with unittest.mock.patch.object(project_io, "QSaveFile",
                                        _FailingSaveFile):
            self.assertFalse(w._save_project_to(p))

        self.assertEqual(p.read_bytes(), original)
        state = project_io.load_project(p)
        self.assertEqual(len(state.clips), 3)
        self.assertEqual(state.clips[0].crop_rect, (0.1, 0.2, 0.5, 0.6))

    def test_j_failed_save_does_not_leave_stray_files(self) -> None:
        w = self.win()
        p = self.proj()
        with unittest.mock.patch.object(project_io, "QSaveFile",
                                        _FailingSaveFile):
            self.assertFalse(w._save_project_to(p))
        self.assertFalse(p.exists())

    def test_j_unserializable_state_never_truncates(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj()
        w._save_project_to(p)
        original = p.read_bytes()
        with unittest.mock.patch.object(
            project_io, "serialize_project",
            side_effect=project_io.ProjectError("boom"),
        ):
            self.assertFalse(w._save_project_to(p))
        self.assertEqual(p.read_bytes(), original)


# --- Group K: Save vs Save As ------------------------------------------


class SaveAsTests(_WinCase):
    def test_k1_save_with_no_path_delegates_to_save_as(self) -> None:
        w = self.win()
        self.assertIsNone(w._current_project_path)
        with unittest.mock.patch.object(MainWindow, "_on_save_project_as") as sa:
            w._on_save_project()
        sa.assert_called_once()

    def test_k2_save_as_success_updates_the_current_path(self) -> None:
        w = self.win()
        self.populated(w)
        target = self.proj("chosen.coveproj")
        with unittest.mock.patch.object(
            app_mod.QFileDialog, "getSaveFileName",
            return_value=(str(target), ""),
        ):
            w._on_save_project_as()
        self.assertEqual(w._current_project_path, target)
        self.assertTrue(target.is_file())

    def test_k3_save_reuses_that_path_afterwards(self) -> None:
        w = self.win()
        self.populated(w)
        target = self.proj("chosen.coveproj")
        with unittest.mock.patch.object(
            app_mod.QFileDialog, "getSaveFileName",
            return_value=(str(target), ""),
        ):
            w._on_save_project_as()
        w._clips = w._clips[:1]
        with unittest.mock.patch.object(MainWindow, "_on_save_project_as") as sa:
            w._on_save_project()
        sa.assert_not_called()
        self.assertEqual(len(project_io.load_project(target).clips), 1)

    def test_k4_failed_save_as_does_not_change_the_current_path(self) -> None:
        w = self.win()
        self.populated(w)
        good = self.proj("good.coveproj")
        self.assertTrue(w._save_project_to(good))
        doomed = self.proj("doomed.coveproj")
        with unittest.mock.patch.object(project_io, "QSaveFile",
                                        _FailingSaveFile), \
                unittest.mock.patch.object(
                    app_mod.QFileDialog, "getSaveFileName",
                    return_value=(str(doomed), "")):
            w._on_save_project_as()
        self.assertEqual(w._current_project_path, good)
        self.assertFalse(doomed.exists())

    def test_k5_cancelled_dialog_changes_nothing(self) -> None:
        w = self.win()
        self.populated(w)
        good = self.proj("good.coveproj")
        w._save_project_to(good)
        before = good.read_bytes()
        with unittest.mock.patch.object(
            app_mod.QFileDialog, "getSaveFileName",
            return_value=("", ""),
        ):
            w._on_save_project_as()
        self.assertEqual(w._current_project_path, good)
        self.assertEqual(good.read_bytes(), before)

    def test_k5_cancelled_open_dialog_changes_nothing(self) -> None:
        w = self.win()
        self.populated(w)
        before = _fingerprint(w)
        with unittest.mock.patch.object(
            app_mod.QFileDialog, "getOpenFileName",
            return_value=("", ""),
        ):
            w._on_open_project()
        self.assertEqual(_fingerprint(w), before)
        self.assertIsNone(w._current_project_path)

    def test_k_open_success_updates_the_current_path(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj("x.coveproj")
        w._save_project_to(p)
        w2 = self.win()
        with unittest.mock.patch.object(
            app_mod.QFileDialog, "getOpenFileName",
            return_value=(str(p), ""),
        ):
            w2._on_open_project()
        self.assertEqual(w2._current_project_path, p)


# --- Group L: an active crop draft at save time ------------------------


class CropDraftSaveTests(_WinCase):
    def test_l_visible_draft_is_committed_before_serialization(self) -> None:
        w = self.win()
        v = _asset(self.media("v.mp4"))
        w._assets[v.id] = v
        w.clip_bin.add_asset(v)
        c = Clip(asset=v, timeline_start=0.0, src_start=0.0, src_end=5.0)
        w._clips = [c]
        w.timeline.set_clips(w._clips)
        w.timeline.select_clip(c.id)
        w._set_preview_clip(c)

        w._begin_crop_edit(c)
        self.assertEqual(w._crop_edit_clip_id, c.id)
        w._on_crop_aspect_changed(SQUARE)
        w._on_crop_fit_clicked()
        draft_rect, draft_preset = w._crop_draft_commit_values()
        self.assertNotEqual((c.crop_rect, c.crop_preset),
                            (draft_rect, draft_preset))

        p = self.proj()
        self.assertTrue(w._save_project_to(p))

        self.assertEqual((c.crop_rect, c.crop_preset),
                         (draft_rect, draft_preset))
        saved = project_io.load_project(p).clips[0]
        self.assertEqual((saved.crop_rect, saved.crop_preset),
                         (draft_rect, draft_preset))
        self.assertEqual(w._crop_edit_clip_id, "")

    def test_l_save_reuses_the_existing_crop_lifecycle(self) -> None:
        """No second crop implementation: the save path must go through
        ``_finish_crop_edit`` rather than writing the overlay itself."""
        w = self.win()
        with unittest.mock.patch.object(MainWindow, "_finish_crop_edit") as fin:
            w._save_project_to(self.proj())
        fin.assert_called_once_with(commit=True)


# --- Group M: post-open transient reset --------------------------------


class PostOpenResetTests(_WinCase):
    def _opened(self) -> MainWindow:
        src = self.win()
        self.populated(src)
        p = self.proj()
        src._save_project_to(p)

        w = self.win()
        self.populated(w)
        w.timeline.select_clip(w._clips[1].id)
        w.timeline._set_selection_span(1.0, 4.0)
        w.timeline.set_playhead(9.0, emit=False)
        w._set_last_export_output(self.media("old-export.mp4"))
        w._region_export_range = (1.0, 4.0)
        w._snapshot()
        w._begin_crop_edit(w._clips[0])
        self.assertTrue(w._play_timer.isActive() is False)
        w._toggle_play()
        self.assertTrue(w._play_timer.isActive())

        self.assertTrue(w._open_project_path(p))
        return w

    def test_m_playback_is_stopped_and_does_not_restart(self) -> None:
        w = self._opened()
        self.assertFalse(w._play_timer.isActive())
        QApplication.processEvents()
        self.assertFalse(w._play_timer.isActive())

    def test_m_crop_editing_is_inactive(self) -> None:
        w = self._opened()
        self.assertEqual(w._crop_edit_clip_id, "")
        self.assertFalse(w.crop_btn.isChecked())

    def test_m_selection_is_cleared(self) -> None:
        w = self._opened()
        self.assertEqual(w.timeline.selected_id(), "")
        self.assertEqual(w.timeline.selection(), (0.0, 0.0))

    def test_m_stale_show_in_folder_state_is_cleared(self) -> None:
        w = self._opened()
        self.assertIsNone(w._last_export_output)
        self.assertFalse(w.show_folder_btn.isVisible())

    def test_m_stale_region_export_range_is_cleared(self) -> None:
        w = self._opened()
        self.assertIsNone(w._region_export_range)

    def test_m_undo_history_from_the_prior_project_is_dropped(self) -> None:
        w = self._opened()
        self.assertEqual(w._undo_stack, [])
        self.assertEqual(w._redo_stack, [])

    def test_m_playhead_returns_to_the_start(self) -> None:
        w = self._opened()
        self.assertEqual(w.timeline.playhead(), 0.0)

    def test_m_timeline_widget_shows_the_loaded_state(self) -> None:
        w = self._opened()
        self.assertEqual(len(w.timeline._clips), 3)
        self.assertEqual(len(w.timeline.added_audios()), 1)

    def test_m_players_exist_for_exactly_the_loaded_audio(self) -> None:
        w = self._opened()
        self.assertEqual(set(w._added_players),
                         {a.id for a in w._added_audios})


class _StubWorker(QObject):
    """Stands in for a ``ThumbnailWorker`` / ``WaveformWorker``.

    Same public shape the app relies on - it lives on the worker thread
    and exposes ``cancel()`` - without spawning ffmpeg.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class OutgoingWorkerTests(_WinCase):
    """Opening a project must not abandon the previous project's media
    workers.

    ``_kick_off_thumbs`` / ``_kick_off_waveform`` key their registries by
    clip id, and a saved project keeps its clip ids - so reopening a
    project whose thumbnails are still being generated would overwrite the
    entry holding a *running* ``QThread``. Dropping the last reference to
    a running QThread aborts the process, so this is asserted against real
    running threads rather than mocks.
    """

    def _running(self) -> tuple[QThread, _StubWorker]:
        thread = QThread()
        worker = _StubWorker()
        worker.moveToThread(thread)
        thread.start()
        self.addCleanup(lambda: (thread.quit(), thread.wait(2000)))
        self.assertTrue(thread.isRunning())
        return thread, worker

    def test_open_cancels_and_joins_outgoing_media_workers(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj()
        self.assertTrue(w._save_project_to(p))

        t_thread, t_worker = self._running()
        w_thread, w_worker = self._running()
        a_thread, a_worker = self._running()
        clip_id = w._clips[0].id
        audio_id = w._added_audios[0].id
        w._thumb_threads[clip_id] = t_thread
        w._thumb_workers[clip_id] = t_worker
        w._wave_threads[clip_id] = w_thread
        w._wave_workers[clip_id] = w_worker
        w._added_wave_threads[audio_id] = a_thread
        w._added_wave_workers[audio_id] = a_worker

        self.assertTrue(w._open_project_path(p))

        for worker in (t_worker, w_worker, a_worker):
            self.assertTrue(worker.cancelled)
        # Joined, not merely asked to stop: a running thread whose last
        # reference the registry just dropped is the abort path.
        for thread in (t_thread, w_thread, a_thread):
            self.assertFalse(thread.isRunning())

    def test_open_does_not_leave_outgoing_registry_entries(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj()
        w._save_project_to(p)
        thread, worker = self._running()
        stale = "stale-id"
        w._thumb_threads[stale] = thread
        w._thumb_workers[stale] = worker

        w._open_project_path(p)

        self.assertNotIn(stale, w._thumb_threads)
        self.assertNotIn(stale, w._thumb_workers)

    def test_timed_out_thread_stays_referenced_and_leaves_the_registry(
        self,
    ) -> None:
        """A worker blocked inside ``subprocess.run`` cannot honour
        ``quit()``: production only checks cancellation after the ffmpeg
        call returns. The id-keyed registry still has to be freed for the
        incoming project, so the thread has to be held somewhere else -
        releasing a running ``QThread`` aborts the process.
        """
        release = threading.Event()
        self.addCleanup(release.set)

        class _BlockingThread(QThread):
            def run(self) -> None:  # noqa: D102
                release.wait(30)

        w = self.win()
        thread = _BlockingThread()
        worker = _StubWorker()
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.wait(5000)))
        while not thread.isRunning():
            QApplication.processEvents()
        w._thumb_threads["busy"] = thread
        w._thumb_workers["busy"] = worker

        # A 1 ms budget: the join must be allowed to fail fast rather than
        # stalling the open for the full production timeout.
        w._stop_media_workers(wait_ms=1)

        self.assertTrue(worker.cancelled)
        self.assertTrue(thread.isRunning())
        self.assertNotIn("busy", w._thumb_threads)
        self.assertNotIn("busy", w._thumb_workers)
        retained = [t for t, _ in w._retired_media]
        self.assertIn(thread, retained)

        release.set()
        self.assertTrue(thread.wait(5000))
        for _ in range(20):
            QApplication.processEvents()
        self.assertEqual(w._retired_media, [])

    def test_close_joins_a_retired_media_thread(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        class _BlockingThread(QThread):
            def run(self) -> None:  # noqa: D102
                release.wait(30)

        w = self.win()
        thread = _BlockingThread()
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.wait(5000)))
        while not thread.isRunning():
            QApplication.processEvents()
        w._thumb_threads["busy"] = thread
        w._thumb_workers["busy"] = _StubWorker()
        w._stop_media_workers(wait_ms=1)
        self.assertTrue(thread.isRunning())

        release.set()
        w.close()
        self.assertFalse(thread.isRunning())

    def _recording_start(self, sink: list) -> object:
        """`start_thumbnails` stand-in that also keeps every thread it
        hands out alive for the whole test.

        Tracking the threads here rather than through the window's
        registries is deliberate: the defect under test is precisely that
        a registry entry gets dropped, and a teardown that walked the
        registries would then leave a running QThread to be garbage
        collected - aborting the process instead of failing an assertion.
        """
        def _start(clip_id, video, duration, count=24):  # noqa: ANN001, ANN202
            thread, worker = _fake_start_thumbnails(
                clip_id, video, duration, count)
            sink.append((thread, worker))
            return thread, worker

        return _start

    @staticmethod
    def _quit_all(sink: list) -> None:
        for thread, _worker in sink:
            thread.quit()
            thread.wait(5000)

    def test_stale_queued_callback_cannot_evict_the_replacement_worker(
        self,
    ) -> None:
        """The scenario the queued connection actually produces.

        ``thread.finished`` is emitted while the outgoing thread is being
        joined, but it is a ``QueuedConnection`` - so it is delivered a
        turn later, by which time the incoming project has already
        registered its own worker under the *same* clip id (a saved
        project keeps its ids). Popping by id alone would drop the live
        thread's only reference.
        """
        w = self.win()
        self.populated(w)
        p = self.proj()
        self.assertTrue(w._save_project_to(p))
        clip_id = w._clips[0].id
        started: list = []
        self.addCleanup(self._quit_all, started)

        with unittest.mock.patch.object(
            MainWindow, "_kick_off_thumbs", _REAL_KICK_OFF_THUMBS,
        ), unittest.mock.patch.object(
            app_mod, "start_thumbnails", self._recording_start(started),
        ):
            # 1-2. Project A's worker for clip id X, run to completion so
            # its `finished` is emitted and queued but NOT yet delivered.
            w._kick_off_thumbs(w._clips[0])
            old_thread = w._thumb_threads[clip_id]
            old_worker = w._thumb_workers[clip_id]
            old_thread.quit()
            self.assertTrue(old_thread.wait(5000))

            # 3-4. Project B replaces A and registers its own worker for
            # the same clip id.
            self.assertTrue(w._open_project_path(p))
            new_thread = w._thumb_threads[clip_id]
            new_worker = w._thumb_workers[clip_id]
            self.assertIsNot(new_thread, old_thread)
            self.assertIsNot(new_worker, old_worker)

            # 5-6. Deliver A's queued callback. B's registration survives.
            for _ in range(20):
                QApplication.processEvents()
            self.assertIs(w._thumb_threads.get(clip_id), new_thread)
            self.assertIs(w._thumb_workers.get(clip_id), new_worker)

            # 7-8. B's own callback still cleans up normally.
            new_thread.quit()
            self.assertTrue(new_thread.wait(5000))
            for _ in range(20):
                QApplication.processEvents()
            self.assertNotIn(clip_id, w._thumb_threads)
            self.assertNotIn(clip_id, w._thumb_workers)

    def test_stale_queued_result_cannot_paint_the_replacement_clip(
        self,
    ) -> None:
        """The other half of the same race.

        ``ThumbnailWorker.run`` has no cancellation check before its final
        ``emit``, and a retired worker still inside ffmpeg finishes on its
        own schedule - so a *result* can arrive after project replacement.
        It is addressed by clip id, which the incoming project reuses, so
        without an identity check the outgoing project's frames land on
        the new timeline.
        """
        w = self.win()
        self.populated(w)
        p = self.proj()
        self.assertTrue(w._save_project_to(p))
        clip_id = w._clips[0].id
        started: list = []
        self.addCleanup(self._quit_all, started)
        stale = [QImage(4, 4, QImage.Format_ARGB32)]
        fresh = [QImage(8, 8, QImage.Format_ARGB32)]

        with unittest.mock.patch.object(
            MainWindow, "_kick_off_thumbs", _REAL_KICK_OFF_THUMBS,
        ), unittest.mock.patch.object(
            app_mod, "start_thumbnails", self._recording_start(started),
        ):
            w._kick_off_thumbs(w._clips[0])
            old_worker = w._thumb_workers[clip_id]
            old_thread = w._thumb_threads[clip_id]
            old_thread.quit()
            self.assertTrue(old_thread.wait(5000))

            self.assertTrue(w._open_project_path(p))
            new_clip = next(c for c in w._clips if c.id == clip_id)
            new_worker = w._thumb_workers[clip_id]
            self.assertIsNot(new_worker, old_worker)
            self.assertEqual(new_clip.thumbs, [])

            # The retired worker reports. Queued, so delivery is ours to
            # drive - and it must change nothing.
            old_worker.finished.emit(clip_id, stale)
            for _ in range(20):
                QApplication.processEvents()
            self.assertEqual(new_clip.thumbs, [])
            self.assertEqual(new_clip.thumb_pixmaps, [])

            # The current worker's own result still lands.
            new_worker.finished.emit(clip_id, fresh)
            for _ in range(20):
                QApplication.processEvents()
            self.assertEqual(len(new_clip.thumbs), 1)
            self.assertEqual(new_clip.thumbs[0].width(), 8)

    def _pump(self) -> None:
        for _ in range(20):
            QApplication.processEvents()

    def test_result_callbacks_ignore_a_superseded_worker(self) -> None:
        """The same identity guard on all three result paths.

        Driven through real emissions on real ``QueuedConnection``s rather
        than by calling the slots: the guard reads ``sender()``, which
        only exists when the slot is reached the way production reaches
        it.
        """
        w = self.win()
        v = _asset(self.media("v.mp4"))
        w._assets[v.id] = v
        clip = Clip(asset=v, timeline_start=0.0, src_start=0.0, src_end=5.0)
        w._clips = [clip]
        audio = AddedAudio(path=self.media("a.mp3"), duration=5.0)
        w._added_audios = [audio]

        def img() -> list:
            return [QImage(4, 4, QImage.Format_ARGB32)]

        # Thumbnails.
        current, superseded = _FakeAnalysisWorker("t"), _FakeAnalysisWorker("t")
        for wk in (current, superseded):
            wk.finished.connect(w._on_thumbs_ready, Qt.QueuedConnection)
        w._thumb_workers[clip.id] = current
        superseded.finished.emit(clip.id, img())
        self._pump()
        self.assertEqual(clip.thumbs, [])
        current.finished.emit(clip.id, img())
        self._pump()
        self.assertEqual(len(clip.thumbs), 1)

        # Clip waveform.
        current, superseded = _FakeWaveWorker(), _FakeWaveWorker()
        for wk in (current, superseded):
            wk.finished.connect(w._on_waveform_ready, Qt.QueuedConnection)
        w._wave_workers[clip.id] = current
        superseded.finished.emit(clip.id, [0.5], 400)
        self._pump()
        self.assertEqual(clip.waveform_peaks, [])
        current.finished.emit(clip.id, [0.5], 400)
        self._pump()
        self.assertEqual(clip.waveform_peaks, [0.5])

        # Added-audio waveform.
        current, superseded = _FakeWaveWorker(), _FakeWaveWorker()
        for wk in (current, superseded):
            wk.finished.connect(w._on_added_waveform_ready, Qt.QueuedConnection)
        w._added_wave_workers[audio.id] = current
        superseded.finished.emit(audio.id, [0.25], 400)
        self._pump()
        self.assertEqual(audio.peaks, [])
        current.finished.emit(audio.id, [0.25], 400)
        self._pump()
        self.assertEqual(audio.peaks, [0.25])

    def test_done_callbacks_ignore_a_superseded_thread(self) -> None:
        """The same identity guard on all three registries, asserted
        directly so a registry that was left un-guarded is caught."""
        w = self.win()
        cases = (
            ("_thumb_done", w._thumb_threads, w._thumb_workers),
            ("_wave_done", w._wave_threads, w._wave_workers),
            ("_added_wave_done", w._added_wave_threads,
             w._added_wave_workers),
        )
        for name, threads, workers in cases:
            with self.subTest(name):
                superseded = QThread()
                current = QThread()
                self.addCleanup(superseded.wait, 1000)
                self.addCleanup(current.wait, 1000)
                threads["k"] = current
                workers["k"] = _StubWorker()
                getattr(w, name)("k", superseded)
                self.assertIs(threads.get("k"), current)
                self.assertIn("k", workers)
                getattr(w, name)("k", current)
                self.assertNotIn("k", threads)
                self.assertNotIn("k", workers)

    def _blocking_thread(self) -> tuple[QThread, threading.Event]:
        release = threading.Event()
        self.addCleanup(release.set)

        class _BlockingThread(QThread):
            def run(self) -> None:  # noqa: D102
                release.wait(30)

        thread = _BlockingThread()
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.wait(5000)))
        while not thread.isRunning():
            QApplication.processEvents()
        return thread, release

    def _fast_close(self) -> None:
        """Close joins with a 1 ms budget instead of the production 1.5 s.

        The point under test is what happens when the join *fails*, so the
        wait must be allowed to time out immediately rather than making
        every one of these cases pay 1.5 seconds.
        """
        p = unittest.mock.patch.object(app_mod, "_CLOSE_JOIN_MS", 1)
        p.start()
        self.addCleanup(p.stop)

    def test_a1_close_releases_a_retired_thread_that_stops(self) -> None:
        self._fast_close()
        w = self.win()
        thread = QThread()
        thread.start()
        self.addCleanup(lambda: (thread.quit(), thread.wait(5000)))
        w._retire_media_thread(thread, _StubWorker())
        w.close()
        for _ in range(20):
            QApplication.processEvents()
        self.assertFalse(thread.isRunning())
        self.assertNotIn(thread,
                         [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS])

    def test_a2_close_retains_a_thread_that_will_not_stop(self) -> None:
        self._fast_close()
        w = self.win()
        thread, release = self._blocking_thread()
        worker = _StubWorker()
        w._retire_media_thread(thread, worker)

        w.close()

        self.assertTrue(thread.isRunning())
        surviving = [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS]
        self.assertIn(thread, surviving)
        self.assertTrue(worker.cancelled)
        release.set()

    def test_a3_retained_ownership_is_released_once_the_thread_stops(
        self,
    ) -> None:
        self._fast_close()
        w = self.win()
        thread, release = self._blocking_thread()
        w._retire_media_thread(thread, _StubWorker())
        w.close()
        self.assertIn(thread,
                      [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS])

        release.set()
        self.assertTrue(thread.wait(5000))
        for _ in range(20):
            QApplication.processEvents()
        self.assertNotIn(thread,
                         [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS])

    def test_a4_window_teardown_cannot_destroy_a_running_thread(self) -> None:
        """The abort path itself.

        After the window is closed and every reference this test holds to
        it is dropped, the C++ QThread must still be alive - which is what
        `shiboken6.isValid` answers - because something other than the
        window now owns it.
        """
        self._fast_close()
        w = self.win()
        thread, release = self._blocking_thread()
        w._retire_media_thread(thread, _StubWorker())
        w.close()

        # Drop the window hard: its own `_retired_media` must not be the
        # only thing keeping the thread alive.
        w._retired_media = []
        gc.collect()
        for _ in range(20):
            QApplication.processEvents()

        self.assertTrue(shiboken6.isValid(thread))
        self.assertTrue(thread.isRunning())
        release.set()

    def test_a5_retired_threads_are_retained_independently(self) -> None:
        self._fast_close()
        w = self.win()
        stopped = QThread()
        stopped.start()
        self.addCleanup(lambda: (stopped.quit(), stopped.wait(5000)))
        blocked, release = self._blocking_thread()
        w._retire_media_thread(stopped, _StubWorker())
        w._retire_media_thread(blocked, _StubWorker())

        w.close()
        for _ in range(20):
            QApplication.processEvents()

        surviving = [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS]
        self.assertNotIn(stopped, surviving)
        self.assertIn(blocked, surviving)
        release.set()

    def test_a5_releasing_one_survivor_keeps_the_others(self) -> None:
        """Release has to be by identity, not by position.

        With a single survivor almost any removal rule looks correct, so
        this drives two of them and finishes the *second*: a rule that
        popped the front would drop a thread that is still running.
        """
        self._fast_close()
        w = self.win()
        first, release_first = self._blocking_thread()
        second, release_second = self._blocking_thread()
        w._retire_media_thread(first, _StubWorker())
        w._retire_media_thread(second, _StubWorker())
        w.close()

        surviving = [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS]
        self.assertIn(first, surviving)
        self.assertIn(second, surviving)

        release_second.set()
        self.assertTrue(second.wait(5000))
        for _ in range(20):
            QApplication.processEvents()

        surviving = [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS]
        self.assertNotIn(second, surviving)
        self.assertIn(first, surviving)
        self.assertTrue(first.isRunning())
        release_first.set()

    def test_a5_registry_threads_use_the_same_ownership_rule(self) -> None:
        """The id-keyed registries are joined by the same loop with the
        same ignored return value, so they get the same treatment."""
        self._fast_close()
        w = self.win()
        blocked, release = self._blocking_thread()
        w._thumb_threads["busy"] = blocked
        w._thumb_workers["busy"] = _StubWorker()

        w.close()

        self.assertTrue(blocked.isRunning())
        self.assertIn(blocked,
                      [t for t, _ in app_mod._SURVIVING_MEDIA_THREADS])
        release.set()

    def test_a6_a_surviving_worker_cannot_mutate_live_state(self) -> None:
        """2N-C's guard still holds for a worker that outlived its
        window: it is no longer the registry's worker, so its result is
        rejected."""
        self._fast_close()
        w = self.win()
        v = _asset(self.media("v.mp4"))
        w._assets[v.id] = v
        clip = Clip(asset=v, timeline_start=0.0, src_start=0.0, src_end=5.0)
        w._clips = [clip]

        stale = _FakeAnalysisWorker(clip.id)
        stale.finished.connect(w._on_thumbs_ready, Qt.QueuedConnection)
        current = _FakeAnalysisWorker(clip.id)
        current.finished.connect(w._on_thumbs_ready, Qt.QueuedConnection)
        w._thumb_workers[clip.id] = current

        stale.finished.emit(clip.id, [QImage(4, 4, QImage.Format_ARGB32)])
        for _ in range(20):
            QApplication.processEvents()
        self.assertEqual(clip.thumbs, [])

    def test_open_rebuilds_added_audio_players_for_reused_ids(self) -> None:
        """Two projects can carry the same added-audio id on different
        files - Save As copies ids - so a retained player would keep
        playing the outgoing project's file under the new timeline."""
        w = self.win()
        self.populated(w)
        p = self.proj("a.coveproj")
        w._save_project_to(p)
        audio_id = w._added_audios[0].id
        old_player = w._added_players[audio_id]

        other = self.media("other bed.mp3")
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["added_audio"][0]["path"] = str(other)
        b = self.proj("b.coveproj")
        b.write_text(json.dumps(doc), encoding="utf-8")

        self.assertTrue(w._open_project_path(b))
        self.assertEqual(w._added_audios[0].id, audio_id)
        self.assertEqual(w._added_audios[0].path, other)
        new_player = w._added_players[audio_id]
        self.assertIsNot(new_player, old_player)
        self.assertEqual(new_player.source(), QUrl.fromLocalFile(str(other)))


# --- Group N: repeated cycle -------------------------------------------


class RepeatedCycleTests(_WinCase):
    def test_n_latest_edit_survives_a_second_save_open_cycle(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj("a.coveproj")
        self.assertTrue(w._save_project_to(p))
        self.assertTrue(w._open_project_path(p))

        # A normal model-level edit through the app's own seam.
        target = w._clips[1]
        moved_id = target.id
        before = target.timeline_start
        # The app's own ripple-move seam, not a field poke. It clamps the
        # target position to keep the sequence gapless, so the assertion
        # is "the edit the app actually made survives", not a guessed
        # number.
        w._ripple_move_clip(moved_id, 30.0)
        after = next(c.timeline_start for c in w._clips if c.id == moved_id)
        self.assertNotEqual(after, before)

        self.assertTrue(w._save_project_to(p))
        self.assertTrue(w._open_project_path(p))
        self.assertEqual(
            next(c.timeline_start for c in w._clips if c.id == moved_id), after)
        self.assertEqual(len(w._clips), 3)

    def test_n_crop_edit_survives_a_second_cycle(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj("a.coveproj")
        w._save_project_to(p)
        w._open_project_path(p)
        clip = w._clips[1]
        clip.crop_rect = (0.05, 0.05, 0.9, 0.9)
        clip.crop_preset = SQUARE
        w._save_project_to(p)
        w._open_project_path(p)
        got = next(c for c in w._clips if c.id == clip.id)
        self.assertEqual(got.crop_rect, (0.05, 0.05, 0.9, 0.9))
        self.assertEqual(got.crop_preset, SQUARE)


# --- Group P: exhaustive schema-v1 scalar range audit ------------------
#
# Every scalar schema v1 persists, with the production consumer that
# defines its legal domain. Earlier slices bounded the time fields,
# `speed`, `crop_rect` and `lane` one at a time as reviews found them;
# this table closes the rest of the class in one place so a new field
# cannot be added without an entry here.
#
# (container, field, good values, bad values)
RANGE_CASES = (
    # MediaAsset.width / height reach QSizeF and the crop pixel maths.
    # 0 is legitimate and load-bearing: `_import_paths` builds audio
    # assets with width=0, height=0, fps=0.0.
    ("assets", "width", (0, 1, 1920, 65535), (-1, 65536, 10**12, 1.5, True)),
    ("assets", "height", (0, 1, 1080, 65535), (-1, 65536, 10**12, 2.5, True)),
    # `_current_fps` only trusts fps > 0 and otherwise falls back to 30.
    ("assets", "fps", (0.0, 23.976, 60.0, 1000.0), (-1.0, 1000.1, 1e9, "30")),
    ("assets", "kind", ("video", "audio", "image"),
     ("", "movie", "VIDEO", 3, None)),
    # Clip volume: the properties dialog is a 0-200 percent spin box.
    ("clips", "audio_volume", (0.0, 0.5, 1.0, 2.0), (-0.1, 2.01, 1e6, "1")),
    ("added_audio", "volume", (0.0, 0.75, 1.0, 2.0), (-0.1, 2.01, 1e6, True)),
    # AddedAudio.rate is a peaks-per-second cache; the only producer is
    # WaveformWorker.PEAK_RATE and the sole consumer guards `rate > 0`.
    ("added_audio", "rate", (0, 400, 192000), (-1, 192001, 10**12, 4.5)),
    ("subtitles", "font_size", (10, 36, 120), (9, 121, 10**9, 36.5, True)),
    ("subtitles", "outline", (0, 2, 8), (-1, 9, 10**9, 2.5, True)),
    ("subtitles", "offset_ms", (-30000, 0, 250, 30000),
     (-30001, 30001, 10**12, 250.5, True)),
    ("subtitles", "position", ("bottom", "top"), ("", "middle", "Top", 1)),
    ("subtitles", "primary_color", ("#FFFFFF", "#ff0000", "#0A1B2C"),
     ("FFFFFF", "#FFF", "#GGGGGG", "#FFFFFFFF", "", 0)),
    ("subtitles", "outline_color", ("#000000", "#123456"),
     ("000000", "#12345", "#XYZXYZ", None)),
)

MIX_CASES = (
    ("added_gain", (0.0, 1.0, 1.4, 3.0), (-0.1, 3.01, 1e9, "1", True)),
    ("original_gain", (0.0, 0.6, 3.0), (-0.1, 3.01, 1e9, None)),
)


class SchemaRangeAuditTests(_TempTree):
    """Group P - no schema-v1 scalar may load outside its consumer's
    legal domain."""

    def _doc(self) -> dict:
        a = _asset(self.media("v.mp4"))
        return project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_end=5.0)],
            added_audios=[AddedAudio(path=self.media("x.mp3"), duration=5.0)],
            subtitles=[SubtitleTrack(path=self.media("s.srt"))],
        ))

    def test_p_in_range_values_are_accepted_unchanged(self) -> None:
        """Boundary halves of the audit: a bound that rejected a value the
        editor can produce would be a worse bug than no bound at all."""
        for container, field, good, _bad in RANGE_CASES:
            for value in good:
                with self.subTest(field=f"{container}.{field}", value=value):
                    doc = self._doc()
                    doc[container][0][field] = value
                    if (container, field) == ("assets", "kind") \
                            and value not in project_io.CLIP_ASSET_KINDS:
                        # An audio asset is a perfectly good bin entry but
                        # cannot host a timeline clip, so the fixture's
                        # clip has to go with it.
                        doc["clips"] = []
                    state = project_io.deserialize_project(doc)
                    got = getattr(getattr(state, {
                        "assets": "assets", "clips": "clips",
                        "added_audio": "added_audios",
                        "subtitles": "subtitles",
                    }[container])[0], field)
                    self.assertEqual(got, value)

    def test_p_out_of_range_values_are_rejected(self) -> None:
        for container, field, _good, bad in RANGE_CASES:
            for value in bad:
                with self.subTest(field=f"{container}.{field}", value=value):
                    doc = self._doc()
                    doc[container][0][field] = value
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(doc)

    def test_p_audio_mix_gains_accepted_in_range(self) -> None:
        for field, good, _bad in MIX_CASES:
            for value in good:
                with self.subTest(field=field, value=value):
                    doc = self._doc()
                    doc["audio_mix"][field] = value
                    state = project_io.deserialize_project(doc)
                    attr = {"added_gain": "added_gain",
                            "original_gain": "original_gain"}[field]
                    self.assertEqual(getattr(state, attr), value)

    def test_p_audio_mix_gains_rejected_out_of_range(self) -> None:
        """The spin boxes are `setRange(0.0, 3.0)`, so `setValue()` would
        silently clamp an out-of-range gain and the next save would write
        the clamped number back - a quiet edit to the user's project."""
        for field, _good, bad in MIX_CASES:
            for value in bad:
                with self.subTest(field=field, value=value):
                    doc = self._doc()
                    doc["audio_mix"][field] = value
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(doc)

    def test_p_non_finite_rejected_for_every_bounded_float(self) -> None:
        floats = (("assets", "fps"), ("clips", "audio_volume"),
                  ("added_audio", "volume"))
        for literal in ("NaN", "Infinity", "-Infinity"):
            for container, field in floats:
                with self.subTest(field=f"{container}.{field}", n=literal):
                    doc = self._doc()
                    doc[container][0][field] = float(literal)
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(doc)
            for field, _g, _b in MIX_CASES:
                with self.subTest(field=field, n=literal):
                    doc = self._doc()
                    doc["audio_mix"][field] = float(literal)
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(doc)

    def test_p_bool_is_not_a_number(self) -> None:
        """`bool` is an `int` in Python, so a true/false where a number
        belongs has to be refused explicitly rather than read as 1/0."""
        for container, field in (("assets", "width"), ("assets", "height"),
                                 ("added_audio", "rate"),
                                 ("subtitles", "font_size"),
                                 ("subtitles", "outline"),
                                 ("subtitles", "offset_ms")):
            for value in (True, False):
                with self.subTest(field=f"{container}.{field}", value=value):
                    doc = self._doc()
                    doc[container][0][field] = value
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(doc)

    def test_p_num_primitive_rejects_non_finite_independently(self) -> None:
        """Pin the shared primitive's own contract.

        Every float in schema v1 now flows through a bounded reader, and
        `lo <= NaN <= hi` is already False, so `_num`'s non-finite guard
        is currently redundant - removing it leaves the whole file green.
        It is kept as the property that makes `_num` safe for any future
        caller that does not bound, and asserted here directly so that
        guarantee cannot be deleted silently.
        """
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal):
                with self.assertRaises(project_io.ProjectError):
                    project_io._num({"k": float(literal)}, "k", "A field")
        self.assertEqual(project_io._num({"k": 1.5}, "k", "A field"), 1.5)

    def test_p_error_names_the_offending_field(self) -> None:
        doc = self._doc()
        doc["subtitles"][0]["font_size"] = 5000
        with self.assertRaises(project_io.ProjectError) as ctx:
            project_io.deserialize_project(doc)
        self.assertIn("font_size", str(ctx.exception))

    def test_p_free_form_strings_stay_unbounded(self) -> None:
        """Two fields deliberately carry no domain, and that is a result
        of the audit rather than an omission.

        `crop_preset` is an opaque display key: both consumers already
        degrade an unknown one gracefully (`CROP_ASPECT_PRESETS.get(...)
        is None` -> Free, `compact_preset_label` -> "Active").
        `font_family` is free text that the exporter already sanitises
        before it reaches libass.
        """
        doc = self._doc()
        doc["clips"][0]["crop_preset"] = "Some Preset That Left The Registry"
        doc["subtitles"][0]["font_family"] = "Noto Sans CJK"
        state = project_io.deserialize_project(doc)
        self.assertEqual(state.clips[0].crop_preset,
                         "Some Preset That Left The Registry")
        self.assertEqual(state.subtitles[0].font_family, "Noto Sans CJK")


class TimeDomainTests(_TempTree):
    """Group P2 - the time fields need their own domain, not just a
    magnitude ceiling.

    `Clip.__post_init__` and `AddedAudio.__post_init__` rewrite
    `src_end <= 0` to the media duration, and `src_span` masks an
    inverted range with `max(0.001, ...)`. Both are silent: a malformed
    document would load as a *different* project than the one on disk.
    """

    def _doc(self, **clip_fields: object) -> dict:
        a = _asset(self.media("v.mp4"), duration=600.0)
        doc = project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_start=1.0, src_end=5.0)],
            added_audios=[AddedAudio(path=self.media("x.mp3"), duration=9.0)],
        ))
        doc["clips"][0].update(clip_fields)
        return doc

    def test_p2_negative_times_rejected(self) -> None:
        cases = (
            ("clips", "timeline_start"), ("clips", "src_start"),
            ("clips", "src_end"), ("assets", "duration"),
            ("added_audio", "duration"), ("added_audio", "offset"),
            ("added_audio", "src_start"), ("added_audio", "src_end"),
        )
        for container, field in cases:
            with self.subTest(field=f"{container}.{field}"):
                doc = self._doc()
                doc[container][0][field] = -1.0
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(doc)

    def test_p2_audio_offset_stays_signed(self) -> None:
        """`Clip.audio_offset` is a drift correction and is legitimately
        negative, so the non-negative rule must not reach it."""
        for value in (-0.25, -5.0, 0.0, 0.25):
            with self.subTest(value=value):
                state = project_io.deserialize_project(
                    self._doc(audio_offset=value))
                self.assertEqual(state.clips[0].audio_offset, value)

    def test_p2_inverted_source_range_rejected(self) -> None:
        doc = self._doc(src_start=5.0, src_end=1.0)
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(doc)
        doc = self._doc()
        doc["added_audio"][0]["src_start"] = 8.0
        doc["added_audio"][0]["src_end"] = 2.0
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(doc)

    def test_p2_degenerate_zero_range_accepted(self) -> None:
        """`_append_added_audio` falls back to `dur = 0.0` when the probe
        fails, and `__post_init__` then leaves `src_end` at 0 - so
        `src_start == src_end == 0` is a state the app really produces.
        The ordering rule is `<=`, not `<`, precisely so this loads."""
        doc = self._doc()
        doc["added_audio"][0].update(duration=0.0, src_start=0.0, src_end=0.0)
        state = project_io.deserialize_project(doc)
        self.assertEqual(state.added_audios[0].src_end, 0.0)

    def test_p2_source_end_beyond_media_duration_rejected(self) -> None:
        """The trim controls are `setRange(0.0, clip.asset.duration)`, so
        a source range past the media is not something the editor can
        make - but ffmpeg would be asked to seek there."""
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(self._doc(src_end=601.0))
        doc = self._doc()
        doc["added_audio"][0]["src_end"] = 99.0
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(doc)

    def test_p2_source_end_exactly_at_media_duration_accepted(self) -> None:
        state = project_io.deserialize_project(self._doc(src_end=600.0))
        self.assertEqual(state.clips[0].src_end, 600.0)

    def test_p2_src_end_is_never_silently_rewritten(self) -> None:
        """The drift this closes.

        `__post_init__` reads `src_end <= 0` as "use the whole media", so
        a stored zero used to come back as the full 600 s asset - the
        project loading as a different edit than the file describes.
        Refusing is the only honest answer: the model has no way to hold
        the value the document asked for.
        """
        doc = self._doc(src_start=0.0, src_end=0.0)
        with self.assertRaises(project_io.ProjectError) as ctx:
            project_io.deserialize_project(doc)
        self.assertIn("silently expand", str(ctx.exception))
        # ...but only where the model would actually change it. A
        # zero-length medium keeps a zero range, so it still loads.
        doc = self._doc()
        doc["assets"][0]["duration"] = 0.0
        doc["clips"][0].update(src_start=0.0, src_end=0.0)
        state = project_io.deserialize_project(doc)
        self.assertEqual(state.clips[0].src_end, 0.0)


class IdentityTests(_TempTree):
    """Group P3 - persisted ids must be present and non-empty.

    A missing id is minted fresh on every load, so the document changes
    every time it is opened and saved. An empty id collides with the
    timeline's own no-selection sentinel (`_selected_id = ""`), leaving
    an entry that cannot be selected, edited or deleted.
    """

    def _doc(self) -> dict:
        a = _asset(self.media("v.mp4"))
        return project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_end=5.0)],
            added_audios=[AddedAudio(path=self.media("x.mp3"), duration=5.0)],
            subtitles=[SubtitleTrack(path=self.media("s.srt"))],
        ))

    def test_p3_missing_id_rejected(self) -> None:
        for container in ("assets", "clips", "added_audio", "subtitles"):
            with self.subTest(container):
                doc = self._doc()
                del doc[container][0]["id"]
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(doc)

    def test_p3_empty_id_rejected(self) -> None:
        for container in ("assets", "clips", "added_audio", "subtitles"):
            with self.subTest(container):
                doc = self._doc()
                doc[container][0]["id"] = ""
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(doc)

    def test_p3_ids_survive_a_load_save_cycle_byte_for_byte(self) -> None:
        doc = self._doc()
        state = project_io.deserialize_project(doc)
        self.assertEqual(project_io.serialize_project(state), doc)


class ActiveSubtitleTests(_TempTree):
    """Group P4 - at most one subtitle may be active.

    `_activate_sub` clears every other flag, and the exporter takes the
    first active track. A document with two would leave the model, the
    bin, the preview and the export disagreeing about which one is on.
    """

    def _doc(self, *active: bool) -> dict:
        subs = [SubtitleTrack(path=self.media(f"s{i}.srt"), active=flag)
                for i, flag in enumerate(active)]
        return project_io.serialize_project(_state(subtitles=subs))

    def test_p4_zero_or_one_active_accepted(self) -> None:
        for flags in ((), (False,), (True,), (True, False),
                      (False, True), (False, False)):
            with self.subTest(flags=flags):
                state = project_io.deserialize_project(self._doc(*flags))
                self.assertLessEqual(
                    sum(1 for s in state.subtitles if s.active), 1)

    def test_p4_two_active_rejected(self) -> None:
        for flags in ((True, True), (True, False, True), (True, True, True)):
            with self.subTest(flags=flags):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(self._doc(*flags))


class MixGainPrecisionTests(_WinCase):
    """Group P5 - the mix gains must be representable by the control that
    receives them.

    `_apply_project_state` writes both gains into `QDoubleSpinBox`es on
    every load, and a spin box keeps `decimals()` places. A schema-valid
    `1.234` therefore becomes `1.23` in the widget, and the next save
    writes that back: opening and resaving would quietly edit the
    project. This is the only field pair with that exposure, because
    speed, per-item volume and the trims stay on the model at load time
    and only meet a widget if the user opens a dialog.
    """

    def test_p5_control_precision_matches_the_schema_constant(self) -> None:
        """Lock the constant to the widget, so changing one without the
        other cannot pass unnoticed."""
        w = self.win()
        self.assertEqual(w.audio_gain.decimals(),
                         project_io.MIX_GAIN_DECIMALS)
        self.assertEqual(w.orig_gain.decimals(),
                         project_io.MIX_GAIN_DECIMALS)

    def test_p5_unrepresentable_gain_rejected(self) -> None:
        for value in (1.234, 0.005, 2.9999, 1.001):
            for field in ("added_gain", "original_gain"):
                with self.subTest(field=field, value=value):
                    doc = project_io.serialize_project(_state())
                    doc["audio_mix"][field] = value
                    with self.assertRaises(project_io.ProjectError):
                        project_io.deserialize_project(doc)

    def test_p5_representable_gain_accepted(self) -> None:
        for value in (0.0, 0.5, 1.0, 1.23, 2.75, 3.0):
            for field in ("added_gain", "original_gain"):
                with self.subTest(field=field, value=value):
                    doc = project_io.serialize_project(_state())
                    doc["audio_mix"][field] = value
                    state = project_io.deserialize_project(doc)
                    self.assertEqual(getattr(state, field), value)

    def test_p5_two_decimal_gain_survives_open_then_save(self) -> None:
        """The drift this closes, end to end through the real widgets."""
        p = self.proj("gains.coveproj")
        project_io.save_project(p, project_io.ProjectState(
            added_gain=1.23, original_gain=2.75))
        before = json.loads(p.read_text(encoding="utf-8"))
        w = self.win()
        self.assertTrue(w._open_project_path(p))
        self.assertTrue(w._save_project_to(p))
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), before)
        self.assertEqual(w._project_state().added_gain, 1.23)
        self.assertEqual(w._project_state().original_gain, 2.75)


class CollectionSizeTests(_TempTree):
    """Group P8 - a project may not describe more items than opening it
    can survive.

    `_apply_project_state` starts up to two ffmpeg-backed analysis
    threads per clip and one per added-audio entry, and it does so
    *after* the previous session has been replaced. A document can reuse
    one valid asset across any number of unique ids, so nothing about
    media validation bounds this.
    """

    def _doc(self, clips: int = 1, audios: int = 0) -> dict:
        a = _asset(self.media("v.mp4"))
        return project_io.serialize_project(_state(
            assets=[a],
            clips=[Clip(asset=a, src_start=0.0, src_end=1.0)
                   for _ in range(clips)],
            added_audios=[AddedAudio(path=self.media("x.mp3"), duration=5.0)
                          for _ in range(audios)],
        ))

    def test_p8_ordinary_and_at_cap_collections_accepted(self) -> None:
        cap = project_io.MAX_COLLECTION_ITEMS
        for count in (0, 1, 50, cap):
            with self.subTest(clips=count):
                state = project_io.deserialize_project(self._doc(clips=count))
                self.assertEqual(len(state.clips), count)

    def test_p8_oversized_collections_rejected(self) -> None:
        cap = project_io.MAX_COLLECTION_ITEMS
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(self._doc(clips=cap + 1))
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(self._doc(audios=cap + 1))

    def test_p8_cap_is_well_clear_of_a_real_project(self) -> None:
        """It must not creep down onto plausible edits: the app's own
        'this is a lot of files' line is 50, and it still allows more."""
        self.assertGreaterEqual(project_io.MAX_COLLECTION_ITEMS,
                                10 * app_mod.FOLDER_IMPORT_WARN_THRESHOLD)


class CollectionSizeTransactionalTests(_WinCase):
    def test_p8_oversized_project_leaves_the_session_untouched(self) -> None:
        w = self.win()
        self.populated(w)
        good = self.proj("a.coveproj")
        self.assertTrue(w._save_project_to(good))
        before = _fingerprint(w)

        doc = json.loads(good.read_text(encoding="utf-8"))
        template = doc["clips"][0]
        doc["clips"] = [
            dict(template, id=f"c{i:07d}")
            for i in range(project_io.MAX_COLLECTION_ITEMS + 1)
        ]
        bad = self.proj("huge.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")

        with unittest.mock.patch.object(
            MainWindow, "_apply_project_state",
        ) as apply_, unittest.mock.patch.object(
            MainWindow, "_kick_off_thumbs",
        ) as thumbs:
            self.assertFalse(w._open_project_path(bad))
        apply_.assert_not_called()
        thumbs.assert_not_called()
        self.assertEqual(_fingerprint(w), before)


class StopWorkersBudgetTests(_WinCase):
    """Group P9 - the shutdown wait is one budget, not one per thread.

    `_stop_media_workers` waited `wait_ms` for every worker in turn, so a
    project open behind N stalled workers blocked for N * wait_ms. The
    budget is now shared: each thread gets whatever is left, and anything
    that does not make it is retired.
    """

    def _stalled(self, count: int) -> threading.Event:
        release = threading.Event()
        self.addCleanup(release.set)

        class _BlockingThread(QThread):
            def run(self) -> None:  # noqa: D102
                release.wait(30)

        w = self.win()
        self._w = w
        for i in range(count):
            thread = _BlockingThread()
            thread.start()
            self.addCleanup(lambda t=thread: (release.set(), t.wait(5000)))
            while not thread.isRunning():
                QApplication.processEvents()
            w._thumb_threads[f"stalled{i}"] = thread
            w._thumb_workers[f"stalled{i}"] = _StubWorker()
        return release

    def test_p9_total_wait_is_one_shared_budget(self) -> None:
        """Cost, not just outcome: with 8 stalled workers and a 120 ms
        budget the old code blocked for ~960 ms."""
        budget_ms = 120
        release = self._stalled(8)
        started = time.monotonic()
        self._w._stop_media_workers(wait_ms=budget_ms)
        elapsed_ms = (time.monotonic() - started) * 1000.0

        self.assertLess(elapsed_ms, budget_ms * 3,
                        f"shutdown took {elapsed_ms:.0f} ms for a "
                        f"{budget_ms} ms budget")
        # And every one of them is still owned, not dropped.
        self.assertEqual(len(self._w._retired_media), 8)
        self.assertEqual(self._w._thumb_threads, {})
        release.set()

    def test_p9_threads_that_stop_are_still_joined_normally(self) -> None:
        w = self.win()
        thread = QThread()
        thread.start()
        self.addCleanup(lambda: (thread.quit(), thread.wait(5000)))
        w._thumb_threads["ok"] = thread
        w._thumb_workers["ok"] = _StubWorker()
        w._stop_media_workers()
        self.assertFalse(thread.isRunning())
        self.assertEqual(w._retired_media, [])


class ImageDurationTests(_TempTree):
    """Group P7 - a still image has a tighter duration domain than the
    generic timeline bound.

    `clip.py` already defines `IMAGE_ASSET_DURATION_CAP` and the
    properties dialog trims a still against it, so an image asset may not
    carry the 30-day ceiling that video and audio use.
    """

    def _doc(self, kind: str, duration: float) -> dict:
        a = _asset(self.media("m.mp4"), kind=kind, duration=duration)
        # The clip has to stay inside its own media: the source-range rule
        # from the previous slice is not what these cases are testing.
        clip = Clip(asset=a, timeline_start=0.0, src_start=0.0,
                    src_end=min(1.0, duration))
        return project_io.serialize_project(_state(assets=[a], clips=[clip]))

    def test_b1_image_duration_at_the_cap_accepted(self) -> None:
        cap = IMAGE_ASSET_DURATION_CAP
        for duration in (0.0, 5.0, cap):
            with self.subTest(duration=duration):
                state = project_io.deserialize_project(
                    self._doc("image", duration))
                self.assertEqual(state.assets[0].duration, duration)

    def test_b2_image_duration_just_above_the_cap_rejected(self) -> None:
        with self.assertRaises(project_io.ProjectError):
            project_io.deserialize_project(
                self._doc("image", IMAGE_ASSET_DURATION_CAP + 0.1))

    def test_b3_huge_image_duration_rejected(self) -> None:
        for duration in (3600.0, 86400.0, project_io.MAX_TIME_SECONDS):
            with self.subTest(duration=duration):
                with self.assertRaises(project_io.ProjectError):
                    project_io.deserialize_project(self._doc("image", duration))

    def test_b4_non_image_duration_above_the_image_cap_still_accepted(
        self,
    ) -> None:
        """Mandatory: the image cap must not leak onto every asset kind.
        A one-hour video is entirely ordinary."""
        for kind in ("video", "audio"):
            for duration in (3600.0, 86400.0, project_io.MAX_TIME_SECONDS):
                with self.subTest(kind=kind, duration=duration):
                    doc = self._doc(kind, duration)
                    if kind == "audio":
                        doc["clips"] = []
                    state = project_io.deserialize_project(doc)
                    self.assertEqual(state.assets[0].duration, duration)

    def test_b_image_cap_comes_from_the_production_constant(self) -> None:
        self.assertEqual(project_io.MAX_IMAGE_DURATION,
                         IMAGE_ASSET_DURATION_CAP)


class ImageDurationTransactionalTests(_WinCase):
    def test_b5_invalid_image_duration_leaves_the_session_untouched(
        self,
    ) -> None:
        w = self.win()
        self.populated(w)
        good = self.proj("a.coveproj")
        self.assertTrue(w._save_project_to(good))
        before = _fingerprint(w)

        doc = json.loads(good.read_text(encoding="utf-8"))
        image = next(a for a in doc["assets"] if a["kind"] == "image")
        image["duration"] = 99999.0
        bad = self.proj("bigimage.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")

        with unittest.mock.patch.object(
            MainWindow, "_apply_project_state",
        ) as apply_:
            self.assertFalse(w._open_project_path(bad))
        apply_.assert_not_called()
        self.assertEqual(_fingerprint(w), before)
        self.assertEqual(w._current_project_path, good)

    def test_b6_accepted_image_duration_survives_open_then_save(self) -> None:
        w = self.win()
        self.populated(w)
        p = self.proj("img.coveproj")
        doc_path = p
        self.assertTrue(w._save_project_to(doc_path))
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        image = next(a for a in doc["assets"] if a["kind"] == "image")
        image["duration"] = float(IMAGE_ASSET_DURATION_CAP)
        doc_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        w2 = self.win()
        self.assertTrue(w2._open_project_path(doc_path))
        self.assertTrue(w2._save_project_to(doc_path))
        after = json.loads(doc_path.read_text(encoding="utf-8"))
        got = next(a for a in after["assets"] if a["kind"] == "image")
        self.assertEqual(got["duration"], float(IMAGE_ASSET_DURATION_CAP))


class ClipAssetKindTests(_TempTree):
    """Group P6 - a timeline clip may only reference visual media.

    `_insert_clip_at` refuses anything that is not video or image, and an
    audio drop becomes an added-audio entry instead. A clip pointing at
    an audio asset would be handed to thumbnail generation and to the
    video branch of the exporter, neither of which has a stream to work
    with.
    """

    def _doc(self, kind: str) -> dict:
        a = _asset(self.media("m.mp4"), kind=kind)
        return project_io.serialize_project(_state(
            assets=[a], clips=[Clip(asset=a, src_end=5.0)]))

    def test_p6_video_and_image_clips_accepted(self) -> None:
        for kind in ("video", "image"):
            with self.subTest(kind=kind):
                state = project_io.deserialize_project(self._doc(kind))
                self.assertEqual(state.clips[0].asset.kind, kind)

    def test_p6_clip_on_an_audio_asset_rejected(self) -> None:
        with self.assertRaises(project_io.ProjectError) as ctx:
            project_io.deserialize_project(self._doc("audio"))
        self.assertIn("audio", str(ctx.exception))

    def test_p6_audio_asset_may_still_sit_in_the_bin(self) -> None:
        """Bin-only audio assets are ordinary: browse-import adds them
        with no timeline clip, so only the *reference* is refused."""
        a = _asset(self.media("only.mp3"), kind="audio")
        state = project_io.deserialize_project(
            project_io.serialize_project(_state(assets=[a])))
        self.assertEqual(state.assets[0].kind, "audio")
        self.assertEqual(state.clips, [])


class BoundaryRoundTripTests(_WinCase):
    """A project holding representative extremes of every newly bounded
    field must survive load -> save with no silent normalization.

    The audio-mix gains are the sharp case: they land in
    `QDoubleSpinBox.setValue()`, which clamps in silence, so a drifted
    value would be written back on the next save.
    """

    def _edge_project(self) -> Path:
        a = _asset(self.media("edge.mp4"), w=65535, h=65535,
                   duration=1.0, fps=1000.0)
        clip = Clip(asset=a, timeline_start=0.0, src_start=0.0, src_end=1.0,
                    speed=project_io.MAX_CLIP_SPEED, audio_volume=2.0,
                    crop_rect=(0.0, 0.0, 1.0, 1.0))
        audio = AddedAudio(path=self.media("edge.mp3"), duration=1.0,
                           rate=192000, offset=0.0,
                           lane=project_io.MAX_AUDIO_LANE,
                           src_start=0.0, src_end=1.0, volume=2.0)
        sub = SubtitleTrack(path=self.media("edge.srt"), font_size=120,
                            primary_color="#0A1B2C", outline_color="#000000",
                            outline=8, position="top", active=True,
                            offset_ms=30000)
        state = project_io.ProjectState(
            assets=[a], clips=[clip], added_audios=[audio], subtitles=[sub],
            replace_added_audio=True, added_gain=3.0, original_gain=0.0,
        )
        p = self.proj("edge.coveproj")
        project_io.save_project(p, state)
        return p

    def test_edge_values_survive_open_then_immediate_save(self) -> None:
        p = self._edge_project()
        before = json.loads(p.read_text(encoding="utf-8"))

        w = self.win()
        self.assertTrue(w._open_project_path(p))
        # Saving straight back must be a no-op on every value.
        self.assertTrue(w._save_project_to(p))
        after = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(after, before)

    def test_audio_mix_gains_are_not_clamped_by_the_spin_boxes(self) -> None:
        p = self._edge_project()
        w = self.win()
        self.assertTrue(w._open_project_path(p))
        self.assertAlmostEqual(w.audio_gain.value(), 3.0)
        self.assertAlmostEqual(w.orig_gain.value(), 0.0)
        self.assertAlmostEqual(w._project_state().added_gain, 3.0)
        self.assertAlmostEqual(w._project_state().original_gain, 0.0)

    def test_subtitle_edge_values_survive_into_the_session(self) -> None:
        p = self._edge_project()
        w = self.win()
        self.assertTrue(w._open_project_path(p))
        sub = w._subs[0]
        self.assertEqual(sub.font_size, 120)
        self.assertEqual(sub.outline, 8)
        self.assertEqual(sub.offset_ms, 30000)
        self.assertEqual(sub.position, "top")
        self.assertEqual(sub.primary_color, "#0A1B2C")


class SubtitleCueParseTests(_WinCase):
    """The cue re-parse happens *after* the session has been cleared.

    ``_apply_project_state`` re-reads every subtitle file rather than
    trusting cached cues, and it does so past the destructive commit
    point - so any input that makes ``parse_sub_cues`` raise turns a
    rejected project into a half-loaded one, with the previous project
    still selected as the save target. The parser documents itself as
    best-effort ("unknown formats / read errors produce an empty list"),
    which is exactly the contract that has to hold for the transactional
    open above it to mean anything.

    The timestamp field is unbounded in the cue regex, so converting it is
    where a real .srt can break that promise - in two different places
    either side of ``sys.get_int_max_str_digits()``, which is why both are
    exercised rather than one arbitrary digit count.
    """

    #: Just under the interpreter's digit limit: the string converts fine
    #: and the *arithmetic* overflows on the way to a float.
    ARITHMETIC = "9" * (sys.get_int_max_str_digits() - 300)
    #: Past the limit: ``int()`` refuses the string before any arithmetic.
    CONVERSION = "9" * (sys.get_int_max_str_digits() + 100)

    @staticmethod
    def _srt(hours: str) -> str:
        return ("1\n" + hours + ":00:00,000 --> "
                + hours + ":00:01,000\nhello\n")

    def test_an_unparseable_timestamp_yields_no_cues_rather_than_raising(
        self,
    ) -> None:
        from cove_video_editor.clip import parse_sub_cues
        for name, hours in (("arithmetic overflow", self.ARITHMETIC),
                            ("conversion refused", self.CONVERSION)):
            with self.subTest(name):
                srt = self.tmp / f"{name.replace(' ', '_')}.srt"
                srt.write_text(self._srt(hours), encoding="utf-8")
                self.assertEqual(parse_sub_cues(srt), [])

    def test_only_the_start_being_unparseable_still_drops_the_cue(
        self,
    ) -> None:
        """An unrepresentable timestamp is not a timestamp of zero.

        Collapsing it to 0.0 is only self-cancelling when *both* ends
        collapse. With a normal end the cue passes the `end > start` check
        and lands at the head of the timeline - a cue the file never
        contained, shown in the preview but not produced on export.
        """
        from cove_video_editor.clip import parse_sub_cues
        for name, line in (
            ("start", f"{self.CONVERSION}:00:00,000 --> 00:00:05,000"),
            ("end", f"00:00:01,000 --> {self.CONVERSION}:00:05,000"),
            ("start arithmetic",
             f"{self.ARITHMETIC}:00:00,000 --> 00:00:05,000"),
        ):
            with self.subTest(name):
                srt = self.tmp / f"half_{name.replace(' ', '_')}.srt"
                srt.write_text(f"1\n{line}\nhello\n", encoding="utf-8")
                self.assertEqual(parse_sub_cues(srt), [])

    def test_ordinary_cues_are_still_parsed(self) -> None:
        """The guard must not swallow timestamps that are simply large."""
        srt = self.tmp / "ok.srt"
        srt.write_text("1\n99:59:59,999 --> 100:00:01,000\nhello\n",
                       encoding="utf-8")
        from cove_video_editor.clip import parse_sub_cues
        cues = parse_sub_cues(srt)
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0][0], 99 * 3600 + 59 * 60 + 59.999)
        self.assertEqual(cues[0][2], "hello")

    def test_a_malformed_subtitle_file_does_not_half_load_a_project(
        self,
    ) -> None:
        """The whole point of the transactional open, asserted against the
        one step that runs after the commit."""
        srt = self.media("bad.srt")
        srt.write_text(self._srt(self.CONVERSION), encoding="utf-8")
        p = self.proj()
        project_io.save_project(p, _state(
            subtitles=[SubtitleTrack(path=srt, active=True)]))

        w = self.win()
        self.populated(w)
        before = project_io.serialize_project(w._project_state())

        self.assertTrue(w._open_project_path(p))
        # Loaded, not aborted: an unreadable *cue list* is a cosmetic
        # loss, so the project itself still opens - with no cues.
        self.assertEqual(len(w._subs), 1)
        self.assertEqual(w._subs[0].cues, [])
        self.assertNotEqual(
            project_io.serialize_project(w._project_state()), before)


class RangeTransactionalTests(_WinCase):
    """An out-of-domain scalar must be refused before the commit point,
    exactly like every other invalid document."""

    def setUp(self) -> None:
        super().setUp()
        self.w = self.win()
        self.populated(self.w)
        self.project_a = self.proj("a.coveproj")
        self.assertTrue(self.w._save_project_to(self.project_a))
        self.before = _fingerprint(self.w)

    def test_out_of_range_scalar_leaves_project_a_intact(self) -> None:
        cases = (
            ("assets", "width", 10**12),
            ("assets", "fps", 1e9),
            ("assets", "kind", "sculpture"),
            ("clips", "audio_volume", 99.0),
            ("added_audio", "volume", 99.0),
            ("subtitles", "font_size", 10**9),
            ("subtitles", "outline", 99),
            ("subtitles", "offset_ms", 10**12),
            ("subtitles", "position", "sideways"),
            ("subtitles", "primary_color", "#GGGGGG"),
        )
        for container, field, value in cases:
            with self.subTest(field=f"{container}.{field}"):
                doc = json.loads(self.project_a.read_text(encoding="utf-8"))
                doc[container][0][field] = value
                bad = self.proj(f"bad-{container}-{field}.coveproj")
                bad.write_text(json.dumps(doc), encoding="utf-8")
                with unittest.mock.patch.object(
                    MainWindow, "_apply_project_state",
                ) as apply_:
                    self.assertFalse(self.w._open_project_path(bad))
                apply_.assert_not_called()
                self.assertEqual(_fingerprint(self.w), self.before)
                self.assertEqual(self.w._current_project_path, self.project_a)

    def test_out_of_range_audio_mix_gain_leaves_project_a_intact(self) -> None:
        for field in ("added_gain", "original_gain"):
            with self.subTest(field=field):
                doc = json.loads(self.project_a.read_text(encoding="utf-8"))
                doc["audio_mix"][field] = 99.0
                bad = self.proj(f"bad-{field}.coveproj")
                bad.write_text(json.dumps(doc), encoding="utf-8")
                with unittest.mock.patch.object(
                    MainWindow, "_apply_project_state",
                ) as apply_:
                    self.assertFalse(self.w._open_project_path(bad))
                apply_.assert_not_called()
                self.assertEqual(_fingerprint(self.w), self.before)

    def test_no_workers_start_for_a_rejected_project(self) -> None:
        doc = json.loads(self.project_a.read_text(encoding="utf-8"))
        doc["assets"][0]["width"] = 10**12
        bad = self.proj("nostart.coveproj")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        with unittest.mock.patch.object(
            MainWindow, "_kick_off_thumbs",
        ) as thumbs, unittest.mock.patch.object(
            MainWindow, "_stop_media_workers",
        ) as stop:
            self.assertFalse(self.w._open_project_path(bad))
        thumbs.assert_not_called()
        stop.assert_not_called()


# --- Group O + path edge cases: structural -----------------------------


class StructuralTests(_TempTree):
    def test_o_module_uses_json_and_never_pickle(self) -> None:
        src = Path(project_io.__file__).read_text(encoding="utf-8")
        for banned in ("pickle", "marshal", "shelve", "eval(", "exec(",
                       "__import__"):
            self.assertNotIn(banned, src)
        self.assertIn("import json", src)

    def test_o_module_owns_no_widgets_or_file_dialogs(self) -> None:
        src = Path(project_io.__file__).read_text(encoding="utf-8")
        for banned in ("QWidget", "QMainWindow", "QMessageBox",
                       "QtWidgets import", "QDialog"):
            self.assertNotIn(banned, src)

    def test_o_schema_version_is_an_explicit_integer(self) -> None:
        self.assertIsInstance(project_io.SCHEMA_VERSION, int)
        self.assertEqual(project_io.SCHEMA_VERSION, 1)
        self.assertEqual(project_io.FORMAT, "cove-video-editor-project")
        self.assertEqual(project_io.PROJECT_EXT, ".coveproj")

    def test_o_no_export_runtime_fields_in_the_schema(self) -> None:
        # A path outside the pytest temp tree: "/tmp/..." would trip the
        # "tmp" needle below for reasons that have nothing to do with the
        # schema. Serialization never touches the filesystem.
        a = _asset(Path("/media/library/v.mp4"))
        doc = project_io.serialize_project(
            _state(assets=[a], clips=[Clip(asset=a)]))
        flat = json.dumps(doc)
        for banned in ("encoder", "fmt_key", "output", "tmp", "progress",
                       "nvenc", "amf"):
            self.assertNotIn(banned, flat)

    def test_o_no_new_dependencies(self) -> None:
        src = Path(project_io.__file__).read_text(encoding="utf-8")
        allowed = {"__future__", "json", "math", "re", "dataclasses",
                   "pathlib", "PySide6.QtCore", ".clip"}
        found = {
            line.split()[1]
            for line in src.splitlines()
            if line.startswith(("import ", "from ")) and len(line.split()) > 1
        }
        self.assertLessEqual(found, allowed, f"unexpected imports: {found}")


class PathEdgeCaseTests(_WinCase):
    def test_extension_is_appended_when_missing(self) -> None:
        got = project_io.normalize_project_path(self.tmp / "holiday")
        self.assertEqual(got.name, "holiday.coveproj")

    def test_existing_extension_is_left_alone(self) -> None:
        got = project_io.normalize_project_path(self.tmp / "holiday.coveproj")
        self.assertEqual(got.name, "holiday.coveproj")

    def test_extension_match_is_case_insensitive(self) -> None:
        got = project_io.normalize_project_path(self.tmp / "holiday.COVEPROJ")
        self.assertEqual(got.name, "holiday.COVEPROJ")

    def test_multiple_dots_keep_every_component(self) -> None:
        got = project_io.normalize_project_path(self.tmp / "my.edit.v2")
        self.assertEqual(got.name, "my.edit.v2.coveproj")

    def test_round_trip_through_spaces_and_unicode_paths(self) -> None:
        w = self.win()
        media = self.media("séquence finale/vidéo été (1).mp4")
        a = _asset(media)
        w._assets[a.id] = a
        w.clip_bin.add_asset(a)
        c = Clip(asset=a, timeline_start=0.0, src_start=0.0, src_end=3.0)
        w._clips = [c]
        w.timeline.set_clips(w._clips)
        p = self.proj("mon projet été.coveproj")
        self.assertTrue(w._save_project_to(p))

        w2 = self.win()
        self.assertTrue(w2._open_project_path(p))
        self.assertEqual(w2._clips[0].asset.path, media)
        self.assertEqual(w2._clips[0].path.name, "vidéo été (1).mp4")


class _FakeExportWorker(QObject):
    """An ``ExportWorker`` stand-in with the production signal shapes.

    Every signal below matches `exporter.ExportWorker` exactly - including
    `finished(Path)` carrying the real destination rather than a bare
    string - because the window's slots are connected to *these* and a
    mismatched shape would test a connection production never makes.

    It stays on the GUI thread deliberately. The window connects with
    `Qt.QueuedConnection`, which posts an event even within one thread, so
    emitting here queues a callback that is delivered on the next
    `processEvents()` - which is exactly the ordering the defect needs and
    the only way to hold callbacks across a project swap without sleeping.
    """

    progress = Signal(int)
    eta = Signal(float)
    log = Signal(str)
    finished = Signal(Path)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1


class StaleExportCallbackTests(_WinCase):
    """An export that outlives its project must not drive the next one.

    Opening a project has never stopped a running export, and this slice
    does not change that. What it fixes is ownership: once the open
    commits, the outgoing run's callbacks are addressed at a window whose
    session it knows nothing about, and they carry progress, log lines, a
    completion summary and - worst - a "Show in folder" target for a file
    the newly opened timeline never produced.

    Disconnecting the worker at the swap would not be enough on its own: a
    queued signal emitted *before* the disconnect is still delivered
    afterwards. So every case here emits first, opens second, and only
    then lets Qt deliver.
    """

    def setUp(self) -> None:
        super().setUp()
        self.threads: list[QThread] = []
        self.addCleanup(self._stop_threads)
        dlg = unittest.mock.patch.object(
            app_mod.QFileDialog, "getSaveFileName",
            side_effect=lambda *a, **k: (str(self.tmp / "out.mp4"), ""))
        dlg.start()
        self.addCleanup(dlg.stop)

    def _stop_threads(self) -> None:
        for t in self.threads:
            t.quit()
            t.wait(5000)

    def _fake_start_export(self, job):  # noqa: ANN001, ANN202
        """`exporter.start_export`'s contract: a started thread and a live
        worker. The thread really runs (production starts it, and
        `_reset_after_export` hangs off its `finished`); the worker stays
        here so the test can emit on demand."""
        thread = QThread()
        self.threads.append(thread)
        return thread, _FakeExportWorker()

    def _exporting(self) -> tuple[MainWindow, _FakeExportWorker]:
        """A window with a populated session and one export in flight."""
        w = self.win()
        self.populated(w)
        with unittest.mock.patch.object(
            app_mod, "start_export", self._fake_start_export,
        ):
            w._on_export_clicked()
        worker = w._export_worker
        self.assertIsInstance(worker, _FakeExportWorker)
        return w, worker

    def _project_b(self, w: MainWindow) -> Path:
        p = self.proj("b.coveproj")
        self.assertTrue(w._save_project_to(p))
        return p

    @staticmethod
    def _export_ui(w: MainWindow) -> tuple:
        """Everything an export callback is able to reach."""
        return (
            w._last_export_output,
            w.show_folder_btn.isVisibleTo(w),
            w.progress.value(),
            w.progress.format(),
            w.export_log.toPlainText(),
            w._last_progress,
            w._last_eta,
            w.status.currentMessage(),
        )

    @staticmethod
    def _deliver() -> None:
        for _ in range(10):
            QApplication.processEvents()

    def _stale(self, emit) -> tuple[MainWindow, tuple, tuple]:
        """Run the whole reproducer for one callback.

        1-3. export A queues a callback; 4. project B commits; 5. the
        queued callback is delivered; the two snapshots are what step 6
        compares.
        """
        w, worker = self._exporting()
        p = self._project_b(w)
        emit(worker)
        self.assertTrue(w._open_project_path(p))
        before = self._export_ui(w)
        self._deliver()
        return w, before, self._export_ui(w)

    # -- A: progress ----------------------------------------------------

    def test_stale_progress_does_not_move_the_new_session_bar(self) -> None:
        w, before, after = self._stale(lambda k: k.progress.emit(73))
        self.assertEqual(after, before)
        self.assertNotEqual(w._last_progress, 73)

    def test_stale_eta_does_not_retitle_the_new_session_bar(self) -> None:
        w, before, after = self._stale(lambda k: k.eta.emit(125.0))
        self.assertEqual(after, before)
        self.assertIsNone(w._last_eta)

    # -- B: log / status ------------------------------------------------

    def test_stale_log_does_not_reach_the_new_session_log(self) -> None:
        w, before, after = self._stale(
            lambda k: k.log.emit("$ ffmpeg -i old-project.mp4"))
        self.assertEqual(after, before)
        self.assertNotIn("old-project", w.export_log.toPlainText())

    # -- C: finished ----------------------------------------------------

    def test_stale_finished_cannot_claim_the_new_session(self) -> None:
        out = self.media("stale-export.mp4")
        w, before, after = self._stale(lambda k: k.finished.emit(out))
        self.assertEqual(after, before)
        self.assertIsNone(w._last_export_output)
        self.assertNotIn("stale-export", w.export_log.toPlainText())

    def test_stale_finished_cannot_expose_show_in_folder(self) -> None:
        out = self.media("stale-export.mp4")
        w, _before, _after = self._stale(lambda k: k.finished.emit(out))
        self.assertFalse(w.show_folder_btn.isVisibleTo(w))
        # The action is not merely hidden - there is nothing behind it.
        with unittest.mock.patch.object(app_mod, "_open_local") as opener:
            w._on_show_in_folder()
        opener.assert_not_called()

    # -- D: cancelled ---------------------------------------------------

    def test_stale_cancellation_does_not_annotate_the_new_session(self) -> None:
        w, before, after = self._stale(lambda k: k.cancelled.emit())
        self.assertEqual(after, before)
        self.assertNotIn("cancelled", w.export_log.toPlainText().lower())

    # -- E: failed ------------------------------------------------------

    def test_stale_failure_does_not_alarm_the_new_session(self) -> None:
        w, before, after = self._stale(lambda k: k.failed.emit("ffmpeg died"))
        self.assertEqual(after, before)
        self.assertNotIn("ffmpeg died", w.export_log.toPlainText())
        # A modal about the *previous* project's export would be the most
        # visible form of this bug.
        self.warned.assert_not_called()

    # -- F: the current run must still work -----------------------------

    def test_the_current_export_still_reports_normally(self) -> None:
        """No project open at all: nothing about this slice may make a
        live export stop driving its own window."""
        w, worker = self._exporting()
        out = self.media("real-export.mp4")
        worker.progress.emit(41)
        worker.eta.emit(30.0)
        worker.log.emit("$ ffmpeg -i in.mp4")
        worker.finished.emit(out)
        self._deliver()

        self.assertEqual(w._last_progress, 100)
        self.assertEqual(w._last_export_output, out)
        self.assertTrue(w.show_folder_btn.isVisibleTo(w))
        self.assertIn("real-export", w.export_log.toPlainText())
        self.assertIn("ffmpeg -i in.mp4", w.export_log.toPlainText())

    def test_a_current_failure_still_reaches_the_user(self) -> None:
        w, worker = self._exporting()
        worker.failed.emit("ffmpeg exited 1")
        self._deliver()
        self.assertIn("ffmpeg exited 1", w.export_log.toPlainText())
        self.warned.assert_called()

    def test_a_current_cancellation_still_reports(self) -> None:
        w, worker = self._exporting()
        worker.cancelled.emit()
        self._deliver()
        self.assertIn("cancelled", w.export_log.toPlainText().lower())

    # -- the commit boundary --------------------------------------------

    def test_a_rejected_open_leaves_the_export_owning_its_window(self) -> None:
        """A failed open is not an event in the export's life.

        Invalidating on *attempt* rather than on commit would silence a
        live export because the user picked the wrong file in a dialog.
        """
        w, worker = self._exporting()
        bad = self.proj("broken.coveproj")
        bad.write_text("{not json", encoding="utf-8")
        self.assertFalse(w._open_project_path(bad))

        out = self.media("real-export.mp4")
        worker.progress.emit(64)
        worker.finished.emit(out)
        self._deliver()
        self.assertEqual(w._last_export_output, out)
        self.assertTrue(w.show_folder_btn.isVisibleTo(w))

    def test_an_export_started_after_the_open_owns_the_new_window(self) -> None:
        """Ownership is per run, not a latch: opening a project must not
        poison every export that follows it."""
        w, first = self._exporting()
        p = self._project_b(w)
        self.assertTrue(w._open_project_path(p))
        self._deliver()
        # The outgoing run's thread has to release the export controls
        # before another export can start, exactly as it always did.
        w._reset_after_export()

        with unittest.mock.patch.object(
            app_mod, "start_export", self._fake_start_export,
        ):
            w._on_export_clicked()
        second = w._export_worker
        self.assertIsNot(second, first)

        out = self.media("second-export.mp4")
        second.finished.emit(out)
        self._deliver()
        self.assertEqual(w._last_export_output, out)
        self.assertTrue(w.show_folder_btn.isVisibleTo(w))


if __name__ == "__main__":
    unittest.main()
