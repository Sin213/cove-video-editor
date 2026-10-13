"""CLI startup through the real parser, Qt and existing project loader.

Positive subprocesses run the real main(), theme, show, queued load, event
loop and drain. Only encoder/media-analysis workers and blocking warning
dialogs are replaced; a bounded timer records GUI state and quits. Negative
subprocesses execute python -m cove_video_editor with a sitecustomize window
sentinel, so an accidental window is a failure rather than a hanging test.
All subprocess settings/log locations are temporary. Fixtures use real models
and save_project, with zero-byte media as in test_project_persistence.py.
"""
from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, QUrl, Qt
from PySide6.QtGui import QTextDocument, Qt as GuiQt
from PySide6.QtWidgets import QApplication
from cove_video_editor import __main__ as entry, __version__, project_io
from cove_video_editor import app as app_mod
from cove_video_editor.app import MainWindow
from cove_video_editor.clip import AddedAudio, Clip, MediaAsset, SubtitleTrack

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TITLE = f"Cove Video Editor v{__version__}"
WORKERS = ("_start_encoder_probe", "_kick_off_thumbs", "_kick_off_waveform",
           "_kick_off_added_waveform")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def fixture_project(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    media = []
    for name in ("video.mp4", "bed.mp3", "captions.srt"):
        p = path.parent / name
        p.write_bytes(b"")
        media.append(p)
    asset = MediaAsset(path=media[0], duration=10, width=1920, height=1080,
                       fps=30, has_audio=True)
    state = project_io.ProjectState(
        assets=[asset],
        clips=[Clip(asset=asset, src_start=1, src_end=6, speed=1.5,
                    crop_rect=(0.1, 0.2, 0.5, 0.6), crop_preset="Free (Custom)")],
        added_audios=[AddedAudio(path=media[1], duration=10, offset=2,
                                src_start=1, src_end=5, volume=0.75)],
        subtitles=[SubtitleTrack(path=media[2], active=True)],
        replace_added_audio=True, added_gain=1.4, original_gain=0.6)
    project_io.save_project(path, state)
    return state


def snapshot(path):
    return (path.read_bytes(), path.stat().st_mtime_ns,
            sorted(p.name for p in path.parent.iterdir()))


def parse(tokens):
    return entry._parse_startup_args(["cove", *tokens])


@pytest.mark.parametrize("tokens,word", [
    (["--project"], "expected one argument"),
    (["--project="], "empty"),
    (["--project", "a", "--project=b"], "once"),
    (["--project", "-x.coveproj"], "expected one argument"),
])
def test_parse_usage_errors(tokens, word, capsys):
    with pytest.raises(SystemExit) as exc:
        parse(tokens)
    assert exc.value.code == 2
    assert word in capsys.readouterr().err


@pytest.mark.parametrize("tokens", [
    ["--proj", "x"], ["--bogus"], ["foo.mp4"], ["--"],
    ["--", "--project=x"], ["-platform", "offscreen", "-style", "fusion"],
    ["--platform", "offscreen", "--bogus", "value"],
])
def test_parser_preserves_qt_remainder_order(tokens):
    _, path, remainder = parse(tokens)
    assert path is None
    assert remainder == tokens


@pytest.mark.parametrize("kind,word", [
    ("missing", "existing file"), ("directory", "existing file"),
    ("wrong", ".coveproj"),
])
def test_pre_gui_path_validation(tmp_path, kind, word, capsys):
    path = tmp_path / ("x.mp4" if kind == "wrong" else "x.coveproj")
    if kind == "directory":
        path.mkdir()
    elif kind == "wrong":
        path.touch()
    with patch.object(entry, "QApplication") as app:
        with pytest.raises(SystemExit) as exc:
            entry.main(["cove", "--project", str(path)])
    assert exc.value.code == 2
    app.assert_not_called()
    assert word in capsys.readouterr().err


@pytest.mark.parametrize("error", [OSError(errno.ENAMETOOLONG, "too long"),
                                   PermissionError("stat denied")])
def test_path_stat_failures_exit_two_before_qt(tmp_path, error, capsys):
    with patch.object(entry.os, "stat", side_effect=error), \
            patch.object(entry, "QApplication") as app:
        with pytest.raises(SystemExit) as exc:
            entry.main(["cove", "--project", str(tmp_path / "x.coveproj")])
    assert exc.value.code == 2
    app.assert_not_called()
    stderr = capsys.readouterr().err
    assert "usage:" in stderr and "existing file" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize("error", [OSError(errno.ENAMETOOLONG, "too long"),
                                   PermissionError("stat denied")])
def test_legacy_path_stat_errors_exit_two_before_qt(tmp_path, error, capsys):
    # Python 3.14 suppresses errors that older Path.is_file implementations
    # propagated. Simulate that legacy behavior as well as the real stat seam
    # so the compatibility assertion and M21 are deterministic on every Python.
    with patch.object(entry.os, "stat", side_effect=error), \
            patch.object(Path, "is_file", side_effect=error), \
            patch.object(entry, "QApplication") as app:
        with pytest.raises(SystemExit) as exc:
            entry.main(["cove", "--project", str(tmp_path / "x.coveproj")])
    assert exc.value.code == 2
    app.assert_not_called()
    assert "usage:" in capsys.readouterr().err


def test_embedded_nul_path_rejected_before_qt(capsys):
    with patch.object(entry, "QApplication") as app:
        with pytest.raises(SystemExit) as exc:
            entry.main(["cove", "--project=bad\x00.coveproj"])
    assert exc.value.code == 2
    app.assert_not_called()
    assert "existing file" in capsys.readouterr().err


@pytest.mark.parametrize("tokens,option", [
    (["-style", "--bogus"], "-style"),
    (["-platform", "--bogus"], "-platform"),
    (["-style"], "-style"),
    (["--platform", "--project", "x.coveproj"], "--platform"),
    (["--style", "-bogus"], "--style"),
])
def test_qt_value_options_require_non_dash_value_before_qt(tokens, option, capsys):
    with patch.object(entry, "QApplication") as app:
        with pytest.raises(SystemExit) as exc:
            entry.main(["cove", *tokens])
    assert exc.value.code == 2
    app.assert_not_called()
    stderr = capsys.readouterr().err
    assert "usage:" in stderr and f"{option} needs a value" in stderr


@pytest.mark.parametrize("tokens", [["-platform", "offscreen"],
    ["--platform", "offscreen"], ["-style", "fusion"],
    ["--style", "fusion"], ["-style=fusion"], ["-other-qt-option", "--value"]])
def test_qt_value_option_positives_preserve_tokens(tokens):
    _, path, remainder = parse(tokens)
    assert path is None and remainder == tokens


def test_help_states_narrow_qt_validation(capsys):
    with pytest.raises(SystemExit) as exc:
        parse(["--help"])
    assert exc.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "--platform" in text and "--style" in text
    assert "Other Qt options pass through without validation" in text


@pytest.mark.parametrize("name", [
    "x.COVEPROJ", "with spaces.coveproj", "日本語 é !;$().coveproj",
    "-x.coveproj", "a" * 220 + ".coveproj",
])
def test_relative_paths_and_equal_form(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / name
    path.touch()
    _, got, remainder = parse([f"--project={name}", "-style", "fusion"])
    assert got == path
    assert remainder == ["-style", "fusion"]


def test_path_abspath_does_not_resolve_symlinks_or_expand_tilde(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real = tmp_path / "real.coveproj"
    real.touch()
    link = tmp_path / "link.coveproj"
    link.symlink_to(real)
    assert parse(["--project", str(link)])[1] == link
    tilde = tmp_path / "~" / "x.coveproj"
    tilde.parent.mkdir()
    tilde.touch()
    assert parse(["--project=~/x.coveproj"])[1] == tilde


@pytest.mark.parametrize("tokens", [["--bogus"], ["--proj", "x"],
                                    ["foo.mp4"], ["--"]])
def test_leftovers_rejected_before_window(tokens, capsys):
    # Real argument parsing; this seam represents what Qt did not consume.
    with patch.object(entry, "QApplication") as app, \
            patch.object(entry, "MainWindow") as win:
        app.return_value.arguments.return_value = ["cove", *tokens]
        assert entry.main(["cove", *tokens]) == 2
        win.assert_not_called()
    assert tokens[0] in capsys.readouterr().err


def test_main_order_qt_argv_and_exit_code(tmp_path):
    path = tmp_path / "x.coveproj"
    path.touch()
    events = []
    with patch.object(entry, "QApplication") as app, \
            patch.object(entry, "MainWindow") as window, \
            patch.object(entry.theme, "apply_theme"), \
            patch.object(entry, "drain_surviving_media_threads") as drain:
        app.return_value.arguments.return_value = ["cove"]
        window.return_value.show.side_effect = lambda: events.append("show")
        window.return_value.open_startup_project.side_effect = \
            lambda p: events.append(("queue", p))
        app.return_value.exec.side_effect = lambda: events.append("exec") or 7
        assert entry.main(["cove", "-platform", "offscreen", "--project", str(path),
                           "-style", "fusion"]) == 7
        app.assert_called_once_with(["cove", "-platform", "offscreen", "-style", "fusion"])
        drain.assert_called_once_with()
    assert events == ["show", ("queue", path), "exec"]


def test_startup_queue_exception_still_drains_threads(tmp_path):
    path = tmp_path / "x.coveproj"
    path.touch()
    with patch.object(entry, "QApplication") as app, \
            patch.object(entry, "MainWindow") as window, \
            patch.object(entry.theme, "apply_theme"), \
            patch.object(entry, "drain_surviving_media_threads") as drain:
        app.return_value.arguments.return_value = ["cove"]
        window.return_value.open_startup_project.side_effect = RuntimeError("queue failed")
        with pytest.raises(RuntimeError, match="queue failed"):
            entry.main(["cove", "--project", str(path)])
        window.return_value.show.assert_called_once_with()
        app.return_value.exec.assert_not_called()
        drain.assert_called_once_with()


def test_help_and_usage_errors_do_not_construct_qt(capsys):
    for tokens, code in ((["--help"], 0), (["--project"], 2),
                         (["--project="], 2)):
        with patch.object(entry, "QApplication") as app:
            with pytest.raises(SystemExit) as exc:
                entry.main(["cove", *tokens])
        assert exc.value.code == code
        app.assert_not_called()
    assert "--project PATH" in capsys.readouterr().out


@pytest.mark.parametrize("argv0,expected", [
    ("/opt/cove/cove_video_editor/__main__.py", "python3 -m cove_video_editor"),
    ("/opt/bin/cove-video-editor", "cove-video-editor"),
    ("/opt/CoveVideoEditor.exe", "CoveVideoEditor.exe"),
])
def test_usage_prog_is_module_command_or_launcher_basename(argv0, expected, capsys):
    parser, path, remainder = entry._parse_startup_args([argv0])
    assert parser.prog == expected
    assert path is None and remainder == []
    with patch.object(entry, "QApplication") as app:
        with pytest.raises(SystemExit) as exc:
            entry.main([argv0, "--project"])
    assert exc.value.code == 2
    app.assert_not_called()
    stderr = capsys.readouterr().err
    assert stderr.startswith(f"usage: {expected} ")
    assert f"{expected}: error:" in stderr
    assert argv0 not in stderr


def test_help_guides_qt_options_and_dash_leading_path(capsys):
    with patch.object(entry, "QApplication") as app, \
            patch.object(entry, "MainWindow") as window:
        with pytest.raises(SystemExit) as exc:
            entry.main(["/opt/cove/__main__.py", "--help"])
    assert exc.value.code == 0
    app.assert_not_called()
    window.assert_not_called()
    help_text = " ".join(capsys.readouterr().out.split())
    assert "-platform" in help_text and "-style" in help_text
    assert "--project=PATH" in help_text


def test_typo_error_guides_project_option_before_window(tmp_path, capsys):
    path = tmp_path / "valid.coveproj"
    path.touch()
    tokens = ["--projet", str(path)]
    with patch.object(entry, "QApplication") as app, \
            patch.object(entry, "MainWindow") as window:
        app.return_value.arguments.return_value = ["cove", *tokens]
        assert entry.main(["cove", *tokens]) == 2
        window.assert_not_called()
    stderr = capsys.readouterr().err
    assert f"unsupported arguments: --projet {path}" in stderr
    assert "Cove's own option is --project PATH" in stderr
    assert "-platform" in stderr and "-style" in stderr


def test_main_default_argv_preserves_no_argument_startup():
    with patch.object(sys, "argv", ["cove"]), \
            patch.object(entry, "QApplication") as app, \
            patch.object(entry, "MainWindow") as window, \
            patch.object(entry.theme, "apply_theme"), \
            patch.object(entry, "drain_surviving_media_threads"):
        app.return_value.arguments.return_value = ["cove"]
        app.return_value.exec.return_value = 9
        assert entry.main() == 9
        app.assert_called_once_with(["cove"])
        window.return_value.show.assert_called_once_with()
        window.return_value.open_startup_project.assert_not_called()


HARNESS = textwrap.dedent('''
    import json, sys
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from cove_video_editor import __main__ as entry
    from cove_video_editor import app as app_mod
    for name in ("_start_encoder_probe", "_kick_off_thumbs", "_kick_off_waveform",
                 "_kick_off_added_waveform"):
        setattr(entry.MainWindow, name, lambda *a: None)
    warnings = []
    app_mod.QMessageBox.warning = lambda *a: warnings.append(str(a[2]))
    class ObservedWindow(entry.MainWindow):
        def __init__(self):
            super().__init__()
            self.calls = []
            self.loop_entered = False
            QTimer.singleShot(0, self.mark_loop)
            QTimer.singleShot(150, self.dump)
        def mark_loop(self):
            self.loop_entered = True
        def _open_project_path(self, path):
            self.calls.append([str(path), self.isVisible(), self.loop_entered])
            return super()._open_project_path(path)
        def dump(self):
            print("COVE_STATE=" + json.dumps(dict(
                title=self.windowTitle(), label=self.titlebar.title_lbl.text(),
                tooltip=self.titlebar.title_lbl.toolTip(), assets=len(self._assets),
                clips=len(self._clips), audio=len(self._added_audios),
                subtitles=len(self._subs), crop=self._clips[0].crop_rect if self._clips else None,
                path=str(self._current_project_path) if self._current_project_path else None,
                calls=self.calls, warnings=warnings, visible=self.isVisible()), ensure_ascii=False),
                flush=True)
            QApplication.quit()
    entry.MainWindow = ObservedWindow
    sys.exit(entry.main(["cove", *sys.argv[1:]]))
''')


def run_cli(tmp_path, tokens, *, positive=False, pre_gui=False):
    sandbox = tmp_path / "process-settings"
    sandbox.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONNOUSERSITE="1",
               QT_QPA_PLATFORM="offscreen", HOME=str(sandbox),
               XDG_CONFIG_HOME=str(sandbox / "config"),
               XDG_CACHE_HOME=str(sandbox / "cache"),
               XDG_DATA_HOME=str(sandbox / "data"),
               XDG_STATE_HOME=str(sandbox / "state"))
    if positive:
        script = sandbox / "startup_harness.py"
        script.write_text(HARNESS, encoding="utf-8")
        command = [sys.executable, str(script), *tokens]
    else:
        # Actual -m entry point, real Qt argument consumption. Prove no window.
        (sandbox / "sitecustomize.py").write_text(
            "from cove_video_editor.app import MainWindow\n"
            "def forbidden(*a, **k):\n"
            "    raise AssertionError('GUI_CREATED')\n"
            "MainWindow.__init__ = forbidden\n", encoding="utf-8")
        env["PYTHONPATH"] += os.pathsep + str(sandbox)
        if pre_gui:
            env["QT_QPA_PLATFORM"] = "cove_invalid_platform_sentinel"
        command = [sys.executable, "-m", "cove_video_editor", *tokens]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode >= 0, result.stderr
    return result


def dump(result):
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("COVE_STATE=")]
    assert len(lines) == 1, (result.stdout, result.stderr)
    return json.loads(lines[0].split("=", 1)[1])


def test_real_entry_help_pre_gui(tmp_path):
    result = run_cli(tmp_path, ["--help"], pre_gui=True)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout and "--project PATH" in result.stdout
    assert result.stdout.startswith("usage: python3 -m cove_video_editor ")
    assert "--project=PATH" in result.stdout
    assert "-platform" in result.stdout and "-style" in result.stdout
    assert "GUI_CREATED" not in result.stderr


@pytest.mark.parametrize("tokens,word,pre_gui", [
    (["--project"], "expected one argument", True),
    (["--project="], "empty", True),
    (["--project=a", "--project=b"], "once", True),
    (["--project", "missing.coveproj"], "existing file", True),
    (["--project", "directory.coveproj"], "existing file", True),
    (["--project", "wrong.mp4"], ".coveproj", True),
    (["--project", "-x.coveproj"], "expected one argument", True),
    (["--proj", "x"], "--proj", False),
    (["--projet", "valid.coveproj"], "--projet", False),
    (["--bogus"], "--bogus", False),
    (["foo.mp4"], "foo.mp4", False),
    (["--"], "--", False),
    (["--", "foo.mp4"], "foo.mp4", False),
])
def test_real_entry_errors_exit_two_without_gui(tmp_path, tokens, word, pre_gui):
    (tmp_path / "directory.coveproj").mkdir()
    (tmp_path / "wrong.mp4").touch()
    (tmp_path / "valid.coveproj").touch()
    result = run_cli(tmp_path, tokens, pre_gui=pre_gui)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "usage:" in result.stderr and word in result.stderr
    assert "GUI_CREATED" not in result.stderr
    if tokens[0] == "--projet":
        assert "unsupported arguments: --projet valid.coveproj" in result.stderr
        assert "Cove's own option is --project PATH" in result.stderr
        assert "-platform" in result.stderr and "-style" in result.stderr


@pytest.mark.parametrize("tokens,option", [
    (["-style", "--bogus", "--project", "valid.coveproj"], "-style"),
    (["-platform", "--bogus"], "-platform"),
    (["-style"], "-style"),
    (["--platform", "--project", "valid.coveproj"], "--platform"),
])
def test_real_qt_value_errors_exit_two_without_gui(tmp_path, tokens, option):
    fixture_project(tmp_path / "valid.coveproj")
    result = run_cli(tmp_path, tokens, pre_gui=True)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "usage:" in result.stderr and f"{option} needs a value" in result.stderr
    assert "GUI_CREATED" not in result.stderr


@pytest.mark.parametrize("qt_tokens", [[], ["-platform", "offscreen"],
    ["--platform", "offscreen"], ["-style", "fusion"],
    ["--style", "fusion"], ["-style=fusion"]])
def test_real_startup_project_and_qt_options(tmp_path, qt_tokens):
    path = tmp_path / "日本語 with spaces.COVEPROJ"
    fixture_project(path)
    before = snapshot(path)
    state = dump(run_cli(tmp_path, [*qt_tokens, "--project", str(path)], positive=True))
    # Ignore only the explicitly isolated harness/settings directory.
    after = snapshot(path)
    assert before[:2] == after[:2]
    assert set(after[2]) - set(before[2]) == {"process-settings"}
    assert state["calls"] == [[str(path), True, True]]
    assert state["path"] == str(path)
    assert state["title"] == state["label"] == f"{path.name} — Cove Video Editor"
    assert state["tooltip"] == str(path)
    assert [state[k] for k in ("assets", "clips", "audio", "subtitles")] == [1, 1, 1, 1]
    assert state["crop"] == [0.1, 0.2, 0.5, 0.6]
    assert state["warnings"] == [] and state["visible"]


@pytest.mark.parametrize("name", ["relative.coveproj", "-dash.coveproj",
                                  "a" * 220 + ".coveproj"])
def test_real_startup_relative_equal_and_long_paths(tmp_path, name):
    parent = tmp_path
    if name.startswith("a"):
        parent = tmp_path / ("b" * 160) / ("c" * 160)
    path = parent / name
    fixture_project(path)
    state = dump(run_cli(tmp_path, ["--project=" + str(path.relative_to(tmp_path))], positive=True))
    assert state["path"] == str(path)
    assert state["calls"] == [[str(path), True, True]]
    assert state["title"] == state["label"] == f"{name} — Cove Video Editor"
    assert state["tooltip"] == str(path)


def test_real_no_argument_startup(tmp_path):
    state = dump(run_cli(tmp_path, [], positive=True))
    assert state["title"] == DEFAULT_TITLE and state["label"] == "Cove Video Editor"
    assert state["path"] is None and state["tooltip"] == ""
    assert state["calls"] == [] and state["warnings"] == []
    assert [state[k] for k in ("assets", "clips", "audio", "subtitles")] == [0, 0, 0, 0]
    assert state["visible"]


@pytest.mark.parametrize("failure", ["json", "utf8", "version", "media", "unreadable",
                                     "long", "spaces", "unicode"])
def test_real_startup_failure_modal_once_empty_unchanged(tmp_path, failure):
    if failure == "unreadable" and os.geteuid() == 0:
        pytest.skip("root can read chmod 000 files")
    parent = tmp_path / ("b" * 160) / ("c" * 160) if failure == "long" else tmp_path
    name = {"long": "a" * 220, "spaces": "with spaces", "unicode": "日本語"}.get(failure, failure)
    path = parent / f"{name}.coveproj"
    fixture_project(path)
    if failure == "version":
        doc = json.loads(path.read_text())
        doc["version"] = 99
        path.write_text(json.dumps(doc))
    elif failure == "media":
        (parent / "video.mp4").unlink()
    elif failure == "utf8":
        path.write_bytes(b"\xff\xfe")
    elif failure != "unreadable":
        path.write_text("{broken")
    before = snapshot(path)
    if failure == "unreadable":
        path.chmod(0)
    try:
        state = dump(run_cli(tmp_path, ["--project", str(path)], positive=True))
    finally:
        if failure == "unreadable":
            path.chmod(0o600)
    after = snapshot(path)
    assert before[:2] == after[:2]
    assert set(after[2]) - set(before[2]) <= {"process-settings"}
    assert state["calls"] == [[str(path), True, True]]
    assert len(state["warnings"]) == 1 and state["warnings"][0]
    assert state["path"] is None and state["title"] == DEFAULT_TITLE
    assert state["label"] == "Cove Video Editor" and state["tooltip"] == ""
    assert [state[k] for k in ("assets", "clips", "audio", "subtitles")] == [0, 0, 0, 0]
    assert state["crop"] is None and state["visible"]


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        for name in WORKERS:
            p = patch.object(MainWindow, name, lambda *a: None)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(app_mod.QMessageBox, "warning", return_value=None)
        self.warned = p.start()
        self.addCleanup(p.stop)

    def win(self):
        w = MainWindow()
        # Native QMediaPlayer's in-process FFmpeg backend can print probe
        # diagnostics for zero-byte fixtures. The subprocess-spawning
        # encoder/thumbnail/waveform workers are all patched in setUp().
        self.assertIsNone(w._encoder_probe_thread)
        self.assertEqual(w._thumb_threads, {})
        self.assertEqual(w._wave_threads, {})
        self.assertEqual(w._added_wave_threads, {})
        self.addCleanup(self.dispose, w)
        return w

    @staticmethod
    def dispose(w):
        if not shiboken6.isValid(w):
            return
        w._play_timer.stop()
        for player in (w.player, w.clip_audio_player, *w._added_players.values()):
            player.stop()
            player.setSource(QUrl())
        for aid in list(w._added_players):
            w._destroy_added_player(aid)
        w.close()
        shiboken6.delete(w)

    def assert_title(self, w, path):
        self.assertEqual(w._current_project_path, path)
        self.assertEqual(w.windowTitle(), f"{path.name} — Cove Video Editor" if path else DEFAULT_TITLE)
        self.assertEqual(w.titlebar.title_lbl.text(), f"{path.name} — Cove Video Editor" if path else "Cove Video Editor")
        self.assertEqual(w.titlebar.title_lbl.toolTip(), str(path) if path else "")

    def test_default_title(self):
        self.assert_title(self.win(), None)

    def test_deferred_once_exact_path_reuses_loader_read_only(self):
        path = self.tmp / "日本語 with spaces.coveproj"
        wanted = fixture_project(path)
        before = snapshot(path)
        w = self.win()
        w.show()
        with patch.object(w, "_open_project_path", wraps=w._open_project_path) as spy:
            w.open_startup_project(path)
            spy.assert_not_called()
            self.assert_title(w, None)
            QApplication.processEvents()
            spy.assert_called_once_with(path)
            QApplication.processEvents()
            spy.assert_called_once_with(path)
        self.assertEqual(project_io.serialize_project(w._project_state()), project_io.serialize_project(wanted))
        self.assert_title(w, path)
        self.assertEqual(snapshot(path), before)

    def test_destroyed_window_cancels_timer(self):
        w = self.win()
        with patch.object(MainWindow, "_open_project_path") as spy:
            w.open_startup_project(self.tmp / "x.coveproj")
            shiboken6.delete(w)
            QApplication.processEvents()
            spy.assert_not_called()

    def test_startup_timer_owned_single_shot_and_deleted_after_firing(self):
        w = self.win()
        existing = set(w.findChildren(QTimer))
        path = self.tmp / "x.coveproj"
        with patch.object(w, "_open_project_path", return_value=True) as spy:
            w.open_startup_project(path)
            timers = set(w.findChildren(QTimer)) - existing
            self.assertEqual(len(timers), 1)
            timer = timers.pop()
            self.assertIs(timer.parent(), w)
            self.assertTrue(timer.isSingleShot())
            self.assertEqual(timer.interval(), 0)
            self.assertTrue(timer.isActive())
            spy.assert_not_called()
            QApplication.processEvents()
            spy.assert_called_once_with(path)
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertFalse(shiboken6.isValid(timer))
            QApplication.processEvents()
            spy.assert_called_once_with(path)

    def test_markup_titles_and_tooltips_are_literal_for_open_save_save_as(self):
        w = self.win()
        # A slash cannot be part of a POSIX filename: the requested closed
        # <i> example is a relative nested path. The unclosed form also proves
        # the filename/label case, while the closed form proves the tooltip.
        for name in ("<b>draft.coveproj", "&amp;x.coveproj",
                     "<i>x.coveproj", "<i>x</i>.coveproj"):
            path = self.tmp / name
            fixture_project(path)
            other = self.tmp / "saved-as" / name
            other.parent.mkdir(parents=True, exist_ok=True)
            for action in ("open", "save", "save-as"):
                with self.subTest(name=name, action=action):
                    w.titlebar.title_lbl.setTextFormat(Qt.AutoText)
                    if action == "open":
                        self.assertTrue(w._open_project_path(path))
                    elif action == "save":
                        w._on_save_project()
                    else:
                        with patch.object(app_mod.QFileDialog, "getSaveFileName",
                                          return_value=(str(other), "")):
                            w._on_save_project_as()
                        path = other
                    title = f"{path.name} — Cove Video Editor"
                    self.assertEqual(w._current_project_path, path)
                    self.assertEqual(w.windowTitle(), title)
                    self.assertEqual(w.titlebar.title_lbl.text(), title)
                    self.assertEqual(w.titlebar.title_lbl.textFormat(), Qt.PlainText)
                    tooltip = w.titlebar.title_lbl.toolTip()
                    if GuiQt.mightBeRichText(tooltip):
                        document = QTextDocument()
                        document.setHtml(tooltip)
                        self.assertEqual(document.toPlainText(), str(path))
                    else:
                        self.assertEqual(tooltip, str(path))

    def test_open_save_save_as_refresh_title(self):
        path = self.tmp / "original.coveproj"
        fixture_project(path)
        w = self.win()
        self.assertTrue(w._open_project_path(path))
        self.assert_title(w, path)
        w._on_save_project()
        self.assert_title(w, path)
        other = self.tmp / "new name.coveproj"
        with patch.object(app_mod.QFileDialog, "getSaveFileName", return_value=(str(other), "")):
            w._on_save_project_as()
        self.assert_title(w, other)
        self.assertTrue(other.is_file())

    def test_first_save_updates_title(self):
        w = self.win()
        path = self.tmp / "first.coveproj"
        self.assertTrue(w._save_project_to(path))
        self.assert_title(w, path)

    def test_failed_open_save_and_cancel_preserve_title_and_path(self):
        path = self.tmp / "original.coveproj"
        fixture_project(path)
        w = self.win()
        self.assertTrue(w._open_project_path(path))
        bad = self.tmp / "bad.coveproj"
        bad.write_text("{broken")
        self.assertFalse(w._open_project_path(bad))
        self.assert_title(w, path)
        with patch.object(project_io, "save_project", side_effect=OSError("write denied")):
            self.assertFalse(w._save_project_to(self.tmp / "failed.coveproj"))
        self.assert_title(w, path)
        for method, action in (("getOpenFileName", w._on_open_project),
                               ("getSaveFileName", w._on_save_project_as)):
            with patch.object(app_mod.QFileDialog, method, return_value=("", "")):
                action()
            self.assert_title(w, path)
        self.assertEqual(self.warned.call_count, 2)

    def test_failed_open_empty_retains_default_and_reports(self):
        w = self.win()
        bad = self.tmp / "bad.coveproj"
        bad.write_text("{broken")
        before = snapshot(bad)
        self.assertFalse(w._open_project_path(bad))
        self.warned.assert_called_once()
        self.assert_title(w, None)
        self.assertEqual(snapshot(bad), before)
        self.assertIn("Could not open project", w.status.currentMessage())
        self.assertIn("Could not open project", w.export_log.toPlainText())
