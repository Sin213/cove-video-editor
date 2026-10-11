import os
from pathlib import Path
from unittest.mock import Mock
from cove_video_editor.clip import MediaAsset, Clip
from cove_video_editor import exporter as ex
import pytest

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

def test_audio_descriptor_substitution(tmp_path, monkeypatch):
    j = job(tmp_path)
    track = ex.AudioTrack(tmp_path / "private.wav")
    track.input_key = "audio:trusted"
    j.audio_tracks = [track]
    j.require_audio_input_fds = True
    j.input_fds = {j.clips[0].asset.id: 42, "audio:trusted": 43}
    w = ex.ExportWorker(j)
    cmd = w._build_command()
    assert [cmd[i+1] for i, arg in enumerate(cmd) if arg == "-i"] == ["/dev/fd/42", "/dev/fd/43"]
    assert str(track.path) not in cmd
    def spawn(argv, **kwargs):
        assert kwargs["pass_fds"] == (42, 43)
        raise OSError("spawn observed")
    monkeypatch.setattr(ex.subprocess, "Popen", spawn)
    with pytest.raises(OSError, match="spawn observed"):
        w._execute(cmd)

@pytest.mark.parametrize("key", [None, "audio:missing"])
def test_strict_audio_missing_descriptor_never_spawns(tmp_path, monkeypatch, key):
    j = job(tmp_path)
    track = ex.AudioTrack(tmp_path / "private.wav")
    track.input_key = key
    j.audio_tracks = [track]
    j.require_audio_input_fds = True
    w = ex.ExportWorker(j)
    spawn = Mock()
    monkeypatch.setattr(ex.subprocess, "Popen", spawn)
    built = []
    with pytest.raises(RuntimeError, match="validated audio descriptor"):
        built.extend(w._build_command())
        w._execute(built)
    assert spawn.call_count == 0 and str(track.path) not in built

def test_gui_audio_job_argv_unchanged(tmp_path):
    from cove_video_editor.clip import AddedAudio
    from cove_video_editor.project_io import ProjectState
    base = job(tmp_path)
    audio = AddedAudio(tmp_path / "music.wav", duration=3, volume=0.8, lane=7)
    state = ProjectState(assets=[base.clips[0].asset], clips=base.clips,
        added_audios=[audio], added_gain=0.5, original_gain=0.7)
    built = ex.build_export_job(state, output=base.output, fmt_key=base.fmt_key, encoder_pref="cpu")
    legacy = job(tmp_path, audio_tracks=[ex.AudioTrack(audio.path, volume=0.4, original_volume=0.7, duration=3)])
    assert ex.ExportWorker(built)._build_command() == ex.ExportWorker(legacy)._build_command()
    assert built.input_fds == {} and built.input_options == ()

@pytest.mark.parametrize("replace", [True, False])
def test_default_job_real_audio_replace_and_mix(tmp_path, replace):
    from cove_video_editor.clip import AddedAudio
    from cove_video_editor.project_io import ProjectState
    from test_agent_paths import video
    from test_agent_editing import tones, pcm, amplitude
    source, music = video(tmp_path / "clip.mp4", True), tones(tmp_path / "music.wav")
    asset = MediaAsset(source, 3, 320, 240, 25, True)
    state = ProjectState(assets=[asset], clips=[Clip(asset)],
        added_audios=[AddedAudio(music, duration=4)], replace_added_audio=replace)
    j = ex.build_export_job(state, output=tmp_path / "result.mp4", fmt_key="MP4 (H.264 + AAC)", encoder_pref="cpu")
    assert j.input_fds == {} and j.input_options == () and not j.require_audio_input_fds
    worker, failed, finished = ex.ExportWorker(j), [], []
    worker.failed.connect(failed.append); worker.finished.connect(finished.append)
    worker.run()
    assert not failed, failed
    assert finished == [j.output]
    samples = pcm(j.output)
    assert amplitude(samples, 660) > (3500 if replace else 1700)
    assert amplitude(samples, 440) < 30 if replace else amplitude(samples, 440) > 1700

# Frozen from the pre-change exporter outside the test run. These literal
# fragments preserve every byte while sharing identical stages across scenarios.
BASELINE_VIDEO = (
    '[0:v]trim=start=0.000:end=3.000,setpts=PTS-STARTPTS,'
    'scale=320:240:force_original_aspect_ratio=decrease:force_divisible_by=2,'
    'pad=320:240:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[v0]')
BASELINE_CROP = (
    '[0:v]trim=start=0.000:end=3.000,setpts=PTS-STARTPTS,crop=256:192:32:24,'
    'scale=256:192:force_original_aspect_ratio=decrease:force_divisible_by=2,'
    'pad=256:192:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[v0]')
BASELINE_CONCAT = (
    ';[0:a]atrim=start=0.000:end=3.000,asetpts=PTS-STARTPTS[a0];'
    '[v0][a0]concat=n=1:v=1:a=1[vc][ac]')
BASELINE_EXTRA = (
    'atrim=start=1.000:end=3.500,asetpts=PTS-STARTPTS,adelay=500:all=1,'
    'apad=whole_dur=3.000,volume=0.400[extra_a0]')
BASELINE_MIX = (
    ';[ac]volume=0.700[orig_a];'
    '[orig_a][extra_a0]amix=inputs=2:duration=longest:dropout_transition=0[mix_a]')
BASELINE_GRAPHS = {
    'video_only': BASELINE_VIDEO + BASELINE_CONCAT,
    'audio_only': ('anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration=3.000,'
        'asetpts=PTS-STARTPTS[a0];[a0]concat=n=1:v=0:a=1[ac];[0:a]') + BASELINE_EXTRA + BASELINE_MIX,
    'mix': BASELINE_VIDEO + BASELINE_CONCAT + ';[1:a]' + BASELINE_EXTRA + BASELINE_MIX,
    'crop': BASELINE_CROP + BASELINE_CONCAT + ';[1:a]' + BASELINE_EXTRA + BASELINE_MIX,
    'replace': BASELINE_VIDEO + BASELINE_CONCAT + ';[1:a]' + BASELINE_EXTRA,
}
BASELINE_COMMAND_PREFIX = [
    '-nostdin', '-y', '-hide_banner', '-progress', 'pipe:1', '-nostats', '-loglevel', 'error']
BASELINE_VIDEO_ENCODING = [
    '-c:v', 'libx264', '-crf', '20', '-preset', 'medium', '-c:a', 'aac', '-b:a', '192k',
    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '/golden/graph-output']
BASELINE_COMMANDS = {
    'video_only': BASELINE_COMMAND_PREFIX + ['-i', '/golden/clip.mp4', '-filter_complex', 'GRAPH',
        '-map', '[vc]', '-map', '[ac]'] + BASELINE_VIDEO_ENCODING,
    'audio_only': BASELINE_COMMAND_PREFIX + ['-i', '/golden/music.wav', '-filter_complex', 'GRAPH',
        '-map', '[mix_a]', '-c:a', 'pcm_s16le', '/golden/graph-output'],
    'mix': BASELINE_COMMAND_PREFIX + ['-i', '/golden/clip.mp4', '-i', '/golden/music.wav',
        '-filter_complex', 'GRAPH', '-map', '[vc]', '-map', '[mix_a]'] + BASELINE_VIDEO_ENCODING,
    'crop': BASELINE_COMMAND_PREFIX + ['-i', '/golden/clip.mp4', '-i', '/golden/music.wav',
        '-filter_complex', 'GRAPH', '-map', '[vc]', '-map', '[mix_a]'] + BASELINE_VIDEO_ENCODING,
}

@pytest.mark.parametrize('mode', ['video_only', 'audio_only', 'mix', 'crop', 'replace'])
def test_filtergraph_matches_prechange_head(mode):
    from cove_video_editor.clip import AddedAudio
    from cove_video_editor.project_io import ProjectState
    root = Path('/golden')
    base = job(root)
    base.clips[0].asset.id, base.clips[0].id = 'asset', 'clip'
    audio = AddedAudio(root/'music.wav', duration=4, src_start=1, src_end=3.5, offset=0.5, volume=0.8, id='audio')
    if mode == 'crop': base.clips[0].crop_rect = (0.1, 0.1, 0.8, 0.8)
    state = ProjectState(assets=[base.clips[0].asset], clips=[] if mode == 'audio_only' else base.clips,
        added_audios=[] if mode == 'video_only' else [audio],
        replace_added_audio=mode == 'replace', added_gain=0.5, original_gain=0.7)
    fmt = 'WAV (audio only)' if mode == 'audio_only' else base.fmt_key
    args = dict(output=root/'graph-output', fmt_key=fmt, encoder_pref='cpu')
    new = ex.ExportWorker(ex.build_export_job(state, **args))._build_command()
    old_graph, new_graph = BASELINE_GRAPHS[mode], new[new.index('-filter_complex')+1]
    assert new_graph == old_graph + (';[ac]anullsink' if mode == 'replace' else '')
    if mode != 'replace':
        old = [ex.ff.require_ffmpeg()] + BASELINE_COMMANDS[mode]
        old[old.index('GRAPH')] = old_graph
        assert new == old
