from copy import deepcopy
import math

import pytest

from server.visual_waypoint import HALF_FOV_RAD,resolve_waypoint,waypoint_schema


def decision(x=.5,y=.8,**fields):
    return dict(action='navigate',waypoint=dict(x=x,y=y,evidence='Podlaha pokračuje do viditelného průchodu.'),speed=180,duration_ms=1000,reason='Přiblížím se k průchodu.',**fields)


def resolve(value,allowed=('forward','rotate_left','rotate_right'),**telemetry):
    t=dict(distance_cm=100,telemetry_age_ms=0,forward_stop_cm=10,motor_max_duration_ms=2000)
    t.update(telemetry)
    return resolve_waypoint(value,t,allowed)


def test_optics_match_renderer_and_turn_signs_have_bounded_pulses():
    assert math.degrees(2*HALF_FOV_RAD)==pytest.approx(97.628,abs=.01)
    for x,action in ((0,'rotate_left'),(.4,'rotate_left'),(.6,'rotate_right'),(1,'rotate_right')):
        result,_=resolve(decision(x))
        assert result['action']==action and result['speed']==150
        assert 100<=result['duration_ms']<=500


def test_centered_target_forward_preserves_fields_and_bounds_speed_duration():
    value=decision();value.update(speed=999,duration_ms=9999)
    before=deepcopy(value)
    result,_=resolve(value)
    assert result['action']=='forward' and result['speed']==180 and result['duration_ms']==2000
    assert result['reason']==value['reason'] and result['waypoint']==value['waypoint']
    assert value==before
    value.update(speed=1,duration_ms=1)
    result,_=resolve(value)
    assert result['speed']==150 and result['duration_ms']==100


@pytest.mark.parametrize('field,value',[('x',True),('x',None),('x',float('nan')),('x',float('inf')),('x',-.1),('x',1.1),
                                      ('y',False),('y',float('nan')),('y',.45),('evidence',''),('evidence',' '*5),('evidence','x'*241)])
def test_invalid_waypoint_fails_to_stop_for_replan(field,value):
    item=decision();item['waypoint'][field]=value
    result,reason=resolve(item)
    assert result['action']=='stop' and result['speed']==0 and 'nový plán' in reason


@pytest.mark.parametrize('age',[None,True,-1,501,float('nan')])
def test_stale_telemetry_blocks_rotations_and_forward(age):
    for x in (0,.5):assert resolve(decision(x),telemetry_age_ms=age)[0]['action']=='stop'


@pytest.mark.parametrize('distance',[None,True,10,0,401,float('nan')])
def test_bad_front_distance_blocks_forward_but_not_permitted_rotation(distance):
    assert resolve(decision(),distance_cm=distance)[0]['action']=='stop'
    assert resolve(decision(.2),distance_cm=distance)[0]['action']=='rotate_left'


def test_no_forbidden_direction_and_no_automatic_action_from_stop_or_drop():
    assert resolve(decision(.2),allowed=['forward','rotate_right'])[0]['action']=='stop'
    assert resolve(decision(),allowed=['rotate_left'])[0]['action']=='stop'
    assert resolve(decision(hazard='drop'))[0]['action']=='stop'
    for action in ('stop',None,'tilt','rotate_left'):
        value=decision();value['action']=action
        assert resolve(value)[0]==value


def test_distance_and_firmware_cap_duration_without_image_depth_guess():
    near=resolve(decision(),distance_cm=25)[0]
    assert near['duration_ms']==300
    assert resolve(decision(),distance_cm=11)[0]['action']=='stop'
    assert resolve(decision(),motor_max_duration_ms=350)[0]['duration_ms']==350
    assert resolve(decision(y=.6))[0]['duration_ms']==resolve(decision(y=.95))[0]['duration_ms']


@pytest.mark.parametrize('field,value',[('speed',True),('speed',float('nan')),('duration_ms',True),('duration_ms',float('inf'))])
def test_invalid_requested_command_does_not_become_motion(field,value):
    item=decision();item[field]=value
    assert resolve(item)[0]['action']=='stop'


def test_schema_is_detached_strict_and_never_adds_navigation_to_nonwheel_schema():
    source=dict(type='object',properties=dict(action=dict(type='string',enum=['stop','forward'])),required=['action'])
    before=deepcopy(source)
    result=waypoint_schema(source)
    assert source==before
    assert result['properties']['action']['enum']==['stop','forward','navigate']
    assert result['properties']['waypoint']['additionalProperties'] is False
    assert set(result['properties']['waypoint']['required'])=={'x','y','evidence'}
    assert 'minLength' not in result['properties']['waypoint']['properties']['evidence']
    nonwheel=dict(properties=dict(action=dict(enum=['stop','tilt'])),required=['action'])
    assert waypoint_schema(nonwheel)==nonwheel


def test_deadband_boundaries_are_symmetric_and_empty_evidence_ignored_for_stop():
    for x in (.42,.58):assert resolve(decision(x))[0]['action']=='forward'
    value=dict(action='stop',waypoint=dict(x=.5,y=.8,evidence=''))
    assert resolve(value)[0]==value
