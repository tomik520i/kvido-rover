import asyncio
import importlib
import os
import tempfile
import time
import pytest
from fastapi.testclient import TestClient

os.environ['ROVER_DATA']=tempfile.mkdtemp(prefix='rover-tests-')
a=importlib.import_module('server.app')

def decision(**kw):
    fields=dict(say='Vidím cestu.',action='forward',speed=120,duration_ms=200,tilt=90,
                reason='Volno.',memory='',hazard='clear',traversable=True,confidence=.95,
                goal_status='continue',evidence='Podlaha bez překážek.')
    fields.update(kw);return a.Decision(**fields)

def allowed(d=None,**kw):
    params=dict(mode='ai',image_source='car',image_age=1,armed=True)
    params.update(kw)
    return a.movement_allowed(d or decision(),{'distance_cm':100,'telemetry_age_ms':20},**params)[0]

@pytest.mark.parametrize('params',[{'mode':'manual'},{'armed':False},{'image_source':'phone'},{'image_source':'none'},{'image_age':9}])
def test_motor_guards(params):assert allowed(**params)=='stop'

@pytest.mark.parametrize('dist,age',[(None,0),(10,0),(500,0),(100,501),(True,0)])
def test_invalid_sensor(dist,age):
    assert a.movement_allowed(decision(),{'distance_cm':dist,'telemetry_age_ms':age},mode='ai',image_source='car',image_age=1,armed=True)[0]=='stop'

@pytest.mark.parametrize('kw',[{'hazard':'drop'},{'hazard':'unknown'},{'traversable':False},{'confidence':.6},{'action':'backward'}])
def test_unsafe_visual_or_action(kw):assert allowed(decision(**kw))=='stop'

@pytest.mark.parametrize('url',['http://127.0.0.1:8080','http://169.254.169.254:8080','https://192.168.50.2:8080','http://example.org:8080','http://192.168.50.2:8080/path','http://u:p@192.168.50.2:8080'])
def test_device_target_restrictions(url):
    with pytest.raises(Exception):a.validate_device_url(url)

@pytest.fixture
def c():
    a.state.update(mode='manual',armed=False,estop=False,owner=None,epoch=0)
    a.mission.update(goal='',status='idle',steps=0,recent=[])
    a.settings['simulation']=True;a.floor_seen=0
    with TestClient(a.app) as client:
        assert client.post('/api/login',json={'key':a.ACCESS_KEY}).status_code==200
        yield client

def test_auth_and_csrf(c):
    assert c.post('/api/arm',json={},headers={'Origin':'https://evil.example'}).status_code==403
    with TestClient(a.app) as other:assert other.get('/api/status').status_code==401

def test_deadman_and_estop(c):
    assert c.post('/api/arm',json={}).status_code==200
    assert c.post('/api/drive',json={'action':'forward'}).status_code==200
    time.sleep(1.8)
    assert c.post('/api/drive',json={'action':'forward'}).status_code==409
    assert c.post('/api/heartbeat',json={}).json()['active'] is False
    c.post('/api/arm',json={});c.post('/api/stop',json={})
    assert c.post('/api/drive',json={'action':'backward'}).status_code==409

def test_second_operator_cannot_steal(c):
    c.post('/api/arm',json={})
    original=c.cookies.get('kvido_session')
    c.post('/api/login',json={'key':a.ACCESS_KEY})
    assert c.cookies.get('kvido_session')!=original
    assert c.post('/api/arm',json={}).status_code==409

def test_observe_never_drives_even_if_ai_asks(c,monkeypatch):
    async def fake(_,schema=None):return decision()
    monkeypatch.setattr(a,'infer',fake)
    c.post('/api/mode',json={'mode':'ai'});c.post('/api/arm',json={})
    a.floor_seen=time.monotonic()
    r=c.post('/api/turn',json={'intent':'observe'})
    assert r.status_code==200,r.text
    assert r.json()['executed_action']=='stop'

def test_stop_during_inference_cancels_motion(c,monkeypatch):
    async def fake(_,schema=None):
        await a.halt('Test emergency',True)
        return decision()
    monkeypatch.setattr(a,'infer',fake)
    c.post('/api/mode',json={'mode':'ai'});c.post('/api/arm',json={})
    a.floor_seen=time.monotonic()
    r=c.post('/api/turn',json={'intent':'act'})
    assert r.json()['executed_action']=='stop'
    assert a.sim['estop']

def test_floor_check_required_and_invalidated(c,monkeypatch):
    async def fake(_,schema=None):return decision()
    monkeypatch.setattr(a,'infer',fake)
    c.post('/api/mode',json={'mode':'ai'});c.post('/api/arm',json={})
    a.sim['servo']=90
    assert c.post('/api/turn',json={'intent':'act'}).json()['executed_action']=='stop'
    a.sim['servo']=55
    assert c.post('/api/turn',json={'intent':'act'}).json()['executed_action']=='forward'
    assert not a.navigation()['floor_valid']

def test_task_survives_disconnect(c):
    c.post('/api/mode',json={'mode':'ai'});c.post('/api/arm',json={})
    assert c.post('/api/mission',json={'action':'start','goal':'Najdi kočku'}).status_code==200
    c.post('/api/stop',json={})
    s=c.get('/api/status').json()
    assert s['mission']['goal']=='Najdi kočku'
    assert s['mission']['status']=='paused'
    assert a.MISSION_PATH.exists()

def test_phone_cannot_execute_motion(c,monkeypatch):
    async def fake(_,schema=None):return decision()
    monkeypatch.setattr(a,'infer',fake)
    c.post('/api/mode',json={'mode':'ai'});c.post('/api/arm',json={})
    import base64
    image='data:image/jpeg;base64,'+base64.b64encode(a.simulation_jpeg()).decode()
    r=c.post('/api/turn',json={'source':'phone','image':image,'intent':'act'})
    assert r.json()['executed_action']=='stop'

def test_bad_output_fails_closed(c,monkeypatch):
    async def fake(_,schema=None):raise ValueError('invalid JSON')
    monkeypatch.setattr(a,'infer',fake)
    c.post('/api/arm',json={})
    assert c.post('/api/turn',json={}).status_code==502
    assert not a.state['armed']

def test_memory_crud(c):
    c.post('/api/memory',json={'text':'Říkej mi Tomáši.'})
    items=c.get('/api/memory').json()['items'];assert items[0]['text']=='Říkej mi Tomáši.'
    assert c.delete('/api/memory/'+str(items[0]['id'])).status_code==200

def test_original_voice_capture_preserved():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    if not (root.parent/'inspection/verity').is_dir():
        pytest.skip('optional private upstream voice snapshot is not distributed')
    for name in ('live-audio.js','pcm-worklet.js'):
        assert (root/'server/static'/name).read_bytes()==(root.parent/'inspection/verity'/name).read_bytes()

def test_owner_can_drive_in_ai_mode_for_alpha(c):
    assert c.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert c.post('/api/drive',json={'action':'forward'}).status_code==409  # not armed
    assert c.post('/api/arm',json={}).status_code==200
    assert c.post('/api/drive',json={'action':'forward'}).status_code==200
    c.post('/api/stop',json={})
    assert c.post('/api/drive',json={'action':'forward'}).status_code==409


@pytest.mark.parametrize('telemetry,clearance,duration,allowed', [
    ({'distance_cm': None, 'camera_clearance_min_cm': 40, 'esp_camera_clearance': True}, 80, 300, True),
    ({'distance_cm': None, 'camera_clearance_min_cm': 40, 'esp_camera_clearance': True}, 39, 300, False),
    ({'distance_cm': None, 'camera_clearance_min_cm': 40, 'esp_camera_clearance': True}, 80, 400, False),
    ({'distance_cm': None, 'camera_clearance_min_cm': None, 'esp_camera_clearance': True}, 80, 300, False),
    ({'distance_cm': None, 'camera_clearance_min_cm': 40}, 80, 300, False),
    ({'distance_cm': 15, 'camera_clearance_min_cm': 40, 'esp_camera_clearance': True}, 200, 300, False),
])
def test_camera_clearance_only_replaces_a_missing_echo(c, monkeypatch, telemetry, clearance, duration, allowed):
    sent = []

    async def fake_telemetry():
        return {'telemetry_age_ms': 50, 'connected': True, 'motor_max_duration_ms': 350, 'forward_stop_cm': 20, **telemetry}

    async def fake_send(action='stop', speed=0, duration_ms=100, tilt=90, clearance_cm=0):
        sent.append((action, clearance_cm))
    monkeypatch.setattr(a, 'telemetry', fake_telemetry)
    monkeypatch.setattr(a, 'send_control', fake_send)
    assert c.post('/api/arm', json={}).status_code == 200
    r = c.post('/api/drive', json={'action': 'forward', 'speed': 180, 'duration_ms': duration, 'clearance_cm': clearance})
    assert (r.status_code == 200) == allowed
    if allowed:
        assert r.json()['camera_only'] is True and sent[-1] == ('forward', clearance)
    else:
        assert ('forward', clearance) not in sent
