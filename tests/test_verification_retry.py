"""Read-only inference retries cannot replay an accepted motor command."""
import asyncio
from copy import deepcopy
import json
import threading

import pytest

from server.ai_provider import AIProviderError
from server.verification_retry import verification_retry
from test_route_integration import route_pipeline,wait_for
from test_simulator_isolation import simulator_client,operator_clock,a


def incomplete(reason='max_output_tokens'):
    error=AIProviderError('incomplete_response',502)
    error.incomplete_reason=reason
    return error


def test_read_only_retry_increases_only_incomplete_token_budget_and_is_bounded():
    async def scenario():
        budgets=[];events=[]
        async def operation(budget):
            budgets.append(budget)
            if len(budgets)<3:raise incomplete()
            return {'status':'continue'},{}
        result=await verification_retry(operation,lambda:True,backoff=0,on_attempt=lambda **row:events.append(row))
        assert result[0]['status']=='continue' and budgets==[1600,2400,3200]
        assert [row['error_category'] for row in events]==['incomplete_response','incomplete_response',None]
    asyncio.run(scenario())


@pytest.mark.parametrize('error',[
    AIProviderError('authentication_failed',401),AIProviderError('access_denied',403),
    AIProviderError('request_rejected',422),AIProviderError('refused_response',502),
    AIProviderError('insufficient_quota',429),
    incomplete('content_filter'),
])
def test_permanent_or_refused_inference_does_not_retry(error):
    async def scenario():
        calls=[]
        async def operation(budget):calls.append(budget);raise error
        with pytest.raises(AIProviderError) as caught:
            await verification_retry(operation,lambda:True,backoff=0)
        assert caught.value is error and calls==[1600]
    asyncio.run(scenario())


def test_schema_failures_exhaust_three_attempts_without_claiming_completed_or_logging_message():
    async def scenario():
        calls=[];events=[]
        async def operation(budget):
            calls.append(budget)
            raise ValueError('PRIVATE RESPONSE BODY AND CREDENTIALS')
        with pytest.raises(ValueError):
            await verification_retry(operation,lambda:True,backoff=0,on_attempt=lambda **row:events.append(row))
        assert calls==[1600]*3
        assert 'PRIVATE' not in json.dumps(events)
        assert all(row['error_category']=='invalid_structured_output' for row in events)
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel',['authorization','cancel_task'])
def test_retry_discards_late_provider_success_after_authorization_loss_or_cancellation(cancel):
    async def scenario():
        entered=asyncio.Event();active=[True];calls=[];late=[]
        async def operation(budget):
            calls.append(budget);entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                late.append(True)
                return {'status':'completed'},{}
        pending=asyncio.create_task(verification_retry(operation,lambda:active[0],backoff=0))
        await entered.wait()
        if cancel=='authorization':active[0]=False
        else:pending.cancel()
        with pytest.raises(asyncio.CancelledError):await pending
        assert calls==[1600] and late==[True]
    asyncio.run(scenario())


def test_total_retry_deadline_cancels_read_only_request():
    async def scenario():
        cancelled=[];calls=[]
        async def operation(budget):
            calls.append(budget)
            try:await asyncio.Event().wait()
            finally:cancelled.append(True)
        with pytest.raises(AIProviderError) as caught:
            await verification_retry(operation,lambda:True,timeout_s=.02,backoff=0)
        assert caught.value.category=='timeout' and calls==[1600] and cancelled==[True]
    asyncio.run(scenario())


def test_real_task_retries_only_verification_and_sends_one_motor_command(route_pipeline,monkeypatch):
    client,provider=route_pipeline
    a.task_runner.max_steps=1
    original=provider.infer;verification_calls=[];commands=[]
    async def flaky(messages,schema,**kwargs):
        if 'route_feedback' in schema.get('properties',{}):
            verification_calls.append((deepcopy(messages),kwargs.get('max_output_tokens')))
            if len(verification_calls)==1:raise incomplete()
            if len(verification_calls)==2:raise AIProviderError('invalid_structured_output',502)
        return await original(messages,schema,**kwargs)
    original_send=a.send_control
    async def send(action='stop',*args,**kwargs):
        commands.append(action);return await original_send(action,*args,**kwargs)
    monkeypatch.setattr(provider,'infer',flaky)
    monkeypatch.setattr(a,'send_control',send)
    assert client.post('/api/task',json={'goal':'Najdi průchod a dojeď k němu.'}).status_code==200
    task=wait_for(client,lambda task:task['status'] in ('blocked','completed','paused'))
    assert task['current_step']==1,task
    assert len(verification_calls)==3 and len(provider.plans)==1
    assert [action for action in commands if action in a.AI_WHEELS]==['rotate_left']
    assert all(messages==verification_calls[0][0] for messages,_ in verification_calls)
    assert verification_calls[0][1]==1600 and verification_calls[-1][1]==2400
    assert task['completed_steps'][0]['assessment']['status']=='continue'
    assert task['status']!='completed'


def test_real_task_does_not_replay_motor_after_permanent_verification_failure(route_pipeline,monkeypatch):
    client,provider=route_pipeline
    original=provider.infer;attempts=[];commands=[]
    async def denied(messages,schema,**kwargs):
        if 'route_feedback' in schema.get('properties',{}):
            attempts.append(True);raise AIProviderError('authentication_failed',401)
        return await original(messages,schema,**kwargs)
    original_send=a.send_control
    async def send(action='stop',*args,**kwargs):
        commands.append(action);return await original_send(action,*args,**kwargs)
    monkeypatch.setattr(provider,'infer',denied)
    monkeypatch.setattr(a,'send_control',send)
    assert client.post('/api/task',json={'goal':'Najdi průchod a dojeď k němu.'}).status_code==200
    task=wait_for(client,lambda task:task['status']=='blocked')
    assert attempts==[True] and task['current_step']==0 and task['last_result'] is None
    assert [action for action in commands if action in a.AI_WHEELS]==['rotate_left']
    assert task['route_memory']['routes']==[]


def test_stop_during_verification_retry_prevents_further_attempts_and_route_commit(route_pipeline,monkeypatch):
    client,provider=route_pipeline
    original=provider.infer;second_started=threading.Event();attempts=[];commands=[]
    async def unavailable(messages,schema,**kwargs):
        if 'route_feedback' in schema.get('properties',{}):
            attempts.append(True)
            if len(attempts)==1:raise AIProviderError('connection_failed',502)
            second_started.set()
            await asyncio.Event().wait()
        return await original(messages,schema,**kwargs)
    original_send=a.send_control
    async def send(action='stop',*args,**kwargs):
        commands.append(action);return await original_send(action,*args,**kwargs)
    monkeypatch.setattr(provider,'infer',unavailable)
    monkeypatch.setattr(a,'send_control',send)
    assert client.post('/api/task',json={'goal':'Najdi průchod a dojeď k němu.'}).status_code==200
    assert second_started.wait(2)
    assert client.post('/api/stop',json={}).status_code==200
    task=wait_for(client,lambda task:task['status']=='paused' and a.task_runner.worker.done())
    assert attempts==[True,True]
    assert task['current_step']==0 and task['route_memory']['routes']==[]
    assert [action for action in commands if action in a.AI_WHEELS]==['rotate_left']
