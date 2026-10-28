"""Visible accelerated SIMULATION experiment. Never connects to a physical car.

Sensor navigation receives telemetry + accepted commands + pixel comparisons.
Gemma receives goal + current image and bounded visual target memory. World truth is recorded exclusively
for the viewer/evaluator, never consulted by either decision component.
"""
import argparse
import os
import asyncio
import base64
from copy import deepcopy
import json
import math
from pathlib import Path
import secrets
import sys
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.ai_provider import AIProvider
from server.semantic_target import resolve_semantic_target
from server.sensor_navigator import SensorNavigator
from server.simulator import RoverSimulator
from server.simulator_perception import (TargetVisualMemory, perceive_current,
    validate_perception, perception_repair_feedback)
from server.verification_retry import verification_retry
from server.vision_history import VisionHistory
from server.visual_odometry import estimate_motion
from server.visual_motion_evidence import translation_evidence
from server.visual_target_track import VisualTargetTrack
from server.goal_approach import GoalApproachNavigator

GOAL = 'Najdi v bytě otevřené dveře s modrým rámem a dojeď k nim. Zastav před prahem.'


class Clock:
    now = 0.
    def __call__(self): return self.now


def encoded(raw):
    return 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()


class Experiment:
    def __init__(self, provider, name='sensor-gemma', max_steps=700):
        self.provider, self.name, self.max_steps = provider, name, max_steps
        self.worker = None
        self.generation = 0
        self.folder = None
        self.reset(GOAL)

    def reset(self, goal):
        self.clock = Clock()
        self.world = RoverSimulator(self.clock)
        self.world.reset('apartment')
        self.navigator = SensorNavigator()
        self.vision = VisionHistory(self.clock)
        self.camera = self.world.camera_jpeg()
        self.before = self.after = self.camera
        self.vision.observe(encoded(self.camera), self.world.telemetry(), 'simulator')
        self.state = dict(status='idle', phase='idle', step=0, goal=goal, reason='Připravený izolovaný experiment.',
                          perception=None, last_command=None, events=[], perception_calls=0, perception_memory_steps=0)
        self.scan_since_perception = 0
        self.goal_candidate_count = 0
        self.semantic_stalls = 0
        self.target_cooldown = 0
        self.target_memory = TargetVisualMemory(max_age_steps=3)
        self.target_track = VisualTargetTrack(self.clock)
        self.goal_approach = GoalApproachNavigator(self.navigator)
        self.approach_scan_attempts = 0
        self.unlocalized_alignments=0
        self.unlocalized_cooldown=0
        self.executing_pulse = False
        self.started = time.monotonic()

    def status(self):
        # Observer data has a separate output-only path; no decision reads it.
        return deepcopy({**self.state, 'navigator': self.navigator.snapshot(),
            'target_memory': self.target_memory.snapshot(),
            'target_track': self.target_track.snapshot(),
            'unlocalized_alignment_cooldown': self.unlocalized_cooldown,
            'observer': self.world.snapshot(), 'simulated_seconds': self.clock.now,
            'elapsed_seconds': round(time.monotonic()-self.started, 2),
            'report': str(self.folder) if self.folder else None})

    def save(self, row=None):
        if not self.folder: return
        snapshot = self.status()
        temporary = self.folder/'latest.tmp'
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        try: temporary.replace(self.folder/'latest.json')
        except PermissionError: pass
        if row is not None:
            with (self.folder/'timeline.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({**row, 'observer': snapshot['observer'],
                    'navigator': snapshot['navigator']}, ensure_ascii=False)+'\n')

    def record_inference_event(self, event):
        if not self.folder: return
        record = {'step': self.state['step']+1, **event}
        with (self.folder/'perception-attempts.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False)+'\n')

    async def pause(self):
        self.generation += 1
        self.state.update(status='paused', phase='paused', reason='Experiment pozastaven; virtuální motory stojí.')
        self.world.command('stop')
        if self.worker and not self.worker.done():
            if self.executing_pulse:
                # The motors already stopped. Finish recording the actual pulse,
                # so resume cannot silently lose an executed movement.
                await self.worker
            else:
                self.worker.cancel()
                try: await self.worker
                except asyncio.CancelledError: pass
        self.save()

    async def start(self, goal):
        await self.pause()
        self.reset(goal)
        self.folder = ROOT/'reports'/(self.name+'-'+secrets.token_hex(4))
        self.folder.mkdir(parents=True)
        self.state.update(status='running', phase='observing', reason='Mapuji neznámý byt; Gemma hledá cíl z aktuální kamery.')
        self.worker = asyncio.create_task(self.run(self.generation))

    async def resume(self):
        if self.state['status'] != 'paused': raise ValueError('Experiment není pozastavený.')
        self.state.update(status='running', phase='observing', reason='Pokračuji se stejnou odhadovanou mapou.')
        self.worker = asyncio.create_task(self.run(self.generation))

    def tracked_command(self, selected, proposed, telemetry, perception):
        """Route to a learned point; current pixels alone can confirm arrival."""
        if telemetry.get('estop') is True:
            return dict(action='stop',speed=0,duration_ms=100,tilt=telemetry.get('servo',90),
                reason='Nouzové zastavení má přednost před mapou i hledáním cíle.')
        tracked=self.target_track.snapshot()
        if tracked['point'] is None:return selected
        if (perception or {}).get('hazard') in ('person','animal','drop'):
            return selected
        if selected and (selected.get('candidate_goal_reached') or selected['action']=='tilt'):
            return selected
        pose=self.navigator.snapshot()['estimated_pose']
        if not tracked['usable']:
            return self.target_track.suggest_search(telemetry) or dict(
                action='stop',speed=0,duration_ms=100,tilt=telemetry['servo'],
                reason='Paměť cíle není použitelná; bez nového zaměření nepovolím jízdu.')
        # Finish requested coverage without a semantic alignment undoing it.
        if self.target_cooldown and proposed.get('scan'):
            return None
        distance=math.hypot(tracked['point']['x']-pose['x'],tracked['point']['y']-pose['y'])
        # Close approach uses today's image and the common map guard; far
        # travel uses the route rather than demanding a straight visible line.
        if distance<=1.5 and not self.target_cooldown:
            if not tracked['current_localized']:
                # An opening/jamb point is not the remembered FLOOR point.
                # Do not alternate its fresh alignment with an opposing old
                # bearing. Search monotonically for a fresh floor measurement.
                return self.target_track.suggest_search(telemetry) or dict(
                    action='stop',speed=0,duration_ms=100,tilt=telemetry['servo'],
                    reason='Blízko cíle chybí aktuální měření podlahové hrany; stojím.')
            if selected and selected['action'] not in ('stop',):return selected
            orient=self.target_track.suggest_orientation(pose,telemetry)
            if orient and orient['action']!='stop':return orient
        approach=self.goal_approach.next_command(tracked,telemetry)
        if approach['approach_status']=='approaching':
            if self.target_track.command_allowed(approach,pose):
                if approach['action'] in ('forward','backward','left','right'):
                    self.approach_scan_attempts=0
                return approach
            return self.target_track.suggest_search(telemetry) or dict(
                action='stop',speed=0,duration_ms=100,tilt=telemetry['servo'],
                reason='Ochranná kontrola odmítla cestu k cíli; bez nového návrhu nepřejdu na jinou jízdu.')
        if approach['approach_status'] in ('scan_required','no_observed_route'):
            if self.approach_scan_attempts<3:
                self.approach_scan_attempts+=1
                self.target_cooldown=self.navigator.SCAN_DIRECTIONS
                self.navigator.request_scan('Doplním známou cestu k uloženému cíli.')
                if approach.get('scan_offset_requested'):
                    self.target_cooldown+=1
                    return dict(action='rotate_left',speed=180,
                        duration_ms=round(self.navigator.SCAN_ANGLE/2/self.navigator.ROTATION_RAD_S*1000),
                        tilt=telemetry['servo'],scan=False,
                        reason='Posunu sonarový scan mezi předchozí paprsky; nepokrytá hrana není volná cesta.')
                scan_command=self.navigator.next_command(telemetry)
                return scan_command if scan_command.get('scan') else approach
            return approach
        # Standoff is not arrival: reorient/reobserve, then use fresh pixels.
        if not tracked['current_localized']:
            return self.target_track.suggest_search(telemetry) or approach
        orient=self.target_track.suggest_orientation(pose,telemetry)
        if orient and orient['action']!='stop':return orient
        return self.target_track.suggest_search(telemetry) or approach

    async def run(self, generation):
        current = lambda: generation == self.generation and self.state['status'] == 'running'
        try:
            while current() and self.state['step'] < self.max_steps:
                before_t = self.world.telemetry()
                self.before = self.camera = self.world.camera_jpeg()
                proposed = self.navigator.next_command(before_t)
                # At most two scan pulses between semantics. Every
                # translation and non-scan alignment gets a current observation.
                scan = bool(proposed.get('scan'))
                inspect = (not scan or self.scan_since_perception >= 1 or self.state['step'] == 0
                           or self.target_track.snapshot()['point'] is not None
                           or bool((self.state.get('perception') or {}).get('goal_visible'))
                           or (self.state.get('perception') or {}).get('hazard') in ('person','animal','drop'))
                perception = None
                selected = None
                metadata = {}
                memory_steps = 0
                semantic_path_deferred = False
                semantic_sensor_deferred = False
                if inspect:
                    previous_observation = self.target_memory.previous_observation()
                    memory_steps = previous_observation['age_steps'] if previous_observation else 0
                    self.state.update(phase='perception', perception_memory_steps=memory_steps,
                        reason='Gemma posuzuje aktuální snímek a krátkou obrazovou paměť cíle.' if memory_steps
                               else 'Gemma posuzuje aktuální snímek.')
                    self.save()
                    if self.folder:
                        (self.folder/f"step-{self.state['step']+1:04d}-before.jpg").write_bytes(self.before)
                    validation_feedback=None
                    async def infer(_budget):
                        nonlocal validation_feedback
                        self.state['perception_calls'] += 1
                        try:
                            return await perceive_current(self.provider, self.state['goal'], encoded(self.before),
                                previous_observation=previous_observation,
                                localize_simulated_threshold=True,
                                validation_feedback=validation_feedback)
                        except ValueError as error:
                            invalid = getattr(error, 'invalid_observation', None)
                            validation_feedback=perception_repair_feedback(invalid)
                            localization = getattr(error, 'current_threshold_localization', None)
                            if isinstance(localization,dict) and localization.get('status')=='current_marker_not_localized':
                                # A distant/occluded marker is an unavailable
                                # measurement, not a failure of sonar exploration.
                                # Do not use guessed coordinates or retain a goal
                                # confirmation. The semantic gate already required
                                # a clear scene; concrete hazards never enter here.
                                validate_perception(invalid)
                                self.record_inference_event({'kind':'unlocalized_target',
                                    'observation_json':json.dumps(invalid,ensure_ascii=False)[:8000],
                                    'localization':localization})
                                return None, {'provider':'gemma','status':'current_target_not_localized',
                                    'raw_model_perception':invalid,'current_threshold_localization':localization}
                            if isinstance(invalid, dict):
                                # Diagnostic model output only, never a future input.
                                self.record_inference_event({'kind':'invalid_perception',
                                    'observation_json':json.dumps(invalid, ensure_ascii=False)[:8000]})
                            raise
                    perception, metadata = await verification_retry(infer, current, max_attempts=3, timeout_s=120,
                        token_budget=900, max_token_budget=900,
                        on_attempt=lambda **event:self.record_inference_event({'kind':'attempt', **event}))
                    if not current(): return
                    if perception is None:
                        self.target_memory.clear(reason='current_target_not_localized')
                        proposed['reason']='Práh zatím nelze přesně změřit; pokračuji v průzkumu podle sonarové mapy.'
                    else:
                        self.target_memory.remember(encoded(self.before), perception,
                            used_memory=previous_observation is not None)
                    self.state['perception'] = perception
                    self.state['perception_diagnostic'] = metadata if perception is None else None
                    selected = resolve_semantic_target(perception, before_t) if perception is not None else None
                    pose = self.navigator.snapshot()['estimated_pose']
                    self.target_track.update(perception,metadata.get('current_threshold_localization'),before_t,pose)
                    if perception is None and metadata.get('status')=='current_target_not_localized':
                        selected=resolve_semantic_target(metadata['raw_model_perception'],before_t,floor_localized=False)
                    if (metadata.get('current_threshold_localization') or {}).get('status')=='localized':
                        self.unlocalized_alignments=self.unlocalized_cooldown=0
                    elif (self.target_track.snapshot()['point'] is None and selected
                          and selected.get('goal_handoff_pending') and selected['action'].startswith('rotate_')):
                        if self.unlocalized_cooldown:
                            selected=None
                        else:
                            self.unlocalized_alignments+=1
                            if self.unlocalized_alignments>=2:
                                # A distant visual hypothesis must not repeatedly
                                # undo the mapped route's steering. Complete a
                                # bounded exploration interval before trying again.
                                self.unlocalized_cooldown=self.navigator.SCAN_DIRECTIONS+4
                    if (selected and selected.get('navigation_block')=='front_sensor'
                            and self.target_track.snapshot()['point'] is not None):
                        # No echo is not free space. Keep the forward refusal,
                        # but permit mapped scanning/sideways alternatives while
                        # the remembered target region remains protected.
                        semantic_sensor_deferred=True
                        selected=None
                    # Seeing the goal does not establish a direct drivable path.
                    # Missing localization/clearance returns to mapped exploration;
                    # explicit people, animals and drops always retain their stop.
                    if (selected and selected['action']=='stop' and not selected.get('candidate_goal_reached')
                            and (not selected.get('goal_handoff_pending')
                                 or self.target_track.snapshot()['point'] is None)
                            and (perception or {}).get('hazard') not in ('person','animal','drop')):
                        selected = None
                    if self.target_cooldown and selected and selected['action'] not in ('stop','tilt'):
                        selected = None
                    if (selected and selected['action'] in ('forward','backward','left','right')
                            and not self.navigator.command_path_observed(selected)):
                        # Visible goals do not grant a shortcut through unknown
                        # chassis clearance. The same map gate covers both drivers.
                        semantic_path_deferred = True
                        self.target_cooldown = self.navigator.SCAN_DIRECTIONS + 2
                        self.navigator.request_scan('Doplním měření celé šířky cesty k viditelnému cíli.')
                        proposed = self.navigator.next_command(before_t)
                        proposed['reason'] = 'Cíl vidím; doplním měření průjezdu a podle mapy navážu další jízdou.'
                        selected = None
                    self.scan_since_perception = 0
                else:
                    self.scan_since_perception += 1
                # A previously localized target remains a spatial hypothesis
                # when it leaves the current camera view. Reacquire it instead
                # of letting exploration silently drive across its location.
                pose=self.navigator.snapshot()['estimated_pose']
                tracked=self.target_track.snapshot()
                selected=self.tracked_command(selected,proposed,before_t,perception)
                if not selected and not self.target_track.command_allowed(proposed,pose):
                    selected=dict(action='stop',speed=0,duration_ms=100,tilt=before_t['servo'],
                        reason='Průzkumný pohyb zasahuje okolí zapamatovaného prahu; znovu zaměřím cíl.',
                        goal_handoff_pending=True)
                command = deepcopy(selected or proposed)
                command.setdefault('command_id', 'semantic-'+str(self.state['step']+1))
                # The candidate was not executed. No estimated movement or scan
                # completion is committed until the selected actual command ends.
                if selected: command.setdefault('scan',False)
                if not current(): return
                self.state.update(phase='moving', last_command=command, reason=command['reason'])
                self.save()
                actual = {key:command[key] for key in ('action','speed','duration_ms','tilt')}
                self.executing_pulse = True
                acceptance = self.world.command(**actual)['accepted']
                self.target_memory.completed(actual, accepted=acceptance)
                if acceptance:
                    self.vision.command(**actual)
                self.clock.now += command['duration_ms']/1000 + .02
                after_t = self.world.telemetry()
                self.world.command('stop')
                self.after = self.camera = self.world.camera_jpeg()
                motion = self.vision.observe(encoded(self.after), after_t, 'simulator')[-1]['motion_observation']
                accepted_command = {**command, 'acceptance':'accepted' if acceptance else 'rejected'}
                visual_metric = None
                if acceptance and command['action'] in ('forward','backward','left','right'):
                    self.state['phase'] = 'measuring_motion'
                    try:
                        visual_metric = await asyncio.wait_for(asyncio.to_thread(estimate_motion, self.before, self.after,
                            command=accepted_command, before_servo=before_t['servo'], after_servo=after_t['servo']), 8)
                    except TimeoutError:
                        visual_metric = {'status':'unknown','reason':'visual_measurement_timeout','motion':None}
                    motion = translation_evidence(accepted_command, visual_metric, motion)
                # Record the already executed pulse even if STOP arrived while
                # the image calculation ran; the next loop still checks generation.
                self.navigator.complete(accepted_command, before_t, after_t, motion)
                self.target_track.note_command(accepted_command)
                if self.unlocalized_cooldown:
                    self.unlocalized_cooldown-=1
                    if not self.unlocalized_cooldown:self.unlocalized_alignments=0
                if self.target_cooldown: self.target_cooldown -= 1
                if selected and command['action']=='forward' and motion.get('status')=='possible_stall':
                    self.semantic_stalls += 1
                    if self.semantic_stalls >= 2:
                        self.target_cooldown = 8
                        self.semantic_stalls = 0
                elif command['action']=='forward' and motion.get('status')=='changed':
                    self.semantic_stalls = 0
                self.state['step'] += 1
                if current(): self.state['phase'] = 'verified'
                is_candidate = bool(selected and selected.get('candidate_goal_reached') and acceptance)
                self.goal_candidate_count = self.goal_candidate_count+1 if is_candidate else 0
                controller=('mapped_goal_approach' if command.get('approach_status') else
                            'target_reacquisition' if command.get('recovery_scan') else
                            'sonar_exploration' if not selected or command.get('scan') else 'semantic_target')
                row = dict(step=self.state['step'], action=actual['action'], command=accepted_command,
                    controller=controller, target_cooldown=self.target_cooldown,
                    reason=command['reason'], before_telemetry=before_t, after_telemetry=after_t,
                    motion_observation=motion, perception=perception, inference=metadata,
                    visual_odometry=visual_metric,
                    visual_memory_age_steps=memory_steps,
                    target_memory=self.target_memory.snapshot(),
                    target_track=self.target_track.snapshot(),
                    unlocalized_alignment_cooldown=self.unlocalized_cooldown,
                    semantic_path_deferred=semantic_path_deferred,
                    semantic_sensor_deferred=semantic_sensor_deferred,
                    candidate_goal_reached=is_candidate, assessment='Změna obrazu: '+str(motion.get('status')))
                self.state['events'] = (self.state['events']+[row])[-100:]
                self.save(row)
                if self.folder:
                    for role, raw in (('before',self.before),('after',self.after)):
                        (self.folder/f"step-{self.state['step']:04d}-{role}.jpg").write_bytes(raw)
                self.executing_pulse = False
                if not current(): break
                if self.goal_candidate_count >= 2:
                    self.state.update(status='completed', phase='completed',
                        reason='Dvě aktuální obrazová pozorování a kalibrovaný odhad potvrzují kandidáta dojezdu. Pravý panel ukazuje nezávislé měření simulátoru.')
                    break
                if selected and command['action'] == 'stop' and not is_candidate:
                    recent = self.state['events'][-3:]
                    if len(recent) == 3 and all(r['action']=='stop' for r in recent):
                        self.state.update(status='blocked', phase='blocked', reason='Tři zastavení bez použitelné lokalizace cíle; uloženy snímky a důvody.')
                        break
                await asyncio.sleep(.08)
            if current():
                self.state.update(status='blocked', phase='blocked', reason='Vyčerpán rozpočet experimentu; cíl nelze vydávat za splněný.')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.target_memory.clear()
            self.state.update(status='blocked', phase='blocked', reason='Experiment přerušen: '+type(error).__name__)
        finally:
            self.executing_pulse = False
            self.world.command('stop')
            self.save()


class StartInput(BaseModel):
    goal: str = Field(default=GOAL, min_length=1, max_length=1000)


def make_app(experiment):
    app = FastAPI()
    @app.middleware('http')
    async def same_origin_mutations(request: Request, call_next):
        origin = request.headers.get('origin')
        if request.method == 'POST' and origin and origin != str(request.base_url).rstrip('/'):
            return Response(status_code=403)
        return await call_next(request)
    @app.get('/')
    async def page(): return FileResponse(ROOT/'server/static/sensor_experiment.html')
    @app.get('/api/status')
    async def status(): return experiment.status()
    @app.get('/{role}.jpg')
    async def picture(role: str):
        if role not in ('camera','before','after'): raise HTTPException(404)
        return Response(getattr(experiment, role), media_type='image/jpeg', headers={'Cache-Control':'no-store'})
    @app.post('/api/start')
    async def start(body: StartInput):
        await experiment.start(body.goal.strip()); return experiment.status()
    @app.post('/api/pause')
    async def pause():
        await experiment.pause(); return experiment.status()
    @app.post('/api/resume')
    async def resume():
        try: await experiment.resume()
        except ValueError as error: raise HTTPException(409,str(error)) from None
        return experiment.status()
    return app


async def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=28743)
    parser.add_argument('--name', default='sensor-gemma')
    parser.add_argument('--max-steps', type=int, default=700)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not args.name.replace('-','').replace('_','').isalnum():
        parser.error('Invalid local port or report name')
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = AIProvider(client, ROOT/'data/simulator', ROOT, gemma_url=os.environ.get('ROVER_LLM_URL', 'http://127.0.0.1:8080'))
        experiment = Experiment(provider, args.name, args.max_steps)
        app = make_app(experiment)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning'))
        try: await server.serve()
        finally: await experiment.pause()


if __name__ == '__main__': asyncio.run(main())
