"""Bounded private diagnostic timeline; transport acceptance isn't wheel motion.

The application only sanitizes and enqueues. A daemon writes/rotates JSONL and
optional before/after JPEGs. Dropped events and write errors are visible in
status(); logging failure must never change motor/STOP behavior.
"""
import hashlib
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid


EVENT_FIELDS={
    'startup':{'provider','model','policy','limits','simulation','transport'},
    'settings':{'provider','model','policy','limits','simulation','transport','thinking_enabled'},
    'task_phase':{'phase','status','outcome','reason','goal','elapsed_s','completed_step_count'},
    'task_error':{'phase','error_type','error_category','status_code'},
    'verification_attempt':{'attempt','error_category','latency_ms','token_budget'},
    'planner_attempt':{'attempt','error_category','latency_ms','token_budget'},
    'telemetry':{'telemetry','source'},
    'planning_telemetry':{'telemetry','require_distance','attempt','elapsed_ms','reason'},
    'model_proposal':{'proposal','latency_ms','attempt','provider','model','policy','goal','prompt_sha256','usage'},
    'model_replan':{'proposal','feedback','attempt'},
    'gate':{'proposal','actual','telemetry','permitted','executed','reason','epoch','source'},
    'command_request':{'action','speed','duration_ms','tilt','simulation','transport'},
    'command_result':{'action','acceptance','latency_ms','category','status_code','simulation'},
    'relay_link':{'connected','relay','reason'},
    'assessment':{'assessment','motion_observation','before','after','command','provider','model','latency_ms','before_sha256','after_sha256'},
    'frame':{'role','sha256','size_bytes','relative_file','stored','captured_at_ms'},
}
CONTEXT={'task_id','step','command_id'}
AI_PROVIDER_ERROR_CATEGORIES=frozenset({
    'key_not_configured','key_unreadable','key_invalid','key_storage_failed',
    'unsupported_message','invalid_model','timeout','connection_failed',
    'authentication_failed','access_denied','model_unavailable','rate_limited','insufficient_quota',
    'upstream_error','request_rejected','invalid_response','incomplete_response',
    'refused_response','invalid_structured_output','vision_probe_unverified',
    'model_requires_vision_probe','invalid_provider',
})
TELEMETRY_FIELDS={
    'connected','distance_cm','telemetry_age_ms','servo','motor','motor_byte','motor_active',
    'pwm_left','pwm_right','stop_reason','refusal_reason','safety_reason','rssi','uptime_ms',
    'reset_reason','free_heap','min_free_heap','loop_max_ms','light_on','light_supported',
    'wifi_disconnect_reason','wifi_disconnect_count','wifi_disconnect_at_ms','wifi_retry_count',
    'camera_ready','uart','simulation','captured_at','relay','ultra','cmd','drive','ultrasonic_valid_count',
    'ultrasonic_invalid_count','ultrasonic_timeout_count','ultrasonic_measurements',
    'motor_max_duration_ms','forward_stop_cm','firmware_version','motor_max_speed','esp_motor_max_speed',
}
RELAY_FIELDS={
    'connected','device_id','telemetry_age_ms','telemetry_fresh','last_disconnect','reason',
    'at_ms','close_code','enabled','online','transport_connected','clock_ready','epoch_s',
    'connect_attempts','connections','disconnects','error_events','last_event',
    'last_event_at_ms','last_start_error','task_error','worker_at_ms','ntp_attempts',
    'ntp_sync_at_ms','last_error','last_error_at_ms',
}
NESTED_FIELDS={
    'ultra':{'seq','us','pre','post','ok','null','last_cm','last_age_ms','age_ms'},
    'cmd':{'seq','code','dir','speed','ms','ok','why','age_ms'},
    'drive':{'seq','dir','speed','ms','ok','why','age_ms','accepted','denied'},
    'motor':{'active','speed','start_ms','deadline_ms','stop','null_stops','near_stops','deadline_stops'},
    'telemetry':TELEMETRY_FIELDS,'before':TELEMETRY_FIELDS,'after':TELEMETRY_FIELDS,
    'relay':RELAY_FIELDS,'last_disconnect':{'reason','at_ms','close_code'},
    'proposal':{'action','speed','duration_ms','tilt','hazard','traversable','turn_clear','confidence','goal_status','reason','evidence','waypoint'},
    'waypoint':{'x','y','evidence'},
    'actual':{'action','speed','duration_ms','tilt'},
    'command':{'action','speed','duration_ms','tilt','acceptance','command_id'},
    'assessment':{'status','progress','summary','evidence','next_hint'},
    'motion_observation':{'status','reason','movement_confirmed','mean_absolute_difference','similarity_threshold','previous_at'},
    'usage':{'input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens'},
    'limits':{'sensor_max_age_ms','lease_ms','relay_recovery_ms','task_steps','task_seconds',
              'forward_min_cm','rotation_min_cm','forward_speed','forward_duration_ms',
              'rotation_speed','rotation_duration_ms','firmware_max_duration_ms'},
}


class DiagnosticTimeline:
    def __init__(self,directory,max_bytes=5*1024*1024,backups=2,queue_size=512,
                 max_frames=120,max_frame_bytes=30*1024*1024,secrets=()):
        self.directory=Path(directory)
        self.path=self.directory/'timeline.jsonl'
        self.max_bytes=max_bytes;self.backups=backups
        self.max_frames=max_frames;self.max_frame_bytes=max_frame_bytes
        self._queue=queue.Queue(maxsize=queue_size)
        self._frame_slots=threading.BoundedSemaphore(8)
        self._closing=threading.Event()
        self._seq=0;self._secrets=()
        self.enabled=True;self.dropped=0;self.write_errors=0
        self._telemetry_at=0.;self._telemetry_fingerprint=None
        self._frame_pairs=deque(maxlen=512)
        self._stored_roles={}
        self.add_secrets(*secrets)
        self._worker=threading.Thread(target=self._run,name='kvido-diagnostics',daemon=True)
        self._worker.start()

    def add_secrets(self,*values):
        self._secrets=tuple(set(self._secrets)|{value for value in values if isinstance(value,str) and len(value)>=4})

    def _text(self,value):
        for secret in self._secrets:
            value=value.replace(secret,'[REDACTED]')
        if re.search(r'data:|base64|authorization|\bbearer\b|api[_ -]?key|access[_ -]?key|relay[_ -]?key|password|set-cookie|\bcookie\b|[?&](?:key|token|secret)=',value,re.I):
            return '[REDACTED]'
        value=re.sub(r'\bsk-[A-Za-z0-9_-]{8,}','[REDACTED]',value)
        value=re.sub(r'[A-Za-z0-9+/=_-]{120,}','[REDACTED]',value)
        return value[:1200]

    def _sanitize(self,value,key=None,depth=0):
        if depth>5:return None
        if value is None or type(value) is bool:return value
        if type(value) in (int,float):return value if math.isfinite(value) else None
        if isinstance(value,str):return self._text(value)
        if isinstance(value,dict):
            allowed=NESTED_FIELDS.get(key,set())
            return {name:self._sanitize(item,name,depth+1) for name,item in value.items() if name in allowed}
        return None

    def _entry(self,kind,fields):
        if kind not in EVENT_FIELDS:return None
        self._seq+=1
        result={'v':1,'event':kind,'at_ms':int(time.time()*1000),
                'monotonic_ms':int(time.monotonic()*1000),'seq':self._seq}
        for key,value in fields.items():
            if key in EVENT_FIELDS[kind]|CONTEXT:result[key]=self._sanitize(value,key)
        return result

    def _enqueue(self,item):
        if self._closing.is_set():self.dropped+=1;return False
        try:self._queue.put_nowait(item);return True
        except queue.Full:self.dropped+=1;return False

    def event(self,kind,**fields):
        try:
            entry=self._entry(kind,fields)
            if entry is None:return False
            return self._enqueue((entry,None))
        except Exception:
            self.dropped+=1
            return False

    def telemetry(self,sample,**context):
        """Log changed observed samples at most five times/second, no polling."""
        try:
            safe=self._sanitize(sample,'telemetry')
            now=time.monotonic()
            fingerprint=json.dumps(safe,sort_keys=True,separators=(',',':'))
            if now-self._telemetry_at<.2 or fingerprint==self._telemetry_fingerprint:return False
            self._telemetry_at=now;self._telemetry_fingerprint=fingerprint
            return self.event('telemetry',telemetry=safe,**context)
        except Exception:
            self.dropped+=1
            return False

    def frame(self,raw,role,**context):
        """Queue only task before/after JPEG bytes, never a URL or video frame."""
        try:
            if role not in ('before','after') or not isinstance(raw,bytes) or not 4<=len(raw)<=2*1024*1024 or raw[:2]!=b'\xff\xd8':
                return False
            digest=hashlib.sha256(raw).hexdigest()
            pair=(context.get('command_id'),role,digest) if context.get('command_id') else (context.get('task_id'),context.get('step'),role,digest)
            if pair[0] is not None and pair in self._frame_pairs:return False
            entry=self._entry('frame',{'role':role,'sha256':digest,
                                      'size_bytes':len(raw),**context})
            if not self._frame_slots.acquire(blocking=False):self.dropped+=1;return False
            if not self._enqueue((entry,raw)):
                self._frame_slots.release();return False
            if pair[0] is not None:self._frame_pairs.append(pair)
            return True
        except Exception:
            self.dropped+=1
            return False

    def status(self):
        return {'enabled':self.enabled and not self._closing.is_set(),'file':'diagnostics/timeline.jsonl',
                'dropped':self.dropped,'write_errors':self.write_errors,'queue_depth':self._queue.qsize()}

    def close(self,timeout=.5):
        self._closing.set()
        self._worker.join(timeout=max(0,timeout))

    def _prepare(self):
        self.directory.mkdir(parents=True,exist_ok=True);self.directory.chmod(0o700)
        for number in range(self.backups+1):
            path=self.path if number==0 else self.path.with_name(self.path.name+f'.{number}')
            if path.exists():path.chmod(0o600)

    def _write(self,entry):
        line=(json.dumps(entry,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n').encode('utf-8')
        if len(line)>min(16384,self.max_bytes):self.dropped+=1;return
        if self.path.exists() and self.path.stat().st_size+len(line)>self.max_bytes:
            oldest=self.path.with_name(self.path.name+f'.{self.backups}')
            oldest.unlink(missing_ok=True)
            for number in range(self.backups-1,0,-1):
                source=self.path.with_name(self.path.name+f'.{number}')
                if source.exists():source.replace(self.path.with_name(self.path.name+f'.{number+1}'))
            if self.backups:self.path.replace(self.path.with_name(self.path.name+'.1'))
            else:self.path.unlink(missing_ok=True)
        descriptor=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(descriptor,'ab') as stream:stream.write(line)
        self.path.chmod(0o600)

    def _write_frame(self,entry,raw):
        folder=self.directory/'frames';folder.mkdir(exist_ok=True);folder.chmod(0o700)
        name='frame-'+uuid.uuid4().hex+'.jpg'
        path=folder/name
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as stream:stream.write(raw)
        path.chmod(0o600)
        entry.update(relative_file='frames/'+name,stored=True)
        if entry.get('task_id') is not None:
            role_key=(entry['task_id'],entry.get('step'),entry['role'])
            previous=self._stored_roles.get(role_key)
            self._stored_roles[role_key]=path
            if previous and previous!=path:previous.unlink(missing_ok=True)
        files=sorted((item for item in folder.glob('frame-*.jpg') if item.is_file()),key=lambda item:item.stat().st_mtime_ns)
        total=sum(item.stat().st_size for item in files)
        while files and (len(files)>self.max_frames or total>self.max_frame_bytes):
            oldest=files.pop(0);total-=oldest.stat().st_size;oldest.unlink()
        remaining=set(files)
        self._stored_roles={key:value for key,value in self._stored_roles.items() if value in remaining}

    def _run(self):
        while not self._closing.is_set() or not self._queue.empty():
            try:entry,raw=self._queue.get(timeout=.1)
            except queue.Empty:continue
            try:
                self._prepare()
                if raw is not None:
                    try:self._write_frame(entry,raw)
                    except Exception:
                        self.write_errors+=1;entry.update(stored=False)
                self._write(entry)
            except Exception:
                self.write_errors+=1;self.enabled=False
            finally:
                if raw is not None:self._frame_slots.release()
                self._queue.task_done()
