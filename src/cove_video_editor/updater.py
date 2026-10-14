"""Auto-updater backed by the GitHub releases API.

Philosophy: absolutely never silently replace the user's binary. We check
the latest release in the background, tell the user about it, and let them
kick off the actual install.

For AppImage installs we *can* do the download-and-swap end-to-end (the
kernel keeps the running mmap alive across an overwrite, so replacing the
file and re-execing works). For every other packaging (Windows Setup or
Portable, .deb, source) we just open the GitHub release page — the user
runs the installer themselves from there.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal


GITHUB_REPO = "Sin213/cove-video-editor"
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
FETCH_DEADLINE_S = 10.0
DOWNLOAD_CONNECT_DEADLINE_S = 45
DOWNLOAD_MIN_RATE_BPS = 128 * 1024
DOWNLOAD_BODY_MIN_S = 120
DOWNLOAD_BODY_MAX_S = 3600
DOWNLOAD_VERIFY_DEADLINE_S = 120
DOWNLOAD_CANCELLED_MESSAGE = "cancelled"
DOWNLOAD_CONNECT_TIMEOUT_MESSAGE = (
    "Timed out waiting for the update server. Check your internet connection, "
    "then restart Cove to be offered the update again, or get it from "
    f"github.com/{GITHUB_REPO}/releases."
)
DOWNLOAD_BODY_TIMEOUT_MESSAGE = (
    "Timed out: the download was too slow or stalled and was stopped. Check "
    "your internet connection, then restart Cove to be offered the update "
    f"again, or get it from github.com/{GITHUB_REPO}/releases."
)
DOWNLOAD_VERIFY_TIMEOUT_MESSAGE = (
    "Timed out while verifying the download, so it was discarded. Restart "
    "Cove to be offered the update again, or get it from "
    f"github.com/{GITHUB_REPO}/releases."
)
_DOWNLOAD_TIMEOUT_MESSAGES = {
    "connect": DOWNLOAD_CONNECT_TIMEOUT_MESSAGE,
    "body": DOWNLOAD_BODY_TIMEOUT_MESSAGE,
    "verify": DOWNLOAD_VERIFY_TIMEOUT_MESSAGE,
}
# Workers poll cancellation every ~25 ms. Joins are upper bounds, not network
# timeouts: daemon I/O has LOGICAL deadlines and cannot publish after abandon.
# Retain any running Qt pair until it stops; never terminate it.
SHUTDOWN_JOIN_MS = 22_000

# Keep both objects owned even if a window never receives closeEvent (for
# example, an application event loop ends via exit()). Only stopped threads
# may be released. The single atexit hook runs before Qt tears them down.
_LIVE_UPDATE_THREADS: list[tuple[QThread, QObject]] = []
_SHUTDOWN_HOOK_REGISTERED = False


def _prune_update_threads() -> None:
    _LIVE_UPDATE_THREADS[:] = [
        pair for pair in _LIVE_UPDATE_THREADS if not pair[0].isFinished()
    ]


def _register_update_thread(thread: QThread, worker: QObject) -> None:
    global _SHUTDOWN_HOOK_REGISTERED
    _prune_update_threads()
    _LIVE_UPDATE_THREADS.append((thread, worker))
    thread.finished.connect(_prune_update_threads, Qt.QueuedConnection)
    if not _SHUTDOWN_HOOK_REGISTERED:
        atexit.register(shutdown_update_threads)
        _SHUTDOWN_HOOK_REGISTERED = True


def shutdown_update_threads(timeout_ms: int = SHUTDOWN_JOIN_MS) -> list[tuple[QThread, QObject]]:
    """Cancel and join owned updater threads within one total time budget.

    Both updater workers poll cancellation every ~25 ms, independently of
    blocking daemon I/O. Their deadlines are LOGICAL: DNS or sockets may remain
    blocked, but those daemons hold no Qt objects and cannot publish. The 22 s
    default is a join upper bound; waits return as soon as threads stop. Retain
    and return any live Qt pair, never terminate it.
    """
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    pairs = list(_LIVE_UPDATE_THREADS)
    for thread, worker in pairs:
        worker.cancel()
        thread.quit()
    for thread, _worker in pairs:
        remaining = int((deadline - time.monotonic()) * 1000)
        thread.wait(max(0, remaining))
    _LIVE_UPDATE_THREADS[:] = [pair for pair in _LIVE_UPDATE_THREADS if pair[0].isRunning()]
    return list(_LIVE_UPDATE_THREADS)


@dataclass
class UpdateInfo:
    latest_version: str
    release_url: str
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int = 0


def _parse_version(v: str) -> tuple[int, int, int]:
    v = v.strip().lstrip("vV")
    out: list[int] = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
        if len(out) == 3:
            break
    while len(out) < 3:
        out.append(0)
    return (out[0], out[1], out[2])


def version_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


class ChecksumError(RuntimeError):
    """Raised when the downloaded asset's sha256 does not match its sidecar.

    Surfaced as a typed failure so the download path can refuse to swap
    the running binary on a tampered or truncated download."""


def _parse_sha256_sidecar(text: str) -> str:
    """Pull the hex hash out of a ``sha256sum`` / ``Get-FileHash`` sidecar."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0]
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
        raise ChecksumError(f"unrecognized sidecar contents: {line!r}")
    raise ChecksumError("empty sidecar")


def _sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def fetch_sha256_sidecar(url: str, timeout: float = 20.0) -> str:
    """GET the sidecar URL and return the parsed sha256 hex digest."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "cove-video-editor-updater"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Sidecar files are tiny; cap the read so a hostile redirect
        # can't dump unbounded bytes.
        raw = resp.read(4096).decode("ascii", errors="replace")
    return _parse_sha256_sidecar(raw)


def verify_sha256(path: Path, sidecar_url: str) -> None:
    """Verify ``path`` matches the hash advertised by ``sidecar_url``.

    On any failure (network error, malformed sidecar, hash mismatch) the
    partial download at ``path`` is unlinked and ChecksumError is raised
    so the caller never swaps in an unverified binary."""
    try:
        expected = fetch_sha256_sidecar(sidecar_url)
    except ChecksumError:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise ChecksumError(f"could not fetch sidecar: {exc}") from exc
    actual = _sha256_of_file(path)
    if actual != expected:
        path.unlink(missing_ok=True)
        raise ChecksumError(
            f"sha256 mismatch: expected {expected}, got {actual}",
        )


def bundle_kind() -> str:
    """Rough detection of how this instance was packaged so we can pick the
    matching release asset."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if sys.platform == "win32":
        exe_path = Path(sys.executable).resolve()
        if not getattr(sys, "frozen", False):
            return "source"
        exe_str = str(exe_path)
        if "Program Files" in exe_str or r"AppData\Local" in exe_str:
            return "win-setup"
        return "win-portable"
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        return "deb"
    return "source"


def preferred_asset(kind: str, assets: list[dict]) -> dict | None:
    def first_match(predicate) -> dict | None:
        return next((a for a in assets if predicate(a["name"].lower())), None)

    if kind == "appimage":
        return first_match(lambda n: n.endswith(".appimage"))
    if kind == "deb":
        return first_match(lambda n: n.endswith(".deb"))
    if kind == "win-setup":
        return first_match(lambda n: "setup" in n and n.endswith(".exe"))
    if kind == "win-portable":
        return first_match(lambda n: "portable" in n and n.endswith(".exe"))
    return None


def fetch_latest_release(timeout: float = 8.0) -> dict | None:
    req = urllib.request.Request(
        LATEST_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "cove-video-editor-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001
        return None


def _fetch_release(holder: dict, completed: threading.Event) -> None:
    """Plain daemon-thread work: no Qt objects, signals or worker references."""
    try:
        holder["data"] = fetch_latest_release()
    except Exception as exc:  # noqa: BLE001
        holder["error"] = str(exc)
    finally:
        completed.set()


class UpdateCheckWorker(QObject):
    updateAvailable = Signal(object)   # UpdateInfo
    noUpdate = Signal()
    failed = Signal(str)

    def __init__(self, current_version: str) -> None:
        super().__init__()
        self._current = current_version
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        """Wait at most FETCH_DEADLINE_S for a release, or until cancelled.

        This is a LOGICAL deadline, not cancellation of DNS or socket I/O.
        The inner daemon may remain blocked in getaddrinfo/urlopen after this
        QThread finishes. It holds no Qt objects; its late result is discarded,
        and process exit does not join it. The fetch's 8 s socket timeout stays
        unchanged.
        """
        if self._cancelled.is_set():
            self.failed.emit("cancelled")
            return
        holder: dict = {}
        completed = threading.Event()
        deadline = time.monotonic() + FETCH_DEADLINE_S
        fetch = threading.Thread(
            target=_fetch_release, args=(holder, completed), daemon=True,
            name="cove-update-fetch",
        )
        fetch.start()
        while True:
            if self._cancelled.is_set():
                self.failed.emit("cancelled")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.failed.emit("could not reach the releases API (timed out)")
                return
            if completed.wait(min(0.025, remaining)):
                break
        # Cancellation/deadline wins even if completion arrives in the same
        # wait slice. Only this QThread emits signals, never the inner fetch.
        if self._cancelled.is_set():
            self.failed.emit("cancelled")
            return
        if time.monotonic() >= deadline:
            self.failed.emit("could not reach the releases API (timed out)")
            return
        if "error" in holder:
            self.failed.emit(f"could not reach the releases API: {holder['error']}")
            return
        data = holder["data"]
        if data is None:
            self.failed.emit("could not reach the releases API")
            return
        tag = data.get("tag_name") or ""
        if not tag:
            self.failed.emit("release had no tag_name")
            return
        latest = tag.lstrip("vV")
        if not version_newer(latest, self._current):
            self.noUpdate.emit()
            return
        assets = data.get("assets") or []
        asset = preferred_asset(bundle_kind(), assets)
        info = UpdateInfo(
            latest_version=latest,
            release_url=(
                data.get("html_url")
                or f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"
            ),
            asset_name=asset["name"] if asset else None,
            asset_url=asset["browser_download_url"] if asset else None,
            asset_size=int(asset["size"]) if asset else 0,
        )
        self.updateAvailable.emit(info)


def _download_body_budget(total: int) -> float:
    if total <= 0:
        return DOWNLOAD_BODY_MAX_S
    return min(DOWNLOAD_BODY_MAX_S,
               max(DOWNLOAD_BODY_MIN_S, total / DOWNLOAD_MIN_RATE_BPS))


@dataclass
class _DownloadState:
    """Plain daemon state. Contains no QObject, signal or worker reference."""

    url: str
    temp: Path
    output: object
    lock: object = field(default_factory=threading.Lock)
    abandoned: threading.Event = field(default_factory=threading.Event)
    completed: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)
    stage: str = "connect"
    started: float = field(default_factory=time.monotonic)
    budget: float = DOWNLOAD_CONNECT_DEADLINE_S
    total: int = 0
    written: int = 0
    error: str | None = None
    done: bool = False


def _advance_download_phase(state: _DownloadState, stage: str, budget: float) -> bool:
    """An overdue phase cannot borrow the following phase's budget."""
    with state.lock:
        now = time.monotonic()
        if now > state.started + state.budget:
            state.error = _DOWNLOAD_TIMEOUT_MESSAGES[state.stage]
            state.wake.set()
            return False
        state.stage, state.started, state.budget = stage, now, budget
        state.wake.set()
        return True


def _download_to_temp(state: _DownloadState) -> None:
    """Blocking I/O in one plain daemon; only the supervisor can publish."""
    try:
        if state.abandoned.is_set():
            return
        req = urllib.request.Request(
            state.url, headers={"User-Agent": "cove-video-editor-updater"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with state.lock:
                state.total = total
            if not _advance_download_phase(state, "body", _download_body_budget(total)):
                return
            while not state.abandoned.is_set():
                chunk = resp.read(262144)
                if state.abandoned.is_set():
                    return
                if not chunk:
                    break
                state.output.write(chunk)
                with state.lock:
                    state.written += len(chunk)
            if state.abandoned.is_set():
                return
            # End-of-body starts the verification budget, including flush and
            # response cleanup as well as the sidecar fetch and hashing.
            if not _advance_download_phase(state, "verify", DOWNLOAD_VERIFY_DEADLINE_S):
                return
        state.output.close()
        if state.abandoned.is_set():
            return
        # Mandatory sidecar verification applies to the private temp only;
        # verify_sha256 retains its existing unlink-on-failure semantics.
        verify_sha256(state.temp, state.url + ".sha256")
        with state.lock:
            state.done = True
            state.wake.set()
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = str(exc)
            state.wake.set()
    finally:
        state.output.close()
        state.completed.set()
        state.wake.set()


def _publish_download(temp: Path, dest: Path) -> None:
    """The supervisor alone publishes verified bytes atomically."""
    os.replace(temp, dest)


class DownloadWorker(QObject):
    """Supervise a private download, emitting all signals on the QThread."""

    progress = Signal(int)           # 0–100
    finished = Signal(str)           # destination path
    failed = Signal(str)

    def __init__(self, url: str, dest: Path) -> None:
        super().__init__()
        self._url = url
        self._dest = dest
        self._cancelled = threading.Event()
        self._wake = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        self._wake.set()

    def is_cancelled(self) -> bool:
        """Read-only cancellation state, including after finished was queued."""
        return self._cancelled.is_set()

    def run(self) -> None:
        """Poll cancel and finite per-phase LOGICAL deadlines every 25 ms.

        DNS and socket syscalls cannot be interrupted here. A stalled daemon
        may outlive this QThread, but holds no Qt objects, writes only its open
        private temp, and can never publish. Abandon unlinks that temp without
        waiting for the daemon (late writes reach an unlinked inode).
        Cancellation and daemon phase/completion changes wake the supervisor
        immediately. Clear the wake handle before reading fresh state, so a
        concurrent notification cannot be lost; the poll ceiling stays 25 ms.
        """
        if self._cancelled.is_set():
            self.failed.emit(DOWNLOAD_CANCELLED_MESSAGE)
            return
        state = None
        temp = None
        output = None
        owns_temp = False
        daemon_started = False
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            temp = self._dest.with_name(
                f"{self._dest.name}.{os.getpid()}-{secrets.token_hex(8)}.part",
            )
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            owns_temp = True
            try:
                output = os.fdopen(fd, "wb")
            except Exception:
                os.close(fd)
                raise
            state = _DownloadState(self._url, temp, output,
                                   budget=DOWNLOAD_CONNECT_DEADLINE_S, wake=self._wake)
            download = threading.Thread(
                target=_download_to_temp, args=(state,), daemon=True,
                name="cove-update-download",
            )
            download.start()
            daemon_started = True
            last_progress = -1
            while True:
                self._wake.clear()
                if self._cancelled.is_set():
                    raise RuntimeError(DOWNLOAD_CANCELLED_MESSAGE)
                with state.lock:
                    stage, started, budget = state.stage, state.started, state.budget
                    total, written = state.total, state.written
                    error, done = state.error, state.done
                if error is not None:
                    raise RuntimeError(error)
                if total > 0:
                    progress = min(100, max(0, int(written * 100 / total)))
                    if progress != last_progress:
                        self.progress.emit(progress)
                        last_progress = progress
                if done:
                    break
                remaining = started + budget - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(_DOWNLOAD_TIMEOUT_MESSAGES[stage])
                self._wake.wait(min(0.025, remaining))
            if self._cancelled.is_set():
                raise RuntimeError(DOWNLOAD_CANCELLED_MESSAGE)
            # Verification/publication use a temp pathname in a user-owned
            # cache. An actor who can alter that directory already controls
            # the user's files (the accepted cache-directory threat model).
            _publish_download(temp, self._dest)
            if self._cancelled.is_set():
                # Roll back the bytes just published. A stale pre-existing
                # cache entry replaced by us is lost in this narrow race.
                self._dest.unlink(missing_ok=True)
                raise RuntimeError(DOWNLOAD_CANCELLED_MESSAGE)
            self.finished.emit(str(self._dest))
        except Exception as exc:  # noqa: BLE001
            if state is not None:
                state.abandoned.set()
            if output is not None and not daemon_started:
                output.close()
            try:
                if owns_temp:
                    temp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            self.failed.emit(str(exc))


def start_check(current_version: str) -> tuple[QThread, UpdateCheckWorker]:
    thread = QThread()
    worker = UpdateCheckWorker(current_version)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    # quit() is thread-safe. Direct delivery lets joins finish even when the
    # GUI event loop is blocked in closeEvent or has already returned.
    worker.updateAvailable.connect(thread.quit, Qt.DirectConnection)
    worker.noUpdate.connect(thread.quit, Qt.DirectConnection)
    worker.failed.connect(thread.quit, Qt.DirectConnection)
    _register_update_thread(thread, worker)
    return thread, worker


def start_download(url: str, dest: Path) -> tuple[QThread, DownloadWorker]:
    thread = QThread()
    worker = DownloadWorker(url, dest)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit, Qt.DirectConnection)
    worker.failed.connect(thread.quit, Qt.DirectConnection)
    _register_update_thread(thread, worker)
    return thread, worker


def swap_in_appimage(new_path: Path) -> Path:
    """Install `new_path` next to the running AppImage under its own
    versioned filename, remove the old file, and return the new path.
    Caller is responsible for relaunching.

    Keeping the release asset's filename (instead of overwriting the old
    file in place) matches electron-updater semantics and keeps the
    on-disk name truthful - external launchers like Cove Nexus derive the
    installed version from it."""
    current = os.environ.get("APPIMAGE")
    if not current:
        raise RuntimeError("APPIMAGE env var not set - not an AppImage install")
    old = Path(current).resolve()
    target = old.parent / new_path.name
    tmp = target.with_name(target.name + ".part")
    shutil.move(str(new_path), str(tmp))
    mode = os.stat(tmp).st_mode
    os.chmod(tmp, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, target)
    if target != old:
        try:
            old.unlink()  # unlinking the running file is fine on Linux
        except OSError:
            pass
    os.environ["APPIMAGE"] = str(target)
    return target


def relaunch(path: Path) -> None:
    """Spawn `path` detached from the current process, then return so the
    caller can quit the Qt app cleanly."""
    # start_new_session detaches from our process group so the child survives
    # our exit — the running process keeps the old binary mmap'd while the
    # new one takes over the path on disk.
    subprocess.Popen(
        [str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
