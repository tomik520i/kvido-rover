import asyncio
import base64
import io
import json
import os
from pathlib import Path
import stat
import threading
import time

import httpx
import pytest
from PIL import Image

from server.diagnostics import DiagnosticTimeline
from test_safety import a,c,decision
from test_task_integration import task_client,wait_task


def records(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_task_error_allowlist_excludes_exception_bodies_requests_and_credentials(tmp_path):
    private='PRIVATE-UNREGISTERED-EXCEPTION-CONTENT'
    timeline=DiagnosticTimeline(tmp_path/'task-errors')
    assert timeline.event('task_error',task_id='task-a',step=32,phase='verifying',
        error_type='ValueError',error_category=None,status_code=502,message=private,
        exception=private,request=private,url=private,body=private,headers=private,
        access_key=private,traceback=private)
    timeline.close(2)
    text=timeline.path.read_text(encoding='utf-8')
    assert private not in text
    row=records(timeline.path)[0]
    assert row['task_id']=='task-a' and row['step']==32 and row['status_code']==502
    assert row['error_type']=='ValueError' and row['phase']=='verifying'
    assert not {'message','exception','request','url','body','headers','access_key','traceback'}&row.keys()


def test_private_whitelisted_trace_redacts_secrets_and_payloads(tmp_path):
    secret='private-test-key-DO-NOT-LOG'
    timeline=DiagnosticTimeline(tmp_path/'diagnostics',secrets=[secret])
    timeline.event('model_proposal',task_id='task-a',step=1,goal='Move '+secret,
        proposal={'action':'forward','speed':100,'reason':secret,'evidence':'data:image/jpeg;base64,AAAA',
                  'memory':'private note','headers':{'Authorization':secret}},
        authorization=secret,cookies=secret,audio='AAAA',image='data:image/jpeg;base64,AAAA')
    timeline.event('telemetry',telemetry={'distance_cm':87,'motor_byte':2,'device_key':secret,
        'drive':{'seq':3,'accepted':2,'denied':1,'ok':True,'why':'ok'},
        'ultra':{'seq':50,'ok':True,'null':8},'motor':{'active':False,'null_stops':1},
        'relay':{'connections':2,'last_error':'Authorization: '+secret,'headers':secret}})
    timeline.close(2)
    text=timeline.path.read_text(encoding='utf-8')
    assert secret not in text and 'data:image' not in text and 'base64' not in text
    assert 'authorization' not in text.lower() and 'cookies' not in text and 'audio' not in text
    rows=records(timeline.path)
    assert rows[0]['proposal']['action']=='forward' and rows[0]['goal']=='Move [REDACTED]'
    assert rows[1]['telemetry']['drive']['accepted']==2
    assert rows[1]['telemetry']['ultra']['ok'] is True
    assert type(rows[1]['telemetry']['ultra']['null']) is int
    assert rows[1]['telemetry']['motor']['active'] is False
    assert rows[0]['seq']<rows[1]['seq'] and rows[0]['at_ms']>0 and rows[0]['monotonic_ms']>0
    if os.name!='nt':
        assert stat.S_IMODE(timeline.directory.stat().st_mode)==0o700
        assert stat.S_IMODE(timeline.path.stat().st_mode)==0o600


def test_rotation_retains_at_most_three_bounded_jsonl_files(tmp_path):
    timeline=DiagnosticTimeline(tmp_path,max_bytes=500,backups=2)
    for index in range(30):timeline.event('command_request',command_id=str(index),action='stop')
    timeline.close(2)
    paths=list(tmp_path.glob('timeline.jsonl*'))
    assert len(paths)==3 and all(path.stat().st_size<=500 for path in paths)
    assert records(timeline.path)[-1]['command_id']=='29'


def test_full_queue_and_stuck_disk_cannot_block_caller_or_shutdown(tmp_path,monkeypatch):
    timeline=DiagnosticTimeline(tmp_path,queue_size=1)
    entered=threading.Event();release=threading.Event()
    original=timeline._write
    def blocked(entry):
        entered.set();release.wait(2);original(entry)
    monkeypatch.setattr(timeline,'_write',blocked)
    timeline.event('command_request',action='stop')
    assert entered.wait(1)
    timeline.event('command_request',action='stop')
    started=time.monotonic()
    assert timeline.event('command_request',action='stop') is False
    timeline.close(.01)
    assert time.monotonic()-started<.2 and timeline.dropped>=1
    release.set();timeline.close(2)


def test_disk_error_is_counted_without_raising_or_logging_exception_body(tmp_path,monkeypatch):
    timeline=DiagnosticTimeline(tmp_path)
    def fail():raise PermissionError('SECRET must not be emitted')
    monkeypatch.setattr(timeline,'_prepare',fail)
    assert timeline.event('command_result',acceptance='unknown')
    timeline.close(2)
    assert timeline.write_errors==1 and timeline.status()['enabled'] is False


def test_telemetry_is_change_sensitive_and_paced_to_five_hz(tmp_path,monkeypatch):
    now=[1.0]
    monkeypatch.setattr('server.diagnostics.time.monotonic',lambda:now[0])
    timeline=DiagnosticTimeline(tmp_path)
    assert timeline.telemetry({'distance_cm':87,'telemetry_age_ms':100})
    now[0]=1.1
    assert not timeline.telemetry({'distance_cm':88,'telemetry_age_ms':100})
    now[0]=1.21
    assert not timeline.telemetry({'distance_cm':87,'telemetry_age_ms':100})
    assert timeline.telemetry({'distance_cm':88,'telemetry_age_ms':100})
    timeline.close(2)
    assert len(records(timeline.path))==2


def test_only_two_private_frames_per_task_step_and_fifo_retention(tmp_path):
    data=io.BytesIO();Image.new('RGB',(16,16),'red').save(data,format='JPEG');raw=data.getvalue()
    timeline=DiagnosticTimeline(tmp_path,max_frames=2,max_frame_bytes=3*len(raw))
    assert timeline.frame(raw,'before',task_id='a',step=1,command_id='cmd1')
    assert not timeline.frame(raw,'before',task_id='a',step=1,command_id='cmd1')
    assert timeline.frame(raw,'after',task_id='a',step=1,command_id='cmd1')
    assert timeline.frame(raw,'before',task_id='a',step=2,command_id='cmd2')
    assert not timeline.frame('https://example.com/image.jpg','after')
    timeline.close(2)
    entries=records(timeline.path)
    assert len(entries)==3 and all(row['stored'] for row in entries)
    paths=list((tmp_path/'frames').glob('*.jpg'))
    assert len(paths)==2
    assert (tmp_path/entries[-1]['relative_file']).read_bytes()==raw
    assert all('image' not in row and len(row['sha256'])==64 for row in entries)


def test_app_trace_correlates_raw_proposal_gate_and_transport_without_hardware(c,monkeypatch):
    assert c.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert c.post('/api/arm',json={}).status_code==200
    monkeypatch.setitem(a.settings,'navigation_policy','sensor')
    async def infer(*args,**kwargs):
        return decision(action='forward',speed=150,duration_ms=300,hazard='unknown',
                        confidence=.1,traversable=False,reason='Explore the visible direction',evidence='87 cm')
    monkeypatch.setattr(a,'infer',infer)
    result=c.post('/api/turn',json={'intent':'act','source':'car'}).json()
    assert result['executed_action']=='forward'
    a.diagnostics.close(2)
    rows=records(a.diagnostics.path)
    proposal=next(row for row in reversed(rows) if row['event']=='model_proposal')
    gate=next(row for row in reversed(rows) if row['event']=='gate' and row.get('executed')=='forward')
    request=next(row for row in rows if row['event']=='command_request' and row.get('command_id')==gate['command_id'])
    accepted=next(row for row in rows if row['event']=='command_result' and row.get('command_id')==gate['command_id'])
    assert proposal['proposal']['speed']==150 and proposal['prompt_sha256']
    assert gate['actual']['speed']==request['speed']==150
    assert request['duration_ms']==300 and accepted['acceptance']=='transport_accepted'
    assert accepted['simulation'] is True and 'UNOexecuted' not in json.dumps(rows)
    assert result['command_id']==gate['command_id']


def test_broken_diagnostics_cannot_prevent_stop(c,monkeypatch):
    class Broken:
        def event(self,*args,**kwargs):raise OSError('private failure')
        def telemetry(self,*args,**kwargs):raise OSError('private failure')
    monkeypatch.setattr(a,'diagnostics',Broken())
    c.portal.call(a.halt,'Test emergency',True)
    assert a.sim['motor']=='estop' and a.state['estop'] and not a.state['armed']


def test_transport_failure_is_unknown_not_physical_execution(c,monkeypatch):
    monkeypatch.setitem(a.settings,'simulation',False)
    async def rejected(*args,**kwargs):raise OSError('private transport failure')
    monkeypatch.setattr(a,'device',rejected)
    async def attempt():
        try:await a.send_control('forward',100,150,90)
        except OSError:pass
    c.portal.call(attempt)
    a.diagnostics.close(2)
    rows=records(a.diagnostics.path)
    request=next(row for row in reversed(rows) if row['event']=='command_request' and row.get('action')=='forward')
    result=next(row for row in rows if row['event']=='command_result' and row.get('command_id')==request['command_id'])
    assert result['acceptance']=='unknown' and result['category']=='send_failed'
    assert 'private transport failure' not in json.dumps(rows)


def test_complete_task_preserves_decision_reason_and_correlated_private_frame_pair(task_client,monkeypatch):
    client,path=task_client
    async def infer(*args,**kwargs):
        return decision(action='rotate_left',turn_clear=True,speed=80,duration_ms=50,
                        reason='Inspect the doorway direction',evidence='Current visible direction')
    async def assess(*args,**kwargs):
        return {'status':'completed','progress':'changed','summary':'Goal visible after action',
                'evidence':'Newest after image','next_hint':''},{'latency_ms':12}
    monkeypatch.setattr(a,'infer',infer);monkeypatch.setattr(a,'assess_step',assess)
    assert client.post('/api/task',json={'goal':'Inspect the doorway'}).status_code==200
    task=wait_task(client)
    assert task['status']=='completed'
    result=task['last_result']
    assert result['decision_reason']=='Inspect the doorway direction'
    assert result['decision_evidence']=='Current visible direction'
    assert result['proposed_action']=='rotate_left' and result['proposed_command']['speed']==80
    a.diagnostics.close(2)
    rows=records(path/'diagnostics/timeline.jsonl')
    frames=[row for row in rows if row['event']=='frame']
    assert len(frames)==2 and {row['role'] for row in frames}=={'before','after'}
    assert all(row['task_id']==task['id'] and row['step']==1 for row in frames)
    assert all(row['command_id']==result['command_id'] for row in frames)
    assert all((path/'diagnostics'/row['relative_file']).read_bytes().startswith(b'\xff\xd8') for row in frames)
    assessment=next(row for row in rows if row['event']=='assessment')
    assert assessment['command_id']==result['command_id'] and assessment['assessment']['status']=='completed'
    saved=json.loads((path/'task.json').read_text(encoding='utf-8'))
    assert saved['last_result']['decision_reason']==result['decision_reason']
    assert saved['completed_steps'][0]['decision_reason']==result['decision_reason']
    assert assessment['before_sha256']==next(row['sha256'] for row in frames if row['role']=='before')
    assert assessment['after_sha256']==next(row['sha256'] for row in frames if row['role']=='after')
    request=next(row for row in rows if row['event']=='command_request' and row.get('command_id')==result['command_id'])
    assert next(row['seq'] for row in frames if row['role']=='before')<request['seq']
    assert all(row['step']<=1 for row in rows if row['event']=='task_phase' and row.get('task_id')==task['id'])


@pytest.mark.parametrize('code,expected',[(400,'transport_rejected'),(401,'transport_rejected'),(409,'transport_rejected'),(408,'unknown'),(500,'unknown'),(504,'unknown')])
def test_error_response_does_not_claim_unobserved_execution(c,monkeypatch,code,expected):
    monkeypatch.setitem(a.settings,'simulation',False)
    async def error(*args,**kwargs):
        response=httpx.Response(code,request=httpx.Request('POST','http://device.test/api/control'))
        response.raise_for_status()
    monkeypatch.setattr(a,'device',error)
    async def attempt():
        with pytest.raises(httpx.HTTPStatusError):await a.send_control('forward',100,150,90)
    c.portal.call(attempt)
    a.diagnostics.close(2)
    row=next(row for row in reversed(records(a.diagnostics.path)) if row['event']=='command_result' and row.get('action')=='forward')
    assert row['acceptance']==expected and row['status_code']==code


def test_recovery_keeps_changed_after_frame_for_same_step(tmp_path):
    timeline=DiagnosticTimeline(tmp_path)
    def jpeg(color):
        output=io.BytesIO();Image.new('RGB',(16,16),color).save(output,format='JPEG');return output.getvalue()
    assert timeline.frame(jpeg('red'),'after',task_id='a',step=1,command_id='same')
    assert timeline.frame(jpeg('blue'),'after',task_id='a',step=1,command_id='same')
    timeline.close(2)
    rows=records(timeline.path)
    assert len(rows)==2 and rows[0]['sha256']!=rows[1]['sha256']


def test_inflight_manual_command_does_not_inherit_new_task(c,monkeypatch):
    monkeypatch.setitem(a.settings,'simulation',False)
    monkeypatch.setattr(a,'task_runner',None)
    class Running:
        data={'id':'new-task','status':'running','current_step':3}
    async def change_task(*args,**kwargs):
        monkeypatch.setattr(a,'task_runner',Running())
    monkeypatch.setattr(a,'device',change_task)
    c.portal.call(a.send_control,'stop')
    monkeypatch.setattr(a,'task_runner',None)
    a.diagnostics.close(2)
    row=next(row for row in reversed(records(a.diagnostics.path)) if row['event']=='command_result')
    assert row['task_id'] is None and row['step'] is None


def test_diagnostics_preserves_both_board_pwm_capabilities(tmp_path):
    timeline=DiagnosticTimeline(tmp_path/'speed-caps')
    timeline.event('planning_telemetry',telemetry={'motor_max_speed':255,'esp_motor_max_speed':255})
    timeline.close(2)
    sample=records(timeline.path)[0]['telemetry']
    assert sample['motor_max_speed']==255 and sample['esp_motor_max_speed']==255
