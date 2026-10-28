"""Observer gates accept every legal passage, not one reference trajectory."""
import math

import pytest

from server.simulator import RoverSimulator


class Clock:
    now=0.
    def __call__(self):return self.now


def test_different_legal_route_outside_original_waypoint_circles_succeeds():
    clock=Clock();s=RoverSimulator(clock);s.reset('apartment')
    route=[(1.2,1.2),(2.4,1.2),(2.4,5.),(3.9,5.),(3.9,2.8),
           (4.9,2.8),(4.9,.8),(6.3,.8),(7.4,3.),(7.75,3.)]
    for a,b in zip(route,route[1:]):
        for i in range(101):
            x=a[0]+(b[0]-a[0])*i/100;y=a[1]+(b[1]-a[1])*i/100
            assert not s._blocked(x,y)
            assert all(math.hypot(x-cx,y-cy)>.5 for cx,cy in [(3.35,4.),(5.95,1.9)])
        s.heading=math.atan2(b[1]-a[1],b[0]-a[0])+math.pi
        remaining=math.dist(a,b)/s.MAX_SPEED
        while remaining>1e-8:
            dt=min(2.,remaining)
            s.command('backward',180,round(dt*1000));clock.now+=dt;s.telemetry()
            remaining-=dt
    s.command('stop')
    state=s.snapshot()
    assert state['collisions']==0
    assert state['route_complete'] and state['goal_reached']
    assert all(p['visited'] for p in state['checkpoints'])


@pytest.mark.parametrize('y',[3.5,3.65,5.85,5.95])
def test_first_gate_rejects_near_wall_crossings(y):
    s=RoverSimulator(Clock());s.reset('apartment')
    s._visit_checkpoints(2.9,y,3.1,y)
    assert not s.checkpoints[0]['visited']


def test_gates_require_order_direction_and_interpolated_crossing_height():
    s=RoverSimulator(Clock());s.reset('apartment')
    s._visit_checkpoints(5.5,1.,5.7,1.)
    assert not any(p['visited'] for p in s.checkpoints)
    s._visit_checkpoints(3.1,5.,2.9,5.)
    assert not s.checkpoints[0]['visited']
    s._visit_checkpoints(2.9,5.,3.1,5.)
    assert s.checkpoints[0]['visited']
    for y in (.1,.15,2.45,2.5):
        s._visit_checkpoints(5.5,y,5.7,y)
        assert not s.checkpoints[1]['visited']
    s._visit_checkpoints(5.5,.6,5.7,1.)
    assert s.checkpoints[1]['visited']
    assert s.snapshot()['geometry']['checkpoints'][1]['gate']==dict(x=5.6,ymin=.15,ymax=2.45)
