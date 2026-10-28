from copy import deepcopy
import pytest
from server.autopilot_core import decide,plan_step,panorama_pick,note_motion


def radar(values=None):
    values=values or [120]*9
    return {'directions':[dict(bearing_deg=40-i*10,status='unknown' if d is None else 'obstacle',distance_cm=d) for i,d in enumerate(values)]}

def seen(**kw):return {**dict(target_visible=False,confidence='high',animal_or_person_close=False,path_ahead_clear=False),**kw}
def context(heading=0):
    return dict(heading=heading,reliable=True,position_uncertainty_cm=5,directions=[dict(bearing_deg=b,known_fraction=1.,visited_fraction=1.,blocked=False,clearance_cm=50,novelty=0.) for b in range(-180,180,15)])
def direction(c,b):return next(e for e in c['directions'] if e['bearing_deg']==b)

def test_map_biases_revisit_toward_currently_observed_less_visited_corridor():
    c=context();direction(c,30).update(visited_fraction=0,novelty=1)
    old=decide(radar(),100,seen(),{})
    memory={};new=decide(radar(),100,seen(),memory,map_context=c)
    assert old[0][0][2]=='forward' and new[0][0][2]=='rotate_left'
    assert memory['map_decision']['source']=='map_corridor'


def test_known_blocked_approach_prevents_forward_without_inventing_free_space():
    c=context();direction(c,0).update(blocked=True,clearance_cm=20)
    actions,_,_=decide(radar(),100,seen(),{},map_context=c)
    assert not any(a[2]=='forward' for a in actions)


@pytest.mark.parametrize('mutation',[lambda c:c.update(reliable=False),lambda c:c.update(position_uncertainty_cm=100),lambda c:[e.update(known_fraction=0,clearance_cm=0,novelty=0) for e in c['directions']]])
def test_unreliable_or_unknown_map_preserves_existing_decision(mutation):
    c=context();mutation(c)
    assert decide(radar(),100,seen(),{},map_context=c)==decide(radar(),100,seen(),{})


def test_fresh_target_retains_priority_over_exploration_bonus():
    c=context();direction(c,30).update(visited_fraction=0,novelty=1)
    view=dict(target_visible=True,target_x=.5,target_floor_y=None,confidence='high',animal_or_person_close=False,path_ahead_clear=True)
    assert decide(radar(),100,view,{},map_context=c)==decide(radar(),100,view,{})


@pytest.mark.parametrize('sonar,view,scan',[(20,seen(),radar()),(None,seen(),radar()),(100,seen(animal_or_person_close=True),radar()),(100,dict(seen(),threshold_ahead=True,threshold_floor_y=.95),radar()),(100,seen(),radar([None]*9))])
def test_map_never_authorizes_forward_denied_by_current_observation(sonar,view,scan):
    c=context();direction(c,30).update(visited_fraction=0,novelty=1)
    actions,_,_=decide(scan,sonar,view,{},map_context=c)
    assert not any(a[2]=='forward' for a in actions)


def test_panorama_converts_world_heading_to_current_relative_bearing():
    c=context(heading=90);direction(c,30).update(visited_fraction=0,novelty=1)
    entries=[dict(heading=90,free=100,target=False,passage=False),dict(heading=120,free=100,target=False,passage=False)]
    memory={};best,_=panorama_pick(entries,map_context=c,memory=memory)
    assert best is entries[1] and memory['map_decision']['bearing_deg']==30
    direction(c,30).update(blocked=True,clearance_cm=20)
    assert panorama_pick(entries,map_context=c)[0] is entries[0]


def test_map_bonus_cannot_make_unknown_panorama_view_drivable():
    c=context();direction(c,30).update(visited_fraction=0,novelty=1)
    entries=[dict(heading=0,free=80,target=False,passage=False),dict(heading=30,free=0,target=False,passage=False)]
    assert panorama_pick(entries,map_context=c)[0] is entries[0]


def test_distinct_heading_required_to_advance_panorama_and_retries_are_bounded():
    memory={};plan_step(radar(),100,seen(),memory)
    left=memory['scan']['left'];count=len(memory['scan']['entries'])
    for _ in range(7):
        actions,_,done=plan_step(radar(),100,seen(),memory)
        assert actions==[(180,1,'rotate_right')] and not done
        assert memory['scan']['left']==left and len(memory['scan']['entries'])==count
    assert plan_step(radar(),100,seen(),memory)[2] is True
    note_motion(memory,[(180,1,'rotate_right')])
    _,_,done=plan_step(radar(),100,seen(),memory)
    assert not done and memory['scan']['left']==left-1
    assert len(memory['scan']['entries'])==count+1


def test_repeated_planning_does_not_apply_motion_or_change_map_context():
    c=context();direction(c,30).update(visited_fraction=0,novelty=1);original=deepcopy(c)
    memory={'heading':50}
    first=decide(radar(),100,seen(),memory,map_context=c)
    second=decide(radar(),100,seen(),memory,map_context=c)
    assert first==second and memory['heading']==50 and c==original


def test_distant_mapped_wall_preserves_measured_short_forward():
    c=context();direction(c,0).update(blocked=True,clearance_cm=50)
    assert decide(radar(),80,seen(),{},map_context=c)[0][0][2]=='forward'


def test_unreliable_next_context_clears_stale_map_hint():
    c=context();direction(c,30).update(visited_fraction=0,novelty=1)
    memory={};decide(radar(),100,seen(),memory,map_context=c)
    assert memory['map_decision']['source']=='map_corridor'
    decide(radar(),100,seen(),memory,map_context=None)
    assert memory['map_decision']['source']=='current_observation'
    assert memory['map_decision']['bearing_deg'] is None


def test_fresh_visible_target_interrupts_scan_even_without_confirmed_rotation():
    memory={};plan_step(radar(),100,seen(),memory)
    view=dict(seen(),target_visible=True,target_x=.5,target_floor_y=None)
    actions,_,_=plan_step(radar(),100,view,memory)
    assert actions[0][2]=='forward' and 'scan' not in memory
