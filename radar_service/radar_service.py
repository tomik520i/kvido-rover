"""Kvido camera radar alpha: a separate companion of the cockpit.

Every few seconds it reads /api/status and a fresh /api/camera.jpg from the
Kvido server, runs Depth Anything V2 Small (ONNX, CPU) and camera_radar.scan,
and keeps a bounded history for the dashboard. It never sends drive commands:
for the autopilot alpha it answers "what next" for one fresh frame, and the
server-owned AlphaRunner executes permitted moves independently of the page.

Failures are expected (relay drops, busy camera, timeouts): every step retries
with exponential backoff, a frame is only used together with the telemetry read
right before it, and an old frame or result is never presented as current.
"""
import argparse
import collections
import copy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import httpx
import numpy as np
import onnxruntime

# Run directly from a checkout as well as from the original flat deployment.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'server'))
from camera_radar import CALIBRATION, FOCAL_PX, fuse, scan
from autopilot_core import MAX_FORWARD_PULSES, TILT, ask_gemma, ask_where, ask_heading_match, decide, note_motion, plan_step, stalled, threshold_cm, unstick
from rover_map import RoverMap, TURN_DEG
from motion_calibration import turn_degrees
from route_verification import check_route, restart_panorama
import place_memory

MOVES = ('forward', 'backward', 'rotate_left', 'rotate_right', 'climb')

# When it does not know where to go or got stuck, it looks down at the floor right in
# front (calibrated 55 deg radar is best up close), then returns to 72 deg for driving.
LOOK_TILT = 55
# The cockpit's proxy gives up after 30 s; answer well before that ('pending' = ask again).
STEP_WAIT_S = 20
RADAR_ATTEMPTS = 5

HERE = Path(__file__).resolve().parent
try:
    _LIBC = ctypes.CDLL('libc.so.6')
except OSError:  # not glibc (e.g. Windows test runs)
    _LIBC = None


def trim_memory():
    """Hand freed inference buffers back to the OS; glibc otherwise keeps them and RSS creeps up."""
    if _LIBC is not None:
        _LIBC.malloc_trim(0)
MEAN = np.array([.485, .456, .406], np.float32)
STD = np.array([.229, .224, .225], np.float32)


def infer(session, jpeg, height=518):
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None or bgr.shape[:2] not in ((480, 640), (240, 320), (120, 160)):
        raise ValueError('Expected a supported 4:3 camera JPEG (640x480, 320x240 or 160x120)')
    # Adaptive profiles preserve the sensor's 4:3 field of view. Depth and
    # calibration coordinates remain 640x480; upscaling does not add detail.
    width = max(14, round(height*640/480/14)*14)
    rgb = cv2.cvtColor(cv2.resize(bgr, (width, height), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
    x = ((rgb.astype(np.float32)/255-MEAN)/STD).transpose(2, 0, 1)[None]
    out = session.run(None, {'pixel_values': x})[0][0]
    return cv2.resize(out, (640, 480), interpolation=cv2.INTER_LINEAR)


def same_waiting_scene(before, after):
    """After a long AI queue, reject a changed scene before using its old decision."""
    gray = [cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE) for raw in (before, after)]
    if any(img is None for img in gray):
        return False
    a,b = [cv2.resize(img,(160,120)).astype(np.float32) for img in gray]
    difference = np.abs(a-b)
    tiles = difference.reshape(6,20,8,20).mean(axis=(1,3))
    return bool(difference.mean() < 8 and tiles.max() < 22)


class Radar:
    def __init__(self, args):
        self.args = args
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = args.threads
        options.inter_op_num_threads = 1
        options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        options.enable_cpu_mem_arena = False  # the host is short on RAM
        self.session = onnxruntime.InferenceSession(str(args.model), options, providers=['CPUExecutionProvider'])
        # --ca '' means the system trust store (public URL); the LAN URL needs the local CA.
        context = ssl.create_default_context(cafile=args.ca) if args.ca else ssl.create_default_context()
        self.client = httpx.Client(base_url=args.base, verify=context, trust_env=False, timeout=httpx.Timeout(8, read=12))
        self.lock = threading.Lock()
        self.history = collections.deque(maxlen=args.history)
        self.map_view = None  # map of the latest alpha run for the dashboard
        self.maps = args.data/'maps'
        self.search_view = None
        journals = sorted((args.data/'search-memory').glob('*.json'), key=lambda p:p.stat().st_mtime)
        if journals:
            try:
                self.search_view = place_memory.public(place_memory.load(journals[-1], ''))
            except (OSError, ValueError, KeyError):
                pass  # retain corrupt file; starting that goal reports the error
        self.latest_jpeg = None
        self.state = dict(service='starting', mapping_version='map-guided-local-matching-v1', backoff_s=0, frames=0, errors=0, consecutive_errors=0,
                          last_error=None, last_ok_at=None, started_at=time.time(), relay_events=[])
        self.log = args.data/'radar.jsonl'
        self.logged_in = False
        self.autopilot_at = 0.  # the background loop pauses while the autopilot is stepping
        # One depth inference at a time: concurrent runs pushed the service over its memory limit.
        self.infer_lock = threading.Lock()

    def login(self):
        r = self.client.post('api/login', json={'key': self.args.key.read_text().strip()})
        r.raise_for_status()
        self.logged_in = True

    def get(self, path):
        if path == 'api/camera.jpg':
            path += '?fresh=1'  # autonomous observations must bypass preview cache
        if not self.logged_in:
            self.login()
        r = self.client.get(path)
        if r.status_code == 401:
            self.login()
            r = self.client.get(path)
        r.raise_for_status()
        return r

    def note(self, **values):
        with self.lock:
            self.state.update(values)

    def track_relay(self, status):
        last = ((status.get('relay') or {}).get('last_disconnect')) or None
        with self.lock:
            events = self.state['relay_events']
            if last and (not events or events[-1].get('at_ms') != last.get('at_ms')):
                events.append(last)
                del events[:-50]

    def step(self):
        status = self.get('api/status').json()
        self.track_relay(status)
        t = status.get('telemetry') or {}
        summary = dict(at=time.time(), connected=bool(t.get('connected')), armed=status.get('armed'), estop=status.get('estop'),
                       mode=status.get('mode'), sonar_cm=t.get('distance_cm'), servo=t.get('servo'), motor_byte=t.get('motor_byte'),
                       telemetry_age_ms=t.get('telemetry_age_ms'), rssi=t.get('rssi'), uptime_ms=t.get('uptime_ms'),
                       firmware=t.get('firmware'), wifi_bssid=t.get('wifi_bssid'), wifi_roam_count=t.get('wifi_roam_count'),
                       relay=(t.get('relay') or {}) and {k: t['relay'].get(k) for k in ('connections', 'disconnects', 'connect_attempts', 'last_error')},
                       task=task_summary(status.get('task') or {}))
        if not summary['connected']:
            raise ConnectionError('Car offline')
        if summary['servo'] not in CALIBRATION:
            # No calibration for this tilt: record telemetry, skip the camera.
            self.store(dict(summary, radar=None, skipped='uncalibrated_tilt'), None)
            return
        jpeg = self.get('api/camera.jpg').content
        started = time.monotonic()
        with self.infer_lock:
            result = scan(infer(self.session, jpeg), summary['servo'])
            trim_memory()
        inference_ms = round((time.monotonic()-started)*1000)
        for d in result['directions']:
            d['x'] = round(320-math.tan(math.radians(d['bearing_deg']))*FOCAL_PX, 1)
        ahead = [d['distance_cm'] for d in result['directions'] if abs(d['bearing_deg']) <= 8 and d['status'] == 'obstacle']
        result['ahead_cm'] = round(float(np.median(ahead)), 1) if ahead else None
        self.store(dict(summary, radar=result, inference_ms=inference_ms), jpeg)

    def store(self, record, jpeg):
        with self.lock:
            self.history.append(record)
            if jpeg is not None:
                self.latest_jpeg = (record['at'], jpeg)
            self.state.update(frames=self.state['frames']+(jpeg is not None), last_ok_at=record['at'], consecutive_errors=0,
                              backoff_s=0, service='running', last_error=None)
        line = json.dumps(record, ensure_ascii=False)+'\n'
        if self.log.exists() and self.log.stat().st_size > self.args.log_bytes:
            self.log.replace(self.log.with_suffix('.jsonl.1'))
        with self.log.open('a', encoding='utf-8') as f:
            f.write(line)

    def run(self):
        backoff = 0.
        while True:
            if time.time()-self.autopilot_at < 20:
                time.sleep(1)
                continue
            try:
                self.step()
                backoff = 0.
                time.sleep(self.args.interval)
            except Exception as e:  # noqa: BLE001 - every failure is retried, never fatal
                backoff = min(self.args.max_backoff, max(2., backoff*2))
                offline = isinstance(e, ConnectionError)
                with self.lock:
                    self.state.update(errors=self.state['errors']+(not offline), consecutive_errors=self.state['consecutive_errors']+1,
                                      last_error=f'{type(e).__name__}: {str(e)[:160]}', backoff_s=backoff,
                                      service='waiting_for_car' if offline else 'retrying')
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 401:
                    self.logged_in = False
                time.sleep(backoff)

    def snapshot(self, limit):
        with self.lock:
            latest_at = self.latest_jpeg[0] if self.latest_jpeg else None
            return dict(state=dict(self.state, now=time.time(), latest_frame_at=latest_at, calibrated_tilts=sorted(CALIBRATION)),
                        history=list(self.history)[-limit:], map=self.map_view, search_memory=self.search_view)

    def save_map(self, run, view):
        """Keep the map of each run (the newest 20) and show the latest on the dashboard."""
        with self.lock:
            self.map_view = dict(view, run=run)
        try:
            self.maps.mkdir(parents=True, exist_ok=True)
            (self.maps/f'{run}.json').write_text(json.dumps(self.map_view), encoding='utf-8')
            for old in sorted(self.maps.glob('*.json'), key=lambda f: f.stat().st_mtime)[:-20]:
                old.unlink()
        except OSError:
            pass

    def save_heading_check(self, run, state, current, verification=None):
        """Private, bounded evidence: latest panorama and reference/current pair per run."""
        root = self.args.data/'heading-checks'
        folder = root/run
        if folder.resolve().parent != root.resolve():
            raise ValueError('Invalid run path')
        folder.mkdir(parents=True, exist_ok=True)
        route = state['memory'].get('route_goal') or state['memory'].get('last_route_goal') or {}
        reference = state.get('route_reference')
        if reference:
            (folder/'reference.jpg').write_bytes(reference)
        (folder/'current.jpg').write_bytes(current)
        if verification is None:
            for i, entry in enumerate(state['memory'].get('panorama', [])):
                raw = state.get('views', {}).get(entry.get('frame'))
                if raw:
                    (folder/f'view-{i+1:02d}.jpg').write_bytes(raw)
            (folder/'panorama.json').write_text(json.dumps(state['memory'].get('panorama', []), ensure_ascii=False), encoding='utf-8')
        evidence = dict(at=time.time(), frame=state['steps'], route=route, verification=verification)
        (folder/'latest.json').write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
        # Only files created by this diagnostic writer are removed; never follow directories/symlinks.
        folders = sorted((d for d in root.iterdir() if d.is_dir() and not d.is_symlink()), key=lambda d: d.stat().st_mtime)
        for old in folders[:-20]:
            if old.resolve().parent != root.resolve():
                continue
            for file in old.iterdir():
                if file.is_file() and not file.is_symlink() and file.suffix in ('.jpg', '.json'):
                    file.unlink()
            if not any(old.iterdir()):
                old.rmdir()


class Autopilot:
    """Per-run memory for the server-owned alpha; one observation and decision per step."""
    def __init__(self, radar, gemma_url, frames=3):
        self.radar, self.gemma_url, self.frames = radar, gemma_url, max(1, frames)
        self.http = httpx.Client(trust_env=False)
        self.runs = {}
        self.lock = threading.Lock()

    def understand(self, goal):
        """Czech instruction -> English target description and a Czech name for speech."""
        schema = {'type': 'object', 'additionalProperties': False,
                  'properties': {'target': {'type': 'string', 'maxLength': 160}, 'name_cs': {'type': 'string', 'maxLength': 60}},
                  'required': ['target', 'name_cs']}
        body = {'messages': [{'role': 'user', 'content': 'A small floor robot got this instruction in Czech: "'+goal+'". '
                              'target = short English description of the object or place it must reach (what the camera should look for). '
                              'name_cs = the same thing as a short Czech noun phrase in accusative (e.g. "práh dveří", "misku").'}],
                'temperature': .1, 'max_tokens': 120, 'chat_template_kwargs': {'enable_thinking': False},
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'goal', 'strict': True, 'schema': schema}}}
        try:
            r = self.http.post(self.gemma_url, json=body, timeout=20)
            r.raise_for_status()
            answer = json.loads(r.json()['choices'][0]['message']['content'])
            if answer['target'].strip() and answer['name_cs'].strip():
                return answer['target'].strip(), answer['name_cs'].strip()
        except Exception:  # noqa: BLE001 - fall back to the raw instruction
            pass
        return goal, goal

    def start(self, goal):
        goal = ' '.join(str(goal or '').split())[:200]
        if not goal:
            raise ValueError('Napiš úkol.')
        target, name = self.understand(goal)
        journal_file = place_memory.journal_path(self.radar.maps.parent, goal)
        journal = place_memory.load(journal_file, goal)
        run = os.urandom(8).hex()
        with self.lock:
            # One car, one alpha: a new start replaces any previous run.
            self.runs = {}
            self.runs[run] = dict(goal=goal, target=target, name=name, memory={}, seen_before=False, steps=0, last=time.time(),
                                  map=RoverMap(), planned=[], run=run, place_journal=journal,
                                  journal_file=journal_file)
        with self.radar.lock:
            self.radar.search_view = place_memory.public(journal)
        self.radar.save_map(run, self.runs[run]['map'].view())
        return dict(run=run, target=target, name=name, say=f'Začínám. Hledám {name}.')

    def depth_scan(self, jpeg, tilt=TILT):
        with self.radar.infer_lock:
            result = scan(infer(self.radar.session, jpeg), tilt)
            trim_memory()
            return result

    def acquire_observation(self, state, executed, telemetry, sonar_cm, servo):
        """Up to five stationary captures; preview map fusion without committing votes.

        Only the chosen observation reaches Gemma and the real map. A failed
        alignment stays uncertain after the budget; retries never relax its gate.
        """
        attempts, hashes, chosen = [], set(), None
        for number in range(1, RADAR_ATTEMPTS+1):
            if number > 1:
                time.sleep(.55)  # ESP preview is capped at 2 fps; do not hammer it.
                telemetry = self.radar.get('api/status').json().get('telemetry') or {}
                sonar_cm = telemetry.get('distance_cm')
            age = telemetry.get('telemetry_age_ms')
            if (telemetry.get('connected') is not True or telemetry.get('servo') != servo
                    or telemetry.get('motor_byte') != 0 or type(age) not in (int, float)
                    or not 0 <= age <= 500):
                if hasattr(self.radar, 'note'):
                    self.radar.note(radar_retry=dict(status='aborted', attempt=number,
                        limit=RADAR_ATTEMPTS, reason='car_state_changed', attempts=attempts))
                raise ConnectionError('Opakované snímání vyžaduje stojící auto a čerstvou telemetrii.')
            if hasattr(self.radar, 'note'):
                self.radar.note(radar_retry=dict(status='capturing', attempt=number,
                    limit=RADAR_ATTEMPTS, attempts=list(attempts)))
            try:
                jpeg = self.radar.get('api/camera.jpg').content
                captured_at = time.time()
                fingerprint = hashlib.sha256(jpeg).hexdigest()
                if fingerprint in hashes:
                    attempts.append(dict(attempt=number, reason='duplicate_frame'))
                    continue
                hashes.add(fingerprint)
                scans = [self.depth_scan(jpeg, servo)]
                # Preserve the normal multi-frame filter. Extra attempts use one
                # fresh image and can never multiply confidence in the map.
                for _ in range(self.frames-1 if number == 1 and 'scan' not in state['memory'] else 0):
                    try:
                        scans.append(self.depth_scan(self.radar.get('api/camera.jpg').content, servo))
                    except (httpx.HTTPError, ValueError, cv2.error):
                        break
                result = fuse(scans)
            except (httpx.HTTPError, ValueError, cv2.error) as exc:
                attempts.append(dict(attempt=number, reason='capture_or_depth_failed', error=type(exc).__name__))
                continue
            gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
            if gray is None:
                attempts.append(dict(attempt=number, reason='invalid_image'))
                continue
            thumb = cv2.resize(gray, (64, 48), interpolation=cv2.INTER_AREA).astype(np.int16)
            moves = self.executed_moves(executed) or []
            prev = dict(state['prev'], forward=any(a == 'forward' for _, _, a in moves)) if state.get('prev') else None
            preview = copy.deepcopy(state['map'])
            preview.move(moves, stalled=stalled(prev, thumb, sonar_cm))
            preview.observe(result, sonar_cm, servo)
            directions = result.get('directions') or []
            reason = ('floor_not_detected' if result.get('floor_detected') is False
                      else 'unknown_scan' if not directions or all(d.get('status') == 'unknown' for d in directions)
                      else preview.localization['reason'] if preview.localization.get('fusion') == 'withheld_ambiguous_pose'
                      else None)
            attempts.append(dict(attempt=number, reason=reason or 'usable',
                map_fusion=preview.localization.get('fusion'), captured_at=captured_at))
            chosen = dict(jpeg=jpeg, result=result, captured_at=captured_at, sonar_cm=sonar_cm,
                          telemetry=telemetry, selected_attempt=number, retry_reason=reason)
            if reason is None:
                break
        diagnostic = dict(status='accepted' if chosen and chosen['retry_reason'] is None else 'unresolved',
                          attempt=len(attempts), limit=RADAR_ATTEMPTS, attempts=attempts,
                          selected_attempt=chosen['selected_attempt'] if chosen else None,
                          reason=chosen['retry_reason'] if chosen else 'no_valid_frame')
        if hasattr(self.radar, 'note'):
            self.radar.note(radar_retry=diagnostic)
        if chosen is None:
            raise ConnectionError('Ani po pěti pokusech nemám použitelný snímek radaru.')
        return chosen, diagnostic

    def guess_where(self, state, entries):
        """Montage of the look-around views, numbered; Gemma picks where the target most likely is."""
        tiles = []
        for i, e in enumerate(entries):
            raw = state.get('views', {}).get(e.get('frame'))
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
            tile = np.zeros((180, 240, 3), np.uint8) if img is None else cv2.resize(img, (240, 180), interpolation=cv2.INTER_AREA)
            cv2.rectangle(tile, (0, 0), (44, 34), (0, 0, 0), -1)
            cv2.putText(tile, str(i+1), (4, 27), cv2.FONT_HERSHEY_SIMPLEX, .9, (255, 255, 255), 2)
            tiles.append(tile)
        if not tiles:
            return None
        while len(tiles) % 4:
            tiles.append(np.zeros_like(tiles[0]))
        montage = np.vstack([np.hstack(tiles[r:r+4]) for r in range(0, len(tiles), 4)])
        ok, buf = cv2.imencode('.jpg', montage, [cv2.IMWRITE_JPEG_QUALITY, 80])
        images = {i+1:state['views'][e['frame']] for i,e in enumerate(entries) if e.get('frame') in state.get('views', {})}
        journal = state['place_journal']
        recognition = place_memory.recognize(journal, images)
        context = place_memory.context(journal, recognition)
        answer = ask_where(buf.tobytes(), state['target'], entries, self.http, self.gemma_url,
                           search_context=context, queued=True) if ok else None
        place_memory.record_panorama(journal, f"{state['run']}:{state['steps']}:panorama", entries,
                                     images, answer, recognition, state['steps']+1)
        state['place_decision'] = dict(step=state['steps']+1, input=context, answer=answer,
                                       area_id=journal.get('current_area_id'))
        # plan_step selects the final view after this hint. Keep its pixels until
        # that choice is pinned as route_reference (and keep scan views on retries).
        trim_memory()
        return (answer['choice'], answer['reason']) if isinstance(answer, dict) else None

    def request_step(self, run, seq=None, executed=None, wait=STEP_WAIT_S):
        """Queue of one: at most one step per run computes at a time.

        A request while a step is running joins it instead of starting another (a proxy timeout
        and the page's retry used to run two steps at once). A request repeating the seq of the
        finished step gets the same answer again, so the car never executes a step twice. When
        the step takes longer than `wait`, the answer is {'pending': True}: ask again."""
        if seq is not None and (type(seq) is not int or seq < 0):
            raise ValueError('Neplatné číslo kroku.')
        with self.lock:
            state = self.runs.get(run)
            if not state:
                raise KeyError('Neznámý nebo prošlý běh alfy.')
            job = state.get('job')
            running = job is not None and not job['done'].is_set()
            repeat = job is not None and seq is not None and job['seq'] == seq and job['error'] is None
            if not running and not repeat:
                job = state['job'] = dict(seq=seq, done=threading.Event(), result=None, error=None)
                threading.Thread(target=self._run_step, args=(run, job, executed), daemon=True).start()
        if not job['done'].wait(wait):
            return dict(pending=True, seq=job['seq'], reason='Ještě přemýšlím nad krokem.')
        if job['error'] is not None:
            raise job['error']
        return dict(job['result'], seq=job['seq'])

    def _run_step(self, run, job, executed):
        # A failed inference/log write must not apply the same motion report again on retry.
        # The job contains threading primitives; the bounded navigation state does not.
        with self.lock:
            state = self.runs.get(run)
            before = copy.deepcopy({k: v for k, v in state.items() if k != 'job'}) if state else None
        try:
            job['result'] = self.step(run, executed)
        except Exception as e:  # noqa: BLE001 - handed to whoever asks for this step
            if before is not None:
                with self.lock:
                    if self.runs.get(run) is state:
                        for key in list(state):
                            if key != 'job':
                                state.pop(key)
                        state.update(before)
            job['error'] = e
        finally:
            if hasattr(self.radar, 'note'):
                self.radar.note(ai_pending_since=None)
            job['done'].set()

    @staticmethod
    def executed_moves(executed):
        """What the cockpit reports it really drove since the last step: [[speed, count, action], ...]."""
        if not isinstance(executed, list):
            return None
        moves = []
        for item in executed[:40]:
            if (isinstance(item, list) and len(item) == 3 and type(item[0]) is int and type(item[1]) is int
                    and 0 < item[1] <= MAX_FORWARD_PULSES and item[2] in MOVES):
                moves.append((item[0], item[1], item[2]))
        return moves

    def move_map(self, state, executed, stalled_now=False):
        """An absent execution report is not evidence that a planned move happened."""
        moves = self.executed_moves(executed) or []
        state['map'].move(moves, stalled=stalled_now)
        actual = [m for m in moves if not (stalled_now and m[2] in ('forward', 'climb'))]
        note_motion(state['memory'], actual)
        state['memory']['heading'] = state['map'].heading
        state['planned'] = []
        return moves

    def step(self, run, executed=None):
        with self.lock:
            state = self.runs.get(run)
        if not state:
            raise KeyError('Neznámý nebo prošlý běh alfy.')
        self.radar.autopilot_at = time.time()
        radar = self.radar
        status = radar.get('api/status').json()
        t = status.get('telemetry') or {}
        if not t.get('connected'):
            raise ConnectionError('Auto je offline.')
        servo = t.get('servo')
        if servo not in (TILT, LOOK_TILT):
            self.move_map(state, executed)
            return dict(actions=[], reason=f'Kamera není na {TILT}°, nastavuji ji.', say=None, done=False, retilt=True, next_tilt=TILT)
        sonar = [t.get('distance_cm')]
        for _ in range(2):
            time.sleep(.2)
            t = radar.get('api/status').json().get('telemetry') or {}
            sonar.append(t.get('distance_cm'))
        numbers = sorted(d for d in sonar if isinstance(d, (int, float)))
        sonar_cm = numbers[len(numbers)//2] if numbers else None
        started = time.monotonic()
        observation, radar_retry = self.acquire_observation(state, executed, t, sonar_cm, servo)
        jpeg, result = observation['jpeg'], observation['result']
        captured_at, sonar_cm, t = observation['captured_at'], observation['sonar_cm'], observation['telemetry']
        if hasattr(radar, 'note'):
            radar.note(ai_pending_since=time.time())
        seen = ask_gemma(jpeg, state['target'], self.http, self.gemma_url, queued=True)
        # The look-around panorama is measured at driving height only.
        # Keep a small view per step for the look-around montage (only the last 20).
        views = state.setdefault('views', {})
        views[state['steps']] = jpeg
        pinned = {e.get('frame') for e in state['memory'].get('scan', {}).get('entries', [])}
        for old in sorted(views)[:-20]:
            if old not in pinned:
                views.pop(old)
        state['memory']['frame'] = state['steps']
        state['memory']['frame_at'] = captured_at
        place_memory.observe(state['place_journal'], f"{run}:{state['steps']}:view", seen, state['steps']+1, servo, captured_at)
        gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        thumb = cv2.resize(gray, (64, 48), interpolation=cv2.INTER_AREA).astype(np.int16) if gray is not None else None
        executed = self.executed_moves(executed) or []
        prev = dict(state['prev'], forward=any(a == 'forward' for _, _, a in executed)) if state.get('prev') else None
        stuck = thumb is not None and stalled(prev, thumb, sonar_cm)
        # Incorporate the last executed motion and this scan BEFORE choosing another move.
        # Matching in observe() compares against the previous map, then fuses the new scan.
        rmap = state['map']
        self.move_map(state, [list(m) for m in executed], stalled_now=stuck)
        rmap.observe(result, sonar_cm, servo)
        state['memory']['heading'] = rmap.heading
        if stuck:
            rmap.stalled(state['steps'])
        sill = threshold_cm(seen) if servo == TILT else None
        if sill is not None and sill <= 120:
            rmap.mark_blocked(max(sill, 10.), 'threshold', 'Práh: '+str((seen or {}).get('note', '')), state['steps'])
        if seen and seen.get('target_visible') and seen.get('confidence') in ('medium', 'high'):
            rmap.event('target', str(seen.get('note', '')), state['steps'])
        map_context = rmap.navigation_context()
        if state['memory'].get('route_goal') and (seen or {}).get('animal_or_person_close'):
            route_result = dict(actions=[], reason='Něco živého je blízko; ověření směru počká, auto stojí.', done=False, verified=False)
        elif state['memory'].get('route_goal') and type(sonar_cm) in (int, float) and 0 < sonar_cm < 30:
            route_result = restart_panorama(state['memory'], 'Před otočením je překážka blízko; nejdřív krátce couvnu.')
            route_result['actions'] = [(180, 1, 'backward')]
        elif state['memory'].get('route_goal') and stuck:
            route_result = restart_panorama(state['memory'], 'Pokus o vjezd do vybraného směru nevedl k pohybu.')
        else:
            route_result = check_route(state['memory'], state.get('route_reference'), jpeg, servo, executed,
                                       lambda reference, current: ask_heading_match(reference, current, self.http, self.gemma_url, queued=True))
        verification = (route_result or {}).get('verification')
        if route_result and not route_result['verified']:
            actions, reason, done = route_result['actions'], route_result['reason'], False
        elif route_result and route_result['verified']:
            actions, reason, done = decide(result, sonar_cm, seen, state['memory'], map_context=map_context)
            reason = 'Směr ověřen porovnáním snímků. '+reason
            if actions and not any(a in ('forward', 'climb') for _, _, a in actions):
                # Geometry/target tracking now requires a different direction. Do
                # not pretend this is entry into the verified panorama route.
                previous = state['memory'].pop('route_goal')
                state['memory']['last_route_goal'] = dict(previous, status='replanned', reason=reason)
        elif servo == TILT:
            actions, reason, done = plan_step(result, sonar_cm, seen, state['memory'],
                                              guess=lambda entries: self.guess_where(state, entries), map_context=map_context)
        else:
            actions, reason, done = decide(result, sonar_cm, seen, state['memory'], map_context=map_context)
        route = state['memory'].get('route_goal')
        if route and route.get('reference_frame') != state.get('route_reference_frame'):
            state['route_reference_frame'] = route.get('reference_frame')
            state['route_reference'] = views.get(route.get('reference_frame'))
            place_memory.note_plan(state['place_journal'], route.get('reference_frame'), reason)
            if hasattr(radar, 'save_heading_check'):
                radar.save_heading_check(run, state, jpeg)
        if verification and hasattr(radar, 'save_heading_check'):
            radar.save_heading_check(run, state, jpeg, verification)
        recover_stuck = stuck and not done and not (seen or {}).get('animal_or_person_close')
        if recover_stuck and (not route_result or route_result.get('rescan')):
            actions, _ = unstick(state['memory'])
            reason = ('Asi jsem uvízl (práh, kabely?): po jízdě vpřed se obraz ani ultrazvuk nezměnil. Tudy to nejde, '
                      'zapamatuji si to; couvám a zkusím jinou stranu.')
        # Unsure at driving height (nothing free / stuck): look down first instead of acting blind.
        unsure = servo == TILT and not done and (recover_stuck or 'nikde volno' in reason)
        look_down = unsure and not route_result and state['steps']-state.get('looked_at', -9) >= 3
        if look_down:
            state['looked_at'] = state['steps']
            if recover_stuck:  # back off first, then look down before the next decision
                reason += ' Pak se podívám pod sebe.'
            else:
                actions, reason = [], 'Nevím kudy dál; skloním kameru, posvítím si pod sebe a podívám se zblízka.'
        next_tilt = LOOK_TILT if look_down else TILT
        core_heading = state['memory'].get('heading', 0.)
        if reason.startswith('Rozhlédl jsem se') and state['memory'].get('panorama'):
            chosen_heading = core_heading
            for speed, count, action in actions:
                # Display the intended turn, without committing it as an executed pose.
                if action in ('rotate_left', 'rotate_right'):
                    chosen_heading += (1 if action == 'rotate_left' else -1)*turn_degrees(speed, action)*count
            rmap.panorama(state['memory']['panorama'], chosen_heading % 360, state['memory'].get('thought', ''),
                          state['steps'], memory_heading=core_heading)
        waited_scene_changed = False
        if time.monotonic()-started > 20:
            fresh_status = radar.get('api/status').json().get('telemetry') or {}
            fresh_image = radar.get('api/camera.jpg').content
            age = fresh_status.get('telemetry_age_ms')
            waited_scene_changed = (fresh_status.get('connected') is not True or fresh_status.get('servo') != servo
                or type(age) not in (int,float) or age > 500 or age < 0
                or bool(fresh_status.get('motor_byte')) or not same_waiting_scene(jpeg, fresh_image))
            if waited_scene_changed:
                actions, done, next_tilt = [], False, TILT
                reason = 'Po čekání na AI se obraz nebo stav auta změnil. Stojím a pořídím nové pozorování.'
        state['planned'] = [a for a in actions if a[2] in MOVES]
        state['prev'] = dict(thumb=thumb, sonar_cm=sonar_cm, forward=any(a == 'forward' for _, _, a in actions))
        visible = bool(seen and seen.get('target_visible') and seen.get('confidence') in ('medium', 'high'))
        say = None
        if waited_scene_changed:
            say = 'Po čekání se znovu rozhlédnu.'
        elif look_down:
            say = 'Podívám se pod sebe.'
        elif recover_stuck:
            say = 'Tudy to nejde. Couvám a zkusím to jinudy.'
        elif reason.startswith('Rozhlížím se kolem sebe (1/'):
            say = f'Hledám {state["name"]}. Rozhlédnu se kolem sebe a popřemýšlím, kde by asi tak mohla být.'
        elif reason.startswith('Rozhlédl jsem se a přemýšlím'):
            say = state['memory'].get('thought') or 'Zkusím to tudy.'
        elif reason.startswith('Rozhlédl jsem se: nejlepší'):
            say = 'Rozhlédl jsem se. Pojedu tudy.'
        elif reason.startswith('Přede mnou je práh') and not state.get('said_threshold'):
            state['said_threshold'] = True
            say = 'Je tu práh, ten nepřejedu. Zkusím to jinudy.'
        elif 'jsem u cíle' in reason:
            say = f'Jsem u cíle. {state["name"].capitalize()} je přede mnou. Zastavuji.'
        elif 'živého' in reason and state['memory'].get('animal_waits') == 1:
            say = 'Je tu něco živého. Zastavuji a čekám, až to odejde.'
        elif 'jinou cestu' in reason:
            say = 'Pořád to tu je. Zkusím jinou cestu.'
        elif visible and not state['seen_before']:
            say = f'Vidím {state["name"]}. Jedu k tomu.'
        elif done:
            say = 'Nikde nevidím volnou cestu. Končím.'
        with self.lock:
            state.update(seen_before=visible, steps=state['steps']+1, last=time.time())
        for d in result['directions']:
            d['x'] = round(320-math.tan(math.radians(d['bearing_deg']))*FOCAL_PX, 1)
        ahead = [d['distance_cm'] for d in result['directions'] if abs(d['bearing_deg']) <= 8 and d['status'] == 'obstacle']
        result['ahead_cm'] = round(float(np.median(ahead)), 1) if ahead else None
        think_ms = round((time.monotonic()-started)*1000)
        map_view = rmap.view()
        map_view['radar_retry'] = radar_retry
        map_view['navigation'] = state['memory'].get('map_decision')
        map_view['route_goal'] = copy.deepcopy(state['memory'].get('route_goal') or state['memory'].get('last_route_goal'))
        self.radar.save_map(run, map_view)
        search_view = place_memory.public(state['place_journal'])
        memory_decision = state.get('place_decision')
        if memory_decision and memory_decision['step'] != state['steps']:
            memory_decision = None
        radar.store(dict(at=time.time(), connected=True, armed=status.get('armed'), estop=status.get('estop'), mode=status.get('mode'),
                         sonar_cm=sonar_cm, servo=t.get('servo'), rssi=t.get('rssi'), uptime_ms=t.get('uptime_ms'), firmware=t.get('firmware'),
                         wifi_bssid=t.get('wifi_bssid'), wifi_roam_count=t.get('wifi_roam_count'), telemetry_age_ms=t.get('telemetry_age_ms'),
                         relay=(t.get('relay') or {}) and {k: t['relay'].get(k) for k in ('connections', 'disconnects', 'connect_attempts', 'last_error')},
                         radar=result, radar_retry=radar_retry, inference_ms=think_ms,
                         executed=executed, pose=map_view['pose'], localization=map_view.get('localization'),
                         map_decision=state['memory'].get('map_decision'), route_goal=map_view['route_goal'], heading_verification=verification,
                         search_memory_decision=memory_decision,
                         task=dict(id='alpha-'+run, status='done' if done else 'running', step=state['steps'], goal=state['goal'],
                                   action=' + '.join(f'{a}×{n}' for _, n, a in actions) or 'stát', reason=reason, say=say or '',
                                   assessment=(seen or {}).get('note', ''), progress=None)), jpeg)
        place_memory.save(state['journal_file'], state['place_journal'])
        with radar.lock:
            radar.search_view = search_view
        return dict(actions=actions, reason=reason, say=say, done=done, seen=seen, sonar_cm=sonar_cm,
                    search_memory=search_view, radar_retry=radar_retry,
                    ahead_cm=result['ahead_cm'], think_ms=think_ms, step=state['steps'],
                    clearance_cm=state['memory'].get('clearance_cm', 0) if servo == TILT else 0,
                    localization=map_view.get('localization'), map_decision=state['memory'].get('map_decision'),
                    path_clear=(seen or {}).get('path_ahead_clear'), tilt=servo, next_tilt=next_tilt,
                    heading_verification=verification, route_goal=map_view['route_goal'],
                    retilt=bool((route_result or {}).get('retilt')))


def task_summary(task):
    last = task.get('last_result') or {}
    assessment = last.get('assessment') or {}
    return dict(id=task.get('id'), status=task.get('status'), step=task.get('current_step'), goal=(task.get('goal') or '')[:160],
                action=last.get('action'), reason=(last.get('decision_reason') or '')[:240], say=(last.get('say') or '')[:200],
                assessment=(assessment.get('summary') or '')[:240], progress=assessment.get('progress'))


def handler_for(radar, autopilot):
    page = (HERE/'dashboard.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, kind):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = self.path.split('?')[0].rstrip('/')
            length = int(self.headers.get('Content-Length') or 0)
            if length > 4096:
                return self.send(413, b'{"detail":"Too large"}', 'application/json')
            try:
                body = json.loads(self.rfile.read(length) or b'{}')
                if path.endswith('/autopilot/start'):
                    answer = autopilot.start(body.get('goal'))
                elif path.endswith('/autopilot/step'):
                    answer = autopilot.request_step(str(body.get('run', '')), body.get('seq'), body.get('executed'))
                else:
                    return self.send(404, b'{"detail":"Not found"}', 'application/json')
                self.send(200, json.dumps(answer, ensure_ascii=False).encode(), 'application/json')
            except (KeyError, ValueError) as e:
                self.send(409, json.dumps({'detail': str(e).strip("'")}, ensure_ascii=False).encode(), 'application/json')
            except Exception as e:  # noqa: BLE001 - the page retries; never crash the service
                self.send(503, json.dumps({'detail': f'Alfa teď nemůže pokračovat: {type(e).__name__}'}, ensure_ascii=False).encode(), 'application/json')

        def do_GET(self):
            path = self.path.split('?')[0].rstrip('/').rsplit('/', 1)[-1] or 'index'
            if path in ('index', 'radar'):
                self.send(200, page, 'text/html; charset=utf-8')
            elif path == 'state.json':
                self.send(200, json.dumps(radar.snapshot(300), ensure_ascii=False).encode(), 'application/json')
            elif path == 'latest.jpg' and radar.latest_jpeg:
                self.send(200, radar.latest_jpeg[1], 'image/jpeg')
            else:
                self.send(404, b'Not found', 'text/plain')
    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    rover = HERE.parent/'data'
    p.add_argument('--base', default='http://127.0.0.1:28741/')
    p.add_argument('--ca', default='')
    p.add_argument('--key', type=Path, default=rover/'access.key')
    p.add_argument('--model', type=Path, default=rover/'models/depth-anything-v2-small/model.onnx')
    p.add_argument('--data', type=Path, default=HERE/'data')
    p.add_argument('--listen', default='127.0.0.1')
    p.add_argument('--port', type=int, default=27261)
    p.add_argument('--interval', type=float, default=3.)
    p.add_argument('--max-backoff', type=float, default=30.)
    p.add_argument('--threads', type=int, default=6)
    p.add_argument('--history', type=int, default=600)
    p.add_argument('--log-bytes', type=int, default=5_000_000)
    p.add_argument('--gemma', default=os.environ.get('ROVER_LLM_URL', 'http://127.0.0.1:8080').rstrip('/') + '/v1/chat/completions')
    p.add_argument('--frames', type=int, default=3, help='Frames fused per autopilot step.')
    args = p.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    radar = Radar(args)
    threading.Thread(target=radar.run, daemon=True).start()
    ThreadingHTTPServer((args.listen, args.port), handler_for(radar, Autopilot(radar, args.gemma, args.frames))).serve_forever()


if __name__ == '__main__':
    main()
