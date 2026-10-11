"""One descriptor-bound export owner per session; no GUI or async signal slots."""
from __future__ import annotations
import copy, os, re, stat, threading, time, uuid
from collections import OrderedDict
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from PySide6.QtCore import Qt
from . import agent_paths as ap, exporter as ex

PREP_TIMEOUT_S, RENDER_DEADLINE_S = 30, 3600
CANCEL_GRACE_S, SHUTDOWN_DEADLINE_S, HISTORY_LIMIT = 5, 15, 20
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
RESOLUTIONS = {"source": None, **{f"{size[0]}x{size[1]}": size for _, size in ex.RESOLUTION_PRESETS if size}}

def _hook(stage, job):
    """Deterministic lifecycle seam; production does nothing."""

@dataclass
class Export:
    job_id: str
    project_id: str
    saved_revision: int
    saved_sha256: str
    render: ex.ExportJob
    identities: dict
    project_fd: int
    project_path: Path
    output_name: str
    state: str = "PREPARING"
    progress_percent: int | None = None
    cancel_requested: bool = False
    error: dict | None = None
    output: dict | None = None
    leftover: str | None = None
    worker: ex.ExportWorker | None = None
    outcome: tuple | None = None
    started: float = field(default_factory=time.monotonic)
    rendering_at: float | None = None
    cancelling_at: float | None = None
    timeout: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    done: threading.Event = field(default_factory=threading.Event)
    owner: threading.Thread | None = None
    supervisor: threading.Thread | None = None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({k: getattr(self, k) for k in (
                "job_id", "project_id", "saved_revision", "saved_sha256", "state",
                "progress_percent", "cancel_requested", "error", "output", "leftover")})
    def progress(self, percent):
        with self.lock:
            self.progress_percent = percent
    def signal(self, state, message=None):
        with self.lock:
            if self.outcome is None:
                self.outcome = (state, message)

class ExportManager:
    def __init__(self, session):
        self.session = session
        self.jobs = OrderedDict()
        self.active = None

    def capabilities(self):
        return dict(available=True, formats=["mp4"], resolutions=list(RESOLUTIONS), encoders=["cpu", "auto"],
            limits=dict(prep_timeout_s=PREP_TIMEOUT_S, render_deadline_s=RENDER_DEADLINE_S,
                cancel_grace_s=CANCEL_GRACE_S, shutdown_deadline_s=SHUTDOWN_DEADLINE_S,
                history=HISTORY_LIMIT, error_chars=240, concurrent_exports=1))

    def start(self, project_id, output_name, format, resolution, encoder):
        if (type(output_name) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.mp4", output_name)
                or type(format) is not str or format != "mp4"
                or type(resolution) is not str or resolution not in RESOLUTIONS
                or type(encoder) is not str or encoder not in {"cpu", "auto"}):
            raise ap.AgentError("INVALID_ARGUMENT", "Choose a simple .mp4 filename, mp4 format, supported resolution and cpu/auto encoder.")
        with self.session.lock:
            if self.session.closing:
                raise ap.AgentError("SHUTTING_DOWN", "Ask the operator to restart the server.")
            p = self.session._project(project_id)
            if not p.saved or p.saved["revision"] != p.revision:
                raise ap.AgentError("PROJECT_NOT_SAVED", "Save the current edit with cove_save_project before exporting.")
            try:
                self.session._verify_project(p)
            except ap.AgentError as exc:
                if exc.code == "PROJECT_CHANGED_ON_DISK":
                    raise ap.AgentError(exc.code, "The saved .coveproj was changed or removed outside this server; call cove_set_timeline again to make a new revision (or create a fresh project), then save and export.") from None
                raise
            if not p.state.clips:
                raise ap.AgentError("EMPTY_TIMELINE", "Place at least one clip and re-save.")
            try:
                os.stat(output_name, dir_fd=p.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ap.AgentError("DESTINATION_EXISTS", "Name already exists in the project folder; it will not be overwritten. Choose another output_name.")
            if self.active is not None:
                raise ap.AgentError("EXPORT_BUSY", f"Export {self.active.job_id} is active; poll or cancel that job_id before starting another.")
            render = ex.build_export_job(copy.deepcopy(p.state), output="render.mp4",
                fmt_key="MP4 (H.264 + AAC)", size=RESOLUTIONS[resolution], encoder_pref=encoder)
            job = Export(uuid.uuid4().hex, project_id, p.saved["revision"], p.saved["sha256"],
                render, dict(p.identities), p.fd, Path(self.session.policy.output_root) / p.dirname, output_name)
            self.jobs[job.job_id] = job
            self.active = job
            job.owner = threading.Thread(target=self._run, args=(job,), name="cove-export-owner")
            job.supervisor = threading.Thread(target=self._supervise, args=(job,), name="cove-export-supervisor")
            try:
                job.supervisor.start()
                job.owner.start()
            except BaseException:
                job.done.set()
                if job.supervisor.is_alive():
                    job.supervisor.join()
                self.jobs.pop(job.job_id)
                self.active = None
                raise
            return {k: job.snapshot()[k] for k in ("job_id", "project_id", "saved_revision", "saved_sha256", "state")}

    def _get(self, job_id):
        with self.session.lock:
            if type(job_id) is not str or job_id not in self.jobs:
                raise ap.AgentError("UNKNOWN_EXPORT", "Unknown or expired job_id; use an id returned by cove_start_export.")
            return self.jobs[job_id]
    def status(self, job_id):
        return self._get(job_id).snapshot()
    def _cancel(self, job, timeout=False):
        with job.lock:
            if job.state in TERMINAL or job.state == "PUBLISHING":
                return
            if not job.cancel_requested:
                job.timeout = timeout
                job.cancel_requested = True
                job.cancelling_at = time.monotonic()
                job.state = "CANCELLING"
            if job.worker is not None:
                job.worker.cancel()
    def cancel(self, job_id):
        job = self._get(job_id)
        self._cancel(job)
        return job.snapshot()

    def _supervise(self, job):
        while not job.done.wait(0.02):
            with job.lock:
                now = time.monotonic()
                deadline = (job.rendering_at + RENDER_DEADLINE_S if job.rendering_at is not None
                            else job.started + PREP_TIMEOUT_S)
                if now >= deadline and job.state not in TERMINAL | {"PUBLISHING"}:
                    self._cancel(job, timeout=True)
                if job.cancel_requested:
                    # Hardware probes belong only to this agent process, not a GUI.
                    if job.worker is None:
                        ex.ff.abort_hardware_probes()
                    if job.worker is not None and now - job.cancelling_at >= CANCEL_GRACE_S:
                        job.worker.kill()

    def _check_cancel(self, job):
        with job.lock:
            if job.cancel_requested:
                raise ap.AgentError("EXPORT_TIMEOUT" if job.timeout else "CANCELLED",
                                    "Shorten the edit, or if you used encoder=auto, retry with encoder=cpu.")

    def _reap(self, job):
        proc = job.worker._proc if job.worker is not None else None
        if proc is not None:
            if proc.poll() is None:
                job.worker.kill()
            proc.wait(timeout=max(0.1, CANCEL_GRACE_S))

    def _check_inputs(self, job):
        if job.state != "PUBLISHING":
            for aid, fd in job.render.input_fds.items():
                if ap.file_identity(os.fstat(fd)) != job.identities[aid]:
                    raise ap.AgentError("ASSET_CHANGED", "Media changed in place during export; re-import and re-save in a fresh project.")

    def _run(self, job):
        workspace = ".export-" + uuid.uuid4().hex
        private_fd = output_fd = None
        owned_dir = owned_output = None
        state, error, output = "FAILED", None, None
        try:
            with ExitStack() as inputs:
                _hook("preparing", job)
                self._check_cancel(job)
                for c in job.render.clips:
                    if c.asset.id in job.render.input_fds:
                        continue
                    fd, _, identity = inputs.enter_context(ap.open_authorized_media(self.session.policy, str(c.path)))
                    if identity != job.identities[c.asset.id]:
                        raise ap.AgentError("ASSET_CHANGED", "Media was replaced; re-import and re-save in a fresh project.")
                    job.render.input_fds[c.asset.id] = fd
                job.render.input_options = ap.INPUT_OPTIONS
                # LIFO callbacks run before the held input descriptors close.
                inputs.callback(self._check_inputs, job)
                inputs.callback(self._reap, job)
                os.mkdir(workspace, 0o700, dir_fd=job.project_fd)
                try:
                    owned_dir = ap.file_identity(os.stat(workspace, dir_fd=job.project_fd, follow_symlinks=False))[:2]
                except OSError:
                    job.leftover = str(job.project_path / workspace)[:4096]
                    raise
                private_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=job.project_fd)
                if ap.file_identity(os.fstat(private_fd))[:2] != owned_dir:
                    raise OSError("Workspace identity changed before descriptor acquisition")
                job.render.output = job.project_path / workspace / "render.mp4"
                if job.render.encoder_pref == "auto":
                    ex.ff.clear_hardware_probe_abort()
                    self._check_cancel(job)
                    codec = ex.resolve_export_video_encoder(ex.ff.EXPORT_FORMATS[job.render.fmt_key], "auto")
                    job.render.encoder_pref = "nvenc" if codec == "h264_nvenc" else "amf" if codec == "h264_amf" else "cpu"
                self._check_cancel(job)
                worker = ex.ExportWorker(job.render)
                direct = Qt.ConnectionType.DirectConnection
                worker.progress.connect(job.progress, direct)
                worker.finished.connect(lambda path: job.signal("SUCCEEDED"), direct)
                worker.failed.connect(lambda message: job.signal("FAILED", message), direct)
                worker.cancelled.connect(lambda: job.signal("CANCELLED"), direct)
                with job.lock:
                    job.worker = worker
                    if job.cancel_requested:
                        worker.cancel()
                    else:
                        job.state = "RENDERING"
                    job.rendering_at = time.monotonic()
                _hook("before_run", job)
                worker.run()
                if job.outcome is None or job.outcome[0] != "SUCCEEDED":
                    self._check_cancel(job)
                    raise ap.AgentError("EXPORT_FAILED", (job.outcome or (None, "Worker did not report a result."))[1] or "Export failed; check the source and FFmpeg installation.")
                try:
                    output_fd = os.open("render.mp4", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=private_fd)
                    st = os.fstat(output_fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
                        raise OSError("Unsafe output inode")
                    owned_output = ap.file_identity(st)[:2]
                    if worker._temp_identity is not None and owned_output != worker._temp_identity:
                        owned_output = None
                        raise OSError("Output identity differs from worker's owned inode")
                    self._check_cancel(job)
                    with job.lock:
                        if not job.cancel_requested:
                            job.state = "VERIFYING"
                    if st.st_size <= 0:
                        raise OSError("Empty output")
                    info = ap.probe_fd(output_fd)
                    expected = ex.resolve_target_size(sorted(job.render.clips, key=lambda c: c.timeline_start), None, job.render.width, job.render.height)
                    if (not info["has_audio"] or "mp4" not in info["container"].split(",")
                            or (info["width"], info["height"]) != expected
                            or abs(info["duration_s"] - job.render.total_timeline) > max(0.5, job.render.total_timeline * 0.03)):
                        raise OSError("Output geometry, streams or duration mismatch")
                except (OSError, ap.AgentError) as exc:
                    raise ap.AgentError("EXPORT_OUTPUT_INVALID", f"Rendered output failed verification: {exc}. Check sources and retry.") from None
                self._check_inputs(job)
                _hook("verified", job)
                # This is the cancellation commit point. Late requests cannot delete output.
                with job.lock:
                    self._check_cancel(job)
                    job.state = "PUBLISHING"
                _hook("publishing", job)
                try:
                    ap.link_output(output_fd, job.project_fd, job.output_name)
                except ap.AgentError as exc:
                    if exc.code == "DESTINATION_EXISTS":
                        raise ap.AgentError(exc.code, "Destination already exists; it will not be overwritten. Choose another output_name.") from None
                    raise
                output = dict(path=str(job.project_path / job.output_name), size_bytes=st.st_size,
                    **{k: info[k] for k in ("duration_s", "width", "height", "video_codec", "audio_codec", "has_audio")})
                state = "SUCCEEDED"
        except ap.AgentError as exc:
            state = "CANCELLED" if exc.code == "CANCELLED" else "FAILED"
            error = None if state == "CANCELLED" else dict(code=exc.code, message=exc.message)
        except Exception as exc:
            error = dict(code="EXPORT_FAILED", message=str(exc)[:240])
        finally:
            if job.worker is not None:
                self._reap(job)
                proc = job.worker._proc
                if proc is not None:
                    for stream in (proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()
            if output_fd is not None:
                os.close(output_fd)
            if owned_output is not None:
                try:
                    if ap.file_identity(os.stat("render.mp4", dir_fd=private_fd, follow_symlinks=False))[:2] == owned_output:
                        os.unlink("render.mp4", dir_fd=private_fd)
                except OSError:
                    pass
            if private_fd is not None:
                os.close(private_fd)
            if owned_dir is not None:
                try:
                    if ap.file_identity(os.stat(workspace, dir_fd=job.project_fd, follow_symlinks=False))[:2] == owned_dir:
                        os.rmdir(workspace, dir_fd=job.project_fd)
                    else:
                        raise OSError("Workspace replaced")
                except OSError:
                    job.leftover = str(job.project_path / workspace)[:4096]
            with self.session.lock, job.lock:
                if job.cancel_requested and job.state != "PUBLISHING":
                    state = "FAILED" if job.timeout else "CANCELLED"
                    error = dict(code="EXPORT_TIMEOUT", message="Shorten the edit, or if you used encoder=auto, retry with encoder=cpu.") if job.timeout else None
                job.state, job.error, job.output = state, error, output
                self.active = None
                self.jobs.move_to_end(job.job_id)
                while len(self.jobs) > HISTORY_LIMIT:
                    oldest = next((jid for jid, j in self.jobs.items() if j.state in TERMINAL), None)
                    if oldest is None:
                        break
                    self.jobs.pop(oldest)
                job.done.set()

    def shutdown(self):
        with self.session.lock:
            self.session.closing = True
            jobs = list(self.jobs.values())
        deadline = time.monotonic() + SHUTDOWN_DEADLINE_S
        for job in jobs:
            self._cancel(job)
        for job in jobs:
            if not job.done.wait(max(0, deadline - time.monotonic() - 1)):
                if job.worker is not None:
                    job.worker.kill()
                else:
                    ex.ff.abort_hardware_probes()
            for thread in (job.owner, job.supervisor):
                if thread is not None and thread.ident is not None:
                    thread.join(max(0, deadline - time.monotonic()))
                    if thread.is_alive():
                        raise ap.AgentError("EXPORT_TIMEOUT", "Export shutdown deadline exceeded; owner has not finished cleanup.")
