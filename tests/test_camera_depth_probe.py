import math

import numpy as np
import pytest

from tools.camera_depth_probe import centre_closeness, metres_from, scan


def test_scan_reads_horizon_band_and_maps_left_pixels_to_positive_angles():
    disparity=np.zeros((480,640),np.float32)
    disparity[:,:100]=5    # near object on the left
    result,rows=scan(disparity,90,90.,8,6.)
    assert rows[0]<240<rows[1]
    assert result['angles_deg'][0]>0>result['angles_deg'][-1]
    assert result['closeness'][0]==5 and result['closeness'][-1]==0


def test_lower_servo_moves_band_up_like_the_simulator_camera():
    _,level=scan(np.zeros((480,640)),90,90.,4,6.)
    _,down=scan(np.zeros((480,640)),55,90.,4,6.)
    assert down[0]<level[0]


def test_centre_uses_only_sonar_cone_columns():
    disparity=np.zeros((480,640),np.float32);disparity[:,300:340]=3
    assert centre_closeness(disparity,(200,280),7.5,90.)==3


@pytest.mark.parametrize('fit,value,expected',[(None,1.,None),(dict(a=.5,b=0),2.,1.),(dict(a=-1,b=0),1.,None)])
def test_metres_only_from_a_valid_fit(fit,value,expected):
    assert metres_from(value,fit)==expected
