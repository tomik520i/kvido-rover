import json

import pytest

from test_safety import a, c, decision


@pytest.fixture
def step(c, monkeypatch, tmp_path):
    assert c.post('/api/mode', json={'mode':'ai'}).status_code == 200
    assert c.post('/api/arm', json={}).status_code == 200
    monkeypatch.setattr(a, 'mission', {**a.mission,'goal':'Explore','status':'paused','recent':[]})
    monkeypatch.setattr(a, 'MISSION_PATH', tmp_path / 'mission.json')
    monkeypatch.setitem(a.sim, 'servo', 55)
    sent = []
    async def send_control(action='stop', speed=0, duration_ms=100, tilt=90):
        sent.append({'action':action,'speed':speed,'duration_ms':duration_ms,'tilt':tilt})
    monkeypatch.setattr(a, 'send_control', send_control)

    def run(proposal, before=None, at_execution=None, disarm=False):
        initial = {'servo':55,'distance_cm':87,'telemetry_age_ms':100, **(before or {})}
        current = {**initial, **(at_execution or {})}
        snapshots = iter([initial,current])
        calls = []
        async def telemetry():
            calls.append(True)
            return next(snapshots)
        async def infer(*args, **kwargs):
            if disarm:
                a.state['armed'] = False
            return proposal
        monkeypatch.setattr(a, 'telemetry', telemetry)
        monkeypatch.setattr(a, 'infer', infer)
        response = c.post('/api/turn', json={'intent':'act','source':'car'})
        assert response.status_code == 200, response.text
        assert len(calls) == 2, 'History must not introduce telemetry network requests'
        return response.json(), a.mission['recent'][-1], sent
    return run


def test_history_distinguishes_observed_servo_and_requested_tilt_in_paused_mission(step):
    result, entry, sent = step(decision(action='tilt',tilt=120,evidence='Visible floor'))
    assert result['executed_action'] == 'tilt'
    assert {k:entry['observed_before'][k] for k in ('servo','distance_cm','telemetry_age_ms')} == {'servo':55,'distance_cm':87,'telemetry_age_ms':100}
    assert entry['observed_before']['captured_at'] > 0
    assert entry['observed_at_execution'] == {k:entry['observed_before'][k] for k in ('servo','distance_cm','telemetry_age_ms')}
    assert entry['command'] == sent[-1] == {'action':'tilt','speed':120,'duration_ms':200,'tilt':120}
    assert entry['evidence'] == 'Visible floor' and entry['at'] > 0
    assert 'after' not in entry  # Command acceptance is not a servo observation.
    assert json.loads(a.MISSION_PATH.read_text(encoding='utf-8'))['recent'][-1] == entry
    _, following, _ = step(decision(action='stop'),before={'servo':90})
    assert following['observed_before']['servo'] == 90
    assert entry['observed_at_execution']['servo'] == 55


def test_history_uses_execution_time_refresh_and_actual_rotation_clamps(step):
    result, entry, sent = step(decision(action='rotate_left',turn_clear=True,speed=150,duration_ms=300),
                               at_execution={'distance_cm':21,'telemetry_age_ms':150})
    assert result['executed_action'] == 'rotate_left'
    assert entry['observed_before']['distance_cm'] == 87
    assert entry['observed_at_execution']['distance_cm'] == 21
    assert entry['command'] == sent[-1]
    assert entry['command']['speed'] == 80 and entry['command']['duration_ms'] == 100
    assert entry['proposed_command']['speed'] == 150 and entry['proposed_command']['duration_ms'] == 300


def test_history_records_no_command_when_authority_lost(step):
    result, entry, sent = step(decision(action='tilt',tilt=120),disarm=True)
    assert result['executed_action'] == entry['executed'] == 'stop'
    assert entry['proposed'] == 'tilt'
    assert entry['command'] is None and sent == []


@pytest.mark.parametrize('servo', [52,55,58])
def test_repeated_tilt_at_current_angle_is_stopped(step, servo):
    result, entry, sent = step(decision(action='tilt',tilt=55),before={'servo':servo})
    assert result['executed_action'] == 'stop'
    assert 'Kamera už je v tomto úhlu' in result['execution_reason']
    assert entry['proposed_command']['tilt'] == 55 and entry['observed_at_execution']['servo'] == servo
    assert entry['command']['action'] == 'stop' and sent[-1]['action'] == 'stop'


@pytest.mark.parametrize('servo', [51,59])
def test_changed_or_unknown_servo_angle_allows_tilt(step, servo):
    result, entry, sent = step(decision(action='tilt',tilt=55),before={'servo':servo})
    assert result['executed_action'] == 'tilt'
    assert entry['command']['tilt'] == 55 and sent[-1]['action'] == 'tilt'


@pytest.mark.parametrize('telemetry', [{},{'servo':None}])
def test_missing_servo_does_not_invent_an_angle_for_tilt(telemetry):
    action, _ = a.movement_allowed(decision(action='tilt',tilt=90),telemetry,
                                  mode='ai',image_source='car',image_age=0,armed=True)
    assert action == 'tilt'
