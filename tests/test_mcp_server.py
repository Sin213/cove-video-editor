from __future__ import annotations
import json, os, subprocess, sys, pytest
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
    for flag in ("--media-root", "--output-root"):
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
@pytest.mark.parametrize("roots", [(), ("--media-root",), ("--output-root",), ("--media-root", "--output-root")])
def test_startup_permission_notice(tmp_path, monkeypatch, capsys, roots):
    from cove_video_editor import mcp_server as module
    class Server:
        def run(self, **kwargs):
            pass
    monkeypatch.setattr(module, "build_server", lambda session: Server())
    assert module.main([arg for flag in roots for arg in (flag, str(tmp_path))]) == 0
    captured = capsys.readouterr()
    assert not captured.out and captured.err.count("Notice:") == (len(roots) < 2)
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
                    "import_media", "set_timeline", "get_project", "save_project")}
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
                assert caps["export"]["available"] is False and not list(out.iterdir())
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
                saved = await call("save_project", project_id=pid)
                state = load_project(Path(saved["path"]))
                assert (state.clips[0].src_start, state.clips[0].src_end, state.clips[0].speed,
                        state.clips[0].audio_volume, state.clips[0].muted) == (0.25, 2.5, 2, 0.4, True)
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
