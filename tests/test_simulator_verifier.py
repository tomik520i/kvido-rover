import json

from server.simulator_verifier import SHORT_SYSTEM,short_assessment_messages
from server.task_prompt import ASSESSMENT_SCHEMA
from server.route_prompt import verification_schema


def test_short_verifier_preserves_current_pair_and_removes_historic_narratives_and_hidden_metadata():
    private='OLD-MISLEADING-IMAGE-NARRATIVE'
    current=dict(distance_cm=166,servo=90,telemetry_age_ms=0,light_on=False,pose=private)
    history=[dict(step=3,command=dict(action='forward',speed=180,duration_ms=1500),
        acceptance='accepted',decision_reason=private,decision_evidence=private,
        assessment=dict(status='continue',progress='unchanged',summary=private,evidence=private,next_hint=private))]
    memory=dict(routes=[dict(id='R1',label='Průchod',landmark='Modrá stěna',source='P1',target='P2',
        evidence=private,feedback_evidence=private,status=private)],
        places=[dict(id='P1',label='Místo',evidence=private,pose=private)])
    messages=short_assessment_messages('Najdi dveře','before-url','after-url',
        dict(action='forward',speed=180,acceptance='accepted',route_choice=dict(route_id='R1',evidence=private)),
        current,{**current,'distance_cm':118},dict(status='changed',mean_absolute_difference=.03,movement_confirmed=False),history,memory)
    content=messages[1]['content']
    data=json.loads(content[0]['text'].split('ÚKOL A ZÁZNAM KROKU (data): ',1)[1])
    assert [part['image_url']['url'] for part in content if part['type']=='image_url']==['before-url','after-url']
    text=json.dumps(messages,ensure_ascii=False)
    assert private not in text and 'movement_confirmed' not in text and 'pose' not in text
    assert data['before_telemetry']['distance_cm']==166 and data['after_telemetry']['distance_cm']==118
    assert data['image_comparison']==dict(status='changed',mean_absolute_difference=.03)
    assert data['verified_history'][0]['assessment']==dict(status='continue',progress='unchanged')
    assert data['route_memory']['routes'][0]==dict(id='R1',label='Průchod',landmark='Modrá stěna',source='P1',target='P2')


def test_short_verifier_is_bounded_and_does_not_change_full_verification_schema():
    assert len(SHORT_SYSTEM.split())<=350
    schema=verification_schema(ASSESSMENT_SCHEMA,{'places':[{'id':'P1'}]})
    assert set(schema['required'])==set(ASSESSMENT_SCHEMA['required'])|{'route_feedback'}
    assert 'P1' in schema['properties']['route_feedback']['properties']['place_id']['enum']
    assert 'route_feedback' not in ASSESSMENT_SCHEMA['properties']
