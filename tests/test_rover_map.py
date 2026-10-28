import base64
import math

import numpy as np

from server.rover_map import BLOCKED_CELL, CELL_CM, FREE_CELL, HALF, OCCUPIED_CELL, VISITED_CELL, RoverMap


def radar(distance, status='obstacle', n=9):
    return {'directions': [dict(bearing_deg=40-80*i/(n-1), status=status, distance_cm=distance) for i in range(n)]}


def cell(view, x, y):
    grid = np.frombuffer(base64.b64decode(view['cells']), np.uint8).reshape(view['shape'])
    i, j = round(x/CELL_CM)-view['origin'][0], round(y/CELL_CM)-view['origin'][1]
    return grid[i, j]


def test_pulses_move_the_pose_and_chained_runs_go_further():
    m = RoverMap()
    m.move([(180, 1, 'forward')])
    assert math.isclose(m.x, 5.8) and m.y == 0
    m.move([(180, 3, 'forward')])
    assert math.isclose(m.x, 5.8+5.8+2*5.5)
    m.move([(180, 9, 'rotate_left')])           # 270 deg left = facing right
    assert math.isclose(m.heading, 270.)
    m.move([(180, 2, 'backward')])
    assert math.isclose(m.y, 11.6, abs_tol=1e-6) and len(m.trail) == 5


def test_a_stall_does_not_move_the_car_and_marks_the_spot():
    m = RoverMap()
    m.move([(180, 3, 'forward')], stalled=True)
    assert m.x == 0 and m.y == 0
    m.stalled(step=4)
    v = m.view()
    assert cell(v, 12, 0) == BLOCKED_CELL and v['events'][-1]['kind'] == 'stuck'


def test_radar_rays_mark_free_floor_and_the_contact():
    m = RoverMap()
    m.observe(radar(80), sonar_cm=None)
    v = m.view()
    assert cell(v, 40, 0) == FREE_CELL and cell(v, 80, 0) == OCCUPIED_CELL
    assert cell(v, 0, 0) == VISITED_CELL
    m2 = RoverMap()
    m2.observe(radar(300), sonar_cm=None)        # beyond the trusted range: free only up to 120 cm
    v2 = m2.view()
    assert cell(v2, 100, 0) == FREE_CELL and v2['shape'][0] < (300//CELL_CM)


def test_turned_observation_lands_on_the_side():
    m = RoverMap()
    m.move([(180, 9, 'rotate_left')])              # 270 deg: facing -y
    m.observe(radar(60, n=3), sonar_cm=None)
    v = m.view()
    assert cell(v, 0, -60) == OCCUPIED_CELL


def test_panorama_and_view_are_json_friendly():
    import json
    m = RoverMap()
    m.panorama([dict(heading=0., free=120., passage=True, target=False), dict(heading=333., free=40., passage=False, target=False)],
               chosen_heading=0., thought='Zkusím to tudy.', step=14, memory_heading=0.)
    v = json.loads(json.dumps(m.view()))
    assert v['panoramas'][0]['rays'][0][:3] == [0.0, 120.0, True] and v['pose'] == [0.0, 0.0, 0.0]
    assert v['origin'][0] <= 0 <= v['origin'][0]+v['shape'][0] and HALF == 80
