"""RAM-only kinematic apartment simulator. No device, filesystem or network access.

Metres/radians, +x forward at heading zero; positive headings turn left.
The clock is injectable. Every public operation advances pending motion first.
Camera observations are rendered from geometry, never from target metadata.
"""
from copy import deepcopy
import io
import math
import time

from PIL import Image, ImageDraw


class RoverSimulator:
    RADIUS = .15
    MAX_SPEED = .35
    MAX_DURATION_MS = 2000
    FORWARD_STOP_CM = 10
    ACTIONS = {'forward':163, 'backward':92, 'left':106, 'right':149,
               'rotate_left':83, 'rotate_right':172, 'stop':0}

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self, scenario='doorway', seed=1):
        if scenario not in ('doorway', 'obstacle', 'stuck', 'dark', 'apartment'):
            raise ValueError('Unknown simulator scenario')
        self.scenario, self.seed = scenario, int(seed)
        self._last = self._boot = float(self.clock())
        self.x, self.y, self.heading = 5.6, 3., 0.
        self.servo, self.light_on, self.estop = 90, False, False
        self.action, self.speed, self.deadline = 'stop', 0, self._last
        self.collisions, self.crossed_threshold = 0, False
        self.path = [[self.x, self.y]]
        self.walls = [(0,0,9.4,0), (9.4,0,9.4,6), (9.4,6,0,6), (0,6,0,0),
                      (8,0,8,2.2), (8,3.8,8,6)]
        self.boxes = [dict(x=2., y=1., w=1.2, h=.7, height=.7, color='#9a6840'),
                      dict(x=3., y=4.5, w=1.5, h=.65, height=.8, color='#406e8e')]
        if scenario == 'obstacle':
            self.boxes.append(dict(x=6.45,y=2.65,w=.65,h=.7,height=.65,color='#b56a39'))
        if scenario == 'stuck':
            # A corner catches the chassis outside the narrow front sensor cone.
            self.boxes.append(dict(x=5.82,y=3.12,w=.6,h=.4,height=.65,color='#b56a39'))
        self.reference_route=[]
        self.checkpoints=[]
        self.travelled=0.
        if scenario=='apartment':
            self.x,self.y,self.heading=1.2,1.2,0.
            self.path=[[self.x,self.y]]
            self.walls.extend([(3,0,3,3.5),(5.6,2.6,5.6,6),(0,2.7,1.8,2.7)])
            self.boxes=[dict(x=4.1,y=3.65,w=.65,h=.65,height=.8,color='#a65c38'),
                        dict(x=6.5,y=4.4,w=.9,h=.6,height=.7,color='#537547')]
            self.reference_route=[(1.2,1.2),(2.4,1.2),(2.4,4.),(3.7,4.),
                                  (4.7,1.9),(6.3,1.9),(7.4,3.),(7.75,3.)]
            self.checkpoints=[dict(x=x,y=y,radius=.5,title=title,visited=False) for x,y,title in
                              [(3.35,4.,'První průchod'),(5.95,1.9,'Druhý průchod'),(7.75,3.,'Před prahem')]]
            self.checkpoints[0]['gate']=dict(x=3.,ymin=3.5+self.RADIUS,ymax=6-self.RADIUS)
            self.checkpoints[1]['gate']=dict(x=5.6,ymin=self.RADIUS,ymax=2.6-self.RADIUS)
        self.route_length=sum(math.dist(a,b) for a,b in zip(self.reference_route,self.reference_route[1:]))
        self._seq = self._drive_seq = self._ultra_seq = self._ultra_ok = self._ultra_null = 0
        self._accepted = self._denied = self._null_stops = self._near_stops = self._deadline_stops = 0
        self._drive = dict(seq=0,dir=0,speed=0,ms=0,ok=False,why='none',age_ms=999999,accepted=0,denied=0)
        self._cmd = dict(seq=0,code=4,dir=0,speed=0,ms=0,ok=True,why='reset',age_ms=0)
        self._command_at = self._drive_at = self._last
        self._motor_started = self._last
        self._stop_reason = 'reset'
        self.sensor_valid = True
        self.deadzone_pwm = 45
        self._contact = False
        return self.snapshot()

    def _segments(self):
        yield from self.walls
        for b in self.boxes:
            x,y,w,h=b['x'],b['y'],b['w'],b['h']
            yield x,y,x+w,y
            yield x+w,y,x+w,y+h
            yield x+w,y+h,x,y+h
            yield x,y+h,x,y

    def _ray(self, angle):
        dx,dy=math.cos(angle),math.sin(angle)
        best=20.
        for ax,ay,bx,by in self._segments():
            sx,sy=bx-ax,by-ay
            den=dx*sy-dy*sx
            if abs(den)<1e-9: continue
            qx,qy=ax-self.x,ay-self.y
            t=(qx*sy-qy*sx)/den
            u=(qx*dy-qy*dx)/den
            if t>=0 and 0<=u<=1: best=min(best,t)
        return best

    def _distance(self):
        if not self.sensor_valid: return None
        # A narrow ultrasonic cone: nearest of five front-facing rays.
        value=max(0,min(self._ray(self.heading+a) for a in (-.07,-.035,0,.035,.07))-self.RADIUS)
        return round(value*100) if .02<=value<=4 else None

    @staticmethod
    def _point_segment(px,py,a,b,c,d):
        vx,vy=c-a,d-b
        t=max(0,min(1,((px-a)*vx+(py-b)*vy)/(vx*vx+vy*vy)))
        return math.hypot(px-a-t*vx,py-b-t*vy)

    def _blocked(self,x,y):
        if any(self._point_segment(x,y,*s)<self.RADIUS for s in self.walls): return True
        for b in self.boxes:
            nx=max(b['x'],min(x,b['x']+b['w']))
            ny=max(b['y'],min(y,b['y']+b['h']))
            if math.hypot(x-nx,y-ny)<self.RADIUS: return True
        return False

    def _stop(self, reason):
        if self.speed:
            if reason=='sensor_null': self._null_stops+=1
            if reason=='sensor_near': self._near_stops+=1
            if reason=='deadline': self._deadline_stops+=1
        self.action,self.speed,self._stop_reason='stop',0,reason

    def _visit_checkpoints(self, previous_x, previous_y, x, y):
        for checkpoint in self.checkpoints:
            if checkpoint['visited']:continue
            gate=checkpoint.get('gate')
            if gate:
                if previous_x<gate['x']<=x:
                    fraction=(gate['x']-previous_x)/(x-previous_x)
                    crossing_y=previous_y+(y-previous_y)*fraction
                    if gate['ymin']<crossing_y<gate['ymax']:
                        checkpoint['visited']=True
            elif math.hypot(x-checkpoint['x'],y-checkpoint['y'])<=checkpoint['radius']:
                checkpoint['visited']=True
            break

    def _advance(self):
        now=float(self.clock())
        if now<self._last: now=self._last
        end=min(now,self.deadline)
        while self._last<end and self.speed:
            dt=min(.02,end-self._last)
            self._ultra_seq+=1
            sampled_distance=self._distance()
            self._ultra_ok+=int(sampled_distance is not None)
            self._ultra_null+=int(sampled_distance is None)
            if self.action=='forward':
                distance=sampled_distance
                if distance is None or distance<=self.FORWARD_STOP_CM:
                    self._stop('sensor_null' if distance is None else 'sensor_near')
                    break
            scale=max(0,(self.speed-self.deadzone_pwm)/(180-self.deadzone_pwm))
            if self.action.startswith('rotate_'):
                self.heading=(self.heading+(1 if self.action=='rotate_left' else -1)*1.2*scale*dt)%(2*math.pi)
            else:
                offset={'forward':0,'backward':math.pi,'left':math.pi/2,'right':-math.pi/2}[self.action]
                angle=self.heading+offset
                nx=self.x+math.cos(angle)*self.MAX_SPEED*scale*dt
                ny=self.y+math.sin(angle)*self.MAX_SPEED*scale*dt
                if self._blocked(nx,ny):
                    if not self._contact:self.collisions+=1
                    self._contact=True
                else:
                    self.travelled+=math.hypot(nx-self.x,ny-self.y)
                    self._visit_checkpoints(self.x,self.y,nx,ny)
                    self.x,self.y=nx,ny
                    self._contact=False
                    if self.x>=8:self.crossed_threshold=True
                    if math.hypot(self.x-self.path[-1][0],self.y-self.path[-1][1])>=.025:
                        self.path.append([round(self.x,4),round(self.y,4)])
                        self.path=self.path[-2000:]
            self._last+=dt
        if self.speed and now>=self.deadline:self._stop('deadline')
        self._last=now

    def command(self, action, speed=0, duration_ms=1000, tilt=90):
        self._advance()
        self._seq+=1
        if action not in set(self.ACTIONS)|{'estop','release','tilt','light_on','light_off'}:
            return dict(accepted=False,reason='unknown_action',telemetry=self.telemetry())
        duration=max(50,min(2000,int(duration_ms)))
        pwm=max(0,min(180,int(speed)))
        moving=action in self.ACTIONS and action!='stop'
        reason='ok'
        if moving and self.estop:reason='estop'
        if moving and action=='forward':
            distance=self._distance()
            if distance is None:reason='sensor_null'
            elif distance<=10:reason='sensor_near'
        accepted=reason=='ok'
        direction=self.ACTIONS.get(action,0)
        self._command_at=self._last
        self._cmd=dict(seq=self._seq,code=0 if moving else {'tilt':1,'estop':2,'release':3}.get(action,4),
                       dir=direction,speed=pwm,ms=duration,ok=accepted,why=reason,age_ms=0)
        if moving:
            self._drive_seq+=1
            self._accepted+=int(accepted);self._denied+=int(not accepted)
            self._drive_at=self._last
            self._drive=dict(seq=self._seq,dir=direction,speed=pwm,ms=duration,ok=accepted,why=reason,
                             age_ms=0,accepted=self._accepted,denied=self._denied)
        if not accepted:self._stop(reason)
        elif moving:
            self.action,self.speed,self.deadline=action,pwm,self._last+duration/1000
            self._motor_started=self._last
        else:
            self._stop(action)
            if action=='estop':self.estop=True
            elif action=='release':self.estop=False
            elif action=='tilt':self.servo=max(35,min(145,int(tilt)))
            elif action in ('light_on','light_off'):self.light_on=action=='light_on'
        return dict(accepted=accepted,reason=reason,telemetry=self.telemetry())

    def telemetry(self):
        self._advance()
        age=lambda at:round((self._last-at)*1000)
        uptime=age(self._boot)
        dist=self._distance()
        cmd=dict(self._cmd,age_ms=age(self._command_at))
        drive=dict(self._drive,age_ms=age(self._drive_at))
        return dict(distance_cm=dist,servo=self.servo,estop=self.estop,motor_byte=self.ACTIONS[self.action],
                    telemetry_age_ms=0,connected=True,camera_ready=True,simulation=True,light_supported=True,
                    light_on=self.light_on,uptime_ms=uptime,rssi=-40,battery_v=7.8,
                    firmware='simulator-motor2s-sonar10',motor_max_duration_ms=2000,forward_stop_cm=10,
                    ultra=dict(seq=self._ultra_seq,us=dist*58 if dist is not None else 0,pre=False,post=False,
                               ok=self._ultra_ok,null=self._ultra_null,last_cm=dist,last_age_ms=0,age_ms=0),cmd=cmd,drive=drive,
                    motor=dict(active=bool(self.speed),speed=self.speed,start_ms=age(self._boot)-age(self._motor_started),
                               deadline_ms=round((self.deadline-self._boot)*1000),stop=self._stop_reason,
                               null_stops=self._null_stops,near_stops=self._near_stops,deadline_stops=self._deadline_stops))

    def snapshot(self):
        self._advance()
        distance=math.hypot(8-self.x,3-self.y)
        at_door=distance<=.35 and not self.crossed_threshold
        route_complete=all(p['visited'] for p in self.checkpoints)
        return deepcopy(dict(scenario=self.scenario,seed=self.seed,
            title='Virtuální byt – klikatá cesta ke dveřím' if self.scenario=='apartment' else 'Virtuální byt – otevřené dveře',
            pose=dict(x=self.x,y=self.y,heading=self.heading),action=self.action,speed=self.speed,servo=self.servo,
            geometry=dict(bounds=[0,0,9.4,6],walls=self.walls,boxes=self.boxes,
                          door=dict(x=8,y=3,width=1.6,threshold_x=8),target=dict(x=7.75,y=3),checkpoints=self.checkpoints),
            path=self.path,collisions=self.collisions,door_distance=distance,at_door=at_door,
            route_length=self.route_length,travelled=self.travelled,checkpoints=self.checkpoints,route_complete=route_complete,
            goal_reached=at_door and not self.speed and route_complete,crossed_threshold=self.crossed_threshold,telemetry=self.telemetry()))

    def camera_jpeg(self):
        self._advance()
        width,height=640,480
        # Wide camera lens keeps a complete standard doorway visible from low height.
        focal=280.
        horizon=240-math.tan(math.radians((90-self.servo)*.55))*focal
        img=Image.new('RGB',(width,height),(174,183,190))
        pixels=img.load()
        # Perspective floor checker is anchored in world coordinates.
        for y in range(max(0,int(horizon)+1),height):
            depth=.22*focal/max(1,y-horizon)
            for x in range(width):
                side=(320-x)*depth/focal
                wx=self.x+depth*math.cos(self.heading)-side*math.sin(self.heading)
                wy=self.y+depth*math.sin(self.heading)+side*math.cos(self.heading)
                check=(math.floor(wx*2)+math.floor(wy*2))%2
                base=150 if check else 190
                pixels[x,y]=(base,base-9,base-21)
        draw=ImageDraw.Draw(img)
        columns=[]
        # Ray-cast room walls and furniture: depth determines their actual apparent size.
        for x in range(0,width,2):
            offset=math.atan((320-x)/focal)
            ray=self.heading+offset
            ray_distance=self._ray(ray)
            depth=ray_distance*math.cos(offset)
            # Find whether the nearest intersection belongs to low furniture.
            hitx=self.x+ray_distance*math.cos(ray);hity=self.y+ray_distance*math.sin(ray)
            furniture=next((b for b in self.boxes if b['x']-.01<=hitx<=b['x']+b['w']+.01 and b['y']-.01<=hity<=b['y']+b['h']+.01),None)
            wallheight=furniture['height'] if furniture else 2.5
            top=horizon-(wallheight-.22)*focal/max(.03,depth)
            bottom=horizon+.22*focal/max(.03,depth)
            shade=max(80,int(215-depth*8))
            color=furniture['color'] if furniture else ((shade-22,shade-2,shade+6) if hitx>8.1 else (shade,shade-7,shade-16))
            if self.scenario=='apartment' and not furniture:
                # Different room finishes and wall orientations are useful visual landmarks.
                if abs(hitx-3)<.02:color=(shade-12,shade-34,shade-48)
                elif abs(hitx-5.6)<.02:color=(shade-38,shade-12,shade-31)
                elif abs(hity-2.7)<.02:color=(shade-36,shade-20,shade-6)
            draw.rectangle((x,max(-2000,top),x+1,min(2000,bottom)),fill=color)
            if not furniture:draw.line((x,bottom-3,x+1,bottom-3),fill=(105,103,97),width=3)
            columns.append((x,depth,top,bottom,offset))
        # A blue structural doorframe, open aperture and contrasting floor threshold.
        door_layer=Image.new('RGBA',img.size,(0,0,0,0))
        door_draw=ImageDraw.Draw(door_layer)
        def project(wx,wy,z):
            dx,dy=wx-self.x,wy-self.y
            depth=dx*math.cos(self.heading)+dy*math.sin(self.heading)
            side=-dx*math.sin(self.heading)+dy*math.cos(self.heading)
            if depth<=.06:return None
            return (int(320-side*focal/depth),int(horizon-(z-.22)*focal/depth))
        for a,b in [((8,2.2,0),(8,2.2,2.1)),((8,3.8,0),(8,3.8,2.1)),((8,2.2,2.1),(8,3.8,2.1))]:
            pa,pb=project(*a),project(*b)
            if pa and pb:door_draw.line((pa,pb),fill=(35,91,174),width=14)
        threshold=[project(*p) for p in ((7.95,2.2,0),(8.05,2.2,0),(8.05,3.8,0),(7.95,3.8,0))]
        if all(p is not None for p in threshold):door_draw.polygon(threshold,fill=(232,192,61))
        # Reuse existing column intersections: a closer wall/box masks only its
        # projected vertical span. This also masks the threshold, without new rays.
        for x,depth,top,bottom,offset in columns:
            ray=self.heading+offset
            if abs(math.cos(ray))<1e-8:continue
            target_ray=(8-self.x)/math.cos(ray)
            target_depth=target_ray*math.cos(offset)
            if target_ray>0 and depth<target_depth-.08:
                door_draw.rectangle((x,max(0,math.floor(top)),x+1,min(height,math.ceil(bottom))),fill=(0,0,0,0))
            # A low foreground box must not hide a taller *occluder* behind it.
            # Check wall intersections too; still O(columns * walls), never per pixel.
            dx,dy=math.cos(ray),math.sin(ray)
            for ax,ay,bx,by in self.walls:
                sx,sy=bx-ax,by-ay
                den=dx*sy-dy*sx
                if abs(den)<1e-9:continue
                qx,qy=ax-self.x,ay-self.y
                t=(qx*sy-qy*sx)/den
                u=(qx*dy-qy*dx)/den
                wall_depth=t*math.cos(offset)
                if 0<=u<=1 and 0<t<target_ray and wall_depth<target_depth-.08:
                    wall_top=horizon-(2.5-.22)*focal/max(.03,wall_depth)
                    wall_bottom=horizon+.22*focal/max(.03,wall_depth)
                    door_draw.rectangle((x,max(0,math.floor(wall_top)),x+1,min(height,math.ceil(wall_bottom))),fill=(0,0,0,0))
        img.paste(door_layer,(0,0),door_layer)
        if self.scenario=='dark' and not self.light_on:
            img=img.point(lambda v:int(v*.10))
        output=io.BytesIO();img.save(output,format='JPEG',quality=85)
        return output.getvalue()
