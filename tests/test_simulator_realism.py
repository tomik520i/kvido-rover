import math

import pytest

from server.simulator import RoverSimulator
from server.simulator_realism import RealisticRoverSimulator


class Clock:
    def __init__(self):self.now=100.
    def __call__(self):return self.now
    def advance(self,dt):self.now+=dt


def make(seed=1,**realism):
    clock=Clock();s=RealisticRoverSimulator(clock,realism);s.reset('apartment',seed)
    return s,clock


def test_ideal_simulator_sonar_is_deterministic():
    clock=Clock();ideal=RoverSimulator(clock);ideal.reset('apartment')
    assert [ideal.telemetry()['distance_cm'] for _ in range(20)]==[ideal.telemetry()['distance_cm']]*20


def facing_wall(s,distance_m):
    s.walls,s.boxes=[(0,0,10,0)],[]
    s.x,s.y,s.heading=1.,distance_m+s.RADIUS,-math.pi/2


def test_near_sonar_drops_samples_and_adds_noise_but_stays_near_truth():
    s,_=make(sonar_dropout=.3);facing_wall(s,.35)
    samples=[s.telemetry()['distance_cm'] for _ in range(400)]
    numbers=[d for d in samples if d is not None]
    assert .2<1-len(numbers)/len(samples)<.4
    assert len(set(numbers))>1
    assert abs(sum(numbers)/len(numbers)-35)<2


def test_far_sonar_is_mostly_missing_and_scattered():
    s,_=make(sonar_dropout=.3,sonar_reliable_m=.5);facing_wall(s,1.8)
    numbers=[d for d in (s.telemetry()['distance_cm'] for _ in range(400)) if d is not None]
    assert len(numbers)<400*.3
    assert max(numbers)-min(numbers)>60


def test_grazing_wall_returns_no_echo():
    s,_=make(sonar_dropout=0);facing_wall(s,.35)
    # Facing along the wall at a shallow angle: every ray in the cone grazes it.
    s.heading=-.2
    assert s.telemetry()['distance_cm'] is None
    s.heading=-math.pi/2
    assert s.telemetry()['distance_cm']==pytest.approx(35,abs=4)


def test_same_seed_is_reproducible_and_seeds_differ():
    a,ca=make(seed=5);b,cb=make(seed=5);c,cc=make(seed=6)
    for s,clock in ((a,ca),(b,cb),(c,cc)):
        s.command('rotate_left',180,1000);clock.advance(1.1);s.telemetry()
    assert a.heading==b.heading!=c.heading


@pytest.mark.parametrize('action,ideal',[('rotate_left',1.2),('right',.35)])
def test_pulses_slip_relative_to_ideal_command_prior(action,ideal):
    moved=[]
    for seed in range(1,9):
        s,clock=make(seed=seed,sonar_dropout=0)
        heading,x,y=s.heading,s.x,s.y
        assert s.command(action,180,1000)['accepted']
        clock.advance(1.1);s.telemetry()
        moved.append(abs((s.heading-heading+math.pi)%(2*math.pi)-math.pi) if action.startswith('rotate') else math.hypot(s.x-x,s.y-y))
    assert all(m<ideal*1.4 for m in moved)
    assert sum(moved)/len(moved)<ideal*.95
    assert max(moved)-min(moved)>ideal*.05


def test_forward_guard_still_rejects_null_sonar():
    s,_=make(sonar_dropout=1)
    result=s.command('forward',180,1000)
    assert not result['accepted'] and result['reason']=='sensor_null'
