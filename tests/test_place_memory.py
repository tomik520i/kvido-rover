import copy
import json

import cv2
import numpy as np
import pytest

from server import place_memory as m
from server.autopilot_core import ask_where


def images():
    result = {}
    for i in range(1, 10):
        img = np.random.default_rng(i).integers(0, 256, (240, 320), np.uint8)
        result[i] = cv2.imencode('.jpg', img)[1].tobytes()
    return result


def add(data, event='r:1:panorama', recognition=None):
    raw = images()
    entries = [dict(frame=i, target=False, note='Cíl nevidím.') for i in range(9)]
    annotation = dict(area_label='pravděpodobně kuchyň', area_confidence='medium',
                      landmarks=['myčka','linka'], uninspected=['pod stolem'], visibility='partial')
    m.record_panorama(data,event,entries,raw,annotation,recognition or dict(area_id=None,association='unknown_or_ambiguous'),1)
    return raw


def test_persistent_goal_specific_journal_and_no_private_images_in_api(tmp_path):
    path=m.journal_path(tmp_path,'Najdi misku'); data=m.load(path,'Najdi misku')
    add(data);m.save(path,data)
    restored=m.load(path,'Najdi misku')
    assert restored==data and restored['areas'][0]['anchors']
    assert m.journal_path(tmp_path,'Najdi kočku')!=path
    assert 'jpeg' not in json.dumps(m.public(data))
    assert m.public(data)['areas'][0]['room_fully_searched'] is False


def test_negative_observations_never_mean_entire_room_empty_and_retry_idempotent(tmp_path):
    data=m.load(tmp_path/'x','misku');add(data);add(data)
    assert len(data['areas'])==1 and data['areas'][0]['panoramas']==1
    assert data['areas'][0]['result']=='not_observed_in_panorama'
    m.observe(data,'r:2:view',None,2,72)
    assert data['recent']==[]
    m.observe(data,'r:2:view',dict(target_visible=False,confidence='low',note='tmavé'),2,72)
    m.observe(data,'r:2:view',dict(target_visible=True,confidence='high'),2,72)
    assert len(data['recent'])==1 and data['recent'][0]['result']=='not_observed_in_this_view'
    assert data['areas'][0]['room_fully_searched'] is False


def test_same_named_rooms_do_not_merge_without_image_evidence(tmp_path):
    data=m.load(tmp_path/'x','misku');add(data);add(data,'r:20:panorama')
    assert len(data['areas'])==2


def test_visual_revisit_needs_two_independent_views(tmp_path):
    data=m.load(tmp_path/'x','misku');raw=add(data)
    found=m.recognize(data,raw)
    assert found['area_id']==data['areas'][0]['id']
    assert m.recognize(data,{1:raw[1]})['area_id'] is None
    assert m.recognize(data,{1:raw[1],4:raw[1],7:raw[1]})['area_id'] is None
    add(data,'r:20:panorama',found)
    assert len(data['areas'])==1 and data['areas'][0]['panoramas']==2
    assert data['areas'][0]['room_fully_searched'] is False


def test_blank_and_ambiguous_views_do_not_merge(tmp_path):
    data=m.load(tmp_path/'x','misku');raw=add(data)
    blank=cv2.imencode('.jpg',np.zeros((240,320),np.uint8))[1].tobytes()
    assert m.recognize(data,{1:blank,4:blank})['area_id'] is None
    duplicate=copy.deepcopy(data['areas'][0]);duplicate['id']='other';data['areas'].append(duplicate)
    assert m.recognize(data,raw)['area_id'] is None


def test_journal_is_bounded_and_corrupt_file_preserved(tmp_path):
    data=m.load(tmp_path/'x','misku')
    for i in range(20):
        m.record_panorama(data,str(i),[],{},None,dict(area_id=None,association='unknown_or_ambiguous'),i)
    assert len(data['areas'])==m.MAX_AREAS
    path=tmp_path/'bad.json';path.write_text('{',encoding='utf-8')
    with pytest.raises(ValueError):m.load(path,'misku')
    assert path.read_text()=='{'


def test_route_model_really_receives_journal_without_contaminating_perception(tmp_path):
    data=m.load(tmp_path/'x','misku');raw=add(data);captured=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':json.dumps(dict(choice=2,reason='Pod stolem jsem ještě nebyl.',area_label='kuchyň',area_confidence='medium',landmarks=['stůl'],uninspected=['pod stolem'],visibility='partial'))}}]}
    class Client:
        def post(self,url,**kwargs):captured.append(kwargs['json']);return Response()
    ctx=m.context(data,dict(area_id=None,known_views={'1':[data['areas'][0]['id']]}))
    result=ask_where(raw[1],'bowl',[dict(free=100,passage=True)]*3,Client(),search_context=ctx,queued=True)
    prompt=captured[0]['messages'][0]['content'][0]['text']
    assert 'SEARCH JOURNAL' in prompt and 'myčka' in prompt and 'pod stolem' in prompt
    assert 'not_observed_in_panorama' in prompt and 'NOT fully searched' in prompt
    assert 'jpeg' not in json.dumps(ctx)
    assert result['choice']==1 and captured[0]['max_tokens']==480


def test_planned_move_never_counts_as_entry(tmp_path):
    data=m.load(tmp_path/'x','misku');add(data);m.note_plan(data,4,'Do kuchyně')
    assert data['areas'][0]['last_plan']['status']=='planned_not_proof_of_entry'
    context=m.context(data,dict(area_id=None,known_views={}))
    assert 'Do kuchyně' not in json.dumps(context,ensure_ascii=False)
    assert context['areas'][0]['last_plan_status']=='planned_not_proof_of_entry'
