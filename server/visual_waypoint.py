"""Simulation-only image target controller; no map, pose or hardware access.

The simulator uses a 640px-wide pinhole image and focal length 280px: horizontal
FOV is 2*atan(320/280), about 97.63 degrees (not 64 degrees). Rotation at PWM150
is approximately .933 radians/s. These are optical/controller constants, never
hidden ground-truth coordinates. The caller owns authority and final guards.
"""
from copy import deepcopy
import math


IMAGE_WIDTH=640.
FOCAL_LENGTH=280.
HALF_FOV_RAD=math.atan(IMAGE_WIDTH/(2*FOCAL_LENGTH))
ROTATION_RATE_RAD_S=1.2*(150-45)/(180-45)


def _number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def waypoint_schema(schema):
    """Clone schema and allow navigate only when wheel targeting is available.

    Caller must invoke only in its simulation-only automatic planning branch.
    The object is required for strict structured outputs, even for stop; ignored
    unless the model explicitly chooses navigate.
    """
    value=deepcopy(schema)
    actions=value.get('properties',{}).get('action',{}).get('enum',[])
    if not any(a in actions for a in ('forward','rotate_left','rotate_right')):
        return value
    if 'navigate' not in actions:actions.append('navigate')
    value['properties']['waypoint']=dict(type='object',additionalProperties=False,properties={
        'x':dict(type='number',minimum=0,maximum=1),
        'y':dict(type='number',minimum=0,maximum=1),
        'evidence':dict(type='string',maxLength=240)},required=['x','y','evidence'])
    if 'waypoint' not in value.setdefault('required',[]):value['required'].append('waypoint')
    return value


def resolve_waypoint(decision, telemetry, allowed_wheels):
    """Resolve explicit navigate to one permitted wheel pulse or a replan stop.

    y is image-down, x image-right; waypoint belongs to the current standalone
    image, not a panel in a contact sheet. Only x determines heading; y ensures
    the target is in the lower image. It never implies a metric depth estimate.
    """
    value=deepcopy(decision) if isinstance(decision,dict) else {}
    if value.get('action')!='navigate':return value,'Původní akce není navigate; obrazový bod ji nemění.'
    def stop(reason):
        value.update(action='stop',speed=0,duration_ms=100)
        return value,reason+' Vyžádej nový plán.'
    if value.get('hazard')=='drop':return stop('Viditelný sráz blokuje navigaci.')
    point=value.get('waypoint')
    if not isinstance(point,dict):return stop('Chybí obrazový waypoint.')
    x,y=point.get('x'),point.get('y')
    evidence=point.get('evidence')
    if not (_number(x) and _number(y) and 0<=x<=1 and 0<=y<=1):
        return stop('Souřadnice waypointu musí být konečná čísla v rozsahu 0 až 1.')
    if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>240:
        return stop('Waypoint potřebuje konkrétní obrazovou evidenci do 240 znaků.')
    if y<=.45:return stop('Waypoint není v dolní části aktuálního obrazu.')
    t=telemetry if isinstance(telemetry,dict) else {}
    age=t.get('telemetry_age_ms')
    if not _number(age) or not 0<=age<=500:return stop('Telemetrie není čerstvá v rozsahu 0 až 500 ms.')
    allowed=set(a for a in allowed_wheels if isinstance(a,str)) if isinstance(allowed_wheels,(list,tuple,set)) else set()
    if abs(x-.5)>.08+1e-12:
        action='rotate_left' if x<.5 else 'rotate_right'
        if action not in allowed:return stop(f'Směr {action} není právě povolený průzkumnou pamětí.')
        heading=math.atan((.5-x)*IMAGE_WIDTH/FOCAL_LENGTH)
        duration=max(100,min(500,round(abs(heading)/ROTATION_RATE_RAD_S*1000)))
        value.update(action=action,speed=150,duration_ms=duration)
        return value,'Krátká otočka zaměří pozorovaný obrazový bod; po ní znovu ověř aktuální pohled.'
    if 'forward' not in allowed:return stop('Vpřed není právě povoleno průzkumnou pamětí.')
    distance=t.get('distance_cm')
    threshold=t.get('forward_stop_cm',20)
    if not _number(threshold) or not 1<=threshold<=100:threshold=20
    if not _number(distance) or not threshold<distance<=400:
        return stop('Vpřed chybí platné aktuální čelní měření nad mezí zastavení.')
    speed=value.get('speed',180)
    duration=value.get('duration_ms',1000)
    if not _number(speed) or not _number(duration):return stop('Rychlost nebo délka návrhu není platné číslo.')
    speed=max(150,min(180,round(speed)))
    capability=t.get('motor_max_duration_ms',350)
    if not _number(capability) or capability<100:capability=350
    # Budget from front sonar only, never from image y. 40cm/s is conservative
    # against the simulator's maximum 35cm/s; reserve another 3cm of margin.
    clearance_budget=math.floor((distance-threshold-3)/40*1000)
    duration=min(max(100,round(duration)),2000,int(capability),clearance_budget)
    if duration<100:return stop('Odstup nestačí ani na nejkratší konzervativní posun vpřed.')
    value.update(action='forward',speed=speed,duration_ms=duration)
    return value,'Obrazový bod je vycentrovaný; krátký posun vpřed omezuje aktuální čidlo a firmware, nikoli odhad mapy.'
