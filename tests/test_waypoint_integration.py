"""Real task/actuation boundaries, fake inference only; never hardware/network."""
import asyncio
import threading

import pytest

from test_route_integration import (route_pipeline,simulator_client,operator_clock,a,
                                    decision,payload,wait_for,pause_pipeline,GOAL)
from server.prompt import response_schema


def install(monkeypatch,planner,assessor=None):
    sent=[];schemas=[];count=[0]
    original=a.send_control
    async def send(action='stop',*args,**kwargs):
        sent.append((action,args,kwargs));return await original(action,*args,**kwargs)
    async def infer(messages,schema):
        schemas.append(schema);count[0]+=1
        result=planner(count[0],schema)
        if hasattr(result,'__await__'):result=await result
        return a.Decision.model_validate(result)
    async def assess(*args,**kwargs):
        return (assessor() if assessor else dict(status='completed',progress='changed',summary='Test completed after observation.',
                evidence='Synthetic after image checked.',next_hint='')),{}
    monkeypatch.setattr(a,'send_control',send)
    monkeypatch.setattr(a,'infer',infer)
    monkeypatch.setattr(a,'assess_step',assess)
    return sent,schemas


def proposal(x=.2):
    result=payload(action='navigate')
    result.update(speed=180,duration_ms=100)
    result['waypoint']=dict(x=x,y=.8,evidence='Viditelný průchod v aktuálním obrazu.')
    return result


def start(client):
    response=client.post('/api/task',json={'goal':GOAL})
    assert response.status_code==200,response.text
    return wait_for(client,lambda t:t['status'] in ('completed','blocked','paused'))


def test_navigate_left_becomes_exactly_one_real_task_rotation(route_pipeline,monkeypatch):
    client,_=route_pipeline
    sent,schemas=install(monkeypatch,lambda n,s:proposal())
    task=start(client)
    wheels=[row for row in sent if row[0] in a.AI_WHEELS]
    assert [row[0] for row in wheels]==['rotate_left']
    assert wheels[0][1][0]==150 and 100<=wheels[0][1][1]<=500
    assert task['last_result']['proposed_action']=='navigate'
    assert task['last_result']['command']['action']=='rotate_left'
    assert 'navigate' in schemas[0]['properties']['action']['enum']


@pytest.mark.parametrize('case',['fresh_allowed','forbidden','stale_at_execution','null_at_execution'])
def test_centered_waypoint_never_bypasses_current_wheel_or_sonar_guard(route_pipeline,monkeypatch,case):
    client,_=route_pipeline
    original_telemetry=a.telemetry
    inferred=[False]
    async def telemetry():
        t=await original_telemetry()
        if inferred[0] and case=='stale_at_execution':t={**t,'telemetry_age_ms':501}
        if inferred[0] and case=='null_at_execution':t={**t,'distance_cm':None}
        return t
    monkeypatch.setattr(a,'telemetry',telemetry)
    if case=='forbidden':
        original_exploration=a.exploration_state
        def exploration(*args,**kwargs):
            result=original_exploration(*args,**kwargs)
            if result:result={**result,'allowed_wheels':['rotate_left','rotate_right']}
            return result
        monkeypatch.setattr(a,'exploration_state',exploration)
    def planner(n,s):inferred[0]=True;return proposal(.5)
    sent,_=install(monkeypatch,planner)
    task=start(client)
    wheels=[row[0] for row in sent if row[0] in a.AI_WHEELS]
    assert wheels==(['forward'] if case=='fresh_allowed' else [])
    assert task['last_result']['command']['action']==('forward' if wheels else 'stop')


def test_stop_during_late_waypoint_inference_cannot_actuate(route_pipeline,monkeypatch):
    client,_=route_pipeline;entered=threading.Event();returned=threading.Event()
    async def planner(n,s):
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:returned.set();return proposal()
    sent,_=install(monkeypatch,planner)
    assert client.post('/api/task',json={'goal':GOAL}).status_code==200
    assert entered.wait(2)
    task=pause_pipeline(client)
    assert returned.is_set() and task['current_step']==0
    assert not [row for row in sent if row[0] in a.AI_WHEELS]


def test_nonisolated_instance_neither_offers_nor_executes_navigate(route_pipeline,monkeypatch):
    client,_=route_pipeline
    monkeypatch.setattr(a,'SIMULATION_ONLY',False)
    sent,schemas=install(monkeypatch,lambda n,s:proposal())
    task=start(client)
    assert all('navigate' not in schema['properties']['action']['enum'] for schema in schemas)
    assert not [row for row in sent if row[0] in a.AI_WHEELS]
    assert task['last_result']['command']['action']=='stop'


@pytest.mark.parametrize('isolated',[True,False])
def test_two_stalls_preserve_physical_guard_but_sim_navigation_can_escape(route_pipeline,monkeypatch,isolated):
    client,_=route_pipeline
    monkeypatch.setattr(a,'SIMULATION_ONLY',isolated)
    original_observe=a.vision_history.observe
    def observe(*args,**kwargs):
        frames=original_observe(*args,**kwargs)
        if frames:frames[-1]['motion_observation']=dict(status='possible_stall',movement_confirmed=False,reason='Comparable synthetic frames')
        return frames
    monkeypatch.setattr(a.vision_history,'observe',observe)
    def planner(n,s):
        return decision(action=('forward','left','backward')[min(n-1,2)],speed=180,duration_ms=50).model_dump()
    verifications=[0]
    def assess():
        verifications[0]+=1
        return dict(status='completed' if verifications[0]>=3 else 'continue',progress='unchanged',
                    summary='Synthetic motion evaluation.',evidence='Comparable images show possible stall.',next_hint='Try another permitted recovery direction.')
    sent,_=install(monkeypatch,planner,assess)
    task=start(client)
    wheels=[row[0] for row in sent if row[0] in a.AI_WHEELS]
    if isolated:
        assert wheels==['forward','left','backward']
        assert task['completed_steps'][1]['assessment']['status']=='continue'
        assert task['completed_steps'][2]['navigation_state']['phase']=='escape'
    else:
        assert wheels==['forward','left']
        assert task['status']=='blocked'
