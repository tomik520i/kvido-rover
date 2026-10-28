"""Experimental sensor exploration policy; pure observation of accepted commands.

No pose, map, simulator or model dependencies. Transport acceptance is command
history, not proof of physical movement. Non-wheel actions remain caller-owned.
"""
import math
import re
import unicodedata


ROTATIONS=('rotate_left','rotate_right')
TRANSLATIONS=('forward','backward','left','right','forward_left','forward_right','back_left','back_right')


def _number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def _navigation_goal(goal):
    if not isinstance(goal,str):return False
    normalized=''.join(ch for ch in unicodedata.normalize('NFKD',goal.casefold()) if not unicodedata.combining(ch))
    return bool(re.search(r'\b(najdi|najit|najdete|hledej|hledat|hledani|prozkoumej|prozkoumat|pruzkum|dojed|dojet|jed|jet|find|explore)\b',normalized))


def _accepted_movements(steps):
    result=[]
    for item in steps if isinstance(steps,(list,tuple)) else ():
        if not isinstance(item,dict) or item.get('acceptance') not in ('accepted','transport_accepted'):continue
        command=item.get('command')
        command=command if isinstance(command,dict) else {}
        action=command.get('action',item.get('action'))
        if action not in ROTATIONS+TRANSLATIONS:continue
        speed=command.get('speed')
        duration=command.get('duration_ms')
        if _number(speed) and speed<=0:continue
        if _number(duration) and duration<=0:continue
        observation=item.get('motion_observation')
        # Do not substitute goal-progress='unchanged' for a motion observation.
        status=observation.get('status') if isinstance(observation,dict) else None
        result.append(dict(step=item.get('step') if isinstance(item.get('step'),int) else None,
                           action=action,speed=speed if _number(speed) else None,
                           duration_ms=duration if _number(duration) else None,
                           motion_status=status if status in ('possible_stall','changed','not_comparable') else 'unknown',
                           acceptance=item['acceptance']))
    return result


def exploration_state(goal, completed_steps, telemetry, route_memory=None):
    """Return wheel constraints or None for non-navigation goals.

    allowed_wheels excludes stop/tilt/light by design. A valid telemetry age is
    finite, non-bool and 0..500ms. Fresh null distance permits scanning, never the
    forced-forward transition. Actual distance/authority guards still run later.
    A possible_stall rotation permits reversal; it never resets the eight-turn
    budget since the last transport-accepted translation.
    """
    if not _navigation_goal(goal):return None
    movements=_accepted_movements(completed_steps)
    scans=[]
    stalled_translations=[]
    direction=None
    reversal=False
    for item in movements:
        if item['action'] in TRANSLATIONS:
            scans=[];direction=None;reversal=False
            if item['motion_status']=='changed':stalled_translations=[]
            elif item['motion_status']=='possible_stall':stalled_translations.append(item['action'])
        else:
            scans.append(item)
            if direction is None or reversal:direction=item['action']
            reversal=item['motion_status']=='possible_stall'
    count=len(scans)
    t=telemetry if isinstance(telemetry,dict) else {}
    age=t.get('telemetry_age_ms')
    fresh=_number(age) and 0<=age<=500
    distance=t.get('distance_cm')
    clear_distance=_number(distance) and 60<distance<=400
    state=dict(phase='scan',allowed_wheels=[],scan_direction=direction,scan_steps=count,
               reason='',observations=movements[-8:],stalled_translation_count=len(stalled_translations),
               stalled_directions=list(dict.fromkeys(stalled_translations)))
    if count>=8:
        state.update(phase='exhausted',reason='Osm přijatých otoček bez přijaté translace; průzkum vyčerpal limit. Zastav a vyhodnoť okolí.')
    elif not fresh:
        state.update(phase='observe',reason='Chybí čerstvá telemetrie 0–500 ms; nyní nelze plánovat pohyb kol.')
    elif reversal:
        opposite='rotate_right' if direction=='rotate_left' else 'rotate_left'
        allowed=[opposite,'backward','left','right']
        if count<3 and _number(distance) and 10<distance<=400:allowed.append('forward')
        state.update(allowed_wheels=allowed,scan_direction=opposite,
                     reason='Poslední přijatá otočka má pozorování possible_stall; zkus opačný směr. Celkový limit otoček zůstává.')
    elif count>=3 and clear_distance:
        state.update(phase='advance',allowed_wheels=['forward'],
                     reason='Po nejméně třech přijatých otočkách je čerstvé čidlo nad 60 cm; dalším pohybem kol má být krátký posun vpřed.')
    else:
        allowed=([direction] if direction else list(ROTATIONS))+['backward','left','right']
        if count<3 and _number(distance) and 10<distance<=400:
            allowed.append('forward')
        state.update(allowed_wheels=allowed,
                     reason='Můžeš rovnou jet za viditelným cílem nebo se rozhlížet; při otočkách udrž dosavadní směr.' if count<3 else
                     'Vpředu není čerstvé platné měření nad 60 cm; pokračuj ve stejném směru rozhlížení nebo ustup dozadu či stranou, nejvýše osm otoček.')
    # A chosen visual passage takes precedence over generic panorama scanning.
    # Alignment may need either turn; stale bearings never prove a free path.
    notebook=route_memory if isinstance(route_memory,dict) else {}
    active=next((r for r in notebook.get('routes',[]) if isinstance(r,dict)
                 and r.get('id')==notebook.get('active_route_id')),None)
    if fresh and count<8 and active and active.get('status') not in ('passed','blocked'):
        allowed=['rotate_left','rotate_right','backward','left','right']
        if _number(distance) and 10<distance<=400:allowed.append('forward')
        state.update(phase='route',allowed_wheels=allowed,
            reason=f"Sleduj pozorovaný mezicíl {active.get('id')}: {str(active.get('label',''))[:120]}. "
                   'Potvrď jej novým obrazem a koriguj směr k němu; nezačínej bez důvodu nový panoramatický průzkum.')
    # A changed camera heading is not evidence that a failed translation worked.
    # This recovery constraint also overrides route alignment/panorama preferences.
    if fresh and count<8 and len(stalled_translations)>=2:
        allowed=[a for a in ('backward','left','right') if a not in stalled_translations]
        route_note=f" Mezicíl {active.get('id')}: {str(active.get('label',''))[:120]} zůstává v paměti." if active else ' Původní průzkumný cíl zůstává.'
        state.update(phase='escape' if allowed else 'exhausted',allowed_wheels=allowed,
                     reason=('Nejméně dvě přijaté translace mají possible_stall; otočky neposun nepotvrdily. Zkus dosud nevyzkoušený ústup.' if allowed else
                             'Všechny směry ústupu mají possible_stall; zastav a vyhodnoť uváznutí.')+route_note)
    return state
