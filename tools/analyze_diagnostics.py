"""Offline JSONL diagnostic analysis; never connects to a rover or AI service.

Usage: python tools/analyze_diagnostics.py trace.jsonl --output-prefix reports/run
Writes run.json and run.md. All counts are trace observations unless explicitly
labelled firmware counters. Transport acceptance is not physical displacement.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys


COUNTER_NAMES = {'connections', 'disconnects', 'connect_attempts', 'error_events'}
MOTION_ACTIONS = {'forward', 'backward', 'left', 'right', 'forward_left',
                  'forward_right', 'back_left', 'back_right', 'rotate_left', 'rotate_right'}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def safe_text(value, limit=700):
    """Only selected scalar fields leave the analyzer; redact accidental secrets."""
    if not isinstance(value, (str, int, float, bool)):
        return ''
    text = str(value)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', text)
    text = re.sub(r'(?i)(\b(?:api[_-]?key|key|token|password|secret|authorization)\s*[=:]\s*)[^\s&,;]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'(https?://|wss?://)[^/\s:@]+:[^/\s@]+@', r'\1[REDACTED]@', text)
    return text[:limit]


def timestamp(record):
    value = record.get('at_ms')
    return value if number(value) and value >= 0 else None


def iso(milliseconds):
    if milliseconds is None:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def latency(values):
    values = sorted(value for value in values if number(value) and value >= 0)
    if not values:
        return {'count': 0, 'min_ms': None, 'median_ms': None, 'p95_ms': None, 'max_ms': None}
    return {'count': len(values), 'min_ms': values[0], 'median_ms': statistics.median(values),
            'p95_ms': values[math.ceil(.95 * len(values)) - 1], 'max_ms': values[-1]}


def load_jsonl(paths):
    records, issues, seen = [], [], set()
    duplicates = 0
    for path in map(Path, paths):
        with path.open(encoding='utf-8-sig') as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                if len(line) > 1_000_000:
                    issues.append({'file': safe_text(path.name), 'line': line_number, 'reason': 'line_too_large'})
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    issues.append({'file': safe_text(path.name), 'line': line_number, 'reason': 'invalid_json'})
                    continue
                if not isinstance(record, dict) or not isinstance(record.get('event'), str):
                    issues.append({'file': safe_text(path.name), 'line': line_number, 'reason': 'not_an_event_object'})
                    continue
                if record.get('v', 1) != 1:
                    issues.append({'file': safe_text(path.name), 'line': line_number, 'reason': 'unsupported_version'})
                    continue
                # Repeated files/rotation overlap must not double-count a trace.
                # Without seq we cannot safely identify a duplicate observation.
                if type(record.get('seq')) is int and timestamp(record) is not None:
                    fingerprint = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).digest()
                    if fingerprint in seen:
                        duplicates += 1
                        continue
                    seen.add(fingerprint)
                records.append(record)
    return records, {'malformed_or_unsupported': issues, 'duplicates_skipped': duplicates}


def _action(value):
    if isinstance(value, dict):
        value = value.get('action')
    return safe_text(value or 'unknown', 80)


def _telemetry_class(t):
    distance, age = t.get('distance_cm'), t.get('telemetry_age_ms')
    if t.get('connected') is False:
        return 'offline'
    if distance is None:
        return 'null_distance'
    if not number(distance) or not 0 < distance <= 400:
        return 'invalid_distance'
    if not number(age) or age < 0:
        return 'unknown_age'
    return 'valid' if age <= 500 else 'stale'


def _firmware_counters(telemetry):
    for name in ('wifi_disconnect_count','wifi_retry_count','ultrasonic_valid_count','ultrasonic_invalid_count','ultrasonic_timeout_count','ultrasonic_measurements'):
        value=telemetry.get(name)
        if type(value) is int and value>=0:
            yield name,value
    for section in ('relay', 'firmware_counters', 'sonar_counters', 'ultra', 'cmd', 'drive', 'motor'):
        values = telemetry.get(section)
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            explicit={'ultra':{'seq','null'},'cmd':{'seq'},'drive':{'seq','accepted','denied'},'motor':{'null_stops','near_stops','deadline_stops'}}
            if (name in explicit.get(section,set()) or (section not in explicit and (name in COUNTER_NAMES or name.endswith(('_count', '_total', '_events'))))):
                if type(value) is int and value >= 0:
                    yield section + '.' + safe_text(name, 80), value


def _analyze_records(records):
    indexed = list(enumerate(records))
    indexed.sort(key=lambda item: (timestamp(item[1]) is None, timestamp(item[1]) or 0, item[0]))
    records = [record for _, record in indexed]
    times = [timestamp(record) for record in records if timestamp(record) is not None]
    event_counts, telemetry_classes = Counter(), Counter()
    motor_counts, reasons, uno_reasons = Counter(), Counter(), Counter()
    ultra_samples, drive_samples = Counter(), Counter()
    proposal_actions, gate_actions, acceptance_events = Counter(), Counter(), Counter()
    assessment_statuses, assessment_progress, motion_statuses, frame_roles = Counter(), Counter(), Counter(), Counter()
    model_latencies, transport_latencies, assessment_latencies = [], [], []
    planning_categories, planning_reasons, replan_reasons = Counter(), Counter(), Counter()
    counters, commands, tasks = {}, {}, {}
    disconnects, progress, gates, proposals, motor_timeline = [], [], [], [], []
    frame_pairs = defaultdict(dict)
    motor_field_samples = active_samples = fresh_active_samples = contradictory_samples = 0
    null_and_stale = 0
    explicit_uno_reason_samples = 0
    for record in records:
        event = safe_text(record.get('event'), 80)
        event_counts[event] += 1
        at = timestamp(record)
        task_id = safe_text(record.get('task_id'), 100)
        command_id = safe_text(record.get('command_id'), 100)
        step = record.get('step') if type(record.get('step')) is int else None
        if task_id:
            task = tasks.setdefault(task_id, {'task_id': task_id, 'statuses_observed': [], 'latest_status': None,
                                             'highest_step_label': None, 'completed_step_count':None, 'last_reason': '', 'last_outcome': ''})
            if step is not None:
                task['highest_step_label'] = max(task['highest_step_label'] or 0, step)
        if event == 'telemetry':
            t = record.get('telemetry')
            if not isinstance(t, dict):
                telemetry_classes['missing_payload'] += 1
                continue
            category = _telemetry_class(t)
            telemetry_classes[category] += 1
            age = t.get('telemetry_age_ms')
            if t.get('distance_cm') is None and number(age) and age > 500:
                null_and_stale += 1
            active, motor_byte, motor = t.get('motor_active'), t.get('motor_byte'), t.get('motor')
            nested_motor = motor if isinstance(motor,dict) else {}
            if type(active) is not bool and type(nested_motor.get('active')) is bool:
                active = nested_motor['active']
            ultra = t.get('ultra') if isinstance(t.get('ultra'),dict) else {}
            for field in ('pre','post','ok'):
                if type(ultra.get(field)) is bool:
                    ultra_samples[field + ('_true' if ultra[field] else '_false')] += 1
            drive = t.get('drive') if isinstance(t.get('drive'),dict) else {}
            if type(drive.get('ok')) is bool:
                drive_samples['last_drive_ok_true' if drive['ok'] else 'last_drive_ok_false'] += 1
            for section, values, field in [('drive',drive,'why'),('motor',nested_motor,'stop')]:
                if values.get(field) is not None:
                    explicit_uno_reason_samples += 1
                    uno_reasons[section+'.'+field+': '+safe_text(values[field])] += 1
            if type(active) is bool or type(motor_byte) is int or isinstance(motor, str):
                motor_field_samples += 1
                motor_timeline.append({'at_ms': at, 'task_id': task_id or None, 'step': step,
                                       'motor_active': active if type(active) is bool else None,
                                       'motor_byte': motor_byte if type(motor_byte) is int else None,
                                       'fresh_telemetry': number(age) and 0 <= age <= 500 and t.get('connected') is not False})
                if type(active) is bool:
                    motor_counts['motor_active_true' if active else 'motor_active_false'] += 1
                if type(motor_byte) is int:
                    motor_counts['motor_byte_nonzero' if motor_byte != 0 else 'motor_byte_zero'] += 1
                if isinstance(motor, str):
                    motor_counts['motor_label_' + safe_text(motor, 60)] += 1
                observed_active = active is True or (type(motor_byte) is int and motor_byte != 0) or (isinstance(motor, str) and motor in MOTION_ACTIONS)
                if observed_active:
                    active_samples += 1
                    if number(age) and 0 <= age <= 500 and t.get('connected') is not False:
                        fresh_active_samples += 1
                if type(active) is bool and type(motor_byte) is int and active != (motor_byte != 0):
                    contradictory_samples += 1
            for field in ('stop_reason', 'refusal_reason', 'safety_reason', 'uno_stop_reason', 'uno_refusal_reason'):
                if field in t and t[field] is not None:
                    explicit_uno_reason_samples += 1
                    uno_reasons[field + ': ' + safe_text(t[field])] += 1
            for name, value in _firmware_counters(t):
                counter = counters.setdefault(name, {'first': value, 'last': value, 'snapshots': 0,
                                                     'observed_nonnegative_increments': 0, 'decreases_or_resets': 0})
                if counter['snapshots']:
                    if value >= counter['last']:
                        counter['observed_nonnegative_increments'] += value - counter['last']
                    else:
                        counter['decreases_or_resets'] += 1
                counter['last'] = value
                counter['snapshots'] += 1
        elif event == 'model_proposal':
            proposal_actions[_action(record.get('proposal'))] += 1
            proposal = record.get('proposal') if isinstance(record.get('proposal'), dict) else {}
            proposals.append({'at_ms': at, 'task_id': task_id or None, 'step': step,
                              'attempt': record.get('attempt') if type(record.get('attempt')) is int else None,
                              'action': _action(proposal), 'speed': proposal.get('speed') if number(proposal.get('speed')) else None,
                              'duration_ms': proposal.get('duration_ms') if number(proposal.get('duration_ms')) else None,
                              'reason': safe_text(proposal.get('reason')), 'evidence':safe_text(proposal.get('evidence')), 'goal_status': safe_text(proposal.get('goal_status')),
                              'provider':safe_text(record.get('provider'),80),'model':safe_text(record.get('model'),100),
                              'latency_ms':record.get('latency_ms') if number(record.get('latency_ms')) else None})
            model_latencies.append(record.get('latency_ms'))
        elif event == 'planning_telemetry':
            sample=record.get('telemetry')
            planning_categories[_telemetry_class(sample) if isinstance(sample,dict) else 'missing_payload']+=1
            if record.get('reason'):planning_reasons[safe_text(record['reason'])]+=1
        elif event == 'model_replan':
            replan_reasons[safe_text(record.get('feedback') or 'unknown')]+=1
        elif event == 'gate':
            proposed, executed = _action(record.get('proposal')), _action(record.get('executed'))
            gate_actions[executed] += 1
            reason = safe_text(record.get('reason'))
            refusal = proposed != 'stop' and (record.get('permitted') is False or executed == 'stop')
            if reason and (refusal or executed == 'stop'):
                reasons[reason] += 1
            gates.append({'at_ms': at, 'task_id': task_id or None, 'step': step,
                          'command_id': command_id or None, 'proposed': proposed,
                          'executed': executed, 'permitted': record.get('permitted') if type(record.get('permitted')) is bool else None,
                          'refused_or_stopped': refusal, 'reason': reason,
                          'distance_category': _telemetry_class(record['telemetry']) if isinstance(record.get('telemetry'), dict) else 'unknown'})
        elif event in ('command_request', 'command_result'):
            key = command_id or f'uncorrelated-{len(commands)}'
            command = commands.setdefault(key, {'command_id': command_id or None, 'action': 'unknown',
                                                 'requested_at_ms': None, 'result_at_ms': None,
                                                 'acceptance': None, 'result_reason': '', 'request_observed': False, 'category': '', 'status_code': None})
            if event == 'command_request':
                command['action'] = _action(record.get('action', record.get('command')))
                command['requested_at_ms'] = at
                command['request_observed'] = True
                for field in ('speed', 'duration_ms', 'tilt'):
                    command[field] = record.get(field) if number(record.get(field)) else None
            else:
                acceptance = safe_text(record.get('acceptance') or 'unknown', 80)
                acceptance_events[acceptance] += 1
                command.update(acceptance=acceptance, result_at_ms=at,
                               category=safe_text(record.get('category'),80),
                               status_code=record.get('status_code') if type(record.get('status_code')) is int else None,
                               result_reason=safe_text(record.get('reason') or record.get('error') or record.get('category') or ''))
                if number(record.get('latency_ms')):
                    command['latency_ms'] = record['latency_ms']
        elif event == 'relay_link':
            relay=record.get('relay') if isinstance(record.get('relay'),dict) else {}
            last_disconnect=relay.get('last_disconnect') if isinstance(relay.get('last_disconnect'),dict) else {}
            disconnects.append({'at_ms': at, 'at_utc': iso(at),
                                'connected': record.get('connected') if type(record.get('connected')) is bool else None,
                                'reason': safe_text(record.get('reason') or record.get('error') or last_disconnect.get('reason') or relay.get('last_error') or '')})
        elif event == 'task_phase' and task_id:
            if type(record.get('completed_step_count')) is int:
                task['completed_step_count']=record['completed_step_count']
            status = safe_text(record.get('status'), 80)
            if status:
                task['latest_status'] = status
                if status not in task['statuses_observed']:
                    task['statuses_observed'].append(status)
            task['last_reason'] = safe_text(record.get('reason') or task['last_reason'])
            task['last_outcome'] = safe_text(record.get('outcome') or task['last_outcome'])
        elif event == 'assessment':
            assessment_latencies.append(record.get('latency_ms'))
            assessment = record.get('assessment') if isinstance(record.get('assessment'), dict) else record
            status, changed = safe_text(assessment.get('status'), 80), safe_text(assessment.get('progress'), 80)
            assessment_statuses[status or 'unknown'] += 1
            assessment_progress[changed or 'unknown'] += 1
            observation = record.get('motion_observation')
            motion = safe_text(observation.get('status'), 80) if isinstance(observation, dict) else ''
            if motion:
                motion_statuses[motion] += 1
            progress.append({'task_id': task_id or None, 'step': step, 'at_ms': at,
                             'model_assessment_status': status or None, 'model_assessment_progress': changed or None,
                             'motion_observation': motion or None, 'summary': safe_text(assessment.get('summary')),
                             'evidence': safe_text(assessment.get('evidence')),
                             'before_sha256':record.get('before_sha256') if isinstance(record.get('before_sha256'),str) and re.fullmatch(r'[a-fA-F0-9]{64}',record['before_sha256']) else None,
                             'after_sha256':record.get('after_sha256') if isinstance(record.get('after_sha256'),str) and re.fullmatch(r'[a-fA-F0-9]{64}',record['after_sha256']) else None})
        elif event == 'frame':
            role = safe_text(record.get('role'), 30)
            frame_roles[role or 'unknown'] += 1
            digest = record.get('sha256')
            if task_id and step is not None and role in ('before', 'after') and isinstance(digest, str) and re.fullmatch(r'[a-fA-F0-9]{64}', digest):
                frame_pairs[(task_id, step)][role] = digest.lower()
    for command in commands.values():
        duration = command.get('latency_ms')
        if duration is None and command['requested_at_ms'] is not None and command['result_at_ms'] is not None:
            duration = command['result_at_ms'] - command['requested_at_ms']
        if number(duration) and duration >= 0:
            transport_latencies.append(duration)
        command['latency_ms'] = duration if number(duration) and duration >= 0 else None
    requested = [command for command in commands.values() if command['request_observed']]
    paired = [pair for pair in frame_pairs.values() if 'before' in pair and 'after' in pair]
    known = [f'Zaznamenáno {len(records)} událostí, {event_counts["telemetry"]} telemetrických událostí a {event_counts["model_proposal"]} návrhů modelu.',
             f'Transport potvrdil {sum(command["acceptance"] == "transport_accepted" for command in requested)} korelovaných požadavků jako transport_accepted.',
             f'V telemetrii bylo pozorováno {active_samples} vzorků aktivního motorového stavu, z toho {fresh_active_samples} čerstvých.']
    unknown = ['Firmware drive.accepted/denied jsou výslovné čítače přijetí/odmítnutí UNO, nikoli odometrie; drive.ok a motor.active jsou vzorkované stavy.',
               'Přijetí transportem není potvrzení vykonání UNO ani fyzického pohybu, ujeté vzdálenosti či dosažení cíle.',
               'Nula pozorovaných aktivních vzorků není důkaz neprovedení: krátký impulz může být mezi vzorky; pacing logu může další vzorky vynechat.',
               'Modelový text, assessment.completed a serverový task.completed samy o sobě nejsou nezávislým potvrzením dojetí k prahu.',
               'Změna snímků může být náklon, světlo nebo expozice; sama nepotvrzuje fyzický posun.']
    if not counters:
        unknown.append('Trace neobsahuje číselné firmware countery; počty logovaných vzorků nejsou počty skutečných měření firmwaru.')
    if not explicit_uno_reason_samples:
        unknown.append('Konkrétní důvody odmítnutí/zastavení UNO nebyly ve vzorcích dostupné; nelze je odvodit z důvodu serverové brány.')
    return {'schema_version': 1, 'offline_only': True, 'window': {'start_ms': min(times) if times else None,
             'end_ms': max(times) if times else None, 'start_utc': iso(min(times)) if times else None,
             'end_utc': iso(max(times)) if times else None, 'duration_s': (max(times)-min(times))/1000 if times else None},
            'event_counts': dict(event_counts), 'telemetry': {'sample_count': event_counts['telemetry'],
             'classes': dict(telemetry_classes), 'null_also_stale_samples': null_and_stale,
             'sample_counts_are_firmware_counters': False, 'ultra_boolean_sample_counts': dict(ultra_samples),
             'note': 'Vzorky pouze z event=telemetry; vložená telemetrie v gate není další vzorek. Logování může být omezené na 5 Hz.'},
            'firmware_counters': counters, 'firmware_counter_note': 'Výhradně čítače výslovně dodané zařízením; přírůstky jsou mezi dostupnými snapshoty. Pokles může být reset/wrap; počáteční a mezery v měřeních zůstávají neznámé.',
            'model': {'proposal_event_count': event_counts['model_proposal'], 'proposal_actions': dict(proposal_actions), 'latency': latency(model_latencies), 'timeline': proposals},
            'planning': {'telemetry_event_count':event_counts['planning_telemetry'],'telemetry_classes':dict(planning_categories),'reasons':dict(planning_reasons),'replan_event_count':event_counts['model_replan'],'replan_feedback':dict(replan_reasons)},
            'gate': {'event_count': event_counts['gate'], 'executed_action_labels': dict(gate_actions),
                     'refused_or_stopped_proposals': sum(row['refused_or_stopped'] for row in gates), 'reasons': dict(reasons), 'timeline': gates},
            'transport': {'request_event_count': event_counts['command_request'], 'result_event_count': event_counts['command_result'],
                          'acceptance_event_counts': dict(acceptance_events), 'correlated_requests': len(requested),
                          'accepted_requests': sum(command['acceptance']=='transport_accepted' for command in requested),
                          'unknown_requests': sum(command['acceptance']=='unknown' for command in requested),
                          'rejected_requests': sum(command['acceptance']=='transport_rejected' for command in requested),
                          'missing_results': sum(command['acceptance'] is None for command in requested),
                          'orphan_results': sum(not command['request_observed'] for command in commands.values()),
                          'latency': latency(transport_latencies), 'commands': list(commands.values()), 'physical_motion_confirmed': None},
            'uno_observations': {'motor_field_samples': motor_field_samples, 'active_samples': active_samples,
                                 'fresh_active_samples': fresh_active_samples, 'contradictory_samples': contradictory_samples,
                                 'field_counts': dict(motor_counts), 'last_drive_boolean_sample_counts': dict(drive_samples), 'timeline': motor_timeline, 'explicit_reason_samples': explicit_uno_reason_samples,
                                 'reasons': dict(uno_reasons), 'physical_displacement_confirmed': None},
            'relay_timeline': disconnects, 'tasks': list(tasks.values()),
            'completion': {'latest_server_completed_tasks': sum(task['latest_status']=='completed' for task in tasks.values()),
                           'model_assessment_status_counts': dict(assessment_statuses), 'target_reached_independently_confirmed': None},
            'before_after': {'model_progress_counts': dict(assessment_progress), 'motion_observation_counts': dict(motion_statuses),
                             'frame_events_by_role': dict(frame_roles), 'paired_frame_steps': len(paired),
                             'identical_encoded_frame_pairs': sum(pair['before']==pair['after'] for pair in paired), 'assessments': progress,'assessment_latency':latency(assessment_latencies)},
            'known': known, 'unknown': unknown}


def analyze(records):
    """Keep simulation, proven live origin and unknown-origin evidence separate."""
    records=list(records)
    ordered=sorted(enumerate(records),key=lambda item:(timestamp(item[1]) is None,timestamp(item[1]) or 0,item[0]))
    modes,by_task,by_command={},defaultdict(set),defaultdict(set)
    current=None
    for index,record in ordered:
        explicit=record.get('simulation') if type(record.get('simulation')) is bool else None
        t=record.get('telemetry')
        nested=t.get('simulation') if isinstance(t,dict) and type(t.get('simulation')) is bool else None
        if explicit is not None and nested is not None and explicit!=nested:
            modes[index]='conflicting';continue
        own=explicit if explicit is not None else nested
        if record.get('event') in ('startup','settings') and own is not None:current=own
        mode=own if own is not None else current
        modes[index]=mode
        if own is not None:
            for field,mapping in [('task_id',by_task),('command_id',by_command)]:
                value=record.get(field)
                if isinstance(value,(str,int)) and not isinstance(value,bool):mapping[str(value)].add(own)
    groups={'live':[],'simulated':[],'unknown':[],'conflicting':[]}
    for index,record in enumerate(records):
        mode=modes[index]
        if mode is None:
            hints=set()
            for field,mapping in [('command_id',by_command),('task_id',by_task)]:
                value=record.get(field)
                if isinstance(value,(str,int)) and not isinstance(value,bool):hints.update(mapping.get(str(value),set()))
            if len(hints)==1:mode=next(iter(hints))
            elif len(hints)>1:mode='conflicting'
        scope='simulated' if mode is True else 'live' if mode is False else 'conflicting' if mode=='conflicting' else 'unknown'
        groups[scope].append(record)
    # Unlabelled legacy traces remain analyzable, but their hardware origin is
    # explicitly unknown. A mixed/simulation trace never leaks simulated counts
    # into the primary live-hardware evidence.
    primary='live' if groups['live'] or groups['simulated'] else 'unknown'
    report=_analyze_records(groups['live'] if primary=='live' else groups['unknown']+groups['conflicting'])
    report['primary_scope']=primary
    report['scope_counts']={scope:len(rows) for scope,rows in groups.items()}
    report['scope_note']='Živý původ vyžaduje simulation=false (přímo, ve startup/settings nebo přes task/command korelaci). Simulace se nikdy nepočítá jako pozorování UNO. Bez označení je původ neznámý.'
    report['simulation_analysis']=_analyze_records(groups['simulated']) if groups['simulated'] else None
    unknown=groups['unknown']+groups['conflicting']
    report['unknown_origin_analysis']=_analyze_records(unknown) if primary=='live' and unknown else None
    if primary=='unknown':report['unknown'].append('Původ těchto událostí není doložen jako živý hardware; motorové vzorky ani čítače zde nelze vydávat za pozorování fyzického UNO.')
    if groups['simulated']:report['unknown'].append('Simulované události jsou oddělené v simulation_analysis a nejsou důkazem chování skutečného auta.')
    return report


def _md(value):
    return safe_text(value).replace('|', '\\|').replace('\n', ' ')


def markdown(report):
    w, t = report['window'], report['telemetry']
    lines = ['# Offline vyhodnocení diagnostiky Kvída', '',
             f"Časové okno: {w['start_utc'] or 'neznámé'} → {w['end_utc'] or 'neznámé'}; délka {w['duration_s'] if w['duration_s'] is not None else 'neznámá'} s.", '',
             'Rozsah hlavního vyhodnocení: **'+report.get('primary_scope','unknown')+'**. '+report.get('scope_note',''), '',
             'Rozdělení vstupu: '+json.dumps(report.get('scope_counts',{}),ensure_ascii=False)+'.', '', '## Co záznam přímo dokládá', '']
    lines += ['- '+item for item in report['known']]
    lines += ['', '## Telemetrie: vzorky, nikoli počty měření čidla', '', '| Kategorie | Vzorky |', '|---|---:|']
    lines += [f'| {_md(key)} | {count} |' for key, count in sorted(t['classes'].items())]
    lines += ['', t['note'], '', '## Návrh → brána → transport → UNO', '',
              f"Návrhy modelu: {report['model']['proposal_event_count']}; rozhodnutí brány: {report['gate']['event_count']}; žádosti transportu: {report['transport']['request_event_count']}.",
              f"Transport accepted: {report['transport']['accepted_requests']}; rejected: {report['transport']['rejected_requests']}; unknown: {report['transport']['unknown_requests']}; bez výsledku: {report['transport']['missing_results']}.",
              f"Pozorované aktivní motorové vzorky: {report['uno_observations']['active_samples']}. Fyzický posun: **nepotvrzen tímto logem**.", '',
              '| Čas UTC | Návrh | Brána | Důvod |', '|---|---|---|---|']
    lines += [f"| {iso(row['at_ms']) or 'neznámý'} | {_md(row['proposed'])} | {_md(row['executed'])} | {_md(row['reason'])} |" for row in report['gate']['timeline']]
    lines += ['', '### Zdůvodnění návrhů AI', '', '| Čas UTC / krok | Návrh | Zdůvodnění AI | Podklady uvedené AI |', '|---|---|---|---|']
    lines += [f"| {iso(row['at_ms']) or 'neznámý'} / {row['step']} | {_md(row['action'])} | {_md(row['reason'])} | {_md(row['evidence'])} |" for row in report['model']['timeline']]
    lines += ['', '### Důvody serveru', '']
    lines += [f'- {_md(reason)} ({count}×)' for reason, count in report['gate']['reasons'].items()] or ['- Nezaznamenané.']
    lines += ['', '### Důvody UNO', '']
    lines += [f'- {_md(reason)} ({count}×)' for reason, count in report['uno_observations']['reasons'].items()] or ['- Neznámé; serverový důvod nenahrazuje důvod UNO.']
    lines += ['', '## Latence', '', '| Fáze | N | Medián ms | P95 ms | Maximum ms |', '|---|---:|---:|---:|---:|']
    for name, stats in [('Model', report['model']['latency']), ('Transport request/result', report['transport']['latency']),('Ověřovatel před/po',report['before_after']['assessment_latency'])]:
        lines.append(f"| {name} | {stats['count']} | {stats['median_ms']} | {stats['p95_ms']} | {stats['max_ms']} |")
    lines += ['', '## Odpojení a obnovení', '']
    lines += [f"- {row['at_utc'] or 'neznámý čas'}: {'připojeno' if row['connected'] is True else 'odpojeno' if row['connected'] is False else 'neznámý stav'}; {_md(row['reason'])}" for row in report['relay_timeline']] or ['- Žádné link události v dostupném okně; to samo nepotvrzuje nepřerušené spojení.']
    lines += ['', '## Firmware countery', '', report['firmware_counter_note'], '']
    lines += [f"- {_md(name)}: první {counter['first']}, poslední {counter['last']}, sledované přírůstky {counter['observed_nonnegative_increments']}, poklesy/reset {counter['decreases_or_resets']}." for name, counter in report['firmware_counters'].items()] or ['- Nedostupné; nepřevádět počty logovaných vzorků na počty měření firmwaru.']
    lines += ['', '## Úkol a porovnání před/po', '',
              f"Serverem hlášené dokončené úkoly: {report['completion']['latest_server_completed_tasks']}. Nezávislé potvrzení dojetí k cíli: **neznámé**.", '',
              '| Úkol / krok | Modelové vyhodnocení | Změna | Shrnutí |', '|---|---|---|---|']
    lines += [f"| {_md(row['task_id'])} / {row['step']} | {_md(row['model_assessment_status'])} | {_md(row['model_assessment_progress'])} | {_md(row['summary'])} |" for row in report['before_after']['assessments']]
    lines += ['', '## Co zůstává neznámé', '']
    lines += ['- '+item for item in report['unknown']]
    if report.get('input'):
        lines += ['', f"Vstupní chyby/nepodporované řádky: {len(report['input']['malformed_or_unsupported'])}; přeskočené přesné duplicity: {report['input']['duplicates_skipped']}."]
    return '\n'.join(lines)+'\n'


def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path, help='Local JSONL trace files, never a URL')
    parser.add_argument('--output-prefix', type=Path, default=Path('reports/diagnostics-analysis'))
    parser.add_argument('--format', choices=('md', 'json'), default='md', help='stdout format; both files are always written')
    args = parser.parse_args(argv)
    inputs={path.resolve() for path in args.files}
    if any(args.output_prefix.with_suffix(suffix).resolve() in inputs for suffix in ('.json','.md')):
        parser.error('Output would overwrite an input file; choose another output prefix.')
    try:
        records, issues = load_jsonl(args.files)
    except (OSError, UnicodeError):
        parser.error('Cannot read a local UTF-8 JSONL input file.')
    report = analyze(records)
    report['input'] = {**issues, 'files': [safe_text(path.name) for path in args.files]}
    json_text, md_text = json.dumps(report, ensure_ascii=False, indent=2)+'\n', markdown(report)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix('.json').write_text(json_text, encoding='utf-8')
    args.output_prefix.with_suffix('.md').write_text(md_text, encoding='utf-8')
    print(json_text if args.format=='json' else md_text, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
