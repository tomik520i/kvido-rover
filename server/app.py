"""Local AD176 companion. No motion without an active operator lease.

Verity owns voice credentials; we reuse its voice proxy unchanged.
"""
import asyncio
import base64
import io
import ipaddress
import json
import os
import secrets
import hashlib
import sqlite3
import ssl
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field, ConfigDict

from .prompt import SCHEMA, messages_for, response_schema
from .decision_loop import duplicate_feedback
from .exploration_memory import exploration_state
from .route_prompt import verification_schema
from .vision_history import VisionHistory
from .relay import RelayHub, RelayUnavailable
from .camera_scheduler import CameraScheduler
from .web_paths import PublicPrefixMiddleware, SESSION_COOKIE, cookie_path
from .task_runner import TaskRunner
from .alpha_runner import AlphaRunner, ConnectionLost, RadarReset, MotionRefused
from .task_prompt import ASSESSMENT_SCHEMA, assessment_messages
from .simulator_verifier import short_assessment_messages
from .ai_provider import AIProvider, AIProviderError
from .verification_retry import verification_retry
from .visual_waypoint import resolve_waypoint
from .diagnostics import DiagnosticTimeline, AI_PROVIDER_ERROR_CATEGORIES
from .motion_policy import AI_WHEELS, ESCAPE, fresh, hardware_limits, hardware_speed_limit, command_limits

ROOT = Path(__file__).resolve().parent.parent
SIMULATION_ONLY = os.environ.get('ROVER_SIMULATION_ONLY','0')=='1'
DATA = Path(os.environ.get('ROVER_DATA', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
KEY_FILE = DATA / 'access.key'
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_urlsafe(18), encoding='utf-8')
    KEY_FILE.chmod(0o600)
ACCESS_KEY = KEY_FILE.read_text().strip()
RELAY_KEY_FILE = DATA / 'relay.key'
if not RELAY_KEY_FILE.exists():
    RELAY_KEY_FILE.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    RELAY_KEY_FILE.chmod(0o600)
RELAY_KEY = RELAY_KEY_FILE.read_text().strip()
LLM = os.environ.get('ROVER_LLM_URL', 'http://127.0.0.1:8080').rstrip('/')
LLM_THINKING = os.environ.get('ROVER_LLM_THINKING', '0').lower() in ('1','true','yes')
VOICE = os.environ.get('ROVER_VERITY_URL', 'http://127.0.0.1:3700').rstrip('/')
CONFIG_PATH = DATA / 'settings.json'
settings = {'device_url':'', 'device_key':'', 'transport':'lan', 'simulation':True, 'name':'Kvído','navigation_policy':'strict','thinking_enabled':LLM_THINKING,
            'floor_angle':55,'floor_calibrated':False,'camera_rotate_180':False,
            'ai_provider':'gemma','openai_model':'gpt-6-luna'}
if CONFIG_PATH.exists():
    settings.update(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
if SIMULATION_ONLY:
    settings.update(simulation=True,transport='lan',device_url='',device_key='')
simulator_engine=None
simulator_scene_started_at=0.0


def simulator():
    global simulator_engine
    if not SIMULATION_ONLY:raise HTTPException(404,'Simulátor není v této instanci zapnutý.')
    if simulator_engine is None:
        from .simulator import RoverSimulator
        simulator_engine=RoverSimulator()
    return simulator_engine
db = sqlite3.connect(DATA / 'memory.sqlite3', check_same_thread=False)
db.executescript('''CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, at REAL);
CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY, text TEXT, source TEXT, at REAL);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, payload TEXT, at REAL);''')
sessions = {}
login_failures = {}
listeners = set()
client = None
motion_lock = asyncio.Lock()
turn_lock = asyncio.Lock()
capture_lock = asyncio.Lock()
camera_scheduler = CameraScheduler()
relay = None
ai_provider = None
task_runner = None
alpha_runner = None
task_pending = None
diagnostics = None
command_trace_id = ContextVar('command_trace_id',default=None)
task_trace = ContextVar('task_trace',default=None)
recovery_attempt_at = 0.0
state = {'mode':'manual', 'armed':False, 'estop':False, 'epoch':0,
         'owner':None, 'heartbeat':0.0, 'last_action':'stop', 'last_reason':'Připraveno',
         'busy':False, 'frame_count':0, 'last_frame_at':None, 'tilt':90}
sim = {'distance_cm':90, 'line':[150,150,150], 'servo':90, 'motor':'stop',
       'uptime_ms':0, 'rssi':None, 'battery_v':None, 'telemetry_age_ms':0, 'estop':False,
       'light_supported':True,'light_on':False}
MISSION_PATH=DATA/'mission.json'
mission={'goal':'','status':'idle','steps':0,'recent':[],'started_at':0,'reason':''}
if MISSION_PATH.exists():
    mission.update(json.loads(MISSION_PATH.read_text(encoding='utf-8')))
    if mission['status']=='running':mission.update(status='paused',reason='Server byl restartován. Cíl zůstává uložený.')
floor_seen=0.0
vision_history=VisionHistory()

def save_mission():
    tmp=MISSION_PATH.with_suffix('.tmp');tmp.write_text(json.dumps(mission,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(MISSION_PATH)

def navigation():
    return {'floor_angle':settings['floor_angle'], 'floor_calibrated':settings['simulation'] or settings['floor_calibrated'],
            'floor_valid':time.monotonic()-floor_seen<15,'floor_max_age_s':15,'policy':settings.get('navigation_policy','strict')}

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class RouteChoice(StrictModel):
    route_id: str = Field(max_length=20)
    label: str = Field(max_length=120)
    landmark: str = Field(max_length=180)
    bearing: Literal['left','ahead','right','unknown']
    evidence: str = Field(max_length=240)

class VisualWaypoint(StrictModel):
    x: float = Field(ge=0,le=1,strict=True)
    y: float = Field(ge=0,le=1,strict=True)
    evidence: str = Field(max_length=240)

class Decision(StrictModel):
    say: str = Field(max_length=1200)
    action: str
    speed: int = Field(ge=0,le=255,strict=True)
    duration_ms: int = Field(ge=50,le=2000,strict=True)
    tilt: int = Field(ge=35,le=145,strict=True)
    reason: str = Field(max_length=600)
    memory: str = Field(max_length=300)
    hazard: str
    traversable: bool = Field(strict=True)
    turn_clear: bool = Field(default=False,strict=True)
    confidence: float = Field(ge=0,le=1)
    goal_status: str
    evidence: str = Field(max_length=600)
    route_choice: RouteChoice | None = None
    waypoint: VisualWaypoint | None = None

class Turn(StrictModel):
    text: str = Field(default='Co vidíš před sebou?',max_length=2000)
    vision: bool = True
    source: str = 'car'
    intent: str = 'observe'
    image: str | None = Field(default=None,max_length=1800000)

class Drive(StrictModel):
    action: str
    speed: int = Field(default=130,ge=0,le=255)
    duration_ms: int = Field(default=250,ge=50,le=2000)
    tilt: int = Field(default=90,ge=35,le=145)
    # Free floor ahead measured by the camera radar; only used when the sonar has no echo.
    clearance_cm: int = Field(default=0,ge=0,le=250)

class Setup(StrictModel):
    ai_provider: Literal['gemma','openai'] = 'gemma'
    openai_model: str = Field(default='gpt-6-luna',min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_.:-]+$')
    thinking_enabled: bool = Field(default=LLM_THINKING,strict=True)
    navigation_policy: Literal['strict','sensor'] = 'strict'
    transport: Literal['lan','relay'] = 'lan'
    device_url: str = ''
    device_key: str = ''
    simulation: bool = True
    name: str = Field(default='Kvído',min_length=1,max_length=32)
    floor_angle: int = Field(default=55,ge=35,le=145)
    floor_calibrated: bool = False
    camera_rotate_180: bool = False

def session(request):
    sid = request.cookies.get(SESSION_COOKIE,'')
    if sessions.get(sid,0) < time.time():
        raise HTTPException(401,'Přihlas se přístupovým klíčem.')
    return sid

def active(sid):
    authorized = bool(alpha_runner and alpha_runner.active and sid == alpha_runner.owner) or sessions.get(sid,0)>time.time()
    return sid is not None and authorized and state['owner'] == sid and time.monotonic()-state['heartbeat'] < 1.5

def guard(sid):
    if not active(sid) or not state['armed'] or state['estop']:
        raise HTTPException(409,'Řízení není aktivní. Převzít řízení a ponechat stránku otevřenou.')

def diagnostic_context():
    if task_trace.get() is not None:return dict(task_trace.get())
    if alpha_runner and alpha_runner.active:
        return {'task_id':alpha_runner.data['id'],'step':alpha_runner.data['current_step']}
    if task_runner:
        data=task_runner.data
        if data.get('status') in ('running','reconnecting'):
            return {'task_id':data.get('id'),'step':data.get('current_step',0)+1}
    return {'task_id':None,'step':None}


def diagnostic_event(kind,**payload):
    try:
        if diagnostics:diagnostics.event(kind,**{**diagnostic_context(),**payload})
    except Exception:pass  # Diagnostics can never prevent STOP or task cancellation.


def diagnostic_settings():
    sensor=settings.get('navigation_policy')=='sensor'
    return {'provider':settings.get('ai_provider','gemma'),
            'model':settings.get('openai_model') if settings.get('ai_provider')=='openai' else 'Gemma',
            'policy':settings.get('navigation_policy','strict'),'simulation':settings['simulation'],
            'transport':settings['transport'],'thinking_enabled':settings.get('thinking_enabled',False),
            'limits':{'sensor_max_age_ms':500,'lease_ms':1500,'relay_recovery_ms':120000,
                      'task_steps':120 if SIMULATION_ONLY else 40,'task_seconds':1800 if SIMULATION_ONLY else 600,'forward_min_cm':10 if sensor else 25,'rotation_min_cm':None if sensor else 20,
                      'forward_speed':180 if sensor else 150,'forward_duration_ms':2000 if sensor else 300,
                      'rotation_speed':150 if sensor else 80,'rotation_duration_ms':2000 if sensor else 100,
                      'firmware_max_duration_ms':2000}}


def diagnostic_protect(*values):
    try:
        if diagnostics:diagnostics.add_secrets(*values)
    except Exception:pass


def diagnostic_status():
    try:return diagnostics.status() if diagnostics else {'enabled':False}
    except Exception:return {'enabled':False}


def diagnostic_frame(image,role,**context):
    try:
        if diagnostics and image:
            raw=base64.b64decode(image.split(',',1)[1],validate=True)
            diagnostics.frame(raw,role,**{**diagnostic_context(),**context})
    except Exception:pass


def diagnostic_image_hash(image):
    try:return hashlib.sha256(base64.b64decode(image.split(',',1)[1],validate=True)).hexdigest() if image else None
    except Exception:return None


def emit(kind, **payload):
    if kind=='task':
        task=payload.get('task',{})
        span=task_trace.get()
        diagnostic_event('task_phase',task_id=task.get('id'),
            step=span['step'] if span and span.get('task_id')==task.get('id') else task.get('current_step',0),
            completed_step_count=task.get('current_step',0),
            **{key:task.get(key) for key in ('phase','status','outcome','reason','goal','elapsed_s')})
    event = {'type':kind, 'at':time.time(), **payload}
    for q in list(listeners):
        if q.full():
            try: q.get_nowait()
            except asyncio.QueueEmpty: pass
        q.put_nowait(event)

def save_settings():
    tmp=CONFIG_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(CONFIG_PATH)
    diagnostic_protect(settings.get('device_key'))
    diagnostic_event('settings',**diagnostic_settings())

def validate_device_url(value):
    if not value: return ''
    p=urlsplit(value)
    try: addr=ipaddress.ip_address(p.hostname or '')
    except ValueError: raise HTTPException(422,'Zadej lokální IPv4 adresu auta, například http://192.168.50.80:24500')
    if p.scheme!='http' or addr.version!=4 or not addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast or p.username or p.password or p.path not in ('','/') or p.query or p.fragment:
        raise HTTPException(422,'Povolena je pouze přímá LAN adresa auta bez cesty.')
    try:
        if p.port is None or not 1024<=p.port<=65534: raise ValueError()
    except ValueError: raise HTTPException(422,'Port auta musí být 1024–65534.')
    return value.rstrip('/')

async def device(path, method='GET', payload=None, *, preview=False):
    if path=='/capture' and method=='GET':
        source=(settings['transport'],settings.get('device_url'),settings.get('device_key'))
        return await camera_scheduler.capture(source,lambda:_device_request(path,method,payload),preview=preview)
    result=await _device_request(path,method,payload)
    if path=='/api/status':camera_scheduler.observe(result.json())
    return result

async def _device_request(path, method='GET', payload=None):
    if SIMULATION_ONLY:raise HTTPException(409,'Izolovaný simulátor nemá přístup k autu.')
    if settings['transport']=='relay':
        if relay is None:raise HTTPException(502,'Spojení auta není připravené.')
        try:
            if path=='/api/status':
                return httpx.Response(200,json=relay.telemetry())
            if path=='/capture':
                async with capture_lock:
                    return httpx.Response(200,content=await relay.capture())
            if path=='/api/control' and method=='POST':
                code,data=await relay.control(payload or {})
                if code>=400:
                    rejection=HTTPException(code,data.get('error','Auto odmítlo povel.'))
                    rejection.transport_rejected=400<=code<500 and code!=408
                    raise rejection
                return httpx.Response(code,json=data)
            raise HTTPException(404,'Neznámý požadavek auta.')
        except RelayUnavailable as exc:
            raise HTTPException(502,'Spojení s autem se přerušilo nebo vypršel požadavek.') from exc
    if not settings['device_url'] or not settings['device_key']:
        raise HTTPException(409,'Nejdřív spáruj autíčko v nastavení.')
    r=await client.request(method, settings['device_url']+path, json=payload,
        headers={'X-Device-Key':settings['device_key']}, timeout=3.0)
    r.raise_for_status()
    return r

async def _telemetry():
    if SIMULATION_ONLY:return simulator().telemetry()
    if settings['simulation']:
        return {**sim,'uptime_ms':int(time.monotonic()*1000), 'simulation':True}
    try:
        d=(await device('/api/status')).json()
        d['simulation']=False
        return d
    except Exception:
        return {'connected':False,'distance_cm':None,'telemetry_age_ms':999999,'simulation':False}


async def telemetry():
    sample=await _telemetry()
    try:
        if diagnostics:diagnostics.telemetry(sample,**diagnostic_context())
    except Exception:pass
    return sample

async def planning_telemetry(*,require_distance=False):
    """Wait briefly for current relay data, optionally including a valid range.

    A close valid obstacle is returned immediately, never waited away. The last
    actual sample is returned on timeout; no previously valid cache is used.
    Actuation still reads telemetry again under the motion lock.
    """
    epoch=state['epoch']
    started=time.monotonic()
    deadline=time.monotonic()+.5
    for attempt in range(11):
        sample=await telemetry()
        age=sample.get('telemetry_age_ms')
        distance=sample.get('distance_cm')
        usable=(type(age) in (int,float) and 0<=age<=500 and
                (not require_distance or (type(distance) in (int,float) and 0<distance<=400)))
        if (settings['transport']!='relay' or settings['simulation'] or
            sample.get('connected') is False or state['epoch']!=epoch or
            usable or attempt==10 or time.monotonic()>=deadline):
            diagnostic_event('planning_telemetry',telemetry=sample,require_distance=require_distance,
                attempt=attempt+1,elapsed_ms=round((time.monotonic()-started)*1000),
                reason='epoch_changed' if state['epoch']!=epoch else 'usable' if usable else 'latest_unusable')
            return sample
        await asyncio.sleep(min(.05,max(0,deadline-time.monotonic())))

async def send_control(action='stop',speed=0,duration_ms=100,tilt=90,clearance_cm=0):
    identifier=command_trace_id.get() or secrets.token_hex(12)
    trace_context=diagnostic_context()
    started=time.monotonic()
    diagnostic_event('command_request',command_id=identifier,action=action,speed=speed,
                     duration_ms=duration_ms,tilt=tilt,simulation=settings['simulation'],transport=settings['transport'],**trace_context)
    try:
        if SIMULATION_ONLY:
            result=simulator().command(action,speed,duration_ms,tilt)
            if result.get('accepted') is False:raise HTTPException(409,result.get('reason','Simulátor odmítl pohyb.'))
        elif settings['simulation']:
            sim['motor']=action
            sim['until']=time.monotonic()+duration_ms/1000
            if action in ('light_on','light_off'):
                sim['motor']='stop';sim['light_on']=action=='light_on';sim['light_until']=time.monotonic()+60
            if action=='tilt':sim['servo']=tilt
            if action=='estop':sim['estop']=True;sim['light_on']=False
            if action=='release':sim['estop']=False
        else:
            payload={'action':action,'speed':speed,'duration_ms':duration_ms,'tilt':tilt}
            if clearance_cm:payload['clearance_cm']=clearance_cm
            await device('/api/control','POST',payload)
    except BaseException as error:
        status_code=error.response.status_code if isinstance(error,httpx.HTTPStatusError) else getattr(error,'status_code',None)
        # A 5xx or timeout can follow partial execution; never infer rejection
        # merely because an HTTP response was received.
        rejected=bool(type(status_code) is int and 400<=status_code<500 and status_code!=408)
        category='device_rejected' if rejected else 'cancelled' if isinstance(error,asyncio.CancelledError) else 'timeout' if isinstance(error,(httpx.TimeoutException,asyncio.TimeoutError)) else 'send_failed'
        diagnostic_event('command_result',command_id=identifier,action=action,acceptance='transport_rejected' if rejected else 'unknown',
            category=category,status_code=status_code,
            latency_ms=round((time.monotonic()-started)*1000),simulation=settings['simulation'],**trace_context)
        raise
    diagnostic_event('command_result',command_id=identifier,action=action,acceptance='transport_accepted',
        latency_ms=round((time.monotonic()-started)*1000),simulation=settings['simulation'],**trace_context)
    state['last_action']=action
    vision_history.command(action,speed,duration_ms,tilt)
    return identifier

async def halt(reason, emergency=False, preserve_autonomy=False):
    global floor_seen
    if alpha_runner and not preserve_autonomy:alpha_runner.interrupt(reason)
    floor_seen=0
    # Invalidate in-flight AI before waiting for a device request.
    state['epoch']+=1
    state['armed']=False
    state['owner']=None
    if emergency:state['estop']=True
    state['last_reason']=reason
    if task_runner:task_runner.interrupt(reason=reason)
    if mission['status']=='running':mission.update(status='paused',reason=reason);save_mission()
    async with motion_lock:
        try: await send_control('estop' if emergency else 'stop')
        except Exception: pass  # Physical firmware expires each command independently.
    emit('stopped',reason=reason,estop=state['estop'])

async def watchdog():
    while True:
        await asyncio.sleep(.15)
        if settings['simulation'] and time.monotonic()>sim.get('until',0):sim['motor']='stop'
        if settings['simulation'] and time.monotonic()>sim.get('light_until',0):sim['light_on']=False
        if state['armed'] and not active(state['owner']):
            autonomous=bool(alpha_runner and alpha_runner.active and state['owner']==alpha_runner.owner)
            if autonomous:alpha_runner.connection_lost('Serverový dohled se zpozdil. Obnovuji spojení.')
            await halt('Serverový dohled se zpozdil.' if autonomous else 'Telefon se odpojil nebo usnul. Autíčko zastaveno.',preserve_autonomy=autonomous)
        if task_runner and task_runner.data['status']=='reconnecting':
            await restore_task_connection()

async def restore_task_connection():
    global recovery_attempt_at
    sid=task_runner.owner
    if not task_runner.can_recover() or sessions.get(sid,0)<time.time():
        await halt('Obnova spojení pozastavena: operátor není přítomný nebo vypršel čas obnovy.');return
    if state['mode']!='ai' or settings['transport']!='relay' or not relay or not relay.status()['connected']:return
    if task_runner.worker and not task_runner.worker.done():return
    if task_runner.data['current_step']>=task_runner.max_steps or task_runner.data.get('elapsed_s',0)>=task_runner.max_seconds:
        await halt('Úkol dosáhl limitu kroků nebo času.');return
    if time.monotonic()-recovery_attempt_at<1:return
    recovery_attempt_at=time.monotonic()
    t=await planning_telemetry()
    age=t.get('telemetry_age_ms',999999)
    if type(age) not in (int,float) or not 0<=age<=500:return
    generation=task_runner.generation;epoch=state['epoch']
    try:await task_capture()  # A live socket alone does not prove the camera recovered.
    except Exception:return
    async with motion_lock:
        if sessions.get(sid,0)<=time.time() or epoch!=state['epoch'] or generation!=task_runner.generation or task_runner.data['status']!='reconnecting' or not task_runner.can_recover():return
        try:await send_control('release')
        except Exception:return
        if sessions.get(sid,0)<=time.time() or epoch!=state['epoch'] or generation!=task_runner.generation or not task_runner.can_recover():return
        state.update(armed=True,owner=sid,estop=False,heartbeat=time.monotonic(),epoch=epoch+1)
    # Resume the goal, not a cached motor command. The next step captures anew.
    try:task_runner.start(sid,task_runner.data['goal'],resume=True)
    except ValueError:
        await halt('Úkol nelze obnovit; dosáhl limitu nebo ještě dobíhá předchozí krok.');return
    mission.update(status='running',reason='Spojení obnoveno; zjišťuji aktuální stav.');save_mission()

def relay_link_change(connected):
    # The relay receive loop is not active during this callback. Invalidate leases
    # synchronously; the firmware itself estops on every link transition.
    global floor_seen
    diagnostic_event('relay_link',connected=connected,relay=relay.status() if relay else {})
    if settings['transport']!='relay' or settings['simulation']:return
    floor_seen=0
    reason='Auto se připojilo. Znovu převezmi řízení.' if connected else 'Internetové spojení auta se přerušilo.'
    state.update(armed=False,owner=None,estop=True,epoch=state['epoch']+1,last_reason=reason,last_action='stop')
    if alpha_runner and alpha_runner.active:alpha_runner.connection_lost(reason)
    recovering=bool(task_runner and task_runner.data['status'] in ('running','reconnecting'))
    if recovering:task_runner.recover(reason='Obnovuji spojení s autem; úkol zůstává uložený.')
    if mission['status']=='running':
        mission.update(status='paused',reason=reason);save_mission()
    emit('connection',reason=reason,reconnecting=recovering)

@asynccontextmanager
async def lifespan(app):
    global client, relay, ai_provider, task_runner, alpha_runner, diagnostics, task_pending, simulator_scene_started_at
    protected=[ACCESS_KEY,RELAY_KEY,settings.get('device_key'),os.environ.get('OPENAI_API_KEY')]
    try:
        if (DATA/'openai.key').exists():protected.append((DATA/'openai.key').read_text(encoding='utf-8').strip())
    except (OSError,UnicodeError):pass
    try:diagnostics=DiagnosticTimeline(DATA/'diagnostics',secrets=protected)
    except Exception:diagnostics=None
    relay=RelayHub(RELAY_KEY,on_link_change=relay_link_change)
    client=httpx.AsyncClient(trust_env=False,follow_redirects=False,verify=ssl.create_default_context(),
                             limits=httpx.Limits(max_connections=20))
    ai_provider=AIProvider(client,DATA,ROOT,LLM)
    task_runner=TaskRunner(DATA/'task.json',task_step,task_authorized,halt,emit,
                           max_steps=120 if SIMULATION_ONLY else 40,
                           max_seconds=1800 if SIMULATION_ONLY else 600)
    alpha_runner=AlphaRunner(DATA/'autonomy.json',autonomy_inspect,autonomy_acquire,
                             autonomy_drive,autonomy_stop,autonomy_radar,autonomy_heartbeat,emit)
    if SIMULATION_ONLY:
        # The virtual world starts anew; prior positions and observations no longer apply.
        await task_runner.reset('Nová virtuální scéna po spuštění serveru.')
        simulator_scene_started_at=time.time()
        task_pending=None
        vision_history.clear()
        mission.update(goal='',status='idle',steps=0,recent=[],started_at=0,reason='Nová virtuální scéna.')
        save_mission()
    diagnostic_event('startup',**diagnostic_settings())
    if alpha_runner.active:
        if SIMULATION_ONLY:alpha_runner.interrupt('Nová virtuální scéna po restartu.')
        else:
            state['mode']='ai'
            alpha_runner.launch()
    task=asyncio.create_task(watchdog())
    yield
    task.cancel()
    await alpha_runner.shutdown()
    await halt('Server se vypíná.',preserve_autonomy=True)
    if task_runner.worker and not task_runner.worker.done():
        try:await asyncio.wait_for(task_runner.worker,2)
        except (asyncio.CancelledError,asyncio.TimeoutError):pass
    await relay.shutdown()
    await client.aclose()
    try:
        if diagnostics:diagnostics.close()
    except Exception:pass

app=FastAPI(title='Kvído · AD176',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
app.add_middleware(PublicPrefixMiddleware)

@app.websocket('/device-ws')
async def device_websocket(websocket:WebSocket):
    if SIMULATION_ONLY:
        await websocket.close(code=1008);return
    await relay.websocket_handler(websocket)

@app.middleware('http')
async def limits(request, call_next):
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        expected=f'{request.url.scheme}://{request.headers.get("host","")}'
        if origin and origin!=expected:return JSONResponse({'detail':'Cizí původ požadavku.'},403)
    # Enforce streaming body size, including chunked requests.
    if request.method=='POST':
        size=0; chunks=[]
        async for chunk in request.stream():
            size+=len(chunk)
            if size>12*1024*1024:return JSONResponse({'detail':'Příliš velký požadavek.'},413)
            chunks.append(chunk)
        request._body=b''.join(chunks)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Cache-Control']='no-store'
    return response

@app.get('/health')
async def health(): return {'ok':True,'service':'rover','simulation':settings['simulation'],'simulation_only':SIMULATION_ONLY}


@app.get('/api/simulator')
async def simulator_state(request:Request):
    session(request)
    return {'simulation_only':SIMULATION_ONLY,'state':simulator().snapshot(),
            'scenarios':[
                {'id':'apartment','title':'Byt za dvěma rohy',
                 'description':'Dvě přepážky, průchody na opačných koncích, nábytek a slepá odbočka. Dveře ze startu nevidíš.',
                 'goal':'Najdi v bytě otevřené dveře s modrým rámem a dojeď k nim. Zastav před prahem.'},
                {'id':'doorway','title':'Otevřené dveře','description':'Krátká přímá cesta pro kontrolu přiblížení.',
                 'goal':'Dojeď ke dveřím a zastav před prahem.'},
                {'id':'obstacle','title':'Překážka v cestě','description':'Krabice blokuje přímou cestu ke dveřím.',
                 'goal':'Objeď překážku, dojeď ke dveřím a zastav před prahem.'},
                {'id':'stuck','title':'Vyproštění','description':'Roh překážky zasahuje mimo čelní kužel čidla.',
                 'goal':'Vyprosti se z překážky a pokračuj ke dveřím. Zastav před prahem.'},
                {'id':'dark','title':'Šero','description':'Omezený obraz, možnost zapnout světlo.',
                 'goal':'Najdi dveře v šeru a dojeď k nim. Zastav před prahem.'}]}


class SimulatorReset(StrictModel):
    scenario:Literal['doorway','obstacle','stuck','dark','apartment']='doorway'
    seed:int=Field(default=1,ge=0,le=1000000)


@app.post('/api/simulator/reset')
async def simulator_reset(body:SimulatorReset,request:Request):
    global task_pending, simulator_scene_started_at
    session(request);engine=simulator()
    await halt('Reset virtuální scény.')
    if task_runner:await task_runner.reset('Reset virtuální scény.')
    async with turn_lock:
        # A pending standalone turn must save its old-scene result before the cutoff.
        simulator_scene_started_at=time.time()
        task_pending=None
        vision_history.clear()
        mission.update(goal='',status='idle',steps=0,recent=[],reason='Nová virtuální scéna.')
        save_mission()
        return {'simulation_only':True,'state':engine.reset(body.scenario,body.seed)}

@app.post('/api/login')
async def login(request:Request):
    ip=request.client.host
    failures=[t for t in login_failures.get(ip,[]) if time.monotonic()-t<60]
    if len(failures)>=8:raise HTTPException(429,'Zkus přihlášení za minutu.')
    body=await request.json()
    if not secrets.compare_digest(str(body.get('key','')),ACCESS_KEY):
        login_failures[ip]=failures+[time.monotonic()]
        raise HTTPException(401,'Nesprávný přístupový klíč.')
    sid=secrets.token_urlsafe(32)
    sessions[sid]=time.time()+12*3600
    response=JSONResponse({'ok':True})
    response.set_cookie(SESSION_COOKIE,sid,path=cookie_path(request),httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=43200)
    return response

@app.post('/api/logout')
async def logout(request:Request):
    sid=session(request)
    if state['owner']==sid:await halt('Odhlášení.')
    sessions.pop(sid,None)
    response=JSONResponse({'ok':True});response.delete_cookie(SESSION_COOKIE,path=cookie_path(request));return response

@app.get('/api/status')
async def status(request:Request):
    sid=session(request)
    return {**{k:v for k,v in state.items() if k not in ('owner','heartbeat')},'diagnostics':diagnostic_status(),
            'mine':active(sid),'simulation':settings['simulation'],'name':settings['name'],
            'transport':settings['transport'],'relay':relay.status() if relay else {'connected':False},
            'device_url':settings['device_url'],'telemetry':await telemetry(),'mission':mission,'navigation':navigation(),
            'autonomy':alpha_runner.snapshot() if alpha_runner else {},
            'task':{**task_runner.snapshot(),'mine':task_runner.owner==sid} if task_runner else {},'ai_provider':settings['ai_provider'],'ai_model':settings['openai_model'] if settings['ai_provider']=='openai' else 'Gemma',
            'camera_rotate_180':settings['camera_rotate_180'] and not settings['simulation']}

@app.get('/api/services')
async def services(request:Request):
    session(request)
    try:return (await client.get(VOICE+'/api/status',timeout=8)).json()
    except Exception:return {'llm':False,'tts':False,'stt':False,'error':'Verity není dostupná.'}

@app.get('/api/settings')
async def get_settings(request:Request):
    session(request)
    return {**settings,'device_key':'','has_device_key':bool(settings['device_key']),
            'llm_url':LLM,'voice_url':VOICE,'has_openai_key':bool(ai_provider and ai_provider.configured())}

@app.post('/api/settings')
async def set_settings(body:Setup,request:Request):
    session(request)
    if SIMULATION_ONLY and (not body.simulation or body.transport!='lan' or body.device_url or body.device_key):
        raise HTTPException(409,'Tato instance je uzamčená na simulaci; připojení hardwaru není možné.')
    url=validate_device_url(body.device_url)
    await halt('Změna nastavení připojení.')
    settings.update(device_url=url,transport=body.transport,simulation=body.simulation,name=body.name,
                    ai_provider=body.ai_provider,openai_model=body.openai_model,
                    navigation_policy=body.navigation_policy,thinking_enabled=body.thinking_enabled,
                    floor_angle=body.floor_angle,floor_calibrated=body.floor_calibrated,
                    camera_rotate_180=body.camera_rotate_180)
    if body.device_key:settings['device_key']=body.device_key
    save_settings()
    vision_history.clear()
    return {'ok':True}

@app.post('/api/arm')
async def arm(request:Request):
    sid=session(request)
    if alpha_runner and alpha_runner.active:raise HTTPException(409,'Běží autonomní úkol. Nejprve jej pozastav nebo zastav.')
    return await take_control(sid)

async def take_control(sid, generation=None):
    if state['armed'] and state['owner']!=sid and active(state['owner']):
        raise HTTPException(409,'Řízení drží jiný telefon.')
    async with motion_lock:
        if generation is None and alpha_runner and alpha_runner.active:
            raise HTTPException(409,'Běží autonomní úkol. Nejprve jej pozastav nebo zastav.')
        epoch=state['epoch']
        if generation is not None and not alpha_runner.valid(generation):raise ConnectionLost('Úkol byl ukončen.')
        try:await send_control('release')
        except Exception:raise HTTPException(502,'Auto neodpovídá, řízení nebylo aktivováno.')
        if epoch!=state['epoch']:raise HTTPException(409,'Spojení se změnilo. Znovu převezmi řízení.')
        if generation is not None and not alpha_runner.valid(generation):raise ConnectionLost('Úkol byl ukončen.')
        state.update(armed=True,estop=False,owner=sid,heartbeat=time.monotonic())
        state['epoch']+=1
    return {'ok':True}

@app.post('/api/heartbeat')
async def heartbeat(request:Request):
    sid=session(request)
    if task_runner:task_runner.operator_heartbeat(sid)
    # A late heartbeat must never resurrect an expired lease.
    if active(sid) and state['armed']:state['heartbeat']=time.monotonic()
    return {'active':active(sid) and state['armed']}

@app.post('/api/mode')
async def mode(request:Request):
    session(request);body=await request.json()
    if body.get('mode') not in ('manual','ai'):raise HTTPException(422,'Neznámý režim.')
    await halt('Změna režimu. Znovu převezmi řízení.')
    state['mode']=body['mode']
    vision_history.clear()
    return {'ok':True}

@app.post('/api/stop')
async def stop(request:Request):
    session(request)
    await halt('Nouzové zastavení uživatelem.',emergency=True)
    return {'ok':True}

@app.post('/api/release')
async def release_browser(request:Request):
    sid=session(request)
    # An old page's delayed release must not cancel a newer server-owned task.
    if state['owner']==sid:
        await halt('Stránka ručního řízení se zavřela.',preserve_autonomy=True)
    return {'ok':True}

@app.post('/api/mission')
async def mission_control(request:Request):
    sid=session(request);body=await request.json();action=body.get('action')
    if alpha_runner and alpha_runner.active:raise HTTPException(409,'Běží serverový autonomní úkol.')
    if task_runner and task_runner.data['status'] in ('running','reconnecting'):
        raise HTTPException(409,'Běží sekvenční úkol; použij ovládání úkolu.')
    if turn_lock.locked():raise HTTPException(409,'Počkej na dokončení aktuálního snímku, nebo použij STOP.')
    if action=='start':
        guard(sid)
        if state['mode']!='ai':raise HTTPException(409,'Přepni do AI režimu a převezmi řízení.')
        goal=str(body.get('goal','')).strip()[:500]
        if not goal:raise HTTPException(422,'Napiš úkol.')
        mission.update(goal=goal,status='running',steps=0,recent=[],started_at=time.time(),reason='Hledám podle zadaného cíle.')
    elif action=='resume':
        guard(sid)
        if state['mode']!='ai' or not mission['goal']:raise HTTPException(409,'Chybí uložený úkol nebo AI režim.')
        mission.update(status='running',started_at=time.time(),steps=0,reason='Pokračuji v uloženém úkolu.')
    elif action in ('pause','cancel'):
        await halt('Úkol pozastaven uživatelem.')
        mission.update(status='cancelled' if action=='cancel' else 'paused')
    else:raise HTTPException(422,'Neznámá změna úkolu.')
    save_mission();return mission

def _visual_motion_clear(d):
    if not d.traversable or d.confidence<.85:
        return False
    if d.action in ('rotate_left','rotate_right'):
        return d.turn_clear and d.hazard in ('clear','obstacle')
    return d.hazard=='clear'


def movement_allowed(d, telemetry, *, mode, image_source, image_age, armed, policy='strict'):
    if d.action=='stop':return 'stop','AI navrhla zůstat stát.'
    if not armed:return 'stop','Řízení není aktivní.'
    if mode!='ai':return 'stop','Manuální režim: AI pouze popisuje.'
    if image_source not in ('car','simulator'):return 'stop','Kamera telefonu slouží k popisu, není ověřena poloha vůči autu.'
    if policy!='sensor' and image_age>8:return 'stop','Snímek je pro pohyb starý; udělej nový.'
    if d.action not in (*AI_WHEELS,'tilt','light_on','light_off'):return 'stop','Nepovolený příkaz modelu.'
    if policy!='sensor' and d.action in ('backward','left','right'):return 'stop','Tento režim nepovoluje ústup dozadu ani do stran.'
    if d.action in ('light_on','light_off'):
        if not telemetry.get('light_supported'):return 'stop','Firmware zatím nepodporuje ovládání světla.'
        return d.action,'Změna světla. Před pohybem je potřeba nový snímek.'
    if d.action=='tilt':
        servo=telemetry.get('servo')
        if type(servo) in (int,float) and abs(d.tilt-servo)<=3:
            return 'stop','Kamera už je v tomto úhlu; zvol jiný pohled nebo akci.'
        return 'tilt','Náklon kamery.'
    if not fresh(telemetry):return 'stop','Chybí čerstvá telemetrie auta.'
    if policy=='sensor' and d.hazard=='drop':return 'stop','AI hlásí viditelnou hranu nebo sráz.'
    if policy=='sensor' and d.action in ESCAPE:
        if not fresh(telemetry):return 'stop','Chybí čerstvá telemetrie auta.'
        return d.action,'Ústup nebo otočení povoleno; přední čidlo tento směr neměří. Po kroku ověř nový pohled.'
    if policy!='sensor' and not _visual_motion_clear(d):
        return 'stop','Obraz nepotvrzuje volnou bezpečnou cestu s dostatečnou jistotou.'
    distance=telemetry.get('distance_cm')
    age=telemetry.get('telemetry_age_ms',999999)
    minimum_distance=hardware_limits(telemetry)[1] if policy=='sensor' else (20 if d.action in ('rotate_left','rotate_right') else 25)
    if type(age) not in (int,float) or not 0<=age<=500 or not isinstance(distance,(int,float)) or isinstance(distance,bool) or not minimum_distance<distance<=400:
        return 'stop','Chybí čerstvé měření nebo je překážka příliš blízko.'
    return d.action,'Krátký krok povolen řadičem; přední čidlo nekryje boky ani srázy.'

@app.post('/api/drive')
async def drive(body:Drive,request:Request):
    sid=session(request)
    return await drive_command(body,sid)

async def drive_command(body:Drive,sid,expected_epoch=None):
    # The alpha autopilot drives from the cockpit page in AI mode under the operator's own lease.
    if state['mode'] not in ('manual','ai'):raise HTTPException(409,'Nejdřív přepni do manuálního režimu.')
    if body.action not in ('stop','forward','backward','left','right','forward_left','forward_right','back_left','back_right','rotate_left','rotate_right','tilt','light_on','light_off'):
        raise HTTPException(422,'Neznámý pohyb.')
    async with motion_lock:
        if expected_epoch is not None and state['epoch']!=expected_epoch:raise ConnectionLost('Spojení se změnilo; starý povel neprovedu.')
        guard(sid)
        t=await telemetry()
        guard(sid)
        if expected_epoch is not None and state['epoch']!=expected_epoch:raise ConnectionLost('Spojení se změnilo během kontroly čidla.')
        if body.action in ('light_on','light_off') and not t.get('light_supported'):
            raise HTTPException(409,'Nejdřív aktualizuj firmware ESP32 pro ovládání světla.')
        if body.action not in ('stop','tilt','light_on','light_off') and not fresh(t):
            await send_control('stop');raise HTTPException(409,'Chybí čerstvá telemetrie auta.')
        # No sonar echo: a camera clearance may stand in, only when both boards advertise it,
        # telemetry is fresh and the pulse is short. A near or stale sonar still refuses.
        camera_only=(body.action.startswith('forward') and body.clearance_cm>=40 and t.get('distance_cm') is None
                     and fresh(t) and body.duration_ms<=350 and t.get('camera_clearance_min_cm')==40
                     and t.get('esp_camera_clearance') is True)
        if body.action.startswith('forward') and not camera_only and (type(t.get('distance_cm')) not in (int,float) or not hardware_limits(t)[1]<t['distance_cm']<=400 or not fresh(t)):
            await send_control('stop');raise HTTPException(409,'Přední překážka nebo neplatné měření.')
        if body.action not in ('stop','tilt','light_on','light_off') and body.duration_ms>hardware_limits(t)[0]:
            raise HTTPException(409,'Dlouhý krok vyžaduje odpovídající firmware obou desek.')
        speed=min(body.speed,hardware_speed_limit({**t,'simulation':settings['simulation']}))
        await send_control(body.action,speed,body.duration_ms,body.tilt,**({'clearance_cm':body.clearance_cm} if camera_only else {}))
    return {'action':body.action,'simulation':settings['simulation'],'camera_only':camera_only}

def simulation_jpeg():
    if SIMULATION_ONLY:return simulator().camera_jpeg()
    im=Image.new('RGB',(640,480),'#d8dcd8');d=ImageDraw.Draw(im)
    d.polygon([(0,250),(640,250),(640,480),(0,480)],fill='#b6a78c')
    d.rectangle((350,120,520,260),fill='#996b42');d.rectangle((365,260,382,325),fill='#795234');d.rectangle((490,260,507,325),fill='#795234')
    d.rectangle((75,220,210,360),fill='#bd514a');d.text((20,20),'SIMULATOR - NOT A LIVE CAMERA',fill='#222222')
    out=io.BytesIO();im.save(out,format='JPEG',quality=85);return out.getvalue()

def checked_image(data):
    try:
        prefix,encoded=data.split(',',1)
        if prefix not in ('data:image/jpeg;base64','data:image/png;base64'):raise ValueError()
        raw=base64.b64decode(encoded,validate=True)
        with Image.open(io.BytesIO(raw)) as im:
            if im.width*im.height>4_000_000:raise ValueError()
            im=im.convert('RGB');im.thumbnail((1024,768));out=io.BytesIO();im.save(out,'JPEG',quality=85)
        return 'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()
    except Exception:raise HTTPException(422,'Neplatný nebo příliš velký obrázek.')

def oriented_car_jpeg(raw):
    """Match real-camera stills and vision input to the displayed MJPEG orientation."""
    if settings['simulation'] or not settings['camera_rotate_180']:
        return raw
    with Image.open(io.BytesIO(raw)) as im:
        if im.width*im.height>4_000_000:raise ValueError('Camera frame too large')
        im=im.convert('RGB').transpose(Image.Transpose.ROTATE_180)
        out=io.BytesIO();im.save(out,'JPEG',quality=90)
        return out.getvalue()

@app.get('/api/camera.jpg')
async def camera(request:Request,fresh:bool=False):
    session(request)
    if settings['simulation']:return Response(simulation_jpeg(),media_type='image/jpeg')
    try:return Response(oriented_car_jpeg((await device('/capture',preview=not fresh)).content),media_type='image/jpeg')
    except Exception:raise HTTPException(502,'Kamera auta není dostupná.')

@app.get('/api/stream')
async def stream(request:Request):
    session(request)
    async def frames():
        if settings['simulation']:
            while not await request.is_disconnected():
                jpeg=simulation_jpeg()
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+jpeg+b'\r\n'
                await asyncio.sleep(.5)
        else:
            # Pull bounded stills in either transport. The ESP's dedicated MJPEG
            # handler can occupy its single stream socket after a lost client.
            # Using /capture also lets control and telemetry run between frames.
            while not await request.is_disconnected():
                try:
                    jpeg=(await device('/capture',preview=True)).content
                    yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(jpeg)).encode()+b'\r\n\r\n'+jpeg+b'\r\n'
                except Exception:
                    emit('error',message='Přenos kamery se přerušil.')
                    break
                await asyncio.sleep(.5)
    return StreamingResponse(frames(),media_type='multipart/x-mixed-replace; boundary=frame')

def history():
    query='SELECT role,content FROM messages'
    params=()
    if SIMULATION_ONLY:
        query+=' WHERE at>=?';params=(simulator_scene_started_at,)
    return [{'role':r[0],'content':r[1]} for r in reversed(db.execute(query+' ORDER BY id DESC LIMIT 12',params).fetchall())]

def memory():
    query='SELECT id,text,source,at FROM memory'
    params=()
    if SIMULATION_ONLY:
        query+=" WHERE COALESCE(source,'') NOT LIKE ? OR at>=?";params=('AI pozorování%',simulator_scene_started_at)
    return [{'id':r[0],'text':r[1],'source':r[2],'at':r[3]} for r in db.execute(query+' ORDER BY id DESC LIMIT 60',params)]

async def infer(messages, schema=None):
    if settings['ai_provider']=='openai':
        value,metadata=await ai_provider.infer(messages,schema or SCHEMA,provider='openai',model=settings['openai_model'])
        state['last_inference']=metadata
        return Decision.model_validate(value)
    thinking=settings.get('thinking_enabled',LLM_THINKING) is True
    value,metadata=await ai_provider.infer(messages,schema or SCHEMA,provider='gemma',thinking=thinking,
        max_output_tokens=4096 if thinking else 1100 if 'route_choice' in (schema or SCHEMA).get('properties',{}) else 700)
    state['last_inference']=metadata
    return Decision.model_validate(value)

async def planner_inference(messages,schema,sid,epoch,timeout_s):
    # Retry read-only inference over the exact same immutable proposal context.
    budget=4096 if settings.get('thinking_enabled',LLM_THINKING) else 1100 if 'route_choice' in schema.get('properties',{}) else 700
    return await verification_retry(lambda budget:infer(messages,schema),
        lambda:task_authorized(sid) and state['epoch']==epoch,
        timeout_s=min(120,max(.01,timeout_s)),token_budget=budget,max_token_budget=budget,
        on_attempt=lambda **fields:diagnostic_event('planner_attempt',**fields))

@app.post('/api/turn')
async def turn(body:Turn,request:Request):
    sid=session(request)
    if alpha_runner and alpha_runner.active:raise HTTPException(409,'Běží serverový autonomní úkol. Nejdřív jej pozastav.')
    if task_runner and task_runner.data['status'] in ('running','reconnecting'):
        raise HTTPException(409,'Probíhá úkol. Pro samostatný snímek jej nejdřív pozastav.')
    return await execute_turn(body,sid)

async def execute_turn(body:Turn,sid,task_mode=False):
    global floor_seen, task_pending
    if body.intent not in ('observe','act'):raise HTTPException(422,'Neznámý záměr.')
    if turn_lock.locked():raise HTTPException(409,'Už přemýšlím. Počkej na odpověď.')
    async with turn_lock:
        state['busy']=True
        epoch=state['epoch'];mode=state['mode'] if body.intent=='act' else 'manual';captured=time.monotonic();source='none';image=None
        captured_utc=time.time()
        try:
            if body.vision:
                if body.source=='phone':
                    if not body.image:raise HTTPException(422,'Chybí snímek z telefonu.')
                    image=checked_image(body.image);source='phone'
                elif body.source=='car':
                    if task_mode:
                        image=await task_capture()
                        if epoch!=state['epoch'] or not task_authorized(sid):raise asyncio.CancelledError()
                    else:
                        raw=simulation_jpeg() if settings['simulation'] else (await device('/capture')).content
                        image=checked_image('data:image/jpeg;base64,'+base64.b64encode(oriented_car_jpeg(raw)).decode())
                    source='simulator' if settings['simulation'] else 'car'
                else:raise HTTPException(422,'Neznámý zdroj kamery.')
                captured=time.monotonic()
                captured_utc=time.time()
            t=await planning_telemetry(require_distance=mode=='ai' and body.intent=='act' and source in ('car','simulator'))
            observed_before={key:t.get(key) for key in ('servo','distance_cm','telemetry_age_ms','light_on')}
            observed_before['captured_at']=captured_utc
            if not task_mode and mission['status']=='running' and (mission['steps']>=40 or time.time()-mission['started_at']>600):
                await halt('Dosažen limit 40 snímků / 10 minut. Cíl zůstává uložený.');mode='manual'
            frames=vision_history.observe(image,t,source) if image else []
            if frames:t={**t,'motion_observation':frames[-1].get('motion_observation')}
            if task_mode and task_runner and task_runner.data.get('last_result'):
                # The new pre-frame was taken while standing. Keep the verified
                # prior movement result for the duplicate-action guard.
                t={**t,'motion_observation':task_runner.data['last_result'].get('motion_observation')}
            replans=[];loop_limit=False;planning_started=time.monotonic()
            for attempt in range(3):
                planning_navigation=navigation()
                exploration=None
                # Qualitative visual hypotheses also work on the real car. Do
                # not transfer simulated odometry, optics or metric waypoints.
                if task_mode and task_runner and mode=='ai' and source=='car':
                    planning_navigation={**planning_navigation,'route_memory_enabled':True,
                        'route_memory':task_runner.data.get('route_memory',{})}
                # Experimental controller remains confined to the isolated simulator.
                # Its only inputs are the task, accepted commands and front sensor.
                if SIMULATION_ONLY and task_mode and task_runner and mode=='ai' and planning_navigation['policy']=='sensor':
                    exploration=exploration_state(task_runner.data['goal'],task_runner.data.get('completed_steps',[]),t,task_runner.data.get('route_memory'))
                    task_runner.data['navigation_state']=exploration
                    task_runner._publish()
                    if exploration:planning_navigation={**planning_navigation,'exploration':exploration,
                        'route_memory':task_runner.data.get('route_memory',{})}
                messages=messages_for(body.text,mode,source,t,[] if task_mode else history(),memory(),image,mission,planning_navigation,settings['name'],feedback=replans,frames=frames,verified_steps=verified_steps() if task_mode else None)
                if image:
                    state['frame_count']+=1;state['last_frame_at']=time.time()
                    emit('frame_sent',source=source,count=state['frame_count'])
                emit('thinking')
                remaining=120-(time.monotonic()-planning_started)
                inference_started=time.monotonic()
                schema=response_schema(mode,source,planning_navigation,t,replans)
                decision=(await planner_inference(messages,schema,sid,epoch,remaining) if task_mode else
                          await asyncio.wait_for(infer(messages,schema),timeout=max(.01,remaining)))
                system_text=json.dumps([message.get('content') for message in messages if message.get('role') in ('system','developer')],ensure_ascii=False)
                diagnostic_event('model_proposal',proposal=decision.model_dump(),attempt=attempt+1,
                    latency_ms=round((time.monotonic()-inference_started)*1000),
                    provider=settings.get('ai_provider','gemma'),model=settings.get('openai_model') if settings.get('ai_provider')=='openai' else 'Gemma',
                    policy=settings.get('navigation_policy','strict'),goal=mission.get('goal') or body.text,
                    prompt_sha256=hashlib.sha256(system_text.encode()).hexdigest())
                raw_action=decision.action;waypoint_reason=None;waypoint_rejected=None
                if raw_action=='navigate':
                    if SIMULATION_ONLY and task_mode and exploration:
                        resolved,waypoint_reason=resolve_waypoint(decision.model_dump(),t,exploration['allowed_wheels'])
                        decision=Decision.model_validate(resolved)
                        if decision.action=='stop':waypoint_rejected=waypoint_reason
                    else:
                        decision=decision.model_copy(update={'action':'stop','speed':0,'duration_ms':100})
                        waypoint_rejected='Obrazové řízení je dostupné pouze při navigačním úkolu v izolovaném simulátoru.'
                proposed_command={key:getattr(decision,key) for key in ('action','speed','duration_ms','tilt')}
                rejected=[{'proposed_command':r['proposal'],'executed':'stop'} for r in replans]
                feedback=waypoint_rejected or (duplicate_feedback(proposed_command,mission['recent']+rejected,t) if mode=='ai' and source in ('car','simulator') and body.intent=='act' else None)
                if exploration and decision.action in AI_WHEELS and decision.action not in exploration['allowed_wheels']:
                    feedback='Průzkumná paměť odmítla návrh, nebyl proveden. '+exploration['reason']
                if not feedback:break
                replans.append({'proposal':proposed_command,'feedback':feedback})
                diagnostic_event('model_replan',proposal=proposed_command,feedback=feedback,attempt=attempt+1)
                emit('replanning',message=feedback,attempt=attempt+1)
                # Stop before asking again; never replay a physical command.
                async with motion_lock:
                    permitted=active(sid) and state['armed'] and not state['estop'] and epoch==state['epoch']
                    if permitted:await send_control('stop')
                if not permitted:break
                if attempt==2 or time.monotonic()-planning_started>=120:
                    loop_limit=True;break
                # Each new proposal sees a new image. A newer image must never
                # retroactively validate the previous proposal.
                if task_mode:
                    image=await task_capture()
                    if epoch!=state['epoch'] or not task_authorized(sid):raise asyncio.CancelledError()
                else:
                    raw=simulation_jpeg() if settings['simulation'] else (await device('/capture')).content
                    image=checked_image('data:image/jpeg;base64,'+base64.b64encode(oriented_car_jpeg(raw)).decode())
                captured=time.monotonic()
                captured_utc=time.time()
                t=await planning_telemetry(require_distance=mode=='ai' and body.intent=='act' and source in ('car','simulator'))
                observed_before={key:t.get(key) for key in ('servo','distance_cm','telemetry_age_ms','light_on')}
                observed_before['captured_at']=captured_utc
                frames=vision_history.observe(image,t,source)
                if frames:t={**t,'motion_observation':frames[-1].get('motion_observation')}
            if decision.action in AI_WHEELS and settings['transport']=='relay' and not settings['simulation']:
                await planning_telemetry(require_distance=decision.action=='forward')
            async with motion_lock:
                t=await telemetry()
                observed_at_execution={key:t.get(key) for key in ('servo','distance_cm','telemetry_age_ms')}
                permitted=active(sid) and state['armed'] and not state['estop'] and epoch==state['epoch']
                if permitted and image and source in ('car','simulator') and navigation()['floor_calibrated'] and type(t.get('servo')) in (int,float) and abs(t['servo']-settings['floor_angle'])<=5 and time.monotonic()-captured<=8:
                    floor_seen=time.monotonic() if _visual_motion_clear(decision) else 0
                action,reason=movement_allowed(decision,t,mode=mode if state['mode']=='ai' else 'manual',image_source=source,
                                               image_age=time.monotonic()-captured,armed=permitted,policy=settings.get('navigation_policy','strict'))
                if waypoint_reason:reason+=' '+waypoint_reason
                if loop_limit:
                    action='stop';reason='Tři návrhy bez nové alternativy. Opakované příkazy nebyly provedeny.'
                    mission.update(status='blocked',reason=reason)
                if action in AI_WHEELS and exploration and SIMULATION_ONLY and task_mode and task_runner:
                    current_exploration=exploration_state(task_runner.data['goal'],task_runner.data.get('completed_steps',[]),t,task_runner.data.get('route_memory'))
                    if current_exploration:
                        exploration=current_exploration
                        task_runner.data['navigation_state']=exploration
                        if action not in exploration['allowed_wheels']:
                            action='stop'
                            reason='Průzkumná paměť změnila povolené směry podle aktuální telemetrie; zastaveno pro nový plán. '+exploration['reason']
                if settings.get('navigation_policy','strict')!='sensor' and action not in ('stop','tilt','light_on','light_off') and not navigation()['floor_valid']:
                    action='stop';reason='Nejdřív sklop kameru a ověř podlahu. Chybí čerstvá kontrola nebo kalibrace.'
                if mission['status']=='running' and permitted and body.intent=='act':
                    mission['steps']+=1
                    if not task_mode and decision.goal_status=='found' and image and source in ('car','phone') and decision.confidence>=.85 and decision.evidence.strip():
                        mission.update(status='found',reason='AI vidí možné splnění cíle: '+decision.evidence);action='stop';reason='Cíl podle AI nalezen. Zastaveno pro kontrolu uživatelem.'
                    elif not task_mode and decision.goal_status=='blocked':
                        mission.update(status='blocked',reason=decision.reason);action='stop';reason='AI hlásí zablokování. Vyžaduje pomoc.'
                if action in AI_WHEELS:
                    max_speed,max_duration=command_limits(action,settings.get('navigation_policy'),t)
                    if task_mode and source=='car' and settings.get('navigation_policy')=='sensor':
                        # Operator-requested physical beta profile. Apply only
                        # AFTER the direction passed every current safety gate.
                        decision=decision.model_copy(update={'speed':max_speed,
                            'duration_ms':min(1000,max_duration)})
                        reason+=f' Profil operátora: PWM {max_speed}, {min(1000,max_duration)} ms.'
                    if decision.duration_ms>max_duration:
                        reason+=f' Délka omezena na {max_duration} ms podle směru a schopností firmwaru.'
                    decision=decision.model_copy(update={'speed':min(decision.speed,max_speed),'duration_ms':min(decision.duration_ms,max_duration)})
                command=None
                trace_id=secrets.token_hex(12) if action!='stop' or permitted else None
                actual={'action':action,'speed':decision.speed,'duration_ms':decision.duration_ms,'tilt':decision.tilt} if action!='stop' else {'action':'stop','speed':0,'duration_ms':100,'tilt':90} if permitted else None
                diagnostic_event('gate',command_id=trace_id,proposal={**proposed_command,'reason':decision.reason,'evidence':decision.evidence},
                    actual=actual,telemetry=t,permitted=permitted,executed=action,reason=reason,epoch=state['epoch'],source=source)
                if task_mode:diagnostic_frame(image,'before',command_id=trace_id,captured_at_ms=int(captured_utc*1000))
                token=command_trace_id.set(trace_id)
                try:
                    if action!='stop':
                        if task_mode:
                            task_pending={'_before_image':image,'_before_telemetry':observed_before,
                                '_command':{'action':action,'speed':decision.speed,'duration_ms':decision.duration_ms,'tilt':decision.tilt},
                                'command_id':trace_id,'proposed_action':raw_action,'reason':decision.reason,'evidence':decision.evidence,'proposed_command':proposed_command,
                                'route_choice':decision.route_choice.model_dump() if decision.route_choice else None,
                                'waypoint':decision.waypoint.model_dump() if decision.waypoint else None,
                                'executed_action':action,'say':decision.say,'execution_reason':'Přijetí povelu se ověřuje.','acceptance':'unknown'}
                        await send_control(action,decision.speed,decision.duration_ms,decision.tilt)
                        command={'action':action,'speed':decision.speed,'duration_ms':decision.duration_ms,'tilt':decision.tilt}
                        if task_mode:task_pending.update(acceptance='accepted',execution_reason=reason)
                    elif permitted:
                        await send_control('stop')
                        command={'action':'stop','speed':0,'duration_ms':100,'tilt':90}
                finally:command_trace_id.reset(token)
                if action not in ('stop','tilt'):floor_seen=0
            result={**decision.model_dump(),'proposed_action':raw_action,'executed_action':action,
                    'proposed_command':proposed_command,'command_id':trace_id,
                    'execution_reason':reason,'simulation':settings['simulation'],
                    'elapsed_ms':round((time.monotonic()-captured)*1000),'source':source,
                    'replans':replans,'loop_limit':loop_limit,'vision_frames':len(frames),
                    'navigation_state':exploration,
                    'motion_observation':frames[-1].get('motion_observation') if frames else None}
            if mode=='ai' and body.intent=='act':
                mission['recent']=(mission['recent']+[{'at':time.time(),'proposed':decision.action,'executed':action,
                    'evidence':decision.evidence,'result':reason,'observed_before':observed_before,
                    'observed_at_execution':observed_at_execution,'proposed_command':proposed_command,'command':command,'replans':replans,'motion_observation':result['motion_observation']}])[-12:]
                recent=mission['recent'][-4:]
                if not task_mode and mission['status']=='running' and len(recent)==4 and all(r['executed'] in ('stop','tilt') for r in recent):
                    mission.update(status='blocked',reason='Čtyři kroky bez průjezdu. Potřebuji pomoc nebo změnu pohledu.')
                save_mission()
            if epoch==state['epoch']:state['last_reason']=reason
            now=time.time()
            db.executemany('INSERT INTO messages(role,content,at) VALUES(?,?,?)',[
                ('user',body.text,now),('assistant',decision.say,now)])
            if decision.memory and source in ('car','phone','simulator'):
                db.execute('INSERT INTO memory(text,source,at) VALUES(?,?,?)',(decision.memory,'AI pozorování · '+source,now))
            db.execute('INSERT INTO decisions(payload,at) VALUES(?,?)',(json.dumps(result,ensure_ascii=False),now))
            db.execute('DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT 200)')
            db.execute('DELETE FROM memory WHERE source LIKE ? AND id NOT IN (SELECT id FROM memory ORDER BY id DESC LIMIT 60)',('AI pozorování%',))
            db.commit();emit('reply',**result)
            if task_mode:
                result.update(_before_image=image,_before_telemetry=observed_before,_command=command)
            return result
        except HTTPException:raise
        except Exception as exc:
            if task_mode:raise
            # No retry that might execute a motion twice. Invalidate any earlier authority.
            await halt('Odpověď AI není použitelná, auto stojí.')
            emit('error',message='AI nebo kamera neodpovídá. Zkus nový snímek.')
            raise HTTPException(502,'AI nebo kamera neodpovídá, případně vrátila neplatné rozhodnutí. Auto stojí.') from exc
        finally:state['busy']=False


def task_authorized(sid):
    return active(sid) and state['armed'] and not state['estop'] and state['mode']=='ai'

async def task_capture():
    # Read-only retries are safe; accepted motor commands are never retried.
    for attempt in range(3):
        try:
            raw=simulation_jpeg() if settings['simulation'] else (await device('/capture')).content
            return checked_image('data:image/jpeg;base64,'+base64.b64encode(oriented_car_jpeg(raw)).decode())
        except Exception:
            if attempt==2:raise
            await asyncio.sleep(.2*(attempt+1))

def verified_steps():
    return [{'step':r.get('step'),'command':r.get('command'),'acceptance':r.get('acceptance'),
             'decision_reason':str(r.get('decision_reason') or '')[:400],
             'decision_evidence':str(r.get('decision_evidence') or '')[:400],
             'assessment':{k:str(v)[:400] for k,v in (r.get('assessment') or {}).items() if k in ('status','progress','summary','evidence')}}
            for r in (task_runner.data.get('completed_steps',[]) if task_runner else [])][-40:]

async def assess_step(goal,before,after,command,before_t,after_t,observation,*,max_output_tokens=None):
    verified=verified_steps()
    route_memory=task_runner.data.get('route_memory',{}) if task_runner else {}
    messages=(short_assessment_messages(goal,before,after,command,before_t,after_t,observation,verified,route_memory)
              if SIMULATION_ONLY else
              assessment_messages(goal,before,after,command,before_t,after_t,observation,verified,route_memory))
    schema=ASSESSMENT_SCHEMA
    if command.get('route_choice'):
        schema=verification_schema(schema,route_memory)
    value,meta=await ai_provider.infer(messages,schema,
        provider=settings['ai_provider'],model=settings['openai_model'],thinking=False,
        **({'max_output_tokens':max_output_tokens} if max_output_tokens is not None else {}))
    if set(value)!=set(schema['required']) or value.get('status') not in ('continue','completed','blocked') or value.get('progress') not in ('changed','unchanged','unknown'):
        raise ValueError('Invalid assessment')
    if not all(isinstance(value[k],str) and len(value[k])<=3000 for k in ASSESSMENT_SCHEMA['required']):raise ValueError('Invalid assessment fields')
    if 'route_feedback' in value:
        feedback=value['route_feedback']
        if (not isinstance(feedback,dict) or set(feedback)!= {'status','evidence','place_id','place_label','place_evidence'}
            or feedback['status'] not in ('visible','closer','passed','blocked','lost','unknown')
            or feedback['place_id'] not in schema['properties']['route_feedback']['properties']['place_id']['enum']
            or any(not isinstance(feedback[k],str) or len(feedback[k])>limit
                   for k,limit in (('evidence',300),('place_label',120),('place_evidence',300)))):raise ValueError('Invalid route feedback')
    return value,meta

async def task_step(sid,goal,previous,set_phase):
    context={'task_id':task_runner.data.get('id') if task_runner else None,
             'step':task_runner.data.get('current_step',0)+1 if task_runner else None}
    token=task_trace.set(context)
    try:return await execute_task_step(sid,goal,previous,set_phase)
    except Exception as exc:
        if isinstance(exc,(HTTPException,httpx.HTTPError)):
            transient=isinstance(exc,httpx.HTTPError) or exc.status_code in (502,503,504)
            if transient and settings['transport']=='relay' and task_runner and task_runner.data['status']=='running':
                relay_link_change(False)
                raise asyncio.CancelledError() from None
        # Classify locally; never stringify an exception or inspect its request/body.
        try:
            status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else getattr(exc,'status_code',None)
            category=exc.category if isinstance(exc,AIProviderError) else None
            diagnostic_event('task_error',phase=task_runner.data.get('phase') if task_runner else None,
                error_type=type(exc).__name__,
                error_category=category if type(category) is str and category in AI_PROVIDER_ERROR_CATEGORIES else None,
                **({'status_code':status} if type(status) is int else {}))
        except Exception:pass  # Diagnostics must preserve the original failure.
        raise
    finally:task_trace.reset(token)

async def execute_task_step(sid,goal,previous,set_phase):
    global task_pending
    guard(sid);epoch=state['epoch'];set_phase('planning')
    feedback=(previous or {}).get('assessment')
    text=('Pracuj na CELÉM úkolu: '+goal+'. Vyber další konkrétní krok; krátký pohyb není sám o sobě splnění vícekrokového cíle. '
          'Výsledek skutečné akce ověří samostatný snímek PO. '
          +'Poslední ověřený výsledek (data): '+json.dumps(feedback,ensure_ascii=False))
    if task_pending:
        # A disconnect may hide the acknowledgement or after-image. Observe the
        # outstanding action first; never send it a second time.
        result=dict(task_pending);set_phase('recovering_result')
    else:
        result=await execute_turn(Turn(text=text[:2000],vision=True,source='car',intent='act'),sid,task_mode=True)
    before=result.pop('_before_image');before_t=result.pop('_before_telemetry');command=result.pop('_command')
    diagnostic_frame(before,'before',command_id=result.get('command_id'),captured_at_ms=int(before_t['captured_at']*1000) if before_t.get('captured_at') else None)
    guard(sid)
    if epoch!=state['epoch']:raise asyncio.CancelledError()
    set_phase('settling')
    duration=(command or {}).get('duration_ms',100)/1000
    await asyncio.sleep(duration+.2)
    guard(sid)
    async with motion_lock:
        if epoch!=state['epoch']:raise asyncio.CancelledError()
        guard(sid);await send_control('stop')
    set_phase('observing_after')
    after=await task_capture()
    after_captured_utc=time.time()
    guard(sid)
    if epoch!=state['epoch']:raise asyncio.CancelledError()
    after_t=await planning_telemetry()
    after_t={**after_t,'captured_at':after_captured_utc}
    diagnostic_frame(after,'after',command_id=result.get('command_id'),captured_at_ms=int(after_captured_utc*1000))
    if after:
        frames=vision_history.observe(after,after_t,'simulator' if settings['simulation'] else 'car')
        observation=frames[-1].get('motion_observation')
        set_phase('verifying')
        verified_command={**(command or {}),'acceptance':result.get('acceptance','accepted')}
        if result.get('route_choice'):verified_command['route_choice']=result['route_choice']
        assessment,metadata=await verification_retry(
            lambda budget:assess_step(goal,before,after,verified_command,before_t,after_t,observation,max_output_tokens=budget),
            lambda:task_authorized(sid) and state['epoch']==epoch,
            on_attempt=lambda **fields:diagnostic_event('verification_attempt',command_id=result.get('command_id'),**fields))
    else:
        observation={'status':'unknown','reason':'after_capture_failed'}
        assessment={'status':'blocked','progress':'unknown','summary':'Chybí nový snímek po akci; povel se nebude opakovat.','evidence':'Výsledek není ověřený.','next_hint':''}
        metadata={}
    guard(sid)
    if epoch!=state['epoch']:raise asyncio.CancelledError()
    route_feedback=assessment.pop('route_feedback',None)
    if ((result.get('navigation_state') or {}).get('phase')=='exhausted'
        and assessment['status']!='completed'):
        assessment.update(status='blocked',summary=result['navigation_state']['reason'])
    if mission['recent']:
        mission['recent'][-1]['after_assessment']=assessment
        mission['recent'][-1]['motion_observation']=observation
        save_mission()
    # Goal progress is not a motion measurement: a useful search rotation can
    # change the view without finding the doorway. Require visual stall evidence
    # from both wheel steps, with comparable camera poses/exposure.
    if (not (SIMULATION_ONLY and result.get('navigation_state'))
        and previous and previous.get('action') in AI_WHEELS
        and previous.get('assessment',{}).get('progress')=='unchanged'
        and assessment['status']=='continue' and assessment['progress']=='unchanged'
        and (previous.get('motion_observation') or {}).get('status')=='possible_stall'
        and (observation or {}).get('status')=='possible_stall'
        and result['executed_action'] in AI_WHEELS):
        assessment.update(status='blocked',summary='Po dvou pohybových krocích zůstává srovnatelný pohled téměř stejný. Potřebuji kontrolu možného uváznutí.')
    diagnostic_event('assessment',command_id=result.get('command_id'),assessment=assessment,motion_observation=observation,
        before_sha256=diagnostic_image_hash(before),after_sha256=diagnostic_image_hash(after),
        before=before_t,after=after_t,command=command,provider=settings.get('ai_provider','gemma'),
        model=settings.get('openai_model') if settings.get('ai_provider')=='openai' else 'Gemma',latency_ms=metadata.get('latency_ms'))
    task_pending=None
    return {'action':result['executed_action'],'command':command,'say':result['say'],'acceptance':result.get('acceptance','accepted'),
            'command_id':result.get('command_id'),'proposed_action':result.get('proposed_action'),
            'proposed_command':result.get('proposed_command'),'decision_reason':result.get('reason'),
            'navigation_state':result.get('navigation_state'),
            'route_choice':result.get('route_choice'),'route_feedback':route_feedback,
            'waypoint':result.get('waypoint'),
            'decision_evidence':result.get('evidence'),
            'execution_reason':result['execution_reason'],'assessment':assessment,'inference':metadata,
            'observed_before':before_t,'observed_after':{k:after_t.get(k) for k in ('servo','distance_cm','telemetry_age_ms','light_on','captured_at')},
            'motion_observation':observation,'before_image':before,'after_image':after}

class TaskInput(StrictModel):
    goal:str=Field(min_length=1,max_length=1000)

@app.post('/api/task')
async def start_task(body:TaskInput,request:Request):
    global task_pending
    if alpha_runner and alpha_runner.active:raise HTTPException(409,'Běží serverový autonomní úkol.')
    sid=session(request);guard(sid)
    if not task_authorized(sid):raise HTTPException(409,'Zvol AI režim a převezmi řízení.')
    if turn_lock.locked():raise HTTPException(409,'Nejdřív pozastav rozpracovaný krok.')
    if task_runner.data['status'] in ('running','reconnecting'):raise HTTPException(409,'Nejdřív pozastav nebo zruš předchozí úkol.')
    if task_runner.worker and not task_runner.worker.done():raise HTTPException(409,'Předchozí krok ještě dobíhá.')
    goal=body.goal.strip()
    if not goal:raise HTTPException(422,'Napiš úkol.')
    mission.update(goal=goal,status='running',steps=0,recent=[],started_at=time.time(),reason='Provádím úkol sekvenčně.')
    save_mission();vision_history.clear();task_pending=None
    try:return task_runner.start(sid,goal)
    except ValueError as exc:raise HTTPException(409,str(exc))

@app.post('/api/task/{action}')
async def task_action(action:str,request:Request):
    sid=session(request)
    if action in ('pause','cancel'):
        await halt('Úkol pozastaven.' if action=='pause' else 'Úkol zrušen.',emergency=action=='cancel')
        if action=='cancel':task_runner.interrupt(status='cancelled',reason='Úkol zrušen.')
    elif action=='resume':
        guard(sid)
        try:
            result=task_runner.start(sid,task_runner.data['goal'],resume=True)
            mission.update(status='running',started_at=time.time(),reason='Pokračuji ve stejném úkolu.');save_mission()
            return result
        except ValueError as exc:raise HTTPException(409,str(exc))
    else:raise HTTPException(404,'Neznámá akce úkolu.')
    return task_runner.snapshot()

@app.get('/api/task/frames/{role}')
async def task_frame(role:str,request:Request):
    session(request)
    if role not in ('before','after'):raise HTTPException(404)
    version=request.query_params.get('v')
    if version and version!=(task_runner.data.get('last_result') or {}).get('id'):
        raise HTTPException(404,'Tento pár snímků už byl nahrazen novějším krokem.')
    raw=task_runner.frame(role)
    if not raw:raise HTTPException(404,'Snímek není dostupný.')
    return Response(raw,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

class AIKeyInput(StrictModel):
    key:str=Field(min_length=1,max_length=1000)

class AIModelInput(StrictModel):
    model:str=Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_.:-]+$')

@app.post('/api/ai/key')
async def ai_key(body:AIKeyInput,request:Request):
    session(request);diagnostic_protect(body.key);await halt('Změna AI připojení.')
    try:ai_provider.set_key(body.key)
    except (ValueError,AIProviderError):raise HTTPException(422,'Neplatný formát klíče.')
    return {'ok':True,'has_openai_key':True}

@app.get('/api/ai/models')
async def ai_models(request:Request):
    session(request)
    try:return {'models':await ai_provider.models()}
    except AIProviderError as exc:raise HTTPException(exc.status_code,str(exc))

@app.post('/api/ai/test')
async def ai_test(body:AIModelInput,request:Request):
    session(request)
    if turn_lock.locked() or (task_runner and task_runner.data['status'] in ('running','reconnecting')):
        raise HTTPException(409,'Test modelu spusť při pozastaveném úkolu.')
    try:return await ai_provider.probe(body.model)
    except AIProviderError as exc:raise HTTPException(exc.status_code,str(exc))

@app.get('/api/memory')
async def get_memory(request:Request):session(request);return {'items':memory(),'history':history()}

@app.post('/api/memory')
async def add_memory(request:Request):
    session(request);body=await request.json();text=str(body.get('text','')).strip()[:300]
    if not text:raise HTTPException(422,'Napiš poznámku.')
    db.execute('INSERT INTO memory(text,source,at) VALUES(?,?,?)',(text,'Uživatel',time.time()));db.commit()
    return {'ok':True}

@app.delete('/api/memory/{item_id}')
async def delete_memory(item_id:int,request:Request):
    session(request);db.execute('DELETE FROM memory WHERE id=?',(item_id,));db.commit();return {'ok':True}

@app.post('/api/forget')
async def forget(request:Request):
    session(request)
    if turn_lock.locked():raise HTTPException(409,'Počkej na dokončení odpovědi.')
    db.executescript('DELETE FROM memory; DELETE FROM messages; DELETE FROM decisions;');db.commit()
    return {'ok':True}

@app.post('/api/tts')
async def tts(request:Request):
    session(request);body=await request.json()
    try:
        r=await client.post(VOICE+'/api/tts',json={'text':str(body.get('text',''))[:600],'emotion':'neutral'},timeout=150)
        if not r.is_success:raise ValueError()
        return Response(r.content,media_type='audio/wav')
    except Exception:raise HTTPException(503,'Hlasová syntéza není dostupná. Odpověď zůstává v chatu.')

@app.post('/api/stt')
async def stt(request:Request):
    session(request)
    params={k:request.query_params[k] for k in ('live','final') if request.query_params.get(k)=='1'}
    try:
        r=await client.post(VOICE+'/api/stt',params=params,content=await request.body(),headers={'Content-Type':'audio/wav'},timeout=150)
        return JSONResponse(r.json(),status_code=r.status_code)
    except Exception:raise HTTPException(503,'Přepis hlasu je právě nedostupný.')

@app.get('/api/events')
async def events(request:Request):
    session(request)
    q=asyncio.Queue(maxsize=30);listeners.add(q)
    async def gen():
        try:
            while not await request.is_disconnected():
                try:yield 'data: '+json.dumps(await asyncio.wait_for(q.get(),10),ensure_ascii=False)+'\n\n'
                except asyncio.TimeoutError:yield ': heartbeat\n\n'
        finally:listeners.discard(q)
    return StreamingResponse(gen(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

RADAR = os.environ.get('ROVER_RADAR_URL', 'http://127.0.0.1:27261').rstrip('/')

async def autonomy_inspect():
    t=await telemetry()
    if settings['simulation']:t={**t,'connected':True}
    return {'telemetry':t,'epoch':state['epoch'],'armed':state['armed'],
            'estop':state['estop'] or bool(t.get('estop')),'owned':state['owner']==alpha_runner.owner}

def autonomy_heartbeat():
    if alpha_runner and alpha_runner.active and state['owner']==alpha_runner.owner:
        state['heartbeat']=time.monotonic()

async def autonomy_acquire():
    if not alpha_runner.active or state['mode']!='ai':raise ConnectionLost('Autonomní řízení není aktivní.')
    try:await take_control(alpha_runner.owner,alpha_runner.generation)
    except (httpx.HTTPError,HTTPException) as e:raise ConnectionLost('Auto zatím nepotvrdilo převzetí řízení.') from e

async def autonomy_drive(action,speed,clearance,tilt,duration,epoch):
    if not alpha_runner.active or state['mode']!='ai':raise ConnectionLost('Autonomní řízení není aktivní.')
    try:
        return await drive_command(Drive(action=action,speed=speed,clearance_cm=clearance,
                                        tilt=tilt,duration_ms=duration),alpha_runner.owner,epoch)
    except HTTPException as e:
        if e.status_code==409 and state['epoch']==epoch and active(alpha_runner.owner) and state['armed']:
            raise MotionRefused(str(e.detail)) from e
        raise ConnectionLost('Povel nelze potvrdit; čekám na obnovení spojení.') from e
    except httpx.HTTPError as e:raise ConnectionLost('Auto nepotvrdilo povel; jeho výsledek je nejistý.') from e

async def autonomy_stop(reason):
    if state['owner']==alpha_runner.owner:
        await halt(reason,preserve_autonomy=True)

async def autonomy_radar(action,body):
    try:
        r=await client.post(f'{RADAR}/autopilot/{action}',json=body,timeout=30)
        data=r.json()
    except (httpx.HTTPError,ValueError) as e:raise ConnectionLost('Radarová služba neodpovídá.') from e
    detail=str(data.get('detail') or data.get('error') or '')
    if r.status_code==409 and 'běh alfy' in detail:raise RadarReset(detail)
    if not r.is_success:raise ConnectionLost('Radar nebo kamera zatím nejsou připravené.')
    return data

@app.get('/api/autonomy')
async def autonomy_status(request:Request):
    session(request)
    return alpha_runner.snapshot()

@app.post('/api/autonomy')
async def autonomy_start(body:TaskInput,request:Request):
    sid=session(request)
    if SIMULATION_ONLY:raise HTTPException(409,'Tento izolovaný simulátor nepoužívá radar fyzického auta.')
    if state['mode']!='ai':raise HTTPException(409,'Zvol režim S Kvídem.')
    if turn_lock.locked() or (task_runner and task_runner.data['status'] in ('running','reconnecting')):
        raise HTTPException(409,'Nejprve ukonči předchozí úkol nebo AI krok.')
    if state['armed'] and state['owner']!=sid and active(state['owner']):
        raise HTTPException(409,'Řízení drží jiný operátor.')
    try:alpha_runner.start(body.goal)
    except ValueError as e:raise HTTPException(409,str(e))
    # The run is established before yielding, so a simultaneous STOP wins.
    await halt('Zahajuji uložený autonomní úkol.',preserve_autonomy=True)
    return alpha_runner.snapshot()

@app.post('/api/autonomy/{action}')
async def autonomy_action(action:str,request:Request):
    session(request)
    if action in ('pause','cancel'):
        alpha_runner.interrupt('Úkol pozastaven uživatelem.' if action=='pause' else 'Úkol zrušen uživatelem.',
                               status='paused' if action=='pause' else 'cancelled')
        await halt('Úkol pozastaven uživatelem.' if action=='pause' else 'Úkol zrušen uživatelem.',
                   emergency=action=='cancel',preserve_autonomy=True)
    elif action=='resume':
        if state['mode']!='ai':raise HTTPException(409,'Zvol režim S Kvídem.')
        if state['armed'] or (task_runner and task_runner.data['status'] in ('running','reconnecting')):
            raise HTTPException(409,'Nejdřív ukonči jiné řízení.')
        try:await alpha_runner.resume()
        except ValueError as e:raise HTTPException(409,str(e))
    else:raise HTTPException(404,'Neznámá akce autonomie.')
    return alpha_runner.snapshot()

@app.get('/radar')
async def radar_slash():return Response(status_code=307,headers={'Location':'radar/'})

@app.post('/radar/autopilot/{action}')
async def radar_autopilot(action:str,request:Request):
    """Alpha autopilot: the radar service only answers what to do next; the page drives."""
    session(request)
    if alpha_runner and alpha_runner.active:raise HTTPException(409,'Radar používá serverový autonomní úkol.')
    if action not in ('start','step'):raise HTTPException(404,'Neznámá akce alfy.')
    body=await request.body()
    if len(body)>2048:raise HTTPException(413,'Příliš dlouhý úkol.')
    try:r=await client.post(f'{RADAR}/autopilot/{action}',content=body,headers={'Content-Type':'application/json'},timeout=30)
    except Exception:raise HTTPException(503,'Radarová služba neběží.')
    return Response(r.content,status_code=r.status_code,media_type='application/json',headers={'Cache-Control':'no-store'})

@app.get('/radar/{name:path}')
async def radar(name:str,request:Request):
    """Read-only camera radar dashboard (radar_service on this host) behind the cockpit login."""
    if name not in ('','state.json','latest.jpg'):raise HTTPException(404,'Neznámá stránka radaru.')
    if sessions.get(request.cookies.get(SESSION_COOKIE,''),0)<time.time():
        if name:raise HTTPException(401,'Přihlas se přístupovým klíčem.')
        return Response('<!doctype html><meta charset="utf-8"><title>Kvído radar</title><body style="font:16px system-ui;background:#0f1410;color:#e8eddf;padding:24px">'
                        '<p>Nejdřív se přihlas v <a style="color:#d0ed8a" href="../">kokpitu</a>, pak otevři radar znovu.</p>',media_type='text/html')
    try:r=await client.get(f'{RADAR}/{name or "radar"}',timeout=5)
    except Exception:raise HTTPException(503,'Radarová služba neběží.')
    return Response(r.content,status_code=r.status_code,media_type=r.headers.get('content-type'),headers={'Cache-Control':'no-store'})

@app.get('/')
async def index():return FileResponse(ROOT/('server/static/simulator.html' if SIMULATION_ONLY else 'server/static/index.html'))

app.mount('/static',StaticFiles(directory=ROOT/'server/static'),name='static')
