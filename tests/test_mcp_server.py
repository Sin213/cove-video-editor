from __future__ import annotations
import json, os, subprocess, sys, pytest
import array, math, selectors, signal, tempfile, time
from pathlib import Path
from test_agent_paths import video
def launch(*args, code=None, input=""):
    cmd = [sys.executable, "-m", "cove_video_editor.mcp_server", *args] if code is None else [sys.executable, "-c", code]
    return subprocess.run(cmd, input=input, text=True, capture_output=True, timeout=10)
def test_stdio_invalid_root(tmp_path):
    result = launch("--media-root", str(tmp_path / "missing"))
    assert result.returncode == 2 and not result.stdout and "root" in result.stderr.lower()
def test_stdio_eof():
    pytest.importorskip("mcp")
    result = launch()
    assert result.returncode == 0 and result.stdout == ""
@pytest.mark.parametrize("module", ["mcp", "mcp_types", "anyio", "pydantic", "pydantic_core", "typing_extensions"])
def test_missing_optional_dependencies(module):
    result = launch(code=f"import sys; sys.modules[{module!r}]=None; from cove_video_editor.mcp_server import main; raise SystemExit(main([]))")
    assert result.returncode == 1 and not result.stdout and "mcp" in result.stderr.lower()
    assert "from the Cove source folder: pip install '.[mcp]'" in result.stderr and "Traceback" not in result.stderr
def test_cli_permission_help_and_errors(tmp_path, capsys):
    from cove_video_editor.mcp_server import main
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    help_text = capsys.readouterr().out
    assert exc.value.code == 0
    assert all(word in help_text for word in ("--media-root DIR", "--output-root DIR", "subfolders", "repeatable", "cannot widen", "exit 2", "exit 1"))
    file = tmp_path / "file"; file.touch()
    for flag in ("--media-root", "--output-root", "--project-root"):
        for value, reason in (("relative", "not an absolute path"), ("~/x", "not an absolute path"), (str(tmp_path / "missing"), "does not exist"), (str(file), "not a directory"), ("/", "is the filesystem root"), (str(tmp_path / "bad\nname"), "does not exist"), ("/tmp/a:b", 'contains ":" or "\\", which is not supported')):
            assert main([flag, value]) == 2
            err = capsys.readouterr().err
            assert err == f"error: {flag} {value!r}: {reason}\n" and "OUTPUT_UNSAFE" not in err
def test_output_root_permission_error(tmp_path, monkeypatch, capsys):
    from cove_video_editor import agent_paths as ap, mcp_server as module
    def denied(*args):
        raise PermissionError()
    monkeypatch.setattr(ap.os, "open", denied)
    assert module.main(["--output-root", str(tmp_path)]) == 2
    assert capsys.readouterr().err == f"error: --output-root {str(tmp_path)!r}: cannot be opened (check permissions)\n"
def test_lazy_import():
    result = launch(code="import sys; import cove_video_editor.mcp_server, cove_video_editor.agent_session; assert 'mcp' not in sys.modules")
    assert result.returncode == 0 and result.stdout == ""
    env = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
    result = subprocess.run(["python3", "-c", "import sys; import cove_video_editor.app; assert 'mcp' not in sys.modules"],
                            env=env, capture_output=True, timeout=15)
    assert result.returncode == 0 and not result.stdout
@pytest.mark.parametrize("roots", [(), ("--media-root",), ("--output-root",), ("--media-root", "--output-root"), ("--media-root", "--output-root", "--project-root")])
def test_startup_permission_notice(tmp_path, monkeypatch, capsys, roots):
    from cove_video_editor import mcp_server as module
    class Server:
        def run(self, **kwargs):
            pass
    monkeypatch.setattr(module, "build_server", lambda session: Server())
    assert module.main([arg for flag in roots for arg in (flag, str(tmp_path))]) == 0
    captured = capsys.readouterr()
    assert not captured.out and captured.err.count("Notice:") == (len(roots) < 3)
    for flag in ("--media-root", "--project-root", "--output-root"):
        assert (flag in captured.err) == (flag not in roots)
    if "--project-root" not in roots:
        assert "needed only to import existing projects" in captured.err
@pytest.mark.parametrize('mode', ['before', 'cleanup', 'cleanup_twice', 'failed', 'failed_signal'])
def test_signal_exit_after_cleanup(monkeypatch, mode):
    import threading
    from types import SimpleNamespace
    from cove_video_editor import mcp_server as module
    events, handlers, messages = [], {}, []
    class Exited(BaseException): pass
    close, real_signal = module.AgentSession.close, signal.signal
    release = threading.Event()
    class Owner(threading.Thread):
        def join(self, timeout=None):
            super().join(timeout); events.append('joined')
    job = SimpleNamespace(lock=threading.RLock(), state='RENDERING', cancel_requested=False, timeout=False,
        worker=SimpleNamespace(cancel=lambda: (events.append('cancelled'), release.set())),
        done=threading.Event(), supervisor=None)
    job.owner = Owner(target=lambda: (release.wait(5), job.done.set()))
    def cleanup(session):
        try:
            if mode not in {'before', 'failed'}:
                os.kill(os.getpid(), signal.SIGTERM)
                if mode == 'cleanup_twice': os.kill(os.getpid(), signal.SIGINT)
            if mode.startswith('failed'): raise module.AgentError('EXPORT_TIMEOUT', 'Injected shutdown failure')
            close(session); events.append('closed')
        finally:
            if job.owner.is_alive(): release.set(); job.owner.join(2)
    class Server:
        def run(self, **kwargs):
            if mode == 'before': handlers[signal.SIGTERM](signal.SIGTERM, None)
    def install(sig, handler):
        if sig not in handlers: handlers[sig] = handler
        else: events.append('restored')
        return real_signal(sig, handler)
    def exit_process(code):
        assert code == (1 if mode == 'failed' else 143)
        assert events == (['joined'] if mode.startswith('failed') else ['cancelled', 'joined', 'closed']) + ['restored', 'restored', 'restored', 'flushed', 'flushed']
        raise Exited(code)
    class Stream:
        def write(self, text): assert self is not module.sys.stdout; messages.append(text)
        def flush(self): events.append('flushed')
    monkeypatch.setattr(module.AgentSession, 'close', cleanup)
    def build(session):
        session.exports.jobs['signal'] = job; job.owner.start(); return Server()
    monkeypatch.setattr(module, 'build_server', build)
    monkeypatch.setattr(module.signal, 'signal', install)
    monkeypatch.setattr(os, '_exit', exit_process)
    monkeypatch.setattr(module.sys, 'stdout', Stream())
    monkeypatch.setattr(module.sys, 'stderr', Stream())
    with pytest.raises(Exited) as exited: module.main([])
    assert exited.value.args == (1 if mode == 'failed' else 143,)
    assert job.cancel_requested == (not mode.startswith('failed')) and not job.owner.is_alive()
    if mode.startswith('failed'): assert ''.join(messages).splitlines()[-1] == 'Cove MCP: shutdown incomplete: EXPORT_TIMEOUT'
@pytest.mark.parametrize("transport", ["memory", "stdio"])
def test_tools_and_workflow(tmp_path, transport):
    pytest.importorskip("mcp")
    import anyio
    import mcp_types as types
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client import Client
    from mcp.client._memory import InMemoryTransport
    from contextlib import asynccontextmanager
    from cove_video_editor.agent_paths import validate_roots
    from cove_video_editor.agent_session import AgentSession
    from cove_video_editor.mcp_server import build_server
    from cove_video_editor.project_io import load_project
    root, out = tmp_path / "media", tmp_path / "output"
    root.mkdir()
    out.mkdir()
    p = video(root / "clip.mp4", True)
    outside = video(tmp_path / "outside.mp4")
    errors = []
    async def advertised_roots(ctx):
        return types.ListRootsResult(roots=[types.Root(uri=tmp_path.as_uri())])
    async def protocol():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "cove_video_editor.mcp_server",
            "--media-root", str(root), "--output-root", str(out)], cwd=tmp_path,
            env=dict(os.environ, PYTHONPATH=str(Path("src").resolve())))
        @asynccontextmanager
        async def connect():
            with anyio.fail_after(12):
                if transport == "memory":
                    session = AgentSession(validate_roots([str(root)], str(out)))
                    try:
                        async with Client(InMemoryTransport(build_server(session)), mode="legacy",
                            read_timeout_seconds=5, list_roots_callback=advertised_roots) as connected:
                            yield connected.session
                    finally:
                        session.close()
                else:
                    async with stdio_client(parameters) as streams:
                        async with ClientSession(*streams, read_timeout_seconds=5, list_roots_callback=advertised_roots) as client:
                            await client.initialize()
                            yield client
        async with connect() as client:
                tools = (await client.list_tools()).tools
                names = {t.name for t in tools}
                assert names == {"cove_" + n for n in ("get_capabilities", "inspect_media", "create_project",
                    "import_media", "set_timeline", "get_project", "save_project", "start_export", "get_export_status", "cancel_export", "set_audio", "set_audio_mix", "set_subtitles", "set_crop", "open_project")}
                assert all(t.input_schema.get("additionalProperties") is False and t.output_schema for t in tools)
                assert all(len(t.description) >= 80 for t in tools)
                schemas = {t.name: t.input_schema for t in tools}
                assert "absolute" in schemas["cove_import_media"]["properties"]["paths"]["description"]
                fields = schemas["cove_set_timeline"]["$defs"]["ClipInput"]["properties"]
                assert all(fields[n].get("description") for n in ("src_start_s", "src_end_s", "speed", "volume", "mute", "timeline_start_s"))
                assert "from cove_import_media" in fields["asset_id"]["description"]
                assert all("1.0 = unchanged" in fields[n]["description"] for n in ("volume", "speed"))
                assert "from cove_create_project" in schemas["cove_get_project"]["properties"]["project_id"]["description"]
                assert "back-to-back" in schemas["cove_set_timeline"]["properties"]["placement"]["description"]
                assert fields["src_end_s"]["maximum"] == 2592000 and fields["timeline_start_s"]["maximum"] == 86400
                async def call(operation, **args):
                    r = await client.call_tool("cove_" + operation, args)
                    assert not r.is_error, r
                    return r.structured_content
                caps = await call("get_capabilities")
                assert caps["export"]["available"] is True and not list(out.iterdir())
                inspected = await call("inspect_media", paths=[str(p), str(outside)])
                assert inspected["media"][1]["error"]["code"] == "PATH_NOT_AUTHORIZED"
                created = await call("create_project", name="Stdio film")
                pid = created["project_id"]
                imported = await call("import_media", project_id=pid, paths=[str(p)])
                aid = imported["assets"][0]["asset_id"]
                base = dict(asset_id=aid, src_start_s=0.25, src_end_s=2.5, speed=2, volume=0.4, mute=True)
                await call("set_timeline", project_id=pid, placement="sequential", clips=[base])
                before = await call("get_project", project_id=pid)
                for bad in ({"speed": True}, {"speed": "1"}, {"speed": "SECRET_INPUT"}, {"speed": float("nan")}, {"speed": float("inf")},
                            {"speed": -1}, {"speed": 5}, {"volume": 3}, {"extra": 1}):
                    r = await client.call_tool("cove_set_timeline", dict(project_id=pid, placement="sequential", clips=[base | bad]))
                    assert r.is_error
                    assert r.structured_content["error"]["code"] == "INVALID_ARGUMENT" and "clips.0" in r.structured_content["error"]["message"]
                    assert "SECRET_INPUT" not in r.content[0].text
                    assert await call("get_project", project_id=pid) == before
                for name, args in [("cove_get_project", {"project_id": "unknown"}),
                    ("cove_get_capabilities", {"roots": [str(tmp_path)]}),
                    ("cove_create_project", {"name": "x", "media_roots": [str(tmp_path)]}),
                    ("missing_tool", {}), ("cove_import_media", {"project_id": pid})]:
                    r = await client.call_tool(name, args)
                    assert r.is_error
                    errors.append(r)
                assert "UNKNOWN_PROJECT" in errors[0].content[0].text
                assert errors[0].structured_content == {"error": {"code": "UNKNOWN_PROJECT", "message": "Unknown project_id. The server may have restarted; unsaved projects are lost - call cove_create_project."}}
                assert "Unknown tool" in errors[3].content[0].text and errors[3].structured_content is None
                assert "Field required" in errors[4].content[0].text and errors[4].structured_content["error"]["code"] == "INVALID_ARGUMENT"
                assert all("Extra inputs are not permitted" in r.content[0].text for r in errors[1:3])
                r = await client.call_tool("cove_get_capabilities", {f"key{i}": "SECRET_INPUT" for i in range(3000)})
                assert r.is_error and r.structured_content["error"]["code"] == "INVALID_ARGUMENT"
                assert len(r.structured_content["error"]["message"]) <= 240 and len(r.content[0].text) <= 258 and "SECRET_INPUT" not in r.content[0].text
                print("SDK error shapes:", json.dumps([r.model_dump(mode="json") for r in errors]))
                from test_agent_editing import tones
                music = tones(root / 'sdk-audio.wav')
                summary = await call('set_audio', project_id=pid, items=[dict(path=str(music),volume=0.8,mute=True)])
                assert summary['audio'][0]['volume'] == 0.8 and summary['audio'][0]['mute']
                assert summary['audio'][0]['src_end_s'] == 4 and summary['audio'][0]['lane'] == 1
                summary = await call('set_audio_mix', project_id=pid, original_gain=0.7, replace_original=True)
                summary = await call('set_audio_mix', project_id=pid, added_gain=0.5)
                assert summary['mix'] == dict(original_gain=0.7,added_gain=0.5,replace_original=True)
                for tool, bad in [('set_audio',{'items':[{'path':str(music),'lane':True}]}),
                    ('set_audio',{'items':[{'path':str(music),'src_end_s':None}]}),
                    ('set_audio',{'items':[{'path':str(music),'volume':True}]}),
                    ('set_audio',{'items':[{'path':str(music),'unknown':1}]}),
                    ('set_audio',{'items':'[]'}), ('set_audio_mix',{}),
                    ('set_audio_mix',{'added_gain':0.123}), ('set_audio_mix',{'added_gain':None}),
                    ('set_audio_mix',{'replace_original':1}), ('set_audio_mix',{'unknown':1})]:
                    r = await client.call_tool('cove_'+tool, dict(project_id=pid)|bad)
                    assert r.is_error and r.structured_content['error']['code'] == 'INVALID_ARGUMENT'
                    assert await call('get_project', project_id=pid) == summary
                saved = await call("save_project", project_id=pid)
                state = load_project(Path(saved["path"]))
                assert (state.clips[0].src_start, state.clips[0].src_end, state.clips[0].speed,
                        state.clips[0].audio_volume, state.clips[0].muted) == (0.25, 2.5, 2, 0.4, True)
                assert state.added_audios[0].volume == 0.8 and state.added_audios[0].muted
                assert (state.original_gain,state.added_gain,state.replace_added_audio) == (0.7,0.5,True)
                # Independent servers share the root while both connections are live.
                async with connect() as second:
                    r = await second.call_tool("cove_create_project", {"name": "Stdio film"})
                    assert not r.is_error and r.structured_content["project_id"] != pid
    async def run_protocol():
        async def heartbeat():
            # Sandbox-only thread-notification workaround; harmless elsewhere.
            while True:
                await anyio.sleep(0.01)
        async with anyio.create_task_group() as group:
            group.start_soon(heartbeat)
            try:
                await protocol()
            finally:
                group.cancel_scope.cancel()
    anyio.run(run_protocol)
    assert len(list(out.iterdir())) == 2
@pytest.mark.parametrize("failure", [ImportError("unrelated dependency"), ModuleNotFoundError("unrelated dependency", name="unrelated_dependency")])
def test_unrelated_import_failure(monkeypatch, failure):
    from cove_video_editor import mcp_server as module
    def unrelated(session):
        raise failure
    monkeypatch.setattr(module, "build_server", unrelated)
    with pytest.raises(type(failure), match="unrelated"):
        module.main([])
def test_stdio_raw_stdout_and_eof(tmp_path):
    pytest.importorskip("mcp")
    import selectors
    import tempfile
    import time
    # Real stdio catches stdout contamination while an actual ffprobe child runs.
    root = tmp_path / "media"
    root.mkdir()
    media = video(root / "clip.mp4")
    requests = [dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2025-11-25",
        capabilities={}, clientInfo=dict(name="test", version="1"))),
        dict(jsonrpc="2.0", method="notifications/initialized"),
        dict(jsonrpc="2.0", id=2, method="tools/list"),
        dict(jsonrpc="2.0", id=3, method="tools/call", params=dict(name="cove_get_capabilities", arguments={})),
        dict(jsonrpc="2.0", id=4, method="tools/call", params=dict(name="cove_inspect_media", arguments=dict(paths=[str(media)])))]
    with tempfile.TemporaryFile() as errors, selectors.DefaultSelector() as selector:
        child = subprocess.Popen([sys.executable, "-m", "cove_video_editor.mcp_server", "--media-root", str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, bufsize=0)
        selector.register(child.stdout, selectors.EVENT_READ)
        pending, replies = bytearray(), []
        try:
            for request in requests:
                child.stdin.write((json.dumps(request) + "\n").encode())
                if "id" not in request:
                    continue
                deadline = time.monotonic() + 10
                while b"\n" not in pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("stdio reply deadline")
                    chunk = os.read(child.stdout.fileno(), 65536)
                    assert chunk, "EOF before reply"
                    pending.extend(chunk)
                line, _, pending = pending.partition(b"\n")
                reply = json.loads(line)
                assert reply["jsonrpc"] == "2.0" and reply["id"] == request["id"]
                replies.append(reply)
            child.stdin.close()
            child.wait(timeout=10)
            assert child.returncode == 0
            tail = bytes(pending) + child.stdout.read()
            assert all(json.loads(line)["jsonrpc"] == "2.0" for line in tail.splitlines())
            assert replies[-1]["result"]["structuredContent"]["media"][0]["kind"] == "video"
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stdin.close()

def export_media(root, duration=4.5):
    root.mkdir()
    paths = [root / f"{color}.mp4" for color in ('red', 'blue')]
    for color, path in zip(('red', 'blue'), paths):
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={color}:s=320x240:r=30:d={duration}',
            '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-c:v', 'mpeg4', '-shortest', str(path)],
            check=True, capture_output=True, timeout=15)
    return paths

def test_sdk_stdio_export_audio_oracle(tmp_path):
    pytest.importorskip('mcp')
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from cove_video_editor.project_io import load_project
    root, out = tmp_path / 'media', tmp_path / 'out'; out.mkdir()
    paths = export_media(root)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'cove_video_editor.mcp_server',
            '--media-root', str(root), '--output-root', str(out)], env=dict(os.environ))
        with anyio.fail_after(25):
            async with stdio_client(params) as streams, ClientSession(*streams, read_timeout_seconds=10) as client:
                await client.initialize()
                assert len((await client.list_tools()).tools) == 15
                async def call(name, /, **args):
                    result = await client.call_tool('cove_' + name, args)
                    assert not result.is_error, result
                    return result.structured_content
                assert all('error' not in m for m in (await call('inspect_media', paths=list(map(str, paths))))['media'])
                pid = (await call('create_project', name='Joined film'))['project_id']
                assets = (await call('import_media', project_id=pid, paths=list(map(str, paths))))['assets']
                await call('set_timeline', project_id=pid, placement='sequential', clips=[
                    dict(asset_id=assets[0]['asset_id'], src_start_s=1.0, src_end_s=4.0),
                    dict(asset_id=assets[1]['asset_id'], src_start_s=0.0, src_end_s=3.0, volume=0.5)])
                saved = await call('save_project', project_id=pid)
                for bad in ({'format': 'mkv'}, {'resolution': 'wrong'}, {'encoder': True}, {'output_name': '../bad.mp4'}, {'input_fds': {}}, {'input_options': []}):
                    r = await client.call_tool('cove_start_export', dict(project_id=pid, output_name='joined.mp4') | bad)
                    assert r.is_error and r.structured_content['error']['code'] == 'INVALID_ARGUMENT'
                started = await call('start_export', project_id=pid, output_name='joined.mp4', encoder='cpu')
                progress = []
                while True:
                    status = await call('get_export_status', job_id=started['job_id'])
                    if status['progress_percent'] is not None: progress.append(status['progress_percent'])
                    if status['state'] in {'SUCCEEDED', 'FAILED', 'CANCELLED'}: break
                    await anyio.sleep(0.01)
                assert status['state'] == 'SUCCEEDED' and any(p > 0 for p in progress), status
                assert status['saved_sha256'] == saved['sha256'] and status['saved_revision'] == saved['revision']
                state = load_project(Path(saved['path']))
                assert len(state.clips) == 2 and state.clips[1].audio_volume == 0.5
                output = status['output']
                assert (output['width'], output['height'], output['video_codec'], output['audio_codec']) == (320, 240, 'h264', 'aac')
                # 0.2 s allows frame quantization and AAC priming without accepting a lost clip.
                assert abs(output['duration_s'] - 6) < 0.2
                pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', output['path'], '-vn', '-ac', '1',
                    '-ar', '48000', '-f', 's16le', '-'], check=True, capture_output=True, timeout=10).stdout
                samples = array.array('h'); samples.frombytes(pcm)
                def rms(start, end):
                    segment = samples[int(start*48000):int(end*48000)]
                    return math.sqrt(sum(x*x for x in segment)/len(segment))
                # Interior windows avoid the concat boundary; 10% allows lossy AAC variation.
                assert 0.45 < rms(4.2, 5.5) / rms(1.2, 2.5) < 0.55
    anyio.run(run)

class ExportStdio:
    def __init__(self, root, out, prepare=False):
        code = ('from cove_video_editor import agent_exports as ae; '
            'from cove_video_editor.mcp_server import main; '
            'ae._hook=lambda stage,job: wait(job) if stage=="preparing" else None;\n'
            'def wait(job):\n while not job.cancel_requested: job.done.wait(0.01)\n'
            'raise SystemExit(main())')
        cmd = [sys.executable, '-c', code] if prepare else [sys.executable, '-m', 'cove_video_editor.mcp_server']
        self.errors = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd + ['--media-root', str(root), '--output-root', str(out)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, bufsize=0, start_new_session=True)
        self.selector = selectors.DefaultSelector(); self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.pending, self.counter = bytearray(), 0
        try:
            self.request('initialize', dict(protocolVersion='2025-11-25', capabilities={}, clientInfo=dict(name='exports',version='1')))
            self.write(dict(jsonrpc='2.0', method='notifications/initialized'))
        except BaseException:
            self.close(check_frames=False)
            raise
    def write(self, message):
        self.proc.stdin.write((json.dumps(message)+'\n').encode())
    def request(self, method, params):
        self.counter += 1
        self.write(dict(jsonrpc='2.0', id=self.counter, method=method, params=params))
        deadline = time.monotonic()+10
        while True:
            assert time.monotonic() < deadline, 'reply deadline'
            while b'\n' not in self.pending:
                assert self.selector.select(max(0, deadline-time.monotonic())), 'reply deadline'
                chunk = os.read(self.proc.stdout.fileno(), 65536); assert chunk, 'premature EOF'
                self.pending.extend(chunk)
                assert len(self.pending) <= 1024*1024, 'oversized protocol frame'
            line, _, self.pending = self.pending.partition(b'\n')
            reply = json.loads(line)  # Every stdout line must be a protocol frame.
            assert reply['jsonrpc'] == '2.0'
            if reply.get('id') == self.counter: return reply['result']
    def call(self, name, /, **args):
        result = self.request('tools/call', dict(name='cove_'+name, arguments=args))
        assert not result.get('isError'), result
        return result['structuredContent']
    def children(self):
        # Linux attributes spawned children to the owner thread, not the main thread.
        children = set()
        for task in Path(f'/proc/{self.proc.pid}/task').glob('*'):
            try: children.update(task.joinpath('children').read_text().split())
            except FileNotFoundError: pass
        return children
    def close(self, check_frames=True):
        try:
            if self.proc.poll() is None:
                self.proc.stdin.close()
                self.proc.wait(timeout=16)
            tail = bytes(self.pending) + self.proc.stdout.read()
            if check_frames:
                assert all(json.loads(line)['jsonrpc'] == '2.0' for line in tail.splitlines())
        finally:
            if self.proc.poll() is None:
                self.proc.kill(); self.proc.wait(timeout=3)
            # The helper owns this private group, including a broken mutant's child.
            try: os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            self.proc.stdout.close(); self.proc.stdin.close(); self.selector.close(); self.errors.close()

@pytest.mark.parametrize('sig', [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_idle_server_signal_exit(tmp_path, sig):
    pytest.importorskip('mcp')
    root, out = tmp_path/'media', tmp_path/'out'; root.mkdir(); out.mkdir()
    client = ExportStdio(root, out)
    try:
        client.proc.send_signal(sig); client.proc.wait(timeout=5)
        assert client.proc.returncode == 128+sig
        assert not list(out.iterdir()) and not client.children()
    finally: client.close()

def test_raw_stdout_export_lifecycle(tmp_path):
    pytest.importorskip('mcp')
    root, out = tmp_path/'media', tmp_path/'out'; out.mkdir()
    paths = export_media(root, duration=30)
    client = ExportStdio(root, out)
    try:
        pid = client.call('create_project', name='Wire lifecycle')['project_id']
        aid = client.call('import_media', project_id=pid, paths=[str(paths[0])])['assets'][0]['asset_id']
        def start(name, speed, resolution):
            client.call('set_timeline', project_id=pid, placement='sequential', clips=[
                dict(asset_id=aid,src_start_s=0,src_end_s=4.5 if speed == 1 else 30,speed=speed)])
            client.call('save_project', project_id=pid)
            return client.call('start_export', project_id=pid, output_name=name, resolution=resolution)['job_id']
        job = start('complete.mp4', 1, 'source')
        deadline = time.monotonic()+20
        while True:
            status = client.call('get_export_status', job_id=job)
            if status['state'] == 'SUCCEEDED': break
            assert status['state'] not in {'FAILED','CANCELLED'} and time.monotonic() < deadline, status
            time.sleep(0.02)
        assert status['progress_percent'] is not None and status['progress_percent'] > 0
        published = Path(status['output']['path']); original = published.read_bytes()
        job = start('cancelled.mp4', 0.25, '1920x1080')
        deadline = time.monotonic()+20
        while True:
            status = client.call('get_export_status', job_id=job)
            assert status['state'] == 'RENDERING' or status['state'] == 'PREPARING', status
            if (status['progress_percent'] or 0) > 0: break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        children = client.children(); assert children
        client.call('cancel_export', job_id=job)
        deadline = time.monotonic()+15
        while True:
            status = client.call('get_export_status', job_id=job)
            if status['state'] == 'CANCELLED': break
            assert status['state'] == 'CANCELLING' and time.monotonic() < deadline, status
            time.sleep(0.02)
        assert all(not Path(f'/proc/{p}').exists() for p in children)
        assert published.read_bytes() == original and not list(out.rglob('cancelled.mp4'))
        assert not list(out.rglob('.export-*'))
        client.proc.stdin.close(); client.proc.wait(timeout=15)
        assert client.proc.returncode == 0
    finally:
        client.close()  # Validate every remaining stdout frame, including EOF cleanup.

@pytest.mark.parametrize('mode', ['cancel', 'eof', 'sigterm', 'prepare_eof'])
def test_real_export_active_cancel_and_disconnect(tmp_path, monkeypatch, mode):
    pytest.importorskip('mcp')
    root, out = tmp_path/'media', tmp_path/'out'; out.mkdir()
    paths = export_media(root, duration=30)
    from test_agent_editing import tones
    music = tones(root/'background.wav')
    owned=tmp_path/'ass-tmp'; owned.mkdir()
    monkeypatch.setenv('TMPDIR',str(owned))
    client = ExportStdio(root, out, prepare=mode == 'prepare_eof')
    try:
        pid = client.call('create_project', name='Long render')['project_id']
        aid = client.call('import_media', project_id=pid, paths=[str(paths[0])])['assets'][0]['asset_id']
        # One 30 s input slowed to 120 s at 1080p avoids 200 simultaneous decoders.
        # Activity is proved by progress AND a live child, not assumed from length.
        client.call('set_timeline', project_id=pid, placement='sequential', clips=[
            dict(asset_id=aid,src_start_s=0,src_end_s=30,speed=0.25)])
        client.call('set_audio', project_id=pid, items=[dict(path=str(music))])
        client.call('set_audio_mix', project_id=pid, added_gain=0.5)
        from test_agent_subtitles import SRT, STYLE
        sub=root/'captions.srt'; sub.write_text(SRT)
        summary=client.call('set_subtitles',project_id=pid,tracks=[dict(path=str(sub),**STYLE)])
        client.call('set_crop',project_id=pid,clip_id=summary['clips'][0]['clip_id'],expected_revision=summary['revision'],
            crop=dict(x=0.5,y=0,width=0.5,height=1))
        client.call('save_project', project_id=pid)
        started = client.call('start_export', project_id=pid, output_name='long.mp4', resolution='1920x1080')
        child_pids = []
        deadline = time.monotonic()+20
        if mode != 'prepare_eof':
            while True:
                status = client.call('get_export_status', job_id=started['job_id'])
                assert status['state'] not in {'FAILED','CANCELLED','SUCCEEDED'}, status
                if (status['progress_percent'] or 0) > 0:
                    assert status['state'] == 'RENDERING', status
                    child_pids = client.children()
                    assert child_pids and all(Path(f'/proc/{p}').exists() for p in child_pids)
                    assert all(Path(f'/proc/{p}/comm').read_text().strip() == 'ffmpeg' for p in child_pids)
                    assert len(list(owned.glob('cove-subs-*')))==1
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
        else: assert started['state'] == 'PREPARING'
        began = time.monotonic()
        if mode == 'cancel':
            cancelled = client.call('cancel_export', job_id=started['job_id'])
            # A responsive child may already be reaped before the tool reply is sent.
            assert cancelled['state'] in {'CANCELLING', 'CANCELLED'} and cancelled['cancel_requested']
            while True:
                status = client.call('get_export_status', job_id=started['job_id'])
                if status['state'] != 'CANCELLING': break
                assert time.monotonic()-began < 15
                time.sleep(0.02)
            assert status['state'] == 'CANCELLED' and status['output'] is None and status['leftover'] is None
        elif mode == 'sigterm':
            client.proc.send_signal(signal.SIGTERM); client.proc.wait(timeout=15)
            assert client.proc.returncode == 128+signal.SIGTERM
        else:
            client.proc.stdin.close(); client.proc.wait(timeout=15)
            assert client.proc.returncode == 0
        assert time.monotonic()-began < 15
        assert all(not Path(f'/proc/{p}').exists() for p in child_pids), 'FFmpeg must be reaped'
        assert not list(out.rglob('long.mp4')) and not list(out.rglob('.export-*'))
        assert not list(owned.glob('cove-subs-*'))
    finally:
        client.close()

def test_stdio_audio_boundary_and_save_recovery(tmp_path):
    pytest.importorskip('mcp')
    from test_agent_editing import tones
    root, out = tmp_path/'media', tmp_path/'out'; out.mkdir()
    paths = export_media(root); music = tones(root/'music.wav')
    client = ExportStdio(root, out)
    try:
        pid = client.call('create_project', name='Audio boundaries')['project_id']
        aid = client.call('import_media', project_id=pid, paths=[str(paths[0])])['assets'][0]['asset_id']
        clips = [dict(asset_id=aid, src_start_s=0, src_end_s=2.25)]
        client.call('set_timeline', project_id=pid, placement='sequential', clips=clips)
        client.call('set_audio', project_id=pid, items=[dict(path=str(music))])
        client.call('save_project', project_id=pid)
        before = client.call('get_project', project_id=pid)
        result = client.request('tools/call', dict(name='cove_set_audio', arguments=dict(project_id=pid,
            items=[dict(path=str(music), offset_s=86399.9995, src_end_s=0.0001)])))
        assert result['isError'] and result['structuredContent']['error']['code']=='INVALID_TIMELINE'
        assert client.call('get_project', project_id=pid)==before
        valid = client.call('set_audio', project_id=pid, items=[dict(path=str(music), offset_s=86399.999, src_end_s=0.0001)])
        assert valid['audio'][0]['timeline_end_s']==86400
        summary = client.call('set_audio', project_id=pid, items=[dict(path=str(music), offset_s=5)])
        files = {p.name:p.read_bytes() for p in out.rglob('*.coveproj')}
        result = client.request('tools/call', dict(name='cove_save_project', arguments=dict(project_id=pid)))
        assert result['isError'] and result['structuredContent']['error']['code']=='INVALID_TIMELINE'
        assert 'cove_set_timeline' in result['structuredContent']['error']['message']
        assert client.call('get_project', project_id=pid)==summary
        assert files=={p.name:p.read_bytes() for p in out.rglob('*.coveproj')}
        client.call('set_timeline', project_id=pid, placement='sequential', clips=clips*3)
        client.call('save_project', project_id=pid)
        assert client.call('get_project', project_id=pid)['saved_is_current']
    finally:
        client.close()

def test_sdk_stdio_background_audio_snapshot(tmp_path):
    pytest.importorskip('mcp')
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from test_agent_editing import tones, pcm, amplitude
    root, out = tmp_path/'media', tmp_path/'out'; out.mkdir()
    paths = export_media(root); music = tones(root/'music.wav')
    release, entered = tmp_path/'release', tmp_path/'entered'
    code = ('from pathlib import Path\nimport time\n'
        'from cove_video_editor import agent_exports as ae\n'
        'from cove_video_editor.mcp_server import main\n'
        'def hold(stage,job):\n'
        ' if stage=="before_run":\n'
        f'  Path({str(entered)!r}).touch()\n'
        f'  while not Path({str(release)!r}).exists() and not job.cancel_requested: time.sleep(0.01)\n'
        'ae._hook=hold\nraise SystemExit(main())')
    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-c', code,
            '--media-root', str(root), '--output-root', str(out)], env=dict(os.environ))
        with anyio.fail_after(30):
            async with stdio_client(params) as streams, ClientSession(*streams, read_timeout_seconds=10) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                assert len(tools) == 15
                schemas = {t.name:t.input_schema for t in tools}
                fields = schemas['cove_set_audio']['$defs']['AudioItem']['properties']
                assert all(fields[n].get('description') for n in fields)
                async def call(name, /, **args):
                    result = await client.call_tool('cove_'+name, args)
                    assert not result.is_error, result
                    return result.structured_content
                await call('inspect_media', paths=[str(paths[0])])
                pid = (await call('create_project', name='Audio snapshot'))['project_id']
                aid = (await call('import_media', project_id=pid, paths=[str(paths[0])]))['assets'][0]['asset_id']
                await call('set_timeline', project_id=pid, placement='sequential', clips=[dict(asset_id=aid,src_start_s=0,src_end_s=2.25)])
                await call('set_audio', project_id=pid, items=[dict(path=str(music),src_start_s=2,offset_s=0.5,volume=0.8,lane=7)])
                await call('set_audio_mix', project_id=pid, added_gain=0.5, replace_original=True)
                summary = await call('get_project', project_id=pid)
                assert summary['audio'][0]['lane'] == 7 and summary['mix']['added_gain'] == 0.5
                for bad in ({'items':[{'path':str(music),'unknown':True}]}, {'items':'[]'}, {'items':[{'path':str(music),'lane':True}]}):
                    error = await client.call_tool('cove_set_audio', dict(project_id=pid)|bad)
                    assert error.is_error and error.structured_content['error']['code'] == 'INVALID_ARGUMENT'
                for bad in ({}, {'added_gain':0.123}, {'replace_original':1}):
                    error = await client.call_tool('cove_set_audio_mix', dict(project_id=pid)|bad)
                    assert error.is_error and error.structured_content['error']['code'] == 'INVALID_ARGUMENT'
                assert await call('get_project', project_id=pid) == summary
                saved = await call('save_project', project_id=pid)
                started = await call('start_export', project_id=pid, output_name='snapshot.mp4')
                try:
                    while not entered.exists(): await anyio.sleep(0.01)
                    await call('set_audio', project_id=pid, items=[])
                    await call('set_audio_mix', project_id=pid, added_gain=3, replace_original=False)
                finally: release.touch()
                while True:
                    status = await call('get_export_status', job_id=started['job_id'])
                    if status['state'] in {'SUCCEEDED','FAILED','CANCELLED'}: break
                    await anyio.sleep(0.01)
                assert status['state'] == 'SUCCEEDED', status
                assert status['saved_revision'] == saved['revision']
                samples = pcm(status['output']['path'])
                assert 1400 < amplitude(samples,880) < 1900
                assert amplitude(samples,440) < 30 and amplitude(samples,660) < 30
                assert amplitude(samples,880,0.1,0.3) < 20
    anyio.run(run)

@pytest.mark.parametrize('transport',['memory','stdio'])
def test_sdk_subtitle_crop_workflow(tmp_path,monkeypatch,transport):
    pytest.importorskip('mcp')
    import anyio
    import mcp.client.stdio as stdio
    from contextlib import asynccontextmanager
    from mcp.client.session import ClientSession
    from mcp.client import Client
    from mcp.client._memory import InMemoryTransport
    from cove_video_editor.agent_session import AgentSession
    from cove_video_editor.agent_paths import validate_roots
    from cove_video_editor.mcp_server import build_server
    from cove_video_editor.project_io import load_project
    from test_agent_crop import quadrants, RIGHT, assert_quadrants
    from test_agent_subtitles import SRT, STYLE, frame, red_pixels
    from test_agent_editing import tones, pcm, amplitude
    root,out,owned=tmp_path/'media',tmp_path/'out',tmp_path/'ass-tmp'
    root.mkdir(); out.mkdir(); owned.mkdir()
    path=quadrants(root/'quad.mp4'); music=tones(root/'music.wav'); sub=root/'text.srt'; sub.write_text(SRT)
    processes=[]; original=stdio._create_platform_compatible_process
    async def capture(*args,**kwargs):
        process=await original(*args,**kwargs); processes.append(process); return process
    monkeypatch.setattr(stdio,'_create_platform_compatible_process',capture)
    @asynccontextmanager
    async def connect():
        if transport=='memory':
            session=AgentSession(validate_roots([str(root)],str(out)))
            try:
                async with Client(InMemoryTransport(build_server(session)),mode='legacy') as client: yield client.session
            finally: session.close()
        else:
            params=stdio.StdioServerParameters(command=sys.executable,args=['-m','cove_video_editor.mcp_server',
                '--media-root',str(root),'--output-root',str(out)],env=dict(os.environ,TMPDIR=str(owned)))
            async with stdio.stdio_client(params) as streams, ClientSession(*streams,read_timeout_seconds=10) as client:
                await client.initialize(); yield client
    async def protocol():
        with anyio.fail_after(45):
            async with connect() as client:
                tools=(await client.list_tools()).tools; assert len(tools)==15
                schemas={t.name:t.input_schema for t in tools}
                active=schemas['cove_set_subtitles']['$defs']['SubtitleTrackInput']['properties']['active']
                assert active['default'] is True and 'defaults to true' in active['description']
                fields=schemas['cove_set_subtitles']['$defs']['SubtitleTrackInput']['properties']
                assert all(fields[k].get('description') for k in ('font_family','font_size','primary_color','outline_color','outline','position','offset_ms'))
                assert 'effective (cropped)' in schemas['cove_start_export']['properties']['resolution']['description']
                assert 'TOP-LEFT' in schemas['cove_set_crop']['properties']['crop']['description']
                assert {'crop','expected_revision','clip_id'} <= set(schemas['cove_set_crop']['required'])
                async def call(name,/,**args):
                    reply=await client.call_tool('cove_'+name,args); assert not reply.is_error,reply
                    return reply.structured_content
                pid=(await call('create_project',name='Subtitles and crop'))['project_id']
                aid=(await call('import_media',project_id=pid,paths=[str(path)]))['assets'][0]['asset_id']
                await call('set_timeline',project_id=pid,placement='sequential',clips=[dict(asset_id=aid,src_start_s=0,src_end_s=3)])
                await call('set_audio',project_id=pid,items=[dict(path=str(music))])
                summary=await call('set_subtitles',project_id=pid,tracks=[dict(path=str(sub),**STYLE)])
                cid=summary['clips'][0]['clip_id']
                summary=await call('set_crop',project_id=pid,clip_id=cid,expected_revision=summary['revision'],crop=RIGHT)
                assert summary['clips'][0]['crop_pixels']==dict(x=160,y=0,width=160,height=240)
                assert summary['subtitles'][0]['primary_color']=='#FF0000' and summary['subtitles'][0]['cue_count']==1
                assert await call('get_project',project_id=pid)==summary
                bads=[('set_subtitles',dict(tracks=[dict(path=str(sub),font_family='Arial\n[Events]')])),
                    ('set_subtitles',dict(tracks=[dict(path=str(sub),font_size=True)])),
                    ('set_subtitles',dict(tracks=[dict(path=str(sub),active=1)])),
                    ('set_subtitles',dict(tracks=[dict(path=str(sub),extra=1)])),
                    ('set_crop',dict(clip_id=cid,expected_revision=summary['revision'],crop=dict(x=0,y=0,width=2,height=1))),
                    ('set_crop',dict(clip_id=cid,expected_revision=0,crop=RIGHT)),
                    ('set_crop',dict(clip_id=cid,expected_revision=summary['revision'])),
                    ('set_crop',dict(clip_id=cid,expected_revision=True,crop=RIGHT))]
                for name,args in bads:
                    reply=await client.call_tool('cove_'+name,dict(project_id=pid)|args); assert reply.is_error
                    assert await call('get_project',project_id=pid)==summary
                saved=await call('save_project',project_id=pid); loaded=load_project(Path(saved['path']))
                assert loaded.clips[0].crop_rect==(0.5,0,0.5,1) and loaded.subtitles[0].font_size==48 and loaded.added_audios
                async def export(name):
                    await call('save_project',project_id=pid)
                    started=await call('start_export',project_id=pid,output_name=name)
                    while True:
                        status=await call('get_export_status',job_id=started['job_id'])
                        if status['state'] in {'SUCCEEDED','FAILED','CANCELLED'}: break
                        await anyio.sleep(0.01)
                    assert status['state']=='SUCCEEDED',status
                    return status['output']
                burned=await export('burned.mp4'); assert (burned['width'],burned['height'])==(160,240)
                assert_quadrants(burned,right=True); assert red_pixels(frame(burned['path'],1.1),160,240)>20
                assert amplitude(pcm(burned['path']),660)>1000
                summary=await call('set_crop',project_id=pid,clip_id=cid,expected_revision=summary['revision'],crop=None)
                full=await export('full.mp4'); assert (full['width'],full['height'])==(320,240); assert_quadrants(full)
                await call('set_subtitles',project_id=pid,tracks=[dict(path=str(sub),active=False,**STYLE)])
                disabled=await export('disabled.mp4'); assert frame(disabled['path'],1.1)!=frame(full['path'],1.1)
                await call('set_subtitles',project_id=pid,tracks=[dict(path=str(sub),**STYLE)])
                await call('save_project',project_id=pid); sub.unlink(); sub.write_text(SRT.replace('RED','NEW'))
                reply=await client.call_tool('cove_start_export',dict(project_id=pid,output_name='changed.mp4'))
                assert reply.is_error and reply.structured_content['error']['code']=='ASSET_CHANGED'
                await call('set_subtitles',project_id=pid,tracks=[dict(path=str(sub),**STYLE)])
                await call('save_project',project_id=pid); path.unlink()
                reply=await client.call_tool('cove_start_export',dict(project_id=pid,output_name='missing.mp4'))
                assert reply.is_error and reply.structured_content['error']['code']=='ASSET_CHANGED'
    async def run():
        async def heartbeat():
            while True: await anyio.sleep(0.01)
        async with anyio.create_task_group() as group:
            group.start_soon(heartbeat)
            try: await protocol()
            finally: group.cancel_scope.cancel()
    anyio.run(run)
    assert all(p.returncode==0 for p in processes)
    if transport=='stdio': assert len(processes)==1
    assert not list(owned.glob('cove-subs-*'))
