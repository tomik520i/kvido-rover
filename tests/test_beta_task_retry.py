import asyncio
import copy
import time
import httpx
import pytest
from test_safety import a,c,decision


def test_planner_retry_uses_same_context_and_never_actuates(monkeypatch):
    messages=[{'role':'user','content':'frame'}];schema={'type':'object'};calls=[];sent=[]
    monkeypatch.setattr(a,'task_authorized',lambda sid:True)
    async def infer(m,s):
        calls.append((copy.deepcopy(m),copy.deepcopy(s)))
        if len(calls)<3:raise ValueError('invalid structured reply')
        return decision()
    async def send(*args):sent.append(args)
    monkeypatch.setattr(a,'infer',infer);monkeypatch.setattr(a,'send_control',send)
    result=asyncio.run(a.planner_inference(messages,schema,'owner',a.state['epoch'],5))
    assert result.action=='forward' and len(calls)==3
    assert all(pair==(messages,schema) for pair in calls) and not sent


def test_planner_retry_discards_result_when_stop_epoch_changes(monkeypatch):
    epoch=a.state['epoch'];calls=[]
    monkeypatch.setattr(a,'task_authorized',lambda sid:True)
    async def infer(*args):
        calls.append(1);a.state['epoch']+=1
        return decision()
    monkeypatch.setattr(a,'infer',infer)
    with pytest.raises(asyncio.CancelledError):asyncio.run(a.planner_inference([],{},'owner',epoch,5))
    assert len(calls)==1


def test_planner_retry_stops_after_three_invalid_replies(monkeypatch):
    calls=[];monkeypatch.setattr(a,'task_authorized',lambda sid:True)
    async def infer(*args):calls.append(1);raise ValueError('invalid')
    monkeypatch.setattr(a,'infer',infer)
    with pytest.raises(ValueError):asyncio.run(a.planner_inference([],{},'owner',a.state['epoch'],5))
    assert len(calls)==3


@pytest.fixture
def task_context(c,monkeypatch):
    c.post('/api/mode',json={'mode':'ai'});c.post('/api/arm',json={})
    monkeypatch.setitem(a.settings,'simulation',False);monkeypatch.setitem(a.settings,'transport','lan')
    monkeypatch.setitem(a.settings,'navigation_policy','sensor')
    monkeypatch.setattr(a,'task_pending',None)
    async def telemetry():return {'distance_cm':100,'telemetry_age_ms':0,'servo':55,'connected':True}
    monkeypatch.setattr(a,'telemetry',telemetry)
    commands=[]
    async def send(action='stop',speed=0,duration_ms=100,tilt=90):commands.append(action)
    monkeypatch.setattr(a,'send_control',send)
    return c,a.state['owner'],commands


def test_task_pre_capture_transient_and_invalid_planner_do_not_duplicate_actuator(task_context,monkeypatch):
    c,sid,commands=task_context;captures=[];inferences=[]
    raw=a.simulation_jpeg()
    async def device(path):
        captures.append(path)
        if len(captures)==1:raise httpx.ReadTimeout('transient')
        return httpx.Response(200,content=raw)
    async def infer(*args):
        a.state['heartbeat']=time.monotonic();inferences.append(1)
        if len(inferences)==1:raise ValueError('invalid reply')
        return decision()
    monkeypatch.setattr(a,'device',device);monkeypatch.setattr(a,'infer',infer)
    async def run():return await a.execute_turn(a.Turn(text='Jeď.',vision=True,source='car',intent='act'),sid,task_mode=True)
    result=c.portal.call(run)
    assert captures==['/capture','/capture'] and len(inferences)==2
    assert commands==['forward'] and result['executed_action']=='forward'


def test_task_pre_capture_stop_discards_frame_before_inference(task_context,monkeypatch):
    c,sid,commands=task_context;calls=[]
    async def capture():a.state['epoch']+=1;return 'unused'
    async def infer(*args):calls.append(1);return decision()
    monkeypatch.setattr(a,'task_capture',capture);monkeypatch.setattr(a,'infer',infer)
    async def run():
        try:await a.execute_turn(a.Turn(text='Jeď.',vision=True,source='car',intent='act'),sid,task_mode=True)
        except asyncio.CancelledError:return 'cancelled'
    assert c.portal.call(run)=='cancelled'
    assert not calls and not commands


def test_stop_during_replan_capture_prevents_second_inference_or_wheel(task_context,monkeypatch):
    c,sid,commands=task_context;captures=[];inferences=[]
    a.mission['recent']=[{'at':time.time(),'executed':'forward',
        'command':{'action':'forward','speed':120,'duration_ms':200,'tilt':90},
        'observed_at_execution':{'servo':55,'distance_cm':100,'telemetry_age_ms':0}}]
    image='data:image/jpeg;base64,'+__import__('base64').b64encode(a.simulation_jpeg()).decode()
    async def capture():
        captures.append(1)
        if len(captures)==2:a.state['epoch']+=1
        return image
    async def infer(*args):
        a.state['heartbeat']=time.monotonic();inferences.append(1);return decision()
    monkeypatch.setattr(a,'task_capture',capture);monkeypatch.setattr(a,'infer',infer)
    async def run():
        try:await a.execute_turn(a.Turn(text='Jeď.',vision=True,source='car',intent='act'),sid,task_mode=True)
        except asyncio.CancelledError:return 'cancelled'
    assert c.portal.call(run)=='cancelled'
    assert len(captures)==2 and len(inferences)==1 and commands==['stop']
