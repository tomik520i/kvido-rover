import base64
import io

import pytest
from PIL import Image

from server.vision_history import VisionHistory


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def step(self, seconds=1):
        self.now += seconds


def jpeg(level=100):
    output = io.BytesIO()
    Image.new('RGB', (80, 60), (level,) * 3).save(output, 'JPEG')
    return 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def history():
    clock = Clock()
    return VisionHistory(clock=clock), clock


def observe(h, level=100, **telemetry):
    return h.observe(jpeg(level), {'servo': 90, 'light_on': False, **telemetry}, 'car')


def forward(h):
    h.command('forward', 120, 200, 90)


def test_chronological_bounded_frames_and_command_metadata(history):
    h, clock = history
    for index in range(5):
        if index:
            forward(h)
        frames = observe(h, 100 + index)
        clock.step()
    assert len(frames) == 3
    assert [frame['at'] for frame in frames] == [1002, 1003, 1004]
    assert frames[-1]['servo'] == 90
    assert frames[-1]['light_on'] is False
    assert frames[-1]['image'].startswith('data:image/jpeg;base64,')
    assert frames[-1]['commands_since_previous'] == [
        {'action': 'forward', 'speed': 120, 'duration_ms': 200, 'tilt': 90, 'at': 1004}]
    frames[-1]['commands_since_previous'].clear()
    clock.step()
    assert len(observe(h)[-2]['commands_since_previous']) == 1


def test_identical_images_only_suggest_a_stall(history):
    h, clock = history
    observe(h)
    forward(h)
    clock.step()
    result = observe(h)[-1]['motion_observation']
    assert result['status'] == 'possible_stall'
    assert result['mean_absolute_difference'] == 0
    assert result['movement_confirmed'] is False


def test_different_brightness_is_change_not_confirmed_wheel_motion(history):
    h, clock = history
    observe(h, 20)
    forward(h)
    clock.step()
    result = observe(h, 200)[-1]['motion_observation']
    assert result['status'] == 'changed'
    assert result['movement_confirmed'] is False
    assert result['reason'] == 'visual_change_not_odometry'


@pytest.mark.parametrize('action', ['tilt', 'light_on', 'light_off'])
def test_view_commands_prevent_stall_comparison_even_if_settings_return(action, history):
    h, clock = history
    observe(h)
    forward(h)
    h.command(action, 0, 100, 90)
    clock.step()
    assert observe(h)[-1]['motion_observation']['status'] == 'not_comparable'


@pytest.mark.parametrize('telemetry', [
    {'servo': None}, {'servo': True}, {'servo': float('nan')},
    {'servo': 94}, {'light_on': None}, {'light_on': True},
])
def test_unknown_or_changed_camera_metadata_prevents_comparison(telemetry, history):
    h, clock = history
    observe(h)
    forward(h)
    clock.step()
    assert observe(h, **telemetry)[-1]['motion_observation']['status'] == 'not_comparable'


def test_angle_tolerance_and_no_wheel_command(history):
    h, clock = history
    observe(h)
    clock.step()
    assert observe(h)[-1]['motion_observation']['status'] == 'no_wheel_command'
    h.command('forward', 0, 200, 90)
    clock.step()
    assert observe(h)[-1]['motion_observation']['status'] == 'no_wheel_command'
    forward(h)
    clock.step()
    assert observe(h, servo=93)[-1]['motion_observation']['status'] == 'possible_stall'


def test_pending_bound_keeps_view_change_guard(history):
    h, clock = history
    observe(h)
    h.command('tilt', 0, 100, 90)
    for index in range(12):
        forward(h)
    clock.step()
    frame = observe(h)[-1]
    assert len(frame['commands_since_previous']) == 8
    assert frame['motion_observation']['status'] == 'not_comparable'
    clock.step()
    forward(h)
    assert observe(h)[-1]['motion_observation']['status'] == 'possible_stall'


def test_source_change_and_stale_frames_clear_context(history):
    h, clock = history
    observe(h)
    forward(h)
    clock.step()
    frames = h.observe(jpeg(), {'servo': 90, 'light_on': False}, 'phone')
    assert len(frames) == 1
    assert frames[0]['commands_since_previous'] == []
    assert frames[0]['motion_observation']['status'] == 'not_comparable'
    forward(h)
    clock.step(121)
    assert len(h.observe(jpeg(), {'servo': 90, 'light_on': False}, 'phone')) == 1
    h.clear()
    assert len(observe(h)) == 1


def test_oldest_frame_expires_even_when_recent_frame_remains(history):
    h, clock = history
    observe(h)
    clock.step(100)
    observe(h)
    clock.step(21)
    assert [frame['at'] for frame in observe(h)] == [1100, 1121]


def test_invalid_image_is_safe_and_does_not_reuse_older_valid_pair(history):
    h, clock = history
    observe(h)
    forward(h)
    clock.step()
    frame = h.observe('data:image/jpeg;base64,not-base64!', {'servo': 90, 'light_on': False}, 'car')[-1]
    assert frame['motion_observation']['status'] == 'not_comparable'
    forward(h)
    clock.step()
    assert observe(h)[-1]['motion_observation']['status'] == 'not_comparable'


def test_stop_commands_do_not_accumulate(history):
    h, clock = history
    observe(h)
    for action in ['stop', 'release', 'estop']:
        h.command(action, 0, 100, 90)
    clock.step()
    assert observe(h)[-1]['commands_since_previous'] == []
