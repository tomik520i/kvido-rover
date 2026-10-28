"""Only the numeric current-image fields can be grounded, never identity."""
from copy import deepcopy
import asyncio
import math
import sys
import types

import pytest

from server.simulator_perception import _ground_current_threshold, perceive_current, validate_perception


def observation(**updates):
    result=dict(goal_visible=True,confidence=.9,bearing='unknown',target_pixel=None,
        threshold_visible=True,threshold_pixel=None,at_threshold=False,hazard='clear',
        description='Aktuální žlutá hrana známého prahu.',evidence='Návaznost skutečně viditelné hrany.')
    result.update(updates)
    return result


def pixels(**updates):
    result=dict(x=.5,y=.8,bounds=dict(left=.1,top=.8,right=.9,bottom=.85),quality=.99,
        provenance='simulated_colored_threshold_current_pixels',measurement='top_edge_of_colored_threshold_band')
    result.update(updates)
    return result


@pytest.mark.parametrize('update',[dict(goal_visible=False),dict(goal_visible=1),dict(confidence=.79),
    dict(confidence=True),dict(confidence=math.nan),dict(threshold_visible=False),
    dict(threshold_visible=1),dict(hazard='unknown'),dict(hazard='person'),dict(hazard='drop')])
def test_closed_semantic_gate_does_not_read_pixels_or_upgrade_any_field(update):
    value=observation(**update)
    def forbidden(_):raise AssertionError('detector must not be consulted')
    result,meta=_ground_current_threshold(value,b'CURRENT ONLY',forbidden)
    assert result==value and meta['status']=='semantic_gate_closed'


def test_actual_current_pixels_can_repair_null_coordinates_not_semantic_claims():
    value=observation();before=deepcopy(value);calls=[]
    def detector(raw):calls.append(raw);return pixels()
    grounded,meta=_ground_current_threshold(value,b'CURRENT ONLY',detector)
    assert calls==[b'CURRENT ONLY'] and value==before
    assert grounded['target_pixel']==dict(x=.5,y=.8) and grounded['threshold_pixel']==.8
    assert grounded['goal_visible'] is True and grounded['confidence']==.9
    assert grounded['at_threshold'] is False and grounded['hazard']=='clear'
    assert validate_perception(grounded)==grounded
    assert meta['bounds']==pixels()['bounds'] and meta['simulation_specific'] is True
    assert meta['provenance']=='simulated_colored_threshold_current_pixels'


@pytest.mark.parametrize('point',[None,{},pixels(x=True),pixels(y=math.nan),pixels(y=1.2),
    pixels(quality=False),pixels(quality=0),pixels(provenance='world_truth'),
    pixels(bounds=dict(left=.7,top=.8,right=.3,bottom=.85)),
    pixels(bounds=dict(left=.1,top=.9,right=.9,bottom=.95))])
def test_missing_ambiguous_or_invalid_measurement_is_not_fabricated(point):
    value=observation();result,meta=_ground_current_threshold(value,b'CURRENT',lambda _:point)
    assert result==value and meta['status'] in ('current_marker_not_localized','invalid_pixel_measurement')


def test_pixel_detection_cannot_create_completion_or_fix_unrelated_semantic_contradictions():
    value=observation(at_threshold='true')
    grounded,_=_ground_current_threshold(value,b'CURRENT',lambda _:pixels())
    assert grounded['at_threshold']=='true' and 'candidate_goal_reached' not in grounded
    with pytest.raises(ValueError):validate_perception(grounded)


def install_detector(monkeypatch, detector):
    module=types.ModuleType('server.threshold_pixels');module.detect_threshold=detector
    monkeypatch.setitem(sys.modules,'server.threshold_pixels',module)


def test_opt_in_pipeline_reads_only_current_image_and_logs_raw_model_claims(monkeypatch):
    calls=[]
    install_detector(monkeypatch,lambda raw:calls.append(raw) or pixels())
    raw=observation()
    class Provider:
        async def infer(self,*args,**kwargs):return deepcopy(raw),{'provider':'gemma'}
    value,meta=asyncio.run(perceive_current(Provider(),'Cíl','data:image/jpeg;base64,Q1VSUkVOVA==',
        localize_simulated_threshold=True))
    assert calls==[b'CURRENT'] and meta['raw_model_perception']==raw
    assert meta['raw_model_perception']['target_pixel'] is None
    assert value['target_pixel']==dict(x=.5,y=.8)
    assert meta['current_threshold_localization']['status']=='localized'


def test_no_current_pixels_fails_closed_even_when_model_supplies_valid_coordinates(monkeypatch):
    install_detector(monkeypatch,lambda raw:None)
    raw=observation(target_pixel=dict(x=.5,y=.8),threshold_pixel=.8)
    class Provider:
        async def infer(self,*args,**kwargs):return deepcopy(raw),{}
    with pytest.raises(ValueError,match='current_threshold_not_localized') as caught:
        asyncio.run(perceive_current(Provider(),'Cíl','data:image/jpeg;base64,Q1VSUkVOVA==',localize_simulated_threshold=True))
    assert caught.value.invalid_observation==raw
    assert caught.value.current_threshold_localization['status']=='current_marker_not_localized'


def test_negative_current_model_output_stays_negative_in_real_wrapper(monkeypatch):
    def forbidden(raw):raise AssertionError('negative model observation must not consult marker')
    install_detector(monkeypatch,forbidden)
    negative=observation(goal_visible=False,threshold_visible=False)
    class Provider:
        async def infer(self,*args,**kwargs):return deepcopy(negative),{}
    value,meta=asyncio.run(perceive_current(Provider(),'Cíl','data:image/jpeg;base64,Q1VSUkVOVA==',localize_simulated_threshold=True))
    assert not value['goal_visible'] and value['target_pixel'] is None
    assert meta['current_threshold_localization']['status']=='semantic_gate_closed'
