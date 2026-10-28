import asyncio
from functools import partial
import time

import httpx
import pytest
from test_safety import a, c, decision


@pytest.mark.parametrize('sequence,expected,calls', [
    ([675,320],320,2),
    ([700]*11,700,None),
    ([0],0,1),
])
def test_waits_for_real_new_sample_without_changing_age(c,monkeypatch,sequence,expected,calls):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    count=[]
    async def telemetry():
        age=sequence[len(count)];count.append(age)
        return {'connected':True,'distance_cm':84,'telemetry_age_ms':age}
    monkeypatch.setattr(a,'telemetry',telemetry)
    result=c.portal.call(a.planning_telemetry)
    assert result['telemetry_age_ms']==expected
    if calls is None:
        assert 2<=len(count)<=11  # Scheduler overhead must not extend the deadline.
    else:
        assert len(count)==calls


def test_disconnected_sample_does_not_wait(c,monkeypatch):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    async def telemetry():
        return {'connected':False,'telemetry_age_ms':999999}
    monkeypatch.setattr(a,'telemetry',telemetry)
    assert c.portal.call(a.planning_telemetry)['connected'] is False


def test_epoch_change_aborts_wait(c,monkeypatch):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    count=[]
    async def telemetry():
        count.append(True)
        a.state['epoch']+=1
        return {'connected':True,'telemetry_age_ms':675}
    monkeypatch.setattr(a,'telemetry',telemetry)
    assert c.portal.call(a.planning_telemetry)['telemetry_age_ms']==675
    assert len(count)==1


def test_fresh_invalid_range_waits_for_new_valid_sample(c,monkeypatch):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    samples=[{'connected':True,'distance_cm':None,'telemetry_age_ms':100},
             {'connected':True,'distance_cm':102,'telemetry_age_ms':170}]
    seen=[]
    async def telemetry():
        sample=samples[len(seen)];seen.append(sample)
        return sample
    monkeypatch.setattr(a,'telemetry',telemetry)
    result=c.portal.call(partial(a.planning_telemetry,require_distance=True))
    assert result is samples[1] and result['telemetry_age_ms']==170
    assert len(seen)==2


def test_stale_valid_then_fresh_null_times_out_with_latest_invalid_sample(c,monkeypatch):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    stale={'connected':True,'distance_cm':102,'telemetry_age_ms':800}
    latest={'connected':True,'distance_cm':None,'telemetry_age_ms':100}
    seen=[]
    async def telemetry():
        sample=stale if not seen else latest
        seen.append(sample);return sample
    monkeypatch.setattr(a,'telemetry',telemetry)
    started=time.monotonic()
    result=c.portal.call(partial(a.planning_telemetry,require_distance=True))
    assert result is latest and result['distance_cm'] is None
    assert result['telemetry_age_ms']==100 and 2<=len(seen)<=11
    assert time.monotonic()-started<1


@pytest.mark.parametrize('distance',[2,20,21,25])
def test_valid_close_range_returns_immediately_without_looking_for_clearance(c,monkeypatch,distance):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    seen=[]
    async def telemetry():
        seen.append(True)
        assert len(seen)==1,'A nearby valid obstacle must not be waited away'
        return {'connected':True,'distance_cm':distance,'telemetry_age_ms':100}
    monkeypatch.setattr(a,'telemetry',telemetry)
    result=c.portal.call(partial(a.planning_telemetry,require_distance=True))
    assert result['distance_cm']==distance and len(seen)==1


def test_stop_epoch_change_interrupts_range_wait(c,monkeypatch):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    seen=[];commands=[]
    async def send_control(action='stop',*args,**kwargs):commands.append(action)
    async def telemetry():
        seen.append(True)
        if len(seen)==2:await a.halt('Test STOP',emergency=True)
        return {'connected':True,'distance_cm':None,'telemetry_age_ms':100}
    monkeypatch.setattr(a,'send_control',send_control)
    monkeypatch.setattr(a,'telemetry',telemetry)
    epoch=a.state['epoch']
    result=c.portal.call(partial(a.planning_telemetry,require_distance=True))
    assert len(seen)==2 and result['distance_cm'] is None
    assert a.state['epoch']>epoch and a.state['estop'] and commands==['estop']


def test_range_wait_remains_cancellable(c,monkeypatch):
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    async def scenario():
        entered=asyncio.Event()
        async def telemetry():
            entered.set()
            return {'connected':True,'distance_cm':None,'telemetry_age_ms':100}
        monkeypatch.setattr(a,'telemetry',telemetry)
        task=asyncio.create_task(a.planning_telemetry(require_distance=True))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    c.portal.call(scenario)


@pytest.mark.parametrize('current,expected',[
    ({'distance_cm':None,'telemetry_age_ms':100},'stop'),
    ({'distance_cm':10,'telemetry_age_ms':100},'stop'),
    ({'distance_cm':102,'telemetry_age_ms':700},'stop'),
    ({'distance_cm':102,'telemetry_age_ms':100},'forward'),
])
def test_planning_wait_never_replaces_latest_execution_guard_sample(c,monkeypatch,current,expected):
    assert c.post('/api/mode',json={'mode':'ai'}).status_code==200
    assert c.post('/api/arm',json={}).status_code==200
    raw=a.simulation_jpeg()
    monkeypatch.setitem(a.settings,'transport','relay')
    monkeypatch.setitem(a.settings,'simulation',False)
    monkeypatch.setitem(a.settings,'navigation_policy','sensor')
    samples=[{'distance_cm':None,'telemetry_age_ms':100},
             {'distance_cm':102,'telemetry_age_ms':170},
             {'distance_cm':102,'telemetry_age_ms':180},current]
    seen=[];commands=[];inferred=[]
    async def telemetry():
        sample={**samples[min(len(seen),3)],'connected':True,'servo':90}
        seen.append(sample);return sample
    async def device(path,*args,**kwargs):
        assert path=='/capture'
        return httpx.Response(200,content=raw)
    async def send_control(action='stop',*args,**kwargs):commands.append(action)
    async def infer(messages,schema=None):
        inferred.append(True)
        assert len(seen)==2
        assert 'forward' in schema['properties']['action']['enum']
        return decision(action='forward',hazard='unknown',traversable=False,confidence=.1)
    monkeypatch.setattr(a,'telemetry',telemetry)
    monkeypatch.setattr(a,'device',device)
    monkeypatch.setattr(a,'send_control',send_control)
    monkeypatch.setattr(a,'infer',infer)
    response=c.post('/api/turn',json={'intent':'act','source':'car'})
    assert response.status_code==200,response.text
    assert len(inferred)==1 and len(seen)==4
    assert response.json()['executed_action']==expected and commands==[expected]
