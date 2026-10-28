import pytest

from tools.autopilot_alpha import decide, free_space, turn_plan
from server.autopilot_core import stalled, unstick, SCAN_PULSES


def radar(distances, status='obstacle'):
    """Synthetic scan, left to right like camera_radar.scan."""
    n = len(distances)
    return {'directions': [dict(bearing_deg=40-80*i/(n-1), status=status if d is not None else 'unknown', distance_cm=d)
                           for i, d in enumerate(distances)]}


def see(x=None, close=False, confidence='high', clear=True):
    return dict(target_visible=x is not None, target_x=x, target_floor_y=None, confidence=confidence,
                animal_or_person_close=close, path_ahead_clear=clear, note='')


def test_living_thing_stops_watches_then_tries_another_way():
    memory = {}
    for i in range(3):
        actions, _, done = decide(radar([150]*9), None, see(close=True), memory)
        assert actions == [] and not done
    actions, reason, done = decide(radar([150]*9), None, see(close=True), memory)
    assert actions == [(150, 2, 'rotate_right')] and not done and 'jinou cestu' in reason
    actions, _, _ = decide(radar([150]*9), None, see(), memory)
    assert actions[0][2] == 'forward'


def test_target_floor_position_gives_distance_and_stops_before_it():
    near = dict(see(.5), target_floor_y=.92)
    memory = {}
    decide(radar([150]*9), None, near, memory)
    actions, reason, done = decide(radar([150]*9), None, near, memory)
    assert done and actions == [] and 'jsem u cíle' in reason.lower()
    far = dict(see(.5), target_floor_y=.55)
    actions, _, done = decide(radar([150]*9), None, far, {})
    assert not done and actions[0][2] == 'forward'


def test_target_off_centre_turns_towards_it_with_measured_pulses():
    actions, _, _ = decide(radar([150]*9), None, see(.1), {})
    assert actions[0][2] == 'rotate_left' and actions[0][0] == 180
    actions, _, _ = decide(radar([150]*9), None, see(.6), {})
    assert actions == [(130, 1, 'rotate_right')]


def test_centred_target_approaches_then_stops_before_it():
    actions, _, done = decide(radar([120]*9), None, see(.5), {})
    assert actions[0][2] == 'forward' and 1 <= actions[0][1] <= 3 and not done
    memory = {}
    actions, reason, done = decide(radar([46]*9), None, see(.5), memory)
    assert actions == [] and not done and 'ověřím' in reason
    actions, reason, done = decide(radar([46]*9), None, see(.5), memory)
    assert actions == [] and done and 'jsem u cíle' in reason


def test_low_confidence_target_is_ignored():
    actions, reason, _ = decide(radar([150]*9), None, see(.1, confidence='low'), {})
    assert actions[0][2] == 'forward' and 'nevidím' in reason


def test_sonar_can_only_make_it_less_free():
    actions, _, _ = decide(radar([150]*9), 40, None, {})
    assert actions[0][2] != 'forward'


def test_isolated_far_reading_does_not_open_a_path():
    space = dict(free_space(radar([30, 30, 30, 30, 150, 30, 30, 30, 30])))
    assert max(space.values()) == 30


def test_turns_to_widest_gap_and_ends_after_a_full_spin():
    actions, _, _ = decide(radar([150, 150, 150, 30, 30, 30, 30, 30, 30]), None, None, {})
    assert actions[0][2] == 'rotate_left'
    memory = {}
    done = False
    for _ in range(23):
        _, _, done = decide(radar([20]*9), None, None, memory)
    assert done


@pytest.mark.parametrize('bearing,expected', [(30, (180, 1, 'rotate_left')), (-60, (180, 2, 'rotate_right')), (12, (130, 1, 'rotate_left'))])
def test_turn_plan(bearing, expected):
    assert turn_plan(bearing) == [expected]


def test_no_echo_needs_ai_confirmation_and_hands_clearance_to_the_uno():
    memory = {}
    actions, reason, _ = decide(radar([120]*9), None, see(clear=False), memory)
    assert actions and actions[0][2] != 'forward' and 'nepotvrdila' in reason and memory['clearance_cm'] == 0
    actions, _, _ = decide(radar([120]*9), None, see(clear=True), memory)
    assert actions[0][2] == 'forward' and memory['clearance_cm'] == 120


def test_false_radar_contact_is_overruled_only_by_sonar_and_ai_together():
    near_contact = radar([150, 150, 150, 150, 30, 150, 150, 150, 150])
    actions, _, _ = decide(near_contact, 140, see(clear=True), {})
    assert actions[0][2] == 'forward'
    actions, _, _ = decide(near_contact, 140, see(clear=False), {})
    assert actions[0][2] != 'forward'
    memory = {}
    decide(radar([150]*9), 140, see(clear=True), memory)
    assert memory['clearance_cm'] == 150  # offered for a flickering echo; ignored while the echo exists
    decide(radar([150]*9), 140, see(clear=False), memory)
    assert memory['clearance_cm'] == 0


def test_visible_target_without_confirmed_path_is_not_an_arrival():
    memory = {}
    for _ in range(2):
        actions, reason, done = decide(radar([150]*9), None, see(.5, clear=False), memory)
        assert actions == [] and not done and 'potvrzenou' in reason
    actions, _, done = decide(radar([150]*9), None, see(.5, clear=False), memory)
    assert not done and actions and actions[0][2].startswith('rotate')


def test_nothing_free_turns_in_small_steps_towards_more_room_and_backs_off_when_close():
    memory = {}
    actions, reason, _ = decide(radar([60, 60, 60, 20, 20, 20, 10, 10, 10]), 25, None, memory)
    assert actions == [(180, 1, 'backward'), (150, 1, 'rotate_left')] and 'vlevo' in reason
    actions, _, _ = decide(radar([10, 10, 10, 20, 20, 20, 60, 60, 60]), 80, None, memory)
    assert actions == [(150, 1, 'rotate_left')]  # keeps its side, no zig-zag


def test_creeps_towards_a_gap_when_the_wide_sonar_sees_its_edge():
    gap = radar([30, 30, 140, 160, 180, 160, 140, 30, 30])
    actions, reason, _ = decide(gap, 63, see(clear=False), {})
    assert actions == [(180, 3, 'forward')] and 'vpředu volno 63' in reason
    actions, _, _ = decide(gap, 44, see(clear=False), {})
    assert actions[0][2] != 'forward'


def test_stall_detection_after_forward_and_alternating_unstick():
    import numpy as np
    still = np.full((48, 64), 100, np.int16)
    moved = still.copy(); moved[:, :32] = 160
    assert stalled({'thumb': still, 'sonar_cm': 80, 'forward': True}, still+1, 81)
    assert not stalled({'thumb': still, 'sonar_cm': 80, 'forward': True}, moved, 80)       # view changed
    assert not stalled({'thumb': still, 'sonar_cm': 80, 'forward': True}, still, 70)        # sonar got closer
    assert not stalled({'thumb': still, 'sonar_cm': 80, 'forward': False}, still, 80)       # it only turned
    memory = {}
    (first, _), (second, _) = unstick(memory), unstick(memory)
    assert first[0] == (180, 2, 'backward') and first[1][2] != second[1][2]


def test_a_single_sighting_is_never_an_arrival():
    memory = {}
    _, reason, done = decide(radar([46]*9), None, see(.5), memory)
    assert not done
    _, _, done = decide(radar([46]*9), None, see(), memory)  # second look: not there any more
    assert not done
    _, _, done = decide(radar([46]*9), None, see(.5), memory)
    assert not done  # confirmation starts again


def run_panorama(memory, free_by_heading, passage_at=None, target_at=None):
    """Drive plan_step through a look-around in a simulated room (free cm per heading)."""
    from server.autopilot_core import note_motion, plan_step, wrap
    for _ in range(20):
        heading = memory.get('heading', 0.)
        free = min(free_by_heading, key=lambda h: abs(wrap(h-heading)))
        near = lambda h: h is not None and abs(wrap(h-heading)) < 14
        seen = dict(see(.5 if near(target_at) else None), passage_visible=near(passage_at))
        actions, reason, done = plan_step(radar([free_by_heading[free]]*9), None, seen, memory)
        note_motion(memory, actions)
        if 'scan' not in memory:
            return actions, reason, done
    raise AssertionError('panorama did not finish')


def test_start_looks_around_and_turns_to_the_passage_not_the_first_gap():
    room = {h: 60 for h in range(0, 360, 27)}
    room[0], room[27*9] = 70, 140      # free ahead, but a doorway lies behind-right
    memory = {}
    actions, reason, done = run_panorama(memory, room, passage_at=27*9)
    assert not done and 'průchod' in reason and len(memory['panorama']) == SCAN_PULSES+1
    best = next(e for e in memory['panorama'] if e['passage'])
    assert abs((memory['heading']-best['heading']+180) % 360-180) <= 14  # within half a pulse


def test_panorama_stops_early_when_the_target_shows_up():
    room = {h: 120 for h in range(0, 360, 27)}
    memory = {}
    actions, reason, done = run_panorama(memory, room, target_at=-54 % 360)
    assert len(memory['panorama']) == 3 and actions and actions[0][2] == 'forward'


def test_checkpoint_after_driving_avoids_going_back():
    from server.autopilot_core import SCAN_EVERY_CM, note_motion, panorama_pick, plan_step
    memory = {'scanned_once': True, 'heading': 0., 'forwards': 0, 'forwards_at_scan': 0}
    while memory.get('travel_cm', 0) < SCAN_EVERY_CM:
        actions, _, _ = plan_step(radar([150]*9), None, see(), memory)
        assert actions[0][2] == 'forward'
        note_motion(memory, actions)
    actions, reason, _ = plan_step(radar([150]*9), None, see(), memory)
    assert 'Rozhlížím' in reason and 'scan' in memory
    entries = [dict(heading=0., free=100., passage=False, target=False),
               dict(heading=180., free=150., passage=False, target=False)]
    best, _ = panorama_pick(entries, came_from=180.)
    assert best['heading'] == 0.


def test_animal_pauses_the_panorama():
    from server.autopilot_core import plan_step
    memory = {}
    plan_step(radar([150]*9), None, see(), memory)
    left = memory['scan']['left']
    actions, _, _ = plan_step(radar([150]*9), None, see(close=True), memory)
    assert actions == [] and memory['scan']['left'] == left


def test_ai_guess_of_where_the_target_is_beats_more_room_but_not_a_wall():
    from server.autopilot_core import note_motion, plan_step
    memory = {}
    asked = []

    def guess(entries):
        asked.append(len(entries))
        return 6, 'Misky bývají v kuchyni, zkusím to tamtudy.'
    for _ in range(20):
        heading = memory.get('heading', 0.)
        free = 160 if heading == 0 else 80
        actions, reason, _ = plan_step(radar([free]*9), None, dict(see(), passage_visible=False), memory, guess=guess)
        note_motion(memory, actions)
        if 'scan' not in memory:
            break
    assert asked == [SCAN_PULSES+1] and 'kuchyni' in reason and memory['thought']
    assert abs((memory['heading']-memory['panorama'][6]['heading']+180) % 360-180) <= 14
    from server.autopilot_core import panorama_pick
    walls = [dict(heading=0., free=120., passage=False, target=False), dict(heading=90., free=20., passage=False, target=False)]
    assert panorama_pick(walls, hint=1)[0] is walls[0]


def test_threshold_is_never_crossed_and_its_direction_is_remembered():
    from server.autopilot_core import panorama_pick, threshold_cm
    memory = {'heading': 90., 'forwards': 7, 'travel_heading': 90.}
    actions, how = unstick(memory, path_clear=True)        # stalled: no run-up, back off
    assert how == 'back' and actions[0] == (180, 2, 'backward') and memory['blocked'][0]['heading'] == 90.
    far = dict(see(clear=False), threshold_ahead=True, threshold_floor_y=.62)
    near = dict(see(clear=False), threshold_ahead=True, threshold_floor_y=.98)
    assert threshold_cm(far) > threshold_cm(near) and threshold_cm(dict(see(), threshold_ahead=False)) is None
    memory = {}
    actions, reason, _ = decide(radar([200]*9), 150, far, memory)
    if threshold_cm(far) >= 46:
        assert actions[0][2] == 'forward' and 'práh' in reason and memory['clearance_cm'] == 0
    actions, reason, _ = decide(radar([200]*9), 150, near, memory)
    assert actions[0][2].startswith('rotate') and 'nepřejedu' in reason and memory['blocked']
    entries = [dict(heading=90., free=150., passage=True, target=False, tried=True),
               dict(heading=0., free=80., passage=False, target=False)]
    assert panorama_pick(entries, hint=0)[0] is entries[1]


def test_run_length_adapts_to_free_floor():
    from server.autopilot_core import CREEP_STOP_CM, forward_pulses, forward_run_cm
    assert forward_pulses(46) == 1 and forward_pulses(60) == 3
    assert forward_pulses(100) > 3 and forward_pulses(250) == 12
    for ahead in range(46, 300, 7):
        n = forward_pulses(ahead)
        assert ahead-forward_run_cm(n) >= CREEP_STOP_CM-3       # never plans past the stop distance
    actions, reason, _ = decide(radar([200]*9), 200, see(), {})
    assert actions == [(180, forward_pulses(120), 'forward')]          # counted only up to 1.2 m
    actions, reason, _ = decide(radar([90]*9), 400, see(), {})         # out-of-range sonar, AI clear
    assert 'volno 150 cm' in reason and actions[0][1] == forward_pulses(120)
