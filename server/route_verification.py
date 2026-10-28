"""Keep a panorama direction until its turn and visual check actually succeed.

This module only proposes actions. It never talks to the car. A visual match is
not evidence of free floor and never corrects the metric map's estimated pose.
"""
try:
    from .autopilot_core import TILT, turn_plan, wrap
    from .visual_heading import estimate_heading
except ImportError:
    from autopilot_core import TILT, turn_plan, wrap
    from visual_heading import estimate_heading

MAX_FAILURES = 3


def restart_panorama(memory, reason):
    route = memory.pop('route_goal', None)
    if route:
        memory['last_route_goal'] = dict(route, status='abandoned', reason=reason)
    memory.pop('scan', None)
    memory['scanned_once'] = False
    return dict(actions=[], reason=reason+' Znovu vyberu směr z panoramatu.',
                done=False, verified=False, rescan=True)


def check_route(memory, reference, current, servo, executed, compare, *, measure=estimate_heading):
    """None = no pending route; verified=True = geometry may now plan this frame.

Only explicit executed-forward feedback consumes a successfully checked route.
An omitted/refused movement leaves the direction pending, and each new frame
needs its own comparison before another forward proposal can be returned.
"""
    route = memory.get('route_goal')
    if not route:
        return None
    if route.get('awaiting_forward') and any(a in ('forward', 'climb') and n > 0 for _, n, a in executed):
        memory['last_route_goal'] = dict(route, status='entered')
        memory.pop('route_goal')
        return None
    route['awaiting_forward'] = False
    if any(a == 'backward' and n > 0 for _, n, a in executed):
        return restart_panorama(memory, 'Po couvnutí už původní snímek nepoužiji jako potvrzení směru.')
    if not reference:
        return restart_panorama(memory, 'Chybí referenční snímek vybraného směru.')
    if servo != TILT:
        return dict(actions=[], reason='Pro porovnání směru vrátím kameru do jízdního náklonu.',
                    done=False, verified=False, retilt=True, next_tilt=TILT)
    if route.get('status') == 'aligning':
        delta = wrap(route['heading']-memory.get('heading', 0.))
        if abs(delta) > 10:
            return dict(actions=turn_plan(delta),
                        reason=f'Dokončuji vybranou otočku ({delta:+.0f}°); směr potom ověřím podle uloženého snímku.',
                        done=False, verified=False)
        route['status'] = 'checking'
    # Compare again even if the previous frame matched: its forward command may
    # have been rejected or discarded after a connection change.
    verdict = compare(reference, current)
    geometry = measure(reference, current)
    verdict = dict(verdict, geometry=geometry)
    route['last_check'] = verdict
    route['checks'] = route.get('checks', 0)+1
    # A correction comes from shared image features, NEVER from the language
    # model's guessed left/right. Reobserve after each single 300 ms pulse.
    delta = geometry.get('correction_deg')
    grounded = geometry.get('reliable') is True and type(delta) in (float, int) and abs(delta) <= 40
    if grounded and abs(delta) > 8:
        previous = route.get('correction_error_deg')
        rotated = any(a in ('rotate_left', 'rotate_right') and n > 0 for _, n, a in executed)
        diverged = rotated and previous is not None and abs(delta) >= previous-2
        if route.get('corrections', 0) >= 3 or diverged:
            result = restart_panorama(memory, 'Oprava natočení nepomohla nebo vyčerpala limit.')
            result['verification'] = verdict
            return result
        speed = 130 if abs(delta) < 17 else 150
        direction = 'rotate_left' if delta > 0 else 'rotate_right'
        route.update(status='checking', corrections=route.get('corrections', 0)+1,
                     correction_error_deg=abs(delta))
        return dict(actions=[(speed, 1, direction)], done=False, verified=False, verification=verdict,
                    reason=f'Podle společných bodů obrazu chybí otočit {delta:+.0f}°. Krátce opravím směr a znovu ho porovnám.')
    # A coherent but large discrepancy cannot be waved through by a model match.
    if geometry.get('reliable') and not grounded:
        result = restart_panorama(memory, 'Obraz ukazuje příliš velkou odchylku od vybraného směru.')
        result['verification'] = verdict
        return result
    if verdict.get('error_type'):
        # A busy/unreachable model is not evidence that the chosen route is
        # wrong. Keep its reference while stationary and retry on a new frame.
        return dict(actions=[], reason='Porovnání AI je dočasně nedostupné; držím vybraný směr a čekám na nové ověření.',
                    done=False, verified=False, verification=verdict, retry_comparison=True)
    if verdict.get('status') == 'match' and verdict.get('confidence') == 'high' and not verdict.get('error_type'):
        route.update(status='checking', awaiting_forward=True, attempts=0)
        return dict(actions=None, reason='Aktuální pohled odpovídá vybranému směru. Teď ověřím průjezdnost.',
                    done=False, verified=True, verification=verdict)
    route['attempts'] = route.get('attempts', 0)+1
    route['status'] = 'checking'
    if route['attempts'] >= MAX_FAILURES:
        result = restart_panorama(memory, 'Vybraný směr se nepodařilo vizuálně potvrdit.')
        result['verification'] = verdict
        return result
    # Without reliable geometry, the model's left/right stays diagnostic only.
    return dict(actions=[], reason='Shodu směru zatím nemám potvrzenou; stojím a porovnám nový snímek.',
                done=False, verified=False, verification=verdict)
