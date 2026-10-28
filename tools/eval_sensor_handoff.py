"""Isolated goal-handoff COMPONENT debug after an exactly replayed command prefix.

The prefix is not a new end-to-end navigation result. Replay consumes only the
logged accepted commands and camera-motion observations, with fresh simulated
telemetry. Observer pose/map/goal fields are never restored or sent to control.
Default CLI operation is offline replay validation; --run explicitly enables
the subsequent standard Experiment with Gemma: 100 new steps by default,
explicitly configurable within 1..600 new steps.
"""
import argparse
import os
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_sensor_gemma import Experiment, GOAL, encoded
from server.simulator_perception import TargetVisualMemory
from server.vision_history import VisionHistory
from server.visual_target_track import VisualTargetTrack

DEFAULT_SOURCE = ROOT / 'reports/sensor-gemma-localization-recovery-95676858'
PREFIX_STEPS = 196
DEFAULT_NEW_STEPS = 100
MAX_NEW_STEPS = 600
MAX_STEPS = PREFIX_STEPS + DEFAULT_NEW_STEPS
COMMAND_FIELDS = ('action', 'speed', 'duration_ms', 'tilt')
REPLAY_COMMAND_FIELDS = COMMAND_FIELDS + ('command_id', 'scan', 'reason', 'acceptance')
# These are the only motion fields the sonar navigator consumes. In particular,
# neither observer coordinates nor diagnostic world metadata can pass through.
MOTION_FIELDS = ('status', 'source', 'measured_translation_m')
PREFIX_SOURCES = ('server/sensor_navigator.py', 'server/simulator.py')
CONTROL_SOURCES = ('tools/run_sensor_gemma.py', 'tools/eval_sensor_handoff.py',
    'server/semantic_target.py', 'server/simulator_perception.py',
    'server/threshold_pixels.py', 'server/visual_target_track.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def control_source_hashes():
    names = list(CONTROL_SOURCES)
    if (ROOT/'server/goal_approach.py').is_file():
        names.append('server/goal_approach.py')
    return {name:digest((ROOT/name).read_bytes()) for name in names}


def load_prefix(source, count=PREFIX_STEPS):
    """Select only control inputs from the first contiguous timeline rows."""
    records = []
    with (source / 'timeline.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            if len(records) >= count:
                break
            row = json.loads(line)
            step = row.get('step')
            if type(step) is not int or step != len(records) + 1:
                raise ValueError('Prefix must contain contiguous steps starting at one')
            command = row.get('command')
            motion = row.get('motion_observation')
            if not isinstance(command, dict) or not isinstance(motion, dict):
                raise ValueError('Prefix requires command and camera motion observation')
            if not all(key in command for key in COMMAND_FIELDS):
                raise ValueError('Incomplete prefix command')
            if command.get('acceptance') not in ('accepted', 'rejected'):
                raise ValueError('Prefix command acceptance must be explicit')
            records.append(dict(step=step,
                command={key:deepcopy(command[key]) for key in REPLAY_COMMAND_FIELDS if key in command},
                motion_observation={key:deepcopy(motion[key]) for key in MOTION_FIELDS if key in motion}))
    if len(records) != count:
        raise ValueError('Insufficient prefix rows')
    return records


def verify_prefix_sources(source):
    manifest = json.loads((source / 'source-manifest.json').read_text(encoding='utf-8'))
    matched = {}
    for name in PREFIX_SOURCES:
        actual = digest((ROOT / name).read_bytes())
        expected = manifest['files'][name]
        if actual != expected:
            raise ValueError('Prefix source changed: ' + name)
        matched[name] = actual
    return matched


def replay_prefix(experiment, records, source):
    """Replay commands through physics and the ordinary navigator lifecycle.

    No provider calls, loaded telemetry, world snapshot, or observer input.
    Every available saved BEFORE/AFTER JPEG is checked at its original step.
    """
    checked = 0
    prefix_hasher = hashlib.sha256()
    for expected_step, row in enumerate(records, 1):
        if row['step'] != expected_step:
            raise ValueError('Replay order changed')
        before_t = experiment.world.telemetry()
        before = experiment.world.camera_jpeg()
        # next_command advances scan/frontier bookkeeping even though replay
        # deliberately executes the original logged actual command instead.
        experiment.navigator.next_command(before_t)
        command = deepcopy(row['command'])
        actual = {key:command[key] for key in COMMAND_FIELDS}
        accepted = experiment.world.command(**actual)['accepted']
        if accepted != (command['acceptance'] == 'accepted'):
            raise ValueError('Replay command acceptance diverged at step ' + str(expected_step))
        experiment.clock.now += command['duration_ms'] / 1000 + .02
        after_t = experiment.world.telemetry()
        experiment.world.command('stop')
        after = experiment.world.camera_jpeg()
        experiment.navigator.complete(command, before_t, after_t, row['motion_observation'])
        for role, raw in (('before', before), ('after', after)):
            saved = source / f'step-{expected_step:04d}-{role}.jpg'
            if saved.exists():
                if digest(raw) != digest(saved.read_bytes()):
                    raise ValueError(f'Replay camera mismatch: step {expected_step} {role}')
                checked += 1
        prefix_hasher.update(json.dumps(row, sort_keys=True, separators=(',', ':')).encode())
    experiment.camera = experiment.before = experiment.after = experiment.world.camera_jpeg()
    return dict(steps=len(records), replayed_control_sha256=prefix_hasher.hexdigest(),
                verified_saved_camera_frames=checked, camera_sha256=digest(experiment.camera),
                simulated_seconds=experiment.clock.now)


def verify_navigator_checkpoint(experiment, source, step):
    """Compare only the navigator's estimate, never the observer's pose."""
    expected = None
    with (source/'timeline.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('step') == step:
                expected = row.get('navigator')
                break
    actual = experiment.navigator.snapshot()
    normalized = lambda value:json.dumps(value, sort_keys=True, separators=(',', ':'))
    if not isinstance(expected,dict) or normalized(actual) != normalized(expected):
        raise ValueError('Replayed navigator checkpoint differs from the saved estimate')
    return digest(normalized(actual).encode())


def prepare_component(provider, source, folder, *, prefix_steps=PREFIX_STEPS, max_steps=MAX_STEPS):
    """Rebuild a checkpoint without loading any observer state or old VLM prose."""
    if (type(prefix_steps) is not int or type(max_steps) is not int or prefix_steps < 1
            or not 1 <= max_steps - prefix_steps <= MAX_NEW_STEPS):
        raise ValueError('Invalid component step budget')
    sources = verify_prefix_sources(source)
    records = load_prefix(source, prefix_steps)
    experiment = Experiment(provider, name='component-handoff', max_steps=max_steps)
    replay = replay_prefix(experiment, records, source)
    replay['navigator_checkpoint_sha256'] = verify_navigator_checkpoint(experiment, source, prefix_steps)
    # Start the new semantic observation history at the actual replayed camera.
    experiment.vision = VisionHistory(experiment.clock)
    experiment.vision.observe(encoded(experiment.camera), experiment.world.telemetry(), 'simulator')
    experiment.target_memory = TargetVisualMemory(max_age_steps=3)
    experiment.target_track = VisualTargetTrack(experiment.clock)
    # Force current-image inspection on the very first new step. No previous
    # semantic observations are restored merely to influence scan scheduling.
    experiment.scan_since_perception = 1
    experiment.state.update(step=prefix_steps, status='running', phase='observing',
        perception=None, events=[], perception_calls=0, perception_memory_steps=0,
        reason='Izolovaný komponentový test předání cíle po přehrání uložených povelů.')
    experiment.folder = folder
    metadata = dict(component_only=True, end_to_end_pass=False, simulation_only=True,
        scenario='apartment', goal=GOAL, max_steps=max_steps,
        max_new_steps=max_steps-prefix_steps,
        replayed_prefix={**replay, 'source_report':source.name,
                         'source_hashes':sources,
                         'inputs':'logged actual commands + camera-motion fields; fresh telemetry',
                         'restored_observer_state':False},
        fresh_visual_memory=True, fresh_target_track=True,
        first_continuation_observation_forced=True,
        control_sources=control_source_hashes())
    # The same labels accompany the UI-compatible status/timeline output.
    experiment.state.update(component_only=True, replayed_prefix=deepcopy(metadata['replayed_prefix']))
    return experiment, metadata


async def run_component(experiment, metadata):
    """Use the unchanged Experiment loop; audit independent world truth only at end."""
    save_json(experiment.folder / 'component-metadata.json', metadata)
    await experiment.run(experiment.generation)
    final = experiment.status()  # Output-only evaluation, never supplied to control.
    observer = final['observer']
    criteria = dict(two_candidates=experiment.goal_candidate_count >= 2,
                    observer_goal_reached=observer['goal_reached'],
                    no_threshold_crossing=not observer['crossed_threshold'],
                    zero_contacts=observer['collisions'] == 0)
    report = {**metadata, 'status':final['status'], 'step':final['step'],
        'perception_calls':final['perception_calls'], 'reason':final['reason'],
        'observer_criteria':criteria,
        'component_pass':all(criteria.values()) and final['status'] == 'completed',
        'final_observer':observer, 'final_navigator':final['navigator'],
        'scope':'Debug component from replayed prefix; never an end-to-end apartment pass.'}
    save_json(experiment.folder / 'report.json', report)
    return report


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-report', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--run', action='store_true', help='Explicitly enable bounded Gemma continuation')
    parser.add_argument('--max-new-steps', type=int, default=DEFAULT_NEW_STEPS,
                        choices=range(1,MAX_NEW_STEPS+1), metavar='1..600',
                        help='New component steps after replay (default: 100)')
    return parser


async def main():
    args = argument_parser().parse_args()
    source = args.source_report.resolve()
    folder = ROOT/'reports'/('component-handoff-'+secrets.token_hex(4))
    folder.mkdir(parents=True)
    if not args.run:
        experiment, metadata = prepare_component(None, source, folder,
            max_steps=PREFIX_STEPS+args.max_new_steps)
        experiment.state.update(status='paused', phase='paused')
        save_json(folder/'component-metadata.json', metadata)
        (folder/'replayed-checkpoint.jpg').write_bytes(experiment.camera)
        print(json.dumps(dict(component_only=True, mode='replay_only', model_calls=0,
                              report=str(folder), replayed_prefix=metadata['replayed_prefix'])))
        return
    # No app, listener, hardware adapter or OpenAI client is created.
    import httpx
    from server.ai_provider import AIProvider
    async def gemma_only(request):
        expected = httpx.URL(os.environ.get('ROVER_LLM_URL', 'http://127.0.0.1:8080').rstrip('/') + '/v1/chat/completions')
        if request.url != expected:
            raise RuntimeError('Non-Gemma network request refused')
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                event_hooks={'request':[gemma_only]}) as client:
        provider = AIProvider(client, ROOT/'data/simulator', ROOT,
                              gemma_url=os.environ.get('ROVER_LLM_URL', 'http://127.0.0.1:8080'))
        experiment, metadata = prepare_component(provider, source, folder,
            max_steps=PREFIX_STEPS+args.max_new_steps)
        report = await run_component(experiment, metadata)
        print(json.dumps({key:report[key] for key in ('component_only', 'component_pass',
            'end_to_end_pass', 'status', 'step', 'perception_calls', 'observer_criteria')}, ensure_ascii=False))
        print(str(folder/'report.json'))


if __name__ == '__main__':
    asyncio.run(main())
