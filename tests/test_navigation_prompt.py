"""Exercise navigation decisions through the actual turn gate, without hardware."""
import time
from types import SimpleNamespace

import pytest
import httpx

from test_safety import a, c, decision


@pytest.fixture
def navigation_turn(c, monkeypatch):
    # These tests compare navigation guards, not rendering time against the
    # operator lease. Freeze only app's monotonic clock; the watchdog still runs.
    clock={'now':a.time.monotonic()}
    monkeypatch.setattr(a,'time',SimpleNamespace(time=a.time.time,monotonic=lambda:clock['now']))
    # Activate the test operator in simulation before replacing all device I/O.
    assert c.post('/api/mode', json={'mode':'ai'}).status_code == 200
    assert c.post('/api/arm', json={}).status_code == 200
    image = a.simulation_jpeg()
    monkeypatch.setitem(a.settings, 'simulation', False)
    monkeypatch.setitem(a.settings, 'floor_calibrated', True)
    monkeypatch.setitem(a.settings, 'floor_angle', 55)
    monkeypatch.setattr(a, 'floor_seen', 0)
    sent = []
    sent_parameters = []

    async def device(path, *args, **kwargs):
        assert path == '/capture', 'Unexpected device I/O in navigation test'
        return httpx.Response(200, content=image)

    async def send_control(action='stop', speed=0, duration_ms=100, tilt=90):
        sent.append(action)
        sent_parameters.append((action,speed,duration_ms))

    monkeypatch.setattr(a, 'device', device)
    monkeypatch.setattr(a, 'send_control', send_control)

    def run(proposal=None, final_telemetry=None, servo=55):
        fresh = {'distance_cm':87, 'telemetry_age_ms':100, 'servo':servo}
        samples = []

        async def telemetry():
            samples.append(True)
            # Model sees a good sensor snapshot; the execution gate must also
            # honor the newer measurement obtained after inference.
            return {**fresh, **(final_telemetry or {})} if len(samples)>1 else fresh

        async def infer(messages, schema=None):
            assert not a.navigation()['floor_valid']
            assert a.navigation()['floor_calibrated']
            if servo == 55:
                assert 'forward' in schema['properties']['action']['enum']
            else:
                assert 'forward' not in schema['properties']['action']['enum']
            return proposal or decision(confidence=.85)

        monkeypatch.setattr(a, 'telemetry', telemetry)
        monkeypatch.setattr(a, 'infer', infer)
        response = c.post('/api/turn', json={'intent':'act','source':'car'})
        assert response.status_code == 200, response.text
        assert len(samples) == 2
        if response.json()['executed_action'] in ('rotate_left','rotate_right'):
            assert sent_parameters[-1][1:] == (response.json()['speed'],response.json()['duration_ms'])
            assert sent_parameters[-1][1] <= 80 and sent_parameters[-1][2] <= 100
        return response.json(), sent

    run.clock=clock
    return run


def test_current_calibrated_floor_can_authorize_without_previous_floor_valid(navigation_turn):
    result, sent = navigation_turn()
    assert result['executed_action'] == 'forward',result['execution_reason']
    assert sent == ['forward']
    # Driving consumes the floor observation, requiring a fresh view next step.
    assert not a.navigation()['floor_valid']


def test_operator_lease_expiration_can_safely_append_stop_after_authorized_navigation(navigation_turn):
    result,sent=navigation_turn()
    assert result['executed_action']=='forward',result['execution_reason']
    assert sent==['forward']
    epoch=a.state['epoch']
    navigation_turn.clock['now']+=2
    deadline=time.monotonic()+2
    while len(sent)==1 and time.monotonic()<deadline:
        time.sleep(.01)
    assert sent==['forward','stop']
    assert a.state['armed'] is False and a.state['epoch']==epoch+1
    assert a.state['last_reason']=='Telefon se odpojil nebo usnul. Autíčko zastaveno.'


def test_sensor_age_boundary_remains_500_ms(navigation_turn):
    result, sent = navigation_turn(final_telemetry={'telemetry_age_ms':500})
    assert result['executed_action'] == 'forward'
    assert sent == ['forward']


@pytest.mark.parametrize('hazard', ['unknown','drop','obstacle'])
def test_current_unsafe_floor_never_authorizes_motion(navigation_turn, hazard):
    result, sent = navigation_turn(decision(hazard=hazard, traversable=True, confidence=.99))
    assert result['executed_action'] == 'stop'
    assert sent == ['stop']
    assert not a.navigation()['floor_valid']


@pytest.mark.parametrize('proposal', [decision(traversable=False), decision(confidence=.84)])
def test_current_floor_requires_visual_confirmation(navigation_turn, proposal):
    result, sent = navigation_turn(proposal)
    assert result['executed_action'] == 'stop'
    assert sent == ['stop']


@pytest.mark.parametrize('measurement', [
    {'telemetry_age_ms':501},
    {'distance_cm':None},
    {'distance_cm':0},
    {'distance_cm':25},
    {'distance_cm':True},
    {'distance_cm':401},
])
def test_current_floor_cannot_override_latest_stale_or_invalid_sensor(navigation_turn, measurement):
    result, sent = navigation_turn(final_telemetry=measurement)
    assert result['executed_action'] == 'stop'
    assert sent == ['stop']


def test_floor_view_requires_calibrated_camera_angle_even_if_model_says_clear(navigation_turn):
    result, sent = navigation_turn(servo=90)
    assert result['executed_action'] == 'stop'
    assert sent == ['stop']


@pytest.mark.parametrize('action', ['rotate_left','rotate_right'])
def test_rotation_at_21_cm_is_short_and_slow_with_current_clear_floor(navigation_turn, action):
    result, sent = navigation_turn(decision(action=action,turn_clear=True,speed=150,duration_ms=300), {'distance_cm':21})
    assert result['executed_action'] == action and sent == [action]
    assert result['speed'] == 80 and result['duration_ms'] == 100


@pytest.mark.parametrize('action', ['rotate_left','rotate_right'])
def test_rotation_preserves_lower_proposed_limits(navigation_turn, action):
    result, sent = navigation_turn(decision(action=action,turn_clear=True,speed=60,duration_ms=50), {'distance_cm':21})
    assert result['executed_action'] == action
    assert result['speed'] == 60 and result['duration_ms'] == 50


@pytest.mark.parametrize('measurement', [
    {'distance_cm':20},{'distance_cm':0},{'distance_cm':None},
    {'distance_cm':21,'telemetry_age_ms':501},
    {'distance_cm':21,'telemetry_age_ms':-1},
])
def test_rotation_preserves_sensor_stop_guards(navigation_turn, measurement):
    result, sent = navigation_turn(decision(action='rotate_left',turn_clear=True), measurement)
    assert result['executed_action'] == 'stop' and sent == ['stop']


@pytest.mark.parametrize('hazard', ['unknown','drop'])
def test_rotation_at_21_cm_still_requires_safe_image(navigation_turn, hazard):
    result, sent = navigation_turn(decision(action='rotate_right',turn_clear=True,hazard=hazard), {'distance_cm':21})
    assert result['executed_action'] == 'stop' and sent == ['stop']


def test_forward_still_stops_at_21_cm(navigation_turn):
    result, sent = navigation_turn(final_telemetry={'distance_cm':21})
    assert result['executed_action'] == 'stop' and sent == ['stop']


@pytest.mark.parametrize('action', ['rotate_left','rotate_right'])
@pytest.mark.parametrize('hazard', ['clear','obstacle'])
def test_rotation_requires_explicit_confirmation_of_turn_space(navigation_turn, action, hazard):
    result, sent = navigation_turn(decision(action=action,hazard=hazard,turn_clear=False), {'distance_cm':21})
    assert result['executed_action'] == 'stop' and sent == ['stop']


@pytest.mark.parametrize('action', ['rotate_left','rotate_right'])
def test_obstacle_outside_confirmed_turn_space_does_not_block_short_rotation(navigation_turn, action):
    result, sent = navigation_turn(decision(action=action,hazard='obstacle',turn_clear=True), {'distance_cm':21})
    assert result['executed_action'] == action and sent == [action]
    assert result['speed'] == 80 and result['duration_ms'] == 100
    assert not a.navigation()['floor_valid']


def test_forward_cannot_use_turn_confirmation_to_ignore_obstacle(navigation_turn):
    result, sent = navigation_turn(decision(action='forward',hazard='obstacle',turn_clear=True))
    assert result['executed_action'] == 'stop' and sent == ['stop']


def test_turn_confirmation_cannot_replace_current_calibrated_floor_view(navigation_turn):
    result, sent = navigation_turn(decision(action='rotate_left',hazard='obstacle',turn_clear=True),
                                   {'distance_cm':21},servo=90)
    assert result['executed_action'] == 'stop' and sent == ['stop']


@pytest.mark.parametrize('proposal', [
    decision(action='rotate_left',hazard='obstacle',turn_clear=True,traversable=False),
    decision(action='rotate_left',hazard='obstacle',turn_clear=True,confidence=.84),
])
def test_confirmed_turn_still_requires_visible_floor_and_confidence(navigation_turn, proposal):
    result, sent = navigation_turn(proposal, {'distance_cm':21})
    assert result['executed_action'] == 'stop' and sent == ['stop']
