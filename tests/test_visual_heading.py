import cv2
import numpy as np
import pytest

from server.visual_heading import estimate_heading


def scene(seed=4):
    rng = np.random.default_rng(seed)
    image = np.zeros((480, 640), np.uint8)
    for _ in range(350):
        x, y = rng.integers([8, 8], [632, 472])
        cv2.circle(image, (int(x), int(y)), int(rng.integers(2, 7)), int(rng.integers(70, 255)), -1)
    return image


def jpg(image):
    return cv2.imencode('.jpg', image)[1].tobytes()


def rotate(image, angle, pitch=0):
    k = np.array([[325., 0, 320], [0, 325., 240], [0, 0, 1]])
    axis = np.array([0., np.cos(np.radians(pitch)), np.sin(np.radians(pitch))])
    r = cv2.Rodrigues(axis*np.radians(angle))[0]
    return cv2.warpPerspective(image, k@r@np.linalg.inv(k), (640, 480))


@pytest.mark.parametrize('angle', [-28, -13, 0, 12, 22])
@pytest.mark.parametrize('pitch', [0, 18])
def test_measures_signed_rotation_without_command_input(angle, pitch):
    before = scene()
    result = estimate_heading(jpg(before), jpg(rotate(before, angle, pitch)))
    assert result['reliable'], result
    assert result['yaw_deg'] == pytest.approx(angle, abs=1)
    assert result['correction_deg'] == -result['yaw_deg']


def test_mixed_adaptive_resolution_preserves_heading():
    before = scene()
    after = cv2.resize(rotate(before, -12), (320, 240))
    result = estimate_heading(jpg(before), jpg(after))
    assert result['reliable']
    assert result['yaw_deg'] == pytest.approx(-12, abs=1)


@pytest.mark.parametrize('after', [b'bad', jpg(np.zeros((480,640),np.uint8)), jpg(scene(55))], ids=['corrupt','blank','different'])
def test_unrelated_or_unusable_pixels_never_guess_a_direction(after):
    result = estimate_heading(jpg(scene()), after)
    assert not result['reliable'] and result['correction_deg'] is None


def test_changed_camera_tilt_is_not_body_yaw():
    image = scene()
    k = np.array([[325.,0,320],[0,325.,240],[0,0,1]])
    r = cv2.Rodrigues(np.array([.25,0.,0.]))[0]
    after = cv2.warpPerspective(image, k@r@np.linalg.inv(k), (640,480))
    result = estimate_heading(jpg(image), jpg(after))
    assert not result['reliable']


def test_small_landmark_cluster_is_insufficient_coverage():
    image = scene()
    image[:180] = 0
    image[280:] = 0
    image[:,:240] = 0
    image[:,340:] = 0
    result = estimate_heading(jpg(image), jpg(rotate(image,-12)))
    assert not result['reliable']
