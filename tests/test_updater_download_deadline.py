"""Download lifetime and publication behavior in owned, bounded Qt children.

All network fixtures are local; DNS/connect stalls never attempt a remote
connection. Raw child evidence is retained by the shutdown test runner.
"""
from __future__ import annotations

import functools
import shutil
import socket

import pytest

from test_updater_shutdown import run_child, returned

TIMEOUT_MESSAGES = {
    'connect': 'Timed out waiting for the update server. Check your internet connection, then restart Cove to be offered the update again, or get it from github.com/Sin213/cove-video-editor/releases.',
    'body': 'Timed out: the download was too slow or stalled and was stopped. Check your internet connection, then restart Cove to be offered the update again, or get it from github.com/Sin213/cove-video-editor/releases.',
    'verify': 'Timed out while verifying the download, so it was discarded. Restart Cove to be offered the update again, or get it from github.com/Sin213/cove-video-editor/releases.',
}

REAL_NETWORK = r'''
import hashlib, http.server, json, math, os, socket, sys, threading, time
from pathlib import Path
from PySide6.QtCore import QObject, QCoreApplication, QThread, Qt, QTimer
from cove_video_editor import updater as upd
case, mode = sys.argv[1:3]
entered, release = threading.Event(), threading.Event()
requests, attempts, sending = [], [], []
payload = b'A' * 600000
digest = hashlib.sha256(payload).hexdigest()
base_case = case.removeprefix('deadline_').removeprefix('cancel_')
if base_case in ('body_budget', 'unknown_budget'): base_case = 'body'
if base_case == 'dialog_cancel': base_case = 'body'
if base_case == 'gui_failure': base_case = 'http_error'
def emit(event, **kw):
    print('RESULT ' + json.dumps(dict(event=event, **kw)), flush=True)
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *a): pass
    def do_GET(self):
        requests.append(self.path)
        sidecar = self.path.endswith('.sha256')
        body = (digest + '  asset\n').encode() if sidecar else payload
        if base_case == 'bad_hash' and sidecar: body = b'0' * 64
        if base_case == 'malformed' and sidecar: body = b'not a sha256'
        if (base_case == 'http_error' and not sidecar) or (base_case == 'missing' and sidecar):
            self.send_error(404); return
        if base_case == 'headers' and not sidecar:
            entered.set(); release.wait(4)
        trickle = ((base_case == 'body' and not sidecar) or
                   (base_case == 'sidecar' and sidecar))
        self.send_response(200)
        self.send_header('Content-Length', str(4096 if trickle and sidecar else len(body)))
        self.end_headers()
        if trickle:
            entered.set()
            while not release.is_set():
                try: self.wfile.write(b'A'); self.wfile.flush()
                except OSError: return
                sending.append(time.monotonic())
                release.wait(.04)
            return
        try: self.wfile.write(body); self.wfile.flush()
        except OSError: pass
class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
srv = Server(('127.0.0.1', 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{srv.server_port}/asset'
# Disable inherited proxy configuration without changing production URL policy.
upd.urllib.request.install_opener(upd.urllib.request.build_opener(upd.urllib.request.ProxyHandler({})))
real_dns, real_connect = socket.getaddrinfo, socket.socket.connect
if base_case in ('dns', 'multiaddr'):
    url = 'http://fixture.invalid:81/asset'
    def dns(host, port, *a, **k):
        if host != 'fixture.invalid': return real_dns(host, port, *a, **k)
        if base_case == 'dns':
            entered.set(); release.wait(4); raise socket.gaierror('released fixture')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (f'203.0.113.{i}', 81))
                for i in range(1, 5)]
    def connect(self, addr):
        if isinstance(addr, tuple) and str(addr[0]).startswith('203.0.113.'):
            attempts.append(self.gettimeout()); entered.set()
            release.wait(self.gettimeout()); raise TimeoutError('fixture address timeout')
        return real_connect(self, addr)
    socket.getaddrinfo, socket.socket.connect = dns, connect
'''


FAKE_NETWORK = r'''
import hashlib, io, json, math, os, socket, sys, threading, time, urllib.error
from pathlib import Path
from PySide6.QtCore import QObject, QCoreApplication, QThread, Qt, QTimer
from cove_video_editor import updater as upd
case, mode = sys.argv[1:3]
entered, release = threading.Event(), threading.Event()
requests, attempts, sending = [], [], []
payload = b'A' * 600000
digest = hashlib.sha256(payload).hexdigest()
base_case = case.removeprefix('deadline_').removeprefix('cancel_')
if base_case in ('body_budget', 'unknown_budget'): base_case = 'body'
if base_case == 'dialog_cancel': base_case = 'body'
if base_case == 'gui_failure': base_case = 'http_error'
def emit(event, **kw):
    print('RESULT ' + json.dumps(dict(event=event, **kw)), flush=True)
url = 'http://fixture.invalid:81/asset'
class Server:
    def shutdown(self): pass
    def server_close(self): pass
srv = Server()
upd.urllib.request.install_opener(upd.urllib.request.build_opener(upd.urllib.request.ProxyHandler({})))
if base_case == 'dns':
    def stalled_dns(*a, **k):
        entered.set(); release.wait(4); raise socket.gaierror('released fixture')
    socket.getaddrinfo = stalled_dns
elif base_case == 'multiaddr':
    def stalled_connect(addr, timeout, *a, **k):
        for i in range(4):
            attempts.append(timeout); entered.set(); release.wait(timeout)
        raise TimeoutError('fixture addresses timed out')
    socket.create_connection = stalled_connect
else:
    class Response:
        def __init__(self, sidecar):
            self.sidecar = sidecar
            body = (digest + '  asset\n').encode() if sidecar else payload
            if base_case == 'bad_hash' and sidecar: body = b'0' * 64
            if base_case == 'malformed' and sidecar: body = b'not a sha256'
            self.stream = io.BytesIO(body)
            self.headers = {'Content-Length': str(len(body))}
            if base_case == 'unknown' or case == 'deadline_unknown_budget': self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): self.stream.close()
        def read(self, n=-1):
            if base_case == 'trickle' and not self.sidecar and not release.is_set():
                entered.set(); sending.append(time.monotonic()); release.wait(.04)
                return self.stream.read(1)
            stalled = ((base_case == 'body' and not self.sidecar) or
                       (base_case == 'sidecar' and self.sidecar))
            if stalled:
                entered.set()
                # A buffered read held open by periodic bytes, without sockets.
                while not release.is_set():
                    sending.append(time.monotonic()); release.wait(.04)
            return self.stream.read(n)
    def urlopen(req, timeout):
        assert timeout == 20
        requests.append(req.full_url)
        sidecar = req.full_url.endswith('.sha256')
        if (base_case == 'http_error' and not sidecar) or (base_case == 'missing' and sidecar):
            raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, None)
        if base_case == 'headers' and not sidecar:
            entered.set(); release.wait(4)
        return Response(sidecar)
    upd.urllib.request.urlopen = urlopen
'''


GUI = FAKE_NETWORK + r'''
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
from cove_video_editor import __main__ as entry
from cove_video_editor import app as appmod
# Device-only isolation: PulseAudio IPC is denied in the author sandbox.
# Keep real Qt media players, MainWindow, updater and shutdown paths; these
# updater tests play no media, so a volume holder needs no audio device.
class SilentOutput(QObject):
    def setVolume(self, value): self._volume = value
    def volume(self): return getattr(self, '_volume', 0.0)
class SilentPlayer(appmod.QMediaPlayer):
    def setAudioOutput(self, output): self._test_output = output
appmod.QAudioOutput, appmod.QMediaPlayer = SilentOutput, SilentPlayer
effects, windows, closes, close_requests = [], [], [], []
start = time.monotonic()
info = upd.UpdateInfo('99.0.0', 'https://invalid/release', 'cove-99.AppImage', url, len(payload))
upd.swap_in_appimage = lambda p: effects.append('swap') or p
upd.relaunch = lambda p: effects.append('relaunch')
warning_messages, failure_controls = [], []
def warning(parent, title, text):
    effects.append('warning'); warning_messages.append(dict(title=title, text=text))
QMessageBox.warning = warning
appmod.MainWindow._prompt_update = lambda *a: effects.append('prompt')
Base = appmod.MainWindow
late_ready, late_release = threading.Event(), threading.Event()
late_controls = []
dialog_cancel_controls, dialog_cancel_signals, dialog_failed = [], [], []
if case.startswith('late_signal'):
    real_verify = upd.verify_sha256
    def held_verify(path, url):
        real_verify(path, url); assert late_release.wait(1)
    upd.verify_sha256 = held_verify
class Win(Base):
    def __init__(self):
        super().__init__()
        entered.clear()
        windows.append(self)
        self.started = time.monotonic(); self.closing = False
        self.poll = QTimer(self); self.poll.setInterval(10)
        self.poll.timeout.connect(self.tick); self.poll.start()
        QTimer.singleShot(25, self.begin_late if case.startswith('late_signal') else
                          lambda: self._install_update(info))
    def begin_late(self):
        self._install_update(info)
        self._update_progress.show()  # show before this test holds GUI delivery
        worker, th = self._update_download_worker, self._update_download_thread
        def cancel_finished(path):
            worker.cancel(); late_ready.set()
        worker.finished.connect(cancel_finished, Qt.DirectConnection)
        late_release.set()
        # Hold this GUI slot so finished is queued, then cancel before delivery.
        assert late_ready.wait(1) and th.wait(1000)
        assert self._update_progress.isVisible() and not self._closing_down
        self._update_progress.canceled.emit()
        late_controls.append(dict(cancelled=worker._cancelled.is_set(), file_exists=
            (Path(os.environ['HOME']) / '.cache/cove-video-editor/cove-99.AppImage').exists()))
        if case == 'late_signal_cleared': self._on_update_download_thread_done()
    def _on_update_downloaded(self, path):
        super()._on_update_downloaded(path)
        if case.startswith('late_signal'): QTimer.singleShot(0, self.close)
    def _on_update_download_failed(self, message):
        if case == 'gui_failure':
            failure_controls.append(dict(message=message, window_visible=self.isVisible(),
                                         closing=self._closing_down))
        super()._on_update_download_failed(message)
        if case == 'gui_failure': QTimer.singleShot(20, QApplication.instance().quit)
    def tick(self):
        if self.closing: return
        if case.startswith('late_signal') or case == 'gui_failure': return
        assert time.monotonic() - self.started < 5, 'stall fixture never became active'
        th = self._update_download_thread
        if entered.is_set() and th and th.isRunning() and self._update_progress.isVisible():
            self.closing = True
            close_requests.append(dict(entered=True, running=True, visible=True))
            if case == 'dialog_cancel':
                self.cancel_thread = th
                self._update_download_worker.failed.connect(dialog_failed.append, Qt.DirectConnection)
                self._update_progress.canceled.connect(lambda: dialog_cancel_signals.append(1))
                cancel_button = self._update_progress.findChild(QPushButton)
                assert cancel_button is not None
                self._update_progress.cancel()
                # cancel() resets/hides but does not emit canceled in Qt. The
                # actual Cancel button drives that signal and its real wiring.
                cancel_button.click()
                QTimer.singleShot(800, self.observe_dialog_cancel)
                return
            if mode == 'quit': QApplication.instance().quit()
            else: self.close()
            if mode == 'cycles':
                if len(windows) < 3: Win().show()
                else: QApplication.instance().quit()
    def observe_dialog_cancel(self):
        cache = Path(os.environ['HOME']) / '.cache/cove-video-editor'
        dialog_cancel_controls.append(dict(running=self.cancel_thread.isRunning(),
            held=not release.is_set(), entered=entered.is_set(),
            dialog_visible=self._update_progress.isVisible(),
            files=sorted(p.name for p in cache.iterdir()) if cache.exists() else []))
        emit('dialog_cancel', **dialog_cancel_controls[-1])
        # Observe at .8 s before releasing the stall, even on the mutant. Then
        # let its worker unwind and quit normally: RED is an assertion, not a hang.
        release.set()
        QTimer.singleShot(100, QApplication.instance().quit)
    def closeEvent(self, event):
        th = self._update_download_thread
        rec = dict(entered=entered.is_set(), running=bool(th and th.isRunning()),
                   visible=bool(self._update_progress and self._update_progress.isVisible()))
        begin = time.monotonic(); super().closeEvent(event)
        rec.update(duration=time.monotonic()-begin, after_running=bool(th and th.isRunning()))
        closes.append(rec); emit('close', **rec)
entry.MainWindow = Win
if mode == 'cycles':
    # main creates QApplication; set this before the first close via the window.
    original_tick = Win.tick
    def cycle_tick(self):
        QApplication.instance().setQuitOnLastWindowClosed(False); original_tick(self)
    Win.tick = cycle_tick
code = entry.main(['cove-download-test'])
assert not any(t.isRunning() for t, w in upd._LIVE_UPDATE_THREADS)
# quit() ends queued delivery before the finished-prune callback necessarily
# runs. Deliver that housekeeping after proving all real workers stopped.
QCoreApplication.processEvents()
cache = Path(os.environ['HOME']) / '.cache/cove-video-editor'
emit('returned', code=code, closes=closes, effects=effects, windows=len(windows),
     close_requests=close_requests,
     late_controls=late_controls,
     dialog_cancel_controls=dialog_cancel_controls, dialog_failed=dialog_failed,
     dialog_cancel_signals=len(dialog_cancel_signals),
     warning_messages=warning_messages, failure_controls=failure_controls,
     elapsed=time.monotonic()-start, registry=len(upd._LIVE_UPDATE_THREADS),
     files=sorted(p.name for p in cache.iterdir()) if cache.exists() else [])
sys.exit(code)
'''


WORKER = FAKE_NETWORK + r'''
app = QCoreApplication([])
if case == 'seam_before' and mode == 'existing':
    # Preserve an old entry when cancel wins before calling the publish seam.
    # The original inside-seam fixture deliberately still performs replace and
    # exercises the accepted rollback race, which loses the old cache entry.
    case = 'pre_publish'
dest = Path(os.environ['HOME']) / 'download' / 'asset'
dest.parent.mkdir()
old = b'preexisting verified binary'
if mode == 'existing': dest.write_bytes(old)
errors, finished, progress, signal_threads, published, signal_affinity = [], [], [], [], [], []
verify_entered, verify_release = threading.Event(), threading.Event()
verify_threads, net_threads = [], []
real_open = upd.urllib.request.urlopen
def monitored_open(*a, **k):
    net_threads.append(threading.current_thread())
    return real_open(*a, **k)
upd.urllib.request.urlopen = monitored_open
if case.startswith('deadline_'):
    upd.DOWNLOAD_CONNECT_DEADLINE_S = .25
    upd.DOWNLOAD_BODY_MIN_S = .25
    upd.DOWNLOAD_BODY_MAX_S = .4
    upd.DOWNLOAD_VERIFY_DEADLINE_S = .25
if case == 'deadline_body_budget':
    upd.DOWNLOAD_BODY_MIN_S, upd.DOWNLOAD_BODY_MAX_S = .2, 2.0
    upd.DOWNLOAD_MIN_RATE_BPS = 1200000  # 600000 bytes => .5 s, between clamps
if case == 'deadline_unknown_budget': upd.DOWNLOAD_BODY_MAX_S = .35
real_verify = upd.verify_sha256
def verify(path, sidecar):
    verify_threads.append(threading.current_thread())
    if case in ('late_verify', 'late_verify_deadline'):
        verify_entered.set(); verify_release.wait(3)
    real_verify(path, sidecar)
upd.verify_sha256 = verify
if case == 'late_verify_deadline': upd.DOWNLOAD_VERIFY_DEADLINE_S = .2
real_publish = getattr(upd, '_publish_download', lambda tmp, final: os.replace(tmp, final))
def publish(tmp, final):
    published.append(str(tmp))
    if case == 'seam_before': worker.cancel()
    real_publish(tmp, final)
    if case == 'seam_after': worker.cancel()
upd._publish_download = publish
if case == 'pre_publish':
    # Force the verified result to be ready before the supervisor's first
    # progress notification; cancel from that signal at the publication edge.
    real_start = threading.Thread.start
    def ready_start(self):
        real_start(self)
        if self.name == 'cove-update-download': self.join(1)
    threading.Thread.start = ready_start
thread, worker = upd.start_download(url, dest)
def record(kind, value):
    signal_threads.append(threading.current_thread())
    signal_affinity.append(QThread.currentThread() is thread)
    {'failed': errors, 'finished': finished, 'progress': progress}[kind].append(value)
    if case == 'pre_publish' and kind == 'progress' and value == 100: worker.cancel()
for name in ('failed', 'finished', 'progress'):
    getattr(worker, name).connect(lambda v, n=name: record(n, v), Qt.DirectConnection)
start = time.monotonic()
thread.start()
is_stall = base_case in ('dns', 'multiaddr', 'body', 'trickle', 'sidecar', 'headers')
positive, inner, joined = False, None, False
try:
    if is_stall or case in ('late_verify', 'late_verify_deadline'):
        event = verify_entered if case.startswith('late_verify') else entered
        assert event.wait(1.5), 'fixture not entered'
        positive = thread.isRunning()
        assert positive, 'QThread not active at fixture'
        inners = [t for t in threading.enumerate() if t.name == 'cove-update-download']
        if inners:
            inner = inners[0]
            assert inner.daemon and type(inner) is threading.Thread
            assert getattr(inner._target, '__module__', '') == upd.__name__
            def has_qobject(value):
                if isinstance(value, QObject): return True
                if isinstance(value, dict): return any(has_qobject(v) for v in value.values())
                if isinstance(value, (tuple, list)): return any(has_qobject(v) for v in value)
                if hasattr(value, '__dict__'): return has_qobject(vars(value))
                return False
            assert not has_qobject(inner._args), 'daemon retained a QObject'
        if case.startswith('cancel_') or case == 'late_verify': worker.cancel()
    joined = thread.wait(800)
    terminal_elapsed = time.monotonic()-start
    before = dict(errors=list(errors), finished=list(finished), files=sorted(p.name for p in dest.parent.iterdir()),
                  published=list(published), bytes=dest.read_bytes().hex() if dest.exists() else None)
    alive = bool(inner and inner.is_alive())
    attempts_before = list(attempts)
    sent = len(sending)
    if case.startswith('deadline_') and base_case in ('body', 'sidecar'):
        # A live trickle after the QThread stopped proves a logical deadline.
        threading.Event().wait(.1)
        assert len(sending) > sent, 'server stopped instead of logical abandonment'
finally:
    worker.cancel(); release.set(); verify_release.set(); thread.quit()
    assert thread.wait(1500), 'cleanup could not join owned QThread'
    if inner: inner.join(1)
    app.processEvents()
    srv.shutdown(); srv.server_close()
after = dict(errors=list(errors), finished=list(finished), files=sorted(p.name for p in dest.parent.iterdir()),
             published=list(published), bytes=dest.read_bytes().hex() if dest.exists() else None)
emit('worker', joined=joined, elapsed=time.monotonic()-start, positive=positive, alive=alive,
     terminal_elapsed=terminal_elapsed,
     before=before, after=after, progress=progress, requests=requests, attempts=attempts_before,
     signals_on_supervisor=all(signal_affinity),
     network_on_daemon=all(type(t) is threading.Thread and t.daemon and t.name == 'cove-update-download'
                           for t in net_threads),
     verify_on_daemon=all(type(t) is threading.Thread and t.daemon and t.name == 'cove-update-download'
                         for t in verify_threads))
'''


UNIT = r'''
import io, json, math, os, sys, threading, time
from pathlib import Path
from PySide6.QtCore import QCoreApplication, Qt
from cove_video_editor import updater as upd
app = QCoreApplication([])
case = sys.argv[1]
dest = Path(os.environ['HOME']) / 'asset'
errors, finished, calls, opens = [], [], [], []
worker = upd.DownloadWorker('http://fixture.invalid/asset', dest)
worker.failed.connect(errors.append, Qt.DirectConnection)
worker.finished.connect(finished.append, Qt.DirectConnection)
if case == 'pre_cancel':
    upd.urllib.request.urlopen = lambda *a, **k: calls.append(1)
    worker.cancel(); worker.run()
    assert errors == ['cancelled'] and not calls and not dest.exists()
    assert list(dest.parent.iterdir()) == []
elif case == 'timeout_texts':
    texts = [upd.DOWNLOAD_CONNECT_TIMEOUT_MESSAGE, upd.DOWNLOAD_BODY_TIMEOUT_MESSAGE,
             upd.DOWNLOAD_VERIFY_TIMEOUT_MESSAGE]
    assert len(set(texts)) == 3
    for text in texts:
        assert text.isascii() and 'Timed out' in text
        assert 'restart cove' in text.lower()
        assert 'github.com/Sin213/cove-video-editor/releases' in text
        for stale in ('release page', 'try again later', 'connect)', 'body)', 'verify)'):
            assert stale not in text.lower()
elif case == 'budgets':
    expected = {'DOWNLOAD_CONNECT_DEADLINE_S': 45, 'DOWNLOAD_MIN_RATE_BPS': 128*1024,
                'DOWNLOAD_BODY_MIN_S': 120, 'DOWNLOAD_BODY_MAX_S': 3600,
                'DOWNLOAD_VERIFY_DEADLINE_S': 120}
    for name, value in expected.items():
        assert getattr(upd, name, None) == value, name
        assert math.isfinite(getattr(upd, name)) and value > 0
    for size, budget in [(0, 3600), (-1, 3600), (1, 120), (128*1024*200, 200),
                         (128*1024*4000, 3600)]:
        assert upd._download_body_budget(size) == budget, (size, budget)
elif case == 'collision':
    dest.write_bytes(b'original')
    temp = dest.with_name(f'{dest.name}.{os.getpid()}-collision.part')
    temp.symlink_to(dest)
    upd.secrets.token_hex = lambda n: 'collision'
    upd.urllib.request.urlopen = lambda *a, **k: calls.append(1)
    worker.run()
    assert len(errors) == 1 and not finished and not calls
    assert dest.read_bytes() == b'original' and temp.is_symlink()
elif case in ('partial_error', 'exclusive', 'no_late_network', 'registry_running'):
    import hashlib
    entered, release = threading.Event(), threading.Event()
    payload = b'data'
    class Response:
        headers = {'Content-Length': '4'}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n):
            if calls[-1].endswith('.sha256'):
                return hashlib.sha256(payload).hexdigest().encode()
            if case == 'partial_error': raise RuntimeError('partial read exploded')
            if case in ('no_late_network', 'registry_running'): entered.set(); release.wait(2)
            return payload if not getattr(self, 'sent', False) and not setattr(self, 'sent', True) else b''
    def urlopen(req, timeout): calls.append(req.full_url); return Response()
    upd.urllib.request.urlopen = urlopen
    real_open = upd.os.open
    def os_open(path, flags, mode):
        opens.append((str(path), flags, mode)); return real_open(path, flags, mode)
    upd.os.open = os_open
    thread, worker = upd.start_download('http://fixture.invalid/asset', dest)
    worker.failed.connect(errors.append, Qt.DirectConnection)
    worker.finished.connect(finished.append, Qt.DirectConnection)
    dest.write_bytes(b'original')
    thread.start()
    joined = False
    try:
        if case in ('no_late_network', 'registry_running'):
            assert entered.wait(1) and thread.isRunning()
            if case == 'registry_running':
                upd._prune_update_threads()
                assert any(t is thread for t, w in upd._LIVE_UPDATE_THREADS), 'running owner released'
            worker.cancel()
        joined = thread.wait(500)
        before = dest.read_bytes()
    finally:
        worker.cancel(); release.set(); thread.quit(); assert thread.wait(1200)
        for inner in threading.enumerate():
            if inner.name == 'cove-update-download': inner.join(1)
        app.processEvents()
    assert joined, 'supervisor waited for blocked read'
    assert sorted(p.name for p in dest.parent.iterdir()) == ['asset']
    if case == 'exclusive':
        assert len(opens) == 1
        path, flags, mode = opens[0]
        assert Path(path).parent == dest.parent and path.endswith('.part') and path != str(dest)
        assert flags & os.O_EXCL and flags & os.O_CREAT and flags & os.O_WRONLY and mode == 0o666
        assert finished == [str(dest)] and dest.read_bytes() == payload
    else:
        assert before == b'original' and dest.read_bytes() == b'original' and not finished
        assert len(errors) == 1
        assert len(calls) == 1, 'abandoned daemon fetched sidecar'
emit = dict(event='unit', case=case, errors=errors, finished=finished, calls=calls)
print('RESULT ' + json.dumps(emit), flush=True)
'''


REPAIR = r'''
import hashlib, io, json, os, sys, threading, types
from pathlib import Path
from PySide6.QtCore import QCoreApplication, Qt
from cove_video_editor import updater as upd
app = QCoreApplication([])
case = sys.argv[1]
dest = Path(os.environ['HOME']) / 'asset'
calls, reads, errors, finished = [], [], [], []
clock = [1.0]
payload = b'data'
class Output(io.BytesIO):
    def close(self):
        if not self.closed: self.saved = self.getvalue()
        if case == 'abandon_eof': state.abandoned.set()
        super().close()
class Response:
    headers = {'Content-Length': '4'}
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, n):
        reads.append(n)
        if case == 'body_expired': clock[0] = 3.0; return b''
        if len(reads) == 1: return payload
        return b''
def urlopen(req, timeout): calls.append(req.full_url); return Response()
upd.urllib.request.urlopen = urlopen
if case in ('connect_expired', 'body_expired', 'abandon_eof'):
    upd.time = types.SimpleNamespace(monotonic=lambda: clock[0])
    output = Output()
    state = upd._DownloadState('http://fixture.invalid/asset', dest, output,
                               started=0.0, budget=.5 if case == 'connect_expired' else 10)
    upd.DOWNLOAD_BODY_MIN_S = upd.DOWNLOAD_BODY_MAX_S = 1.0
    upd.verify_sha256 = lambda p, url: calls.append(url)
    upd._download_to_temp(state)
    result = dict(error=state.error, written=state.written, bytes=output.saved.hex(), calls=calls, reads=reads)
else:
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    read_error, close_release = threading.Event(), threading.Event()
    states = []
    original_state = upd._DownloadState
    def state_factory(*a, **k):
        state = original_state(*a, **k); states.append(state)
        if case in ('done_expired', 'error_expired'): state.started, state.budget = 0.0, .5
        if case in ('cancel_wake', 'error_wake'):
            event = getattr(state, 'wake', state.completed)
            original_wait = event.wait
            # Delay a scheduling slice deterministically: cancellation must
            # wake the event actually awaited rather than depend on polling.
            def held_wait(timeout): waiting.set(); return original_wait(2)
            event.wait = held_wait
            if case == 'error_wake':
                output = state.output
                class HeldClose:
                    def write(self, data): return output.write(data)
                    def close(self): close_release.wait(2); output.close()
                state.output = HeldClose()
        return state
    upd._DownloadState = state_factory
    if case in ('done_expired', 'error_expired'):
        upd.time = types.SimpleNamespace(monotonic=lambda: 2.0)
        def ready(state):
            state.output.write(payload); state.output.close()
            if case == 'done_expired':
                # Verify real bytes with the unchanged real sha256 verifier.
                class Sidecar(Response):
                    def read(self, n): return hashlib.sha256(payload).hexdigest().encode()
                upd.urllib.request.urlopen = lambda *a, **k: Sidecar()
                upd.verify_sha256(state.temp, state.url + '.sha256'); state.done = True
            else: state.error = 'controlled terminal failure'
            state.completed.set()
            if hasattr(state, 'wake'): state.wake.set()
        upd._download_to_temp = ready
        real_start = threading.Thread.start
        def ready_start(self):
            real_start(self)
            if self.name == 'cove-update-download': self.join(1)
        threading.Thread.start = ready_start
    elif case in ('cancel_wake', 'error_wake'):
        class Held(Response):
            def read(self, n):
                entered.set()
                if case == 'error_wake':
                    read_error.wait(2); raise RuntimeError('controlled read error')
                release.wait(2); return b''
        upd.urllib.request.urlopen = lambda *a, **k: Held()
    elif case == 'late_signal':
        class Sidecar(Response):
            def read(self, n): return hashlib.sha256(payload).hexdigest().encode()
        upd.urllib.request.urlopen = lambda req, **k: Sidecar() if req.full_url.endswith('.sha256') else Response()
    thread, worker = upd.start_download('http://fixture.invalid/asset', dest)
    worker.failed.connect(errors.append, Qt.DirectConnection)
    if case == 'late_signal': worker.finished.connect(lambda _: worker.cancel(), Qt.DirectConnection)
    worker.finished.connect(finished.append, Qt.DirectConnection)
    thread.start()
    joined = False
    try:
        if case in ('cancel_wake', 'error_wake'):
            assert entered.wait(1) and waiting.wait(1) and thread.isRunning()
            if case == 'cancel_wake': worker.cancel()
            else: read_error.set()
        joined = thread.wait(150 if case in ('cancel_wake', 'error_wake') else 600)
    finally:
        worker.cancel(); release.set()
        read_error.set(); close_release.set()
        for state in states:
            state.completed.set()
            if hasattr(state, 'wake'): state.wake.set()
        thread.quit(); assert thread.wait(1500)
        for inner in threading.enumerate():
            if inner.name == 'cove-update-download': inner.join(1)
        app.processEvents()
    result = dict(joined=joined, errors=errors, finished=finished,
                  cancelled=getattr(worker, 'is_cancelled', lambda: False)(),
                  bytes=dest.read_bytes().hex() if dest.exists() else None)
print('RESULT ' + json.dumps(dict(event='repair', **result)), flush=True)
'''


def assert_abandoned(row, message, *, existing=False):
    assert row['joined'], 'supervisor did not finish within 800 ms'
    assert row['positive'] and row['alive'], 'blocked plain daemon must outlive QThread'
    assert row['before'] == row['after'], 'late daemon changed publication or signals'
    state = row['after']
    assert len(state['errors']) == 1 and message.lower() in state['errors'][0].lower()
    assert state['finished'] == [] and state['published'] == []
    assert state['files'] == (['asset'] if existing else [])
    expected = b'preexisting verified binary'.hex() if existing else None
    assert state['bytes'] == expected
    assert row['network_on_daemon'] and row['signals_on_supervisor']


@pytest.mark.parametrize('case', ['body', 'trickle', 'dns', 'multiaddr', 'sidecar'])
@pytest.mark.parametrize('mode', ['x', 'quit'])
def test_active_download_gui_close(case, mode):
    row = returned(run_child(GUI, case, mode, timeout=8))
    assert row['code'] == 0 and row['elapsed'] < 3
    assert row['effects'] == [] and row['files'] == [] and row['registry'] == 0
    assert len(row['closes']) == 1
    close = row['closes'][0]
    assert close['entered'] and close['running']
    # QApplication.quit() may hide modal dialogs before closing their parent.
    # The real dialog must be visible at the quit request in that ordering.
    assert close['visible'] or (mode == 'quit' and row['close_requests'][0]['visible'])
    assert close['duration'] < 1 and not close['after_running']


@pytest.mark.parametrize('case', ['body', 'dns', 'multiaddr'])
@pytest.mark.parametrize('mode', ['x', 'quit'])
def test_active_download_gui_xvfb(case, mode):
    if not shutil.which('xvfb-run'): pytest.skip('xvfb-run unavailable')
    row = returned(run_child(GUI, case, mode, xvfb=True, timeout=8))
    assert row['effects'] == [] and row['files'] == [] and row['registry'] == 0
    assert row['elapsed'] < 3 and len(row['closes']) == 1
    close = row['closes'][0]
    assert close['entered'] and close['running']
    assert close['visible'] or (mode == 'quit' and row['close_requests'][0]['visible'])
    assert close['duration'] < 1 and not close['after_running']


@pytest.mark.parametrize('case,phase', [('dns', 'connect'), ('multiaddr', 'connect'),
                                     ('headers', 'connect'), ('body', 'body'), ('sidecar', 'verify')])
def test_phase_logical_deadline(case, phase):
    row = run_child(WORKER, 'deadline_' + case, timeout=7)[0]
    assert_abandoned(row, TIMEOUT_MESSAGES[phase])
    if case == 'multiaddr': assert row['attempts'] == [20.0]


def test_trickle_body_has_total_deadline():
    row = run_child(WORKER, 'deadline_trickle', timeout=7)[0]
    assert row['joined'] and row['positive']
    assert row['before'] == row['after']
    assert row['after']['errors'] == [TIMEOUT_MESSAGES['body']]
    assert not row['after']['finished'] and row['after']['files'] == []
    assert row['signals_on_supervisor'] and row['network_on_daemon']


@pytest.mark.parametrize('case,low,high', [('body_budget', .45, 1.0), ('unknown_budget', .3, .75)])
def test_content_length_deadline_budget(case, low, high):
    row = run_child(WORKER, 'deadline_' + case, timeout=7)[0]
    assert_abandoned(row, TIMEOUT_MESSAGES['body'])
    assert low <= row['terminal_elapsed'] <= high
    if case == 'unknown_budget': assert row['progress'] == []


@pytest.mark.parametrize('case,phase', [('connect_expired', 'connect'), ('body_expired', 'body')])
def test_overdue_phase_cannot_transition(case, phase):
    row = run_child(REPAIR, case, timeout=4)[0]
    assert row['error'] == TIMEOUT_MESSAGES[phase]
    assert row['written'] == 0 and row['bytes'] == ''
    assert len(row['calls']) == 1 and not any(c.endswith('.sha256') for c in row['calls'])
    if phase == 'connect': assert row['reads'] == []


@pytest.mark.parametrize('case', ['done_expired', 'error_expired'])
def test_terminal_state_precedes_deadline(case):
    row = run_child(REPAIR, case, timeout=4)[0]
    assert row['joined']
    if case == 'done_expired':
        assert row['errors'] == [] and len(row['finished']) == 1 and row['bytes'] == b'data'.hex()
    else:
        assert row['errors'] == ['controlled terminal failure'] and row['finished'] == []


def test_abandon_at_eof_never_fetches_sidecar():
    row = run_child(REPAIR, 'abandon_eof', timeout=4)[0]
    assert row['bytes'] == b'data'.hex() and len(row['reads']) == 2
    assert len(row['calls']) == 1 and not any(c.endswith('.sha256') for c in row['calls'])


def test_cancel_wakes_supervisor():
    row = run_child(REPAIR, 'cancel_wake', timeout=4)[0]
    assert row['joined'] and row['errors'] == ['cancelled'] and row['finished'] == []


def test_terminal_error_wakes_supervisor_before_file_cleanup():
    row = run_child(REPAIR, 'error_wake', timeout=4)[0]
    assert row['joined'] and row['errors'] == ['controlled read error'] and row['finished'] == []


def test_worker_late_signal_keeps_cancel_state():
    row = run_child(REPAIR, 'late_signal', timeout=4)[0]
    assert row['joined'] and len(row['finished']) == 1 and row['cancelled']


@pytest.mark.parametrize('case', ['late_signal', 'late_signal_cleared'])
def test_gui_cancel_before_queued_completion_prevents_install(case):
    row = returned(run_child(GUI, case, timeout=6))
    assert row['late_controls'] == [dict(cancelled=True, file_exists=True)]
    assert row['effects'] == [] and row['files'] == [] and row['registry'] == 0


def test_real_dialog_cancel_stops_stalled_download():
    row = returned(run_child(GUI, 'dialog_cancel', timeout=6))
    assert row['close_requests'] == [dict(entered=True, running=True, visible=True)]
    assert row['dialog_cancel_controls'] == [dict(running=False, held=True, entered=True,
                                                 dialog_visible=False, files=[])]
    assert row['dialog_failed'] == ['cancelled'] and row['effects'] == []
    assert row['warning_messages'] == []
    assert row['dialog_cancel_signals'] >= 1 and row['files'] == [] and row['registry'] == 0


def test_non_cancel_download_failure_warns():
    row = returned(run_child(GUI, 'gui_failure', timeout=6))
    error = 'HTTP Error 404: Not Found'
    assert row['failure_controls'] == [dict(message=error, window_visible=True, closing=False)]
    assert row['effects'] == ['warning'] and row['warning_messages'] == [dict(
        title='Update failed', text="The download didn't complete:\n" + error)]
    assert row['files'] == [] and row['registry'] == 0


@pytest.mark.parametrize('case', ['body', 'dns', 'multiaddr', 'sidecar'])
def test_cancel_stalled_daemon(case):
    row = run_child(WORKER, 'cancel_' + case, timeout=7)[0]
    assert_abandoned(row, 'cancelled')


@pytest.mark.parametrize('case', ['late_verify', 'late_verify_deadline'])
def test_late_verification_cannot_publish(case):
    row = run_child(WORKER, case, timeout=7)[0]
    assert_abandoned(row, TIMEOUT_MESSAGES['verify'] if 'deadline' in case else 'cancelled')
    assert row['verify_on_daemon']


def test_real_download_success():
    row = run_child(WORKER, 'success', timeout=7)[0]
    assert row['joined'] and row['before'] == row['after']
    assert row['after']['errors'] == [] and len(row['after']['finished']) == 1
    assert row['after']['files'] == ['asset']
    assert bytes.fromhex(row['after']['bytes']) == b'A' * 600000
    assert row['progress'][-1] == 100
    assert all(0 <= p <= 100 for p in row['progress'])
    assert len(row['progress']) == len(set(row['progress']))
    assert len(row['requests']) == 2 and row['requests'][-1].endswith('.sha256')
    assert row['network_on_daemon'] and row['verify_on_daemon'] and row['signals_on_supervisor']


def test_unknown_length_has_no_progress():
    row = run_child(WORKER, 'unknown', timeout=7)[0]
    assert row['joined'] and row['progress'] == [] and row['after']['errors'] == []
    assert len(row['after']['finished']) == 1 and row['after']['files'] == ['asset']
    assert bytes.fromhex(row['after']['bytes']) == b'A' * 600000


@pytest.mark.parametrize('case,fragment', [('bad_hash', 'sha256 mismatch'),
    ('malformed', 'unrecognized sidecar'), ('missing', 'could not fetch sidecar'),
    ('http_error', 'HTTP Error 404')])
def test_verification_and_http_errors_preserve_destination(case, fragment):
    row = run_child(WORKER, case, 'existing', timeout=7)[0]
    state = row['after']
    assert row['joined'] and not state['finished'] and not state['published']
    assert len(state['errors']) == 1 and fragment in state['errors'][0]
    assert state['bytes'] == b'preexisting verified binary'.hex() and state['files'] == ['asset']


@pytest.mark.parametrize('case,mode', [
    pytest.param('pre_publish', 'x', id='pre_publish'),
    pytest.param('seam_before', 'x', id='seam_before'),
    pytest.param('seam_after', 'x', id='seam_after'),
    pytest.param('seam_before', 'existing', id='existing-seam_before'),
    pytest.param('seam_after', 'existing', id='existing-seam_after'),
])
def test_publication_cancel_boundaries(case, mode):
    row = run_child(WORKER, case, mode, timeout=7)[0]
    state = row['after']
    assert row['joined'] and state['errors'] == ['cancelled'] and state['finished'] == []
    early = case == 'pre_publish' or (mode == 'existing' and case == 'seam_before')
    if early and mode == 'existing':
        assert state['files'] == ['asset'] and state['bytes'] == b'preexisting verified binary'.hex()
    else:
        # Accepted post-replace cancellation removes our new bytes and the old
        # cache entry they replaced; it does not restore that stale entry.
        assert state['files'] == [] and state['bytes'] is None
    assert len(state['published']) == (0 if early else 1)


@pytest.mark.parametrize('case', ['cancel_body', 'deadline_body', 'cancel_sidecar'])
def test_abandon_preserves_existing_destination(case):
    assert_abandoned(run_child(WORKER, case, 'existing', timeout=7)[0],
                     'timed out' if 'deadline' in case else 'cancelled', existing=True)


def test_verified_download_replaces_existing_destination():
    row = run_child(WORKER, 'success', 'existing', timeout=7)[0]
    assert row['joined'] and row['after']['errors'] == [] and len(row['after']['finished']) == 1
    assert row['after']['files'] == ['asset']
    assert bytes.fromhex(row['after']['bytes']) == b'A' * 600000


@pytest.mark.parametrize('case', ['pre_cancel', 'budgets', 'partial_error', 'exclusive', 'no_late_network',
                                 'registry_running', 'collision', 'timeout_texts'])
def test_download_unit_contract(case):
    run_child(UNIT, case, timeout=5)


def test_repeated_download_shutdown_matrix():
    for case, mode in [('body', 'x'), ('dns', 'quit'), ('sidecar', 'x'),
                       ('multiaddr', 'quit'), ('body', 'quit')]:
        row = returned(run_child(GUI, case, mode, timeout=8))
        assert row['files'] == [] and row['registry'] == 0 and row['effects'] == []
        assert row['closes'][0]['running'] and not row['closes'][0]['after_running']


def test_three_window_download_close_cycle():
    row = returned(run_child(GUI, 'body', 'cycles', timeout=10))
    assert row['windows'] == 3 and len(row['closes']) == 3
    assert all(c['entered'] and c['running'] and c['visible'] and c['duration'] < 1
               and not c['after_running'] for c in row['closes'])
    assert row['effects'] == [] and row['files'] == [] and row['registry'] == 0


@functools.lru_cache(maxsize=1)
def loopback_permitted():
    """Tier B skips only EPERM; every other fixture error is a real failure."""
    try:
        probe = socket.socket()
    except PermissionError:
        return False
    else:
        probe.close()
        return True


def require_loopback_permission():
    if not loopback_permitted():
        pytest.skip('Tier B lead-run: sandbox denies socket creation (PermissionError)')


@pytest.mark.parametrize('case', ['deadline_body', 'deadline_sidecar', 'success'])
def test_tier_b_real_http(case):
    require_loopback_permission()
    script = WORKER.replace(FAKE_NETWORK, REAL_NETWORK, 1)
    row = run_child(script, case, timeout=7)[0]
    if case.startswith('deadline_'):
        assert_abandoned(row, TIMEOUT_MESSAGES['body' if case.endswith('body') else 'verify'])
    else:
        assert row['joined'] and row['after']['errors'] == []
        assert len(row['after']['finished']) == 1 and row['after']['files'] == ['asset']
        assert bytes.fromhex(row['after']['bytes']) == b'A' * 600000
        assert row['progress'][-1] == 100 and len(row['requests']) == 2


@pytest.mark.parametrize('case', ['body', 'sidecar'])
@pytest.mark.parametrize('mode', ['x', 'quit'])
def test_tier_b_real_http_close(case, mode):
    require_loopback_permission()
    row = returned(run_child(GUI.replace(FAKE_NETWORK, REAL_NETWORK, 1), case, mode, timeout=8))
    assert row['effects'] == [] and row['files'] == [] and row['registry'] == 0
    close = row['closes'][0]
    assert close['entered'] and close['running']
    assert close['visible'] or (mode == 'quit' and row['close_requests'][0]['visible'])
    assert close['duration'] < 1 and not close['after_running']
