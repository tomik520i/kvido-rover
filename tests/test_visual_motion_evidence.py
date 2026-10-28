import pytest
from server.visual_motion_evidence import translation_evidence


COMMAND = dict(action='right', speed=180, duration_ms=714, acceptance='accepted')
PIXEL = dict(status='possible_stall')


def metric(**fields):
    return dict(status='estimated', motion=dict(forward_m=0., left_m=-.005, yaw_rad=0., **fields),
                uncertainty=dict(translation_m=.01))


def test_measured_partial_side_motion_is_not_nominal_command_or_fake_zero():
    value = translation_evidence(COMMAND, metric(), PIXEL)
    assert value['measured_translation_m'] == .005
    assert value['source'] == 'floor_visual_odometry'
    assert value['status'] == 'possible_stall'
    assert PIXEL == {'status':'possible_stall'}


@pytest.mark.parametrize('kind', ['unknown','large_cross','yaw','opposite','too_long','invalid','uncertain','consistent'])
def test_unknown_or_inconsistent_geometry_cannot_override_prior(kind):
    value = metric()
    if kind == 'unknown': value['status'] = 'unknown'
    if kind == 'large_cross': value['motion']['forward_m'] = .10
    if kind == 'yaw': value['motion']['yaw_rad'] = .2
    if kind == 'opposite': value['motion']['left_m'] = .05
    if kind == 'too_long': value['motion']['left_m'] = -.5
    if kind == 'invalid': value['motion']['left_m'] = float('nan')
    if kind == 'uncertain': value['uncertainty']['translation_m'] = .1
    if kind == 'consistent': value['motion']['left_m'] = -.24
    assert translation_evidence(COMMAND, value, PIXEL) == PIXEL


def test_rejected_command_and_rotation_do_not_use_translation_correction():
    assert translation_evidence({**COMMAND,'acceptance':'rejected'}, metric(), PIXEL) == PIXEL
    assert translation_evidence({**COMMAND,'action':'rotate_left'}, metric(), PIXEL) == PIXEL
