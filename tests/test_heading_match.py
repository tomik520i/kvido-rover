"""Two-image heading comparison contract; fake HTTP only."""
import json

import httpx
import pytest

from server.autopilot_core import HEADING_MATCH_SCHEMA, ask_heading_match


VALID = dict(status='match', confidence='high', correction='none', reason='Same doorway and cabinet.')


class Client:
    def __init__(self, answer=VALID, error=None, envelope=None):
        self.answer, self.error, self.envelope = answer, error, envelope
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        envelope = self.envelope if self.envelope is not None else {
            'choices': [{'message': {'content': json.dumps(self.answer)}}]}
        return httpx.Response(200, json=envelope, request=httpx.Request('POST', url))


def test_request_labels_two_images_and_bounds_inference():
    client = Client()
    result = ask_heading_match(b'reference', b'current', client, 'http://test/model')
    assert {key: result[key] for key in VALID} == VALID
    assert result['error_type'] is None
    assert type(result['latency_ms']) is int and result['latency_ms'] >= 0
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url == 'http://test/model'
    assert kwargs['timeout'] == 15
    body = kwargs['json']
    assert body['max_tokens'] == 180
    assert body['chat_template_kwargs'] == {'enable_thinking': False}
    assert body['response_format']['json_schema']['schema'] == HEADING_MATCH_SCHEMA
    parts = body['messages'][0]['content']
    assert 'REFERENCE' in parts[1]['text'] and 'CURRENT' in parts[3]['text']
    assert parts[2]['image_url']['url'].endswith('cmVmZXJlbmNl')
    assert parts[4]['image_url']['url'].endswith('Y3VycmVudA==')


@pytest.mark.parametrize('answer', [
    dict(VALID, status='mismatch', correction='left'),
    dict(VALID, status='mismatch', confidence='medium', correction='right'),
    dict(VALID, status='uncertain', confidence='low', reason='Too dark; no shared landmarks.'),
])
def test_valid_comparisons(answer):
    result = ask_heading_match(b'a', b'b', Client(answer))
    assert result['error_type'] is None
    assert result['status'] == answer['status']
    assert result['correction'] == answer['correction']


@pytest.mark.parametrize('answer', [
    [], None, {}, dict(VALID, extra='unexpected'),
    dict(VALID, status=True), dict(VALID, status='yes'),
    dict(VALID, confidence=1), dict(VALID, confidence='certain'),
    dict(VALID, correction=False), dict(VALID, correction='up'),
    dict(VALID, reason=None), dict(VALID, reason=' '), dict(VALID, reason='x'*161),
    dict(VALID, correction='left'),
    dict(VALID, status='uncertain', confidence='high'),
    dict(VALID, status='uncertain', confidence='low', correction='right'),
    dict(VALID, status='mismatch', confidence='low', correction='left'),
])
def test_invalid_fields_fail_closed(answer):
    result = ask_heading_match(b'a', b'b', Client(answer))
    assert result['status'] == 'uncertain'
    assert result['correction'] == 'none'
    assert result['confidence'] == 'low'
    assert result['error_type'] == 'invalid_answer'


@pytest.mark.parametrize('envelope,error_type', [
    ({}, 'malformed_response'),
    ({'choices': []}, 'malformed_response'),
    ({'choices': [{'message': {'content': None}}]}, 'malformed_response'),
    ({'choices': [{'message': {'content': 'not json SECRET'}}]}, 'invalid_json'),
])
def test_malformed_response_is_explicit_uncertainty(envelope, error_type):
    client = Client(envelope=envelope)
    result = ask_heading_match(b'a', b'b', client)
    assert result['error_type'] == error_type
    assert result['status'] == 'uncertain' and result['correction'] == 'none'
    assert 'SECRET' not in json.dumps(result)
    assert len(client.calls) == 1


@pytest.mark.parametrize('error,error_type', [
    (httpx.ReadTimeout('SECRET'), 'timeout'),
    (httpx.ConnectError('SECRET'), 'transport'),
    (httpx.HTTPStatusError('SECRET', request=httpx.Request('POST', 'http://test'),
                           response=httpx.Response(503)), 'http_status'),
])
def test_http_failure_never_retries_or_exposes_error(error, error_type):
    client = Client(error=error)
    result = ask_heading_match(b'a', b'b', client)
    assert result['error_type'] == error_type
    assert result['status'] == 'uncertain' and result['correction'] == 'none'
    assert len(client.calls) == 1
    assert 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('reference,current', [(None,b'a'), (b'a',b''), ('text',b'a')])
def test_invalid_images_make_no_request(reference, current):
    client = Client()
    assert ask_heading_match(reference, current, client)['error_type'] == 'invalid_input'
    assert not client.calls


@pytest.mark.parametrize('selected_heading', [20, 110])
def test_panorama_selection_retains_reference_even_when_already_facing_it(monkeypatch, selected_heading):
    from server import autopilot_core as core
    selected = dict(heading=selected_heading, frame=3, free=100,
                    target=False, passage=False, threshold=False)
    previous = dict(selected, heading=90, frame=4)
    memory = dict(heading=20, frame=5, scanned_once=True,
                  scan={'left': 0, 'entries': [selected, previous]})
    monkeypatch.setattr(core, 'free_space', lambda radar: [(0, 100)])
    monkeypatch.setattr(core, 'panorama_pick', lambda entries, *args, **kwargs: (entries[0], 'volno'))
    actions, reason, done = core.plan_step({}, 100, {}, memory)
    assert memory['route_goal'] == dict(reference_frame=3, heading=selected_heading,
                                      selected_at_frame=5, status='aligning', attempts=0)
    assert not done
    if selected_heading == 20:
        assert actions == []
    else:
        assert actions
