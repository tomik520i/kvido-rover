import math

import pytest

from server.sensor_navigator import SensorNavigator


def telemetry(distance=150,age=0,**extra):
    return dict(distance_cm=distance,telemetry_age_ms=age,motor_max_duration_ms=2000,forward_stop_cm=10,**extra)


def cover_local_footprint(nav):
    nav.grid.update({(x,y):-3 for x in range(-4,15) for y in range(-4,5)})


def test_only_explicit_accepted_completed_commands_integrate_once():
    nav=SensorNavigator()
    command=dict(command_id='x',action='forward',speed=180,duration_ms=1000,acceptance='accepted')
    nav.complete(command,telemetry(),telemetry(),dict(status='changed'))
    assert nav.x==pytest.approx(.35) and nav.y==0
    nav.complete(command,telemetry(),telemetry(),dict(status='changed'))
    assert nav.x==pytest.approx(.35)
    for index,acceptance in enumerate(('unknown','rejected',None)):
        nav.complete({**command,'command_id':str(index),'acceptance':acceptance},telemetry(),telemetry(),dict(status='changed'))
    assert nav.x==pytest.approx(.35)
    nav.complete({**command,'command_id':'stall'},telemetry(),telemetry(),dict(status='possible_stall'))
    assert nav.x==pytest.approx(.70)
    assert nav.snapshot()['last_integration']['verified_motion'] is False
    assert not nav.failures


def test_calibrated_rotation_and_strafe_are_estimates_not_external_position():
    nav=SensorNavigator()
    nav.complete(dict(action='rotate_left',speed=180,duration_ms=1000,acceptance='accepted'),telemetry(),telemetry(),dict(status='changed'))
    assert nav.heading==pytest.approx(1.2)
    nav.complete(dict(action='left',speed=180,duration_ms=1000,acceptance='accepted'),telemetry(),telemetry(),dict(status='changed'))
    assert nav.x==pytest.approx(.35*math.cos(1.2+math.pi/2))
    assert nav.y==pytest.approx(.35*math.sin(1.2+math.pi/2))
    assert 'not_ground_truth' in nav.snapshot()['localization']


@pytest.mark.parametrize('age',[None,True,-1,501,float('nan')])
def test_stale_telemetry_never_produces_motion(age):
    nav=SensorNavigator()
    assert nav.next_command(telemetry(age=age))['action']=='stop'


@pytest.mark.parametrize('distance',[None,True,0,10,15,20,float('nan')])
def test_null_or_short_clearance_never_generates_forward(distance):
    nav=SensorNavigator();nav.scan_remaining=0
    nav.target=(10,0)
    for i in range(15):nav.grid[(i,0)]=-3
    assert nav.next_command(telemetry(distance))['action']!='forward'


def test_frontier_is_derived_from_observed_free_cells_and_command_bounds():
    nav=SensorNavigator();nav.scan_remaining=0
    for i in range(16):
        nav.heading=i*math.pi/8;nav.observe(telemetry())
    nav.heading=0
    command=nav.next_command(telemetry())
    assert command['action'] in ('forward','left','right','backward','rotate_left','rotate_right')
    assert command['speed']<=180 and 50<=command['duration_ms']<=2000
    assert nav.target in nav.grid and nav.grid[nav.target]<0
    assert nav.next_command(telemetry(estop=True))['action']=='stop'


def test_early_front_guard_does_not_integrate_full_requested_pulse():
    nav=SensorNavigator()
    nav.complete(dict(action='forward',speed=180,duration_ms=2000,acceptance='accepted'),
        telemetry(30),telemetry(10,motor=dict(stop='sensor_near')),dict(status='changed'))
    assert nav.x==pytest.approx(.2)
    snap=nav.snapshot();snap['cells'].clear()
    assert nav.grid


def test_hidden_simulator_metadata_cannot_influence_controller():
    first=SensorNavigator();second=SensorNavigator()
    clean=telemetry()
    poisoned={**clean,'pose':dict(x=800,y=300,heading=2),'geometry':dict(walls='secret'),
              'goal_reached':True,'door_distance':0,'checkpoints':[True,True]}
    assert first.next_command(clean)==second.next_command(poisoned)
    assert first.snapshot()==second.snapshot()
    assert not set(first.snapshot())&{'goal_reached','checkpoints','geometry','door_distance'}


def test_grid_and_failure_memories_have_hard_bounds():
    nav=SensorNavigator()
    for i in range(13000):nav._mark((i%150-75,i//150-75),-1)
    assert len(nav.grid)==12000
    for i in range(300):nav.target=(i,0);nav._fail_target()
    assert len(nav.failed_targets)==256


def test_fully_observed_enclosed_room_does_not_reward_novel_interior():
    nav=SensorNavigator();nav.scan_remaining=0
    for x in range(-15,16):
        for y in range(-15,16):nav.grid[(x,y)]=-3
    # The enclosing boundary is not a reachable frontier after obstacle inflation.
    for x in range(-16,17):nav.grid[(x,-16)]=nav.grid[(x,16)]=5
    for y in range(-16,17):nav.grid[(-16,y)]=nav.grid[(16,y)]=5
    target=nav._choose_target()
    assert target is None


def test_endless_scanning_is_bounded_and_never_claims_semantic_success():
    nav=SensorNavigator();nav.pulses_without_translation=256
    command=nav.next_command(telemetry())
    assert command['action']=='stop' and 'no semantic goal claim' in command['reason']


def test_next_pulse_does_not_shortcut_a_valid_l_shaped_path(monkeypatch):
    nav=SensorNavigator();nav.scan_remaining=0;nav.target=(2,1)
    cover_local_footprint(nav)
    monkeypatch.setattr(nav,'_path',lambda target:[(0,0),(1,0),(1,1),(2,1)])
    command=nav.next_command(telemetry())
    # First edge is straight ahead; aiming at path[2] would turn across the corner.
    assert command['action']=='forward'
    assert command['duration_ms']==math.floor(.2/.35*1000)


def test_astar_diagonal_cannot_squeeze_between_inflated_corners(monkeypatch):
    nav=SensorNavigator()
    nav.grid={(0,0):-3,(1,0):-3,(0,1):-3,(1,1):-3}
    monkeypatch.setattr(nav,'_blocked',lambda cell:cell in ((1,0),(0,1)))
    assert nav._path((1,1)) is None


def test_unreachable_high_novelty_points_cannot_starve_a_reachable_viewpoint():
    nav=SensorNavigator();nav.scan_remaining=0
    # A small reachable corridor has modest novelty; 40 disconnected farther
    # points have higher scores and used to consume the entire top-24 shortlist.
    nav.grid={(i,0):-3 for i in range(6)}
    for i in range(40):nav.grid[(20+i,20)]=-3
    target=nav._choose_target()
    assert target is not None and target[1]==0 and 3<=target[0]<=5
    assert target in nav._reachable_cells()


def test_null_front_reorients_chassis_then_keeps_side_mode_on_observed_free_edge():
    nav=SensorNavigator();nav.scan_remaining=0;nav.target=(10,0)
    cover_local_footprint(nav)
    first=nav.next_command(telemetry(None))
    assert first['action'] in ('rotate_left','rotate_right')
    assert not first['scan']
    nav.complete({**first,'acceptance':'accepted'},telemetry(None),telemetry(None),dict(status='changed'))
    second=nav.next_command(telemetry(None))
    assert second['action'] in ('left','right')
    assert second['duration_ms']<=1000
    assert nav._travel_action==second['action']


def test_sideways_path_requires_observed_free_swept_segment(monkeypatch):
    nav=SensorNavigator();nav.scan_remaining=0;nav.target=(10,0)
    nav.grid={(0,0):-3,(10,0):-3}
    nav.heading=-math.pi/2;nav._travel_action='left'
    monkeypatch.setattr(nav,'_path',lambda target:[(0,0),(1,0),(10,0)])
    command=nav.next_command(telemetry(None))
    assert command['action']=='stop' and 'swept' in command['reason']


def test_sideways_or_backward_motion_never_requires_a_valid_front_echo():
    nav=SensorNavigator();nav.scan_remaining=0;nav.target=(10,0)
    cover_local_footprint(nav)
    nav.heading=math.pi
    command=nav.next_command(telemetry(None))
    assert command['action']=='backward' and command['duration_ms']<=1000


def test_free_centerline_does_not_certify_unknown_chassis_outline():
    nav=SensorNavigator();nav.grid={(i,0):-3 for i in range(8)}
    assert not nav._sweep_free(0,.2)
    assert nav._sweep_block_reason=='missing_coverage'
    cover_local_footprint(nav)
    assert nav._sweep_free(0,.2)
    nav.grid[(1,1)]=5
    assert not nav._sweep_free(0,.2) and nav._sweep_block_reason=='occupied'


def test_missing_coverage_interlaces_scans_without_marking_unknown_occupied():
    nav=SensorNavigator();nav.scan_remaining=0;nav.target=(10,0)
    nav.grid={(i,0):-3 for i in range(12)}
    assert nav.next_command(telemetry(None))['action'] in ('rotate_left','rotate_right')
    nav.heading=-math.pi/2;nav._travel_action='left'
    command=nav.next_command(telemetry(None))
    assert command['action']=='stop' and 'missing_coverage' in command['reason']
    assert nav.target is None and nav.scan_remaining==16
    assert not nav.failed_targets
    assert nav.next_command(telemetry(None))['reason'].startswith('Interlace')


def test_pixel_similarity_is_not_zero_rotation_and_does_not_invent_obstacle():
    nav=SensorNavigator()
    nav.complete(dict(action='rotate_left',speed=180,duration_ms=164,acceptance='accepted',scan=True),
                 telemetry(),telemetry(),dict(status='possible_stall'))
    assert nav.heading==pytest.approx(1.2*.164)
    assert nav.scan_remaining==15 and nav.uncertain_steps==1
    assert not nav.failures
    assert nav.last_integration['uncertain'] is True


@pytest.mark.parametrize('measured',[0,.12,.35])
def test_visual_odometry_length_overrides_prior_even_when_pixel_status_stalled(measured):
    nav=SensorNavigator()
    nav.complete(dict(action='forward',speed=180,duration_ms=1000,acceptance='accepted'),
                 telemetry(),telemetry(),dict(status='possible_stall',source='floor_visual_odometry',measured_translation_m=measured))
    assert nav.x==pytest.approx(measured)
    assert nav.last_integration['source']=='floor_visual_odometry'


@pytest.mark.parametrize('measured',[True,-.1,.40,float('nan')])
def test_invalid_visual_odometry_does_not_override_calibrated_prior(measured):
    nav=SensorNavigator()
    nav.complete(dict(action='left',speed=180,duration_ms=1000,acceptance='accepted'),
                 telemetry(),telemetry(),dict(status='possible_stall',source='floor_visual_odometry',measured_translation_m=measured))
    assert nav.y==pytest.approx(.35)
    assert nav.last_integration['source']=='calibrated_command_prior'


def test_single_range_never_clears_its_own_endpoint_and_deduplicates_cells():
    nav=SensorNavigator()
    nav.observe(telemetry(150))
    length=1.5+nav.RADIUS
    endpoints={nav._cell(length*math.cos(a),length*math.sin(a)) for a in (-.07,-.035,0,.035,.07)}
    assert all(nav.grid[cell]==3 for cell in endpoints)
    # Every ray crosses these same cells repeatedly; one observation is one vote.
    assert nav.grid[(2,0)]==-1
    assert all(value>=-4 for value in nav.grid.values())


def test_endpoint_remains_occupied_under_repeated_same_valid_range():
    nav=SensorNavigator()
    for seq in range(10):nav.observe(telemetry(150,ultra={'seq':seq}))
    assert nav.grid[nav._cell(1.65,0)]==7
    assert nav._blocked(nav._cell(1.65,0))


def test_information_gain_stops_at_observed_occupied_boundary():
    nav=SensorNavigator()
    for x in range(-3,4):
        for y in range(-3,4):nav.grid[(x,y)]=-3
    for x in range(-4,5):nav.grid[(x,-4)]=nav.grid[(x,4)]=5
    for y in range(-4,5):nav.grid[(-4,y)]=nav.grid[(4,y)]=5
    assert nav._information_gain((0,0))==0
    del nav.grid[(4,0)]
    assert nav._information_gain((0,0))>0


def test_information_gain_counts_unique_unknown_cells_not_ray_repetitions():
    nav=SensorNavigator()
    gain=nav._information_gain((0,0))
    assert 0<gain<32*20*nav.CELL**2


def test_uncovered_target_is_deferred_without_inventing_obstacle():
    nav=SensorNavigator();nav.scan_remaining=0;nav.target=(5,0)
    nav.grid.update({(i,0):-3 for i in range(7)})
    command=nav.next_command(telemetry())
    assert command['action']=='stop'
    assert nav.coverage_deferred[((0,0),(1,0))]['attempts']==1
    assert not nav.failed_targets and not nav.failures
    assert nav.snapshot()['coverage_deferred'][0]['attempts']==1


def test_missing_coverage_defers_first_edge_until_local_information_changes():
    nav=SensorNavigator()
    nav._defer_edge((1,0));nav._defer_edge((1,0))
    assert nav._edge_deferred((1,0))
    assert not nav._edge_deferred((0,1))
    nav.grid[(1,1)]=-1
    assert not nav._edge_deferred((1,0))


def test_public_command_path_observed_uses_full_calibrated_sweep_and_offsets():
    nav=SensorNavigator();cover_local_footprint(nav)
    assert nav.command_path_observed(dict(action='forward',speed=180,duration_ms=1000))
    del nav.grid[(0,4)]
    assert not nav.command_path_observed(dict(action='left',speed=180,duration_ms=2000))
    assert nav.command_path_observed(dict(action='rotate_left'))
    assert nav.command_path_observed(dict(action='stop'))
    assert not nav.command_path_observed(dict(action='forward',speed=True,duration_ms=1000))
    assert not nav.command_path_observed(dict(action='forward',speed=180,duration_ms=2001))
    nav.grid[(2,0)]=5
    assert not nav.command_path_observed(dict(action='forward',speed=180,duration_ms=1000))


def test_public_request_scan_preserves_pose_and_map_and_clears_travel_state():
    nav=SensorNavigator();nav.x=.3;nav.y=.4;nav.heading=.7
    nav.grid[(0,0)]=-3;nav.target=(4,5);nav._travel_action='right'
    nav.scan_remaining=0;nav._scan_offset_pending=True
    nav.request_scan('Semantic approach needs coverage.')
    assert (nav.x,nav.y,nav.heading)==(.3,.4,.7)
    assert nav.grid=={(0,0):-3}
    assert nav.scan_remaining==16 and nav.target is None and nav._travel_action is None
    assert not nav._scan_offset_pending
    assert nav.snapshot()['reason']=='Semantic approach needs coverage.'
