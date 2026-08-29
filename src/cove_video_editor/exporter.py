from __future__ import annotations

import collections
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from . import ffmpeg_utils as ff
from .clip import Clip, SubtitleTrack, sequence_length, sort_clips
# The encoder argument group lives next to the format table because the
# capability probe builds its command from it too; re-exported here since
# this is where export command construction lives.
from .ffmpeg_utils import build_export_video_encoder_args  # noqa: F401


#: Resolved once, and named, so the places that need to branch on the
#: platform read as intent rather than as a string comparison - and so a
#: test can drive the other platform's branch without patching `os.name`
#: out from under `tempfile` and `pathlib` for the whole run.
_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    _CREATE_NO_WINDOW = 0x08000000
    _POPEN_KWARGS: dict = {"creationflags": _CREATE_NO_WINDOW}
else:
    _POPEN_KWARGS = {}


_FILTER_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Infix that makes a temporary export recognizable as Cove's own, so a
#: leftover from a crashed run can be identified rather than guessed at.
TEMP_MARKER = "cove-export"

#: Fallback for the bytes a single path component may occupy. 255 is the
#: limit on every filesystem Cove ships against (ext4, btrfs, XFS, APFS,
#: NTFS, exFAT), and is only used when the real limit cannot be read. The
#: decoration below costs ~26 bytes, so without bounding the stem a
#: destination name that is itself perfectly valid would become
#: unexportable with ENAMETOOLONG.
_NAME_MAX = 255


def _name_max(directory: Path) -> int:
    """The component-length limit that actually applies to ``directory``.

    The constraint belongs to the filesystem holding the destination, not
    to a constant: a mount with a smaller limit would accept the name the
    user chose and then reject the decorated temp beside it. Where the
    real limit cannot be had - `pathconf` does not exist on Windows, and
    can fail anywhere - the portable floor is the safer answer, since
    refusing to export is worse than a name that is merely shorter than
    it needed to be.
    """
    try:
        limit = os.pathconf(directory, "PC_NAME_MAX")
    except (OSError, ValueError, AttributeError):
        return _NAME_MAX
    return limit if limit and limit > 0 else _NAME_MAX


def owned_temp_output(final: Path) -> Path:
    """Return the temporary output path a single export run owns.

    The file lives beside the requested destination so promotion can be a
    same-filesystem ``os.replace`` - moving it through a system temp
    directory would risk a cross-device rename and turn the one atomic
    step into a copy. It keeps the destination's suffix because ffmpeg
    infers the muxer from it, and it is prefixed with a dot so an
    in-progress export does not clutter the user's folder.

    The token is random rather than derived from a counter or the pid:
    two Cove processes exporting to the same destination must not be able
    to agree on a name, since cleanup deletes this path unconditionally
    and would otherwise remove another run's work in progress.

    The decorated name is bounded to the filesystem's component limit in
    priority order. The marker and token never give way - they are what
    makes the file identifiable and unique. The suffix gives way only if
    it alone would break the limit, which no suffix ffmpeg could infer a
    muxer from ever does. The stem gives way first, since it is there for
    the user's benefit rather than the encoder's. Truncation is on the
    encoded bytes, because that is what the limit counts, and
    ``errors="ignore"`` drops a multi-byte character split by the cut
    rather than emitting a lone surrogate.

    The suffix is split the way *ffmpeg* reads a filename - everything
    after the last dot - not the way POSIX does. The two disagree on
    leading-dot names: ``Path(".mp4").suffix`` is empty because POSIX
    calls that a hidden file named "mp4", while ffmpeg happily infers the
    MP4 muxer from it. Using the POSIX reading would hand ffmpeg a temp
    with no extension at all and lose the container the user chose.
    """
    core = f".{TEMP_MARKER}-{uuid.uuid4().hex[:12]}"
    budget = max(0, _name_max(final.parent) - len(core.encode()) - len(b"."))
    name = final.name
    dot = name.rfind(".")
    raw_stem, raw_suffix = (name[:dot], name[dot:]) if dot >= 0 else (name, "")
    suffix = raw_suffix.encode()[:budget].decode(errors="ignore")
    stem = raw_stem.encode()[:budget - len(suffix.encode())].decode(errors="ignore")
    return final.parent / f".{stem}{core}{suffix}"


def _resolve_destination(final: Path) -> Path:
    """Return the file the export's bytes should actually land in.

    Only a genuine symlink is followed. Resolving every path would be
    gratuitous - it would rewrite the directory the temp lives in and the
    folder the UI reveals, for destinations that were never indirect.

    A link that cannot be resolved (a loop, or a permission problem on
    the way) is left alone rather than guessed at; the promotion will
    then fail against the literal path with the destination untouched,
    which is the safe direction.
    """
    if not final.is_symlink():
        return final
    try:
        return final.resolve()
    except OSError:
        return final


def _carry_posix_metadata(source: Path, onto: Path) -> None:
    """Copy an existing destination's access controls onto the encode.

    ``source`` is the file about to be replaced and ``onto`` is the file
    that will take its place. A destination that does not exist yet has
    nothing to hand over, which is the only condition treated as normal
    here; every other error propagates so the caller can abandon the
    promotion with the original still in place.

    The mode goes on first and the extended attributes second, matching
    the order ``shutil.copystat`` uses. The two interact - ``chmod``
    rewrites a POSIX ACL's mask entry, and applying the ACL rewrites the
    group permission bits - but not here: both values are read from the
    same file, so they already agree and either order lands in the same
    place. The order is kept for the reader's benefit and to stay
    correct if the two ever stop coming from one source.

    Every attribute is copied rather than a chosen subset: which ones
    carry access-control meaning is a policy of the filesystem and the
    host, not something this function is in a position to judge.
    """
    try:
        st = os.stat(source)
    except FileNotFoundError:
        return
    # Ownership is an access control too: an in-place truncate kept the
    # replaced file's uid/gid, and a rename hands the file to whoever ran
    # the export. Only attempted when it would actually change something,
    # so the ordinary case of re-exporting your own file cannot acquire a
    # new way to fail - and the case that *would* silently transfer a
    # file to a different owner is the one that stops the promotion.
    onto_st = os.stat(onto)
    if (st.st_uid, st.st_gid) != (onto_st.st_uid, onto_st.st_gid):
        os.chown(onto, st.st_uid, st.st_gid)
    os.chmod(onto, st.st_mode & 0o7777)

    # The encode's attributes are reconciled *to* the destination's, not
    # merely topped up from them. A directory carrying a default ACL
    # grants named entries to every file created in it, so the temp can
    # arrive holding permissions the destination never had; leaving those
    # in place would let a re-export widen access rather than reproduce
    # it. Anything the destination does not have is removed.
    wanted = {name: os.getxattr(source, name) for name in os.listxattr(source)}
    for name in os.listxattr(onto):
        if name not in wanted:
            os.removexattr(onto, name)
    for name, value in wanted.items():
        # Writing an attribute that already holds the right value is a
        # privileged operation performed for no reason, and some
        # attributes are readable but not writable - which would fail an
        # export that has nothing wrong with it.
        try:
            if os.getxattr(onto, name) == value:
                continue
        except OSError:
            pass
        os.setxattr(onto, name, value)


def _restore_replaced_file(backup: Path, final: Path) -> None:
    """Put ``backup`` back at ``final``, refusing to overwrite anything.

    Recovery runs because a replacement failed, so it must not become a
    second way to destroy something: a file created at the destination
    between the check and the move is one this export never nominated,
    and it has to survive. The move therefore has to refuse rather than
    clobber, which rules out ``os.replace``.

    Two platforms, one guarantee. On Windows ``os.rename`` already means
    "fail if the target exists", which is the primitive this path runs on
    in production. Elsewhere the same guarantee comes from an atomic hard
    link followed by dropping the old name. The branch is on the real
    platform rather than on the promotion-policy flag, because what
    differs here is which primitive the operating system actually offers.
    """
    if os.name == "nt":
        os.rename(backup, final)
        return
    os.link(backup, final)
    os.unlink(backup)


def _file_identity(path: Path) -> tuple[int, int]:
    """Identify the filesystem object at ``path``, without following it.

    A pathname is not an identity: the entry it names can be swapped for
    another file at any moment. This is what lets the code tell a file it
    created from one that merely occupies the same name. A path that is
    not an ordinary file has no identity worth recording, since nothing
    here should ever be acting on a link or a device node.
    """
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"{path.name} is not a regular file")
    return (st.st_dev, st.st_ino)


def _replace_file_win32(replaced: Path, replacement: Path, backup: Path) -> None:
    """Replace ``replaced`` with ``replacement`` via ``ReplaceFileW``.

    Windows has no POSIX-style metadata to copy by hand; the access
    control state lives in a security descriptor that a rename does not
    move. ``MoveFileEx`` - which is what ``os.replace`` becomes here -
    therefore leaves the new file inheriting the directory's permissions
    rather than keeping the replaced file's DACL.

    ``ReplaceFileW`` is the API Windows provides for exactly this
    situation. It performs the substitution and transfers the replaced
    file's attributes to the replacement, so the result keeps the access
    controls, named streams and creation time the destination had.

    ``backup`` is mandatory, not a convenience. Windows documents
    ERROR_UNABLE_TO_MOVE_REPLACEMENT as leaving the replaced file
    *deleted* when no backup name was given - a promotion that can
    destroy the destination is the one outcome this whole boundary exists
    to rule out. With a backup name the replaced file always survives
    somewhere, so the caller can put it back.

    Reached through ``ctypes`` deliberately: it is in ``kernel32``, so no
    third-party extension is needed to call it. ``ctypes.wintypes`` is
    imported here rather than at module scope because it does not exist
    on other platforms.
    """
    import ctypes
    import ctypes.wintypes as wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        wintypes.LPCWSTR,   # lpReplacedFileName
        wintypes.LPCWSTR,   # lpReplacementFileName
        wintypes.LPCWSTR,   # lpBackupFileName - none wanted
        wintypes.DWORD,     # dwReplaceFlags
        wintypes.LPVOID,    # lpExclude - reserved
        wintypes.LPVOID,    # lpReserved
    ]
    replace_file.restype = wintypes.BOOL

    if not replace_file(str(replaced), str(replacement), str(backup),
                        0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


@dataclass
class AudioTrack:
    path: Path
    replace: bool = False
    volume: float = 1.0
    original_volume: float = 1.0
    offset: float = 0.0          # timeline seconds where the track starts
    duration: float = 0.0        # natural length; 0 means "use full input"
    src_start: float = 0.0       # source-file start in seconds (trim)


@dataclass
class ExportJob:
    clips: list[Clip]
    output: Path
    fmt_key: str
    crop: tuple[int, int, int, int] | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    # List of added-audio tracks; each placed at its own offset and mixed
    # with the clip audio (or replacing it if `replace` is true on all
    # tracks; the final flag wins).
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    # Optional region restriction — if set, only [region_start, region_end)
    # of the timeline is exported (via output-side -ss / -t on the final map).
    region_start: float | None = None
    region_end: float | None = None
    # Optional burn-in subtitle track. When present, the active
    # SubtitleTrack is applied to the concat'd video output via the
    # `subtitles=` filter before final mapping.
    subtitles: SubtitleTrack | None = None
    # Which video encoder family to use: "auto" (hardware when it really
    # works, else CPU), "cpu", "nvenc" or "amf". Callers that predate the
    # setting - and every audio-only job - keep the safe default.
    encoder_pref: str = "auto"

    @property
    def total_timeline(self) -> float:
        if self.clips:
            return sequence_length(self.clips)
        return _audio_only_duration(self.audio_tracks) if self.audio_tracks else 0.0


class ExportWorker(QObject):
    progress = Signal(int)
    eta = Signal(float)
    log = Signal(str)
    finished = Signal(Path)
    failed = Signal(str)
    #: The user stopped the export. A deliberate stop is not a failure, so
    #: it gets its own terminal outcome rather than a `failed("Cancelled")`.
    #: Exactly one of finished/cancelled/failed is emitted per run.
    cancelled = Signal()

    def __init__(self, job: ExportJob) -> None:
        super().__init__()
        self._job = job
        self._cancelled = False
        self._proc: subprocess.Popen | None = None
        self._started_wall: float = 0.0
        self._eta_smoothed: float | None = None
        self._tmp_dir: Path | None = None
        # Runtime-only ownership state. `job.output` keeps meaning "where
        # the user asked for the export"; this is the only path ffmpeg is
        # ever given, and the only path cleanup is ever allowed to delete.
        # Allocated once here so a run has exactly one owned temp, and so
        # no code path can encode before the name exists. Naming it does
        # not create it - ffmpeg's `-y` does that when the encode starts.
        #
        # A symlinked destination is a path the user pointed somewhere
        # else on purpose. ffmpeg wrote through it; renaming onto it
        # would delete the link and drop a regular file in its place,
        # leaving the real target stale. So the bytes are promoted onto
        # the resolved target, and the temp sits beside *that* - which is
        # also what keeps the rename on one filesystem. `job.output`
        # stays the path the user chose, and is what `finished` reports.
        self._promotion_target: Path = _resolve_destination(job.output)
        self._temp_output: Path = owned_temp_output(self._promotion_target)
        # Set when the temp path has been claimed atomically, to the
        # identity of the file that claim created. An unguessable name is
        # not the same as ownership: until this is set nothing at that
        # path is ours to write over, promote, or delete.
        self._temp_identity: tuple[int, int] | None = None
        # A cancel *request* and a cancel that actually decided this run's
        # outcome are different things, and conflating them lets a click
        # that arrives after ffmpeg already died relabel a real failure as
        # a cancellation. `_cancelled` records the request; `_cancel_claimed`
        # records that cancellation got there first and owns the result.
        self._cancel_claimed = False
        self._encode_ok = False
        # `Popen` spawns the child before it returns, so there is an
        # interval where a real process exists and `_proc` is still None.
        # Reading "no process object" as "no process" during that window
        # would let a cancel claim a run whose child had already failed.
        # The lock makes the four states a cancel can observe explicit:
        # not started, starting, live, already terminal.
        self._proc_lock = threading.Lock()
        self._proc_starting = False
        self._cancel_awaiting_publication = False

    def cancel(self) -> None:
        self._cancelled = True
        with self._proc_lock:
            proc = self._proc
            if proc is None:
                if self._proc_starting:
                    # A child may already exist but we cannot poll it yet.
                    # Defer ownership to publication rather than guess.
                    self._cancel_awaiting_publication = True
                else:
                    # Genuinely nothing started, so nothing can have
                    # finished either: the cancellation is first.
                    self._cancel_claimed = True
                return
            self._claim_if_live(proc)

    def _claim_if_live(self, proc: subprocess.Popen) -> None:
        """Take ownership of the run only if the child is still running.

        Callers hold ``_proc_lock``. A child that already reached its own
        terminal status keeps it: a nonzero exit stays a failure and a
        clean exit stays a success, however late the click arrives.
        """
        if proc.poll() is None:
            self._cancel_claimed = True
            proc.terminate()

    def run(self) -> None:
        self._started_wall = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="cove-subs-") as _tmp:
            self._tmp_dir = Path(_tmp)
            try:
                # ffmpeg is pointed at this run's own temp, never at the
                # user's destination, so nothing can appear there until
                # the encode has fully succeeded.
                cmd = self._build_command(self._temp_output)
            except Exception as exc:  # noqa: BLE001
                self._discard_temp()
                self._emit_abnormal(exc)
                return
            self.log.emit("$ " + " ".join(cmd))
            try:
                self._execute(cmd)
            except Exception as exc:  # noqa: BLE001
                self._discard_temp()
                self._emit_abnormal(exc)
                return
            finally:
                self._tmp_dir = None
        # A cancel that arrives after ffmpeg already exited 0 has nothing
        # left to cancel: the export is on disk and complete.
        if self._cancel_claimed and not self._encode_ok:
            self._discard_temp()
            self.cancelled.emit()
            return
        self._promote()

    def _promote(self) -> None:
        """Move the finished encode onto the requested destination.

        The single point at which an export becomes user-visible. Until
        this returns, whatever was at ``job.output`` before is still
        there, and ``finished`` is only emitted once the replacement has
        actually happened - a zero exit status alone is not a completed
        export.

        ``os.replace`` is atomic on a same-filesystem move, which the
        sibling temp guarantees, so the destination is never observed
        half-written. There is deliberately no copy fallback: a copy would
        reintroduce exactly the partial-destination window this slice
        exists to close.

        A promotion that fails leaves the encode where it is. It is real,
        completed work and the only copy of it, so deleting it to tidy up
        would destroy the one thing worth recovering.
        """
        try:
            self._verify_encode()
            self._verify_destination_binding()
            self._replace_destination()
        except OSError as exc:
            self.failed.emit(self._promotion_failure_message(exc))
            return
        self.finished.emit(self._job.output)

    def _replace_destination(self) -> None:
        """Put the encode at the destination, carrying its security state.

        Replacing a file is not the same as overwriting one. ffmpeg used
        to truncate the destination in place, so its inode survived and
        with it every access control attached to it. A rename swaps in a
        different file object, and anything bound to the old one - POSIX
        ACLs and extended attributes, Windows DACLs and named streams -
        goes with it unless it is carried across first.

        Two platforms, two primitives, one boundary. On Windows
        ``ReplaceFileW`` is the operation built for this and transfers
        that state itself. On POSIX there is no such call, so the
        metadata is applied to the encode before the rename.

        Nothing is preserved when there is no destination yet, because
        there is nothing to preserve: a first export is an ordinary
        atomic rename on both platforms.

        Every failure here happens *before* the replacement, so a
        destination whose access controls could not be carried over is
        left exactly as it was rather than replaced and reported after
        the fact.
        """
        final, temp = self._promotion_target, self._temp_output
        if _IS_WINDOWS:
            if final.exists():
                self._replace_win32_with_backup(final, temp)
                return
        else:
            _carry_posix_metadata(final, temp)
        os.replace(temp, final)

    def _replace_win32_with_backup(self, final: Path, temp: Path) -> None:
        """``ReplaceFileW`` with a backup, and a real recovery on failure.

        Windows does not guarantee that a failed replacement rolls back.
        With a backup name the replaced file always survives as either
        itself or the backup, so a failure that already removed the
        destination can be undone by moving the backup into its place.

        The backup only exists for the length of this call. A successful
        replacement leaves it holding the previous contents, which is
        clutter in the user's folder rather than anything they asked for,
        so it is removed - and failing to remove it does not un-succeed
        an export that genuinely completed.
        """
        backup = owned_temp_output(final)
        # ``ReplaceFileW`` moves the replaced file to the backup name, so
        # the backup carries the destination's identity. Recording it now
        # is what later distinguishes the file this operation set aside
        # from anything else that might come to occupy that name.
        try:
            replaced_identity: tuple[int, int] | None = _file_identity(final)
        except OSError:
            replaced_identity = None
        try:
            _replace_file_win32(final, temp, backup)
        except OSError:
            self._recover_replaced_file(final, backup)
            raise
        self._discard_backup(final, backup, replaced_identity)

    def _recover_replaced_file(self, final: Path, backup: Path) -> None:
        """Put the destination back after a failed replacement.

        Windows can fail having already removed the destination, leaving
        it only in the backup. Moving the backup into place undoes that.

        Once a backup exists after a failed replacement it is never
        deleted here. Windows documents states in which the original
        lives at the backup path, and a file sitting at the destination
        is not proof that it is the original - another process may have
        created it in the meantime. Since the code cannot tell those
        apart, it keeps the backup and says where it is. Leaving a spare
        copy behind is a cost; deleting the user's only one is not a
        risk worth taking to avoid it.

        The export still fails on its original error either way, so
        nothing in here may raise: this runs while that error is being
        propagated, and a second exception from a probe would replace the
        real reason the export failed with an incidental one. Every check
        is therefore guarded, and an answer that cannot be determined is
        read the cautious way - assume there is a backup, assume the
        destination is occupied, keep the file and say where it is.
        """
        try:
            backup_present = backup.exists()
        except OSError:
            backup_present = True
        if not backup_present:
            return
        try:
            destination_present = final.exists()
        except OSError:
            destination_present = True
        if not destination_present:
            try:
                _restore_replaced_file(backup, final)
                return
            except OSError as exc:
                self.log.emit(
                    f"{final.name} could not be put back after a failed "
                    f"replacement ({exc}); the previous file is preserved "
                    f"as {backup.name}"
                )
                return
        self.log.emit(
            f"the replacement of {final.name} failed; the file that was "
            f"there beforehand is preserved as {backup.name}"
        )

    def _discard_backup(self, final: Path, backup: Path,
                        replaced_identity: tuple[int, int] | None) -> None:
        """Drop the backup once the destination is provably in place.

        Only ever called after a successful replacement, so this can
        never remove the last copy of anything. It still has to prove
        *what* it is removing: the backup name is generated rather than
        reserved, so the entry is only deleted when it is still the file
        the replacement set aside. Anything else there belongs to
        somebody else and is left alone, exactly as at the temp path.

        Failing to remove it leaves clutter, which is not a reason to
        un-succeed an export that genuinely completed.
        """
        try:
            if replaced_identity is None or _file_identity(backup) != replaced_identity:
                self.log.emit(
                    f"{backup.name} is not the file that was replaced, so "
                    f"it has been left in place"
                )
                return
        except FileNotFoundError:
            return
        except OSError as exc:
            self.log.emit(
                f"could not check the temporary backup of "
                f"{final.name}: {exc}"
            )
            return
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            self.log.emit(
                f"could not remove the temporary backup of "
                f"{final.name}: {exc}"
            )

    def _reserve_temp(self) -> None:
        """Claim the temp path atomically, and remember what we claimed.

        An unguessable name keeps two exports from colliding, but it does
        not establish ownership: anything with write access to the folder
        could occupy that path first, and a planted symlink would send
        ffmpeg's ``-y`` somewhere else entirely. ``O_EXCL`` settles it -
        the call succeeds only if it is the thing that created the file,
        and refuses to follow a symlink - so from here on the path is
        provably this run's.

        Created with the ordinary permissive mode so the umask applies,
        exactly as ffmpeg's own file creation would: reserving the name
        must not quietly change what an export's permissions look like.

        Called after the pre-start cancel check, so a run that never
        begins still leaves nothing behind.
        """
        fd = os.open(self._temp_output,
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        try:
            st = os.fstat(fd)
            # Recorded before the close, because the file is already ours
            # by this point. Waiting until afterwards would mean a close
            # that failed left a reserved file nothing could claim, and
            # so nothing would ever clean up.
            self._temp_identity = (st.st_dev, st.st_ino)
        finally:
            os.close(fd)

    def _verify_destination_binding(self) -> None:
        """Check the destination still means what it meant at the start.

        A symlinked destination is resolved once, when the run begins,
        and the encode is placed beside whatever it named then. If the
        link is repointed while ffmpeg is working, that file is no longer
        the one the user is asking for: overwriting it would destroy a
        file nobody nominated, and reporting success would misstate where
        the export went. Better to stop with both files intact and the
        encode retained.
        """
        current = _resolve_destination(self._job.output)
        if current != self._promotion_target:
            raise OSError(
                f"{self._job.output.name} no longer points at "
                f"{self._promotion_target}, so the export was not moved "
                f"into place"
            )

    def _verify_encode(self) -> None:
        """Check the thing about to be promoted is the encode we made.

        Between reserving the path and finishing the encode the file
        could in principle have been swapped for something else. Since
        the next step overwrites the user's destination with it, it is
        worth confirming that it is still the same file, is still an
        ordinary file rather than a link, and actually holds an encode -
        a zero-byte output is ffmpeg reporting success while producing
        nothing, and promoting that would replace a real video with an
        empty file.
        """
        if self._temp_identity is None:
            raise OSError("the export never reserved an output file")
        st = os.lstat(self._temp_output)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{self._temp_output.name} is no longer a regular file")
        if (st.st_dev, st.st_ino) != self._temp_identity:
            raise OSError(
                f"{self._temp_output.name} was replaced by a different file "
                f"while the export was running"
            )
        if st.st_size == 0:
            raise OSError("ffmpeg reported success but produced no output")

    def _promotion_failure_message(self, exc: OSError) -> str:
        """Explain a failed promotion, and only promise what is there.

        The encode usually survives a promotion failure and is worth
        pointing at. It does not always: a zero exit with no output file
        lands here too, and sending the user after a file that does not
        exist wastes their time at the least helpful moment.
        """
        msg = f"the export finished but could not be moved into place: {exc}"
        # Only worth pointing at if there is a real encode there. An
        # empty reservation is nothing to recover.
        #
        # One guarded lookup, and any problem with it simply means no
        # recovery sentence. Explaining a failure must never become a
        # second way to fail: an exception raised here would escape
        # before `failed` was emitted and leave the run with no terminal
        # outcome at all.
        try:
            retained = self._temp_output.stat().st_size > 0
        except OSError:
            retained = False
        if retained:
            # The temp sits beside the *resolved* destination, which is
            # not `job.output`'s folder when that path is a symlink.
            msg += (f". The encoded file is still in "
                    f"{self._temp_output.parent} as {self._temp_output.name}")
        return msg

    def _discard_temp(self) -> None:
        """Drop this run's own temporary output, and nothing else.

        The path is one this run invented, so deleting it cannot destroy
        anything the user or another export owns - which is the whole
        reason the temp exists. ``job.output`` is never a candidate here.

        Failing to tidy up is not itself a terminal outcome: a user who
        cancelled still cancelled, and an encode that failed still failed
        for its own reason. So the problem is reported through the export
        log, which is already on screen, and swallowed otherwise.

        Without a reservation there is nothing of ours at that path, and
        whatever is there belongs to somebody else - deleting it would be
        the exact mistake the owned temp exists to avoid.

        Having reserved it once is not enough either. Promotion proves
        the file is still the one this run created before overwriting the
        destination with it; deleting a file is just as irreversible, so
        it proves the same thing first. Anything else at that path is
        somebody else's, and is left alone and reported.
        """
        if self._temp_identity is None:
            return
        try:
            st = os.lstat(self._temp_output)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.log.emit(
                f"could not check the temporary export file "
                f"{self._temp_output.name}: {exc}"
            )
            return
        if (not stat.S_ISREG(st.st_mode)
                or (st.st_dev, st.st_ino) != self._temp_identity):
            self.log.emit(
                f"{self._temp_output.name} is no longer the file this "
                f"export created, so it has been left alone"
            )
            return
        try:
            self._temp_output.unlink(missing_ok=True)
        except OSError as exc:
            self.log.emit(
                f"could not remove the temporary export file "
                f"{self._temp_output.name}: {exc}"
            )

    def _emit_abnormal(self, exc: Exception) -> None:
        """Terminal outcome for a run that did not reach a normal end.

        Killing the child can make whatever ran next raise, so an
        exception on a run that cancellation already claimed is a
        consequence of the user's stop, not a separate defect. Every
        other exception is a real failure and keeps the failure channel
        to itself - including one raised because ffmpeg exited nonzero on
        its own, which stays a failure however late the cancel arrives.
        """
        if self._cancel_claimed:
            self.cancelled.emit()
        else:
            self.failed.emit(str(exc))

    def _resolve_subtitle_path(self, sub: SubtitleTrack, tgt_w: int, tgt_h: int) -> Path:
        """Return the path ffmpeg's ``subtitles=`` filter should load.

        We always materialize a temp ASS file with ``PlayResX/PlayResY``
        matching the output resolution. That anchors libass's coordinate
        system to the output video so ``Fontsize=N`` renders N pixels
        tall — 1:1 with Cove's preview overlay. The previous path loaded
        the raw SRT; libass then converted it to ASS using its default
        PlayResY=288, which scaled any font up by ``out_h / 288`` and
        produced burn-ins 2–3× larger than the preview.

        Cues are written with the user's sync offset already applied so
        the live preview, sync dialog, and burn-in all stay in lockstep.
        """
        assert self._tmp_dir is not None, "_resolve_subtitle_path called outside run()"
        out = self._tmp_dir / f"{sub.id}.ass"
        out.write_text(_render_ass(sub, tgt_w, tgt_h), encoding="utf-8")
        return out

    # --- build --------------------------------------------------------

    def _build_command(self, output: Path | None = None) -> list[str]:
        """Build the ffmpeg command for this job, writing to ``output``.

        Every argument except the destination is a function of the job
        alone. ``run`` always passes the run-owned temp; leaving it unset
        yields the command for the job's requested destination, which is
        what the command-shape tests inspect.
        """
        job = self._job
        clips = sort_clips(job.clips)
        spec = ff.EXPORT_FORMATS.get(job.fmt_key)
        if spec is None:
            raise RuntimeError(f"unknown format {job.fmt_key!r}")

        is_audio_only = spec["vcodec"] is None
        needs_audio = spec["acodec"] is not None

        # Video/Project export still needs at least one clip. Audio-only
        # export may run on standalone added-audio tracks with no clips.
        if not clips and not (is_audio_only and job.audio_tracks):
            raise RuntimeError("no clips to export")

        # Build the list of segments on the timeline: either a clip, or a gap
        # (black + silent). Gaps between clips are filled with `color` /
        # `anullsrc` sources so concat matches. With no clips, this yields a
        # single silent gap spanning the added-audio duration.
        timeline_end = sequence_length(clips) if clips else _audio_only_duration(job.audio_tracks)
        segments = _segments_with_gaps(clips, timeline_end)

        tgt_w, tgt_h = resolve_target_size(clips, job.crop, job.width, job.height)

        cmd: list[str] = [ff.require_ffmpeg(), "-nostdin", "-y", "-hide_banner",
                          "-progress", "pipe:1", "-nostats", "-loglevel", "error"]

        # one -i per real clip; gaps are synthesized inside filter_complex.
        # Image clips need `-loop 1 -framerate 30 -t dur` before `-i` so
        # ffmpeg produces a finite video stream of the right length.
        clip_inputs: dict[str, int] = {}
        for c in clips:
            clip_inputs[c.id] = len(clip_inputs)
            if c.asset.kind == "image":
                img_dur = max(0.1, c.src_end - c.src_start) / max(0.01, c.speed)
                cmd += [
                    "-loop", "1",
                    "-framerate", "30",
                    "-t", f"{img_dur:.3f}",
                    "-i", str(c.path),
                ]
            else:
                cmd += ["-i", str(c.path)]

        # One -i per added-audio track. Parallel list of ffmpeg input indices
        # so the filter graph can reference them.
        add_track_indices: list[int] = []
        for track in job.audio_tracks:
            add_track_indices.append(len(clip_inputs) + len(add_track_indices))
            cmd += ["-i", str(track.path)]

        filter_complex, v_label, a_label = self._build_filtergraph(
            segments, clip_inputs, add_track_indices,
            tgt_w=tgt_w, tgt_h=tgt_h,
            is_audio_only=is_audio_only, needs_audio=needs_audio,
            acodec=spec.get("acodec"),
        )
        cmd += ["-filter_complex", filter_complex]

        if not is_audio_only:
            cmd += ["-map", f"[{v_label}]"]
        if needs_audio and a_label is not None:
            cmd += ["-map", f"[{a_label}]"]

        # region export (output-side trim — cheap and precise)
        if job.region_start is not None and job.region_end is not None:
            cmd += [
                "-ss", f"{max(0.0, job.region_start):.3f}",
                "-t",  f"{max(0.01, job.region_end - job.region_start):.3f}",
            ]

        if spec["vcodec"]:
            # Encoder choice is resolved here, outside the filtergraph: the
            # visual normalization above is identical no matter which
            # encoder ends up consuming it.
            encoder = resolve_export_video_encoder(spec, job.encoder_pref)
            cmd += build_export_video_encoder_args(encoder, fps=job.fps)
        if needs_audio and spec["acodec"]:
            cmd += ["-c:a", spec["acodec"]]
            if spec["acodec"] == "aac":
                cmd += ["-b:a", "192k"]
        cmd += list(spec.get("extra", []))
        cmd.append(str(job.output if output is None else output))
        return cmd

    def _build_filtergraph(
        self,
        segments: list[tuple[str, float, float, Clip | None]],
        clip_inputs: dict[str, int],
        add_track_indices: list[int],
        *, tgt_w: int, tgt_h: int,
        is_audio_only: bool, needs_audio: bool,
        acodec: str | None = None,
    ) -> tuple[str, str, str | None]:
        job = self._job
        parts: list[str] = []
        v_labels: list[str] = []
        a_labels: list[str] = []
        # Resolved once for the whole export, matching resolve_target_size:
        # either committed per-clip crops are authoritative, or the legacy
        # global crop is. Never both, so a stale global crop can't leak onto
        # clips the user left uncropped.
        per_clip_crop = has_per_clip_crop(job.clips)
        # libmp3lame works best with 44100 Hz fltp; force that in silence sources.
        if acodec == "libmp3lame":
            _null_sr = 44100
            _null_aformat = ",aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
        else:
            _null_sr = 48000
            _null_aformat = ""

        for i, (kind, seg_start, seg_end, clip) in enumerate(segments):
            seg_dur = max(0.01, seg_end - seg_start)
            if kind == "clip":
                c = clip
                assert c is not None
                inp = clip_inputs[c.id]
                is_image = c.asset.kind == "image"
                if not is_audio_only:
                    if is_image:
                        # Image input is already the right length (`-t`),
                        # so trim/setpts is unnecessary - just normalize
                        # pts to 0 and run through crop/scale/pad.
                        vchain = ["setpts=PTS-STARTPTS"]
                    else:
                        vchain = [f"trim=start={c.src_start:.3f}:end={c.src_end:.3f}",
                                  "setpts=PTS-STARTPTS"]
                    # Crop describes *source* framing, so it runs before the
                    # canvas normalization below - never on the already
                    # scaled/padded frame. Image and video share this one
                    # resolver so the two branches cannot drift apart.
                    seg_crop = (
                        effective_clip_crop_pixels(c) if per_clip_crop else job.crop
                    )
                    if seg_crop:
                        x, y, w, h = seg_crop
                        vchain.append(f"crop={w}:{h}:{x}:{y}")
                    vchain.append(
                        f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease"
                        ":force_divisible_by=2,"
                        f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                        "setsar=1"
                    )
                    if not is_image and abs(c.speed - 1.0) > 1e-6:
                        vchain.append(f"setpts={1.0/c.speed:.5f}*PTS")
                    # Mixed-resolution sources can reach concat with matching
                    # dimensions but different SAR/pix_fmt; setsar=1 above and
                    # yuv420p here keep every visual branch identical. For
                    # images it also normalizes RGBA/RGB24 sources.
                    vchain.append("format=yuv420p")
                    parts.append(f"[{inp}:v]" + ",".join(vchain) + f"[v{i}]")
                    v_labels.append(f"v{i}")
                if needs_audio:
                    # Image clips never contribute audio.
                    if (
                        not is_image and c.asset.has_audio and not c.muted
                        and c.linked_audio and not c.audio_removed
                    ):
                        achain = [f"atrim=start={c.src_start:.3f}:end={c.src_end:.3f}",
                                  "asetpts=PTS-STARTPTS"]
                        if abs(c.speed - 1.0) > 1e-6:
                            achain.append(f"atempo={_atempo_chain(c.speed)}")
                        volume = max(0.0, min(2.0, c.audio_volume))
                        if abs(volume - 1.0) > 1e-6:
                            achain.append(f"volume={volume:.3f}")
                        parts.append(f"[{inp}:a]" + ",".join(achain) + f"[a{i}]")
                    else:
                        parts.append(
                            f"anullsrc=channel_layout=stereo:sample_rate={_null_sr},"
                            f"atrim=duration={seg_dur:.3f},asetpts=PTS-STARTPTS"
                            f"{_null_aformat}[a{i}]"
                        )
                    a_labels.append(f"a{i}")
            else:  # gap
                if not is_audio_only:
                    parts.append(
                        f"color=c=black:s={tgt_w}x{tgt_h}:d={seg_dur:.3f}:r=30,"
                        f"setsar=1,format=yuv420p[v{i}]"
                    )
                    v_labels.append(f"v{i}")
                if needs_audio:
                    parts.append(
                        f"anullsrc=channel_layout=stereo:sample_rate={_null_sr},"
                        f"atrim=duration={seg_dur:.3f},asetpts=PTS-STARTPTS"
                        f"{_null_aformat}[a{i}]"
                    )
                    a_labels.append(f"a{i}")

        # concat across all segments
        n = len(segments)
        if n == 0:
            raise RuntimeError("empty timeline")

        v_out: str | None = None
        a_out: str | None = None
        if not is_audio_only:
            if len(v_labels) != n:
                raise RuntimeError(
                    f"internal export error: expected {n} video labels, got {len(v_labels)}"
                )
            if needs_audio:
                if len(a_labels) != n:
                    raise RuntimeError(
                        f"internal export error: expected {n} audio labels, got {len(a_labels)}"
                    )
                # Concat expects inputs interleaved per segment: v0,a0,v1,a1,...
                # NOT all-video-then-all-audio (which causes type-mismatch errors).
                interleaved = [lbl for pair in zip(v_labels, a_labels) for lbl in pair]
                parts.append(
                    f"{_join_filter_labels(interleaved)}"
                    f"concat=n={n}:v=1:a=1[vc][ac]"
                )
                v_out, a_out = "vc", "ac"
            else:
                parts.append(f"{_join_filter_labels(v_labels)}concat=n={n}:v=1:a=0[vc]")
                v_out = "vc"
        else:
            if len(a_labels) != n:
                raise RuntimeError(
                    f"internal export error: expected {n} audio labels, got {len(a_labels)}"
                )
            parts.append(f"{_join_filter_labels(a_labels)}concat=n={n}:v=0:a=1[ac]")
            a_out = "ac"

        # Added-audio tracks: each placed at its offset, plays for its own
        # duration (padded with silence), then mixed with the clip audio.
        if add_track_indices and needs_audio:
            total = (
                max(0.01, sequence_length(job.clips))
                if job.clips else _audio_only_duration(job.audio_tracks)
            )
            extra_labels: list[str] = []
            replace_any = False
            orig_volume = 1.0
            for i, track_idx in enumerate(add_track_indices):
                track = job.audio_tracks[i]
                offset = max(0.0, track.offset)
                natural_dur = track.duration if track.duration > 0 else total
                end_t = min(total, offset + natural_dur)
                play_dur = max(0.01, end_t - offset)
                pre_ms = int(round(offset * 1000))
                delay_stage = (
                    f"adelay={pre_ms}:all=1," if pre_ms > 0 else ""
                )
                label = f"extra_a{i}"
                trim_start = max(0.0, track.src_start)
                trim_end = trim_start + play_dur
                parts.append(
                    f"[{track_idx}:a]"
                    f"atrim=start={trim_start:.3f}:end={trim_end:.3f},"
                    f"asetpts=PTS-STARTPTS,"
                    f"{delay_stage}"
                    f"apad=whole_dur={total:.3f},"
                    f"volume={track.volume:.3f}[{label}]"
                )
                extra_labels.append(label)
                if track.replace:
                    replace_any = True
                orig_volume = track.original_volume

            if len(extra_labels) == 1:
                mixed_extra = extra_labels[0]
            else:
                joined = "".join(f"[{lbl}]" for lbl in extra_labels)
                parts.append(
                    f"{joined}amix=inputs={len(extra_labels)}:"
                    f"duration=longest:dropout_transition=0[extra_mix]"
                )
                mixed_extra = "extra_mix"

            if replace_any or a_out is None:
                a_out = mixed_extra
            else:
                parts.append(
                    f"[{a_out}]volume={orig_volume:.3f}[orig_a];"
                    f"[orig_a][{mixed_extra}]amix=inputs=2:"
                    f"duration=longest:dropout_transition=0[mix_a]"
                )
                a_out = "mix_a"

        # Burn-in subtitles last so they appear on the final frame
        # regardless of what the video went through. We build a fresh ASS
        # file with PlayResX/PlayResY matching the output, which means
        # every Fontsize / Outline / MarginV value we bake in is in
        # output pixels — the same sizing Cove's preview overlay uses.
        # No `force_style` needed since the ASS already carries the
        # resolved style block.
        if job.subtitles is not None and not is_audio_only and v_out:
            sub_source = self._resolve_subtitle_path(job.subtitles, tgt_w, tgt_h)
            sub_path = ff.escape_filter_arg(str(sub_source))
            parts.append(
                f"[{v_out}]subtitles='{sub_path}':"
                f"original_size={tgt_w}x{tgt_h}[v_sub]"
            )
            v_out = "v_sub"

        return ";".join(parts), v_out or "", a_out

    # --- run ----------------------------------------------------------

    def _execute(self, cmd: list[str]) -> None:
        job = self._job
        if job.region_start is not None and job.region_end is not None:
            total = max(0.01, job.region_end - job.region_start)
        else:
            total = max(0.01, job.total_timeline)
        with self._proc_lock:
            if self._cancel_claimed:
                # Cancelled before startup: nothing to launch, and the
                # outcome is already decided.
                return
            self._proc_starting = True

        # Deliberately outside the lock: `Popen` can block, and a Cancel
        # click must never wait on it. `_proc_starting` is what keeps the
        # window honest while we are in here.
        #
        # Reserving the temp sits inside this try for the same reason the
        # spawn does: it is part of the startup window, and every exit
        # from that window has to clear `_proc_starting` and let a cancel
        # that deferred to publication claim the run. A reservation that
        # fails while the user is cancelling is still a cancellation.
        try:
            self._reserve_temp()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                **_POPEN_KWARGS,
            )
        except BaseException:
            with self._proc_lock:
                self._proc_starting = False
                if self._cancel_awaiting_publication:
                    # No child was ever produced, so the deferred cancel
                    # owns the run after all.
                    self._cancel_claimed = True
            raise

        with self._proc_lock:
            self._proc = proc
            self._proc_starting = False
            if self._cancel_awaiting_publication:
                self._cancel_awaiting_publication = False
                self._claim_if_live(proc)

        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        stderr_lines: collections.deque[str] = collections.deque(maxlen=200)

        def _drain_stderr() -> None:
            for line in self._proc.stderr:
                line = line.rstrip()
                if line:
                    stderr_lines.append(line)
                    self.log.emit(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        for line in self._proc.stdout:
            if self._cancelled:
                # Just stop reading. Terminating belongs to whoever took
                # ownership: either the cancel claimed a live child and
                # already signalled it, or it did not claim - in which
                # case the child is terminal and must keep its status.
                # Re-terminating here would be redundant at best, and
                # deciding ownership here would relabel a genuine failure
                # as a cancellation.
                break
            line = line.strip()
            if not line:
                continue
            key, _, value = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
                t = int(value) / 1_000_000
                pct = min(1.0, max(0.0, t / total))
                self.progress.emit(int(pct * 100))
                self._update_eta(pct * 100)
            elif key == "progress" and value == "end":
                self.progress.emit(100)
                break

        rc = self._proc.wait()
        self._encode_ok = rc == 0
        stderr_thread.join(timeout=5)
        # A nonzero status is only ours to explain away if cancellation
        # actually claimed this run. `_cancel_claimed` can no longer turn
        # true once the child is dead, so a cancel arriving during the
        # join above cannot convert a genuine failure into a cancellation.
        if rc != 0 and not self._cancel_claimed:
            err = "\n".join(stderr_lines).strip()
            raise RuntimeError(f"ffmpeg exited {rc}: {err[-600:]}")

    def _update_eta(self, overall_pct: float) -> None:
        if overall_pct < 2.0:
            return
        elapsed = time.monotonic() - self._started_wall
        if elapsed < 0.5:
            return
        eta_raw = max(0.0, elapsed * (100.0 - overall_pct) / overall_pct)
        if self._eta_smoothed is None:
            self._eta_smoothed = eta_raw
        else:
            alpha = 0.35
            self._eta_smoothed = alpha * eta_raw + (1 - alpha) * self._eta_smoothed
        self.eta.emit(self._eta_smoothed)


#: A committed crop covering the whole source frame. The UI canonicalizes
#: "no crop" to ``None``, but the domain deliberately allows this tuple to
#: be stored, so the exporter has to treat it as "no effective crop".
_FULL_FRAME_CROP = (0.0, 0.0, 1.0, 1.0)


def effective_clip_crop_pixels(clip: Clip) -> tuple[int, int, int, int] | None:
    """Convert one clip's committed normalized ``crop_rect`` into an even,
    in-bounds pixel crop ``(x, y, w, h)``, or ``None`` when the clip has no
    effective crop.

    This is the single normalized-to-pixel conversion for the exporter -
    both the video and the image branch call it, so the two can never
    diverge. The rounding and even-snapping mirror ``MainWindow._crop_pixels``
    so a rectangle committed in the UI exports as the same pixels the crop
    overlay showed.

    ``crop_preset`` is editor metadata and is deliberately not consulted: a
    Free custom crop exports exactly like a preset crop. Source dimensions
    come from the already-probed ``clip.asset``; this helper never probes.
    """
    rect = clip.crop_rect
    if rect is None or tuple(rect) == _FULL_FRAME_CROP:
        return None
    sw, sh = clip.asset.width, clip.asset.height
    if sw <= 0 or sh <= 0:
        return None
    nx, ny, nw, nh = rect
    x = int(round(nx * sw)); y = int(round(ny * sh))
    w = int(round(nw * sw)); h = int(round(nh * sh))
    w -= w % 2; h -= h % 2
    x = max(0, min(sw - w, x - x % 2))
    y = max(0, min(sh - h, y - y % 2))
    if w < 2 or h < 2:
        return None
    # A rectangle that resolves to the whole frame is not an effective
    # crop, even when the normalized tuple was not exactly (0, 0, 1, 1) -
    # float noise from the overlay's aspect math lands here routinely.
    # Emitting crop=<full frame> would be a no-op filter, and worse, it
    # would flip the export into per-clip mode and discard the legacy
    # global crop for no reason. `sw - sw % 2` is the widest even crop an
    # odd-width source can yield, so it counts as full coverage too.
    if x == 0 and y == 0 and w >= sw - sw % 2 and h >= sh - sh % 2:
        return None
    return (x, y, w, h)


def has_per_clip_crop(clips: list[Clip]) -> bool:
    """True when committed per-clip crop state is authoritative for this
    export, i.e. at least one clip carries an effective (non-full-frame)
    crop. Resolved once per export: in per-clip mode the legacy global
    ``ExportJob.crop`` is ignored entirely, so an old selection-scoped crop
    cannot leak onto clips the user never cropped."""
    return any(effective_clip_crop_pixels(c) is not None for c in clips)


def resolve_target_size(
    clips: list[Clip],
    crop: tuple[int, int, int, int] | None,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    """Output size policy, in two modes, with the final dimensions forced
    even so encoders accept them.

    Legacy mode (no clip carries an effective ``crop_rect``): honor the
    global ``crop``, else an explicit width+height pair, else the first
    real (non-gap) visual clip, else 1280x720.

    Per-clip mode (at least one effective ``crop_rect``): an explicit
    width+height pair wins over every crop, because a mixed-crop timeline
    has no single crop that could define the canvas. Without one, the
    canvas is the *first visual clip's* effective geometry - its crop size
    if it has a crop, else its native size. Timeline order is authoritative
    (``_build_command`` passes timeline-sorted clips); the selected clip,
    the last crop, and the largest crop deliberately have no say, so the
    same project always exports at the same size.
    """
    first_real = next((c for c in clips if c.asset.width > 0), None)
    if has_per_clip_crop(clips):
        if width and height:
            tgt_w, tgt_h = width, height
        elif first_real is not None:
            first_crop = effective_clip_crop_pixels(first_real)
            if first_crop is not None:
                tgt_w, tgt_h = first_crop[2], first_crop[3]
            else:
                tgt_w, tgt_h = first_real.asset.width, first_real.asset.height
        else:
            tgt_w, tgt_h = 1280, 720
    elif crop:
        _, _, tgt_w, tgt_h = crop
    elif width and height:
        tgt_w, tgt_h = width, height
    elif first_real:
        tgt_w, tgt_h = first_real.asset.width, first_real.asset.height
    else:
        tgt_w, tgt_h = 1280, 720
    return tgt_w - tgt_w % 2, tgt_h - tgt_h % 2


def _audio_only_duration(audio_tracks: list[AudioTrack]) -> float:
    """Timeline length for a no-video-clip audio-only export, derived from
    each added-audio track's placement (offset + span) — the same
    offset/duration semantics used to place tracks during mixing.

    With no video clips there is no timeline length to fall back on, so a
    track left at its ``duration <= 0`` ("use full input") default has no
    resolvable end time here — fail fast instead of guessing a length.
    """
    ends = []
    for t in audio_tracks:
        if t.duration <= 0:
            raise RuntimeError(
                "cannot determine audio-only export duration: added-audio "
                "track has no explicit duration and there are no video "
                "clips to derive the timeline length from"
            )
        ends.append(max(0.0, t.offset) + t.duration)
    return max(0.01, max(ends))


def _segments_with_gaps(clips: list[Clip], end: float) -> list[tuple[str, float, float, Clip | None]]:
    """Return [(kind, seg_start, seg_end, clip|None)] covering [0, end)."""
    out: list[tuple[str, float, float, Clip | None]] = []
    cursor = 0.0
    for c in sort_clips(clips):
        if c.timeline_start > cursor + 1e-3:
            out.append(("gap", cursor, c.timeline_start, None))
        out.append(("clip", c.timeline_start, c.timeline_end, c))
        cursor = c.timeline_end
    if end > cursor + 1e-3:
        out.append(("gap", cursor, end, None))
    return out


def _join_filter_labels(labels: list[str]) -> str:
    """Return ffmpeg link labels, accepting only generated filter labels."""
    for label in labels:
        if _FILTER_LABEL_RE.fullmatch(label) is None:
            raise RuntimeError(f"internal export error: invalid concat label {label!r}")
    return "".join(f"[{label}]" for label in labels)


def _render_ass(sub: SubtitleTrack, out_w: int, out_h: int) -> str:
    """Serialize a SubtitleTrack to a full ASS script with PlayRes matching
    the output video. Applies the sync offset and bakes in Fontname,
    Fontsize (output pixels), PrimaryColour, OutlineColour, Outline,
    Alignment, and a bottom/top MarginV sized to 6% of the video height
    — same safe margin the preview overlay uses."""
    primary = _hex_to_libass(sub.primary_color)
    outline_c = _hex_to_libass(sub.outline_color)
    alignment = 8 if sub.position == "top" else 2
    # ASS is comma-separated; strip anything that would break style parsing.
    font_name = (sub.font_family or "Arial").replace(",", " ").replace(":", " ")
    font_size = max(8, int(sub.font_size))
    outline_w = max(0, int(sub.outline))
    margin_v = max(4, int(round(out_h * 0.06)))

    # ASS Style format (libass reference):
    #   Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,
    #   OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,
    #   ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,
    #   Alignment, MarginL, MarginR, MarginV, Encoding
    style_fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    style_row = (
        f"Style: Default,{font_name},{font_size},{primary},&H000000FF,"
        f"{outline_c},&H00000000,-1,0,0,0,100,100,0,0,1,{outline_w},0,"
        f"{alignment},20,20,{margin_v},1"
    )

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        f"PlayResX: {out_w}",
        f"PlayResY: {out_h}",
        "",
        "[V4+ Styles]",
        style_fmt,
        style_row,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    offset_s = sub.offset_ms / 1000.0
    for start, end, text in sub.cues:
        s = max(0.0, start + offset_s)
        e = max(s + 0.01, end + offset_s)
        # `,` separates Dialogue fields — the Text field is last so commas
        # inside Text are fine. `{` / `}` toggle libass override codes so
        # we escape them to stop a stray `{` in the caption from eating
        # the rest of the line. `\N` is a hard break; SRT uses `\n`.
        txt = (
            text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}")
                .replace("\r", "")
                .replace("\n", "\\N")
        )
        lines.append(
            f"Dialogue: 0,{_format_ass_ts(s)},{_format_ass_ts(e)},Default,,0,0,0,,{txt}"
        )

    return "\n".join(lines) + "\n"


def _format_ass_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - (h * 3600 + m * 60)
    return f"{h}:{m:02d}:{s:05.2f}"


def _format_srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _hex_to_libass(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    # Alpha 00 = fully opaque in libass.
    return f"&H00{b.upper()}{g.upper()}{r.upper()}&"


def _atempo_chain(speed: float) -> str:
    s = max(0.01, speed)
    chain: list[float] = []
    while s < 0.5:
        chain.append(0.5)
        s /= 0.5
    while s > 2.0:
        chain.append(2.0)
        s /= 2.0
    chain.append(s)
    return ",atempo=".join(f"{v:.4f}" for v in chain)


def resolve_export_video_encoder(spec: dict, pref: str) -> str | None:
    """Map (format spec, user preference) onto one concrete video encoder.

    Audio-only formats have no video encoder at all and never trigger a
    hardware probe. "cpu" is honoured without probing either. "auto"
    prefers NVENC, then AMF, then the CPU encoder. An explicit hardware
    choice that is not genuinely usable on this machine falls back to the
    CPU encoder for the format - never to the other vendor - so a stale
    preference carried over from another machine still exports.
    """
    cpu_codec = spec["vcodec"]
    if cpu_codec is None:
        return None
    pref = ff.normalize_encoder_pref(pref)
    if pref == "cpu":
        return cpu_codec
    nvenc_codec = spec.get("nvenc_codec")
    amf_codec = spec.get("amf_codec")
    if pref == "nvenc":
        if nvenc_codec and ff.nvenc_available(nvenc_codec):
            return nvenc_codec
        return cpu_codec
    if pref == "amf":
        if amf_codec and ff.amf_available(amf_codec):
            return amf_codec
        return cpu_codec
    if nvenc_codec and ff.nvenc_available(nvenc_codec):
        return nvenc_codec
    if amf_codec and ff.amf_available(amf_codec):
        return amf_codec
    return cpu_codec


def start_export(job: ExportJob) -> tuple[QThread, ExportWorker]:
    thread = QThread()
    worker = ExportWorker(job)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    # Cancellation is a terminal outcome too: without this the thread never
    # finishes and `thread.finished -> _reset_after_export` never runs.
    worker.cancelled.connect(thread.quit)
    return thread, worker
