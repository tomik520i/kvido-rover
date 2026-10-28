from copy import deepcopy

import pytest

from server.route_memory import empty_route_memory,update_route_memory


def new(**extra):
    result=dict(route_choice=dict(route_id='new',label='Průchod vpravo',landmark='Konec modré stěny',bearing='right',evidence='Podlaha pokračuje za hranou stěny.'))
    result.update(extra)
    return result


def test_none_default_and_detached_snapshots():
    assert update_route_memory(None,None,None)==empty_route_memory()
    memory=update_route_memory(None,new(),1);before=deepcopy(memory)
    result=update_route_memory(memory,{},2)
    result['routes'][0]['label']='changed'
    assert memory==before


def test_new_route_stable_id_exact_description_merge_and_provenance():
    memory=update_route_memory(None,new(),1)
    again=new();again['route_choice']['label']='  PRŮCHOD   VPRAVO '
    again['route_choice']['landmark']='konec modre steny'
    memory=update_route_memory(memory,again,2)
    assert len(memory['routes'])==1 and memory['active_route_id']=='R1'
    route=memory['routes'][0]
    assert route['first_seen_step']==1 and route['last_seen_step']==2
    assert route['status']=='candidate' and route['provenance']=='inferred_by_model'


@pytest.mark.parametrize('acceptance,action,motion,evidence',[
    ('unknown','forward','changed','vidím změnu'),('rejected','forward','changed','vidím změnu'),
    ('accepted','rotate_left','changed','vidím změnu'),('accepted','forward','possible_stall','vidím změnu'),
    ('accepted','forward','changed','')])
def test_passed_requires_all_motion_evidence(acceptance,action,motion,evidence):
    result=new(acceptance=acceptance,command=dict(action=action),motion_observation=dict(status=motion),route_feedback=dict(status='passed',evidence=evidence))
    memory=update_route_memory(None,result,1)
    assert memory['active_route_id']=='R1'
    assert memory['routes'][0]['status'] in ('candidate','visible')


def test_passed_clears_active_but_not_history_and_new_scene_allocates_new_id():
    result=new(acceptance='accepted',command=dict(action='forward'),motion_observation=dict(status='changed'),route_feedback=dict(status='passed',evidence='Hrana stěny zmizela za bokem, nový prostor je před kamerou.',place_id='new',place_label='Místo za stěnou',place_evidence='Původní hrana zůstala za autem; vpředu je nový prostor.'))
    memory=update_route_memory(None,result,1)
    assert memory['active_route_id'] is None
    assert memory['routes'][0]['status']=='passed' and memory['routes'][0]['attempts']==1
    memory=update_route_memory(memory,new(),2)
    assert memory['active_route_id']=='R2'


def test_lost_and_model_blockage_preserve_active_landmark():
    memory=update_route_memory(None,new(),1)
    for status in ('lost','blocked','unknown'):
        memory=update_route_memory(memory,dict(route_choice=dict(route_id='R1'),route_feedback=dict(status=status,evidence='Obraz již neukazuje původní průchod.')),2)
        assert memory['active_route_id']==('R1' if status=='lost' else None)
        assert memory['routes'][0]['provenance']=='inferred_by_model'
    assert memory['routes'][0]['status']=='blocked'


def test_unknown_reference_does_not_allocate_or_misattribute_feedback():
    memory=update_route_memory(None,new(),1)
    for i in range(20):
        memory=update_route_memory(memory,dict(route_choice=dict(route_id='R999'),route_feedback=dict(status='passed',evidence='claim')),i)
    assert len(memory['routes'])==1 and memory['active_route_id']=='R1'
    assert memory['routes'][0]['status']=='candidate'
    assert len(memory['notes'])==8 and memory['next_route_number']==2


def test_twelve_route_bound_and_monotonic_ids():
    memory=None
    for i in range(20):
        item=new();item['route_choice']['label']=f'Průchod {i}'
        memory=update_route_memory(memory,item,i)
    assert len(memory['routes'])==12
    assert memory['active_route_id']=='R20' and memory['next_route_number']==21


def test_closer_is_inference_not_ground_truth_and_actual_command_wins():
    result=new(acceptance='accepted',action='rotate_left',command=dict(action='right'),motion_observation=dict(status='changed'),route_feedback=dict(status='closer',evidence='Průchod je v novém obrazu větší.'))
    memory=update_route_memory(None,result,1)
    assert memory['routes'][0]['status']=='closer'
    assert memory['routes'][0]['provenance']=='inferred_by_model'


@pytest.mark.parametrize('identifier',['R1','new'])
def test_existing_bearing_tracks_current_view_including_unknown(identifier):
    memory=update_route_memory(None,new(),1)
    for bearing in ('left','ahead','unknown'):
        choice=new()['route_choice'];choice.update(route_id=identifier,bearing=bearing)
        memory=update_route_memory(memory,dict(route_choice=choice),2)
        assert len(memory['routes'])==1
        assert memory['routes'][0]['bearing']==bearing
        assert memory['active_route_id']=='R1'


@pytest.mark.parametrize('choice',[None,{},dict(route_id='none')])
def test_no_explicit_route_reference_preserves_memory_without_attributing_feedback(choice):
    memory=update_route_memory(None,new(),1)
    before=deepcopy(memory)
    result=dict(route_choice=choice,acceptance='accepted',command=dict(action='forward'),
                motion_observation=dict(status='changed'),route_feedback=dict(status='passed',evidence='New image claim'))
    assert update_route_memory(memory,result,2)==before
