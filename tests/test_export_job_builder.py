"""Baseline GUI characterization and shared export assembly contract."""
import os
import copy
import inspect
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog
from cove_video_editor import app as app_mod
from cove_video_editor.clip import AddedAudio, SubtitleTrack
from cove_video_editor.project_io import ProjectState
from cove_video_editor.exporter import RESOLUTION_PRESETS, added_audio_tracks, build_export_job
from cove_video_editor import ffmpeg_utils as ff
from PySide6.QtCore import QThread
from test_crop_edit_lifecycle import _asset, _clip, _win

VIDEO = "MP4 (H.264 + AAC)"
AUDIO = "WAV (audio only)"
PRESETS = (
    ("Auto (First Clip)", None),
    ("3840 × 2160", (3840, 2160)),
    ("1920 × 1080", (1920, 1080)),
    ("1280 × 720", (1280, 720)),
    ("1080 × 1920", (1080, 1920)),
    ("1080 × 1080", (1080, 1080)),
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app):
    with (
        patch.object(app_mod, "load_encoder_pref", return_value="auto"),
        patch.object(app_mod, "save_encoder_pref", return_value=None),
    ):
        w = _win([_clip(_asset())])
        try:
            yield w
        finally:
            w.close()


class StopExport(Exception):
    pass


def capture(w, region=None):
    jobs = []

    def start(job):
        jobs.append(job)
        raise StopExport

    with (
        patch.object(QFileDialog, "getSaveFileName", return_value=("/tmp/out.mp4", "")),
        patch.object(app_mod, "start_export", side_effect=start) as spy,
    ):
        with pytest.raises(StopExport):
            if region is None:
                w._on_export_clicked()
            else:
                w._on_region_export(*region)
        assert spy.call_count == 1
    assert len(jobs) == 1
    return jobs[0]


def rich_state():
    a = _asset()
    c = _clip(a, speed=2.0)
    c.src_start, c.src_end = 2.0, 8.0
    c.crop_rect = (0.1, 0.2, 0.5, 0.5)
    c.waveform_peaks = [0.2, 0.4]
    return ProjectState(
        assets=[a], clips=[c, _clip(_asset("b.mp4"), 5.0, 4.0)],
        added_audios=[
            AddedAudio(
                Path("music.wav"), duration=20, src_start=3, src_end=9,
                offset=1.5, volume=0.4, peaks=[0.3],
            ),
            AddedAudio(
                Path("muted.wav"), duration=12, src_start=2, src_end=7,
                offset=4, volume=0.7, muted=True,
            ),
        ],
        subtitles=[
            SubtitleTrack(Path("inactive.srt")),
            SubtitleTrack(
                Path("first.srt"), active=True, offset_ms=125, cues=[(1, 2, "hello")],
            ),
            SubtitleTrack(Path("second.srt"), active=True),
        ],
        added_gain=1.5, original_gain=0.25,
    )


def assert_rich(job, replace=False):
    assert [
        (c.path, c.timeline_start, c.src_start, c.src_end, c.speed) for c in job.clips
    ] == [
        (Path("a.mp4"), 0.0, 2.0, 8.0, 2.0),
        (Path("b.mp4"), 5.0, 0.0, 4.0, 1.0),
    ]
    assert job.clips[0].crop_rect == (0.1, 0.2, 0.5, 0.5)
    assert job.clips[0].waveform_peaks == [0.2, 0.4]
    assert [
        (t.path, t.replace, t.volume, t.original_volume, t.offset, t.duration, t.src_start)
        for t in job.audio_tracks
    ] == [
        (Path("music.wav"), replace, pytest.approx(0.6), 0.25, 1.5, 6, 3),
        (Path("muted.wav"), replace, 0.0, 0.25, 4, 5, 2),
    ]
    assert job.subtitles is not None
    assert job.subtitles.path == Path("first.srt")
    assert job.subtitles.active is True
    assert job.subtitles.offset_ms == 125
    assert job.subtitles.cues == [(1, 2, "hello")]
    assert job.fps is None


def test_gui_defaults(window):
    assert window.resolution_combo.currentIndex() == 0
    assert window._selected_encoder_pref() == "auto"
    job = capture(window)
    assert job.output == Path("/tmp/out.mp4")
    assert job.fmt_key == VIDEO
    assert (
        job.width, job.height, job.fps, job.crop, job.subtitles,
        job.region_start, job.region_end,
    ) == (None,) * 7
    assert job.encoder_pref == "auto"
    assert job.audio_tracks == []
    assert (
        job.clips[0].src_start, job.clips[0].src_end,
        job.clips[0].speed, job.clips[0].timeline_start,
    ) == (0, 10, 1, 0)
    assert job.clips[0] is not window._clips[0]


@pytest.mark.parametrize(
    "index,label,size", [(i, label, size) for i, (label, size) in enumerate(PRESETS)],
)
@pytest.mark.parametrize("fmt", [VIDEO, AUDIO])
def test_gui_resolutions(window, index, label, size, fmt):
    assert window.resolution_combo.count() == 6
    assert window.resolution_combo.itemText(index) == label
    assert window.resolution_combo.itemData(index) == size
    window.resolution_combo.setCurrentIndex(index)
    if fmt == AUDIO:
        window.export_type_combo.setCurrentText("Audio Only")
        assert window.format_combo.currentText() == AUDIO
    window.format_combo.setCurrentText(fmt)
    assert window.format_combo.currentText() == fmt
    job = capture(window)
    assert job.fmt_key == fmt
    assert (job.width, job.height) == (size if size and fmt == VIDEO else (None, None))


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("encoder", ["auto", "cpu", "nvenc", "amf"])
def test_gui_rich(window, replace, encoder):
    state = rich_state()
    window._clips = state.clips
    window._added_audios = state.added_audios
    window._subs = state.subtitles
    window.audio_gain.setValue(1.5)
    window.orig_gain.setValue(0.25)
    window.audio_replace_cb.setChecked(replace)
    window.encoder_combo.setCurrentIndex(window.encoder_combo.findData(encoder))
    with patch.object(window, "_crop_pixels", return_value=(10, 20, 300, 400)):
        job = capture(window, (1.25, 6.75))
    assert_rich(job, replace)
    assert job.encoder_pref == encoder
    assert job.crop is not None
    assert job.crop == (10, 20, 300, 400)
    assert job.region_start is not None
    assert job.region_end is not None
    assert (job.region_start, job.region_end) == (1.25, 6.75)
    assert window._region_export_range is None
    assert job.subtitles is not state.subtitles[1]
    assert job.subtitles.cues is not state.subtitles[1].cues
    assert state.added_audios[1].volume == 0.7
    assert state.added_audios[1].muted is True


def test_gui_inactive_subtitles(window):
    window._subs = [SubtitleTrack(Path("inactive.srt"))]
    assert capture(window).subtitles is None


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("encoder", ["auto", "cpu", "nvenc", "amf", "UNCHANGED"])
def test_builder_rich_and_read_only(replace, encoder):
    state = rich_state()
    state.replace_added_audio = replace
    snapshot = copy.deepcopy(state)
    job = build_export_job(state, output="/tmp/out.mp4", fmt_key=VIDEO,
                           size=(1280, 720), crop=(10, 20, 300, 400),
                           region=(1.25, 6.75), encoder_pref=encoder)
    assert_rich(job, replace)
    assert job.output == Path("/tmp/out.mp4")
    assert job.fmt_key == VIDEO
    assert (job.width, job.height) == (1280, 720)
    assert job.crop is not None
    assert job.crop == (10, 20, 300, 400)
    assert job.region_start is not None
    assert job.region_end is not None
    assert (job.region_start, job.region_end) == (1.25, 6.75)
    assert job.encoder_pref == encoder
    assert state == snapshot
    assert all(clone is not original for clone, original in zip(job.clips, state.clips))
    assert job.subtitles is not state.subtitles[1]
    assert job.subtitles.cues is not state.subtitles[1].cues


def test_builder_defaults():
    state = ProjectState(clips=[_clip(_asset())])
    job = build_export_job(state, output=Path("out.mp4"), fmt_key=VIDEO)
    assert (
        job.width, job.height, job.fps, job.crop, job.subtitles,
        job.region_start, job.region_end,
    ) == (None,) * 7
    assert job.audio_tracks == []
    assert job.encoder_pref == "auto"
    assert job.output == Path("out.mp4")
    assert (job.clips[0].src_start, job.clips[0].src_end, job.clips[0].speed) == (0, 10, 1)
    state.subtitles = [SubtitleTrack(Path("inactive.srt"))]
    assert build_export_job(state, output="out.mp4", fmt_key=VIDEO).subtitles is None


@pytest.mark.parametrize(
    "size", [None, (3840, 2160), (1920, 1080), (1280, 720), (1080, 1920), (1080, 1080)],
)
def test_builder_audio_only(size):
    state = rich_state()
    state.clips = []
    job = build_export_job(state, output="out.wav", fmt_key=AUDIO, size=size)
    assert (job.width, job.height, job.fps) == (None, None, None)
    assert job.clips == []
    assert job.audio_tracks[0].volume == pytest.approx(0.6)
    assert job.audio_tracks[0].src_start == 3


def test_builder_presets():
    assert RESOLUTION_PRESETS == PRESETS
    assert isinstance(RESOLUTION_PRESETS, tuple)


def test_audio_helper_read_only_no_normalization():
    audios = rich_state().added_audios
    before = copy.deepcopy(audios)
    tracks = added_audio_tracks(audios, replace=True, added_gain=1.5, original_gain=0.25)
    assert [
        (t.volume, t.src_start, t.duration, t.offset, t.replace, t.original_volume)
        for t in tracks
    ] == [
        (pytest.approx(0.6), 3, 6, 1.5, True, 0.25),
        (0, 2, 5, 4, True, 0.25),
    ]
    assert audios == before
    audios[0].volume = -2.0
    assert added_audio_tracks(
        audios, replace=False, added_gain=3.0, original_gain=-1.0,
    )[0].volume == -6.0
    assert added_audio_tracks([], replace=False, added_gain=1.0, original_gain=1.0) == []


def test_builder_zero_side_effects():
    state = rich_state()
    targets = ["subprocess.Popen", "subprocess.run", "builtins.open"]
    with ExitStack() as stack:
        spies = [stack.enter_context(patch(target)) for target in targets]
        for owner, name in [
            (ff, "probe"), (ff, "probe_audio_duration"), (ff, "hardware_encoder_available"),
            (Path, "open"), (Path, "write_text"), (Path, "write_bytes"),
            (Path, "mkdir"), (QThread, "start"),
        ]:
            spies.append(stack.enter_context(patch.object(owner, name)))
        job = build_export_job(state, output="out.mp4", fmt_key=VIDEO)
        assert_rich(job)
        assert all(spy.call_count == 0 for spy in spies)


def test_builder_without_application():
    src = str(Path(app_mod.__file__).resolve().parents[1])
    code = f"""
import sys
sys.path.insert(0, {src!r})
from PySide6.QtCore import QCoreApplication
from cove_video_editor.exporter import build_export_job
assert 'cove_video_editor.project_io' not in sys.modules
assert 'cove_video_editor.app' not in sys.modules
from cove_video_editor.project_io import ProjectState
from cove_video_editor.clip import Clip, MediaAsset
from pathlib import Path
assert QCoreApplication.instance() is None
asset = MediaAsset(Path('a.mp4'), duration=2, width=320, height=240, fps=30, has_audio=True)
state = ProjectState(clips=[Clip(asset, src_end=1)])
job = build_export_job(state, output='out.mp4', fmt_key={VIDEO!r})
assert job.clips[0].src_end == 1
assert job.clips[0] is not state.clips[0]
assert QCoreApplication.instance() is None
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_source_uses_shared_builder():
    source = inspect.getsource(app_mod)
    assert "ExportJob(" not in source
    click = inspect.getsource(app_mod.MainWindow._on_export_clicked)
    assert click.index("_finish_crop_edit(commit=True)") < click.index("crop=self._crop_pixels()")


def test_gui_delegation(window):
    built = []

    def wrapping_builder(*args, **kwargs):
        job = build_export_job(*args, **kwargs)
        built.append(job)
        return job

    with patch.object(app_mod, "build_export_job", wraps=wrapping_builder) as spy:
        job = capture(window)
    assert spy.call_count == 1
    assert len(built) == 1
    assert job is built[0]
    args, kwargs = spy.call_args
    assert isinstance(args[0], ProjectState)
    assert kwargs == dict(
        output="/tmp/out.mp4", fmt_key=VIDEO, size=None,
        crop=None, region=None, encoder_pref="auto",
    )


def test_gui_presets_shared(window):
    assert RESOLUTION_PRESETS == PRESETS
    assert tuple(
        (window.resolution_combo.itemText(i), window.resolution_combo.itemData(i))
        for i in range(window.resolution_combo.count())
    ) == PRESETS
    assert window.resolution_combo.currentIndex() == 0


def styled_state():
    asset = _asset("styled.mp4")
    clip = _clip(
        asset, start=7.0, length=10.0, speed=0.75, muted=True,
        audio_volume=0.35, linked_audio=False, audio_offset=1.25, audio_removed=True,
        crop_rect=(0.1, 0.2, 0.5, 0.5), crop_preset="Free (Custom)",
    )
    clip.src_start = 2.5
    clip.id = "styled-clip"
    clip.waveform_peaks = [0.2, 0.7]
    clip.waveform_rate = 20
    subtitle = SubtitleTrack(
        Path("styled.srt"), font_family="DejaVu Sans Mono", font_size=47,
        primary_color="#12AB34", outline_color="#5634EF", outline=5, position="top",
        active=True, offset_ms=-325, cues=[(0.5, 1.75, "styled cue")], id="styled-subtitle",
    )
    return ProjectState(assets=[asset], clips=[clip], subtitles=[subtitle])


def assert_styled_job(job, state):
    assert len(job.clips) == 1
    clip = job.clips[0]
    assert clip is not state.clips[0]
    assert clip.asset is state.assets[0]
    assert clip.path == Path("styled.mp4")
    assert clip.timeline_start == 7.0
    assert clip.src_start == 2.5
    assert clip.src_end == 10.0
    assert clip.speed == 0.75
    assert clip.muted is True
    assert clip.audio_volume == 0.35
    assert clip.linked_audio is False
    assert clip.audio_offset == 1.25
    assert clip.audio_removed is True
    assert isinstance(clip.id, str)
    assert clip.id != "styled-clip"
    assert clip.thumbs == []
    assert clip.thumb_pixmaps == []
    assert clip.waveform_peaks == [0.2, 0.7]
    assert clip.waveform_rate == 20
    assert clip.crop_rect == (0.1, 0.2, 0.5, 0.5)
    assert clip.crop_preset == "Free (Custom)"
    subtitle = job.subtitles
    assert subtitle is not None
    assert subtitle is not state.subtitles[0]
    assert subtitle.path == Path("styled.srt")
    assert subtitle.font_family == "DejaVu Sans Mono"
    assert subtitle.font_size == 47
    assert subtitle.primary_color == "#12AB34"
    assert subtitle.outline_color == "#5634EF"
    assert subtitle.outline == 5
    assert subtitle.position == "top"
    assert subtitle.active is True
    assert subtitle.offset_ms == -325
    assert subtitle.cues == [(0.5, 1.75, "styled cue")]
    assert subtitle.cues is not state.subtitles[0].cues
    assert subtitle.id == "styled-subtitle"


def test_builder_clip_audio_and_subtitle_style():
    state = styled_state()
    before = copy.deepcopy(state)
    job = build_export_job(state, output="styled.mp4", fmt_key=VIDEO)
    assert_styled_job(job, state)
    assert state == before


def test_gui_clip_audio_and_subtitle_style(window):
    state = styled_state()
    window._clips = state.clips
    window._subs = state.subtitles
    window._assets = {state.assets[0].id: state.assets[0]}
    assert_styled_job(capture(window), state)


@pytest.mark.parametrize("replace", [False, True])
def test_builder_input_order_and_audio_lanes(replace):
    state = ProjectState(
        clips=[
            _clip(_asset("late.mp4"), start=12.0, length=3.0, speed=1.5),
            _clip(_asset("middle.mp4"), start=4.0, length=4.0, speed=0.5),
            _clip(_asset("early.mp4"), start=0.0, length=2.0, speed=2.0),
        ],
        added_audios=[
            AddedAudio(
                Path("repeat.wav"), duration=10, lane=0, offset=8.0,
                src_start=1.0, src_end=5.0, volume=0.2,
            ),
            AddedAudio(
                Path("repeat.wav"), duration=10, lane=1, offset=2.0,
                src_start=3.0, src_end=6.0, volume=0.5,
            ),
            AddedAudio(
                Path("other.wav"), duration=10, lane=2, offset=5.0,
                src_start=2.0, src_end=9.0, volume=0.75, muted=True,
            ),
        ],
        replace_added_audio=replace, added_gain=2.0, original_gain=0.3,
    )
    before = copy.deepcopy(state)
    tracks = added_audio_tracks(
        state.added_audios, replace=replace, added_gain=2.0, original_gain=0.3,
    )
    job = build_export_job(state, output="unsorted.mp4", fmt_key=VIDEO)
    for exported in (tracks, job.audio_tracks):
        assert [
            (t.path, t.offset, t.src_start, t.duration, t.volume, t.original_volume, t.replace)
            for t in exported
        ] == [
            (Path("repeat.wav"), 8.0, 1.0, 4.0, 0.4, 0.3, replace),
            (Path("repeat.wav"), 2.0, 3.0, 3.0, 1.0, 0.3, replace),
            (Path("other.wav"), 5.0, 2.0, 7.0, 0.0, 0.3, replace),
        ]
    assert [
        (c.path, c.timeline_start, c.src_start, c.src_end, c.speed) for c in job.clips
    ] == [
        (Path("late.mp4"), 12.0, 0.0, 3.0, 1.5),
        (Path("middle.mp4"), 4.0, 0.0, 4.0, 0.5),
        (Path("early.mp4"), 0.0, 0.0, 2.0, 2.0),
    ]
    assert all(clone is not original for clone, original in zip(job.clips, state.clips))
    assert state == before
