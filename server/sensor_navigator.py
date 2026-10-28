"""EXPERIMENT: sonar frontier exploration with calibrated dead-reckoning estimates.

Never imports a simulator, image model, device or map. Coordinates start at an
arbitrary origin; command integration is an uncertain estimate, not odometry or
proof of displacement. No semantic target or goal completion is represented.
"""
from copy import deepcopy
from collections import deque
import heapq
import math


def _number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


class SensorNavigator:
    CELL=.2
    RADIUS=.15
    MAX_CELLS=12000
    SCAN_DIRECTIONS=16
    SCAN_ANGLE=2*math.pi/SCAN_DIRECTIONS
    SPEED_M_S=.35
    ROTATION_RAD_S=1.2
    DEADZONE=45

    def __init__(self):
        self.x=self.y=self.heading=0.
        self.grid={}
        self.visited=[(0.,0.)]
        self.completed=set()
        self._completed_order=[]
        self._next_id=1
        self.scan_remaining=self.SCAN_DIRECTIONS
        self.target=None
        self._travel_action=None
        self.failed_targets={}
        self.coverage_deferred={}
        self.failures=[]
        self.accepted_steps=0
        self.uncertain_steps=0
        self.distance_estimate=0.
        self.pulses_without_translation=0
        self._last_sample=None
        self._last_reason='Initial full sonar scan.'
        self._sweep_block_reason=None
        self._scan_offset_pending=False
        self._uncertain_translations=0
        self.last_integration={}

    @staticmethod
    def _fresh(t):
        age=t.get('telemetry_age_ms') if isinstance(t,dict) else None
        return _number(age) and 0<=age<=500

    @staticmethod
    def _distance(t):
        d=t.get('distance_cm') if isinstance(t,dict) else None
        return d/100 if _number(d) and 0<d<=400 else None

    def _cell(self,x,y):return (round(x/self.CELL),round(y/self.CELL))

    def _point(self,cell):return (cell[0]*self.CELL,cell[1]*self.CELL)

    def _mark(self,cell,delta):
        if max(abs(cell[0]),abs(cell[1]))>100:return
        self.grid[cell]=max(-5,min(7,self.grid.get(cell,0)+delta))
        if len(self.grid)>self.MAX_CELLS:
            victim=next(iter(self.grid));del self.grid[victim]

    def _fail_target(self):
        if self.target:
            self.failed_targets[self.target]=self.failed_targets.get(self.target,0)+1
            if len(self.failed_targets)>256:del self.failed_targets[next(iter(self.failed_targets))]

    def observe(self,telemetry):
        if not self._fresh(telemetry):return
        key=(telemetry.get('uptime_ms'),(telemetry.get('ultra') or {}).get('seq'),self.x,self.y,self.heading)
        if key==self._last_sample and key[0] is not None:return
        self._last_sample=key
        distance=self._distance(telemetry)
        self._mark(self._cell(self.x,self.y),-3)
        if distance is None:return
        # Sonar originates at the front of the estimated chassis, not its centre.
        length=distance+self.RADIUS
        # One measurement contributes at most once per cell. Repeated samples
        # inside a coarse cell must not erase that very measurement's endpoint.
        free_cells=set();endpoints=set()
        free_length=max(0,length-self.CELL/math.sqrt(2))
        for offset in (-.07,-.035,0,.035,.07):
            angle=self.heading+offset
            for i in range(max(1,math.ceil(free_length/.025))):
                d=min(free_length,i*.025)
                free_cells.add(self._cell(self.x+d*math.cos(angle),self.y+d*math.sin(angle)))
            endpoints.add(self._cell(self.x+length*math.cos(angle),self.y+length*math.sin(angle)))
        for cell in free_cells-endpoints:self._mark(cell,-1)
        for cell in endpoints:self._mark(cell,3)

    def complete(self,command,before_t,after_t,motion_observation):
        """Consume a caller-confirmed finished command once.

        Pass command.acceptance='accepted' (or legacy transport_accepted).
        Returned command_id enables deduplication. Without an ID the caller is
        responsible for once-only delivery; unknown/rejected commands never move
        the estimate. A visual changed flag corroborates but does not prove motion.
        """
        if not isinstance(command,dict):return
        identifier=command.get('command_id')
        if identifier is not None:
            if identifier in self.completed:return
            self.completed.add(identifier);self._completed_order.append(identifier)
            if len(self._completed_order)>2048:self.completed.discard(self._completed_order.pop(0))
        if command.get('acceptance') not in ('accepted','transport_accepted'):
            self.uncertain_steps+=1;self.target=None;self._travel_action=None;self.scan_remaining=self.SCAN_DIRECTIONS
            return
        action=command.get('action')
        speed=command.get('speed');duration=command.get('duration_ms')
        if not (_number(speed) and _number(duration)) or not 0<=speed<=180 or not 0<=duration<=2000:return
        scale=max(0,(speed-self.DEADZONE)/(180-self.DEADZONE))
        status=motion_observation.get('status') if isinstance(motion_observation,dict) else None
        if action in ('stop','release','estop','tilt','light_on','light_off'):return
        self.pulses_without_translation+=1
        # Pixel similarity is not a displacement measurement: short translations
        # and rotations in uniform imagery can look unchanged. Keep the calibrated
        # prior explicitly uncertain rather than silently asserting zero motion.
        uncertain=status!='changed'
        if uncertain:self.uncertain_steps+=1
        self.last_integration=dict(source='calibrated_command_prior',uncertain=True,
                                   visual_status=status,verified_motion=False)
        self.accepted_steps+=1
        if action in ('rotate_left','rotate_right'):
            sign=1 if action=='rotate_left' else -1
            self.heading=(self.heading+sign*self.ROTATION_RAD_S*scale*duration/1000)%(2*math.pi)
            if command.get('scan'):self.scan_remaining=max(0,self.scan_remaining-1)
        elif action in ('forward','backward','left','right'):
            travel=self.SPEED_M_S*scale*duration/1000
            measured=motion_observation.get('measured_translation_m') if isinstance(motion_observation,dict) else None
            measured_valid=(isinstance(motion_observation,dict) and motion_observation.get('source')=='floor_visual_odometry'
                            and _number(measured) and 0<=measured<=travel+.04)
            if measured_valid:
                travel=measured
                self.last_integration.update(source='floor_visual_odometry',measured_translation_m=measured)
            # A front guard may end a pulse before its requested deadline. A same-
            # heading sonar reduction can then provide a conservative upper bound.
            motor=after_t.get('motor',{}) if isinstance(after_t,dict) else {}
            if not measured_valid and action=='forward' and motor.get('stop') in ('sensor_near','sensor_null','sensor_stale'):
                before=self._distance(before_t);after=self._distance(after_t)
                if before is None or after is None:
                    self.uncertain_steps+=1;self.target=None;self.scan_remaining=self.SCAN_DIRECTIONS;return
                travel=min(travel,max(0,before-after))
            offset={'forward':0,'backward':math.pi,'left':math.pi/2,'right':-math.pi/2}[action]
            angle=self.heading+offset
            self.x+=travel*math.cos(angle);self.y+=travel*math.sin(angle)
            self.distance_estimate+=travel
            if travel>.05:self.pulses_without_translation=0
            self.visited.append((self.x,self.y));self.visited=self.visited[-512:]
            self._travel_action=None
            self.last_integration['translation_m']=travel
            self._uncertain_translations=self._uncertain_translations+1 if uncertain else 0
            if self._uncertain_translations>=2:
                self.target=None;self.scan_remaining=self.SCAN_DIRECTIONS
                self._scan_offset_pending=True;self._uncertain_translations=0
        self.observe(after_t)

    def _blocked(self,cell):
        # Inflate occupied sonar endpoints by chassis radius plus grid uncertainty.
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                if self.grid.get((cell[0]+dx,cell[1]+dy),0)>=2:return True
        return False

    def _path(self,target):
        start=self._cell(self.x,self.y)
        queue=[(0,start)];cost={start:0};previous={}
        while queue and len(cost)<5000:
            _,cell=heapq.heappop(queue)
            if cell==target:
                path=[cell]
                while cell in previous:cell=previous[cell];path.append(cell)
                return path[::-1]
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                child=(cell[0]+dx,cell[1]+dy)
                if max(abs(child[0]-start[0]),abs(child[1]-start[1]))>35 or self._blocked(child):continue
                if dx and dy and (self._blocked((cell[0]+dx,cell[1])) or self._blocked((cell[0],cell[1]+dy))):continue
                # Navigate only along observed free cells. Unknown shortcuts can
                # point into no-echo headings despite a valid frontier endpoint.
                if self.grid.get(child,0)>=0:continue
                new=cost[cell]+math.hypot(dx,dy)
                if new<cost.get(child,1e9):
                    cost[child]=new;previous[child]=cell
                    priority=new+math.hypot(child[0]-target[0],child[1]-target[1])
                    heapq.heappush(queue,(priority,child))
        return None

    def _reachable_cells(self):
        """One bounded flood fill; distant disconnected scans cannot starve moves."""
        start=self._cell(self.x,self.y)
        reachable={start};queue=deque([start])
        while queue and len(reachable)<self.MAX_CELLS:
            cell=queue.popleft()
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                child=(cell[0]+dx,cell[1]+dy)
                if child in reachable or self.grid.get(child,0)>=0 or self._blocked(child):continue
                if dx and dy and (self._blocked((cell[0]+dx,cell[1])) or self._blocked((cell[0],cell[1]+dy))):continue
                reachable.add(child);queue.append(child)
        return reachable

    def _information_gain(self,cell):
        """Potential unseen area from a reachable viewpoint, never ground truth.

        Unknown cells are a hypothesis of useful observations, not free motion.
        Known occupied cells occlude all later cells on that bearing.
        """
        unseen=set();x,y=self._point(cell)
        for index in range(32):
            angle=index*2*math.pi/32
            for step in range(1,math.ceil(4/self.CELL)+1):
                ray_cell=self._cell(x+step*self.CELL*math.cos(angle),y+step*self.CELL*math.sin(angle))
                value=self.grid.get(ray_cell)
                if value is not None and value>=2:break
                if value is None:unseen.add(ray_cell)
        return len(unseen)*self.CELL*self.CELL

    def _choose_target(self):
        reachable=self._reachable_cells()
        candidates=[]
        for cell in reachable:
            if self.grid.get(cell,0)>=0 or self._blocked(cell) or self.failed_targets.get(cell,0)>=2:continue
            x,y=self._point(cell);distance=math.hypot(x-self.x,y-self.y)
            if distance<.20:continue
            novelty=min(math.hypot(x-vx,y-vy) for vx,vy in self.visited)
            candidates.append((cell,distance,novelty))
        # Bounded spatially distributed shortlist; nearby candidates remain
        # represented rather than being starved by distant novel interiors.
        candidates.sort(key=lambda item:item[0])
        if len(candidates)>200:
            candidates=[candidates[round(i*(len(candidates)-1)/199)] for i in range(200)]
        ranked=[]
        for cell,distance,novelty in candidates:
            gain=self._information_gain(cell)
            if gain<=0:continue
            revisit=max(0,.55-novelty)*4
            score=gain-distance*.6-revisit-self.failed_targets.get(cell,0)*2
            ranked.append((score,cell))
        # Measure actual known-free path cost for only the strongest hypotheses.
        best=None
        for score,cell in sorted(ranked,reverse=True):
            path=self._path(cell)
            if not path or len(path)<2 or self._edge_deferred(path[1]):continue
            path_length=sum(math.dist(a,b)*self.CELL for a,b in zip(path,path[1:]))
            adjusted=score+.6*math.dist(self._point(cell),(self.x,self.y))-.6*path_length
            if best is None or (adjusted,cell)>best:best=(adjusted,cell)
        return best[1] if best else None

    def _coverage_signature(self,next_cell):
        origin=self._cell(self.x,self.y)
        margin=math.ceil((self.RADIUS+self.CELL/math.sqrt(2))/self.CELL)
        return tuple((x,y, -1 if self.grid.get((x,y),0)<0 else 1 if self.grid.get((x,y),0)>=2 else 0)
                     for x in range(min(origin[0],next_cell[0])-margin,max(origin[0],next_cell[0])+margin+1)
                     for y in range(min(origin[1],next_cell[1])-margin,max(origin[1],next_cell[1])+margin+1))

    def _edge_deferred(self,next_cell):
        key=(self._cell(self.x,self.y),next_cell)
        entry=self.coverage_deferred.get(key)
        return bool(entry and entry['attempts']>=2 and entry['signature']==self._coverage_signature(next_cell))

    def _defer_edge(self,next_cell):
        key=(self._cell(self.x,self.y),next_cell);signature=self._coverage_signature(next_cell)
        previous=self.coverage_deferred.get(key,{})
        count=previous.get('attempts',0)+1 if previous.get('signature')==signature else 1
        self.coverage_deferred[key]=dict(signature=signature,attempts=count)
        if len(self.coverage_deferred)>256:del self.coverage_deferred[next(iter(self.coverage_deferred))]

    def _sweep_free(self,angle,travel):
        """Require observed free coverage of the WHOLE chassis, not a free ray.

        Include half the coarse cell diagonal: one ray touching a rounded cell
        centre cannot certify its whole square. Unknown is never free. Occupancy
        still expresses sonar evidence, not certainty about reflective surfaces.
        """
        self._sweep_block_reason=None
        radius=self.RADIUS+self.CELL/math.sqrt(2)
        for i in range(max(1,math.ceil(travel/.025))+1):
            distance=min(travel,i*.025)
            x=self.x+distance*math.cos(angle);y=self.y+distance*math.sin(angle)
            for cx in range(math.floor((x-radius)/self.CELL),math.ceil((x+radius)/self.CELL)+1):
                for cy in range(math.floor((y-radius)/self.CELL),math.ceil((y+radius)/self.CELL)+1):
                    if math.hypot(cx*self.CELL-x,cy*self.CELL-y)>radius:continue
                    occupancy=self.grid.get((cx,cy),0)
                    if occupancy>=2:
                        self._sweep_block_reason='occupied';return False
                    if occupancy>=0:
                        self._sweep_block_reason='missing_coverage';return False
        return True

    def command_path_observed(self,command):
        """Check a proposed calibrated translation against observed footprint.

        This does not authorize stale telemetry or prove physical displacement;
        the caller must retain its normal actuator and authority guards.
        """
        if not isinstance(command,dict):return False
        action=command.get('action')
        if action in ('stop','rotate_left','rotate_right'):return True
        if action not in ('forward','backward','left','right'):return False
        speed=command.get('speed');duration=command.get('duration_ms')
        if not (_number(speed) and _number(duration)) or not 0<=speed<=180 or not 0<=duration<=2000:return False
        scale=max(0,(speed-self.DEADZONE)/(180-self.DEADZONE))
        travel=self.SPEED_M_S*scale*duration/1000
        offset={'forward':0,'backward':math.pi,'left':math.pi/2,'right':-math.pi/2}[action]
        return self._sweep_free(self.heading+offset,travel)

    def request_scan(self,reason='Supervisor requested fresh sonar coverage.'):
        """Reset bounded dense scanning without changing estimated pose or map."""
        self.scan_remaining=self.SCAN_DIRECTIONS
        self.target=None;self._travel_action=None;self._scan_offset_pending=False
        self._last_reason=str(reason)[:240]

    def _command(self,action,speed=0,duration=100,reason='',scan=False):
        result=dict(action=action,speed=speed,duration_ms=max(50,min(2000,int(duration))),tilt=90,
                    reason=reason,command_id=f'SN{self._next_id}',scan=scan)
        self._next_id+=1;self._last_reason=reason
        return result

    def next_command(self,telemetry):
        self.observe(telemetry)
        if not self._fresh(telemetry):return self._command('stop',reason='Telemetry stale or invalid; no wheel motion.')
        if telemetry.get('estop') is True:return self._command('stop',reason='Emergency stop is active.')
        if self.pulses_without_translation>=256:
            return self._command('stop',reason='Bounded exploration stalled: 256 accepted pulses without an estimated translation; no semantic goal claim.')
        if self._scan_offset_pending:
            self._scan_offset_pending=False
            return self._command('rotate_left',180,round(self.SCAN_ANGLE/2/self.ROTATION_RAD_S*1000),
                                 'Interlace the next dense scan to measure missing footprint coverage.')
        if self.scan_remaining:
            return self._command('rotate_left',180,round(self.SCAN_ANGLE/self.ROTATION_RAD_S*1000),
                                 'Measure another sonar direction from the same estimated place.',scan=True)
        if self.target and math.dist((self.x,self.y),self._point(self.target))<.12:
            self.target=None;self._travel_action=None;self.scan_remaining=self.SCAN_DIRECTIONS
            return self.next_command(telemetry)
        if self.target is None:self.target=self._choose_target()
        if self.target is None:
            self.scan_remaining=self.SCAN_DIRECTIONS
            return self._command('rotate_left',180,round(self.SCAN_ANGLE/self.ROTATION_RAD_S*1000),'No reachable observed frontier; rescan without inventing a target.',scan=True)
        path=self._path(self.target)
        if not path:
            self._fail_target()
            self.target=None;self._travel_action=None;self.scan_remaining=self.SCAN_DIRECTIONS
            return self.next_command(telemetry)
        # Do not shortcut two valid A* edges across the inside of a corner.
        next_cell=path[min(1,len(path)-1)]
        tx,ty=self._point(next_cell)
        desired=math.atan2(ty-self.y,tx-self.x)
        offsets={'forward':0,'backward':math.pi,'left':math.pi/2,'right':-math.pi/2}
        distance=self._distance(telemetry)
        if self._travel_action is None:
            choices=['forward','left','right','backward']
            if distance is None or distance<=.20:choices.remove('forward')
            self._travel_action=min(choices,key=lambda action:abs((desired-self.heading-offsets[action]+math.pi)%(2*math.pi)-math.pi))
        action=self._travel_action
        error=(desired-self.heading-offsets[action]+math.pi)%(2*math.pi)-math.pi
        if abs(error)>.12:
            return self._command('rotate_left' if error>0 else 'rotate_right',180,
                                 min(2000,max(100,round(abs(error)/self.ROTATION_RAD_S*1000))),
                                 'Align with a reachable frontier in the estimated sonar map.')
        if action=='forward' and (distance is None or distance<=.20):
            # Reorient the chassis, preserving the movement segment. Null is not
            # interpreted as free space: only the old observed-free grid supports it.
            self._travel_action=None
            return self.next_command(telemetry)
        travel=min(.25,math.hypot(tx-self.x,ty-self.y))
        if action=='forward':travel=min(travel,max(0,distance-.18))
        safe_travel=travel
        while safe_travel>=.07 and not self._sweep_free(desired,safe_travel):safe_travel-=.025
        if safe_travel<.07:
            # Missing coverage calls for additional measurements, not an invented
            # obstacle or a claim that a physical direction was tried and failed.
            block_reason=self._sweep_block_reason
            if block_reason=='occupied':self._fail_target()
            if block_reason=='missing_coverage':
                self._scan_offset_pending=True
                self._defer_edge(next_cell)
            self.target=None;self._travel_action=None;self.scan_remaining=self.SCAN_DIRECTIONS
            return self._command('stop',reason=f'Estimated swept chassis segment blocked: {block_reason}; rescan before motion.')
        travel=safe_travel
        duration=math.floor(travel/self.SPEED_M_S*1000)
        capability=telemetry.get('motor_max_duration_ms',350)
        capability=int(capability) if _number(capability) else 350
        return self._command(action,180,min(1000 if action!='forward' else 1500,capability,max(100,duration)),
                             'Follow one swept, previously observed free grid edge using bounded mecanum motion; null never authorizes forward.')

    def snapshot(self):
        return deepcopy(dict(experimental=True,localization='calibrated_dead_reckoning_estimate_not_ground_truth',
            estimated_pose=dict(x=self.x,y=self.y,heading=self.heading),estimated_travelled=self.distance_estimate,
            resolution_m=self.CELL,cells=[dict(x=c[0],y=c[1],occupancy=v) for c,v in self.grid.items()],
            estimated_target=list(self.target) if self.target else None,visited_estimates=self.visited,
            estimated_travel_action=self._travel_action,
            sweep_block_reason=self._sweep_block_reason,last_integration=self.last_integration,
            coverage_deferred=[dict(origin=list(edge[0]),next_cell=list(edge[1]),attempts=entry['attempts']) for edge,entry in self.coverage_deferred.items()],
            accepted_steps=self.accepted_steps,uncertain_steps=self.uncertain_steps,scan_remaining=self.scan_remaining,
            pulses_without_translation=self.pulses_without_translation,
            failed_directions=self.failures,failed_frontiers=[dict(cell=list(cell),attempts=count) for cell,count in self.failed_targets.items()],reason=self._last_reason))
