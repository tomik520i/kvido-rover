"""Direction limits must agree at generation and the final motion gate."""
import math

import pytest

from test_safety import a, decision
from server.motion_policy import AI_WHEELS, ESCAPE, command_limits, fresh, hardware_limits
from server.prompt import response_schema


def sample():
    return dict(distance_cm=100,telemetry_age_ms=0,connected=True,
                motor_max_duration_ms=2000,forward_stop_cm=10,servo=55)


def telemetry(**updates):
    result=sample();result.update(updates);return result


def permitted(action,t,policy='sensor',**updates):
    return a.movement_allowed(decision(action=action,turn_clear=True,**updates),t,
        mode='ai',image_source='simulator',image_age=0,armed=True,policy=policy)[0]


def schema(t,policy='sensor'):
    return response_schema('ai','simulator',dict(policy=policy,floor_calibrated=True,
        floor_valid=True,floor_angle=55),t)['properties']


@pytest.mark.parametrize('age',[None,True,False,-1,501,math.nan,math.inf,-math.inf,'0'])
@pytest.mark.parametrize('policy',['sensor','strict'])
def test_invalid_age_blocks_every_wheel_at_schema_and_gate(age,policy):
    t=telemetry(telemetry_age_ms=age)
    assert fresh(t) is False
    assert not set(AI_WHEELS)&set(schema(t,policy)['action']['enum'])
    assert all(permitted(action,t,policy)=='stop' for action in AI_WHEELS)


@pytest.mark.parametrize('policy',['sensor','strict'])
def test_disconnected_sample_cannot_authorize_wheels(policy):
    t=telemetry(connected=False)
    assert not fresh(t)
    assert not set(AI_WHEELS)&set(schema(t,policy)['action']['enum'])
    assert all(permitted(action,t,policy)=='stop' for action in AI_WHEELS)


@pytest.mark.parametrize('distance',[None,True,False,0,10,math.nan,math.inf,401])
def test_sensor_front_stop_and_blind_escape_are_direction_specific(distance):
    t=telemetry(distance_cm=distance)
    actions=schema(t)['action']['enum']
    assert 'forward' not in actions
    assert permitted('forward',t)=='stop'
    for action in ESCAPE:
        assert action in actions
        assert permitted(action,t)==action


@pytest.mark.parametrize('distance',[10.01,11,20,100,400])
def test_new_firmware_allows_front_motion_above_ten_cm(distance):
    t=telemetry(distance_cm=distance)
    assert 'forward' in schema(t)['action']['enum']
    assert permitted('forward',t)=='forward'


@pytest.mark.parametrize('action',AI_WHEELS)
def test_visible_drop_still_blocks_every_sensor_direction(action):
    assert permitted(action,telemetry(),hazard='drop')=='stop'


@pytest.mark.parametrize('policy,action,expected',[
    ('sensor','forward',(180,2000)),('sensor','backward',(180,1000)),
    ('sensor','left',(180,1000)),('sensor','right',(180,1000)),
    ('sensor','rotate_left',(150,2000)),('sensor','rotate_right',(150,2000)),
    ('strict','forward',(150,300)),('strict','rotate_left',(80,100)),
])
def test_verified_capabilities_enable_direction_limits(policy,action,expected):
    assert command_limits(action,policy,telemetry())==expected


def test_physical_full_power_preserves_legacy_duration_and_simulator_calibration():
    real=dict(simulation=False,distance_cm=100,telemetry_age_ms=0,connected=True)
    for action in AI_WHEELS:
        assert command_limits(action,'sensor',real)==(180,350)
    assert hardware_limits(real)==(350,20)
    assert command_limits('rotate_left','sensor',{**real,'simulation':True})==(150,350)
    assert command_limits('rotate_left','strict',real)==(80,100)


@pytest.mark.parametrize('updates',[
    {'motor_max_duration_ms':None},{'motor_max_duration_ms':True},
    {'motor_max_duration_ms':2000.0},{'motor_max_duration_ms':2001},
    {'motor_max_duration_ms':349},{'forward_stop_cm':None},
    {'forward_stop_cm':True},{'forward_stop_cm':math.nan},
    {'forward_stop_cm':math.inf},{'forward_stop_cm':9},
    {'forward_stop_cm':21},{'telemetry_age_ms':501},{'connected':False},
])
def test_missing_invalid_or_stale_capabilities_keep_legacy_envelope(updates):
    t=telemetry(**updates)
    assert hardware_limits(t)==(350,20)
    assert all(command_limits(action,'sensor',t)[1]<=350 for action in AI_WHEELS)


def test_legacy_firmware_keeps_twenty_cm_front_stop_and_short_duration():
    t={'distance_cm':20,'telemetry_age_ms':0,'servo':55}
    assert hardware_limits(t)==(350,20)
    assert permitted('forward',t)=='stop'
    assert 'forward' not in schema(t)['action']['enum']
    t['distance_cm']=21
    assert permitted('forward',t)=='forward'
    assert schema(t)['duration_ms']['maximum']==350


def test_strict_policy_retains_visual_requirements_and_old_limits():
    t=telemetry(distance_cm=25)
    assert permitted('forward',t,'strict')=='stop'
    assert permitted('rotate_left',t,'strict')=='rotate_left'
    for action in ('backward','left','right'):
        assert permitted(action,t,'strict')=='stop'
        assert action not in schema(t,'strict')['action']['enum']
    assert permitted('rotate_left',t,'strict',hazard='unknown')=='stop'
    assert schema(t,'strict')['duration_ms']['maximum']==300


@pytest.mark.parametrize('updates,expected',[
    ({},180),({'motor_max_speed':255},180),({'esp_motor_max_speed':255},180),
    ({'motor_max_speed':255,'esp_motor_max_speed':180},180),
    ({'motor_max_speed':255.0,'esp_motor_max_speed':255},180),
    ({'motor_max_speed':True,'esp_motor_max_speed':255},180),
    ({'motor_max_speed':255,'esp_motor_max_speed':255,'telemetry_age_ms':501},180),
    ({'motor_max_speed':255,'esp_motor_max_speed':255,'simulation':True},180),
    ({'motor_max_speed':255,'esp_motor_max_speed':255},255),
])
def test_full_pwm_requires_both_fresh_physical_board_capabilities(updates,expected):
    from server.motion_policy import hardware_speed_limit
    t={**telemetry(),'simulation':False,**updates}
    assert hardware_speed_limit(t)==expected
    assert command_limits('forward','sensor',t)[0]==expected
    assert command_limits('forward','strict',t)[0]==150
    assert schema(t)['speed']['maximum']==expected
