from __future__ import annotations

import array
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QImage

from . import ffmpeg_utils as ff

#: How often a worker looks up from the child it is waiting on. It is the
#: latency between a cancel and the child being reaped, and it is a
#: constant so tests can shrink it instead of waiting one out.
_REAP_POLL_S = 0.25

#: How long a terminated child gets to exit before it is killed. ffmpeg
#: normally goes on the first signal; this bounds the case where it does
#: not, because an unbounded wait is the running-QThread abort again.
_TERMINATE_GRACE_S = 2.0


class _Cancelled(Exception):
    """Raised inside a worker when its run was cancelled before spawning."""


class _MediaChild:
    """Single-child ffmpeg ownership for a media-analysis worker.

    A worker sitting inside ffmpeg cannot honour ``quit()``, so cancelling
    it has to reach the *child*. That means the worker must hold the
    ``Popen`` - and holding it is only half the job, because ``Popen``
    spawns the process before it returns: there is a window where a real
    child exists and the attribute is still ``None``. Reading "no process
    object" as "no process" there silently drops the cancellation, which
    is the failure ``ExportWorker`` had to be repaired for. The lock makes
    all four states a cancel can observe explicit: not started, starting,
    live, already terminal.

    ``cancel()`` only *signals*; the waiting worker thread does the
    reaping. Cancels arrive on the GUI thread during ``closeEvent``, and
    waiting for a stuck ffmpeg there would freeze the window for exactly
    as long as the shutdown is trying to escape.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._starting = False
        self._cancel_pending = False
        self.cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            proc = self._proc
            if proc is None:
                # Either nothing has been launched - in which case `run`
                # will refuse to launch anything - or a spawn is in
                # flight and the child cannot be polled yet. Defer to
                # publication rather than guess which.
                self._cancel_pending = self._starting
                return
        self._signal(proc)

    @staticmethod
    def _signal(proc: subprocess.Popen) -> None:
        """Ask a child to stop, but only if it is still running.

        A child that already reached a terminal status keeps it: signalling
        a reaped process is at best pointless and at worst aimed at a pid
        the OS has since reused.
        """
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            pass

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """``subprocess.run(cmd, capture_output=True)``, but cancellable.

        Raises ``_Cancelled`` if the run was cancelled before the child
        could be launched - there is nothing to spawn and nothing to
        report.
        """
        with self._lock:
            if self.cancelled:
                raise _Cancelled
            self._starting = True
        # Outside the lock on purpose: `Popen` can block, and a cancel
        # must never wait on it. `_starting` is what keeps the window
        # honest while we are in here.
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                **ff._SUBPROCESS_KWARGS,  # type: ignore[attr-defined]
            )
        except BaseException:
            with self._lock:
                self._starting = False
                self._cancel_pending = False
            raise
        with self._lock:
            self._proc = proc
            self._starting = False
            pending = self._cancel_pending
            self._cancel_pending = False
        if pending:
            self._signal(proc)
        try:
            out, err = self._reap(proc)
        finally:
            with self._lock:
                self._proc = None
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def _reap(self, proc: subprocess.Popen) -> tuple[bytes, bytes]:
        """Collect the child's output and its exit status.

        Polling rather than one open-ended wait is what makes the escalation
        possible: a child that ignores ``terminate`` would otherwise keep
        this thread - and the ``QThread`` under it - alive indefinitely.
        ``kill`` cannot be ignored, so the wait after it is unbounded.
        """
        deadline: float | None = None
        while True:
            try:
                return proc.communicate(timeout=_REAP_POLL_S)
            except subprocess.TimeoutExpired:
                if not self.cancelled:
                    continue
                now = time.monotonic()
                if deadline is None:
                    deadline = now + _TERMINATE_GRACE_S
                elif now >= deadline:
                    proc.kill()
                    return proc.communicate()


class ThumbnailWorker(QObject):
    finished = Signal(str, list)   # clip id, list[QImage]
    failed = Signal(str, str)
    #: Emitted instead of `finished` / `failed` when the run was cancelled.
    #: A cancelled worker publishes no result, so without this the thread's
    #: event loop would keep spinning after `run` returned, waiting for a
    #: `quit()` only the owner can send.
    cancelled = Signal(str)

    def __init__(self, clip_id: str, video: Path, duration: float, count: int = 24, height: int = 80) -> None:
        super().__init__()
        self._id = clip_id
        self._video = video
        self._duration = duration
        self._count = max(1, count)
        self._height = height
        self._child = _MediaChild()

    def cancel(self) -> None:
        self._child.cancel()

    def _command(self, t: float, out: Path) -> list[str]:
        # Same command `ff.extract_thumbnail` builds; spawned here instead
        # so this worker owns the child and a cancel can reach it. A frame
        # from a slow or remote source can block for a long time, and
        # checking the cancel flag only between frames is what used to
        # leave the QThread running past window teardown.
        return [
            ff.require_ffmpeg(),
            "-y",
            "-ss", f"{t:.3f}",
            "-i", str(self._video),
            "-frames:v", "1",
            "-vf", f"scale=-2:{self._height}",
            "-q:v", "5",
            str(out),
        ]

    def _render(self, tmp_path: Path) -> list[QImage] | None:
        """Extract every frame, or return ``None`` if cancelled part-way."""
        images: list[QImage] = []
        step = self._duration / self._count
        for i in range(self._count):
            if self._child.cancelled:
                return None
            t = min(self._duration - 0.05, max(0.0, step * (i + 0.5)))
            out = tmp_path / f"t_{i:03d}.jpg"
            try:
                proc = self._child.run(self._command(t, out))
            except _Cancelled:
                return None
            except Exception:  # noqa: BLE001
                continue
            # A cancelled frame comes back as a killed child, not as a
            # missing one: check before reading its output so a
            # half-written JPEG never reaches the strip.
            if self._child.cancelled:
                return None
            if proc.returncode != 0:
                continue
            img = QImage(str(out))
            if not img.isNull():
                images.append(img.copy())
        return images

    def run(self) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="cove-ve-thumbs-") as tmp:
                images = self._render(Path(tmp))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self._id, str(exc))
            return
        if images is None:
            self.cancelled.emit(self._id)
            return
        self.finished.emit(self._id, images)


class WaveformWorker(QObject):
    """Decodes the audio to mono float32 at PEAK_RATE Hz, then emits a
    normalized absolute-amplitude envelope. The timeline renders it as a
    filled polygon, so it stays crisp at any zoom."""

    finished = Signal(str, list, int)   # clip id, peaks (list[float] in 0..1), rate
    failed = Signal(str, str)
    cancelled = Signal(str)   # see `ThumbnailWorker.cancelled`

    PEAK_RATE = 400

    def __init__(self, clip_id: str, path: Path) -> None:
        super().__init__()
        self._id = clip_id
        self._path = path
        self._child = _MediaChild()

    def cancel(self) -> None:
        self._child.cancel()

    def run(self) -> None:
        try:
            cmd = [
                ff.require_ffmpeg(),
                "-hide_banner", "-loglevel", "error",
                "-i", str(self._path),
                "-vn", "-ac", "1",
                "-ar", str(self.PEAK_RATE),
                "-f", "f32le",
                "-",
            ]
            proc = self._child.run(cmd)
            if self._child.cancelled:
                self.cancelled.emit(self._id)
                return
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(
                    proc.returncode, cmd, proc.stdout, proc.stderr)
            samples = array.array("f")
            samples.frombytes(proc.stdout)
            if not samples:
                raise RuntimeError("audio stream produced no samples")
            peaks = [abs(s) for s in samples]
            peak_max = max(peaks)
            if peak_max > 1e-4:
                peaks = [p / peak_max for p in peaks]
            self.finished.emit(self._id, peaks, self.PEAK_RATE)
        except _Cancelled:
            # Cancelled before ffmpeg was even launched. Nothing ran, so
            # there is no failure to put in front of the user.
            self.cancelled.emit(self._id)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self._id, str(exc))


def start_thumbnails(clip_id: str, video: Path, duration: float, count: int = 24) -> tuple[QThread, ThumbnailWorker]:
    # Callers must keep Python refs to both returned objects until the thread
    # finishes; we deliberately avoid deleteLater here because double-deletion
    # via Python GC + C++ deleteLater triggers a Qt fatal in PySide6.
    thread = QThread()
    worker = ThumbnailWorker(clip_id, video, duration, count=count)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.cancelled.connect(thread.quit)
    return thread, worker


def start_waveform(clip_id: str, video: Path) -> tuple[QThread, WaveformWorker]:
    thread = QThread()
    worker = WaveformWorker(clip_id, video)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.cancelled.connect(thread.quit)
    return thread, worker
