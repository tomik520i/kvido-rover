import pytest

from server.exploration_memory import exploration_state


def move(action='rotate_left',acceptance='accepted',**extra):
    return dict(action=action,command=dict(action=action,speed=150,duration_ms=1000),acceptance=acceptance,**extra)


def state(steps=(),distance=100,age=0,goal='Najdi otevřené dveře a dojeď k nim'):
    return exploration_state(goal,list(steps),dict(distance_cm=distance,telemetry_age_ms=age))


@pytest.mark.parametrize('goal',['NAJDI KOČKU','Hledání misky','Prozkoumej byt','Dojeď ke dveřím','Jeď ke dveřím','najdi misku'])
def test_navigation_intent_with_case_and_diacritics(goal):
    assert state(goal=goal) is not None


@pytest.mark.parametrize('goal',['Rozsviť světlo','Ukaž kameru','Otoč se vlevo a potom doprava','Nakloň kameru na 55 stupňů','Jednou otoč kameru',None])
def test_non_navigation_intent_is_untouched(goal):
    assert state(goal=goal) is None


def test_scan_keeps_first_direction_and_then_forces_translation():
    assert set(state()['allowed_wheels'])=={'rotate_left','rotate_right','forward','backward','left','right'}
    for count in (1,2):
        allowed=state([move()]*count)['allowed_wheels']
        assert 'forward' in allowed and 'rotate_left' in allowed and 'rotate_right' not in allowed
    result=state([move()]*3)
    assert result['phase']=='advance' and result['allowed_wheels']==['forward']
    assert result['scan_steps']==3
    assert 'rotate_left' not in state([move('rotate_right')]*2)['allowed_wheels']


@pytest.mark.parametrize('distance',[None,False,float('nan'),float('inf'),-1,0,10,60,401,'100'])
def test_invalid_or_near_distance_never_forces_forward(distance):
    assert state([move()]*3,distance=distance)['allowed_wheels']==['rotate_left','backward','left','right']


@pytest.mark.parametrize('age',[None,True,-1,501,float('nan'),float('inf'),'0'])
def test_stale_or_invalid_age_never_allows_wheels(age):
    result=state([move()]*3,age=age)
    assert result['phase']=='observe' and result['allowed_wheels']==[]


def test_unknown_rejected_proposals_and_zero_speed_do_not_count():
    steps=[move(acceptance='unknown')]*5+[move(acceptance='transport_rejected')]*5
    steps+=[dict(action='rotate_left',acceptance='transport_accepted',command=dict(action='stop'))]
    zero=move();zero['command']['speed']=0;steps.append(zero)
    assert state(steps)['scan_steps']==0
    assert state(steps)['observations']==[]


def test_only_accepted_translation_resets_scan_and_observations_are_bounded():
    steps=[move()]*7+[move('forward','unknown')]
    assert state(steps)['scan_steps']==7
    steps+=[move('right')]
    result=state(steps)
    assert result['scan_steps']==0 and result['scan_direction'] is None
    assert len(result['observations'])==8
    assert result['observations'][-1]['action']=='right'


def test_possible_stall_allows_reversal_but_goal_unchanged_does_not():
    allowed=state([move(assessment=dict(progress='unchanged'))])['allowed_wheels']
    assert 'rotate_left' in allowed and 'rotate_right' not in allowed
    steps=[move(motion_observation=dict(status='possible_stall'))]
    assert 'rotate_right' in state(steps)['allowed_wheels'] and 'rotate_left' not in state(steps)['allowed_wheels']
    steps.append(move('rotate_right',motion_observation=dict(status='changed')))
    assert state(steps)['scan_direction']=='rotate_right'
    assert state(steps)['scan_steps']==2
    assert 'rotate_right' in state(steps)['allowed_wheels'] and 'rotate_left' not in state(steps)['allowed_wheels']
    assert 'forward' in state(steps)['allowed_wheels']


def test_eight_turn_limit_cannot_be_reset_by_repeated_stalls_or_fake_translation():
    steps=[move('rotate_left' if n%2 else 'rotate_right',motion_observation=dict(status='possible_stall')) for n in range(8)]
    steps.append(move('forward','unknown'))
    result=state(steps)
    assert result['phase']=='exhausted' and result['allowed_wheels']==[]
    assert 'Osm' in result['reason']


def test_actual_command_wins_over_proposal_and_inputs_are_not_mutated():
    item=move('forward');item['proposed_action']='rotate_left';item['action']='rotate_right'
    items=[move()]*3+[item]
    before=repr(items)
    result=state(items)
    assert result['scan_steps']==0 and result['observations'][-1]['action']=='forward'
    assert repr(items)==before


def test_legacy_transport_acceptance_remains_compatible():
    assert state([move(acceptance='transport_accepted')]*3)['phase']=='advance'


def test_active_visual_passage_allows_alignment_without_weakening_sensor_or_scan_budget():
    route={'active_route_id':'R1','routes':[{'id':'R1','label':'Průchod za rohem','status':'visible','bearing':'right'}]}
    def evaluate(age=0,distance=100,count=3):
        return exploration_state('Najdi dveře',[move()]*count,dict(telemetry_age_ms=age,distance_cm=distance),route)
    result=evaluate()
    assert result['phase']=='route' and 'rotate_right' in result['allowed_wheels']
    assert 'forward' not in evaluate(distance=None)['allowed_wheels']
    assert not evaluate(age=501)['allowed_wheels']
    assert evaluate(count=8)['phase']=='exhausted'


@pytest.mark.parametrize('count',[0,1,2])
@pytest.mark.parametrize('distance,forward',[(11,True),(60,True),(10,False),(None,False),(True,False),(float('nan'),False)])
def test_early_translation_options_respect_current_sensor(count,distance,forward):
    allowed=state([move()]*count,distance=distance)['allowed_wheels']
    assert ('forward' in allowed)==forward
    assert {'backward','left','right'}<=set(allowed)


def stalled(action='forward',acceptance='accepted'):
    return move(action,acceptance,motion_observation=dict(status='possible_stall'))


def test_failed_translations_survive_changed_rotations_tilt_and_route_alignment():
    steps=[stalled(),move('rotate_right',motion_observation=dict(status='changed')),
           stalled(),move('rotate_left',motion_observation=dict(status='changed')),move('tilt')]
    notebook=dict(active_route_id='R1',routes=[dict(id='R1',label='Průchod za stěnou',status='visible')])
    result=exploration_state('Najdi dveře',steps,dict(distance_cm=100,telemetry_age_ms=0),notebook)
    assert result['phase']=='escape'
    assert result['allowed_wheels']==['backward','left','right']
    assert result['stalled_translation_count']==2 and result['stalled_directions']==['forward']
    assert 'R1' in result['reason'] and 'zůstává' in result['reason']


def test_changed_accepted_translation_alone_resets_stall_memory():
    steps=[stalled(),stalled(),move('backward',motion_observation=dict(status='changed'))]
    result=state(steps)
    assert result['phase']!='escape' and result['stalled_translation_count']==0
    assert 'forward' in result['allowed_wheels']
    steps=[stalled(),stalled(),move('backward',motion_observation=dict(status='unknown'))]
    assert state(steps)['phase']=='escape'


def test_escape_excludes_every_stalled_recovery_direction_until_exhausted():
    steps=[stalled(),stalled()]
    for action in ('backward','left','right'):
        assert action in state(steps)['allowed_wheels']
        steps.append(stalled(action))
        assert action not in state(steps)['allowed_wheels']
    assert state(steps)['phase']=='exhausted'
    assert state(steps)['allowed_wheels']==[]


@pytest.mark.parametrize('acceptance',['unknown','rejected','transport_rejected'])
def test_rejected_or_unknown_translations_neither_prove_stall_nor_reset_it(acceptance):
    assert state([stalled(acceptance=acceptance)]*3)['stalled_translation_count']==0
    steps=[stalled(),stalled(),move('forward',acceptance,motion_observation=dict(status='changed'))]
    assert state(steps)['phase']=='escape'


def test_goal_progress_unchanged_is_not_translation_stall():
    steps=[move('forward',assessment=dict(progress='unchanged'))]*3
    assert state(steps)['stalled_translation_count']==0
    assert state(steps)['phase']!='escape'


@pytest.mark.parametrize('distance',[None,0,10,100])
def test_escape_preserves_front_guard_and_freshness(distance):
    steps=[stalled(),stalled()]
    assert state(steps,distance=distance)['allowed_wheels']==['backward','left','right']
    result=state(steps,distance=distance,age=501)
    assert result['phase']=='observe' and result['allowed_wheels']==[]
