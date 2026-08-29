"""Tab 2N-R: media-analysis workers must let go of their ffmpeg child.

``ThumbnailWorker`` and ``WaveformWorker`` each block inside an ffmpeg
subprocess. Before this slice ``cancel()`` only set a flag that was read
*between* children, so a worker sitting in a long decode kept its
``QThread`` running until ffmpeg finished on its own - long past the
window that owned it. The survivor machinery in ``app`` kept that thread
referenced (dropping a running ``QThread`` aborts the process), but the
interpreter still tore down with a live thread, which is the
``QThread: Destroyed while thread is still running`` abort.

So the contract asserted here is end to end: ``cancel()`` stops the
*child*, the child is reaped, the worker returns, the thread emits
``finished`` and the survivor entry releases.

Every process test drives a deterministic fake child rather than real
ffmpeg: the behaviour under test is the ownership handshake around
``Popen``, and the timing windows (a cancel arriving mid-spawn, a child
that ignores ``terminate``) cannot be produced on demand with a real one.
The two bounds - the reap poll and the terminate grace - are module
constants precisely so they can be shrunk here instead of sleeping
production-length intervals.
"""
from __future__ import annotations

import array
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import thumbnails as th  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402

_app: QApplication | None = None

#: Every wait in this file is bounded by this. A test that needs longer
#: has stopped testing cancellation and started testing patience.
LIMIT = 5.0


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class FakeChild:
    """A ``subprocess.Popen`` stand-in whose lifetime the test owns.

    It models a *successful* child by default - a real one is already
    running by the time ``Popen`` returns, so ``poll()`` returning ``None``
    is the truthful starting state, and ``communicate()`` yields the
    captured output a caller would really get.
    """

    def __init__(self, cmd, *, exits_on_terminate: bool = True,
                 out_data: bytes = b"", err_data: bytes = b"",
                 returncode: int | None = None, **_kw) -> None:
        # `**_kw` absorbs the real `Popen` keywords production passes -
        # `stdout=PIPE` and friends - which is also why the payloads here
        # are not named after the streams they stand in for.
        self.args = list(cmd)
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self.reaped = False
        self._stdout = out_data
        self._stderr = err_data
        self._exits_on_terminate = exits_on_terminate
        self._exited = threading.Event()
        if returncode is not None:
            self._exited.set()

    # -- Popen surface --------------------------------------------------

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._exits_on_terminate:
            self._exit(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        self._exit(-9)

    def communicate(self, timeout=None):
        if not self._exited.wait(LIMIT if timeout is None else timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.reaped = True
        return self._stdout, self._stderr

    # -- test surface ---------------------------------------------------

    def _exit(self, rc: int) -> None:
        if self.returncode is None:
            self.returncode = rc
        self._exited.set()

    def finish(self, rc: int = 0) -> None:
        """Let the child exit on its own, as a completed encode would."""
        self._exit(rc)

    @property
    def live(self) -> bool:
        return self.returncode is None


def _succeeding_child(cmd, **kw) -> FakeChild:
    """A child that has already exited 0, writing whatever it was told to.

    The thumbnail command names its output file as the last argument, so
    honouring that here is what makes the success test cover the real
    command shape rather than a rewritten one.
    """
    out = Path(cmd[-1])
    if out.suffix == ".jpg":
        img = QImage(6, 4, QImage.Format_RGB32)
        img.fill(0xFF203040)
        img.save(str(out), "JPG")
    return FakeChild(cmd, returncode=0, **kw)


class _ProcCase(unittest.TestCase):
    """Patches the process seam and shrinks the two production bounds."""

    def setUp(self) -> None:
        self.spawned: list[FakeChild] = []
        p = unittest.mock.patch.object(
            th.ff, "require_ffmpeg", return_value="ffmpeg")
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("_REAP_POLL_S", 0.01),
                            ("_TERMINATE_GRACE_S", 0.05)):
            b = unittest.mock.patch.object(th, name, value)
            b.start()
            self.addCleanup(b.stop)

    def popen(self, factory=FakeChild):
        """Install ``factory`` as ``Popen`` and record what it hands out."""
        def _spawn(cmd, **kw):
            child = factory(cmd, **kw)
            self.spawned.append(child)
            return child

        p = unittest.mock.patch.object(th.subprocess, "Popen", _spawn)
        self.mock_popen = p.start()
        self.addCleanup(p.stop)
        return _spawn

    def run_off_thread(self, worker) -> threading.Thread:
        """Run ``worker.run()`` on a plain thread and join it in cleanup."""
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        self.addCleanup(lambda: t.join(LIMIT))
        return t

    def await_child(self) -> FakeChild:
        deadline = time.monotonic() + LIMIT
        while not self.spawned and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.spawned, "worker never spawned a child")
        return self.spawned[0]

    def wave(self) -> th.WaveformWorker:
        return th.WaveformWorker("a1", Path("in.mp4"))

    def thumbs(self, count: int = 2) -> th.ThumbnailWorker:
        return th.ThumbnailWorker("c1", Path("in.mp4"), 10.0, count=count)

    def collect(self, worker) -> tuple[list, list]:
        ok: list = []
        bad: list = []
        worker.finished.connect(lambda *a: ok.append(a))
        worker.failed.connect(lambda *a: bad.append(a))
        return ok, bad


# --- A: cancel before the child is launched ----------------------------


class CancelBeforeLaunchTests(_ProcCase):

    def test_a_cancelled_thumbnail_worker_never_spawns_ffmpeg(self) -> None:
        self.popen()
        w = self.thumbs()
        ok, bad = self.collect(w)
        w.cancel()
        started = time.monotonic()
        w.run()
        self.assertEqual(self.spawned, [])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((ok, bad), ([], []))

    def test_a_cancelled_waveform_worker_never_spawns_ffmpeg(self) -> None:
        self.popen()
        w = self.wave()
        ok, bad = self.collect(w)
        w.cancel()
        started = time.monotonic()
        w.run()
        self.assertEqual(self.spawned, [])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((ok, bad), ([], []),
                         "a cancel is not a failure to report")


# --- B: cancelling a live child ----------------------------------------


class LiveChildCancelTests(_ProcCase):

    def test_b_cancel_terminates_and_reaps_a_live_waveform_child(self) -> None:
        self.popen()
        w = self.wave()
        ok, bad = self.collect(w)
        t = self.run_off_thread(w)
        child = self.await_child()
        self.assertTrue(child.live)

        w.cancel()
        t.join(LIMIT)
        self.assertFalse(t.is_alive(), "worker stayed inside ffmpeg")
        self.assertEqual(child.terminate_calls, 1)
        self.assertFalse(child.live)
        self.assertTrue(child.reaped, "child was terminated but never reaped")
        self.assertEqual((ok, bad), ([], []))

    def test_b_cancel_terminates_and_reaps_a_live_thumbnail_child(self) -> None:
        self.popen()
        w = self.thumbs(count=8)
        ok, bad = self.collect(w)
        t = self.run_off_thread(w)
        child = self.await_child()

        w.cancel()
        t.join(LIMIT)
        self.assertFalse(t.is_alive())
        self.assertEqual(child.terminate_calls, 1)
        self.assertTrue(child.reaped)
        # The remaining frames are abandoned, not queued up behind it.
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual((ok, bad), ([], []))

    def test_b_cancel_does_not_block_the_calling_thread(self) -> None:
        """``cancel()`` runs on the GUI thread during ``closeEvent``.

        Waiting for the child there would freeze the window for exactly as
        long as the stuck ffmpeg it is trying to escape, so the reaping is
        the worker thread's job.
        """
        self.popen(lambda cmd, **kw: FakeChild(cmd, exits_on_terminate=False,
                                               **kw))
        w = self.wave()
        t = self.run_off_thread(w)
        child = self.await_child()

        started = time.monotonic()
        w.cancel()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, th._TERMINATE_GRACE_S,
                        "cancel() waited on the child")
        t.join(LIMIT)
        self.assertFalse(t.is_alive())
        self.assertTrue(child.reaped)


# --- C: a child that ignores terminate ---------------------------------


class StubbornChildTests(_ProcCase):

    def test_c_a_child_that_ignores_terminate_is_killed_and_reaped(self) -> None:
        self.popen(lambda cmd, **kw: FakeChild(cmd, exits_on_terminate=False,
                                               **kw))
        w = self.wave()
        t = self.run_off_thread(w)
        child = self.await_child()

        w.cancel()
        t.join(LIMIT)
        self.assertFalse(t.is_alive(), "stubborn child kept the worker alive")
        self.assertEqual(child.terminate_calls, 1)
        self.assertGreaterEqual(child.kill_calls, 1)
        self.assertFalse(child.live)
        self.assertTrue(child.reaped)

    def test_c_the_kill_waits_out_the_grace_period_first(self) -> None:
        """``terminate`` is given its bounded chance before ``kill``.

        Killing immediately would leave a half-written temp behind for any
        child that would have exited cleanly a moment later.
        """
        self.popen(lambda cmd, **kw: FakeChild(cmd, exits_on_terminate=False,
                                               **kw))
        with unittest.mock.patch.object(th, "_TERMINATE_GRACE_S", 0.4):
            w = self.wave()
            t = self.run_off_thread(w)
            child = self.await_child()
            w.cancel()
            t.join(LIMIT)
        self.assertFalse(t.is_alive())
        self.assertEqual(child.kill_calls, 1)


# --- D: a cancel that races Popen publication --------------------------


class PublicationRaceTests(_ProcCase):

    def test_d_a_cancel_during_spawn_is_not_lost(self) -> None:
        in_flight = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def _slow_spawn(cmd, **kw):
            in_flight.set()
            release.wait(LIMIT)
            child = FakeChild(cmd, **kw)
            self.spawned.append(child)
            return child

        p = unittest.mock.patch.object(th.subprocess, "Popen", _slow_spawn)
        p.start()
        self.addCleanup(p.stop)

        w = self.wave()
        ok, bad = self.collect(w)
        t = self.run_off_thread(w)
        self.assertTrue(in_flight.wait(LIMIT), "spawn never started")

        # The child exists but has not been published yet: this is the
        # window where reading "no process object" as "no process" loses
        # the cancellation.
        w.cancel()
        release.set()

        t.join(LIMIT)
        self.assertFalse(t.is_alive())
        child = self.await_child()
        self.assertEqual(child.terminate_calls, 1,
                         "cancel was dropped in the publication window")
        self.assertFalse(child.live)
        self.assertTrue(child.reaped)
        self.assertEqual((ok, bad), ([], []))

    def test_d_a_cancel_during_a_failing_spawn_still_stops_the_worker(
        self,
    ) -> None:
        """No child is ever produced, so there is nothing to terminate -
        and the worker must still come back rather than trip over its own
        deferred cancellation."""
        in_flight = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def _failing_spawn(cmd, **kw):
            in_flight.set()
            release.wait(LIMIT)
            raise OSError("no ffmpeg")

        p = unittest.mock.patch.object(th.subprocess, "Popen", _failing_spawn)
        p.start()
        self.addCleanup(p.stop)

        w = self.wave()
        t = self.run_off_thread(w)
        self.assertTrue(in_flight.wait(LIMIT))
        w.cancel()
        release.set()
        t.join(LIMIT)
        self.assertFalse(t.is_alive())


# --- E: a child that already exited ------------------------------------


class TerminalChildTests(_ProcCase):

    def test_e_cancel_does_not_signal_a_child_that_already_exited(self) -> None:
        self.popen()
        w = self.wave()
        t = self.run_off_thread(w)
        child = self.await_child()

        child.finish(0)          # ffmpeg got there first
        t.join(LIMIT)
        self.assertFalse(t.is_alive())

        w.cancel()
        self.assertEqual(child.terminate_calls, 0)
        self.assertEqual(child.kill_calls, 0)
        self.assertEqual(child.returncode, 0, "a finished run stays finished")

    def test_e_cancel_before_the_worker_notices_the_exit_is_harmless(
        self,
    ) -> None:
        """The child is terminal but still published: ``poll()`` is what
        distinguishes it from a live one."""
        child = FakeChild(["ffmpeg"], returncode=0)
        owner = th._MediaChild()
        with owner._lock:
            owner._proc = child
        owner.cancel()
        self.assertEqual(child.terminate_calls, 0)
        self.assertEqual(child.kill_calls, 0)


# --- F: repeated cancel -------------------------------------------------


class RepeatedCancelTests(_ProcCase):

    def test_f_cancelling_three_times_signals_the_child_once(self) -> None:
        self.popen()
        w = self.wave()
        t = self.run_off_thread(w)
        child = self.await_child()

        w.cancel()
        w.cancel()
        w.cancel()
        t.join(LIMIT)
        self.assertFalse(t.is_alive())
        self.assertEqual(child.terminate_calls, 1)
        self.assertEqual(child.kill_calls, 0)
        self.assertFalse(child.live)

    def test_f_cancelling_a_worker_that_never_ran_is_harmless(self) -> None:
        self.popen()
        w = self.thumbs()
        w.cancel()
        w.cancel()
        self.assertEqual(self.spawned, [])


# --- G: the uncancelled path is unchanged ------------------------------


class NormalCompletionTests(_ProcCase):

    def test_g_thumbnails_are_produced_for_every_frame(self) -> None:
        self.popen(_succeeding_child)
        w = self.thumbs(count=5)
        ok, bad = self.collect(w)
        w.run()
        self.assertEqual(bad, [])
        self.assertEqual(len(ok), 1)
        clip_id, images = ok[0]
        self.assertEqual(clip_id, "c1")
        self.assertEqual(len(images), 5)
        self.assertEqual(len(self.spawned), 5)
        self.assertFalse(any(c.terminate_calls or c.kill_calls
                             for c in self.spawned))

    def test_g_the_thumbnail_command_still_asks_ffmpeg_for_one_frame(
        self,
    ) -> None:
        self.popen(_succeeding_child)
        self.thumbs(count=1).run()
        cmd = self.spawned[0].args
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-frames:v", cmd)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")
        self.assertIn("-ss", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "scale=-2:80")
        self.assertEqual(cmd[cmd.index("-i") + 1], "in.mp4")

    def test_g_waveform_peaks_are_normalised_as_before(self) -> None:
        pcm = array.array("f", [0.0, -0.25, 0.5, -0.5]).tobytes()
        self.popen(lambda cmd, **kw: FakeChild(cmd, returncode=0,
                                               out_data=pcm, **kw))
        w = self.wave()
        ok, bad = self.collect(w)
        w.run()
        self.assertEqual(bad, [])
        self.assertEqual(len(ok), 1)
        clip_id, peaks, rate = ok[0]
        self.assertEqual(clip_id, "a1")
        self.assertEqual(rate, th.WaveformWorker.PEAK_RATE)
        self.assertEqual([round(p, 3) for p in peaks], [0.0, 0.5, 1.0, 1.0])

    def test_g_a_failing_waveform_child_still_reports_failure(self) -> None:
        self.popen(lambda cmd, **kw: FakeChild(cmd, returncode=1,
                                               err_data=b"boom", **kw))
        w = self.wave()
        ok, bad = self.collect(w)
        w.run()
        self.assertEqual(ok, [])
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0][0], "a1")

    def test_g_a_failing_frame_is_skipped_not_fatal(self) -> None:
        calls = {"n": 0}

        def _spawn(cmd, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                child = FakeChild(cmd, returncode=1, **kw)
                self.spawned.append(child)
                return child
            return _succeeding_child(cmd, **kw)

        p = unittest.mock.patch.object(th.subprocess, "Popen", _spawn)
        p.start()
        self.addCleanup(p.stop)
        w = self.thumbs(count=3)
        ok, bad = self.collect(w)
        w.run()
        self.assertEqual(bad, [])
        self.assertEqual(len(ok[0][1]), 2)


# --- H: a cancelled worker publishes nothing ---------------------------


class ResultSuppressionTests(_ProcCase):

    def test_h_a_worker_cancelled_mid_child_emits_no_result(self) -> None:
        self.popen()
        w = self.thumbs(count=4)
        ok, bad = self.collect(w)
        t = self.run_off_thread(w)
        child = self.await_child()
        w.cancel()
        t.join(LIMIT)
        self.assertFalse(t.is_alive())
        self.assertFalse(child.live)
        self.assertEqual(ok, [], "partial frames were published anyway")
        self.assertEqual(bad, [], "a cancel is not an error to show")

    def test_h_a_cancelled_waveform_worker_emits_no_result(self) -> None:
        pcm = array.array("f", [0.5, 0.5]).tobytes()
        self.popen(lambda cmd, **kw: FakeChild(cmd, out_data=pcm, **kw))
        w = self.wave()
        ok, bad = self.collect(w)
        t = self.run_off_thread(w)
        self.await_child()
        w.cancel()
        t.join(LIMIT)
        self.assertEqual((ok, bad), ([], []))


# --- I: the same thing on a real QThread -------------------------------


class QThreadIntegrationTests(_ProcCase):

    def test_i_cancel_lets_the_real_qthread_finish(self) -> None:
        self.popen()
        thread, worker = th.start_waveform("a1", Path("in.mp4"))
        self.addCleanup(lambda: (thread.quit(), thread.wait(int(LIMIT * 1000))))
        thread.start()
        child = self.await_child()
        self.assertTrue(thread.isRunning())

        worker.cancel()
        deadline = time.monotonic() + LIMIT
        while thread.isRunning() and time.monotonic() < deadline:
            QApplication.processEvents()
        self.assertFalse(thread.isRunning(),
                         "QThread never returned after cancel")
        self.assertTrue(child.reaped)

    def test_i_a_thumbnail_qthread_finishes_the_same_way(self) -> None:
        self.popen()
        thread, worker = th.start_thumbnails("c1", Path("in.mp4"), 10.0,
                                             count=6)
        self.addCleanup(lambda: (thread.quit(), thread.wait(int(LIMIT * 1000))))
        thread.start()
        child = self.await_child()

        worker.cancel()
        deadline = time.monotonic() + LIMIT
        while thread.isRunning() and time.monotonic() < deadline:
            QApplication.processEvents()
        self.assertFalse(thread.isRunning())
        self.assertTrue(child.reaped)


# --- the 2N-E teardown reproducer --------------------------------------


class WindowTeardownTests(_ProcCase):
    """The acceptance case: closing over a blocked worker must not leave a
    running ``QThread`` for interpreter shutdown to abort on.

    The close deadline is shortened to a millisecond so the *timed out*
    branch is the one under test - the survivor list is only reached when
    the bounded join fails, which is exactly the situation this slice has
    to make recoverable.
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
        d = unittest.mock.patch.object(app_mod, "_CLOSE_JOIN_MS", 1)
        d.start()
        self.addCleanup(d.stop)
        self.addCleanup(self._drain_surviving_threads)

    @staticmethod
    def _drain_surviving_threads() -> None:
        for thread, _worker in list(app_mod._SURVIVING_MEDIA_THREADS):
            thread.quit()
            thread.wait(5000)
        for _ in range(20):
            QApplication.processEvents()
        app_mod._SURVIVING_MEDIA_THREADS.clear()

    def _spin(self, predicate) -> bool:
        deadline = time.monotonic() + LIMIT
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_close_over_a_blocked_worker_leaves_no_running_thread(
        self,
    ) -> None:
        self.popen(lambda cmd, **kw: FakeChild(cmd, exits_on_terminate=False,
                                               **kw))
        w = MainWindow()
        self.addCleanup(w.deleteLater)
        thread, worker = th.start_waveform("c1", Path("in.mp4"))
        w._wave_threads["c1"] = thread
        w._wave_workers["c1"] = worker
        thread.start()
        child = self.await_child()
        self.assertTrue(thread.isRunning())

        w.close()

        # 4-6. The child is stopped and reaped, the worker returns, and
        # whichever owner is holding the thread lets go of it.
        self.assertTrue(self._spin(lambda: not thread.isRunning()),
                        "the media QThread was still running after close")
        self.assertTrue(child.reaped)
        self.assertTrue(
            self._spin(lambda: not app_mod._SURVIVING_MEDIA_THREADS),
            "survivor entry never released after the thread finished",
        )
        self.assertEqual(w._retired_media, [])

    def test_close_over_a_retired_worker_releases_it_too(self) -> None:
        """A worker already retired by a project swap takes the same path:
        ``closeEvent`` re-cancels it, and that cancel now reaches ffmpeg."""
        self.popen(lambda cmd, **kw: FakeChild(cmd, exits_on_terminate=False,
                                               **kw))
        w = MainWindow()
        self.addCleanup(w.deleteLater)
        thread, worker = th.start_waveform("c1", Path("in.mp4"))
        w._wave_threads["c1"] = thread
        w._wave_workers["c1"] = worker
        thread.start()
        child = self.await_child()

        w._stop_media_workers(wait_ms=1)
        self.assertEqual(w._wave_threads, {})

        w.close()
        self.assertTrue(self._spin(lambda: not thread.isRunning()))
        self.assertTrue(child.reaped)
        self.assertTrue(
            self._spin(lambda: not app_mod._SURVIVING_MEDIA_THREADS))

    def test_a_normal_close_still_joins_a_finishing_worker(self) -> None:
        """The uncancelled path must not start needing the survivor list."""
        self.popen(_succeeding_child)
        w = MainWindow()
        self.addCleanup(w.deleteLater)
        thread, worker = th.start_thumbnails("c1", Path("in.mp4"), 10.0,
                                             count=2)
        w._thumb_threads["c1"] = thread
        w._thumb_workers["c1"] = worker
        thread.start()
        self.assertTrue(self._spin(lambda: not thread.isRunning()))
        w.close()
        self.assertEqual(app_mod._SURVIVING_MEDIA_THREADS, [])


#: A whole application run: real `exec()` loop, real last-window close, no
#: manual event pumping afterwards. It has to be a separate process - the
#: defect is what the *interpreter* does to a still-running QThread on its
#: way out, which cannot be observed from inside a test that must survive.
#:
#: The child ignores `terminate`, so the worker only returns after the
#: kill fallback. That is deliberately longer than `_CLOSE_JOIN_MS`: the
#: parked thread is therefore still running when `exec()` returns, which
#: is precisely the window where a queued `finished` can no longer be
#: delivered and the reference alone protects nothing.
_SHUTDOWN_PROBE = '''
import os, subprocess, sys, tempfile, threading
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from cove_video_editor import app as app_mod
from cove_video_editor import thumbnails as th

class StubbornChild:
    def __init__(self, cmd, **kw):
        self.args = list(cmd); self.returncode = None
        self._exited = threading.Event()
    def poll(self): return self.returncode
    def terminate(self): pass
    def kill(self):
        self.returncode = -9; self._exited.set()
    def communicate(self, timeout=None):
        if not self._exited.wait(30 if timeout is None else timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return b"", b""

th.subprocess.Popen = StubbornChild
th.ff.require_ffmpeg = lambda: "ffmpeg"
# Both bounds are shrunk, but their *relationship* is the production one
# and is what the case needs: the kill fallback lands after the close join
# has already given up, so the thread really is parked and really is still
# running when exec() returns.
th._TERMINATE_GRACE_S = {grace}
app_mod._CLOSE_JOIN_MS = {join_ms}
assert {join_ms} / 1000.0 < {grace}, "reproducer needs a join that gives up first"

app = QApplication(sys.argv)
app_mod.MainWindow._start_encoder_probe = lambda self: None
win = app_mod.MainWindow()
thread, worker = th.start_waveform("c1", Path(tempfile.mkdtemp()) / "x.wav")
win._wave_threads["c1"] = thread
win._wave_workers["c1"] = worker
thread.start()
win.show()
QTimer.singleShot(200, win.close)
code = app.exec()
{drain}
print("running=%s" % thread.isRunning(), flush=True)
sys.exit(code)
'''


class ApplicationShutdownTests(unittest.TestCase):
    """Closing the last window must not leave a live QThread for the
    interpreter to abort on.

    `closeEvent`'s join is bounded on purpose - a stalled worker must not
    freeze the window - so a worker whose child only dies at the kill
    fallback outlives it and gets parked. Parking is a reference plus a
    queued `finished`, and both stop meaning anything the moment `exec()`
    returns: there is no event loop left to deliver the release, and a
    reference does not stop the interpreter destroying a running QThread.
    """

    def _run_probe(self, drain: str) -> subprocess.CompletedProcess:
        src = Path(__file__).resolve().parent.parent / "src"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            probe.write_text(
                _SHUTDOWN_PROBE.format(grace=0.6, join_ms=100, drain=drain),
                encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(src),
                       QT_QPA_PLATFORM="offscreen")
            return subprocess.run([sys.executable, str(probe)], env=env,
                                  capture_output=True, text=True, timeout=90)

    def test_the_parked_thread_is_joined_before_the_process_exits(self) -> None:
        proc = self._run_probe(
            "app_mod.drain_surviving_media_threads()")
        self.assertEqual(proc.returncode, 0,
                         f"process did not exit cleanly:\n{proc.stderr}")
        self.assertIn("running=False", proc.stdout)
        self.assertNotIn("QThread: Destroyed", proc.stderr)

    def test_without_the_drain_the_thread_is_still_running(self) -> None:
        """The other half of the same fact, and what makes the test above
        mean something: skipping the drain leaves exactly the state that
        aborts, so the assertion is not passing for an unrelated reason."""
        proc = self._run_probe("pass")
        self.assertIn("running=True", proc.stdout,
                      "the reproducer no longer reproduces")


if __name__ == "__main__":
    unittest.main()
