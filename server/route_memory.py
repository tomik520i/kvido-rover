"""Bounded model-inferred landmark memory, with no map or pose inputs.

Labels and landmarks are observations, not a metric map. Exact normalized text
deduplication only merges descriptions; it cannot establish physical identity.
All feedback remains attributed to the model, including blocked/passed claims.
"""
from copy import deepcopy
import re
import unicodedata


def empty_route_memory():
    return dict(active_route_id=None,routes=[],notes=[],next_route_number=1,
                active_place_id='P1',places=[dict(id='P1',label='Výchozí pozorované místo',evidence='',
                    first_seen_step=0,last_seen_step=0,visits=1,provenance='inferred_by_model')],
                next_place_number=2,alternatives=[],alternative_reason='')


def _text(value,limit=500):
    return ' '.join(value.split())[:limit] if isinstance(value,str) else ''


def _normalized(value):
    value=unicodedata.normalize('NFKD',value.casefold())
    return ' '.join(''.join(c for c in value if not unicodedata.combining(c)).split())


def update_route_memory(memory, result, step):
    """Return a detached bounded memory. Unknown IDs never create a route.

    'none' preserves an unfinished active route, but does not attribute fresh
    verifier feedback to it. Only explicitly referenced 'passed' clears active.
    Movement corroboration is visual change, never proof of physical progress.
    """
    state=deepcopy(memory) if isinstance(memory,dict) else empty_route_memory()
    routes=state.get('routes')
    routes=[r for r in routes if isinstance(r,dict) and re.fullmatch(r'R[1-9][0-9]*',str(r.get('id','')))] if isinstance(routes,list) else []
    routes=routes[-12:]
    notes=state.get('notes')
    notes=[_text(n,240) for n in notes if isinstance(n,str)][-8:] if isinstance(notes,list) else []
    active=state.get('active_route_id')
    if active not in {r['id'] for r in routes}:active=None
    number=state.get('next_route_number',1)
    number=number if isinstance(number,int) and not isinstance(number,bool) and number>0 else 1
    number=max(number,1+max((int(r['id'][1:]) for r in routes),default=0))
    places=state.get('places')
    places=[p for p in places if isinstance(p,dict) and re.fullmatch(r'P[1-9][0-9]*',str(p.get('id','')))] if isinstance(places,list) else []
    if not places:places=empty_route_memory()['places']
    current=state.get('active_place_id')
    if current not in {p['id'] for p in places}:current=places[0]['id']
    place_number=state.get('next_place_number',2)
    place_number=place_number if isinstance(place_number,int) and not isinstance(place_number,bool) else 2
    place_number=max(place_number,1+max(int(p['id'][1:]) for p in places))
    for route in routes:
        route.setdefault('source',current);route.setdefault('target',None)
        route.setdefault('visited',route.get('status')=='passed')
        route.setdefault('failures',0);route.setdefault('consecutive_failures',0)
        route.setdefault('exploration_status','visited' if route['visited'] else 'untried')
    result=result if isinstance(result,dict) else {}
    step=step if isinstance(step,int) and not isinstance(step,bool) and step>=0 else 0
    choice=result.get('route_choice')
    choice=choice if isinstance(choice,dict) else {}
    identifier=choice.get('route_id')
    selected=None
    if identifier=='new':
        label=_text(choice.get('label'),120)
        landmark=_text(choice.get('landmark'),200)
        evidence=_text(choice.get('evidence'))
        if label and landmark and evidence:
            # Scene scope: stop comparing descriptions once that route was passed.
            selected=next((r for r in routes if r.get('status')!='passed' and
                           r.get('source')==current and
                           _normalized(r.get('label',''))==_normalized(label) and
                           _normalized(r.get('landmark',''))==_normalized(landmark)),None)
            if selected is None:
                if len(routes)>=12:
                    victim=next((r for r in routes if r['id']!=active),routes[0])
                    routes.remove(victim)
                selected=dict(id=f'R{number}',label=label,landmark=landmark,
                              bearing=choice.get('bearing') if choice.get('bearing') in ('left','ahead','right') else 'unknown',
                              status='candidate',evidence=evidence,first_seen_step=step,last_seen_step=step,
                              attempts=0,provenance='inferred_by_model',feedback_evidence='')
                selected.update(source=current,target=None,visited=False,failures=0,consecutive_failures=0,exploration_status='untried')
                number+=1;routes.append(selected)
            else:
                selected.update(last_seen_step=step,evidence=evidence)
            selected['bearing']=choice.get('bearing') if choice.get('bearing') in ('left','ahead','right') else 'unknown'
            active=selected['id']
        else:notes.append(f'Krok {step}: nový mezicíl nemá label, landmark nebo obrazové evidence.')
    elif identifier not in (None,'none'):
        selected=next((r for r in routes if r['id']==identifier),None)
        if selected is None:notes.append(f'Krok {step}: neznámý route_id {_text(identifier,60)!r} byl ignorován.')
        else:
            if current not in (selected.get('source'),selected.get('target')):
                notes.append(f'Krok {step}: cesta {selected["id"]} není spojena s aktuálním místem {current}; reference ignorována.')
                selected=None
            else:
                active=selected['id'];selected['last_seen_step']=step
        if selected is not None:
            selected['bearing']=choice.get('bearing') if choice.get('bearing') in ('left','ahead','right') else 'unknown'
            if _text(choice.get('evidence')):selected['evidence']=_text(choice['evidence'])
    feedback=result.get('route_feedback')
    feedback=feedback if isinstance(feedback,dict) else {}
    if selected is not None:
        command=result.get('command')
        command=command if isinstance(command,dict) else {}
        action=command.get('action',result.get('action'))
        accepted=result.get('acceptance') in ('accepted','transport_accepted')
        translation=action in ('forward','backward','left','right','forward_left','forward_right','back_left','back_right')
        observation=result.get('motion_observation')
        changed=isinstance(observation,dict) and observation.get('status')=='changed'
        stalled=isinstance(observation,dict) and observation.get('status')=='possible_stall'
        if accepted and translation:selected['attempts']=int(selected.get('attempts',0))+1
        status=feedback.get('status')
        evidence=_text(feedback.get('evidence'))
        if status in ('visible','closer','passed','blocked','lost','unknown'):
            selected['last_feedback_step']=step
            selected['feedback_evidence']=evidence
            selected['provenance']='inferred_by_model'
            if status in ('closer','passed') and not (accepted and translation and changed and evidence):
                # Preserve only the weak observation, never promote an ACK to progress.
                selected['status']='visible' if evidence else 'candidate'
                notes.append(f'Krok {step}: {status} bez přijaté translace, změny obrazu a evidence nebylo potvrzeno.')
            elif status!='unknown':
                selected['status']=status
                if status=='passed':
                    place_id=feedback.get('place_id','unknown')
                    place_evidence=_text(feedback.get('place_evidence'))
                    place_label=_text(feedback.get('place_label'),120)
                    target=next((p for p in places if p['id']==place_id),None)
                    if place_id in ('unknown',None):
                        notes.append(f'Krok {step}: identita místa po passed je neznámá; hrana se nepropojí a aktuální místo se nemění.')
                        selected['status']='visible'
                    elif place_id=='new' and not (place_label and place_evidence):
                        notes.append(f'Krok {step}: nové místo vyžaduje place_label i place_evidence; aktuální místo se nemění.')
                        selected['status']='visible'
                    elif place_id not in ('new','unknown',None) and (target is None or not place_evidence):
                        notes.append(f'Krok {step}: návrat na {str(place_id)[:40]} nemá platnou referenci a identifikační evidenci.')
                        selected['status']='visible'
                    elif target is not None and target['id']==current:
                        notes.append(f'Krok {step}: passed nemůže současně tvrdit stejné aktuální místo {current}.')
                        selected['status']='visible'
                    elif selected.get('target') is not None and (
                        target is None or not place_evidence or
                        target['id']!=(selected['target'] if selected['source']==current else selected['source'])):
                        notes.append(f'Krok {step}: známá hrana {selected["id"]} vyžaduje výslovné rozpoznání druhého místa; identita není potvrzena.')
                        selected['status']='visible'
                    else:
                        if target is None:
                            target=dict(id=f'P{place_number}',label=place_label,
                                        evidence=place_evidence,first_seen_step=step,last_seen_step=step,visits=0,
                                        provenance='inferred_by_model')
                            place_number+=1;places.append(target)
                        target.update(last_seen_step=step,visits=int(target.get('visits',0))+1)
                        if place_evidence:target['evidence']=place_evidence
                        if selected['source']==current:
                            selected['target']=target['id']
                        selected.update(visited=True,exploration_status='visited')
                        selected['traversals']=int(selected.get('traversals',0))+1
                        current=target['id'];active=None
        failed=(accepted and translation and stalled) or (status=='blocked' and bool(evidence))
        if failed:
            selected['failures']+=1;selected['consecutive_failures']+=1
            selected['exploration_status']='blocked' if status=='blocked' and evidence else ('exhausted' if selected['consecutive_failures']>=2 else 'stalled')
        elif accepted and translation and changed and evidence and selected['status'] in ('closer','passed') and status in ('closer','passed'):
            selected['consecutive_failures']=0
            if selected['status']!='passed':selected['exploration_status']='available'
        elif status=='visible' and selected['exploration_status']=='untried':selected['exploration_status']='available'
    adjacent=[r for r in routes if current in (r.get('source'),r.get('target'))]
    alternatives=sorted((r for r in adjacent if r.get('exploration_status') not in ('blocked','stalled','exhausted') and r['id']!=active),
                        key=lambda r:(bool(r.get('visited')),r.get('failures',0),r.get('attempts',0),r['first_seen_step']))
    alternative_reason=_text(state.get('alternative_reason')) if selected is None else ''
    active_edge=next((r for r in routes if r['id']==active),None)
    if active_edge and (active_edge.get('exploration_status') in ('blocked','exhausted') or
                        (active_edge.get('exploration_status')=='stalled' and alternatives)):
        previous=active
        active=alternatives[0]['id'] if alternatives else None
        alternative_reason=f'Cesta {previous} je modelově zablokovaná nebo má pozorování možného uváznutí. '+(
            f'Zkus jinou pozorovanou cestu {active} z místa {current}.' if active else f'V místě {current} zatím není jiná pozorovaná použitelná cesta; hledej alternativu, neopakuj {previous}.')
    # Drop orphaned old nodes after edge retention. Remaining graph has <=13 nodes.
    referenced={current}|{r.get('source') for r in routes}|{r.get('target') for r in routes}
    places=[p for p in places if p['id'] in referenced]
    if len(places)>13:
        # Conservative bound: remove oldest edges until their orphaned nodes can go.
        while len(places)>13 and routes:
            victim=next((r for r in routes if r['id']!=active),routes[0]);routes.remove(victim)
            referenced={current}|{r.get('source') for r in routes}|{r.get('target') for r in routes}
            places=[p for p in places if p['id'] in referenced]
    route_ids={r['id'] for r in routes}
    return dict(active_route_id=active if active in route_ids else None,routes=routes,notes=notes[-8:],next_route_number=number,
                active_place_id=current,places=places,next_place_number=place_number,
                alternatives=[r['id'] for r in alternatives if r['id'] in route_ids and r['id']!=active],alternative_reason=alternative_reason)
