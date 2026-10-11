"""Synthetic boundary tests; seams force races without scheduler timing."""
from __future__ import annotations
import errno, hashlib, json, os, subprocess, sys, time, pytest
from pathlib import Path
from cove_video_editor import agent_paths as ap
def rejected(code):
    return pytest.raises(ap.AgentError, match=code)
def video(path, audio=False, color="red", size="320x240"):
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
           f"color=c={color}:s={size}:r=25:d=3"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=duration=3", "-shortest"]
    subprocess.run(cmd + ["-c:v", "mpeg4", "-y", str(path)], check=True,
                   capture_output=True, timeout=20)
    return path
@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "media"
    out = tmp_path / "out"
    root.mkdir()
    out.mkdir()
    policy = ap.validate_roots([str(root)], str(out))
    yield root, out, policy
    if hasattr(policy, "close"):
        policy.close()
@pytest.mark.parametrize("path", ["a.mp4", "../a.mp4", "http://x/a.mp4",
    "file:///tmp/a.mp4", "//host/a.mp4", "\\\\host\\a.mp4", "C:a.mp4",
    "C:\\a.mp4", "/tmp/a.mp4:ads", "/tmp/a\0.mp4", "\\\\?\\C:\\a", "/" + "x" * 4096])
def test_unsafe_text(path):
    with rejected("INVALID_ARGUMENT"):
        ap.reject_unsafe_path_text(path)
def test_root_enforcement(tree, tmp_path):
    root, _, policy = tree
    outside = tmp_path / "media-evil"
    outside.mkdir()
    p = outside / "a.mp4"
    p.write_bytes(b"x")
    for path in (p, outside / "missing.mp4", root / ".." / "media-evil" / "a.mp4"):
        with rejected("PATH_NOT_AUTHORIZED"), ap.open_authorized_media(policy, str(path)):
            pass
def test_symlink_outside(tree, tmp_path):
    root, _, policy = tree
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    link = root / "link.mp4"
    link.symlink_to(outside)
    with rejected("PATH_NOT_AUTHORIZED"), ap.open_authorized_media(policy, str(link)):
        pass
def test_regular_and_inside_symlink(tree):
    root, _, policy = tree
    p = video(root / "sp ace-影片.mp4", True)
    link = root / "link.mp4"
    link.symlink_to(p)
    with ap.open_authorized_media(policy, str(link)) as (fd, canonical, identity):
        assert canonical == str(p)
        assert identity == ap.file_identity(os.fstat(fd))
        info = ap.probe_fd(fd)
    assert (info["width"], info["height"], info["has_audio"]) == (320, 240, True)
    assert 2.9 < info["duration_s"] < 3.1
    assert set(info) == {"kind", "duration_s", "width", "height", "fps", "has_audio",
                         "video_codec", "audio_codec", "container"}
def test_special_files(tree):
    root, _, policy = tree
    os.mkfifo(root / "fifo.mp4")
    (root / "dir.mp4").mkdir()
    (root / "device.mp4").symlink_to("/dev/null")
    for name, code in [("fifo.mp4", "NOT_A_REGULAR_FILE"), ("dir.mp4", "NOT_A_REGULAR_FILE"),
                       ("device.mp4", "PATH_NOT_AUTHORIZED"), ("missing.mp4", "FILE_NOT_FOUND")]:
        with rejected(code), ap.open_authorized_media(policy, str(root / name)):
            pass
@pytest.mark.parametrize("body", ["#EXTM3U\n#EXTINF:3,\n{target}\n",
                                  "ffconcat version 1.0\nfile '{target}'\n"])
@pytest.mark.parametrize("target", ["../outside.mp4", "/tmp/outside.mp4", "http://127.0.0.1:9/x"])
@pytest.mark.parametrize("suffix", [".mp4", ".mkv"])
def test_adversarial_demuxers(tree, body, target, suffix):
    root, _, policy = tree
    outside = video(root.parent / "outside.mp4", True)
    if target == "/tmp/outside.mp4":
        target = str(outside)
    p = root / ("playlist" + suffix)
    p.write_text(body.format(target=target))
    with ap.open_authorized_media(policy, str(p)) as (fd, _, _), rejected("PROBE_FAILED"):
        ap.probe_fd(fd)
    assert outside.exists()
def test_extension(tree):
    root, _, policy = tree
    p = root / "playlist.m3u8"
    p.write_text("#EXTM3U")
    with rejected("UNSUPPORTED_EXTENSION"), ap.open_authorized_media(policy, str(p)):
        pass
@pytest.mark.parametrize("phase", ["before_open", "after_open"])
def test_parent_swap(tree, tmp_path, monkeypatch, phase):
    root, _, policy = tree
    parent = root / "child"
    parent.mkdir()
    (parent / "a.mp4").write_bytes(b"x")
    moved = tmp_path / "moved"
    def swap(stage):
        if stage == phase:
            parent.rename(moved)
            if phase == "before_open":
                parent.symlink_to(moved, target_is_directory=True)
    monkeypatch.setattr(ap, "_media_hook", swap)
    with rejected("PATH_NOT_AUTHORIZED"), ap.open_authorized_media(policy, str(parent / "a.mp4")):
        pass
def test_symlink_swap(tree, tmp_path, monkeypatch):
    root, _, policy = tree
    p = root / "a.mp4"
    p.write_bytes(b"x")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    def swap(stage):
        if stage == "before_open":
            p.unlink()
            p.symlink_to(outside)
    monkeypatch.setattr(ap, "_media_hook", swap)
    with rejected("PATH_NOT_AUTHORIZED"), ap.open_authorized_media(policy, str(p)):
        pass
def test_content_swap_uses_fd(tree, monkeypatch):
    root, _, policy = tree
    p = video(root / "a.mp4")
    replacement = video(root / "b.mp4", True, size="640x360")
    def swap(stage):
        if stage == "after_open":
            os.replace(replacement, p)
    monkeypatch.setattr(ap, "_media_hook", swap)
    with ap.open_authorized_media(policy, str(p)) as (fd, _, _):
        info = ap.probe_fd(fd)
        assert info["has_audio"] is False and (info["width"], info["height"]) == (320, 240)
def fake_probe(tmp_path, monkeypatch, body):
    script = tmp_path / "fake-probe"
    script.write_text(f"#!{sys.executable}\n" + body)
    script.chmod(0o700)
    monkeypatch.setattr(ap, "require_ffprobe", lambda: str(script))
    return script

@pytest.mark.parametrize('suffix', ['.mp3', '.m4a', '.aac', '.wav', '.ogg', '.opus', '.flac'])
def test_audio_formats_probe_held_descriptor(tree, suffix):
    root, _, policy = tree; path = root / ('tone'+suffix)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=1',
        '-y', str(path)], check=True, capture_output=True, timeout=15)
    with ap.open_authorized_media(policy, str(path), kind='audio') as (fd, _, _):
        result = ap.probe_fd(fd, kind='audio')
        assert result['kind'] == 'audio' and 0.9 < result['duration_s'] < 1.2
        assert result['audio_codec'] and result['container']

def test_audio_probe_policy_and_cover_art(tree, tmp_path, monkeypatch):
    doc = dict(streams=[dict(codec_type='audio', codec_name='mp3', nb_read_frames='10'),
        dict(codec_type='video', codec_name='mjpeg', disposition=dict(attached_pic=1))],
        format=dict(duration='3', format_name='mp3'))
    record = tmp_path/'audio-argv.json'
    fake_probe(tmp_path, monkeypatch, 'import sys,os,json\n'
        + f'open({str(record)!r},"w").write(json.dumps([sys.argv,dict(os.environ),os.getcwd(),os.getpid(),os.getpgrp()]))\n'
        + f'print({json.dumps(doc)!r})\n')
    path = tree[0]/'cover.mp3'; path.touch()
    with ap.open_authorized_media(tree[2], str(path), kind='audio') as (fd, _, _):
        assert ap.probe_fd(fd, kind='audio')['duration_s'] == 3
    args, env, cwd, pid, group = json.loads(record.read_text())
    assert args[args.index('-i')+1].startswith('/dev/fd/')
    assert args[args.index('-protocol_whitelist')+1] == 'file'
    assert args[args.index('-format_whitelist')+1] == 'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mp3,wav,flac,ogg,aac'
    assert set(env) <= {'PATH', 'LC_CTYPE'} and cwd == '/' and pid == group
    assert '-count_frames' in args and '-read_intervals' in args

def test_real_audio_cover_art_and_video_rejection(tree):
    root, _, policy = tree
    cover = root/'cover.png'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32',
        '-frames:v', '1', '-threads', '1', '-y', str(cover)], check=True, capture_output=True, timeout=15)
    path = root/'covered.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=550:duration=3',
        '-i', str(cover), '-map', '0:a', '-map', '1:v', '-c:a', 'libmp3lame', '-c:v', 'mjpeg',
        '-disposition:v', 'attached_pic', '-id3v2_version', '3', '-y', str(path)],
        check=True, capture_output=True, timeout=15)
    metadata = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json',
        str(path)], check=True, capture_output=True, timeout=15).stdout)
    assert any(s['codec_type']=='video' and s['disposition']['attached_pic']==1 for s in metadata['streams'])
    with ap.open_authorized_media(policy, str(path), kind='audio') as (fd, _, _):
        result = ap.probe_fd(fd, kind='audio')
    assert result['kind'] == 'audio' and result['audio_codec'] == 'mp3'
    assert 3 <= result['duration_s'] < 3.2
    movie = video(root/'movie.m4a', audio=True)
    with ap.open_authorized_media(policy, str(movie), kind='audio') as (fd, _, _):
        with rejected('UNSUPPORTED_MEDIA'): ap.probe_fd(fd, kind='audio')

@pytest.mark.parametrize('change', ['zero', 'huge', 'nan', 'bad_codec', 'bad_format', 'no_frames', 'no_audio', 'video'])
def test_invalid_audio_probe_metadata(tree, tmp_path, monkeypatch, change):
    audio = dict(codec_type='audio', codec_name='pcm_s16le', nb_read_frames='20')
    doc = dict(streams=[audio], format=dict(duration='3', format_name='wav'))
    if change in {'zero', 'huge', 'nan'}: doc['format']['duration'] = {'zero': '0', 'huge': '86401', 'nan': 'nan'}[change]
    elif change == 'bad_codec': audio['codec_name'] = 'evil:codec'
    elif change == 'bad_format': doc['format']['format_name'] = 'evil:format'
    elif change == 'no_frames': audio['nb_read_frames'] = '0'
    elif change == 'no_audio': doc['streams'] = []
    elif change == 'video': doc['streams'].append(dict(codec_type='video', codec_name='h264', disposition=dict(attached_pic=0)))
    fake_probe(tmp_path, monkeypatch, f'print({json.dumps(doc)!r})\n')
    path = tree[0]/'bad.wav'; path.touch()
    with ap.open_authorized_media(tree[2], str(path), kind='audio') as (fd, _, _), pytest.raises(ap.AgentError):
        ap.probe_fd(fd, kind='audio')

def test_audio_symlink_swap_and_reap(tree, tmp_path, monkeypatch):
    path = tree[0]/'race.wav'; path.touch(); outside = tmp_path/'outside.wav'; outside.touch()
    def swap(stage):
        if stage == 'before_open': path.unlink(); path.symlink_to(outside)
    monkeypatch.setattr(ap, '_media_hook', swap)
    before = len(os.listdir('/proc/self/fd'))
    with rejected('PATH_NOT_AUTHORIZED'), ap.open_authorized_media(tree[2], str(path), kind='audio'): pass
    assert len(os.listdir('/proc/self/fd')) == before
def test_launch_policy(tree, tmp_path, monkeypatch):
    record = tmp_path / "argv.json"
    fake_probe(tmp_path, monkeypatch, "import sys,os,json\n"
        f"open({str(record)!r},'w').write(json.dumps([sys.argv,dict(os.environ),os.getcwd(),os.getpid(),os.getpgrp()]))\n"
        "print('{}')\n")
    root, _, policy = tree
    p = root / "a.mp4"
    p.touch()
    with ap.open_authorized_media(policy, str(p)) as (fd, _, _), rejected("UNSUPPORTED_MEDIA"):
        ap.probe_fd(fd)
    args, env, cwd, pid, group = json.loads(record.read_text())
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args[args.index("-format_whitelist") + 1] == "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi"
    assert args[args.index("-i") + 1].startswith("/dev/fd/")
    assert "tags" not in args[args.index("-show_entries") + 1]
    assert set(env) <= {"PATH", "LC_CTYPE"}  # Python may inject its locale coercion variable.
    assert cwd == "/" and pid == group
def test_timeout_reaps(tree, tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    fake_probe(tmp_path, monkeypatch, f"import os,time\nopen({str(pidfile)!r},'w').write(str(os.getpid()))\ntime.sleep(2)\n")
    root, _, policy = tree
    p = root / "a.mp4"
    p.touch()
    started = time.monotonic()
    with ap.open_authorized_media(policy, str(p)) as (fd, _, _), rejected("PROBE_TIMEOUT"):
        ap.probe_fd(fd, timeout=0.2)
    assert time.monotonic() - started < 1.5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
def test_output_cap(tree, tmp_path, monkeypatch):
    fake_probe(tmp_path, monkeypatch, "import sys\nsys.stdout.write('x'*70000)\n")
    root, _, policy = tree
    p = root / "a.mp4"
    p.touch()
    with ap.open_authorized_media(policy, str(p)) as (fd, _, _), rejected("PROBE_OUTPUT_TOO_LARGE"):
        ap.probe_fd(fd)
@pytest.mark.parametrize("change", [{"duration": "nan"}, {"duration": "inf"},
    {"width": 0}, {"height": 65536}, {"r_frame_rate": "1001/1"}, {"codec_name": "bad\ncodec"}])
def test_sanitized_probe(tree, tmp_path, monkeypatch, change):
    stream = dict(codec_type="video", width=320, height=240, r_frame_rate="25/1", codec_name="mpeg4")
    fmt = dict(duration="3", format_name="mov", tags={"secret": "PRIVATE"})
    (fmt if "duration" in change else stream).update(change)
    fake_probe(tmp_path, monkeypatch, f"import json\nprint({json.dumps({'streams':[stream], 'format':fmt})!r})\n")
    p = tree[0] / "a.mp4"
    p.touch()
    with ap.open_authorized_media(tree[2], str(p)) as (fd, _, _), rejected("PROBE_FAILED"):
        ap.probe_fd(fd)
@pytest.mark.parametrize("refusal", [errno.EPERM, errno.EINVAL, errno.ENOENT])
def test_safe_publish(tree, monkeypatch, refusal):
    _, out, policy = tree
    fd = ap.create_project_dir(policy, "owned")
    folder = out / "owned"
    other = folder / "unrelated"
    other.write_bytes(b"keep")
    def race():
        (folder / "final").write_bytes(b"existing")
    monkeypatch.setattr(ap, "_publish_hook", race)
    with rejected("DESTINATION_EXISTS"):
        ap.publish(fd, "final", {"candidate": True})
    assert (folder / "final").read_bytes() == b"existing"
    assert sorted(p.name for p in folder.iterdir()) == ["final", "unrelated"]
    monkeypatch.setattr(ap, "_publish_hook", lambda: None)
    def unsupported(*a, **kw):
        raise OSError(refusal, "unsupported")
    monkeypatch.setattr(ap.os, "link", unsupported)
    with rejected("OUTPUT_FILESYSTEM_UNSUPPORTED") as exc:
        ap.publish(fd, "new", {"candidate": True})
    assert exc.value.message == "Output root must support hard links; restart with another --output-root (unsaved edits are lost)."
    assert other.read_bytes() == b"keep" and not (folder / "new").exists()
    assert len(list(folder.iterdir())) == 2
    os.close(fd)
def test_cleanup_identity(tree, monkeypatch):
    fd = ap.create_project_dir(tree[2], "owned")
    folder = tree[1] / "owned"
    held = []
    def swap():
        temp = next(folder.glob(".tmp-*")) / "project"
        held.append(str(temp.relative_to(folder)))
        temp.rename(folder / "original")
        temp.write_bytes(b"intruder")
        raise OSError(errno.EPERM, "stop")
    monkeypatch.setattr(ap, "_publish_hook", swap)
    with rejected("OUTPUT_FILESYSTEM_UNSUPPORTED"):
        ap.publish(fd, "final", {"ours": True})
    assert (folder / held[0]).read_bytes() == b"intruder"
    os.close(fd)
def test_roots_and_output(tree, tmp_path):
    with rejected("OUTPUT_UNSAFE"):
        ap.validate_roots([], "/")
    with rejected("OUTPUT_UNSAFE"):
        ap.validate_roots([], str(tmp_path / "absent"))
    (tree[1] / "symlink").symlink_to(tmp_path, target_is_directory=True)
    with rejected("OUTPUT_UNSAFE"):
        ap.create_project_dir(tree[2], "symlink")
    with rejected("NOT_CONFIGURED"), ap.open_authorized_media(ap.validate_roots(), "/tmp/a.mp4"):
        pass
def test_open_once_and_platform(tree, monkeypatch):
    p = tree[0] / "a.mp4"
    p.touch()
    original, opens = ap.os.open, []
    def count(*args, **kw):
        opens.append(args)
        return original(*args, **kw)
    monkeypatch.setattr(ap.os, "open", count)
    with ap.open_authorized_media(tree[2], str(p)):
        pass
    assert len(opens) == 1
    monkeypatch.setattr(ap.sys, "platform", "darwin")
    with rejected("OUTPUT_UNSAFE"):
        ap.validate_roots()
def test_missing_probe_is_bounded(tree, monkeypatch):
    from cove_video_editor.ffmpeg_utils import FFmpegMissingError
    def missing():
        raise FFmpegMissingError("private installation detail")
    monkeypatch.setattr(ap, "require_ffprobe", missing)
    p = tree[0] / "a.mp4"
    p.touch()
    with ap.open_authorized_media(tree[2], str(p)) as (fd, _, _), rejected("PROBE_FAILED") as exc:
        ap.probe_fd(fd)
    assert "private" not in str(exc.value)
    assert "Install FFmpeg (ffprobe)." in exc.value.message
def test_umask_permissions(tree):
    previous = os.umask(0o022)
    try:
        fd = ap.create_project_dir(tree[2], "modes")
        try:
            ap.publish(fd, "final", {"data": True})
        finally:
            os.close(fd)
    finally:
        os.umask(previous)
    assert (tree[1] / "modes").stat().st_mode & 0o777 == 0o755
    assert (tree[1] / "modes/final").stat().st_mode & 0o777 == 0o644
    assert not list((tree[1] / "modes").glob(".tmp-*"))
@pytest.mark.parametrize("symlink", [False, True])
def test_publish_open_inode(tree, monkeypatch, symlink):
    fd = ap.create_project_dir(tree[2], "race"); folder = tree[1] / "race"
    def swap():
        temp = next(folder.glob(".tmp-*")) / "project"; temp.rename(folder / "original")
        temp.symlink_to(folder / "original") if symlink else temp.write_bytes(b"intruder")
    monkeypatch.setattr(ap, "_publish_hook", swap)
    try:
        digest, size = ap.publish(fd, "final", {"ours": True})
        data = (folder / "final").read_bytes()
        assert json.loads(data) == {"ours": True} and not (folder / "final").is_symlink()
        assert (digest, size) == (hashlib.sha256(data).hexdigest(), len(data))
        temp = next(folder.glob(".tmp-*")) / "project"
        assert temp.is_symlink() if symlink else temp.read_bytes() == b"intruder"
    finally:
        os.close(fd)
@pytest.mark.parametrize("inside", [False, True])
def test_indirect_metadata_oracle(tree, inside):
    root, _, policy = tree; outside = (root if inside else root.parent) / "segments"; outside.mkdir()
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=s=640x360:r=25:d=3','-c:v','libx264','-f','dash',str(outside/'source.mpd')],check=True,capture_output=True,timeout=20)
    prefix = "segments/" if inside else str(outside) + "/"
    body = (outside/'source.mpd').read_text().replace('initialization="', 'initialization="'+prefix).replace('media="', 'media="'+prefix)
    bait = root / "bait.mp4"; bait.write_text(body)
    with ap.open_authorized_media(policy, str(bait)) as (fd, _, _):
        weak = subprocess.run(['ffprobe','-v','error','-show_entries','stream=width,height','-of','json','-i',str(bait) if inside else f'/dev/fd/{fd}'],pass_fds=(fd,),capture_output=True,text=True,timeout=10)
        assert weak.returncode == 0 and json.loads(weak.stdout)["streams"][0] == dict(width=640, height=360)
        with rejected("PROBE_FAILED"):
            ap.probe_fd(fd)
def test_single_project_serializer():
    for name in ("agent_paths.py", "agent_session.py", "mcp_server.py"):
        source = (Path(ap.__file__).parent / name).read_text()
        assert all(marker not in source for marker in ("dataclasses.asdict", "asdict(", "json.dump", "pickle.dump"))
@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_preexisting_private_temp(tree, monkeypatch, kind):
    from types import SimpleNamespace
    fd = ap.create_project_dir(tree[2], "collision"); folder = tree[1] / "collision"
    temp = folder / ".tmp-fixed"; target = folder / "target"; target.write_bytes(b"keep")
    if kind == "directory":
        temp.mkdir(); (temp / "child").write_bytes(b"keep")
    elif kind == "symlink":
        temp.symlink_to(target)
    else:
        temp.write_bytes(b"keep")
    monkeypatch.setattr(ap.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    try:
        with rejected("OUTPUT_UNSAFE"):
            ap.publish(fd, "final", {"ours": True})
        assert not (folder / "final").exists() and target.read_bytes() == b"keep"
        assert temp.is_symlink() if kind == "symlink" else (temp / "child" if kind == "directory" else temp).read_bytes() == b"keep"
    finally:
        os.close(fd)
@pytest.mark.parametrize("case", ["failed", "linked", "large", "emptyfailure"])
def test_publish_writer_failure_and_inode_checks(tree, monkeypatch, case):
    fd = ap.create_project_dir(tree[2], "invalid"); folder = tree[1] / "invalid"
    keep = folder / "keep"; keep.write_bytes(b"unrelated")
    def writer(path, doc):
        if case == "emptyfailure":
            raise ap.ProjectError("private path")
        path.write_bytes(b"x" * (16 * 1024 * 1024 + 1) if case == "large" else b"intruder")
        if case == "failed":
            raise ap.ProjectError("private path")
        if case == "linked":
            os.link(path, folder / "other")
    monkeypatch.setattr(ap, "write_project", writer)
    try:
        with rejected("OUTPUT_UNSAFE"):
            ap.publish(fd, "final", {})
        assert keep.read_bytes() == b"unrelated" and not (folder / "final").exists() and bool(list(folder.glob(".tmp-*"))) == (case in {"failed", "linked"})
    finally:
        os.close(fd)
