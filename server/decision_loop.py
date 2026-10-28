"""Reject repeated proposals before issuing any actuator command."""
import math

from .motion_policy import AI_WHEELS

WHEELS = set(AI_WHEELS)


def same_proposal(left, right):
    if not left or not right or left.get('action') != right.get('action'):
        return False
    if left.get('action') == 'tilt':
        a, b = left.get('tilt'), right.get('tilt')
        return (type(a) in (int, float) and type(b) in (int, float)
                and math.isfinite(a) and math.isfinite(b) and abs(a-b) <= 3)
    # Changing PWM or duration does not make the same direction a new strategy.
    return True


def duplicate_feedback(proposal, recent, telemetry):
    action = proposal.get('action')
    if action == 'stop' or not recent:
        return None  # Stopping remains available, including explicit user STOP.
    if action in WHEELS and (telemetry.get('motion_observation') or {}).get('status')=='changed':
        return None  # A fresh changed scene is progress, not the same stuck state.
    label = f"tilt na {proposal.get('tilt')}°" if action == 'tilt' else action
    reason = None
    if action == 'tilt' and same_proposal(proposal, {'action':'tilt','tilt':telemetry.get('servo')}):
        reason = 'kamera už v tomto úhlu je'
    if action in ('light_on', 'light_off') and telemetry.get('light_on') is (action == 'light_on'):
        reason = 'světlo už je v požadovaném stavu'
    for entry in reversed(recent):
        observed=entry.get('observed_at_execution') or {}
        old_servo=observed.get('servo');new_servo=telemetry.get('servo')
        if (entry.get('executed')=='stop' and type(old_servo) in (int,float)
                and type(new_servo) in (int,float) and abs(old_servo-new_servo)>3):
            # A blocked driving proposal may become usable after a real change
            # of view (e.g. the operator has now aimed at the calibrated floor).
            continue
        prior = entry.get('proposed_command') or entry.get('command') or {'action':entry.get('proposed')}
        if same_proposal(proposal, prior):
            if (entry.get('executed') == action and action in WHEELS
                    and (entry.get('motion_observation') or {}).get('status') == 'changed'):
                # Looking down or switching the light after successful motion
                # must not turn that verified motion into a stuck attempt.
                # Newer rejected proposals are visited first and still block.
                break
            reason = 'stejný návrh už byl vyzkoušen nebo odmítnut'
            break
        if entry.get('executed') in WHEELS:
            break  # An accepted wheel command starts a new observation context.
    if reason is None:
        return None
    return (f'ŘADIČ ODMÍTL opakovaný návrh {label}: {reason}. '
            'TENTO NÁVRH NEBYL PROVEDEN. Už jsi to explicitně zkoušel; '
            'zvol jinou věcnou akci, ne pouze jinou rychlost či délku. '
            'Návrat mezi stejnými úhly není pokrok. Vyhodnoť nový snímek, '
            'původní úkol stále platí. Pokud nevidíš bezpečnou alternativu, '
            'zvol stop a konkrétně popiš překážku. Bezpečnostní podmínky zůstávají stejné.')
