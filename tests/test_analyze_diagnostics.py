import json
from pathlib import Path
import subprocess
import sys

from tools.analyze_diagnostics import analyze, load_jsonl, markdown


def event(kind, at=1000, **fields):
    return {'v': 1, 'event': kind, 'at_ms': at, **fields}


def test_full_pipeline_acceptance_is_not_physical_motion_or_target_completion():
    rows = [
        event('model_proposal', 1000, task_id='task', step=1, latency_ms=3200,
              proposal={'action': 'forward', 'speed': 100, 'duration_ms': 150, 'reason': 'Volno.'}),
        event('gate', 1100, task_id='task', step=1, command_id='c1', proposal={'action': 'forward'},
              executed='forward', permitted=True, reason='Krátký krok.',
              telemetry={'distance_cm': 102, 'telemetry_age_ms': 200}),
        event('command_request', 1110, command_id='c1', action='forward', speed=100, duration_ms=150, tilt=90),
        event('command_result', 1150, command_id='c1', acceptance='transport_accepted'),
        event('telemetry', 1700, telemetry={'distance_cm': 102, 'telemetry_age_ms': 100, 'motor_byte': 0}),
        event('assessment', 2100, task_id='task', step=1,
              assessment={'status': 'completed', 'progress': 'changed', 'summary': 'Model hlásí hotovo.'},
              motion_observation={'status': 'changed'}),
        event('task_phase', 2200, task_id='task', step=1, status='completed', outcome='Model hlásí hotovo.'),
    ]
    report = analyze(rows)
    assert report['model']['proposal_actions'] == {'forward': 1}
    assert report['gate']['executed_action_labels'] == {'forward': 1}
    assert report['transport']['accepted_requests'] == 1
    assert report['transport']['latency']['median_ms'] == 40
    assert report['uno_observations']['active_samples'] == 0
    assert report['transport']['physical_motion_confirmed'] is None
    assert report['completion']['latest_server_completed_tasks'] == 1
    assert report['completion']['target_reached_independently_confirmed'] is None
    assert any('Nula pozorovaných' in item for item in report['unknown'])
    text = markdown(report)
    assert 'nepotvrzen tímto logem' in text
    assert 'nikoli počty měření čidla' in text


def test_telemetry_sampling_excludes_embedded_gate_and_keeps_null_stale_distinct():
    rows = [event('telemetry', telemetry=value) for value in [
        {'distance_cm': 102, 'telemetry_age_ms': 500},
        {'distance_cm': None, 'telemetry_age_ms': 700},
        {'distance_cm': 102, 'telemetry_age_ms': 501},
        {'distance_cm': '102', 'telemetry_age_ms': 0},
        {'distance_cm': 102},
        {'distance_cm': 102, 'telemetry_age_ms': 0, 'connected': False},
    ]]
    rows += [event('gate', proposal={'action': 'forward'}, executed='stop', permitted=True,
                   reason='Chybí čerstvé měření.', telemetry={'distance_cm': None, 'telemetry_age_ms': 0})]
    report = analyze(rows)
    assert report['telemetry']['sample_count'] == 6
    assert report['telemetry']['classes'] == {'valid': 1, 'null_distance': 1, 'stale': 1,
                                             'invalid_distance': 1, 'unknown_age': 1, 'offline': 1}
    assert report['telemetry']['null_also_stale_samples'] == 1
    assert report['telemetry']['sample_counts_are_firmware_counters'] is False
    assert report['gate']['refused_or_stopped_proposals'] == 1
    assert report['gate']['timeline'][0]['distance_category'] == 'null_distance'


def test_actual_firmware_counters_are_separate_and_reset_does_not_fabricate_measurements():
    rows = [event('telemetry', index * 1000, telemetry={'distance_cm': 100, 'telemetry_age_ms': 100,
               'relay': {'connections': count, 'last_event': 'connected', 'last_error': 'none'},
               'firmware_counters': {'sonar_valid_total': count * 10}})
            for index, count in enumerate([4, 6, 1, 2])]
    report = analyze(rows)
    assert report['telemetry']['sample_count'] == 4
    counters = report['firmware_counters']
    assert counters['relay.connections'] == {'first': 4, 'last': 2, 'snapshots': 4,
                                             'observed_nonnegative_increments': 3, 'decreases_or_resets': 1}
    assert counters['firmware_counters.sonar_valid_total']['observed_nonnegative_increments'] == 30
    assert 'relay.last_error' not in counters


def test_refusals_timeouts_link_events_and_uno_reasons_stay_separate():
    rows = [
        event('gate', 0, proposal={'action': 'forward'}, executed='stop', permitted=False, reason='Lease expired'),
        event('gate', 10, proposal={'action': 'stop'}, executed='stop', permitted=True, reason='User STOP'),
        event('command_request', 100, command_id='timed-out', action='forward'),
        event('command_result', 1000, command_id='timed-out', acceptance='unknown', reason='transport timeout'),
        event('command_request', 1100, command_id='incomplete', action='stop'),
        event('relay_link', 1200, connected=False, reason='read timeout'),
        event('relay_link', 2200, connected=True),
        event('telemetry', 2300, telemetry={'distance_cm': 15, 'telemetry_age_ms': 100,
              'motor_active': False, 'motor_byte': 0, 'stop_reason': 'front_obstacle'}),
    ]
    report = analyze(rows)
    assert report['gate']['refused_or_stopped_proposals'] == 1
    assert report['gate']['reasons'] == {'Lease expired': 1, 'User STOP': 1}
    assert report['transport']['accepted_requests'] == 0
    assert report['transport']['unknown_requests'] == 1
    assert report['transport']['missing_results'] == 1
    assert report['uno_observations']['reasons'] == {'stop_reason: front_obstacle': 1}
    assert [row['connected'] for row in report['relay_timeline']] == [False, True]
    assert report['window']['duration_s'] == 2.3


def test_observed_motor_activity_is_sampling_evidence_not_odometry():
    rows = [event('telemetry', telemetry=value) for value in [
        {'motor_active': True, 'motor_byte': 1, 'telemetry_age_ms': 200},
        {'motor_active': False, 'motor_byte': 2, 'telemetry_age_ms': 700},
        {'motor': {'unexpected': 'object'}},
    ]]
    report = analyze(rows)
    assert report['uno_observations']['active_samples'] == 2
    assert report['uno_observations']['fresh_active_samples'] == 1
    assert report['uno_observations']['contradictory_samples'] == 1
    assert report['uno_observations']['physical_displacement_confirmed'] is None


def test_partial_missing_timestamp_or_orphan_result_does_not_create_false_correlation():
    report = analyze([{'event': 'command_request', 'command_id': 'missing-time', 'action': 'forward'},
                      event('command_result', command_id='orphan', acceptance='transport_accepted')])
    assert report['transport']['correlated_requests'] == 1
    assert report['transport']['missing_results'] == 1
    assert report['transport']['orphan_results'] == 1
    assert report['transport']['latency']['count'] == 0
    assert report['transport']['accepted_requests'] == 0


def test_frame_hashes_do_not_become_false_physical_proof():
    rows = [event('frame', task_id='a', step=1, role=role, sha256='a' * 64) for role in ['before', 'after']]
    rows += [event('assessment', task_id='a', step=1, status='continue', progress='unchanged',
                   summary='Beze změny.', motion_observation={'status': 'possible_stall'})]
    report = analyze(rows)
    assert report['before_after']['paired_frame_steps'] == 1
    assert report['before_after']['identical_encoded_frame_pairs'] == 1
    assert report['before_after']['model_progress_counts'] == {'unchanged': 1}
    assert report['before_after']['motion_observation_counts'] == {'possible_stall': 1}
    assert report['completion']['target_reached_independently_confirmed'] is None


def test_jsonl_overlap_malformed_unknown_fields_and_secret_redaction(tmp_path):
    row = event('gate', seq=1, proposal={'action': 'forward', 'api_key': 'private-proposal'},
                executed='stop', reason='token=private-token password=private-password Bearer private-bearer sk-privatevalue',
                api_key='never-output-this', image='data:image/jpeg;base64,private-image')
    path = tmp_path / 'trace.jsonl'
    path.write_text(json.dumps(row) + '\nnot-json\n[]\n' + json.dumps({**row, 'v': 2}), encoding='utf-8')
    records, issues = load_jsonl([path, path])
    assert len(records) == 1
    assert issues['duplicates_skipped'] == 1
    assert len(issues['malformed_or_unsupported']) == 6
    report = analyze(records)
    output = json.dumps(report) + markdown(report)
    for value in ['private-token', 'private-password', 'private-bearer', 'sk-privatevalue',
                  'never-output-this', 'private-proposal', 'private-image']:
        assert value not in output
    assert '[REDACTED]' in output


def test_cli_writes_both_formats_without_network_or_key_access(tmp_path):
    source = tmp_path / 'trace.jsonl'
    source.write_text(json.dumps(event('startup', 1000)) + '\n', encoding='utf-8')
    output = tmp_path / 'analysis'
    script = Path(__file__).resolve().parents[1] / 'tools/analyze_diagnostics.py'
    result = subprocess.run([sys.executable, str(script), str(source), '--output-prefix', str(output), '--format', 'json'],
                            check=True, capture_output=True, text=True, encoding='utf-8')
    report = json.loads(result.stdout)
    assert report['offline_only'] is True
    assert report['event_counts']['startup'] == 1
    assert json.loads(output.with_suffix('.json').read_text(encoding='utf-8')) == report
    assert output.with_suffix('.md').read_text(encoding='utf-8').startswith('# Offline')


def test_nested_uno_diagnostics_keep_booleans_and_true_drive_counters_separate():
    rows = [event('telemetry', index * 1000, telemetry={
        'distance_cm': None, 'telemetry_age_ms': 100,
        'ultra': {'seq': seq, 'ok': False, 'pre': False, 'post': True, 'null': nulls, 'us': 0},
        'drive': {'seq': index + 1, 'ok': False, 'why': 'no_distance', 'accepted': 2, 'denied': denied},
        'motor': {'active': False, 'speed': 0, 'stop': 'null_sensor', 'null_stops': stops,
                  'near_stops': 0, 'deadline_stops': 3},
    }) for index, (seq, nulls, denied, stops) in enumerate([(10, 8, 1, 4), (15, 12, 3, 5)])]
    report = analyze(rows)
    counters = report['firmware_counters']
    assert counters['ultra.seq']['observed_nonnegative_increments'] == 5
    assert counters['ultra.null']['observed_nonnegative_increments'] == 4
    assert counters['drive.denied']['observed_nonnegative_increments'] == 2
    assert counters['motor.null_stops']['observed_nonnegative_increments'] == 1
    assert 'ultra.ok' not in counters and 'ultra.us' not in counters
    assert report['telemetry']['ultra_boolean_sample_counts'] == {'ok_false': 2, 'pre_false': 2, 'post_true': 2}
    assert report['uno_observations']['last_drive_boolean_sample_counts'] == {'last_drive_ok_false': 2}
    assert report['uno_observations']['field_counts']['motor_active_false'] == 2
    assert report['uno_observations']['reasons']['drive.why: no_distance'] == 2
    assert report['transport']['accepted_requests'] == 0


def test_cli_refuses_to_overwrite_input(tmp_path):
    source = tmp_path / 'same.json'
    original = json.dumps(event('startup')) + '\n'
    source.write_text(original, encoding='utf-8')
    script = Path(__file__).resolve().parents[1] / 'tools/analyze_diagnostics.py'
    result = subprocess.run([sys.executable, str(script), str(source), '--output-prefix', str(source.with_suffix(''))],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert source.read_text(encoding='utf-8') == original


def test_simulated_counters_are_never_counted_as_live_hardware():
    rows=[
        event('startup',1000,simulation=False),
        event('telemetry',1100,telemetry={'simulation':False,'ultra':{'seq':5}}),
        event('telemetry',1200,telemetry={'simulation':False,'ultra':{'seq':7}}),
        event('settings',1300,simulation=True),
        event('telemetry',1400,telemetry={'simulation':True,'ultra':{'seq':100},'motor':{'active':True}}),
        event('telemetry',1500,telemetry={'simulation':True,'ultra':{'seq':200},'motor':{'active':True}}),
    ]
    report=analyze(rows)
    assert report['primary_scope']=='live'
    assert report['scope_counts']['live']==3 and report['scope_counts']['simulated']==3
    assert report['firmware_counters']['ultra.seq']['observed_nonnegative_increments']==2
    assert report['simulation_analysis']['firmware_counters']['ultra.seq']['observed_nonnegative_increments']==100
