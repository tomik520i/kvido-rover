"""Stubbed FastAPI task flow: simulator images, fake inference, no external I/O."""
import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from test_safety import a,decision


@pytest.fixture
def task_client(monkeypatch,tmp_path):
    # Isolate every mutable module singleton; the legacy safety fixture shares them.
    for name in ('client','relay','ai_provider','task_runner','task_pending'):
        monkeypatch.setattr(a,name,None)
    monkeypatch.setattr(a,'DATA',tmp_path)
    monkeypatch.setattr(a,'MISSION_PATH',tmp_path/'mission.json')
    monkeypatch.setattr(a,'CONFIG_PATH',tmp_path/'settings.json')
    monkeypatch.setattr(a,'settings',{**a.settings,'simulation':True,'transport':'lan','navigation_policy':'strict','camera_rotate_180':False})
    monkeypatch.setattr(a,'state',{**a.state,'mode':'manual','armed':False,'estop':False,'owner':None,'heartbeat':0,'epoch':0,'busy':False})
    monkeypatch.setattr(a,'mission',{'goal':'','status':'idle','steps':0,'recent':[],'started_at':0,'reason':''})
    monkeypatch.setattr(a,'sim',{**a.sim,'servo':55,'distance_cm':100,'telemetry_age_ms':0,'estop':False,'motor':'stop'})
    monkeypatch.setattr(a,'sessions',{})
    monkeypatch.setattr(a,'login_failures',{})
    monkeypatch.setattr(a,'listeners',set())
    monkeypatch.setattr(a,'vision_history',type(a.vision_history)())
    monkeypatch.setattr(a,'floor_seen',0)
    for name in ('motion_lock','turn_lock','capture_lock'):monkeypatch.setattr(a,name,asyncio.Lock())
    db=sqlite3.connect(tmp_path/'memory.sqlite3',check_same_thread=False)
    db.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY,role TEXT,content TEXT,at REAL);
                       CREATE TABLE memory(id INTEGER PRIMARY KEY,text TEXT,source TEXT,at REAL);
                       CREATE TABLE decisions(id INTEGER PRIMARY KEY,payload TEXT,at REAL);''')
    monkeypatch.setattr(a,'db',db)
    with TestClient(a.app) as client:
        a.task_runner.interval=0
        assert client.post('/api/login',json={'key':a.ACCESS_KEY}).status_code==200
        assert client.post('/api/mode',json={'mode':'ai'}).status_code==200
        assert client.post('/api/arm',json={}).status_code==200
        yield client,tmp_path
    db.close()


def wait_task(client,terminal=('completed','blocked','paused','cancelled'),timeout=4):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        client.post('/api/heartbeat',json={})
        response=client.get('/api/status');assert response.status_code==200
        task=response.json()['task']
        if task['status'] in terminal:return task
        time.sleep(.02)
    pytest.fail('Task did not reach a terminal state')


def test_fastapi_two_steps_assessed_then_completed_frames_authenticated_and_history_saved(task_client,monkeypatch):
    client,path=task_client;planned=[];assessed=[];sent=[]
    original_send=a.send_control
    async def send(action='stop',*args,**kwargs):
        sent.append(action);return await original_send(action,*args,**kwargs)
    async def infer(*args,**kwargs):
        planned.append(1)
        return decision(action='rotate_left' if len(planned)==1 else 'rotate_right',speed=80,duration_ms=50,turn_clear=True)
    async def assess(goal,before,after,command,*args,**kwargs):
        assert before.startswith('data:image/jpeg;base64,') and after.startswith('data:image/jpeg;base64,')
        assessed.append((goal,command['action']))
        return {'status':'continue' if len(assessed)==1 else 'completed','progress':'changed',
                'summary':'Next step' if len(assessed)==1 else 'Goal observed after second action',
                'evidence':'The labelled after image was checked.','next_hint':''},{}
    monkeypatch.setattr(a,'send_control',send);monkeypatch.setattr(a,'infer',infer);monkeypatch.setattr(a,'assess_step',assess)
    started=client.post('/api/task',json={'goal':'Find the blue cup'})
    assert started.status_code==200,started.text
    task=wait_task(client)
    assert task['status']=='completed' and task['current_step']==2
    assert task['goal']=='Find the blue cup'
    assert assessed==[('Find the blue cup','rotate_left'),('Find the blue cup','rotate_right')]
    assert [action for action in sent if action.startswith('rotate_')]==['rotate_left','rotate_right']
    assert task['last_result']['has_before'] is True and task['last_result']['has_after'] is True
    version=task['last_result']['id']
    for role in ('before','after'):
        endpoint=f'/api/task/frames/{role}'
        assert client.get(endpoint,params={'v':'different-step'}).status_code==404
        response=client.get(endpoint,params={'v':version})
        assert response.status_code==200 and response.content.startswith(b'\xff\xd8')
        assert response.headers['cache-control']=='no-store'
        cookies=dict(client.cookies);client.cookies.clear()
        try:assert client.get(endpoint,params={'v':version}).status_code==401
        finally:client.cookies.update(cookies)
    saved=json.loads((path/'task.json').read_text(encoding='utf-8'))
    assert saved['status']=='completed' and saved['current_step']==2 and saved['goal']=='Find the blue cup'
    assert 'data:image' not in (path/'task.json').read_text(encoding='utf-8')
    assert len(json.loads((path/'mission.json').read_text(encoding='utf-8'))['recent'])==2
    assert a.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]==2


@pytest.mark.parametrize('observations,expected,steps',[
    (['changed','changed'],'completed',3),
    (['possible_stall','changed'],'completed',3),
    (['changed','possible_stall'],'completed',3),
    (['unknown','unknown'],'completed',3),
    (['possible_stall','possible_stall'],'blocked',2),
])
def test_search_without_goal_progress_only_blocks_on_two_visual_stalls(task_client,monkeypatch,observations,expected,steps):
    client,_=task_client;planned=[];assessed=[]
    original_observe=a.vision_history.observe
    def observe(*args,**kwargs):
        frames=original_observe(*args,**kwargs)
        if frames:
            frames[-1]['motion_observation']={'status':observations[min(len(assessed),1)]}
        return frames
    async def infer(*args,**kwargs):
        planned.append(1)
        action=['rotate_left','rotate_right','light_on'][min(len(planned)-1,2)]
        return decision(action=action,speed=80,duration_ms=50,turn_clear=True)
    async def assess(*args,**kwargs):
        assessed.append(1)
        return {'status':'completed' if len(assessed)==3 else 'continue','progress':'unchanged',
                'summary':'Doorway not found yet; search continues.','evidence':'Goal not reached.',
                'next_hint':'Look at another part of the room.'},{}
    monkeypatch.setattr(a.vision_history,'observe',observe)
    monkeypatch.setattr(a,'infer',infer);monkeypatch.setattr(a,'assess_step',assess)
    assert client.post('/api/task',json={'goal':'Find the doorway'}).status_code==200
    task=wait_task(client,timeout=6)
    assert task['status']==expected
    assert task['current_step']==steps
    assert [item['action'] for item in task['completed_steps'][:2]]==['rotate_left','rotate_right']


def test_fastapi_stop_during_planning_discards_late_model_movement_and_keeps_goal(task_client,monkeypatch):
    client,path=task_client;planning=threading.Event();sent=[]
    original_send=a.send_control
    async def send(action='stop',*args,**kwargs):
        sent.append(action);return await original_send(action,*args,**kwargs)
    async def infer(*args,**kwargs):
        planning.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A provider may return an answer even after its caller cancels.
            return decision(action='rotate_left',turn_clear=True)
    monkeypatch.setattr(a,'send_control',send);monkeypatch.setattr(a,'infer',infer)
    response=client.post('/api/task',json={'goal':'Preserve this goal'})
    assert response.status_code==200,response.text
    assert planning.wait(1)
    assert client.post('/api/stop',json={}).status_code==200
    task=wait_task(client)
    assert task['status']=='paused' and task['goal']=='Preserve this goal'
    assert not any(action in ('forward','rotate_left','rotate_right') for action in sent)
    assert a.state['armed'] is False and a.state['estop'] is True
    assert client.post('/api/heartbeat',json={}).json()['active'] is False
    saved=json.loads((path/'task.json').read_text(encoding='utf-8'))
    assert saved['status']=='paused' and saved['goal']=='Preserve this goal'
