"""Rough top-down map of one alpha run: dead-reckoned pose + occupancy grid from the camera radar.

The pose comes from the pulses the cockpit actually executed (forward ~5.8 cm, turn ~30 deg at
PWM 180; current priors in motion_calibration), so it drifts. The grid has 5 cm
cells around the start; radar rays mark free floor up to a contact and the contact as occupied,
trusted only up to ~1.2 m (beyond that the radar underestimates). Stalls and thresholds become
'blocked' cells the car cannot pass even when the floor looks free.

Uncertainty values are conservative heuristics, not calibrated centimetre accuracy.
Geometrically ambiguous rejected scans are withheld: map growth can pause until
better geometry arrives, rather than allowing a scan to confirm its own insertion.

Frame: x = forward from the start pose, y = to the left, heading in degrees (+ = left/CCW).
"""
import base64
import math
from collections import deque

if __package__:
    from .scan_matching import align_scan
    from .motion_calibration import TURN_DEG, turn_degrees
else:
    from scan_matching import align_scan
    from motion_calibration import TURN_DEG, turn_degrees

import numpy as np

CELL_CM = 5
SIZE = 160                    # 8 x 8 m around the start
HALF = SIZE//2
STEP_CM = {180: 5.8, 150: 4.1, 130: 2.0}    # one pulse from standstill (calibration drives)
CHAIN_CM = 5.5                # each further pulse of a back-to-back forward run (live: ~16-18 cm per 3)
RANGE_CM = {72: 120., 55: 60., 90: 150.}
FREE, OCCUPIED, LIMIT = -.4, .85, 4.
CAR_RADIUS_CM = 8

UNKNOWN, FREE_CELL, OCCUPIED_CELL, VISITED_CELL, BLOCKED_CELL = 0, 1, 2, 3, 4


class RoverMap:
    def __init__(self):
        self.odds = np.zeros((SIZE, SIZE), np.float32)
        self.visited = np.zeros((SIZE, SIZE), bool)
        self.blocked = np.zeros((SIZE, SIZE), bool)
        self.x = self.y = self.heading = 0.
        self.trail = [(0., 0.)]
        self.events = []
        self.panoramas = []
        self.steps = 0
        self._position_uncertainty_cm=8.
        self._heading_uncertainty_deg=5.
        self._last_aligned_step=None
        self._observations=deque(maxlen=16)
        self.localization={'status':'unlocalized','reason':'no_scan_yet','confidence':0.,
                           'correction':{'dx_cm':0.,'dy_cm':0.,'dheading_deg':0.}}
        self._visit()

    # -- motion -------------------------------------------------------------------------------
    def move(self, actions, stalled=False):
        """Apply executed pulses [(speed, count, action)]. A stall means the forward run did not
        happen (wheels caught on a threshold/cables): the pose stays where it was."""
        for speed, count, action in actions or []:
            count = max(0, int(count))
            if not count:continue
            previous_x,previous_y=self.x,self.y
            if action in ('rotate_left', 'rotate_right'):
                sign = 1 if action == 'rotate_left' else -1
                turned=turn_degrees(speed, action)*count
                self.heading = (self.heading+sign*turned) % 360
                self._heading_uncertainty_deg=min(90.,self._heading_uncertainty_deg+turned*.08)
                self._position_uncertainty_cm=min(250.,self._position_uncertainty_cm+.1*count)
            elif action in ('forward', 'backward', 'climb'):
                if action != 'backward':
                    if stalled:
                        continue
                    dist = STEP_CM.get(speed, STEP_CM[180])+CHAIN_CM*max(0, count-1)
                else:
                    dist = -STEP_CM.get(speed, STEP_CM[180])*count
                a = math.radians(self.heading)
                self.x += dist*math.cos(a)
                self.y += dist*math.sin(a)
                self._position_uncertainty_cm=min(250.,self._position_uncertainty_cm+abs(dist)*.18+.5)
            else:
                continue
            self.trail.append((round(self.x, 1), round(self.y, 1)))
            self.trail=self.trail[-600:]
            self._visit_segment(previous_x,previous_y,self.x,self.y)

    def _cell(self, x, y):
        i, j = int(round(x/CELL_CM))+HALF, int(round(y/CELL_CM))+HALF
        return (i, j) if 0 <= i < SIZE and 0 <= j < SIZE else None

    def _visit_segment(self,x0,y0,x1,y1):
        # Clip before sampling: arbitrarily long executed chains cannot create
        # unbounded CPU work or mark cells outside this fixed map.
        dx,dy=x1-x0,y1-y0;t0,t1=0.,1.
        lower=-HALF*CELL_CM-CAR_RADIUS_CM
        upper=(SIZE-1-HALF)*CELL_CM+CAR_RADIUS_CM
        for origin,delta in ((x0,dx),(y0,dy)):
            if abs(delta)<1e-9:
                if not lower<=origin<=upper:return
            else:
                a,b=sorted(((lower-origin)/delta,(upper-origin)/delta))
                t0=max(t0,a);t1=min(t1,b)
                if t1<t0:return
        length=math.hypot(dx,dy)*(t1-t0)
        count=min(SIZE*2,max(1,int(math.ceil(length/CELL_CM))))
        for t in np.linspace(t0,t1,count+1):self._visit(x0+t*dx,y0+t*dy)

    def _visit(self,x=None,y=None):
        r = int(math.ceil(CAR_RADIUS_CM/CELL_CM))
        centre = self._cell(self.x if x is None else x,self.y if y is None else y)
        if centre is None:
            return
        i, j = centre
        self.visited[max(0, i-r):i+r+1, max(0, j-r):j+r+1] = True
        patch=self.odds[max(0,i-r):i+r+1,max(0,j-r):j+r+1]
        # Driving/odometry is not evidence that a contradictory obstacle vanished.
        patch[patch<.6]=np.minimum(patch[patch<.6],FREE*3)

    # -- observation --------------------------------------------------------------------------
    def _ray(self, bearing, free_to, hit=None, weight=1., cells=None):
        own=cells is None
        free,hits=({}, {}) if own else cells
        a=math.radians(self.heading+bearing);ca,sa=math.cos(a),math.sin(a)
        d=CAR_RADIUS_CM
        while d<free_to:
            c=self._cell(self.x+d*ca,self.y+d*sa)
            if c:free[c]=max(free.get(c,0.),weight)
            d+=CELL_CM/2
        if hit is not None:
            c=self._cell(self.x+hit*ca,self.y+hit*sa)
            if c:hits[c]=max(hits.get(c,0.),weight)
        if own:self._fuse(free,hits)

    def _fuse(self,free,hits):
        # One vote per cell per observation, regardless of overlapping ray density.
        # A contact overrides any ray calling the same cell free in this scan.
        for c,weight in free.items():
            if c not in hits:self.odds[c]=max(-LIMIT,self.odds[c]+FREE*weight)
        for c,weight in hits.items():self.odds[c]=min(LIMIT,self.odds[c]+OCCUPIED*weight)

    def observe(self, radar, sonar_cm=None, tilt=72):
        """Align to the previous map first, then fuse this observation once per cell."""
        self.steps+=1;reach=RANGE_CM.get(tilt,100.)
        directions=list((radar or {}).get('directions',[]))[:96]
        diagnostic=align_scan(self.odds,directions,(self.x,self.y,self.heading),reach,CELL_CM)
        if diagnostic['status']=='aligned':
            correction=diagnostic['correction']
            self.x+=correction['dx_cm'];self.y+=correction['dy_cm']
            self.heading=(self.heading+correction['dheading_deg'])%360
            self._position_uncertainty_cm=diagnostic['position_uncertainty_cm']
            self._heading_uncertainty_deg=diagnostic['heading_uncertainty_deg']
            self._last_aligned_step=self.steps
            if self.trail:self.trail[-1]=(round(self.x,1),round(self.y,1))
        self.localization=diagnostic
        self._observations.append({'step':self.steps,'status':diagnostic['status'],
                                   'reason':diagnostic['reason'],'hits':diagnostic['hits']})
        if diagnostic['reason'] in ('low_overlap','competing_modes_or_weak_constraint',
                                     'correction_at_search_limit','insufficient_improvement',
                                     'weak_match_confidence'):
            self.localization['fusion']='withheld_ambiguous_pose'
            return
        self.localization['fusion']='aligned' if diagnostic['status']=='aligned' else 'odometry'
        free,hits={},{}
        for ray in directions:
            if not isinstance(ray,dict):continue
            dist,status,bearing=ray.get('distance_cm'),ray.get('status'),ray.get('bearing_deg',0.)
            if type(bearing) not in (int,float) or not math.isfinite(bearing):continue
            valid=type(dist) in (int,float) and math.isfinite(dist) and dist>0
            if status=='near':self._ray(bearing,0,hit=12.,cells=(free,hits))
            elif status=='obstacle' and valid:
                if dist<=reach:self._ray(bearing,dist-CELL_CM,hit=dist,cells=(free,hits))
                else:self._ray(bearing,reach,cells=(free,hits))
            elif status=='open' and valid:self._ray(bearing,min(dist,reach),cells=(free,hits))
        if type(sonar_cm) in (int,float) and math.isfinite(sonar_cm) and sonar_cm>0:
            for b in (-6,0,6):
                self._ray(b,min(sonar_cm,150.)-CELL_CM,hit=sonar_cm if sonar_cm<=150 else None,
                          weight=.5,cells=(free,hits))
        self._fuse(free,hits)

    def mark_blocked(self, distance_cm, kind, text='', step=None, width_deg=15):
        """Something the car cannot pass at distance_cm ahead (stall, threshold)."""
        for b in np.linspace(-width_deg, width_deg, 7):
            a = math.radians(self.heading+b)
            c = self._cell(self.x+distance_cm*math.cos(a), self.y+distance_cm*math.sin(a))
            if c:
                self.blocked[c] = True
        self.event(kind, text, step, distance_cm=distance_cm)

    def stalled(self, step=None):
        """The last forward run did not move the car (move(..., stalled=True) skipped it): mark the spot ahead."""
        self.mark_blocked(12., 'stuck', 'Uvízl jsem tady.', step)

    def event(self, kind, text='', step=None, **extra):
        self.events.append(dict(kind=kind, x=round(self.x, 1), y=round(self.y, 1), heading=round(self.heading, 1),
                                text=text, step=step, **extra))
        self.events = self.events[-200:]

    def panorama(self, entries, chosen_heading=None, thought='', step=None, memory_heading=0.):
        """Look-around rays (free cm per heading). Entry headings are in the decision core's frame;
        re-based on the map heading via the core heading at the end of the scan."""
        offset = self.heading-memory_heading
        self.panoramas.append(dict(x=round(self.x, 1), y=round(self.y, 1), step=step, thought=thought,
                                   chosen=None if chosen_heading is None else round((chosen_heading+offset) % 360, 1),
                                   rays=[[round((e['heading']+offset) % 360, 1), round(min(e['free'], 250.), 1),
                                          bool(e.get('passage')), bool(e.get('target')), bool(e.get('threshold') or e.get('tried'))]
                                         for e in entries]))
        self.panoramas = self.panoramas[-30:]

    # -- output -------------------------------------------------------------------------------
    def grid(self):
        g = np.full((SIZE, SIZE), UNKNOWN, np.uint8)
        g[self.odds <= -.4] = FREE_CELL
        g[self.visited] = VISITED_CELL
        g[self.odds >= .6] = OCCUPIED_CELL
        g[self.blocked] = BLOCKED_CELL
        return g

    def view(self):
        """Compact JSON for the dashboard: the known part of the grid as base64 bytes (row = x index)."""
        g = self.grid()
        known = np.argwhere(g != UNKNOWN)
        if not len(known):known=np.array([[HALF,HALF]])
        (i0, j0), (i1, j1) = known.min(0), known.max(0)
        i0, j0, i1, j1 = max(0, i0-4), max(0, j0-4), min(SIZE-1, i1+4), min(SIZE-1, j1+4)
        crop = np.ascontiguousarray(g[i0:i1+1, j0:j1+1])
        return dict(cell_cm=CELL_CM, origin=[int(i0-HALF), int(j0-HALF)], shape=list(crop.shape),
                    cells=base64.b64encode(crop.tobytes()).decode(), pose=[round(self.x, 1), round(self.y, 1), round(self.heading, 1)],
                    trail=self.trail[-600:], events=self.events[-60:], panoramas=self.panoramas[-8:], steps=self.steps,
                    localization={**self.localization,
                                  'position_uncertainty_cm':round(self._position_uncertainty_cm,2),
                                  'heading_uncertainty_deg':round(self._heading_uncertainty_deg,2),
                                  'reference':'previous_occupancy_map_not_ground_truth',
                                  'uncertainty_kind':'heuristic_not_calibrated_accuracy',
                                  'source':'scan_to_prior_map' if self.localization.get('status')=='aligned' else 'local_odometry'})


    def navigation_context(self):
        """Conservative swept-footprint hints; unknown cells provide zero clearance.

        Bearings are relative to the current estimated heading, positive left.
        Novelty counts only known-free, unvisited cells, never unknown space.
        """
        directions=[]
        for bearing in range(-180,180,15):
            angle=math.radians(self.heading+bearing);co,si=math.cos(angle),math.sin(angle)
            corridor=set();outside_count=0;prefix=0.;stopped=False;is_blocked=False
            for distance in range(5,81,5):
                slice_clear=True
                for lateral in (-8.,-4.,0.,4.,8.):
                    c=self._cell(self.x+distance*co-lateral*si,self.y+distance*si+lateral*co)
                    if c is None:outside_count+=1;slice_clear=False;continue
                    corridor.add(c)
                    obstacle=self.blocked[c] or self.odds[c]>=.6
                    is_blocked|=bool(obstacle)
                    if obstacle or self.odds[c]>-.4:slice_clear=False
                if not stopped and slice_clear:prefix=float(distance)
                else:stopped=True
            known=visited=novel=0
            for c in corridor:
                if c is None:continue
                is_known=self.odds[c]<=-.4 or self.odds[c]>=.6 or self.blocked[c]
                known+=bool(is_known);visited+=bool(self.visited[c])
                novel+=bool(self.odds[c]<=-.4 and not self.visited[c] and not self.blocked[c])
            total=max(1,len(corridor)+outside_count)
            directions.append({'bearing_deg':bearing,'known_fraction':round(known/total,3),
                               'visited_fraction':round(visited/total,3),'blocked':bool(is_blocked),
                               'clearance_cm':round(max(0.,prefix-CAR_RADIUS_CM-self._position_uncertainty_cm),2),'novelty':round(novel/total,3)})
        ambiguous=self.localization.get('reason') in (
            'straight_wall_ambiguity','competing_modes_or_weak_constraint','weak_match_confidence',
            'low_overlap','correction_at_search_limit','insufficient_improvement')
        # These are short-range map hints, not a localization guarantee. Until
        # matched, bounded dead reckoning may still describe a known corridor.
        reliable=(not ambiguous and self._position_uncertainty_cm<=15 and self._heading_uncertainty_deg<=12)
        matched=self.localization.get('status')=='aligned'
        confidence=self.localization.get('confidence',0.) if matched else max(0.,.35*(1-self._position_uncertainty_cm/30))
        return {'heading':round(self.heading,2),'reliable':bool(reliable),
                'source':'scan_to_prior_map' if matched else 'local_odometry',
                'confidence':round(confidence,3),'uncertainty_kind':'heuristic_not_calibrated_accuracy',
                'position_uncertainty_cm':round(self._position_uncertainty_cm,2),
                'corridor_extent_cm':80,'visited_kind':'estimated_executed_motion','directions':directions}
