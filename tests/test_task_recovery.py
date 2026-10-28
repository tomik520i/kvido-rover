"""Server recovery races, with mocked transport only: never touches hardware."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from test_safety import a
from server.task_runner import TaskRunner
from server.ai_provider import AIProviderError
from server.diagnostics import DiagnosticTimeline


class ConnectedRelay:
    def status(self):return {'connected':True}


@pytest.mark.parametrize('kind,expected_type,expected_category,expected_status',[
    ('provider','AIProviderError','invalid_structured_output',502),
    ('schema','ValueError',None,None),
    ('http','HTTPStatusError',None,503),
    ('http_exception','HTTPException',None,409),
    ('unknown_category','AIProviderError',None,502),
    ('foreign_category','ValueError',None,None),
    ('boolean_status','ValueError',None,None),
])
def test_task_error_diagnostics_log_only_safe_failure_metadata(monkeypatch,tmp_path,
        kind,expected_type,expected_category,expected_status):
    secret='SENSITIVE-UNREGISTERED-BODY-DO-NOT-LOG'
    if kind in ('provider','unknown_category'):
        error=AIProviderError('invalid_structured_output' if kind=='provider' else secret,502)
        error.args=(secret,)
    elif kind=='http':
        request=httpx.Request('POST','https://private.invalid/'+secret,headers={'Authorization':secret})
        error=httpx.HTTPStatusError(secret,request=request,response=httpx.Response(503,request=request,text=secret))
    elif kind=='http_exception':error=HTTPException(409,secret)
    else:error=ValueError(secret)
    if kind=='foreign_category':error.category='timeout'
    if kind=='boolean_status':error.status_code=True
    timeline=DiagnosticTimeline(tmp_path/'error-diagnostics')
    async def scenario():
        setup_recovery(monkeypatch,tmp_path)
        a.settings['transport']='lan'
        runner=TaskRunner(tmp_path/'task.json',a.task_step,lambda sid:True,a.halt)
        runner.data.update(id='task-safe',status='running',phase='verifying',current_step=31)
        monkeypatch.setattr(a,'task_runner',runner)
        monkeypatch.setattr(a,'diagnostics',timeline)
        async def failing_step(*args):raise error
        monkeypatch.setattr(a,'execute_task_step',failing_step)
        prior={'task_id':'outer'};token=a.task_trace.set(prior)
        try:
            with pytest.raises(type(error)) as caught:
                await a.task_step('operator','Goal',None,lambda phase:None)
            assert caught.value is error
            assert a.task_trace.get() is prior
        finally:a.task_trace.reset(token)
    try:asyncio.run(scenario())
    finally:timeline.close(2)
    text=timeline.path.read_text(encoding='utf-8')
    assert secret not in text and 'private.invalid' not in text and 'Authorization' not in text
    rows=[json.loads(line) for line in text.splitlines()]
    assert len(rows)==1
    row=rows[0]
    assert row['event']=='task_error' and row['phase']=='verifying'
    assert row['task_id']=='task-safe' and row['step']==32
    assert row['error_type']==expected_type and row['error_category']==expected_category
    if expected_status is None:assert 'status_code' not in row
    else:assert row['status_code']==expected_status and type(row['status_code']) is int


def test_cancelled_task_does_not_emit_task_error(monkeypatch,tmp_path):
    async def scenario():
        setup_recovery(monkeypatch,tmp_path)
        events=[]
        monkeypatch.setattr(a,'task_runner',None)
        monkeypatch.setattr(a,'diagnostic_event',lambda kind,**fields:events.append((kind,fields)))
        async def cancelled(*args):raise asyncio.CancelledError()
        monkeypatch.setattr(a,'execute_task_step',cancelled)
        with pytest.raises(asyncio.CancelledError):
            await a.task_step('operator','Goal',None,lambda phase:None)
        assert not events
    asyncio.run(scenario())


@pytest.mark.parametrize('error',[
    HTTPException(502,'Disconnected'),HTTPException(503,'Unavailable'),HTTPException(504,'Timeout'),
    httpx.ReadTimeout('No acknowledgement'),httpx.ConnectError('Disconnected'),
])
def test_task_step_transient_relay_failure_enters_recovery_and_restores_trace(monkeypatch,tmp_path,error):
    async def scenario():
        setup_recovery(monkeypatch,tmp_path)
        a.state.update(armed=True,owner='operator',estop=False,heartbeat=time.monotonic())
        a.mission['status']='running'
        runner=TaskRunner(tmp_path/'task.json',a.task_step,lambda sid:True,a.halt)
        runner.data.update(id='task-123',status='running',current_step=2,goal='Persistent goal')
        monkeypatch.setattr(a,'task_runner',runner)
        seen=[]
        async def failing_step(*args):
            seen.append(a.task_trace.get())
            raise error
        monkeypatch.setattr(a,'execute_task_step',failing_step)
        prior={'task_id':'outer','step':9};token=a.task_trace.set(prior)
        try:
            with pytest.raises(asyncio.CancelledError):
                await a.task_step('operator','Persistent goal',None,lambda phase:None)
            assert a.task_trace.get() is prior
        finally:a.task_trace.reset(token)
        assert seen==[{'task_id':'task-123','step':3}]
        assert runner.snapshot()['status']=='reconnecting'
        assert runner.snapshot()['goal']=='Persistent goal'
        assert a.mission['status']=='paused'
        assert a.state['armed'] is False and a.state['estop'] is True
        assert a.state['epoch']==11
    asyncio.run(scenario())


@pytest.mark.parametrize('transport',['relay','lan'])
def test_task_step_success_returns_body_result_without_recovery(monkeypatch,tmp_path,transport):
    async def scenario():
        setup_recovery(monkeypatch,tmp_path)
        a.settings['transport']=transport
        runner=TaskRunner(tmp_path/'task.json',a.task_step,lambda sid:True,a.halt)
        runner.data.update(id='task-123',status='running',current_step=2)
        monkeypatch.setattr(a,'task_runner',runner)
        previous={'assessment':{'status':'continue'}};phases=[];expected={'action':'stop'}
        async def successful_step(sid,goal,prior,set_phase):
            assert (sid,goal,prior)==('operator','Goal',previous)
            assert a.task_trace.get()=={'task_id':'task-123','step':3}
            set_phase('observing_after')
            return expected
        def unexpected_recovery(*args):pytest.fail('Successful step must not enter recovery')
        monkeypatch.setattr(a,'execute_task_step',successful_step)
        monkeypatch.setattr(a,'relay_link_change',unexpected_recovery)
        prior={'task_id':'outer'};token=a.task_trace.set(prior)
        try:
            result=await a.task_step('operator','Goal',previous,phases.append)
            assert result is expected
            assert a.task_trace.get() is prior
        finally:a.task_trace.reset(token)
        assert phases==['observing_after'] and runner.data['status']=='running'
    asyncio.run(scenario())


@pytest.mark.parametrize('transport,status,error',[
    ('lan','running',HTTPException(502,'LAN failure')),
    ('relay','paused',HTTPException(503,'Paused')),
    ('relay','running',HTTPException(409,'Control inactive')),
])
def test_task_step_other_errors_propagate_without_recovery(monkeypatch,tmp_path,transport,status,error):
    async def scenario():
        setup_recovery(monkeypatch,tmp_path)
        a.settings['transport']=transport
        runner=TaskRunner(tmp_path/'task.json',a.task_step,lambda sid:True,a.halt)
        runner.data['status']=status
        monkeypatch.setattr(a,'task_runner',runner)
        async def failing_step(*args):raise error
        def unexpected_recovery(*args):pytest.fail('This failure must not enter relay recovery')
        monkeypatch.setattr(a,'execute_task_step',failing_step)
        monkeypatch.setattr(a,'relay_link_change',unexpected_recovery)
        with pytest.raises(HTTPException) as caught:
            await a.task_step('operator','Goal',None,lambda phase:None)
        assert caught.value is error
    asyncio.run(scenario())


def setup_recovery(monkeypatch,tmp_path):
    monkeypatch.setattr(a,'state',{**a.state,'mode':'ai','armed':False,'estop':True,'owner':None,'epoch':10})
    monkeypatch.setattr(a,'settings',{**a.settings,'transport':'relay','simulation':False})
    monkeypatch.setattr(a,'mission',{**a.mission,'status':'paused','goal':'Persistent goal','recent':[]})
    monkeypatch.setattr(a,'MISSION_PATH',tmp_path/'mission.json')
    monkeypatch.setattr(a,'motion_lock',asyncio.Lock())
    monkeypatch.setattr(a,'relay',ConnectedRelay())
    monkeypatch.setattr(a,'sessions',{'operator':time.time()+100})
    monkeypatch.setattr(a,'recovery_attempt_at',0)
    monkeypatch.setattr(a,'emit',lambda *args,**kw:None)
    async def telemetry():return {'distance_cm':100,'telemetry_age_ms':10,'connected':True}
    monkeypatch.setattr(a,'planning_telemetry',telemetry)
    async def capture():return 'mock recovered frame'
    monkeypatch.setattr(a,'task_capture',capture)


async def recovering_runner(monkeypatch,tmp_path):
    entered=asyncio.Event()
    async def step(*args):entered.set();await asyncio.Event().wait()
    runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,a.halt,interval=0)
    monkeypatch.setattr(a,'task_runner',runner)
    runner.start('operator','Persistent goal');await entered.wait();runner.recover('Disconnected')
    await runner.worker
    return runner


def test_recovery_exhausted_budget_never_releases_or_escapes_watchdog(monkeypatch,tmp_path):
    async def scenario():
        setup_recovery(monkeypatch,tmp_path);sent=[]
        async def send(action='stop',*args,**kwargs):sent.append(action)
        monkeypatch.setattr(a,'send_control',send)
        runner=await recovering_runner(monkeypatch,tmp_path)
        runner.data['elapsed_s']=runner.max_seconds+1
        await a.restore_task_connection()
        assert 'release' not in sent
        assert a.state['armed'] is False
        assert runner.snapshot()['status'] in ('blocked','paused')
    asyncio.run(scenario())


def test_stop_during_recovery_release_prevents_rearm_and_resume(monkeypatch,tmp_path):
    async def scenario():
        setup_recovery(monkeypatch,tmp_path)
        release_started=asyncio.Event();release_return=asyncio.Event();sent=[]
        async def send(action='stop',*args,**kwargs):
            sent.append(action)
            if action=='release':release_started.set();await release_return.wait()
        monkeypatch.setattr(a,'send_control',send)
        runner=await recovering_runner(monkeypatch,tmp_path)
        recovery=asyncio.create_task(a.restore_task_connection());await asyncio.wait_for(release_started.wait(),1)
        stopping=asyncio.create_task(a.halt('User STOP',emergency=True));await asyncio.sleep(0)
        release_return.set();await asyncio.wait_for(asyncio.gather(recovery,stopping),1)
        assert a.state['armed'] is False and a.state['owner'] is None
        assert runner.snapshot()['status']=='paused'
        assert runner.snapshot()['goal']=='Persistent goal'
        assert sent==['release','estop']
        assert runner.can_recover() is False
    asyncio.run(scenario())


def test_recovery_waits_for_cancelled_step_to_finish(monkeypatch,tmp_path):
    async def scenario():
        setup_recovery(monkeypatch,tmp_path);entered=asyncio.Event();finish_old=asyncio.Event();sent=[]
        async def step(*args):
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:await finish_old.wait();return {}
        async def send(action='stop',*args,**kwargs):sent.append(action)
        monkeypatch.setattr(a,'send_control',send)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,a.halt,interval=0)
        monkeypatch.setattr(a,'task_runner',runner)
        runner.start('operator','Persistent goal');await entered.wait();runner.recover('Disconnected')
        await asyncio.sleep(0)
        await a.restore_task_connection()
        assert sent==[] and runner.snapshot()['status']=='reconnecting'
        finish_old.set();await runner.worker
        runner.interrupt(reason='Test cleanup')
    asyncio.run(scenario())

def test_unknown_ack_pending_action_is_observed_without_motor_replay(monkeypatch,tmp_path):
    async def scenario():
        import base64
        setup_recovery(monkeypatch,tmp_path)
        a.state.update(armed=True,owner='operator',heartbeat=time.monotonic(),estop=False)
        monkeypatch.setattr(a,'vision_history',type(a.vision_history)())
        before='data:image/jpeg;base64,'+base64.b64encode(a.simulation_jpeg()).decode()
        pending={'_before_image':before,'_before_telemetry':{'servo':55},
                 '_command':{'action':'rotate_left','speed':80,'duration_ms':0,'tilt':90},
                 'executed_action':'rotate_left','say':'Checking prior action',
                 'execution_reason':'Unknown acknowledgement','acceptance':'unknown'}
        monkeypatch.setattr(a,'task_pending',pending)
        sent=[];assessed=[]
        async def send(action='stop',*args,**kw):sent.append(action)
        async def capture():return before
        async def execute(*args,**kw):raise AssertionError('Pending motor command must not be planned or replayed')
        async def assess(goal,before,after,command,*args,**kwargs):
            assessed.append(command);return {'status':'continue','progress':'changed','summary':'Observed','evidence':'Fresh view','next_hint':''},{}
        monkeypatch.setattr(a,'send_control',send);monkeypatch.setattr(a,'task_capture',capture)
        monkeypatch.setattr(a,'execute_turn',execute);monkeypatch.setattr(a,'assess_step',assess)
        out=await a.execute_task_step('operator','Persistent goal',None,lambda phase:None)
        assert sent==['stop']
        assert out['acceptance']=='unknown' and assessed[0]['acceptance']=='unknown'
        assert out['before_image']==before and out['after_image']==before
        assert a.task_pending is None
    asyncio.run(scenario())


def test_stop_during_after_assessment_preserves_pending_and_discards_late_result(monkeypatch,tmp_path):
    async def scenario():
        import base64
        import pytest
        from fastapi import HTTPException
        setup_recovery(monkeypatch,tmp_path)
        a.state.update(armed=True,owner='operator',heartbeat=time.monotonic(),estop=False)
        monkeypatch.setattr(a,'task_runner',None)
        monkeypatch.setattr(a,'vision_history',type(a.vision_history)())
        image='data:image/jpeg;base64,'+base64.b64encode(a.simulation_jpeg()).decode()
        pending={'_before_image':image,'_before_telemetry':{'servo':55},
                 '_command':{'action':'rotate_left','speed':80,'duration_ms':0,'tilt':90},
                 'executed_action':'rotate_left','say':'Checking','execution_reason':'Accepted','acceptance':'accepted'}
        monkeypatch.setattr(a,'task_pending',pending)
        entered=asyncio.Event();release=asyncio.Event();sent=[]
        async def send(action='stop',*args,**kw):sent.append(action)
        async def capture():return image
        async def assess(*args,**kwargs):
            entered.set();await release.wait()
            return {'status':'completed','progress':'changed','summary':'Done','evidence':'View','next_hint':''},{}
        monkeypatch.setattr(a,'send_control',send);monkeypatch.setattr(a,'task_capture',capture)
        monkeypatch.setattr(a,'assess_step',assess)
        operation=asyncio.create_task(a.execute_task_step('operator','Goal',None,lambda phase:None))
        await asyncio.wait_for(entered.wait(),1)
        await a.halt('Manual STOP',emergency=True);release.set()
        with pytest.raises((HTTPException,asyncio.CancelledError)):await operation
        assert sent==['stop','estop']
        assert a.task_pending is pending
        assert a.state['armed'] is False
    asyncio.run(scenario())
