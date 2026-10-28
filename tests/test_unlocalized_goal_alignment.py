"""A model floor claim without pixel support may align but never translate."""
import pytest
from server.semantic_target import resolve_semantic_target


@pytest.mark.parametrize('y,at_threshold',[(.54,False),(.9,True)])
def test_centered_ungrounded_floor_never_drives_or_finishes(y,at_threshold):
    p=dict(goal_visible=True,confidence=.9,bearing='ahead',target_pixel=dict(x=.5,y=y),
        threshold_visible=True,threshold_pixel=y,at_threshold=at_threshold,hazard='clear')
    t=dict(simulation=True,connected=True,telemetry_age_ms=0,servo=55,distance_cm=200)
    result=resolve_semantic_target(p,t,floor_localized=False)
    assert result['action']=='stop' and result['goal_handoff_pending']
    assert not result['candidate_goal_reached'] and result['estimated_range_m'] is None
    assert p['threshold_visible'] is True  # input claim remains unchanged


def test_ungrounded_floor_alignment_keeps_concrete_hazard_stop():
    p=dict(goal_visible=True,confidence=.9,target_pixel=dict(x=.8,y=.6),hazard='person')
    result=resolve_semantic_target(p,{},floor_localized=False)
    assert result['action']=='stop'
