import asyncio
from copy import deepcopy
import json

import pytest

from server.simulator_perception import (
    TargetVisualMemory, perception_messages, perceive_current)

BEFORE = 'data:image/jpeg;base64,YmVmb3Jl'
CURRENT = 'data:image/jpeg;base64,Y3VycmVudA=='
COMMAND = dict(action='forward',speed=180,duration_ms=500,tilt=90)


def observation(positive=True, **updates):
    value = dict(goal_visible=positive,confidence=.9,bearing='ahead' if positive else 'unknown',
        target_pixel=dict(x=.5,y=.5) if positive else None,
        threshold_visible=positive,threshold_pixel=.7 if positive else None,
        at_threshold=False,hazard='clear',description='Aktuální obraz.',
        evidence='Aktuální viditelná hrana.' if positive else 'Aktuální cíl není doložený.')
    value.update(updates)
    return value


def seeded():
    memory=TargetVisualMemory()
    memory.remember(BEFORE,observation())
    memory.completed(COMMAND,accepted=True)
    return memory


def test_default_messages_stay_backward_compatible_and_memory_has_only_two_images():
    assert len(perception_messages('Cíl',CURRENT)[1]['content'])==2
    previous=seeded().previous_observation()
    messages=perception_messages('Cíl',CURRENT,previous)
    content=messages[1]['content']
    assert json.loads(content[0]['text'])=={'goal':'Cíl'}
    assert [part['image_url']['url'] for part in content if part['type']=='image_url']==[BEFORE,CURRENT]
    assert 'BEFORE' in content[1]['text'] and 'CURRENT' in content[-2]['text']
    assert json.loads(content[3]['text'])==dict(accepted_commands_since_before=[COMMAND],age_steps=1,before_provenance='current_image_goal',before_equals_current=False)
    assert 'Pozitivní BEFORE NIKDY automaticky nepotvrzuje CURRENT' in messages[0]['content']
    assert 'CURRENT práh\nnedokládá' in messages[0]['content']
    assert 'STŘED VIDITELNÉHO ÚSEKU AKTUÁLNÍ HRANY PRAHU' in messages[0]['content']
    assert 'střed VIDITELNÉHO OTEVŘENÍ/PRŮCHODU' not in messages[0]['content']
    assert 'y=0.688, NIKOLI 688' in messages[0]['content']


def test_old_positive_does_not_upgrade_a_negative_current_model_response():
    class Provider:
        async def infer(self,messages,schema,**kwargs):
            self.messages=deepcopy(messages)
            return observation(False),dict(latency_ms=1)
    memory=seeded();provider=Provider()
    value,_=asyncio.run(perceive_current(provider,'Cíl',CURRENT,memory.previous_observation()))
    assert value['goal_visible'] is False and value['threshold_visible'] is False
    assert value['target_pixel'] is None and value['threshold_pixel'] is None
    memory.remember(CURRENT,value,used_memory=True)
    assert memory.previous_observation() is None


def test_inconsistent_current_output_still_fails_and_is_only_attached_as_diagnostic():
    invalid=observation(False);invalid['threshold_visible']=True;invalid['threshold_pixel']=.7
    class Provider:
        async def infer(self,*args,**kwargs):return deepcopy(invalid),{}
    with pytest.raises(ValueError) as caught:
        asyncio.run(perceive_current(Provider(),'Cíl',CURRENT,seeded().previous_observation()))
    assert caught.value.invalid_observation==invalid
    assert caught.value.invalid_observation['goal_visible'] is False


def test_negative_and_low_confidence_frames_never_create_or_retain_an_anchor():
    for value in (observation(False),observation(confidence=.79)):
        memory=seeded();memory.remember(CURRENT,value,used_memory=True)
        assert memory.previous_observation() is None
        memory.remember(CURRENT,value);memory.completed(COMMAND,accepted=True)
        assert memory.previous_observation() is None


def test_memory_expires_after_three_accepted_steps_without_new_visual_confirmation():
    memory=seeded()
    for age in (1,2,3):
        previous=memory.previous_observation()
        assert previous['age_steps']==age and previous['image']==BEFORE
        memory.remember(CURRENT,observation(target_pixel=None),used_memory=True)
        assert memory.previous_observation()['age_steps']==age
        memory.completed(COMMAND,accepted=True)
    assert memory.previous_observation() is None


def test_new_positive_current_threshold_and_point_can_renew_the_visual_frame_not_a_claim_alone():
    memory=seeded()
    memory.completed(COMMAND,accepted=True)
    assert memory.previous_observation()['age_steps']==2
    memory.remember(CURRENT,observation(),used_memory=True)
    assert memory.previous_observation() is None  # no accepted step since this frame yet
    memory.completed(COMMAND,accepted=True)
    assert memory.previous_observation()['image']==CURRENT
    assert memory.previous_observation()['age_steps']==1
    assert memory.snapshot()['provenance']=='tracked_current_threshold'
    assert memory.snapshot()['positive_chain_count']==2
    no_threshold=observation(threshold_visible=False,threshold_pixel=None)
    memory.remember(BEFORE,no_threshold,used_memory=True)
    assert memory.previous_observation()['image']==CURRENT


def test_current_threshold_without_clear_hazard_cannot_renew_and_negative_clears_provenance():
    for hazard in ('unknown','obstacle','person','animal','drop'):
        memory=seeded();memory.remember(CURRENT,observation(hazard=hazard),used_memory=True)
        assert memory.previous_observation()['image']==BEFORE
    memory=seeded();memory.remember(CURRENT,observation(),used_memory=True)
    memory.remember(CURRENT,observation(False),used_memory=True)
    snapshot=memory.snapshot()
    assert snapshot['provenance'] is None and snapshot['positive_chain_count']==0
    assert snapshot['negative_resets']==1 and not snapshot['has_frame']
    assert snapshot['events'][-1]['reason']=='negative_or_uncertain_current'


def test_tracking_metadata_never_contains_old_reasoning_or_a_completion_claim():
    memory=seeded();snapshot=memory.snapshot()
    assert not {'image','evidence','description','candidate_goal_reached','action'} & snapshot.keys()
    memory.completed(COMMAND,accepted=True);memory.completed(COMMAND,accepted=True);memory.completed(COMMAND,accepted=True)
    assert memory.snapshot()['expired_resets']==1


def test_only_categorical_provenance_is_sent_and_old_context_without_it_still_works():
    memory=seeded();memory.remember(CURRENT,observation(),used_memory=True);memory.completed(COMMAND,accepted=True)
    previous=memory.previous_observation();assert previous['provenance']=='tracked_current_threshold'
    messages=perception_messages('Cíl',CURRENT,previous)
    metadata=next(json.loads(part['text']) for part in messages[1]['content'] if part['type']=='text' and part['text'].startswith('{') and 'before_provenance' in part['text'])
    assert metadata['before_provenance']=='tracked_current_threshold'
    assert 'zárubně v OBOU snímcích samy o sobě' in messages[0]['content']
    previous.pop('provenance');assert perception_messages('Cíl',CURRENT,previous)
    previous['provenance']='Starý příběh o R1'
    with pytest.raises(ValueError):perception_messages('Cíl',CURRENT,previous)


def test_identity_appearance_frame_is_retained_separately_from_recent_tracked_frame():
    memory=seeded();memory.remember(CURRENT,observation(),used_memory=True);memory.completed(COMMAND,accepted=True)
    previous=memory.previous_observation()
    assert previous['identity_image']==BEFORE and previous['image']==CURRENT
    messages=perception_messages('Cíl',CURRENT,previous)
    assert [part['image_url']['url'] for part in messages[1]['content'] if part['type']=='image_url']==[BEFORE,CURRENT]
    assert 'PIXELOVĚ TOTOŽNÝ' in ' '.join(part.get('text','') for part in messages[1]['content'])
    assert 'IDENTITY / starší' in messages[1]['content'][1]['text']
    assert 'bez odpovídající aktuální podlahové hrany vrať false/null' in messages[0]['content'].lower()
    assert memory.snapshot()['has_identity_frame']
    memory.remember(CURRENT,observation(False),used_memory=True)
    assert memory.previous_observation() is None and not memory.snapshot()['has_identity_frame']


def test_distinct_identity_previous_current_frames_are_all_preserved():
    memory=seeded();memory.remember(CURRENT,observation(),used_memory=True);memory.completed(COMMAND,accepted=True)
    newest='data:image/jpeg;base64,bmV3ZXN0'
    messages=perception_messages('Cíl',newest,memory.previous_observation())
    assert [part['image_url']['url'] for part in messages[1]['content'] if part['type']=='image_url']==[BEFORE,CURRENT,newest]


def test_invalid_external_identity_is_rejected_and_unrelated_metadata_cannot_leak():
    previous=seeded().previous_observation();previous['identity_image']='http://external.invalid/picture.jpg'
    with pytest.raises(ValueError):perception_messages('Cíl',CURRENT,previous)
    previous=seeded().previous_observation();previous['identity_evidence']='Old story'
    with pytest.raises(ValueError):perception_messages('Cíl',CURRENT,previous)


def test_only_actual_accepted_commands_are_retained_and_memory_is_detached():
    memory=TargetVisualMemory();memory.remember(BEFORE,observation())
    assert memory.previous_observation() is None
    memory.completed(COMMAND,accepted=False);assert memory.previous_observation() is None
    command=deepcopy(COMMAND);memory.completed(command,accepted=True);command['speed']=0
    previous=memory.previous_observation();previous['accepted_commands'][0]['speed']=1
    assert memory.previous_observation()['accepted_commands']==[COMMAND]
    memory.clear();assert memory.previous_observation() is None


@pytest.mark.parametrize('age',[None,True,0,4,-1,3.5])
def test_expired_or_invalid_external_memory_cannot_be_sent_to_model(age):
    previous=seeded().previous_observation();previous['age_steps']=age
    with pytest.raises(ValueError):perception_messages('Cíl',CURRENT,previous)


@pytest.mark.parametrize('field,value', [('pose',dict(x=1)),('route_id','R1'),('evidence','Starý příběh')])
def test_world_or_narrative_fields_are_not_accepted_in_visual_memory(field,value):
    previous=seeded().previous_observation();previous[field]=value
    with pytest.raises(ValueError):perception_messages('Cíl',CURRENT,previous)


@pytest.mark.parametrize('field,value',[('speed',True),('speed',181),('duration_ms',float('nan')),
    ('duration_ms',2001),('tilt',34),('action','navigate'),('route_id','R1')])
def test_invalid_or_narrative_accepted_command_is_rejected(field,value):
    previous=seeded().previous_observation();previous['accepted_commands'][0][field]=value
    with pytest.raises(ValueError):perception_messages('Cíl',CURRENT,previous)


@pytest.mark.parametrize('age',[0,4,True,-1,None])
def test_configuration_cannot_expand_the_three_step_limit(age):
    with pytest.raises(ValueError):TargetVisualMemory(max_age_steps=age)


def test_mismatched_command_age_and_invalid_acceptance_are_rejected():
    previous=seeded().previous_observation();previous['accepted_commands']=[]
    with pytest.raises(ValueError):perception_messages('Cíl',CURRENT,previous)
    with pytest.raises(ValueError):seeded().completed(COMMAND,accepted=1)
