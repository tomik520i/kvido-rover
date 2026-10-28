"""Geometry and rendering checks without an API, model or hardware."""
import io
import math

from PIL import Image
import pytest

from server.simulator import RoverSimulator


class Clock:
    now=0.
    def __call__(self):return self.now


def colors(jpeg):
    pixels=Image.open(io.BytesIO(jpeg)).convert('RGB').getdata()
    blue=yellow=0
    for r,g,b in pixels:
        blue+=b>130 and b>r*1.5 and b>g*1.3
        yellow+=r>180 and g>140 and b<110
    return blue,yellow


def test_target_frame_and_threshold_are_occluded_by_apartment_partition():
    s=RoverSimulator(Clock());s.reset('apartment')
    # Aim directly at the target, so absence cannot be explained by camera FOV.
    s.heading=math.atan2(3-s.y,8-s.x)
    hidden=colors(s.camera_jpeg())
    assert hidden==(0,0)
    s.x,s.y,s.heading=6.3,3.,0.
    visible=colors(s.camera_jpeg())
    assert visible[0]>1000 and visible[1]>20


def test_foreground_box_occludes_lower_frame_and_entire_threshold():
    s=RoverSimulator(Clock())
    visible=colors(s.camera_jpeg())
    s.boxes=[dict(x=6.,y=1.,w=.5,h=4.,height=1.,color='#a65c38')]
    occluded=colors(s.camera_jpeg())
    assert occluded[1]==0
    assert occluded[0]<visible[0]  # high lintel can remain above low furniture


def test_wall_behind_low_box_still_hides_high_lintel():
    s=RoverSimulator(Clock())
    s.boxes=[dict(x=6.,y=1.,w=.4,h=4.,height=.3,color='#a65c38')]
    s.walls.append((7.,0,7.,6))
    assert colors(s.camera_jpeg())==(0,0)


def test_reference_route_has_clear_radius_and_required_turns():
    s=RoverSimulator(Clock());state=s.reset('apartment')
    route=s.reference_route
    assert state['pose']==dict(x=1.2,y=1.2,heading=0.)
    assert 9<=state['route_length']<=12
    assert state['route_length']==pytest.approx(sum(math.dist(a,b) for a,b in zip(route,route[1:])))
    for a,b in zip(route,route[1:]):
        for i in range(101):
            t=i/100
            assert not s._blocked(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t)
    assert len(state['geometry']['walls'])>=9
    assert len(state['geometry']['checkpoints'])==3


def test_actual_motion_records_ordered_checkpoints_distance_and_success():
    clock=Clock();s=RoverSimulator(clock);s.reset('apartment')
    route=s.reference_route
    for a,b in zip(route,route[1:]):
        # Exercise physics along an independently specified reference, not scripted AI.
        # Reverse travel avoids making route validation depend on sonar's finite range.
        s.heading=math.atan2(b[1]-a[1],b[0]-a[0])+math.pi
        remaining=math.dist(a,b)/s.MAX_SPEED
        while remaining>1e-8:
            dt=min(2.,remaining)
            s.command('backward',180,round(dt*1000))
            clock.now+=dt
            s.telemetry()
            remaining-=dt
    s.command('stop')
    state=s.snapshot()
    assert state['collisions']==0
    assert all(p['visited'] for p in state['checkpoints'])
    assert state['route_complete'] and state['goal_reached']
    assert not state['crossed_threshold']
    assert state['travelled']==pytest.approx(state['route_length'],abs=.005)
    assert not set(s.telemetry())&{'travelled','route_length','checkpoints','pose','route_complete'}


def test_teleport_to_door_does_not_count_as_valid_route():
    s=RoverSimulator(Clock());s.reset('apartment')
    s.x,s.y=7.75,3
    state=s.snapshot()
    assert state['at_door'] and not state['goal_reached']
    assert not state['route_complete']
    assert state['travelled']==0


def test_render_uses_only_column_rays_not_per_pixel_rays():
    s=RoverSimulator(Clock());s.reset('apartment')
    original=s._ray
    calls=0
    def counted(angle):
        nonlocal calls
        calls+=1
        return original(angle)
    s._ray=counted
    s.camera_jpeg()
    assert calls==320
