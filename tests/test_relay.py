import asyncio
from enum import IntEnum
import json
import time

import pytest

from server.relay import RelayHub, RelayTimeout, RelayUnavailable, RelayBusy


class CloseCode(IntEnum):
    ABNORMAL_CLOSURE = 1006


class WS:
    def __init__(self, headers=None):
        self.headers = headers if headers is not None else {'authorization': 'Bearer ' + 'x'*32}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def close(self, code):
        self.closed.append(code)
        await self.incoming.put({'type':'websocket.disconnect'})

    async def send_json(self, data):
        await self.outgoing.put(data)

    async def receive_json(self):
        return json.loads((await self.incoming.get())['text'])

    async def receive(self):
        return await self.incoming.get()

    async def send(self, data):
        await self.incoming.put({'type':'websocket.receive', 'text':json.dumps(data)})


async def connect(hub, ws=None):
    ws = ws or WS()
    await ws.send({'type':'hello','protocol':1,'device_id':hub.device_id,'boot_id':'boot'})
    task = asyncio.create_task(hub.websocket_handler(ws))
    welcome = await asyncio.wait_for(ws.outgoing.get(), 1)
    return ws, task, welcome['session']


def test_auth_origin_and_identity():
    async def run():
        hub = RelayHub('x'*32)
        for headers in [{}, {'authorization':'Bearer wrong'},
                        {'authorization':'Bearer '+'x'*32,'origin':'https://rover.example.com'}]:
            ws = WS(headers)
            await hub.websocket_handler(ws)
            assert not ws.accepted and 1008 in ws.closed
        ws = WS()
        await ws.send({'type':'hello','protocol':1,'device_id':'intruder','boot_id':'x'})
        await hub.websocket_handler(ws)
        assert not hub.status()['connected'] and 1008 in ws.closed
    asyncio.run(run())


@pytest.mark.parametrize('received,expected',[(CloseCode.ABNORMAL_CLOSURE,1006),(True,None),(False,None),(1000,1000)])
def test_disconnect_diagnostics_normalizes_intenum_without_accepting_bool(received,expected):
    async def run():
        hub = RelayHub('x'*32)
        ws,task,_ = await connect(hub)
        await ws.incoming.put({'type':'websocket.disconnect','code':received})
        await task
        diagnostic = hub.status()['last_disconnect']
        assert diagnostic['reason'] == 'remote_close'
        assert diagnostic['close_code'] == expected
        assert diagnostic['close_code'] is None or type(diagnostic['close_code']) is int
        assert json.loads(json.dumps(diagnostic))['close_code'] == expected
    asyncio.run(run())


def test_concurrent_requests_and_fresh_captures():
    async def run():
        hub = RelayHub('x'*32)
        ws, task, session = await connect(hub)
        a = asyncio.create_task(hub.control({'action':'stop'}))
        b = asyncio.create_task(hub.capture())
        messages = [await ws.outgoing.get(), await ws.outgoing.get()]
        command = next(x for x in messages if x['type']=='command')
        capture = next(x for x in messages if x['type']=='capture')
        assert command['id'] != capture['id']
        assert command['expires_at_ms'] - command['issued_at_ms'] <= 1000
        await ws.send({'type':'ack','session':session,'id':command['id'],'status':200})
        jpeg = b'\xff\xd8test\xff\xd9'
        await ws.incoming.put({'type':'websocket.receive','bytes':b'KVF1'+capture['id'].to_bytes(4,'big')+jpeg})
        assert await a == (200, {})
        assert await b == jpeg
        second = asyncio.create_task(hub.capture())
        request = await ws.outgoing.get()
        assert request['type'] == 'capture' and request['id'] != capture['id']
        await ws.close(1000)
        with pytest.raises(RelayUnavailable):
            await second
        await task
    asyncio.run(run())


def test_disconnect_session_replay_and_stale_telemetry():
    async def run():
        transitions = []
        hub = RelayHub('x'*32, on_link_change=transitions.append)
        ws, task, session = await connect(hub)
        await ws.send({'type':'telemetry','session':session,'sampled_at_ms':int(time.time()*1000),
                       'data':{'telemetry_age_ms':800,'distance_cm':90}})
        await asyncio.sleep(.02)
        assert hub.telemetry()['telemetry_age_ms'] >= 800
        assert not hub.status()['telemetry_fresh']
        ws2 = WS()
        await ws2.send({'type':'hello','protocol':1,'device_id':hub.device_id,'boot_id':'other'})
        await hub.websocket_handler(ws2)
        assert hub._connection['ws'] is ws
        await ws.close(1000)
        await task
        assert hub.telemetry()['telemetry_age_ms'] == 999999
        ws3, task3, session3 = await connect(hub)
        assert session3 != session
        await ws3.send({'type':'ack','session':session,'id':1,'status':200})
        await task3
        assert not hub.status()['connected']
        assert transitions == [True,False,True,False]
    asyncio.run(run())


def test_expiry_does_not_replay_or_accept_old_frame():
    async def run():
        hub = RelayHub('x'*32)
        hub.REQUEST_TIMEOUT = .03
        ws, task, session = await connect(hub)
        first = asyncio.create_task(hub.capture())
        request = await ws.outgoing.get()
        with pytest.raises(RelayTimeout):
            await first
        assert not hub._connection['pending']
        await ws.incoming.put({'type':'websocket.receive','bytes':b'KVF1'+request['id'].to_bytes(4,'big')+b'\xff\xd8test\xff\xd9'})
        second = asyncio.create_task(hub.capture())
        await ws.outgoing.get()
        with pytest.raises(RelayTimeout):
            await second
        await hub.shutdown()
        await task
    asyncio.run(run())


def test_heartbeat_timeout_and_invalid_motion():
    async def run():
        hub = RelayHub('x'*32)
        hub.HEARTBEAT_INTERVAL = .01
        hub.HEARTBEAT_TIMEOUT = .04
        ws, task, _ = await connect(hub)
        assert (await hub.control({'action':'forward','duration_ms':2001}))[0] == 400
        assert (await hub.control({'action':'forward','speed':True}))[0] == 400
        await asyncio.wait_for(task, .2)
        assert not hub.status()['connected']
        assert hub.status()['last_disconnect']['reason'] in ('heartbeat_pong_timeout','receive_timeout')
    asyncio.run(run())


def test_release_and_capture_error_ack():
    async def run():
        hub = RelayHub('x'*32)
        ws, task, session = await connect(hub)
        release = asyncio.create_task(hub.control({'action':'release'}))
        command = await ws.outgoing.get()
        assert command['action'] == 'release'
        await ws.send({'type':'ack','session':session,'id':command['id'],'status':200})
        assert await release == (200,{})
        capture = asyncio.create_task(hub.capture())
        request = await ws.outgoing.get()
        await ws.send({'type':'ack','session':session,'id':request['id'],'status':503,'error':'camera unavailable'})
        with pytest.raises(RelayUnavailable, match='camera unavailable'):
            await asyncio.wait_for(capture, .1)
        await hub.shutdown()
        await task
    asyncio.run(run())


def test_bounded_concurrency_and_single_capture():
    async def run():
        hub = RelayHub('x'*32)
        ws, task, _ = await connect(hub)
        requests = [asyncio.create_task(hub.capture())]
        await ws.outgoing.get()
        with pytest.raises(RelayBusy):
            await hub.capture()
        for _ in range(hub.MAX_PENDING-1):
            requests.append(asyncio.create_task(hub.control({'action':'stop'})))
            await ws.outgoing.get()
        with pytest.raises(RelayBusy):
            await hub.control({'action':'stop'})
        assert len(hub._connection['pending']) == hub.MAX_PENDING
        await hub.shutdown()
        results = await asyncio.gather(*requests, return_exceptions=True)
        assert all(isinstance(result, RelayUnavailable) for result in results)
        await task
    asyncio.run(run())


def test_failed_ping_drops_connection_despite_telemetry():
    class FailedPingWS(WS):
        async def send_json(self, data):
            if data['type'] == 'ping':
                raise OSError('Broken outbound transport')
            await super().send_json(data)

    async def run():
        hub = RelayHub('x'*32)
        hub.HEARTBEAT_INTERVAL = .01
        ws, task, session = await connect(hub, FailedPingWS())
        async def telemetry():
            while not ws.closed:
                await ws.send({'type':'telemetry','session':session,'sampled_at_ms':int(time.time()*1000),
                               'data':{'telemetry_age_ms':0}})
                await asyncio.sleep(.002)
        sender = asyncio.create_task(telemetry())
        await asyncio.wait_for(task, .2)
        await sender
        assert not hub.status()['connected'] and 1001 in ws.closed
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['command','capture'])
def test_response_deadline_checked_before_resolving_future(kind):
    async def run():
        hub = RelayHub('x'*32)
        ws, task, session = await connect(hub)
        request = asyncio.create_task(hub.capture() if kind == 'capture' else hub.control({'action':'stop'}))
        message = await ws.outgoing.get()
        c = hub._connection
        pending_kind, future, _ = c['pending'][message['id']]
        # Simulate an expired request whose timeout coroutine has not run yet.
        c['pending'][message['id']] = (pending_kind, future, time.monotonic()-1)
        if kind == 'capture':
            await ws.incoming.put({'type':'websocket.receive','bytes':b'KVF1'+message['id'].to_bytes(4,'big')+b'\xff\xd8test\xff\xd9'})
        else:
            await ws.send({'type':'ack','session':session,'id':message['id'],'status':200})
        with pytest.raises(RelayTimeout):
            await request
        await hub.shutdown()
        await task
    asyncio.run(run())


def test_send_lock_wait_uses_request_deadline():
    async def run():
        hub = RelayHub('x'*32)
        hub.REQUEST_TIMEOUT = .03
        ws, task, _ = await connect(hub)
        lock = hub._connection['send_lock']
        await lock.acquire()
        try:
            with pytest.raises(RelayTimeout):
                await asyncio.wait_for(hub.control({'action':'stop'}), .1)
            assert ws.outgoing.empty()
            assert not hub._connection['pending']
        finally:
            lock.release()
            await hub.shutdown()
            await task
    asyncio.run(run())


def test_command_send_failure_immediately_drops_link_outside_lock():
    class FailedCommandWS(WS):
        async def send_json(self, data):
            if data['type'] == 'command':
                raise OSError('Broken send')
            await super().send_json(data)
    async def run():
        transitions = []
        hub = RelayHub('x'*32)
        ws, task, _ = await connect(hub, FailedCommandWS())
        c = hub._connection
        async def callback(connected):
            transitions.append(connected)
            assert not c['send_lock'].locked()
        hub.on_link_change = callback
        with pytest.raises(RelayUnavailable, match='send failed'):
            await hub.control({'action':'stop'})
        assert transitions == [False]
        assert not hub.status()['connected'] and not c['pending']
        assert hub.status()['last_disconnect']['reason'] == 'send_error'
        await task
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['remote','protocol','send'])
def test_disconnect_diagnostics_are_sanitized(caplog, failure):
    secret = 'NEVER_LOG_SECRET_PAYLOAD'
    class FailedSendWS(WS):
        async def send_json(self, data):
            if data['type'] == 'command':
                raise OSError(secret)
            await super().send_json(data)
    async def run():
        hub = RelayHub('x'*32)
        assert hub.status()['last_disconnect'] is None
        ws, task, session = await connect(hub, FailedSendWS() if failure == 'send' else WS())
        if failure == 'remote':
            await ws.incoming.put({'type':'websocket.disconnect','code':1006,'reason':secret})
            expected, code = 'remote_close', 1006
        elif failure == 'protocol':
            await ws.send({'type':'telemetry','session':secret,'data':{'private':secret}})
            expected, code = 'protocol_error', 1008
        else:
            with pytest.raises(RelayUnavailable):
                await hub.control({'action':'stop'})
            expected, code = 'send_error', 1001
        await task
        diagnostic = hub.status()['last_disconnect']
        assert diagnostic['reason'] == expected and diagnostic['close_code'] == code
        assert type(diagnostic['at_ms']) is int and diagnostic['at_ms'] > 0
        assert f'reason={expected}' in caplog.text and f'close_code={code}' in caplog.text
        assert 'pong_age_ms=' in caplog.text and 'rx_age_ms=' in caplog.text and 'pending=' in caplog.text
        assert secret not in caplog.text and session not in caplog.text and hub.key not in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize('sample_offset,expected', [(-2000,2150),(0,150),(50,150),
                                                   (101,999999),(None,999999),('bad',999999)])
def test_telemetry_accounts_for_transit_and_invalid_sample(monkeypatch, sample_offset, expected):
    async def run():
        hub = RelayHub('x'*32)
        ws, task, session = await connect(hub)
        monkeypatch.setattr('server.relay.time.time', lambda: 2000000000.0)
        message = {'type':'telemetry','session':session,'data':{'telemetry_age_ms':50}}
        if sample_offset is not None:
            message['sampled_at_ms'] = 2000000000000+sample_offset if type(sample_offset) is int else sample_offset
        await ws.send(message)
        for _ in range(20):
            if hub._connection['telemetry'] is not None:
                break
            await asyncio.sleep(0)
        assert hub._connection['telemetry']['telemetry_age_ms'] == expected
        assert hub.status()['connected']
        assert hub.status()['telemetry_fresh'] == (expected <= 500)
        await hub.shutdown()
        await task
    asyncio.run(run())


def test_transport_accepts_full_pwm_and_rejects_out_of_range(monkeypatch):
    hub=RelayHub('x'*32,'test-device');sent=[]
    async def request(kind,payload):sent.append(payload);return 200,{'ok':True}
    monkeypatch.setattr(hub,'_request',request)
    assert asyncio.run(hub.control({'action':'forward','speed':255}))[0]==200
    assert sent[0]['speed']==255
    assert asyncio.run(hub.control({'action':'forward','speed':256}))[0]==400
    assert len(sent)==1


def test_camera_clearance_is_forwarded_only_when_valid():
    async def run():
        hub = RelayHub('x'*32)
        ws, task, session = await connect(hub)
        assert (await hub.control({'action': 'forward', 'clearance_cm': -1}))[0] == 400
        assert (await hub.control({'action': 'forward', 'clearance_cm': 300}))[0] == 400
        assert (await hub.control({'action': 'forward', 'clearance_cm': True}))[0] == 400
        sent = asyncio.create_task(hub.control({'action': 'forward', 'speed': 180, 'duration_ms': 300, 'clearance_cm': 80}))
        command = await ws.outgoing.get()
        assert command['action'] == 'forward' and command['clearance_cm'] == 80
        await ws.send({'type': 'ack', 'session': session, 'id': command['id'], 'status': 200})
        assert await sent == (200, {})
        plain = asyncio.create_task(hub.control({'action': 'backward'}))
        command = await ws.outgoing.get()
        assert 'clearance_cm' not in command
        await ws.send({'type': 'ack', 'session': session, 'id': command['id'], 'status': 200})
        assert await plain == (200, {})
    asyncio.run(run())
