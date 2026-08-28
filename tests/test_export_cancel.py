"""Tab 2K: export cancellation is a first-class terminal outcome.

Before this slice a deliberate cancellation was laundered through the
failure channel - ``ExportWorker.run`` emitted ``failed("Cancelled")``,
so ``MainWindow`` showed ``Failed: Cancelled``, a red cross in the log,
force-opened Details and raised an "Export failed" modal. A user who
chose to stop an export was told their export broke.

Export now has exactly three terminal outcomes:

    finished(path)   completed
    cancelled()      the user stopped it
    failed(message)  something genuinely went wrong

Exactly one of them is emitted per run. The tests below pin that
one-terminal-outcome contract across every route out of ``run()``
(normal end, cancel observed in the progress loop, cancel observed
before execution, cancel racing an exception) because the defect was
precisely that only *one* tail branch knew about cancellation.

Determinism: no sleeps and no real ffmpeg. ``subprocess.Popen`` is
replaced by a fake exposing the same surface production touches
(``stdout``/``stderr`` iterators, ``poll``, ``terminate``, ``wait``),
and the cancel is triggered from inside the stdout iterator so the
worker observes it at an exact, reproducible point. The MainWindow
tests run on the offscreen platform with the background NVENC/AMF probe
suppressed - it spawns ffmpeg children that outlive the window and no
cancellation behavior depends on encoder capabilities.
"""
from __future__ import annotations

import os
import threading
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import exporter as exporter_mod  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402
from cove_video_editor.clip import Clip, MediaAsset  # noqa: E402
from cove_video_editor.exporter import (  # noqa: E402
    ExportJob,
    ExportWorker,
    start_export,
)

_app: QApplication | None = None

MP4 = "MP4 (H.264 + AAC)"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --- fakes -------------------------------------------------------------


class _Stdout:
    """ffmpeg's ``-progress pipe:1`` stream, line by line.

    ``hook`` runs *before* the line at that index is handed to the
    worker, which is how a cancellation is injected at an exact point in
    the progress loop without any timing dependency.
    """

    def __init__(self, lines: list[str], hooks: dict[int, object] | None = None):
        self._lines = lines
        self._hooks = hooks or {}

    def __iter__(self):
        for i, line in enumerate(self._lines):
            hook = self._hooks.get(i)
            if hook is not None:
                hook()
            yield line + "\n"


class FakeProc:
    """Stands in for a real ``subprocess.Popen`` of ffmpeg.

    Models the *success* shape production expects: ``wait()`` returns an
    int return code, ``poll()`` reports liveness, and both pipes are
    iterable text streams. A fake returning ``None`` from ``wait()``
    would model a failure production never sees.
    """

    def __init__(self, lines: list[str], *, rc: int = 0,
                 hooks: dict[int, object] | None = None,
                 stderr: list[str] | None = None,
                 on_wait: object = None):
        self.stdout = _Stdout(lines, hooks)
        self.stderr = iter([ln + "\n" for ln in (stderr or [])])
        self._rc = rc
        self._on_wait = on_wait
        self.terminate_calls = 0
        self._done = False

    def poll(self):
        return self._rc if self._done else None

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._done = True

    def wait(self) -> int:
        self._done = True
        if self._on_wait is not None:
            self._on_wait()
        return self._rc


class Outcomes:
    """Records every terminal signal a worker emitted, in order."""

    def __init__(self, worker: ExportWorker):
        self.finished: list[Path] = []
        self.failed: list[str] = []
        self.cancelled: list[None] = []
        self.order: list[str] = []
        worker.finished.connect(self._fin)
        worker.failed.connect(self._fail)
        worker.cancelled.connect(self._cancel)

    def _fin(self, p: Path) -> None:
        self.finished.append(p)
        self.order.append("finished")

    def _fail(self, m: str) -> None:
        self.failed.append(m)
        self.order.append("failed")

    def _cancel(self) -> None:
        self.cancelled.append(None)
        self.order.append("cancelled")


# --- helpers -----------------------------------------------------------


def _asset(name: str = "a.mp4") -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=60.0, width=1280, height=720,
        fps=30.0, has_audio=True, kind="video",
    )


def _job(out: Path, **kw) -> ExportJob:
    clips = [Clip(asset=_asset(), timeline_start=0.0, src_start=0.0, src_end=5.0)]
    return ExportJob(clips=clips, output=out, fmt_key=MP4, **kw)


PROGRESS = ["out_time_us=1000000", "out_time_us=2000000", "progress=end"]


def _run(worker: ExportWorker, proc: FakeProc | None = None,
         popen_exc: BaseException | None = None):
    """Run the worker with the *export* ffmpeg replaced by ``proc``.

    Only the encode itself is faked. Command construction legitimately
    shells out (encoder capability probing), and hijacking those calls
    too would fail them in a way production never sees, so anything that
    is not the ``-progress pipe:1`` export command goes to the real
    ``Popen``.
    """
    real_popen = exporter_mod.subprocess.Popen

    def _spawn(cmd, *a, **kw):
        if not (isinstance(cmd, list) and "-progress" in cmd):
            return real_popen(cmd, *a, **kw)
        if popen_exc is not None:
            raise popen_exc
        return proc

    with unittest.mock.patch.object(
        exporter_mod.subprocess, "Popen", side_effect=_spawn,
    ):
        worker.run()


class _TempOut(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory(prefix="cove-cancel-")
        self.addCleanup(self._td.cleanup)
        self.out = Path(self._td.name) / "out.mp4"


# --- R1: the public signal surface -------------------------------------


class SignalSurfaceTests(_TempOut):
    def test_worker_exposes_a_dedicated_cancelled_signal(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen: list[str] = []
        worker.cancelled.connect(lambda: seen.append("c"))
        worker.cancelled.emit()
        self.assertEqual(seen, ["c"])

    def test_start_export_quits_the_thread_on_cancellation(self) -> None:
        """Without this wiring the thread never finishes, so the existing
        ``thread.finished -> _reset_after_export`` path never runs and the
        UI stays stuck in the exporting state."""
        import warnings

        thread, worker = start_export(_job(self.out))
        self.addCleanup(thread.deleteLater)
        # PySide only *warns* when disconnecting something that was never
        # connected, so warnings are promoted to errors - otherwise a
        # missing connection would look identical to a present one.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            try:
                worker.cancelled.disconnect(thread.quit)
            except Exception:  # noqa: BLE001
                self.fail("start_export must connect cancelled -> thread.quit")


# --- R2-R5, R9: one terminal outcome per run ---------------------------


class TerminalOutcomeTests(_TempOut):
    def test_cancel_mid_run_emits_cancelled_only(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        proc = FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel})
        _run(worker, proc)

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(seen.failed, [])
        self.assertEqual(seen.finished, [])
        self.assertGreaterEqual(proc.terminate_calls, 1)

    def test_cancel_before_execution_emits_cancelled_only(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        worker.cancel()
        _run(worker, FakeProc(PROGRESS, rc=-15))

        self.assertEqual(seen.order, ["cancelled"])

    def test_cancel_racing_an_exception_is_still_cancellation(self) -> None:
        """Terminating the child can make the very next call blow up. The
        user still cancelled - that must not surface as a failure."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        worker.cancel()
        _run(worker, popen_exc=OSError("process gone"))

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(seen.failed, [])

    def test_genuine_failure_is_never_reported_as_cancellation(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(
            ["out_time_us=1000000"], rc=1,
            stderr=["Invalid data found when processing input"],
        ))

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.cancelled, [])
        self.assertEqual(seen.finished, [])
        self.assertIn("ffmpeg exited 1", seen.failed[0])

    def test_a_cancel_after_a_nonzero_exit_cannot_hide_the_failure(self) -> None:
        """ffmpeg has already died nonzero when Cancel is clicked.

        `cancel()` sets the request flag before it polls, so classifying
        on that flag alone turns a genuine encode failure into a neutral
        "Export cancelled" - hiding the error, the log detail and the
        warning modal. Cancellation only owns the outcome if it actually
        claimed a live process; a terminal status already reached by the
        process itself wins.
        """
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        # `on_wait` fires once the fake child is already marked exited,
        # so `cancel()` observes a dead process - exactly the race.
        proc = FakeProc(["out_time_us=1"], rc=1, stderr=["bad codec"],
                        on_wait=worker.cancel)
        _run(worker, proc)

        self.assertTrue(worker._cancelled)  # the request really was made
        self.assertEqual(proc.terminate_calls, 0)  # nothing was claimed
        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.cancelled, [])

    def test_a_late_cancel_during_the_stderr_join_is_also_a_failure(self) -> None:
        """The same race one window later: after `wait()` returned
        nonzero, while the worker is still draining stderr."""
        import threading as _threading

        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with unittest.mock.patch.object(
            _threading.Thread, "join", autospec=True,
            side_effect=lambda self, *a, **k: worker.cancel(),
        ):
            _run(worker, FakeProc(["out_time_us=1"], rc=1, stderr=["bad codec"]))

        self.assertTrue(worker._cancelled)
        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.cancelled, [])

    def test_cancel_claiming_a_live_process_is_a_cancellation(self) -> None:
        """The counterpart: the child is genuinely alive when Cancel
        lands, so cancellation owns the outcome even though ffmpeg then
        exits nonzero because we terminated it."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        proc = FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel})
        self.assertIsNone(proc.poll())  # alive at the moment of the click
        _run(worker, proc)

        self.assertGreaterEqual(proc.terminate_calls, 1)
        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(seen.failed, [])
        self.assertEqual(seen.finished, [])

    def test_default_uncancelled_worker_completes_normally(self) -> None:
        """The default path: nothing cancelled, nothing patched about
        cancellation state."""
        worker = ExportWorker(_job(self.out))
        self.assertFalse(worker._cancelled)
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(seen.finished, [self.out])


# --- the Popen publication window --------------------------------------


class StartupPublicationRaceTests(_TempOut):
    """`subprocess.Popen` spawns the child before it returns.

    For that interval a real ffmpeg process exists while `self._proc` is
    still ``None``. Reading "no process object yet" as "no process
    exists" lets a cancel in that window claim a run whose child has
    already failed, handing a genuine error the neutral cancellation UI.
    Ownership must therefore be *deferred* until the process can actually
    be polled, not decided from the unpublished attribute.

    The window is real but microscopic, so it is driven deterministically
    rather than by timing: the fake `Popen` blocks on an Event, the test
    cancels while it is blocked, sets the child's state, then releases.
    """

    def _race(self, *, child: str):
        """Cancel while `Popen` is in flight; the child reaches `child`
        state ("live", "failed", "succeeded") before publication."""
        rc = {"live": -15, "failed": 1, "succeeded": 0}[child]
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        proc = FakeProc(PROGRESS, rc=rc, stderr=["bad codec"])
        entered = threading.Event()
        release = threading.Event()
        real_popen = exporter_mod.subprocess.Popen

        def _spawn(cmd, *a, **kw):
            if not (isinstance(cmd, list) and "-progress" in cmd):
                return real_popen(cmd, *a, **kw)
            # The child now exists as far as the OS is concerned, but the
            # worker has no handle on it yet.
            entered.set()
            self.assertTrue(release.wait(5), "test seam was never released")
            return proc

        with unittest.mock.patch.object(
            exporter_mod.subprocess, "Popen", side_effect=_spawn,
        ):
            runner = threading.Thread(target=worker.run)
            runner.start()
            self.assertTrue(entered.wait(5), "export Popen was never reached")
            worker.cancel()
            if child != "live":
                proc._done = True  # the child settled on its own
            release.set()
            runner.join(timeout=10)

        # Direct evidence, not a timing assertion: the run completed.
        self.assertFalse(runner.is_alive(), "worker deadlocked on cancellation")
        # The worker really is on its own thread here, so its terminal
        # signal is queued to this one exactly as it is in the app.
        QApplication.processEvents()
        self.assertEqual(len(seen.order), 1, f"one terminal signal: {seen.order}")
        return worker, seen, proc

    def test_startup_cancel_of_an_already_failed_child_is_a_failure(self) -> None:
        _worker, seen, proc = self._race(child="failed")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.cancelled, [])
        self.assertEqual(seen.finished, [])
        self.assertEqual(proc.terminate_calls, 0)
        self.assertIn("ffmpeg exited 1", seen.failed[0])

    def test_startup_cancel_of_a_live_child_is_a_cancellation(self) -> None:
        _worker, seen, proc = self._race(child="live")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(seen.failed, [])
        self.assertEqual(seen.finished, [])
        self.assertEqual(proc.terminate_calls, 1)

    def test_startup_cancel_of_an_already_finished_child_still_succeeds(self) -> None:
        _worker, seen, proc = self._race(child="succeeded")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(seen.cancelled, [])
        self.assertEqual(seen.failed, [])
        # Nothing to signal: the child had already exited cleanly.
        self.assertEqual(proc.terminate_calls, 0)

    def test_cancel_before_startup_never_spawns_a_child(self) -> None:
        """Pre-start cancellation owns the run outright, and there is no
        reason to launch ffmpeg only to kill it."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        spawned: list[list[str]] = []
        real_popen = exporter_mod.subprocess.Popen

        def _spawn(cmd, *a, **kw):
            if isinstance(cmd, list) and "-progress" in cmd:
                spawned.append(cmd)
                return FakeProc(PROGRESS, rc=0)
            return real_popen(cmd, *a, **kw)

        worker.cancel()
        with unittest.mock.patch.object(
            exporter_mod.subprocess, "Popen", side_effect=_spawn,
        ):
            worker.run()

        self.assertEqual(spawned, [])
        self.assertEqual(seen.order, ["cancelled"])


# --- destination files: this slice makes no ownership claim -------------


class OutputFileTests(_TempOut):
    """Tab 2K deliberately does not delete anything at ``job.output``.

    An earlier revision removed the partial file on cancellation. Without
    a run-owned output path there is no way to prove that whatever sits
    at the destination when cleanup runs is still the partial this run
    wrote - another process can create or replace it in between - so the
    deletion was dropped rather than replaced with a second heuristic.
    Deleting a file we do not own is worse than leaving a partial behind.
    Safe cleanup is deferred to the "safe export destination ownership"
    slice (run-owned temporary output plus atomic promotion).
    """

    def test_cancellation_leaves_the_partial_destination_in_place(self) -> None:
        """The deferred limitation, pinned so it cannot regress silently
        in either direction."""
        def _write_partial() -> None:
            self.out.write_bytes(b"\x00" * 4096)

        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=-15,
                              hooks={0: _write_partial, 1: worker.cancel}))

        self.assertEqual(seen.order, ["cancelled"])
        self.assertTrue(self.out.exists())

    def test_cancellation_never_touches_a_file_it_does_not_own(self) -> None:
        self.out.write_bytes(b"the user's existing file")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}))

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(self.out.read_bytes(), b"the user's existing file")

    def test_cancellation_with_no_output_file_is_harmless(self) -> None:
        self.assertFalse(self.out.exists())
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        worker.cancel()
        _run(worker, FakeProc(PROGRESS, rc=-15))

        self.assertEqual(seen.order, ["cancelled"])
        self.assertFalse(self.out.exists())

    def test_successful_output_is_never_deleted(self) -> None:
        self.out.write_bytes(b"\x00" * 2048)
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(seen.order, ["finished"])
        self.assertTrue(self.out.exists())
        self.assertEqual(self.out.stat().st_size, 2048)

    def test_a_cancel_that_loses_the_race_still_reports_success(self) -> None:
        """Clicking Cancel at the exact moment ffmpeg exits 0.

        The encode is already on disk and complete, so there is nothing
        left to cancel and the run reports the success it achieved.
        """
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        # `wait()` returning 0 is the success; the cancel lands right after.
        proc = FakeProc(PROGRESS, rc=0, on_wait=worker.cancel)
        self.out.write_bytes(b"\x00" * 4096)
        _run(worker, proc)

        self.assertEqual(seen.order, ["finished"])
        self.assertTrue(self.out.exists())
        self.assertEqual(self.out.stat().st_size, 4096)

    def test_failed_output_is_left_alone(self) -> None:
        """Failure keeps its existing semantics."""
        self.out.write_bytes(b"partial")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(["out_time_us=1"], rc=1, stderr=["boom"]))

        self.assertEqual(seen.order, ["failed"])
        self.assertTrue(self.out.exists())


# --- R10: command construction is untouched ----------------------------


class CommandParityTests(_TempOut):
    def test_cancellation_state_does_not_change_the_ffmpeg_command(self) -> None:
        job = _job(self.out, width=1920, height=1080)
        plain = ExportWorker(job)._build_command()

        cancelled_worker = ExportWorker(job)
        cancelled_worker.cancel()
        self.assertEqual(cancelled_worker._build_command(), plain)

        # The command still carries the pieces this slice must not touch.
        self.assertIn("-progress", plain)
        self.assertIn("pipe:1", plain)
        self.assertIn("-y", plain)
        self.assertIn("-c:v", plain)  # encoder choice is host-dependent
        self.assertIn("aac", plain)
        self.assertTrue(any("scale=1920:1080" in a for a in plain))
        self.assertTrue(any("setsar=1" in a for a in plain))


# --- MainWindow ---------------------------------------------------------


def _win() -> MainWindow:
    with unittest.mock.patch.object(
        MainWindow, "_start_encoder_probe", lambda self: None,
    ):
        return MainWindow()


class _WinCase(unittest.TestCase):
    def setUp(self) -> None:
        self.w = _win()
        self.addCleanup(self.w.deleteLater)
        self.warn = unittest.mock.patch.object(
            app_mod.QMessageBox, "warning", return_value=None,
        ).start()
        self.addCleanup(unittest.mock.patch.stopall)

    def log_text(self) -> str:
        return self.w.export_log.toPlainText()


# --- R11-R12: cancellation UX ------------------------------------------


class CancelUiTests(_WinCase):
    def test_cancelled_status_and_log_are_neutral(self) -> None:
        self.w._on_export_cancelled()

        self.assertEqual(self.w.status.currentMessage(), "Export cancelled")
        text = self.log_text()
        self.assertIn("Export cancelled", text)
        self.assertNotIn("✗", text)
        self.assertNotIn("Failed", text)

    def test_cancellation_shows_no_modal(self) -> None:
        self.w._on_export_cancelled()
        self.warn.assert_not_called()

    def test_cancellation_does_not_reuse_the_failure_handler(self) -> None:
        with unittest.mock.patch.object(
            MainWindow, "_on_export_failed",
        ) as failed:
            self.w._on_export_cancelled()
        failed.assert_not_called()

    def test_details_stays_collapsed_when_it_was_collapsed(self) -> None:
        self.w.details_btn.setChecked(False)
        self.w._on_details_toggled(False)
        self.w._on_export_cancelled()

        self.assertFalse(self.w.details_btn.isChecked())
        self.assertFalse(self.w._log_panel.isVisibleTo(self.w))

    def test_details_stays_expanded_when_it_was_expanded(self) -> None:
        self.w.details_btn.setChecked(True)
        self.w._on_details_toggled(True)
        self.w._on_export_cancelled()

        self.assertTrue(self.w.details_btn.isChecked())
        self.assertTrue(self.w._log_panel.isVisibleTo(self.w))


# --- R13: the genuine-failure UX is unchanged --------------------------


class FailureUiRegressionTests(_WinCase):
    def test_failure_still_shouts(self) -> None:
        self.w.details_btn.setChecked(False)
        self.w._on_details_toggled(False)

        self.w._on_export_failed("ffmpeg exited 1: bad codec")

        self.assertTrue(
            self.w.status.currentMessage().startswith("Failed: ffmpeg exited 1")
        )
        self.assertIn("✗ ffmpeg exited 1", self.log_text())
        self.assertTrue(self.w.details_btn.isChecked())
        self.assertTrue(self.w._log_panel.isVisibleTo(self.w))
        self.warn.assert_called_once()


# --- R14: the success slot cannot crash on a vanished output -----------


class SuccessStatSafetyTests(_WinCase):
    def test_success_reports_size_when_the_file_is_there(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "clip.mp4"
            out.write_bytes(b"\x00" * 2048)
            self.w._on_export_done(out)

        self.assertIn("Saved clip.mp4 (2.0 KB)", self.log_text())

    def test_success_survives_the_output_disappearing(self) -> None:
        out = Path("/nonexistent-cove/gone.mp4")
        self.w._on_export_done(out)  # must not raise

        text = self.log_text()
        self.assertIn("gone.mp4", text)
        self.assertNotIn("KB", text)
        self.assertNotIn("MB", text)
        self.assertEqual(self.w.progress.value(), 100)


# --- R15: cancel leaves the UI ready for the next export ---------------


class CancelResetTests(_WinCase):
    def test_reset_after_cancel_restores_export_controls(self) -> None:
        clips = [Clip(asset=_asset(), timeline_start=0.0,
                      src_start=0.0, src_end=5.0)]
        self.w._clips = clips
        for c in clips:
            self.w._assets[c.asset.id] = c.asset
        self.w.timeline.set_clips(self.w._clips)
        self.w._export_thread = object()
        self.w._export_worker = object()
        self.w.export_btn.setEnabled(False)
        self.w.cancel_btn.setEnabled(True)

        self.w._on_export_cancelled()
        # thread.finished -> _reset_after_export, wired by start_export.
        self.w._reset_after_export()

        self.assertIsNone(self.w._export_thread)
        self.assertIsNone(self.w._export_worker)
        self.assertFalse(self.w.cancel_btn.isEnabled())
        self.assertTrue(self.w.export_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
