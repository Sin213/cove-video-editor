"""Export admission and lifecycle behavior without a Qt application."""
import os, threading, time, pytest
import ctypes, errno, select, shutil, subprocess, sys
from pathlib import Path
from test_agent_session import editing, clip
from test_agent_paths import tree, rejected
from cove_video_editor import agent_exports as ae, agent_paths as ap, exporter as ex

@pytest.fixture
def saved(editing):
    s, pid, aid, p = editing
    s.set_timeline(pid, "sequential", [dict(asset_id=aid, src_start_s=0, src_end_s=3)])
    s.save_project(pid)
    return s, pid, aid, p

def finish(s, started, state="SUCCEEDED"):
    job = s.exports._get(started["job_id"])
    completed = job.done.wait(12)
    if not completed and job.worker is not None:
        # Keep a broken escalation mutant from orphaning its deliberately stubborn child.
        job.worker.kill()
    job.owner.join(1); job.supervisor.join(1)
    assert completed, job.snapshot()
    assert not job.owner.is_alive() and not job.supervisor.is_alive()
    result = s.get_export_status(job.job_id)
    assert result["state"] == state, result
    assert result["leftover"] is None, result
    return result

def block(monkeypatch, stage):
    entered, release = threading.Event(), threading.Event()
    def hook(current, job):
        if current == stage:
            entered.set()
            assert release.wait(10)
    monkeypatch.setattr(ae, "_hook", hook)
    return entered, release

def test_success_direct_signals_and_no_fd_leaks(saved):
    s, pid, _, _ = saved
    before = len(os.listdir("/proc/self/fd"))
    for i in range(3):
        started = s.start_export(pid, f"movie{i}.mp4")
        result = finish(s, started)
        assert result["progress_percent"] == 100 and not result["cancel_requested"]
        assert result["saved_revision"] == s.get_project(pid)["saved_revision"]
        assert result["saved_sha256"] == s.get_project(pid)["saved_sha256"]
        assert (result["output"]["width"], result["output"]["height"]) == (320, 240)
        assert Path(result["output"]["path"]).stat().st_size == result["output"]["size_bytes"]
        assert s.cancel_export(started["job_id"]) == result
        assert len(os.listdir("/proc/self/fd")) == before

def test_success_waits_for_publication(saved, monkeypatch):
    s, pid, _, _ = saved
    entered, release = threading.Event(), threading.Event()
    original = ap.link_output
    def publish(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(ap, "link_output", publish)
    started = s.start_export(pid, "movie.mp4")
    try:
        assert entered.wait(5)
        status = s.get_export_status(started["job_id"])
        assert status["state"] == "PUBLISHING" and status["output"] is None
        assert not (s.exports._get(started["job_id"]).project_path / "movie.mp4").exists()
    finally:
        release.set()
    finish(s, started)

def test_late_cancel_preserves_published_inode(saved):
    s, pid, _, _ = saved
    started = s.start_export(pid, "movie.mp4")
    result = finish(s, started)
    path = Path(result["output"]["path"])
    identity, contents = ap.file_identity(path.stat()), path.read_bytes()
    for _ in range(2):
        assert s.cancel_export(started["job_id"]) == result
        assert path.read_bytes() == contents and ap.file_identity(path.stat()) == identity

@pytest.mark.parametrize("options", [dict(output_name="../x.mp4"), dict(output_name="x.mp4\n"),
    dict(format="mkv"), dict(resolution="640x480"), dict(encoder="nvenc"), dict(encoder=True)])
def test_invalid_before_spawn(saved, monkeypatch, options):
    s, pid, _, _ = saved
    spawn = __import__('unittest.mock', fromlist=['Mock']).Mock(side_effect=AssertionError("spawn"))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with rejected("INVALID_ARGUMENT"):
        s.start_export(pid, **(dict(output_name="movie.mp4") | options))
    assert spawn.call_count == 0

def test_external_changes_and_empty(saved):
    s, pid, _, p = saved
    revision = Path(s.get_project(pid)["saved_path"])
    data = revision.read_bytes(); revision.write_bytes(b"changed")
    with rejected("PROJECT_CHANGED_ON_DISK") as changed:
        s.start_export(pid, "movie.mp4")
    assert "cove_set_timeline" in changed.value.message
    revision.write_bytes(data)
    clips = s.projects[pid].state.clips
    s.projects[pid].state.clips = []
    with rejected("EMPTY_TIMELINE"):
        s.start_export(pid, "movie.mp4")
    s.projects[pid].state.clips = clips
    p.write_bytes(b"replaced")
    with rejected("ASSET_CHANGED"):
        s.start_export(pid, "movie.mp4")
    s.set_timeline(pid, "sequential", [])
    with rejected("PROJECT_NOT_SAVED"):
        s.start_export(pid, "movie.mp4")

def test_exceptional_worker_reaps_child(saved, monkeypatch, tmp_path):
    s, pid, _, _ = saved
    script = tmp_path / "ffmpeg"
    script.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(60)\n'); script.chmod(0o700)
    monkeypatch.setattr(ex.ff, "require_ffmpeg", lambda: str(script))
    spawned, failed = [], threading.Event()
    popen, start = subprocess.Popen, threading.Thread.start
    def spawn(*args, **kwargs):
        proc = popen(*args, **kwargs); spawned.append(proc); return proc
    def thread_start(thread):
        if getattr(thread._target, '__name__', '') == '_drain_stderr':
            failed.set(); raise RuntimeError('Injected reader start failure')
        return start(thread)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(threading.Thread, 'start', thread_start)
    before, began = len(os.listdir('/proc/self/fd')), time.monotonic()
    try:
        job = s.start_export(pid, 'movie.mp4'); assert failed.wait(2)
        result = finish(s, job, 'FAILED')
        assert result['error']['code'] == 'EXPORT_FAILED'
        assert spawned[0].poll() is not None, 'Exceptional worker child was not reaped'
        assert spawned[0].stdout.closed and spawned[0].stderr.closed
        assert time.monotonic()-began < 2 and s.exports.active is None
        assert len(os.listdir('/proc/self/fd')) == before
    finally:
        for proc in spawned:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=2)

def test_cancel_after_real_worker_return(saved, monkeypatch):
    s, pid, _, _ = saved
    entered, release = threading.Event(), threading.Event()
    run = ex.ExportWorker.run
    def wrapped(worker):
        run(worker); entered.set(); assert release.wait(5)
    monkeypatch.setattr(ex.ExportWorker, 'run', wrapped)
    job = s.start_export(pid, 'movie.mp4')
    try:
        assert entered.wait(5)
        assert s.exports._get(job['job_id']).outcome[0] == 'SUCCEEDED'
        s.cancel_export(job['job_id'])
    finally: release.set()
    finish(s, job, 'CANCELLED')
    assert not list(Path(s.policy.output_root).rglob('.export-*'))
    assert not list(Path(s.policy.output_root).rglob('movie.mp4'))

@pytest.mark.parametrize('timeout', [False, True])
def test_probe_error_preserves_cancel_ownership(saved, monkeypatch, timeout):
    s, pid, _, _ = saved
    entered, release, timed_out = threading.Event(), threading.Event(), threading.Event()
    cancel = s.exports._cancel
    def request(job, timeout=False):
        cancel(job, timeout)
        if timeout: timed_out.set()
    def probe(fd):
        entered.set(); assert release.wait(5)
        raise ap.AgentError('PROBE_TIMEOUT', 'Injected verification timeout')
    monkeypatch.setattr(s.exports, '_cancel', request)
    monkeypatch.setattr(ap, 'probe_fd', probe)
    before = len(os.listdir('/proc/self/fd'))
    job = s.start_export(pid, 'movie.mp4')
    try:
        assert entered.wait(5)
        if timeout:
            monkeypatch.setattr(ae, 'RENDER_DEADLINE_S', 0.001)
            assert timed_out.wait(2)
        else: s.cancel_export(job['job_id'])
    finally: release.set()
    result = finish(s, job, 'FAILED' if timeout else 'CANCELLED')
    if timeout: assert result['error']['code'] == 'EXPORT_TIMEOUT'
    assert len(os.listdir('/proc/self/fd')) == before
    assert not list(Path(s.policy.output_root).rglob('movie.mp4'))

@pytest.mark.parametrize('stage', ['before_run', 'probe'])
def test_corrupt_held_source_failure_is_asset_changed(saved, monkeypatch, stage):
    s, pid, _, path = saved
    entered, release = block(monkeypatch, 'before_run')
    if stage == 'probe':
        monkeypatch.setattr(ae, '_hook', lambda *args: None)
        def probe(fd):
            entered.set(); assert release.wait(5); raise OSError('Invalid render')
        monkeypatch.setattr(ap, 'probe_fd', probe)
    before = len(os.listdir('/proc/self/fd'))
    job = s.start_export(pid, 'movie.mp4')
    try:
        assert entered.wait(5); path.write_bytes(b'')
    finally: release.set()
    result = finish(s, job, 'FAILED')
    assert result['error']['code'] == 'ASSET_CHANGED'
    if stage == 'before_run': assert s.exports._get(job['job_id']).outcome[0] == 'FAILED'
    assert len(os.listdir('/proc/self/fd')) == before
    assert not list(Path(s.policy.output_root).rglob('movie.mp4'))

def test_atomic_preparing_admission_and_snapshot(saved, monkeypatch):
    s, pid, aid, _ = saved
    entered, release = block(monkeypatch, "preparing")
    gate = threading.Barrier(3)
    results = []
    def start():
        gate.wait()
        try:
            results.append(s.start_export(pid, "movie.mp4"))
        except ap.AgentError as exc:
            if exc.code == "EXPORT_BUSY": assert s.exports.active.job_id in exc.message and len(exc.message) <= 240
            results.append(exc.code)
    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads: t.start()
    gate.wait()
    for t in threads: t.join(5)
    assert entered.wait(3) and results.count("EXPORT_BUSY") == 1, results
    started = next(r for r in results if isinstance(r, dict))
    job = s.exports._get(started["job_id"])
    original_revision = job.saved_revision
    s.set_timeline(pid, "sequential", [clip(aid, speed=2, volume=0.1)])
    s.import_media(pid, [str(saved[3])])
    release.set()
    result = finish(s, started)
    assert result["saved_revision"] == original_revision
    assert abs(result["output"]["duration_s"] - 3) < 0.2
    assert job.render.clips[0].audio_volume == 1

def test_held_inode_replacement_and_in_place(saved, monkeypatch):
    s, pid, _, p = saved
    entered, release = block(monkeypatch, "before_run")
    started = s.start_export(pid, "held.mp4")
    assert entered.wait(3)
    original = p.with_name("old.mp4"); p.rename(original)
    from test_agent_paths import video
    video(p, True, size="640x480", color="blue")
    release.set()
    result = finish(s, started)
    assert result["output"]["width"] == 320
    rgb = subprocess.run(['ffmpeg', '-v', 'error', '-i', result["output"]["path"],
        '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
        check=True, capture_output=True, timeout=10).stdout
    assert sum(rgb[0::3]) > 3 * sum(rgb[2::3]), "The held red inode must win over the replacement blue pathname"
    p.unlink(); original.rename(p)
    entered, release = block(monkeypatch, "before_run")
    before = len(os.listdir("/proc/self/fd"))
    started = s.start_export(pid, "changed.mp4")
    assert entered.wait(3)
    # Change mtime on the held inode without corrupting the encode.
    st = p.stat(); os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
    release.set()
    result = finish(s, started, "FAILED")
    assert result["error"]["code"] == "ASSET_CHANGED"
    assert len(os.listdir("/proc/self/fd")) == before

@pytest.mark.parametrize("stage", ["preparing", "before_run", "verified", "publishing"])
def test_cancel_commit_point(saved, monkeypatch, stage):
    s, pid, _, _ = saved
    entered, release = block(monkeypatch, stage)
    before = len(os.listdir("/proc/self/fd"))
    started = s.start_export(pid, "movie.mp4")
    assert entered.wait(5)
    snapshot = s.cancel_export(started["job_id"])
    assert snapshot["state"] == ("PUBLISHING" if stage == "publishing" else "CANCELLING")
    assert s.cancel_export(started["job_id"]) == snapshot
    release.set()
    result = finish(s, started, "SUCCEEDED" if stage == "publishing" else "CANCELLED")
    assert s.cancel_export(started["job_id"]) == result
    assert len(list(Path(s.projects[pid].dirname).glob("*.mp4"))) == 0  # no cwd output
    assert len(os.listdir("/proc/self/fd")) == before
    if stage != "publishing":
        assert not (s.exports._get(started["job_id"]).project_path / "movie.mp4").exists()

@pytest.mark.parametrize("kind", ["file", "live_symlink", "dangling_symlink", "directory"])
def test_existing_output_rejected_before_spawn(saved, monkeypatch, kind):
    s, pid, _, _ = saved
    project = Path(s.policy.output_root) / s.projects[pid].dirname
    final, target = project / "blocked.mp4", project / "target"
    if kind == "file": final.write_bytes(b"keep")
    elif kind == "directory": final.mkdir(); (final / "marker").write_bytes(b"keep")
    else:
        if kind == "live_symlink": target.write_bytes(b"keep")
        final.symlink_to(target)
    identity = ap.file_identity(final.lstat())
    entries, jobs = set(project.iterdir()), dict(s.exports.jobs)
    calls = []
    def spawn(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Existing output must be rejected before any subprocess")
    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", spawn)
        guard.setattr(subprocess, "run", spawn)
        with pytest.raises(ap.AgentError) as caught:
            s.start_export(pid, "blocked.mp4")
        assert caught.value.code == "DESTINATION_EXISTS"
        assert "Choose another output_name" in caught.value.message
        assert not calls and s.exports.active is None and s.exports.jobs == jobs
        assert set(project.iterdir()) == entries and ap.file_identity(final.lstat()) == identity
        if kind.endswith("symlink"):
            assert final.is_symlink() and final.readlink() == target
            if kind == "live_symlink": assert target.read_bytes() == b"keep"
            else: assert not target.exists()
        else: assert (final / "marker" if kind == "directory" else final).read_bytes() == b"keep"
    finish(s, s.start_export(pid, "available.mp4"))

@pytest.mark.parametrize("destination", ["file", "symlink", "race"])
def test_no_clobber_and_owned_cleanup(saved, monkeypatch, destination):
    s, pid, _, _ = saved
    project = Path(s.policy.output_root) / s.projects[pid].dirname
    final = project / "movie.mp4"
    unrelated = project / "unrelated"; unrelated.write_bytes(b"keep")
    sibling = project / ".export-unowned"; sibling.mkdir(); (sibling / "render.mp4").write_bytes(b"keep")
    if destination == "symlink": final.symlink_to(unrelated)
    elif destination == "file": final.write_bytes(b"keep")
    else:
        monkeypatch.setattr(ae, "_hook", lambda stage, job: final.write_bytes(b"keep") if stage == "publishing" else None)
    before = len(os.listdir("/proc/self/fd"))
    if destination == "race":
        result = finish(s, s.start_export(pid, "movie.mp4"), "FAILED")
        assert result["error"]["code"] == "DESTINATION_EXISTS"
        assert result["error"]["message"].endswith("Choose another output_name.")
    else:
        with rejected("DESTINATION_EXISTS"):
            s.start_export(pid, "movie.mp4")
    assert final.read_bytes() == unrelated.read_bytes() == (sibling / "render.mp4").read_bytes() == b"keep"
    assert final.is_symlink() == (destination == "symlink")
    assert len(os.listdir("/proc/self/fd")) == before

@pytest.mark.parametrize("kind", ["missing", "empty", "invalid", "dimensions", "duration", "no_audio"])
def test_invalid_output_never_published(saved, monkeypatch, kind):
    s, pid, _, p = saved
    class Worker(ex.ExportWorker):
        def run(self):
            if kind == "empty": self._job.output.touch()
            elif kind == "invalid": self._job.output.write_bytes(b"invalid")
            elif kind != "missing":
                cmd = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    f'color=s={"640x480" if kind == "dimensions" else "320x240"}:d={1 if kind == "duration" else 3}']
                if kind != "no_audio": cmd += ['-f', 'lavfi', '-i', 'sine=duration=3', '-shortest']
                subprocess.run(cmd + ['-y', str(self._job.output)], check=True, capture_output=True, timeout=10)
            self.finished.emit(self._job.output)
    monkeypatch.setattr(ex, "ExportWorker", Worker)
    result = finish(s, s.start_export(pid, "movie.mp4"), "FAILED")
    assert result["error"]["code"] == "EXPORT_OUTPUT_INVALID" and result["output"] is None

def test_error_path_fd_cleanup(saved, monkeypatch):
    s, pid, _, _ = saved
    before = len(os.listdir("/proc/self/fd"))
    def fail(*args, **kwargs): raise PermissionError("workspace denied")
    monkeypatch.setattr(ae.os, "mkdir", fail)
    result = finish(s, s.start_export(pid, "movie.mp4"), "FAILED")
    assert result["error"]["code"] == "EXPORT_FAILED"
    assert len(os.listdir("/proc/self/fd")) == before

@pytest.mark.parametrize('failure', ['open', 'stat'])
def test_workspace_acquisition_failure_cleanup(saved, monkeypatch, failure):
    s, pid, _, _ = saved
    project = Path(s.policy.output_root) / s.projects[pid].dirname
    unrelated = project / '.export-unowned'; unrelated.mkdir(); (unrelated / 'keep').write_bytes(b'keep')
    before = len(os.listdir('/proc/self/fd'))
    real_open, real_stat = os.open, os.stat
    def fail(operation, real, name, *args, **kwargs):
        if operation == failure and str(name).startswith('.export-'):
            raise OSError(errno.EMFILE, 'Injected acquisition failure')
        return real(name, *args, **kwargs)
    with monkeypatch.context() as guard:
        guard.setattr(os, 'open', lambda name, *a, **kw: fail('open', real_open, name, *a, **kw))
        guard.setattr(os, 'stat', lambda name, *a, **kw: fail('stat', real_stat, name, *a, **kw))
        started = s.start_export(pid, 'movie.mp4')
        job = s.exports._get(started['job_id']); assert job.done.wait(3)
        job.owner.join(1); job.supervisor.join(1)
        result = s.get_export_status(job.job_id)
    assert result['state'] == 'FAILED' and len(result['error']['message']) <= 240
    assert not job.owner.is_alive() and not job.supervisor.is_alive() and s.exports.active is None
    remaining = set(project.glob('.export-*')) - {unrelated}
    if failure == 'open': assert result['leftover'] is None and not remaining
    else: assert remaining == {Path(result['leftover'])}
    assert (unrelated / 'keep').read_bytes() == b'keep'
    assert len(os.listdir('/proc/self/fd')) == before
    finish(s, s.start_export(pid, 'available.mp4'))

@pytest.mark.parametrize("mode", ["failure", "cancel", "deadline", "shutdown", "cancel_deadline", "deadline_cancel"])
def test_chatty_child_deadlines_and_reaping(saved, monkeypatch, tmp_path, mode):
    s, pid, _, _ = saved
    script = tmp_path / "fake-ffmpeg"
    script.write_text(f'#!{sys.executable}\nimport os,signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
        'os.write(2,b"diagnostic\\n"*100000)\nprint("out_time_us=1000000",flush=True)\n'
        + ('raise SystemExit(7)\n' if mode == "failure" else 'while True: time.sleep(0.01)\n'))
    script.chmod(0o700)
    monkeypatch.setattr(ex.ff, "require_ffmpeg", lambda: str(script))
    monkeypatch.setattr(ae, "CANCEL_GRACE_S", 0.1)
    monkeypatch.setattr(ae, "RENDER_DEADLINE_S", 0.4 if mode in {"deadline", "deadline_cancel"} else 10)
    monkeypatch.setattr(ae, "SHUTDOWN_DEADLINE_S", 2)
    expired, rendered, release = threading.Event(), threading.Event(), threading.Event()
    cancel, run = s.exports._cancel, ex.ExportWorker.run
    def stop(job, timeout=False):
        cancel(job, timeout)
        if timeout: expired.set()
    def blocked(worker):
        run(worker); rendered.set(); assert release.wait(5)
    monkeypatch.setattr(s.exports, '_cancel', stop)
    if mode == 'cancel_deadline': monkeypatch.setattr(ex.ExportWorker, 'run', blocked)
    before = len(os.listdir("/proc/self/fd"))
    started = s.start_export(pid, "movie.mp4")
    job = s.exports._get(started["job_id"])
    deadline = time.monotonic() + 2
    while s.get_export_status(job.job_id)["progress_percent"] is None and not job.done.is_set():
        assert time.monotonic() < deadline
        job.done.wait(0.01)
    if mode != "failure":
        assert job.progress_percent > 0 and job.worker._proc.poll() is None
    began = time.monotonic()
    if mode == 'cancel_deadline':
        try:
            s.cancel_export(job.job_id); assert rendered.wait(2)
            assert job.worker._proc.returncode == -9
            monkeypatch.setattr(ae, 'RENDER_DEADLINE_S', 0.001)
            assert expired.wait(2) and not job.timeout, 'Later deadline overwrote user cancellation'
        finally: release.set()
    elif mode == 'deadline_cancel':
        assert expired.wait(2)
        s.cancel_export(job.job_id); assert job.timeout
    elif mode == "cancel": assert s.cancel_export(job.job_id)["state"] == "CANCELLING"
    elif mode == "shutdown": s.shutdown_exports()
    result = finish(s, started, "FAILED" if mode in {"failure", "deadline", "deadline_cancel"} else "CANCELLED")
    assert time.monotonic() - began < 2
    assert job.worker._proc.poll() is not None
    if mode != "failure": assert job.worker._proc.returncode == -9
    if mode == "failure": assert result["error"]["code"] == "EXPORT_FAILED" and "exited 7" in result["error"]["message"]
    if mode in {"deadline", "deadline_cancel"}: assert result["error"]["code"] == "EXPORT_TIMEOUT"
    assert len(os.listdir("/proc/self/fd")) == before

def test_preparation_deadline_history_and_shutdown(saved, monkeypatch):
    s, pid, _, _ = saved
    monkeypatch.setattr(ae, "PREP_TIMEOUT_S", 0.1)
    def hook(stage, job):
        if stage == "preparing":
            while not job.cancel_requested: job.done.wait(0.01)
    monkeypatch.setattr(ae, "_hook", hook)
    result = finish(s, s.start_export(pid, "movie.mp4"), "FAILED")
    assert result["error"]["code"] == "EXPORT_TIMEOUT"
    monkeypatch.setattr(ae, "_hook", lambda *args: None)
    monkeypatch.setattr(ae, "PREP_TIMEOUT_S", 30)
    monkeypatch.setattr(ae, "HISTORY_LIMIT", 2)
    for i in range(3): finish(s, s.start_export(pid, f"movie{i}.mp4"))
    assert len(s.exports.jobs) == 2
    with rejected("UNKNOWN_EXPORT"): s.get_export_status(result["job_id"])
    s.shutdown_exports()
    with rejected("SHUTTING_DOWN"): s.start_export(pid, "closed.mp4")

def test_two_processes_publish_exactly_one(tmp_path):
    source = tmp_path / "source"; source.write_bytes(b"owned")
    code = ('import os,sys; from cove_video_editor.agent_paths import link_output,AgentError; '
        'fd=os.open(sys.argv[1],os.O_RDONLY); directory=os.open(sys.argv[2],os.O_RDONLY); '
        'sys.stdin.readline();\ntry: link_output(fd,directory,"final"); print("WON")\n'
        'except AgentError as e: print(e.code)\nfinally: os.close(fd); os.close(directory)')
    children = [subprocess.Popen([sys.executable, '-c', code, str(source), str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for p in children: p.stdin.write("go\n"); p.stdin.flush()
    results = [p.communicate(timeout=10) for p in children]
    assert sorted(r[0].strip() for r in results) == ['DESTINATION_EXISTS', 'WON']
    assert all(p.returncode == 0 and not r[1] for p, r in zip(children, results))
    assert (tmp_path / "final").read_bytes() == b"owned"

def test_render_restrictions_calibrated_file_observer(tmp_path):
    from PySide6.QtCore import Qt
    from test_exporter_agent_hooks import job
    segment = tmp_path / "observed.ts"
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=s=320x240:d=3',
        '-f','lavfi','-i','sine=duration=3','-c:v','mpeg2video','-c:a','mp2','-shortest',str(segment)],
        check=True, capture_output=True, timeout=10)
    manifest = tmp_path / "clip.mp4"
    target_fd = os.open(segment, os.O_RDONLY | os.O_CLOEXEC)
    # A relative safe concat filename resolves beside /dev/fd/<manifest fd>.
    manifest.write_text(f"ffconcat version 1.0\nfile '{target_fd}'\nduration 3\n")
    libc = ctypes.CDLL(None, use_errno=True)
    watcher = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    assert watcher >= 0 and libc.inotify_add_watch(watcher, os.fsencode(segment), 0x20) >= 0
    try:
        for restricted in (False, True):
            j = job(tmp_path, input_options=ap.INPUT_OPTIONS if restricted else ())
            with ap.open_authorized_media(ap.PathPolicy((str(tmp_path),), None, None), str(manifest)) as (fd, _, _):
                j.input_fds = {j.clips[0].asset.id: fd, "observer-target": target_fd}
                worker = ex.ExportWorker(j)
                results = []
                worker.finished.connect(lambda p: results.append('success'), Qt.ConnectionType.DirectConnection)
                worker.failed.connect(lambda e: results.append(e), Qt.ConnectionType.DirectConnection)
                owner = threading.Thread(target=worker.run); owner.start(); owner.join(10)
                assert not owner.is_alive()
                fired = bool(select.select([watcher], [], [], 0)[0])
                if fired: os.read(watcher, 65536)
                assert fired == (not restricted), (restricted, results)
                assert (results == ['success']) if not restricted else (len(results) == 1 and results[0] != 'success')
                if worker._proc:
                    worker._proc.stdout.close(); worker._proc.stderr.close()
    finally:
        os.close(watcher)
        os.close(target_fd)

def test_requires_current_saved_revision(editing):
    s, pid, aid, _ = editing
    with rejected("PROJECT_NOT_SAVED"):
        s.start_export(pid, "movie.mp4")
    s.set_timeline(pid, "sequential", [clip(aid)])
    s.save_project(pid)
    s.set_timeline(pid, "sequential", [clip(aid, volume=0.5)])
    with rejected("PROJECT_NOT_SAVED"):
        s.start_export(pid, "movie.mp4")

def test_unknown_export(editing):
    s, _, _, _ = editing
    for method in (s.get_export_status, s.cancel_export):
        with rejected("UNKNOWN_EXPORT"):
            method("unknown")
