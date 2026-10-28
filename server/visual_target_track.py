"""Experimental current-pixel point memory, never a map or goal-completion oracle.

A partial threshold is only a point hypothesis, not an inferred door centre or
threshold line. Negative/out-of-FOV observations do not erase its protective
region. All poses are caller-provided calibrated odometry estimates.
"""
from copy import deepcopy
import math
import time


def _number(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)


def _pose(p):
    return isinstance(p,dict) and all(_number(p.get(k)) for k in ('x','y','heading'))


def _unit(v):return _number(v) and 0<=v<=1


class VisualTargetTrack:
    RECOVERY_ROTATIONS=16
    RECOVERY_FAILURES=4
    RECOVERY_TILT_ATTEMPTS=2
    RECOVERY_SPEED=150
    RECOVERY_DURATION_MS=round((2*math.pi/16)/(1.2*(150-45)/135)*1000)

    def __init__(self,clock=time.monotonic,max_age_s=90,max_uncertainty_m=.6):
        self.clock=clock;self.max_age_s=max_age_s;self.max_uncertainty_m=max_uncertainty_m
        self._point=None;self._seen=None;self._uncertainty=0.;self._commands=0
        self._observations=0;self._reason='No current localized target has been learned.'
        self._current_localized=False
        self._recovery_rotations=0;self._recovery_failures=0;self._recovery_tilts=0

    @staticmethod
    def _fresh(t):
        return (isinstance(t,dict) and t.get('connected') is not False
                and _number(t.get('telemetry_age_ms')) and 0<=t['telemetry_age_ms']<=500
                and _number(t.get('servo')) and 35<=t['servo']<=145)

    def update(self,perception,pixelmetadata,telemetry,estimated_pose):
        self._current_localized=False
        p=perception if isinstance(perception,dict) else {}
        m=pixelmetadata if isinstance(pixelmetadata,dict) else {}
        point=p.get('target_pixel')
        valid=(p.get('goal_visible') is True and p.get('threshold_visible') is True
               and _unit(p.get('confidence')) and p['confidence']>=.8
               and m.get('status')=='localized' and bool(m.get('provenance'))
               and _unit(m.get('quality')) and m['quality']>0
               and isinstance(point,dict) and _unit(point.get('x')) and _unit(point.get('y'))
               and self._fresh(telemetry) and _pose(estimated_pose))
        if not valid:return self.snapshot()
        horizon=240-math.tan(math.radians((90-telemetry['servo'])*.55))*280
        below=point['y']*480-horizon
        if below<=2:return self.snapshot()
        forward=.22*280/below
        if not .05<=forward<=4:return self.snapshot()
        left=(.5-point['x'])*640/280*forward
        heading=estimated_pose['heading']
        x=estimated_pose['x']+forward*math.cos(heading)-left*math.sin(heading)
        y=estimated_pose['y']+forward*math.sin(heading)+left*math.cos(heading)
        uncertainty=.04+forward*.04+2*.22*280/(below*below)
        bounds=m.get('bounds',{})
        if isinstance(bounds,dict) and (bounds.get('left',1)<=.01 or bounds.get('right',0)>=.99):
            uncertainty+=.12  # partial visible segment; no invented threshold line
        self._point=dict(x=x,y=y);self._uncertainty=uncertainty
        self._seen=float(self.clock());self._commands=0;self._observations+=1
        self._current_localized=True
        self._recovery_rotations=0;self._recovery_failures=0;self._recovery_tilts=0
        self._reason='Current localized floor point; calibrated pose and projection remain uncertain.'
        return self.snapshot()

    def note_command(self,command):
        """Increase uncertainty after a command; never infer movement or acceptance."""
        if self._point is None or not isinstance(command,dict):return
        action=command.get('action')
        accepted=command.get('acceptance') in ('accepted','transport_accepted')
        if accepted and action in ('forward','backward','left','right','rotate_left','rotate_right','tilt'):
            self._current_localized=False
        # Search proposals do not consume budget. Only the caller's actual
        # command result does; rejected/unknown attempts cannot become scan steps.
        if command.get('recovery_scan') is True and action!='stop':
            valid_tilt=(action=='tilt' and command.get('tilt')==55
                        and command.get('speed')==0)
            valid_rotation=(action=='rotate_left' and command.get('speed')==self.RECOVERY_SPEED
                            and command.get('duration_ms')==self.RECOVERY_DURATION_MS
                            and command.get('tilt')==55)
            if valid_tilt:self._recovery_tilts+=1
            if accepted and valid_rotation:self._recovery_rotations+=1
            elif not (accepted and valid_tilt):self._recovery_failures+=1
        if action not in ('forward','backward','left','right','rotate_left','rotate_right'):return
        self._commands+=1
        if command.get('acceptance') not in ('accepted','transport_accepted'):
            self._uncertainty+=.10;return
        speed=command.get('speed');duration=command.get('duration_ms')
        if not (_number(speed) and _number(duration)) or not 0<=speed<=180 or not 0<=duration<=2000:
            self._uncertainty+=.10;return
        scale=max(0,(speed-45)/135)
        self._uncertainty+=.01+(0 if action.startswith('rotate_') else .35*scale*duration/1000*.08)

    def suggest_search(self,telemetry):
        """Bounded nontranslating scan when CURRENT localization is unavailable.

        The protected point's coordinates do not set the search direction. This
        is a separate leftward scan: tilt first, then at most sixteen accepted
        22.5-degree pulses. Fresh localization alone renews the search budget.
        The caller must retain independent current-hazard and command guards.
        """
        if self._point is None:return None
        t=telemetry if isinstance(telemetry,dict) else {}
        def command(action,reason):
            return dict(action=action,speed=0,duration_ms=100,tilt=t.get('servo',90),
                        reason=reason,provenance='bounded_visual_target_reacquisition',
                        recovery_scan=True,candidate_goal_reached=False)
        if not self._fresh(t):
            return command('stop','Fresh telemetry and a valid current camera angle are required for target search.')
        if t.get('estop') is True:
            return command('stop','Emergency stop is active; target search remains stopped.')
        if self._current_localized and self._usable():return None
        if self._recovery_rotations>=self.RECOVERY_ROTATIONS:
            return command('stop','Target reacquisition exhausted sixteen accepted scan rotations; current localization is still missing.')
        if self._recovery_failures>=self.RECOVERY_FAILURES:
            return command('stop','Target reacquisition exhausted its bounded failed attempts; no successful scan step is assumed.')
        if abs(t['servo']-55)>.5:
            if self._recovery_tilts>=self.RECOVERY_TILT_ATTEMPTS:
                return command('stop','Camera did not settle at the search angle after bounded tilt attempts.')
            result=command('tilt','Look down first for bounded visual reacquisition; no translation from memory.')
            result['tilt']=55
            return result
        result=command('rotate_left','Scan one new direction for CURRENT visual localization; old point coordinates do not steer this search.')
        result.update(speed=self.RECOVERY_SPEED,duration_ms=self.RECOVERY_DURATION_MS,tilt=55)
        return result

    def _usable(self):
        return (self._point is not None and 0<=float(self.clock())-self._seen<=self.max_age_s
                and self._uncertainty<=self.max_uncertainty_m and self._commands<=80)

    def suggest_orientation(self,pose,telemetry):
        if self._point is None:return None
        def command(action,reason,**extra):
            return dict(action=action,speed=0,duration_ms=100,tilt=telemetry.get('servo',90),
                        reason=reason,provenance='remembered_visual_point_estimate',**extra)
        if not _pose(pose) or not self._fresh(telemetry) or not self._usable():
            return command('stop','Remembered point is uncertain or telemetry stale; reacquire a current localization.')
        if abs(telemetry['servo']-55)>.5:
            result=command('tilt','Look down to reacquire the remembered floor point; no motion from memory.')
            result['tilt']=55;return result
        error=(math.atan2(self._point['y']-pose['y'],self._point['x']-pose['x'])-pose['heading']+math.pi)%(2*math.pi)-math.pi
        if abs(error)<.08:return None
        result=command('rotate_left' if error>0 else 'rotate_right','Reorient only to reacquire current visual evidence.')
        result.update(speed=150,duration_ms=max(100,min(500,round(abs(error)/(1.2*(150-45)/135)*1000))))
        return result

    def command_allowed(self,cmd,pose):
        if not isinstance(cmd,dict):return False
        action=cmd.get('action')
        if action not in ('forward','backward','left','right'):return True
        if self._point is None:return True
        if not _pose(pose) or not self._usable():return False
        speed=cmd.get('speed');duration=cmd.get('duration_ms')
        if not (_number(speed) and _number(duration)) or not 0<=speed<=180 or not 0<=duration<=2000:return False
        length=.35*max(0,(speed-45)/135)*duration/1000
        angle=pose['heading']+{'forward':0,'backward':math.pi,'left':math.pi/2,'right':-math.pi/2}[action]
        dx=length*math.cos(angle);dy=length*math.sin(angle)
        px=self._point['x']-pose['x'];py=self._point['y']-pose['y']
        fraction=max(0,min(1,(px*dx+py*dy)/(length*length))) if length else 0
        clearance=math.hypot(px-fraction*dx,py-fraction*dy)
        # 0.30 m is a protective centre-to-point radius including the chassis;
        # this is not a fitted threshold line or semantic arrival assertion.
        return clearance>.30+self._uncertainty

    def snapshot(self):
        return deepcopy(dict(provenance='current_pixels_plus_calibrated_pose_estimate',
            point=self._point,uncertainty_m=self._uncertainty,
            age_s=None if self._seen is None else max(0,float(self.clock())-self._seen),
            observations=self._observations,commands_since_observation=self._commands,
            usable=self._usable(),current_localized=self._current_localized,
            recovery=dict(accepted_rotations=self._recovery_rotations,rotation_limit=self.RECOVERY_ROTATIONS,
                failed_attempts=self._recovery_failures,failed_attempt_limit=self.RECOVERY_FAILURES,
                tilt_attempts=self._recovery_tilts,tilt_attempt_limit=self.RECOVERY_TILT_ATTEMPTS,
                exhausted=self._recovery_rotations>=self.RECOVERY_ROTATIONS or self._recovery_failures>=self.RECOVERY_FAILURES),
            reason=self._reason,goal_completed=False))
