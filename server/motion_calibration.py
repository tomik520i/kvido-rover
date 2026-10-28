"""Physical 300 ms turn priors, 26 Oct 2026; approximate, not wheel odometry.

Shared by the planner, map and panorama display. Image measurements vary with
floor/battery; a command prior never replaces visual heading verification.
See docs/KALIBRACE_POHYBU_2026-10-26.md for raw runs and rejected/outlier pairs.
"""
TURN_DEG = {180: 30., 150: 22., 130: 12.}
TURN_BY_DIRECTION = {
    'rotate_left': {180: 30., 150: 22.5, 130: 11.5},
    'rotate_right': {180: 30., 150: 22., 130: 13.},
}


def turn_degrees(speed, action):
    return TURN_BY_DIRECTION.get(action, TURN_DEG).get(speed, TURN_DEG[180])
