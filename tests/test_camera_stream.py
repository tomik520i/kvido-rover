"""The live panel must not depend on the ESP's single MJPEG socket."""
import asyncio
from types import SimpleNamespace

import pytest
from test_safety import a


@pytest.mark.parametrize('transport', ['lan', 'relay'])
def test_stream_pulls_fresh_frames_and_stops_when_viewer_leaves(monkeypatch, transport):
    monkeypatch.setattr(a, 'settings', {**a.settings, 'simulation': False, 'transport': transport})
    monkeypatch.setattr(a, 'session', lambda request: 'test')
    calls = []

    async def capture(path, *, preview=False):
        assert preview is True
        calls.append(path)
        return SimpleNamespace(content=b'jpeg-' + str(len(calls)).encode())

    monkeypatch.setattr(a, 'device', capture)

    class Viewer:
        async def is_disconnected(self):
            return len(calls) >= 2

    async def check():
        response = await a.stream(Viewer())
        frames = [frame async for frame in response.body_iterator]
        assert calls == ['/capture', '/capture']
        assert b'jpeg-1' in frames[0] and b'jpeg-2' in frames[1]
        assert b'Content-Length: 6\r\n' in frames[0]

    asyncio.run(check())


def test_capture_failure_ends_stream_for_browser_retry(monkeypatch):
    monkeypatch.setattr(a, 'settings', {**a.settings, 'simulation': False, 'transport': 'lan'})
    monkeypatch.setattr(a, 'session', lambda request: 'test')
    errors = []
    monkeypatch.setattr(a, 'emit', lambda kind, **data: errors.append(kind))

    async def unavailable(path, *, preview=False):
        assert preview is True
        raise TimeoutError('device unavailable')

    monkeypatch.setattr(a, 'device', unavailable)

    class Viewer:
        async def is_disconnected(self):
            return False

    async def check():
        response = await a.stream(Viewer())
        assert [frame async for frame in response.body_iterator] == []
        assert errors == ['error']

    asyncio.run(check())
