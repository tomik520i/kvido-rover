import copy
import base64
import io

import httpx
import pytest
from PIL import Image

from test_safety import a, c, decision
from server.vision_history import VisionHistory
from server.decision_loop import duplicate_feedback


def sensor_allowed(proposal=None, telemetry=None, **overrides):
    arguments = {'mode':'ai','image_source':'car','image_age':90,'armed':True,'policy':'sensor'}
    arguments.update(overrides)
    return a.movement_allowed(proposal or decision(hazard='unknown',traversable=False,
                                                   turn_clear=False,confidence=.1),
                              telemetry or {'distance_cm':87,'telemetry_age_ms':100},**arguments)[0]


def test_sensor_policy_allows_uncertain_visuals_and_old_image_with_fresh_range():
    assert sensor_allowed() == 'forward'


@pytest.mark.parametrize('action,distance,expected', [
    ('forward',87,'forward'),('forward',25,'forward'),('forward',22,'forward'),('forward',20,'stop'),
    ('rotate_left',22,'rotate_left'),('rotate_right',22,'rotate_right'),
    ('rotate_left',20,'rotate_left'),('rotate_right',0,'rotate_right'),
])
def test_sensor_policy_preserves_direction_specific_distance_limits(action,distance,expected):
    proposal = decision(action=action,hazard='unknown',traversable=False,turn_clear=False,confidence=.1)
    assert sensor_allowed(proposal,{'distance_cm':distance,'telemetry_age_ms':100}) == expected


@pytest.mark.parametrize('age', [501,-1,None,'100',True,float('nan'),float('inf')])
def test_sensor_policy_rejects_stale_and_invalid_measurement_age(age):
    assert sensor_allowed(telemetry={'distance_cm':87,'telemetry_age_ms':age}) == 'stop'


@pytest.mark.parametrize('distance', [None,0,True,'87',401,float('nan')])
def test_sensor_policy_rejects_invalid_range(distance):
    assert sensor_allowed(telemetry={'distance_cm':distance,'telemetry_age_ms':100}) == 'stop'


@pytest.mark.parametrize('age', [0,500])
def test_sensor_policy_accepts_freshness_boundaries(age):
    assert sensor_allowed(telemetry={'distance_cm':87,'telemetry_age_ms':age}) == 'forward'


@pytest.mark.parametrize('action', ['forward','rotate_left','rotate_right'])
def test_visible_drop_still_blocks_wheel_motion(action):
    assert sensor_allowed(decision(action=action,hazard='drop')) == 'stop'


@pytest.mark.parametrize('override', [{'image_source':'phone'},{'image_source':'none'},
                                      {'mode':'manual'},{'armed':False}])
def test_sensor_policy_preserves_source_mode_and_authority_guards(override):
    assert sensor_allowed(**override) == 'stop'


def test_actual_sensor_turn_clamps_and_passes_three_frames_with_commands(c,monkeypatch):
    assert c.post('/api/mode',json={'mode':'ai'}).status_code == 200
    assert c.post('/api/arm',json={}).status_code == 200
    raw = a.simulation_jpeg()
    monkeypatch.setitem(a.settings,'navigation_policy','sensor')
    monkeypatch.setitem(a.settings,'simulation',False)
    monkeypatch.setitem(a.settings,'floor_calibrated',False)
    monkeypatch.setattr(a,'floor_seen',0)
    monkeypatch.setattr(a,'vision_history',VisionHistory())
    captures,commands,contexts,inference_messages = [],[],[],[]
    async def device(path,method='GET',payload=None):
        if path == '/capture':
            captures.append(True)
            return httpx.Response(200,content=raw)
        assert path == '/api/control' and method == 'POST'
        commands.append(copy.deepcopy(payload))
        return httpx.Response(200,json={'ok':True})
    async def telemetry():
        return {'servo':90,'distance_cm':87,'telemetry_age_ms':100,'light_on':False}
    original_messages = a.messages_for
    def messages_for(*args,**kwargs):
        contexts.append(copy.deepcopy(kwargs['frames']))
        return original_messages(*args,**kwargs)
    proposals = iter([decision(action=action,speed=180,duration_ms=2000,hazard='unknown',
                               traversable=False,turn_clear=False,confidence=.1)
                      for action in ('forward','rotate_left','stop')])
    async def infer(messages,schema=None):
        inference_messages.append(messages)
        assert not a.navigation()['floor_calibrated'] and not a.navigation()['floor_valid']
        return next(proposals)
    monkeypatch.setattr(a,'device',device)
    monkeypatch.setattr(a,'telemetry',telemetry)
    monkeypatch.setattr(a,'messages_for',messages_for)
    monkeypatch.setattr(a,'infer',infer)
    results = []
    for _ in range(3):
        response = c.post('/api/turn',json={'intent':'act','source':'car'})
        assert response.status_code == 200,response.text
        results.append(response.json())
    assert len(captures) == 3 and [len(frames) for frames in contexts] == [1,2,3]
    assert results[0]['executed_action'] == 'forward'
    assert (results[0]['speed'],results[0]['duration_ms']) == (180,350)
    assert results[1]['executed_action'] == 'rotate_left'
    assert (results[1]['speed'],results[1]['duration_ms']) == (150,350)
    assert [(command['action'],command['speed'],command['duration_ms']) for command in commands[:2]] == [
        ('forward',180,350),('rotate_left',150,350)]
    frames = contexts[-1]
    assert [frame['at'] for frame in frames] == sorted(frame['at'] for frame in frames)
    assert all(frame['servo'] == 90 for frame in frames)
    assert frames[0]['commands_since_previous'] == []
    assert frames[1]['commands_since_previous'][0]['action'] == 'forward'
    assert frames[1]['commands_since_previous'][0]['duration_ms'] == 350
    assert frames[2]['commands_since_previous'][0]['action'] == 'rotate_left'
    assert frames[2]['commands_since_previous'][0]['duration_ms'] == 350
    images = [part for part in inference_messages[-1][-1]['content'] if part['type']=='image_url']
    assert len(images) == 2
    assert images[-1]['image_url']['url']==contexts[-1][-1]['image']
    labels = '\n'.join(part['text'] for part in inference_messages[-1][-1]['content'] if part['type']=='text')
    assert all(f'SNÍMEK {index}/3' in labels for index in (1,2,3))
    assert results[-1]['vision_frames'] == 3


@pytest.mark.parametrize('action', ['forward','rotate_left','rotate_right'])
@pytest.mark.parametrize('scene_changed', [True,False])
def test_repeated_wheel_direction_depends_on_fresh_motion_observation(action,scene_changed):
    def picture(color):
        data = io.BytesIO()
        Image.new('RGB',(80,60),color).save(data,format='JPEG')
        return 'data:image/jpeg;base64,'+base64.b64encode(data.getvalue()).decode()
    tracker = VisionHistory()
    measurement = {'servo':90,'light_on':False,'distance_cm':87,'telemetry_age_ms':100}
    tracker.observe(picture('black'),measurement,'car')
    # This records an accepted command in memory; it never controls hardware.
    tracker.command(action,80,100,90)
    frames = tracker.observe(picture('white' if scene_changed else 'black'),measurement,'car')
    observation = frames[-1]['motion_observation']
    assert observation['status'] == ('changed' if scene_changed else 'possible_stall')
    assert observation['movement_confirmed'] is False
    prior = {'executed':action,'proposed_command':{'action':action,'speed':80,'duration_ms':100,'tilt':90}}
    feedback = duplicate_feedback({'action':action,'speed':100,'duration_ms':150,'tilt':90},[prior],
                                  {**measurement,'motion_observation':observation})
    if scene_changed:
        assert feedback is None
    else:
        assert feedback and action in feedback


@pytest.mark.parametrize('observation,blocked',[('changed',False),('possible_stall',True)])
def test_camera_check_preserves_last_verified_wheel_result(observation,blocked):
    recent=[{'executed':'forward','proposed_command':{'action':'forward'},
             'motion_observation':{'status':observation}},
            {'executed':'tilt','proposed_command':{'action':'tilt','tilt':55}}]
    feedback=duplicate_feedback({'action':'forward'},recent,
        {'servo':55,'motion_observation':{'status':'no_wheel_command'}})
    assert bool(feedback) is blocked


def test_newer_rejection_overrides_earlier_successful_motion():
    recent=[{'executed':'forward','proposed_command':{'action':'forward'},
             'motion_observation':{'status':'changed'}},
            {'executed':'stop','proposed_command':{'action':'forward'},
             'observed_at_execution':{'servo':55}}]
    assert duplicate_feedback({'action':'forward'},recent,
        {'servo':55,'motion_observation':{'status':'no_wheel_command'}})
