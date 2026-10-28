"""Known camera projections evaluate the estimator; no truth enters its API."""
import inspect
import math

import pytest

from server import visual_odometry as vo


def test_no_dependencies_produces_unknown_not_command_displacement(monkeypatch):
    monkeypatch.setattr(vo, 'cv2', None)
    result = vo.estimate_motion(b'a', b'b', command=dict(action='forward', speed=180,
        duration_ms=714, acceptance='accepted'))
    assert result['motion'] is None
    assert result['reason'] == 'opencv_numpy_unavailable'


def test_estimator_api_has_no_world_pose_or_map_input():
    assert set(inspect.signature(vo.estimate_motion).parameters) == {
        'before_jpeg', 'after_jpeg', 'calibration', 'before_servo', 'after_servo', 'command'}


@pytest.fixture
def optics():
    if vo.cv2 is None:
        pytest.skip('optional OpenCV/numpy unavailable')
    cv, np = vo.cv2, vo.np
    texture = np.random.default_rng(42).integers(40, 230, size=(120, 120), dtype=np.uint8)
    texture = cv.resize(texture, (1800, 1800), interpolation=cv.INTER_NEAREST)
    texture = cv.GaussianBlur(texture, (11, 11), 2.)
    yy, xx = np.indices((480, 640), dtype=float)
    depth = .22*280/np.maximum(1, yy-240)
    left = (320-xx)*depth/280
    def render(forward=0., lateral=0., yaw=0., occlusion=False):
        wx = 2+forward+depth*np.cos(yaw)-left*np.sin(yaw)
        wy = 2+lateral+depth*np.sin(yaw)+left*np.cos(yaw)
        frame = cv.remap(texture, (wx*300).astype(np.float32), (wy*300).astype(np.float32),
                         cv.INTER_LINEAR, borderMode=cv.BORDER_CONSTANT, borderValue=100)
        frame[:241] = 120
        if occlusion:
            cv.rectangle(frame, (420, 310), (620, 440), 160, -1)
        return cv.imencode('.jpg', frame, [cv.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
    return render


def command(action, duration):
    return dict(action=action, speed=180, duration_ms=duration, acceptance='accepted')


@pytest.mark.parametrize('forward,left,yaw,prior', [
    (.06, 0, 0, None), (.15, 0, 0, command('forward', 714)),
    (0, .15, 0, command('left', 714)), (.07, .025, 0, command('forward', 714)),
    (0, 0, .2, command('rotate_left', 167)), (0, 0, math.pi/4, command('rotate_left', 655)),
])
def test_floor_plane_motion_and_partial_contact_are_image_measurements(optics, forward, left, yaw, prior):
    result = vo.estimate_motion(optics(), optics(forward, left, yaw), command=prior)
    assert result['motion'] is not None, result
    motion = result['motion']
    assert math.hypot(motion['forward_m']-forward, motion['left_m']-left) < .025, result
    assert abs(motion['yaw_rad']-yaw) < .04, result
    assert result['uncertainty']['translation_m'] > 0


def test_outlier_occluder_does_not_supply_fictitious_floor_motion(optics):
    result = vo.estimate_motion(optics(), optics(.09, occlusion=True), command=command('forward', 714))
    if result['motion'] is not None:
        assert abs(result['motion']['forward_m']-.09) < .025, result
        assert abs(result['motion']['left_m']) < .025, result
    else:
        assert result['status'] == 'unknown'


def test_blank_identical_bad_jpeg_and_changed_tilt_abstain(optics):
    cv, np = vo.cv2, vo.np
    blank = cv.imencode('.jpg', np.full((480, 640), 100, np.uint8))[1].tobytes()
    for before, after, kwargs in [
        (blank, optics(), {}), (optics(), optics(), {}),
        (b'invalid-jpeg', optics(), {}), (optics(), optics(.1), {'after_servo': 89})]:
        result = vo.estimate_motion(before, after, command=command('forward', 714), **kwargs)
        assert result['motion'] is None and result['uncertainty'] is None, result


def test_accepted_command_never_fills_missing_image_measurement(optics):
    raw = optics()
    result = vo.estimate_motion(raw, raw, command=command('forward', 714))
    assert result['motion'] is None
    assert result['reason'] == 'identical_images_do_not_establish_metric_motion'


def test_periodic_floor_can_hide_real_displacement_and_must_not_be_called_zero(optics):
    cv, np = vo.cv2, vo.np
    yy, xx = np.indices((480, 640), dtype=float)
    depth = .22*280/np.maximum(1, yy-240)
    lateral = (320-xx)*depth/280
    def repeated_floor(forward, left):
        checker = (np.floor((depth+forward)*10+1e-7)+np.floor((lateral+left)*10+1e-7)) % 2
        picture = (checker*40+120).astype(np.uint8); picture[:241] = 120
        return cv.imencode('.jpg', picture)[1].tobytes()
    before, after = repeated_floor(0, 0), repeated_floor(.1, .1)
    assert before == after
    result = vo.estimate_motion(before, after, command=command('forward', 714))
    assert result['motion'] is None


def test_inconsistent_equally_supported_image_solutions_are_unknown(monkeypatch):
    def conflicting(before, after, calibration, **options):
        forward = .1 if options['seed'][0] else 0
        return dict(status='estimated', motion=dict(forward_m=forward, left_m=0., yaw_rad=0.),
                    diagnostics=dict(inliers=12))
    monkeypatch.setattr(vo, '_estimate_seed', conflicting)
    result = vo.estimate_motion(b'a', b'b', command=command('forward', 714))
    assert result['motion'] is None and result['reason'] == 'ambiguous_image_solutions'
