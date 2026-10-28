"""Route hypotheses cross the real planner, verifier and TaskRunner boundaries.

Only the provider and camera pixels are synthetic. No model API, hardware, world
pose or hidden apartment map is consulted by these tests.
"""
import asyncio
from copy import deepcopy
import io
import json
import threading
import time

import pytest
from PIL import Image
from pydantic import ValidationError

from test_simulator_isolation import simulator_client,operator_clock,a,decision
from server.ai_provider import _validate
from server.prompt import SCHEMA,response_schema
from server.route_memory import empty_route_memory


GOAL='Najdi výstup za průchodem a dojeď k němu.'


def choice(identifier='new'):
    return dict(route_id=identifier,label='Průchod vpravo',landmark='Konec modré stěny',
                bearing='right',evidence='Podlaha pokračuje napravo za hranou modré stěny.')


def payload(identifier='new',action='rotate_left'):
    return decision(action=action,speed=100,duration_ms=50,tilt=55,turn_clear=True,
                    route_choice=choice(identifier),waypoint=dict(x=.5,y=.8,evidence='')).model_dump()


def embedded_state(messages,marker):
    for message in messages:
        parts=message.get('content')
        for part in parts if isinstance(parts,list) else []:
            text=part.get('text','')
            if marker in text:
                return json.JSONDecoder().raw_decode(text.split(marker,1)[1])[0]
    raise AssertionError('Missing context JSON: '+marker)


def no_hidden_map(messages):
    forbidden={'pose','geometry','bounds','scenario','seed','door_distance','crossed_threshold','goal_reached'}
    def inspect(value):
        if isinstance(value,dict):
            assert not forbidden&value.keys(),'Hidden simulator metadata leaked: '+repr(forbidden&value.keys())
            for child in value.values():inspect(child)
        elif isinstance(value,list):
            for child in value:inspect(child)
    for marker in ('Stav a starší poznámky (data): ','ÚKOL A ZÁZNAM KROKU (data): '):
        try:context=embedded_state(messages,marker)
        except AssertionError:continue
        inspect(context)


def wait_for(client,predicate,timeout=4):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        client.post('/api/heartbeat',json={})
        response=client.get('/api/status')
        assert response.status_code==200,response.text
        task=response.json()['task']
        if predicate(task):return task
        time.sleep(.01)
    pytest.fail('Task did not reach the expected state: '+repr(a.task_runner.snapshot()))


class RouteProvider:
    def __init__(self):
        self.plans=[];self.verifications=[];self.held=threading.Event()

    async def infer(self,messages,schema,**kwargs):
        no_hidden_map(messages)
        if 'route_feedback' in schema['properties']:
            self.verifications.append((deepcopy(messages),deepcopy(schema)))
            value=dict(status='continue',progress='changed',summary='Průchod je stále mezicíl, výstup zatím nevidím.',
                evidence='Na snímku PO stále pokračuje podlaha za modrou stěnou.',
                next_hint='Pokračuj k dosavadnímu průchodu.',
                route_feedback=dict(status='visible',evidence='Průchod zůstává viditelný vpravo.',place_id='unknown',place_label='',place_evidence=''))
        else:
            self.plans.append((deepcopy(messages),deepcopy(schema)))
            if len(self.plans)>2:
                self.held.set()
                await asyncio.Event().wait()
            value=payload('new' if len(self.plans)==1 else 'R1',
                          'rotate_left' if len(self.plans)==1 else 'forward')
        _validate(value,schema)
        return value,{'provider':'fake','model':'test-only','latency_ms':0}


@pytest.fixture
def route_pipeline(simulator_client,operator_clock,monkeypatch):
    provider=RouteProvider()
    monkeypatch.setattr(a,'ai_provider',provider)
    a.settings['ai_provider']='openai'
    a.task_runner.interval=0
    frames=[]
    for color in ('#202020','#d0d0d0','#404040','#b0b0b0'):
        image=io.BytesIO();Image.new('RGB',(80,60),color).save(image,format='JPEG')
        frames.append(image.getvalue())
    index=[0]
    def camera():
        raw=frames[index[0]%len(frames)];index[0]+=1;return raw
    monkeypatch.setattr(a,'simulation_jpeg',camera)
    assert simulator_client.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert simulator_client.post('/api/arm',json={}).status_code==200
    return simulator_client,provider


def start_two_steps(pipeline):
    client,provider=pipeline
    started=client.post('/api/task',json={'goal':GOAL})
    assert started.status_code==200,started.text
    task=wait_for(client,lambda task:task['current_step']==2 and provider.held.is_set())
    assert task['status']=='running',task
    return task


def pause_pipeline(client):
    assert client.post('/api/stop',json={}).status_code==200
    return wait_for(client,lambda task:task['status']=='paused' and
                    (not a.task_runner.worker or a.task_runner.worker.done()))


def test_verified_route_is_persisted_reselected_and_never_completes_the_goal_by_itself(route_pipeline):
    client,provider=route_pipeline
    task=start_two_steps(route_pipeline)
    assert len(provider.verifications)==2
    first_messages,first_schema=provider.plans[0]
    second_messages,second_schema=provider.plans[1]
    assert first_schema['properties']['route_choice']['properties']['route_id']['enum']==['none','new']
    assert 'route_choice' in first_schema['required']
    first_state=embedded_state(first_messages,'Stav a starší poznámky (data): ')
    second_state=embedded_state(second_messages,'Stav a starší poznámky (data): ')
    assert first_state['navigation']['route_memory']==empty_route_memory()
    memory=second_state['navigation']['route_memory']
    assert memory['active_route_id']=='R1'
    assert memory['routes'][0]['label']==choice()['label']
    assert memory['routes'][0]['status']=='visible'
    assert memory['routes'][0]['provenance']=='inferred_by_model'
    assert second_schema['properties']['route_choice']['properties']['route_id']['enum']==['none','new','R1']
    assert [row['route_choice']['route_id'] for row in task['completed_steps']]==['new','R1']
    assert all(row['route_feedback']['status']=='visible' for row in task['completed_steps'])
    assert all(row['assessment']['status']=='continue' for row in task['completed_steps'])
    for index,(messages,schema) in enumerate(provider.verifications):
        assert 'route_feedback' in schema['required']
        context=embedded_state(messages,'ÚKOL A ZÁZNAM KROKU (data): ')
        assert context['command']['route_choice']['route_id']==('new' if index==0 else 'R1')
        assert context['command']['acceptance']=='accepted'
    assert task['route_memory']['active_route_id']=='R1' and len(task['route_memory']['routes'])==1
    assert task['route_memory']['routes'][0]['attempts']==1
    assert task['status']=='running' and task['outcome']==''
    saved=json.loads((a.DATA/'task.json').read_text(encoding='utf-8'))
    assert saved['route_memory']==task['route_memory']
    pause_pipeline(client)


@pytest.mark.parametrize('operation',['reset','new_task','resume'])
def test_route_lifecycle_clears_new_world_or_task_and_preserves_resume(route_pipeline,operation):
    client,provider=route_pipeline
    original=start_two_steps(route_pipeline)
    paused=pause_pipeline(client)
    assert paused['route_memory']==original['route_memory']
    if operation=='reset':
        response=client.post('/api/simulator/reset',json={})
        assert response.status_code==200,response.text
        task=client.get('/api/status').json()['task']
    else:
        assert client.post('/api/arm',json={}).status_code==200
        if operation=='resume':response=client.post('/api/task/resume',json={})
        else:response=client.post('/api/task',json={'goal':'Najdi jiný průchod.'})
        assert response.status_code==200,response.text
        task=response.json()
        if operation=='resume':
            assert task['id']==original['id'] and task['current_step']==2
            assert task['route_memory']==original['route_memory']
        else:assert task['id']!=original['id'] and task['current_step']==0
        wait_for(client,lambda task:len(provider.plans)>=4)
        resumed_state=embedded_state(provider.plans[-1][0],'Stav a starší poznámky (data): ')
        expected=original['route_memory'] if operation=='resume' else empty_route_memory()
        assert resumed_state['navigation']['route_memory']==expected
        pause_pipeline(client)
    if operation!='resume':assert task['route_memory']==empty_route_memory()
    saved=json.loads((a.DATA/'task.json').read_text(encoding='utf-8'))
    assert saved['route_memory']==task['route_memory']


@pytest.mark.parametrize('stage',['planner','verifier'])
def test_stop_during_inference_cannot_commit_a_late_new_route(route_pipeline,monkeypatch,stage):
    client,provider=route_pipeline
    entered=threading.Event();late_returned=threading.Event();sent=[]
    async def late_provider(messages,schema,**kwargs):
        verifying='route_feedback' in schema['properties']
        assert ('route_feedback' if verifying else 'route_choice') in schema['required']
        value=(dict(status='continue',progress='changed',summary='Průchod je mezicíl.',
                    evidence='Je stále vidět.',next_hint='Pokračuj k němu.',
                    route_feedback=dict(status='visible',evidence='Podlaha pokračuje za stěnu.',place_id='unknown',place_label='',place_evidence=''))
               if verifying else payload())
        if stage=='verifier' and not verifying:
            _validate(value,schema)
            return value,{}
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            late_returned.set()
            _validate(value,schema)
            return value,{}
    original_send=a.send_control
    async def send(action='stop',*args,**kwargs):
        sent.append(action)
        return await original_send(action,*args,**kwargs)
    monkeypatch.setattr(provider,'infer',late_provider)
    monkeypatch.setattr(a,'send_control',send)
    assert client.post('/api/task',json={'goal':GOAL}).status_code==200
    assert entered.wait(2)
    commands_before_stop=[action for action in sent if action in a.AI_WHEELS]
    task=pause_pipeline(client)
    assert late_returned.is_set()
    assert task['route_memory']==empty_route_memory() and task['current_step']==0
    assert task['completed_steps']==[] and task['last_result'] is None
    assert [action for action in sent if action in a.AI_WHEELS]==commands_before_stop
    assert commands_before_stop==([] if stage=='planner' else ['rotate_left'])
    saved=json.loads((a.DATA/'task.json').read_text(encoding='utf-8'))
    assert saved['route_memory']==empty_route_memory()


def test_route_schema_and_pydantic_parser_preserve_nested_fields_without_changing_base_schema():
    base=deepcopy(SCHEMA)
    navigation=dict(policy='sensor',floor_calibrated=True,floor_valid=True,floor_angle=55,
        exploration=dict(allowed_wheels=list(a.AI_WHEELS),reason='Test scan'),
        route_memory=dict(routes=[dict(id='R1')]))
    sample=dict(distance_cm=100,telemetry_age_ms=0,servo=90)
    schema=response_schema('ai','simulator',navigation,sample)
    value=payload('R1')
    _validate(value,schema)
    parsed=a.Decision.model_validate_json(json.dumps(value))
    assert parsed.route_choice.model_dump()==choice('R1')
    assert SCHEMA==base and 'route_choice' not in SCHEMA['properties']
    invalid=deepcopy(value);invalid['route_choice']['route_id']='R999'
    with pytest.raises(ValueError):_validate(invalid,schema)
    invalid=deepcopy(value);invalid['route_choice']['pose']={'x':8,'y':3}
    with pytest.raises(ValueError):_validate(invalid,schema)
    with pytest.raises(ValidationError):a.Decision.model_validate(invalid)
    ordinary=response_schema('ai','simulator',{k:v for k,v in navigation.items() if k!='exploration'},sample)
    assert 'route_choice' not in ordinary['properties'] and 'route_choice' not in ordinary['required']
    assert decision().route_choice is None
