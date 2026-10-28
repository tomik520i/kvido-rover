"""Supervised autonomous alpha: camera radar drives, Gemma only looks.

Each cycle: one fresh frame + ultrasonic samples while standing; in parallel
the camera radar (Depth Anything + server/camera_radar.py, tilt 72) and one
Gemma question about THIS frame only (no history: with history Gemma copies
its previous answers, docs/JAZYK_PROMPTU_AB.md). Movement uses the measured
pulses (PWM 180/150/130 x 300 ms: 5.8/4.1/2.0 cm, 27/18.5/11 deg).

  target visible   turn towards it, approach, stop ~45 cm before it
  not visible      drive where the radar sees free floor, else keep turning
  cat/person near  stand still and look again
  operator STOP    always ends the run; a relay drop is recovered instead

Requires the operator next to the car. --frames DIR replays saved JPEGs
without any car or motor command (decision check only).
"""
import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
import time

import httpx
import numpy as np
import onnxruntime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'tools'))
from server.camera_radar import scan  # noqa: E402
from server.autopilot_core import TILT, ask_gemma, decide, free_space, turn_plan  # noqa: E402,F401
from camera_depth_probe import MODEL, infer, imread  # noqa: E402

class Brain:
    def __init__(self, threads=4):
        options = onnxruntime.SessionOptions(); options.intra_op_num_threads = threads
        self.session = onnxruntime.InferenceSession(str(MODEL), options, providers=['CPUExecutionProvider'])
        self.http = httpx.Client(trust_env=False)
        self.pool = concurrent.futures.ThreadPoolExecutor(2)

    def observe(self, jpeg, target):
        import cv2
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        radar_job = self.pool.submit(lambda: scan(infer(self.session, bgr), TILT))
        seen_job = self.pool.submit(ask_gemma, jpeg, target, self.http)
        return radar_job.result(), seen_job.result()


def replay(args, brain):
    memory = {}
    for path in sorted(Path(args.frames).glob('*.jpg')):
        radar, seen = brain.observe(path.read_bytes(), args.target)
        actions, reason, done = decide(radar, None, seen, memory)
        print(json.dumps(dict(frame=path.name, seen=seen, actions=actions, reason=reason, done=done), ensure_ascii=False))


class Voice:
    """Czech speech via the cockpit's TTS (Verity), played on this computer; never blocks driving."""
    def __init__(self, car, enabled):
        self.car, self.enabled, self.last = car, enabled and sys.platform == 'win32', None
        self.pool = concurrent.futures.ThreadPoolExecutor(1)

    def say(self, text):
        if not self.enabled or text == self.last:
            return
        self.last = text

        def speak():
            try:
                import winsound
                r = self.car.c.post('api/tts', json={'text': text}, timeout=20)
                if r.status_code == 200 and r.content[:4] == b'RIFF':
                    winsound.PlaySound(r.content, winsound.SND_MEMORY)
            except Exception:  # noqa: BLE001 - speech is optional
                pass
        self.pool.submit(speak)


def recover(car, seconds):
    """After a relay drop: wait for fresh telemetry, then take control again."""
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        try:
            t = car.status()['telemetry']
            if t.get('connected') and t.get('telemetry_age_ms', 9e9) <= 500:
                car.alive = True
                car.arm()
                car.send('tilt', 0, 100, TILT); time.sleep(1)
                return True
        except Exception:  # noqa: BLE001 - keep waiting
            car.alive = False
        time.sleep(3)
    return False


def drive(args, brain):
    from calibration_drive import Car
    out = ROOT/'reports/private'/f"autopilot-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    car = Car(out)
    voice = Voice(car, not args.quiet)
    memory, recoveries, seen_before = {}, 0, False
    car.record(event='start', target=args.target, max_steps=args.max_steps)
    voice.say(f'Začínám. Hledám {args.name}.')
    try:
        car.arm()
        car.send('tilt', 0, 100, TILT); time.sleep(1)
        for step in range(1, args.max_steps+1):
            try:
                if not args.dark:
                    car.send('light_on', 0, 100, TILT)  # Gemma cannot see in the dark; firmware switches off after 60 s
                car.settle(.8)
                reading = car.sonar(.8)
                jpeg = car.c.get('api/camera.jpg'); jpeg.raise_for_status(); jpeg = jpeg.content
                (out/f'step-{step:03d}.jpg').write_bytes(jpeg)
                started = time.monotonic()
                radar, seen = brain.observe(jpeg, args.target)
                actions, reason, done = decide(radar, reading['median'], seen, memory)
                car.record(event='decision', step=step, sonar=reading, seen=seen, actions=actions, reason=reason, done=done,
                           think_s=round(time.monotonic()-started, 2),
                           radar=[(d['bearing_deg'], d['status'][0], d['distance_cm']) for d in radar['directions']])
                print(f'[{step}] {reason}', flush=True)
                visible = bool(seen and seen.get('target_visible') and seen.get('confidence') in ('medium', 'high'))
                if 'jsem u cíle' in reason:
                    voice.say(f'Jsem u cíle. Zastavuji.')
                elif 'živého' in reason:
                    voice.say('Je tu něco živého. Zastavuji a čekám, až to odejde.')
                elif 'jinou cestu' in reason:
                    voice.say('Pořád to tu je. Zkusím jinou cestu.')
                elif visible and not seen_before:
                    voice.say(f'Vidím {args.name}. Jedu k tomu.')
                elif done:
                    voice.say('Nikde nevidím volnou cestu. Končím.')
                seen_before = visible
                if done:
                    break
                for speed, count, action in actions:
                    for _ in range(count):
                        if action == 'forward':
                            check = car.sonar(.4)['median']
                            if check is not None and check <= 35:
                                raise StopIteration
                        if not car.send(action, speed, 300):
                            raise RuntimeError('command refused')
                        car.settle(.3)
            except StopIteration:
                car.record(event='sonar_guard', step=step)
            except Exception as e:  # noqa: BLE001 - recover from relay drops, never from operator STOP
                status = car.status()
                operator_stop = status.get('estop') and status['telemetry'].get('connected') and 'uživatel' in str(status.get('last_reason', ''))
                car.record(event='error', step=step, error=f'{type(e).__name__}: {e}', operator_stop=bool(operator_stop))
                if operator_stop:
                    voice.say('Zastaveno.')
                    print('Konec: STOP operátora.', flush=True)
                    break
                if recoveries >= args.max_recoveries:
                    print('Konec: příliš mnoho výpadků.', flush=True)
                    break
                recoveries += 1
                voice.say('Spojení vypadlo. Stojím a čekám na obnovení.')
                print(f'Výpadek ({type(e).__name__}); stojím a čekám na obnovení spojení.', flush=True)
                if not recover(car, args.recover_seconds):
                    print('Konec: auto se nevrátilo.', flush=True)
                    break
                voice.say('Spojení je zpátky. Pokračuji.')
                car.record(event='recovered', step=step, recoveries=recoveries)
        else:
            voice.say('Došel mi limit kroků. Zastavuji.')
    finally:
        car.stop()
        voice.pool.shutdown(wait=True)
    print('saved', out)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--target', default='a shiny silver/chrome metal dog bowl on the floor')
    p.add_argument('--max-steps', type=int, default=25)
    p.add_argument('--name', default='misku', help='Czech name of the target for speech (accusative).')
    p.add_argument('--max-recoveries', type=int, default=10)
    p.add_argument('--recover-seconds', type=float, default=180)
    p.add_argument('--quiet', action='store_true', help='No speech.')
    p.add_argument('--dark', action='store_true', help='Do not switch the car light on.')
    p.add_argument('--frames', help='Replay saved JPEGs (no car, no motor commands).')
    a = p.parse_args()
    assert 1 <= a.max_steps <= 60
    brain = Brain()
    (replay if a.frames else drive)(a, brain)


if __name__ == '__main__':
    main()
