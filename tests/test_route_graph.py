from copy import deepcopy

import pytest

from server.route_memory import empty_route_memory,update_route_memory
from server.route_prompt import planning_schema,verification_schema


def choice(identifier='new',label='Průchod u béžového rohu'):
    return dict(route_id=identifier,label=label,landmark='Roh stěny a navazující podlaha',bearing='ahead',evidence='Podlaha pokračuje za původním rohem.')


def passed(identifier='new',place_id='new',place_evidence='Nový výhled, původní roh je za kamerou.'):
    return dict(route_choice=choice(identifier),acceptance='accepted',command=dict(action='forward'),
                motion_observation=dict(status='changed'),route_feedback=dict(status='passed',evidence='Historie a nový obraz ukazují projetí původního průchodu.',
                place_id=place_id,place_label='Místo za rohem',place_evidence=place_evidence))


def test_passed_creates_topological_edge_without_geometry():
    memory=update_route_memory(None,passed(),1)
    edge=memory['routes'][0]
    assert (edge['source'],edge['target'],edge['visited'])==('P1','P2',True)
    assert edge['exploration_status']=='visited'
    assert memory['active_place_id']=='P2' and memory['active_route_id'] is None
    assert [p['id'] for p in memory['places']]==['P1','P2']
    assert all(p['provenance']=='inferred_by_model' for p in memory['places'])
    assert not set(memory)&{'pose','geometry','coordinates','heading','x','y'}


def test_same_labels_in_new_places_do_not_merge_place_identity():
    memory=update_route_memory(None,passed(),1)
    memory=update_route_memory(memory,passed(),2)
    assert memory['active_place_id']=='P3'
    assert len(memory['places'])==3
    assert [(r['source'],r['target']) for r in memory['routes']]==[('P1','P2'),('P2','P3')]


@pytest.mark.parametrize('identity,evidence',[('P1',''),('P999','detail'),('unknown','detail')])
def test_revisit_existing_edge_requires_explicit_known_place_and_evidence(identity,evidence):
    memory=update_route_memory(None,passed(),1)
    result=update_route_memory(memory,passed('R1',identity,evidence),2)
    assert result['active_place_id']=='P2'
    assert len(result['places'])==2
    assert result['notes']


def test_explicit_visual_revisit_links_back_and_counts_visit():
    memory=update_route_memory(None,passed(),1)
    memory=update_route_memory(memory,passed('R1','P1','Rozpoznávám původní roh a modrou stěnu v opačném pohledu navazujícím na historii.'),2)
    assert memory['active_place_id']=='P1'
    assert len(memory['places'])==2
    assert memory['places'][0]['visits']==2
    assert memory['routes'][0]['traversals']==2
    assert memory['routes'][0]['target']=='P2'


def test_repeated_stall_selects_other_observed_edge_not_infinite_r1():
    memory=update_route_memory(None,dict(route_choice=choice()),1)
    memory=update_route_memory(memory,dict(route_choice=choice(label='Jiná chodba vlevo')),2)
    stall=dict(route_choice=choice('R1'),acceptance='accepted',command=dict(action='forward'),
               motion_observation=dict(status='possible_stall'),route_feedback=dict(status='unknown',evidence=''))
    memory=update_route_memory(memory,stall,3)
    assert memory['active_route_id']=='R2'
    memory=update_route_memory(memory,stall,4)
    assert memory['routes'][0]['failures']==2
    assert memory['routes'][0]['exploration_status']=='exhausted'
    assert memory['active_route_id']=='R2' and 'R1' in memory['alternative_reason']
    memory=update_route_memory(memory,stall,5)
    assert memory['active_route_id']=='R2'


def test_model_blockage_alternative_is_hypothesis_and_unknown_acceptance_not_stall():
    memory=update_route_memory(None,dict(route_choice=choice()),1)
    unknown=dict(route_choice=choice('R1'),acceptance='unknown',command=dict(action='forward'),motion_observation=dict(status='possible_stall'))
    memory=update_route_memory(memory,unknown,2)
    assert memory['routes'][0]['failures']==0
    memory=update_route_memory(memory,dict(route_choice=choice('R1'),route_feedback=dict(status='blocked',evidence='Průchod je na aktuálním obrazu vyplněn krabicí.')),3)
    assert memory['active_route_id'] is None
    assert memory['routes'][0]['provenance']=='inferred_by_model'
    assert 'modelově' in memory['alternative_reason']


def test_graph_retention_has_no_dangling_place_references_and_monotonic_ids():
    memory=None
    for step in range(1,35):memory=update_route_memory(memory,passed(),step)
    assert len(memory['routes'])<=12 and len(memory['places'])<=13
    ids={p['id'] for p in memory['places']}
    assert memory['active_place_id']=='P35'
    assert all(r['source'] in ids and r['target'] in ids for r in memory['routes'])
    assert memory['next_place_number']==36 and memory['next_route_number']==35


def test_unknown_route_reference_does_not_change_graph_or_attribute_feedback():
    memory=update_route_memory(None,passed(),1)
    before=deepcopy(memory)
    result=update_route_memory(memory,passed('R999','P1'),2)
    assert result['places']==before['places'] and result['active_place_id']=='P2'
    assert memory==before


def test_schema_places_are_accounted_for_explicitly_without_mutating_input():
    schema=dict(type='object',properties={},required=[],additionalProperties=False)
    before=deepcopy(schema)
    memory=update_route_memory(None,passed(),1)
    value=verification_schema(schema,memory)
    props=value['properties']['route_feedback']['properties']
    assert props['place_id']['enum']==['new','unknown','P1','P2']
    assert set(value['properties']['route_feedback']['required'])==set(props)
    assert planning_schema(schema,memory)['properties']['route_choice']['properties']['route_id']['enum']==['none','new','R1']
    assert schema==before
    assert verification_schema(schema)['properties']['route_feedback']['properties']['place_id']['enum']==['new','unknown']


@pytest.mark.parametrize('identity,label,evidence',[
    ('unknown','Nová místnost','Nová podlaha'),('new','','Nová podlaha'),
    ('new','Nová místnost',''),(None,'Nová místnost','Nová podlaha')])
def test_unknown_or_undescribed_place_cannot_create_graph_connection(identity,label,evidence):
    result=passed(place_id=identity,place_evidence=evidence)
    result['route_feedback']['place_label']=label
    memory=update_route_memory(None,result,1)
    assert memory['active_place_id']=='P1' and len(memory['places'])==1
    assert memory['routes'][0]['target'] is None
    assert not memory['routes'][0]['visited']
    assert memory['active_route_id']=='R1' and memory['notes']


@pytest.mark.parametrize('blocked_status',['stalled','exhausted','blocked'])
def test_backward_escape_and_visible_feedback_do_not_reopen_failed_route(blocked_status):
    memory=update_route_memory(None,dict(route_choice=choice()),1)
    memory=update_route_memory(memory,dict(route_choice=choice(label='Jiná pozorovaná cesta')),2)
    edge=memory['routes'][0]
    edge.update(failures=2,consecutive_failures=2,exploration_status=blocked_status)
    result=dict(route_choice=choice('R1'),acceptance='accepted',command=dict(action='backward'),
                motion_observation=dict(status='changed'),route_feedback=dict(status='visible',evidence='Průchod je po ústupu znovu vidět.'))
    memory=update_route_memory(memory,result,3)
    edge=memory['routes'][0]
    assert edge['failures']==2 and edge['consecutive_failures']==2
    assert edge['exploration_status']==blocked_status
    assert memory['active_route_id']=='R2'
    assert 'R1' not in memory['alternatives']


@pytest.mark.parametrize('feedback_status',['closer','passed'])
def test_positive_route_progress_can_restore_availability_without_erasing_failure_history(feedback_status):
    memory=update_route_memory(None,dict(route_choice=choice()),1)
    memory['routes'][0].update(failures=3,consecutive_failures=2,exploration_status='exhausted')
    result=passed('R1')
    result['route_feedback']['status']=feedback_status
    memory=update_route_memory(memory,result,2)
    edge=memory['routes'][0]
    assert edge['failures']==3 and edge['consecutive_failures']==0
    assert edge['exploration_status']==('visited' if feedback_status=='passed' else 'available')
