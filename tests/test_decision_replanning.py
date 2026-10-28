"""Exercise replanning through /api/turn with all device effects replaced."""
import copy
import time

import pytest

from test_safety import a, c, decision


def previous(action, tilt=55):
    return {'at':time.time(),'proposed':action,'executed':action,
            'command':{'action':action,'speed':80,'duration_ms':100,'tilt':tilt},
            'observed_at_execution':{'servo':tilt,'distance_cm':87,'telemetry_age_ms':100},
            'evidence':'Previous verified observation','result':'Previous command accepted'}


@pytest.fixture
def replanning(c, monkeypatch, tmp_path):
    assert c.post('/api/mode',json={'mode':'ai'}).status_code == 200
    assert c.post('/api/arm',json={}).status_code == 200
    monkeypatch.setattr(a,'MISSION_PATH',tmp_path/'mission.json')
    monkeypatch.setattr(a,'mission',{**a.mission,'goal':'Find the bowl','status':'running',
                                    'started_at':time.time(),'steps':0,'recent':[]})
    monkeypatch.setattr(a,'floor_seen',0)
    original_jpeg = a.simulation_jpeg
    captures, measurements, commands, calls = [], [], [], []
    def frame():
        captures.append(True)
        return original_jpeg()
    async def telemetry():
        measurements.append(True)
        return {'servo':a.sim.get('servo',55),'distance_cm':87,'telemetry_age_ms':100,
                'light_supported':True,'light_on':False}
    async def send_control(action='stop',speed=0,duration_ms=100,tilt=90):
        commands.append({'action':action,'speed':speed,'duration_ms':duration_ms,'tilt':tilt})
    monkeypatch.setattr(a,'simulation_jpeg',frame)
    monkeypatch.setattr(a,'telemetry',telemetry)
    monkeypatch.setattr(a,'send_control',send_control)
    monkeypatch.setitem(a.sim,'servo',55)

    def run(proposals, recent, before_infer=None):
        a.mission['recent'] = copy.deepcopy(recent)
        async def infer(messages,schema=None):
            index = len(calls)
            calls.append(copy.deepcopy(messages))
            run.schemas.append(copy.deepcopy(schema))
            assert index < len(proposals), 'Replanning exceeded the provided bounded model sequence'
            a.state['heartbeat'] = time.monotonic()
            if before_infer:
                before_infer(index)
            return proposals[index]
        monkeypatch.setattr(a,'infer',infer)
        response = c.post('/api/turn',json={'intent':'act','source':'car'})
        assert response.status_code == 200,response.text
        return response.json()
    run.schemas = []
    return run,captures,measurements,commands,calls


def test_duplicate_wheel_action_replans_from_fresh_frame_without_replaying_motor(replanning):
    run,captures,measurements,commands,calls = replanning
    result = run([decision(action='forward',speed=70,duration_ms=50),
                  decision(action='rotate_left',turn_clear=True)], [previous('forward')])
    assert len(calls) == 2 and len(captures) == 2
    assert len(measurements) >= 2
    assert [command['action'] for command in commands] == ['stop','rotate_left']
    assert result['executed_action'] == 'rotate_left'
    assert len(result['replans']) == 1
    rejected = result['replans'][0]
    assert rejected['proposal']['action'] == 'forward' and rejected['feedback']
    assert rejected['feedback'] in str(calls[1])
    assert a.mission['recent'][-1]['replans'] == result['replans']


def test_tilt_aba_is_replanned_even_if_last_tilt_was_different(replanning,monkeypatch):
    run,captures,_,commands,calls = replanning
    monkeypatch.setitem(a.sim,'servo',90)
    result = run([decision(action='tilt',tilt=57),decision(action='tilt',tilt=120)],
                 [previous('tilt',55),previous('tilt',90)])
    assert len(calls) == 2 and len(captures) == 2
    assert [command['action'] for command in commands] == ['stop','tilt']
    assert commands[-1]['tilt'] == 120
    assert result['replans'][0]['proposal']['tilt'] == 57
    assert result['executed_action'] == 'tilt'


def test_stop_is_never_rejected_as_repetition(replanning):
    run,captures,_,commands,calls = replanning
    result = run([decision(action='stop')],[previous('stop')])
    assert len(calls) == 1 and len(captures) == 1
    assert [command['action'] for command in commands] == ['stop']
    assert result['executed_action'] == 'stop'
    assert result.get('replans',[]) == []


def test_third_repeated_proposal_stops_and_blocks_mission(replanning):
    run,captures,_,commands,calls = replanning
    result = run([decision(action='forward',speed=speed) for speed in (80,100,120)],
                 [previous('forward')])
    assert len(calls) == 3 and len(captures) == 3
    assert commands and all(command['action'] == 'stop' for command in commands)
    assert result['executed_action'] == 'stop'
    assert result['loop_limit'] is True
    assert result['execution_reason']
    assert len(result['replans']) == 3
    assert a.mission['status'] == 'blocked'
    assert a.mission['recent'][-1]['replans'] == result['replans']


def test_lost_authority_during_retry_cannot_actuate_new_proposal(replanning):
    run,_,_,commands,calls = replanning
    def invalidate(index):
        if index == 1:
            a.state.update(armed=False,owner=None,epoch=a.state['epoch']+1)
    result = run([decision(action='forward'),decision(action='rotate_right',turn_clear=True)],
                 [previous('forward')],before_infer=invalidate)
    assert len(calls) == 2
    assert [command['action'] for command in commands] == ['stop']
    assert result['executed_action'] == 'stop'


def test_wheel_motion_resets_old_tilt_cycle_history(replanning):
    run,captures,_,commands,calls = replanning
    result = run([decision(action='tilt',tilt=120)],
                 [previous('tilt',120),previous('rotate_left'),previous('tilt',55)])
    assert len(calls) == 1 and len(captures) == 1
    assert [command['action'] for command in commands] == ['tilt']
    assert result['executed_action'] == 'tilt' and not result.get('replans')


def test_retry_schema_excludes_rejected_forward_and_preserves_stop(replanning):
    run,_,_,commands,_ = replanning
    result = run([decision(action='forward'),decision(action='rotate_right',turn_clear=True)],
                 [previous('forward')])
    first,second = run.schemas
    assert 'forward' in first['properties']['action']['enum']
    assert 'forward' not in second['properties']['action']['enum']
    assert all('stop' in schema['properties']['action']['enum'] for schema in run.schemas)
    assert result['executed_action'] == 'rotate_right'
    assert [command['action'] for command in commands] == ['stop','rotate_right']


def test_retry_schema_excludes_rejected_tilt_target_with_tolerance(replanning):
    run,_,_,commands,_ = replanning
    result = run([decision(action='tilt',tilt=90),decision(action='tilt',tilt=120)],
                 [previous('tilt',90),previous('tilt',55)])
    first,second = run.schemas
    assert 90 in first['properties']['tilt']['enum']
    assert 'tilt' in second['properties']['action']['enum']
    assert all(abs(angle-90)>3 for angle in second['properties']['tilt']['enum'])
    assert 'stop' in second['properties']['action']['enum']
    assert result['executed_action'] == 'tilt' and commands[-1]['tilt'] == 120


def test_previously_blocked_forward_can_be_reconsidered_after_real_camera_change(replanning):
    run,captures,_,commands,calls = replanning
    old = previous('forward',90)
    old.update(executed='stop',proposed_command={'action':'forward','speed':80,'duration_ms':100,'tilt':90},
               command={'action':'stop','speed':0,'duration_ms':100,'tilt':90})
    # Current telemetry is 55 degrees: the previously missing floor view now exists.
    result = run([decision(action='forward')],[old])
    assert len(calls) == 1 and len(captures) == 1
    assert 'forward' in run.schemas[0]['properties']['action']['enum']
    assert result['executed_action'] == 'forward' and not result.get('replans')
    assert [command['action'] for command in commands] == ['forward']
