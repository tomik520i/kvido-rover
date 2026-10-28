from copy import deepcopy
import asyncio
import json

import pytest

from server.simulator_perception import (
    PERCEPTION_SCHEMA, PERCEPTION_SYSTEM, perception_messages, perceive_current, validate_perception)


IMAGE = 'data:image/jpeg;base64,eA=='


def observation(**updates):
    return dict(goal_visible=False, confidence=.9, bearing='unknown', target_pixel=None,
                threshold_visible=False, threshold_pixel=None, at_threshold=False,
                hazard='unknown', description='Béžová stěna a podlaha.',
                evidence='Modrý rám ani otevřený průchod nejsou viditelné.', **updates)


def test_messages_contain_only_goal_and_one_current_image():
    messages = perception_messages('Najdi modré dveře', IMAGE)
    assert len(messages) == 2
    content = messages[1]['content']
    assert json.loads(content[0]['text']) == {'goal': 'Najdi modré dveře'}
    assert content[1] == {'type': 'image_url', 'image_url': {'url': IMAGE}}
    assert len(content) == 2
    with pytest.raises(TypeError):
        perception_messages('Cíl', IMAGE, history=[{'route_id': 'R1', 'pose': 'SECRET'}])
    with pytest.raises(ValueError):
        perception_messages('Cíl', 'http://example.invalid/image.jpg')


@pytest.mark.parametrize('field,value', [
    ('confidence', True), ('confidence', float('nan')), ('confidence', 1.1),
    ('goal_visible', 'true'), ('bearing', 'forward'), ('hazard', 'safe'),
    ('target_pixel', {'x': .3, 'y': float('inf')}),
    ('target_pixel', {'x': .3, 'y': True}), ('target_pixel', {'x': .3}),
    ('threshold_pixel', float('nan')), ('description', ''), ('evidence', 'x' * 801)])
def test_invalid_observation_rejected(field, value):
    data = observation(); data[field] = value
    with pytest.raises(ValueError, match='invalid_perception_observation'):
        validate_perception(data)


def test_visible_doorway_is_not_automatically_at_threshold_or_completed():
    data = observation()
    data.update(goal_visible=True, bearing='ahead', target_pixel={'x': .5, 'y': .4},
                threshold_visible=True, threshold_pixel=.58, at_threshold=False)
    assert validate_perception(data) == data
    assert 'completed' not in PERCEPTION_SCHEMA['properties']
    assert not validate_perception(data)['at_threshold']
    result = validate_perception(data); result['target_pixel']['x'] = .1
    assert data['target_pixel']['x'] == .5


def test_explicit_close_threshold_claim_remains_observation_without_motor_fields():
    data = observation()
    data.update(goal_visible=True, bearing='ahead', target_pixel={'x': .5, 'y': .6},
                threshold_visible=True, threshold_pixel=.93, at_threshold=True,
                description='Modrý průchod a práh v popředí.',
                evidence='Žlutá hrana prahu je těsně u spodního okraje obrazu.')
    assert validate_perception(data)['at_threshold']
    assert not {'action', 'speed', 'duration_ms', 'goal_status'} & data.keys()


def test_localization_rules_require_visible_opening_and_real_floor_threshold():
    assert 'střed VIDITELNÉHO OTEVŘENÍ/PRŮCHODU' in PERCEPTION_SYSTEM
    assert 'Nevyplňuj 0.5' in PERCEPTION_SYSTEM
    assert 'HRANY\nPRAHU NA PODLAZE' in PERCEPTION_SYSTEM
    assert 'Lokalizace souřadnic sama nezvyšuje confidence' in PERCEPTION_SYSTEM
    data = observation()
    data.update(goal_visible=True, confidence=.6, bearing='ahead',
                target_pixel={'x': .49, 'y': .29},
                threshold_visible=True, threshold_pixel=.58)
    assert validate_perception(data)['confidence'] == .6
    data['target_pixel'] = None
    assert validate_perception(data)['target_pixel'] is None


@pytest.mark.parametrize('changes', [
    {'goal_visible': False, 'bearing': 'left'},
    {'goal_visible': False, 'target_pixel': {'x': .5, 'y': .5}},
    {'goal_visible': False, 'threshold_visible': True, 'threshold_pixel': .5},
    {'goal_visible': True, 'at_threshold': True},
    {'threshold_visible': True}, {'threshold_pixel': .5}, {'pose': 'hidden'},
])
def test_inconsistent_or_extra_fields_rejected(changes):
    data = observation(); data.update(changes)
    with pytest.raises(ValueError):
        validate_perception(data)


def test_single_gemma_only_call_keeps_unknown_without_inventing_success():
    class Provider:
        def __init__(self): self.calls = []
        async def infer(self, messages, schema, **kwargs):
            self.calls.append((deepcopy(messages), deepcopy(schema), kwargs))
            return observation(), {'latency_ms': 12}
    provider = Provider()
    value, meta = asyncio.run(perceive_current(provider, 'Najdi dveře', IMAGE))
    assert len(provider.calls) == 1
    assert provider.calls[0][2] == dict(provider='gemma', thinking=False, max_output_tokens=900, gemma_temperature=0.0)
    assert provider.calls[0][1] == PERCEPTION_SCHEMA
    assert not value['goal_visible'] and not value['at_threshold']
    assert meta == {'latency_ms': 12}


def test_provider_error_propagates_without_retry_or_fake_observation():
    class Provider:
        calls = 0
        async def infer(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError('offline')
    provider = Provider()
    with pytest.raises(RuntimeError, match='offline'):
        asyncio.run(perceive_current(provider, 'Najdi dveře', IMAGE))
    assert provider.calls == 1
