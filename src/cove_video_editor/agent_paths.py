"""Linux descriptor boundaries. Permissions come exclusively from startup."""
from __future__ import annotations
import errno, hashlib, json, math, os, re, selectors, signal, stat, subprocess, sys, time, uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from .ffmpeg_utils import FFmpegMissingError, require_ffprobe
from .project_io import ProjectError, write_project
MAX_PATHS, MAX_PATH_CHARS, MAX_NAME, MAX_PROJECTS = 50, 4096, 80, 20
MAX_ASSETS, MAX_CLIPS, MAX_TIMELINE, MAX_WARNINGS = 200, 200, 86400, 50
PROBE_TIMEOUT, PROBE_BUDGET, PROBE_OUTPUT_CAP = 10, 60, 65536
EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"})
SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt"})
MAX_SUBTITLE_TRACKS, MAX_SUBTITLE_BYTES, MAX_SUBTITLE_CUES, MAX_SUBTITLE_CUE_CHARS = 8, 1048576, 5000, 1000
MAX_AUDIO_ITEMS, MAX_AUDIO_LANE = 50, 63
MAX_PROJECT_BYTES = 4194304
INPUT_OPTIONS = ("-protocol_whitelist", "file", "-format_whitelist",
                 "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi")
AUDIO_INPUT_OPTIONS = (*INPUT_OPTIONS[:-1], INPUT_OPTIONS[-1] + ",mp3,wav,flac,ogg,aac")
ERROR_CODES = frozenset("INVALID_ARGUMENT NOT_CONFIGURED PATH_NOT_AUTHORIZED FILE_NOT_FOUND "
    "NOT_A_REGULAR_FILE UNSUPPORTED_EXTENSION UNSUPPORTED_MEDIA PROBE_TIMEOUT PROBE_FAILED "
    "PROBE_OUTPUT_TOO_LARGE PROBE_BUDGET_EXCEEDED LIMIT_EXCEEDED UNKNOWN_PROJECT UNKNOWN_ASSET "
    "ASSET_NOT_VIDEO INVALID_TIMELINE EMPTY_TIMELINE ASSET_CHANGED DESTINATION_EXISTS "
    "OUTPUT_FILESYSTEM_UNSUPPORTED OUTPUT_UNSAFE PROJECT_CHANGED_ON_DISK INTERNAL_ERROR "
    "EXPORT_BUSY PROJECT_NOT_SAVED UNKNOWN_EXPORT EXPORT_FAILED EXPORT_TIMEOUT "
    "EXPORT_OUTPUT_INVALID SHUTTING_DOWN INVALID_SUBTITLE UNKNOWN_CLIP REVISION_MISMATCH INVALID_CROP "
    "INVALID_PROJECT UNSUPPORTED_PROJECT_FEATURE PROJECT_REFERENCE_INVALID".split())
LIMITS = dict(paths=MAX_PATHS, path_chars=MAX_PATH_CHARS, name_chars=MAX_NAME,
    projects=MAX_PROJECTS, assets=MAX_ASSETS, clips=MAX_CLIPS, timeline_s=MAX_TIMELINE,
    probe_timeout_s=PROBE_TIMEOUT, probe_budget_s=PROBE_BUDGET,
    probe_output_bytes=PROBE_OUTPUT_CAP, warnings=MAX_WARNINGS,
    audio_items=MAX_AUDIO_ITEMS, audio_lane_max=MAX_AUDIO_LANE, subtitle_tracks=MAX_SUBTITLE_TRACKS,
    subtitle_bytes=MAX_SUBTITLE_BYTES, subtitle_cues=MAX_SUBTITLE_CUES, subtitle_cue_chars=MAX_SUBTITLE_CUE_CHARS,
    project_bytes=MAX_PROJECT_BYTES)
class AgentError(Exception):
    def __init__(self, code: str, message: str):
        if code == "PATH_NOT_AUTHORIZED" and "See cove_get_capabilities" not in message:
            message += " See cove_get_capabilities media_roots."
        self.code, self.message = code, message[:240]
        super().__init__(f"{code}: {self.message}")
@dataclass
class PathPolicy:
    media_roots: tuple[str, ...]
    output_root: str | None
    output_fd: int | None
    project_roots: tuple[str, ...] = ()
    def close(self) -> None:
        if self.output_fd is not None:
            os.close(self.output_fd)
            self.output_fd = None
def reject_unsafe_path_text(path: str) -> str:
    if (type(path) is not str or not path or len(path) > MAX_PATH_CHARS
            or "\0" in path or ":" in path or "\\" in path
            or path.startswith("//") or not os.path.isabs(path)):
        raise AgentError("INVALID_ARGUMENT", "Use a bounded absolute local path.")
    return path
def validate_roots(media_roots=(), output_root=None, project_roots=()) -> PathPolicy:
    roots = []
    entries = ([("--media-root", r) for r in media_roots] + [("--project-root", r) for r in project_roots]
               + ([("--output-root", output_root)] if output_root is not None else []))
    for flag, raw in entries:
        try:
            canonical = os.path.realpath(reject_unsafe_path_text(raw))
            reason = "is the filesystem root" if canonical == "/" else ("does not exist" if not os.path.exists(canonical) else ("not a directory" if not os.path.isdir(canonical) else None))
        except (ValueError, OSError, AgentError):
            reason = 'contains ":" or "\\", which is not supported' if type(raw) is str and (":" in raw or "\\" in raw) else "not an absolute path"
        if reason:
            raise AgentError("OUTPUT_UNSAFE" if flag == "--output-root" else "INVALID_ARGUMENT", f"{flag} {repr(raw)[:200]}: {reason}") from None
        roots.append(canonical)
    if sys.platform != "linux":
        raise AgentError("OUTPUT_UNSAFE", "UNSUPPORTED_PLATFORM: Linux is required.")
    output = roots.pop() if output_root is not None else None
    try:
        fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC) if output else None
    except OSError:
        raise AgentError("OUTPUT_UNSAFE", f"--output-root {repr(output_root)[:180]}: cannot be opened (check permissions)") from None
    return PathPolicy(tuple(dict.fromkeys(roots[:len(media_roots)])), output, fd,
                      tuple(dict.fromkeys(roots[len(media_roots):])))
def _contained(path: str, roots: tuple[str, ...]) -> bool:
    return any(os.path.commonpath([path, root]) == root for root in roots)
def file_identity(st) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns
def _media_hook(stage: str) -> None:
    """Deterministic race seam; production has no hook behavior."""
def authorize_media_path(policy: PathPolicy, path: str, kind: str = "video") -> str:
    """Authorize text/containment/extension without opening or probing the file."""
    if kind not in {"video", "audio", "subtitle"}:
        raise AgentError("INVALID_ARGUMENT", "Use video, audio or subtitle media.")
    reject_unsafe_path_text(path)
    if not policy.media_roots:
        raise AgentError("NOT_CONFIGURED", "Configure a media root at startup.")
    if sys.platform != "linux":
        raise AgentError("PATH_NOT_AUTHORIZED", "UNSUPPORTED_PLATFORM")
    canonical = os.path.realpath(path)
    if not _contained(canonical, policy.media_roots):
        raise AgentError("PATH_NOT_AUTHORIZED", "Path is outside authorized media roots. See cove_get_capabilities media_roots.")
    extensions = {"video": EXTENSIONS, "audio": AUDIO_EXTENSIONS, "subtitle": SUBTITLE_EXTENSIONS}[kind]
    suffix = Path(canonical).suffix.lower()
    if suffix not in extensions:
        message = f"Only supported {kind} extensions are accepted."
        if kind == "subtitle":
            message = "Use a .srt or .vtt file."
            if suffix in {".ass", ".ssa"}:
                message += " Cove renders only parsed SRT/VTT cues; .ass/.ssa are unsupported."
        raise AgentError("UNSUPPORTED_EXTENSION", message)
    return canonical
@contextmanager
def _open_authorized(canonical, roots, label, hint):
    _media_hook("before_open")
    try:
        # Open once; O_NOFOLLOW refuses a symlink swapped in after authorization.
        fd = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise AgentError("FILE_NOT_FOUND", "Authorized file does not exist.") from None
    except OSError:
        raise AgentError("PATH_NOT_AUTHORIZED", f"Cannot open {label} safely. See cove_get_capabilities {hint}.") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AgentError("NOT_A_REGULAR_FILE", f"{label} must be a regular file.")
        _media_hook("after_open")
        location = os.readlink(f"/proc/self/fd/{fd}")
        # /proc checks the inode's real location after parent-directory swaps.
        if not _contained(location.removesuffix(" (deleted)"), roots):
            raise AgentError("PATH_NOT_AUTHORIZED", f"Opened file left authorized roots. See cove_get_capabilities {hint}.")
        yield fd, canonical, file_identity(st)
    finally:
        os.close(fd)
@contextmanager
def open_authorized_media(policy: PathPolicy, path: str, *, kind: str = "video"):
    canonical = authorize_media_path(policy, path, kind)
    with _open_authorized(canonical, policy.media_roots, "Media", "media_roots") as opened:
        yield opened
@contextmanager
def open_authorized_project(policy: PathPolicy, path: str):
    reject_unsafe_path_text(path)
    if os.path.normpath(path) != path:
        raise AgentError("INVALID_ARGUMENT", "Project path must be normalized and absolute.")
    if not policy.project_roots:
        raise AgentError("NOT_CONFIGURED", "Configure --project-root at startup to read .coveproj files.")
    canonical = os.path.realpath(path)
    if not _contained(canonical, policy.project_roots):
        raise AgentError("PATH_NOT_AUTHORIZED", "Project path is outside authorized roots. See cove_get_capabilities project_roots.")
    if Path(canonical).suffix.lower() != ".coveproj":
        raise AgentError("UNSUPPORTED_EXTENSION", "Project path must end in .coveproj.")
    with _open_authorized(canonical, policy.project_roots, "Project", "project_roots") as opened:
        yield opened
def read_project_snapshot(fd: int, identity: tuple) -> tuple[bytes, str, int]:
    """Bounded snapshot of the held project descriptor, with identity rechecks."""
    if file_identity(os.fstat(fd)) != identity:
        raise AgentError("ASSET_CHANGED", "Project changed before reading.")
    if identity[2] > MAX_PROJECT_BYTES:
        raise AgentError("LIMIT_EXCEEDED", "Project exceeds 4 MiB.")
    data = bytearray()
    while len(data) <= MAX_PROJECT_BYTES:
        chunk = os.read(fd, min(65536, MAX_PROJECT_BYTES + 1 - len(data)))
        if not chunk: break
        data.extend(chunk)
    if file_identity(os.fstat(fd)) != identity:
        raise AgentError("ASSET_CHANGED", "Project changed while reading.")
    if len(data) > MAX_PROJECT_BYTES:
        raise AgentError("LIMIT_EXCEEDED", "Project exceeds 4 MiB.")
    return bytes(data), hashlib.sha256(data).hexdigest(), len(data)
def read_subtitle_snapshot(fd: int, identity: tuple) -> tuple[str, str, int]:
    """Read bounded bytes from the authorized held fd, never reopen its path."""
    if file_identity(os.fstat(fd)) != identity:
        raise AgentError("ASSET_CHANGED", "Subtitle changed before reading.")
    chunks, size = [], 0
    while size <= MAX_SUBTITLE_BYTES:
        chunk = os.read(fd, min(65536, MAX_SUBTITLE_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk); size += len(chunk)
    if file_identity(os.fstat(fd)) != identity:
        raise AgentError("ASSET_CHANGED", "Subtitle changed while reading.")
    if size > MAX_SUBTITLE_BYTES:
        raise AgentError("LIMIT_EXCEEDED", "Subtitle exceeds 1 MiB.")
    data = b"".join(chunks)
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise AgentError("INVALID_SUBTITLE", "Subtitle must be strict UTF-8 (BOM allowed).") from None
    if any((ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127 for c in text):
        raise AgentError("INVALID_SUBTITLE", "Subtitle contains forbidden control characters.")
    validate_subtitle_parse_cost(text)
    return text, hashlib.sha256(data).hexdigest(), size
def validate_subtitle_parse_cost(text: str) -> None:
    """Linear scan bounds the shared parser's potentially quadratic regex work.

    Timing searches see <=256 chars; blocks see <=8192 chars and <=16
    unmatched openers after their last closer. With the 1 MiB byte bound,
    both failed regex searches have a small fixed work factor per byte.
    No content is altered; normalization mirrors parse_sub_cues exactly.
    """
    if text.count("-->") > MAX_SUBTITLE_CUES:
        raise AgentError("LIMIT_EXCEEDED", "Too many timing markers; use at most 5000 cues.")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if any("-->" in line and len(line) > 256 for line in normalized.split("\n")):
        raise AgentError("INVALID_SUBTITLE", "Timing line exceeds 256 characters; shorten it.")
    for block in re.split(r"\n\n+", normalized.strip()):
        if len(block) > 8192:
            raise AgentError("INVALID_SUBTITLE", "Subtitle block exceeds 8192 characters; split or shorten it.")
        if block.count("<", block.rfind(">") + 1) + block.count("{", block.rfind("}") + 1) > 16:
            raise AgentError("INVALID_SUBTITLE", "Too many unterminated markup characters; balance markup or use at most 16.")
def probe_fd(fd: int, *, timeout: float = PROBE_TIMEOUT, kind: str = "video") -> dict:
    if kind not in {"video", "audio"}:
        raise AgentError("INVALID_ARGUMENT", "Use video or audio media.")
    try:
        binary = require_ffprobe()
    except FFmpegMissingError:
        raise AgentError("PROBE_FAILED", "ffprobe is unavailable. Install FFmpeg (ffprobe).") from None
    # Probe the held fd with whitelists: rejecting metadata afterward is not access control.
    entries = "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration,format_name"
    options = INPUT_OPTIONS
    decoding = []
    if kind == "audio":
        options = AUDIO_INPUT_OPTIONS
        # Decode a bounded initial window, without returning frames or raw metadata.
        decoding = ["-count_frames", "-read_intervals", "%+1"]
        entries += ":stream=nb_read_frames:stream_disposition=attached_pic"
    cmd = [binary, "-v", "error", *options, *decoding,
        "-show_entries", entries,
        "-of", "json", "-i", f"/dev/fd/{fd}"]
    try:
        proc = subprocess.Popen(cmd, pass_fds=(fd,), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={"PATH": os.defpath},
            cwd="/", start_new_session=True)
    except OSError:
        raise AgentError("PROBE_FAILED", "Cannot launch ffprobe.") from None
    data = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentError("PROBE_TIMEOUT", "Probe exceeded its time limit.")
                if not selector.select(remaining):
                    raise AgentError("PROBE_TIMEOUT", "Probe exceeded its time limit.")
                chunk = os.read(proc.stdout.fileno(), 8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > PROBE_OUTPUT_CAP:
                    raise AgentError("PROBE_OUTPUT_TOO_LARGE", "Probe output exceeded its limit.")
        try:
            code = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AgentError("PROBE_TIMEOUT", "Probe exceeded its time limit.") from None
        if code != 0:
            raise AgentError("PROBE_FAILED", "Media could not be probed safely.")
    finally:
        # Kill the private group, including descendants, then reap the child.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        proc.stdout.close()
    try:
        doc = json.loads(data)
        streams = doc.get("streams", [])
        if kind == "audio":
            a = next((s for s in streams if s.get("codec_type") == "audio"), None)
            if any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1 for s in streams):
                raise AgentError("UNSUPPORTED_MEDIA", "Choose audio without a real video stream; cover art is allowed.")
            if a is None or int(a.get("nb_read_frames", "0")) <= 0:
                raise AgentError("UNSUPPORTED_MEDIA", "A decodable audio stream is required.")
            fmt = doc["format"]
            duration = float(fmt["duration"])
            if not math.isfinite(duration) or not 0 < duration <= MAX_TIMELINE:
                raise ValueError()
            values = [fmt["format_name"], *(s["codec_name"] for s in streams)]
            if any(type(s) is not str or not re.fullmatch(r"[A-Za-z0-9_,.-]{1,100}", s) for s in values):
                raise ValueError()
            return dict(kind="audio", duration_s=duration, audio_codec=a["codec_name"], container=values[0])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        if v is None:
            raise AgentError("UNSUPPORTED_MEDIA", "A video stream is required.")
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = doc["format"]
        duration = float(fmt["duration"])
        width, height = v["width"], v["height"]
        num, den = map(float, v["r_frame_rate"].split("/"))
        fps = num / den if den else 0.0
        if not (math.isfinite(duration) and duration > 0 and math.isfinite(fps) and 0 <= fps <= 1000
                and type(width) is int and type(height) is int and 1 <= width <= 65535 and 1 <= height <= 65535):
            raise ValueError()
        values = [v["codec_name"], fmt["format_name"], *([a["codec_name"]] if a else [])]
        if any(type(s) is not str or not re.fullmatch(r"[A-Za-z0-9_,.-]{1,100}", s) for s in values):
            raise ValueError()
        return dict(kind="video", duration_s=duration, width=width, height=height, fps=fps,
            has_audio=a is not None, video_codec=values[0], audio_codec=values[2] if a else None,
            container=values[1])
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise AgentError("PROBE_FAILED", f"Invalid {kind} metadata.") from None
def create_project_dir(policy: PathPolicy, name: str) -> int:
    if policy.output_fd is None:
        raise AgentError("NOT_CONFIGURED", "Configure an output root at startup.")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,150}", name):
        raise AgentError("OUTPUT_UNSAFE", "Invalid project directory name.")
    created = False
    try:
        # Exclusive mkdir anchored to the startup descriptor never follows existing paths.
        os.mkdir(name, mode=0o777, dir_fd=policy.output_fd)
        created = True
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                       dir_fd=policy.output_fd)
    except OSError:
        if created:
            try: os.rmdir(name, dir_fd=policy.output_fd)
            except OSError: pass  # Never recursively remove a changed/nonempty name.
        raise AgentError("OUTPUT_UNSAFE", "Cannot create an exclusive project directory.") from None
def _publish_hook() -> None:
    """Seam between complete temporary write and no-clobber publication."""
def link_output(fd: int, dir_fd: int, final: str) -> None:
    """Publish the held inode without replacing any existing name."""
    try:
        os.link(f"/proc/self/fd/{fd}", final, dst_dir_fd=dir_fd, follow_symlinks=True)
        os.fsync(dir_fd)
    except OSError as exc:
        code = "DESTINATION_EXISTS" if exc.errno == errno.EEXIST else (
            "OUTPUT_FILESYSTEM_UNSUPPORTED" if exc.errno in {
                errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS, errno.EINVAL, errno.ENOENT}
            else "OUTPUT_UNSAFE")
        raise AgentError(code, "Output root must support hard links; restart with another --output-root (unsaved edits are lost)." if code == "OUTPUT_FILESYSTEM_UNSUPPORTED" else ("Destination already exists; it will not be overwritten." if code == "DESTINATION_EXISTS" else "Output could not be published safely.")) from None
def publish(dir_fd: int, final: str, doc: dict) -> tuple[str, int]:
    temp = ".tmp-" + uuid.uuid4().hex
    owned, fd, private_fd, owned_dir = None, None, None, None
    try:
        # QSaveFile replaces its target; an exclusive private directory owns that namespace.
        try:
            os.mkdir(temp, 0o700, dir_fd=dir_fd)
            private_fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
            owned_dir = file_identity(os.fstat(private_fd))[:2]
        except OSError:
            raise AgentError("OUTPUT_UNSAFE", "Cannot create an exclusive temporary directory.") from None
        # The existing writer owns serialization; hard-link publication forbids overwrite.
        try:
            write_project(Path(f"/proc/self/fd/{private_fd}/project"), doc)
        except (ProjectError, OSError):
            raise AgentError("OUTPUT_UNSAFE", "Could not write the temporary project safely.") from None
        fd = os.open("project", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=private_fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            raise AgentError("OUTPUT_UNSAFE", "Temporary project inode is unsafe.")
        owned = file_identity(st)[:2]
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise AgentError("OUTPUT_UNSAFE", "Temporary project exceeds 16 MiB.")
        _publish_hook()
        # Publish our held inode even if another writer replaces the temp name.
        link_output(fd, dir_fd, final)
        return hashlib.sha256(data).hexdigest(), len(data)
    except OSError as exc:
        code = "DESTINATION_EXISTS" if exc.errno == errno.EEXIST else (
            "OUTPUT_FILESYSTEM_UNSUPPORTED" if exc.errno in {
                errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS, errno.EINVAL, errno.ENOENT}
            else "OUTPUT_UNSAFE")
        raise AgentError(code, "Output root must support hard links; restart with another --output-root (unsaved edits are lost)." if code == "OUTPUT_FILESYSTEM_UNSUPPORTED" else ("Destination already exists; it will not be overwritten." if code == "DESTINATION_EXISTS" else "Revision could not be published safely.")) from None
    finally:
        if fd is not None:
            os.close(fd)
        if owned is not None:
            try:
                # A name may have been replaced; delete only our positively owned inode.
                if file_identity(os.stat("project", dir_fd=private_fd, follow_symlinks=False))[:2] == owned:
                    os.unlink("project", dir_fd=private_fd)
            except OSError:
                pass
        if private_fd is not None:
            os.close(private_fd)
        if owned_dir is not None:
            try:
                if file_identity(os.stat(temp, dir_fd=dir_fd, follow_symlinks=False))[:2] == owned_dir:
                    os.rmdir(temp, dir_fd=dir_fd)
                    os.fsync(dir_fd)
            except OSError:  # Nonempty or replaced private directories must survive.
                pass
