"""The scan memory constrains generation and execution, never reads world truth."""
import asyncio
import json
import time

import pytest

from test_simulator_isolation import simulator_client, a, decision
from server.prompt import response_schema


def rotation(action='rotate_left'):
    return {'action':action,'command':{'action':action},'acceptance':'accepted',
            'motion_observation':{'status':'changed'}}


def test_simulated_observations_are_remembered_but_not_carried_into_reset_scene(simulator_client,monkeypatch):
    async def infer(*args,**kwargs):
        return decision(action='stop',memory='VISIBLE_PASSAGE_ON_RIGHT')
    monkeypatch.setattr(a,'infer',infer)
    response=simulator_client.post('/api/turn',json={'intent':'observe','vision':True})
    assert response.status_code==200,response.text
    assert 'VISIBLE_PASSAGE_ON_RIGHT' in json.dumps(a.memory())
    a.task_runner.data['completed_steps']=[{'step':1,'command':{'action':'rotate_left'},
        'acceptance':'accepted','decision_reason':'PASSAGE_SUBGOAL',
        'decision_evidence':'Passage at right edge','assessment':{'status':'continue'}}]
    assert 'PASSAGE_SUBGOAL' in json.dumps(a.verified_steps())
    assert simulator_client.post('/api/simulator/reset',json={'scenario':'apartment'}).status_code==200
    assert 'VISIBLE_PASSAGE_ON_RIGHT' not in json.dumps(a.memory())
    assert a.verified_steps()==[]


@pytest.mark.parametrize('isolated',[True,False])
def test_task_scan_memory_is_simulator_only_and_rejects_inverse(simulator_client,monkeypatch,isolated):
    monkeypatch.setattr(a,'SIMULATION_ONLY',isolated)
    async def capture(path,*args,**kwargs):
        import httpx
        return httpx.Response(200,content=a.simulation_jpeg())
    monkeypatch.setattr(a,'device',capture)
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    a.task_runner.data.update(goal='Najdi dveře a dojeď k nim',completed_steps=[rotation()])
    proposals=iter(['rotate_right','forward'])
    schemas=[]
    async def infer(messages,schema):
        schemas.append(schema)
        # A malformed provider/test double must not bypass the actuator check.
        return decision(action=next(proposals),duration_ms=200,speed=100)
    monkeypatch.setattr(a,'infer',infer)
    result=asyncio.run(a.execute_turn(a.Turn(text='Najdi dveře',intent='act'),a.state['owner'],task_mode=True))
    assert result['executed_action']==('forward' if isolated else 'rotate_right')
    if isolated:
        assert 'rotate_right' not in schemas[0]['properties']['action']['enum']
        assert result['replans'] and result['navigation_state']['scan_direction']=='rotate_left'
        assert 'pose' not in json.dumps(result['navigation_state'])
    else:
        assert result['navigation_state'] is None


@pytest.mark.parametrize('age,distance,expected',[(0,120,True),(501,120,False),(0,10,False),(0,None,False)])
def test_scan_advance_never_weakens_sensor_guard(age,distance,expected):
    navigation={'policy':'sensor','exploration':{'allowed_wheels':['forward']}}
    telemetry={'telemetry_age_ms':age,'distance_cm':distance,'forward_stop_cm':10,
               'motor_max_duration_ms':2000,'servo':90}
    schema=response_schema('ai','simulator',navigation,telemetry)
    actions=schema['properties']['action']['enum']
    assert ('forward' in actions) is expected
    assert 'stop' in actions and 'rotate_right' not in actions


@pytest.mark.parametrize('planning_distance,execution_distance,proposal',[
    (100,40,'forward'),(40,100,'rotate_left'),
])
def test_changed_final_scan_allowance_stops_without_replacing_model_action(simulator_client,monkeypatch,
        planning_distance,execution_distance,proposal):
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    a.task_runner.data.update(goal='Najdi dveře a dojeď k nim',completed_steps=[rotation()]*3)
    base={'distance_cm':planning_distance,'telemetry_age_ms':0,'connected':True,
          'servo':90,'motor_max_duration_ms':2000,'forward_stop_cm':10}
    commands=[];gates=[];calls=[]
    async def planning(**kwargs):return dict(base)
    async def telemetry():return {**base,'distance_cm':execution_distance}
    async def infer(messages,schema):
        calls.append(schema)
        assert proposal in schema['properties']['action']['enum']
        return decision(action=proposal,speed=120,duration_ms=200)
    async def send(action='stop',*args,**kwargs):commands.append(action)
    def diagnostic(kind,**payload):
        if kind=='gate':gates.append(payload)
    monkeypatch.setattr(a,'planning_telemetry',planning)
    monkeypatch.setattr(a,'telemetry',telemetry)
    monkeypatch.setattr(a,'infer',infer)
    monkeypatch.setattr(a,'send_control',send)
    monkeypatch.setattr(a,'diagnostic_event',diagnostic)
    result=asyncio.run(a.execute_turn(a.Turn(text='Najdi dveře',intent='act'),a.state['owner'],task_mode=True))
    assert len(calls)==1 and commands==['stop']
    assert result['proposed_command']['action']==proposal and result['executed_action']=='stop'
    assert result['_command']['action']=='stop'
    assert 'Průzkumná paměť' in result['execution_reason']
    assert gates[-1]['reason']==result['execution_reason']
    assert gates[-1]['proposal']['action']==proposal and gates[-1]['actual']['action']=='stop'
    assert a.task_pending is None


@pytest.mark.parametrize('race',['telemetry_expired','stop','epoch','lease_expired'])
def test_scan_final_guard_preserves_expiry_and_stop_reasons(simulator_client,monkeypatch,race):
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    a.task_runner.data.update(goal='Najdi dveře a dojeď k nim',completed_steps=[rotation()]*3)
    sample={'distance_cm':100,'telemetry_age_ms':0,'connected':True,
            'servo':90,'motor_max_duration_ms':2000,'forward_stop_cm':10}
    commands=[];gates=[];inference_calls=[]
    async def planning(**kwargs):return dict(sample)
    async def telemetry():return {**sample,'telemetry_age_ms':501 if race=='telemetry_expired' else 0}
    async def infer(*args,**kwargs):
        inference_calls.append(1)
        if race=='stop':await a.halt('Test STOP during inference',emergency=True)
        elif race=='epoch':a.state['epoch']+=1
        elif race=='lease_expired':a.state['heartbeat']=time.monotonic()-2
        return decision(action='forward',speed=120,duration_ms=200)
    async def send(action='stop',*args,**kwargs):commands.append(action)
    def diagnostic(kind,**payload):
        if kind=='gate':gates.append(payload)
    monkeypatch.setattr(a,'planning_telemetry',planning)
    monkeypatch.setattr(a,'telemetry',telemetry)
    monkeypatch.setattr(a,'infer',infer)
    monkeypatch.setattr(a,'send_control',send)
    monkeypatch.setattr(a,'diagnostic_event',diagnostic)
    if race!='telemetry_expired':
        # Authority loss now discards the inference before reaching the final
        # motor gate. Only the explicit emergency STOP may actuate.
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(a.execute_turn(a.Turn(text='Najdi dveře',intent='act'),a.state['owner'],task_mode=True))
        assert inference_calls==[1]  # no read-only retry after STOP/epoch/lease loss
        assert gates==[]
        assert commands==(['estop'] if race=='stop' else [])
        assert a.task_pending is None
        return
    result=asyncio.run(a.execute_turn(a.Turn(text='Najdi dveře',intent='act'),a.state['owner'],task_mode=True))
    assert inference_calls==[1]
    assert result['executed_action']=='stop'
    assert not set(commands)&set(a.AI_WHEELS)
    expected='Chybí čerstvá telemetrie auta.'
    assert result['execution_reason']==expected
    assert gates[-1]['reason']==expected
    assert result['proposed_command']['action']=='forward'
    assert a.task_pending is None
    assert gates[-1]['actual']['action']=='stop'
