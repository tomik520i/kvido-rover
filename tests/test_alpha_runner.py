"""Server autonomy tests using callback fakes; no browser, network or motors."""
import asyncio
import json

import pytest

from server.alpha_runner import AlphaRunner, ConnectionLost


class Rig:
    def __init__(self, path):
        self.connected = True
        self.owned = False
        self.epoch = 1
        self.calls = []
        self.steps = []
        self.acquire_failures = 0
        self.acquire_gate = None
        self.inference_gate = None
        self.swallow_cancellation = False
        self.entered = asyncio.Event()
        self.fail_motion = False
        self.beats = 0
        self.runner = AlphaRunner(path, self.inspect, self.acquire, self.drive,
                                  self.stop, self.radar, self.heartbeat,
                                  sleep=self.sleep)

    async def sleep(self, seconds):
        await asyncio.sleep(0)

    async def inspect(self):
        self.calls.append(('inspect',))
        return dict(telemetry=dict(connected=self.connected,
                                  telemetry_age_ms=0, distance_cm=100),
                    owned=self.owned, armed=self.owned, estop=False,
                    epoch=self.epoch)

    async def acquire(self):
        self.calls.append(('acquire',))
        self.entered.set()
        if self.acquire_gate:
            await self.wait_gate(self.acquire_gate)
        if self.acquire_failures:
            self.acquire_failures -= 1
            raise ConnectionLost('release unavailable')
        self.owned = True

    async def drive(self, action, speed, clearance, tilt, duration, epoch):
        self.calls.append(('drive', action, epoch))
        if self.fail_motion and action == 'forward':
            self.fail_motion = False
            raise TimeoutError('outcome unknown')

    async def stop(self, reason):
        self.calls.append(('stop', reason))
        self.owned = False

    async def radar(self, action, body):
        self.calls.append(('radar', action, body.copy()))
        if action == 'start':
            return {'run': 'radar-run'}
        self.entered.set()
        if self.inference_gate:
            await self.wait_gate(self.inference_gate)
        return self.steps.pop(0) if self.steps else {'done': True}

    async def wait_gate(self, gate):
        try:
            await gate.wait()
        except asyncio.CancelledError:
            if not self.swallow_cancellation:
                raise
            await gate.wait()  # Simulate a transport that still returns a late response.

    def heartbeat(self):
        self.beats += 1


async def finish(runner):
    await asyncio.wait_for(runner.join(), 2)


def test_unavailable_start_retries_acquire_and_observes_fresh(tmp_path):
    async def scenario():
        rig = Rig(tmp_path / 'alpha.json')
        rig.connected = False
        rig.acquire_failures = 1
        original_sleep = rig.sleep

        async def recover(seconds):
            rig.connected = True
            await original_sleep(seconds)

        rig.runner.sleep = recover
        rig.steps = [{'actions': [[90, 3, 'forward']]}, {'done': True}]
        rig.runner.start('Find the doorway')
        await finish(rig.runner)
        assert rig.runner.data['status'] == 'completed'
        assert rig.runner.data['goal'] == 'Find the doorway'
        assert rig.runner.data['retry_count'] == 2
        assert len([c for c in rig.calls if c[:2] == ('radar', 'step')]) == 2
        assert not any(c[:2] == ('drive', 'forward') for c in rig.calls)
        assert rig.beats > 0  # Entire run has no browser heartbeat callback.
    asyncio.run(scenario())


def test_ambiguous_motion_discards_remaining_plan(tmp_path):
    async def scenario():
        rig = Rig(tmp_path / 'alpha.json')
        rig.fail_motion = True
        rig.steps = [{'actions': [[90, 4, 'forward']]},
                     {'actions': [[90, 4, 'forward']]}, {'done': True}]
        rig.runner.start('Explore')
        await finish(rig.runner)
        assert len([c for c in rig.calls if c[:2] == ('drive', 'forward')]) == 1
        assert len([c for c in rig.calls if c[:2] == ('radar', 'step')]) == 3
        assert rig.runner.data['pending_command'] is None
        assert any(h['kind'] == 'uncertain' for h in rig.runner.data['history'])
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['acquire', 'inference'])
@pytest.mark.parametrize('late_response', [False, True])
def test_cancellation_while_waiting_never_sends_late_motion(tmp_path, phase, late_response):
    async def scenario():
        rig = Rig(tmp_path / 'alpha.json')
        rig.swallow_cancellation = late_response
        gate = asyncio.Event()
        if phase == 'acquire':
            rig.acquire_gate = gate
        else:
            rig.owned = True
            rig.inference_gate = gate
        rig.steps = [{'actions': [[90, 2, 'forward']]}]
        rig.runner.start('Explore')
        await asyncio.wait_for(rig.entered.wait(), 1)
        rig.runner.interrupt('Explicit STOP')
        gate.set()
        await finish(rig.runner)
        assert rig.runner.data['status'] == 'cancelled'
        assert not any(c[:2] == ('drive', 'forward') for c in rig.calls)
        assert any(c[0] == 'stop' for c in rig.calls)
        assert json.loads(rig.runner.path.read_text())['status'] == 'cancelled'
    asyncio.run(scenario())


def test_pause_resume_preserves_goal_and_discards_old_observation(tmp_path):
    async def scenario():
        rig = Rig(tmp_path / 'alpha.json')
        rig.owned = True
        rig.inference_gate = asyncio.Event()
        rig.runner.start('Same goal')
        await asyncio.wait_for(rig.entered.wait(), 1)
        identifier = rig.runner.data['id']
        rig.runner.interrupt('Pause', status='paused')
        await finish(rig.runner)
        rig.inference_gate = None
        rig.steps = [{'actions': [[90, 2, 'forward']]}, {'done': True}]
        await rig.runner.resume()
        await finish(rig.runner)
        assert rig.runner.data['id'] == identifier
        assert rig.runner.data['goal'] == 'Same goal'
        assert rig.runner.data['status'] == 'completed'
        assert not any(c[:2] == ('drive', 'forward') for c in rig.calls)
        assert any(h['kind'] == 'resume' for h in rig.runner.data['history'])
    asyncio.run(scenario())


def test_restart_retains_history_without_replaying_persisted_command(tmp_path):
    async def scenario():
        path = tmp_path / 'alpha.json'
        saved = AlphaRunner.empty()
        saved.update(status='running', goal='Stored goal', radar_run='old-run',
                     seq=7, history=[{'kind': 'decision', 'text': 'Earlier observation'}],
                     last_decision={'actions': [[90, 5, 'forward']]},
                     pending_command={'action': 'forward'})
        path.write_text(json.dumps(saved))
        rig = Rig(path)
        assert rig.runner.data['status'] == 'reconnecting'
        assert rig.runner.data['pending_command'] is None
        rig.steps = [{'actions': [[90, 5, 'forward']]}, {'done': True}]
        rig.runner.launch()
        await finish(rig.runner)
        assert rig.runner.data['goal'] == 'Stored goal'
        assert any(h['text'] == 'Earlier observation' for h in rig.runner.data['history'])
        assert not any(c[:2] == ('drive', 'forward') for c in rig.calls)
        assert rig.runner.owner not in path.read_text()
        assert rig.runner.owner not in json.dumps(rig.runner.snapshot())
    asyncio.run(scenario())


def test_cancelled_saved_run_does_not_launch(tmp_path):
    async def scenario():
        path = tmp_path / 'alpha.json'
        path.write_text(json.dumps(dict(AlphaRunner.empty(), status='cancelled', goal='Cancelled')))
        rig = Rig(path)
        rig.runner.launch()
        await asyncio.sleep(0)
        assert rig.runner.worker is None
        assert rig.calls == []
    asyncio.run(scenario())


def test_stop_while_resume_joins_old_worker_cannot_resurrect_run(tmp_path):
    async def scenario():
        rig = Rig(tmp_path / 'alpha.json')
        rig.runner.data.update(status='paused', goal='Keep this cancelled')
        joining = asyncio.Event()
        cleanup = asyncio.Event()

        async def old_worker():
            try:
                await cleanup.wait()
            except asyncio.CancelledError:
                await cleanup.wait()  # Device cleanup has not finished yet.

        rig.runner.worker = asyncio.create_task(old_worker())
        original_join = rig.runner.join

        async def observed_join():
            joining.set()
            await original_join()

        rig.runner.join = observed_join
        pending_resume = asyncio.create_task(rig.runner.resume())
        await asyncio.wait_for(joining.wait(), 1)
        rig.runner.interrupt('Explicit STOP')
        cleanup.set()
        with pytest.raises(ValueError):
            await asyncio.wait_for(pending_resume, 1)
        assert rig.runner.data['status'] == 'cancelled'
        assert not rig.runner.active
        assert rig.calls == []
        assert json.loads(rig.runner.path.read_text())['status'] == 'cancelled'
    asyncio.run(scenario())


def test_concurrent_resume_only_launches_once(tmp_path):
    async def scenario():
        rig = Rig(tmp_path / 'alpha.json')
        rig.runner.data.update(status='paused', goal='Resume once')
        cleanup = asyncio.Event()
        both_joining = asyncio.Event()
        join_count = 0
        rig.runner.worker = asyncio.create_task(cleanup.wait())
        original_join = rig.runner.join

        async def observed_join():
            nonlocal join_count
            join_count += 1
            if join_count == 2:
                both_joining.set()
            await original_join()

        rig.runner.join = observed_join
        first = asyncio.create_task(rig.runner.resume())
        second = asyncio.create_task(rig.runner.resume())
        await asyncio.wait_for(both_joining.wait(), 1)
        cleanup.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert sum(isinstance(result, dict) for result in results) == 1
        await finish(rig.runner)
        assert len([h for h in rig.runner.data['history'] if h['kind'] == 'resume']) == 1
        assert len([c for c in rig.calls if c[:2] == ('radar', 'start')]) == 1
        assert rig.runner.data['status'] == 'completed'
    asyncio.run(scenario())
