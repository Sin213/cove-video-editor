import os
from pathlib import Path
from unittest.mock import Mock
from cove_video_editor.clip import MediaAsset, Clip
from cove_video_editor import exporter as ex

def job(tmp_path, **kwargs):
    a = MediaAsset(tmp_path / "clip.mp4", 3, 320, 240, 25, True)
    return ex.ExportJob([Clip(a)], tmp_path / "out.mp4", "MP4 (H.264 + AAC)", encoder_pref="cpu", **kwargs)

def test_input_capabilities(tmp_path):
    j = job(tmp_path, input_options=("-protocol_whitelist", "file"), input_fds={})
    j.input_fds[j.clips[0].asset.id] = 42
    cmd = ex.ExportWorker(j)._build_command()
    i = cmd.index("-i")
    assert cmd[i-2:i+2] == ["-protocol_whitelist", "file", "-i", "/dev/fd/42"]
    assert str(j.clips[0].path) not in cmd

def test_kill_owns_only_live_child(tmp_path):
    w = ex.ExportWorker(job(tmp_path))
    w.kill()
    p = Mock(); p.poll.return_value = None
    w._proc = p
    w.kill()
    p.kill.assert_called_once_with()
    p.poll.return_value = 0
    w.kill()
    p.kill.assert_called_once_with()

def test_default_argv_and_popen_compatibility(tmp_path, monkeypatch):
    j = job(tmp_path)
    w = ex.ExportWorker(j)
    graph = ('[0:v]trim=start=0.000:end=3.000,setpts=PTS-STARTPTS,'
        'scale=320:240:force_original_aspect_ratio=decrease:force_divisible_by=2,'
        'pad=320:240:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[v0];'
        '[0:a]atrim=start=0.000:end=3.000,asetpts=PTS-STARTPTS[a0];'
        '[v0][a0]concat=n=1:v=1:a=1[vc][ac]')
    assert w._build_command() == [ex.ff.require_ffmpeg(), '-nostdin', '-y', '-hide_banner',
        '-progress', 'pipe:1', '-nostats', '-loglevel', 'error', '-i', str(j.clips[0].path),
        '-filter_complex', graph, '-map', '[vc]', '-map', '[ac]', '-c:v', 'libx264',
        '-crf', '20', '-preset', 'medium', '-c:a', 'aac', '-b:a', '192k', '-pix_fmt',
        'yuv420p', '-movflags', '+faststart', str(j.output)]
    def spawn(cmd, **kwargs):
        assert "pass_fds" not in kwargs
        raise OSError("spawn observed")
    monkeypatch.setattr(ex.subprocess, "Popen", spawn)
    with __import__('pytest').raises(OSError, match="spawn observed"):
        w._execute(w._build_command())

def test_options_each_file_and_distinct_pass_fds(tmp_path, monkeypatch):
    j = job(tmp_path, input_options=("-protocol_whitelist", "file"))
    j.clips.append(j.clips[0].clone())
    j.audio_tracks = [ex.AudioTrack(tmp_path / "audio.wav")]
    j.input_fds = {j.clips[0].asset.id: 42, "extra-trusted-asset": 42, "another": 43}
    w = ex.ExportWorker(j)
    cmd = w._build_command()
    inputs = [i for i, arg in enumerate(cmd) if arg == "-i"]
    assert len(inputs) == 3
    assert all(cmd[i-2:i] == list(j.input_options) for i in inputs)
    assert [cmd[i+1] for i in inputs] == ["/dev/fd/42", "/dev/fd/42", str(tmp_path / "audio.wav")]
    def spawn(cmd, **kwargs):
        assert kwargs["pass_fds"] == (42, 43)
        raise OSError("spawn observed")
    monkeypatch.setattr(ex.subprocess, "Popen", spawn)
    with __import__('pytest').raises(OSError, match="spawn observed"):
        w._execute(cmd)
