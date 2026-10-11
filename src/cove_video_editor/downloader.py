from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from collections import deque
from json import dumps as _escape_json_string
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QObject, QStandardPaths, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_QUOTED_MP4_RE = re.compile(r'"([^"]+\.mp4)"')


def _quality_format(max_height: int | None = None) -> str:
    height = f"[height<={max_height}]" if max_height is not None else ""
    return "/".join(
        (
            f"bestvideo[ext=mp4][vcodec^=avc1]{height}+bestaudio[ext=m4a][acodec^=mp4a]",
            f"bestvideo[ext=mp4][vcodec^=avc1]{height}+bestaudio[ext=m4a]",
            f"bestvideo[ext=mp4]{height}+bestaudio[ext=m4a]",
            f"best[ext=mp4][vcodec^=avc1][acodec^=mp4a]{height}",
            f"best[ext=mp4]{height}",
            f"bestvideo{height}+bestaudio",
            f"best{height}",
        ),
    )


QUALITY_FORMATS = {
    "Best": _quality_format(),
    "1080p": _quality_format(1080),
    "720p": _quality_format(720),
    "480p": _quality_format(480),
    "360p": _quality_format(360),
}

AUTH_MODES = ("None", "Browser cookies", "Cookie file")
YT_DLP_MODES = ("Automatic", "Custom executable")
YT_DLP_INFO_NEUTRAL = "yt-dlp: version shown when a download starts"
SUPPORTED_BROWSERS = ("brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale")
CONFIG_NOTE = "External yt-dlp configuration is ignored (--ignore-config)."


class DownloadInputError(ValueError):
    """An input cannot safely be passed to yt-dlp."""


def browser_specification(browser: str, profile: str = "") -> str:
    if browser not in SUPPORTED_BROWSERS:
        raise DownloadInputError("Choose a supported browser.")
    # Trim outer whitespace; preserve internal spaces, Unicode and drive colons.
    if len(profile) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in profile):
        raise DownloadInputError("Profile must not contain control characters.")
    profile = profile.strip()
    if (len(profile) > 1024 or "::" in profile or profile.startswith(("+", ":"))
            or any(ord(c) < 32 or ord(c) == 127 for c in profile)):
        raise DownloadInputError("Use a profile name or path without control characters, :: or a leading +.")
    return browser + (":" + profile if profile else "")


def validate_cookie_file(value: str | Path) -> Path:
    if not str(value).strip():
        raise DownloadInputError("Choose a cookie file.")
    try:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024 or not os.access(path, os.R_OK):
            raise DownloadInputError("Choose a readable regular cookie file no larger than 16 MiB.")
        with path.open("rb"):
            pass  # Access check only: cookie contents never enter Cove.
        return path
    except (OSError, ValueError, RuntimeError):
        raise DownloadInputError("Choose a readable regular cookie file no larger than 16 MiB.") from None


def build_download_command(base_command, *, url, quality, output_folder,
                           ffmpeg_path=None, auth_mode="None", browser="",
                           browser_profile="", cookie_file="") -> list[str]:
    url = url.strip()
    if not url or url.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise DownloadInputError("Paste a video URL that does not start with '-'.")
    if auth_mode not in AUTH_MODES:
        raise DownloadInputError("Choose a valid sign-in mode.")
    # Verified against yt-dlp 2026.08.19 --help; external config must not
    # enable cookie access behind the user's visible consent choice.
    cmd = [*base_command, "--ignore-config", "--newline", "--no-playlist",
           "--merge-output-format", "mp4", "--remux-video", "mp4",
           "--format-sort", "vcodec:h264,acodec:aac,ext:mp4:m4a",
           "-f", QUALITY_FORMATS.get(quality, QUALITY_FORMATS["Best"]),
           "-P", str(output_folder), "-o", "%(title).200B [%(id)s].%(ext)s",
           "--print", "after_move:filepath"]
    if ffmpeg_path:
        cmd.extend(["--ffmpeg-location", ffmpeg_path])
    if auth_mode == "Browser cookies":
        cmd.extend(["--cookies-from-browser", browser_specification(browser, browser_profile)])
    elif auth_mode == "Cookie file":
        cmd.extend(["--cookies", str(validate_cookie_file(cookie_file))])
    return [*cmd, url]


def sanitize(text: str, *, cookie_file="", browser_profile="", extra_literals=()) -> str:
    text = str(text)
    cookie_paths = set(map(str, cookie_file)) if isinstance(cookie_file, (tuple, list, set, frozenset)) else {str(cookie_file)}
    secrets = {*(str(value) for value in cookie_paths), str(browser_profile), str(browser_profile).strip(),
               *(str(value) for value in extra_literals)} - {""}
    patterns = set()
    for secret in secrets:
        path_like = "/" in secret or "\\" in secret or secret in cookie_paths
        variants = {secret, secret.replace("\\", "/"), secret.replace("/", "\\")} if path_like else {secret}
        for value in variants:
            forms = {value, repr(value)[1:-1], repr(repr(value))[1:-1],
                     _escape_json_string(value)[1:-1], _escape_json_string(value, ensure_ascii=False)[1:-1], shlex.quote(value)}
            patterns.update((re.escape(form), re.I if path_like else 0) for form in forms)
        if path_like:
            parts = re.split(r"[\\/]", secret)
            for encode in (str, lambda s: repr(s)[1:-1], lambda s: _escape_json_string(s)[1:-1],
                           lambda s: _escape_json_string(s, ensure_ascii=False)[1:-1],
                           lambda s: repr(repr(s)[1:-1])[1:-1]):
                patterns.add((r"(?:/|\\{1,4})".join(re.escape(encode(part)) for part in parts), re.I))
    literals = [re.compile(f"(?=({pattern}))", flags) for pattern, flags in sorted(patterns, key=lambda p: len(p[0]), reverse=True)]
    # Deferred: isolated parent/basename, NFC/NFD, 8.3 names and U+FFFD bytes.
    argument = re.compile(r"(?P<flag>--cookies(?:-from-browser)?)(?P<separator>['\"]?[ \t]*,[ \t]*|=|[ \t]+)(?P<value>(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,'\"\]\)]+)+)", re.I)
    rules = [
        r"(?im)skipping cookie file entry(?: due to)?(?P<value>[^\r\n]+)",
        r"(?im)(?<![\w-])(?:set-cookie|cookie|authorization|proxy-authorization|x-[\w-]*(?:token|auth)[\w-]*)[ \t]*:[ \t]*(?P<value>[^\r\n]+)",
        r"(?i)\b(?:Bearer|Basic)[ \t]+(?P<value>[^\s,;\"']+)",
        r"(?i)\b(?:SID|SIDCC|DSID|HSID|SSID|APISID|SAPISID|LOGIN_INFO|__Secure-[\w-]+|__Host-[\w-]+|YSC|VISITOR_INFO1_LIVE|VISITOR_PRIVACY_METADATA|SESSION_TOKEN|youtube_session_token)[ \t]*=[ \t]*(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;,\"']+)",
    ]
    while True:
        protected = [match.span() for match in re.finditer("<redacted>", text)]
        spans = protected.copy()
        for pattern in literals:
            for match in pattern.finditer(text):
                span = (match.start(), match.start() + len(match[1]))
                # A literal inside a token cannot corrupt it; crossing its edge
                # still participates in the union and removes outside text.
                if not any(a <= span[0] and span[1] <= b for a, b in protected):
                    spans.append(span)
        for rule in rules:
            spans.extend(match.span("value") for match in re.finditer(rule, text))
        for match in argument.finditer(text):
            value = match["value"]
            if (match["separator"].isspace() and not value.startswith(("'", '"'))
                    and (value.rstrip(".!?;:").lower() in {"or", "for", "and", "to", "the", "with", "is", "in"}
                         or value in (".", "!", "?", ";"))):
                continue  # R4 prose mentions flags without granting cookie access.
            spans.append(match.span("value"))
        offset = 0
        for line in text.splitlines(keepends=True):
            if line.count("\t") + line.count(r"\t") >= 6:
                spans.append((offset, offset + len(line.rstrip("\r\n"))))
            offset += len(line)
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        pieces, cursor = [], 0
        for start, end in merged:
            pieces.extend((text[cursor:start], "<redacted>"))
            cursor = end
        result = "".join([*pieces, text[cursor:]])
        if result == text:
            return result
        text = result  # Fixed point: every change removes non-token characters.


def classify_failure(output: str, auth_mode: str = "None") -> str:
    # Only terminal ERROR lines establish a category; warnings stay in Details.
    error = "\n".join(line for line in output.splitlines() if line.strip().startswith("ERROR:")).lower().replace("’", "'")
    if "not a bot" in error:
        return "BOT_VERIFICATION" if auth_mode == "None" else "AUTH_REJECTED"
    if "cookies are no longer valid" in error or "cookies have expired" in error:
        return "AUTH_REJECTED"
    if any(s in error for s in ("sign in", "login required", "authentication required", "login to")):
        return "AUTH_REQUIRED" if auth_mode == "None" else "AUTH_REJECTED"
    if "netscape format" in error or "invalid cookie file" in error:
        return "COOKIE_FILE_INVALID"
    if any(s in error for s in ("cookie database", "cookies database", "failed to load cookies", "decrypt", "keyring", "cookie file")):
        return "COOKIE_ACCESS_FAILED"
    if "yt-dlp" in error and any(s in error for s in ("not found", "no such file", "not installed", "required")):
        return "YT_DLP_MISSING"
    if "ffmpeg" in error or "postprocessing:" in error:
        return "FFMPEG_ERROR"
    if any(s in error for s in ("video unavailable", "this video is unavailable", "this video is not available",
                                "private video", "video has been removed", "has been removed by the uploader")):
        return "VIDEO_UNAVAILABLE"
    if any(s in error for s in ("unable to download", "timed out", "connection refused", "connection reset", "network is unreachable", "name resolution", "http error")):
        return "NETWORK_ERROR"
    return "GENERIC_DOWNLOAD_ERROR"


def failure_summary(category: str) -> str:
    if category in ("AUTH_REQUIRED", "BOT_VERIFICATION"):
        return "YouTube is asking for authentication.\n\nChoose Browser cookies or Cookie file\nand try again.\n\nAuthentication may help, but YouTube\ncan still reject some requests or networks."
    if category == "AUTH_REJECTED":
        return "YouTube still rejected this request\nwith the selected authentication.\n\nCheck that your browser session or\ncookie file is valid, then retry."
    return {
        "COOKIE_ACCESS_FAILED": "yt-dlp could not access the selected cookies.\nCheck the browser session or cookie file permissions.",
        "COOKIE_FILE_INVALID": "yt-dlp could not use this cookie file.\nChoose a valid Netscape format cookie file.",
        "VIDEO_UNAVAILABLE": "This video is unavailable to this request.",
        "NETWORK_ERROR": "The download encountered a network error.\nCheck your connection and try again.",
        "YT_DLP_MISSING": "yt-dlp is required to download videos. Install yt-dlp and try again.",
        "YT_DLP_UNVERIFIED": "The selected yt-dlp could not be verified. Choose another executable or switch to Automatic.",
        "FFMPEG_ERROR": "The video could not be processed.\nCheck that ffmpeg is available; see Details.",
    }.get(category, "The download failed. See Details for the yt-dlp diagnostic.")


def _popen_options(os_name: str) -> dict:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os_name == "nt" else {}


class DownloadTool(NamedTuple):
    command: list[str]
    source: str


def custom_yt_dlp_tool(value: str) -> DownloadTool:
    if not isinstance(value, str) or not value.strip():
        raise DownloadInputError("Choose a custom yt-dlp executable.")
    if (len(value) > 4096 or re.search(r"[\x00-\x1f\x7f-\x9f]", value)
            or re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", value.strip())):
        raise DownloadInputError("Choose a local yt-dlp executable with a valid path.")
    try:
        path = Path(value).expanduser().absolute()
        if not path.exists():
            raise DownloadInputError("The selected yt-dlp executable does not exist.")
        if not path.is_file():
            raise DownloadInputError("Choose a regular yt-dlp executable file.")
        if sys.executable and path.samefile(sys.executable):
            raise DownloadInputError("Choose yt-dlp rather than Cove or its Python interpreter.")
        if os.name == "nt":
            if path.suffix.lower() != ".exe":
                raise DownloadInputError("Choose a yt-dlp .exe file.")
        elif not os.access(path, os.X_OK):
            raise DownloadInputError("The selected yt-dlp file is not executable.")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, DownloadInputError):
            raise
        raise DownloadInputError("The selected yt-dlp executable could not be accessed.") from None
    return DownloadTool([str(path)], "custom")


def _yt_dlp_command() -> DownloadTool | None:
    exe_name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
    for directory in _yt_dlp_dirs():
        candidate = directory / exe_name
        if candidate.is_file():
            return DownloadTool([str(candidate)], "bundled")

    exe = shutil.which("yt-dlp")
    if exe:
        return DownloadTool([exe], "system")
    if not getattr(sys, "frozen", False) and sys.executable and importlib.util.find_spec("yt_dlp") is not None:
        return DownloadTool([sys.executable, "-m", "yt_dlp"], "python module")
    return None


def _yt_dlp_dirs() -> list[Path]:
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(Path(__file__).resolve().parent.parent.parent / "assets" / "bin")
    return dirs


class DownloadWorker(QObject):
    progress = Signal(int)
    status = Signal(str)
    finished = Signal(Path)
    failed = Signal(str)
    cancelled = Signal()
    details = Signal(str)
    tool_info = Signal(str)

    def __init__(
        self,
        url: str,
        quality: str,
        output_folder: Path,
        ffmpeg_path: str | None = None,
        *, auth_mode: str = "None", browser: str = "",
        browser_profile: str = "", cookie_file: str = "", extra_literals=(),
        yt_dlp_mode: str = "Automatic", yt_dlp_path: str = "",
    ) -> None:
        super().__init__()
        self._url = url
        self._quality = quality
        self._output_folder = output_folder
        self._ffmpeg_path = ffmpeg_path
        self._auth = dict(auth_mode=auth_mode, browser=browser,
                          browser_profile=browser_profile, cookie_file=cookie_file)
        self._cookie_file_input = cookie_file
        self._extra_literals = tuple(extra_literals)
        self._yt_dlp_mode, self._yt_dlp_path = yt_dlp_mode, yt_dlp_path
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._termination_requested = False
        self._cleanup_done = False
        self._process_lock = threading.Lock()
        self._cancel_timer: threading.Timer | None = None
        self._kill_requested = False

    def _safe(self, text: str) -> str:
        return sanitize(text, cookie_file=(self._auth["cookie_file"], self._cookie_file_input,
                        self._extra_literals[0] if self._extra_literals else ""),
                        browser_profile=self._auth["browser_profile"], extra_literals=self._extra_literals)

    def _version_details(self, tool: DownloadTool, env: dict) -> str | None:
        version = None
        reason = ""
        try:
            proc = self._start_process([*tool.command, "--ignore-config", "--version"],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      encoding="utf-8", errors="replace", env=env, **_popen_options(os.name))
            output, _ = proc.communicate(timeout=10)
            candidate = output.strip()
            if (proc.returncode == 0 and len(candidate) <= 64
                    and re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}\S*", candidate)):
                version = candidate
            else:
                reason = f"Version probe exited with status {proc.returncode}."
        except subprocess.TimeoutExpired:
            if self._proc is not None:
                self._kill_cancelled_process(self._proc)
            reason = "Version probe timed out."
        except Exception as exc:  # Automatic downloads remain non-gating.
            reason = self._safe(f"Version probe failed: {exc}")
        finally:
            self._finish_cancelled_process()
            with self._process_lock:
                self._proc = None
        info = self._safe(f"yt-dlp {version or 'unknown'} ({tool.source})")
        self.tool_info.emit(info)
        lines = [info, CONFIG_NOTE, f"Executable: {tool.command[0]}"]
        if reason:
            lines.append(reason)
        self.details.emit(self._safe("\n".join(lines)))
        return version

    def _start_process(self, command: list[str], **options) -> subprocess.Popen:
        proc = subprocess.Popen(command, **options)
        with self._process_lock:
            self._proc = proc
            self._termination_requested = self._cleanup_done = self._kill_requested = False
        if self._cancelled:
            self.cancel()  # Cancellation may have arrived before handle publication.
        return proc

    @Slot()
    def run(self) -> None:
        outcome, value = "failed", failure_summary("GENERIC_DOWNLOAD_ERROR")
        try:
            if self._cancelled:
                return
            if self._yt_dlp_mode not in YT_DLP_MODES:
                raise DownloadInputError("Choose a valid yt-dlp executable mode.")
            tool = (custom_yt_dlp_tool(self._yt_dlp_path)
                    if self._yt_dlp_mode == "Custom executable" else _yt_dlp_command())
            if tool is None:
                value = failure_summary("YT_DLP_MISSING")
                self.details.emit(f"yt-dlp unknown (not found)\n{CONFIG_NOTE}")
                return
            cmd = build_download_command(tool.command, url=self._url, quality=self._quality,
                                         output_folder=self._output_folder,
                                         ffmpeg_path=self._ffmpeg_path, **self._auth)
            # Include the canonical path in redaction, too; never read cookies.
            if self._auth["auth_mode"] == "Cookie file":
                self._auth["cookie_file"] = cmd[cmd.index("--cookies") + 1]
            self._output_folder.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            version = self._version_details(tool, env)  # Runs only on this worker thread.
            if self._cancelled:
                return
            if self._yt_dlp_mode == "Custom executable" and version is None:
                value = failure_summary("YT_DLP_UNVERIFIED")
                return
            self.status.emit("Starting download...")
            self._start_process(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1, env=env,
                **_popen_options(os.name),
            )
            if self._cancelled:
                return
            assert self._proc.stdout is not None
            out_path, last_pct = None, 0
            recent_output: deque[str] = deque(maxlen=16)
            error_output: deque[str] = deque(maxlen=8)
            for raw_line in self._proc.stdout:
                if self._cancelled:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                pct = self._progress_percent(line)
                if pct is not None:
                    last_pct = max(last_pct, pct)
                    self.progress.emit(last_pct)
                    self.status.emit("Downloading...")
                    continue
                maybe_path = self._path_from_line(line)
                if maybe_path is not None:
                    out_path = maybe_path
                if self._is_error_line(line):
                    error_output.append(line)  # Classify before display redaction can alter words.
                if self._is_warning_line(line) or self._is_error_line(line):
                    self.details.emit(self._safe(line))
                recent_output.append(self._safe(line))
                status = self._status_from_output(line)
                if status is not None:
                    self.status.emit(status)

            if self._cancelled:
                return
            rc = self._proc.wait(timeout=3)
            if rc != 0:
                self.details.emit(self._safe(self._failure_message(rc, error_output, recent_output)))
                value = failure_summary(classify_failure("\n".join(error_output), self._auth["auth_mode"]))
                return
            if out_path is None or self._path_from_line(str(out_path)) is None:
                value = "Download finished, but yt-dlp did not report a valid MP4 file in the output folder."
                return
            self.progress.emit(100)
            self.status.emit(self._safe(f"Downloaded {out_path.name}"))
            outcome, value = "finished", out_path
        except FileNotFoundError:
            value = failure_summary("YT_DLP_MISSING")
        except DownloadInputError as exc:
            value = str(exc)  # Input validation errors use fixed copy, without input echoes.
        except Exception as exc:  # noqa: BLE001
            self.details.emit(self._safe(f"Download exception: {exc}"))
        finally:
            self._finish_cancelled_process()
            self._proc = None
            if self._cancelled:
                self.cancelled.emit()
            elif outcome == "finished":
                self.finished.emit(value)
            else:
                self.failed.emit(value)  # This channel contains only Cove's fixed summaries.

    @Slot()
    def cancel(self) -> None:
        self._cancelled = True
        proc = self._proc
        if proc is None:
            return
        with self._process_lock:
            try:
                if not self._cleanup_done and not self._termination_requested and proc.poll() is None:
                    self._termination_requested = True
                    proc.terminate()
                    self._cancel_timer = threading.Timer(3, self._kill_cancelled_process, args=(proc,))
                    self._cancel_timer.daemon = True
                    self._cancel_timer.start()
            except OSError:
                pass  # The child may exit between poll and terminate.

    def _kill_cancelled_process(self, proc: subprocess.Popen) -> None:
        with self._process_lock:
            try:
                if proc is self._proc and not self._kill_requested and proc.poll() is None:
                    self._kill_requested = True
                    proc.kill()
            except OSError:
                pass  # The child may exit just before kill.

    def _finish_cancelled_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            with self._process_lock:
                if self._cleanup_done:
                    return
                self._cleanup_done = True
                if proc.poll() is None and not self._termination_requested:
                    self._termination_requested = True
                    try:
                        proc.terminate()
                    except OSError:
                        pass  # Still reap if the child exited between poll and terminate.
            # Only the worker waits; never hold the state lock across a wait.
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                self._kill_cancelled_process(proc)
                proc.wait(timeout=3)
            except Exception:
                self.details.emit("Child process cleanup did not complete within the timeout.")
        except Exception:  # Cleanup failures must still produce one terminal signal.
            self.details.emit("Child process cleanup could not be completed.")
        finally:
            with self._process_lock:
                if self._cancel_timer is not None:
                    self._cancel_timer.cancel()
                    self._cancel_timer = None

    @staticmethod
    def _progress_percent(line: str) -> int | None:
        match = _PROGRESS_RE.search(line)
        if not match:
            return None
        return max(0, min(100, int(float(match.group(1)))))

    @staticmethod
    def _is_warning_line(line: str) -> bool:
        return line.startswith("WARNING:")

    @staticmethod
    def _is_error_line(line: str) -> bool:
        return line.startswith("ERROR:")

    @staticmethod
    def _status_from_output(line: str) -> str | None:
        lowered = line.lower()
        if "[merger]" in lowered or "merging formats" in lowered:
            return "Merging..."
        if (
            "remux" in lowered
            or "post-process" in lowered
            or "postprocess" in lowered
            or "[movefiles]" in lowered
            or "deleting original file" in lowered
        ):
            return "Finalizing..."
        if line.startswith("[download]"):
            return "Downloading..."
        return None

    @staticmethod
    def _failure_message(
        return_code: int,
        error_output: deque[str],
        recent_output: deque[str],
    ) -> str:
        message = f"yt-dlp exited with status {return_code}."
        context = list(dict.fromkeys([*error_output, *recent_output]))
        if context:
            message = f"{message}\n\n" + "\n".join(context)
        return message

    def _path_from_line(self, line: str) -> Path | None:
        candidate = Path(line.strip('"'))
        quoted = _QUOTED_MP4_RE.search(line)
        if candidate.suffix.lower() != ".mp4" and quoted:
            candidate = Path(quoted.group(1))
        try:
            candidate = candidate.resolve()
            if (candidate.suffix.lower() == ".mp4" and candidate.is_file()
                    and candidate.is_relative_to(self._output_folder.resolve())):
                return candidate
        except (OSError, ValueError, RuntimeError):
            pass
        return None


class DownloadVideoDialog(QDialog):
    downloadFinished = Signal(Path)

    def __init__(self, parent=None, ffmpeg_path: str | None = None, *, settings=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Download Video")
        self.setModal(False)
        self.resize(580, 390)
        self._ffmpeg_path = ffmpeg_path
        self._settings = settings
        self._thread: QThread | None = None
        self._worker: DownloadWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://...")
        form.addRow("URL", self.url_edit)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_FORMATS.keys()))
        form.addRow("Quality", self.quality_combo)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(str(self._default_output_folder()))
        self.folder_edit.setReadOnly(True)
        self.folder_btn = QPushButton("Choose...")
        self.folder_btn.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_edit, stretch=1)
        folder_row.addWidget(self.folder_btn)
        form.addRow("Output folder", folder_row)
        self.yt_dlp_combo = QComboBox()
        self.yt_dlp_combo.addItems(YT_DLP_MODES)
        self.yt_dlp_combo.setAccessibleName("yt-dlp executable mode")
        form.addRow("yt-dlp", self.yt_dlp_combo)
        self.yt_dlp_edit = QLineEdit()
        self.yt_dlp_edit.setPlaceholderText("Path to your yt-dlp executable")
        self.yt_dlp_edit.setAccessibleName("Custom yt-dlp executable path")
        self.yt_dlp_btn = QPushButton("Choose...")
        self.yt_dlp_btn.setAccessibleName("Choose yt-dlp executable")
        self.yt_dlp_btn.clicked.connect(self._choose_yt_dlp)
        self.yt_dlp_label = QLabel("Executable")
        self.yt_dlp_label.setBuddy(self.yt_dlp_edit)
        tool_row = QHBoxLayout()
        tool_row.addWidget(self.yt_dlp_edit, stretch=1)
        tool_row.addWidget(self.yt_dlp_btn)
        form.addRow(self.yt_dlp_label, tool_row)
        self.yt_dlp_hint = QLabel()
        self.yt_dlp_hint.setWordWrap(True)
        form.addRow(self.yt_dlp_hint)
        self.yt_dlp_info = QLabel(YT_DLP_INFO_NEUTRAL)
        self.yt_dlp_info.setWordWrap(True)
        form.addRow(self.yt_dlp_info)
        self._restore_yt_dlp()
        self.yt_dlp_combo.currentIndexChanged.connect(self._yt_dlp_changed)
        self.yt_dlp_edit.textChanged.connect(self._reset_yt_dlp_info)
        self._yt_dlp_changed()
        self.auth_combo = QComboBox()
        self.auth_combo.addItems(AUTH_MODES)
        self.auth_combo.setAccessibleName("Sign-in mode")
        form.addRow("Sign-in", self.auth_combo)
        self.browser_combo = QComboBox()
        self.browser_combo.addItem("Choose a browser", "")
        for browser in SUPPORTED_BROWSERS:
            self.browser_combo.addItem(browser, browser)
        self.profile_edit = QLineEdit()
        self.profile_edit.setPlaceholderText("Profile (optional)")
        self.browser_label = QLabel("Browser")
        self.browser_label.setBuddy(self.browser_combo)
        self.profile_label = QLabel("Profile")
        self.profile_label.setBuddy(self.profile_edit)
        form.addRow(self.browser_label, self.browser_combo)
        form.addRow(self.profile_label, self.profile_edit)
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setReadOnly(True)
        self.cookie_btn = QPushButton("Choose...")
        self.cookie_btn.clicked.connect(self._choose_cookie_file)
        self.cookie_label = QLabel("Cookie file")
        self.cookie_label.setBuddy(self.cookie_edit)
        self.cookie_row = QHBoxLayout()
        self.cookie_row.addWidget(self.cookie_edit, stretch=1)
        self.cookie_row.addWidget(self.cookie_btn)
        form.addRow(self.cookie_label, self.cookie_row)
        for widget, name in ((self.browser_combo, "Browser"), (self.profile_edit, "Browser profile"),
                             (self.cookie_edit, "Cookie file"), (self.cookie_btn, "Choose cookie file")):
            widget.setAccessibleName(name)
        self.auth_hint = QLabel()
        self.auth_hint.setWordWrap(True)
        form.addRow(self.auth_hint)
        self._restore_auth()
        self.auth_combo.currentIndexChanged.connect(self._auth_changed)
        self._auth_changed()
        root.addLayout(form)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.details_btn = QPushButton("Details")
        self.details_btn.setCheckable(True)
        self.details_btn.setAccessibleName("Show download details")
        self.details_edit = QPlainTextEdit()
        self.details_edit.setReadOnly(True)
        self.details_edit.setAccessibleName("Download details")
        self.details_edit.setPlainText(CONFIG_NOTE)
        self.details_edit.hide()
        self.details_btn.toggled.connect(self.details_edit.setVisible)
        root.addWidget(self.details_btn)
        root.addWidget(self.details_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_download)
        self.download_btn = QPushButton("Download")
        self.download_btn.setObjectName("PrimaryButton")
        self.download_btn.clicked.connect(self._start_download)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.download_btn)
        root.addLayout(btn_row)

    def _restore_yt_dlp(self) -> None:
        if self._settings is None:
            return
        try:
            mode = self._settings.value("downloader/yt_dlp_mode", "Automatic")
            path = self._settings.value("downloader/yt_dlp_path", "")
            if (not isinstance(mode, str) or not isinstance(path, str) or mode not in YT_DLP_MODES
                    or len(path) > 4096 or re.search(r"[\x00-\x1f\x7f-\x9f]", path)):
                return
            self.yt_dlp_combo.setCurrentText(mode)
            self.yt_dlp_edit.setText(path)
        except Exception:
            pass  # Malformed preferences leave Automatic/empty defaults.

    def _yt_dlp_changed(self) -> None:
        self._reset_yt_dlp_info()
        custom = self.yt_dlp_combo.currentText() == "Custom executable"
        enabled = not hasattr(self, "download_btn") or self.download_btn.isEnabled()
        for widget in (self.yt_dlp_label, self.yt_dlp_edit, self.yt_dlp_btn):
            widget.setVisible(custom)
            widget.setEnabled(custom and enabled)
        self.yt_dlp_hint.setText(
            "Cove will run this program with your permissions. Only choose a yt-dlp you trust." if custom else
            "Uses the yt-dlp bundled with Cove, or one found on your system.")

    def _reset_yt_dlp_info(self) -> None:
        if not hasattr(self, "download_btn") or self.download_btn.isEnabled():
            self.yt_dlp_info.setText(YT_DLP_INFO_NEUTRAL)

    def _choose_yt_dlp(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose yt-dlp executable", self.yt_dlp_edit.text())
        if path:
            self.yt_dlp_edit.setText(path)

    def _show_tool_info(self, text: str) -> None:
        self.yt_dlp_info.setText(self._safe(text.replace("yt-dlp ", "yt-dlp: ", 1)))

    def _auth_values(self) -> dict:
        return dict(auth_mode=self.auth_combo.currentText(), browser=self.browser_combo.currentData(),
                    browser_profile=self.profile_edit.text(), cookie_file=self.cookie_edit.text())

    def _restore_auth(self) -> None:
        if self._settings is None:
            return
        try:
            values = [self._settings.value("downloader/" + key, "None" if key == "auth_mode" else "")
                      for key in ("auth_mode", "browser", "browser_profile", "cookie_file")]
            mode, browser, profile, cookie = values
            if (not all(isinstance(v, str) for v in values) or mode not in AUTH_MODES
                    or browser not in ("", *SUPPORTED_BROWSERS) or len(cookie) > 4096
                    or any(ord(c) < 32 for c in cookie)):
                return
            browser_specification(browser or "firefox", profile)  # Syntax only; no cookie access.
            self.auth_combo.setCurrentText(mode)
            self.browser_combo.setCurrentIndex(max(0, self.browser_combo.findData(browser)))
            self.profile_edit.setText(profile.strip())
            self.cookie_edit.setText(cookie)
        except Exception:
            pass  # Malformed preferences always leave None/empty defaults.

    def _auth_changed(self) -> None:
        mode = self.auth_combo.currentText()
        enabled = not hasattr(self, "download_btn") or self.download_btn.isEnabled()
        for widget in (self.browser_label, self.browser_combo, self.profile_label, self.profile_edit):
            widget.setVisible(mode == "Browser cookies")
            widget.setEnabled(mode == "Browser cookies" and enabled)
        for widget in (self.cookie_label, self.cookie_edit, self.cookie_btn):
            widget.setVisible(mode == "Cookie file")
            widget.setEnabled(mode == "Cookie file" and enabled)
        self.auth_hint.setText({"None": "No sign-in. Public videos only.",
                               "Browser cookies": "yt-dlp will use your signed-in session from this browser for this download.",
                               "Cookie file": "yt-dlp will use the cookies in this file and may update it."}[mode])

    def _choose_cookie_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose Cookie File", self.cookie_edit.text())
        if path:
            self.cookie_edit.setText(path)

    def _safe(self, text: str) -> str:
        return sanitize(text, cookie_file=(self.cookie_edit.text(), getattr(self, "_redaction_literals", ("",))[0]), browser_profile=self.profile_edit.text(),
                        extra_literals=getattr(self, "_redaction_literals", ()))

    def _append_details(self, text: str) -> None:
        self.details_edit.appendPlainText(self._safe(text))

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self.cancel_and_wait()
        super().closeEvent(event)

    def cancel_and_wait(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()

    def _default_output_folder(self) -> Path:
        location = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
        if location:
            return Path(location)
        return Path.home() / "Downloads"

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose Output Folder",
            self.folder_edit.text(),
        )
        if folder:
            self.folder_edit.setText(folder)

    def _start_download(self) -> None:
        url = self.url_edit.text().strip()
        output_folder = Path(self.folder_edit.text()).expanduser()
        auth = self._auth_values()
        tool_settings = dict(yt_dlp_mode=self.yt_dlp_combo.currentText(), yt_dlp_path=self.yt_dlp_edit.text())
        self._redaction_literals = (auth["cookie_file"], auth["browser_profile"])
        try:
            if tool_settings["yt_dlp_mode"] == "Custom executable":
                custom_yt_dlp_tool(tool_settings["yt_dlp_path"])
            cmd = build_download_command(["yt-dlp"], url=url, quality=self.quality_combo.currentText(),
                                         output_folder=output_folder, **auth)
            if auth["auth_mode"] == "Browser cookies":
                auth["browser_profile"] = auth["browser_profile"].strip()
                self.profile_edit.setText(auth["browser_profile"])
            if auth["auth_mode"] == "Cookie file":
                auth["cookie_file"] = cmd[cmd.index("--cookies") + 1]
                self.cookie_edit.setText(auth["cookie_file"])
        except DownloadInputError as exc:
            QMessageBox.warning(self, "Download input", str(exc))
            return
        if self._settings is not None:
            for key, value in {**auth, **tool_settings}.items():
                self._settings.setValue("downloader/" + key, value)
        self.details_edit.clear()  # The worker supplies the config note with its diagnostics.
        self.yt_dlp_info.setText("yt-dlp: checking version...")
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting download...")
        self.download_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.url_edit.setEnabled(False)
        self.quality_combo.setEnabled(False)
        self.folder_btn.setEnabled(False)
        self.auth_combo.setEnabled(False)
        self._auth_changed()
        self.yt_dlp_combo.setEnabled(False)
        self._yt_dlp_changed()

        thread = QThread(self)
        worker = DownloadWorker(
            url,
            self.quality_combo.currentText(),
            output_folder,
            self._ffmpeg_path,
            **auth, **tool_settings, extra_literals=self._redaction_literals,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.progress_bar.setValue, Qt.QueuedConnection)
        worker.status.connect(self.status_label.setText, Qt.QueuedConnection)
        worker.details.connect(self._append_details, Qt.QueuedConnection)
        worker.tool_info.connect(self._show_tool_info, Qt.QueuedConnection)
        worker.finished.connect(self._download_finished, Qt.QueuedConnection)
        worker.failed.connect(self._download_failed, Qt.QueuedConnection)
        worker.cancelled.connect(self._download_cancelled, Qt.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.QueuedConnection)
        worker.failed.connect(thread.quit, Qt.QueuedConnection)
        worker.cancelled.connect(thread.quit, Qt.QueuedConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _cancel_download(self) -> None:
        if self._worker is not None:
            self.status_label.setText("Cancelling...")
            self._worker.cancel()

    def _download_finished(self, path: Path) -> None:
        self.downloadFinished.emit(path)
        self.accept()

    def _download_failed(self, message: str) -> None:
        # The worker failed signal carries only fixed, input-free Cove copy.
        self.status_label.setText(message)
        QMessageBox.warning(self, "Download failed", message)
        if self._thread is None:
            self._reset_controls()

    def _download_cancelled(self) -> None:
        self.status_label.setText("Cancelled")
        if self._thread is None:
            self._reset_controls()

    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._reset_controls()

    def _reset_controls(self) -> None:
        self.yt_dlp_info.setText(YT_DLP_INFO_NEUTRAL)
        self.download_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.url_edit.setEnabled(True)
        self.quality_combo.setEnabled(True)
        self.folder_btn.setEnabled(True)
        self.auth_combo.setEnabled(True)
        self._auth_changed()
        self.yt_dlp_combo.setEnabled(True)
        self._yt_dlp_changed()
