"""Unit tests for bounded task orchestration; no device or network access."""
import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from server.task_runner import TaskRunner


def image(color):
    out=io.BytesIO();Image.new('RGB',(8,8),color).save(out,'JPEG')
    return 'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()


BEFORE=image('red')
AFTER=image('blue')


def result(status='continue', **overrides):
    data={'action':'rotate_left',
          'command':{'action':'rotate_left','speed':80,'duration_ms':100,'tilt':90},
          'assessment':{'status':status,'summary':'Compared the labelled frames.',
                        'progress':'A new part of the room is visible.',
                        'evidence':'Before and after show different views.'},
          'before_image':BEFORE,'after_image':AFTER}
    data.update(overrides);return data


async def finish(worker):
    try:await asyncio.wait_for(asyncio.shield(worker),2)
    except asyncio.CancelledError:pass


def test_runner_continues_with_persistent_explicit_goal_and_previous_result(tmp_path):
    async def scenario():
        seen=[];halts=[]
        async def step(sid,goal,previous,set_phase):
            seen.append((sid,goal,previous));set_phase('assessing')
            return result('completed' if len(seen)==2 else 'continue')
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Find the blue cup');await finish(runner.worker)
        state=runner.snapshot()
        assert state['status']=='completed'
        assert [entry[:2] for entry in seen]==[('operator','Find the blue cup')]*2
        assert seen[0][2] is None
        assert seen[1][2]['assessment']['status']=='continue'
        assert state['goal']=='Find the blue cup'
        assert halts, 'Completion must leave the physical controller stopped'
    asyncio.run(scenario())


def test_completion_waits_for_after_image_assessment(tmp_path):
    async def scenario():
        assessing=asyncio.Event();release=asyncio.Event();halts=[]
        async def step(sid,goal,previous,set_phase):
            set_phase('assessing');assessing.set();await release.wait()
            return result('completed')
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Find cup');await assessing.wait()
        assert runner.snapshot()['status']!='completed'
        release.set();await finish(runner.worker)
        assert runner.snapshot()['status']=='completed'
    asyncio.run(scenario())


def test_missing_after_image_never_completes_or_replays_accepted_command(tmp_path):
    async def scenario():
        calls=[];halts=[]
        async def step(*args):calls.append('accepted once');return result('completed',after_image=None)
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Find cup');await finish(runner.worker)
        assert runner.snapshot()['status']=='blocked'
        assert calls==['accepted once'] and halts
        assert runner.snapshot()['goal']=='Find cup'
    asyncio.run(scenario())


def test_stop_discards_late_inference_even_when_callback_suppresses_cancellation(tmp_path):
    async def scenario():
        started=asyncio.Event();calls=[];halts=[]
        async def step(sid,goal,previous,set_phase):
            calls.append(goal);set_phase('thinking');started.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                set_phase('executing')  # Stale progress must also be ignored.
                return result('completed')
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Keep this goal');await started.wait();worker=runner.worker
        runner.interrupt(status='paused',reason='User STOP');await finish(worker)
        state=runner.snapshot()
        assert state['status']=='paused' and state['reason']=='User STOP'
        assert state['phase']!='executing'
        assert state['goal']=='Keep this goal'
        assert state.get('last_result') is None
        assert calls==['Keep this goal']
    asyncio.run(scenario())


def test_authority_loss_pauses_without_starting_another_step(tmp_path):
    async def scenario():
        authorized=True;calls=[];halts=[]
        async def step(*args):
            nonlocal authorized
            calls.append(1);authorized=False;return result()
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:authorized,halt,interval=0)
        runner.start('operator','Saved goal');await finish(runner.worker)
        assert calls==[1]
        assert runner.snapshot()['status']=='paused'
        assert runner.snapshot()['goal']=='Saved goal'
        assert halts
    asyncio.run(scenario())


def test_step_limit_bounds_continuous_work(tmp_path):
    async def scenario():
        calls=[];halts=[]
        async def step(*args):calls.append(1);return result()
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,max_steps=2,interval=0)
        runner.start('operator','Explore');await finish(runner.worker)
        assert len(calls)==2
        assert runner.snapshot()['status'] in ('blocked','paused')
        assert runner.snapshot()['goal']=='Explore' and halts
    asyncio.run(scenario())


def test_elapsed_limit_prevents_a_second_action(tmp_path):
    async def scenario():
        calls=[];halts=[]
        async def step(*args):calls.append(1);await asyncio.sleep(.03);return result()
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,max_seconds=.01,interval=0)
        runner.start('operator','Explore');await finish(runner.worker)
        assert len(calls)<=1
        assert runner.snapshot()['status'] in ('blocked','paused')
        assert halts
    asyncio.run(scenario())


def test_step_exception_blocks_and_never_retries_unknown_acceptance(tmp_path):
    async def scenario():
        calls=[];halts=[]
        async def step(*args):calls.append('sent');raise TimeoutError('Acknowledgement unknown')
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Find cup');await finish(runner.worker)
        assert calls==['sent']
        assert runner.snapshot()['status']=='blocked' and halts
        assert runner.snapshot()['goal']=='Find cup'
    asyncio.run(scenario())


def test_frame_roles_are_distinct_and_images_are_not_persisted(tmp_path):
    async def scenario():
        path=tmp_path/'task.json'
        async def step(*args):return result('completed')
        async def halt(reason):pass
        runner=TaskRunner(path,step,lambda sid:True,halt,interval=0)
        runner.start('operator','Find cup');await finish(runner.worker)
        assert runner.frame('before')==base64.b64decode(BEFORE.split(',',1)[1])
        assert runner.frame('after')==base64.b64decode(AFTER.split(',',1)[1])
        assert runner.frame('before')!=runner.frame('after')
        persisted=path.read_text(encoding='utf-8')
        assert 'Find cup' in persisted
        assert BEFORE not in persisted and AFTER not in persisted
        assert 'data:image' not in persisted
        json.dumps(runner.snapshot())  # Public state is JSON serializable.
    asyncio.run(scenario())


def test_restart_preserves_goal_but_never_auto_resumes(tmp_path):
    async def scenario():
        path=tmp_path/'task.json';calls=[];started=asyncio.Event()
        async def step(*args):
            calls.append(1);started.set();await asyncio.Event().wait()
        async def halt(reason):pass
        runner=TaskRunner(path,step,lambda sid:True,halt,interval=0)
        runner.start('operator','Persistent explicit goal');await started.wait()
        restarted=TaskRunner(path,step,lambda sid:True,halt,interval=0)
        assert restarted.snapshot()['goal']=='Persistent explicit goal'
        assert restarted.snapshot()['status']=='paused'
        assert restarted.frame('before') is None and restarted.frame('after') is None
        await asyncio.sleep(0)
        assert calls==[1]
        runner.interrupt(reason='Test cleanup');await finish(runner.worker)
    asyncio.run(scenario())


def test_concurrent_start_is_rejected_instead_of_duplicating_actions(tmp_path):
    async def scenario():
        started=asyncio.Event();calls=[]
        async def step(*args):calls.append(1);started.set();await asyncio.Event().wait()
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','First goal');await started.wait()
        with pytest.raises(ValueError):runner.start('operator','Second goal')
        assert runner.snapshot()['goal']=='First goal' and calls==[1]
        runner.interrupt(reason='Test cleanup');await finish(runner.worker)
    asyncio.run(scenario())


def test_blocked_assessment_halts_without_following_step(tmp_path):
    async def scenario():
        calls=[];halts=[]
        async def step(*args):calls.append(1);return result('blocked')
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Explicit goal');await finish(runner.worker)
        assert calls==[1] and halts
        assert runner.snapshot()['status']=='blocked'
        assert runner.snapshot()['last_result']['has_before'] is True
        assert runner.snapshot()['last_result']['has_after'] is True
        assert runner.snapshot()['last_result']['step']==1
    asyncio.run(scenario())


def test_resume_cannot_reset_exhausted_step_budget(tmp_path):
    async def scenario():
        calls=[]
        async def step(*args):calls.append(1);return result()
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,max_steps=1,interval=0)
        runner.start('operator','Original goal');await finish(runner.worker)
        with pytest.raises(ValueError):runner.start('operator','Original goal',resume=True)
        assert calls==[1] and runner.snapshot()['goal']=='Original goal'
    asyncio.run(scenario())

def test_recovery_retains_goal_identity_budget_and_resumes_only_after_new_authority(tmp_path):
    async def scenario():
        entered=asyncio.Event();calls=[];halts=[]
        async def step(sid,goal,previous,set_phase):
            calls.append((sid,goal,previous))
            if len(calls)==2:entered.set();await asyncio.Event().wait()
            return result('completed' if len(calls)==3 else 'continue')
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Same explicit goal');await entered.wait()
        before=runner.snapshot();worker=runner.worker
        assert runner.recover('Link lost') is True
        await finish(worker)
        state=runner.snapshot()
        assert state['status']=='reconnecting' and state['phase']=='reconnecting'
        assert state['id']==before['id'] and state['current_step']==1
        assert state['goal']=='Same explicit goal' and state['elapsed_s']>=before['elapsed_s']
        assert runner.owner=='operator' and runner.can_recover() is True
        assert halts==[], 'Recovery relies on the firmware stop and must not replay a command'
        assert runner.operator_heartbeat('other operator') is False
        assert runner.operator_heartbeat('operator') is True
        runner.start(runner.owner,state['goal'],resume=True);await finish(runner.worker)
        assert runner.snapshot()['status']=='completed'
        assert runner.snapshot()['id']==before['id'] and runner.snapshot()['current_step']==2
        assert calls[-1][2]['assessment']['status']=='continue'
    asyncio.run(scenario())


def test_stop_during_recovery_is_terminal_until_explicit_new_start(tmp_path):
    async def scenario():
        entered=asyncio.Event()
        async def step(*args):entered.set();await asyncio.Event().wait()
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Keep goal');await entered.wait()
        runner.recover('Link lost');await finish(runner.worker)
        runner.interrupt(status='paused',reason='Manual STOP')
        assert runner.can_recover() is False
        assert runner.recover('Link returned') is False
        assert runner.operator_heartbeat('operator') is False
        assert runner.snapshot()['status']=='paused' and runner.snapshot()['goal']=='Keep goal'
    asyncio.run(scenario())


def test_recovery_grace_does_not_extend_on_repeated_link_events(tmp_path):
    async def scenario():
        entered=asyncio.Event()
        async def step(*args):entered.set();await asyncio.Event().wait()
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Goal');await entered.wait();runner.recover('First loss')
        await finish(runner.worker)
        original=runner.reconnect_started
        runner.recover('Still lost')
        assert runner.reconnect_started==original
        runner.reconnect_started-=121
        assert runner.can_recover() is False
        assert runner.operator_heartbeat('operator') is True
        assert runner.can_recover() is False, 'Heartbeat cannot extend the 120 second grace'
        runner.interrupt(reason='Grace expired')
    asyncio.run(scenario())


def test_expired_operator_heartbeat_cannot_resurrect_recovery(tmp_path):
    async def scenario():
        entered=asyncio.Event()
        async def step(*args):entered.set();await asyncio.Event().wait()
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Goal');await entered.wait();runner.recover('Link lost')
        await finish(runner.worker)
        runner._operator_at-=2
        assert runner.can_recover() is False
        assert runner.operator_heartbeat('operator') is False
        assert runner.can_recover() is False
        runner.interrupt(reason='Operator disconnected')
    asyncio.run(scenario())


def test_reconnecting_state_restarts_paused_without_saved_operator(tmp_path):
    async def scenario():
        path=tmp_path/'task.json';entered=asyncio.Event()
        async def step(*args):entered.set();await asyncio.Event().wait()
        async def halt(reason):pass
        runner=TaskRunner(path,step,lambda sid:True,halt,interval=0)
        runner.start('operator','Saved goal');await entered.wait();runner.recover('Link lost')
        await finish(runner.worker)
        restarted=TaskRunner(path,step,lambda sid:True,halt,interval=0)
        assert restarted.snapshot()['status']=='paused'
        assert restarted.snapshot()['goal']=='Saved goal'
        assert restarted.owner is None and restarted.can_recover() is False
    asyncio.run(scenario())


def test_inference_itself_is_bounded_and_timeout_stops_without_retry(tmp_path):
    async def scenario():
        calls=[];halts=[]
        async def step(*args):calls.append(1);await asyncio.Event().wait()
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,max_seconds=.02,interval=0)
        runner.start('operator','Goal');await finish(runner.worker)
        assert calls==[1] and halts
        assert runner.snapshot()['status']=='blocked'
        assert runner.snapshot()['goal']=='Goal'
    asyncio.run(scenario())


def test_interrupt_persists_partial_elapsed_time(tmp_path):
    async def scenario():
        entered=asyncio.Event()
        async def step(*args):entered.set();await asyncio.Event().wait()
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Goal');await entered.wait();await asyncio.sleep(.025)
        runner.interrupt(reason='STOP');await finish(runner.worker)
        assert runner.snapshot()['elapsed_s']>=.02
        saved=json.loads((tmp_path/'task.json').read_text(encoding='utf-8'))
        assert saved['elapsed_s']>=.02
    asyncio.run(scenario())

@pytest.mark.parametrize('distance,sensor_reason',[(None,True),(True,True),(401,True),('289',True),(2,False),(289,False),(400,False)])
def test_three_stop_steps_without_progress_block_with_specific_sensor_reason(tmp_path,distance,sensor_reason):
    async def scenario():
        calls=[];halts=[]
        async def step(*args):
            calls.append(1)
            item=result(action='stop',command={'action':'stop'},observed_after={'distance_cm':distance})
            item['assessment'].update(status='continue',progress='unchanged' if len(calls)%2 else 'unknown')
            return item
        async def halt(reason):halts.append(reason)
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Still unfinished');await finish(runner.worker)
        state=runner.snapshot()
        assert len(calls)==3 and state['current_step']==3
        assert state['status']=='blocked' and state['goal']=='Still unfinished'
        assert ('Čidlo' in state['reason']) is sensor_reason
        assert halts==[state['reason']]
    asyncio.run(scenario())


def test_two_stops_then_forward_resets_no_progress_streak(tmp_path):
    async def scenario():
        actions=['stop','stop','forward','stop','stop','forward'];seen=[]
        async def step(*args):
            action=actions[len(seen)];seen.append(action)
            item=result(action=action,command={'action':action},observed_after={'distance_cm':100})
            item['assessment'].update(status='completed' if len(seen)==len(actions) else 'continue',
                                      progress='unchanged' if action=='stop' else 'changed')
            return item
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Goal');await finish(runner.worker)
        assert seen==actions and runner.snapshot()['status']=='completed'
    asyncio.run(scenario())


@pytest.mark.parametrize('verdict',['completed','blocked'])
def test_terminal_stop_assessment_is_not_overridden_by_three_stop_guard(tmp_path,verdict):
    async def scenario():
        calls=[]
        async def step(*args):
            calls.append(1);item=result(action='stop',command={'action':'stop'},observed_after={'distance_cm':None})
            item['assessment'].update(status=verdict if len(calls)==3 else 'continue',progress='unknown',summary='Original assessment')
            return item
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Goal');await finish(runner.worker)
        assert calls==[1,1,1]
        assert runner.snapshot()['status']==verdict and runner.snapshot()['reason']=='Original assessment'
    asyncio.run(scenario())


@pytest.mark.parametrize('interlude',['tilt','light_on','light_off'])
def test_camera_and_light_steps_do_not_count_as_stop_streak(tmp_path,interlude):
    async def scenario():
        actions=['stop','stop',interlude,'stop','stop','stop'];seen=[]
        async def step(*args):
            action=actions[len(seen)];seen.append(action)
            item=result(action=action,command={'action':action})
            item['assessment'].update(status='completed' if len(seen)==len(actions) else 'continue',progress='unknown')
            return item
        async def halt(reason):pass
        runner=TaskRunner(tmp_path/'task.json',step,lambda sid:True,halt,interval=0)
        runner.start('operator','Goal');await finish(runner.worker)
        assert seen==actions and runner.snapshot()['status']=='completed'
    asyncio.run(scenario())
