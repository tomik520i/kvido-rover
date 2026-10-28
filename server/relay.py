"""Single-device outbound relay. Link callbacks are awaited outside internal locks.

``on_link_change(connected: bool)`` runs on both connection and disconnection;
callers should invalidate operator leases on either transition. No request survives
a connection, and telemetry age includes both the device's age and network age.
The callback must only invalidate local state: it must not wait for relay network
requests, because connection setup awaits it before starting the receive loop.
"""
import asyncio
import inspect
import json
import logging
import math
import secrets
import time

logger = logging.getLogger(__name__)


class RelayUnavailable(RuntimeError):
    pass


class RelayTimeout(RelayUnavailable):
    pass


class RelayBusy(RelayUnavailable):
    pass


class RelayHub:
    HEARTBEAT_INTERVAL = 1.0
    HEARTBEAT_TIMEOUT = 3.0
    REQUEST_TIMEOUT = 0.9
    MAX_FRAME = 160 * 1024
    MAX_PENDING = 4

    def __init__(self, key, device_id='kvido-e465b8788f40', on_link_change=None):
        if not isinstance(key, str) or len(key) < 24:
            raise ValueError('A separate random relay key of at least 24 characters is required')
        self.key, self.device_id = key, device_id
        self.on_link_change = on_link_change
        self._connection = None
        self._next_id = secrets.randbits(32)
        self._closed = False
        self._last_disconnect = None

    async def _notify(self, connected):
        if self.on_link_change:
            result = self.on_link_change(connected)
            if inspect.isawaitable(result):
                await result

    def status(self):
        c = self._connection
        age = self.telemetry().get('telemetry_age_ms', 999999)
        return {'connected': c is not None, 'device_id': self.device_id,
                'telemetry_age_ms': age, 'telemetry_fresh': c is not None and age <= 500,
                'last_disconnect': dict(self._last_disconnect) if self._last_disconnect else None}

    def telemetry(self):
        c = self._connection
        if c is None or c['telemetry'] is None:
            return {'connected': c is not None, 'telemetry_age_ms': 999999}
        data = dict(c['telemetry'])
        data['telemetry_age_ms'] += max(0, int((time.monotonic() - c['received']) * 1000))
        data['connected'] = True
        return data

    async def _send(self, c, message, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + self.REQUEST_TIMEOUT
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayTimeout('Device request expired before send')
        try:
            await asyncio.wait_for(c['send_lock'].acquire(), remaining)
        except asyncio.TimeoutError as exc:
            raise RelayTimeout('Device request expired waiting to send') from exc
        failure = None
        try:
            if self._connection is not c:
                raise RelayUnavailable('Device disconnected')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RelayTimeout('Device request expired before send')
            await asyncio.wait_for(c['ws'].send_json(message), remaining)
        except RelayUnavailable:
            raise
        except asyncio.TimeoutError as exc:
            failure = RelayTimeout('Device send expired')
            failure.__cause__ = exc
        except Exception as exc:
            failure = RelayUnavailable('Device send failed')
            failure.__cause__ = exc
        finally:
            c['send_lock'].release()
        if failure:
            await self._drop(c, 'send_timeout' if isinstance(failure, RelayTimeout) else 'send_error', 1001)
            try:
                await c['ws'].close(code=1001)
            except Exception:
                pass
            raise failure

    @staticmethod
    def _pending(c, ident):
        pending = c['pending'].get(ident)
        if pending and not pending[1].done():
            if time.monotonic() >= pending[2]:
                pending[1].set_exception(RelayTimeout('Device response arrived after deadline'))
            else:
                return pending
        return None

    async def _request(self, kind, payload):
        c = self._connection
        if c is None:
            raise RelayUnavailable('Device disconnected')
        if len(c['pending']) >= self.MAX_PENDING:
            raise RelayBusy('Device request capacity reached')
        if kind == 'capture' and any(item[0] == 'capture' for item in c['pending'].values()):
            raise RelayBusy('Camera request already in progress')
        if self._next_id >= 0xffffffff:
            await self._drop(c, 'request_counter_exhausted', 1001)
            await c['ws'].close(code=1001)
            raise RelayUnavailable('Request counter exhausted; reconnect required')
        self._next_id += 1
        ident = self._next_id
        future = asyncio.get_running_loop().create_future()
        now = int(time.time() * 1000)
        deadline = time.monotonic() + self.REQUEST_TIMEOUT
        c['pending'][ident] = (kind, future, deadline)
        try:
            await self._send(c, {**payload, 'type': kind, 'session': c['session'],
                                 'id': ident, 'issued_at_ms': now,
                                 'expires_at_ms': now + int(self.REQUEST_TIMEOUT * 1000)}, deadline)
            return await asyncio.wait_for(future, max(0, deadline - time.monotonic()))
        except asyncio.TimeoutError as exc:
            raise RelayTimeout('Device request expired') from exc
        finally:
            c['pending'].pop(ident, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A send failure can drop the connection before this coroutine
                # starts awaiting its pending future. Consume that exception too.
                future.exception()

    async def control(self, payload):
        allowed = {'stop','estop','release','forward','backward','left','right',
                   'forward_left','forward_right','back_left','back_right',
                   'rotate_left','rotate_right','tilt','light_on','light_off'}
        action = payload.get('action')
        if action not in allowed:
            return 400, {'error': 'Invalid action'}
        values = {'speed': payload.get('speed', 130), 'duration_ms': payload.get('duration_ms', 250),
                  'tilt': payload.get('tilt', 90)}
        for name, low, high in [('speed',0,255),('duration_ms',0,2000),('tilt',35,145)]:
            if type(values[name]) is not int or not low <= values[name] <= high:
                return 400, {'error': 'Invalid ' + name}
        clearance = payload.get('clearance_cm', 0)
        if type(clearance) is not int or not 0 <= clearance <= 254:
            return 400, {'error': 'Invalid clearance_cm'}
        if clearance:
            values['clearance_cm'] = clearance
        return await self._request('command', {'action': action, **values})

    async def capture(self):
        return await self._request('capture', {})

    async def _heartbeat(self, c):
        reason = 'heartbeat_pong_timeout'
        try:
            while self._connection is c:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if time.monotonic() - c['pong'] >= self.HEARTBEAT_TIMEOUT:
                    break
                await self._send(c, {'type': 'ping', 'session': c['session']})
        except asyncio.CancelledError:
            reason = 'heartbeat_cancelled'
            raise
        except Exception:
            reason = 'heartbeat_send_error'
        finally:
            if self._connection is c:
                await self._drop(c, reason, 1001)
                await c['ws'].close(code=1001)

    async def _drop(self, c, reason='handler_exit', close_code=1000):
        if self._connection is c:
            self._connection = None
            now = time.monotonic()
            close_code = int(close_code) if isinstance(close_code,int) and not isinstance(close_code,bool) and 1000 <= close_code <= 4999 else None
            self._last_disconnect = {'reason': reason, 'at_ms': int(time.time()*1000), 'close_code': close_code}
            logger.warning('Relay disconnected reason=%s close_code=%s uptime_ms=%d pong_age_ms=%d rx_age_ms=%d pending=%d',
                           reason, close_code, max(0,int((now-c['connected_at'])*1000)),
                           max(0,int((now-c['pong'])*1000)), max(0,int((now-c['last_rx'])*1000)), len(c['pending']))
            for _, future, _ in c['pending'].values():
                if not future.done():
                    future.set_exception(RelayUnavailable('Device disconnected'))
            c['pending'].clear()
            await self._notify(False)

    async def websocket_handler(self, ws):
        headers = ws.headers
        auth = headers.get('authorization', '')
        if ('origin' in headers or not secrets.compare_digest(auth, 'Bearer ' + self.key)
                or self._closed):
            await ws.close(code=1008)
            return
        await ws.accept()
        c = None
        heartbeat = None
        reason, close_code = 'handler_exit', 1000
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 3)
            if (not isinstance(hello, dict) or hello.get('type') != 'hello'
                    or type(hello.get('protocol')) is not int or hello['protocol'] != 1
                    or hello.get('device_id') != self.device_id
                    or not isinstance(hello.get('boot_id'), str) or not 1 <= len(hello['boot_id']) <= 128
                    or self._connection is not None or self._closed):
                await ws.close(code=1008)
                return
            c = {'ws': ws, 'session': secrets.token_urlsafe(24), 'pending': {},
                 'telemetry': None, 'received': 0, 'pong': time.monotonic(),
                 'connected_at': time.monotonic(), 'last_rx': time.monotonic(),
                 'send_lock': asyncio.Lock()}
            self._connection = c
            self._next_id = 0
            await self._notify(True)
            await self._send(c, {'type':'welcome', 'session':c['session'],
                                 'server_time_ms':int(time.time()*1000)})
            heartbeat = asyncio.create_task(self._heartbeat(c))
            while self._connection is c:
                reason = 'receive_timeout'
                packet = await asyncio.wait_for(ws.receive(), self.HEARTBEAT_TIMEOUT)
                c['last_rx'] = time.monotonic()
                if packet['type'] == 'websocket.disconnect':
                    reason, close_code = 'remote_close', packet.get('code')
                    break
                reason = 'protocol_error'
                blob = packet.get('bytes')
                if blob is not None:
                    if len(blob) > self.MAX_FRAME or len(blob) < 12 or blob[:4] != b'KVF1':
                        raise ValueError('Invalid camera frame')
                    ident = int.from_bytes(blob[4:8], 'big')
                    pending = self._pending(c, ident)
                    if pending and pending[0] == 'capture' and not pending[1].done():
                        jpeg = blob[8:]
                        if jpeg[:2] != b'\xff\xd8' or jpeg[-2:] != b'\xff\xd9':
                            raise ValueError('Invalid JPEG')
                        pending[1].set_result(jpeg)
                    continue
                raw = packet.get('text', '')
                if len(raw) > 16384:
                    raise ValueError('Oversized message')
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get('session') != c['session']:
                    raise ValueError('Invalid session')
                kind = message.get('type')
                if kind == 'pong':
                    c['pong'] = time.monotonic()
                elif kind == 'telemetry':
                    data = message.get('data')
                    age = data.get('telemetry_age_ms') if isinstance(data, dict) else None
                    if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                        raise ValueError('Invalid telemetry age')
                    sampled = message.get('sampled_at_ms')
                    now_ms = int(time.time() * 1000)
                    data = dict(data)
                    if type(sampled) is not int or sampled <= 0 or sampled > now_ms + 100:
                        data['telemetry_age_ms'] = 999999
                    else:
                        data['telemetry_age_ms'] += max(0, now_ms - sampled) + 100
                    c['telemetry'], c['received'] = data, time.monotonic()
                elif kind == 'ack':
                    ident, status = message.get('id'), message.get('status')
                    if type(ident) is not int or not 0 <= ident <= 0xffffffff or type(status) is not int or not 100 <= status <= 599:
                        raise ValueError('Invalid acknowledgement')
                    pending = self._pending(c, ident)
                    if pending and not pending[1].done():
                        if pending[0] == 'command':
                            pending[1].set_result((status, {k:v for k,v in message.items() if k not in ('type','session','id','status')}))
                        elif status >= 400:
                            pending[1].set_exception(RelayUnavailable(str(message.get('error', 'Camera request failed'))[:300]))
        except asyncio.CancelledError:
            reason, close_code = 'handler_cancelled', 1001
        except asyncio.TimeoutError:
            reason, close_code = 'receive_timeout', 1001
        except (ValueError, TypeError, KeyError):
            reason, close_code = 'protocol_error', 1008
        except Exception:
            reason, close_code = 'receive_error', 1011
        finally:
            if c is not None and self._connection is c:
                await self._drop(c, reason, close_code)
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await ws.close(code=close_code if type(close_code) is int and close_code not in (1005,1006,1015) else 1000)
            except Exception:
                pass

    async def shutdown(self):
        self._closed = True
        c = self._connection
        if c:
            await self._drop(c, 'shutdown', 1001)
            await c['ws'].close(code=1001)
