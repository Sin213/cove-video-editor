"""Per-clip revision binding, preservation and decoded quadrant oracles."""
import copy, dataclasses, subprocess, threading, pytest
from pathlib import Path
from test_agent_session import editing
from test_agent_paths import tree, rejected
from test_agent_exports import finish
from test_agent_subtitles import SRT, STYLE, set_sub, render, frame, red_pixels
from test_agent_editing import tones, pcm, amplitude
from cove_video_editor import agent_paths as ap, agent_exports as ae, exporter as ex, project_io as io
RIGHT=dict(x=0.5,y=0,width=0.5,height=1)
@pytest.fixture
def cropped(editing):
    s,pid,aid,path=editing
    s.set_timeline(pid,'sequential',[dict(asset_id=aid,src_start_s=0,src_end_s=0.8),
        dict(asset_id=aid,src_start_s=0.2,src_end_s=2.8,speed=2,volume=0.4,mute=True),
        dict(asset_id=aid,src_start_s=0,src_end_s=0.8)])
    return s,pid,aid,path
def crop(s,pid,value=RIGHT,index=0):
    p=s.projects[pid]; return s.set_crop(pid,p.state.clips[index].id,p.revision,value)
def test_uncropped_summary(cropped):
    s,pid,_,_=cropped; result=s.get_project(pid)
    assert result['cleared_crops']==[]
    for c,output in zip(s.projects[pid].state.clips,result['clips']):
        assert output['clip_id']==c.id and output['crop'] is None and output['crop_pixels'] is None
def test_timeline_reports_existing_crop(cropped):
    s,pid,aid,_=cropped; old=s.projects[pid].state.clips[1]; old.crop_rect=(0.5,0,0.5,1)
    result=s.set_timeline(pid,'sequential',[dict(asset_id=aid,src_start_s=0,src_end_s=3)])
    assert result['cleared_crops']==[dict(clip_id=old.id,asset_id=aid,src_start_s=0.2,src_end_s=2.8,crop=RIGHT)]
    assert any('re-apply' in w and 'cove_set_crop' in w for w in result['warnings'])
    assert result['clips'][0]['clip_id']!=old.id and result['clips'][0]['crop'] is None
    assert s.get_project(pid)['cleared_crops']==[]
    result=s.set_timeline(pid,'sequential',[dict(asset_id=aid,src_start_s=0,src_end_s=3)])
    assert result['cleared_crops']==[] and not any('re-apply' in w for w in result['warnings'])
def test_crop_preserves_all_fields(cropped):
    s,pid,_,path=cropped; music=tones(path.parent/'music.wav'); sub=path.with_suffix('.srt'); sub.write_text(SRT)
    s.set_audio(pid,[dict(path=str(music),volume=0.6)]); s.set_audio_mix(pid,original_gain=0.7,added_gain=0.8)
    set_sub(s,pid,sub,**STYLE); crop(s,pid,dict(x=0,y=0,width=0.5,height=1),0)
    p=s.projects[pid]
    p.state.clips[1].linked_audio=False; p.state.clips[1].audio_offset=0.3; p.state.clips[1].audio_removed=True
    before=copy.deepcopy(p.state); identities=copy.deepcopy((p.identities,p.audio_identities,p.subtitle_snapshots))
    revision=p.revision; result=crop(s,pid,RIGHT,1)
    assert result['revision']==revision+1 and result['cleared_crops']==[]
    for i,(old,new) in enumerate(zip(before.clips,p.state.clips)):
        for field in dataclasses.fields(old):
            if i==1 and field.name in {'crop_rect','crop_preset'}: continue
            assert getattr(new,field.name)==getattr(old,field.name),(i,field.name)
    assert p.state.clips[1].crop_rect==(0.5,0,0.5,1) and p.state.clips[1].crop_preset=='Free (Custom)'
    for field in dataclasses.fields(before):
        if field.name!='clips': assert getattr(p.state,field.name)==getattr(before,field.name),field.name
    assert (p.identities,p.audio_identities,p.subtitle_snapshots)==identities
    assert result['clips'][1]['crop_pixels']==dict(x=160,y=0,width=160,height=240)
    assert tuple(result['clips'][1]['crop_pixels'].values())==ex.effective_clip_crop_pixels(p.state.clips[1])
    saved=s.save_project(pid); loaded=io.load_project(Path(saved['path']))
    assert [c.crop_rect for c in loaded.clips]==[(0,0,0.5,1),(0.5,0,0.5,1),None]
    result=crop(s,pid,None,1)
    assert result['clips'][1]['crop'] is None and result['clips'][1]['crop_pixels'] is None
    assert p.state.clips[1].crop_preset=='Free (Custom)' and p.state.clips[0].crop_rect==(0,0,0.5,1)
@pytest.mark.parametrize('bad', [dict(x=-0.1,y=0,width=0.5,height=1),dict(x=0.6,y=0,width=0.5,height=1),
    dict(x=0,y=-0.1,width=0.5,height=1),dict(x=0,y=0.5,width=0.5,height=0.6),
    dict(x=0,y=0,width=0,height=1),dict(x=0,y=0,width=-1,height=1),dict(x=0,y=0,width=1.1,height=1),
    dict(x=0,y=0,width=0.5,height=0),dict(x=True,y=0,width=0.5,height=1),
    dict(x=0,y=float('nan'),width=0.5,height=1),dict(x=0,y=0,width=float('inf'),height=1),
    dict(x='0',y=0,width=0.5,height=1),dict(x=0,y=0,width=1,height=1),
    dict(x=0,y=0,width=0.00001,height=0.00001),dict(x=0,y=0,width=0.9999999,height=1),
    dict(x=0,y=0,width=0.5),dict(x=0,y=0,width=0.5,height=1,extra=1),[],True])
def test_invalid_crop_atomic(cropped,bad):
    s,pid,_,_=cropped; crop(s,pid); before=s.get_project(pid)
    with rejected('INVALID_CROP'): crop(s,pid,bad)
    assert s.get_project(pid)==before
@pytest.mark.parametrize('revision,code', [(-1,'INVALID_ARGUMENT'),(True,'INVALID_ARGUMENT'),(1.0,'INVALID_ARGUMENT'),
    ('2','INVALID_ARGUMENT'),(None,'INVALID_ARGUMENT'),(0,'REVISION_MISMATCH')])
def test_revision_binding(cropped,revision,code):
    s,pid,_,_=cropped; before=s.get_project(pid)
    with rejected(code) as exc: s.set_crop(pid,before['clips'][0]['clip_id'],revision,RIGHT)
    if code=='REVISION_MISMATCH': assert str(before['revision']) in exc.value.message and 'get_project' in exc.value.message
    assert s.get_project(pid)==before
def test_unknown_clip(cropped):
    s,pid,_,_=cropped; before=s.get_project(pid)
    with rejected('UNKNOWN_CLIP'): s.set_crop(pid,'missing',before['revision'],RIGHT)
    assert s.get_project(pid)==before
def quadrants(path):
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
        'color=red:s=320x240:r=25:d=3,drawbox=x=160:y=0:w=160:h=120:color=lime:t=fill,'
        'drawbox=x=0:y=120:w=160:h=120:color=blue:t=fill,drawbox=x=160:y=120:w=160:h=120:color=yellow:t=fill',
        '-c:v','mpeg4',str(path)],check=True,capture_output=True,timeout=15)
    return path
def pixel(data,width,x,y): return tuple(data[(y*width+x)*3:(y*width+x)*3+3])
def assert_quadrants(output,t=0.4,right=False):
    width,height=output['width'],output['height']; data=frame(output['path'],t)
    left=pixel(data,width,width//4,height//4); bottom=pixel(data,width,width//4,3*height//4)
    if right: assert left[1]>200 and left[0]<30 and bottom[0]>200 and bottom[1]>200
    else: assert left[0]>200 and left[1]<30 and bottom[2]>200 and bottom[0]<30
    top=pixel(data,width,3*width//4,height//4); assert top[1]>200 and top[0]<30
    bottom=pixel(data,width,3*width//4,3*height//4); assert bottom[0]>200 and bottom[1]>200
@pytest.fixture
def quadrant_project(tree):
    from cove_video_editor.agent_session import AgentSession
    s=AgentSession(tree[2]); path=quadrants(tree[0]/'quad.mp4'); pid=s.create_project('Quadrants')['project_id']
    aid=s.import_media(pid,[str(path)])['assets'][0]['asset_id']
    s.set_timeline(pid,'sequential',[dict(asset_id=aid,src_start_s=0,src_end_s=3)])
    yield s,pid,aid,path
    s.close()
def test_real_crop_clear_trim_audio_subtitles(quadrant_project):
    s,pid,aid,path=quadrant_project
    base=render(s,pid,'base.mp4'); assert (base['width'],base['height'])==(320,240); assert_quadrants(base)
    crop(s,pid); saved=s.save_project(pid); loaded=io.load_project(Path(saved['path']))
    assert loaded.clips[0].crop_rect==(0.5,0,0.5,1)
    output=render(s,pid,'right.mp4'); assert (output['width'],output['height'])==(160,240); assert_quadrants(output,right=True)
    # Build the existing exporter from the reloaded document to prove persistence behavior.
    reloaded=path.parent/'reload.mp4'; job=ex.build_export_job(loaded,output=str(reloaded),fmt_key='MP4 (H.264 + AAC)',size=(None,None),encoder_pref='cpu')
    worker=ex.ExportWorker(job); failures=[]; worker.failed.connect(failures.append); worker.run(); assert not failures
    assert_quadrants(dict(path=str(reloaded),width=160,height=240),right=True)
    crop(s,pid,None); clear=render(s,pid,'clear.mp4'); assert (clear['width'],clear['height'])==(320,240); assert_quadrants(clear)
    s.set_timeline(pid,'sequential',[dict(asset_id=aid,src_start_s=0.4,src_end_s=2.8,speed=2)])
    crop(s,pid); music=tones(path.parent/'music.wav'); s.set_audio(pid,[dict(path=str(music),src_end_s=1.2)])
    s.set_audio_mix(pid,replace_original=True)
    audio=render(s,pid,'audio.mp4'); assert abs(audio['duration_s']-1.2)<0.15; assert_quadrants(audio,right=True)
    assert amplitude(pcm(audio['path']),660,start=0.2,end=0.7)>1000
    sub=path.with_suffix('.srt'); sub.write_text(SRT.replace('00:00:01,600','00:00:01,100'))
    set_sub(s,pid,sub,**(STYLE|dict(font_size=24))); combined=render(s,pid,'combined.mp4')
    data=frame(combined['path'],0.9)
    # Yellow background is not red-dominant; text contributes new red pixels.
    assert red_pixels(data,160,240)>10; assert_quadrants(combined,0.3,right=True)
def test_real_second_clip_crop_canvas(quadrant_project):
    s,pid,aid,_=quadrant_project
    s.set_timeline(pid,'sequential',[dict(asset_id=aid,src_start_s=0,src_end_s=1.2),dict(asset_id=aid,src_start_s=0,src_end_s=1.2)])
    crop(s,pid,RIGHT,1); output=render(s,pid,'second.mp4')
    assert (output['width'],output['height'])==(320,240); assert_quadrants(output,0.4)
    # Existing exporter fits the narrower second crop into the first clip's canvas.
    data=frame(output['path'],1.6)
    assert max(pixel(data,320,10,60))<10 and max(pixel(data,320,310,60))<10
    assert pixel(data,320,140,60)[1]>200 and pixel(data,320,140,180)[0]>200
def test_export_subtitle_crop_snapshot_immutable(quadrant_project,monkeypatch):
    s,pid,_,path=quadrant_project; sub=path.with_suffix('.srt'); sub.write_text(SRT)
    set_sub(s,pid,sub,**(STYLE|dict(font_size=24))); crop(s,pid); s.save_project(pid)
    entered,release=threading.Event(),threading.Event()
    def hook(stage,job):
        if stage=='before_run': entered.set(); assert release.wait(8)
    monkeypatch.setattr(ae,'_hook',hook); started=s.start_export(pid,'snapshot.mp4')
    try:
        assert entered.wait(5); old=s.exports._get(started['job_id']).render
        crop(s,pid,None); sub.write_text(SRT.replace('Cove RED','CHANGED')); s.set_subtitles(pid,[])
        assert old.clips[0].crop_rect==(0.5,0,0.5,1) and old.subtitles.cues==[(0.8,1.6,'Cove RED')]
    finally: release.set()
    output=finish(s,started)['output']; assert (output['width'],output['height'])==(160,240)
    assert red_pixels(frame(output['path'],1.1),160,240)>10; assert_quadrants(output,0.4,right=True)

def crop_response_models(session):
    from typing import get_args
    from cove_video_editor.mcp_server import build_server
    summary=build_server(session)._tool_manager.get_tool('cove_get_project').fn_metadata.output_model
    clip_model=get_args(summary.model_fields['clips'].annotation)[0]
    pixels=next(t for t in get_args(clip_model.model_fields['crop_pixels'].annotation) if t is not type(None))
    return summary,pixels
def test_crop_pixels_odd_source_sweep(cropped):
    from cove_video_editor.clip import Clip,MediaAsset
    s,_,_,_=cropped; _,model=crop_response_models(s); count=0
    # Every dimension 1..401 participates both horizontally and vertically;
    # all width/height pairs plus all four corners exercise odd-edge clamps.
    rectangles=[(0.5,0,0.5,1),(0,0.5,1,0.5),(0,0,0.5,0.5),(0.5,0.5,0.5,0.5),(0.9,0.9,0.1,0.1)]
    for width in range(1,402):
        for height in range(1,402):
            asset=MediaAsset(Path('/fixture.mp4'),3,width,height,25,False)
            for rect in rectangles:
                pixels=ex.effective_clip_crop_pixels(Clip(asset,crop_rect=rect))
                if pixels is None: continue
                x,y,w,h=pixels; model.model_validate(dict(x=x,y=y,width=w,height=h))
                assert 0<=x and x+w<=width and 0<=y and y+h<=height
                assert w>=2 and h>=2 and w%2==h%2==0; count+=1
    print('validated crop pixel results',count)
def odd_video(path):
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
        'color=red:s=320x240:r=25:d=3,format=yuv444p,crop=319:240:0:0,'
        'drawbox=x=159:y=0:w=160:h=240:color=lime:t=fill','-c:v','libx264','-pix_fmt','yuv444p',str(path)],
        check=True,capture_output=True,timeout=15)
    return path
def test_real_odd_crop_mcp_session(tree):
    from cove_video_editor.agent_session import AgentSession
    s=AgentSession(tree[2]); path=odd_video(tree[0]/'odd.mp4')
    try:
        pid=s.create_project('Odd')['project_id']; asset=s.import_media(pid,[str(path)])['assets'][0]
        assert (asset['width'],asset['height'])==(319,240)
        summary=s.set_timeline(pid,'sequential',[dict(asset_id=asset['asset_id'],src_start_s=0,src_end_s=3)])
        output_model,_=crop_response_models(s)
        result=crop(s,pid); output_model.model_validate(result); output_model.model_validate(s.get_project(pid))
        expected=ex.effective_clip_crop_pixels(s.projects[pid].state.clips[0]); assert expected==(159,0,160,240)
        output=render(s,pid,'odd.mp4'); assert (output['width'],output['height'])==expected[2:]
        data=frame(output['path'],0.4)
        for x,y in [(10,10),(150,10),(10,230),(150,230)]:
            color=pixel(data,160,x,y); assert color[1]>200 and color[0]<30
    finally: s.close()
def test_sdk_real_odd_crop(tree):
    pytest.importorskip('mcp')
    import anyio
    from mcp.client import Client
    from mcp.client._memory import InMemoryTransport
    from cove_video_editor.agent_session import AgentSession
    from cove_video_editor.mcp_server import build_server
    s=AgentSession(tree[2]); path=odd_video(tree[0]/'sdk-odd.mp4')
    async def protocol():
        async with Client(InMemoryTransport(build_server(s)),mode='legacy') as connected:
            async def call(name,/,**args):
                reply=await connected.session.call_tool('cove_'+name,args); assert not reply.is_error,reply
                return reply.structured_content
            pid=(await call('create_project',name='SDK odd'))['project_id']
            asset=(await call('import_media',project_id=pid,paths=[str(path)]))['assets'][0]
            assert (asset['width'],asset['height'])==(319,240)
            summary=await call('set_timeline',project_id=pid,placement='sequential',clips=[dict(asset_id=asset['asset_id'],src_start_s=0,src_end_s=3)])
            summary=await call('set_crop',project_id=pid,clip_id=summary['clips'][0]['clip_id'],expected_revision=summary['revision'],crop=RIGHT)
            assert summary['clips'][0]['crop_pixels']==dict(x=159,y=0,width=160,height=240)
            assert await call('get_project',project_id=pid)==summary
            await call('save_project',project_id=pid); started=await call('start_export',project_id=pid,output_name='sdk-odd.mp4')
            while True:
                status=await call('get_export_status',job_id=started['job_id'])
                if status['state'] in {'SUCCEEDED','FAILED','CANCELLED'}: break
                await anyio.sleep(0.01)
            assert status['state']=='SUCCEEDED',status; output=status['output']
            assert (output['width'],output['height'])==(160,240)
            data=frame(output['path'],0.4); color=pixel(data,160,150,230)
            assert color[1]>200 and color[0]<30
    async def run():
        async def heartbeat():
            while True: await anyio.sleep(0.01)
        with anyio.fail_after(15):
            async with anyio.create_task_group() as group:
                group.start_soon(heartbeat)
                try: await protocol()
                finally: group.cancel_scope.cancel()
    try: anyio.run(run)
    finally: s.close()
