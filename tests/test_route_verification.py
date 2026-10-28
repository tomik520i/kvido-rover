from server.route_verification import check_route


def memory(heading=0, desired=270, status='aligning'):
    return {'heading': heading, 'scanned_once': True,
            'route_goal': {'heading': desired, 'reference_frame': 4, 'status': status, 'attempts': 0}}


def verdict(status='match', confidence='high', correction='none'):
    return dict(status=status, confidence=confidence, correction=correction, reason='landmarks', error_type=None)


def no_compare(*args):
    raise AssertionError('Comparison must wait until the planned turn is confirmed')


def test_refused_turn_keeps_destination_and_never_falls_through_to_forward():
    m = memory()
    for _ in range(3):
        r = check_route(m, b'ref', b'now', 72, [], no_compare)
        assert r['actions'] == [(180, 3, 'rotate_right')]
        assert not r['verified'] and m['route_goal']['heading'] == 270
    # The map has applied one confirmed 30-degree pulse. Only the remaining turn is proposed.
    m['heading'] = 330
    r = check_route(m, b'ref', b'now', 72, [[180, 1, 'rotate_right']], no_compare)
    assert r['actions'] == [(180, 2, 'rotate_right')]


def test_success_needs_fresh_verification_after_refused_or_discarded_forward():
    m = memory(heading=279)
    calls = []
    def compare(*a):
        calls.append(a)
        return verdict()
    assert check_route(m, b'ref', b'now', 72, [], compare)['verified']
    assert check_route(m, b'ref', b'new', 72, [], compare)['verified']
    assert len(calls) == 2
    assert check_route(m, b'ref', b'after', 72, [[180, 1, 'forward']], no_compare) is None
    assert 'route_goal' not in m and m['last_route_goal']['status'] == 'entered'


def test_uncertain_retries_without_forward_then_selects_a_new_panorama():
    m = memory(heading=270)
    for i in range(3):
        r = check_route(m, b'ref', b'now', 72, [], lambda *a: verdict('uncertain', 'low'))
        assert r['actions'] == [] and not r['verified']
    assert r['rescan'] and 'route_goal' not in m and not m['scanned_once']


def test_low_or_medium_match_does_not_allow_forward():
    for confidence in ['low', 'medium']:
        r = check_route(memory(270), b'ref', b'now', 72, [], lambda *a: verdict(confidence=confidence))
        assert not r['verified'] and r['actions'] == []


def test_ai_correction_without_geometric_evidence_is_not_a_motor_command():
    m = memory(270)
    first = check_route(m, b'ref', b'now', 72, [], lambda *a: verdict('mismatch', correction='left'))
    assert first['actions'] == [] and not first['verified']
    assert first['verification']['correction'] == 'left'  # retained for diagnosis only


def test_no_shared_landmarks_never_invents_a_turn():
    r = check_route(memory(270), b'ref', b'now', 72, [], lambda *a: verdict('mismatch'))
    assert r['actions'] == [] and not r['verified']


def test_reference_missing_or_translation_requires_a_new_panorama():
    m = memory()
    assert check_route(m, None, b'now', 72, [], no_compare)['rescan']
    m = memory(270)
    assert check_route(m, b'ref', b'now', 72, [[180, 1, 'backward']], no_compare)['rescan']


def test_camera_tilt_must_match_reference_before_comparison():
    r = check_route(memory(270), b'ref', b'now', 55, [], no_compare)
    assert r['actions'] == [] and r['retilt'] and r['next_tilt'] == 72


def geometry(delta):
    return lambda *a: dict(reliable=True, correction_deg=delta, yaw_deg=-delta)


def test_visual_error_corrects_one_pulse_even_if_model_claims_match():
    m = memory(270)
    r = check_route(m, b'ref', b'now', 72, [], lambda *a: verdict(), measure=geometry(22))
    assert r['actions'] == [(150, 1, 'rotate_left')] and not r['verified']
    assert m['route_goal']['status'] == 'checking'
    # Actual movement and a NEW close view are required, then model match.
    r = check_route(m, b'ref', b'new', 72, [[150,1,'rotate_left']], lambda *a: verdict(), measure=geometry(2))
    assert r['verified'] and r['actions'] is None


def test_right_correction_is_small_and_requires_new_model_confirmation():
    m = memory(270)
    r = check_route(m, b'ref', b'now', 72, [], lambda *a: verdict('mismatch'), measure=geometry(-12))
    assert r['actions'] == [(130,1,'rotate_right')]
    r = check_route(m, b'ref', b'new', 72, [[130,1,'rotate_right']], lambda *a: verdict('uncertain','low'), measure=geometry(1))
    assert not r['verified'] and r['actions'] == []


def test_no_progress_or_larger_error_abandons_route_without_forward():
    for residual in (22, 26):
        m = memory(270)
        check_route(m, b'r', b'c',72,[],lambda *a:verdict(),measure=geometry(22))
        r = check_route(m,b'r',b'n',72,[[150,1,'rotate_left']],lambda *a:verdict(),measure=geometry(residual))
        assert r['rescan'] and r['actions'] == [] and not r['verified']


def test_refused_corrections_cannot_retry_forever():
    m = memory(270)
    for _ in range(3):
        r = check_route(m,b'r',b'n',72,[],lambda *a:verdict(),measure=geometry(22))
        assert not r['verified'] and len(r['actions']) == 1
    r = check_route(m,b'r',b'n',72,[],lambda *a:verdict(),measure=geometry(22))
    assert r['rescan'] and r['actions'] == []


def test_model_timeout_preserves_reference_and_does_not_spend_mismatch_budget():
    m = memory(270)
    for _ in range(5):
        r = check_route(m,b'r',b'n',72,[],lambda *a:dict(verdict('uncertain','low'),error_type='timeout'))
        assert r['retry_comparison'] and not r['verified'] and r['actions'] == []
    assert m['route_goal']['reference_frame'] == 4 and m['route_goal']['attempts'] == 0
