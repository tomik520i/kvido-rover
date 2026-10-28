"""Experiment boundaries: real simulated pulses, no model access to world truth."""
import asyncio
import json

from fastapi.testclient import TestClient
from tools.run_sensor_gemma import Experiment, make_app


NEGATIVE = dict(goal_visible=False, confidence=.8, bearing='unknown', target_pixel=None,
    threshold_visible=False, threshold_pixel=None, at_threshold=False,
    hazard='clear', description='Podlaha a stěny.', evidence='Cílový otvor zde není viditelný.')


class Provider:
    def __init__(self): self.messages=[]
    async def infer(self,messages,schema,**kwargs):
        self.messages.append(messages)
        assert kwargs['provider']=='gemma' and kwargs['thinking'] is False
        parts=messages[-1]['content']
        assert len(parts)==2
        assert set(json.loads(parts[0]['text']))=={'goal'}
        assert parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
        return dict(NEGATIVE),{'provider':'fake','latency_ms':0}


def test_real_pulses_are_logged_but_world_is_never_sent_to_model(tmp_path):
    async def run():
        provider=Provider();experiment=Experiment(provider,max_steps=3)
        experiment.folder=tmp_path
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        status=experiment.status()
        assert status['step']==3 and status['status']=='blocked'
        assert len(provider.messages)==2  # 0, then after two scan directions
        assert status['observer']['speed']==0
        assert status['navigator']['accepted_steps']==3
        assert status['observer']['pose']['heading']!=0
        assert status['events'][0]['command']['acceptance']=='accepted'
        assert (tmp_path/'step-0003-after.jpg').exists()
        rows=[json.loads(row) for row in (tmp_path/'timeline.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(rows)==3 and all('observer' in row for row in rows)
    asyncio.run(run())


def test_pause_cancels_pending_perception_without_movement(tmp_path):
    class Held:
        async def infer(self,*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
    async def run():
        nonlocal entered
        entered=asyncio.Event()
        experiment=Experiment(Held());experiment.folder=tmp_path
        experiment.state['status']='running'
        experiment.worker=asyncio.create_task(experiment.run(experiment.generation))
        await asyncio.wait_for(entered.wait(),2)
        await experiment.pause()
        status=experiment.status()
        assert status['status']=='paused' and status['step']==0
        assert status['observer']['speed']==0
        assert status['navigator']['accepted_steps']==0
    entered=None
    asyncio.run(run())


def test_viewing_is_inert_and_cross_origin_mutations_are_rejected():
    provider=Provider();experiment=Experiment(provider)
    with TestClient(make_app(experiment)) as client:
        assert client.get('/api/status').json()['status']=='idle'
        assert client.get('/camera.jpg').headers['content-type']=='image/jpeg'
        assert client.post('/api/start',headers={'Origin':'https://unrelated.invalid'},json={'goal':'Najdi dveře.'}).status_code==403
        assert experiment.worker is None and provider.messages==[]


def test_visible_goal_without_localization_keeps_searching_and_concrete_hazard_stops(tmp_path):
    class ObservationProvider:
        def __init__(self,observation): self.observation=observation
        async def infer(self,*args,**kwargs): return dict(self.observation),{}
    async def run():
        uncertain={**NEGATIVE,'goal_visible':True,'confidence':.7,'threshold_visible':True,'threshold_pixel':.7}
        experiment=Experiment(ObservationProvider(uncertain),max_steps=2)
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        assert len(experiment.state['events'])==2
        assert all(row['controller']=='sonar_exploration' for row in experiment.state['events'])
        assert all(row['action'].startswith('rotate_') for row in experiment.state['events'])
        assert experiment.state['status']!='completed'
        danger=Experiment(ObservationProvider({**NEGATIVE,'hazard':'person'}),max_steps=4)
        danger.state['status']='running'
        await danger.run(danger.generation)
        assert all(row['action']=='stop' for row in danger.state['events'])
        assert danger.status()['observer']['travelled']==0
        assert danger.state['status']=='blocked'
    asyncio.run(run())


def test_pause_after_executed_pulse_keeps_its_evidence_before_resume(tmp_path, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    def held_metric(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'status':'unknown','motion':None}
    monkeypatch.setattr('tools.run_sensor_gemma.estimate_motion', held_metric)
    async def run():
        experiment=Experiment(Provider(),max_steps=3)
        experiment.folder=tmp_path
        experiment.navigator.next_command=lambda _: dict(action='forward',speed=180,duration_ms=500,
            tilt=90,reason='Bounded test pulse.',command_id='pause-test',scan=False)
        experiment.state['status']='running'
        experiment.worker=asyncio.create_task(experiment.run(experiment.generation))
        assert await asyncio.to_thread(entered.wait,3)
        pause=asyncio.create_task(experiment.pause())
        await asyncio.sleep(.02)
        assert experiment.state['status']=='paused'
        assert experiment.world.telemetry()['motor']['active'] is False
        release.set()
        await asyncio.wait_for(pause,3)
        assert experiment.state['status']=='paused' and experiment.state['phase']=='paused'
        assert experiment.state['step']==1
        assert experiment.navigator.accepted_steps==1
        assert (tmp_path/'step-0001-after.jpg').exists()
        assert len((tmp_path/'timeline.jsonl').read_text(encoding='utf-8').splitlines())==1
    try: asyncio.run(run())
    finally: release.set()


def test_visible_goal_cannot_bypass_mapped_chassis_clearance(monkeypatch):
    monkeypatch.setattr('server.threshold_pixels.detect_threshold', lambda _: dict(
        x=.5,y=.55,bounds=dict(left=.4,top=.55,right=.6,bottom=.58),quality=1.,
        provenance='simulated_colored_threshold_current_pixels',measurement={}))
    class GoalProvider:
        async def infer(self,*args,**kwargs):
            return {**NEGATIVE,'goal_visible':True,'confidence':1.,'bearing':'ahead',
                'target_pixel':{'x':.5,'y':.3},'threshold_visible':True,'threshold_pixel':.55},{}
    async def run():
        experiment=Experiment(GoalProvider(),max_steps=2)
        experiment.navigator.command_path_observed=lambda _:False
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        events=experiment.state['events']
        assert len(events)==2
        assert events[0]['semantic_path_deferred'] is True
        assert all(event['action'].startswith('rotate_') for event in events)
        assert experiment.world.snapshot()['travelled']==0
        assert experiment.target_cooldown>0
    asyncio.run(run())


def test_unmeasurable_marker_allows_alignment_but_no_guessed_translation(monkeypatch):
    monkeypatch.setattr('server.threshold_pixels.detect_threshold',lambda _:None)
    class DistantGoal:
        calls=0
        async def infer(self,*args,**kwargs):
            self.calls+=1
            return {**NEGATIVE,'goal_visible':True,'confidence':.9,'bearing':'right',
                'target_pixel':{'x':.785,'y':.182},'threshold_visible':True,'threshold_pixel':.355},{}
    async def run():
        provider=DistantGoal();experiment=Experiment(provider,max_steps=4)
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        assert experiment.state['step']==4
        assert provider.calls==2  # no repeated inference on deterministic missing pixels
        assert all(r['action'].startswith('rotate_') for r in experiment.state['events'])
        assert experiment.state['events'][0]['controller']=='semantic_target'
        assert all(not r['candidate_goal_reached'] for r in experiment.state['events'])
        assert experiment.target_memory.previous_observation() is None
        assert experiment.state['perception'] is None
        assert experiment.state['events'][0]['inference']['status']=='current_target_not_localized'
    asyncio.run(run())


def test_lost_localized_target_does_not_fall_back_to_exploratory_translation(monkeypatch):
    monkeypatch.setattr('server.threshold_pixels.detect_threshold',lambda _:dict(
        x=.2,y=.55,bounds=dict(left=.1,top=.55,right=.3,bottom=.58),quality=1.,
        provenance='simulated_colored_threshold_current_pixels',measurement={}))
    class SeenThenLost:
        calls=0
        async def infer(self,*args,**kwargs):
            self.calls+=1
            if self.calls>1:return dict(NEGATIVE),{}
            return {**NEGATIVE,'goal_visible':True,'confidence':1.,'bearing':'left',
                'target_pixel':{'x':.2,'y':.55},'threshold_visible':True,'threshold_pixel':.55},{}
    async def run():
        experiment=Experiment(SeenThenLost(),max_steps=3)
        experiment.navigator.next_command=lambda _:dict(action='forward',speed=180,duration_ms=500,
            tilt=90,reason='Exploration proposal must not win after target loss.',scan=False)
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        assert experiment.state['step']==3
        assert experiment.target_track.snapshot()['point'] is not None
        assert all(r['action'] not in ('forward','backward','left','right') for r in experiment.state['events'])
        assert experiment.state['events'][-1]['perception']['goal_visible'] is False
        assert experiment.world.snapshot()['travelled']==0
    asyncio.run(run())


def test_distant_opening_without_any_grounded_point_does_not_deadlock_exploration():
    class Opening:
        async def infer(self,*args,**kwargs):
            return {**NEGATIVE,'goal_visible':True,'confidence':.9,'bearing':'ahead',
                'target_pixel':{'x':.47,'y':.25}},{}
    async def run():
        experiment=Experiment(Opening(),max_steps=4)
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        events=experiment.state['events']
        assert len(events)==4 and events[0]['action']=='tilt'
        assert all(r['action'].startswith('rotate_') for r in events[1:])
        assert experiment.target_track.snapshot()['point'] is None
        assert all(not r['candidate_goal_reached'] for r in events)
    asyncio.run(run())


def test_ungrounded_goal_cannot_repeatedly_undo_the_mapped_route():
    class Opening:
        async def infer(self,*args,**kwargs):
            return {**NEGATIVE,'goal_visible':True,'confidence':.9,'bearing':'right',
                'target_pixel':{'x':.8,'y':.25}},{}
    async def run():
        experiment=Experiment(Opening(),max_steps=6)
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        events=experiment.state['events']
        assert [r['controller'] for r in events[:2]]==['semantic_target','semantic_target']
        assert all(r['controller']=='sonar_exploration' for r in events[2:])
        assert all(r['action']=='rotate_left' for r in events[2:])
        assert experiment.unlocalized_cooldown>0
        assert experiment.target_track.snapshot()['point'] is None
    asyncio.run(run())


def test_null_front_sensor_replans_around_remembered_target_without_forward(monkeypatch):
    monkeypatch.setattr('server.threshold_pixels.detect_threshold',lambda _:dict(
        x=.5,y=.55,bounds=dict(left=.4,top=.55,right=.6,bottom=.58),quality=1.,
        provenance='simulated_colored_threshold_current_pixels',measurement={}))
    class Goal:
        async def infer(self,*args,**kwargs):
            return {**NEGATIVE,'goal_visible':True,'confidence':.9,'bearing':'ahead',
                'target_pixel':{'x':.5,'y':.55},'threshold_visible':True,'threshold_pixel':.55},{}
    async def run():
        experiment=Experiment(Goal(),max_steps=4)
        original_telemetry=experiment.world.telemetry
        experiment.world.telemetry=lambda:{**original_telemetry(),'distance_cm':None}
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        events=experiment.state['events']
        assert len(events)==4
        assert all(r['action'].startswith('rotate_') for r in events)
        assert events[0]['semantic_sensor_deferred'] is True
        assert [r['target_cooldown'] for r in events]==[15,14,13,12]
        assert experiment.target_track.snapshot()['point'] is not None
    asyncio.run(run())


def test_other_invalid_perception_remains_bounded_and_stops():
    class Invalid:
        calls=0
        async def infer(self,*args,**kwargs):
            self.calls+=1
            return {**NEGATIVE,'confidence':12},{}
    async def run():
        provider=Invalid();experiment=Experiment(provider,max_steps=4)
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        assert provider.calls==3
        assert experiment.state['status']=='blocked' and experiment.state['step']==0
        assert experiment.world.snapshot()['travelled']==0
    asyncio.run(run())


def test_expired_target_performs_bounded_scan_and_never_translates():
    async def run():
        experiment=Experiment(Provider(),max_steps=25)
        experiment.target_track.update(
            dict(goal_visible=True,threshold_visible=True,confidence=.9,target_pixel=dict(x=.5,y=.75)),
            dict(status='localized',provenance='test_current_pixels',quality=.9),
            experiment.world.telemetry(),experiment.navigator.snapshot()['estimated_pose'])
        experiment.clock.now=100
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        events=experiment.state['events']
        rotations=[row for row in events if row['action'].startswith('rotate_')]
        assert len(rotations)==16
        assert all(row['command'].get('recovery_scan') is True for row in rotations)
        assert all(row['action'] in ('tilt','rotate_left','stop') for row in events)
        assert experiment.world.snapshot()['travelled']==0
        assert experiment.state['status']=='blocked'
        assert experiment.target_track.snapshot()['recovery']['exhausted'] is True
        assert all(not row['candidate_goal_reached'] for row in events)
    asyncio.run(run())


def test_current_hazard_prevents_target_search_even_when_track_expired():
    class Person:
        async def infer(self,*args,**kwargs):return {**NEGATIVE,'hazard':'person'},{}
    async def run():
        experiment=Experiment(Person(),max_steps=5)
        experiment.target_track.update(
            dict(goal_visible=True,threshold_visible=True,confidence=.9,target_pixel=dict(x=.5,y=.75)),
            dict(status='localized',provenance='test_current_pixels',quality=.9),
            experiment.world.telemetry(),experiment.navigator.snapshot()['estimated_pose'])
        experiment.clock.now=100
        experiment.state['status']='running'
        await experiment.run(experiment.generation)
        assert [row['action'] for row in experiment.state['events']]==['stop']*3
        assert experiment.target_track.snapshot()['recovery']['accepted_rotations']==0
    asyncio.run(run())
