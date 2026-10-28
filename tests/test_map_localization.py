"""Offline synthetic geometry only; production matching never receives truth poses."""
import json
import math

import numpy as np
import pytest

from server.rover_map import RoverMap,FREE,OCCUPIED,OCCUPIED_CELL,BLOCKED_CELL
from server.scan_matching import align_scan,obstacle_points


def corner():
    # Perpendicular walls rotated 45 degrees, both visible within the real camera FOV.
    return {'directions':[{'bearing_deg':a,'status':'obstacle',
        'distance_cm':65/max(math.cos(math.radians(a+45)),math.cos(math.radians(a-45)))}
        for a in range(-40,41,4)]}


def flat_wall():
    return {'directions':[{'bearing_deg':a,'status':'obstacle','distance_cm':90/math.cos(math.radians(a))}
                          for a in range(-35,36,5)]}


def test_injected_drift_corrected_and_repeated_corner_does_not_creep():
    m=RoverMap();m.observe(corner())
    assert m.localization['reason']=='insufficient_prior_map'
    m.x,m.y,m.heading=5.,-5.,4.
    before=math.hypot(m.x,m.y)
    m.observe(corner())
    assert m.localization['status']=='aligned'
    assert math.hypot(m.x,m.y)<before*.25
    assert min(m.heading,360-m.heading)<=1
    assert m.localization['confidence']>=.55
    assert m.navigation_context()['reliable'] is True
    corrected=(m.x,m.y,m.heading)
    for _ in range(20):m.observe(corner())
    assert np.allclose((m.x,m.y,m.heading),corrected,atol=.01)
    assert len(m._observations)<=16
    json.dumps(m.view(),allow_nan=False)


def test_motion_then_injected_drift_reduces_error_only_with_strong_features():
    m=RoverMap();m.observe(corner());m.x,m.y,m.heading=3.75,-3.75,2.
    m.observe(corner())
    assert m.localization['status']=='aligned'
    assert math.hypot(m.x,m.y)<=1.8 and min(m.heading,360-m.heading)<=1


@pytest.mark.parametrize('scan',[
    {'directions':[]},
    {'directions':corner()['directions'][:3]},
    {'directions':[dict(r,bearing_deg=r['bearing_deg'],status='open') for r in corner()['directions']]},
    {'directions':[dict(r,status='unknown') for r in corner()['directions']]},
    {'directions':[dict(r,status='near',distance_cm=12) for r in corner()['directions']]},
    {'directions':[dict(r,distance_cm=20) for r in corner()['directions']]},
])
def test_sparse_open_unknown_or_near_scans_never_move_pose(scan):
    m=RoverMap();m.observe(corner());m.x,m.y,m.heading=5.,-5.,4.
    pose=(m.x,m.y,m.heading);m.observe(scan)
    assert (m.x,m.y,m.heading)==pose
    assert m.localization['status']=='rejected'
    assert m.navigation_context()['source']=='local_odometry'


def test_straight_wall_tangential_ambiguity_cannot_teleport_repeatedly():
    m=RoverMap();m.observe(flat_wall());m.x,m.y,m.heading=0.,7.5,0.
    for _ in range(30):
        m.observe(flat_wall())
        assert (m.x,m.y,m.heading)==(0.,7.5,0.)
        assert m.localization['reason']=='straight_wall_ambiguity'


def test_competing_local_modes_are_rejected_without_forcing_merge():
    m=RoverMap();scan=corner()['directions']
    for x,y,_ in obstacle_points(scan,120):
        for offset in (-5,5):
            c=m._cell(x+offset,y)
            if c:m.odds[c]=2
    before=m.odds.copy();diagnostic=align_scan(m.odds,scan,(0.,0.,0.),120)
    assert diagnostic['status']=='rejected'
    assert diagnostic['reason'] in ('competing_modes_or_weak_constraint','weak_match_confidence')
    assert np.array_equal(before,m.odds)
    assert diagnostic['correction']=={'dx_cm':0.,'dy_cm':0.,'dheading_deg':0.}


def test_huge_correction_and_low_overlap_leave_odometry_unchanged():
    m=RoverMap();m.observe(corner());m.x=40
    pose=(m.x,m.y,m.heading);m.observe(corner())
    assert (m.x,m.y,m.heading)==pose and m.localization['status']=='rejected'
    assert m.localization['reason'] in ('low_overlap','correction_at_search_limit')
    unrelated={'directions':[dict(r,distance_cm=35) for r in corner()['directions']]}
    pose=(m.x,m.y,m.heading);m.observe(unrelated)
    assert (m.x,m.y,m.heading)==pose and m.localization['status']=='rejected'


def test_observation_casts_each_cell_once_and_hits_override_free():
    m=RoverMap()
    duplicates=[{'bearing_deg':0,'status':'obstacle','distance_cm':30}]*80
    m.observe({'directions':duplicates+[{'bearing_deg':0,'status':'open','distance_cm':80}]})
    assert math.isclose(float(m.odds[m._cell(30,0)]),OCCUPIED,rel_tol=1e-6)
    assert math.isclose(float(m.odds[m._cell(20,0)]),FREE,rel_tol=1e-6)
    n=RoverMap();n.observe({'directions':[{'bearing_deg':0,'status':'open','distance_cm':80},
                                       {'bearing_deg':0,'status':'obstacle','distance_cm':30}]})
    assert math.isclose(float(n.odds[n._cell(30,0)]),OCCUPIED,rel_tol=1e-6)


def test_visited_footprint_never_hides_or_erases_obstacle_or_blocked():
    m=RoverMap();occupied=m._cell(5,0);blocked=m._cell(0,5)
    m.odds[occupied]=2;m.blocked[blocked]=True;m._visit()
    assert m.odds[occupied]==2
    assert m.grid()[occupied]==OCCUPIED_CELL
    assert m.grid()[blocked]==BLOCKED_CELL


def test_navigation_unknown_is_not_clearance_or_novelty_and_bearings_are_relative():
    m=RoverMap();context=m.navigation_context()
    assert [d['bearing_deg'] for d in context['directions']]==list(range(-180,180,15))
    forward=next(d for d in context['directions'] if d['bearing_deg']==0)
    assert forward['clearance_cm']==0 and forward['known_fraction']<1
    assert forward['novelty']==0 and context['source']=='local_odometry'
    assert context['confidence']<.4
    m.odds[:]=-1;m.heading=90
    # Off-centre obstacle inside the swept footprint must still block forward.
    m.odds[m._cell(-7,30)]=2
    values={d['bearing_deg']:d for d in m.navigation_context()['directions']}
    assert values[0]['blocked'] is True and values[0]['clearance_cm']<30
    assert values[90]['blocked'] is False and values[90]['known_fraction']==1
    assert values[90]['clearance_cm']<=80


def test_history_and_trail_are_bounded_even_outside_grid():
    m=RoverMap()
    for _ in range(700):m.move([(180,1,'forward')])
    assert len(m.trail)<=600
    for _ in range(40):m.observe({'directions':[]})
    assert len(m._observations)<=16
    json.dumps(m.view(),allow_nan=False)


def test_bounded_odometry_can_describe_known_short_corridor_without_match():
    m=RoverMap();m.odds[:]=-1
    context=m.navigation_context();forward=context['directions'][12]
    assert context['reliable'] is True and context['source']=='local_odometry'
    assert context['uncertainty_kind']=='heuristic_not_calibrated_accuracy'
    assert forward['known_fraction']==1 and forward['clearance_cm']>=40
    m.move([(180,10,'forward')])
    assert m.navigation_context()['reliable'] is False


def test_executed_swept_path_marks_intermediate_cells_but_preserves_obstacles():
    m=RoverMap();contact=m._cell(25,0);m.odds[contact]=2
    m.move([(180,10,'forward')])
    for x in (10,20,30,40,50):assert m.visited[m._cell(x,0)]
    assert m.odds[contact]==2 and m.grid()[contact]==OCCUPIED_CELL
    stalled=RoverMap();stalled.move([(180,10,'forward')],stalled=True)
    assert not stalled.visited[stalled._cell(30,0)]


def test_rejected_scan_cannot_insert_geometry_and_confirm_itself():
    m=RoverMap();m.observe(corner());prior=m.odds.copy();m.x=40.
    for _ in range(8):
        m.observe(corner())
        assert m.localization['status']=='rejected'
        assert m.localization['fusion']=='withheld_ambiguous_pose'
        assert np.array_equal(m.odds,prior)
        assert (m.x,m.y,m.heading)==(40.,0.,0.)


def test_noisy_nearly_planar_surface_is_not_a_localization_anchor():
    # Variance ratio around .04 resembles the rejected real wall captures.
    points=[(90+9*math.sin(i*2.1),y) for i,y in enumerate(range(-50,51,5))]
    rays=[{'bearing_deg':math.degrees(math.atan2(y,x)), 'distance_cm':math.hypot(x,y),
           'status':'obstacle'} for x,y in points]
    m=RoverMap();m.observe({'directions':rays})
    assert m.localization['reason']=='straight_wall_ambiguity'
