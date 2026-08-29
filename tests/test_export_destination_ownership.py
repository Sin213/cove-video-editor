"""Tab 2M: every export run owns its own temporary output.

Tab 2K gave export three distinct terminal outcomes and then deliberately
stopped short of cleaning anything up. The reason was ownership: a
path-level existence snapshot cannot prove that whatever occupies
``job.output`` when cleanup runs is still the partial *this* run wrote, so
deleting it risked destroying a file Cove never created. The limitation
Cove accepted instead was a partial destination left behind after a
cancellation.

This slice removes the limitation by removing the guesswork. ffmpeg never
writes to the user's destination at all. Each run allocates one unique
sibling temp in the destination's own directory, encodes into that, and
only a run that fully succeeded promotes it onto the destination with a
single atomic ``os.replace``. Cleanup is then trivially safe because it
only ever names a path this run invented.

The tests below are mostly about what must *not* happen: the destination
must survive a cancellation, a failure, and a promotion error byte for
byte, and a file some other actor drops at the destination mid-export must
survive too. Determinism: no sleeps and no real ffmpeg - the fake child
writes to whichever path the command actually names, so routing the
encode at the destination shows up as a test failure rather than as a
silently different filesystem effect. Real temporary directories are used
throughout because the ownership semantics under test *are* filesystem
semantics.
"""
from __future__ import annotations

import inspect
import os
import re
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cove_video_editor import app as app_mod  # noqa: E402
from cove_video_editor import exporter as exporter_mod  # noqa: E402
from cove_video_editor import ffmpeg_utils as ff  # noqa: E402
from cove_video_editor.app import MainWindow  # noqa: E402
from cove_video_editor.clip import Clip, MediaAsset  # noqa: E402
from cove_video_editor.exporter import (  # noqa: E402
    TEMP_MARKER,
    ExportJob,
    ExportWorker,
    owned_temp_output,
)

_app: QApplication | None = None

MP4 = "MP4 (H.264 + AAC)"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --- fakes -------------------------------------------------------------


class _Stdout:
    """ffmpeg's ``-progress pipe:1`` stream, line by line.

    ``hooks[i]`` runs *before* the line at that index reaches the worker,
    which places an action at an exact point of the encode with no timing
    dependency at all.
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

    Models the success shape production expects: ``wait()`` returns an
    int return code, ``poll()`` reports liveness, and both pipes are
    iterable text streams.
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


PROGRESS = ["out_time_us=1000000", "out_time_us=2000000", "progress=end"]


def _asset(name: str = "a.mp4") -> MediaAsset:
    return MediaAsset(
        path=Path(name), duration=60.0, width=1280, height=720,
        fps=30.0, has_audio=True, kind="video",
    )


def _job(out: Path, fmt_key: str = MP4, **kw) -> ExportJob:
    clips = [Clip(asset=_asset(), timeline_start=0.0, src_start=0.0, src_end=5.0)]
    return ExportJob(clips=clips, output=out, fmt_key=fmt_key, **kw)


class _Spawned:
    """The export command the worker actually handed to ``Popen``."""

    def __init__(self) -> None:
        self.cmds: list[list[str]] = []

    @property
    def cmd(self) -> list[str]:
        assert len(self.cmds) == 1, f"expected one export spawn, got {len(self.cmds)}"
        return self.cmds[0]

    @property
    def dest(self) -> Path:
        return Path(self.cmd[-1])


def _run(worker: ExportWorker, proc: FakeProc | None = None,
         *, writes: bytes | None = b"NEW",
         popen_exc: BaseException | None = None,
         on_spawn: object = None) -> _Spawned:
    """Run the worker with the *export* ffmpeg replaced by ``proc``.

    ``writes`` is the fake child's filesystem effect and it lands on
    whichever path the command actually names. That is the point: a build
    that still pointed ffmpeg at the user's destination would write there,
    and the ownership assertions below would catch it. Pass ``None`` for a
    child that produces nothing.

    Only the encode itself is faked. Command construction legitimately
    shells out (encoder capability probing), so anything that is not the
    ``-progress pipe:1`` export command goes to the real ``Popen``.
    """
    spawned = _Spawned()
    real_popen = exporter_mod.subprocess.Popen

    def _spawn(cmd, *a, **kw):
        if not (isinstance(cmd, list) and "-progress" in cmd):
            return real_popen(cmd, *a, **kw)
        spawned.cmds.append(list(cmd))
        if popen_exc is not None:
            raise popen_exc
        if writes is not None:
            Path(cmd[-1]).write_bytes(writes)
        if on_spawn is not None:
            on_spawn()
        return proc

    with unittest.mock.patch.object(
        exporter_mod.subprocess, "Popen", side_effect=_spawn,
    ):
        worker.run()
    return spawned


class _Dest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cove-own-")
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        self.out = self.dir / "movie.mp4"

    def siblings(self) -> list[Path]:
        return sorted(p for p in self.dir.iterdir() if p != self.out)


# --- Group A: the shape of the run-owned temp --------------------------


class GroupATempShape(_Dest):
    def test_a1_the_temp_is_a_sibling_of_the_destination(self) -> None:
        """Promotion has to be a same-filesystem replace, so the temp
        cannot live in /tmp, a cache dir, or anywhere else."""
        worker = ExportWorker(_job(self.out))
        self.assertEqual(worker._temp_output.parent, self.out.parent)

    def test_a1b_a_relative_destination_keeps_its_own_directory(self) -> None:
        temp = owned_temp_output(Path("out.wav"))
        self.assertEqual(temp.parent, Path("out.wav").parent)

    def test_a2_the_temp_keeps_the_destination_suffix(self) -> None:
        """ffmpeg infers the muxer from the output suffix, so a temp named
        ``movie.mp4.tmp`` would pick the wrong format - or none."""
        worker = ExportWorker(_job(self.out))
        self.assertEqual(worker._temp_output.suffix, ".mp4")

    def test_a3_two_runs_at_the_same_destination_get_different_temps(self) -> None:
        a = ExportWorker(_job(self.out))._temp_output
        b = ExportWorker(_job(self.out))._temp_output
        self.assertNotEqual(a, b)

    def test_a3b_the_token_is_not_a_predictable_counter(self) -> None:
        """A counter restarts with the process; two runs of Cove targeting
        the same destination would then collide."""
        tokens = {owned_temp_output(self.out).name for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for name in tokens:
            self.assertGreaterEqual(len(name), len(".movie.cove-export-.mp4") + 8)

    def test_a4_the_temp_is_recognizably_cove_owned_and_unobtrusive(self) -> None:
        name = ExportWorker(_job(self.out))._temp_output.name
        self.assertIn("cove-export", name)
        self.assertTrue(name.startswith("."), name)
        self.assertIn("movie", name)

    def test_a6_a_long_destination_name_still_yields_a_usable_temp(self) -> None:
        """The temp basename adds ~26 bytes to the stem. A destination
        name that is itself near the 255-byte component limit is perfectly
        valid, and must not become unexportable because of the decoration.
        """
        stem = "h" * 240
        out = self.dir / f"{stem}.mp4"
        temp = owned_temp_output(out)

        self.assertLessEqual(len(temp.name.encode()), 255)
        self.assertEqual(temp.suffix, ".mp4")
        self.assertIn(TEMP_MARKER, temp.name)
        self.assertEqual(temp.parent, self.dir)
        # The name has to be usable, not merely short enough on paper.
        temp.write_bytes(b"NEW")
        self.assertTrue(temp.exists())

    def test_a7_a_long_destination_name_is_still_unique_per_run(self) -> None:
        """Truncating the stem must not truncate away the random token."""
        out = self.dir / ("h" * 240 + ".mp4")
        names = {owned_temp_output(out).name for _ in range(50)}
        self.assertEqual(len(names), 50)

    def test_a8_a_long_destination_exports_end_to_end(self) -> None:
        out = self.dir / ("h" * 240 + ".mp4")
        worker = ExportWorker(_job(out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(out.read_bytes(), b"NEW")

    def test_a9_a_long_suffix_is_bounded_too_not_just_the_stem(self) -> None:
        """``a.<253 bytes>`` is a valid 255-byte destination name. Bounding
        only the stem still overruns the limit, because the suffix is
        carried whole."""
        out = self.dir / ("a." + "x" * 253)
        temp = owned_temp_output(out)

        self.assertLessEqual(len(temp.name.encode()), 255)
        self.assertIn(TEMP_MARKER, temp.name)
        temp.write_bytes(b"NEW")
        self.assertTrue(temp.exists())

    def test_a10_a_suffix_that_fits_is_never_truncated(self) -> None:
        """The suffix outranks the stem: ffmpeg's muxer inference depends
        on it, so it may only give way when it alone breaks the limit."""
        for stem_len in (5, 200, 240, 250):
            with self.subTest(stem=stem_len):
                out = self.dir / ("s" * stem_len + ".webm")
                temp = owned_temp_output(out)
                self.assertEqual(temp.suffix, ".webm")
                self.assertLessEqual(len(temp.name.encode()), 255)

    def test_a11_a_dotfile_destination_keeps_its_container_extension(self) -> None:
        """``Path(".mp4").suffix`` is empty - POSIX reads that name as a
        hidden file called "mp4". ffmpeg does not: it infers the muxer
        from the text after the last dot, so the temp has to carry it or
        the export loses its container.
        """
        for name, tail in ((".mp4", ".mp4"), (".webm", ".webm")):
            with self.subTest(name=name):
                temp = owned_temp_output(self.dir / name)
                self.assertTrue(temp.name.endswith(tail), temp.name)
                self.assertIn(TEMP_MARKER, temp.name)
                self.assertTrue(temp.name.startswith("."), temp.name)
                self.assertEqual(temp.parent, self.dir)

    def test_a12_a_dotfile_destination_exports_end_to_end(self) -> None:
        out = self.dir / ".mp4"
        worker = ExportWorker(_job(out))
        seen = Outcomes(worker)
        spawned = _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertTrue(spawned.cmd[-1].endswith(".mp4"))
        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(out.read_bytes(), b"NEW")

    def test_a13_a_suffixless_destination_is_left_alone(self) -> None:
        """No dot means no container hint either way - the temp must not
        invent one."""
        temp = owned_temp_output(self.dir / "movie")
        self.assertIn(TEMP_MARKER, temp.name)
        self.assertTrue(temp.name.startswith(".movie."), temp.name)

    def test_a17_the_component_limit_comes_from_the_destination_filesystem(
            self) -> None:
        """255 is not universal. A mount with a smaller limit can accept
        the destination the user chose and then reject the decorated temp
        name, failing an export that has nothing wrong with it."""
        with unittest.mock.patch.object(
            exporter_mod.os, "pathconf", lambda *_a: 64,
        ):
            temp = owned_temp_output(self.dir / ("h" * 40 + ".mp4"))

        self.assertLessEqual(len(temp.name.encode()), 64)
        self.assertEqual(temp.suffix, ".mp4")
        self.assertIn(TEMP_MARKER, temp.name)

    def test_a18_an_unreadable_limit_falls_back_to_the_portable_one(self) -> None:
        """`pathconf` is absent on Windows and can fail anywhere, which is
        not a reason to refuse to export."""
        for boom in (OSError("nope"), ValueError("bad name"),
                     AttributeError("no pathconf")):
            with self.subTest(error=type(boom).__name__):
                with unittest.mock.patch.object(
                    exporter_mod.os, "pathconf", side_effect=boom,
                ):
                    temp = owned_temp_output(self.dir / ("h" * 240 + ".mp4"))
                self.assertLessEqual(len(temp.name.encode()), 255)
                self.assertEqual(temp.suffix, ".mp4")

    def test_a14_the_temp_path_is_claimed_atomically(self) -> None:
        """An unguessable name is not ownership. Something already at the
        path means this run did not create it, so it must not be written
        over, promoted, or deleted."""
        worker = ExportWorker(_job(self.out))
        worker._temp_output.write_bytes(b"SOMEONE ELSE'S FILE")
        seen = Outcomes(worker)
        spawned = _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(spawned.cmds, [], "ffmpeg ran against a foreign file")
        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(worker._temp_output.read_bytes(), b"SOMEONE ELSE'S FILE")
        self.assertFalse(self.out.exists())

    def test_a15_a_planted_symlink_is_refused_not_followed(self) -> None:
        """The attack the reservation exists to stop: ffmpeg's ``-y``
        would happily write through a symlink to somewhere else."""
        elsewhere = self.dir / "victim.txt"
        elsewhere.write_bytes(b"UNRELATED")
        worker = ExportWorker(_job(self.out))
        worker._temp_output.symlink_to(elsewhere)
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(elsewhere.read_bytes(), b"UNRELATED")
        self.assertTrue(worker._temp_output.is_symlink(),
                        "a link this run did not create was removed")

    def test_a19_a_close_failure_still_leaves_the_reservation_owned(self) -> None:
        """The file is ours the moment ``open`` and ``fstat`` succeed. If
        closing the descriptor then fails, forgetting that would strand a
        hidden file in the user's folder that nothing will ever remove."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        real_open, real_close = os.open, os.close
        reserved: list[int] = []

        def _open(path, *a, **k):
            fd = real_open(path, *a, **k)
            if Path(path) == worker._temp_output:
                reserved.append(fd)
            return fd

        def _close(fd):
            real_close(fd)
            # Only the reservation's own descriptor, and only once: fd
            # numbers are reused, so a later close of an unrelated file
            # would otherwise be failed too.
            if fd in reserved:
                reserved.remove(fd)
                raise OSError(5, "Input/output error")

        with unittest.mock.patch.object(exporter_mod.os, "open", _open), \
                unittest.mock.patch.object(exporter_mod.os, "close", _close):
            _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(seen.order, ["failed"])
        self.assertIsNotNone(worker._temp_identity)
        self.assertFalse(worker._temp_output.exists(),
                         "the reservation was left behind")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a16_an_unreserved_run_deletes_nothing_and_says_nothing(self) -> None:
        """Cleanup must be inert until a reservation proves ownership.

        Silent, too: a run that never reserved anything has no business
        reporting that some file "is no longer the one it created" - it
        never created one, and the message would only confuse.
        """
        worker = ExportWorker(_job(self.out))
        logged: list[str] = []
        worker.log.connect(logged.append)
        worker._temp_output.write_bytes(b"SOMEONE ELSE'S FILE")

        worker._discard_temp()

        self.assertEqual(worker._temp_output.read_bytes(), b"SOMEONE ELSE'S FILE")
        self.assertEqual(logged, [])

    def test_a5_allocating_the_temp_creates_no_file(self) -> None:
        """Construction must not litter the user's folder; ffmpeg's own
        ``-y`` creates the file when the encode starts."""
        worker = ExportWorker(_job(self.out))
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(list(self.dir.iterdir()), [])


# --- Group B: ffmpeg is pointed at the temp, never the destination -----


class GroupBCommandRouting(_Dest):
    def test_b1_the_spawned_command_writes_to_the_owned_temp(self) -> None:
        worker = ExportWorker(_job(self.out))
        spawned = _run(worker, FakeProc(PROGRESS, rc=0))
        self.assertEqual(spawned.dest, worker._temp_output)
        self.assertNotEqual(spawned.dest, self.out)

    def test_b2_the_destination_appears_nowhere_in_the_command(self) -> None:
        worker = ExportWorker(_job(self.out))
        spawned = _run(worker, FakeProc(PROGRESS, rc=0))
        self.assertNotIn(str(self.out), spawned.cmd)

    def test_b3_every_other_argument_is_unchanged(self) -> None:
        """Command parity: this slice substitutes the output destination
        and nothing else - no codec, filtergraph, or option may move."""
        job = _job(self.out, width=1920, height=1080)
        before = ExportWorker(job)._build_command()

        worker = ExportWorker(job)
        spawned = _run(worker, FakeProc(PROGRESS, rc=0))
        after = spawned.cmd

        self.assertEqual(before[-1], str(self.out))
        self.assertEqual(after[-1], str(worker._temp_output))
        self.assertEqual(before[:-1], after[:-1])


# --- Group C: a normal success --------------------------------------


class GroupCSuccess(_Dest):
    def test_c1_success_promotes_the_temp_onto_the_destination(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(self.siblings(), [])

    def test_c2_finished_carries_the_destination_not_the_temp(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(seen.finished, [self.out])
        self.assertNotIn(worker._temp_output, seen.finished)

    def test_c3_the_destination_stays_absent_until_the_encode_ends(self) -> None:
        """Mid-encode there must be no half-written file at the path the
        user chose."""
        worker = ExportWorker(_job(self.out))
        during: list[tuple[bool, bool]] = []

        def _look() -> None:
            during.append((self.out.exists(), worker._temp_output.exists()))

        _run(worker, FakeProc(PROGRESS, rc=0, hooks={1: _look}))

        self.assertEqual(during, [(False, True)])
        self.assertTrue(self.out.exists())


# --- Group D: a pre-existing destination survives until success -------


class GroupDExistingDestinationSuccess(_Dest):
    def test_d1_the_old_file_is_replaced_only_after_a_full_success(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        during: list[bytes] = []

        _run(worker, FakeProc(
            PROGRESS, rc=0, hooks={1: lambda: during.append(self.out.read_bytes())},
        ), writes=b"NEW")

        self.assertEqual(during, [b"OLD"])
        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(seen.finished, [self.out])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertEqual(self.siblings(), [])

    def test_d2_replacing_a_private_file_keeps_it_private(self) -> None:
        """Promotion swaps the inode, so the destination's own permissions
        would otherwise be replaced by the temp's umask defaults - turning
        a deliberately private export world-readable on the next run.
        """
        self.out.write_bytes(b"OLD")
        self.out.chmod(0o600)
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)

    def test_d3_an_executable_destination_keeps_its_mode_too(self) -> None:
        """Not just the restrictive direction: whatever the user set is
        what the replacement carries."""
        self.out.write_bytes(b"OLD")
        self.out.chmod(0o640)
        worker = ExportWorker(_job(self.out))
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(self.out.stat().st_mode & 0o777, 0o640)

    def test_d4_a_new_destination_keeps_the_ordinary_default(self) -> None:
        """With nothing to inherit from, the encode's own file mode is
        the right answer - no invented policy."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _encode() -> None:
            worker._temp_output.write_bytes(b"NEW")
            worker._temp_output.chmod(0o644)

        _run(worker, FakeProc(PROGRESS, rc=0, hooks={0: _encode}), writes=None)

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o644)

    def test_d5_an_unappliable_mode_blocks_the_replacement(self) -> None:
        """If the destination's restrictions cannot be carried over, the
        replacement must not happen at all.

        Promoting anyway would silently widen access to a file the user
        deliberately restricted, and they would have no signal that it
        happened. Refusing leaves them with an intact original and a
        retained encode, which is recoverable; the alternative is not.
        """
        self.out.write_bytes(b"OLD")
        self.out.chmod(0o600)
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with unittest.mock.patch.object(
            exporter_mod.os, "chmod",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.finished, [])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(worker._temp_output.read_bytes(), b"NEW")

    def test_d6_an_absent_destination_is_not_a_permission_failure(self) -> None:
        """There is nothing to inherit from and nothing to protect, so a
        first-time export must not be held up by the same check."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")


# --- Group Y: promotion carries the destination's security metadata ---


def _xattr_capable(d: Path) -> bool:
    probe = d / ".xattr-probe"
    probe.write_bytes(b"")
    try:
        os.setxattr(probe, "user.cove.probe", b"1")
    except OSError:
        return False
    finally:
        probe.unlink()
    return True


class GroupYPosixMetadata(_Dest):
    """`os.replace` swaps in a different file object, so everything bound
    to the replaced one is lost unless it is carried over first.

    Before the ownership slice ffmpeg truncated the destination in place,
    which kept its inode and with it the ACL and extended attributes an
    administrator or user had attached. Promotion has to reproduce that,
    or a re-export silently changes who can read the video.
    """

    def setUp(self) -> None:
        super().setUp()
        if not _xattr_capable(self.dir):
            self.skipTest("filesystem does not support extended attributes")

    def _export(self, worker: ExportWorker) -> Outcomes:
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")
        return seen

    def test_y1_extended_attributes_survive_the_replacement(self) -> None:
        self.out.write_bytes(b"OLD")
        os.setxattr(self.out, "user.cove.marker", b"keep-me")
        worker = ExportWorker(_job(self.out))
        seen = self._export(worker)

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertEqual(os.getxattr(self.out, "user.cove.marker"), b"keep-me")

    def test_y2_a_posix_acl_survives_the_replacement(self) -> None:
        """The case that actually decides who may read the file."""
        self.out.write_bytes(b"OLD")
        if subprocess.run(["setfacl", "-m", "u:nobody:r", str(self.out)],
                          capture_output=True).returncode != 0:
            self.skipTest("setfacl unavailable or unsupported here")
        before = os.getxattr(self.out, "system.posix_acl_access")
        worker = ExportWorker(_job(self.out))
        seen = self._export(worker)

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertEqual(os.getxattr(self.out, "system.posix_acl_access"), before)
        acl = subprocess.run(["getfacl", "-c", str(self.out)],
                             capture_output=True, text=True).stdout
        self.assertIn("user:nobody:r--", acl)

    def test_y3_the_mode_still_survives_alongside_the_attributes(self) -> None:
        self.out.write_bytes(b"OLD")
        self.out.chmod(0o600)
        os.setxattr(self.out, "user.cove.marker", b"keep-me")
        worker = ExportWorker(_job(self.out))
        self._export(worker)

        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.getxattr(self.out, "user.cove.marker"), b"keep-me")

    def test_y4_an_absent_destination_needs_no_attributes(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = self._export(worker)

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_y5_a_destination_without_attributes_is_untouched(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = self._export(worker)

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(os.listxattr(self.out), [])

    def test_y6_an_uncopyable_attribute_aborts_before_the_replacement(self) -> None:
        """The non-negotiable rule: never destroy the original and *then*
        report that its access controls could not be carried over."""
        self.out.write_bytes(b"OLD")
        os.setxattr(self.out, "user.cove.marker", b"keep-me")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with unittest.mock.patch.object(
            exporter_mod.os, "setxattr",
            side_effect=PermissionError(1, "Operation not permitted"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.finished, [])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertEqual(os.getxattr(self.out, "user.cove.marker"), b"keep-me")
        self.assertEqual(worker._temp_output.read_bytes(), b"NEW")

    def test_y16_inherited_attributes_absent_from_the_destination_are_dropped(
            self) -> None:
        """A directory default ACL grants the *newly created* temp named
        entries the destination never had. Copying the destination's
        attributes on top does not remove them, so the promoted export
        would hand out access the file it replaced did not.
        """
        if subprocess.run(["setfacl", "-d", "-m", "u:nobody:rwx", str(self.dir)],
                          capture_output=True).returncode != 0:
            self.skipTest("default ACLs unsupported here")
        # The destination predates the default ACL's effect on new files:
        # it carries no named entries at all.
        self.out.write_bytes(b"OLD")
        subprocess.run(["setfacl", "-b", str(self.out)], capture_output=True)
        self.assertNotIn("system.posix_acl_access", os.listxattr(self.out))

        worker = ExportWorker(_job(self.out))
        seen = self._export(worker)

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        acl = subprocess.run(["getfacl", "-c", str(self.out)],
                             capture_output=True, text=True).stdout
        self.assertNotIn("user:nobody", acl, f"inherited ACL survived:\n{acl}")
        self.assertNotIn("system.posix_acl_access", os.listxattr(self.out))

    def test_y17_an_attribute_already_matching_is_not_rewritten(self) -> None:
        """Rewriting an attribute that already holds the right value is a
        privileged operation performed for no reason, and some readable
        attributes are not writable - which would fail a valid export."""
        self.out.write_bytes(b"OLD")
        os.setxattr(self.out, "user.cove.marker", b"same")
        worker = ExportWorker(_job(self.out))
        written: list[str] = []
        real_setxattr = os.setxattr

        def _seed(*_a) -> None:
            worker._temp_output.write_bytes(b"NEW")
            real_setxattr(worker._temp_output, "user.cove.marker", b"same")

        with unittest.mock.patch.object(
            exporter_mod.os, "setxattr",
            lambda p, n, v: written.append(n) or real_setxattr(p, n, v),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0, hooks={0: _seed}), writes=None)

        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertNotIn("user.cove.marker", written)

    def test_y7_an_unreadable_attribute_list_aborts_too(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with unittest.mock.patch.object(
            exporter_mod.os, "listxattr",
            side_effect=PermissionError(1, "Operation not permitted"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"OLD")


class GroupYOwnership(_Dest):
    """Ownership is part of the destination's access-control state.

    In-place truncation kept the replaced file's uid/gid. A rename does
    not, so re-exporting a file owned by someone else would quietly
    transfer it to whoever ran the export.
    """

    def test_y8_matching_ownership_needs_no_chown_at_all(self) -> None:
        """The ordinary case, re-exporting your own file. Nothing to
        change, so nothing may be attempted - a pointless chown would be
        a new way for a working export to fail."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with unittest.mock.patch.object(
            exporter_mod.os, "chown",
            side_effect=AssertionError("chown attempted for identical ownership"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_y9_differing_ownership_is_carried_onto_the_encode(self) -> None:
        self.out.write_bytes(b"OLD")
        real_stat = os.stat
        st = real_stat(self.out)
        foreign = type("S", (), {
            "st_mode": st.st_mode, "st_uid": st.st_uid + 1, "st_gid": st.st_gid + 1,
        })()
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        chowns: list[tuple] = []

        def _stat(path, *a, **k):
            return foreign if Path(path) == self.out else real_stat(path, *a, **k)

        with unittest.mock.patch.object(exporter_mod.os, "stat", _stat), \
                unittest.mock.patch.object(
                    exporter_mod.os, "chown",
                    lambda p, u, g: chowns.append((Path(p), u, g))):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(
            chowns, [(worker._temp_output, st.st_uid + 1, st.st_gid + 1)])

    def test_y10_unpreservable_ownership_aborts_before_the_replacement(self) -> None:
        """Taking silent ownership of another user's file is exactly the
        change this slice exists to prevent."""
        self.out.write_bytes(b"OLD")
        real_stat = os.stat
        st = real_stat(self.out)
        foreign = type("S", (), {
            "st_mode": st.st_mode, "st_uid": st.st_uid + 1, "st_gid": st.st_gid,
        })()
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _stat(path, *a, **k):
            return foreign if Path(path) == self.out else real_stat(path, *a, **k)

        with unittest.mock.patch.object(exporter_mod.os, "stat", _stat), \
                unittest.mock.patch.object(
                    exporter_mod.os, "chown",
                    side_effect=PermissionError(1, "Operation not permitted")):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertEqual(worker._temp_output.read_bytes(), b"NEW")


class GroupYSymlink(_Dest):
    """A symlinked destination is a path the user deliberately points
    somewhere else. ffmpeg wrote through it; a rename would delete it and
    drop a regular file in its place, leaving the real target stale."""

    def setUp(self) -> None:
        super().setUp()
        self.target_dir = self.dir / "real"
        self.target_dir.mkdir()
        self.target = self.target_dir / "actual.mp4"

    def _link(self) -> None:
        self.target.write_bytes(b"OLD")
        self.out.symlink_to(self.target)

    def test_y11_the_symlink_survives_and_its_target_is_updated(self) -> None:
        self._link()
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertTrue(self.out.is_symlink(), "the symlink was replaced")
        self.assertEqual(self.target.read_bytes(), b"NEW")
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_y12_the_temp_lives_beside_the_resolved_target(self) -> None:
        """Promotion has to be a same-filesystem rename onto the file the
        bytes actually land in, so the temp belongs next to the target."""
        self._link()
        worker = ExportWorker(_job(self.out))
        self.assertEqual(worker._temp_output.parent, self.target_dir)

    def test_y13_finished_still_reports_the_path_the_user_chose(self) -> None:
        self._link()
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.finished, [self.out])

    def test_y14_a_plain_destination_is_not_rewritten(self) -> None:
        """Only the destination itself is followed, never its parents.

        Resolving unconditionally would rewrite the directory whenever
        any ancestor happened to be a link - which is the normal state of
        ``/tmp`` on macOS - and silently move both the owned temp and the
        folder Show-in-folder opens somewhere the user never chose.
        """
        linked_dir = self.dir / "link-to-real"
        linked_dir.symlink_to(self.target_dir)
        dest = linked_dir / "movie.mp4"      # the file itself is not a link
        worker = ExportWorker(_job(dest))

        self.assertEqual(worker._temp_output.parent, linked_dir)
        self.assertNotEqual(worker._temp_output.parent, self.target_dir)

    def test_y18_a_retargeted_symlink_is_not_promoted_onto_the_old_target(
            self) -> None:
        """The link is resolved once, when the run starts. If somebody
        repoints it while ffmpeg is working, the file it used to name is
        no longer the file the user is asking for - overwriting it anyway
        would destroy a file nobody nominated, and calling that success
        would be a lie about where the export went.
        """
        self._link()
        other = self.target_dir / "other.mp4"
        other.write_bytes(b"OTHER")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _retarget() -> None:
            self.out.unlink()
            self.out.symlink_to(other)

        _run(worker, FakeProc(PROGRESS, rc=0, hooks={1: _retarget}),
             writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.finished, [])
        self.assertEqual(self.target.read_bytes(), b"OLD")
        self.assertEqual(other.read_bytes(), b"OTHER")
        self.assertEqual(worker._temp_output.read_bytes(), b"NEW")

    def test_y19_an_unchanged_binding_still_promotes(self) -> None:
        """The check must not make ordinary symlinked exports fail."""
        self._link()
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.target.read_bytes(), b"NEW")

    def test_y15_cancelling_a_symlinked_export_keeps_both(self) -> None:
        self._link()
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertTrue(self.out.is_symlink())
        self.assertEqual(self.target.read_bytes(), b"OLD")
        self.assertFalse(worker._temp_output.exists())


# --- Group Z: the Windows replacement primitive -----------------------


class GroupZWindowsReplace(_Dest):
    """`os.replace` maps to ``MoveFileEx``, which creates a new file
    object and drops the replaced file's DACL and named streams.
    ``ReplaceFileW`` exists precisely to avoid that, and needs no
    third-party dependency to reach.

    These run on any host by driving the ``nt`` branch directly. The
    genuine end-to-end Windows execution is a separate wine proof.
    """

    def _win(self):
        return unittest.mock.patch.object(exporter_mod, "_IS_WINDOWS", True)

    def test_z1_an_existing_destination_goes_through_replace_file_w(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        calls: list[tuple] = []
        real_replace = os.replace

        def _fake(replaced, replacement, backup):
            calls.append((replaced, replacement))
            real_replace(replacement, replaced)

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _fake,
        ), unittest.mock.patch.object(
            exporter_mod.os, "replace", wraps=real_replace,
        ) as plain_rename:
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(calls, [(self.out, worker._temp_output)])
        # The plain rename is what loses the DACL, so it must not be the
        # primitive that replaced an existing file.
        plain_rename.assert_not_called()
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_z2_a_windows_replacement_failure_aborts_the_promotion(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32",
            side_effect=OSError("ReplaceFileW failed: access denied"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertEqual(worker._temp_output.read_bytes(), b"NEW")
        self.assertIn("ReplaceFileW", seen.failed[0])

    def test_z3_a_first_export_does_not_need_replace_file_w(self) -> None:
        """``ReplaceFileW`` requires a file to replace. With no
        destination there is no metadata to carry, so the ordinary
        atomic rename is the right primitive."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32",
            side_effect=AssertionError("ReplaceFileW needs an existing destination"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_z6_a_backup_name_is_supplied_so_failure_is_recoverable(self) -> None:
        """Without ``lpBackupFileName``, Windows documents ERROR_UNABLE_TO
        _MOVE_REPLACEMENT (1176) as leaving the replaced file *deleted*.
        A promotion that can destroy the destination is exactly what this
        boundary exists to prevent, so a backup name is not optional."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        got: list[tuple] = []
        real_replace = os.replace

        def _fake(replaced, replacement, backup):
            got.append((replaced, replacement, backup))
            real_replace(replacement, replaced)

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _fake,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(len(got), 1)
        replaced, replacement, backup = got[0]
        self.assertEqual(replaced, self.out)
        self.assertEqual(replacement, worker._temp_output)
        self.assertIsNotNone(backup, "no backup name was supplied")
        self.assertEqual(Path(backup).parent, self.dir)
        self.assertNotIn(Path(backup), (self.out, worker._temp_output))

    def test_z7_a_successful_replacement_leaves_no_backup_behind(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        real_replace = os.replace

        def _fake(replaced, replacement, backup):
            # What ReplaceFileW actually does: the replaced file *becomes*
            # the backup, keeping its identity, and the replacement takes
            # its place. Copying instead would model a file Cove never
            # replaced, which is a case it is now required to refuse.
            real_replace(replaced, backup)
            real_replace(replacement, replaced)

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _fake,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertEqual(self.siblings(), [], "a backup file was left in the folder")

    def test_z8_a_failure_that_removed_the_destination_restores_it(self) -> None:
        """The documented 1176 shape: the replacement could not be renamed
        and the replaced file is gone, with only the backup holding it."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _fake(replaced, replacement, backup):
            Path(backup).write_bytes(Path(replaced).read_bytes())
            Path(replaced).unlink()          # Windows already deleted it
            raise OSError(1176, "The replacement file could not be renamed")

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _fake,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.finished, [])
        self.assertTrue(self.out.exists(), "the destination was not restored")
        self.assertEqual(self.out.read_bytes(), b"OLD")
        # The retained encode is expected; a leftover backup is not.
        self.assertEqual(self.siblings(), [worker._temp_output],
                         "recovery left a backup behind")

    def test_z10_a_failed_recovery_never_deletes_the_only_surviving_copy(
            self) -> None:
        """The worst case: the replacement removed the destination, and
        putting the backup back also fails. The backup is then the only
        copy of the user's file in existence, so it must survive - a
        tidy-up that deletes it turns a recoverable failure into real
        data loss.
        """
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        logged: list[str] = []
        worker.log.connect(logged.append)
        made: list[Path] = []

        def _lossy(replaced, replacement, backup):
            Path(backup).write_bytes(Path(replaced).read_bytes())
            made.append(Path(backup))
            Path(replaced).unlink()
            raise OSError(1176, "The replacement file could not be renamed")

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _lossy,
        ), unittest.mock.patch.object(
            exporter_mod, "_restore_replaced_file",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        backup = made[0]
        self.assertTrue(backup.exists(), "the only surviving copy was deleted")
        self.assertEqual(backup.read_bytes(), b"OLD")
        # and the user is told where it is
        self.assertIn(backup.name, "\n".join(logged) + "\n".join(seen.failed))

    def test_z11_a_foreign_file_at_the_destination_does_not_justify_deleting_the_backup(
            self) -> None:
        """Windows can fail with the original living only in the backup.
        A file existing at the destination afterwards is not proof that
        it is that original - another process may have created it - so
        the backup cannot be deleted on the strength of the path being
        occupied.
        """
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        logged: list[str] = []
        worker.log.connect(logged.append)
        made: list[Path] = []

        def _lossy(replaced, replacement, backup):
            Path(backup).write_bytes(Path(replaced).read_bytes())
            made.append(Path(backup))
            Path(replaced).unlink()
            # Somebody else drops an unrelated file at the destination.
            Path(replaced).write_bytes(b"FOREIGN")
            raise OSError(1176, "The replacement file could not be renamed")

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _lossy,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        backup = made[0]
        self.assertTrue(backup.exists(), "the original was deleted")
        self.assertEqual(backup.read_bytes(), b"OLD")
        self.assertEqual(self.out.read_bytes(), b"FOREIGN")
        # The destination is occupied, so recovery should say so plainly
        # rather than report a restore it never attempted.
        joined = "\n".join(logged)
        self.assertIn("the file that was there beforehand is preserved", joined)
        self.assertNotIn("could not be put back", joined)

    def test_z12_a_probe_failure_during_recovery_keeps_the_original_error(
            self) -> None:
        """Recovery is failure handling, so it cannot introduce a second
        unhandled filesystem call. If probing raises, the replacement
        error is still what the user is told, and the backup holding
        their file is still named."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        logged: list[str] = []
        worker.log.connect(logged.append)
        made: list[Path] = []

        attempted: list[bool] = []

        def _lossy(replaced, replacement, backup):
            Path(backup).write_bytes(Path(replaced).read_bytes())
            made.append(Path(backup))
            Path(replaced).unlink()
            attempted.append(True)
            raise OSError(1176, "The replacement file could not be renamed")

        real_exists = Path.exists

        def _exists(self_path, *a, **k):
            # Only once recovery is under way; the dispatch check that
            # runs beforehand is a different call site.
            if attempted and Path(self_path) in (self.out, *made):
                raise PermissionError(13, "Permission denied")
            return real_exists(self_path, *a, **k)

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _lossy,
        ), unittest.mock.patch.object(Path, "exists", _exists):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(len(seen.failed), 1)
        self.assertIn("1176", seen.failed[0])
        backup = made[0]
        self.assertTrue(os.path.exists(backup), "the only copy was discarded")
        self.assertIn(backup.name, "\n".join(logged))

    def test_z13_a_backup_that_is_not_the_replaced_file_is_not_deleted(
            self) -> None:
        """The backup path is generated, so something else can occupy it.
        Deleting whatever sits there would be the same mistake the owned
        temp exists to prevent."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        real_replace = os.replace
        made: list[Path] = []

        def _fake(replaced, replacement, backup):
            # The replacement succeeds, but a *different* file ends up at
            # the backup pathname rather than the replaced one.
            real_replace(replacement, replaced)
            Path(backup).write_bytes(b"NOT THE REPLACED FILE")
            made.append(Path(backup))

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _fake,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertTrue(made[0].exists(), "a foreign file at the backup path was deleted")
        self.assertEqual(made[0].read_bytes(), b"NOT THE REPLACED FILE")

    def test_z14_recovery_never_overwrites_a_destination_that_reappears(
            self) -> None:
        """Recovery looks to see the destination is gone, then puts the
        backup back. Anything created in between is a file this export
        never nominated, and a failed promotion must not turn into
        destroying it. The backup stays recoverable instead.
        """
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        logged: list[str] = []
        worker.log.connect(logged.append)
        made: list[Path] = []

        def _lossy(replaced, replacement, backup):
            Path(backup).write_bytes(Path(replaced).read_bytes())
            made.append(Path(backup))
            Path(replaced).unlink()
            raise OSError(1176, "The replacement file could not be renamed")

        real_exists = Path.exists
        raced: list[bool] = []

        def _exists(self_path, *a, **k):
            answer = real_exists(self_path, *a, **k)
            if Path(self_path) == self.out and made and not raced:
                # Absent when recovery looks; occupied a moment later.
                raced.append(True)
                self.out.write_bytes(b"CREATED-BY-SOMEONE-ELSE")
                return False
            return answer

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _lossy,
        ), unittest.mock.patch.object(Path, "exists", _exists):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertTrue(raced, "the race was never exercised")
        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"CREATED-BY-SOMEONE-ELSE")
        backup = made[0]
        self.assertTrue(backup.exists(), "the recovery copy was lost")
        self.assertEqual(backup.read_bytes(), b"OLD")
        self.assertIn(backup.name, "\n".join(logged))

    def test_z15_uncontended_recovery_still_restores_and_tidies_up(self) -> None:
        """The check must not cost the ordinary recovery its effect."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        made: list[Path] = []

        def _lossy(replaced, replacement, backup):
            Path(replaced).replace(backup)      # as ReplaceFileW would
            made.append(Path(backup))
            raise OSError(1176, "The replacement file could not be renamed")

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _lossy,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertFalse(made[0].exists(), "the backup was left behind")

    def test_z9_a_failure_that_kept_the_destination_leaves_it_alone(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _fake(replaced, replacement, backup):
            raise OSError(5, "Access is denied")

        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32", _fake,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertEqual(worker._temp_output.read_bytes(), b"NEW")
        self.assertEqual(self.siblings(), [worker._temp_output])

    def test_z4_posix_hosts_never_reach_the_windows_primitive(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32",
            side_effect=AssertionError("windows primitive used on posix"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_z5_posix_metadata_is_not_applied_on_windows(self) -> None:
        """``ReplaceFileW`` carries the metadata itself; doing it twice
        would mean chmod/xattr calls that mean nothing there."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        with self._win(), unittest.mock.patch.object(
            exporter_mod, "_replace_file_win32",
            lambda replaced, replacement, backup: os.replace(replacement, replaced),
        ), unittest.mock.patch.object(
            exporter_mod.os, "listxattr",
            side_effect=AssertionError("posix metadata carried on windows"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(self.out.read_bytes(), b"NEW")


# --- Groups E-H: cancel and failure clean the temp, never the file ----


class GroupEExistingDestinationCancel(_Dest):
    def test_e1_cancelling_leaves_the_old_file_byte_for_byte(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(seen.failed, [])
        self.assertEqual(seen.finished, [])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(self.siblings(), [])


class GroupFExistingDestinationFailure(_Dest):
    def test_f1_a_failed_encode_leaves_the_old_file_byte_for_byte(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(["out_time_us=1"], rc=1, stderr=["bad codec"]),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.cancelled, [])
        self.assertEqual(seen.finished, [])
        self.assertIn("ffmpeg exited 1", seen.failed[0])
        self.assertEqual(self.out.read_bytes(), b"OLD")
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(self.siblings(), [])


class GroupGAbsentDestinationCancel(_Dest):
    def test_g1_cancelling_removes_the_partial_and_creates_no_destination(self) -> None:
        """The Tab 2K limitation this slice exists to remove: a cancelled
        export used to leave a partial file at the user's chosen path."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.dir.iterdir()), [])


class GroupHAbsentDestinationFailure(_Dest):
    def test_h1_a_failed_encode_removes_the_partial(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(["out_time_us=1"], rc=1, stderr=["boom"]),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["failed"])
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_h2_a_command_that_never_ran_still_leaves_nothing_behind(self) -> None:
        """``Popen`` itself blew up, so there is no child and no output -
        the failure must still not invent one."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, popen_exc=OSError("no such file"), writes=None)

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(list(self.dir.iterdir()), [])


# --- Group I: cancelling before the child starts ----------------------


class GroupIPreStartCancel(_Dest):
    def test_i1_a_pre_start_cancel_spawns_nothing_and_leaves_nothing(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        worker.cancel()
        spawned = _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(spawned.cmds, [])
        self.assertEqual(seen.order, ["cancelled"])
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_i2_a_pre_start_cancel_never_touches_an_existing_file(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        worker.cancel()
        _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(self.out.read_bytes(), b"OLD")


# --- Groups J/K: the Popen publication window -------------------------


class GroupJKPublicationRace(_Dest):
    """Tab 2K-C's startup race, now with ownership layered on.

    ``subprocess.Popen`` spawns the child before it returns, so there is a
    window where a real process exists and ``_proc`` is still None. The
    fake blocks on an Event inside that window so the cancel lands there
    deterministically rather than by timing.
    """

    def _race(self, *, child: str, existing: bytes | None = None):
        rc = {"live": -15, "failed": 1, "succeeded": 0}[child]
        if existing is not None:
            self.out.write_bytes(existing)
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        proc = FakeProc(PROGRESS, rc=rc, stderr=["bad codec"])
        entered = threading.Event()
        release = threading.Event()
        real_popen = exporter_mod.subprocess.Popen

        def _spawn(cmd, *a, **kw):
            if not (isinstance(cmd, list) and "-progress" in cmd):
                return real_popen(cmd, *a, **kw)
            # The child exists as far as the OS is concerned; the worker
            # has no handle on it yet. It has already produced output.
            Path(cmd[-1]).write_bytes(b"NEW")
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

        self.assertFalse(runner.is_alive(), "worker deadlocked on cancellation")
        QApplication.processEvents()
        self.assertEqual(len(seen.order), 1, f"one terminal signal: {seen.order}")
        return worker, seen, proc

    def test_j1_a_live_child_is_cancelled_and_its_temp_removed(self) -> None:
        worker, seen, proc = self._race(child="live", existing=b"OLD")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(proc.terminate_calls, 1)
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(self.out.read_bytes(), b"OLD")

    def test_j2_an_already_failed_child_stays_a_failure_and_is_cleaned(self) -> None:
        worker, seen, proc = self._race(child="failed", existing=b"OLD")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(proc.terminate_calls, 0)
        self.assertIn("ffmpeg exited 1", seen.failed[0])
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(self.out.read_bytes(), b"OLD")

    def test_k1_an_already_finished_child_still_promotes(self) -> None:
        """Cancellation cannot claim a child that already exited 0, and
        the encode it produced is still promoted onto the destination."""
        worker, seen, proc = self._race(child="succeeded", existing=b"OLD")

        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(seen.finished, [self.out])
        self.assertEqual(proc.terminate_calls, 0)
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_k2_a_claimed_cancel_whose_child_still_exits_zero_promotes(self) -> None:
        """The narrowest success case: cancellation genuinely claimed a
        live child, signalled it, and the child finished cleanly anyway.

        A zero exit means the encode really is complete, so discarding it
        as "cancelled" would throw away a finished export - and would
        leave the destination holding whatever was there before while the
        user was told nothing happened.
        """
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        # `cancel` lands while the fake child is still live, so it claims
        # the run and terminates it; `wait()` then reports 0 regardless.
        proc = FakeProc(PROGRESS, rc=0, hooks={1: worker.cancel})
        _run(worker, proc, writes=b"NEW")

        self.assertTrue(worker._cancel_claimed)
        self.assertTrue(worker._encode_ok)
        self.assertGreaterEqual(proc.terminate_calls, 1)
        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(seen.finished, [self.out])
        self.assertEqual(self.out.read_bytes(), b"NEW")
        self.assertFalse(worker._temp_output.exists())


# --- Group L: promotion itself fails ----------------------------------


class GroupLPromotionFailure(_Dest):
    def _fail_promotion(self, exc: OSError):
        return unittest.mock.patch.object(
            exporter_mod.os, "replace", side_effect=exc,
        )

    def test_l1_a_promotion_error_is_a_failure_not_a_success(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with self._fail_promotion(PermissionError(13, "Permission denied")):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.finished, [])
        self.assertEqual(seen.cancelled, [])
        self.assertEqual(self.out.read_bytes(), b"OLD")

    def test_l2_the_completed_encode_is_retained_for_recovery(self) -> None:
        """Deleting a finished encode because the last step failed would
        throw away the only copy of work that really did complete."""
        worker = ExportWorker(_job(self.out))
        with self._fail_promotion(PermissionError(13, "Permission denied")):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertTrue(worker._temp_output.exists())
        self.assertEqual(worker._temp_output.read_bytes(), b"NEW")

    def test_l3_the_failure_message_identifies_the_retained_file(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with self._fail_promotion(PermissionError(13, "Permission denied")):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertIn(worker._temp_output.name, seen.failed[0])
        self.assertIn("Permission denied", seen.failed[0])

    def test_l3b_an_empty_encode_promises_no_recovery(self) -> None:
        """The recovery sentence is only true when there is something to
        recover. Since the path is reserved up front, an ffmpeg that
        produced nothing leaves an empty file rather than no file - and
        pointing the user at that wastes their time just the same."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=None)

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(worker._temp_output.stat().st_size, 0)
        # The OS error names the path it could not act on, which is fair
        # diagnostic detail. What must be absent is the claim that the
        # file is sitting there waiting to be recovered.
        self.assertNotIn("still in", seen.failed[0])
        self.assertNotIn("The encoded file", seen.failed[0])

    def test_l5_an_encode_swapped_for_another_file_is_not_promoted(self) -> None:
        """The reservation records which file it created. If what sits
        there at promotion time is a different one, it is not this run's
        output and must not be written over the user's destination."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _swap() -> None:
            worker._temp_output.unlink()
            worker._temp_output.write_bytes(b"SUBSTITUTED")

        _run(worker, FakeProc(PROGRESS, rc=0, hooks={1: _swap}), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"OLD")

    def test_l4_a_missing_encode_is_a_failure_not_a_silent_success(self) -> None:
        """ffmpeg claimed success but produced nothing. Reporting
        "Saved movie.mp4" here would point the user at a stale file."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(PROGRESS, rc=0), writes=None)

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"OLD")


# --- Groups M/N: cleanup itself fails ---------------------------------


class GroupMNCleanupFailure(_Dest):
    def _fail_unlink(self):
        return unittest.mock.patch.object(
            Path, "unlink", autospec=True,
            side_effect=PermissionError(13, "Permission denied"),
        )

    def test_m1_a_cancel_whose_cleanup_fails_is_still_a_cancellation(self) -> None:
        """The user stopped the export. Being unable to tidy up afterwards
        does not turn that into an error they have to react to."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with self._fail_unlink():
            _run(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}),
                 writes=b"PARTIAL")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(seen.failed, [])
        self.assertEqual(self.out.read_bytes(), b"OLD")

    def test_m2_the_cleanup_problem_is_still_reported_in_the_log(self) -> None:
        worker = ExportWorker(_job(self.out))
        logged: list[str] = []
        worker.log.connect(logged.append)
        with self._fail_unlink():
            _run(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}),
                 writes=b"PARTIAL")

        joined = "\n".join(logged)
        self.assertIn(worker._temp_output.name, joined)
        self.assertIn("Permission denied", joined)

    def test_n1_a_failed_encode_whose_cleanup_fails_is_still_that_failure(self) -> None:
        """The encode error is what the user needs to see; a cleanup
        problem must not displace it."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with self._fail_unlink():
            _run(worker, FakeProc(["out_time_us=1"], rc=1,
                                  stderr=["Invalid data found"]),
                 writes=b"PARTIAL")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(seen.cancelled, [])
        self.assertIn("ffmpeg exited 1", seen.failed[0])
        self.assertIn("Invalid data found", seen.failed[0])
        self.assertEqual(self.out.read_bytes(), b"OLD")


# --- Groups O/P: someone else's file appears at the destination -------


class GroupOPForeignDestination(_Dest):
    """The exact data-loss race that made Tab 2K drop cleanup entirely."""

    def test_o1_a_file_created_mid_export_survives_cancellation(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _someone_else() -> None:
            self.out.write_bytes(b"UNRELATED")

        _run(worker, FakeProc(PROGRESS, rc=-15,
                              hooks={1: _someone_else, 2: worker.cancel}),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(self.out.read_bytes(), b"UNRELATED")
        self.assertFalse(worker._temp_output.exists())

    def test_p1_a_file_created_mid_export_survives_a_failure(self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(["out_time_us=1", "out_time_us=2"], rc=1,
                              stderr=["boom"],
                              hooks={1: lambda: self.out.write_bytes(b"UNRELATED")}),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(self.out.read_bytes(), b"UNRELATED")
        self.assertFalse(worker._temp_output.exists())


# --- Group Q: two runs aimed at one destination -----------------------


class GroupQConcurrentRuns(_Dest):
    def test_q1_two_workers_never_share_a_temp(self) -> None:
        a = ExportWorker(_job(self.out))
        b = ExportWorker(_job(self.out))
        self.assertNotEqual(a._temp_output, b._temp_output)

    def test_q2_one_worker_cleanup_cannot_remove_another_workers_temp(self) -> None:
        a = ExportWorker(_job(self.out))
        b = ExportWorker(_job(self.out))
        b._temp_output.write_bytes(b"B WORK IN PROGRESS")
        seen = Outcomes(a)
        _run(a, FakeProc(PROGRESS, rc=-15, hooks={1: a.cancel}), writes=b"A PARTIAL")

        self.assertEqual(seen.order, ["cancelled"])
        self.assertFalse(a._temp_output.exists())
        self.assertEqual(b._temp_output.read_bytes(), b"B WORK IN PROGRESS")


# --- Group R: hardware encoder selection ------------------------------


class GroupRHardware(_Dest):
    """Encoder choice is resolved once, before the command is built, so a
    run has exactly one encode attempt. The ownership boundary therefore
    has to hold for whichever encoder was picked - and only the output
    destination may differ from the CPU command."""

    def _hw(self):
        return unittest.mock.patch.object(ff, "nvenc_available", lambda c: True)

    def test_r1_a_hardware_encode_still_targets_the_owned_temp(self) -> None:
        with self._hw():
            worker = ExportWorker(_job(self.out, encoder_pref="nvenc"))
            spawned = _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertIn("h264_nvenc", spawned.cmd)
        self.assertEqual(spawned.dest, worker._temp_output)
        self.assertNotIn(str(self.out), spawned.cmd)

    def test_r2_a_failed_hardware_encode_never_writes_the_destination(self) -> None:
        with self._hw():
            worker = ExportWorker(_job(self.out, encoder_pref="nvenc"))
            seen = Outcomes(worker)
            _run(worker, FakeProc(["out_time_us=1"], rc=1,
                                  stderr=["No capable devices found"]),
                 writes=b"PARTIAL")

        self.assertEqual(seen.order, ["failed"])
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_r3_a_hardware_success_promotes_exactly_once(self) -> None:
        with self._hw():
            worker = ExportWorker(_job(self.out, encoder_pref="nvenc"))
            seen = Outcomes(worker)
            with unittest.mock.patch.object(
                exporter_mod.os, "replace", wraps=os.replace,
            ) as promote:
                _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(promote.call_count, 1)
        self.assertEqual(seen.order, ["finished"])
        self.assertEqual(self.out.read_bytes(), b"NEW")

    def test_r4_only_the_destination_differs_from_the_cpu_command(self) -> None:
        with self._hw():
            job = _job(self.out, encoder_pref="nvenc")
            reference = ExportWorker(job)._build_command()
            worker = ExportWorker(job)
            spawned = _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(reference[:-1], spawned.cmd[:-1])


# --- Group S: the output-format matrix --------------------------------


class GroupSFormatMatrix(_Dest):
    """Temp naming must not break ffmpeg's muxer inference for any format
    the export UI offers - video, animation and audio-only alike."""

    CASES = [
        (MP4, "mp4"),
        ("WebM (VP9 + Opus)", "webm"),
        ("GIF (animation)", "gif"),
        ("MP3 (audio only)", "mp3"),
        ("WAV (audio only)", "wav"),
        ("AAC (audio only)", "m4a"),
    ]

    def test_s1_every_format_keeps_its_suffix_on_the_owned_temp(self) -> None:
        for fmt_key, ext in self.CASES:
            with self.subTest(fmt=fmt_key):
                out = self.dir / f"clip.{ext}"
                worker = ExportWorker(_job(out, fmt_key=fmt_key))
                self.assertEqual(worker._temp_output.suffix, f".{ext}")
                self.assertEqual(worker._temp_output.parent, self.dir)

    def test_s2_every_format_encodes_to_the_temp_and_promotes(self) -> None:
        for fmt_key, ext in self.CASES:
            with self.subTest(fmt=fmt_key):
                out = self.dir / f"clip-{ext}.{ext}"
                worker = ExportWorker(_job(out, fmt_key=fmt_key))
                seen = Outcomes(worker)
                spawned = _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

                self.assertEqual(spawned.dest, worker._temp_output)
                self.assertEqual(spawned.dest.suffix, f".{ext}")
                self.assertEqual(seen.order, ["finished"])
                self.assertEqual(out.read_bytes(), b"NEW")

    def test_s3_a_dotted_destination_stem_keeps_only_the_real_suffix(self) -> None:
        """Cove's own suggested name is ``<source>.edited.mp4``."""
        out = self.dir / "holiday.edited.mp4"
        worker = ExportWorker(_job(out))
        self.assertEqual(worker._temp_output.suffix, ".mp4")
        self.assertIn("holiday.edited", worker._temp_output.name)


# --- Group T: what Tab 2L's Show in folder is given -------------------


def _win() -> MainWindow:
    with unittest.mock.patch.object(
        MainWindow, "_start_encoder_probe", lambda self: None,
    ):
        return MainWindow()


class GroupTShowInFolder(_Dest):
    """The real MainWindow handlers, driven by a real ExportWorker."""

    def setUp(self) -> None:
        super().setUp()
        self.w = _win()
        self.addCleanup(self.w.deleteLater)
        unittest.mock.patch.object(
            app_mod.QMessageBox, "warning", return_value=None).start()
        self.opener = unittest.mock.patch.object(app_mod, "_open_local").start()
        self.addCleanup(unittest.mock.patch.stopall)
        self.addCleanup(QApplication.processEvents)

    def _drive(self, worker: ExportWorker, proc: FakeProc, **kw) -> None:
        worker.finished.connect(self.w._on_export_done)
        worker.failed.connect(self.w._on_export_failed)
        worker.cancelled.connect(self.w._on_export_cancelled)
        self.w._set_last_export_output(None)
        _run(worker, proc, **kw)
        QApplication.processEvents()

    def revealable(self) -> bool:
        # ``isVisible()`` is False for every child of an unshown window,
        # so ask about visibility *within* the window instead.
        return self.w.show_folder_btn.isVisibleTo(self.w)

    def test_t1_a_success_reveals_the_destination_folder(self) -> None:
        worker = ExportWorker(_job(self.out))
        self._drive(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(self.w._last_export_output, self.out)
        self.assertTrue(self.revealable())
        self.w._on_show_in_folder()
        self.opener.assert_called_once()
        self.assertEqual(Path(self.opener.call_args.args[0]), self.out.parent)

    def test_t2_no_temp_path_ever_reaches_the_ui(self) -> None:
        worker = ExportWorker(_job(self.out))
        self._drive(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        text = self.w.export_log.toPlainText()
        self.assertIn("movie.mp4", text)
        self.assertNotIn("cove-export", text)
        self.assertNotEqual(self.w._last_export_output, worker._temp_output)

    def test_t3_a_cancelled_export_exposes_no_reveal_target(self) -> None:
        worker = ExportWorker(_job(self.out))
        self._drive(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}),
                    writes=b"PARTIAL")

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.revealable())

    def test_t4_a_failed_export_exposes_no_reveal_target(self) -> None:
        worker = ExportWorker(_job(self.out))
        self._drive(worker, FakeProc(["out_time_us=1"], rc=1, stderr=["boom"]),
                    writes=b"PARTIAL")

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.revealable())

    def test_t5_a_promotion_failure_exposes_no_reveal_target(self) -> None:
        """The encode completed, but nothing was ever placed at the
        destination - so there is nothing to reveal."""
        worker = ExportWorker(_job(self.out))
        with unittest.mock.patch.object(
            exporter_mod.os, "replace",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            self._drive(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertIsNone(self.w._last_export_output)
        self.assertFalse(self.revealable())


# --- Group U: exactly one terminal outcome ----------------------------


class GroupUTerminalExclusivity(_Dest):
    def _count(self, seen: Outcomes) -> int:
        return len(seen.finished) + len(seen.failed) + len(seen.cancelled)

    def test_u1_every_representative_run_emits_exactly_one_outcome(self) -> None:
        def _success(w):
            return _run(w, FakeProc(PROGRESS, rc=0))

        def _failure(w):
            return _run(w, FakeProc(["out_time_us=1"], rc=1, stderr=["boom"]),
                        writes=b"PARTIAL")

        def _cancel(w):
            return _run(w, FakeProc(PROGRESS, rc=-15, hooks={1: w.cancel}),
                        writes=b"PARTIAL")

        def _pre_start_cancel(w):
            w.cancel()
            return _run(w, FakeProc(PROGRESS, rc=0))

        def _promotion_failure(w):
            with unittest.mock.patch.object(
                exporter_mod.os, "replace",
                side_effect=PermissionError(13, "Permission denied"),
            ):
                return _run(w, FakeProc(PROGRESS, rc=0))

        def _popen_failure(w):
            return _run(w, popen_exc=OSError("boom"), writes=None)

        cases = {
            "success": (_success, "finished"),
            "failure": (_failure, "failed"),
            "cancel": (_cancel, "cancelled"),
            "pre-start cancel": (_pre_start_cancel, "cancelled"),
            "promotion failure": (_promotion_failure, "failed"),
            "popen failure": (_popen_failure, "failed"),
        }
        for name, (drive, expected) in cases.items():
            with self.subTest(case=name):
                out = self.dir / f"{name.replace(' ', '-')}.mp4"
                worker = ExportWorker(_job(out))
                seen = Outcomes(worker)
                drive(worker)
                self.assertEqual(self._count(seen), 1, seen.order)
                self.assertEqual(seen.order, [expected])


# --- Group AA: reservation inside the startup window ------------------


class GroupAAReservationStartup(_Dest):
    """Reserving the temp happens inside the Popen startup window, so it
    has to obey the same rules as everything else in there: whatever goes
    wrong, ``_proc_starting`` is cleared and a Cancel that deferred to
    publication gets to claim the run.
    """

    def _cancel_during_reservation(self) -> tuple[ExportWorker, Outcomes]:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        # Something already occupies the reserved path, so O_EXCL fails.
        worker._temp_output.write_bytes(b"FOREIGN")
        real_open = os.open

        def _open(path, *a, **k):
            if Path(path) == worker._temp_output:
                worker.cancel()      # the click lands inside the window
            return real_open(path, *a, **k)

        with unittest.mock.patch.object(exporter_mod.os, "open", _open):
            _run(worker, FakeProc(PROGRESS, rc=0))
        return worker, seen

    def test_aa1_a_cancel_during_a_failed_reservation_is_still_a_cancellation(
            self) -> None:
        """The user stopped the export. That a reservation happened to
        fail in the same instant is not their problem, and must not turn
        a deliberate stop into an error."""
        _worker, seen = self._cancel_during_reservation()

        self.assertEqual(seen.order, ["cancelled"])
        self.assertEqual(seen.failed, [])
        self.assertEqual(seen.finished, [])

    def test_aa2_a_failed_reservation_clears_the_startup_flag(self) -> None:
        """Leaving ``_proc_starting`` set strands the four-state
        cancellation model: a later cancel would defer to a publication
        that is never coming."""
        worker, _seen = self._cancel_during_reservation()

        self.assertFalse(worker._proc_starting)

    def test_aa3_a_failed_reservation_without_a_cancel_is_still_a_failure(
            self) -> None:
        """The other half: nobody cancelled, so this is a real error."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        worker._temp_output.write_bytes(b"FOREIGN")
        _run(worker, FakeProc(PROGRESS, rc=0))

        self.assertEqual(seen.order, ["failed"])
        self.assertFalse(worker._proc_starting)
        self.assertEqual(worker._temp_output.read_bytes(), b"FOREIGN")


# --- Group AB: cleanup proves ownership at deletion time --------------


class GroupABCleanupIdentity(_Dest):
    """Promotion verifies that the file it is about to move is the one
    this run created. Cleanup deletes a file, which is just as
    irreversible, so it has to prove the same thing."""

    def _swap_temp_during(self, worker: ExportWorker) -> None:
        worker._temp_output.unlink()
        worker._temp_output.write_bytes(b"SOMEONE ELSE'S FILE")

    def test_ab1_a_replacement_planted_at_the_temp_path_survives_cleanup(
            self) -> None:
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        proc = FakeProc(["out_time_us=1"], rc=1, stderr=["boom"],
                        on_wait=lambda: self._swap_temp_during(worker))
        _run(worker, proc, writes=b"PARTIAL")

        self.assertEqual(seen.order, ["failed"])
        self.assertTrue(worker._temp_output.exists())
        self.assertEqual(worker._temp_output.read_bytes(), b"SOMEONE ELSE'S FILE")

    def test_ab2_a_planted_replacement_is_reported_not_silently_kept(
            self) -> None:
        worker = ExportWorker(_job(self.out))
        logged: list[str] = []
        worker.log.connect(logged.append)
        proc = FakeProc(PROGRESS, rc=-15,
                        hooks={1: worker.cancel},
                        on_wait=lambda: self._swap_temp_during(worker))
        _run(worker, proc, writes=b"PARTIAL")

        # The command line already mentions the temp path, so the guard
        # has to be the explanation itself, not just the name.
        self.assertIn("no longer the file this export created",
                      "\n".join(logged))

    def test_ab3_the_run_s_own_temp_is_still_removed(self) -> None:
        """The check must not make ordinary cleanup a no-op."""
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        _run(worker, FakeProc(["out_time_us=1"], rc=1, stderr=["boom"]),
             writes=b"PARTIAL")

        self.assertEqual(seen.order, ["failed"])
        self.assertFalse(worker._temp_output.exists())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_ab4_a_symlink_planted_at_the_temp_path_is_not_followed(
            self) -> None:
        victim = self.dir / "victim.txt"
        victim.write_bytes(b"UNRELATED")
        worker = ExportWorker(_job(self.out))

        def _plant() -> None:
            worker._temp_output.unlink()
            worker._temp_output.symlink_to(victim)

        _run(worker, FakeProc(["out_time_us=1"], rc=1, stderr=["boom"],
                              on_wait=_plant), writes=b"PARTIAL")

        self.assertTrue(victim.exists())
        self.assertEqual(victim.read_bytes(), b"UNRELATED")
        self.assertTrue(worker._temp_output.is_symlink())


# --- Group AC: the promotion-failure diagnostic -----------------------


class GroupACFailureDiagnostic(_Dest):
    def test_ac1_the_recovery_text_names_the_directory_holding_the_encode(
            self) -> None:
        """With a symlinked destination the temp lives beside the resolved
        target, which is not where ``job.output`` points. Sending the user
        to the wrong folder to find their export helps nobody."""
        target_dir = self.dir / "real"
        target_dir.mkdir()
        target = target_dir / "actual.mp4"
        target.write_bytes(b"OLD")
        self.out.symlink_to(target)

        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        with unittest.mock.patch.object(
            exporter_mod.os, "replace",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(worker._temp_output.parent, target_dir)
        self.assertIn(str(target_dir), seen.failed[0])
        self.assertNotIn(f"still in {self.dir} ", seen.failed[0])

    def test_ac2_a_probe_that_raises_never_suppresses_the_failure(self) -> None:
        """Building the diagnostic must not become a second failure path:
        an export that failed still has to say so, exactly once."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)
        real_stat = Path.stat

        def _stat(self_path, *a, **k):
            if Path(self_path) == worker._temp_output:
                raise PermissionError(13, "Permission denied")
            return real_stat(self_path, *a, **k)

        with unittest.mock.patch.object(
            exporter_mod.os, "replace",
            side_effect=PermissionError(13, "Permission denied"),
        ), unittest.mock.patch.object(Path, "stat", _stat):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertEqual(len(seen.failed), 1)
        self.assertIn("could not be moved into place", seen.failed[0])
        self.assertEqual(self.out.read_bytes(), b"OLD")

    def test_ac3_a_vanishing_encode_during_probing_is_survivable(self) -> None:
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        seen = Outcomes(worker)

        def _replace(*_a, **_k):
            worker._temp_output.unlink()
            raise PermissionError(13, "Permission denied")

        with unittest.mock.patch.object(
            exporter_mod.os, "replace", side_effect=_replace,
        ):
            _run(worker, FakeProc(PROGRESS, rc=0), writes=b"NEW")

        self.assertEqual(seen.order, ["failed"])
        self.assertNotIn("The encoded file", seen.failed[0])


# --- Group V: structural guards ---------------------------------------


class GroupVStructure(_Dest):
    def test_v1_deletion_only_ever_targets_run_owned_paths(self) -> None:
        """Behavioural proof is above; this pins the structure so a future
        edit cannot reintroduce a destination-path unlink. ``backup`` is
        a name this run invented too, exactly like the temp."""
        src = inspect.getsource(exporter_mod.ExportWorker)
        targets = set(re.findall(r"([\w.\[\]]+)\.unlink\(", src))
        self.assertEqual(targets, {"self._temp_output", "backup"})
        for banned in ("os.remove", "os.unlink", "shutil.rmtree"):
            self.assertNotIn(banned, src)

    def test_v2_the_only_overwriting_move_is_the_promotion(self) -> None:
        """``os.replace`` overwrites whatever it lands on, so exactly one
        call site may use it: the promotion, which is the one operation
        allowed to take the destination. Recovery puts the destination
        back and must refuse to overwrite, so it goes through the
        no-clobber helper instead."""
        src = inspect.getsource(exporter_mod.ExportWorker)
        self.assertEqual(re.findall(r"os\.replace\((\w+), (\w+)\)", src),
                         [("temp", "final")])
        self.assertIn("_restore_replaced_file(backup, final)", src)
        for banned in ("shutil.copy", "shutil.move", ".rename("):
            self.assertNotIn(banned, src)

    def test_v4_the_metadata_helper_only_ever_writes_to_the_encode(self) -> None:
        """It is handed the file about to be replaced. Reading it is the
        whole point; writing to it would be modifying the user's file
        during a promotion that may still be abandoned."""
        src = inspect.getsource(exporter_mod._carry_posix_metadata)
        writers = re.findall(r"os\.(chmod|setxattr|remove|unlink|replace|chown)"
                             r"\((\w+)", src)
        self.assertTrue(writers)
        for call, target in writers:
            self.assertEqual(target, "onto", f"os.{call} writes to {target!r}")

    def test_v5_there_is_one_windows_replacement_call_site(self) -> None:
        src = inspect.getsource(exporter_mod)
        self.assertEqual(src.count("_replace_file_win32(final, temp, backup)"), 1)
        self.assertEqual(src.count("kernel32.ReplaceFileW"), 1)
        # The backup argument is what makes a failed replacement
        # recoverable, so passing NULL must not creep back in.
        self.assertNotIn("str(replacement), None", src)

    def test_v3_a_destination_deletion_would_be_caught(self) -> None:
        """The guard above only works if a real unlink of the destination
        is observable, so prove the detector fires."""
        self.out.write_bytes(b"OLD")
        worker = ExportWorker(_job(self.out))
        with unittest.mock.patch.object(
            ExportWorker, "_discard_temp",
            lambda self: self._job.output.unlink(missing_ok=True),
        ):
            _run(worker, FakeProc(PROGRESS, rc=-15, hooks={1: worker.cancel}),
                 writes=b"PARTIAL")
        self.assertFalse(self.out.exists())  # the mutation really is visible


if __name__ == "__main__":
    unittest.main()
