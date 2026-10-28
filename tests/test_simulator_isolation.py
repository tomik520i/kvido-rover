"""Exercise the isolated server without hardware or external inference."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from test_safety import a,decision
from server.simulator import RoverSimulator


@pytest.fixture
def simulator_client(monkeypatch,tmp_path):
    for name in ('client','relay','ai_provider','task_runner','task_pending','diagnostics'):
        monkeypatch.setattr(a,name,None)
    monkeypatch.setattr(a,'SIMULATION_ONLY',True)
    monkeypatch.setattr(a,'simulator_scene_started_at',0.0)
    monkeypatch.setattr(a,'simulator_engine',RoverSimulator())
    monkeypatch.setattr(a,'DATA',tmp_path)
    monkeypatch.setattr(a,'MISSION_PATH',tmp_path/'mission.json')
    monkeypatch.setattr(a,'CONFIG_PATH',tmp_path/'settings.json')
    monkeypatch.setattr(a,'settings',{**a.settings,'simulation':True,'transport':'lan',
        'device_url':'','device_key':'','navigation_policy':'sensor','camera_rotate_180':False})
    monkeypatch.setattr(a,'state',{**a.state,'mode':'manual','armed':False,'estop':False,
        'owner':None,'heartbeat':0,'epoch':0,'busy':False})
    monkeypatch.setattr(a,'mission',dict(goal='',status='idle',steps=0,recent=[],started_at=0,reason=''))
    for name,value in [('sessions',{}),('login_failures',{}),('listeners',set()),
                       ('vision_history',type(a.vision_history)()),('floor_seen',0)]:
        monkeypatch.setattr(a,name,value)
    for name in ('motion_lock','turn_lock','capture_lock'):
        monkeypatch.setattr(a,name,asyncio.Lock())
    db=sqlite3.connect(tmp_path/'memory.sqlite3',check_same_thread=False)
    db.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY,role TEXT,content TEXT,at REAL);
        CREATE TABLE memory(id INTEGER PRIMARY KEY,text TEXT,source TEXT,at REAL);
        CREATE TABLE decisions(id INTEGER PRIMARY KEY,payload TEXT,at REAL);''')
    monkeypatch.setattr(a,'db',db)
    # Any accidental HTTP request, including inference, fails before network I/O.
    async def forbidden_request(*args,**kwargs):
        pytest.fail('Isolated simulator attempted external HTTP I/O')
    monkeypatch.setattr(a.httpx.AsyncClient,'request',forbidden_request)
    with TestClient(a.app) as client:
        assert client.post('/api/login',json={'key':a.ACCESS_KEY}).status_code==200
        yield client
    db.close()


@pytest.fixture
def operator_clock(simulator_client,monkeypatch):
    # Limit tests exercise actuation, so camera CPU time must not expire their
    # operator lease. Replace only app.time; asyncio and simulator clocks stay real.
    clock={'now':a.time.monotonic()}
    monkeypatch.setattr(a,'time',SimpleNamespace(time=a.time.time,monotonic=lambda:clock['now']))
    return clock


def test_process_guard_overrides_persisted_real_hardware_configuration(tmp_path):
    (tmp_path/'settings.json').write_text(json.dumps(dict(simulation=False,transport='relay',
        device_url='http://192.168.50.7:8080',device_key='test-secret')),encoding='utf-8')
    program='''import json,os,sys
os.environ['ROVER_SIMULATION_ONLY']='1'
os.environ['ROVER_DATA']=sys.argv[1]
from server import app
assert app.SIMULATION_ONLY
assert app.DATA.resolve()==__import__('pathlib').Path(sys.argv[1]).resolve()
assert app.settings['simulation'] is True
assert app.settings['transport']=='lan'
assert app.settings['device_url']==app.settings['device_key']==''
print('isolated')
'''
    result=subprocess.run([sys.executable,'-c',program,str(tmp_path)],cwd=Path(__file__).resolve().parents[1],
        capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='isolated'


@pytest.mark.parametrize('simulation_only',[True,False])
def test_startup_discards_old_scene_history_only_in_isolated_simulator(tmp_path,simulation_only):
    old_task=dict(id='old-scene',status='paused',phase='paused',goal='Old scene goal',
        current_step=3,last_result={'id':'old-step'},completed_steps=[{'action':'forward'}],elapsed_s=12)
    old_mission=dict(goal='Old scene goal',status='paused',steps=3,
        recent=[{'executed':'forward','say':'The doorway is behind me.'}],started_at=123,reason='Old world')
    (tmp_path/'task.json').write_text(json.dumps(old_task),encoding='utf-8')
    (tmp_path/'mission.json').write_text(json.dumps(old_mission),encoding='utf-8')
    program='''import json,os,sys
os.environ['ROVER_SIMULATION_ONLY']=sys.argv[2]
os.environ['ROVER_DATA']=sys.argv[1]
from fastapi.testclient import TestClient
from server import app
async def forbidden_request(*args,**kwargs):
    raise AssertionError('Startup must not contact external services')
app.httpx.AsyncClient.request=forbidden_request
app.task_pending={'_before_image':'old-scene'}
app.vision_history._frames.append({'image':'old-scene'})
app.vision_history.command('forward',120,300,90)
app.db.execute('INSERT INTO messages(role,content,at) VALUES(?,?,?)',('assistant','OLD_SCENE_CHAT',0))
app.db.executemany('INSERT INTO memory(text,source,at) VALUES(?,?,?)',[
    ('OLD_SCENE_AI_NOTE','AI pozorování · simulator',0),('MANUAL_NOTE','Uživatel',0)])
app.db.commit()
with TestClient(app.app):
    task=app.task_runner.snapshot()
    saved_task=json.loads((app.DATA/'task.json').read_text(encoding='utf-8'))
    saved_mission=json.loads(app.MISSION_PATH.read_text(encoding='utf-8'))
    if app.SIMULATION_ONLY:
        for value in (task,saved_task):
            assert value['status']=='idle' and value['goal']==''
            assert value['id'] is None and value['current_step']==0
            assert value['last_result'] is None and value['completed_steps']==[]
            assert value['elapsed_s']==0
        for value in (app.mission,saved_mission):
            assert value['status']=='idle' and value['goal']==''
            assert value['steps']==0 and value['recent']==[] and value['started_at']==0
        assert app.task_pending is None
        assert not app.vision_history._frames and not app.vision_history._pending
        assert app.simulator().snapshot()['pose']=={'x':5.6,'y':3.,'heading':0.}
    else:
        for value in (task,saved_task):
            assert value['goal']=='Old scene goal' and value['current_step']==3
            assert value['last_result']=={'id':'old-step'}
        for value in (app.mission,saved_mission):
            assert value['goal']=='Old scene goal' and value['steps']==3
            assert value['recent'][0]['executed']=='forward'
        assert app.task_pending=={'_before_image':'old-scene'}
        assert app.vision_history._frames and app.vision_history._pending
    now=app.time.time()
    app.db.execute('INSERT INTO messages(role,content,at) VALUES(?,?,?)',('assistant','NEW_SCENE_CHAT',now))
    app.db.execute('INSERT INTO memory(text,source,at) VALUES(?,?,?)',('NEW_SCENE_AI_NOTE','AI pozorování · simulator',now))
    app.db.commit()
    context=json.dumps(app.messages_for('Describe', 'manual','none',{},app.history(),app.memory(),None),ensure_ascii=False)
    assert 'MANUAL_NOTE' in context and 'NEW_SCENE_CHAT' in context and 'NEW_SCENE_AI_NOTE' in context
    assert ('OLD_SCENE_CHAT' in context)==(not app.SIMULATION_ONLY)
    assert ('OLD_SCENE_AI_NOTE' in context)==(not app.SIMULATION_ONLY)
    assert app.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==2
    assert app.db.execute('SELECT COUNT(*) FROM memory').fetchone()[0]==3
print('startup verified')
'''
    result=subprocess.run([sys.executable,'-c',program,str(tmp_path),'1' if simulation_only else '0'],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='startup verified'


@pytest.mark.parametrize('simulation_only',[True,False])
def test_scene_reset_filters_sql_context_but_keeps_manual_notes_and_stored_rows(simulator_client,monkeypatch,simulation_only):
    old_time=a.time.time()
    a.db.execute('INSERT INTO messages(role,content,at) VALUES(?,?,?)',('assistant','OLD_SCENE_CHAT',old_time))
    a.db.executemany('INSERT INTO memory(text,source,at) VALUES(?,?,?)',[
        ('OLD_SCENE_AI_NOTE','AI pozorování · car',old_time),
        ('MANUAL_NOTE','Uživatel',0)])
    a.db.commit()
    if simulation_only:
        response=simulator_client.post('/api/simulator/reset',json={})
        assert response.status_code==200,response.text
        assert a.simulator_scene_started_at>old_time
    else:
        monkeypatch.setattr(a,'SIMULATION_ONLY',False)
        # A stale simulator cutoff must have no effect on a normal instance.
        a.simulator_scene_started_at=old_time+1000
    current=a.simulator_scene_started_at if simulation_only else old_time
    a.db.execute('INSERT INTO messages(role,content,at) VALUES(?,?,?)',('assistant','NEW_SCENE_CHAT',current))
    a.db.execute('INSERT INTO memory(text,source,at) VALUES(?,?,?)',('NEW_SCENE_AI_NOTE','AI pozorování · simulator',current))
    a.db.commit()
    contexts=[]
    async def infer(messages,*args,**kwargs):
        contexts.append(json.dumps(messages,ensure_ascii=False))
        return decision(action='stop',say='Current scene only')
    monkeypatch.setattr(a,'infer',infer)
    result=simulator_client.post('/api/turn',json={'intent':'observe','vision':False})
    assert result.status_code==200,result.text
    assert len(contexts)==1
    context=contexts[0]
    assert 'NEW_SCENE_CHAT' in context and 'NEW_SCENE_AI_NOTE' in context and 'MANUAL_NOTE' in context
    assert ('OLD_SCENE_CHAT' in context)==(not simulation_only)
    assert ('OLD_SCENE_AI_NOTE' in context)==(not simulation_only)
    visible=simulator_client.get('/api/memory').json()
    assert any(item['text']=='MANUAL_NOTE' for item in visible['items'])
    assert a.db.execute("SELECT COUNT(*) FROM messages WHERE content='OLD_SCENE_CHAT'").fetchone()[0]==1
    assert a.db.execute("SELECT COUNT(*) FROM memory WHERE text='OLD_SCENE_AI_NOTE'").fetchone()[0]==1


def test_reset_waits_for_inflight_turn_before_starting_new_context(simulator_client,monkeypatch):
    started=threading.Event();reset_waiting=threading.Event();pending={};contexts=[]
    class ObservedLock(asyncio.Lock):
        async def acquire(self):
            if self.locked():reset_waiting.set()
            return await super().acquire()
    monkeypatch.setattr(a,'turn_lock',ObservedLock())
    async def infer(messages,*args,**kwargs):
        contexts.append(json.dumps(messages,ensure_ascii=False))
        if len(contexts)==1:
            pending['loop']=asyncio.get_running_loop()
            pending['release']=asyncio.Event()
            started.set()
            await pending['release'].wait()
            return decision(action='forward',say='INFLIGHT_OLD_SCENE_OBSERVATION')
        return decision(action='stop',say='Fresh scene')
    monkeypatch.setattr(a,'infer',infer)
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    with ThreadPoolExecutor(max_workers=2) as pool:
        turn=pool.submit(simulator_client.post,'/api/turn',json={'intent':'act'})
        assert started.wait(2)
        reset=pool.submit(simulator_client.post,'/api/simulator/reset',json={'scenario':'obstacle'})
        try:
            assert reset_waiting.wait(2),'Reset did not wait for the active turn lock'
            assert not reset.done()
        finally:
            pending['loop'].call_soon_threadsafe(pending['release'].set)
        result=turn.result(timeout=3)
        reset_result=reset.result(timeout=3)
    assert result.status_code==200,result.text
    assert result.json()['executed_action']=='stop'
    assert reset_result.status_code==200,reset_result.text
    old_at=a.db.execute("SELECT at FROM messages WHERE content='INFLIGHT_OLD_SCENE_OBSERVATION'").fetchone()[0]
    assert old_at<a.simulator_scene_started_at
    assert not a.history()
    assert not a.mission['recent'] and not a.vision_history._frames
    status=simulator_client.get('/api/status').json()
    assert status['armed'] is False and status['telemetry']['drive']['accepted']==0
    fresh=simulator_client.post('/api/turn',json={'intent':'observe','vision':False})
    assert fresh.status_code==200,fresh.text
    assert len(contexts)==2 and 'INFLIGHT_OLD_SCENE_OBSERVATION' not in contexts[1]


@pytest.mark.parametrize('updates',[{'simulation':False},{'transport':'relay'},
    {'device_url':'http://192.168.50.7:8080'},{'device_key':'test-device-key'}])
def test_settings_cannot_attach_hardware(simulator_client,updates):
    response=simulator_client.post('/api/settings',json={'simulation':True,**updates})
    assert response.status_code==409,response.text
    assert a.settings['simulation'] is True
    assert a.settings['transport']=='lan'
    assert a.settings['device_url']==a.settings['device_key']==''


def test_hardware_routes_block_even_after_accidental_config_corruption(simulator_client):
    a.settings.update(simulation=False,transport='relay',device_url='http://192.168.50.7:8080',device_key='secret')
    for path in ('/api/status','/api/control','/capture'):
        with pytest.raises(HTTPException) as error:
            asyncio.run(a.device(path))
        assert error.value.status_code==409
    asyncio.run(a.send_control('release'))
    asyncio.run(a.send_control('rotate_left',120,1000))
    assert asyncio.run(a.telemetry())['simulation'] is True
    assert a.simulation_jpeg().startswith(b'\xff\xd8')
    with pytest.raises(WebSocketDisconnect) as error:
        with simulator_client.websocket_connect('/device-ws'):
            pytest.fail('Device websocket must never be accepted by the isolated server')
    assert error.value.code==1008


def test_simulator_endpoints_require_login(simulator_client):
    simulator_client.cookies.clear()
    assert simulator_client.get('/api/simulator').status_code==401
    assert simulator_client.post('/api/simulator/reset',json={}).status_code==401


def test_scene_and_camera_are_available_without_external_services(simulator_client):
    scene=simulator_client.get('/api/simulator')
    assert scene.status_code==200,scene.text
    assert scene.json()['simulation_only'] is True
    assert {item['id'] for item in scene.json()['scenarios']}=={'doorway','obstacle','stuck','dark','apartment'}
    camera=simulator_client.get('/api/camera.jpg')
    assert camera.status_code==200
    assert camera.headers['content-type']=='image/jpeg'
    assert camera.content.startswith(b'\xff\xd8')


def test_reset_stops_motion_disarms_and_discards_the_old_goal(simulator_client,monkeypatch):
    started=threading.Event()
    async def waiting_step(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(a.task_runner,'step',waiting_step)
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    assert simulator_client.post('/api/task',json={'goal':'Old scene goal'}).status_code==200
    assert started.wait(1)
    a.task_runner._frames={'before':b'old-scene'}
    a.task_runner.data['last_result']={'id':'old-step'}
    a.task_pending={'_before_image':'old-scene'}
    asyncio.run(a.send_control('forward',180,2000))
    response=simulator_client.post('/api/simulator/reset',json={'scenario':'obstacle','seed':17})
    assert response.status_code==200,response.text
    scene=response.json()['state']
    assert scene['scenario']=='obstacle' and scene['seed']==17
    assert scene['action']=='stop' and scene['speed']==0
    status=simulator_client.get('/api/status').json()
    assert status['armed'] is False
    assert status['mission']['goal']=='' and status['mission']['status']=='idle'
    assert status['task']['status'] in ('cancelled','idle')
    assert status['task']['goal']==''
    assert status['task']['last_result'] is None
    assert a.task_runner.frame('before') is None
    assert a.task_pending is None


@pytest.mark.parametrize('action,expected_speed,expected_ms',[
    ('forward',180,2000),('backward',180,1000),('left',180,1000),
    ('right',180,1000),('rotate_left',150,2000),('rotate_right',150,2000),
])
def test_ai_actuation_uses_verified_direction_limits(simulator_client,operator_clock,monkeypatch,action,expected_speed,expected_ms):
    async def infer(*args,**kwargs):
        return decision(action=action,speed=180,duration_ms=2000,turn_clear=True)
    monkeypatch.setattr(a,'infer',infer)
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    result=simulator_client.post('/api/turn',json={'intent':'act'})
    assert result.status_code==200,result.text
    assert result.json()['executed_action']==action,result.json().get('execution_reason',result.text)
    assert result.json()['speed']==expected_speed
    assert result.json()['duration_ms']==expected_ms
    actual=simulator_client.get('/api/status').json()['telemetry']['drive']
    assert actual['ok'] is True
    assert actual['speed']==expected_speed and actual['ms']==expected_ms


@pytest.mark.parametrize('policy,action,capabilities,expected_speed,expected_ms',[
    ('sensor','forward',False,180,350),('sensor','backward',False,180,350),
    ('sensor','rotate_right',False,150,350),('strict','forward',True,150,300),
    ('strict','rotate_left',True,80,100),
])
def test_actuator_clamps_model_ignoring_legacy_or_strict_limits(simulator_client,operator_clock,monkeypatch,
        policy,action,capabilities,expected_speed,expected_ms):
    a.settings['navigation_policy']=policy
    a.simulator_engine.servo=55
    original=a.telemetry
    async def reported_telemetry():
        t=await original()
        if not capabilities:
            t.pop('motor_max_duration_ms',None)
            t.pop('forward_stop_cm',None)
        return t
    async def infer(*args,**kwargs):
        return decision(action=action,speed=180,duration_ms=2000,turn_clear=True)
    monkeypatch.setattr(a,'telemetry',reported_telemetry)
    monkeypatch.setattr(a,'infer',infer)
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    result=simulator_client.post('/api/turn',json={'intent':'act'})
    assert result.status_code==200,result.text
    assert result.json()['executed_action']==action,result.json().get('execution_reason',result.text)
    actual=simulator_client.get('/api/status').json()['telemetry']['drive']
    assert actual['ok'] is True
    assert actual['speed']==expected_speed and actual['ms']==expected_ms


def test_controlled_operator_clock_still_expires_the_real_actuator_guard(simulator_client,operator_clock,monkeypatch):
    async def infer(*args,**kwargs):
        operator_clock['now']+=2
        return decision(action='forward',speed=180,duration_ms=2000)
    monkeypatch.setattr(a,'infer',infer)
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    response=simulator_client.post('/api/turn',json={'intent':'act'})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['executed_action']=='stop',result.get('execution_reason',response.text)
    assert result['execution_reason']=='Řízení není aktivní.'
    assert a.simulator_engine.telemetry()['drive']['accepted']==0
