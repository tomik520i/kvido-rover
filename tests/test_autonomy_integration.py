"""Exercise ownership, persistence and browser independence in the real ASGI app."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from test_safety import a


@pytest.fixture
def cockpit(monkeypatch, tmp_path):
    monkeypatch.setattr(a, 'DATA', tmp_path)
    monkeypatch.setattr(a, 'settings', {**a.settings, 'simulation': True, 'transport': 'lan'})
    monkeypatch.setattr(a, 'state', {**a.state, 'armed': False, 'owner': None, 'mode': 'ai', 'epoch': 0, 'estop': False})
    monkeypatch.setattr(a, 'sessions', {})
    monkeypatch.setattr(a, 'listeners', set())
    monkeypatch.setattr(a, 'motion_lock', asyncio.Lock())

    async def radar(action, body):
        return {'run': 'test-run'} if action == 'start' else {'pending': True}

    monkeypatch.setattr(a, 'autonomy_radar', radar)
    with TestClient(a.app) as client:
        client.post('/api/login', json={'key': a.ACCESS_KEY}).raise_for_status()
        yield client


def wait_for(predicate, timeout=4):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def test_start_without_arm_survives_page_release_session_expiry_and_reload(cockpit):
    started = cockpit.post('/api/autonomy', json={'goal': 'Najdi misku'}).json()
    assert started['goal'] == 'Najdi misku'
    wait_for(lambda: a.state['armed'] and a.state['owner'] == a.alpha_runner.owner)
    original = cockpit.cookies.get('kvido_session')
    assert cockpit.post('/api/release', json={}).status_code == 200
    # No browser heartbeats, and the originating login is gone.
    a.sessions.pop(original)
    time.sleep(1.8)
    assert a.state['armed'] and a.alpha_runner.active
    cockpit.post('/api/login', json={'key': a.ACCESS_KEY}).raise_for_status()
    state = cockpit.get('/api/status').json()
    assert state['autonomy']['id'] == started['id']
    assert state['autonomy']['goal'] == 'Najdi misku'
    assert state['autonomy']['history']
    assert not state['mine']
    assert a.alpha_runner.owner not in json.dumps(state)
    cockpit.post('/api/stop', json={}).raise_for_status()
    saved = json.loads((a.DATA/'autonomy.json').read_text(encoding='utf8'))
    assert saved['status'] == 'cancelled'
    assert not a.state['armed']


def test_active_and_reconnecting_tasks_exclude_other_controllers(cockpit, monkeypatch):
    async def offline():
        return {'connected': False, 'telemetry_age_ms': 999999}

    monkeypatch.setattr(a, 'telemetry', offline)
    assert cockpit.post('/api/autonomy', json={'goal': 'Najdi dveře'}).status_code == 200
    wait_for(lambda: a.alpha_runner.data['status'] == 'reconnecting')
    for path, body in [('/api/autonomy', {'goal': 'druhý úkol'}), ('/api/arm', {}),
                       ('/api/task', {'goal': 'starý řadič'}),
                       ('/api/mission', {'action': 'start', 'goal': 'jiný'}),
                       ('/radar/autopilot/start', {'goal': 'jiný'}),
                       ('/api/turn', {'text': 'jeď', 'intent': 'act'})]:
        assert cockpit.post(path, json=body).status_code == 409, path
    cockpit.post('/api/autonomy/cancel', json={}).raise_for_status()
    assert cockpit.get('/api/autonomy').json()['status'] == 'cancelled'


def test_pause_resume_and_explicit_mode_change(cockpit):
    cockpit.post('/api/autonomy', json={'goal': 'Najdi misku'}).raise_for_status()
    wait_for(lambda: a.state['armed'])
    assert cockpit.post('/api/autonomy/pause', json={}).json()['status'] == 'paused'
    assert not a.state['armed']
    assert cockpit.post('/api/autonomy/resume', json={}).status_code == 200
    wait_for(lambda: a.state['armed'])
    cockpit.post('/api/mode', json={'mode': 'manual'}).raise_for_status()
    assert a.alpha_runner.data['status'] == 'cancelled'
    assert not a.state['armed']


def test_stop_during_awaited_telemetry_invalidates_queued_motion(cockpit, monkeypatch):
    cockpit.post('/api/autonomy', json={'goal': 'Najdi misku'}).raise_for_status()
    wait_for(lambda: a.state['armed'])
    calls = []

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        old = a.telemetry
        async def delayed():
            entered.set()
            await finish.wait()
            return {'connected': True, 'telemetry_age_ms': 0, 'distance_cm': 100}
        async def send(action='stop', *args, **kwargs):
            calls.append(action)
        monkeypatch.setattr(a, 'telemetry', delayed)
        monkeypatch.setattr(a, 'send_control', send)
        epoch = a.state['epoch']
        queued = asyncio.create_task(a.autonomy_drive('forward', 150, 0, 72, 300, epoch))
        await entered.wait()
        stop = asyncio.create_task(a.halt('Nouzové zastavení uživatelem.', emergency=True))
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(queued, return_exceptions=True)
        await stop
        monkeypatch.setattr(a, 'telemetry', old)
    cockpit.portal.call(scenario)
    assert 'forward' not in calls
    assert not a.state['armed']


def test_browser_arm_queued_before_autonomy_cannot_take_its_control(cockpit):
    sid = cockpit.cookies.get('kvido_session')
    async def scenario():
        async with a.motion_lock:
            queued = asyncio.create_task(a.take_control(sid))
            await asyncio.sleep(0)
            a.alpha_runner.start('Najdi misku')
        result = await asyncio.gather(queued, return_exceptions=True)
        assert getattr(result[0], 'status_code', None) == 409
        assert a.state['owner'] != sid
        await a.halt('Test ukončen.', emergency=True)
    cockpit.portal.call(scenario)
