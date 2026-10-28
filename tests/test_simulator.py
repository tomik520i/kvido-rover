import io
import math

from PIL import Image, ImageStat
import pytest

from server.simulator import RoverSimulator


class Clock:
    def __init__(self):self.now=100.
    def __call__(self):return self.now
    def advance(self,dt):self.now+=dt


@pytest.fixture
def sim():
    clock=Clock()
    return RoverSimulator(clock),clock


def test_forward_integrates_real_time_and_stops_at_two_second_deadline(sim):
    s,c=sim
    start=s.snapshot()['pose']['x']
    result=s.command('forward',180,9999)
    assert result['accepted'] and result['telemetry']['drive']['ms']==2000
    c.advance(10)
    t=s.telemetry()
    assert s.snapshot()['pose']['x']==pytest.approx(start+.7)
    assert not t['motor']['active'] and t['motor']['deadline_stops']==1
    c.advance(10)
    assert s.snapshot()['pose']['x']==pytest.approx(start+.7)


def test_stop_and_estop_interrupt_without_replaying(sim):
    s,c=sim
    s.command('forward',180,2000);c.advance(.4)
    s.command('stop');pose=s.snapshot()['pose']
    c.advance(2)
    assert s.snapshot()['pose']==pose
    s.command('estop')
    assert not s.command('backward',180)['accepted']
    s.command('release')
    assert s.command('backward',180)['accepted']


@pytest.mark.parametrize('distance,reason',[(None,'sensor_null'),(10,'sensor_near'),(9,'sensor_near')])
def test_forward_sensor_guard_is_continuous_and_escape_ignores_front(sim,distance,reason):
    s,c=sim
    s.command('forward',180,2000);c.advance(.2);s.telemetry()
    s._distance=lambda:distance
    c.advance(.2)
    assert s.telemetry()['motor']['stop']==reason
    assert not s.command('forward',180)['accepted']
    for action in ('backward','left','right','rotate_left','rotate_right'):
        assert s.command(action,180)['accepted']


def test_turn_and_strafe_change_pose_and_ultrasonic(sim):
    s,c=sim
    distance=s.telemetry()['distance_cm']
    s.command('rotate_left',180,1000);c.advance(1)
    assert s.snapshot()['pose']['heading']==pytest.approx(1.2)
    assert s.telemetry()['distance_cm']!=distance
    s.command('right',180,1000);before=s.snapshot()['pose'];c.advance(1)
    after=s.snapshot()['pose']
    assert math.hypot(after['x']-before['x'],after['y']-before['y'])==pytest.approx(.35)


def test_collision_prevents_penetration_and_retreat_frees_rover(sim):
    s,c=sim;s.reset('stuck')
    s.command('forward',180,2000);c.advance(2)
    before=s.snapshot()
    assert before['collisions']==1
    assert before['pose']['x']<5.8
    assert s.telemetry()['drive']['accepted']==1
    s.command('backward',180,1000);c.advance(1)
    assert s.snapshot()['pose']['x']<before['pose']['x']-.2


def test_goal_requires_approach_and_never_threshold_crossing(sim):
    s,c=sim
    assert not s.snapshot()['goal_reached']
    for _ in range(3):s.command('forward',180,2000);c.advance(2);s.telemetry()
    assert s.snapshot()['goal_reached']
    s.command('forward',180,1000)
    assert not s.snapshot()['goal_reached']
    s.command('forward',180,2000);c.advance(2)
    assert s.snapshot()['crossed_threshold']
    assert not s.snapshot()['goal_reached']


def test_camera_is_deterministic_first_person_and_responds_to_pose_tilt_light(sim):
    s,c=sim
    initial=s.camera_jpeg()
    assert initial==s.camera_jpeg()
    image=Image.open(io.BytesIO(initial));assert image.size==(640,480)
    s.command('rotate_left',180,1000);c.advance(1)
    assert s.camera_jpeg()!=initial
    turned=s.camera_jpeg();s.command('tilt',tilt=55)
    assert s.camera_jpeg()!=turned
    s.reset('dark');dark=Image.open(io.BytesIO(s.camera_jpeg()))
    s.command('light_on');lit=Image.open(io.BytesIO(s.camera_jpeg()))
    assert sum(ImageStat.Stat(lit).mean)>5*sum(ImageStat.Stat(dark).mean)


def test_caps_counters_and_snapshot_are_isolated(sim):
    s,c=sim
    t=s.telemetry()
    assert t['motor_max_duration_ms']==2000 and t['forward_stop_cm']==10
    assert t['telemetry_age_ms']==0 and t['simulation']
    assert not set(t)&{'pose','geometry','path','door_distance','goal_reached','at_door'}
    s.command('forward',999,1000);c.advance(.1)
    assert s.telemetry()['motor']['speed']==180
    snap=s.snapshot();snap['geometry']['boxes'].clear();snap['path'].clear()
    assert s.snapshot()['geometry']['boxes'] and s.snapshot()['path']
    s.reset('doorway',42)
    assert s.snapshot()['seed']==42 and s.telemetry()['drive']['accepted']==0
    with pytest.raises(ValueError):s.reset('unknown')


def test_real_front_ray_stops_before_wall_without_contact(sim):
    s,c=sim
    s.x,s.y=7.,1.
    assert s.telemetry()['distance_cm']==85
    for _ in range(2):s.command('forward',180,2000);c.advance(2);s.telemetry()
    state=s.snapshot()
    assert state['pose']['x']<7.76
    assert state['collisions']==0
    assert state['telemetry']['motor']['near_stops']==1
    assert not state['telemetry']['motor']['active']
