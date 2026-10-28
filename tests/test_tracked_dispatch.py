"""Tracked dispatch regressions: fake inference, real isolated orchestration.

No running server, network/model call, physical device or browser is used.
"""
import asyncio
import math

import pytest

from tools.run_sensor_gemma import Experiment
from server.semantic_target import resolve_semantic_target


NEGATIVE=dict(goal_visible=False,confidence=.8,bearing='unknown',target_pixel=None,
    threshold_visible=False,threshold_pixel=None,at_threshold=False,hazard='clear',
    description='Podlaha a stěny.',evidence='Cílový průchod není v aktuálním záběru.')


class Provider:
    def __init__(self, hazard='clear'):
        self.calls=0;self.images=[];self.hazard=hazard
    async def infer(self,messages,schema,**options):
        self.calls+=1
        self.images.append(messages[-1]['content'][-1]['image_url']['url'])
        assert options['provider']=='gemma' and options['thinking'] is False
        return {**NEGATIVE,'hazard':self.hazard},{}


def learned_point(experiment, distance=2., *, servo=90):
    t=experiment.world.telemetry();t['servo']=servo
    horizon=240-math.tan(math.radians((90-servo)*.55))*280
    y=(horizon+.22*280/distance)/480
    p={**NEGATIVE,'goal_visible':True,'threshold_visible':True,'confidence':.9,
       'target_pixel':{'x':.5,'y':y},'threshold_pixel':y}
    m=dict(status='localized',provenance='simulated_colored_threshold_current_pixels',quality=.9)
    experiment.target_track.update(p,m,t,experiment.navigator.snapshot()['estimated_pose'])
    assert experiment.target_track.snapshot()['point'] is not None
    return t,p


def scan_proposal(_):
    return dict(action='rotate_left',speed=180,duration_ms=327,tilt=90,
                reason='Controlled sonar scan proposal.',scan=True)


def motion(action='backward',duration=100):
    return dict(action=action,speed=180,duration_ms=duration,tilt=90,
                reason='Controlled known-route pulse.',approach_status='approaching',goal_completed=False)


def test_existing_track_forces_current_inference_before_each_actual_mapped_move(monkeypatch):
    monkeypatch.setattr('tools.run_sensor_gemma.estimate_motion',lambda *args,**kwargs:
                        dict(status='unknown',motion=None,reason='offline_test'))
    async def run():
        provider=Provider();experiment=Experiment(provider,max_steps=4)
        learned_point(experiment)
        experiment.target_track.update(NEGATIVE,{},experiment.world.telemetry(),
                                       experiment.navigator.snapshot()['estimated_pose'])
        # Start beyond step zero: the original inspect scheduling skipped this
        # first scan even though dispatch then replaced it with a translation.
        experiment.state.update(status='running',step=1,perception=dict(NEGATIVE))
        experiment.scan_since_perception=0
        experiment.navigator.next_command=scan_proposal
        dispatched=[]
        def approach(*_):
            assert provider.calls==len(dispatched)+1,'each substituted move needs a current inference'
            dispatched.append(provider.calls)
            return motion()
        experiment.goal_approach.next_command=approach
        await experiment.run(experiment.generation)
        events=experiment.state['events']
        assert len(events)==3 and provider.calls==3
        assert len(set(provider.images))==3,'fresh camera observations must follow actual pulses'
        assert all(event['action']=='backward' for event in events)
        assert all(event['controller']=='mapped_goal_approach' for event in events)
        assert all(event['command']['acceptance']=='accepted' for event in events)
        assert all(not event['candidate_goal_reached'] for event in events)
        assert experiment.state['status']!='completed'
    asyncio.run(run())


def test_search_none_at_current_standoff_retains_explicit_approach_stop():
    experiment=Experiment(Provider())
    telemetry,perception=learned_point(experiment,.5,servo=55)
    assert experiment.target_track.suggest_search(telemetry) is None
    command=experiment.tracked_command(None,motion(),telemetry,perception)
    assert command is not None and command['action']=='stop'
    assert command['approach_status']=='reacquire'
    assert not command.get('candidate_goal_reached') and not command.get('goal_completed')


def test_target_guard_veto_cannot_fall_through_to_unrelated_exploration_when_search_none():
    experiment=Experiment(Provider())
    telemetry,perception=learned_point(experiment,.5,servo=55)
    unsafe_approach=motion('forward',2000)
    proposed=motion('backward',100)
    assert not experiment.target_track.command_allowed(unsafe_approach,
        experiment.navigator.snapshot()['estimated_pose'])
    assert experiment.target_track.command_allowed(proposed,
        experiment.navigator.snapshot()['estimated_pose'])
    assert experiment.target_track.suggest_search(telemetry) is None
    # Simulate disagreement between two independent safety layers. The final
    # target-region veto must remain a STOP even if reacquisition is unnecessary.
    experiment.goal_approach.next_command=lambda *_:unsafe_approach
    command=experiment.tracked_command(None,proposed,telemetry,perception)
    assert command is not None and command['action']=='stop','a target guard veto cannot become selected=None'
    assert not command.get('candidate_goal_reached')


@pytest.mark.parametrize('expired',[False,True])
def test_active_estop_precedes_point_orientation_recovery_and_current_goal_candidate(expired):
    experiment=Experiment(Provider())
    telemetry,perception=learned_point(experiment,.5,servo=55)
    # The old orientation path would rotate toward this off-axis stored point.
    perception['target_pixel']['x']=.2
    experiment.target_track.update(perception,
        dict(status='localized',provenance='current_pixels',quality=.9),telemetry,
        experiment.navigator.snapshot()['estimated_pose'])
    if expired:
        experiment.clock.now=100
    telemetry['estop']=True
    before=experiment.target_track.snapshot()['recovery']
    proposed=motion('backward',100)
    for selected in [None,dict(action='stop',speed=0,duration_ms=100,tilt=55,
                              candidate_goal_reached=True,reason='Current semantic candidate.')]:
        command=experiment.tracked_command(selected,proposed,telemetry,perception)
        assert command is not None and command['action']=='stop'
        assert command['speed']==0 and not command.get('candidate_goal_reached')
        assert not command.get('recovery_scan')
    assert experiment.target_track.snapshot()['recovery']==before


@pytest.mark.parametrize('hazard',['person','animal','drop'])
def test_current_concrete_hazard_precedes_expired_track_search_and_blocks_arrival(hazard):
    async def run():
        provider=Provider(hazard);experiment=Experiment(provider,max_steps=4)
        learned_point(experiment)
        experiment.clock.now=100
        assert not experiment.target_track.snapshot()['usable']
        experiment.navigator.next_command=scan_proposal
        experiment.state.update(status='running',step=1,perception=dict(NEGATIVE))
        await experiment.run(experiment.generation)
        assert provider.calls==3
        events=experiment.state['events']
        assert len(events)==3 and all(event['action']=='stop' for event in events)
        assert not any(event['command'].get('recovery_scan') for event in events)
        assert not any(event['candidate_goal_reached'] for event in events)
        assert experiment.state['status']=='blocked'
        assert experiment.target_track.snapshot()['recovery']['accepted_rotations']==0
        assert experiment.world.snapshot()['travelled']==0
    asyncio.run(run())


def test_alternating_opening_points_without_current_floor_use_one_way_bounded_recovery():
    class OpeningProvider:
        calls=0
        async def infer(self,messages,schema,**options):
            x=(.395,.5)[self.calls%2]
            self.calls+=1
            return {**NEGATIVE,'goal_visible':True,'confidence':.9,
                'bearing':'left' if x<.5 else 'ahead','target_pixel':{'x':x,'y':.2},
                'threshold_visible':False,'threshold_pixel':None},{}
    async def run():
        provider=OpeningProvider();experiment=Experiment(provider,max_steps=6)
        learned_point(experiment,1.)
        original=experiment.target_track.snapshot()['point']
        experiment.navigator.next_command=scan_proposal
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        events=experiment.state['events']
        assert provider.calls==6 and len(events)==6
        assert [event['perception']['target_pixel']['x'] for event in events]==[.395,.5]*3
        assert all(event['perception']['threshold_visible'] is False for event in events)
        assert [event['action'] for event in events]==['tilt']+['rotate_left']*5
        assert all(event['command'].get('recovery_scan') is True for event in events)
        assert all(event['command']['acceptance']=='accepted' for event in events)
        assert not any(event['candidate_goal_reached'] for event in events)
        state=experiment.target_track.snapshot()
        assert state['point']==original and not state['current_localized']
        assert state['observations']==1,'opening/jamb pixels must not renew floor-point memory'
        assert state['recovery']['accepted_rotations']==5
        assert experiment.world.snapshot()['travelled']==0
        assert experiment.state['status']!='completed'
    asyncio.run(run())


def test_standoff_without_current_floor_uses_independent_scan_not_old_bearing():
    experiment=Experiment(Provider())
    t,p=learned_point(experiment,2.,servo=55)
    p['target_pixel']['x']=.8
    experiment.target_track.update(p,
        dict(status='localized',provenance='current_pixels',quality=.9),t,
        experiment.navigator.snapshot()['estimated_pose'])
    experiment.target_track.update(NEGATIVE,{},t,experiment.navigator.snapshot()['estimated_pose'])
    assert experiment.target_track.suggest_orientation(
        experiment.navigator.snapshot()['estimated_pose'],t)['action']=='rotate_right'
    experiment.goal_approach.next_command=lambda *_:dict(action='stop',speed=0,duration_ms=100,
        tilt=55,approach_status='reacquire',reason='Controlled mapped standoff.',goal_completed=False)
    command=experiment.tracked_command(None,scan_proposal(t),t,NEGATIVE)
    assert command['action']=='rotate_left' and command['recovery_scan'] is True
    assert command['duration_ms']==421 and not command.get('candidate_goal_reached')


@pytest.mark.parametrize('distance,expected_action,expected_candidate',[(.8,'forward',False),(.3,'stop',True)])
def test_valid_current_floor_retains_semantic_forward_and_candidate_dispatch(distance,expected_action,expected_candidate):
    experiment=Experiment(Provider())
    t,p=learned_point(experiment,distance,servo=55)
    selected=resolve_semantic_target(p,t)
    assert selected['action']==expected_action
    assert selected['candidate_goal_reached'] is expected_candidate
    assert experiment.target_track.snapshot()['current_localized']
    command=experiment.tracked_command(selected,scan_proposal(t),t,p)
    assert command is selected
    assert command['action']==expected_action and command['candidate_goal_reached'] is expected_candidate
    assert not command.get('recovery_scan')
