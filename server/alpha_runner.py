"""Persistent server-owned radar autopilot; browsers only submit and observe goals.

Motor requests are never replayed after an unknown outcome. The existing motor
guards and firmware pulse deadlines remain authoritative.
"""
import asyncio
import copy
import json
import secrets
import time
from pathlib import Path


class ConnectionLost(Exception):
    pass


class RadarReset(Exception):
    pass


class MotionRefused(Exception):
    pass


class AlphaRunner:
    ACTIVE = {'running', 'reconnecting'}

    def __init__(self, path, inspect, acquire, drive, stop, radar, heartbeat,
                 emit=None, sleep=asyncio.sleep):
        self.path = Path(path)
        self.inspect, self.acquire, self.drive = inspect, acquire, drive
        self.stop, self.radar, self.heartbeat = stop, radar, heartbeat
        self.emit = emit or (lambda *a, **kw: None)
        self.sleep = sleep
        self.worker = self.keeper = None
        self.owner = secrets.token_urlsafe(32)  # private; never serialized
        self.generation = 0
        self.link_generation = 0
        self.discard_observation = False
        self.data = self.empty()
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding='utf-8')))
        if self.active:
            self.data.update(status='reconnecting', phase='reconnecting',
                             reason='Server se vrátil. Ověřím spojení a pořídím nový pohled.')
            self.discard_observation = True
            if self.data.get('pending_command'):
                self.note('uncertain', 'Před restartem nebyl potvrzen výsledek pohybu; povel neopakuji.')
                self.data['pending_command'] = None
        self.save()

    @staticmethod
    def empty():
        return dict(id=None, status='idle', phase='idle', goal='', current_step=0,
                    reason='', history=[], last_decision=None, started_at=None,
                    updated_at=time.time(), retry_count=0, radar_run=None, seq=1,
                    executed=[], pending_observation=False, pending_command=None)

    @property
    def active(self):
        return self.data['status'] in self.ACTIVE

    def snapshot(self):
        private = {'radar_run', 'seq', 'executed', 'pending_observation', 'pending_command'}
        return copy.deepcopy({k: v for k, v in self.data.items() if k not in private})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data['updated_at'] = time.time()
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def publish(self):
        self.save()
        self.emit('autonomy', autonomy=self.snapshot())

    def note(self, kind, text, say=None):
        event = dict(at=time.time(), kind=kind, text=str(text)[:1500])
        if say:
            event['say'] = str(say)[:600]
        self.data['history'] = (self.data['history'] + [event])[-80:]
        self.data['reason'] = event['text']
        self.publish()
        log = self.path.with_suffix('.jsonl')
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(log.with_suffix('.previous.jsonl'))
        with log.open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(event, task_id=self.data['id']), ensure_ascii=False)+'\n')
        log.chmod(0o600)

    def launch(self):
        if self.active and (not self.worker or self.worker.done()):
            self.generation += 1
            self.worker = asyncio.create_task(self.run(self.generation))

    def start(self, goal):
        goal = goal.strip()
        if not goal:
            raise ValueError('Napiš úkol.')
        if self.active or (self.worker and not self.worker.done()):
            raise ValueError('Úkol už běží nebo se ještě zastavuje.')
        self.data = self.empty()
        self.data.update(id=secrets.token_hex(8), goal=goal, status='running',
                         phase='starting', started_at=time.time())
        self.owner = secrets.token_urlsafe(32)
        self.discard_observation = False
        self.note('start', 'Úkol je uložený na serveru. Pokračuje i se zavřeným kokpitem.')
        self.launch()
        return self.snapshot()

    def interrupt(self, reason, status='cancelled'):
        if not self.active and self.data['status'] != 'paused':
            return
        self.generation += 1
        self.data.update(status=status, phase=status)
        self.note(status, reason)
        if self.worker and self.worker is not asyncio.current_task() and not self.worker.done():
            self.worker.cancel()

    async def resume(self):
        if self.data['status'] != 'paused':
            raise ValueError('Není tu pozastavený úkol.')
        generation = self.generation
        await self.join()
        if self.generation != generation or self.data['status'] != 'paused':
            raise ValueError('Úkol se mezitím změnil nebo byl zastaven.')
        self.data.update(status='reconnecting', phase='reconnecting')
        self.discard_observation = True
        self.note('resume', 'Navazuji na uložený úkol; nejdřív se znovu rozhlédnu.')
        self.launch()
        return self.snapshot()

    def connection_lost(self, reason):
        if not self.active:
            return
        self.link_generation += 1
        self.discard_observation = True
        self.data.update(status='reconnecting', phase='reconnecting')
        self.note('connection', reason)

    async def join(self):
        if self.worker and self.worker is not asyncio.current_task():
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    async def shutdown(self):
        if self.active:
            self.data.update(status='reconnecting', phase='reconnecting')
            self.note('shutdown', 'Server se vypíná. Cíl zůstává uložený pro obnovení.')
            self.generation += 1
            if self.worker:
                self.worker.cancel()
        await self.join()

    def valid(self, generation):
        return generation == self.generation and self.active

    @staticmethod
    def fresh(view):
        t = view.get('telemetry') or {}
        age = t.get('telemetry_age_ms')
        return t.get('connected') is True and type(age) in (int, float) and 0 <= age <= 500

    async def keep_alive(self, generation):
        while self.valid(generation):
            self.heartbeat()
            await asyncio.sleep(.3)

    async def command(self, generation, epoch, action, speed=0, clearance=0, tilt=72, duration=300, chained=False):
        if not self.valid(generation):
            raise asyncio.CancelledError
        moving = action not in ('stop', 'tilt', 'light_on', 'light_off')
        if moving:
            self.data['pending_command'] = dict(action=action, speed=speed, at=time.time())
            self.save()  # Record intent before I/O; restart must not replay it.
        try:
            result = await self.drive(action, speed, clearance, tilt, duration, epoch)
        except MotionRefused:
            self.data['pending_command'] = None
            self.save()
            raise
        except BaseException:
            if moving:
                self.note('uncertain', f'Výsledek povelu {action} není potvrzený. Neopakuji ho naslepo.')
                self.data['pending_command'] = None
                self.save()
            raise
        if moving:
            last = self.data['executed'][-1] if self.data['executed'] else None
            if chained and last and last[0] == speed and last[2] == action:
                last[1] += 1
            else:
                self.data['executed'].append([speed, 1, action])
            self.data['pending_command'] = None
            self.note('motion', f'Potvrzen povel {action}, PWM {speed}, {duration} ms.')
        return result

    async def ask(self, generation):
        body = dict(run=self.data['radar_run'], seq=self.data['seq'],
                    executed=copy.deepcopy(self.data['executed']))
        self.data['pending_observation'] = True
        self.save()
        while self.valid(generation):
            answer = await self.radar('step', body)
            if not self.valid(generation):
                raise asyncio.CancelledError
            if answer.get('pending'):
                await self.sleep(1.5)
                continue
            self.data['executed'] = self.data['executed'][len(body['executed']):]
            self.data['seq'] += 1
            self.data['pending_observation'] = False
            self.save()
            return answer
        raise asyncio.CancelledError

    async def run(self, generation):
        self.keeper = asyncio.create_task(self.keep_alive(generation))
        retry = 0
        needs_tilt = True
        try:
            while self.valid(generation):
                try:
                    link = self.link_generation
                    view = await self.inspect()
                    if not self.fresh(view):
                        raise ConnectionLost('Čekám na čerstvou telemetrii auta.')
                    if not view.get('owned') or not view.get('armed') or view.get('estop'):
                        await self.acquire()
                        if not self.valid(generation):
                            raise asyncio.CancelledError
                        view = await self.inspect()
                    if not self.fresh(view) or not view.get('owned') or not view.get('armed'):
                        raise ConnectionLost('Čekám na převzetí řízení auta.')
                    epoch = view['epoch']
                    if needs_tilt:
                        await self.command(generation, epoch, 'tilt')
                        needs_tilt = False
                        await self.sleep(.9)
                    await self.command(generation, epoch, 'light_on')
                    if not self.data['radar_run']:
                        self.data['phase'] = 'starting'
                        self.publish()
                        started = await self.radar('start', dict(goal=self.data['goal']))
                        if not self.valid(generation):
                            raise asyncio.CancelledError
                        self.data.update(radar_run=started['run'], seq=1, executed=[], pending_observation=False)
                        self.note('target', started.get('say') or 'Cíl připraven.', started.get('say'))
                    self.data['phase'] = 'observing'
                    self.publish()
                    decision = await self.ask(generation)
                    current = await self.inspect()
                    if link != self.link_generation or current['epoch'] != epoch or current.get('estop') or not current.get('owned') or not current.get('armed') or not self.fresh(current):
                        raise ConnectionLost('Spojení se během pozorování změnilo. Pořídím nový pohled.')
                    if self.discard_observation:
                        # Complete the old sequence for exactly-once map feedback, but discard its commands.
                        self.discard_observation = False
                        self.note('reobserve', 'Spojení se obnovilo; starý návrh zahazuji a snímám znovu.')
                        continue
                    if not self.valid(generation):
                        raise asyncio.CancelledError
                    retry = 0
                    self.data.update(status='running', phase='acting', current_step=decision.get('step', self.data['current_step']+1), last_decision=decision)
                    self.note('decision', decision.get('reason', ''), decision.get('say'))
                    if decision.get('done'):
                        self.data.update(status='completed', phase='completed')
                        self.publish()
                        break
                    if decision.get('retilt'):
                        await self.command(generation, epoch, 'tilt', tilt=decision.get('next_tilt', 72))
                        await self.sleep(.9)
                        continue
                    if decision.get('next_tilt') and decision.get('next_tilt') != decision.get('tilt'):
                        await self.command(generation, epoch, 'tilt', tilt=decision['next_tilt'])
                        await self.sleep(.9)
                    for speed, count, action in decision.get('actions', []):
                        if type(count) is not int or not 0 <= count <= 12:
                            raise ValueError('Neplatný počet pohybů radaru.')
                        move = 'forward' if action == 'climb' else action
                        for i in range(count):
                            if link != self.link_generation:
                                raise ConnectionLost('Spojení se změnilo; zbývající pohyby zahazuji.')
                            # Keep the original firmware pulse lengths. Server guard checks every pulse.
                            clearance = max(0, (decision.get('clearance_cm') or 0)-i*6) if move == 'forward' else 0
                            if move == 'forward' and i == 0:
                                before = await self.inspect()
                                cm = (before.get('telemetry') or {}).get('distance_cm')
                                if type(cm) in (int, float) and cm <= 35:
                                    self.note('obstacle', 'Přední čidlo hlásí překážku; znovu se rozhlédnu.')
                                    break
                            chain = move == 'forward' and count > 1
                            await self.command(generation, epoch, move, speed, clearance if clearance >= 40 else 0,
                                               duration=350 if chain else 300, chained=chain and i > 0)
                            await self.sleep(.12 if chain and i < count-1 else .65)
                except MotionRefused as e:
                    self.note('refused', f'{e} Znovu se rozhlédnu a zvolím další krok.')
                    await self.sleep(1)
                except RadarReset:
                    self.data.update(radar_run=None, seq=1, executed=[], pending_observation=False)
                    self.discard_observation = True
                    self.note('radar_reset', 'Radar se restartoval. Cíl a deník zůstávají; místní mapu začínám znovu.')
                    await self.sleep(1)
                except (ConnectionLost, OSError, TimeoutError) as e:
                    if not self.valid(generation):
                        break
                    retry += 1
                    self.discard_observation = True
                    needs_tilt = True
                    self.data.update(status='reconnecting', phase='reconnecting', retry_count=self.data['retry_count']+1)
                    self.note('retry', f'{e} Auto stojí, cíl zůstává uložený. Pokus o obnovu {retry}.')
                    await self.stop('Čekám na obnovení spojení s autem.')
                    await self.sleep(min(10, 2 ** min(retry-1, 4)))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.data.update(status='failed', phase='failed')
            self.note('failed', f'Úkol vyžaduje opravu: {type(e).__name__}: {str(e)[:200]}')
        finally:
            self.keeper.cancel()
            await asyncio.gather(self.keeper, return_exceptions=True)
            await self.stop('Autonomní běh stojí.')
