import numpy as np
import pytest

from server.camera_radar import CALIBRATION, bearing_deg, fit_calibration, row_to_cm, scan


def scene(contacts, slope=.05, near=()):
    """Synthetic inverse depth: floor line, a flat obstacle above each contact row."""
    rows=np.arange(480,dtype=np.float32)[:,None]
    disparity=np.repeat(slope*rows,640,axis=1)
    for (a,b),row in contacts.items():
        if row is not None:disparity[:row,a:b]=slope*row
    for a,b in near:disparity[:,a:b]=slope*480
    return disparity


def test_flat_obstacle_contact_row_and_distance_per_direction():
    result=scan(scene({(0,640):360}),72,bins=8)
    assert result['floor_detected']
    for d in result['directions']:
        # Reported a few rows above the true contact (tolerance band); the
        # calibration is fitted with this same detector, so the bias cancels.
        assert d['status']=='obstacle' and 350<=d['row']<=360
        assert d['distance_cm']==pytest.approx(row_to_cm(d['row'],72),abs=.1)


def test_left_obstacle_is_nearer_than_right_and_bearings_point_left_first():
    result=scan(scene({(0,320):440,(320,640):330}),72,bins=4)
    left,right=result['directions'][0],result['directions'][-1]
    assert left['bearing_deg']>0>right['bearing_deg']
    assert left['distance_cm']<right['distance_cm']


def test_bumper_blocked_direction_is_near_not_free():
    result=scan(scene({(0,640):300},near=[(0,80)]),72,bins=8)
    assert result['directions'][0]['status']=='near' and result['directions'][0]['distance_cm'] is None
    assert result['directions'][-1]['status']=='obstacle'


def test_floor_up_to_the_search_limit_is_open_and_flagged_extrapolated():
    d=scan(scene({}),72,bins=4)['directions'][1]
    assert d['status']=='open' and d['extrapolated']


def test_no_floor_means_unknown_never_free():
    result=scan(np.ones((480,640),np.float32),72,bins=4)
    assert not result['floor_detected'] and {d['status'] for d in result['directions']}=={'unknown'}


def test_rejects_uncalibrated_tilt_and_wrong_size():
    with pytest.raises(ValueError):scan(scene({}),80)
    with pytest.raises(ValueError):scan(np.zeros((240,320)),72)


def test_calibration_fit_recovers_model_and_ignores_outliers():
    truth=dict(c=230.,k=5600.,o=12.)
    distances=np.linspace(33,70,20)
    rows=truth['c']+truth['k']/(distances+truth['o'])
    rows[5]+=40  # one wrong contact detection
    fit,keep=fit_calibration(rows,distances)
    assert not keep[5] and keep.sum()==19
    for d in (35,50,65):
        y=truth['c']+truth['k']/(d+truth['o'])
        assert fit['k']/(y-fit['c'])-fit['o']==pytest.approx(d,abs=.5)


def test_bearing_and_calibrated_tilts():
    assert bearing_deg(320)==0 and bearing_deg(0)>40 and set(CALIBRATION)=={55,72,90}
    assert row_to_cm(10,72) is None


def test_fuse_needs_a_majority_and_takes_the_median():
    from server.camera_radar import fuse
    a = scan(scene({(0, 640): 360}), 72, bins=4)
    b = scan(scene({(0, 640): 362}), 72, bins=4)
    false_near = scan(scene({(0, 640): 360}, near=[(0, 160)]), 72, bins=4)
    fused = fuse([a, b, false_near])
    assert fused['frames'] == 3 and fused['directions'][0]['status'] == 'obstacle'  # one false 'near' is outvoted
    assert fused['directions'][0]['distance_cm'] in (a['directions'][0]['distance_cm'], b['directions'][0]['distance_cm'])
    two_near = fuse([false_near, false_near, a])
    assert two_near['directions'][0]['status'] == 'near'
    assert fuse([a]) is a


def test_fuse_without_majority_keeps_the_nearest_obstacle():
    from server.camera_radar import fuse
    far, near_obstacle = scan(scene({(0, 640): 300}), 72, bins=2), scan(scene({(0, 640): 420}), 72, bins=2)
    unknown = scan(np.ones((480, 640), np.float32), 72, bins=2)
    open_scan = scan(scene({}), 72, bins=2)
    fused = fuse([near_obstacle, open_scan, unknown])
    assert fused['directions'][0]['status'] == 'obstacle'
    assert fused['directions'][0]['distance_cm'] == near_obstacle['directions'][0]['distance_cm']
