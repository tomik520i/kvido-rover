"""Cockpit proxy for the read-only radar dashboard (radar_service on the host)."""
import pytest
from fastapi.testclient import TestClient

from test_safety import a


class FakeResponse:
    def __init__(self, content, kind):
        self.content, self.status_code, self.headers = content, 200, {'content-type': kind}


@pytest.fixture
def cockpit(monkeypatch, tmp_path):
    monkeypatch.setattr(a, 'settings', {**a.settings, 'transport': 'lan', 'simulation': True})
    monkeypatch.setattr(a, 'sessions', {})
    monkeypatch.setattr(a, 'login_failures', {})
    monkeypatch.setattr(a, 'CONFIG_PATH', tmp_path/'settings.json')
    monkeypatch.setattr(a, 'MISSION_PATH', tmp_path/'mission.json')
    with TestClient(a.app, base_url='https://rover.example.com') as client:
        requested = []

        async def fake_get(url, timeout=None):
            requested.append(url)
            return FakeResponse(b'{"state":{}}' if url.endswith('state.json') else b'<html>radar</html>',
                                'application/json' if url.endswith('state.json') else 'text/html')
        monkeypatch.setattr(a.client, 'get', fake_get)
        yield client, requested


def test_radar_needs_login_and_never_reaches_service(cockpit):
    client, requested = cockpit
    page = client.get('/kvido/radar/')
    assert page.status_code == 200 and 'přihlas' in page.text.lower()
    assert client.get('/kvido/radar/state.json').status_code == 401
    assert requested == []


def test_logged_in_radar_proxies_only_known_files(cockpit):
    client, requested = cockpit
    assert client.post('/kvido/api/login', json={'key': a.ACCESS_KEY}).status_code == 200
    assert client.get('/kvido/radar', follow_redirects=False).headers['location'] == 'radar/'
    assert client.get('/kvido/radar/').text == '<html>radar</html>'
    state = client.get('/kvido/radar/state.json')
    assert state.json() == {'state': {}} and state.headers['cache-control'] == 'no-store'
    assert client.get('/kvido/radar/../api/settings').status_code in (200, 404)
    assert client.get('/kvido/radar/secret.txt').status_code == 404
    assert requested == ['http://127.0.0.1:27261/radar', 'http://127.0.0.1:27261/state.json']


def test_autopilot_actions_need_login_and_only_known_actions(cockpit, monkeypatch):
    client, requested = cockpit
    posted = []

    async def fake_post(url, content=None, headers=None, timeout=None):
        posted.append((url, content))
        return FakeResponse(b'{"run":"r1"}', 'application/json')
    monkeypatch.setattr(a.client, 'post', fake_post)
    assert client.post('/kvido/radar/autopilot/start', json={'goal': 'k prahu'}).status_code == 401
    assert client.post('/kvido/api/login', json={'key': a.ACCESS_KEY}).status_code == 200
    r = client.post('/kvido/radar/autopilot/start', json={'goal': 'k prahu'})
    assert r.status_code == 200 and r.json() == {'run': 'r1'}
    assert client.post('/kvido/radar/autopilot/drive', json={}).status_code == 404
    assert client.post('/kvido/radar/autopilot/step', content=b'x'*3000, headers={'Content-Type': 'application/json'}).status_code == 413
    assert posted == [('http://127.0.0.1:27261/autopilot/start', b'{"goal":"k prahu"}')]
