import pytest

from tools.analyze_sensor_experiment import summarize


def row(*, goal=False, crossed=False, gates=True, speed=0):
    return dict(step=1, action='stop', command={'action':'stop','acceptance':'accepted'},
        candidate_goal_reached=True, motion_observation={'status':'no_wheel_command'},
        observer=dict(pose=dict(x=2., y=3., heading=1.), travelled=1., collisions=0,
            goal_reached=goal, crossed_threshold=crossed, speed=speed,
            checkpoints=[dict(visited=gates)]),
        navigator=dict(estimated_pose=dict(x=0., y=0., heading=1.)))


def test_candidate_claim_alone_does_not_pass_independent_audit():
    result = summarize([row()])
    assert result['goal_candidates'] == [{'step':1,'observer_goal_reached':False}]
    assert result['verified_success'] is False


def completed_rows(**fields):
    a, b = row(**fields), row(**fields)
    b['step'] = 2
    return [a, b], {'status':'completed','step':2}


def test_only_stopped_arrival_with_all_gates_and_two_accepted_final_stops_passes():
    rows, latest = completed_rows(goal=True)
    result = summarize(rows, latest)
    assert result['verified_success'] is True
    assert result['position_success'] is True
    assert result['autonomous_termination_confirmed'] is True
    for fields in [dict(goal=True,crossed=True), dict(goal=True,gates=False), dict(goal=True,speed=180)]:
        rows, latest = completed_rows(**fields)
        result = summarize(rows, latest)
        assert result['verified_success'] is False
        assert result['position_success'] is False
        assert result['autonomous_termination_confirmed'] is True


@pytest.mark.parametrize('status', ['paused','blocked','running','idle','unknown'])
def test_position_alone_does_not_prove_autonomous_completion(status):
    rows, latest = completed_rows(goal=True)
    latest['status'] = status
    result = summarize(rows, latest)
    assert result['position_success'] is True
    assert result['autonomous_termination_confirmed'] is False
    assert result['verified_success'] is False


def test_one_candidate_or_missing_completed_state_does_not_pass():
    result = summarize([row(goal=True)], {'status':'completed','step':1})
    assert result['position_success'] is True and result['verified_success'] is False
    rows, _ = completed_rows(goal=True)
    result = summarize(rows)
    assert result['position_success'] is True and result['verified_success'] is False
    rows[0]['candidate_goal_reached'] = False
    result = summarize(rows, {'status':'completed','step':2})
    assert result['autonomous_termination_confirmed'] is False


@pytest.mark.parametrize('field,value', [('action','forward'),('acceptance','rejected'),
                                      ('acceptance','unknown'),('acceptance','transport_accepted')])
def test_final_command_must_be_an_actually_accepted_stop(field, value):
    rows, latest = completed_rows(goal=True)
    rows[-1]['command'][field] = value
    assert summarize(rows, latest)['verified_success'] is False


def test_actual_action_and_candidate_flags_cannot_be_replaced_by_proposed_stop():
    rows, latest = completed_rows(goal=True)
    rows[-1]['action'] = 'forward'
    assert summarize(rows, latest)['verified_success'] is False
    rows[-1]['action'] = 'stop'
    rows[-1]['candidate_goal_reached'] = 'true'
    assert summarize(rows, latest)['verified_success'] is False


def test_incomplete_or_mismatched_recording_does_not_pass():
    rows, latest = completed_rows(goal=True)
    latest['step'] = 3
    assert summarize(rows, latest)['verified_success'] is False
    rows[-1]['step'] = 3
    assert summarize(rows, latest)['verified_success'] is False


def test_empty_recording_does_not_prove_any_success():
    result = summarize([], {'status':'completed'})
    assert not result['position_success'] and not result['autonomous_termination_confirmed']
    assert not result['verified_success']


def test_pose_drift_is_observer_only_and_contact_is_counted_once():
    a, b = row(), row()
    b['step'] = 2
    b['observer']['pose']['x'] += .2
    b['observer']['collisions'] = 1
    result = summarize([a,b])
    assert abs(result['max_translation_estimate_error_m'] - .2) < 1e-9
    assert len(result['new_contact_events']) == 1
    assert result['new_contact_events'][0]['step'] == 2
