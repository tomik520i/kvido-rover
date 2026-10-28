"""Offline observer audit; never imported into a navigation/controller path."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path


def _accepted_candidate_stop(row):
    command = row.get('command') or {}
    return bool(row.get('action') == 'stop' and command.get('action') == 'stop'
                and command.get('acceptance') == 'accepted'
                and row.get('candidate_goal_reached') is True)


def summarize(rows, latest=None):
    latest = latest or {}
    if not rows:
        return {'steps': 0, 'position_success': False, 'autonomous_termination_confirmed': False,
                'verified_success': False, 'conclusion': 'No recorded movement.'}
    first, last = rows[0], rows[-1]
    world = latest.get('observer') or last['observer']
    checkpoints = [bool(item['visited']) for item in world.get('checkpoints', [])]
    position_success = bool(world.get('goal_reached') and not world.get('crossed_threshold')
                            and all(checkpoints) and world.get('speed') == 0)
    terminal_stops = len(rows) >= 2 and all(_accepted_candidate_stop(row) for row in rows[-2:])
    consecutive = (len(rows) >= 2 and type(last.get('step')) is int
                   and type(rows[-2].get('step')) is int and rows[-2]['step']+1 == last['step'])
    termination_checks = dict(completed_status=latest.get('status') == 'completed',
        two_accepted_candidate_stops=terminal_stops,
        consecutive_recorded_steps=consecutive,
        final_status_matches_recorded_step=latest.get('step') == last.get('step'))
    autonomous_termination = all(termination_checks.values())
    success = position_success and autonomous_termination
    previous_contacts = 0
    contacts = []
    candidates = []
    actions = Counter()
    statuses = Counter()
    errors = []
    initial_world = first['observer']['pose']
    initial_estimate = first['navigator']['estimated_pose']
    yaw_offset = initial_world['heading'] - initial_estimate['heading']
    cosine, sine = math.cos(yaw_offset), math.sin(yaw_offset)
    origin_x = initial_world['x'] - cosine*initial_estimate['x'] + sine*initial_estimate['y']
    origin_y = initial_world['y'] - sine*initial_estimate['x'] - cosine*initial_estimate['y']
    for row in rows:
        observer, navigator = row['observer'], row['navigator']
        actions[row['action']] += 1
        statuses[row.get('motion_observation', {}).get('status', 'unknown')] += 1
        if observer['collisions'] > previous_contacts:
            contacts.append({'step': row['step'], 'command': row['command'],
                'new_contacts': observer['collisions'] - previous_contacts,
                'motion_observation': row.get('motion_observation')})
        previous_contacts = observer['collisions']
        if row.get('candidate_goal_reached'):
            candidates.append({'step': row['step'], 'observer_goal_reached': observer['goal_reached']})
        estimate, pose = navigator['estimated_pose'], observer['pose']
        projected_x = origin_x + cosine*estimate['x'] - sine*estimate['y']
        projected_y = origin_y + sine*estimate['x'] + cosine*estimate['y']
        errors.append(math.hypot(projected_x-pose['x'], projected_y-pose['y']))
    return {
        'offline_observer_only': True,
        'status': latest.get('status', 'unknown'),
        'steps': len(rows), 'perception_calls': latest.get('perception_calls'),
        'travelled_m': world['travelled'], 'collisions': world['collisions'],
        'checkpoints': checkpoints, 'goal_reached': world['goal_reached'],
        'crossed_threshold': world['crossed_threshold'],
        'position_success': position_success,
        'autonomous_termination_confirmed': autonomous_termination,
        'termination_checks': termination_checks,
        'verified_success': success,
        'conclusion': 'Independent simulator and two accepted terminal STOP candidates confirm autonomous stopped arrival before the threshold.' if success
                      else 'Stopped position is satisfied, but autonomous completion is NOT demonstrated.' if position_success
                      else 'Autonomous stopped arrival is NOT demonstrated.',
        'actions': dict(actions), 'motion_statuses': dict(statuses),
        'new_contact_events': contacts, 'goal_candidates': candidates,
        'max_translation_estimate_error_m': max(errors),
        'final_translation_estimate_error_m': errors[-1],
        'pose_error_note': 'Observer-only initial SE2 alignment; not supplied to the controller. Translation drift only.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in (args.report/'timeline.jsonl').read_text(encoding='utf-8').splitlines()]
    latest = json.loads((args.report/'latest.json').read_text(encoding='utf-8'))
    result = summarize(rows, latest)
    destination = args.report/'audit.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('steps','travelled_m','collisions','checkpoints','verified_success','conclusion')}, ensure_ascii=False))
    print(destination)


if __name__ == '__main__':
    main()
