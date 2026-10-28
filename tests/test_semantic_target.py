"""Pure optical/control checks; no app, model, renderer or simulator imports."""
from copy import deepcopy
import math

import pytest

from server.semantic_target import resolve_semantic_target


def observation(**updates):
    value = dict(goal_visible=True, confidence=.9, bearing='ahead',
                 target_pixel=dict(x=.5, y=.5), threshold_visible=True,
                 threshold_pixel=.54, at_threshold=False, hazard='clear')
    value.update(updates)
    return value


def telemetry(**updates):
    value = dict(simulation=True, connected=True, telemetry_age_ms=0,
                 servo=55, distance_cm=200, motor_max_duration_ms=2000)
    value.update(updates)
    return value


def resolve(value=None, **updates):
    return resolve_semantic_target(observation() if value is None else value, telemetry(**updates))


def threshold_at(distance, servo=55):
    horizon = 240 - math.tan(math.radians((90 - servo) * .55)) * 280
    return (horizon + .22 * 280 / distance) / 480


@pytest.mark.parametrize('value', [None, [], '', False, {}, dict(goal_visible=False, confidence=1),
                                  observation(goal_visible=1), observation(goal_visible='true')])
def test_no_confirmed_goal_produces_no_proposal(value):
    assert resolve_semantic_target(value, telemetry()) is None


@pytest.mark.parametrize('confidence', [None, True, False, math.nan, math.inf, -.1, 1.1, .79999, '1'])
def test_invalid_or_low_confidence_does_not_claim_goal(confidence):
    assert resolve(observation(confidence=confidence)) is None


@pytest.mark.parametrize('hazard', ['person', 'animal', 'drop'])
def test_specific_hazard_stops_even_without_confirmed_goal(hazard):
    result = resolve(dict(goal_visible=False, hazard=hazard))
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


@pytest.mark.parametrize('field,value', [('simulation', None), ('simulation', 1), ('simulation', False),
    ('connected', False), ('telemetry_age_ms', None), ('telemetry_age_ms', True),
    ('telemetry_age_ms', -1), ('telemetry_age_ms', 501), ('telemetry_age_ms', math.nan),
    ('telemetry_age_ms', math.inf), ('servo', None), ('servo', True), ('servo', math.nan),
    ('servo', 34), ('servo', 146)])
def test_missing_stale_non_sim_or_invalid_calibration_cannot_move(field, value):
    for x in (.1, .5):
        result = resolve(observation(target_pixel=dict(x=x, y=.5)), **{field: value})
        assert result['action'] == 'stop' and not result['candidate_goal_reached']


@pytest.mark.parametrize('pixel', [None, {}, {'x': .5}, {'x': .5, 'y': .5, 'z': 0},
    {'x': None, 'y': .5}, {'x': True, 'y': .5}, {'x': -.1, 'y': .5}, {'x': 1.1, 'y': .5},
    {'x': math.nan, 'y': .5}, {'x': math.inf, 'y': .5}, {'x': .5, 'y': False}, {'x': .5, 'y': 2}])
def test_missing_or_invalid_target_pixel_never_uses_bearing_as_position(pixel):
    result = resolve(observation(target_pixel=pixel, bearing='ahead', at_threshold=True))
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


def test_observed_positive_with_null_target_pixel_cannot_move_or_complete():
    result = resolve(observation(confidence=1, target_pixel=None, threshold_pixel=.54))
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


@pytest.mark.parametrize('threshold', [None, True, math.nan, math.inf, -.1, 1.1, '0.8'])
def test_invalid_threshold_cannot_supply_floor_geometry(threshold):
    result = resolve(observation(threshold_pixel=threshold, at_threshold=True))
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


def test_at_threshold_alone_does_not_complete_and_missing_threshold_can_only_tilt():
    for servo in (35, 90, 145):
        result = resolve(observation(threshold_visible=False, threshold_pixel=None, target_pixel=None), servo=servo)
        assert result['action'] == 'tilt' and result['tilt'] == 55
        assert result['speed'] == 0 and not result['candidate_goal_reached']
    result = resolve(observation(threshold_visible=False, threshold_pixel=None), servo=55)
    assert result['action'] == 'stop' and result['goal_handoff_pending']
    result = resolve(observation(threshold_visible=False, threshold_pixel=None, at_threshold=True))
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


@pytest.mark.parametrize('x,action', [(0, 'rotate_left'), (.4, 'rotate_left'), (.6, 'rotate_right'), (1, 'rotate_right')])
def test_rotation_sign_uses_atan_and_sim_calibration_with_short_pulse(x, action):
    result = resolve(observation(target_pixel=dict(x=x, y=.5)), distance_cm=None)
    heading = math.atan((.5-x)*640/280)
    assert result['action'] == action and result['speed'] == 150
    assert 50 <= result['duration_ms'] <= 500
    assert result['heading_error_rad'] == pytest.approx(heading)
    assert result['duration_ms'] == min(500, max(50, round(abs(heading)/(1.2*(150-45)/(180-45))*1000)))
    assert not result['candidate_goal_reached']


@pytest.mark.parametrize('servo', [35, 55, 90, 110])
def test_camera_tilt_changes_horizon_and_restores_the_same_floor_range(servo):
    y = threshold_at(.8, servo)
    assert 0 <= y <= 1
    result = resolve(observation(threshold_pixel=y), servo=servo)
    assert result['action'] == 'forward'
    assert result['estimated_range_m'] == pytest.approx(.8)
    assert result['speed'] == 180 and 50 <= result['duration_ms'] <= 1000
    assert result['provenance'] == 'model_perception_calibrated_simulation_estimate'
    assert result['simulation_only'] is True


def test_same_pixel_has_different_range_at_different_servo_not_a_fixed_image_y_rule():
    level = resolve(observation(threshold_pixel=.6), servo=90)
    down = resolve(observation(threshold_pixel=.6), servo=55)
    assert level['estimated_range_m'] > down['estimated_range_m']


@pytest.mark.parametrize('y', [0, .2, .5, 241/480])
def test_threshold_at_or_above_horizon_is_not_distance_or_completion(y):
    result = resolve(observation(threshold_pixel=y, at_threshold=True), servo=90)
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


@pytest.mark.parametrize('distance', [.25, .30, .35])
def test_near_threshold_is_only_a_stopped_candidate_even_if_model_says_not_at_threshold(distance):
    result = resolve(observation(threshold_pixel=threshold_at(distance), at_threshold=False))
    assert result['action'] == 'stop' and result['speed'] == 0
    assert result['candidate_goal_reached'] is True
    assert result['estimated_range_m'] == pytest.approx(distance)


@pytest.mark.parametrize('distance', [None, True, False, 0, 10, 401, math.nan, math.inf, '100'])
def test_bad_sonar_blocks_forward(distance):
    result = resolve(distance_cm=distance)
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


@pytest.mark.parametrize('distance', [.351, .36, .38, .4, .5, 1, 5])
def test_duration_never_rounds_up_past_threshold_budget(distance):
    result = resolve(observation(threshold_pixel=threshold_at(distance)))
    if result['action'] == 'forward':
        assert result['duration_ms'] <= 1000
        assert result['duration_ms']*.4/1000 <= distance-.33+1e-12
        assert distance-result['duration_ms']*.35/1000 >= .30
    else:
        assert result['action'] == 'stop' and not result['candidate_goal_reached']


def test_sonar_firmware_and_maximum_duration_each_bound_a_single_forward():
    value = observation(threshold_pixel=threshold_at(3))
    assert resolve(value)['duration_ms'] == 1000
    assert resolve(value, motor_max_duration_ms=250)['duration_ms'] == 250
    result = resolve(value, distance_cm=25)
    assert result['action'] == 'forward' and result['duration_ms'] == 300
    assert resolve(value, distance_cm=11)['action'] == 'stop'
    for capability in (None, True, 0, 49, math.nan, math.inf):
        assert resolve(value, motor_max_duration_ms=capability)['action'] == 'stop'


def test_just_outside_candidate_band_can_finish_one_bounded_step_without_a_dead_zone():
    distance = .351
    result = resolve(observation(threshold_pixel=threshold_at(distance)))
    assert result['action'] == 'forward' and 50 <= result['duration_ms'] <= 55
    after = distance - .35 * result['duration_ms'] / 1000
    assert .30 < after < .35
    candidate = resolve(observation(threshold_pixel=threshold_at(after)))
    assert candidate['action'] == 'stop' and candidate['candidate_goal_reached']


@pytest.mark.parametrize('hazard', ['obstacle', 'unknown', None, 'clearish'])
def test_unclear_floor_cannot_authorize_forward_or_completion(hazard):
    result = resolve(observation(hazard=hazard, threshold_pixel=threshold_at(.30)))
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


def test_stop_or_tilt_inputs_never_become_locomotion():
    for action in ('stop', 'tilt'):
        result = resolve(observation(action=action))
        assert result['action'] == 'stop' and result['speed'] == 0


def test_resolver_is_detached_and_does_not_change_inputs_or_depend_on_bearing_claim():
    value, t = observation(bearing='left'), telemetry()
    before = deepcopy((value,t))
    result = resolve_semantic_target(value,t)
    assert result['action'] == 'forward'  # actual normalized x is centered
    result['reason'] = 'changed'
    assert (value,t) == before
    assert resolve_semantic_target(observation(), None)['action'] == 'stop'
    assert resolve(telemetry_age_ms=500)['action'] == 'forward'
    assert resolve(observation(confidence=.8))['action'] == 'forward'


@pytest.mark.parametrize('x,action', [(.15, 'rotate_left'), (.05, 'rotate_left'),
    (.67, 'rotate_right'), (.44, 'rotate_left'), (.345, 'rotate_left'), (.375, 'rotate_left')])
def test_visible_opening_aligns_before_threshold_localization(x, action):
    # Current-image points from the recovery handoff failure; no map or pose.
    value = observation(target_pixel=dict(x=x, y=.15), threshold_visible=False,
                        threshold_pixel=None, bearing='unknown')
    result = resolve(value, distance_cm=None)
    assert result['action'] == action and result['speed'] == 150
    assert 50 <= result['duration_ms'] <= 500 and result['tilt'] == 55
    assert result['semantic_phase'] == 'goal_alignment'
    assert result['goal_handoff_pending'] is True
    assert result['estimated_range_m'] is None
    assert result['candidate_goal_reached'] is False


@pytest.mark.parametrize('x', [.46, .5, .54])
@pytest.mark.parametrize('servo,action', [(55, 'stop'), (90, 'tilt')])
def test_centered_opening_without_floor_geometry_stays_in_stopped_observation(x, servo, action):
    value = observation(target_pixel=dict(x=x, y=.1), threshold_visible=False,
                        threshold_pixel=None)
    result = resolve(value, servo=servo)
    assert result['action'] == action and result['speed'] == 0
    assert result['semantic_phase'] == 'goal_alignment'
    assert result['goal_handoff_pending'] is True
    assert result['estimated_range_m'] is None and not result['candidate_goal_reached']


@pytest.mark.parametrize('point', [None, {}, {'x': True, 'y': .3},
    {'x': math.nan, 'y': .3}, {'x': .1, 'y': math.inf}, {'x': -.1, 'y': .3},
    {'x': .1, 'y': .3, 'z': 0}])
def test_missing_threshold_and_invalid_target_never_rotates_using_bearing(point):
    value = observation(target_pixel=point, threshold_visible=False,
                        threshold_pixel=None, bearing='left')
    result = resolve(value)
    assert result['action'] == 'stop' and result['speed'] == 0
    assert result['estimated_range_m'] is None and not result['candidate_goal_reached']


@pytest.mark.parametrize('hazard', ['unknown', 'obstacle', 'person', 'animal', 'drop'])
def test_opening_without_threshold_does_not_rotate_with_hazard_or_unclear_space(hazard):
    result = resolve(observation(target_pixel=dict(x=.1, y=.2), threshold_visible=False,
                                threshold_pixel=None, hazard=hazard))
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


@pytest.mark.parametrize('updates', [dict(simulation=False), dict(telemetry_age_ms=501),
                                   dict(servo=None), dict(connected=False)])
def test_new_alignment_phase_keeps_fresh_simulation_guards(updates):
    value = observation(target_pixel=dict(x=.1, y=.2), threshold_visible=False,
                        threshold_pixel=None)
    result = resolve(value, **updates)
    assert result['action'] == 'stop' and not result['candidate_goal_reached']


def test_alignment_then_threshold_requires_a_new_measurement_before_approach():
    value = observation(target_pixel=dict(x=.1, y=.2), threshold_visible=False,
                        threshold_pixel=None)
    aligned = resolve(value)
    assert aligned['action'] == 'rotate_left'
    centered = dict(value, target_pixel=dict(x=.5, y=.2))
    assert resolve(centered)['action'] == 'stop'
    measured = dict(centered, threshold_visible=True, threshold_pixel=threshold_at(.8))
    assert resolve(measured)['action'] == 'forward'
    lost = dict(centered, goal_visible=False, confidence=0, target_pixel=None, bearing='unknown')
    assert resolve(lost) is None  # prior alignment never upgrades a negative frame


def test_malformed_threshold_cannot_be_bypassed_by_alignment():
    for update in (dict(threshold_visible=False, threshold_pixel=.4),
                   dict(threshold_visible=False, threshold_pixel=None, at_threshold=True),
                   dict(threshold_visible=True, threshold_pixel=None),
                   dict(threshold_visible=True, threshold_pixel=math.nan)):
        result = resolve(observation(target_pixel=dict(x=.1, y=.2), **update))
        assert result['action'] == 'stop' and not result['candidate_goal_reached']
