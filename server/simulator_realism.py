"""Opt-in physical imperfections on top of the ideal RoverSimulator.

The ideal simulator stays byte-identical because saved replays pin its hash.
Values below are estimates, not a calibration of the real car: the physical
door test returned a number in only 29 of 78 sonar samples, and no forward or
rotation distance per pulse has been measured yet.
"""
import math
import random
import time

from .simulator import RoverSimulator


class RealisticRoverSimulator(RoverSimulator):
    REALISM = dict(
        # HC-SR04: ~15 deg half-cone, no echo from grazing surfaces, dropouts.
        # Saved UNO counters show 1.5-14 % nulls and repeatable readings up to
        # 344 cm, so by default the whole range counts as reliable. Pass
        # sonar_reliable_m=.5 for the pessimistic "only ~50 cm" scenario.
        sonar_cone=.26, sonar_max_incidence=.7, sonar_max_m=3., sonar_dropout=.3,
        sonar_noise_cm=1.5, sonar_noise_ratio=.02,
        sonar_reliable_m=3., sonar_far_dropout=.7, sonar_far_noise_ratio=.15,
        # Mecanum on a household floor: battery/surface gain per run, spread and
        # yaw drift per pulse, weaker strafing, static friction at each start.
        speed_gain=(.7,1.15), rotation_gain=(.6,1.1), pulse_noise=.15,
        strafe_efficiency=.65, yaw_drift_rad_s=.08, start_delay_s=.08)

    def __init__(self, clock=time.monotonic, realism=None):
        self.realism = {**self.REALISM, **(realism or {})}
        super().__init__(clock)

    def reset(self, scenario='doorway', seed=1):
        r = self.realism
        self._rng = random.Random(int(seed))
        self._speed_gain = self._rng.uniform(*r['speed_gain'])
        self._rotation_gain = self._rng.uniform(*r['rotation_gain'])
        self._pulse = dict(gain=1., yaw=0.)
        self.MAX_SPEED = RoverSimulator.MAX_SPEED
        return super().reset(scenario, seed)

    def _distance(self):
        if not self.sensor_valid: return None
        r = self.realism
        if self._rng.random() < r['sonar_dropout']: return None
        # Only surfaces facing the sensor return an echo; the nearest one wins.
        echoes = []
        for i in range(9):
            angle = self.heading+r['sonar_cone']*(i/4-1)
            t, segment = self._ray_hit(angle)
            if segment is None: continue
            ax,ay,bx,by = segment
            facing = abs(math.cos(angle)*(by-ay)-math.sin(angle)*(bx-ax))/math.hypot(bx-ax,by-ay)
            if math.acos(min(1,facing)) <= r['sonar_max_incidence']: echoes.append(t)
        if not echoes: return None
        value = min(echoes)-self.RADIUS
        far = value > r['sonar_reliable_m']
        if far and self._rng.random() < r['sonar_far_dropout']: return None
        ratio = r['sonar_far_noise_ratio'] if far else r['sonar_noise_ratio']
        value += self._rng.gauss(0, r['sonar_noise_cm']/100+ratio*value)
        return round(value*100) if .02 <= value <= r['sonar_max_m'] else None

    def _ray_hit(self, angle):
        dx,dy = math.cos(angle),math.sin(angle)
        best, segment = 20., None
        for s in self._segments():
            ax,ay,bx,by = s
            sx,sy = bx-ax,by-ay
            den = dx*sy-dy*sx
            if abs(den) < 1e-9: continue
            qx,qy = ax-self.x,ay-self.y
            t = (qx*sy-qy*sx)/den
            u = (qx*dy-qy*dx)/den
            if t >= 0 and 0 <= u <= 1 and t < best: best, segment = t, s
        return best, segment

    def command(self, action, speed=0, duration_ms=1000, tilt=90):
        result = super().command(action, speed, duration_ms, tilt)
        if result['accepted'] and action in self.ACTIONS and action != 'stop':
            r = self.realism
            rotation = action.startswith('rotate_')
            gain = (self._rotation_gain if rotation else self._speed_gain)*max(0, 1+self._rng.gauss(0, r['pulse_noise']))
            if action in ('left','right'): gain *= r['strafe_efficiency']
            self._pulse = dict(gain=gain, yaw=0. if rotation else self._rng.gauss(0, r['yaw_drift_rad_s']))
            self.MAX_SPEED = RoverSimulator.MAX_SPEED*(1 if rotation else gain)
            # Static friction swallows the start of every pulse.
            self.deadline -= r['start_delay_s']
        return result

    def _advance(self):
        heading, start, action, moving = self.heading, self._last, self.action, self.speed
        end = min(max(float(self.clock()), self._last), self.deadline)
        super()._advance()
        if not moving: return
        if action.startswith('rotate_'):
            delta = (self.heading-heading+math.pi)%(2*math.pi)-math.pi
            self.heading = (heading+delta*self._pulse['gain'])%(2*math.pi)
        else:
            self.heading = (self.heading+self._pulse['yaw']*max(0, end-start))%(2*math.pi)
