"""M3a audio behavior, using synthetic tones and independent PCM measurements."""
from __future__ import annotations
import array, copy, json, math, os, subprocess, threading, time
from pathlib import Path
import pytest
from cove_video_editor import agent_paths as ap, agent_session as am, agent_exports as ae, exporter as ex, project_io as io
from test_agent_paths import tree, video, rejected, fake_probe
from test_agent_session import editing, clip
from test_agent_exports import finish, block

def tones(path):
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
        'sine=frequency=660:duration=2:sample_rate=48000', '-f', 'lavfi', '-i',
        'sine=frequency=880:duration=2:sample_rate=48000', '-filter_complex',
        '[0:a][1:a]concat=n=2:v=0:a=1[a]', '-map', '[a]', '-y', str(path)],
        check=True, capture_output=True, timeout=15)
    return path

def pcm(path):
    data = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1',
        '-ar', '48000', '-f', 's16le', '-'], check=True, capture_output=True, timeout=15).stdout
    result = array.array('h'); result.frombytes(data)
    return result

def amplitude(samples, frequency, start=0.8, end=1.8):
    # Independently measure the known sine by projection, away from AAC boundaries.
    segment = samples[int(start*48000):int(end*48000)]
    assert segment
    step = 2*math.pi*frequency/48000
    real = sum(x*math.cos(step*i) for i, x in enumerate(segment))
    imag = sum(x*math.sin(step*i) for i, x in enumerate(segment))
    return 2*math.hypot(real, imag)/len(segment)

@pytest.fixture
def audio_edit(editing):
    s, pid, aid, source = editing
    music = tones(source.parent / 'music.wav')
    s.set_timeline(pid, 'sequential', [clip(aid)])
    return s, pid, aid, music

def test_audio_replace_clear_roundtrip(audio_edit):
    s, pid, aid, music = audio_edit
    old = s.get_project(pid)
    added = s.set_audio(pid, [dict(path=str(music), offset_s=0.5, src_start_s=2,
        src_end_s=3.5, lane=7, volume=0.8, mute=True)])
    item = added['audio'][0]
    assert item == dict(audio_id=item['audio_id'], path=str(music), duration_s=4,
        offset_s=0.5, src_start_s=2, src_end_s=3.5, lane=7, volume=0.8,
        mute=True, timeline_end_s=2)
    assert len(item['audio_id']) == 32 and added['revision'] == old['revision']+1
    assert added['clips'] == old['clips'] and added['mix'] == old['mix']
    p = s.projects[pid]
    assert set(p.identities) == {aid} and set(p.audio_identities) == {item['audio_id']}
    mixed = s.set_audio_mix(pid, original_gain=0.7, added_gain=1.25, replace_original=True)
    assert mixed['revision'] == added['revision']+1
    saved = s.save_project(pid)
    state = io.load_project(Path(saved['path']))
    a = state.added_audios[0]
    assert (a.path, a.duration, a.offset, a.src_start, a.src_end, a.lane, a.volume, a.muted, a.id) == (
        music, 4, 0.5, 2, 3.5, 7, 0.8, True, item['audio_id'])
    assert (state.original_gain, state.added_gain, state.replace_added_audio) == (0.7, 1.25, True)
    assert io.serialize_project(state) == io.serialize_project(p.state)
    assert s.get_project(pid)['saved_is_current']
    replace = s.set_audio(pid, [dict(path=str(music)), dict(path=str(music), lane=1)])
    assert len(replace['audio']) == 2 and all(a['audio_id'] != item['audio_id'] for a in replace['audio'])
    assert replace['audio'][0]['src_end_s'] == 4 and replace['audio'][0]['lane'] == 1
    assert not replace['saved_is_current'] and replace['mix'] == mixed['mix']
    removed = s.set_audio(pid, [dict(path=str(music))])
    assert len(removed['audio']) == 1
    cleared = s.set_audio(pid, [])
    assert cleared['audio'] == [] and not p.audio_identities and cleared['mix'] == mixed['mix']
    assert s.set_audio(pid, [])['revision'] == cleared['revision']+1

def test_audio_preserved_by_video_edits(audio_edit):
    s, pid, aid, music = audio_edit
    s.set_audio(pid, [dict(path=str(music), lane=63, offset_s=1)])
    before = s.set_audio_mix(pid, added_gain=0.5, original_gain=0.4, replace_original=True)
    identities = dict(s.projects[pid].audio_identities)
    other = video(music.parent / 'other.mp4')
    s.import_media(pid, [str(other)])
    imported = s.get_project(pid)
    assert imported['audio'] == before['audio'] and imported['mix'] == before['mix']
    edited = s.set_timeline(pid, 'sequential', [clip(aid), clip(aid)])
    assert edited['audio'] == before['audio'] and edited['mix'] == before['mix']
    assert s.projects[pid].audio_identities == identities
    s.set_timeline(pid, 'sequential', [])
    with rejected('EMPTY_TIMELINE'):
        s.save_project(pid)

@pytest.mark.parametrize('bad,code', [
    ({'path': None}, 'INVALID_ARGUMENT'), ({'path': 'relative.wav'}, 'INVALID_ARGUMENT'),
    ({'extra': 1}, 'INVALID_ARGUMENT'), ({'offset_s': True}, 'INVALID_ARGUMENT'),
    ({'offset_s': -1}, 'INVALID_TIMELINE'), ({'offset_s': 86401}, 'INVALID_TIMELINE'),
    ({'offset_s': 86400}, 'INVALID_TIMELINE'), ({'src_start_s': -1}, 'INVALID_TIMELINE'),
    ({'src_start_s': 4}, 'INVALID_TIMELINE'), ({'src_end_s': 0}, 'INVALID_TIMELINE'),
    ({'src_end_s': 5}, 'INVALID_TIMELINE'), ({'src_end_s': True}, 'INVALID_ARGUMENT'),
    ({'volume': -0.1}, 'INVALID_TIMELINE'), ({'volume': 2.1}, 'INVALID_TIMELINE'),
    ({'volume': True}, 'INVALID_ARGUMENT'), ({'volume': '1'}, 'INVALID_ARGUMENT'),
    ({'volume': float('nan')}, 'INVALID_ARGUMENT'), ({'offset_s': float('inf')}, 'INVALID_ARGUMENT'),
    ({'lane': True}, 'INVALID_ARGUMENT'), ({'lane': 1.0}, 'INVALID_ARGUMENT'),
    ({'lane': -1}, 'INVALID_ARGUMENT'), ({'lane': 64}, 'INVALID_ARGUMENT'),
    ({'mute': 1}, 'INVALID_ARGUMENT')])
def test_audio_invalid_atomic(audio_edit, bad, code):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))])
    before, identities = s.get_project(pid), dict(s.projects[pid].audio_identities)
    with rejected(code) as error:
        s.set_audio(pid, [dict(path=str(music)), dict(path=str(music)) | bad])
    assert 'items[1]' in error.value.message
    assert s.get_project(pid) == before and s.projects[pid].audio_identities == identities

@pytest.mark.parametrize('items,code', [([{}], 'INVALID_ARGUMENT'), ([None], 'INVALID_ARGUMENT'),
    ('[]', 'INVALID_ARGUMENT'), ([{}]*51, 'LIMIT_EXCEEDED')])
def test_audio_invalid_list_atomic(audio_edit, items, code):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); before = s.get_project(pid)
    with rejected(code): s.set_audio(pid, items)
    assert s.get_project(pid) == before

def test_mix_partial_and_independence(audio_edit):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music), mute=True, volume=0.8)])
    before = s.set_audio_mix(pid, original_gain=0.7, added_gain=0.8, replace_original=True)
    changed = s.set_audio_mix(pid, added_gain=0.5)
    assert changed['mix'] == dict(original_gain=0.7, added_gain=0.5, replace_original=True)
    assert changed['audio'] == before['audio'] and changed['revision'] == before['revision']+1
    assert s.set_audio_mix(pid, replace_original=False)['mix'] == dict(original_gain=0.7, added_gain=0.5, replace_original=False)

@pytest.mark.parametrize('args', [{}, {'added_gain': True}, {'original_gain': '1'},
    {'added_gain': -1}, {'original_gain': 3.01}, {'added_gain': 0.123},
    {'original_gain': float('nan')}, {'added_gain': float('inf')}, {'replace_original': 1},
    {'added_gain': None}])
def test_mix_invalid_atomic(audio_edit, args):
    s, pid, _, _ = audio_edit
    before = s.get_project(pid)
    with pytest.raises(ap.AgentError): s.set_audio_mix(pid, **args)
    assert s.get_project(pid) == before

def test_audio_warnings_and_budget(audio_edit, monkeypatch):
    s, pid, _, music = audio_edit
    summary = s.set_audio(pid, [dict(path=str(music)), dict(path=str(music), offset_s=2.25)])
    assert 'truncated at timeline end' in summary['warnings'][0]
    assert 'inaudible' in summary['warnings'][1]
    monkeypatch.setattr(ap, 'PROBE_BUDGET', 0)
    with rejected('PROBE_BUDGET_EXCEEDED'): s.set_audio(pid, [dict(path=str(music))])
    assert s.get_project(pid) == summary

def test_audio_elapsed_budget_is_atomic(audio_edit, monkeypatch):
    s, pid, _, music = audio_edit; clock = [0.0]; original = ap.probe_fd
    s.set_audio(pid, [dict(path=str(music))]); before = s.get_project(pid)
    identities = dict(s.projects[pid].audio_identities)
    monkeypatch.setattr(am.time, 'monotonic', lambda: clock[0])
    def slow(fd, **kwargs):
        result = original(fd, **kwargs); clock[0] += 61; return result
    monkeypatch.setattr(ap, 'probe_fd', slow)
    with rejected('PROBE_BUDGET_EXCEEDED') as error:
        s.set_audio(pid, [dict(path=str(music)),dict(path=str(music))])
    assert 'items[1]' in error.value.message
    assert s.get_project(pid) == before and s.projects[pid].audio_identities == identities

@pytest.mark.parametrize('kind,code', [('outside', 'PATH_NOT_AUTHORIZED'), ('missing', 'FILE_NOT_FOUND'),
    ('video', 'UNSUPPORTED_EXTENSION'), ('garbage', 'PROBE_FAILED'), ('truncated', 'PROBE_FAILED'),
    ('fifo', 'NOT_A_REGULAR_FILE'), ('real_video', 'UNSUPPORTED_MEDIA'), ('symlink', 'PATH_NOT_AUTHORIZED')])
def test_audio_path_and_content_atomic(audio_edit, tmp_path, kind, code):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); before = s.get_project(pid)
    path = music.parent / 'bad.mp3'
    if kind == 'outside': path = tones(tmp_path / 'outside.wav')
    elif kind == 'video': path = video(music.parent / 'bad.mp4', True)
    elif kind == 'real_video':
        source = video(music.parent / 'real.mp4', True); path = music.parent / 'real.m4a'; path.write_bytes(source.read_bytes())
    elif kind == 'garbage': path.write_bytes(b'garbage')
    elif kind == 'truncated': path.write_bytes(music.read_bytes()[:20])
    elif kind == 'fifo': os.mkfifo(path)
    elif kind == 'symlink': path.symlink_to(tones(tmp_path / 'outside.wav'))
    with rejected(code) as error: s.set_audio(pid, [dict(path=str(path))])
    assert 'items[0]' in error.value.message and s.get_project(pid) == before

@pytest.mark.parametrize('change', ['replace', 'modify', 'remove'])
def test_audio_changed_before_save_and_export(audio_edit, change):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); s.save_project(pid)
    if change == 'replace':
        replacement = tones(music.parent / 'new.wav'); replacement.replace(music)
    elif change == 'modify': music.write_bytes(music.read_bytes()+b'changed')
    else: music.unlink()
    for action in (lambda: s.save_project(pid), lambda: s.start_export(pid, 'changed.mp4')):
        with rejected('ASSET_CHANGED') as error: action()
        assert 'audio[0]' in error.value.message and 'cove_set_audio' in error.value.message

def render(s, pid, name):
    s.save_project(pid)
    return finish(s, s.start_export(pid, name))

def test_audio_effective_endpoint_atomic(audio_edit):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); s.save_project(pid)
    before = s.get_project(pid)
    identities = dict(s.projects[pid].audio_identities)
    with rejected('INVALID_TIMELINE'):
        s.set_audio(pid, [dict(path=str(music), offset_s=86399.9995, src_end_s=0.0001)])
    assert s.get_project(pid) == before
    assert s.projects[pid].audio_identities == identities
    valid = s.set_audio(pid, [dict(path=str(music), offset_s=86399.999, src_end_s=0.0001)])
    assert valid['audio'][0]['timeline_end_s'] == 86400
    assert valid['revision'] == before['revision']+1

@pytest.mark.parametrize('offset', [2.25, 5])
@pytest.mark.parametrize('recovery', ['timeline', 'offset'])
def test_audio_after_end_save_refused(audio_edit, offset, recovery):
    s, pid, aid, music = audio_edit
    s.save_project(pid)
    summary = s.set_audio(pid, [dict(path=str(music), offset_s=offset)])
    p = s.projects[pid]; identities = dict(p.audio_identities)
    files = {f.name:f.read_bytes() for f in Path(s.policy.output_root).rglob('*.coveproj')}
    with rejected('INVALID_TIMELINE') as exc:
        s.save_project(pid)
    assert 'audio[0]' in exc.value.message and summary['audio'][0]['audio_id'] in exc.value.message
    assert f'{offset:g}' in exc.value.message and '2.25 s' in exc.value.message
    assert 'move it earlier' in exc.value.message and 'cove_set_timeline' in exc.value.message
    assert 'saving will be refused' in summary['warnings'][0]
    assert s.get_project(pid) == summary and p.audio_identities == identities
    assert files == {f.name:f.read_bytes() for f in Path(s.policy.output_root).rglob('*.coveproj')}
    if recovery == 'timeline':
        s.set_timeline(pid, 'sequential', [clip(aid)]*3)
    else:
        s.set_audio(pid, [dict(path=str(music), offset_s=2)])
    s.save_project(pid)
    assert s.get_project(pid)['saved_is_current']

def test_real_audio_starts_before_end(audio_edit):
    s, pid, _, music = audio_edit
    baseline = render(s, pid, 'before-end-baseline.mp4')['output']
    s.set_audio(pid, [dict(path=str(music), offset_s=2)])
    s.set_audio_mix(pid, replace_original=True)
    output = render(s, pid, 'before-end.mp4')['output']
    samples = pcm(output['path'])
    assert abs(output['duration_s']-baseline['duration_s']) < 0.1
    assert 2.2 < len(samples)/48000 < 2.4
    assert amplitude(samples, 660, 0.5, 1.5) < 30
    assert amplitude(samples, 660, 2.05, 2.2) > 3000
    assert amplitude(samples, 880, 2.05, 2.2) < 50

def test_real_audio_trim_offset_and_truncation(audio_edit):
    s, pid, _, music = audio_edit
    baseline = render(s, pid, 'baseline.mp4')['output']
    s.set_audio(pid, [dict(path=str(music), src_start_s=2, offset_s=0.5, lane=9)])
    s.set_audio_mix(pid, replace_original=True)
    output = render(s, pid, 'trim.mp4')['output']; samples = pcm(output['path'])
    assert amplitude(samples, 880) > 3500 and amplitude(samples, 660) < 30
    assert amplitude(samples, 880, 0.1, 0.3) < 20 and amplitude(samples, 440) < 30
    assert len(samples)/48000 < 2.4
    for field in ('width', 'height', 'duration_s', 'video_codec', 'audio_codec', 'has_audio'):
        assert output[field] == baseline[field]

def test_real_audio_gain_mute_mix_overlap(audio_edit):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); s.set_audio_mix(pid, replace_original=True)
    reference = pcm(render(s, pid, 'reference.mp4')['output']['path'])
    strength = amplitude(reference, 660)
    assert strength > 3500 and amplitude(reference, 440) < 30
    s.set_audio(pid, [dict(path=str(music), volume=0.4)])
    s.set_audio_mix(pid, added_gain=0.5)
    quiet = pcm(render(s, pid, 'compound.mp4')['output']['path'])
    assert 0.18 < amplitude(quiet, 660)/strength < 0.22
    s.set_audio_mix(pid, added_gain=1, replace_original=False, original_gain=1)
    s.set_audio(pid, [dict(path=str(music))])
    mixed = pcm(render(s, pid, 'mixed.mp4')['output']['path'])
    original = amplitude(mixed, 440)
    assert original > 1700 and amplitude(mixed, 660) > 1700
    s.set_audio_mix(pid, original_gain=0.4)
    low_orig = pcm(render(s, pid, 'original-low.mp4')['output']['path'])
    assert 0.36 < amplitude(low_orig, 440)/original < 0.44
    s.set_audio_mix(pid, original_gain=1)
    s.set_audio(pid, [dict(path=str(music), volume=0.8, mute=True)])
    muted = pcm(render(s, pid, 'muted.mp4')['output']['path'])
    assert amplitude(muted, 660) < 30 and 0.9 < amplitude(muted, 440)/original < 1.1
    s.set_audio_mix(pid, replace_original=True)
    silent = pcm(render(s, pid, 'silent.mp4')['output']['path'])
    assert max(map(abs, silent)) < 5
    s.set_audio(pid, [dict(path=str(music), src_end_s=2), dict(path=str(music), src_start_s=2, lane=1)])
    overlapping = pcm(render(s, pid, 'overlap.mp4')['output']['path'])
    assert amplitude(overlapping, 660) > 1700 and amplitude(overlapping, 880) > 1700

def test_audio_export_snapshot_and_descriptors(audio_edit, monkeypatch):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music), src_start_s=2, volume=0.8)])
    s.set_audio_mix(pid, added_gain=0.5, replace_original=True)
    saved = s.save_project(pid)
    entered, release = block(monkeypatch, 'before_run')
    before = len(os.listdir('/proc/self/fd'))
    started = s.start_export(pid, 'snapshot.mp4'); job = s.exports._get(started['job_id'])
    try:
        assert entered.wait(5)
        assert job.render.require_audio_input_fds
        cmd = job.worker._build_command()
        assert str(music) not in cmd
        assert all(cmd[i+1].startswith('/dev/fd/') for i, x in enumerate(cmd) if x == '-i')
        assert job.render.input_options[-1] == 'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mp3,wav,flac,ogg,aac'
        s.set_audio(pid, []); s.set_audio_mix(pid, added_gain=3, replace_original=False)
        assert len(job.render.audio_tracks) == 1 and job.render.audio_tracks[0].volume == 0.4
    finally: release.set()
    result = finish(s, started)
    assert result['saved_revision'] == saved['revision']
    samples = pcm(result['output']['path'])
    assert amplitude(samples, 880) > 1400 and amplitude(samples, 440) < 30
    assert len(os.listdir('/proc/self/fd')) == before

def test_audio_missing_fd_agent_fails_closed(audio_edit, monkeypatch):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); s.save_project(pid)
    spawned, original = [], ex.subprocess.Popen
    def spawn(cmd, **kwargs):
        spawned.append(cmd); return original(cmd, **kwargs)
    monkeypatch.setattr(ex.subprocess, 'Popen', spawn)
    def missing(stage, job):
        if stage == 'before_run':
            key = job.render.audio_tracks[0].input_key
            job.render.input_fds.pop(key)
    monkeypatch.setattr(ae, '_hook', missing)
    result = finish(s, s.start_export(pid, 'missing.mp4'), 'FAILED')
    assert result['error']['code'] == 'EXPORT_FAILED' and 'validated audio descriptor' in result['error']['message']
    assert not spawned

@pytest.mark.parametrize('stage', ['preparing', 'before_run'])
@pytest.mark.parametrize('change', ['replace', 'remove'])
def test_audio_replaced_after_admission(audio_edit, monkeypatch, stage, change):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); s.save_project(pid)
    replacement = tones(music.parent / 'replacement.wav')
    def swap(current, job):
        if current == stage:
            replacement.replace(music) if change == 'replace' else music.unlink()
    monkeypatch.setattr(ae, '_hook', swap)
    started = s.start_export(pid, 'swapped.mp4')
    result = finish(s, started, 'FAILED' if stage == 'preparing' else 'SUCCEEDED')
    if stage == 'preparing': assert result['error']['code'] == 'ASSET_CHANGED'
    else: assert amplitude(pcm(result['output']['path']), 660) > 1700

def test_audio_in_place_during_render(audio_edit, monkeypatch):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); s.save_project(pid)
    original = ex.ExportWorker.run
    def changed(worker):
        original(worker)
        music.write_bytes(music.read_bytes()+b'in-place edit')
    monkeypatch.setattr(ex.ExportWorker, 'run', changed)
    before = len(os.listdir('/proc/self/fd'))
    result = finish(s, s.start_export(pid, 'inplace.mp4'), 'FAILED')
    assert result['error']['code'] == 'ASSET_CHANGED'
    assert len(os.listdir('/proc/self/fd')) == before
    assert not list(Path(s.policy.output_root).rglob('inplace.mp4'))

@pytest.mark.parametrize('mode', ['failure', 'cancel', 'eof'])
def test_audio_resource_cleanup(audio_edit, monkeypatch, tmp_path, mode):
    s, pid, _, music = audio_edit
    s.set_audio(pid, [dict(path=str(music))]); s.save_project(pid)
    script = tmp_path/'owned-ffmpeg'
    script.write_text('#!/usr/bin/python3\nimport time\nprint("out_time_us=1000000",flush=True)\n'
        + ('raise SystemExit(7)\n' if mode == 'failure' else 'time.sleep(60)\n'))
    script.chmod(0o700); monkeypatch.setattr(ex.ff, 'require_ffmpeg', lambda: str(script))
    before = len(os.listdir('/proc/self/fd'))
    started = s.start_export(pid, 'cleanup.mp4'); job = s.exports._get(started['job_id'])
    deadline = time.monotonic()+5
    while job.progress_percent is None and not job.done.is_set():
        assert time.monotonic() < deadline; job.done.wait(0.01)
    assert job.worker is not None and job.worker._proc is not None
    if mode == 'cancel': s.cancel_export(job.job_id)
    elif mode == 'eof': s.shutdown_exports()
    finish(s, started, 'FAILED' if mode == 'failure' else 'CANCELLED')
    assert job.worker._proc.poll() is not None
    assert job.worker._proc.stdout.closed and job.worker._proc.stderr.closed
    assert not job.owner.is_alive() and not job.supervisor.is_alive()
    assert not job.owner.daemon and not job.supervisor.daemon
    assert len(os.listdir('/proc/self/fd')) == before
    assert not list(Path(s.policy.output_root).rglob('.export-*'))

def test_video_only_whitelist_stays_strict(audio_edit, monkeypatch):
    s, pid, _, _ = audio_edit
    observed = []
    def inspect(stage, job):
        if stage == 'before_run': observed.append(job.render.input_options)
    monkeypatch.setattr(ae, '_hook', inspect)
    render(s, pid, 'video-only.mp4')
    assert observed == [('-protocol_whitelist', 'file', '-format_whitelist',
        'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi')]

def test_audio_capabilities(audio_edit):
    s, _, _, _ = audio_edit; caps = s.capabilities()
    assert len(caps['operations']) == 12
    assert {'cove_set_audio', 'cove_set_audio_mix'} <= set(caps['operations'])
    assert caps['media_kinds'] == ['video', 'audio']
    assert caps['audio_extensions'] == ['.aac', '.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav']
    assert caps['limits']['audio_items'] == 50 and caps['limits']['audio_lane_max'] == 63
    assert caps['deferred'] == ['subtitles', 'crop', 'open_project']
    assert 'background audio' in caps['supported']
