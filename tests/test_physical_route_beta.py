"""Physical-car task integration with a fake device/provider; no network or GT."""
from copy import deepcopy
import io
import json

import httpx
from PIL import Image, ImageDraw
import pytest

from test_safety import a, decision
from test_task_integration import task_client, wait_task


class MockCar:
    def __init__(self):
        self.controls=[]
        self.captures=0
        self.translations=0
        self.distance=100
        self.age=20

    async def device(self,path,method='GET',payload=None):
        if path=='/api/status':
            return httpx.Response(200,json=dict(connected=True,camera_ready=True,
                distance_cm=self.distance,telemetry_age_ms=self.age,servo=55,
                light_supported=True,light_on=False,estop=False,uptime_ms=self.captures*100))
        if path=='/capture':
            self.captures+=1
            # Different AFTER pixels corroborate the accepted translation; this
            # is a mock camera, not simulator rendering or metric odometry.
            image=Image.new('RGB',(160,120),(80+self.translations*45,100,120))
            ImageDraw.Draw(image).rectangle((10+self.translations*20,35,65+self.translations*20,100),fill=(20,40,210))
            output=io.BytesIO();image.save(output,format='JPEG')
            return httpx.Response(200,content=output.getvalue())
        assert path=='/api/control' and method=='POST', 'Unexpected device I/O'
        self.controls.append(deepcopy(payload))
        if payload['action']=='forward':self.translations+=1
        return httpx.Response(200,json={'ok':True})


def route_choice(identifier):
    return dict(route_id=identifier,label='Průchod vedle modré stěny',
        landmark='Modrá hrana a navazující podlaha',bearing='ahead',
        evidence='V aktuálním obrazu je vidět pokračující podlaha průchodem.')


def planning_state(messages):
    text=next(part['text'] for part in messages[-1]['content']
              if part.get('type')=='text' and 'Stav a starší poznámky (data): ' in part['text'])
    return json.loads(text.split('Stav a starší poznámky (data): ',1)[1].split('\nUživatel: ',1)[0])


def schema_property_names(value):
    result=set()
    if isinstance(value,dict):
        result.update(value.get('properties',{}))
        for child in value.values():result.update(schema_property_names(child))
    elif isinstance(value,list):
        for child in value:result.update(schema_property_names(child))
    return result


class RouteProvider:
    def __init__(self,car,second_sensor=None):
        self.car=car;self.second_sensor=second_sensor;self.plans=[];self.verifications=[]

    async def infer(self,messages,schema,**kwargs):
        assert a.settings['simulation'] is False and a.SIMULATION_ONLY is False
        assert kwargs['provider']=='gemma'
        if 'route_feedback' not in schema['properties']:
            context=planning_state(messages)
            self.plans.append((deepcopy(context),deepcopy(schema)))
            assert context['image_source']=='car' and context['telemetry']['simulation'] is False
            assert context['navigation']['route_memory_enabled'] is True
            assert not context['navigation'].get('exploration')
            assert 'route_choice' in schema['required']
            assert 'navigate' not in schema['properties']['action']['enum']
            assert not {'waypoint','x','y','pose','heading','coordinates','distance_m'} & schema_property_names(schema)
            choice=schema['properties']['route_choice']['properties']
            assert set(choice)=={'route_id','label','landmark','bearing','evidence'}
            if len(self.plans)==1:
                assert context['navigation']['route_memory']['routes']==[]
                identifier='new'
            else:
                remembered=context['navigation']['route_memory']
                assert remembered['active_route_id']=='R1'
                assert remembered['routes'][0]['id']=='R1'
                assert remembered['routes'][0]['status']=='closer'
                assert remembered['routes'][0]['feedback_evidence']=='Průchod je po prvním kroku větší.'
                assert 'R1' in choice['route_id']['enum']
                identifier='R1'
            # Deliberately keep asking forward for the invalid-telemetry tests:
            # an overoptimistic model cannot bypass the actuator boundary.
            return decision(action='forward',speed=80,duration_ms=50,tilt=55,
                            route_choice=route_choice(identifier)).model_dump(),{}
        self.verifications.append((deepcopy(messages),deepcopy(schema)))
        content=messages[-1]['content']
        assert len([p for p in content if p['type']=='image_url'])==2
        record=json.loads(content[0]['text'].split('(data): ',1)[1])
        # The persisted BEFORE record intentionally contains only measured
        # fields; physical source was asserted in the actual planning request.
        expected_age=self.second_sensor[1] if len(self.verifications)==2 and self.second_sensor else 20
        assert record['before_telemetry']['telemetry_age_ms']==expected_age
        assert record['after_telemetry']['simulation'] is False
        assert record['command']['route_choice']['route_id'] in ('new','R1')
        memory_text=next(p['text'] for p in content if p['type']=='text' and p['text'].startswith('PAMĚŤ MÍST'))
        remembered=json.loads(memory_text.split('(data): ',1)[1])
        if len(self.verifications)==2:
            assert remembered['routes'][0]['id']=='R1'
            assert remembered['routes'][0]['feedback_evidence']=='Průchod je po prvním kroku větší.'
        second=len(self.verifications)==2
        blocked=second and self.second_sensor is not None
        feedback=dict(status='blocked' if blocked else 'passed' if second else 'closer',
            evidence='Čidlo nepovolilo další pohyb.' if blocked else
                     'Po druhém kroku je vidět nový prostor.' if second else 'Průchod je po prvním kroku větší.',
            place_id='new' if second and not blocked else 'unknown',
            place_label='Prostor za průchodem' if second and not blocked else '',
            place_evidence='Nový roh za modrou hranou je vidět na snímku PO.' if second and not blocked else '')
        if not second and self.second_sensor is not None:
            self.car.distance,self.car.age=self.second_sensor
        return dict(status='blocked' if blocked else 'completed' if second else 'continue',
            progress='unchanged' if blocked else 'changed',summary='Ověřen nový pohled.',
            evidence='Porovnány skutečné snímky mock kamery PŘED a PO.',next_hint='',
            route_feedback=feedback),{}


def configure_physical_mock(client,monkeypatch,second_sensor=None):
    car=MockCar();provider=RouteProvider(car,second_sensor)
    monkeypatch.setattr(a,'SIMULATION_ONLY',False)
    monkeypatch.setitem(a.settings,'simulation',False)
    monkeypatch.setitem(a.settings,'transport','lan')
    monkeypatch.setitem(a.settings,'navigation_policy','strict')
    monkeypatch.setitem(a.settings,'floor_calibrated',True)
    monkeypatch.setitem(a.settings,'floor_angle',55)
    monkeypatch.setitem(a.settings,'ai_provider','gemma')
    monkeypatch.setitem(a.settings,'thinking_enabled',False)
    monkeypatch.setattr(a,'device',car.device)
    monkeypatch.setattr(a,'ai_provider',provider)
    def forbidden_simulator(*args,**kwargs):raise AssertionError('Physical beta must not render or consult a simulator')
    monkeypatch.setattr(a,'simulation_jpeg',forbidden_simulator)
    monkeypatch.setattr(a,'simulator',forbidden_simulator)
    assert client.post('/api/arm',json={}).status_code==200
    assert client.get('/api/status').json()['telemetry']['simulation'] is False
    return car,provider


def test_physical_task_remembers_route_in_second_plan_and_persists_feedback(task_client,monkeypatch):
    client,path=task_client
    car,provider=configure_physical_mock(client,monkeypatch)
    monkeypatch.setattr(a,'history',lambda:[{'role':'user','content':'UNRELATED_OLD_DOOR_TASK'}])
    response=client.post('/api/task',json={'goal':'Prozkoumej viditelný průchod a zastav v novém prostoru.'})
    assert response.status_code==200,response.text
    task=wait_task(client)
    assert task['status']=='completed' and task['current_step']==2,(task,len(provider.plans),len(provider.verifications))
    assert len(provider.plans)==len(provider.verifications)==2
    assert 'UNRELATED_OLD_DOOR_TASK' not in json.dumps(provider.plans)
    assert [c['action'] for c in car.controls if c['action']=='forward']==['forward','forward']
    assert car.captures==4
    assert all(row['acceptance']=='accepted' for row in task['completed_steps'])
    assert [row['route_feedback']['status'] for row in task['completed_steps']]==['closer','passed']
    route=task['route_memory']['routes'][0]
    assert route['id']=='R1' and route['status']=='passed' and route['attempts']==2
    assert route['source']=='P1' and route['target']=='P2'
    assert route['feedback_evidence']=='Po druhém kroku je vidět nový prostor.'
    saved=json.loads((path/'task.json').read_text(encoding='utf-8'))
    assert saved['route_memory']==task['route_memory']
    assert saved['completed_steps'][1]['route_feedback']['status']=='passed'


@pytest.mark.parametrize('second_sensor',[(None,20),(100,501)])
def test_remembered_physical_route_never_bypasses_missing_or_stale_front_telemetry(task_client,monkeypatch,second_sensor):
    client,_=task_client
    car,provider=configure_physical_mock(client,monkeypatch,second_sensor)
    assert client.post('/api/task',json={'goal':'Pokračuj průchodem jen při bezpečných měřeních.'}).status_code==200
    task=wait_task(client)
    assert task['status']=='blocked' and task['current_step']==2
    assert len(provider.plans)==len(provider.verifications)==2
    assert [c['action'] for c in car.controls if c['action']=='forward']==['forward']
    assert task['completed_steps'][1]['proposed_action']=='forward'
    assert task['completed_steps'][1]['action']=='stop'
    assert 'forward' not in provider.plans[1][1]['properties']['action']['enum']
    assert task['route_memory']['routes'][0]['status']=='blocked'


@pytest.mark.parametrize('second_sensor',[None,(None,20),(100,501)])
def test_operator_power_profile_applies_only_to_approved_physical_motion(task_client,monkeypatch,second_sensor):
    client,_=task_client
    car,provider=configure_physical_mock(client,monkeypatch,second_sensor)
    monkeypatch.setitem(a.settings,'navigation_policy','sensor')
    assert client.post('/api/task',json={'goal':'Prozkoumej průchod.'}).status_code==200
    task=wait_task(client,timeout=6)
    moves=[command for command in car.controls if command['action']=='forward']
    assert len(moves)==(2 if second_sensor is None else 1)
    assert all(command['speed']==180 and command['duration_ms']==350 for command in moves)
    assert task['completed_steps'][0]['proposed_command']['speed']==80
    assert task['completed_steps'][0]['command']['speed']==180
    if second_sensor is not None:assert task['completed_steps'][1]['action']=='stop'


@pytest.mark.parametrize('second_sensor',[None,(None,20),(100,501)])
def test_approved_physical_profile_uses_both_board_full_pwm_and_one_second(task_client,monkeypatch,second_sensor):
    client,_=task_client
    car,provider=configure_physical_mock(client,monkeypatch,second_sensor)
    monkeypatch.setitem(a.settings,'navigation_policy','sensor')
    original=car.device
    async def device(path,method='GET',payload=None):
        response=await original(path,method,payload)
        if path=='/api/status':
            return httpx.Response(200,json={**response.json(),'motor_max_speed':255,'esp_motor_max_speed':255,
                'motor_max_duration_ms':2000,'forward_stop_cm':10})
        return response
    monkeypatch.setattr(a,'device',device)
    assert client.post('/api/task',json={'goal':'Prozkoumej pruchod.'}).status_code==200
    task=wait_task(client,timeout=8)
    moves=[cmd for cmd in car.controls if cmd['action']=='forward']
    assert len(moves)==(2 if second_sensor is None else 1) and all(cmd['speed']==255 and cmd['duration_ms']==1000 for cmd in moves)
    assert task['completed_steps'][0]['proposed_command']['speed']==80
    assert task['completed_steps'][0]['command']['speed']==255

    if second_sensor is not None:assert task['completed_steps'][1]['action']=='stop'
