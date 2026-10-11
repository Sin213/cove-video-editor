"""Offline downloader evidence. RegressionTests were run RED before implementation.

New helper/UI tests below are GREEN-only; no real cookies or child processes.
"""
import importlib
import io
import json
import os
import random
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cove_video_editor import downloader as d

_app = None


@pytest.fixture(scope="module", autouse=True)
def qt_application():
    global _app
    _app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def fake_timers(monkeypatch):
    timers = []
    class Timer:
        def __init__(self, interval, callback, args=(), kwargs=None):
            self.interval, self.callback, self.args = interval, callback, args
            self.kwargs = kwargs or {}
            self.daemon = self.started = self.cancelled = False
            timers.append(self)
        def start(self): self.started = True
        def cancel(self): self.cancelled = True
        def fire(self): self.callback(*self.args, **self.kwargs)
    monkeypatch.setattr(d.threading, "Timer", Timer)
    return timers


class FakeProc:
    def __init__(self, lines=(), rc=0, hook=None, broken=False, stubborn=False):
        self.lines, self.rc, self.hook = lines, rc, hook
        self.broken, self.stubborn = broken, stubborn
        self.done = False
        self.terminated = self.killed = 0
        self.waits = []
        self.stdout = self.stream()

    def stream(self):
        if self.hook:
            self.hook()
        if self.broken:
            raise RuntimeError("stdout exploded")
        yield from self.lines

    def poll(self):
        return self.rc if self.done else None

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.stubborn and not self.killed:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.done = True
        return self.rc


class FakeProbe(FakeProc):
    def __init__(self, kind="ok"):
        super().__init__(rc=1 if kind == "failure" else 0)
        self.kind, self.returncode, self.timeout = kind, self.rc, None
    def communicate(self, timeout=None):
        self.timeout = timeout
        if self.kind == "timeout":
            raise subprocess.TimeoutExpired("version", timeout)
        self.done = True
        return "2026.08.19", None


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(d, "_yt_dlp_command", lambda: (
        d.DownloadTool(["fake-yt-dlp"], "system") if hasattr(d, "DownloadTool")
        else ["fake-yt-dlp"]))
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **kw: FakeProbe())


def run_worker(monkeypatch, tmp_path, proc, probe=None, **kwargs):
    worker = d.DownloadWorker("https://example.invalid/video", "Best", tmp_path, **kwargs)
    outcomes, details, statuses, progress, calls = [], [], [], [], []
    worker.finished.connect(lambda path: outcomes.append(("finished", path)))
    worker.failed.connect(lambda text: outcomes.append(("failed", text)))
    worker.cancelled.connect(lambda: outcomes.append(("cancelled", None)))
    worker.status.connect(statuses.append)
    worker.progress.connect(progress.append)
    if hasattr(worker, "details"):
        worker.details.connect(details.append)
    def spawn(cmd, **options):
        if "--version" in cmd:
            child = probe or FakeProbe()
            child.command, child.options = cmd, options
            return child
        assert worker._proc is None, "probe handle must be released before download"
        calls.append((cmd, options))
        return proc
    monkeypatch.setattr(d.subprocess, "Popen", spawn)
    if proc.hook == "cancel":
        proc.hook = worker.cancel
    worker.run()
    assert len(outcomes) == 1, outcomes
    return outcomes[0], calls, details, statuses, progress


class TestRegression:
    def test_no_unrelated_newest_mp4(self, monkeypatch, tmp_path, offline):
        (tmp_path / "foreign.mp4").write_bytes(b"foreign")
        result, *_ = run_worker(monkeypatch, tmp_path, FakeProc())
        assert result[0] == "failed", "unreported pre-existing MP4 must not be imported"

    @pytest.mark.parametrize("kind", ["outside", pytest.param("symlink", marks=pytest.mark.skipif(
        not hasattr(os, "symlink") or os.name == "nt", reason="needs symlink support and privileges")), "directory", "missing"])
    def test_invalid_reported_path(self, monkeypatch, tmp_path, offline, kind):
        path = tmp_path / "reported.mp4"
        if kind in ("outside", "symlink"):
            outside = tmp_path.parent / (tmp_path.name + "-outside.mp4")
            outside.write_bytes(b"foreign")
            if kind == "symlink":
                path.symlink_to(outside)
            else:
                path = outside
        elif kind == "directory":
            path.mkdir()
        result, *_ = run_worker(monkeypatch, tmp_path, FakeProc([str(path)]))
        assert result[0] == "failed", "reported path must be a contained regular MP4"

    def test_windows_utf8_process_options(self, monkeypatch, tmp_path, offline):
        monkeypatch.setattr(d, "os", SimpleNamespace(name="nt", environ=os.environ))
        monkeypatch.setattr(d.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
        _, calls, *_ = run_worker(monkeypatch, tmp_path, FakeProc())
        options = calls[0][1]
        assert options.get("creationflags") == 0x08000000, "Windows child must be hidden"
        assert options.get("encoding") == "utf-8"
        assert options.get("errors") == "replace"
        assert options["env"]["PYTHONUTF8"] == "1"
        assert options["env"]["PYTHONIOENCODING"] == "utf-8"

    def test_exception_reaps_live_child(self, monkeypatch, tmp_path, offline):
        proc = FakeProc(broken=True, stubborn=True)
        result, *_ = run_worker(monkeypatch, tmp_path, proc)
        assert result[0] == "failed"
        assert proc.terminated == 1, "live child must be terminated on stdout exception"
        assert proc.killed == 1
        assert proc.waits == [3, 3]
        assert proc.done, "child must be reaped"

    def test_cancel_exception_one_terminal(self, monkeypatch, tmp_path, offline):
        proc = FakeProc(hook="cancel", broken=True, stubborn=True)
        result, *_ = run_worker(monkeypatch, tmp_path, proc)
        assert result[0] == "cancelled"
        assert proc.done, "cancel racing stdout exception must reap child"

    def test_frozen_never_runs_itself(self, monkeypatch):
        monkeypatch.setattr(d, "_yt_dlp_dirs", lambda: [])
        monkeypatch.setattr(d.shutil, "which", lambda _: None)
        monkeypatch.setattr(d.importlib.util, "find_spec", lambda _: object())
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert d._yt_dlp_command() is None, "frozen Cove is not a Python interpreter"


# GREEN-only coverage of new command, classification, diagnostics and UI helpers.
def command(tmp_path, **kwargs):
    values = dict(url="https://example.invalid/watch?v=1", quality="Best", output_folder=tmp_path)
    values.update(kwargs)
    return d.build_download_command(["python", "-m", "yt_dlp"], **values)


@pytest.mark.parametrize("quality", list(d.QUALITY_FORMATS))
def test_none_command_parity(tmp_path, quality):
    cmd = command(tmp_path, quality=quality)
    assert cmd == ["python", "-m", "yt_dlp", "--ignore-config", "--newline", "--no-playlist",
                   "--merge-output-format", "mp4", "--remux-video", "mp4", "--format-sort",
                   "vcodec:h264,acodec:aac,ext:mp4:m4a", "-f", d.QUALITY_FORMATS[quality],
                   "-P", str(tmp_path), "-o", "%(title).200B [%(id)s].%(ext)s",
                   "--print", "after_move:filepath", "https://example.invalid/watch?v=1"]
    assert "--cookies" not in cmd and "--cookies-from-browser" not in cmd


def test_browser_args(tmp_path):
    cmd = command(tmp_path, auth_mode="Browser cookies", browser="firefox", browser_profile=" Person Ω ")
    assert cmd[-3:-1] == ["--cookies-from-browser", "firefox:Person Ω"]
    assert d.browser_specification("chrome", "   ") == "chrome"


@pytest.mark.parametrize("profile", ["Person Ω", "C:\\Users\\Person Ω", "Default+extra", "", "a:b"])
def test_real_parser_roundtrip(tmp_path, profile):
    try:
        yt = importlib.import_module("yt_dlp")
        cookies = importlib.import_module("yt_dlp.cookies")
    except ImportError:
        pytest.skip("installed yt_dlp parser unavailable; no runtime dependency is required")
    parsed = yt.parse_options(["--ignore-config", "--cookies-from-browser", d.browser_specification("firefox", profile)])
    actual = parsed.ydl_opts["cookiesfrombrowser"]
    assert actual == ("firefox", profile or None, None, None)
    assert cookies._parse_browser_specification(*actual) == actual
    assert set(d.SUPPORTED_BROWSERS) == cookies.SUPPORTED_BROWSERS


@pytest.mark.parametrize("values", [dict(url=""), dict(url=" --cookies=secret"), dict(url="url\nnext"),
    dict(auth_mode="invalid"), dict(auth_mode="Browser cookies", browser="chrome+keyring"),
    *[dict(auth_mode="Browser cookies", browser="firefox", browser_profile=p)
      for p in ["a::b", "+keyring", ":container", "x\n", "\x00x", "\x7f", "x" * 1025, " " * 1025]]])
def test_invalid_command(tmp_path, values):
    with pytest.raises(d.DownloadInputError):
        command(tmp_path, **values)


def test_cookie_unicode_and_ffmpeg(tmp_path):
    cookie = tmp_path / "cookie Ω file.txt"
    cookie.write_bytes(b"SYNTHETIC-NOT-A-REAL-COOKIE")
    cmd = command(tmp_path, auth_mode="Cookie file", cookie_file=cookie, ffmpeg_path="ffmpeg Ω path")
    assert cmd[-5:-1] == ["--ffmpeg-location", "ffmpeg Ω path", "--cookies", str(cookie.resolve())]
    assert "--cookies-from-browser" not in cmd


@pytest.mark.parametrize("kind", ["empty", "missing", "directory", "oversize", pytest.param("fifo",
    marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")), "access", "open"])
def test_cookie_file_validation(tmp_path, monkeypatch, kind):
    path = tmp_path / "cookies.txt"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind not in ("missing", "empty"):
        path.write_bytes(b"synthetic")
        if kind == "oversize":
            with path.open("wb") as stream:
                stream.truncate(16 * 1024 * 1024 + 1)
        elif kind == "access":
            monkeypatch.setattr(d.os, "access", lambda *a: False)
        elif kind == "open":
            monkeypatch.setattr(Path, "open", lambda *a, **kw: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(d.DownloadInputError):
        command(tmp_path, auth_mode="Cookie file", cookie_file="" if kind == "empty" else path)


@pytest.mark.parametrize("text,mode,expected", [
    ("Sign in to confirm you're not a bot", "None", "BOT_VERIFICATION"),
    ("Sign in to confirm you’re not a bot", "None", "BOT_VERIFICATION"),
    ("Sign in to confirm you're not a bot", "Browser cookies", "AUTH_REJECTED"),
    ("Sign in to confirm your age", "None", "AUTH_REQUIRED"),
    ("Sign in to confirm your age", "Cookie file", "AUTH_REJECTED"),
    ("The provided YouTube account cookies are no longer valid", "Cookie file", "AUTH_REJECTED"),
    ("could not find firefox cookies database", "Browser cookies", "COOKIE_ACCESS_FAILED"),
    ("Could not copy Chrome cookie database", "Browser cookies", "COOKIE_ACCESS_FAILED"),
    ("failed to decrypt cookies using keyring", "Browser cookies", "COOKIE_ACCESS_FAILED"),
    ("Unable to open cookie file: No such file", "Cookie file", "COOKIE_ACCESS_FAILED"),
    ("does not look like a Netscape format cookies file", "Cookie file", "COOKIE_FILE_INVALID"),
    ("Cookies file must be Netscape formatted, not JSON", "Cookie file", "COOKIE_FILE_INVALID"),
    ("yt-dlp not found", "None", "YT_DLP_MISSING"),
    ("Unable to download webpage: timed out", "None", "NETWORK_ERROR"),
    ("Video unavailable", "None", "VIDEO_UNAVAILABLE"),
    ("Postprocessing: ffmpeg not found", "None", "FFMPEG_ERROR"),
    ("Unrecognized response", "None", "GENERIC_DOWNLOAD_ERROR"),
])
def test_classification(text, mode, expected):
    assert d.classify_failure("ERROR: " + text, mode) == expected
    assert d.classify_failure("WARNING: " + text, mode) == "GENERIC_DOWNLOAD_ERROR"


def test_exact_auth_summaries():
    assert d.failure_summary("BOT_VERIFICATION") == "YouTube is asking for authentication.\n\nChoose Browser cookies or Cookie file\nand try again.\n\nAuthentication may help, but YouTube\ncan still reject some requests or networks."
    assert d.failure_summary("AUTH_REJECTED") == "YouTube still rejected this request\nwith the selected authentication.\n\nCheck that your browser session or\ncookie file is valid, then retry."


@pytest.mark.parametrize("text", ["--cookies PLANTED", "--cookies='PLANTED'", '--cookies-from-browser "PLANTED"',
    "['--cookies', 'PLANTED']", "['--cookies-from-browser', 'PLANTED']", "--cookies-from-browser=PLANTED",
    "Cookie: PLANTED", "Set-Cookie: PLANTED", "Authorization: PLANTED", "Proxy-Authorization: PLANTED",
    "X-Api-Token: PLANTED", "X-Custom-Auth: PLANTED", "Bearer PLANTED", "Basic PLANTED",
    "SID=PLANTED", 'SID="PLANTED"', "SID='PLANTED'", "HSID=PLANTED", "SAPISID=PLANTED", "LOGIN_INFO=PLANTED", "__Secure-3PSID=PLANTED",
    "youtube_session_token=PLANTED"])
def test_redaction(text):
    assert "PLANTED" not in d.sanitize(text)


@pytest.mark.parametrize("route", ["success", "failure", "cancel", "kill", "exception", "cancel_exception", "damaged"])
def test_worker_routes(monkeypatch, tmp_path, offline, route):
    path = tmp_path / "video Ω with spaces.mp4"
    path.write_bytes(b"synthetic-mp4")
    lines = ["[download] 25.5%", str(path)]
    proc = FakeProc(lines, rc=1 if route == "failure" else 0,
                    hook="cancel" if route in ("cancel", "kill", "cancel_exception") else None,
                    broken=route in ("exception", "cancel_exception"), stubborn=route == "kill")
    if route == "damaged":
        proc.stdout = io.TextIOWrapper(io.BytesIO(b"/missing/bad\xff.mp4\n"), encoding="utf-8", errors="replace")
    elif route == "success":
        proc.stdout = io.TextIOWrapper(io.BytesIO(("\n".join(lines) + "\n").encode()), encoding="utf-8", errors="replace")
    result, calls, _, _, progress = run_worker(monkeypatch, tmp_path, proc)
    expected = "finished" if route == "success" else "cancelled" if route in ("cancel", "kill", "cancel_exception") else "failed"
    assert result[0] == expected
    assert proc.done
    assert isinstance(calls[0][0], list) and not calls[0][1].get("shell")
    assert calls[0][1]["env"] is not os.environ
    if route == "success":
        assert result[1] == path and progress == [25, 100]
    if route == "kill":
        assert proc.terminated == proc.killed == 1 and proc.waits == [3, 3]


def test_last_valid_report_wins(monkeypatch, tmp_path, offline):
    first, last = tmp_path / "first.mp4", tmp_path / "last Ω.mp4"
    first.touch()
    last.touch()
    result, *_ = run_worker(monkeypatch, tmp_path, FakeProc([str(first), f'"{last}"', "/missing.mp4"]))
    assert result == ("finished", last)


@pytest.mark.parametrize("probe", ["ok", "timeout", "failure"])
def test_version_probe(monkeypatch, tmp_path, offline, probe):
    child = FakeProbe(probe)
    path = tmp_path / "ok.mp4"
    path.touch()
    result, _, details, *_ = run_worker(monkeypatch, tmp_path, FakeProc([str(path)]), probe=child)
    assert result[0] == "finished"
    assert child.command == ["fake-yt-dlp", "--ignore-config", "--version"]
    assert child.timeout == 10 and child.options["encoding"] == "utf-8" and child.done
    assert child.options["errors"] == "replace" and not child.options.get("shell")
    assert child.killed == (probe == "timeout") and child.waits == [3]
    assert ("2026.08.19" if probe == "ok" else "unknown") in details[0]
    assert "(system)" in details[0] and d.CONFIG_NOTE in details[0]


def test_failure_details_no_secrets(monkeypatch, tmp_path, offline):
    cookie = tmp_path / "PLANTED_FILE.txt"
    cookie.write_bytes(b"PLANTED_CONTENT")
    lines = ["WARNING: The provided YouTube account cookies are no longer valid",
             f"ERROR: {cookie} PLANTED_PROFILE Cookie: PLANTED_HEADER",
             "ERROR: Bearer PLANTED_BEARER SID=PLANTED_SID",
             "ERROR: Sign in to confirm you're not a bot"]
    result, _, details, statuses, _ = run_worker(monkeypatch, tmp_path, FakeProc(lines, rc=1),
        auth_mode="Cookie file", cookie_file=str(cookie), browser_profile="PLANTED_PROFILE")
    dialog = d.DownloadVideoDialog()
    for line in details:
        dialog._append_details(line)
    visible = "\n".join([result[1], *details, *statuses, dialog.details_edit.toPlainText()])
    assert "PLANTED" not in visible
    assert "cookies are no longer valid" in visible
    assert result[1] == d.failure_summary("AUTH_REJECTED")
    dialog.close()


def test_discovery_order(monkeypatch, tmp_path):
    executable = tmp_path / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    executable.touch()
    monkeypatch.setattr(d, "_yt_dlp_dirs", lambda: [tmp_path])
    monkeypatch.setattr(d.shutil, "which", lambda _: "/system/yt-dlp")
    assert d._yt_dlp_command().source == "bundled"
    executable.unlink()
    assert d._yt_dlp_command() == d.DownloadTool(["/system/yt-dlp"], "system")
    monkeypatch.setattr(d.shutil, "which", lambda _: None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda _: object())
    assert d._yt_dlp_command() == d.DownloadTool([sys.executable, "-m", "yt_dlp"], "python module")
    monkeypatch.setattr(sys, "executable", "")
    assert d._yt_dlp_command() is None


def test_platform_options(monkeypatch):
    monkeypatch.setattr(d.subprocess, "CREATE_NO_WINDOW", 123, raising=False)
    assert d._popen_options("nt") == {"creationflags": 123}
    assert d._popen_options("posix") == {}


def test_missing_and_prelaunch_cancel(monkeypatch, tmp_path, offline):
    monkeypatch.setattr(d, "_yt_dlp_command", lambda: None)
    result, calls, *_ = run_worker(monkeypatch, tmp_path, FakeProc())
    assert result == ("failed", d.failure_summary("YT_DLP_MISSING")) and calls == []
    worker = d.DownloadWorker("url", "Best", tmp_path)
    seen = []
    worker.cancelled.connect(lambda: seen.append("cancelled"))
    worker.cancel()
    worker.run()
    assert seen == ["cancelled"]


def test_dialog_visibility_and_details():
    dialog = d.DownloadVideoDialog()
    assert dialog.auth_combo.currentText() == "None"
    assert dialog.browser_combo.currentData() == ""
    for mode in d.AUTH_MODES:
        dialog.auth_combo.setCurrentText(mode)
        assert dialog.browser_combo.isHidden() == (mode != "Browser cookies")
        assert dialog.cookie_edit.isHidden() == (mode != "Cookie file")
        assert dialog.profile_edit.isEnabled() == (mode == "Browser cookies")
        assert dialog.cookie_btn.isEnabled() == (mode == "Cookie file")
    assert dialog.details_edit.isHidden() and dialog.details_edit.isReadOnly()
    dialog.details_btn.setChecked(True)
    assert not dialog.details_edit.isHidden()
    dialog.close()


@pytest.mark.parametrize("garbage", ["unknown", 42, ["Cookie file"]])
def test_garbage_settings(tmp_path, garbage):
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    settings.setValue("downloader/auth_mode", garbage)
    dialog = d.DownloadVideoDialog(settings=settings)
    assert dialog.auth_combo.currentText() == "None" and dialog.cookie_edit.text() == ""
    dialog.close()


def test_dialog_validation_no_thread(monkeypatch, tmp_path):
    dialog = d.DownloadVideoDialog()
    dialog.url_edit.setText("https://example.invalid/video")
    warnings = []
    monkeypatch.setattr(d.QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    monkeypatch.setattr(d, "QThread", lambda *args: pytest.fail("invalid input started a thread"))
    for mode in ("Browser cookies", "Cookie file"):
        dialog.auth_combo.setCurrentText(mode)
        dialog._start_download()
        assert dialog._thread is None
    assert len(warnings) == 2
    dialog.close()


@pytest.mark.parametrize("mode", ["Browser cookies", "Cookie file"])
def test_dialog_settings_controls_no_cookie_read(monkeypatch, tmp_path, mode):
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    cookie = tmp_path / "synthetic.txt"
    cookie.write_bytes(b"PLANTED_CONTENT")
    class FakeThread:
        def __init__(self, *a):
            self.started = SimpleNamespace(connect=lambda *a: None)
            self.finished = SimpleNamespace(connect=lambda *a: None)
        def quit(self): pass
        def start(self): pass
        def deleteLater(self): pass
        def isRunning(self): return False
    monkeypatch.setattr(d, "QThread", FakeThread)
    monkeypatch.setattr(d.DownloadWorker, "moveToThread", lambda *a: None)
    dialog = d.DownloadVideoDialog(settings=settings)
    dialog.url_edit.setText("https://example.invalid/video")
    dialog.auth_combo.setCurrentText(mode)
    dialog.browser_combo.setCurrentText("firefox")
    dialog.profile_edit.setText(" Person Ω ")
    dialog.cookie_edit.setText(str(cookie))
    dialog._start_download()
    for control in (dialog.auth_combo, dialog.browser_combo, dialog.profile_edit, dialog.cookie_edit, dialog.cookie_btn):
        assert not control.isEnabled()
    dialog._reset_controls()
    assert dialog.auth_combo.isEnabled()
    dialog._thread_finished()
    dialog.close()
    settings.sync()
    assert "PLANTED_CONTENT" not in Path(settings.fileName()).read_text()
    monkeypatch.setattr(Path, "open", lambda *a, **kw: pytest.fail("restoring preferences must not read cookies"))
    restored = d.DownloadVideoDialog(settings=settings)
    assert restored.auth_combo.currentText() == mode
    assert restored.browser_combo.currentData() == "firefox"
    assert restored.profile_edit.text() == "Person Ω"
    assert restored.cookie_edit.text() == str(cookie)
    restored.close()


def test_y2_info_initial_neutral():
    dialog = d.DownloadVideoDialog()
    assert dialog.yt_dlp_info.text() == "yt-dlp: version shown when a download starts"
    dialog.close()


@pytest.mark.parametrize("change", ["mode", "path"])
def test_y2_info_selection_change_resets(change):
    dialog = d.DownloadVideoDialog()
    try:
        dialog.yt_dlp_combo.setCurrentText("Custom executable")
        dialog._show_tool_info("yt-dlp 2026.08.19 (custom)")
        assert dialog.yt_dlp_info.text() == "yt-dlp: 2026.08.19 (custom)"
        if change == "mode":
            dialog.yt_dlp_combo.setCurrentText("Automatic")
        else:
            dialog.yt_dlp_edit.setText("another executable")
        assert dialog.yt_dlp_info.text() == "yt-dlp: version shown when a download starts"
    finally:
        dialog.close()


@pytest.fixture
def y2_running_dialog(monkeypatch, tmp_path):
    class FakeThread:
        def __init__(self, *a):
            self.started = SimpleNamespace(connect=lambda *a: None)
            self.finished = SimpleNamespace(connect=lambda *a: None)
        def quit(self): pass
        def start(self): pass
        def deleteLater(self): pass
        def isRunning(self): return False
    monkeypatch.setattr(d, "QThread", FakeThread)
    monkeypatch.setattr(d.DownloadWorker, "moveToThread", lambda *a: None)
    monkeypatch.setattr(d.QMessageBox, "warning", lambda *a: None)
    dialog = d.DownloadVideoDialog()
    dialog.yt_dlp_combo.setCurrentText("Custom executable")
    dialog.yt_dlp_edit.setText(str(custom_executable(tmp_path)))
    dialog.url_edit.setText("https://example.invalid/video")
    dialog._start_download()
    try:
        yield dialog
    finally:
        dialog._thread_finished()
        dialog.close()


@pytest.mark.parametrize("outcome", ["failed", "missing", "input-error", "unverified", "cancelled"])
def test_y2_info_terminal_reset(y2_running_dialog, outcome):
    dialog = y2_running_dialog
    assert dialog.yt_dlp_info.text() == "yt-dlp: checking version..."
    if outcome == "failed":
        dialog._worker.tool_info.emit("yt-dlp 2026.08.19 (custom)")
        QApplication.processEvents()
        assert dialog.yt_dlp_info.text() == "yt-dlp: 2026.08.19 (custom)"
    if outcome == "cancelled":
        dialog._worker.cancelled.emit()
    else:
        message = {"failed": d.failure_summary("GENERIC_DOWNLOAD_ERROR"),
                   "missing": d.failure_summary("YT_DLP_MISSING"),
                   "input-error": "Choose a custom yt-dlp executable.",
                   "unverified": d.failure_summary("YT_DLP_UNVERIFIED")}[outcome]
        dialog._worker.failed.emit(message)
    QApplication.processEvents()
    # The real QThread emits finished after the worker's terminal signal.
    dialog._thread_finished()
    assert dialog.yt_dlp_info.text() == "yt-dlp: version shown when a download starts"
    assert dialog.yt_dlp_info.text() != "yt-dlp: checking version..."


@pytest.mark.parametrize("probed", [False, True])
def test_y2_info_running_changes_preserve_live_label(y2_running_dialog, probed):
    dialog = y2_running_dialog
    assert dialog.yt_dlp_info.text() == "yt-dlp: checking version..."
    if probed:
        dialog._worker.tool_info.emit("yt-dlp 2026.08.19 (custom)")
        QApplication.processEvents()
    expected = "yt-dlp: 2026.08.19 (custom)" if probed else "yt-dlp: checking version..."
    assert dialog.yt_dlp_info.text() == expected
    dialog.yt_dlp_combo.setCurrentText("Automatic")
    assert dialog.yt_dlp_info.text() == expected
    dialog.yt_dlp_edit.setText("programmatic path change")
    assert dialog.yt_dlp_info.text() == expected


@pytest.mark.parametrize("key,value", [("browser", "unknown"), ("browser_profile", "x::y"),
                                     ("cookie_file", "x\x00y")])
def test_other_garbage_settings(tmp_path, key, value):
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    settings.setValue("downloader/auth_mode", "Browser cookies")
    settings.setValue("downloader/" + key, value)
    dialog = d.DownloadVideoDialog(settings=settings)
    assert dialog.auth_combo.currentText() == "None" and dialog.profile_edit.text() == ""
    dialog.close()


def test_missing_executable_exception(monkeypatch, tmp_path, offline):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    seen = []
    worker.finished.connect(lambda p: seen.append("finished"))
    worker.cancelled.connect(lambda: seen.append("cancelled"))
    worker.failed.connect(seen.append)
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    worker.run()
    assert seen == [d.failure_summary("YT_DLP_MISSING")]


def test_exception_sanitized_and_cleanup_idempotent(monkeypatch, tmp_path, offline):
    proc = FakeProc(broken=True)
    result, _, details, *_ = run_worker(monkeypatch, tmp_path, proc)
    assert result[0] == "failed" and "stdout exploded" in "\n".join(details)
    worker = d.DownloadWorker("url", "Best", tmp_path)
    worker._proc = FakeProc(stubborn=True)
    worker.cancel()
    worker.cancel()
    worker._finish_cancelled_process()
    assert worker._proc.waits == [3, 3]
    assert worker._proc.terminated == worker._proc.killed == 1


def test_none_never_validates_cookie_input(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "validate_cookie_file", lambda *a: pytest.fail("None mode read cookies"))
    monkeypatch.setattr(d, "browser_specification", lambda *a: pytest.fail("None mode inspected browser"))
    assert "--cookies" not in command(tmp_path, cookie_file="/not/read", browser="firefox")


def test_cancel_only_terminates_without_wait_or_kill(tmp_path):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    proc = FakeProc(stubborn=True)
    calls = []
    for name in ("terminate", "wait", "kill"):
        original = getattr(proc, name)
        def record(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        setattr(proc, name, record)
    worker._proc = proc
    worker.cancel()
    worker.cancel()
    assert worker._cancelled
    assert calls == ["terminate"], "cancel must never wait or kill on its caller thread"
    runner = threading.Thread(target=worker._finish_cancelled_process)
    runner.start()
    runner.join(timeout=3)
    assert not runner.is_alive()
    assert calls == ["terminate", "wait", "kill", "wait"]
    assert proc.waits == [3, 3] and proc.done


def test_concurrent_cancel_terminates_once_without_cleanup(tmp_path):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    proc = worker._proc = FakeProc(stubborn=True)
    barrier = threading.Barrier(5)
    def cancel():
        barrier.wait(timeout=3)
        worker.cancel()
    runners = [threading.Thread(target=cancel) for _ in range(4)]
    for runner in runners:
        runner.start()
    barrier.wait(timeout=3)
    for runner in runners:
        runner.join(timeout=3)
        assert not runner.is_alive()
    assert proc.terminated == 1
    assert proc.waits == [] and proc.killed == 0, "concurrent cancel must not perform cleanup"
    worker._finish_cancelled_process()
    assert proc.waits == [3, 3] and proc.killed == 1 and proc.done


@pytest.mark.parametrize("phrase,category", [
    ("Requested format is not available", "GENERIC_DOWNLOAD_ERROR"),
    ("This video is unavailable", "VIDEO_UNAVAILABLE"),
    ("This video is not available", "VIDEO_UNAVAILABLE"),
    ("Video unavailable", "VIDEO_UNAVAILABLE"),
    ("Private video", "VIDEO_UNAVAILABLE"),
    ("Video has been removed", "VIDEO_UNAVAILABLE"),
    ("This upload has been removed by the uploader", "VIDEO_UNAVAILABLE"),
])
def test_format_and_video_unavailability(phrase, category):
    assert d.classify_failure("ERROR: " + phrase) == category


def test_format_failure_details_preserved(monkeypatch, tmp_path, offline):
    line = "ERROR: Requested format is not available"
    result, _, details, *_ = run_worker(monkeypatch, tmp_path, FakeProc([line], rc=1))
    assert result == ("failed", d.failure_summary("GENERIC_DOWNLOAD_ERROR"))
    assert line in "\n".join(details)


def test_cancel_during_worker_cleanup_does_not_wait_on_lock(tmp_path):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    proc = worker._proc = FakeProc()
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original_wait = proc.wait
    def wait(timeout=None):
        entered.set()
        assert release.wait(timeout=3), "cleanup seam was not released"
        return original_wait(timeout=timeout)
    proc.wait = wait
    cleanup = threading.Thread(target=worker._finish_cancelled_process)
    cancel = threading.Thread(target=lambda: (worker.cancel(), returned.set()))
    cleanup.start()
    try:
        assert entered.wait(timeout=3)
        cancel.start()
        assert returned.wait(timeout=3), "cancel blocked behind cleanup wait"
    finally:
        release.set()
        cleanup.join(timeout=3)
        if cancel.ident is not None:
            cancel.join(timeout=3)
    assert not cleanup.is_alive() and not cancel.is_alive()
    assert proc.terminated == 1 and proc.killed == 0 and proc.waits == [3]


@pytest.mark.parametrize("operation", ["poll", "terminate"])
def test_cancel_guards_process_oserror(tmp_path, operation):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    proc = worker._proc = FakeProc()
    def gone():
        raise OSError("child already gone")
    original = getattr(proc, operation)
    setattr(proc, operation, gone)
    worker.cancel()
    assert worker._cancelled and proc.waits == [] and proc.killed == 0
    setattr(proc, operation, original)
    worker._finish_cancelled_process()
    assert proc.done


def test_cancel_timer_unblocks_stdout_and_reaps(monkeypatch, tmp_path, offline, fake_timers):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    proc = FakeProc(stubborn=True)
    entered, released = threading.Event(), threading.Event()
    outcomes = []
    worker.cancelled.connect(lambda: outcomes.append("cancelled"), d.Qt.DirectConnection)
    worker.failed.connect(lambda text: outcomes.append("failed"), d.Qt.DirectConnection)
    worker.finished.connect(lambda path: outcomes.append("finished"), d.Qt.DirectConnection)
    def stream():
        entered.set()
        assert released.wait(timeout=3), "blocked stdout was not released"
        yield "[download] 10%"
    proc.stdout = stream()
    original_kill = proc.kill
    def kill():
        original_kill()
        released.set()
    proc.kill = kill
    monkeypatch.setattr(d.subprocess, "Popen", lambda cmd, **kw: FakeProbe() if "--version" in cmd else proc)
    runner = threading.Thread(target=worker.run)
    runner.start()
    try:
        assert entered.wait(timeout=3)
        worker.cancel()
        worker.cancel()
        assert proc.waits == [] and proc.killed == 0
        assert len(fake_timers) == 1, "cancel must arm one escalation timer"
        timer = fake_timers[0]
        assert timer.interval == 3 and timer.daemon and timer.started
        timer.fire()
    finally:
        released.set()
        runner.join(timeout=3)
    assert not runner.is_alive()
    assert outcomes == ["cancelled"] and proc.done and proc.killed == 1
    assert timer.cancelled, "worker cleanup must cancel the timer"


def test_cancel_timer_cancelled_on_child_exit(monkeypatch, tmp_path, offline, fake_timers):
    proc = FakeProc(hook="cancel")
    result, *_ = run_worker(monkeypatch, tmp_path, proc)
    assert result[0] == "cancelled" and proc.done
    assert len(fake_timers) == 1, "a live terminated child needs an escalation timer"
    assert fake_timers[0].cancelled, "child exit must cancel its timer"
    fake_timers[0].fire()  # A callback racing cancellation must not kill again.
    assert proc.killed == 0


@pytest.mark.parametrize("text", ["Use --cookies-from-browser or --cookies for the authentication.",
                                  "Use --cookies", "Use --cookies.", "Use --cookies\nERROR: next diagnostic"])
def test_bot_diagnostic_sentence_survives_redaction(text):
    assert d.sanitize(text) == text, "flag mentions in prose must survive byte-for-byte"


@pytest.mark.parametrize("profile", ["file", "session", "a"])
@pytest.mark.parametrize("mode,category", [("None", "BOT_VERIFICATION"), ("Browser cookies", "AUTH_REJECTED")])
def test_fixed_summary_survives_worker_and_dialog(monkeypatch, tmp_path, offline, profile, mode, category):
    result, *_ = run_worker(monkeypatch, tmp_path,
        FakeProc(["ERROR: Sign in to confirm you're not a bot"], rc=1),
        auth_mode=mode, browser="firefox", browser_profile=profile)
    expected = d.failure_summary(category)
    assert result == ("failed", expected), "worker must preserve approved fixed copy"
    dialog = d.DownloadVideoDialog()
    dialog.profile_edit.setText(profile)
    seen = []
    monkeypatch.setattr(d.QMessageBox, "warning", lambda *args: seen.append(args[-1]))
    try:
        dialog._download_failed(result[1])
        assert seen == [expected] and dialog.status_label.text() == expected, "dialog must preserve approved fixed copy"
    finally:
        dialog.close()


def test_details_config_once_without_empty_lines(monkeypatch, tmp_path, offline):
    signal = SimpleNamespace(connect=lambda *args: None)
    monkeypatch.setattr(d, "QThread", lambda *args: SimpleNamespace(
        started=signal, finished=signal, start=lambda: None, quit=lambda: None,
        deleteLater=lambda: None, isRunning=lambda: False))
    monkeypatch.setattr(d.DownloadWorker, "moveToThread", lambda *args: None)
    dialog = d.DownloadVideoDialog()
    dialog.url_edit.setText("https://example.invalid/video")
    dialog.folder_edit.setText(str(tmp_path))
    try:
        dialog._start_download()
        dialog._worker._version_details(d._yt_dlp_command(), os.environ.copy())
        QApplication.processEvents()
        text = dialog.details_edit.toPlainText()
        assert text.count(d.CONFIG_NOTE) == 1, "Details must show the config note once"
        assert text and all(text.split("\n")), "Details must omit empty lines"
    finally:
        dialog._thread_finished()
        dialog.close()


def test_cancel_timer_keeps_deadline_during_worker_wait(tmp_path, fake_timers):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    proc = worker._proc = FakeProc(stubborn=True)
    entered, released = threading.Event(), threading.Event()
    original_wait, original_kill = proc.wait, proc.kill
    def wait(timeout=None):
        entered.set()
        assert released.wait(timeout=3)
        return original_wait(timeout=timeout)
    def kill():
        original_kill()
        released.set()
    proc.wait, proc.kill = wait, kill
    worker.cancel()
    cleanup = threading.Thread(target=worker._finish_cancelled_process)
    cleanup.start()
    try:
        assert entered.wait(timeout=3)
        assert not fake_timers[0].cancelled, "cleanup must not postpone the cancellation deadline"
        fake_timers[0].fire()
    finally:
        released.set()
        cleanup.join(timeout=3)
    assert not cleanup.is_alive() and proc.done and proc.killed == 1
    assert proc.waits == [3] and fake_timers[0].cancelled


@pytest.mark.parametrize("word", ["or", "for", "and", "to", "the", "with", "is", "in", "."])
def test_cookie_flag_prose_connectives_preserved(word):
    text = "Use --cookies " + word
    assert d.sanitize(text) == text


@pytest.mark.parametrize("text", ['--cookies "or"', "--cookies=for", "['--cookies', 'and']",
    "--cookies /path", '--cookies "quoted path"', "--cookies-from-browser firefox:profile"])
def test_explicit_cookie_arguments_still_redacted(text):
    assert "<redacted>" in d.sanitize(text)


@pytest.mark.parametrize("profile", ["file", "session", "a"])
def test_fixed_no_mp4_and_validation_copy(monkeypatch, tmp_path, offline, profile):
    result, *_ = run_worker(monkeypatch, tmp_path, FakeProc(), browser_profile=profile)
    assert result == ("failed", "Download finished, but yt-dlp did not report a valid MP4 file in the output folder.")
    worker = d.DownloadWorker("-invalid", "Best", tmp_path, browser_profile=profile)
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    assert failures == ["Paste a video URL that does not start with '-'."]


def test_auth_consent_hints():
    dialog = d.DownloadVideoDialog()
    expected = ["No sign-in. Public videos only.",
                "yt-dlp will use your signed-in session from this browser for this download.",
                "yt-dlp will use the cookies in this file and may update it."]
    try:
        for mode, hint in zip(d.AUTH_MODES, expected):
            dialog.auth_combo.setCurrentText(mode)
            assert dialog.auth_hint.text() == hint
    finally:
        dialog.close()


@pytest.mark.parametrize("profile", ["SID", "a", "Cookie", "Bearer", "<", "redacted"])
@pytest.mark.parametrize("payload", ["ERROR: SID=PLANTED", "Cookie: PLANTED", "Set-Cookie: PLANTED",
    "Authorization: Bearer PLANTED", "Bearer PLANTED", "Basic PLANTED", "--cookies PLANTED_PATH", "['--cookies', 'PLANTED']",
    *[prefix + "youtube.com" + sep + sep.join(["TRUE", "/", "TRUE", "bad", "SID", "PLANTED"]) + "'"
      for sep in ("\t", r"\t") for prefix in ("", "'", "WARNING: skipping cookie file entry due to invalid expires at PLANTED_FIELD: '")]])
def test_adversarial_redaction_end_to_end(monkeypatch, tmp_path, offline, profile, payload):
    safe = d.sanitize(payload, browser_profile=profile)
    assert "PLANTED" not in safe, "structural redaction must remove cookie payloads first"
    assert d.sanitize(safe, browser_profile=profile) == safe, "redaction must be idempotent"
    assert "PLANTED" not in diagnostic_texts(monkeypatch, tmp_path, payload, browser_profile=profile)
def test_probe_cancel_owns_child_and_dialog_close(monkeypatch, tmp_path, offline, fake_timers):
    proc, entered, released = FakeProc(stubborn=True), threading.Event(), threading.Event()
    worker = d.DownloadWorker("url", "Best", tmp_path)
    outcomes, calls = [], []
    for name in ("cancelled", "finished", "failed"):
        getattr(worker, name).connect(lambda *a, _name=name: outcomes.append(_name), d.Qt.DirectConnection)
    def communicate(timeout=None):
        entered.set()
        assert released.wait(timeout=3)
        proc.done = True
        return "", None
    proc.communicate, proc.returncode = communicate, 0
    original_kill = proc.kill
    proc.kill = lambda: (original_kill(), released.set())
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=communicate()[0], returncode=0))
    monkeypatch.setattr(d.subprocess, "Popen", lambda cmd, **kw: (calls.append(cmd), proc)[1])
    runner = threading.Thread(target=worker.run)
    runner.start()
    dialog = d.DownloadVideoDialog()
    try:
        assert entered.wait(timeout=3)
        worker.cancel()
        assert proc.terminated == 1, "cancel must own and terminate the version probe"
        assert proc.waits == [] and proc.killed == 0
        dialog._worker = worker
        dialog._thread = SimpleNamespace(isRunning=runner.is_alive, quit=lambda: None,
            wait=lambda: (fake_timers[0].fire(), runner.join(timeout=3)))
        dialog.cancel_and_wait()
        assert not runner.is_alive() and proc.done and proc.waits == [3]
        assert outcomes == ["cancelled"] and len(calls) == 1 and "--version" in calls[0]
    finally:
        released.set()
        runner.join(timeout=3)
        dialog._thread_finished()
        dialog.close()
def test_del_url_rejected_before_thread(monkeypatch, tmp_path):
    url = "https://example.invalid/\x7fvideo"
    with pytest.raises(d.DownloadInputError, match="Paste a video URL"):
        command(tmp_path, url=url)
    dialog = d.DownloadVideoDialog()
    dialog.url_edit.setText(url)
    warnings = []
    monkeypatch.setattr(d.QMessageBox, "warning", lambda *a: warnings.append(a[-1]))
    monkeypatch.setattr(d, "QThread", lambda *a: pytest.fail("DEL URL started a thread"))
    dialog._start_download()
    assert warnings and dialog._thread is None
    dialog.close()
@pytest.mark.parametrize("field", ["cookie_file", "browser_profile"])
@pytest.mark.parametrize("secret", [r"C:\Users\Sin\cookies.txt", r"C:\Users\Sin\Firefox\x.default",
    "C:\\Users\\Sin\\Pröfil '1'\\x.default", 'C:/Users/Sin/Pröfil "1"/x.default', "Pröfil 1",
    "/home/alice/Basic Profile/cookies.txt", r"C:\Users\Bearer Q\c.txt", "/h/SID=x/c.txt",
    "Work --cookies foo", "Cookie: Pro", '"Q\'uo,te"', "Ünï Prøf", "Authorization: Pro", "__Secure-x=Pro", "Pro\tfile\nPrivate"])
@pytest.mark.parametrize("form", ["literal", "repr", "double_repr", "json", "json_unicode", "mixed", "mixed_repr", "forward", "backward", "shlex", "shlex_argv", "case"])
@pytest.mark.parametrize("template", ["'%s' does not look like a Netscape format cookies file", "[Errno 13] Permission denied: %s",
    'could not find cookies database in "%s"', "['--cookies', %s]", "Authorization: Basic %s"])
def test_escaped_paths_end_to_end(monkeypatch, tmp_path, offline, field, secret, form, template):
    rendered = {"literal": secret, "repr": repr(secret), "double_repr": repr(repr(secret)),
        "json": json.dumps(secret), "json_unicode": json.dumps(secret, ensure_ascii=False),
        "mixed": secret.replace("\\Users\\", "/Users\\").replace("\\Sin\\", "\\Sin/"),
        "mixed_repr": repr(secret.replace("\\Users\\", "/Users\\").replace("\\Sin\\", "\\Sin/")),
        "forward": secret.replace("\\", "/"), "backward": secret.replace("/", "\\"), "shlex": shlex.quote(secret),
        "shlex_argv": "--cookies " + shlex.quote(secret), "case": secret.upper() if "/" in secret or "\\" in secret else secret}[form]
    line = "ERROR: " + template % rendered
    safe = d.sanitize(line, **{field: secret})
    markers = [part for part in secret.replace("\\", "/").split("/") if len(part) >= 3] if "/" in secret or "\\" in secret else [secret]
    assert not any(marker.casefold() in safe.replace("<redacted>", "").casefold() for marker in markers), "union redaction must remove every secret fragment"
    assert rendered not in safe and d.sanitize(safe, **{field: secret}) == safe
    texts = diagnostic_texts(monkeypatch, tmp_path, line, **{field: secret}).replace("<redacted>", "")
    assert not any(marker.casefold() in texts.casefold() for marker in markers), "no signal or widget may expose a secret fragment"
def diagnostic_texts(monkeypatch, tmp_path, line, **auth):
    result, _, details, statuses, _ = run_worker(monkeypatch, tmp_path, FakeProc([line], rc=1), **auth)
    dialog, modal = d.DownloadVideoDialog(), []
    dialog.cookie_edit.setText(auth.get("cookie_file", "")); dialog.profile_edit.setText(auth.get("browser_profile", ""))
    monkeypatch.setattr(d.QMessageBox, "warning", lambda *a: modal.append(a[-1]))
    try:
        for text in details:
            dialog._append_details(text)
        dialog._download_failed(result[1])
        texts = [result[1], *details, *statuses, dialog.status_label.text(), dialog.details_edit.toPlainText(), *modal]
        return "\n".join(texts)
    finally:
        dialog.close()
def test_canonical_and_entered_cookie_paths_redacted(monkeypatch, tmp_path, offline):
    monkeypatch.chdir(tmp_path)
    cookie = tmp_path / "PLANTED 'cookies'.txt"
    cookie.write_bytes(b"synthetic")
    lines = [f"ERROR: {value!r} invalid cookie file" for value in (cookie.name, str(cookie))]
    _, _, details, _, _ = run_worker(monkeypatch, tmp_path, FakeProc(lines, rc=1), auth_mode="Cookie file", cookie_file=cookie.name)
    assert "PLANTED" not in "\n".join(details), "both entered and canonical repr paths must be redacted"
def test_downloader_preserves_serialization_boundary():
    source = Path(d.__file__).read_text()
    offenders = [token for token in ("dataclasses.asdict", "asdict(", "json.dump", "pickle.dump") if token in source]
    assert not offenders, f"downloader must preserve the serialization source guard: {offenders}"
@pytest.mark.parametrize("text,profile,cookie,expected", [("abcdef", "ab", "bcdef", "<redacted>"),
    ("ababa", "aba", "", "<redacted>"), ("<redacted> x", "d> x", "", "<redacted>"), ("abc x", "d> x", "abc", "<redacted>"),
    ("<redacted>", "a", "", "<redacted>"), ("COOKIES.TXT", "", "cookies.txt", "<redacted>"), (r"C:\USERS\PRIVATE\C.TXT", "", r"C:\Users\Private\c.txt", "<redacted>")])
def test_union_overlap_fixed_point_and_case(text, profile, cookie, expected):
    safe = d.sanitize(text, cookie_file=cookie, browser_profile=profile)
    assert safe == expected, "overlapping original spans must form one protected union"
    assert d.sanitize(safe, cookie_file=cookie, browser_profile=profile) == safe
def test_worker_all_literals_single_union(tmp_path):
    worker = d.DownloadWorker("url", "Best", tmp_path, cookie_file="ab")
    worker._auth["cookie_file"] = "bcdef"
    assert worker._safe("abcdef") == "<redacted>", "entered and canonical literals must share one union pass"
    dialog = d.DownloadVideoDialog(); dialog.cookie_edit.setText("bcdef"); dialog._redaction_literals = ("ab",)
    assert dialog._safe("abcdef") == "<redacted>"
    dialog.close()
@pytest.mark.parametrize("kind", ["relative", "tilde", pytest.param("symlink", marks=pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt", reason="needs symlink support and privileges"))])
def test_entered_alias_and_canonical_paths(monkeypatch, tmp_path, offline, kind):
    monkeypatch.chdir(tmp_path)
    cookie = tmp_path / "CANON_SECRET.txt"; cookie.write_bytes(b"synthetic")
    entered = cookie.name
    if kind == "symlink":
        alias = tmp_path / "ENTERED_ALIAS.txt"; alias.symlink_to(cookie); entered = alias.name
    if kind == "tilde":
        entered = "~/CANON_SECRET.txt"; expand = Path.expanduser
        monkeypatch.setattr(Path, "expanduser", lambda path: tmp_path / str(path)[2:] if str(path).startswith("~/") else expand(path))
    texts = diagnostic_texts(monkeypatch, tmp_path, f"ERROR: {entered!r} {str(cookie)!r} invalid cookie file", auth_mode="Cookie file", cookie_file=entered)
    assert "CANON_SECRET" not in texts and "ENTERED_ALIAS" not in texts
def test_seeded_union_and_linear_inputs():
    rng = random.Random(2718)
    for _ in range(30):
        text = "".join(rng.choices("abcd", k=16)); start = rng.randrange(8)
        cookie, profile = text[start:start+5], text[start+3:start+8]
        safe = d.sanitize(text, cookie_file=cookie, browser_profile=profile)
        assert cookie not in safe and profile not in safe and d.sanitize(safe, cookie_file=cookie, browser_profile=profile) == safe
    started = time.monotonic()
    for text in ('--cookies "' + 'a'*200000, 'Cookie:' + ' '*200000, 'Basic ' + 'a'*200000, 'X-' + 'x-'*100000):
        d.sanitize(text)
    assert time.monotonic() - started < 3, "diagnostic scanning must stay linear on large malformed input"


# Y2 tests written before production changes. All children are fakes.
def custom_executable(tmp_path, name="chosen yt-dlp Ω.exe"):
    path = tmp_path / name
    path.write_bytes(b"synthetic executable")
    path.chmod(0o755)
    return path


def test_y2_default_dialog_executable_selection():
    dialog = d.DownloadVideoDialog()
    try:
        combos = [c for c in dialog.findChildren(d.QComboBox)
                  if c.accessibleName() == "yt-dlp executable mode"]
        assert len(combos) == 1, "Download dialog must expose executable selection"
        assert [combos[0].itemText(i) for i in range(combos[0].count())] == ["Automatic", "Custom executable"]
        assert combos[0].currentText() == "Automatic"
    finally:
        dialog.close()


def test_y2_probe_returns_real_version(monkeypatch, tmp_path):
    worker = d.DownloadWorker("url", "Best", tmp_path)
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **kw: FakeProbe())
    assert worker._version_details(d.DownloadTool(["fake"], "system"), {}) == "2026.08.19"


@pytest.mark.parametrize("name", ["yt-dlp.exe", "path with spaces.exe", "下载 Ω.exe"])
def test_y2_custom_invokes_exact_tool(monkeypatch, tmp_path, name):
    path = custom_executable(tmp_path, name)
    monkeypatch.setattr(d, "_yt_dlp_command", lambda: pytest.fail("Custom must never discover Automatic"))
    probe = FakeProbe()
    mp4 = tmp_path / "result.mp4"
    mp4.write_bytes(b"synthetic mp4")
    result, calls, details, *_ = run_worker(monkeypatch, tmp_path, FakeProc([str(mp4)]), probe,
        yt_dlp_mode="Custom executable", yt_dlp_path=str(path))
    assert result == ("finished", mp4)
    assert probe.command == [str(path), "--ignore-config", "--version"]
    assert probe.timeout == 10
    assert calls[0][0][0] == str(path) and "--ignore-config" in calls[0][0]
    assert "--cookies" not in calls[0][0] and "--cookies-from-browser" not in calls[0][0]
    assert not calls[0][1].get("shell", False)
    assert "yt-dlp 2026.08.19 (custom)" in "\n".join(details)


@pytest.mark.parametrize("kind", ["empty", "missing", "directory", "url", "nul", "control", "self", "no-exec"])
def test_y2_custom_static_validation(monkeypatch, tmp_path, kind):
    path = custom_executable(tmp_path)
    value = {"empty": "", "missing": str(tmp_path / "SECRET_MISSING"), "directory": str(tmp_path),
             "url": "https://example.invalid/SECRET", "nul": "SECRET\0", "control": "SECRET\n",
             "self": sys.executable, "no-exec": str(path)}[kind]
    if kind == "no-exec":
        if os.name == "nt":
            pytest.skip("POSIX executable permission check")
        path.chmod(0o644)
    with pytest.raises(d.DownloadInputError) as error:
        d.custom_yt_dlp_tool(value)
    assert "SECRET" not in str(error.value) and (not value or value not in str(error.value))
    monkeypatch.setattr(d, "_yt_dlp_command", lambda: pytest.fail("invalid Custom silently fell back"))
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **kw: pytest.fail("invalid Custom launched a process"))
    worker = d.DownloadWorker("url", "Best", tmp_path, yt_dlp_mode="Custom executable", yt_dlp_path=value)
    failed, imported = [], []
    worker.failed.connect(failed.append)
    worker.finished.connect(imported.append)
    worker.run()
    assert len(failed) == 1 and not imported


def test_y2_windows_extension_validation(monkeypatch, tmp_path):
    # Exercise Windows policy on real POSIX files; not a native Windows filesystem test.
    monkeypatch.setattr(d, "os", SimpleNamespace(name="nt", access=os.access, X_OK=os.X_OK))
    exe = custom_executable(tmp_path, "tool.EXE")
    assert d.custom_yt_dlp_tool(str(exe)) == d.DownloadTool([str(exe)], "custom")
    other = custom_executable(tmp_path, "tool.cmd")
    with pytest.raises(d.DownloadInputError):
        d.custom_yt_dlp_tool(str(other))


@pytest.mark.parametrize("frozen", [False, True])
def test_y2_custom_never_launches_self_alias(monkeypatch, tmp_path, frozen):
    alias = tmp_path / "Cove.exe"
    try:
        alias.symlink_to(sys.executable)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    with pytest.raises(d.DownloadInputError):
        d.custom_yt_dlp_tool(str(alias))


@pytest.mark.parametrize("relative", [False, True])
def test_y2_custom_preserves_selected_symlink(monkeypatch, tmp_path, relative):
    target = custom_executable(tmp_path, "target executable.exe")
    selected = tmp_path / "selected launcher Ω.exe"
    try:
        selected.symlink_to(target)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    monkeypatch.chdir(tmp_path)
    value = selected.name if relative else str(selected)
    monkeypatch.setattr(d, "_yt_dlp_command", lambda: pytest.fail("Custom must never discover Automatic"))
    probe = FakeProbe()
    mp4 = tmp_path / "result.mp4"
    mp4.write_bytes(b"synthetic mp4")
    result, calls, details, *_ = run_worker(monkeypatch, tmp_path, FakeProc([str(mp4)]), probe,
        yt_dlp_mode="Custom executable", yt_dlp_path=value)
    assert result == ("finished", mp4)
    assert probe.command == [str(selected), "--ignore-config", "--version"]
    assert calls[0][0][0] == str(selected)
    assert f"Executable: {selected}" in "\n".join(details)
    assert str(target) not in "\n".join(details)


@pytest.mark.parametrize("mode", ["Automatic", "Custom executable"])
@pytest.mark.parametrize("token", ["2026.08.19", "2026.08.19.232802", "2026.08.19" + "x" * 54,
                                  "2026.08.19" + "x" * 55], ids=["normal", "nightly", "64-chars", "65-chars"])
def test_y2_version_token_length(monkeypatch, tmp_path, mode, token):
    selected = custom_executable(tmp_path)
    monkeypatch.setattr(d, "_yt_dlp_command", lambda: d.DownloadTool(["automatic-fake"], "system"))
    worker = d.DownloadWorker("https://example.invalid/video", "Best", tmp_path,
                              yt_dlp_mode=mode, yt_dlp_path=str(selected))
    probe = FakeProbe()
    def communicate(timeout=None):
        probe.timeout, probe.done = timeout, True
        return "  " + token + "\n", None
    probe.communicate = communicate
    mp4 = tmp_path / "result.mp4"
    mp4.write_bytes(b"synthetic mp4")
    download = FakeProc([str(mp4)])
    calls, outcomes, info, details = [], [], [], []
    def spawn(cmd, **options):
        calls.append(cmd)
        return probe if "--version" in cmd else download
    monkeypatch.setattr(d.subprocess, "Popen", spawn)
    worker.finished.connect(lambda path: outcomes.append(("finished", path)))
    worker.failed.connect(lambda text: outcomes.append(("failed", text)))
    worker.tool_info.connect(info.append)
    worker.details.connect(details.append)
    dialog = d.DownloadVideoDialog()
    worker.tool_info.connect(dialog._show_tool_info)
    try:
        worker.run()
        source = "custom" if mode == "Custom executable" else "system"
        version = token if len(token) <= 64 else "unknown"
        assert info == [f"yt-dlp {version} ({source})"]
        assert dialog.yt_dlp_info.text() == f"yt-dlp: {version} ({source})"
        assert probe.timeout == 10
        if len(token) > 64:
            assert token not in "\n".join(details)
        if len(token) > 64 and mode == "Custom executable":
            assert outcomes == [("failed", d.failure_summary("YT_DLP_UNVERIFIED"))]
            assert len(calls) == 1, "unrecognized Custom version must gate downloads"
        else:
            assert outcomes == [("finished", mp4)] and len(calls) == 2
    finally:
        dialog.close()


def test_y2_automatic_invalid_probe_is_non_gating(monkeypatch, tmp_path, offline):
    probe = FakeProbe()
    probe.communicate = lambda timeout=None: ("unrecognized version", None)
    mp4 = tmp_path / "automatic.mp4"
    mp4.write_bytes(b"synthetic")
    result, calls, *_ = run_worker(monkeypatch, tmp_path, FakeProc([str(mp4)]), probe)
    assert result == ("finished", mp4) and len(calls) == 1


@pytest.mark.parametrize("kind", ["invalid", "empty", "multiline", "failure", "timeout"])
def test_y2_custom_probe_failure_gates_download(monkeypatch, tmp_path, kind):
    path = custom_executable(tmp_path)
    probe = FakeProbe(kind)
    if kind in ("invalid", "empty", "multiline"):
        def communicate(timeout=None):
            probe.timeout, probe.done = timeout, True
            return {"invalid": "SECRET not yt-dlp", "empty": "", "multiline": "2026.08.19\nSECRET"}[kind], None
        probe.communicate = communicate
    monkeypatch.setattr(d, "_yt_dlp_command", lambda: pytest.fail("Custom probe failure fell back"))
    result, calls, *_ = run_worker(monkeypatch, tmp_path, FakeProc(), probe,
        yt_dlp_mode="Custom executable", yt_dlp_path=str(path))
    assert result == ("failed", "The selected yt-dlp could not be verified. Choose another executable or switch to Automatic.")
    assert calls == [] and probe.timeout == 10
    if kind == "timeout":
        assert probe.killed == 1 and probe.done


def test_y2_custom_cancel_probe(monkeypatch, tmp_path):
    path = custom_executable(tmp_path)
    worker = d.DownloadWorker("url", "Best", tmp_path, yt_dlp_mode="Custom executable", yt_dlp_path=str(path))
    probe = FakeProbe("timeout")
    def communicate(timeout=None):
        assert timeout == 10
        worker.cancel()
        raise subprocess.TimeoutExpired("version", timeout)
    probe.communicate = communicate
    calls, outcomes = [], []
    def spawn(cmd, **kw):
        calls.append(cmd)
        assert "--version" in cmd, "cancelled probe started a download"
        return probe
    monkeypatch.setattr(d.subprocess, "Popen", spawn)
    worker.cancelled.connect(lambda: outcomes.append("cancelled"))
    worker.failed.connect(lambda _: outcomes.append("failed"))
    worker.run()
    assert outcomes == ["cancelled"] and len(calls) == 1
    assert probe.killed == 1 and probe.done


def test_y2_tool_info_and_redaction(monkeypatch, tmp_path):
    path = custom_executable(tmp_path)
    worker = d.DownloadWorker("url", "Best", tmp_path, yt_dlp_mode="Custom executable", yt_dlp_path=str(path),
                              browser_profile="PRIVATE_PROFILE", cookie_file="PRIVATE_COOKIES")
    info, details = [], []
    worker.tool_info.connect(info.append)
    worker.details.connect(details.append)
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **kw: FakeProbe())
    worker._version_details(d.custom_yt_dlp_tool(str(path)), {})
    assert info == ["yt-dlp 2026.08.19 (custom)"]
    assert not any("PRIVATE" in text for text in info + details)
    dialog = d.DownloadVideoDialog()
    dialog._show_tool_info(info[0])
    assert dialog.yt_dlp_info.text() == "yt-dlp: 2026.08.19 (custom)"
    dialog.close()


@pytest.mark.parametrize("key,value", [("yt_dlp_mode", "bad"), ("yt_dlp_mode", 2),
    ("yt_dlp_path", ["bad"]), ("yt_dlp_path", "x\0"), ("yt_dlp_path", "x\x7f"), ("yt_dlp_path", "x" * 4097)])
def test_y2_malformed_settings(tmp_path, key, value):
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    settings.setValue("downloader/yt_dlp_mode", "Custom executable")
    settings.setValue("downloader/" + key, value)
    dialog = d.DownloadVideoDialog(settings=settings)
    assert dialog.yt_dlp_combo.currentText() == "Automatic" and dialog.yt_dlp_edit.text() == ""
    dialog.close()


def test_y2_dialog_custom_validation_keeps_text(monkeypatch, tmp_path):
    dialog = d.DownloadVideoDialog()
    dialog.url_edit.setText("https://example.invalid/video")
    dialog.yt_dlp_combo.setCurrentText("Custom executable")
    dialog.yt_dlp_edit.setText("SECRET_MISSING")
    warnings = []
    monkeypatch.setattr(d.QMessageBox, "warning", lambda *a: warnings.append(a[-1]))
    monkeypatch.setattr(d, "QThread", lambda *a: pytest.fail("bad path started thread"))
    monkeypatch.setattr(d.subprocess, "Popen", lambda *a, **kw: pytest.fail("dialog ran a probe"))
    dialog._start_download()
    assert len(warnings) == 1 and "SECRET" not in warnings[0]
    assert dialog.yt_dlp_edit.text() == "SECRET_MISSING" and dialog._thread is None
    assert dialog.auth_combo.currentText() == "None"
    dialog.close()


def test_y2_dialog_settings_wiring_controls(monkeypatch, tmp_path):
    class FakeThread:
        def __init__(self, *a):
            self.started = SimpleNamespace(connect=lambda *a: None)
            self.finished = SimpleNamespace(connect=lambda *a: None)
        def quit(self): pass
        def start(self): pass
        def deleteLater(self): pass
        def isRunning(self): return False
    monkeypatch.setattr(d, "QThread", FakeThread)
    monkeypatch.setattr(d.DownloadWorker, "moveToThread", lambda *a: None)
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    dialog = d.DownloadVideoDialog(settings=settings)
    assert dialog.yt_dlp_edit.isHidden() and not dialog.yt_dlp_edit.isEnabled()
    assert dialog.yt_dlp_edit.accessibleName() == "Custom yt-dlp executable path"
    assert dialog.yt_dlp_btn.accessibleName() == "Choose yt-dlp executable"
    path = custom_executable(tmp_path)
    dialog.yt_dlp_combo.setCurrentText("Custom executable")
    dialog.yt_dlp_edit.setText(str(path))
    assert not settings.contains("downloader/yt_dlp_mode"), "persist only at start"
    assert dialog.auth_combo.currentText() == "None" and not dialog.cookie_btn.isEnabled()
    assert "permissions" in dialog.yt_dlp_hint.text()
    monkeypatch.setattr(d.QFileDialog, "getOpenFileName", lambda *a: (str(path), ""))
    dialog.yt_dlp_btn.click()
    assert dialog.yt_dlp_edit.text() == str(path)
    dialog.url_edit.setText("https://example.invalid/video")
    dialog._start_download()
    for widget in (dialog.yt_dlp_combo, dialog.yt_dlp_edit, dialog.yt_dlp_btn):
        assert not widget.isEnabled()
    assert dialog._worker._yt_dlp_mode == "Custom executable" and dialog._worker._yt_dlp_path == str(path)
    dialog._worker.tool_info.emit("yt-dlp 2026.08.19 (custom)")
    QApplication.processEvents()
    assert dialog.yt_dlp_info.text() == "yt-dlp: 2026.08.19 (custom)"
    imported = []
    dialog.downloadFinished.connect(imported.append)
    monkeypatch.setattr(d.QMessageBox, "warning", lambda *a: None)
    dialog._download_failed("fixed failure")
    assert imported == []
    dialog._thread_finished()
    assert all(w.isEnabled() for w in (dialog.yt_dlp_combo, dialog.yt_dlp_edit, dialog.yt_dlp_btn))
    dialog.close()
    restored = d.DownloadVideoDialog(settings=settings)
    assert restored.yt_dlp_combo.currentText() == "Custom executable"
    assert restored.yt_dlp_edit.text() == str(path)
    assert restored.auth_combo.currentText() == "None"
    restored.close()
