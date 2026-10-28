"""One sequential task; image pairs remain private and bounded in RAM."""
import asyncio
import base64
import copy
import json
import math
import secrets
import time
from pathlib import Path
from .route_memory import empty_route_memory, update_route_memory
from .ai_provider import AIProviderError


class TaskRunner:
    def __init__(self,path,step,authorized,halt,emit=None,max_steps=40,max_seconds=600,interval=.35):
        self.path=Path(path);self.step=step;self.authorized=authorized;self.halt=halt
        self.emit=emit or (lambda *args,**kwargs:None)
        self.max_steps=max_steps;self.max_seconds=max_seconds;self.interval=interval
        self.worker=None;self.generation=0;self._frames={};self._sid=None
        self._operator_at=None;self.reconnect_started=None
        self._started_at=None;self._elapsed_before_run=0
        self.data={'id':None,'status':'idle','phase':'idle','goal':'','current_step':0,
                   'outcome':'','last_result':None,'reason':'','elapsed_s':0,'completed_steps':[],'navigation_state':None,'route_memory':empty_route_memory()}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding='utf-8')))
            if self.data['status'] in ('running','reconnecting'):
                self.data.update(status='paused',phase='paused',reason='Server byl restartován. Pokračování vyžaduje převzetí řízení.')
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.chmod(0o600);tmp.replace(self.path)

    @property
    def owner(self):return self._sid

    def operator_heartbeat(self,sid):
        if sid!=self._sid or sid is None or self.data['status'] not in ('running','reconnecting'):
            return False
        now=time.monotonic()
        if self._operator_at is None or now-self._operator_at>=1.5:return False
        self._operator_at=now
        return True

    def can_recover(self):
        now=time.monotonic()
        return (self.data['status']=='reconnecting' and self._sid is not None
                and self._operator_at is not None and now-self._operator_at<1.5
                and self.reconnect_started is not None and now-self.reconnect_started<120)

    def recover(self,reason):
        if self.data['status'] not in ('running','reconnecting'):return False
        now=time.monotonic()
        if self.data['status']=='running' and self._started_at is not None:
            self.data['elapsed_s']=round(self._elapsed_before_run+now-self._started_at,2)
        if self.reconnect_started is None:self.reconnect_started=now
        self.generation+=1
        self.data.update(status='reconnecting',phase='reconnecting',reason=reason)
        self._publish()
        if self.worker and not self.worker.done() and self.worker is not asyncio.current_task():self.worker.cancel()
        return True

    def snapshot(self):return copy.deepcopy(self.data)

    async def reset(self,reason='Nová scéna.'):
        self.interrupt(status='cancelled',reason=reason)
        if self.worker and not self.worker.done():
            try:await asyncio.wait_for(self.worker,2)
            except (asyncio.CancelledError,asyncio.TimeoutError):pass
        self._frames={};self._sid=None;self._operator_at=None;self.reconnect_started=None
        self.data={'id':None,'status':'idle','phase':'idle','goal':'','current_step':0,
                   'outcome':'','last_result':None,'reason':reason,'elapsed_s':0,'completed_steps':[],'navigation_state':None,'route_memory':empty_route_memory()}
        self._publish()

    def frame(self,role):return self._frames.get(role)

    def _publish(self):
        self._save();self.emit('task',task=self.snapshot())

    def start(self,sid,goal,resume=False):
        if self.worker and not self.worker.done():raise ValueError('Úkol už běží nebo dobíhá předchozí krok.')
        if not self.authorized(sid):raise ValueError('Nejdřív převezmi AI řízení.')
        goal=goal.strip()
        if not goal:raise ValueError('Napiš úkol.')
        if resume and (self.data['current_step']>=self.max_steps or self.data.get('elapsed_s',0)>=self.max_seconds):
            raise ValueError('Úkol dosáhl limitu; zadej nový úkol.')
        self.generation+=1;generation=self.generation;self._sid=sid
        self._operator_at=time.monotonic();self.reconnect_started=None
        self._started_at=None
        if not resume:
            self._frames={}
            self.data.update(id=secrets.token_hex(8),goal=goal,current_step=0,last_result=None,elapsed_s=0,completed_steps=[],navigation_state=None,route_memory=empty_route_memory())
        self.data.update(status='running',phase='starting',reason='Provádím úkol po krocích.',outcome='')
        self._publish()
        self.worker=asyncio.create_task(self._run(sid,generation))
        return self.snapshot()

    def interrupt(self,status='paused',reason='Úkol pozastaven.'):
        if self.data['status'] not in ('running','reconnecting') and status!='cancelled':return
        if self.data['status']=='running' and self._started_at is not None:
            self.data['elapsed_s']=round(self._elapsed_before_run+time.monotonic()-self._started_at,2)
        self.generation+=1;self.reconnect_started=None
        self.data.update(status=status,phase=status,reason=reason)
        self._publish()
        if self.worker and not self.worker.done() and self.worker is not asyncio.current_task():self.worker.cancel()

    async def _finish(self,status,reason):
        self.data.update(status=status,phase=status,reason=reason,outcome=reason)
        self._publish()
        await self.halt(reason)

    async def _run(self,sid,generation):
        started=time.monotonic();previous_elapsed=self.data.get('elapsed_s',0)
        self._started_at=started;self._elapsed_before_run=previous_elapsed
        def current():return generation==self.generation and self.data['status']=='running'
        def phase(value):
            if current():self.data['phase']=value;self._publish()
        try:
            while current():
                if not self.authorized(sid):
                    await self._finish('paused','Řízení nebo spojení není aktivní.');break
                elapsed=previous_elapsed+time.monotonic()-started
                self.data['elapsed_s']=round(elapsed,2)
                if self.data['current_step']>=self.max_steps or elapsed>=self.max_seconds:
                    await self._finish('blocked','Dosažen limit kroků nebo doby úkolu.');break
                remaining=self.max_seconds-elapsed
                result=await asyncio.wait_for(self.step(sid,self.data['goal'],copy.deepcopy(self.data['last_result']),phase),timeout=remaining)
                if not current():return
                if previous_elapsed+time.monotonic()-started>=self.max_seconds:
                    await self._finish('blocked','Dosažen limit doby úkolu. Povel se neopakuje.');return
                if not self.authorized(sid):
                    await self._finish('paused','Řízení se během kroku přerušilo.');return
                before=result.pop('before_image',None);after=result.pop('after_image',None)
                self.data['current_step']+=1
                result.update(id=f"{self.data['id']}-{self.data['current_step']}",step=self.data['current_step'],at=time.time())
                self._frames={}
                for role,image in (('before',before),('after',after)):
                    if image:self._frames[role]=base64.b64decode(image.split(',',1)[1],validate=True)
                result['has_before']=bool(before);result['has_after']=bool(after)
                if before and after and result.get('route_choice'):
                    self.data['route_memory']=update_route_memory(self.data.get('route_memory'),result,self.data['current_step'])
                self.data['last_result']=result
                self.data['completed_steps']=(self.data.get('completed_steps',[])+[{
                    'step':self.data['current_step'],'action':result.get('action'),'command':result.get('command'),
                    'proposed_action':result.get('proposed_action'),'proposed_command':result.get('proposed_command'),
                    'decision_reason':result.get('decision_reason'),'decision_evidence':result.get('decision_evidence'),
                    'execution_reason':result.get('execution_reason'),
                    'command_id':result.get('command_id'),'motion_observation':result.get('motion_observation'),
                    'navigation_state':result.get('navigation_state'),
                    'route_choice':result.get('route_choice'),'route_feedback':result.get('route_feedback'),
                    'waypoint':result.get('waypoint'),
                    'assessment':result.get('assessment'),'acceptance':result.get('acceptance','unknown')}])[-self.max_steps:]
                self.data['elapsed_s']=round(previous_elapsed+time.monotonic()-started,2)
                self._publish()
                if not before or not after:
                    await self._finish('blocked','Chybí snímek před nebo po akci. Výsledek nelze ověřit.');break
                assessment=result.get('assessment') or {}
                verdict=assessment.get('status')
                if verdict=='completed':
                    await self._finish('completed',assessment.get('summary') or 'Úkol je podle nového pozorování splněn.');break
                if verdict=='blocked':
                    await self._finish('blocked',assessment.get('summary') or 'Úkol nemůže pokračovat.');break
                if verdict!='continue':
                    await self._finish('blocked','Neplatné vyhodnocení výsledku.');break
                recent=self.data['completed_steps'][-3:]
                if len(recent)==3 and all(
                    (entry.get('action') or (entry.get('command') or {}).get('action'))=='stop'
                    and (entry.get('assessment') or {}).get('status')=='continue'
                    and (entry.get('assessment') or {}).get('progress') in ('unchanged','unknown')
                    for entry in recent
                ):
                    distance=(result.get('observed_after') or {}).get('distance_cm')
                    valid_distance=type(distance) in (int,float) and math.isfinite(distance) and 2<=distance<=400
                    reason=('Tři kroky bez pohybu nebo pokroku. Čidlo neposkytuje platnou vzdálenost; potřebuji kontrolu.'
                            if not valid_distance else 'Tři kroky bez pohybu nebo ověřeného pokroku. Potřebuji pomoc nebo nový pokyn.')
                    await self._finish('blocked',reason);break
                phase('next_step')
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            return
        except AIProviderError as error:
            if current():
                reason=('OpenAI nemá dostupnou API kvótu. Doplň kredit nebo přepni AI poskytovatele; přijatý povel se neopakuje.'
                        if error.category=='insufficient_quota' else
                        'AI nedokončila plán nebo ověření ani po dostupných pokusech. Auto stojí; přijatý povel se neopakuje.')
                await self._finish('blocked',reason)
        except Exception:
            if current():await self._finish('blocked','Krok se nepodařilo ověřit. Auto bylo zastaveno; povel se neopakuje.')
        finally:
            if generation==self.generation:
                self.data['elapsed_s']=round(previous_elapsed+time.monotonic()-started,2);self._save()
