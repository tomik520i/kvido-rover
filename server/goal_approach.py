"""Opt-in simulator adapter: approach a remembered pixel point in estimated map.

Uses SensorNavigator's internal estimated-map APIs deliberately; no simulator,
world pose, map geometry, semantic model or device API is imported. A point is
not a threshold line, and arriving at its standoff never completes the goal.
"""
import math


def _number(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)


class GoalApproachNavigator:
    def __init__(self,navigator):
        self.navigator=navigator;self._edge=None;self._action=None;self._excluded_edges={};self._segment=None

    @staticmethod
    def _result(action='stop',status='unavailable',reason='',speed=0,duration_ms=100,tilt=90,**extra):
        return dict(action=action,speed=speed,duration_ms=duration_ms,tilt=tilt,
                    approach_status=status,reason=reason,goal_completed=False,
                    provenance='remembered_pixel_point_estimated_map_approach',**extra)

    @staticmethod
    def _segment_clear(x,y,angle,length,point,radius):
        dx=math.cos(angle)*length;dy=math.sin(angle)*length
        px=point['x']-x;py=point['y']-y
        u=max(0,min(1,(px*dx+py*dy)/(length*length))) if length else 0
        return math.hypot(px-u*dx,py-u*dy)>radius

    def next_command(self,target,telemetry,standoff_m=.45):
        nav=self.navigator;t=telemetry if isinstance(telemetry,dict) else {}
        servo=t.get('servo',90);servo=servo if _number(servo) else 90
        def result(**kw):
            if self._segment:
                kw.setdefault('planned_edge',dict(origin=list(self._segment['origin_cell']),next_cell=list(self._segment['next_cell'])))
                kw.setdefault('planned_body_action',self._segment.get('action'))
            return self._result(tilt=servo,**kw)
        if not nav._fresh(t) or t.get('connected') is False or t.get('estop') is True:
            return result(reason='Fresh connected telemetry and released emergency stop are required.')
        target=target if isinstance(target,dict) else {};point=target.get('point')
        uncertainty=target.get('uncertainty_m')
        if (target.get('usable') is not True or not isinstance(point,dict)
                or not all(_number(point.get(k)) for k in ('x','y'))
                or not _number(uncertainty) or not 0<=uncertainty<=.6
                or not _number(standoff_m) or not .30<=standoff_m<=2):
            return result(reason='Remembered point is absent, expired or too uncertain; obtain current localization.')
        protection=.30+uncertainty
        stand=max(standoff_m,protection+nav.CELL*.75)
        current=math.hypot(point['x']-nav.x,point['y']-nav.y)
        if current<=stand+nav.CELL*.5:
            self._segment=None
            return result(status='reacquire',reason='Estimated standoff reached; stop and obtain current visual evidence, not completion.',estimated_standoff_m=stand)
        segment=self._segment
        if segment and (math.dist(segment['origin_xy'],(nav.x,nav.y))>.025
                        or math.dist(segment['target_point'],(point['x'],point['y']))>max(.45,uncertainty*2)
                        or nav.grid.get(segment['next_cell'],0)>=0 or nav._blocked(segment['next_cell'])
                        or nav._edge_deferred(segment['next_cell'])):
            self._segment=None;segment=None;self._edge=None;self._action=None
        if segment:
            destination=segment['destination'];next_cell=segment['next_cell']
        else:
            candidates=[]
            for cell in nav._reachable_cells():
                if nav.grid.get(cell,0)>=0 or nav._blocked(cell):continue
                x,y=nav._point(cell);distance=math.hypot(point['x']-x,point['y']-y)
                if distance<stand or distance>=current-.05:continue
                candidates.append((abs(distance-stand),cell))
            # Bounded hypotheses; candidates never come from unknown space.
            candidates=sorted(candidates)[:200]
            routes=[]
            for remaining,cell in candidates:
                path=nav._path(cell)
                if not path or len(path)<2 or nav._edge_deferred(path[1]):continue
                edge=(nav._cell(nav.x,nav.y),path[1])
                if self._excluded_edges.get(edge)==nav._coverage_signature(path[1]):continue
                path_length=sum(math.dist(a,b)*nav.CELL for a,b in zip(path,path[1:]))
                routes.append((remaining+path_length*.35,cell,path))
            if not routes:
                return result(status='no_observed_route',reason='No known free route toward the remembered standoff; do not substitute unrelated exploration.')
            _,destination,path=min(routes);next_cell=path[1]
            self._edge=None;self._action=None
            self._segment=dict(origin_xy=(nav.x,nav.y),origin_cell=nav._cell(nav.x,nav.y),
                               next_cell=next_cell,destination=destination,target_point=(point['x'],point['y']),action=None)
        tx,ty=nav._point(next_cell)
        desired=math.atan2(ty-nav.y,tx-nav.x)
        edge=(nav._cell(nav.x,nav.y),next_cell)
        offsets={'forward':0,'backward':math.pi,'left':math.pi/2,'right':-math.pi/2}
        front=nav._distance(t);choices=list(offsets)
        if front is None or front<=.20:choices.remove('forward')
        if self._edge!=edge or self._action not in choices:
            self._edge=edge
            self._action=min(choices,key=lambda a:abs((desired-nav.heading-offsets[a]+math.pi)%(2*math.pi)-math.pi))
        action=self._action
        self._segment['action']=action
        error=(desired-nav.heading-offsets[action]+math.pi)%(2*math.pi)-math.pi
        travel=min(.20,math.hypot(tx-nav.x,ty-nav.y))
        if action=='forward':travel=min(travel,max(0,front-.18))
        angle=desired if abs(error)>.10 else nav.heading+offsets[action]
        coverage_missing=False;occupied=False
        while travel>=.07:
            sweep_free=nav._sweep_free(angle,travel)
            coverage_missing=coverage_missing or nav._sweep_block_reason=='missing_coverage'
            occupied=occupied or nav._sweep_block_reason=='occupied'
            if sweep_free and self._segment_clear(nav.x,nav.y,angle,travel,point,protection):break
            travel-=.025
        if travel<.07:
            self._segment=None;self._edge=None;self._action=None
            if coverage_missing:
                nav._defer_edge(next_cell)
                return result(status='scan_required',reason='First approach edge lacks observed body coverage; bounded scan, then defer repeated unchanged edge.',
                              scan_offset_requested=nav._edge_deferred(next_cell))
            if occupied:
                self._excluded_edges[edge]=nav._coverage_signature(next_cell)
                if len(self._excluded_edges)>256:del self._excluded_edges[next(iter(self._excluded_edges))]
                return result(status='scan_required',reason='Known occupied footprint excludes this approach edge until local map information changes.')
            return result(status='reacquire',reason='Approach segment enters remembered-point protection; stop for current visual evidence.')
        if abs(error)>.10:
            return result(action='rotate_left' if error>0 else 'rotate_right',status='approaching',speed=180,
                          duration_ms=max(100,min(500,round(abs(error)/nav.ROTATION_RAD_S*1000))),
                          reason='Align a bounded mecanum segment along the known route; fresh image required afterward.')
        capability=t.get('motor_max_duration_ms',350)
        capability=int(capability) if _number(capability) and capability>0 else 350
        duration=min(1000,capability,math.floor(travel/nav.SPEED_M_S*1000))
        if duration<1:return result(reason='No usable bounded pulse capability.')
        return result(action=action,status='approaching',speed=180,duration_ms=duration,
                      reason='Approach remembered-point standoff through observed swept free space; this is an estimate, not arrival.',
                      estimated_standoff_m=stand,estimated_route_destination=list(destination))
