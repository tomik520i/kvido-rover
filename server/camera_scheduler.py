"""One paced camera feed per car. Only previews may reuse a recent frame."""
import asyncio
import math
import time


class CameraScheduler:
    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.lock = asyncio.Lock()
        self.clock, self.sleep = clock, sleep
        self.interval = .5
        self.source = None
        self.frame = None
        self.completed_at = None

    def observe(self, telemetry):
        profile = telemetry.get('camera_profile') or {}
        value = profile.get('interval_ms')
        if type(value) in (int, float) and math.isfinite(value) and 500 <= value <= 2000:
            self.interval = value / 1000

    async def capture(self, source, fetch, *, preview=False):
        async with self.lock:
            if source != self.source:
                self.source, self.frame, self.completed_at = source, None, None
                self.interval = .5
            if (preview and self.frame is not None and self.completed_at is not None
                    and self.clock() - self.completed_at < self.interval):
                return self.frame
            if self.completed_at is not None:
                wait = self.interval - (self.clock() - self.completed_at)
                if wait > 0:
                    await self.sleep(wait)
            # The relay's short deadline starts inside fetch, after pacing.
            # AI always gets a new exposure; never a preview from before motion.
            try:
                frame = await fetch()
            except BaseException:
                self.frame = None
                raise
            finally:
                self.completed_at = self.clock()
            self.frame = frame
            return frame
