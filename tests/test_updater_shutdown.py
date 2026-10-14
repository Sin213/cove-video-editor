"""Updater shutdown through real Qt threads and the real GUI entry point.

Network seams are deterministic: no test may contact GitHub. Each Qt scenario
owns a bounded subprocess/session and isolated HOME/XDG directories. Child
stdout/stderr (including crash evidence on the baseline) are retained when
COVE_SHUTDOWN_EVIDENCE is set. Logical deadlines leave plain daemon fetches
alive; they do not cancel DNS or sockets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]

GUI = r'''
import atexit, gc, hashlib, io, json, os, sys, threading, time, weakref
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox
import shiboken6
from cove_video_editor import updater as upd
from cove_video_editor import __main__ as entry
from cove_video_editor import app as appmod

case, mode = sys.argv[1:3]
t0 = time.monotonic()
entered = threading.Event()
release = threading.Event()
fetch_threads, fetch_calls, prompts, effects, windows, closes = [], [], [], [], [], []
destroyed, terminal = [], []
deleted_window = None
payload = b'complete verified AppImage'
info = upd.UpdateInfo('99.0.0', 'https://invalid/release',
                      'cove-99.AppImage', 'https://invalid/asset', len(payload))
upd.FETCH_DEADLINE_S = .18 if case == 'deadline' else 10.0

def emit(event, **kw):
    print('RESULT ' + json.dumps(dict(event=event, **kw)), flush=True)

def fetch(timeout=8.0):
    fetch_calls.append(timeout)
    fetch_threads.append(threading.current_thread())
    entered.set()
    if case in ('active', 'unclosed', 'cycles', 'deadline', 'destroy_check', 'double_close'):
        release.wait(30)
    if case == 'exception':
        raise RuntimeError('fake fetch exception')
    if case in ('normal', 'active', 'unclosed', 'cycles', 'deadline', 'destroy_check', 'double_close'):
        return {'tag_name': 'v99.0.0', 'html_url': 'https://invalid/release', 'assets': []}
    return None
upd.fetch_latest_release = fetch

class Response:
    def __init__(self, sidecar=False):
        self.sidecar = sidecar
        self.headers = {'Content-Length': str(len(payload))}
        self.stream = io.BytesIO((hashlib.sha256(payload).hexdigest() + '  asset\n').encode()
                                 if sidecar else payload)
        self.reads = 0
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, n=-1):
        self.reads += 1
        if case == 'download_failed_close':
            entered.set()
            raise RuntimeError('controlled download failure')
        if not self.sidecar and case in ('download', 'download_park', 'download_boundary',
                                        'download_destroy_done', 'download_destroy_fail'):
            if self.reads == 1:
                return self.stream.read(3)
            entered.set()
            # Finite socket-read model: cancellation is observed after read.
            release.wait(3 if '_destroy_' in case else 20.5 if case == 'download_boundary' else
                         (.35 if case == 'download' else .6))
            if case == 'download_destroy_fail': raise RuntimeError('held read failed')
        return self.stream.read(n)

def urlopen(req, timeout):
    assert req.full_url.startswith('https://invalid/'), req.full_url
    return Response(req.full_url.endswith('.sha256'))
upd.urllib.request.urlopen = urlopen
upd.swap_in_appimage = lambda path: effects.append('swap') or path
upd.relaunch = lambda path: effects.append('relaunch')
QMessageBox.warning = lambda *args: effects.append('warning')
appmod._open_url = lambda *args: effects.append('open_url')

def prompt(self, data): prompts.append(data.latest_version)
base_prompt = appmod.MainWindow._prompt_update
appmod.MainWindow._prompt_update = prompt

class SpyBox:
    Information = AcceptRole = HelpRole = RejectRole = 0
    def __init__(self, *args): effects.append('prompt_dialog')
    def setIcon(self, *args): pass
    def setWindowTitle(self, *args): pass
    def setText(self, *args): pass
    def setInformativeText(self, *args): pass
    def addButton(self, *args): return object()
    def exec(self): return 0
    def clickedButton(self): return None
    @staticmethod
    def warning(*args): effects.append('warning')

Base = appmod.MainWindow
class Win(Base):
    def __init__(self):
        super().__init__()
        if case == 'cycles': entered.clear()
        windows.append(self)
        self.state = 'wait'
        self.start_time = time.monotonic()
        self.cancel_signals = []
        self.timer_fires = []
        self.download_outcome = None
        self.poll = QTimer(self)
        self.poll.setInterval(10)
        self.poll.timeout.connect(self.tick)
        self.poll.start()
        if case in ('early', 'guards', 'late_downloaded', 'late_failed', 'timer'):
            if case == 'timer' and hasattr(self, '_update_timer'):
                self._update_timer.timeout.connect(lambda: self.timer_fires.append(1))
                self._update_timer.start(100)
            QTimer.singleShot(25, self.request_close)
        elif case.startswith('download'):
            if case == 'download_park': appmod._CLOSE_JOIN_MS = 25
            QTimer.singleShot(25, lambda: self._install_update(info))
        else:
            QTimer.singleShot(25, self.begin_check)
    def begin_check(self):
        self._check_for_updates_in_background()
        self._check_for_updates_in_background()  # one in-flight fetch only
        self._update_worker.failed.connect(self.cancel_signals.append, Qt.DirectConnection)
    def closeEvent(self, event):
        th = self._update_download_thread if case.startswith('download') else self._update_thread
        record = dict(entered=entered.is_set(), running=bool(th and th.isRunning()),
                      progress_visible=bool(self._update_progress and self._update_progress.isVisible()))
        begin = time.monotonic()
        super().closeEvent(event)
        record.update(duration=time.monotonic()-begin,
                      after_running=bool(th and th.isRunning()),
                      cancelled=list(self.cancel_signals))
        closes.append(record)
        emit('close', **record)
    def _on_update_downloaded(self, path):
        if '_destroy_' in case: effects.append('deleted_completion_callback')
        if case == 'download_completed_close':
            self.download_outcome = 'finished'
        else:
            super()._on_update_downloaded(path)
    def _on_update_download_failed(self, message):
        if '_destroy_' in case: effects.append('deleted_failure_callback')
        if case == 'download_failed_close':
            self.download_outcome = 'failed'
        else:
            super()._on_update_download_failed(message)
    def request_close(self):
        if self.state == 'closing': return
        self.state = 'closing'
        if case == 'unclosed':
            # Leave MainWindow open and bypass its closeEvent; only atexit owns
            # this live check after the event loop returns.
            emit('unclosed', entered=entered.is_set(), running=self._update_thread.isRunning())
            self._stop_encoder_probe()
            QApplication.instance().exit(0)
            return
        if case in ('cycles', 'timer', 'double_close'):
            QApplication.instance().setQuitOnLastWindowClosed(False)
        if mode == 'quit': QApplication.instance().quit()
        else: self.close()
        if case == 'double_close':
            # QWidget may ignore close() on a hidden window; dispatch its second
            # close event through Qt to exercise the same virtual closeEvent.
            QCoreApplication.sendEvent(self, QCloseEvent())
            QApplication.instance().quit()
        elif case == 'cycles':
            if len(windows) < 3:
                Win().show()
            else:
                QApplication.instance().quit()
        elif case == 'timer':
            QTimer.singleShot(200, QApplication.instance().quit)
        elif case == 'guards':
            appmod.QMessageBox = SpyBox
            self._check_for_updates_in_background()
            self._on_update_available(info)
            base_prompt(self, info)
            self._install_update(info)
        elif case == 'late_downloaded':
            # A queued completion may arrive after closeEvent returns.
            from PySide6.QtWidgets import QProgressDialog
            self._update_progress = QProgressDialog(self)
            self._on_update_downloaded('/tmp/never-published.AppImage')
        elif case == 'late_failed':
            from PySide6.QtWidgets import QProgressDialog
            self._update_progress = QProgressDialog(self)
            self._on_update_download_failed('cancelled')
    def tick(self):
        if time.monotonic()-self.start_time > 4:
            emit('watchdog')
            self.request_close()
        if self.state != 'wait': return
        if case == 'destroy_check' or '_destroy_' in case:
            th = self._update_download_thread if '_destroy_' in case else self._update_thread
            if entered.is_set() and th and th.isRunning():
                del th
                self.destroy_active()
            return
        if case.startswith('download'):
            if case == 'download_normal':
                # Production completion swaps, relaunches and quits itself.
                return
            th = self._update_download_thread
            if case in ('download_completed_close', 'download_failed_close'):
                if self.download_outcome and th is None:
                    # Hold only terminal UI actions so a real finished/failed
                    # download leaves its real progress dialog for window close.
                    self._update_progress.show()
                    self.request_close()
            elif (entered.is_set() and th and th.isRunning()
                  and self._update_progress.isVisible()):
                self.request_close()
        elif case in ('active', 'unclosed', 'cycles', 'double_close'):
            th = self._update_thread
            if entered.is_set() and th and th.isRunning(): self.request_close()
        elif case == 'deadline':
            if entered.is_set() and self._update_thread is None:
                emit('deadline', inner_alive=fetch_threads[0].is_alive(),
                     inner_daemon=fetch_threads[0].daemon, failed=self.cancel_signals)
                self.request_close()
        elif case in ('after', 'normal', 'fail', 'exception'):
            if entered.is_set() and self._update_thread is None:
                emit('completed', stale_worker=self._update_worker is not None)
                if case == 'normal': self._on_update_available(info)
                self.request_close()
    def destroy_active(self):
        global deleted_window, destruction_owned, observer
        QApplication.instance().setQuitOnLastWindowClosed(False)
        worker = self._update_download_worker if '_destroy_' in case else self._update_worker
        if case == 'destroy_check':
            worker.updateAvailable.connect(lambda _: terminal.append('available'), Qt.DirectConnection)
        else:
            worker.finished.connect(lambda _: terminal.append('finished'), Qt.DirectConnection)
            worker.failed.connect(lambda _: terminal.append('failed'), Qt.DirectConnection)
        self._stop_encoder_probe()
        self.destroyed.connect(lambda: destroyed.append('window'))
        deleted_window = weakref.ref(self)
        self._update_thread = self._update_worker = None
        self._update_download_thread = self._update_download_worker = None
        windows.clear()
        del worker
        self.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()
        destruction_owned = bool(upd._LIVE_UPDATE_THREADS and upd._LIVE_UPDATE_THREADS[0][0].isRunning())
        emit('destroyed_active', invalid=not shiboken6.isValid(self), owned=destruction_owned)
        release.set()  # only now produce the real queued terminal signal
        observer = QTimer()
        observer.timeout.connect(observe_destroyed)
        observer.start(10)

def observe_destroyed():
    if terminal and not upd._LIVE_UPDATE_THREADS:
        observer.stop()
        QApplication.instance().quit()
observer = None

if case == 'download_boundary':
    # Registered before start_download's hook: report filesystem state AFTER
    # the real atexit cancel/join has run, rather than at main()'s early return.
    def report_download_exit():
        cache = Path(os.environ['HOME']) / '.cache/cove-video-editor'
        emit('download_exit', download_exists=(cache / 'cove-99.AppImage').exists(),
             cache_files=sorted(p.name for p in cache.iterdir()) if cache.exists() else [],
             effects=effects, join_ms=getattr(upd, 'SHUTDOWN_JOIN_MS', 0))
    atexit.register(report_download_exit)
entry.MainWindow = Win
code = entry.main(['cove-test'])
if case == 'destroy_check' or '_destroy_' in case:
    receiver = deleted_window()
    path = Path(os.environ['HOME']) / '.cache/cove-video-editor/cove-99.AppImage'
    emit('returned', code=code, destroyed=destroyed, terminal=terminal, closes=closes,
         prompts=prompts, effects=effects, registry=len(upd._LIVE_UPDATE_THREADS),
         invalid=receiver is None or not shiboken6.isValid(receiver),
         owners=len(windows), download_exists=path.exists())
    sys.exit(code)
w = windows[-1]
download_path = Path(os.environ['HOME']) / '.cache/cove-video-editor/cove-99.AppImage'
th = w._update_thread
emit('returned', code=code, fetches=len(fetch_calls), prompts=prompts,
     effects=effects, closes=closes, windows=len(windows),
     running=bool(th and th.isRunning()), timer_fires=w.timer_fires,
     timer_active=bool(getattr(w, '_update_timer', None) and w._update_timer.isActive()),
     download_exists=download_path.exists(),
     download_outcome=w.download_outcome,
     download_bytes=download_path.read_bytes().hex() if download_path.exists() else '',
     elapsed=time.monotonic()-t0)
# This is registered AFTER start_check's hook, so it prints the pre-shutdown
# positive control first; production's atexit hook then must join the check.
if case == 'unclosed':
    atexit.register(lambda: emit('atexit_positive', running=w._update_thread.isRunning()))
sys.exit(code)
'''


WORKER = r'''
import io, json, sys, threading, time
from pathlib import Path
from PySide6.QtCore import QCoreApplication, Qt
from cove_video_editor import updater as upd
from cove_video_editor import app as appmod
app = QCoreApplication([])
case = sys.argv[1]
entered, release = threading.Event(), threading.Event()
inners, signals = [], []
hold_registered = False
new = {'tag_name': 'v99.0.0', 'html_url': 'https://invalid/release', 'assets': []}
data = {'older': {'tag_name': 'v0.0.0', 'assets': []}, 'missing': {},
        'no_assets': new, 'none': None}.get(case, new)
upd.FETCH_DEADLINE_S = .08 if case in ('deadline', 'late_deadline') else 10.0
def fetch(timeout=8.0):
    entered.set()
    inners.append(threading.current_thread())
    if hold_registered or case in ('cancel', 'deadline', 'late_cancel', 'late_deadline', 'shutdown'):
        release.wait(3)
    if case == 'exception': raise RuntimeError('fake exploded')
    return data
upd.fetch_latest_release = fetch
# Safety net for every URL path in these unit cases.
upd.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected network'))
thread, worker = upd.start_check('1.0.0')
for name in ('updateAvailable', 'noUpdate', 'failed'):
    getattr(worker, name).connect(lambda *a, n=name: signals.append((n, str(a[0]) if a else '')),
                                 Qt.DirectConnection)
if case == 'registry_unstarted':
    # Keep the first registered pair unstarted while another finishes/prunes.
    other, other_worker = upd.start_check('1.0.0')
    other.start()
    assert other.wait(600)
    app.processEvents()
    retained = any(t is thread for t, w in upd._LIVE_UPDATE_THREADS)
    assert retained and len(upd._LIVE_UPDATE_THREADS) == 1, 'unstarted owner pruned'
    hold_registered = True
    entered.clear()
    blocked, blocked_worker = upd.start_check('1.0.0')
    gate = threading.Event()
    blocked_worker.failed.connect(lambda _: gate.wait(1), Qt.DirectConnection)
    blocked.start()
    try:
        assert entered.wait(1)
        start = time.monotonic()
        remaining = upd.shutdown_update_threads(0)
        duration = time.monotonic() - start
        assert duration < .2 and not thread.isRunning()
        assert blocked.isRunning() and any(t is blocked for t, w in remaining)
        assert any(t is blocked for t, w in upd._LIVE_UPDATE_THREADS)
    finally:
        gate.set(); release.set(); blocked.quit()
        assert blocked.wait(1500)
        app.processEvents()
    assert not upd._LIVE_UPDATE_THREADS, 'finished pair retained'
    print('RESULT '+json.dumps(dict(event='registry', retained=retained,
                                   duration=duration, remaining=len(remaining), released=True)), flush=True)
    sys.exit(0)
thread.start()
assert entered.wait(1), 'fake never entered'
t0 = time.monotonic()
result = {}
try:
    if case in ('cancel', 'late_cancel'):
        cancel = getattr(worker, 'cancel', lambda: None)
        cancel()
        thread.quit()
        joined = thread.wait(450)
        result.update(joined=joined, duration=time.monotonic()-t0)
    elif case in ('deadline', 'late_deadline'):
        joined = thread.wait(450)
        result.update(joined=joined, duration=time.monotonic()-t0,
                      inner_alive=inners[0].is_alive(), inner_daemon=inners[0].daemon)
    elif case == 'shutdown':
        shutdown = getattr(upd, 'shutdown_update_threads', None)
        if shutdown is None:
            result.update(joined=False, duration=0)
        else:
            remaining = shutdown(300)
            result.update(joined=not thread.isRunning(), remaining=len(remaining),
                          duration=time.monotonic()-t0)
    else:
        # Deliver the baseline's queued quit; production must also finish.
        deadline = time.monotonic()+1
        while thread.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            thread.wait(5)
        result['joined'] = not thread.isRunning()
    result['before_release'] = list(signals)
finally:
    release.set()
    thread.quit()
    assert thread.wait(1500), 'unit cleanup could not join'
    for inner in inners:
        # Baseline fetch runs on Qt's _DummyThread; it cannot be joined from
        # Python. The approved runner uses an actual plain threading.Thread.
        if type(inner) is threading.Thread: inner.join(.3)
    app.processEvents()
result['signals'] = signals
result['registry'] = len(getattr(upd, '_LIVE_UPDATE_THREADS', []))
print('RESULT ' + json.dumps(dict(event='worker', **result)), flush=True)
'''


BOUNDED = r'''
import io, json, sys, threading, time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from cove_video_editor import updater as upd
from cove_video_editor import app as appmod
app = QApplication([])
entered, release = threading.Event(), threading.Event()
class Response:
    headers = {'Content-Length': '4'}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1):
        entered.set()
        release.wait(.5)
        return b'data'
upd.urllib.request.urlopen = lambda *a, **k: Response()
thread, worker = upd.start_download('https://invalid/asset', Path(sys.argv[2])/'partial')
thread.start()
assert entered.wait(1)
appmod._CLOSE_JOIN_MS = 200
start = time.monotonic()
appmod._join_or_survive(thread, worker)
park_duration = time.monotonic()-start
start = time.monotonic()
if sys.argv[1] == 'drain':
    appmod.drain_surviving_media_threads(200)
    remaining = list(appmod._SURVIVING_MEDIA_THREADS)
else:
    shutdown = getattr(upd, 'shutdown_update_threads', None)
    remaining = shutdown(200) if shutdown else []
duration = time.monotonic()-start
running = thread.isRunning()
worker.cancel()  # Cleanup must work even before the registry API exists.
release.set()
thread.quit()
assert thread.wait(1000)
appmod.drain_surviving_media_threads(20)
if hasattr(upd, 'shutdown_update_threads'): upd.shutdown_update_threads(20)
print('RESULT '+json.dumps(dict(event='bounded', duration=duration,
      park_duration=park_duration, running=running, remaining=len(remaining),
      partial_exists=(Path(sys.argv[2])/'partial').exists())), flush=True)
'''


def run_child(script, case, mode="x", *, xvfb=False, timeout=9):
    """Retain exact child evidence and never leave an owned process group."""
    with tempfile.TemporaryDirectory(prefix="cove-shutdown-") as home:
        env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=home + "/config",
                   XDG_CACHE_HOME=home + "/cache", XDG_DATA_HOME=home + "/data",
                   QT_QPA_PLATFORM="xcb" if xvfb else "offscreen",
                   PYTHONPATH=str(ROOT / "src"), PYTHONNOUSERSITE="1",
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("APPIMAGE", None)
        env.pop("DISPLAY", None)
        args = [sys.executable, "-c", script, case, home if script == BOUNDED else mode]
        if xvfb:
            args = ["xvfb-run", "-a", "-s", "-screen 0 1280x800x24", *args]
        begin = time.monotonic()
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        # xvfb-run's X server can take a moment to finish after its child.
        # Still require group-empty AND exact rc 0; an abort never passes.
        group_deadline = time.monotonic() + 1.0
        while True:
            try:
                os.killpg(proc.pid, 0)
                lingering = True
            except ProcessLookupError:
                lingering = False
            if not lingering or time.monotonic() >= group_deadline:
                break
            time.sleep(.02)
        evidence = os.environ.get("COVE_SHUTDOWN_EVIDENCE")
        if evidence:
            out = Path(evidence)
            out.mkdir(parents=True, exist_ok=True)
            tag = f"{case}-{mode}-{'xcb' if xvfb else 'offscreen'}-{proc.pid}"
            (out / f"{tag}.stdout").write_text(stdout)
            (out / f"{tag}.stderr").write_text(stderr)
            (out / f"{tag}.json").write_text(json.dumps(dict(
                command=args, exit_code=proc.returncode, timed_out=timed_out,
                lingering=lingering, duration=time.monotonic()-begin), indent=2))
        assert not timed_out, f"owned child timed out: {case}\n{stdout}\n{stderr}"
        assert not lingering, f"owned process group still exists: {proc.pid}"
        assert proc.returncode == 0, f"rc={proc.returncode}: {case}\n{stdout}\n{stderr}"
        for fatal in ("Destroyed", "Fatal Python", "Aborted", "Segmentation fault"):
            assert fatal not in stderr, stderr
        rows = [json.loads(line[7:]) for line in stdout.splitlines() if line.startswith("RESULT ")]
        assert rows, stdout
        assert not any(row["event"] == "watchdog" for row in rows), stdout
        return rows


def returned(rows):
    return next(row for row in rows if row["event"] == "returned")


@pytest.mark.parametrize("mode", ["x", "quit"])
def test_active_check_close(mode):
    row = returned(run_child(GUI, "active", mode))
    assert row["fetches"] == 1
    assert row["prompts"] == []
    assert row["running"] is False
    assert len(row["closes"]) == 1
    close = row["closes"][0]
    assert close["entered"] and close["running"], "positive control: active at closeEvent"
    assert close["after_running"] is False
    assert close["duration"] < 1
    assert close["cancelled"] == ["cancelled"]


@pytest.mark.parametrize("mode", ["x", "quit"])
def test_active_check_xvfb(mode):
    if not shutil.which("xvfb-run"):
        pytest.skip("xvfb-run unavailable")
    row = returned(run_child(GUI, "active", mode, xvfb=True))
    assert row["closes"][0]["running"] and row["closes"][0]["entered"]
    assert not row["closes"][0]["after_running"]
    assert not row["running"] and row["fetches"] == 1


def test_close_before_timer():
    row = returned(run_child(GUI, "early"))
    assert row["fetches"] == 0 and not row["running"]


def test_owned_timer_stopped_on_close():
    row = returned(run_child(GUI, "timer"))
    assert row["timer_fires"] == []
    assert not row["timer_active"] and row["fetches"] == 0


@pytest.mark.parametrize("case,prompts", [("after", []), ("normal", ["99.0.0"]),
                                         ("fail", []), ("exception", [])])
def test_completed_check_cleanup(case, prompts):
    rows = run_child(GUI, case)
    row = returned(rows)
    assert row["prompts"] == prompts and row["fetches"] == 1
    assert not row["running"] and row["effects"] == []
    assert next(r for r in rows if r["event"] == "completed")["stale_worker"] is False


def test_logical_deadline_leaves_only_daemon_fetch():
    rows = run_child(GUI, "deadline")
    deadline = next(r for r in rows if r["event"] == "deadline")
    assert deadline["inner_alive"] and deadline["inner_daemon"]
    assert len(deadline["failed"]) == 1 and "releases API" in deadline["failed"][0]
    row = returned(rows)
    assert row["elapsed"] < 2 and not row["running"] and not row["prompts"]


def test_atexit_joins_unclosed_window():
    rows = run_child(GUI, "unclosed")
    row = returned(rows)
    assert row["closes"] == [] and row["running"] is True
    assert row["fetches"] == 1
    assert next(r for r in rows if r["event"] == "unclosed")["entered"]
    assert next(r for r in rows if r["event"] == "atexit_positive")["running"]


def test_repeated_active_create_close_cycles():
    row = returned(run_child(GUI, "cycles"))
    assert row["windows"] == 3 and row["fetches"] == 3
    assert len(row["closes"]) == 3
    assert all(c["running"] and c["entered"] and not c["after_running"] for c in row["closes"])
    assert row["prompts"] == []


def test_closing_blocks_new_checks_and_prompts():
    row = returned(run_child(GUI, "guards"))
    assert row["fetches"] == 0 and row["prompts"] == [] and row["effects"] == []


@pytest.mark.parametrize("case", ["cancel", "deadline", "late_cancel", "late_deadline", "shutdown"])
def test_worker_bounded_cancel_and_deadline(case):
    # shutdown exercises the new registry API; cancel/deadline RED is through
    # observable joins, even if baseline has no cancel() method.
    row = run_child(WORKER, case)[0]
    assert row["joined"] and row["duration"] < .6
    assert row["before_release"] == row["signals"], "late fetch must never emit a result"
    assert len(row["signals"]) == 1 and row["signals"][0][0] == "failed"
    if "deadline" in case:
        assert "releases API" in row["signals"][0][1]
    else:
        assert row["signals"][0][1] == "cancelled"
    if case == "deadline":
        assert row["inner_alive"] and row["inner_daemon"]
    if case == "shutdown": assert row["remaining"] == 0
    assert row["registry"] == 0


@pytest.mark.parametrize("case,kind,text", [
    ("older", "noUpdate", ""), ("missing", "failed", "release had no tag_name"),
    ("none", "failed", "could not reach the releases API"),
    ("no_assets", "updateAvailable", "99.0.0"),
    ("exception", "failed", "fake exploded"),
])
def test_worker_normal_results_and_exceptions(case, kind, text):
    row = run_child(WORKER, case)[0]
    assert row["joined"] and len(row["signals"]) == 1
    assert row["signals"][0][0] == kind and text in row["signals"][0][1]
    assert row["registry"] == 0


@pytest.mark.parametrize("case", ["download", "download_park"])
def test_close_cancels_download_without_publication(case):
    row = returned(run_child(GUI, case))
    assert row["closes"][0]["entered"] and row["closes"][0]["running"]
    assert row["closes"][0]["progress_visible"]
    assert row["elapsed"] < 2
    assert row["closes"][0]["duration"] < 1
    assert not row["download_exists"] and row["effects"] == []
    assert not row["closes"][0]["after_running"]


def test_download_socket_timeout_boundary_slow():
    """A held 20.5 s read must not hold the QThread or shutdown open."""
    start = time.monotonic()
    rows = run_child(GUI, "download_boundary", timeout=6)
    assert time.monotonic() - start < 6
    close = returned(rows)["closes"][0]
    assert close["entered"] and close["running"] and close["progress_visible"]
    assert not close["after_running"] and close["duration"] < 1
    final = next(row for row in rows if row["event"] == "download_exit")
    assert final["join_ms"] == 22_000  # retained upper bound, not a required wait
    assert not final["download_exists"] and final["cache_files"] == []
    assert final["effects"] == []  # rc 0/no fatal/watchdog/lingering asserted by runner


@pytest.mark.parametrize("case,outcome", [("download_completed_close", "finished"),
                                         ("download_failed_close", "failed")])
def test_close_after_download_terminal_with_visible_progress(case, outcome):
    row = returned(run_child(GUI, case))  # exact rc 0, no watchdog/lingering
    assert row["elapsed"] < 2 and row["download_outcome"] == outcome
    assert len(row["closes"]) == 1 and row["closes"][0]["progress_visible"]
    assert not row["closes"][0]["running"] and not row["closes"][0]["after_running"]
    assert row["effects"] == []
    assert row["download_exists"] is (outcome == "finished")


def test_download_normal_completion_still_verified_and_installed():
    row = returned(run_child(GUI, "download_normal"))
    assert row["effects"] == ["swap", "relaunch"]
    assert row["download_exists"]
    assert bytes.fromhex(row["download_bytes"]) == b"complete verified AppImage"


@pytest.mark.parametrize("case", ["late_downloaded", "late_failed"])
def test_closing_blocks_download_callbacks(case):
    row = returned(run_child(GUI, case))
    assert row["effects"] == []


@pytest.mark.parametrize("case", ["drain", "registry"])
def test_stalled_download_shutdown_budget(case):
    # Cancel must finish the supervisor independently of the held body read.
    row = run_child(BOUNDED, case)[0]
    assert row["park_duration"] < .5 and row["duration"] < .5
    assert not row["running"] and row["remaining"] == 0
    assert not row["partial_exists"]


@pytest.mark.parametrize("case,signal", [("destroy_check", "available"),
    ("download_destroy_done", "finished"), ("download_destroy_fail", "failed")])
def test_destroyed_window_discards_real_queued_update_results(case, signal):
    rows = run_child(GUI, case)
    before = next(row for row in rows if row["event"] == "destroyed_active")
    assert before["invalid"] and before["owned"]
    row = returned(rows)
    assert row["destroyed"] == ["window"] and row["invalid"] and row["owners"] == 0
    assert row["terminal"] == [signal] and row["registry"] == 0 and row["closes"] == []
    assert row["prompts"] == [] and row["effects"] == []
    assert row["download_exists"] is (signal == "finished")


def test_repeated_close_event_is_idempotent():
    row = returned(run_child(GUI, "double_close"))
    assert row["fetches"] == 1 and len(row["closes"]) == 2
    assert row["closes"][0]["entered"] and row["closes"][0]["running"]
    assert all(not c["after_running"] and c["duration"] < 1 for c in row["closes"])
    assert not row["closes"][1]["running"] and not row["running"]
    assert row["prompts"] == [] and row["effects"] == []


def test_registry_retains_unstarted_pair_and_running_survivor():
    row = run_child(WORKER, "registry_unstarted")[0]
    assert row["retained"] and row["released"] and row["remaining"] == 1
    assert row["duration"] < .2
