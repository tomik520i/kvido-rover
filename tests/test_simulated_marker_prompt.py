"""Simulator-only visual calibration changes instructions, never model flags."""
import asyncio
from copy import deepcopy
import sys
import types

import pytest

from server.simulator_perception import (
    PERCEPTION_SYSTEM, SIM_MARKER_INSTRUCTION, perceive_current)

IMAGE='data:image/jpeg;base64,Q1VSUkVOVA=='


def observation(**updates):
    value=dict(goal_visible=True,confidence=.9,bearing='ahead',target_pixel={'x':.5,'y':.3},
        threshold_visible=True,threshold_pixel=.4,at_threshold=False,hazard='clear',
        description='Modrý rám a žlutá šikmá hrana na podlaze.',evidence='Viditelná podlahová hrana.')
    value.update(updates)
    return value


class Provider:
    def __init__(self,value):self.value=value;self.calls=[]
    async def infer(self,messages,schema,**kwargs):
        self.calls.append(deepcopy(messages));return deepcopy(self.value),{}


def detector(monkeypatch,callback):
    module=types.ModuleType('server.threshold_pixels');module.detect_threshold=callback
    monkeypatch.setitem(sys.modules,'server.threshold_pixels',module)


def test_general_production_prompt_unchanged_and_sim_instruction_explicitly_opt_in(monkeypatch):
    value=observation(threshold_visible=False,threshold_pixel=None)
    plain,sim=Provider(value),Provider(value)
    detector(monkeypatch,lambda _:(_ for _ in ()).throw(AssertionError('No marker gate')))
    asyncio.run(perceive_current(plain,'Cíl',IMAGE))
    asyncio.run(perceive_current(sim,'Cíl',IMAGE,localize_simulated_threshold=True))
    assert plain.calls[0][0]['content']==PERCEPTION_SYSTEM
    assert sim.calls[0][0]['content']==PERCEPTION_SYSTEM+'\n\n'+SIM_MARKER_INSTRUCTION
    assert plain.calls[0][1]==sim.calls[0][1]  # identical goal and CURRENT bytes
    assert 'perspektiva' in SIM_MARKER_INSTRUCTION and 'at_threshold=false' in SIM_MARKER_INSTRUCTION


def test_positive_visible_slanted_threshold_remains_visible_but_not_close(monkeypatch):
    detector(monkeypatch,lambda _:dict(x=.55,y=.43,quality=.95,
        bounds=dict(left=.3,right=.8,top=.4,bottom=.46),
        provenance='simulated_colored_threshold_current_pixels',measurement='top_edge_of_colored_threshold_band'))
    value,meta=asyncio.run(perceive_current(Provider(observation()),'Cíl',IMAGE,
                                          localize_simulated_threshold=True))
    assert value['goal_visible'] and value['threshold_visible'] and not value['at_threshold']
    assert meta['raw_model_perception']['threshold_pixel']==.4
    assert value['threshold_pixel']==.43


@pytest.mark.parametrize('update',[
    dict(goal_visible=False,confidence=0,bearing='unknown',target_pixel=None,
         threshold_visible=False,threshold_pixel=None),
    dict(threshold_visible=False,threshold_pixel=None),
    dict(hazard='person'),dict(hazard='animal'),dict(hazard='drop')])
def test_negative_threshold_and_hazards_never_upgrade_from_sim_instruction(monkeypatch,update):
    raw=observation(**update)
    detector(monkeypatch,lambda _:(_ for _ in ()).throw(AssertionError('Closed semantic gate')))
    value,meta=asyncio.run(perceive_current(Provider(raw),'Cíl',IMAGE,
                                          localize_simulated_threshold=True))
    assert value==raw and meta['raw_model_perception']==raw
    assert meta['current_threshold_localization']['status']=='semantic_gate_closed'
