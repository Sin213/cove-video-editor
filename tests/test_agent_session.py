from __future__ import annotations
import os, pytest
from pathlib import Path
from cove_video_editor import agent_paths as ap, agent_session as mod, project_io as io
from test_agent_paths import tree, video, rejected
@pytest.fixture
def editing(tree):
    session = mod.AgentSession(tree[2])
    project = session.create_project("My film")
    p = video(tree[0] / "clip.mp4", True)
    imported = session.import_media(project["project_id"], [str(p)])
    yield session, project["project_id"], imported["assets"][0]["asset_id"], p
    if hasattr(session, "close"):
        session.close()
def clip(asset, **kw):
    return dict(asset_id=asset, src_start_s=0.25, src_end_s=2.5, **kw)
@pytest.mark.parametrize("bad,code", [({"asset_id": "absent"}, "UNKNOWN_ASSET"),
    ({"src_end_s": 4}, "INVALID_TIMELINE"), ({"src_start_s": 2.8}, "INVALID_TIMELINE"),
    ({"src_end_s": 0}, "INVALID_TIMELINE"), ({"src_start_s": -1}, "INVALID_TIMELINE"),
    ({"speed": 0.1}, "INVALID_TIMELINE"), ({"volume": 3}, "INVALID_TIMELINE"),
    ({"speed": True}, "INVALID_ARGUMENT"), ({"volume": "1"}, "INVALID_ARGUMENT"),
    ({"src_end_s": float("nan")}, "INVALID_ARGUMENT"), ({"speed": float("inf")}, "INVALID_ARGUMENT"),
    ({"extra": 1}, "INVALID_ARGUMENT"), ({"timeline_start_s": 0}, "INVALID_TIMELINE")])
def test_atomic_invalid_timeline(editing, bad, code):
    s, pid, aid, _ = editing
    s.set_timeline(pid, "sequential", [clip(aid)])
    before = s.get_project(pid)
    invalid = clip(aid)
    invalid.update(bad)
    with rejected(code) as exc:
        s.set_timeline(pid, "sequential", [clip(aid), invalid])
    assert "clips[1]" in exc.value.message and next(iter(bad)) in exc.value.message
    assert s.get_project(pid) == before
def test_unknown_project(editing):
    s, _, _, _ = editing
    with rejected("UNKNOWN_PROJECT") as exc:
        s.get_project("unknown")
    assert exc.value.message == "Unknown project_id. The server may have restarted; unsaved projects are lost - call cove_create_project."
def test_all_or_nothing_import(editing, tree):
    s, pid, _, _ = editing
    p = video(tree[0] / "new.mp4")
    before = s.get_project(pid)
    with rejected("PATH_NOT_AUTHORIZED") as exc:
        s.import_media(pid, [str(p), "/outside.mp4"])
    assert "paths[1]" in exc.value.message and "See cove_get_capabilities media_roots." in exc.value.message
    assert s.get_project(pid) == before
def test_save_roundtrip_immutability(editing, tmp_path):
    s, pid, aid, p = editing
    assert s.import_media(pid, [str(p), str(p)])["added"] == 0
    assert s.get_project(pid)["revision"] == 1
    s.set_timeline(pid, "sequential", [clip(aid, speed=2, volume=0.4, mute=True), clip(aid)])
    saved = s.save_project(pid)
    path = Path(saved["path"])
    original = path.read_bytes()
    loaded = io.load_project(path)
    assert len(loaded.clips) == 2 and loaded.clips[1].timeline_start == 1.125
    assert (loaded.clips[0].src_start, loaded.clips[0].src_end, loaded.clips[0].speed,
            loaded.clips[0].audio_volume, loaded.clips[0].muted) == (0.25, 2.5, 2, 0.4, True)
    other = tmp_path / "manual.coveproj"
    io.write_project(other, io.serialize_project(loaded))
    assert other.read_bytes() == original
    assert s.save_project(pid) == saved and s.get_project(pid)["saved_is_current"]
    s.set_timeline(pid, "sequential", [clip(aid, volume=0.7)])
    assert not s.get_project(pid)["saved_is_current"]
    new = s.save_project(pid)
    assert new["path"] != saved["path"] and path.read_bytes() == original
    Path(new["path"]).write_bytes(b"external")
    with rejected("PROJECT_CHANGED_ON_DISK"):
        s.save_project(pid)
    assert Path(new["path"]).read_bytes() == b"external"
def test_asset_changed_and_output_swap(editing, tmp_path):
    s, pid, aid, p = editing
    s.set_timeline(pid, "sequential", [clip(aid)])
    p.write_bytes(b"changed")
    with rejected("ASSET_CHANGED") as exc:
        s.save_project(pid)
    assert aid in exc.value.message
    assert not list(tmp_path.rglob("*.coveproj"))
def test_overlap_gaps_and_empty(editing):
    s, pid, aid, _ = editing
    with rejected("EMPTY_TIMELINE"):
        s.save_project(pid)
    s.set_timeline(pid, "explicit", [clip(aid, timeline_start_s=5), clip(aid, timeline_start_s=0)])
    assert s.get_project(pid)["clips"][0]["timeline_start_s"] == 0
    assert s.get_project(pid)["warnings"]
    before = s.get_project(pid)
    for clips in ([clip(aid, timeline_start_s=0), clip(aid, timeline_start_s=1)],
                  [clip(aid)], [clip(aid, timeline_start_s=86400)]):
        with rejected("INVALID_TIMELINE"):
            s.set_timeline(pid, "explicit", clips)
        assert s.get_project(pid) == before
    s.set_timeline(pid, "explicit", [clip(aid, timeline_start_s=0), clip(aid, timeline_start_s=2.25)])
    assert not s.get_project(pid)["warnings"]
    s.set_timeline(pid, "sequential", [])
    assert not s.get_project(pid)["clips"]
def test_limits_names_no_writes(tree, editing, monkeypatch):
    s, pid, aid, p = editing
    before = sorted(tree[1].rglob("*"))
    assert s.inspect_media([str(p)])["media"][0]["kind"] == "video"
    assert sorted(tree[1].rglob("*")) == before
    for paths in ([], [str(p)] * 51):
        with rejected("LIMIT_EXCEEDED"):
            s.import_media(pid, paths)
    with rejected("LIMIT_EXCEEDED"):
        s.set_timeline(pid, "sequential", [clip(aid)] * 201)
    for name in ("", "x" * 81, "bad\nname", "bad\u200bname"):
        with rejected("INVALID_ARGUMENT"):
            s.create_project(name)
    for _ in range(19):
        s.create_project("another")
    with rejected("LIMIT_EXCEEDED"):
        s.create_project("too many")
    monkeypatch.setattr(ap, "MAX_ASSETS", 1)
    other = video(tree[0] / "other.mp4")
    with rejected("LIMIT_EXCEEDED"):
        s.import_media(pid, [str(other)])
def test_probe_budget_and_audio_only(tree, monkeypatch):
    s = mod.AgentSession(tree[2])
    pid = s.create_project("budget")["project_id"]
    p = tree[0] / "audio.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=3",
                    "-y", str(p)], check=True, capture_output=True, timeout=20)
    with rejected("UNSUPPORTED_MEDIA"):
        s.import_media(pid, [str(p)])
    assert s.inspect_media([str(p)])["media"][0]["error"]["code"] == "UNSUPPORTED_MEDIA"
    monkeypatch.setattr(ap, "PROBE_BUDGET", 0)
    with rejected("PROBE_BUDGET_EXCEEDED"):
        s.import_media(pid, [str(p)])
    s.close()
def test_saved_project_subdir_symlink(editing, tmp_path):
    s, pid, aid, _ = editing
    s.set_timeline(pid, "sequential", [clip(aid)])
    folder = next(Path(s.policy.output_root).iterdir())
    folder.rename(tmp_path / "moved")
    folder.symlink_to(tmp_path / "moved", target_is_directory=True)
    with rejected("OUTPUT_UNSAFE"):
        s.save_project(pid)
def test_budget_warnings_and_asset_kind(editing, monkeypatch):
    s, pid, aid, p = editing
    s.set_timeline(pid, "explicit", [clip(aid, timeline_start_s=i * 4 + 1) for i in range(200)])
    assert len(s.get_project(pid)["warnings"]) == 50
    assert s.capabilities()["limits"] == dict(paths=50, path_chars=4096, name_chars=80, projects=20,
        assets=200, clips=200, timeline_s=86400, probe_timeout_s=10, probe_budget_s=60,
        probe_output_bytes=65536, warnings=50)
    asset = s.projects[pid].state.assets[0]
    asset.kind = "audio"
    with rejected("ASSET_NOT_VIDEO"):
        s.set_timeline(pid, "sequential", [clip(aid)])
    asset.kind = "video"
    monkeypatch.setattr(ap, "PROBE_BUDGET", 0)
    batch = s.inspect_media([str(p)] * 3)["media"]
    assert all(item["error"]["code"] == "PROBE_BUDGET_EXCEEDED" for item in batch)
@pytest.mark.parametrize("valid", [True, False])
def test_reimport_changed_inode_atomic(editing, valid):
    s, pid, _, p = editing
    before, inode = s.get_project(pid), p.stat().st_ino
    other = video(p.parent / "new.mp4")
    video(p) if valid else p.write_bytes(b"changed")
    assert p.stat().st_ino == inode
    with rejected("ASSET_CHANGED"):
        s.import_media(pid, [str(other), str(p)])
    assert s.get_project(pid) == before
def test_actionable_errors_and_audio_warning(editing):
    s, pid, aid, p = editing
    with rejected("INVALID_TIMELINE") as exc:
        s.set_timeline(pid, "sequential", [clip(aid, volume=1), dict(asset_id=aid, src_start_s=0, src_end_s=40)])
    assert "clips[1]: src_end_s 40 exceeds asset duration 3" in exc.value.message
    with rejected("INVALID_TIMELINE") as exc:
        s.set_timeline(pid, "explicit", [clip(aid, timeline_start_s=0), clip(aid, timeline_start_s=1)])
    assert "clips[1] starts at 1 s but clips[0] ends at 2.25 s" in exc.value.message
    assert not s.set_timeline(pid, "sequential", [clip(aid, volume=0.5)])["warnings"]
    silent = video(p.parent / "silent.mp4")
    silent_id = s.import_media(pid, [str(silent)])["assets"][0]["asset_id"]
    assert s.set_timeline(pid, "sequential", [clip(silent_id, mute=True)])["warnings"] == ["clips[0]: source has no audio; volume/mute have no effect."]
    assert not s.set_timeline(pid, "sequential", [clip(silent_id)])["warnings"]
    assert len(s.set_timeline(pid, "explicit", [clip(silent_id, volume=0.5, timeline_start_s=i * 4 + 1) for i in range(100)])["warnings"]) == 50
    for path in ("/outside.mp4", "/nonexistent-outside.mp4"):
        error = s.inspect_media([path])["media"][0]["error"]
        assert error["message"] == f"paths[0] {path!r}: Path is outside authorized media roots. See cove_get_capabilities media_roots."
def test_long_source_short_timeline(editing):
    s, pid, aid, _ = editing; s.projects[pid].state.assets[0].duration = 100000
    item = dict(asset_id=aid, src_start_s=90000, src_end_s=90001)
    assert s.set_timeline(pid, "sequential", [item])["duration_s"] == 1
    loaded = io.load_project(Path(s.save_project(pid)["path"]))
    assert (loaded.clips[0].src_start, loaded.clips[0].src_end) == (90000, 90001)
    with rejected("INVALID_TIMELINE"):
        s.set_timeline(pid, "explicit", [item | dict(timeline_start_s=86400)])
def test_concurrent_import_and_edit(editing):
    from concurrent.futures import ThreadPoolExecutor
    s, _, _, p = editing; pid = s.create_project("Concurrent")["project_id"]
    def edit():
        revisions = [s.set_timeline(pid, "sequential", [])["revision"] for _ in range(10)]
        assert revisions == sorted(revisions) and not s.get_project(pid)["clips"]
    with ThreadPoolExecutor(3) as pool:
        jobs = [pool.submit(s.import_media, pid, [str(p)]) for _ in range(2)] + [pool.submit(edit)]
        assert jobs[0].result(timeout=10)["assets"][0]["asset_id"] == jobs[1].result(timeout=10)["assets"][0]["asset_id"]; jobs[2].result(timeout=10)
    assert len(s.get_project(pid)["assets"]) == 1 and s.get_project(pid)["revision"] == 11
def test_elapsed_probe_budget(editing, monkeypatch):
    s, _, _, p = editing; clock = [0.0]; original = ap.probe_fd
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    def slow(fd, **kwargs):
        result = original(fd, **kwargs); clock[0] += 61; return result
    monkeypatch.setattr(ap, "probe_fd", slow)
    results = s.inspect_media([str(p)] * 3)["media"]
    assert results[0]["kind"] == "video" and all(r["error"]["code"] == "PROBE_BUDGET_EXCEEDED" for r in results[1:])
