"""Copy-on-import oracles use the real project serializer and synthetic media."""
import copy, hashlib, json, math, os, subprocess, sys, threading, time, warnings
from pathlib import Path
import pytest
from cove_video_editor import agent_paths as ap, agent_session as mod, project_io as io
from cove_video_editor.clip import MediaAsset, Clip, AddedAudio, SubtitleTrack
from cove_video_editor.ffmpeg_utils import probe, probe_audio_duration
from test_agent_paths import video, rejected
from test_agent_exports import finish, block
from test_agent_editing import tones, pcm, amplitude

SRT = '1\n00:00:00,800 --> 00:00:01,600\nCove import\n'
@pytest.fixture
def source(tmp_path):
    media, projects, out = [tmp_path/n for n in ('media','projects','out')]
    for p in (media,projects,out): p.mkdir()
    path = video(media/'clip.mp4', True)
    info = probe(path)
    asset = MediaAsset(path, info.duration, info.width, info.height, info.fps, info.has_audio, id='asset-one')
    clip = Clip(asset, src_start=0.25, src_end=2.5, id='clip-one')
    state = io.ProjectState(assets=[asset], clips=[clip])
    project = projects/'Original.coveproj'
    io.save_project(project, state)
    policy = ap.validate_roots([str(media)],str(out))
    # Works on the pre-M3c baseline too, so RED is behavioral, not a constructor error.
    policy.project_roots = (str(projects),)
    session = mod.AgentSession(policy)
    yield session, project, state, media, out
    session.close()

def save(source, state=None, doc=None):
    _, path, original, _, _ = source
    if doc is None: io.save_project(path, state or original)
    else: path.write_text(json.dumps(doc), encoding='utf-8')
    return path

def test_open_capabilities(source):
    caps = source[0].capabilities()
    assert 'cove_open_project' in caps['operations']
    assert len(caps['operations']) == 15 and caps['deferred'] == []
    assert caps['project_roots'] == [str(source[1].parent)]

def test_simple_copy(source):
    s, path, state, _, out = source
    original = path.read_bytes()
    result = s.open_project(str(path))
    p = s.projects[result['project_id']]
    assert result['name'] == 'Original' and result['revision'] == 1 and result['saved_revision'] is None
    assert result['imported'] == dict(source_path=str(path), source_sha256=hashlib.sha256(original).hexdigest(),
        source_size_bytes=len(original), schema_version=1, copy_on_import=True)
    assert result['counts'] == dict(assets=1,video_clips=1,audio_items=0,subtitle_tracks=0,cropped_clips=0)
    assert io.serialize_project(p.state) == io.serialize_project(state)
    assert p.state.clips[0].asset is p.state.assets[0]
    assert p.identities.keys() == {'asset-one'}
    assert path.read_bytes() == original and list((out/p.dirname).iterdir()) == []

def test_real_server_schema_accepts_imported_state(source):
    from cove_video_editor.mcp_server import build_server
    s,path,_,_,_=source; rich_state(source)
    server=build_server(s); result=s.open_project(str(path))
    for name,payload in [('open_project',result),('get_project',s.get_project(result['project_id'])),
                         ('get_capabilities',s.capabilities())]:
        tool=server._tool_manager.get_tool('cove_'+name)
        assert tool.fn_metadata.convert_result(payload).structured_content==payload

def test_mcp_direct_server_dispatch(source):
    import anyio
    from cove_video_editor.mcp_server import build_server
    async def run():
        with anyio.fail_after(10):
            result=await build_server(source[0]).call_tool('cove_open_project',{'path':str(source[1])})
        assert result.structured_content['revision']==1
    anyio.run(run)

def test_output_models_have_no_new_warnings(source):
    from cove_video_editor.mcp_server import build_server
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        server=build_server(source[0])
    assert not [w for w in caught if issubclass(w.category,UserWarning)], caught
    result=source[0].open_project(str(source[1]))
    assert result['imported']['copy_on_import'] is True
    server._tool_manager.get_tool('cove_open_project').fn_metadata.convert_result(result)

def test_mcp_open_behavioral_red(source):
    import anyio
    from mcp.client import Client
    from mcp.client._memory import InMemoryTransport
    from cove_video_editor.mcp_server import build_server
    async def run():
        with anyio.fail_after(12):
            await protocol()
    async def protocol():
        async with Client(InMemoryTransport(build_server(source[0])), mode='legacy', read_timeout_seconds=5) as connected:
            client = connected.session
            await client.list_tools()
            result = await client.call_tool('cove_open_project', {'path':str(source[1])})
            assert not result.is_error, result
            assert result.structured_content['revision'] == 1
    anyio.run(run)

def refusal(source, code, path=None):
    s, original, _, _, out = source
    before = (dict(s.projects), sorted(out.iterdir()), len(os.listdir('/proc/self/fd')), original.read_bytes())
    with rejected(code) as exc: s.open_project(str(path or original))
    assert dict(s.projects) == before[0] and sorted(out.iterdir()) == before[1]
    assert len(os.listdir('/proc/self/fd')) == before[2] and original.read_bytes() == before[3]
    assert len(exc.value.message) <= 240
    assert exc.value.message.endswith('Nothing was changed.')
    return exc.value

def rich_state(source):
    _, _, state, media, _ = source
    state = copy.deepcopy(state)
    c = state.clips[0]
    c.speed, c.muted, c.audio_volume = 2, True, 0.4
    c.linked_audio, c.audio_removed, c.audio_offset = False, True, -0.2
    c.crop_rect, c.crop_preset = (0.5,0,0.5,1), 'Right half'
    state.clips.append(Clip(state.assets[0],src_start=0.5,src_end=2.5,timeline_start=1.5,
                            audio_volume=0.7,id='clip-two'))
    path = tones(media/'music.wav'); duration = probe_audio_duration(path)
    state.added_audios = [AddedAudio(path, duration, rate=120, offset=0.2,lane=3,src_start=0.4,
        src_end=2.4,volume=0.6,muted=False,id='audio-one'), AddedAudio(path, duration, rate=100,
        offset=0.5,lane=6,src_start=1,src_end=2,volume=0.8,muted=True,id='audio-two')]
    state.original_gain, state.added_gain, state.replace_added_audio = 0.3, 1.25, True
    subtitle = media/'captions.srt'; subtitle.write_text(SRT,encoding='utf-8')
    inactive = media/'inactive.vtt'; inactive.write_text('WEBVTT\n\n'+SRT.replace(',','.'),encoding='utf-8')
    state.subtitles = [SubtitleTrack(subtitle,font_family='DejaVu Sans',font_size=30,
        primary_color='#FF0000',outline_color='#123456',outline=3,position='top',active=True,
        offset_ms=100,id='sub-one'), SubtitleTrack(inactive,active=False,id='sub-two')]
    save(source,state)
    return state

def test_all_state_edit_save_reload_and_repeat(source):
    s, original, _, _, out = source
    state = rich_state(source); digest = hashlib.sha256(original.read_bytes()).hexdigest()
    opened = s.open_project(str(original),name='Imported film'); pid = opened['project_id']
    p = s.projects[pid]
    assert io.serialize_project(p.state) == io.serialize_project(state)
    assert all(c.asset is p.state.assets[0] for c in p.state.clips)
    assert opened['counts'] == dict(assets=1,video_clips=2,audio_items=2,subtitle_tracks=2,cropped_clips=1)
    assert opened['crops'] == [dict(clip_id='clip-one',crop=dict(x=0.5,y=0,width=0.5,height=1))]
    assert p.audio_identities.keys() == {'audio-one','audio-two'}
    for track in p.state.subtitles:
        assert track.cues == [(0.8,1.6,'Cove import')]
        snapshot = p.subtitle_snapshots[track.id]
        assert snapshot['cue_count'] == 1 and snapshot['first_cue_s'] == 0.8 and snapshot['last_cue_s'] == 1.6
        assert snapshot['sha256'] == hashlib.sha256(track.path.read_bytes()).hexdigest()
        assert snapshot['size_bytes'] == track.path.stat().st_size and snapshot['skipped_cue_blocks'] == 0
    summary = s.set_audio_mix(pid,added_gain=1.5)
    state.added_gain = 1.5
    assert summary['revision'] == 2 and summary['audio'][1]['mute'] and summary['audio'][1]['volume'] == 0.8
    assert p.state.subtitles[0].cues == [(0.8,1.6,'Cove import')]
    try: saved = s.save_project(pid)
    except ap.AgentError as exc:
        raise AssertionError('A validated imported copy must save a new owned revision: '+exc.code) from exc
    assert io.serialize_project(io.load_project(Path(saved['path']))) == io.serialize_project(state)
    second = s.open_project(str(original)); other = s.projects[second['project_id']]
    assert second['project_id'] != pid and p.dirname != other.dirname and p.fd != other.fd
    assert other.state is not p.state and other.state.clips[0] is not p.state.clips[0]
    s.set_crop(pid,'clip-one',2,None)
    assert other.state.clips[0].crop_rect == (0.5,0,0.5,1)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert len(list(out.iterdir())) == 2 and not list((out/other.dirname).iterdir())

@pytest.mark.parametrize('case,code', [('no-roots','NOT_CONFIGURED'),('outside','PATH_NOT_AUTHORIZED'),
    ('prefix','PATH_NOT_AUTHORIZED'),('parent','INVALID_ARGUMENT'),('file-link','PATH_NOT_AUTHORIZED'),
    ('parent-link','PATH_NOT_AUTHORIZED'),('missing','FILE_NOT_FOUND'),('directory','NOT_A_REGULAR_FILE'),
    ('fifo','NOT_A_REGULAR_FILE'),('extension','UNSUPPORTED_EXTENSION')])
def test_project_path_security(source,tmp_path,case,code):
    s,path,_,_,out=source; candidate=path
    if case=='no-roots': s.policy.project_roots=()
    elif case in ('outside','prefix'):
        parent = out if case=='outside' else tmp_path/'projects-evil'; parent.mkdir(exist_ok=True)
        candidate=parent/'outside.coveproj'; candidate.write_bytes(path.read_bytes())
    elif case=='parent': candidate=str(path.parent)+'/../projects/Original.coveproj'
    elif case in ('file-link','parent-link'):
        external=tmp_path/'external'; external.mkdir(); target=external/'file.coveproj'; target.write_bytes(path.read_bytes())
        candidate=path.parent/('link.coveproj' if case=='file-link' else 'linked')
        candidate.symlink_to(target if case=='file-link' else external)
        if case=='parent-link': candidate=candidate/'file.coveproj'
    elif case=='missing': candidate=path.parent/'missing.coveproj'
    elif case=='directory': candidate=path.parent/'directory.coveproj'; candidate.mkdir()
    elif case=='fifo': candidate=path.parent/'fifo.coveproj'; os.mkfifo(candidate)
    elif case=='extension': candidate=path.with_suffix('.json'); candidate.write_bytes(path.read_bytes())
    exc=refusal(source,code,candidate)
    if case=='no-roots': assert '--project-root' in exc.message
    if code=='PATH_NOT_AUTHORIZED': assert 'project_roots' in exc.message and 'media_roots' not in exc.message

@pytest.mark.parametrize('payload', [b'not JSON',b'\xff',b'\xef\xbb\xbf{}',b'{"x":NaN}',
    b'{"x":Infinity}',b'{"x":-Infinity}',b'{"x":1e999}',b'{"x":1,"x":2}',
    b'['*2000+b'0'+b']'*2000,b'['*65+b'0'+b']'*65,b'{}',b'[]'])
def test_strict_project_bytes(source,payload):
    source[1].write_bytes(payload)
    refusal(source,'INVALID_PROJECT')

@pytest.mark.parametrize('change', [{'format':'wrong'},{'version':True},{'version':'1'},
    {'assets':'list'},{'clips':[None]},{'audio_mix':{'added_gain':'1'}},
    {'audio_mix':{'original_gain':math.inf}}])
def test_invalid_fields(source,change):
    save(source,doc=io.serialize_project(source[2])|change)
    refusal(source,'INVALID_PROJECT')

def test_unsupported_version_message(source):
    save(source,doc=io.serialize_project(source[2])|{'version':987})
    exc=refusal(source,'INVALID_PROJECT')
    assert '987' in exc.message and 'supported version is 1' in exc.message

@pytest.mark.parametrize('size,code', [(4194304,None),(4194305,'LIMIT_EXCEEDED')])
def test_project_byte_boundary(source,size,code):
    data=source[1].read_bytes(); source[1].write_bytes(data+b' '*(size-len(data)))
    if code: refusal(source,code)
    else:
        try: result=source[0].open_project(str(source[1]))
        except ap.AgentError as exc:
            raise AssertionError('A project exactly at the byte cap must be accepted: '+exc.code) from exc
        assert result['imported']['source_size_bytes']==size

@pytest.mark.parametrize('collection,maximum', [('assets',200),('clips',200),('added_audio',50),('subtitles',8)])
def test_collection_caps_before_media_open(source,monkeypatch,collection,maximum):
    state=rich_state(source); doc=io.serialize_project(state)
    doc[collection]=[dict(doc[collection][0],id=f'item-{i}') for i in range(maximum+1)]
    save(source,doc=doc)
    monkeypatch.setattr(ap,'open_authorized_media',lambda *a,**k:pytest.fail('reference opened before limit refusal'))
    refusal(source,'LIMIT_EXCEEDED')

@pytest.mark.parametrize('collection,index', [('assets',0),('added_audio',0),('subtitles',0),('subtitles',1)])
def test_each_reference_authorized_before_any_open(source,monkeypatch,tmp_path,collection,index):
    state=rich_state(source); doc=io.serialize_project(state)
    suffix=Path(doc[collection][index]['path']).suffix
    outside=tmp_path/f'unauthorized{suffix}'; outside.write_bytes(b'must not read')
    doc[collection][index]['path']=str(outside); save(source,doc=doc)
    def forbidden(*a,**k):
        raise AssertionError('No reference may be opened/probed before ALL references are authorized')
    monkeypatch.setattr(ap,'open_authorized_media',forbidden)
    monkeypatch.setattr(ap,'probe_fd',forbidden)
    exc=refusal(source,'PATH_NOT_AUTHORIZED')
    assert f'{collection}[{index}].path' in exc.message and 'media_roots' in exc.message

@pytest.mark.parametrize('raw', ['relative.mp4','../clip.mp4','http://localhost/clip.mp4','file:///tmp/clip.mp4',
    'C:/clip.mp4','/tmp/a\\b.mp4','/tmp/a/../b.mp4','/tmp/./a.mp4','/tmp//a.mp4','/tmp/a.mp4/','//tmp/a.mp4'])
def test_reference_form_never_normalized_by_pathlib(source,monkeypatch,raw):
    doc=io.serialize_project(source[2]); doc['assets'][0]['path']=raw; save(source,doc=doc)
    monkeypatch.setattr(ap,'open_authorized_media',lambda *a,**k:pytest.fail('invalid reference opened'))
    assert 'assets[0].path' in refusal(source,'PROJECT_REFERENCE_INVALID').message

def test_missing_reference(source):
    doc=io.serialize_project(source[2]); doc['assets'][0]['path']=str(source[3]/'missing.mp4'); save(source,doc=doc)
    assert 'assets[0].path' in refusal(source,'FILE_NOT_FOUND').message

@pytest.mark.parametrize('kind', ['image','audio'])
def test_unsupported_asset_kind_before_open(source,monkeypatch,kind):
    doc=io.serialize_project(source[2]); doc['assets'][0]['kind']=kind; doc['clips']=[]; save(source,doc=doc)
    monkeypatch.setattr(ap,'open_authorized_media',lambda *a,**k:pytest.fail('unsupported feature opened'))
    assert 'assets[0].kind' in refusal(source,'UNSUPPORTED_PROJECT_FEATURE').message

def test_symlink_inside_canonicalized_and_outside_refused(source,tmp_path):
    s,path,state,media,_=source; link=media/'link.mp4'; link.symlink_to(state.assets[0].path)
    doc=io.serialize_project(state); doc['assets'][0]['path']=str(link); save(source,doc=doc)
    opened=s.open_project(str(path)); assert 'Canonicalized 1' in opened['warnings'][0]
    assert s.projects[opened['project_id']].state.assets[0].path==state.assets[0].path
    link.unlink(); outside=tmp_path/'outside.mp4'; outside.write_bytes(b'outside'); link.symlink_to(outside)
    refusal(source,'PATH_NOT_AUTHORIZED')

def test_swap_reference_after_authorization(source,monkeypatch,tmp_path):
    s,_,state,media,_=source
    external=tmp_path/'outside.mp4'; external.write_bytes(b'not authorized')
    def hook(stage):
        if stage=='authorized':
            state.assets[0].path.unlink(); state.assets[0].path.symlink_to(external)
    monkeypatch.setattr(mod,'_import_hook',hook)
    monkeypatch.setattr(ap,'probe_fd',lambda *a,**k:pytest.fail('swapped unauthorized source probed'))
    refusal(source,'PATH_NOT_AUTHORIZED')

def test_project_metadata_cannot_grant_roots(source,tmp_path,monkeypatch):
    doc=io.serialize_project(source[2]); doc['media_roots']=[str(tmp_path)]; doc['options']={'protocol_whitelist':'all'}
    doc['assets'][0]['path']=str(tmp_path/'outside.mp4'); save(source,doc=doc)
    monkeypatch.setattr(ap,'open_authorized_media',lambda *a,**k:pytest.fail('metadata granted authority'))
    refusal(source,'PATH_NOT_AUTHORIZED')

@pytest.mark.parametrize('field,value', [('duration',3.01),('width',322),('height',242),('fps',24.99),('has_audio',False)])
def test_metadata_mismatch(source,field,value):
    doc=io.serialize_project(source[2]); doc['assets'][0][field]=value; save(source,doc=doc)
    assert f'assets[0].{field}' in refusal(source,'PROJECT_REFERENCE_INVALID').message

@pytest.mark.parametrize('kind,field', [('video','duration'),('video','fps'),('audio','duration')])
@pytest.mark.parametrize('delta,accepted', [(0.000999,True),(0.001,True),(-0.001,True),
                                         (0.001001,False),(-0.001001,False)])
def test_measured_tolerance_boundary(source,kind,field,delta,accepted):
    s,path,state,_,_=source
    if kind=='audio': state=rich_state(source)
    doc=io.serialize_project(state); key='assets' if kind=='video' else 'added_audio'
    doc[key][0][field]+=delta; save(source,doc=doc)
    if accepted:
        opened=s.open_project(str(path))
        assert io.serialize_project(s.projects[opened['project_id']].state)==doc
    else: assert f'{key}[0].{field}' in refusal(source,'PROJECT_REFERENCE_INVALID').message

def test_tolerance_uses_existing_time_epsilon(source):
    from cove_video_editor import agent_imports as ai
    assert ai.DURATION_TOLERANCE==io._TIME_EPS==0.001 and ai.FPS_TOLERANCE==0.001

def test_clip_source_end_checked_against_probe(source):
    doc=io.serialize_project(source[2]); doc['assets'][0]['duration']=3.0009
    doc['clips'][0]['src_end']=3.001001
    save(source,doc=doc)
    assert 'clips[0].src_end' in refusal(source,'PROJECT_REFERENCE_INVALID').message

@pytest.mark.parametrize('feature', ['overlap','clip-end','audio-end','empty-trim','id','font','preset','zero-width'])
def test_agent_invariants_before_open(source,monkeypatch,feature):
    state=rich_state(source); doc=io.serialize_project(state); code='INVALID_PROJECT'
    if feature=='overlap': doc['clips'][1]['timeline_start']=0.5; code='UNSUPPORTED_PROJECT_FEATURE'
    elif feature=='clip-end': doc['clips'][1]['timeline_start']=86400; code='LIMIT_EXCEEDED'
    elif feature=='audio-end': doc['added_audio'][0]['offset']=86400; code='LIMIT_EXCEEDED'
    elif feature=='empty-trim': doc['clips'][0]['src_start']=doc['clips'][0]['src_end']
    elif feature=='id': doc['assets'][0]['id']='x'*81; doc['clips'][0]['asset_id']=doc['clips'][1]['asset_id']='x'*81
    elif feature=='font': doc['subtitles'][0]['font_family']='Arial\n[Events]'
    elif feature=='preset': doc['clips'][0]['crop_preset']='x'*81
    elif feature=='zero-width': doc['assets'][0]['width']=0
    save(source,doc=doc)
    monkeypatch.setattr(ap,'open_authorized_media',lambda *a,**k:pytest.fail('invalid state opened media'))
    refusal(source,code)

@pytest.mark.parametrize('active', [True,False])
@pytest.mark.parametrize('content,code', [(b'no cues','INVALID_SUBTITLE'),(b'\xff','INVALID_SUBTITLE'),
    (b'x'*1048577,'LIMIT_EXCEEDED'),(SRT.replace('Cove import','x'*1001).encode(),'LIMIT_EXCEEDED'),
    ((SRT+'\n').encode()*5001,'LIMIT_EXCEEDED')],ids=['empty','utf8','bytes','cue-chars','cue-count'])
def test_subtitle_snapshot_rules_all_tracks(source,active,content,code):
    state=rich_state(source); state.subtitles=state.subtitles[:1]; state.subtitles[0].active=active
    state.subtitles[0].path.write_bytes(content); save(source,state)
    assert 'subtitles[0].path' in refusal(source,code).message

def test_warning_skipped_cues_and_late_audio(source):
    state=rich_state(source)
    state.subtitles[0].path.write_text(SRT+'\n2\n00:00:03,000 --> 00:00:02,000\nbad\n')
    state.added_audios[0].offset=10; save(source,state)
    result=source[0].open_project(str(source[1]))
    assert any('skipped 1' in w for w in result['warnings'])
    assert any('at/after timeline end' in w for w in result['warnings'])

@pytest.mark.parametrize('replaced', [False,True])
def test_failure_after_allocation_cleans_only_owned_directory(source,monkeypatch,replaced):
    s,_,_,_,out=source; before=len(os.listdir('/proc/self/fd')); captured={}
    def hook(stage):
        if stage=='allocated':
            directory=next(out.iterdir()); captured['directory']=directory
            if replaced:
                directory.rename(out/'owned-moved'); directory.mkdir(); (directory/'keep').write_text('user')
            raise ap.AgentError('INTERNAL_ERROR','injected after allocation')
    monkeypatch.setattr(mod,'_import_hook',hook)
    with rejected('INTERNAL_ERROR'): s.open_project(str(source[1]))
    assert s.projects=={} and len(os.listdir('/proc/self/fd'))==before
    if replaced:
        assert (captured['directory']/'keep').read_text()=='user'
        assert (out/'owned-moved').is_dir()
    else: assert list(out.iterdir())==[]

def test_concurrent_import_at_project_limit(source,monkeypatch):
    s,path,_,_,out=source; monkeypatch.setattr(ap,'MAX_PROJECTS',1)
    barrier=threading.Barrier(2); results=[]
    def hook(stage):
        if stage=='authorized': barrier.wait(5)
    monkeypatch.setattr(mod,'_import_hook',hook)
    def worker():
        try: results.append(s.open_project(str(path)))
        except ap.AgentError as exc: results.append(exc.code)
    threads=[threading.Thread(target=worker) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(10); assert not t.is_alive()
    assert len(s.projects)==1 and len(list(out.iterdir()))==1
    assert sum(isinstance(r,dict) for r in results)==1 and 'LIMIT_EXCEEDED' in results

def test_failed_import_does_not_consume_project_limit(source,monkeypatch):
    monkeypatch.setattr(ap,'MAX_PROJECTS',1); original=source[1].read_bytes()
    source[1].write_bytes(b'bad'); refusal(source,'INVALID_PROJECT'); source[1].write_bytes(original)
    assert source[0].open_project(str(source[1]))['revision']==1
    refusal(source,'LIMIT_EXCEEDED')

def test_disguised_container(source):
    source[2].assets[0].path.write_text('ffconcat version 1.0\nfile /outside.mp4\n')
    refusal(source,'PROBE_FAILED')

def test_imported_export_snapshot_subtitles_and_original_hash(source,monkeypatch):
    from cove_video_editor import agent_exports as ae
    s,path,_,_,_=source; rich_state(source); original=hashlib.sha256(path.read_bytes()).hexdigest()
    pid=s.open_project(str(path))['project_id']
    with rejected('PROJECT_NOT_SAVED'): s.start_export(pid,'import.mp4')
    saved=s.save_project(pid); entered,release=block(monkeypatch,'before_run')
    started=s.start_export(pid,'import.mp4')
    try:
        assert entered.wait(5)
        snapshot=s.exports._get(started['job_id']).render
        s.set_subtitles(pid,[]); s.set_crop(pid,'clip-one',2,None)
    finally: release.set()
    result=finish(s,started)
    assert result['saved_sha256']==saved['sha256'] and result['saved_revision']==1
    assert result['output']['width']==160 and result['output']['height']==240
    # Red text on a red source needs a dark outline to be observable: compare
    # subtitle interval vs adjacent source frame in the rendered TOP region.
    def frame(at):
        return subprocess.run(['ffmpeg','-v','error','-ss',str(at),'-i',result['output']['path'],
            '-frames:v','1','-vf','crop=160:90:0:0','-f','rawvideo','-pix_fmt','rgb24','-'],
            check=True,capture_output=True,timeout=15).stdout
    assert frame(0.2)!=frame(1.0), 'subtitle cues were not burned in'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==original
    with rejected('PROJECT_NOT_SAVED'): s.start_export(pid,'unsaved.mp4')

@pytest.mark.parametrize('kind', ['video','audio','subtitle'])
def test_imported_source_change_refused_at_save_and_export(source,kind):
    s,path,_,_,_=source; state=rich_state(source); pid=s.open_project(str(path))['project_id']; s.save_project(pid)
    target={'video':state.assets[0].path,'audio':state.added_audios[0].path,'subtitle':state.subtitles[1].path}[kind]
    target.write_bytes(target.read_bytes()+b'changed')
    with rejected('ASSET_CHANGED'): s.save_project(pid)
    with rejected('ASSET_CHANGED'): s.start_export(pid,'changed.mp4')

def test_imported_immutable_revision_and_output(source):
    s,path,_,_,_=source; original=path.read_bytes(); pid=s.open_project(str(path))['project_id']
    p=s.projects[pid]; collision=Path(s.policy.output_root)/p.dirname/f'{p.slug}.r0001.coveproj'
    collision.write_bytes(b'owner')
    with rejected('DESTINATION_EXISTS'): s.save_project(pid)
    assert collision.read_bytes()==b'owner' and path.read_bytes()==original
    collision.unlink(); saved=s.save_project(pid)
    assert s.save_project(pid)==saved
    Path(saved['path']).write_bytes(b'tampered')
    with rejected('PROJECT_CHANGED_ON_DISK'): s.save_project(pid)
    with rejected('PROJECT_CHANGED_ON_DISK'): s.start_export(pid,'tamper.mp4')
    assert path.read_bytes()==original

def test_imported_audio_gain_applied_once(source):
    s,path,state,media,_=source
    state.clips[0].src_start=0; state.clips[0].src_end=3
    music=tones(media/'tone.wav'); state.added_audios=[AddedAudio(music,probe_audio_duration(music),
        src_start=0,src_end=3,volume=0.5,id='music')]
    state.replace_added_audio=True; state.added_gain=1.5; save(source,state)
    pid=s.open_project(str(path))['project_id']; s.save_project(pid)
    result=finish(s,s.start_export(pid,'gain.mp4'))
    ratio=amplitude(pcm(Path(result['output']['path'])),660)/amplitude(pcm(music),660)
    assert ratio==pytest.approx(0.75,abs=0.06)

def test_real_stdio_open_save_export_and_output_schema(source):
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    s,path,state,media,out=source; original=path.read_bytes()
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-m','cove_video_editor.mcp_server',
            '--project-root',str(path.parent),'--media-root',str(media),'--output-root',str(out)],
            env=dict(os.environ,PYTHONPATH=str(Path('src').resolve())))
        with anyio.fail_after(25):
            async with stdio_client(params) as streams, ClientSession(*streams,read_timeout_seconds=10) as client:
                await client.initialize(); tools=(await client.list_tools()).tools
                assert len(tools)==15
                async def call(operation,/,**args):
                    result=await client.call_tool('cove_'+operation,args)
                    assert not result.is_error,result
                    return result.structured_content
                caps=await call('get_capabilities'); assert caps['project_roots']==[str(path.parent)]
                for bad in ({'path':'relative.coveproj'}, {'path':True}, {'name':True},
                            {'name':'x'*81}, {'media_roots':[str(out)]}, {'project_roots':[str(out)]}):
                    result=await client.call_tool('cove_open_project',dict(path=str(path))|bad)
                    assert result.is_error and result.structured_content['error']['code']=='INVALID_ARGUMENT'
                opened=await call('open_project',path=str(path),name='Stdio imported'); pid=opened['project_id']
                assert opened['revision']==1 and opened['imported']['copy_on_import'] is True
                assert opened['name']=='Stdio imported'
                assert (await call('get_project',project_id=pid))['clips'][0]['clip_id']=='clip-one'
                saved=await call('save_project',project_id=pid)
                assert io.serialize_project(io.load_project(Path(saved['path'])))==io.serialize_project(state)
                started=await call('start_export',project_id=pid,output_name='stdio.mp4')
                for _ in range(100):
                    status=await call('get_export_status',job_id=started['job_id'])
                    if status['state'] in ('SUCCEEDED','FAILED','CANCELLED'):break
                    await anyio.sleep(0.05)
                assert status['state']=='SUCCEEDED',status
                assert Path(status['output']['path']).is_file()
    anyio.run(run)
    assert path.read_bytes()==original

def test_valid_document_duplicate_key_is_refused(source):
    payload=json.dumps(io.serialize_project(source[2]))
    source[1].write_text(payload.replace('"version": 1','"version": 987, "version": 1'))
    refusal(source,'INVALID_PROJECT')

@pytest.mark.parametrize('value', [math.nan,math.inf,-math.inf],ids=['nan','inf','minus-inf'])
def test_nonfinite_unknown_metadata_is_still_refused(source,value):
    save(source,doc=io.serialize_project(source[2])|{'metadata':value})
    refusal(source,'INVALID_PROJECT')

def test_valid_document_depth_is_bounded(source):
    nested=0
    for _ in range(65): nested=[nested]
    save(source,doc=io.serialize_project(source[2])|{'metadata':nested})
    refusal(source,'INVALID_PROJECT')

# Baseline 73570a33 schemas, canonical JSON sort_keys=True / separators=(",",":").
# A future intentional schema change must explicitly update these digests.
SCHEMA_DIGESTS = {
    'cove_get_capabilities': ('7e0fb18f66e985bf85af787c936a4d80eea42cb740e098e36aa2917272c6ddf9', 'de5067e5e8ce43f69460a67ec2a91f6bfe3e08d959e0fe00d945e88f19f7942d'),
    'cove_inspect_media': ('c58d7d94d50c7ca48ed1494a1243e274a14611f7f8c0e76782f723a89827014f', '1cc3c600689a177bf66944a099c3a34746aceb1b67e0ee527dd750cf5e58bcd6'),
    'cove_create_project': ('f3677070f9faaa736fa7e03b67fed37f2ce1d7d27efbdaa824d2f9db1013e637', 'b34f08958bf625db07ff1b849f0808895937c5386d11df795e8d58d255a22bc0'),
    'cove_import_media': ('162b19170aa31d712b0adf0dde195cf463581bee3701eae77688c18e309f7f33', '2ff9d6b21dd4472f4e8787672eadac522e24c004d15cc2944fc46575d02f6af0'),
    'cove_set_timeline': ('b99149e9f251df5a21f76b01c7d380d2350dbba697faebb2607ef40a8aed4c73', 'ad38a84a624d5bae062c93f6fe008631aeb73d5d1764f576f55dcd9ed045cd3d'),
    'cove_set_audio': ('13ee0ac5494b46ef936033b154fc528f86b4f2a8ae7a3502de1357196b97aedd', 'ad38a84a624d5bae062c93f6fe008631aeb73d5d1764f576f55dcd9ed045cd3d'),
    'cove_set_audio_mix': ('eeec62ba1c0567555564a6279cbcb3ca10e77fb2e1d143f38835aeb1fc590c75', 'ad38a84a624d5bae062c93f6fe008631aeb73d5d1764f576f55dcd9ed045cd3d'),
    'cove_set_subtitles': ('dd0d768a017e99fd6508fa055727b5fd3a26d24ddcdc2e924d1464bf515ffa7c', 'ad38a84a624d5bae062c93f6fe008631aeb73d5d1764f576f55dcd9ed045cd3d'),
    'cove_set_crop': ('c82d51b14a0f5562c00ade760ba5f2a0970c97838880409ca3ddf087848dd1da', 'ad38a84a624d5bae062c93f6fe008631aeb73d5d1764f576f55dcd9ed045cd3d'),
    'cove_get_project': ('48844a08cd8ec826b0b4a4ee0a098d575635fb0f9b1133a8f424f3053afea414', 'ad38a84a624d5bae062c93f6fe008631aeb73d5d1764f576f55dcd9ed045cd3d'),
    'cove_save_project': ('c8c72ae7a19a87f4a9c706f79a67753dee0d59d58ad6898547c4741b94d0d9e6', 'e08b79579effe30df765a5e04033208cde4098114ca87f0f9d92e013ad8e4797'),
    'cove_start_export': ('e4c3d4d5455e862ab26d63a8bd9ce41a215428cb9948bf66cc6a4f206fd1ded5', 'b1711ad2695f7deeb830dbc46bdede8dff2c00d6003d71a1ad427f2bd890c467'),
    'cove_get_export_status': ('3e4aede9e6bc0cd97a517e59c9359588bf0ff6a02c2164559f063f546ebdeea1', 'c1722b45112f39d1690eb842382e4c624d772ee1917bd79ceb9f1c04065f0fd5'),
    'cove_cancel_export': ('e824aa0ff2ae4d2e29cbd559c62b1834314529a064888131e44307a8f6c82f26', 'c1722b45112f39d1690eb842382e4c624d772ee1917bd79ceb9f1c04065f0fd5'),
}
def test_all_existing_tool_schemas_unchanged_except_capabilities(source):
    from cove_video_editor.mcp_server import build_server
    tools={t.name:t for t in build_server(source[0])._tool_manager.list_tools()}
    assert tools.keys()==SCHEMA_DIGESTS.keys() | {'cove_open_project'}
    def digest(schema):
        return hashlib.sha256(json.dumps(schema,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    for name,(input_sha,output_sha) in SCHEMA_DIGESTS.items():
        tool=tools[name]; assert digest(tool.parameters)==input_sha,name
        schema=copy.deepcopy(tool.output_schema)
        if name=='cove_get_capabilities':
            # Assert exactly the authorized capabilities extension, then pin
            # all remaining fields/constraints to the baseline output digest.
            assert schema['properties'].pop('project_roots')==dict(
                items=dict(maxLength=4096,type='string'),title='Project Roots',type='array')
            schema['required'].remove('project_roots')
            for field,old,new in [('operations',14,15),('supported',7,8),('deferred',1,0)]:
                assert schema['properties'][field]['maxItems']==new
                schema['properties'][field]['maxItems']=old
        assert digest(schema)==output_sha,name

def test_project_roots_repeatable_and_read_only_cli(source,monkeypatch,capsys):
    from cove_video_editor import mcp_server as server
    s,path,_,media,out=source; captured=[]
    class Stub:
        def run(self,**kwargs): pass
    def build(session): captured.append(session.policy); return Stub()
    monkeypatch.setattr(server,'build_server',build)
    assert server.main(['--project-root',str(path.parent),'--project-root',str(out),
        '--project-root',str(path.parent),'--media-root',str(media),'--output-root',str(out)])==0
    assert captured[0].project_roots==(str(path.parent),str(out))
    assert captured[0].media_roots==(str(media),) and not capsys.readouterr().err
    for raw in ('relative','/',str(path),str(out/'missing')):
        assert server.main(['--project-root',raw])==2
        assert '--project-root' in capsys.readouterr().err
    assert ap.PathPolicy((),None,None).project_roots==()

def test_output_root_does_not_authorize_projects(source):
    s,path,_,_,out=source; copy_path=out/'output.coveproj'; copy_path.write_bytes(path.read_bytes())
    s.policy.project_roots=()
    refusal(source,'NOT_CONFIGURED',copy_path)

def test_empty_media_roots_do_not_inherit_project_root(source):
    source[0].policy.media_roots=()
    assert 'assets[0].path' in refusal(source,'NOT_CONFIGURED').message

def test_media_authorization_pass_performs_no_opens(source,monkeypatch):
    state=rich_state(source); real_open=ap.os.open
    def denied(*args,**kwargs): pytest.fail('authorization opened a reference')
    monkeypatch.setattr(ap.os,'open',denied)
    for kind,field,path in __import__('cove_video_editor.agent_imports',fromlist=['enumerate_references']).enumerate_references(state):
        assert ap.authorize_media_path(source[0].policy,path,kind)==path
    monkeypatch.setattr(ap.os,'open',real_open)

def test_project_fd_flags_make_fifo_nonblocking(source,monkeypatch):
    path=source[1].parent/'fifo.coveproj'; os.mkfifo(path)
    real_open=ap.os.open; observed=[]
    def checked(pathname,flags,*args,**kwargs):
        if str(pathname)==str(path):
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC
            observed.append(flags)
        return real_open(pathname,flags,*args,**kwargs)
    monkeypatch.setattr(ap.os,'open',checked)
    refusal(source,'NOT_A_REGULAR_FILE',path)
    assert len(observed)==1

@pytest.mark.parametrize('stage', ['before_open','after_open','read','probe'])
def test_failures_leave_no_descriptors_or_directories(source,monkeypatch,stage):
    if stage in ('before_open','after_open'):
        def hook(current):
            if current==stage: raise ap.AgentError('INTERNAL_ERROR','injected file boundary failure')
        monkeypatch.setattr(ap,'_media_hook',hook)
    else:
        def fail(*a,**k): raise ap.AgentError('INTERNAL_ERROR','injected validation failure')
        monkeypatch.setattr(ap,'read_project_snapshot' if stage=='read' else 'probe_fd',fail)
    refusal(source,'INTERNAL_ERROR')

def test_project_changes_during_snapshot_rejected(source,monkeypatch):
    real_read=ap.os.read; changed=False
    def change(fd,size):
        nonlocal changed
        data=real_read(fd,size)
        if not changed:
            changed=True
            with source[1].open('ab') as stream:stream.write(b' ')
        return data
    monkeypatch.setattr(ap.os,'read',change)
    s,_,_,_,out=source; before=len(os.listdir('/proc/self/fd'))
    with rejected('ASSET_CHANGED'):s.open_project(str(source[1]))
    assert s.projects=={} and list(out.iterdir())==[] and len(os.listdir('/proc/self/fd'))==before

def test_media_changes_while_probing_rejected(source,monkeypatch):
    real_probe=ap.probe_fd
    def change(fd,**options):
        info=real_probe(fd,**options)
        with source[2].assets[0].path.open('ab') as stream:stream.write(b'changed')
        return info
    monkeypatch.setattr(ap,'probe_fd',change)
    refusal(source,'ASSET_CHANGED')

def test_project_parent_swap_after_open_recontained(source,monkeypatch,tmp_path):
    s,path,_,_,_=source; old=path.parent; moved=tmp_path/'outside-projects'; fired=False
    def swap(stage):
        nonlocal fired
        if stage=='after_open' and not fired:
            fired=True; old.rename(moved); old.mkdir(); path.write_bytes((moved/path.name).read_bytes())
    monkeypatch.setattr(ap,'_media_hook',swap)
    refusal(source,'PATH_NOT_AUTHORIZED')

def test_import_preserves_asset_and_clip_document_order(source):
    s,path,state,media,_=source
    second=video(media/'second.mp4',False,color='blue'); info=probe(second)
    asset=MediaAsset(second,info.duration,info.width,info.height,info.fps,info.has_audio,id='asset-two')
    state.assets.insert(0,asset)
    state.clips.insert(0,Clip(asset,src_start=0.1,src_end=2,timeline_start=4,id='clip-two'))
    save(source,state); pid=s.open_project(str(path))['project_id']
    assert io.serialize_project(s.projects[pid].state)==io.serialize_project(state)
    result=s.get_project(pid); assert result['duration_s']==5.9
    assert [c['clip_id'] for c in result['clips']]==['clip-two','clip-one']

def test_odd_source_crop_validates_real_response_schema(source):
    from test_agent_crop import odd_video
    from cove_video_editor.mcp_server import build_server
    s,path,state,media,_=source; odd=odd_video(media/'odd.mp4'); info=probe(odd)
    a=state.assets[0]; a.path=odd; a.width=info.width; a.height=info.height; a.has_audio=info.has_audio
    a.duration=info.duration; a.fps=info.fps; state.clips[0].crop_rect=(0.5,0,0.5,1)
    save(source,state); opened=s.open_project(str(path)); summary=s.get_project(opened['project_id'])
    assert summary['clips'][0]['crop_pixels']==dict(x=159,y=0,width=160,height=240)
    server=build_server(s)
    server._tool_manager.get_tool('cove_open_project').fn_metadata.convert_result(opened)
    server._tool_manager.get_tool('cove_get_project').fn_metadata.convert_result(summary)

@pytest.mark.parametrize('name', ['',True,'x'*81,'line\nname'])
def test_import_name_strict_and_atomic(source,name):
    s,path,_,_,out=source; before=len(os.listdir('/proc/self/fd')); original=path.read_bytes()
    with rejected('INVALID_ARGUMENT'):s.open_project(str(path),name=name)
    assert s.projects=={} and list(out.iterdir())==[] and path.read_bytes()==original
    assert len(os.listdir('/proc/self/fd'))==before

def test_imported_export_existing_output_not_overwritten(source):
    s,path,_,_,out=source; pid=s.open_project(str(path))['project_id']; s.save_project(pid)
    destination=out/s.projects[pid].dirname/'owned.mp4'; destination.write_bytes(b'owner')
    with rejected('DESTINATION_EXISTS'):s.start_export(pid,'owned.mp4')
    assert destination.read_bytes()==b'owner'

def test_relative_reference_never_resolves_from_cwd(source,monkeypatch):
    doc=io.serialize_project(source[2]); doc['assets'][0]['path']='clip.mp4'; save(source,doc=doc)
    monkeypatch.chdir(source[3])
    refusal(source,'PROJECT_REFERENCE_INVALID')

def test_imported_mid_export_source_identity_change(source,monkeypatch):
    s,path,state,_,out=source; pid=s.open_project(str(path))['project_id']; s.save_project(pid)
    entered,release=block(monkeypatch,'before_run'); started=s.start_export(pid,'changed.mp4')
    try:
        assert entered.wait(5)
        media=state.assets[0].path; st=media.stat()
        os.utime(media,ns=(st.st_atime_ns,st.st_mtime_ns+1000000))
    finally: release.set()
    result=finish(s,started,'FAILED')
    assert result['error']['code']=='ASSET_CHANGED'
    assert not (out/s.projects[pid].dirname/'changed.mp4').exists()

def test_unreadable_subtitle_snapshot_refused_atomically(source,monkeypatch):
    rich_state(source)
    def denied(*args):raise OSError('synthetic subtitle read error')
    monkeypatch.setattr(ap,'read_subtitle_snapshot',denied)
    refusal(source,'INVALID_SUBTITLE')

def test_reference_parent_symlink_escape_no_open(source,monkeypatch,tmp_path):
    outside=tmp_path/'outside'; outside.mkdir(); (outside/'clip.mp4').write_bytes(b'never open')
    link=source[3]/'linked'; link.symlink_to(outside,target_is_directory=True)
    doc=io.serialize_project(source[2]); doc['assets'][0]['path']=str(link/'clip.mp4'); save(source,doc=doc)
    monkeypatch.setattr(ap,'open_authorized_media',lambda *a,**k:pytest.fail('parent escape opened'))
    refusal(source,'PATH_NOT_AUTHORIZED')

@pytest.mark.parametrize('active',[True,False])
def test_unsupported_subtitle_extension_all_tracks(source,active):
    state=rich_state(source); state.subtitles=state.subtitles[:1]; state.subtitles[0].active=active
    state.subtitles[0].path=source[3]/'captions.ass'; save(source,state)
    assert 'subtitles[0].path' in refusal(source,'UNSUPPORTED_EXTENSION').message

@pytest.mark.parametrize('collection', ['assets','clips','added_audio','subtitles'])
def test_colon_ids_refused_before_import_save_export(source,collection):
    state=rich_state(source); doc=io.serialize_project(state)
    old=doc[collection][0]['id']; doc[collection][0]['id']='audio:music'
    if collection=='assets':
        for clip in doc['clips']:
            if clip['asset_id']==old: clip['asset_id']='audio:music'
    save(source,doc=doc)
    exc=refusal(source,'INVALID_PROJECT')
    assert f'{collection}[0].id' in exc.message and ':' in exc.message
    assert 'Nothing was changed.' in exc.message

def test_export_key_collision_seam_and_import_guard(source):
    from cove_video_editor import exporter as ex
    state=rich_state(source); doc=io.serialize_project(state)
    doc['assets'][0]['id']='audio:music'
    for clip in doc['clips']:clip['asset_id']='audio:music'
    doc['added_audio'][0]['id']='music'; save(source,doc=doc)
    # The pure GUI parser permits these ids. Prove the exporter namespace
    # collision without opening inputs, then prove the agent refuses admission.
    hand_built=io.deserialize_project(doc)
    render=ex.build_export_job(hand_built,output='render.mp4',fmt_key='MP4 (H.264 + AAC)',size=None,encoder_pref='cpu')
    video_keys={c.asset.id for c in render.clips}
    audio_keys={a.input_key for a in render.audio_tracks}
    assert video_keys & audio_keys=={'audio:music'}
    refusal(source,'INVALID_PROJECT')

@pytest.mark.parametrize('fault', ['open','fstat'])
def test_directory_acquisition_failure_is_atomic(source,monkeypatch,fault):
    s,path,_,_,out=source; before=len(os.listdir('/proc/self/fd')); original=path.read_bytes()
    real_open,real_fstat=ap.os.open,ap.os.fstat; acquired=set()
    def opened(name,flags,*args,**kwargs):
        is_project=kwargs.get('dir_fd')==s.policy.output_fd and flags & os.O_DIRECTORY
        if is_project and fault=='open': raise OSError('synthetic directory open failure')
        fd=real_open(name,flags,*args,**kwargs)
        if is_project: acquired.add(fd)
        return fd
    def checked(fd):
        if fault=='fstat' and fd in acquired: raise OSError('synthetic ownership capture failure')
        return real_fstat(fd)
    monkeypatch.setattr(ap.os,'open',opened); monkeypatch.setattr(ap.os,'fstat',checked)
    with rejected('OUTPUT_UNSAFE'):s.open_project(str(path))
    assert s.projects=={} and list(out.iterdir())==[] and len(os.listdir('/proc/self/fd'))==before
    assert path.read_bytes()==original
    monkeypatch.setattr(ap.os,'open',real_open); monkeypatch.setattr(ap.os,'fstat',real_fstat)
    monkeypatch.setattr(ap,'MAX_PROJECTS',1)
    assert s.open_project(str(path))['revision']==1

@pytest.mark.parametrize('root,flag', [('project_roots','--project-root'),('media_roots','--media-root'),('output_fd','--output-root')])
def test_import_configuration_errors_name_operator_and_flag(source,root,flag):
    s=source[0]
    if root=='output_fd':s.policy.close()
    else:setattr(s.policy,root,())
    exc=refusal(source,'NOT_CONFIGURED')
    assert 'operator' in exc.message.lower() and 'restart' in exc.message.lower() and flag in exc.message
    assert exc.message.endswith('Nothing was changed.')

def test_missing_output_root_fails_before_any_probe(source,monkeypatch):
    source[0].policy.close()
    monkeypatch.setattr(ap,'probe_fd',lambda *a,**k:pytest.fail('probe before missing output-root refusal'))
    refusal(source,'NOT_CONFIGURED')

def test_overlap_refusal_explains_next_action(source):
    state=rich_state(source); state.clips[1].timeline_start=0.5; save(source,state)
    exc=refusal(source,'UNSUPPORTED_PROJECT_FEATURE')
    assert 'Remove or replace it in Cove, save, then retry' in exc.message
    assert exc.message.endswith('Nothing was changed.')

@pytest.mark.parametrize('field', ['primary_color','outline_color'])
@pytest.mark.parametrize('color', ['#FFFFFF\n','#a1B2c3\n'],ids=['upper-newline','mixed-newline'])
@pytest.mark.parametrize('active',[True,False])
def test_subtitle_colour_fullmatch_before_any_reference_open(source,monkeypatch,field,color,active):
    state=rich_state(source); state.subtitles=state.subtitles[:1]; state.subtitles[0].active=active
    setattr(state.subtitles[0],field,color); save(source,state)
    def forbidden(*a,**k):
        raise AssertionError('Invalid subtitle colour must be refused before any reference is opened/probed')
    monkeypatch.setattr(ap,'open_authorized_media',forbidden); monkeypatch.setattr(ap,'probe_fd',forbidden)
    monkeypatch.setattr(ap,'MAX_PROJECTS',1)
    exc=refusal(source,'INVALID_PROJECT')
    assert f'subtitles[0].{field}' in exc.message
    assert 'Operator' in exc.message and exc.message.endswith('Nothing was changed.')
    monkeypatch.undo(); setattr(state.subtitles[0],field,'#a1B2c3'); save(source,state)
    monkeypatch.setattr(ap,'MAX_PROJECTS',1)
    assert source[0].open_project(str(source[1]))['revision']==1

@pytest.mark.parametrize('size', [10,120])
@pytest.mark.parametrize('outline', [0,8])
@pytest.mark.parametrize('offset', [-30000,30000])
@pytest.mark.parametrize('position', ['top','bottom'])
def test_valid_style_extremes_validate_real_output_models(source,size,outline,offset,position):
    from cove_video_editor.mcp_server import build_server
    s,path,_,_,_=source; state=rich_state(source)
    state.assets[0].id='a'*80
    for i,c in enumerate(state.clips):c.id='c'*79+str(i)
    for i,a in enumerate(state.added_audios):a.id='m'*79+str(i)
    for i,sub in enumerate(state.subtitles):
        sub.id='s'*79+str(i); sub.font_family='A'*64
        sub.font_size=size; sub.outline=outline; sub.offset_ms=offset; sub.position=position
        sub.primary_color='#a1B2c3'; sub.outline_color='#Ff0011'
    save(source,state); opened=s.open_project(str(path)); summary=s.get_project(opened['project_id'])
    server=build_server(s)
    for operation,payload in [('open_project',opened),('get_project',summary)]:
        tool=server._tool_manager.get_tool('cove_'+operation)
        assert tool.fn_metadata.convert_result(payload).structured_content==payload
    assert io.serialize_project(s.projects[opened['project_id']].state)==io.serialize_project(state)

@pytest.mark.parametrize('kind',['video','audio','subtitle','project'])
def test_canonical_surrogate_paths_cannot_break_output_schema(source,monkeypatch,kind):
    state=rich_state(source)
    original={'video':state.assets[0].path,'audio':state.added_audios[0].path,
              'subtitle':state.subtitles[0].path,'project':source[1]}[kind]
    bad=original.with_name('bad'+chr(0xdc80)+original.suffix)
    original.rename(bad); original.symlink_to(bad)
    def forbidden(*a,**k):
        raise AssertionError('Non-Unicode canonical output path must be refused before any reference open/probe')
    monkeypatch.setattr(ap,'open_authorized_media',forbidden); monkeypatch.setattr(ap,'probe_fd',forbidden)
    monkeypatch.setattr(ap,'MAX_PROJECTS',1)
    refusal(source,'PROJECT_REFERENCE_INVALID')
    original.unlink(); bad.rename(original); monkeypatch.undo()
    monkeypatch.setattr(ap,'MAX_PROJECTS',1)
    opened=source[0].open_project(str(source[1]))
    from cove_video_editor.mcp_server import build_server
    server=build_server(source[0])
    server._tool_manager.get_tool('cove_open_project').fn_metadata.convert_result(opened)
    server._tool_manager.get_tool('cove_get_project').fn_metadata.convert_result(source[0].get_project(opened['project_id']))

def test_raw_surrogate_reference_rejected_before_open(source,monkeypatch):
    doc=io.serialize_project(source[2]); doc['assets'][0]['path']=str(source[3]/('bad'+chr(0xdc80)+'.mp4'))
    save(source,doc=doc)
    def forbidden(*a,**k):raise AssertionError('Invalid Unicode reference was opened')
    monkeypatch.setattr(ap,'open_authorized_media',forbidden)
    refusal(source,'PROJECT_REFERENCE_INVALID')

@pytest.mark.parametrize('field,value', [('position','top\n'),('font_family','Arial\n'),
    ('font_family','\u0661font'),('id','track\n'),('primary_color','#\uff11\uff12\uff13\uff14\uff15\uff16'),
    ('font_size',True),('outline',True),('offset_ms',True)])
def test_subtitle_scalar_audit_refuses_existing_invalid_forms(source,monkeypatch,field,value):
    state=rich_state(source); doc=io.serialize_project(state); doc['subtitles'][0][field]=value; save(source,doc=doc)
    def forbidden(*a,**k):raise AssertionError('Invalid subtitle scalar reached reference I/O')
    monkeypatch.setattr(ap,'open_authorized_media',forbidden)
    refusal(source,'INVALID_PROJECT')
