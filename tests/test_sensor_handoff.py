"""Offline replay checks: never call a provider or restore observer coordinates."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tools import eval_sensor_handoff as handoff


def test_loader_discards_observer_and_unconsumed_motion_fields(tmp_path):
    value = dict(step=1, command=dict(action='rotate_left', speed=150, duration_ms=100,
        tilt=55, acceptance='accepted', command_id='old1', scan=False, observer_pose={'x':99}),
        motion_observation=dict(status='changed', source='floor_visual_odometry',
            measured_translation_m=.02, observer_pose={'x':99}, world={'door':99}),
        observer=dict(pose={'x':99}), before_telemetry={'distance_cm':999})
    (tmp_path/'timeline.jsonl').write_text(json.dumps(value)+'\n', encoding='utf-8')
    record = handoff.load_prefix(tmp_path, 1)[0]
    assert set(record) == {'step','command','motion_observation'}
    assert set(record['command']) == set(handoff.REPLAY_COMMAND_FIELDS)-{'reason'}
    assert record['motion_observation'] == dict(status='changed', source='floor_visual_odometry',
                                              measured_translation_m=.02)
    assert 'observer' not in json.dumps(record)


@pytest.mark.parametrize('update', [dict(step=2), dict(command={}),
    dict(command=dict(action='stop',speed=0,duration_ms=100,tilt=90,acceptance='unknown')),
    dict(motion_observation=None)])
def test_loader_rejects_ambiguous_or_noncontiguous_prefix(tmp_path, update):
    row = dict(step=1, command=dict(action='stop',speed=0,duration_ms=100,tilt=90,
                                   acceptance='accepted'), motion_observation={'status':'changed'})
    row.update(update)
    (tmp_path/'timeline.jsonl').write_text(json.dumps(row)+'\n', encoding='utf-8')
    with pytest.raises(ValueError): handoff.load_prefix(tmp_path, 1)


def test_replay_uses_actual_telemetry_and_advances_navigator_without_observer(tmp_path):
    clock=SimpleNamespace(now=0.)
    class World:
        def __init__(self): self.commands=[]
        def telemetry(self): return {'fresh_at':clock.now}
        def camera_jpeg(self): return str(clock.now).encode()
        def command(self, action, **fields):
            self.commands.append(dict(action=action,**fields));return {'accepted':True}
        def snapshot(self): raise AssertionError('Observer state must not be read in replay')
    class Nav:
        def __init__(self): self.proposed=[];self.completed=[]
        def next_command(self,t): self.proposed.append(deepcopy(t));return {'action':'different_proposal'}
        def complete(self,*args): self.completed.append(deepcopy(args))
    experiment=SimpleNamespace(clock=clock,world=World(),navigator=Nav())
    command=dict(action='rotate_left',speed=150,duration_ms=100,tilt=55,
                 acceptance='accepted',command_id='logged1',scan=False)
    records=[dict(step=1,command=command,motion_observation={'status':'changed'})]
    result=handoff.replay_prefix(experiment,records,tmp_path)
    assert experiment.world.commands == [dict(action='rotate_left',speed=150,duration_ms=100,tilt=55),
                                          dict(action='stop')]
    assert experiment.navigator.proposed == [{'fresh_at':0.}]
    completed=experiment.navigator.completed[0]
    assert completed[0] == command and completed[1] == {'fresh_at':0.}
    assert completed[2] == {'fresh_at':pytest.approx(.12)}
    assert completed[3] == {'status':'changed'}
    assert result['simulated_seconds'] == pytest.approx(.12)
    assert result['steps'] == 1


def test_replay_rejects_changed_saved_camera(tmp_path):
    from tools.run_sensor_gemma import Experiment
    experiment=Experiment(None)
    records=[dict(step=1,command=dict(action='stop',speed=0,duration_ms=100,tilt=90,
                                     acceptance='accepted'),motion_observation={'status':'changed'})]
    (tmp_path/'step-0001-before.jpg').write_bytes(b'not-the-replayed-camera')
    with pytest.raises(ValueError,match='camera mismatch'):
        handoff.replay_prefix(experiment,records,tmp_path)


def test_checkpoint_continuation_frame_matches_saved_prefix():
    source=handoff.DEFAULT_SOURCE
    if not source.exists(): pytest.skip('Local experiment fixtures are not distributed')
    handoff.verify_prefix_sources(source)
    # An independent saved continuation BEFORE is the same stationary camera as
    # prefix196 AFTER. The full replay CLI additionally verifies all 392 JPEGs.
    after=(source/'step-0196-after.jpg').read_bytes()
    continuation=(source/'step-0197-before.jpg').read_bytes()
    assert handoff.digest(after) == handoff.digest(continuation)


def test_prefix_manifest_source_change_fails_closed(tmp_path):
    (tmp_path/'source-manifest.json').write_text(json.dumps({'files':{
        name:'wrong' for name in handoff.PREFIX_SOURCES}}))
    with pytest.raises(ValueError,match='source changed'):
        handoff.verify_prefix_sources(tmp_path)


def test_navigator_checkpoint_compares_only_its_own_estimate(tmp_path):
    estimate={'estimated_pose':{'x':1.,'y':2.,'heading':.3},'accepted_steps':1}
    row=dict(step=1,navigator=estimate,observer={'pose':{'x':'irrelevant-world-truth'}})
    (tmp_path/'timeline.jsonl').write_text(json.dumps(row)+'\n',encoding='utf-8')
    experiment=SimpleNamespace(navigator=SimpleNamespace(snapshot=lambda:deepcopy(estimate)))
    assert len(handoff.verify_navigator_checkpoint(experiment,tmp_path,1)) == 64
    experiment.navigator.snapshot=lambda:{**estimate,'accepted_steps':2}
    with pytest.raises(ValueError,match='checkpoint differs'):
        handoff.verify_navigator_checkpoint(experiment,tmp_path,1)


def test_cli_default_is_offline_replay_with_100_new_step_budget():
    args=handoff.argument_parser().parse_args([])
    assert args.run is False and args.max_new_steps == 100
    assert handoff.MAX_STEPS == handoff.PREFIX_STEPS + 100


@pytest.mark.parametrize('value',[1,100,600])
def test_cli_accepts_explicit_bounded_new_step_budget(value):
    args=handoff.argument_parser().parse_args(['--max-new-steps',str(value)])
    assert args.max_new_steps == value and args.run is False


@pytest.mark.parametrize('value',['0','601','-1','1.5'])
def test_cli_rejects_invalid_new_step_budgets(value):
    with pytest.raises(SystemExit):
        handoff.argument_parser().parse_args(['--max-new-steps',value])


@pytest.mark.parametrize('max_steps',[196,797,True,None])
def test_prepare_rejects_budget_before_any_replay_or_provider_use(tmp_path,max_steps):
    with pytest.raises(ValueError,match='step budget'):
        handoff.prepare_component(None,tmp_path,tmp_path,max_steps=max_steps)


def test_control_manifest_uses_current_runner_hash_and_optional_goal_approach(tmp_path,monkeypatch):
    monkeypatch.setattr(handoff,'ROOT',tmp_path)
    for name in handoff.CONTROL_SOURCES:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(name.encode())
    first=handoff.control_source_hashes()
    assert first['tools/run_sensor_gemma.py'] == handoff.digest(b'tools/run_sensor_gemma.py')
    assert 'server/goal_approach.py' not in first
    (tmp_path/'server/goal_approach.py').write_bytes(b'new approach controller')
    (tmp_path/'tools/run_sensor_gemma.py').write_bytes(b'integrated current runner')
    second=handoff.control_source_hashes()
    assert second['server/goal_approach.py'] == handoff.digest(b'new approach controller')
    assert second['tools/run_sensor_gemma.py'] == handoff.digest(b'integrated current runner')
