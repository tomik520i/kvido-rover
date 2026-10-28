import math
import pytest
from server.visual_target_track import VisualTargetTrack

class Clock:
    now=0
    def __call__(self):return self.now

def telemetry(**extra):return dict(telemetry_age_ms=0,servo=90,connected=True,**extra)
def pose(x=0,y=0,heading=0):return dict(x=x,y=y,heading=heading)
def perception(x=.5,y=.75):return dict(goal_visible=True,threshold_visible=True,confidence=.9,target_pixel=dict(x=x,y=y))
def metadata():return dict(status='localized',provenance='simulated_colored_threshold_current_pixels',quality=.9)
def move(action='forward',duration=1000):return dict(action=action,speed=180,duration_ms=duration,acceptance='accepted')

def test_current_floor_projection_uses_arbitrary_pose_and_vertical_servo():
    track=VisualTargetTrack();s=track.update(perception(),metadata(),telemetry(),pose(2,3,math.pi/2))
    assert s['point']['x']==pytest.approx(2)
    assert s['point']['y']==pytest.approx(3+.22*280/120)
    assert s['goal_completed'] is False
    assert s['uncertainty_m']>0


def test_negative_or_unlocalized_current_frame_preserves_protection_and_returns_clone():
    track=VisualTargetTrack();before=track.update(perception(),metadata(),telemetry(),pose())
    after=track.update({'goal_visible':False},{},telemetry(),pose())
    assert after['point']==before['point'] and after['uncertainty_m']==before['uncertainty_m']
    assert after['current_localized'] is False and before['current_localized'] is True
    after['point']['x']=999
    assert track.snapshot()['point']['x']!=999
    after=track.update(perception(),{'status':'current_marker_not_localized'},telemetry(),pose())
    assert after['point']==before['point'] and after['uncertainty_m']==before['uncertainty_m']


@pytest.mark.parametrize('value',[True,float('nan'),-.1,1.1,None])
def test_invalid_pixels_never_create_track(value):
    track=VisualTargetTrack()
    assert track.update(perception(value),metadata(),telemetry(),pose())['point'] is None


def test_stale_servo_and_above_horizon_cannot_create_target():
    track=VisualTargetTrack()
    t=telemetry();t['telemetry_age_ms']=501
    assert track.update(perception(),metadata(),t,pose())['point'] is None
    assert track.update(perception(y=.4),metadata(),telemetry(),pose())['point'] is None


def test_swept_guard_blocks_crossing_not_only_end_position_and_allows_other_direction():
    track=VisualTargetTrack();track.update(perception(),metadata(),telemetry(),pose())
    assert not track.command_allowed(move(duration=2000),pose())
    assert track.command_allowed(move('backward'),pose())
    assert track.command_allowed(move('left'),pose())
    assert track.command_allowed(dict(action='rotate_left'),pose())
    assert track.command_allowed(dict(action='stop'),pose())


def test_expired_point_fails_closed_for_translation_without_erasing_memory():
    clock=Clock();track=VisualTargetTrack(clock,max_age_s=10)
    track.update(perception(),metadata(),telemetry(),pose())
    clock.now=11
    assert not track.snapshot()['usable'] and track.snapshot()['point']
    assert not track.command_allowed(move('backward'),pose())
    assert track.suggest_orientation(pose(),telemetry())['action']=='stop'


def test_reacquire_only_turns_or_tilts_never_drives_or_completes_from_memory():
    track=VisualTargetTrack();track.update(perception(.2),metadata(),telemetry(),pose())
    assert track.suggest_orientation(pose(),telemetry())['action']=='tilt'
    t=telemetry();t['servo']=55
    cmd=track.suggest_orientation(pose(),t)
    assert cmd['action']=='rotate_left' and 100<=cmd['duration_ms']<=500
    assert track.snapshot()['goal_completed'] is False


def test_commands_increase_uncertainty_without_changing_point_or_claiming_motion():
    track=VisualTargetTrack();before=track.update(perception(),metadata(),telemetry(),pose())
    track.note_command(move())
    after=track.snapshot()
    assert after['point']==before['point'] and after['uncertainty_m']>before['uncertainty_m']
    track.note_command({**move(),'acceptance':'unknown'})
    assert track.snapshot()['uncertainty_m']>after['uncertainty_m']


def test_partial_visible_segment_has_larger_uncertainty_not_an_invented_line():
    whole=VisualTargetTrack();partial=VisualTargetTrack()
    a=whole.update(perception(),metadata(),telemetry(),pose())
    b=partial.update(perception(),{**metadata(),'bounds':dict(left=0,right=.5)},telemetry(),pose())
    assert b['uncertainty_m']>a['uncertainty_m']
    assert set(b['point'])=={'x','y'}


def lost_track(x=.5):
    track=VisualTargetTrack()
    track.update(perception(x),metadata(),telemetry(),pose())
    track.update({'goal_visible':False},{},telemetry(),pose())
    return track


def down_telemetry():
    result=telemetry();result['servo']=55
    return result


def executed(command,acceptance='accepted'):
    return {**command,'acceptance':acceptance}


def test_search_requires_existing_point_and_missing_or_expired_current_localization():
    track=VisualTargetTrack()
    assert track.suggest_search(telemetry()) is None
    track.update(perception(),metadata(),telemetry(),pose())
    assert track.suggest_search(telemetry()) is None
    clock=Clock();track=VisualTargetTrack(clock,max_age_s=10)
    track.update(perception(),metadata(),telemetry(),pose())
    clock.now=11
    assert track.suggest_search(telemetry())['action']=='tilt'
    assert not track.command_allowed(move('backward'),pose())


def test_search_tilts_first_and_proposals_do_not_consume_budget():
    track=lost_track()
    before=track.snapshot()['recovery']
    first=track.suggest_search(telemetry())
    assert first['action']=='tilt' and first['tilt']==55 and first['speed']==0
    assert first['candidate_goal_reached'] is False and first['recovery_scan'] is True
    for _ in range(30):assert track.suggest_search(telemetry())==first
    assert track.snapshot()['recovery']==before
    track.note_command(executed(first))
    rotation=track.suggest_search(down_telemetry())
    assert rotation['action']=='rotate_left' and rotation['speed']==150
    assert rotation['duration_ms']==421
    assert rotation['candidate_goal_reached'] is False
    assert track.snapshot()['recovery']['accepted_rotations']==0


def test_search_has_exactly_sixteen_accepted_rotations_then_explicit_stop():
    track=lost_track();t=down_telemetry()
    for count in range(16):
        command=track.suggest_search(t)
        assert command['action']=='rotate_left'
        assert track.snapshot()['recovery']['accepted_rotations']==count
        track.note_command(executed(command))
    for _ in range(3):
        assert track.suggest_search(t)['action']=='stop'
    s=track.snapshot()
    assert s['recovery']['accepted_rotations']==16 and s['recovery']['exhausted']
    assert s['goal_completed'] is False and s['point'] is not None


@pytest.mark.parametrize('acceptance',['rejected','unknown',None])
def test_rejected_search_attempts_are_bounded_without_counting_successful_rotations(acceptance):
    track=lost_track();t=down_telemetry()
    for count in range(4):
        command=track.suggest_search(t)
        assert command['action']=='rotate_left'
        track.note_command(executed(command,acceptance))
        assert track.snapshot()['recovery']['accepted_rotations']==0
        assert track.snapshot()['recovery']['failed_attempts']==count+1
    assert track.suggest_search(t)['action']=='stop'
    assert track.snapshot()['recovery']['exhausted']


def test_accepted_tilts_without_actual_camera_angle_change_are_also_bounded():
    track=lost_track()
    for _ in range(2):
        command=track.suggest_search(telemetry())
        assert command['action']=='tilt'
        track.note_command(executed(command))
    assert track.suggest_search(telemetry())['action']=='stop'
    assert track.snapshot()['recovery']['tilt_attempts']==2
    assert track.snapshot()['recovery']['accepted_rotations']==0


@pytest.mark.parametrize('changes',[{'telemetry_age_ms':501},{'telemetry_age_ms':float('nan')},
    {'telemetry_age_ms':True},{'connected':False},{'servo':None},{'servo':500}])
def test_search_missing_fresh_telemetry_stops_without_activating_or_consuming_scan(changes):
    track=lost_track();before=track.snapshot()['recovery']
    t=down_telemetry();t.update(changes)
    assert track.suggest_search(t)['action']=='stop'
    assert track.suggest_search({})['action']=='stop'
    assert track.snapshot()['recovery']==before


def test_search_does_not_use_old_point_direction_or_authorize_movement_or_completion():
    a,b=lost_track(.15),lost_track(.85)
    assert a.snapshot()['point']!=b.snapshot()['point']
    assert a.suggest_search(down_telemetry())==b.suggest_search(down_telemetry())
    assert a.suggest_search({**down_telemetry(),'estop':True})['action']=='stop'
    assert not a.snapshot()['goal_completed']


def test_only_valid_current_localized_update_renews_exhausted_scan_budget():
    track=lost_track();t=down_telemetry()
    for _ in range(4):track.note_command(executed(track.suggest_search(t),'rejected'))
    exhausted=track.snapshot()['recovery']
    track.update({'goal_visible':False},{},telemetry(),pose())
    track.update(perception(),{'status':'current_marker_not_localized'},telemetry(),pose())
    assert track.snapshot()['recovery']==exhausted
    assert track.suggest_search(t)['action']=='stop'
    track.update(perception(),metadata(),telemetry(),pose())
    state=track.snapshot()
    assert state['current_localized'] and state['recovery']['accepted_rotations']==0
    assert state['recovery']['failed_attempts']==0 and state['recovery']['tilt_attempts']==0
    assert track.suggest_search(t) is None


def test_untagged_commands_do_not_consume_recovery_budget_and_fresh_motion_invalidates_current():
    track=VisualTargetTrack();track.update(perception(),metadata(),telemetry(),pose())
    before=track.snapshot()['recovery']
    track.note_command(move('rotate_right'))
    assert track.snapshot()['recovery']==before
    assert not track.snapshot()['current_localized']
    assert track.suggest_search(down_telemetry())['action']=='rotate_left'
