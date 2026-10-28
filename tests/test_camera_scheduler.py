import asyncio
import pytest
from functools import wraps
from server.camera_scheduler import CameraScheduler

def async_test(fn):
    @wraps(fn)
    def run():return asyncio.run(fn())
    return run

@async_test
async def test_viewers_share_frame_but_ai_after_motion_waits_for_new_exposure():
    now=[0.];calls=[]
    async def sleep(delay):now[0]+=delay
    async def fetch():calls.append(now[0]);return len(calls)
    camera=CameraScheduler(clock=lambda:now[0],sleep=sleep)
    assert await camera.capture('car',fetch,preview=True)==1
    assert await camera.capture('car',fetch,preview=True)==1
    assert await camera.capture('car',fetch)==2
    assert calls==[0,.5]
    camera.observe({'camera_profile':{'interval_ms':1000}})
    assert await camera.capture('car',fetch)==3
    assert calls==[0,.5,1.5]


@async_test
async def test_simultaneous_viewers_make_one_request_and_source_switch_drops_cache():
    camera=CameraScheduler();calls=[]
    async def fetch():calls.append(1);await asyncio.sleep(.01);return len(calls)
    assert await asyncio.gather(*(camera.capture('a',fetch,preview=True) for _ in range(5)))==[1]*5
    assert await camera.capture('b',fetch,preview=True)==2


@async_test
async def test_failed_fetch_never_returns_stale_cached_picture():
    now=[0.]
    async def sleep(delay):now[0]+=delay
    async def good():return 'old'
    async def bad():raise ConnectionError()
    camera=CameraScheduler(clock=lambda:now[0],sleep=sleep)
    await camera.capture('a',good,preview=True)
    with pytest.raises(ConnectionError):await camera.capture('a',bad)
    assert camera.frame is None
    camera.observe({'camera_profile':{'interval_ms':float('nan')}})
    assert camera.interval==.5
