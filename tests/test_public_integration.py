"""Exercise the real ASGI app while sharing test_safety's isolated data import."""
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from test_safety import a


@pytest.fixture
def public(monkeypatch, tmp_path):
    monkeypatch.setattr(a, 'settings', {**a.settings, 'transport':'lan', 'simulation':True})
    monkeypatch.setattr(a, 'state', {**a.state, 'armed':False, 'owner':None, 'epoch':0, 'estop':False})
    monkeypatch.setattr(a, 'mission', {**a.mission, 'goal':'', 'status':'idle', 'recent':[]})
    monkeypatch.setattr(a, 'sessions', {})
    monkeypatch.setattr(a, 'login_failures', {})
    monkeypatch.setattr(a, 'listeners', set())
    monkeypatch.setattr(a, 'floor_seen', 0)
    monkeypatch.setattr(a, 'CONFIG_PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(a, 'MISSION_PATH', tmp_path / 'mission.json')
    with TestClient(a.app, base_url='https://rover.example.com') as client:
        yield client


def login(client):
    response = client.post('/kvido/api/login', json={'key':a.ACCESS_KEY})
    assert response.status_code == 200, response.text
    return response


def test_public_login_cookie_static_and_api(public):
    assert public.get('/kvido/api/status').status_code == 401
    response = login(public)
    cookie = response.headers['set-cookie'].lower()
    assert 'kvido_session=' in cookie
    assert 'path=/kvido' in cookie and 'secure' in cookie
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert public.get('/kvido/api/status').status_code == 200
    # The public credential must not leak into another service on this host.
    assert public.get('/api/status').status_code == 401
    assert public.get('/kvido/').status_code == 200
    script = public.get('/kvido/static/app.js')
    assert script.status_code == 200 and 'javascript' in script.headers['content-type']
    assert public.get('/kvido/static/style.css').status_code == 200
    redirect = public.get('/kvido', follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers['location'] == '/kvido/'


def test_fresh_camera_query_for_radar_bypasses_preview_only(public, monkeypatch):
    import httpx
    login(public)
    monkeypatch.setitem(a.settings, 'simulation', False)
    calls=[]
    async def capture(path, **kwargs):
        calls.append((path,kwargs.get('preview')))
        return httpx.Response(200,content=b'jpeg')
    monkeypatch.setattr(a,'device',capture)
    monkeypatch.setattr(a,'oriented_car_jpeg',lambda jpeg:jpeg)
    for url in ('/kvido/api/camera.jpg','/kvido/api/camera.jpg?fresh=1'):
        assert public.get(url).content == b'jpeg'
    assert calls == [('/capture',True),('/capture',False)]


@pytest.mark.parametrize('headers', [
    {},
    {'Authorization':'Bearer invalid'},
    {'Authorization':'Bearer ' + a.RELAY_KEY, 'Origin':'https://rover.example.com'},
])
def test_public_websocket_rejects_unauthorized_and_browser(public, headers):
    with pytest.raises(WebSocketDisconnect) as error:
        with public.websocket_connect('/kvido/device-ws', headers=headers):
            pytest.fail('Device socket accepted an unauthorized or browser connection')
    assert error.value.code == 1008


def test_relay_selection_never_falls_back_to_lan(public, monkeypatch):
    login(public)
    monkeypatch.setitem(a.settings, 'transport', 'relay')
    monkeypatch.setitem(a.settings, 'simulation', False)
    monkeypatch.setitem(a.settings, 'device_url', 'http://192.168.50.80:24500')
    monkeypatch.setitem(a.settings, 'device_key', 'lan-device-secret')
    calls = []
    async def forbidden_lan(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('Relay selection attempted a LAN request')
    monkeypatch.setattr(a.client, 'request', forbidden_lan)
    response = public.get('/kvido/api/status')
    assert response.status_code == 200
    assert response.json()['transport'] == 'relay'
    assert response.json()['telemetry']['telemetry_age_ms'] >= 999999
    async def capture_and_control():
        for path, method, payload in [('/capture','GET',None),('/api/control','POST',{'action':'stop'})]:
            with pytest.raises(HTTPException) as error:
                await a.device(path, method, payload)
            assert error.value.status_code == 502
    public.portal.call(capture_and_control)
    assert calls == []


def test_real_device_link_invalidates_lease_and_mission(public, monkeypatch):
    monkeypatch.setitem(a.settings, 'transport', 'relay')
    monkeypatch.setitem(a.settings, 'simulation', False)
    def prime():
        a.state.update(armed=True, owner='operator', heartbeat=time.monotonic(), epoch=10, estop=False)
        a.sessions['operator'] = time.time()+60
        a.mission.update(goal='Find a cat', status='running')
        a.floor_seen = time.monotonic()
    prime()
    with public.websocket_connect('/kvido/device-ws', headers={'Authorization':'Bearer '+a.RELAY_KEY}) as ws:
        ws.send_json({'type':'hello','protocol':1,'device_id':'kvido-e465b8788f40','boot_id':'integration-test'})
        assert ws.receive_json()['type'] == 'welcome'
        assert a.state['epoch'] == 11 and not a.state['armed']
        assert a.state['owner'] is None and a.state['estop']
        assert a.mission['goal'] == 'Find a cat' and a.mission['status'] == 'paused'
        assert a.floor_seen == 0
        prime()
    # The test client's websocket context waits for ASGI connection cleanup.
    assert a.state['epoch'] == 11 and not a.state['armed']
    assert a.state['owner'] is None and a.state['estop']
    assert a.mission['status'] == 'paused'
    assert json.loads(a.MISSION_PATH.read_text())['goal'] == 'Find a cat'


def test_settings_accepts_explicit_relay_transport(public):
    login(public)
    response = public.post('/kvido/api/settings', json={'transport':'relay', 'simulation':True})
    assert response.status_code == 200, response.text
    assert public.get('/kvido/api/settings').json()['transport'] == 'relay'
    assert json.loads(a.CONFIG_PATH.read_text())['transport'] == 'relay'
    assert public.post('/kvido/api/settings', json={'transport':'unexpected'}).status_code == 422
