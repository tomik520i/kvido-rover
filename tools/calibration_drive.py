"""Supervised physical calibration through the public manual-drive API.

Requires the operator next to the car. Only short manual pulses within the
server and firmware limits; every frame is saved with the ultrasonic samples
taken while the car stands still. Any error, Ctrl+C or end of plan sends STOP,
which leaves the car disarmed with emergency stop active.

Plans:
  wall     back away from a wall the car faces, then approach it again;
           sonar change per pulse calibrates translation.
  approach drive towards the wall the car faces, stopping at 40 cm.
  rotate   turn on the spot in equal pulses and capture each view.

--speeds repeats a plan for each PWM value; --tilts captures every stop at each
camera angle (the ultrasonic sensor is fixed, so its reading is shared).
"""
import argparse
import os
import json
import ssl
import statistics
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get('ROVER_PANEL_URL', 'http://127.0.0.1:28741/')


class Car:
    def __init__(self, out):
        self.c = httpx.Client(base_url=BASE, verify=ssl.create_default_context(), trust_env=False, timeout=20)
        self.c.post('api/login', json={'key': (ROOT/'data/access.key').read_text().strip()}).raise_for_status()
        self.out = out
        self.alive = True
        self.log = (out/'timeline.jsonl').open('a', encoding='utf-8')

    def status(self):
        r = self.c.get('api/status'); r.raise_for_status(); return r.json()

    def record(self, **row):
        row['at'] = time.time()
        self.log.write(json.dumps(row, ensure_ascii=False)+'\n'); self.log.flush()
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def _beat(self):
        while self.alive:
            try:
                started = time.monotonic()
                if not self.c.post('api/heartbeat').json().get('active'):
                    self.record(event='lease_lost', latency_ms=round(1000*(time.monotonic()-started)))
                    self.alive = False
            except Exception as exc:
                self.record(event='heartbeat_error', error=type(exc).__name__)
                self.alive = False
            time.sleep(.5)

    def arm(self):
        s = self.status()
        assert s['telemetry'].get('connected') and not s['simulation'], 'Car is not connected.'
        assert s['mode'] == 'manual', 'Panel must be in manual mode.'
        self.c.post('api/arm').raise_for_status()
        threading.Thread(target=self._beat, daemon=True).start()

    def send(self, action, speed=0, duration_ms=100, tilt=90):
        for attempt in range(3):
            if not self.alive:
                raise RuntimeError('Control lease lost.')
            r = self.c.post('api/drive', json=dict(action=action, speed=speed, duration_ms=duration_ms, tilt=tilt))
            body = r.json() if r.headers.get('content-type', '').startswith('application/json') else r.text[:200]
            self.record(event='drive', action=action, speed=speed, duration_ms=duration_ms, tilt=tilt,
                        http=r.status_code, response=body, attempt=attempt)
            # A refusal for momentarily stale telemetry never moved the car; retrying is safe.
            if r.status_code == 409 and 'telemetrie' in str(body):
                time.sleep(.6); continue
            return r.status_code == 200
        return False

    def sonar(self, seconds=1.2):
        """Distinct fresh samples while standing; nulls are counted, never replaced."""
        samples, seen, end = [], set(), time.monotonic()+seconds
        while time.monotonic() < end:
            t = self.status()['telemetry']
            key = (t.get('ultra') or {}).get('seq')
            if t.get('telemetry_age_ms', 9e9) <= 500 and key not in seen:
                seen.add(key); samples.append(t.get('distance_cm'))
            time.sleep(.12)
        numbers = [d for d in samples if isinstance(d, (int, float))]
        # uptime/rssi tell an ESP reset apart from a Wi-Fi or relay drop.
        return dict(samples=samples, median=statistics.median(numbers) if numbers else None, nulls=len(samples)-len(numbers),
                    servo=t.get('servo'), motor_byte=t.get('motor_byte'), uptime_ms=t.get('uptime_ms'), rssi=t.get('rssi'))

    def settle(self, seconds):
        # Relay delivery can lag; wait until the motor reports idle, then settle.
        end = time.monotonic()+3
        while time.monotonic() < end:
            t = self.status()['telemetry']
            if t.get('telemetry_age_ms', 9e9) <= 500 and t.get('motor_byte') == 0:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('No fresh confirmation that motors stopped.')
        time.sleep(seconds)

    def snap(self, label, tilts=(None,), settle=1.):
        self.settle(settle)
        for tilt in tilts:
            if tilt is not None:
                if not self.send('tilt', 0, 100, tilt):
                    raise RuntimeError('Camera tilt refused.')
                time.sleep(.9)
            reading = self.sonar()
            frame = self.c.get('api/camera.jpg?fresh=1'); frame.raise_for_status()
            name = label if tilt is None else f'{label}-t{tilt}'
            (self.out/f'{name}.jpg').write_bytes(frame.content)
            self.record(event='snap', label=name, tilt=tilt, **reading)
        return reading

    def stop(self):
        self.alive = False
        try:
            self.c.post('api/stop').raise_for_status()
            self.record(event='stop', confirmed=True)
        finally:
            self.log.close()


def wall(car, speed, ms, pulses, max_cm, tilts, prefix='wall'):
    readings = [car.snap(f'{prefix}-000-start', tilts)]
    for i in range(1, pulses+1):
        if readings[-1]['median'] and readings[-1]['median'] >= max_cm:
            break
        if not car.send('backward', speed, ms):
            break
        readings.append(car.snap(f'{prefix}-{i:03d}-back', tilts))
    approach(car, speed, ms, pulses, tilts, prefix, readings)


def approach(car, speed, ms, pulses, tilts, prefix='wall', readings=None, step=1, stop_cm=40):
    readings = readings or [car.snap(f'{prefix}-000-start', tilts)]
    for i in range(1, pulses+1):
        moved = True
        for _ in range(step):
            last = readings[-1]['median'] if moved else car.sonar(.6)['median']
            # Firmware stops forward at <=20 cm; keep a margin for one pulse of travel.
            if last is None or last <= stop_cm:
                return
            moved = car.send('forward', speed, ms)
            if not moved:
                return
            car.settle(.3)
        readings.append(car.snap(f'{prefix}-{i:03d}-fwd', tilts))


def rotate(car, speed, ms, pulses, tilts, prefix='rot', action='rotate_right'):
    car.snap(f'{prefix}-000-start', tilts)
    for i in range(1, pulses+1):
        if not car.send(action, speed, ms):
            break
        car.snap(f'{prefix}-{i:03d}', tilts)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('plan', choices=('wall', 'approach', 'rotate'))
    p.add_argument('--speeds', type=lambda v: [int(x) for x in v.split(',')], default=[180])
    p.add_argument('--ms', type=int, default=300)
    p.add_argument('--pulses', type=int, default=6)
    p.add_argument('--max-cm', type=int, default=150)
    p.add_argument('--step', type=int, default=1, help='approach: pulses between two captures')
    p.add_argument('--stop-cm', type=int, default=40, help='approach: no forward pulse at or below this sonar value')
    p.add_argument('--tilts', type=lambda v: [int(x) for x in v.split(',')], default=[90])
    p.add_argument('--direction', choices=('right', 'left'), default='right')
    p.add_argument('--name', default=time.strftime('%Y%m%d-%H%M%S'))
    a = p.parse_args()
    assert all(0 < s <= 180 for s in a.speeds) and all(35 <= t <= 145 for t in a.tilts)
    assert 50 <= a.ms <= 350 and 1 <= a.pulses <= 24 and 1 <= a.step <= 4
    out = ROOT/'reports/private'/f'calibration-{a.plan}-{a.name}'
    out.mkdir(parents=True, exist_ok=True)
    car = Car(out)
    try:
        car.arm()
        for speed in a.speeds:
            prefix = f'{a.plan}-s{speed}'
            if a.plan == 'wall':
                wall(car, speed, a.ms, a.pulses, a.max_cm, a.tilts, prefix)
            elif a.plan == 'approach':
                approach(car, speed, a.ms, a.pulses, a.tilts, prefix, step=a.step, stop_cm=max(35, a.stop_cm))
            else:
                rotate(car, speed, a.ms, a.pulses, a.tilts, prefix, 'rotate_'+a.direction)
        car.send('tilt', 0, 100, 90)
    finally:
        car.stop()
    print('saved', out)


if __name__ == '__main__':
    main()
