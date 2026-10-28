"""Autopilot.step end to end with a fake car: the map follows what the cockpit reports as driven."""
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
sys.path.insert(0, str(ROOT/'radar_service'))
radar_service = pytest.importorskip('radar_service')


@pytest.mark.parametrize('size', [(640,480),(320,240),(160,120)])
def test_adaptive_camera_profiles_keep_calibrated_depth_coordinates(size):
    class Session:
        def run(self, _, feed):
            assert feed['pixel_values'].shape == (1,3,518,686)
            return [np.full((1,518,686), 2.5, dtype=np.float32)]
    ok, jpeg = cv2.imencode('.jpg', np.zeros((size[1],size[0],3),np.uint8))
    assert ok
    depth = radar_service.infer(Session(), jpeg.tobytes())
    assert depth.shape == (480,640)
    assert np.allclose(depth,2.5)


def test_radar_rejects_unknown_geometry_and_corrupt_jpeg():
    _, jpeg = cv2.imencode('.jpg', np.zeros((640,480,3),np.uint8))
    for raw in (jpeg.tobytes(), b'broken'):
        with pytest.raises(ValueError, match='supported 4:3'):
            radar_service.infer(None, raw)


def test_radar_camera_request_bypasses_preview_cache():
    calls = []
    class Client:
        def get(self, path):
            calls.append(path)
            return type('Response',(),{'status_code':200,'raise_for_status':lambda self:None})()
    radar = object.__new__(radar_service.Radar)
    radar.client, radar.logged_in = Client(), True
    radar.get('api/camera.jpg')
    radar.get('api/status')
    assert calls == ['api/camera.jpg?fresh=1','api/status']


class FakeResponse:
    def __init__(self, data=None, content=b''):
        self.data, self.content = data, content

    def json(self):
        return self.data


class FakeRadar:
    def __init__(self, tmp):
        self.lock = threading.Lock()
        self.infer_lock = threading.Lock()
        self.map_view = None
        self.maps = tmp/'maps'
        self.autopilot_at = 0
        self.stored = []
        img = np.random.default_rng(1).integers(0, 255, (480, 640, 3), np.uint8)
        self.jpeg = cv2.imencode('.jpg', img)[1].tobytes()

    def get(self, path):
        if path == 'api/status':
            return FakeResponse({'telemetry': {'connected': True, 'servo': 72, 'distance_cm': 150,
                                               'motor_byte': 0, 'telemetry_age_ms': 20}})
        return FakeResponse(content=self.jpeg)

    def store(self, record, jpeg):
        self.stored.append(record)

    save_map = radar_service.Radar.save_map


def test_step_moves_the_map_by_the_reported_moves_and_publishes_it(tmp_path, monkeypatch):
    fake = FakeRadar(tmp_path)
    monkeypatch.setattr(radar_service.time, 'sleep', lambda s: None)
    monkeypatch.setattr(radar_service, 'ask_gemma', lambda *a, **k: dict(
        target_visible=False, target_x=None, target_floor_y=None, confidence='low', animal_or_person_close=False,
        path_ahead_clear=True, passage_visible=False, threshold_ahead=False, threshold_floor_y=None, note='nic'))
    pilot = radar_service.Autopilot(fake, 'http://gemma', frames=1)
    monkeypatch.setattr(pilot, 'understand', lambda goal: ('silver dog bowl', 'misku'))
    monkeypatch.setattr(pilot, 'depth_scan', lambda jpeg, tilt=72: {
        'tilt': tilt, 'directions': [dict(bearing_deg=40-80*i/8, status='obstacle', distance_cm=150., row=300) for i in range(9)]})
    monkeypatch.setattr(pilot, 'guess_where', lambda state, entries: None)
    run = pilot.start('najdi misku')['run']
    first = pilot.step(run)
    assert first['actions'] == [(180, 1, 'rotate_right')]          # look-around starts
    pilot.step(run, executed=[[180, 1, 'rotate_right']])
    assert fake.map_view['pose'][2] == pytest.approx(330.)
    # The before/after frame must change too, otherwise the car may have stalled.
    img = np.random.default_rng(2).integers(0, 255, (480, 640, 3), np.uint8)
    fake.jpeg = cv2.imencode('.jpg', img)[1].tobytes()
    pilot.step(run, executed=[[180, 2, 'forward'], ['x', 1, 'forward'], [180, 1, 'teleport']])
    assert fake.map_view['pose'][0] != 0 and fake.map_view['steps'] == 3
    assert (tmp_path/'maps'/f'{run}.json').exists()
    with pytest.raises(KeyError):
        pilot.step('nope')


def test_step_queue_runs_one_step_at_a_time_and_repeats_answers(monkeypatch):
    pilot = radar_service.Autopilot(FakeRadar(Path('.')), 'http://gemma', frames=1)
    pilot.runs['r'] = dict(memory={})
    calls, gate = [], threading.Event()

    def slow_step(run, executed=None):
        calls.append(executed)
        gate.wait(5)
        return dict(actions=[(180, 1, 'forward')], reason='ok', done=False)
    monkeypatch.setattr(pilot, 'step', slow_step)
    first = pilot.request_step('r', 1, [[180, 1, 'rotate_right']], wait=.05)
    assert first['pending'] and first['seq'] == 1
    again = pilot.request_step('r', 1, [[180, 1, 'rotate_right']], wait=.05)    # retry joins, no 2nd step
    other = pilot.request_step('r', 2, None, wait=.05)                         # even a newer seq waits
    assert again['pending'] and other['pending'] and len(calls) == 1
    gate.set()
    done = pilot.request_step('r', 1, None, wait=2)
    assert done['reason'] == 'ok' and done['seq'] == 1
    assert pilot.request_step('r', 1, None, wait=2)['reason'] == 'ok' and len(calls) == 1   # repeated answer
    nxt = pilot.request_step('r', 2, [[180, 3, 'forward']], wait=2)
    assert nxt['seq'] == 2 and len(calls) == 2 and calls[1] == [[180, 3, 'forward']]


def test_step_queue_hands_errors_and_retries_after_them(monkeypatch):
    pilot = radar_service.Autopilot(FakeRadar(Path('.')), 'http://gemma', frames=1)
    pilot.runs['r'] = dict(memory={})
    answers = [ConnectionError('Auto je offline.'), dict(actions=[], reason='zpět', done=False)]

    def step(run, executed=None):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(pilot, 'step', step)
    with pytest.raises(ConnectionError):
        pilot.request_step('r', 5, None, wait=2)
    assert pilot.request_step('r', 5, None, wait=2)['reason'] == 'zpět'      # the same seq retried
    with pytest.raises(KeyError):
        pilot.request_step('nope', 1)
    with pytest.raises(ValueError):
        pilot.request_step('r', 'x')


@pytest.fixture
def mapped_pilot(tmp_path, monkeypatch):
    fake = FakeRadar(tmp_path)
    monkeypatch.setattr(radar_service.time, 'sleep', lambda s: None)
    monkeypatch.setattr(radar_service, 'ask_gemma', lambda *a, **k: dict(
        target_visible=False, confidence='low', animal_or_person_close=False,
        path_ahead_clear=True, passage_visible=False, threshold_ahead=False, note='volno'))
    pilot = radar_service.Autopilot(fake, 'http://unused', frames=1)
    monkeypatch.setattr(pilot, 'understand', lambda goal: (goal, goal))
    monkeypatch.setattr(pilot, 'depth_scan', lambda *a, **k: {'tilt': 72, 'directions': [
        dict(bearing_deg=b, status='open', distance_cm=120., row=300) for b in range(-40, 41, 10)]})
    run = pilot.start('miska')['run']
    return pilot, run, fake


def test_planner_sees_executed_pose_and_new_map_before_planning(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    observed = []
    def planner(radar, sonar, seen, memory, **kwargs):
        observed.append((memory.get('heading'), kwargs['map_context'], pilot.runs[run]['map'].steps))
        return [(180, 1, 'rotate_right')], 'test', False
    monkeypatch.setattr(radar_service, 'plan_step', planner)
    pilot.step(run, executed=[])
    pilot.step(run, executed=[])  # previous planned turn was rejected
    pilot.step(run, executed=[[150, 1, 'rotate_left']])
    assert [o[0] for o in observed] == [0., 0., 22.5]
    assert [o[2] for o in observed] == [1, 2, 3]
    assert observed[-1][1]['heading'] == 22.5
    assert fake.stored[-1]['executed'] == [(150, 1, 'rotate_left')]
    assert 'localization' in fake.stored[-1]


def test_unreported_plans_do_not_move_map_or_trigger_fake_stall(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    monkeypatch.setattr(radar_service, 'plan_step', lambda *a, **k: ([(180, 4, 'forward')], 'vpřed', False))
    pilot.step(run)
    pilot.step(run)  # legacy/no report: never assume a successful command
    state = pilot.runs[run]
    assert fake.map_view['pose'] == [0., 0., 0.]
    assert not state['memory'].get('forwards')
    assert not state['memory'].get('stalls')


def test_reported_but_stalled_forward_does_not_advance_map(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    monkeypatch.setattr(radar_service, 'plan_step', lambda *a, **k: ([(180, 4, 'forward')], 'vpřed', False))
    pilot.step(run, executed=[])
    answer = pilot.step(run, executed=[[180, 4, 'forward']])
    assert fake.map_view['pose'][:2] == [0., 0.]
    assert answer['actions'][0][2] == 'backward'
    assert pilot.runs[run]['memory']['stalls'] == 1
    assert not pilot.runs[run]['memory'].get('forwards')


def test_retry_after_partial_step_failure_applies_motion_once(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    original = fake.store
    attempts = []
    def fail_once(record, jpeg):
        attempts.append(record)
        if len(attempts) == 1:
            raise OSError('simulated log write failure after planning')
        original(record, jpeg)
    monkeypatch.setattr(fake, 'store', fail_once)
    moves = [[180, 1, 'rotate_right']]
    with pytest.raises(OSError):
        pilot.request_step(run, seq=1, executed=moves, wait=10)
    assert pilot.runs[run]['map'].heading == 0
    answer = pilot.request_step(run, seq=1, executed=moves, wait=10)
    assert answer['step'] == 1
    assert fake.map_view['pose'][2] == 330
    pilot.request_step(run, seq=1, executed=moves, wait=10)
    assert fake.map_view['pose'][2] == 330 and len(attempts) == 2


def test_execution_parser_accepts_the_longest_core_forward_chain():
    from autopilot_core import MAX_FORWARD_PULSES
    moves = [[180, MAX_FORWARD_PULSES, 'forward']]
    assert radar_service.Autopilot.executed_moves(moves) == [tuple(moves[0])]


def test_search_journal_survives_new_run_and_flows_into_next_panorama(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    pilot.step(run, [])
    state=pilot.runs[run]
    state['views']={i:fake.jpeg for i in range(3)}
    entries=[dict(frame=i,free=100,passage=True,target=False,note='no bowl') for i in range(3)]
    calls=[]
    def where(*args, **kwargs):
        calls.append(kwargs['search_context'])
        return dict(choice=1,reason='Nový výhled',area_label='kuchyň',area_confidence='medium',
                    landmarks=['linka'],uninspected=['pod stolem'],visibility='partial')
    monkeypatch.setattr(radar_service,'ask_where',where)
    assert pilot.guess_where(state,entries)==(1,'Nový výhled')
    pilot.step(run, [])  # commit journal together with a successful step
    assert fake.search_view['areas'][0]['label']=='kuchyň'
    new_run=pilot.start('miska')['run'];state=pilot.runs[new_run]
    assert len(state['place_journal']['areas'])==1
    state['views']={i:fake.jpeg for i in range(3)}
    pilot.guess_where(state,entries)
    assert calls[-1]['areas'][0]['label']=='kuchyň'
    assert calls[-1]['areas'][0]['result']=='not_observed_in_panorama'


def test_stall_recovery_cannot_override_current_animal_stop(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    pilot.step(run, executed=[])
    monkeypatch.setattr(radar_service, 'ask_gemma', lambda *a, **k: dict(
        target_visible=False, confidence='low', animal_or_person_close=True,
        path_ahead_clear=False, passage_visible=False, threshold_ahead=False, note='pes'))
    answer = pilot.step(run, executed=[[180, 4, 'forward']])
    assert answer['actions'] == []
    assert 'couv' not in (answer['say'] or '').lower()


def test_real_map_context_changes_service_decision_on_remembered_blockage(mapped_pilot):
    pilot, run, fake = mapped_pilot
    state = pilot.runs[run]
    state['memory']['scanned_once'] = True
    first = pilot.step(run, executed=[])
    assert first['actions'][0][2] == 'forward'
    # An earlier stalled/threshold approach remains blocked despite a currently
    # clear-looking image. Use the actual grid and planner, no fabricated context.
    state['map'].mark_blocked(20, 'stuck', 'neprojel', 1)
    second = pilot.step(run, executed=[])
    assert all(action != 'forward' for _, _, action in second['actions'])
    assert second['map_decision']['source'] == 'map_blocked'


def pending_route(pilot, run, fake, heading=0):
    state = pilot.runs[run]
    state['memory'].update(scanned_once=True, route_goal=dict(heading=heading, reference_frame=0, status='aligning', attempts=0))
    state.update(route_reference=fake.jpeg, route_reference_frame=0)
    return state


def test_route_heading_gate_blocks_unverified_forward(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    state = pending_route(pilot, run, fake, heading=270)
    calls = []
    monkeypatch.setattr(radar_service, 'ask_heading_match', lambda *a, **k: calls.append(a))
    first = pilot.step(run, [])
    refused = pilot.step(run, [])
    assert first['actions'] == refused['actions'] == [(180, 3, 'rotate_right')]
    assert calls == [] and state['memory']['route_goal']['heading'] == 270
    assert refused['route_goal']['status'] == 'aligning'


def test_match_allows_geometry_but_does_not_overwrite_it(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    pending_route(pilot, run, fake)
    monkeypatch.setattr(radar_service, 'ask_heading_match', lambda *a, **k: dict(status='match', confidence='high', correction='none', reason='same', error_type=None))
    first = pilot.step(run, [])
    assert first['actions'][0][2] == 'forward'
    assert first['heading_verification']['status'] == 'match'
    pilot.runs[run]['map'].mark_blocked(20, 'stuck', 'neprojel', 1)
    second = pilot.step(run, [])
    assert all(a != 'forward' for _, _, a in second['actions'])
    # Cached/logged prior result must not mutate when the live route state changes.
    assert first['route_goal']['awaiting_forward'] is True


def test_animal_prevents_route_turn_and_comparison(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    pending_route(pilot, run, fake, heading=270)
    monkeypatch.setattr(radar_service, 'ask_gemma', lambda *a, **k: dict(animal_or_person_close=True, note='pes'))
    monkeypatch.setattr(radar_service, 'ask_heading_match', lambda *a, **k: pytest.fail('no comparison needed while animal close'))
    assert pilot.step(run, [])['actions'] == []


def test_panorama_reference_is_retained_across_more_than20_retry_frames(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    state = pilot.runs[run]
    state['steps'] = 30
    state['views'] = {i: fake.jpeg for i in range(30)}
    state['memory']['scan'] = {'entries': [dict(frame=0)], 'left': 0}
    def planner(radar, sonar, seen, memory, **kwargs):
        memory.pop('scan')
        memory['route_goal'] = dict(reference_frame=0, heading=270, status='aligning', attempts=0)
        return [(180, 3, 'rotate_right')], 'chosen', False
    monkeypatch.setattr(radar_service, 'plan_step', planner)
    pilot.step(run, [])
    assert state['route_reference'] == fake.jpeg and state['route_reference_frame'] == 0


def test_close_sonar_prevents_route_turn_and_restarts_after_backoff(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    state = pending_route(pilot, run, fake, heading=270)
    original = fake.get
    def near(path):
        if path == 'api/status':
            return FakeResponse({'telemetry': {'connected': True, 'servo': 72, 'distance_cm': 10,
                                               'motor_byte': 0, 'telemetry_age_ms': 20}})
        return original(path)
    monkeypatch.setattr(fake, 'get', near)
    monkeypatch.setattr(radar_service, 'ask_heading_match', lambda *a, **k: pytest.fail('do not compare before backing off'))
    answer = pilot.step(run, [])
    assert answer['actions'] == [(180, 1, 'backward')]
    assert 'route_goal' not in state['memory'] and not state['memory']['scanned_once']


def test_stalled_entry_never_counts_as_successful_route_entry(mapped_pilot, monkeypatch):
    pilot, run, fake = mapped_pilot
    state = pending_route(pilot, run, fake)
    monkeypatch.setattr(radar_service, 'ask_heading_match', lambda *a, **k: dict(status='match', confidence='high', correction='none', reason='same', error_type=None))
    pilot.step(run, [])
    answer = pilot.step(run, [[180, 2, 'forward']])
    assert state['memory']['last_route_goal']['status'] == 'abandoned'
    assert 'route_goal' not in state['memory'] and answer['actions'][0][2] == 'backward'


def test_long_ai_wait_rechecks_scene_before_movement(mapped_pilot, monkeypatch):
    pilot,run,fake=mapped_pilot
    clock=[0.]
    monkeypatch.setattr(radar_service.time,'monotonic',lambda:clock[0])
    def delayed(*args,**kwargs):
        assert kwargs['queued'] is True
        clock[0]=50.
        fake.jpeg=cv2.imencode('.jpg',np.zeros((480,640,3),np.uint8))[1].tobytes()
        return dict(target_visible=False,confidence='low',path_ahead_clear=True)
    monkeypatch.setattr(radar_service,'ask_gemma',delayed)
    pilot.runs[run]['memory']['scanned_once']=True
    answer=pilot.step(run,[])
    assert answer['actions']==[] and not answer['done']
    assert 'Po čekání' in answer['reason']


def test_waiting_scene_detection_rejects_local_change_and_bad_frames():
    image=np.full((240,320,3),100,np.uint8)
    a=cv2.imencode('.jpg',image)[1].tobytes()
    image[80:140,100:160]=240
    b=cv2.imencode('.jpg',image)[1].tobytes()
    assert radar_service.same_waiting_scene(a,a)
    assert not radar_service.same_waiting_scene(a,b)
    assert not radar_service.same_waiting_scene(a,b'broken')


def fresh_sequence(fake, monkeypatch, telemetry_change=None):
    """Different real JPEGs, without moving the fake robot."""
    count = [0]
    original = fake.get
    def get(path):
        if path == 'api/status':
            response = original(path)
            if telemetry_change and count[0]:
                response.data['telemetry'].update(telemetry_change)
            return response
        count[0] += 1
        im = np.random.default_rng(100+count[0]).integers(0,255,(240,320,3),np.uint8)
        return FakeResponse(content=cv2.imencode('.jpg',im)[1].tobytes())
    monkeypatch.setattr(fake,'get',get)
    return count


def test_radar_retry_recovers_on_fifth_and_applies_motion_and_ai_once(mapped_pilot, monkeypatch):
    pilot,run,fake=mapped_pilot
    captures=fresh_sequence(fake,monkeypatch)
    calls=[]
    original=pilot.depth_scan
    def depth(*args,**kwargs):
        if captures[0]<5:raise ValueError('incomplete JPEG')
        return original(*args,**kwargs)
    monkeypatch.setattr(pilot,'depth_scan',depth)
    monkeypatch.setattr(radar_service,'ask_gemma',lambda jpeg,*a,**k: calls.append(jpeg) or dict(path_ahead_clear=True))
    answer=pilot.step(run,[[180,1,'rotate_right']])
    assert captures[0]==5 and len(calls)==1
    assert answer['radar_retry']['status']=='accepted'
    assert answer['radar_retry']['selected_attempt']==5
    assert fake.map_view['pose'][2]==330 and fake.map_view['steps']==1
    assert len(fake.stored)==1


def test_five_rejected_scans_never_force_alignment_or_commit_five_votes(mapped_pilot,monkeypatch):
    pilot,run,fake=mapped_pilot
    captures=fresh_sequence(fake,monkeypatch)
    def rejected(m,*args,**kwargs):
        m.steps+=1
        m.localization={'status':'rejected','reason':'low_overlap','fusion':'withheld_ambiguous_pose'}
    monkeypatch.setattr(radar_service.RoverMap,'observe',rejected)
    answer=pilot.step(run,[[180,1,'rotate_right']])
    assert captures[0]==5
    assert answer['radar_retry']['status']=='unresolved'
    assert answer['radar_retry']['reason']=='low_overlap'
    assert fake.map_view['steps']==1 and fake.map_view['pose'][2]==330
    assert pilot.runs[run]['map'].localization['fusion']=='withheld_ambiguous_pose'
    assert not np.any(pilot.runs[run]['map'].odds>0)


def test_identical_retry_images_do_not_get_repeated_depth_votes(mapped_pilot,monkeypatch):
    pilot,run,fake=mapped_pilot
    calls=[]
    original=pilot.depth_scan
    def unknown(*a,**k):
        calls.append(1)
        return dict(original(*a,**k),floor_detected=False)
    monkeypatch.setattr(pilot,'depth_scan',unknown)
    answer=pilot.step(run,[])
    assert len(calls)==1
    assert answer['radar_retry']['attempt']==5
    assert answer['radar_retry']['selected_attempt']==1
    assert [a['reason'] for a in answer['radar_retry']['attempts'][1:]]==['duplicate_frame']*4
    assert fake.map_view['steps']==1


@pytest.mark.parametrize('change',[{'motor_byte':163},{'servo':55},{'telemetry_age_ms':501},{'connected':False}])
def test_retry_aborts_if_car_stops_being_stationary_or_fresh(mapped_pilot,monkeypatch,change):
    pilot,run,fake=mapped_pilot
    captures=fresh_sequence(fake,monkeypatch,change)
    monkeypatch.setattr(pilot,'depth_scan',lambda *a,**k: (_ for _ in ()).throw(ValueError('bad frame')))
    monkeypatch.setattr(radar_service,'ask_gemma',lambda *a,**k: pytest.fail('no AI or movement from bad acquisition'))
    with pytest.raises(ConnectionError):pilot.request_step(run,1,[[180,1,'rotate_right']],wait=10)
    assert captures[0]==1 and pilot.runs[run]['map'].heading==0
    assert pilot.runs[run]['steps']==0


def test_all_bad_captures_stop_after_five_without_advancing_navigation(mapped_pilot,monkeypatch):
    pilot,run,fake=mapped_pilot
    captures=fresh_sequence(fake,monkeypatch)
    monkeypatch.setattr(pilot,'depth_scan',lambda *a,**k: (_ for _ in ()).throw(ValueError('bad depth')))
    with pytest.raises(ConnectionError,match='pěti'):pilot.request_step(run,1,[[180,1,'rotate_right']],wait=10)
    assert captures[0]==5 and not fake.stored
    assert pilot.runs[run]['map'].steps==0 and pilot.runs[run]['map'].heading==0
