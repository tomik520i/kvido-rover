import math
import pytest
from server.sensor_navigator import SensorNavigator
from server.goal_approach import GoalApproachNavigator


def setup():
    nav=SensorNavigator();nav.scan_remaining=0
    nav.grid.update({(x,y):-3 for x in range(-8,23) for y in range(-10,11)})
    return nav,GoalApproachNavigator(nav)

def telemetry(distance=150):return dict(distance_cm=distance,telemetry_age_ms=0,servo=55,motor_max_duration_ms=2000)
def target(x=2,y=0,**extra):return dict(point=dict(x=x,y=y),uncertainty_m=.05,usable=True,**extra)

def test_known_free_approach_returns_short_bounded_command_not_completion():
    nav,approach=setup();cmd=approach.next_command(target(),telemetry())
    assert cmd['action']=='forward' and cmd['speed']==180 and 1<=cmd['duration_ms']<=1000
    assert nav.command_path_observed(cmd)
    assert cmd['goal_completed'] is False and cmd['approach_status']=='approaching'
    assert nav.target is None  # adapter does not replace exploration's state


def test_null_front_uses_reorientation_then_observed_lateral_segment_never_forward():
    nav,approach=setup()
    for index in range(8):
        cmd=approach.next_command(target(),telemetry(None))
        assert cmd['action']!='forward'
        if cmd['action'] in ('left','right','backward'):
            assert nav.command_path_observed(cmd)
            break
        assert cmd['action'].startswith('rotate_') and cmd['duration_ms']<=500
        nav.complete({**cmd,'acceptance':'accepted'},telemetry(None),telemetry(None),{'status':'changed'})
    else:pytest.fail('Never produced an observed lateral alternative')


def test_standoff_stops_and_requests_current_evidence_not_goal_completion():
    nav,approach=setup();nav.x=1.55
    cmd=approach.next_command(target(),telemetry())
    assert cmd['action']=='stop' and cmd['approach_status']=='reacquire'
    assert cmd['goal_completed'] is False


@pytest.mark.parametrize('invalid',[None,{'usable':False},dict(point={'x':True,'y':0},uncertainty_m=.1,usable=True),dict(point={'x':2,'y':0},uncertainty_m=.61,usable=True)])
def test_invalid_or_expired_point_never_falls_back_to_exploration(invalid):
    nav,approach=setup();cmd=approach.next_command(invalid,telemetry())
    assert cmd['action']=='stop' and cmd['approach_status']=='unavailable'


def test_stale_or_emergency_telemetry_never_moves():
    nav,approach=setup()
    for t in ({**telemetry(),'telemetry_age_ms':501},{**telemetry(),'estop':True}):
        assert approach.next_command(target(),t)['action']=='stop'


def test_disconnected_known_map_returns_no_observed_route_not_unknown_shortcut():
    nav,approach=setup();nav.grid={(0,0):-3}
    cmd=approach.next_command(target(),telemetry())
    assert cmd['action']=='stop' and cmd['approach_status']=='no_observed_route'


def test_free_centerline_without_body_coverage_requires_scan():
    nav,approach=setup();nav.grid={(x,0):-3 for x in range(12)}
    cmd=approach.next_command(target(),telemetry())
    assert cmd['action']=='stop' and cmd['approach_status']=='scan_required'


def test_known_obstacle_routes_around_it_instead_of_sweeping_through_corner():
    nav,approach=setup()
    for y in range(-2,3):nav.grid[(5,y)]=5
    cmd=approach.next_command(target(3),telemetry())
    assert cmd['action']!='stop'
    if cmd['action'] in ('forward','left','right','backward'):
        assert nav.command_path_observed(cmd)
    assert cmd['goal_completed'] is False


def test_protected_point_is_not_crossed_even_with_long_available_capability():
    nav,approach=setup();nav.x=1.2
    cmd=approach.next_command(target(),telemetry())
    assert cmd['action'] in ('forward','stop')
    if cmd['action']=='forward':
        length=.35*cmd['duration_ms']/1000
        assert nav.x+length<2-.35


def test_hidden_world_fields_do_not_change_approach():
    nav,a=setup();nav2,b=setup()
    clean=target()
    assert a.next_command(clean,telemetry())==b.next_command({**clean,'geometry':'hidden','door_position':{'x':500}},telemetry())


def test_repeated_missing_first_edge_is_deferred_and_does_not_scan_forever():
    nav,approach=setup();nav.grid={(x,0):-3 for x in range(12)}
    first=approach.next_command(target(),telemetry())
    second=approach.next_command(target(),telemetry())
    third=approach.next_command(target(),telemetry())
    assert first['approach_status']==second['approach_status']=='scan_required'
    assert first['scan_offset_requested'] is False
    assert second['scan_offset_requested'] is True
    assert nav._edge_deferred((1,0))
    assert third['approach_status']=='no_observed_route'
    assert not nav.failed_targets  # missing observations are not physical obstacles


def test_occupied_swept_edge_is_excluded_without_rewriting_map():
    nav,approach=setup();original=dict(nav.grid)
    real_sweep=nav._sweep_free
    def occupied(angle,travel):
        nav._sweep_block_reason='occupied'
        return False
    nav._sweep_free=occupied
    first=approach.next_command(target(),telemetry())
    assert first['approach_status']=='scan_required'
    assert approach._excluded_edges
    assert nav.grid==original
    nav._sweep_free=real_sweep
    # Same route's first edge stays excluded even though an unrelated candidate
    # endpoint has a different label/cell; no invented occupied-cell mutation.
    key=next(iter(approach._excluded_edges))
    assert key[0]==nav._cell(nav.x,nav.y)
    assert not nav.failed_targets


def test_target_jitter_during_completed_rotations_keeps_first_segment_and_then_translates():
    nav,approach=setup();nav.heading=.75
    real_path=nav._path;calls=[]
    def counted(cell):
        calls.append(cell);return real_path(cell)
    nav._path=counted
    edges=[];actions=[];path_calls_after_first=None
    for step in range(6):
        cmd=approach.next_command(target(2,0 if step%2==0 else .268),telemetry())
        actions.append(cmd['action']);edges.append(cmd.get('planned_edge'))
        assert cmd.get('planned_body_action')=='forward'
        if path_calls_after_first is None:path_calls_after_first=len(calls)
        else:assert len(calls)==path_calls_after_first
        if cmd['action']=='forward':break
        assert cmd['action']=='rotate_right'
        nav.complete({**cmd,'acceptance':'accepted'},telemetry(),telemetry(),{'status':'changed'})
    assert actions[-1]=='forward'
    assert edges and all(edge==edges[0] for edge in edges)


def test_persistent_segment_is_cancelled_when_new_occupied_sweep_appears():
    nav,approach=setup();nav.heading=.75
    first=approach.next_command(target(),telemetry())
    assert first['action']=='rotate_right'
    edge=first['planned_edge']['next_cell']
    nav.grid[tuple(edge)]=5
    second=approach.next_command(target(),telemetry())
    assert second.get('planned_edge')!=first['planned_edge'] or second['action']=='stop'


def test_completed_translation_releases_segment_for_next_route_plan():
    nav,approach=setup();first=approach.next_command(target(),telemetry())
    origin=first['planned_edge']['origin']
    nav.complete({**first,'acceptance':'accepted'},telemetry(),telemetry(),{'status':'changed'})
    second=approach.next_command(target(),telemetry())
    assert second.get('planned_edge',{}).get('origin')!=origin
