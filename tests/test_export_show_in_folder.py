"""Tab 2L: a success-only "Show in folder" action.

Tab 2K gave export three distinct terminal outcomes - ``finished``,
``cancelled`` and ``failed``. This slice hangs exactly one affordance off
the *success* one: after an export completes, the user can open the
directory that received the file, without hunting for it in a file
manager.

The whole risk of the feature is staleness. A button that survives into
the next export run points at yesterday's file, and after a cancellation
it would point at a partial destination Tab 2K deliberately leaves on
disk. So the tests below spend most of their weight on the *absence* of
the action: fresh window, export start, cancellation, failure. Only a
genuinely completed export may expose it, and only for that exact output.

Determinism: no sleeps, no ffmpeg, no encoder probing. Success, cancel
and failure handlers are the real ones; ``start_export`` and the save
dialog are replaced only where a test needs to open a *new* export run,
and ``_open_local`` is patched at the seam ``app`` actually imports so a
click never spawns a file manager on the test machine.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402
from cove_video_editor.clip import Clip, MediaAsset  # noqa: E402

_app: QApplication | None = None

REPO = Path(__file__).resolve().parents[1]


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --- fakes -------------------------------------------------------------


class FakeWorker(QObject):
    """Same signal surface ``MainWindow`` connects to on ``start_export``.

    Models the success shape: every terminal signal exists, so the window
    wires up exactly as it does against a real ``ExportWorker``.
    """

    progress = Signal(int)
    eta = Signal(float)
    log = Signal(str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def cancel(self) -> None:
        pass


class FakeThread(QObject):
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.started = 0

    def start(self) -> None:
        self.started += 1


def _asset(name: str = "a.mp4") -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=60.0, width=1280, height=720,
        fps=30.0, has_audio=True, kind="video",
    )


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
        self.info = unittest.mock.patch.object(
            app_mod.QMessageBox, "information", return_value=None,
        ).start()
        self.opener = unittest.mock.patch.object(app_mod, "_open_local").start()
        self.addCleanup(unittest.mock.patch.stopall)
        # Registered last, so it runs *first*: production wires the worker
        # with Qt.QueuedConnection, and an undelivered emit would otherwise
        # land in whichever later test next spins the event loop.
        self.addCleanup(self.drain)

    def drain(self) -> None:
        """Deliver queued signal emissions before asserting on their effect."""
        QApplication.processEvents()

    def log_text(self) -> str:
        return self.w.export_log.toPlainText()

    def btn(self):
        # ``isVisible()`` is False for every child of an unshown window, so
        # visibility is asked relative to the window that owns the button.
        return self.w.show_folder_btn

    def btn_shown(self) -> bool:
        return self.w.show_folder_btn.isVisibleTo(self.w)

    def make_output(self, name: str = "render.mp4") -> Path:
        """A real file in a real temp directory, torn down with the test."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        out = Path(td.name) / name
        out.write_bytes(b"\x00" * 4096)
        return out

    def succeed(self, name: str = "render.mp4") -> Path:
        out = self.make_output(name)
        self.w._on_export_done(out)
        return out

    def load_clip(self) -> None:
        clips = [Clip(asset=_asset(), timeline_start=0.0,
                      src_start=0.0, src_end=5.0)]
        self.w._clips = clips
        for c in clips:
            self.w._assets[c.asset.id] = c.asset
        self.w.timeline.set_clips(self.w._clips)

    def start_export(self, dest: str = "/tmp/cove-next/out.mp4") -> FakeWorker:
        """Drive the real export-start path with the I/O edges replaced.

        Everything between the click and ``thread.start()`` is production
        code; only the save dialog and the thread/worker pair are faked.
        """
        self.load_clip()
        worker, thread = FakeWorker(), FakeThread()
        with unittest.mock.patch.object(
            app_mod.QFileDialog, "getSaveFileName", return_value=(dest, ""),
        ), unittest.mock.patch.object(
            app_mod, "start_export", return_value=(thread, worker),
        ):
            self.w._on_export_clicked()
        self.assertEqual(thread.started, 1, "export did not actually start")
        return worker


# --- Group A: initial state --------------------------------------------


class InitialStateTests(_WinCase):
    def test_fresh_window_has_no_recorded_export_output(self) -> None:
        self.assertIsNone(self.w._last_export_output)

    def test_fresh_window_hides_the_show_in_folder_action(self) -> None:
        self.assertFalse(self.btn_shown())

    def test_action_is_labelled_show_in_folder(self) -> None:
        self.assertEqual(self.btn().text(), "Show in folder")

    def test_clicking_before_any_success_cannot_open_anything(self) -> None:
        self.w._on_show_in_folder()
        self.opener.assert_not_called()


# --- Group B: success ---------------------------------------------------


class SuccessTests(_WinCase):
    def test_success_records_the_exact_output(self) -> None:
        out = self.succeed()
        self.assertEqual(self.w._last_export_output, out)

    def test_success_shows_and_enables_the_action(self) -> None:
        self.succeed()
        self.assertTrue(self.btn_shown())
        self.assertTrue(self.btn().isEnabled())

    def test_success_status_is_unchanged(self) -> None:
        out = self.succeed()
        self.assertEqual(
            self.w.status.currentMessage(), f"Saved {out.name} (4.0 KB)",
        )
        self.assertIn(f"✓ Saved {out.name} (4.0 KB)", self.log_text())

    def test_success_does_not_open_anything_automatically(self) -> None:
        self.succeed()
        self.opener.assert_not_called()

    def test_success_shows_no_modal(self) -> None:
        self.succeed()
        self.info.assert_not_called()
        self.warn.assert_not_called()

    def test_success_with_an_unstattable_output_still_offers_the_folder(self) -> None:
        # Tab 2K: a vanished file must not break success reporting. The
        # export still happened, so the folder is still worth offering.
        out = Path("/nonexistent-cove/gone.mp4")
        self.w._on_export_done(out)
        self.assertEqual(self.w._last_export_output, out)
        self.assertTrue(self.btn_shown())


# --- Group C: click semantics ------------------------------------------


class ClickTests(_WinCase):
    def test_click_opens_the_containing_directory(self) -> None:
        out = self.succeed()
        self.btn().click()

        self.opener.assert_called_once()
        (target,), _ = self.opener.call_args
        self.assertEqual(Path(target), out.parent)

    def test_click_does_not_open_the_media_file(self) -> None:
        out = self.succeed()
        self.btn().click()

        (target,), _ = self.opener.call_args
        self.assertNotEqual(Path(target), out)

    def test_one_click_opens_exactly_once(self) -> None:
        self.succeed()
        self.btn().click()
        self.assertEqual(self.opener.call_count, 1)

    def test_click_leaves_export_state_untouched(self) -> None:
        out = self.succeed()
        before = (self.w._export_thread, self.w._export_worker,
                  self.w.progress.value(), self.w.export_btn.isEnabled(),
                  self.w.cancel_btn.isEnabled(), self.log_text())

        self.btn().click()

        self.assertEqual(self.w._last_export_output, out)
        self.assertEqual(
            (self.w._export_thread, self.w._export_worker,
             self.w.progress.value(), self.w.export_btn.isEnabled(),
             self.w.cancel_btn.isEnabled(), self.log_text()),
            before,
        )


# --- Group D: a new export clears the stale success ---------------------


class NewExportClearsTests(_WinCase):
    def test_starting_a_new_export_clears_the_previous_success(self) -> None:
        self.succeed()
        self.start_export()

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.btn_shown())

    def test_a_cancelled_second_export_does_not_resurrect_the_first(self) -> None:
        self.succeed()
        worker = self.start_export()
        worker.cancelled.emit()
        self.drain()  # production connects terminal signals queued
        self.assertIn("Export cancelled", self.log_text())

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.btn_shown())

    def test_a_failed_second_export_does_not_resurrect_the_first(self) -> None:
        self.succeed()
        worker = self.start_export()
        worker.failed.emit("ffmpeg exploded")
        self.drain()  # production connects terminal signals queued
        self.assertIn("ffmpeg exploded", self.log_text())

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.btn_shown())

    def test_a_successful_second_export_points_at_the_second_output(self) -> None:
        self.succeed("first.mp4")
        self.start_export()
        second = self.make_output("second.mp4")
        self.w._on_export_done(second)

        self.assertEqual(self.w._last_export_output, second)
        self.assertTrue(self.btn_shown())


# --- Group E: cancellation ----------------------------------------------


class CancelTests(_WinCase):
    def test_cancellation_without_a_prior_success_exposes_nothing(self) -> None:
        self.w._on_export_cancelled()

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.btn_shown())

    def test_cancellation_never_surfaces_the_partial_destination(self) -> None:
        # Tab 2K leaves a cancelled partial file on disk on purpose. It
        # must not become a reveal target.
        self.succeed()
        with tempfile.TemporaryDirectory() as td:
            partial = Path(td) / "partial.mp4"
            partial.write_bytes(b"\x00" * 16)
            self.start_export(str(partial))
            self.w._on_export_cancelled()

            self.assertIsNone(self.w._last_export_output)
            self.assertFalse(self.btn_shown())

    def test_cancellation_ux_is_still_neutral(self) -> None:
        self.w._on_export_cancelled()

        self.assertEqual(self.w.status.currentMessage(), "Export cancelled")
        self.assertNotIn("✗", self.log_text())
        self.warn.assert_not_called()


# --- Group F: failure ---------------------------------------------------


class FailureTests(_WinCase):
    def test_failure_without_a_prior_success_exposes_nothing(self) -> None:
        self.w._on_export_failed("boom")

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.btn_shown())

    def test_failure_after_a_success_run_exposes_nothing(self) -> None:
        self.succeed()
        self.start_export()
        self.w._on_export_failed("boom")

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.btn_shown())

    def test_failure_ux_is_unchanged(self) -> None:
        self.w._on_export_failed("boom")

        self.assertEqual(self.w.status.currentMessage(), "Failed: boom")
        self.assertIn("✗ boom", self.log_text())
        self.assertTrue(self.w.details_btn.isChecked())
        self.warn.assert_called_once()


# --- Group G: repeated success ------------------------------------------


class RepeatedSuccessTests(_WinCase):
    def test_the_newest_success_replaces_the_older_target(self) -> None:
        first = self.succeed("a.mp4")
        second = self.succeed("b.mp4")
        self.assertNotEqual(first.parent, second.parent)

        self.btn().click()

        self.opener.assert_called_once()
        (target,), _ = self.opener.call_args
        self.assertEqual(Path(target), second.parent)

    def test_no_stale_connection_reopens_the_first_output(self) -> None:
        first = self.succeed("a.mp4")
        self.succeed("b.mp4")
        self.btn().click()

        opened = [Path(c.args[0]) for c in self.opener.call_args_list]
        self.assertNotIn(first.parent, opened)


# --- Group H: the file went away, the folder did not --------------------


class OutputRemovedTests(_WinCase):
    def test_a_deleted_output_still_opens_its_folder(self) -> None:
        out = self.succeed()
        out.unlink()

        self.btn().click()

        (target,), _ = self.opener.call_args
        self.assertEqual(Path(target), out.parent)

    def test_a_renamed_output_still_opens_its_folder(self) -> None:
        out = self.succeed()
        out.rename(out.with_name("moved.mp4"))

        self.btn().click()

        (target,), _ = self.opener.call_args
        self.assertEqual(Path(target), out.parent)


# --- Group I: the folder itself went away -------------------------------


class DirectoryRemovedTests(_WinCase):
    def _succeed_then_remove_folder(self) -> Path:
        td = tempfile.TemporaryDirectory()
        out = Path(td.name) / "render.mp4"
        out.write_bytes(b"\x00" * 32)
        self.w._on_export_done(out)
        td.cleanup()
        return out

    def test_missing_folder_does_not_crash_and_does_not_open(self) -> None:
        # Called directly, not through ``click()``: PySide6 swallows an
        # exception raised inside a slot, so a click could never prove
        # "does not raise".
        self._succeed_then_remove_folder()
        self.w._on_show_in_folder()  # must not raise
        self.opener.assert_not_called()

    def test_missing_folder_reports_non_modally(self) -> None:
        self._succeed_then_remove_folder()
        self.btn().click()

        self.assertEqual(
            self.w.status.currentMessage(),
            "Export folder is no longer available",
        )
        self.assertIn("Export folder is no longer available", self.log_text())
        self.info.assert_not_called()
        self.warn.assert_not_called()

    def test_missing_folder_is_not_reported_as_an_export_failure(self) -> None:
        self._succeed_then_remove_folder()
        self.btn().click()

        self.assertNotIn("Export failed", self.log_text())
        self.assertNotIn("Failed:", self.w.status.currentMessage())


# --- Group J: the open helper failed ------------------------------------


class OpenHelperFailureTests(_WinCase):
    def setUp(self) -> None:
        super().setUp()
        self.opener.side_effect = OSError("no file manager")

    def test_helper_failure_does_not_crash(self) -> None:
        self.succeed()
        self.btn().click()  # must not raise

    def test_helper_failure_reports_non_modally(self) -> None:
        self.succeed()
        self.btn().click()

        self.assertEqual(
            self.w.status.currentMessage(), "Could not open export folder",
        )
        self.assertIn("Could not open export folder", self.log_text())
        self.info.assert_not_called()
        self.warn.assert_not_called()

    def test_helper_failure_leaves_the_action_usable_for_a_retry(self) -> None:
        out = self.succeed()
        self.btn().click()

        self.assertEqual(self.w._last_export_output, out)
        self.assertTrue(self.btn_shown())
        self.assertTrue(self.btn().isEnabled())

        self.opener.side_effect = None
        self.btn().click()
        (target,), _ = self.opener.call_args
        self.assertEqual(Path(target), out.parent)


# --- Group K: never automatic -------------------------------------------


class NoAutoOpenTests(_WinCase):
    def test_the_success_handler_never_opens_a_folder(self) -> None:
        self.succeed()
        self.start_export()
        self.w._on_export_done(self.make_output("again.mp4"))

        self.opener.assert_not_called()

    def test_no_terminal_handler_opens_a_folder(self) -> None:
        self.succeed()
        self.w._on_export_cancelled()
        self.w._on_export_failed("boom")

        self.opener.assert_not_called()


# --- Group L: terminal outcomes stay separated --------------------------


class TerminalSeparationTests(_WinCase):
    def test_success_shows_cancel_hides_failure_hides(self) -> None:
        self.succeed()
        self.assertTrue(self.btn_shown())

        self.start_export()
        self.w._on_export_cancelled()
        self.assertFalse(self.btn_shown())

        self.succeed()
        self.assertTrue(self.btn_shown())

        self.start_export()
        self.w._on_export_failed("boom")
        self.assertFalse(self.btn_shown())


# --- Group M: production scope ------------------------------------------


class ProductionScopeTests(unittest.TestCase):
    """Tab 2L is an ``app.py``-only slice by construction."""

    FORBIDDEN = (
        "src/cove_video_editor/exporter.py",
        "src/cove_video_editor/system_open.py",
        "src/cove_video_editor/ffmpeg_utils.py",
        "src/cove_video_editor/clip.py",
        "src/cove_video_editor/timeline_widget.py",
        "src/cove_video_editor/theme.py",
        "src/cove_video_editor/titlebar.py",
    )

    def test_no_forbidden_production_file_is_modified(self) -> None:
        try:
            out = subprocess.run(
                ["git", "diff", "--name-only", "HEAD", "--", *self.FORBIDDEN],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f"git unavailable: {exc}")
        if out.returncode != 0:
            self.skipTest("not a git worktree")
        self.assertEqual(out.stdout.strip(), "")

    def test_the_open_helper_is_only_called_from_the_reveal_action(self) -> None:
        src = (REPO / "src/cove_video_editor/app.py").read_text()
        callers = [ln.strip() for ln in src.splitlines() if "_open_local(" in ln]
        self.assertTrue(callers, "app.py never calls the local-open helper")
        self.assertTrue(
            all(ln.startswith("_open_local(") or "import" in ln for ln in callers),
            f"unexpected _open_local call sites: {callers}",
        )


if __name__ == "__main__":
    unittest.main()
