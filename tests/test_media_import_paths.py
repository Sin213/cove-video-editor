"""Tab 2D: click-to-browse media import + recursive folder import.

Covers the path-expansion helper (`_expand_media_paths`), the batch gate
that guards large imports (`_prepare_import_batch` /
`_confirm_bulk_import`), the file-picker wiring, and the existing
drag-and-drop pathways that must keep working unchanged.

`_expand_media_paths` is a pure function: it takes a mix of files and
folders and returns the ordered, de-duplicated list of supported media
files that should be handed to `_import_paths`. It never shows a dialog,
touches the UI, or probes ffmpeg, so it is tested directly against real
temporary directories.

The app-level tests drive the real `MainWindow` methods against a bare
instance (`MainWindow.__new__`) with narrow fakes at exactly two
boundaries: the dialog calls (`QFileDialog` / `QMessageBox`) and the
lower-level importer (`_import_paths`). Everything between them is
production code.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPointF, QUrl, Qt  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor.app import (  # noqa: E402
    FOLDER_IMPORT_WARN_THRESHOLD,
    MainWindow,
    _expand_media_paths,
)


_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _names(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


class _Recorder:
    """Stand-in for `MainWindow._import_paths`. Records every batch with
    the same shape production receives: (list[Path], append_to_timeline)."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[Path], bool]] = []

    def __call__(self, paths, append_to_timeline: bool = True) -> None:
        self.calls.append((list(paths), append_to_timeline))

    @property
    def imported(self) -> list[Path]:
        out: list[Path] = []
        for paths, _ in self.calls:
            out.extend(paths)
        return out


def _bare_window(recorder: _Recorder | None = None) -> MainWindow:
    """A MainWindow with no Qt construction - enough state for the import
    batch methods, which only touch `_import_paths` and the status bar."""
    win = MainWindow.__new__(MainWindow)
    win._import_paths = recorder or _Recorder()
    win._status_messages = []
    win.status = types.SimpleNamespace(
        showMessage=lambda text, *a, **k: win._status_messages.append(text),
    )
    return win


# ---------------------------------------------------------------- group A
class ExpandFilesTests(unittest.TestCase):
    """A: flat file inputs."""

    def test_a1_single_supported_file(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "movie.mp4")
            self.assertEqual(_expand_media_paths([f]), [f])

    def test_a2_unsupported_file_ignored(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "notes.txt")
            self.assertEqual(_expand_media_paths([f]), [])

    def test_a3_extension_match_is_case_insensitive(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "MOVIE.MP4")
            self.assertEqual(_expand_media_paths([f]), [f])

    def test_a4_mixed_files_keeps_only_supported(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            mp4 = _touch(root / "a.mp4")
            txt = _touch(root / "b.txt")
            mp3 = _touch(root / "c.mp3")
            srt = _touch(root / "d.srt")
            png = _touch(root / "e.png")
            exe = _touch(root / "f.exe")
            got = _expand_media_paths([mp4, txt, mp3, srt, png, exe])
            self.assertEqual(got, [mp4, mp3, srt, png])

    def test_a4b_multiple_dots_and_no_extension(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            dotted = _touch(root / "my.holiday.clip.mp4")
            bare = _touch(root / "README")
            self.assertEqual(_expand_media_paths([dotted, bare]), [dotted])

    def test_a5_nonexistent_path_is_ignored(self) -> None:
        with TemporaryDirectory() as td:
            missing = Path(td) / "gone.mp4"
            real = _touch(Path(td) / "here.mp4")
            # Ignored, not raised: the importer already skips paths that
            # disappear between selection and import.
            self.assertEqual(_expand_media_paths([missing, real]), [real])


# ---------------------------------------------------------------- group B
class ExpandFoldersTests(unittest.TestCase):
    """B: recursive folder expansion."""

    def test_b1_single_folder_filters_unsupported(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "media"
            _touch(root / "a.mp4")
            _touch(root / "b.txt")
            _touch(root / "c.mp3")
            self.assertEqual(_names(_expand_media_paths([root])), ["a.mp4", "c.mp3"])

    def test_b2_nested_file_is_found(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "media"
            _touch(root / "one" / "two" / "three" / "deep.mp4")
            self.assertEqual(_names(_expand_media_paths([root])), ["deep.mp4"])

    def test_b3_multiple_nested_folders(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "media"
            _touch(root / "x" / "x1.mp4")
            _touch(root / "y" / "y1.mp3")
            _touch(root / "y" / "z" / "z1.png")
            _touch(root / "y" / "z" / "skip.txt")
            self.assertEqual(
                _names(_expand_media_paths([root])),
                ["x1.mp4", "y1.mp3", "z1.png"],
            )

    def test_b4_ordering_is_deterministic_not_creation_order(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "media"
            # Deliberately non-sorted creation order, and a mixed-case name
            # to pin the case-insensitive sort.
            _touch(root / "zeta.mp4")
            _touch(root / "sub_b" / "n2.mp4")
            _touch(root / "Alpha.mp4")
            _touch(root / "sub_a" / "n1.mp4")
            _touch(root / "mid.mp4")
            # Files of a directory come before its subdirectories, each
            # level sorted case-insensitively.
            self.assertEqual(
                _names(_expand_media_paths([root])),
                ["Alpha.mp4", "mid.mp4", "zeta.mp4", "n1.mp4", "n2.mp4"],
            )

    @unittest.skipIf(
        os.path.normcase("A") == os.path.normcase("a"),
        "needs a case-sensitive filesystem",
    )
    def test_b4c_case_only_collision_has_a_total_order(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "media"
            # Same name apart from case: the case-insensitive primary key
            # ties, so the raw name must break it rather than the
            # filesystem's enumeration order.
            _touch(root / "b.mp4")
            _touch(root / "a.mp4")
            _touch(root / "A.mp4")
            self.assertEqual(
                _names(_expand_media_paths([root])),
                ["A.mp4", "a.mp4", "b.mp4"],
            )

    def test_b4b_top_level_input_order_is_preserved(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            second = _touch(root / "second.mp4")
            first = _touch(root / "first.mp4")
            self.assertEqual(_expand_media_paths([second, first]), [second, first])

    def test_b5_mixed_file_and_folder_input(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            loose = _touch(root / "loose.mp4")
            folder = root / "folder"
            _touch(folder / "inner.mp3")
            _touch(folder / "inner.txt")
            self.assertEqual(
                _names(_expand_media_paths([loose, folder])),
                ["loose.mp4", "inner.mp3"],
            )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs POSIX FIFOs")
    def test_b7_non_regular_files_are_skipped(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "media"
            real = _touch(root / "real.mp4")
            # A media-named FIFO would block the synchronous ffprobe
            # forever, so extension alone must not be enough.
            os.mkfifo(root / "pipe.mp4")
            self.assertEqual(_expand_media_paths([root]), [real])

    def test_b6_empty_folder_yields_nothing(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "empty"
            (root / "nested").mkdir(parents=True)
            self.assertEqual(_expand_media_paths([root]), [])


# ---------------------------------------------------------------- group C
class DeduplicationTests(unittest.TestCase):
    """C: duplicate suppression within one expansion batch."""

    def test_c1_same_file_listed_twice(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "movie.mp4")
            self.assertEqual(_expand_media_paths([f, f]), [f])

    def test_c2_file_plus_parent_folder(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "folder"
            f = _touch(root / "movie.mp4")
            self.assertEqual(_expand_media_paths([f, root]), [f])
            self.assertEqual(len(_expand_media_paths([root, f])), 1)

    def test_c3_two_folders_with_distinct_media_kept(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a"
            b = root / "b"
            _touch(a / "one.mp4")
            _touch(b / "two.mp4")
            self.assertEqual(_names(_expand_media_paths([a, b])), ["one.mp4", "two.mp4"])

    def test_c4_first_occurrence_wins_and_keeps_position(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "folder"
            inner = _touch(folder / "inner.mp4")
            other = _touch(root / "other.mp4")
            got = _expand_media_paths([inner, other, folder])
            self.assertEqual(got, [inner, other])


# ---------------------------------------------------------------- group D
@unittest.skipIf(sys.platform == "win32", "symlink creation needs privileges on Windows")
class SymlinkSafetyTests(unittest.TestCase):
    """D: traversal must not loop through a directory symlink cycle."""

    def test_d1_directory_symlink_loop_terminates(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "media"
            _touch(root / "real.mp4")
            try:
                os.symlink(root, root / "loop", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:  # pragma: no cover
                self.skipTest(f"symlinks unavailable: {exc}")
            # Terminates, and does not descend through the symlinked dir.
            self.assertEqual(_names(_expand_media_paths([root])), ["real.mp4"])


# ---------------------------------------------------------------- group E
class BulkThresholdTests(unittest.TestCase):
    """E: the >50 confirmation boundary, counted after filtering."""

    def _folder(self, td: str, supported: int, unsupported: int = 0) -> Path:
        root = Path(td) / "batch"
        for i in range(supported):
            _touch(root / f"clip_{i:03d}.mp4")
        for i in range(unsupported):
            _touch(root / f"note_{i:03d}.txt")
        return root

    def _run(self, paths: list[Path]) -> tuple[list, int]:
        rec = _Recorder()
        win = _bare_window(rec)
        with patch.object(
            app_mod.QMessageBox, "question", return_value=QMessageBox.Yes,
        ) as q:
            out = MainWindow._prepare_import_batch(win, paths)
        return out, q.call_count

    def test_threshold_constant_is_fifty(self) -> None:
        self.assertEqual(FOLDER_IMPORT_WARN_THRESHOLD, 50)

    def test_e1_49_files_no_confirmation(self) -> None:
        with TemporaryDirectory() as td:
            out, asked = self._run([self._folder(td, 49)])
            self.assertEqual(len(out), 49)
            self.assertEqual(asked, 0)

    def test_e2_50_files_no_confirmation(self) -> None:
        with TemporaryDirectory() as td:
            out, asked = self._run([self._folder(td, 50)])
            self.assertEqual(len(out), 50)
            self.assertEqual(asked, 0)

    def test_e3_51_files_confirms_exactly_once(self) -> None:
        with TemporaryDirectory() as td:
            out, asked = self._run([self._folder(td, 51)])
            self.assertEqual(len(out), 51)
            self.assertEqual(asked, 1)

    def test_e4_unsupported_files_do_not_count(self) -> None:
        with TemporaryDirectory() as td:
            out, asked = self._run([self._folder(td, 40, unsupported=40)])
            self.assertEqual(len(out), 40)
            self.assertEqual(asked, 0)

    def test_e5_nested_files_count_toward_one_batch(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "batch"
            for i in range(26):
                _touch(root / "a" / f"a{i:03d}.mp4")
            for i in range(26):
                _touch(root / "b" / "c" / f"b{i:03d}.mp4")
            out, asked = self._run([root])
            self.assertEqual(len(out), 52)
            self.assertEqual(asked, 1)

    def test_e6_empty_folder_never_confirms(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "empty"
            root.mkdir()
            out, asked = self._run([root])
            self.assertEqual(out, [])
            self.assertEqual(asked, 0)


# ---------------------------------------------------------------- group F
class ConfirmationCancelAcceptTests(unittest.TestCase):
    """F: no import side effects before the user confirms."""

    def _folder(self, td: str, n: int) -> Path:
        root = Path(td) / "batch"
        for i in range(n):
            _touch(root / f"clip_{i:03d}.mp4")
        return root

    def test_f1_cancel_imports_nothing(self) -> None:
        with TemporaryDirectory() as td:
            root = self._folder(td, 51)
            rec = _Recorder()
            win = _bare_window(rec)
            with patch.object(
                app_mod.QMessageBox, "question", return_value=QMessageBox.No,
            ):
                MainWindow._on_bin_files_dropped(win, [str(root)])
            self.assertEqual(rec.calls, [])

    def test_f2_accept_imports_all_once(self) -> None:
        with TemporaryDirectory() as td:
            root = self._folder(td, 51)
            rec = _Recorder()
            win = _bare_window(rec)
            with patch.object(
                app_mod.QMessageBox, "question", return_value=QMessageBox.Yes,
            ):
                MainWindow._on_bin_files_dropped(win, [str(root)])
            self.assertEqual(len(rec.calls), 1)
            self.assertEqual(len(rec.imported), 51)
            self.assertEqual(len(set(rec.imported)), 51)
            self.assertFalse(rec.calls[0][1])  # append_to_timeline=False

    def test_f3_confirmation_text_carries_the_supported_count(self) -> None:
        with TemporaryDirectory() as td:
            root = self._folder(td, 51)
            for i in range(5):
                _touch(root / f"note_{i}.txt")
            win = _bare_window()
            with patch.object(
                app_mod.QMessageBox, "question", return_value=QMessageBox.No,
            ) as q:
                MainWindow._prepare_import_batch(win, [root])
            body = q.call_args[0][2]
            self.assertIn("51", body)
            self.assertNotIn("56", body)


# ---------------------------------------------------------------- group H
class PickerWiringTests(unittest.TestCase):
    """H: native multi-file picker feeds the existing import pipeline."""

    def _browse_win(self, selected: list[str], kind: str = "video") -> MainWindow:
        win = _bare_window()
        with patch.object(
            app_mod.QFileDialog,
            "getOpenFileNames",
            return_value=(list(selected), ""),
        ), patch.object(
            app_mod.QMessageBox, "question", return_value=QMessageBox.Yes,
        ):
            MainWindow._on_browse_requested(win, kind)
        return win

    def _browse(self, selected: list[str], kind: str = "video") -> _Recorder:
        rec = _Recorder()
        win = _bare_window(rec)
        with patch.object(
            app_mod.QFileDialog,
            "getOpenFileNames",
            return_value=(list(selected), ""),
        ), patch.object(
            app_mod.QMessageBox, "question", return_value=QMessageBox.Yes,
        ):
            MainWindow._on_browse_requested(win, kind)
        return rec

    def test_h1_cancelled_picker_imports_nothing(self) -> None:
        self.assertEqual(self._browse([]).calls, [])

    def test_h2_single_file(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "one.mp4")
            rec = self._browse([str(f)])
            self.assertEqual(rec.imported, [f])
            self.assertFalse(rec.calls[0][1])

    def test_h3_multiple_files_preserve_selection_order(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            a = _touch(root / "a.mp4")
            b = _touch(root / "b.mp3")
            c = _touch(root / "c.png")
            rec = self._browse([str(b), str(a), str(c)])
            self.assertEqual(rec.imported, [b, a, c])

    def test_h4_unsupported_picker_result_is_filtered_out(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            good = _touch(root / "good.mp4")
            bad = _touch(root / "bad.txt")
            rec = self._browse([str(bad), str(good)])
            self.assertEqual(rec.imported, [good])

    def test_h6_no_unverified_success_count_is_reported(self) -> None:
        # `_import_paths` silently skips files it cannot open, so the
        # number of selected files must never be announced as imported.
        with TemporaryDirectory() as td:
            root = Path(td)
            a = _touch(root / "a.mp4")
            b = _touch(root / "b.mp4")
            win = self._browse_win([str(a), str(b)])
            for msg in win._status_messages:
                self.assertNotIn("2", msg)

    def test_h5_picker_filter_covers_every_supported_extension(self) -> None:
        seen: dict[str, str] = {}

        def _fake(parent, caption, directory, filters):  # noqa: ANN001
            seen["filters"] = filters
            return ([], "")

        win = _bare_window()
        with patch.object(app_mod.QFileDialog, "getOpenFileNames", _fake):
            MainWindow._on_browse_requested(win, "video")
        for ext in app_mod.MEDIA_EXTS:
            self.assertIn(f"*{ext}", seen["filters"])


# ---------------------------------------------------------------- group I
class ExistingDropRegressionTests(unittest.TestCase):
    """I: the drag-and-drop pathways that already worked keep working."""

    def test_i1_clip_bin_single_supported_file_drop(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "clip.mp4")
            rec = _Recorder()
            win = _bare_window(rec)
            with patch.object(app_mod.QMessageBox, "question") as q:
                MainWindow._on_bin_files_dropped(win, [str(f)])
            self.assertEqual(rec.calls, [([f], False)])
            self.assertEqual(q.call_count, 0)

    def test_i2_unsupported_file_drop_imports_nothing(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "notes.txt")
            rec = _Recorder()
            win = _bare_window(rec)
            with patch.object(app_mod.QMessageBox, "question") as q:
                MainWindow._on_bin_files_dropped(win, [str(f)])
            self.assertEqual(rec.calls, [])
            self.assertEqual(q.call_count, 0)

    def test_i3_multiple_supported_files_keep_batch_and_order(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            b = _touch(root / "b.mp4")
            a = _touch(root / "a.mp3")
            rec = _Recorder()
            win = _bare_window(rec)
            MainWindow._on_bin_files_dropped(win, [str(b), str(a)])
            self.assertEqual(rec.calls, [([b, a], False)])

    # --- main-window drops -------------------------------------------

    def _drop(self, win: MainWindow, paths: list[Path]) -> None:
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        ev = QDropEvent(
            QPointF(1.0, 1.0), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier,
        )
        MainWindow.dropEvent(win, ev)

    def _window(self) -> tuple[MainWindow, _Recorder, list[Path]]:
        rec = _Recorder()
        win = _bare_window(rec)
        audio: list[Path] = []
        win._append_added_audio = audio.append
        return win, rec, audio

    def test_i4_window_video_drop_still_appends_to_timeline(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "clip.mp4")
            win, rec, audio = self._window()
            self._drop(win, [f])
            self.assertEqual(rec.calls, [([f], True)])
            self.assertEqual(audio, [])

    def test_i5_window_audio_drop_still_routes_to_added_audio(self) -> None:
        with TemporaryDirectory() as td:
            f = _touch(Path(td) / "song.mp3")
            win, rec, audio = self._window()
            self._drop(win, [f])
            self.assertEqual(rec.calls, [])
            self.assertEqual(audio, [f])

    def test_i6_window_folder_drop_expands_recursively(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "folder"
            vid = _touch(root / "nested" / "clip.mp4")
            song = _touch(root / "song.mp3")
            _touch(root / "notes.txt")
            win, rec, audio = self._window()
            self._drop(win, [root])
            self.assertEqual(rec.imported, [vid])
            self.assertEqual(audio, [song])

    def test_i7_window_folder_drop_cancel_imports_nothing(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td) / "folder"
            for i in range(51):
                _touch(root / f"clip_{i:03d}.mp4")
            _touch(root / "song.mp3")
            win, rec, audio = self._window()
            with patch.object(
                app_mod.QMessageBox, "question", return_value=QMessageBox.No,
            ) as q:
                self._drop(win, [root])
            self.assertEqual(q.call_count, 1)
            self.assertEqual(rec.calls, [])
            self.assertEqual(audio, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
