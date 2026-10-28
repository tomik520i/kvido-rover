"""Bounded, RAM-only visual context. Image similarity is not wheel odometry.

Call ``command`` only after a device has accepted the command. ``observe`` returns
copies of up to three chronological frames for a model prompt. A possible stall
is a cautious visual hint, never proof that a wheel command failed or succeeded.
"""
import base64
import binascii
from collections import deque
from copy import deepcopy
import io
import math
import time

from PIL import Image, ImageDraw, ImageFont


WHEEL_ACTIONS = frozenset({
    'forward', 'backward', 'left', 'right', 'forward_left', 'forward_right',
    'back_left', 'back_right', 'rotate_left', 'rotate_right',
})


def contact_sheet(frames):
    """Present chronological originals together; do not infer missing content."""
    frames=frames[-3:]
    sheet=Image.new('RGB',(320*len(frames),278),'white')
    draw=ImageDraw.Draw(sheet)
    try:font=ImageFont.truetype('DejaVuSans.ttf',18)
    except OSError:font=ImageFont.load_default()
    for index,frame in enumerate(frames):
        raw=base64.b64decode(frame['image'].split(',',1)[1],validate=True)
        with Image.open(io.BytesIO(raw)) as original:
            if original.width*original.height>4_000_000:raise ValueError('Frame too large')
            scaled=original.convert('RGB');scaled.thumbnail((320,240))
            x=index*320+(320-scaled.width)//2
            sheet.paste(scaled,(x,38+(240-scaled.height)//2))
        label=f'{index+1} / {len(frames)} - '+('CURRENT' if index==len(frames)-1 else 'OLDER')
        draw.text((index*320+8,8),label,fill='black',font=font)
        draw.line((index*320,0,index*320,278),fill='black',width=2)
    out=io.BytesIO();sheet.save(out,'JPEG',quality=90)
    return 'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()
VIEW_ACTIONS = frozenset({'tilt', 'light_on', 'light_off'})


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _thumbnail(data_url):
    try:
        if not isinstance(data_url, str) or len(data_url) > 8_000_000:
            return None
        header, encoded = data_url.split(',', 1)
        if header.lower() not in ('data:image/jpeg;base64', 'data:image/jpg;base64'):
            return None
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'JPEG' or image.width * image.height > 4_000_000:
                return None
            return image.convert('L').resize((64, 48), Image.Resampling.BILINEAR).tobytes()
    except (ValueError, TypeError, OSError, binascii.Error, Image.DecompressionBombError):
        return None


class VisionHistory:
    MAX_AGE_SECONDS = 120
    SIMILARITY_THRESHOLD = 3 / 255

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self.clear()

    def clear(self):
        self._frames = deque(maxlen=3)
        self._pending = deque(maxlen=8)
        self._source = None
        self._wheel_seen = False
        self._view_changed = False

    def command(self, action, speed, duration_ms, tilt):
        if action in ('stop', 'release', 'estop'):
            return
        self._pending.append({'action': action, 'speed': speed,
                              'duration_ms': duration_ms, 'tilt': tilt,
                              'at': self._clock()})
        # Preserve comparison guards even if a bounded command list overflows.
        self._view_changed |= action in VIEW_ACTIONS
        self._wheel_seen |= (action in WHEEL_ACTIONS and _number(speed) and speed > 0
                             and _number(duration_ms) and duration_ms > 0)

    def observe(self, image_data_url, telemetry, source):
        now = self._clock()
        if self._frames:
            gap = now - self._frames[-1]['frame']['at']
            if source != self._source or gap < 0 or gap > self.MAX_AGE_SECONDS:
                self.clear()
        self._source = source
        # Do not retain an old first frame just because recent frames are fresh.
        while self._frames and now - self._frames[0]['frame']['at'] > self.MAX_AGE_SECONDS:
            self._frames.popleft()
        t = telemetry if isinstance(telemetry, dict) else {}
        servo = t.get('servo')
        servo = servo if _number(servo) else None
        light = t.get('light_on')
        light = light if isinstance(light, bool) else None
        frame = {'image': image_data_url, 'at': now, 'servo': servo,
                 'light_on': light,
                 'commands_since_previous': list(self._pending) if self._frames else []}
        gray = _thumbnail(image_data_url)
        previous = self._frames[-1] if self._frames else None
        observation = {'status': 'not_comparable', 'reason': 'no_previous_frame',
                       'movement_confirmed': False}
        if previous:
            old = previous['frame']
            if gray is None or previous['gray'] is None:
                observation['reason'] = 'invalid_image'
            elif servo is None or old['servo'] is None:
                observation['reason'] = 'unknown_camera_angle'
            elif abs(servo - old['servo']) > 3:
                observation['reason'] = 'camera_angle_changed'
            elif light is None or old['light_on'] is None:
                observation['reason'] = 'unknown_light_state'
            elif light != old['light_on'] or self._view_changed:
                observation['reason'] = 'camera_or_light_command'
            elif not self._wheel_seen:
                observation.update(status='no_wheel_command', reason='no_wheel_command')
            else:
                difference = sum(abs(a - b) for a, b in zip(gray, previous['gray'])) / (64 * 48 * 255)
                similar = difference <= self.SIMILARITY_THRESHOLD
                observation.update(
                    status='possible_stall' if similar else 'changed',
                    reason='similar_images_after_wheel_command' if similar else 'visual_change_not_odometry',
                    mean_absolute_difference=difference,
                    similarity_threshold=self.SIMILARITY_THRESHOLD,
                    previous_at=old['at'],
                )
        frame['motion_observation'] = observation
        self._frames.append({'frame': frame, 'gray': gray})
        self._pending.clear()
        self._wheel_seen = self._view_changed = False
        return deepcopy([entry['frame'] for entry in self._frames])
