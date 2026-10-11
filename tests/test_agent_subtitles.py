"""Verified subtitle snapshots and real Linux/libass burn-in oracles."""
import copy, hashlib, json, os, stat, subprocess, sys, tempfile, threading, time, pytest
from pathlib import Path
from test_agent_session import editing
from test_agent_paths import tree, rejected, video
from test_agent_exports import finish
from test_agent_editing import tones, pcm, amplitude
from cove_video_editor import agent_paths as ap, agent_session as mod, exporter as ex, project_io as io
from cove_video_editor.clip import SubtitleTrack, parse_sub_cues

SRT = '1\n00:00:00,800 --> 00:00:01,600\nCove RED\n'
STYLE = dict(font_family='DejaVu Sans', font_size=48, primary_color='#FF0000',
    outline_color='#000000', outline=0, position='bottom', offset_ms=0)
@pytest.fixture
def subtitled(editing):
    s, pid, aid, path = editing
    s.set_timeline(pid, 'sequential', [dict(asset_id=aid, src_start_s=0, src_end_s=3)])
    sub = path.with_suffix('.srt'); sub.write_text(SRT, encoding='utf-8')
    return s, pid, aid, sub
def set_sub(s, pid, path, **style):
    return s.set_subtitles(pid, [dict(path=str(path), **style)])
def test_cues_survive_validated_candidate(subtitled):
    s, pid, _, path = subtitled
    state = s.projects[pid].state
    state.subtitles = [SubtitleTrack(path, cues=[(0.8, 1.6, 'Cove RED')])]
    candidate = mod._validated(state)
    assert candidate.subtitles[0].cues == [(0.8, 1.6, 'Cove RED')]
    assert candidate.subtitles[0].cues is not state.subtitles[0].cues
def test_subtitle_capabilities(subtitled):
    caps = subtitled[0].capabilities()
    assert 'cove_set_subtitles' in caps['operations'] and len(caps['operations']) == 14
    assert caps['subtitle_extensions'] == ['.srt', '.vtt']
    assert caps['deferred'] == ['open_project']
    assert {k: caps['limits'][k] for k in ('subtitle_tracks','subtitle_bytes','subtitle_cues','subtitle_cue_chars')} == dict(
        subtitle_tracks=8, subtitle_bytes=1048576, subtitle_cues=5000, subtitle_cue_chars=1000)
@pytest.mark.parametrize('text,suffix', [(SRT,'.srt'), ('\ufeff'+SRT,'.srt'),
    ('WEBVTT\n\n'+SRT.replace(',', '.'),'.vtt'),
    (SRT.replace('Cove RED', '中文 😀 café\n\\ colon: comma, "quote" 50% {}'),'.srt')])
def test_snapshot_parser_parity_canary(subtitled, text, suffix):
    s, pid, _, path = subtitled; path = path.with_suffix(suffix); path.write_text(text, encoding='utf-8')
    adapter = mod._SubtitleText(text.removeprefix('\ufeff'))
    assert parse_sub_cues(adapter) == parse_sub_cues(path)
    # No delegated Path methods: a future parser API change must fail loudly.
    assert not hasattr(adapter, 'open') and not hasattr(adapter, 'suffix')
    with pytest.raises(TypeError): adapter.read_text(encoding='latin-1', errors='strict')
    result = set_sub(s, pid, path)
    cue = result['subtitles'][0]
    assert cue['active'] is True and cue['cue_count'] == 1 and cue['format'] == suffix[1:]
    assert (cue['first_cue_s'],cue['last_cue_s'],cue['skipped_cue_blocks']) == (0.8,1.6,0)
    snapshot = s.projects[pid].subtitle_snapshots[cue['subtitle_id']]
    assert snapshot['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert snapshot['size_bytes'] == len(path.read_bytes()) and snapshot['cue_count'] == 1
    class Unreadable:
        def read_text(self, *, encoding, errors): raise OSError('fixture')
    assert parse_sub_cues(Unreadable()) == []
def test_replace_clear_atomic_and_style_roundtrip(subtitled):
    s, pid, _, path = subtitled; revision = s.get_project(pid)['revision']
    result = set_sub(s, pid, path, **(STYLE | dict(outline=3,outline_color='#123456',position='top',offset_ms=400)))
    assert result['revision'] == revision+1
    cue = result['subtitles'][0]
    for name, value in STYLE.items():
        if name not in {'outline','outline_color','position','offset_ms'}: assert cue[name] == value
    before, snapshots = copy.deepcopy(result), copy.deepcopy(s.projects[pid].subtitle_snapshots)
    with rejected('FILE_NOT_FOUND') as exc: s.set_subtitles(pid,[dict(path=str(path)),dict(path=str(path.parent/'missing.vtt'),active=False)])
    assert 'tracks[1]' in exc.value.message and s.get_project(pid) == before
    assert s.projects[pid].subtitle_snapshots == snapshots
    saved = s.save_project(pid); loaded = io.load_project(Path(saved['path']))
    track = loaded.subtitles[0]
    assert track.id == cue['subtitle_id'] and track.active
    assert (track.font_family,track.font_size,track.primary_color,track.outline_color,track.outline,track.position,track.offset_ms) == (
        'DejaVu Sans',48,'#FF0000','#123456',3,'top',400)
    assert len(parse_sub_cues(track.path)) == 1 and s.projects[pid].state.subtitles[0].cues == [(0.8,1.6,'Cove RED')]
    result = set_sub(s,pid,path,active=False)
    assert not result['subtitles'][0]['active'] and any('none active' in x for x in result['warnings'])
    assert result['subtitles'][0]['subtitle_id'] != cue['subtitle_id']
    result = s.set_subtitles(pid,[])
    assert result['subtitles'] == [] and s.projects[pid].subtitle_snapshots == {}
@pytest.mark.parametrize('bad', [dict(active=1),dict(font_family='Arial\n[Events]'),dict(font_family='A,B'),
    dict(font_family='A:B'),dict(font_family='A\tB'),dict(font_family='A'*65),dict(font_family=''),
    dict(font_size=True),dict(font_size=9),dict(font_size=121),dict(font_size=float('nan')),
    dict(primary_color='red'),dict(primary_color='#FFFFFF\n'),dict(outline_color='#12345G'),
    dict(outline=True),dict(outline=-1),dict(outline=9),dict(position='center'),dict(position=[]),
    dict(offset_ms=True),dict(offset_ms=-30001),dict(offset_ms=30001),dict(offset_ms=0.5),dict(extra=1)])
def test_strict_style_atomic(subtitled,bad):
    s,pid,_,path = subtitled; set_sub(s,pid,path)
    before = s.get_project(pid); snapshots = copy.deepcopy(s.projects[pid].subtitle_snapshots)
    with rejected('INVALID_ARGUMENT') as exc: set_sub(s,pid,path,**bad)
    assert 'tracks[0]' in exc.value.message and s.get_project(pid) == before
    assert s.projects[pid].subtitle_snapshots == snapshots
@pytest.mark.parametrize('items,code', [(None,'INVALID_ARGUMENT'),({},'INVALID_ARGUMENT'),([{}],'INVALID_ARGUMENT'),
    ([dict(path='x')]*9,'LIMIT_EXCEEDED')])
def test_strict_list(subtitled,items,code):
    s,pid,_,_ = subtitled; before = s.get_project(pid)
    with rejected(code): s.set_subtitles(pid,items)
    assert s.get_project(pid) == before
def test_multiple_active(subtitled):
    s,pid,_,path = subtitled
    with rejected('INVALID_ARGUMENT'): s.set_subtitles(pid,[dict(path=str(path)),dict(path=str(path))])
    result = s.set_subtitles(pid,[dict(path=str(path),active=False),dict(path=str(path))])
    assert [x['active'] for x in result['subtitles']] == [False,True]
def test_defaults_and_limits_inclusive(subtitled):
    s,pid,_,path=subtitled; result=set_sub(s,pid,path)
    cue=result['subtitles'][0]
    assert {k:cue[k] for k in ('active','font_family','font_size','primary_color','outline_color','outline','position','offset_ms')} == dict(
        active=True,font_family='Arial',font_size=36,primary_color='#FFFFFF',outline_color='#000000',outline=2,position='bottom',offset_ms=0)
    result=s.set_subtitles(pid,[dict(path=str(path),active=False,font_size=10,outline=0,offset_ms=-30000),
        *[dict(path=str(path),active=False,font_family='A'*64,font_size=120,outline=8,offset_ms=30000) for _ in range(7)]])
    assert len(result['subtitles'])==8 and len(s.projects[pid].subtitle_snapshots)==8
    path.write_text(SRT.replace('Cove RED','x'*1000)); assert set_sub(s,pid,path)['subtitles'][0]['cue_count']==1
    path.write_text(SRT.replace('00:00:01,600','24:00:00,000'))
    assert set_sub(s,pid,path)['subtitles'][0]['last_cue_s']==86400
def test_binary_invalid_utf8_even_with_valid_cues(subtitled):
    s,pid,_,path=subtitled; path.write_bytes(SRT.encode()+b'\n\xff')
    with rejected('INVALID_SUBTITLE'): set_sub(s,pid,path)
def test_all_forbidden_controls(subtitled):
    s,pid,_,path=subtitled
    for number in [n for n in range(32) if n not in (9,10,13)]+[127]:
        path.write_text(SRT+chr(number))
        with rejected('INVALID_SUBTITLE'): set_sub(s,pid,path)
@pytest.mark.parametrize('content,code', [(b'\xff','INVALID_SUBTITLE'),(b'\x00','INVALID_SUBTITLE'),
    ((SRT+'\x01').encode(),'INVALID_SUBTITLE'),((SRT+'\x7f').encode(),'INVALID_SUBTITLE'),
    (b'no cue','INVALID_SUBTITLE'),(b'1\n00:00:02,000 --> 00:00:01,000\nx','INVALID_SUBTITLE'),
    ((SRT.replace('Cove RED','x'*1001)).encode(),'LIMIT_EXCEEDED'),
    (SRT.replace('00:00:01,600','25:00:00,000').encode(),'LIMIT_EXCEEDED'),
    (b'x'*1048577,'LIMIT_EXCEEDED'),
    ((SRT+'\n') .encode()*5001,'LIMIT_EXCEEDED'),
    (('1\n'+'9'*5000+':00:00,000 --> 00:00:01,000\nx').encode(),'INVALID_SUBTITLE')],
    ids=['utf8','nul','c0','del','empty','timestamps','cue-chars','cue-end','bytes','cue-count','huge-hours'])
def test_content_limits_atomic(subtitled,content,code):
    s,pid,_,path = subtitled; set_sub(s,pid,path); before=s.get_project(pid)
    bad=path.parent/'bad.srt'; bad.write_bytes(content)
    with rejected(code): set_sub(s,pid,bad)
    assert s.get_project(pid)==before
def test_partial_skip_and_outside_window_warning(subtitled):
    s,pid,_,path = subtitled
    path.write_text(SRT+'\n2\nbad --> timestamp\nx\n\n3\n00:00:05,000 --> 00:00:04,000\nx')
    assert set_sub(s,pid,path)['subtitles'][0]['skipped_cue_blocks']==2
    result=set_sub(s,pid,path,offset_ms=30000)
    assert any('no cue' in x for x in result['warnings'])
@pytest.mark.parametrize('kind,code', [('ass','UNSUPPORTED_EXTENSION'),('ssa','UNSUPPORTED_EXTENSION'),
    ('txt','UNSUPPORTED_EXTENSION'),('fifo','NOT_A_REGULAR_FILE'),('directory','NOT_A_REGULAR_FILE'),
    ('outside','PATH_NOT_AUTHORIZED'),('symlink','PATH_NOT_AUTHORIZED'),('relative','INVALID_ARGUMENT'),('missing','FILE_NOT_FOUND')])
def test_subtitle_boundary(subtitled,tmp_path,kind,code):
    s,pid,_,path=subtitled
    target=path.parent/('bad.'+kind if kind in {'ass','ssa','txt'} else 'bad.srt')
    if kind=='fifo': os.mkfifo(target)
    elif kind=='directory': target.mkdir()
    elif kind in {'outside','symlink'}:
        outside=tmp_path/'outside.srt'; outside.write_text(SRT)
        if kind=='symlink': target.symlink_to(outside)
        else: target=outside
    elif kind not in {'relative','missing'}: target.write_text(SRT)
    if kind=='relative': target=Path('relative.srt')
    with rejected(code) as exc: set_sub(s,pid,target)
    assert 'tracks[0]' in exc.value.message
    if kind in {'ass','ssa'}: assert 'parsed SRT/VTT' in exc.value.message
def test_subtitle_path_swap_and_fd_snapshot(subtitled,tmp_path,monkeypatch):
    s,pid,_,path=subtitled; outside=tmp_path/'outside.srt'; outside.write_text(SRT.replace('RED','EVIL'))
    def swap(stage):
        if stage=='before_open': path.unlink(); path.symlink_to(outside)
    monkeypatch.setattr(ap,'_media_hook',swap)
    with rejected('PATH_NOT_AUTHORIZED'): set_sub(s,pid,path)
    path.unlink(); path.write_text(SRT)
    def replace_after(stage):
        if stage=='after_open': path.unlink(); path.write_text(SRT.replace('RED','LATER'))
    monkeypatch.setattr(ap,'_media_hook',replace_after)
    set_sub(s,pid,path)
    assert s.projects[pid].state.subtitles[0].cues==[(0.8,1.6,'Cove RED')]
def test_snapshot_changed_while_reading(subtitled,monkeypatch):
    s,pid,_,path=subtitled; read=os.read
    def changed(fd,n):
        data=read(fd,n); path.write_text(SRT+'\nchanged'); return data
    monkeypatch.setattr(ap.os,'read',changed)
    with rejected('ASSET_CHANGED'): set_sub(s,pid,path)
@pytest.mark.parametrize('change', ['replace','edit','hash','missing'])
def test_subtitle_reverify_save_and_export(subtitled,change):
    s,pid,_,path=subtitled; set_sub(s,pid,path); s.save_project(pid); before=s.get_project(pid)
    st=path.stat()
    if change in {'replace','missing'}: path.unlink()
    if change!='missing': path.write_text(SRT.replace('RED','NEW'))
    if change=='hash': os.utime(path,ns=(st.st_atime_ns,st.st_mtime_ns))
    for method in (lambda:s.save_project(pid),lambda:s.start_export(pid,'changed.mp4')):
        with rejected('ASSET_CHANGED') as exc: method()
        assert 'subtitle' in exc.value.message and 'call cove_set_subtitles again, then save' in exc.value.message
    assert s.get_project(pid)==before
def test_subtitles_preserved_by_every_edit(subtitled):
    s,pid,aid,path=subtitled; music=tones(path.parent/'music.wav')
    s.set_audio(pid,[dict(path=str(music),volume=0.7)]); s.set_audio_mix(pid,added_gain=0.8,original_gain=0.6)
    audio=copy.deepcopy(s.projects[pid].state.added_audios); mix=s.get_project(pid)['mix']
    set_sub(s,pid,path); assert s.projects[pid].state.added_audios==audio and s.get_project(pid)['mix']==mix
    operations=[lambda:s.set_audio(pid,[dict(path=str(music),volume=0.7)]),lambda:s.set_audio_mix(pid,added_gain=0.9),
        lambda:s.set_timeline(pid,'sequential',[dict(asset_id=aid,src_start_s=0,src_end_s=3)]),
        lambda:s.import_media(pid,[str(path.with_suffix('.mp4'))]),
        lambda:s.set_crop(pid,s.projects[pid].state.clips[0].id,s.projects[pid].revision,dict(x=0.5,y=0,width=0.5,height=1)),
        lambda:s.save_project(pid)]
    snapshots=copy.deepcopy(s.projects[pid].subtitle_snapshots)
    for edit in operations:
        edit(); assert s.projects[pid].state.subtitles[0].cues==[(0.8,1.6,'Cove RED')]
        assert s.projects[pid].subtitle_snapshots==snapshots
def test_export_subtitles_fail_closed(subtitled):
    s,pid,_,path=subtitled; set_sub(s,pid,path); s.save_project(pid)
    cues=s.projects[pid].state.subtitles[0].cues
    for invalid in ([],cues*2):
        s.projects[pid].state.subtitles[0].cues=invalid
        with rejected('INVALID_SUBTITLE'): s.start_export(pid,'empty.mp4')
    s.projects[pid].state.subtitles[0].cues=cues
    s.projects[pid].subtitle_snapshots.clear()
    with rejected('INVALID_SUBTITLE'): s.start_export(pid,'missing-snapshot.mp4')
    assert s.exports.active is None
def render(s,pid,name):
    s.save_project(pid); return finish(s,s.start_export(pid,name))['output']
def frame(path,t):
    data=subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(path),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'],
        check=True,capture_output=True,timeout=15).stdout
    assert data; return data
def red_pixels(data,width,height,position='bottom'):
    start,end=(height//2,height) if position=='bottom' else (0,height//2)
    return sum(r>100 and r>g*1.7 and r>b*1.7 for r,g,b in zip(data[start*width*3:end*width*3:3],
        data[start*width*3+1:end*width*3:3],data[start*width*3+2:end*width*3:3]))
def test_real_subtitle_timing_style_clear(subtitled):
    # Linux installed fonts/libass may fall back for CJK; nonempty glyphs are the oracle.
    s,pid,_,path=subtitled; video(path.with_suffix('.mp4'),True,color='black')
    # Refresh video identity in a fresh project; fixture media has been deliberately replaced.
    s.projects[pid].identities[s.projects[pid].state.assets[0].id]=ap.file_identity(path.with_suffix('.mp4').stat())
    base=render(s,pid,'base.mp4'); baseline=frame(base['path'],1.1)
    counts=[]
    for size,position in [(24,'bottom'),(48,'bottom'),(48,'top')]:
        set_sub(s,pid,path,**(STYLE|dict(font_size=size,position=position)))
        output=render(s,pid,f'{size}-{position}.mp4'); inside=frame(output['path'],1.1)
        count=red_pixels(inside,320,240,position); assert count>20; counts.append(count)
        assert red_pixels(inside,320,240,'top' if position=='bottom' else 'bottom')==0
        for t in (0.4,2.0):
            data=frame(output['path'],t); assert red_pixels(data,320,240)==0
            assert sum(abs(a-b) for a,b in zip(data,baseline))/len(data)<1
    assert counts[1]>counts[0]*1.5
    set_sub(s,pid,path,**(STYLE|dict(offset_ms=500)))
    shifted=render(s,pid,'shifted.mp4')
    assert red_pixels(frame(shifted['path'],1.0),320,240)==0
    assert red_pixels(frame(shifted['path'],1.8),320,240)>20
    path.write_text(SRT.replace('Cove RED','中文 😀 café')); set_sub(s,pid,path,**STYLE)
    unicode=render(s,pid,'unicode.mp4'); assert red_pixels(frame(unicode['path'],1.1),320,240)>20
    for label,tracks in [('disabled',[dict(path=str(path),active=False,**STYLE)]),('cleared',[])]:
        s.set_subtitles(pid,tracks); output=render(s,pid,label+'.mp4')
        assert frame(output['path'],1.1)==baseline
@pytest.mark.parametrize('mode',['success','failure','cancel'])
def test_real_ass_cleanup(subtitled,tmp_path,monkeypatch,mode):
    s,pid,_,path=subtitled; set_sub(s,pid,path,**STYLE); s.save_project(pid)
    owned=tmp_path/'ass-tmp'; owned.mkdir(); monkeypatch.setenv('TMPDIR',str(owned)); monkeypatch.setattr(tempfile,'tempdir',str(owned))
    entered,release=threading.Event(),threading.Event(); original=ex.ExportWorker._execute
    def observed(worker,*args,**kwargs):
        directories=list(owned.glob('cove-subs-*')); assert len(directories)==1
        assert stat.S_IMODE(directories[0].stat().st_mode)==0o700 and list(directories[0].glob('*.ass'))
        if mode=='cancel':
            from PySide6.QtCore import Qt
            def progress(percent):
                if percent>0:
                    assert worker._proc.poll() is None
                    entered.set(); assert release.wait(5)
            worker.progress.connect(progress,Qt.ConnectionType.DirectConnection)
        else: entered.set()
        if mode=='failure': args=([*args[0],'-cove-invalid-option'],*args[1:])
        return original(worker,*args,**kwargs)
    monkeypatch.setattr(ex.ExportWorker,'_execute',observed)
    started=s.start_export(pid,mode+'.mp4')
    try:
        assert entered.wait(5)
        if mode=='cancel': s.cancel_export(started['job_id'])
    finally: release.set()
    finish(s,started,dict(success='SUCCEEDED',failure='FAILED',cancel='CANCELLED')[mode])
    assert not list(owned.glob('cove-subs-*'))

@pytest.mark.parametrize('family',['angles','braces','mixed','digits','blocks'])
def test_parser_cost_is_bounded_atomic(subtitled,family):
    s,pid,_,path=subtitled; bad=path.parent/'adversarial.srt'
    content=SRT.replace('Cove RED',{'angles':'<'*900000,'braces':'{'*900000,'mixed':'<{'*450000,
        'digits':'x','blocks':'x'}[family])
    if family=='digits': content='1\n'+'9'*500000+' --> invalid\nx'
    if family=='blocks': content=('1\n00:00:00,800 --> 00:00:01,600\n'+'<'*16+'\n\n')*5001
    bad.write_text(content)
    code='''import copy,json,sys,time
from cove_video_editor.agent_paths import validate_roots,AgentError
from cove_video_editor.agent_session import AgentSession
from pathlib import Path
with_session=AgentSession(validate_roots([sys.argv[1]],sys.argv[2]))
try:
 pid=with_session.create_project("bounded")['project_id']
 with_session.set_subtitles(pid,[dict(path=sys.argv[3])])
 before=with_session.get_project(pid); snapshots=copy.deepcopy(with_session.projects[pid].subtitle_snapshots)
 started=time.perf_counter()
 try: with_session.set_subtitles(pid,[dict(path=sys.argv[4])]); result='ACCEPTED'
 except AgentError as exc: result=exc.code
 assert with_session.get_project(pid)==before
 assert with_session.projects[pid].subtitle_snapshots==snapshots
 print(json.dumps(dict(code=result,elapsed=time.perf_counter()-started,revision=with_session.get_project(pid)['revision'])))
finally: with_session.close()
'''
    # Hard external deadline kills a quadratic parser rather than hanging pytest/server.
    result=subprocess.run([sys.executable,'-c',code,str(path.parent),s.policy.output_root,str(path),str(bad)],
        capture_output=True,text=True,timeout=2)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout); print(family,report)
    assert report['code']==('LIMIT_EXCEEDED' if family=='blocks' else 'INVALID_SUBTITLE')
    assert report['elapsed']<1
@pytest.mark.parametrize('text', ['I <3 you','a < b','<i>balanced</i>', '<'*16, '{'*16,
    'line one\nline two', 'ok'+('<i></i>'*100)])
def test_parser_prevalidation_parity_allowed(subtitled,text):
    s,pid,_,path=subtitled; path.write_text(SRT.replace('Cove RED',text))
    expected=parse_sub_cues(path); set_sub(s,pid,path)
    assert s.projects[pid].state.subtitles[0].cues==expected
@pytest.mark.parametrize('cap',['timing','block','markers'])
def test_parser_prevalidation_caps(subtitled,cap):
    s,pid,_,path=subtitled
    if cap=='timing':
        line='00:00:00,800 --> 00:00:01,600'; content='1\n'+line+' '*(256-len(line))+'\nx\n'
    elif cap=='block':
        prefix='1\n00:00:00,800 --> 00:00:01,600\n'; body='<i></i>'*((8192-len(prefix)-1)//7)
        content=prefix+body+'x'*(8192-len(prefix)-len(body))
    else: content=SRT.replace('Cove RED','<'*8+'{'*8)
    path.write_text(content); expected=parse_sub_cues(path); set_sub(s,pid,path)
    assert s.projects[pid].state.subtitles[0].cues==expected
    before=s.get_project(pid); snapshots=copy.deepcopy(s.projects[pid].subtitle_snapshots)
    excessive=content.replace('\nx\n','x\nx\n') if cap=='timing' else content.rstrip('\n')+('x' if cap=='block' else '<')
    path.write_text(excessive)
    with rejected('INVALID_SUBTITLE'): set_sub(s,pid,path)
    assert s.get_project(pid)==before and s.projects[pid].subtitle_snapshots==snapshots
@pytest.mark.parametrize('operation',['save','export'])
@pytest.mark.parametrize('change',['identical','metadata','digest','missing'])
def test_identity_digest_independent(subtitled,change,operation):
    s,pid,_,path=subtitled; set_sub(s,pid,path); s.save_project(pid); before=s.get_project(pid); old=path.stat()
    if change=='identical':
        fd=os.open(path,os.O_RDONLY)
        try: path.unlink(); path.write_text(SRT); assert path.stat().st_ino!=old.st_ino
        finally: os.close(fd)
    elif change=='metadata': os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns+1000000))
    elif change=='digest': path.write_text(SRT.replace('RED','NEW')); os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
    else: path.unlink()
    if change=='digest': assert ap.file_identity(path.stat())==ap.file_identity(old)
    call=(lambda:s.save_project(pid)) if operation=='save' else (lambda:s.start_export(pid,'changed.mp4'))
    with rejected('ASSET_CHANGED'): call()
    assert s.get_project(pid)==before
def test_vtt_short_timestamps_documented_limitation(subtitled):
    s,pid,_,path=subtitled; path=path.with_suffix('.vtt')
    short='WEBVTT\n\n00:00.800 --> 00:01.600\nshort\n'; path.write_text(short)
    with rejected('INVALID_SUBTITLE') as exc: set_sub(s,pid,path)
    assert 'hh:mm:ss,mmm / hh:mm:ss.mmm' in exc.value.message
    path.write_text(short+'\n'+SRT.replace(',','.'))
    result=set_sub(s,pid,path)
    assert result['subtitles'][0]['cue_count']==1 and result['subtitles'][0]['skipped_cue_blocks']==1
    assert any('skipped' in warning for warning in result['warnings'])
def test_subtitle_extension_recovery_text(subtitled):
    s,pid,_,path=subtitled
    for suffix in ('.txt','.ass','.ssa'):
        unsupported=path.with_suffix(suffix); unsupported.write_text(SRT)
        with rejected('UNSUPPORTED_EXTENSION') as exc: set_sub(s,pid,unsupported)
        assert 'Use a .srt or .vtt file' in exc.value.message
        assert ('parsed SRT/VTT' in exc.value.message)==(suffix in {'.ass','.ssa'})
def test_many_maximum_blocks_bounded_parity(subtitled):
    s,pid,_,path=subtitled; timing='00:00:00,800 --> 00:00:01,600'
    prefix='1\n'+timing+' '*(256-len(timing))+'\n'; body='<i></i>'*((8192-len(prefix)-16)//7)
    block=prefix+body+'x'*(8192-len(prefix)-len(body)-16)+'<'*16
    assert len(block)==8192; path.write_text((block+'\n\n')*120)
    started=time.perf_counter(); expected=parse_sub_cues(path); result=set_sub(s,pid,path)
    assert time.perf_counter()-started<2 and result['subtitles'][0]['cue_count']==120
    assert s.projects[pid].state.subtitles[0].cues==expected
def test_literal_markup_real_burn_in(subtitled):
    s,pid,_,path=subtitled; video_path=path.with_suffix('.mp4'); video(video_path,True,color='black')
    s.projects[pid].identities[s.projects[pid].state.assets[0].id]=ap.file_identity(video_path.stat())
    path.write_text(SRT.replace('Cove RED','I <3 you\na < b'))
    set_sub(s,pid,path,**STYLE); output=render(s,pid,'literal-markup.mp4')
    assert red_pixels(frame(output['path'],1.1),320,240)>20
