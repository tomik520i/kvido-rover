import base64
import io
import json
import time

from PIL import Image
from test_safety import a, c, decision
from server.prompt import response_schema


def corner_image():
    im=Image.new('RGB',(60,40),'blue')
    for x in range(30):
        for y in range(20):im.putpixel((x,y),(255,0,0))
    out=io.BytesIO();im.save(out,'JPEG',quality=100)
    return out.getvalue()


def test_rotation_and_simulation(monkeypatch):
    raw=corner_image()
    monkeypatch.setitem(a.settings,'simulation',False)
    monkeypatch.setitem(a.settings,'camera_rotate_180',True)
    with Image.open(io.BytesIO(a.oriented_car_jpeg(raw))) as im:
        assert im.getpixel((55,35))[0]>220
        assert im.getpixel((5,5))[2]>220
    monkeypatch.setitem(a.settings,'simulation',True)
    assert a.oriented_car_jpeg(raw)==raw


def test_vision_receives_rotated_car_frame(c,monkeypatch):
    monkeypatch.setitem(a.settings,'simulation',False)
    monkeypatch.setitem(a.settings,'camera_rotate_180',True)
    class Frame:content=corner_image()
    async def device(*args,**kw):return Frame()
    async def telemetry():return {'distance_cm':100,'telemetry_age_ms':0,'servo':90}
    async def infer(messages,schema=None):
        url=messages[-1]['content'][-1]['image_url']['url']
        with Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as im:
            assert im.getpixel((55,35))[0]>220
        return decision(action='stop')
    monkeypatch.setattr(a,'device',device)
    monkeypatch.setattr(a,'telemetry',telemetry)
    monkeypatch.setattr(a,'infer',infer)
    assert c.post('/api/turn',json={'intent':'observe'}).status_code==200


def test_light_never_overrides_stop_or_phone():
    t={'distance_cm':100,'telemetry_age_ms':0,'light_supported':True}
    for source,armed in [('phone',True),('car',False)]:
        assert a.movement_allowed(decision(action='light_on'),t,mode='ai',image_source=source,image_age=1,armed=armed)[0]=='stop'
    t['light_supported']=False
    assert a.movement_allowed(decision(action='light_on'),t,mode='ai',image_source='car',image_age=1,armed=True)[0]=='stop'


def test_light_schema_capability():
    nav={'floor_valid':False,'floor_angle':55,'floor_calibrated':False}
    for mode,source,supported in [('manual','car',True),('ai','phone',True),('ai','car',False)]:
        schema=response_schema(mode,source,nav,{'light_supported':supported,'distance_cm':5})
        assert 'light_on' not in schema['properties']['action']['enum']
    schema=response_schema('ai','car',nav,{'light_supported':True,'distance_cm':5})
    assert 'light_on' in schema['properties']['action']['enum']
    assert 'forward' not in schema['properties']['action']['enum']


def test_ai_can_light_without_driving_and_invalidates_floor(c,monkeypatch):
    async def infer(*args,**kwargs):return decision(action='light_on',hazard='unknown',traversable=False)
    monkeypatch.setattr(a,'infer',infer)
    c.post('/api/mode',json={'mode':'ai'});c.post('/api/arm',json={})
    a.floor_seen=time.monotonic()
    r=c.post('/api/turn',json={'intent':'act'})
    assert r.status_code==200,r.text
    assert r.json()['executed_action']=='light_on'
    assert a.sim['light_on'] is True and a.sim['motor']=='stop'
    assert not a.navigation()['floor_valid']
    c.post('/api/stop',json={})
    assert not a.sim['light_on']


def test_settings_rotation_persists(c,monkeypatch):
    monkeypatch.setitem(a.settings,'camera_rotate_180',False)
    r=c.post('/api/settings',json={'camera_rotate_180':True})
    assert r.status_code==200
    assert json.loads(a.CONFIG_PATH.read_text(encoding='utf-8'))['camera_rotate_180'] is True
    assert c.get('/api/settings').json()['camera_rotate_180'] is True
