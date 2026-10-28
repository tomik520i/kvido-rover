import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles
from server.web_paths import PublicPrefixMiddleware, SESSION_COOKIE, cookie_path

STATIC = Path(__file__).resolve().parents[1] / 'server' / 'static'


def panel():
    app = FastAPI()
    app.add_middleware(PublicPrefixMiddleware)

    @app.get('/api/probe')
    async def probe(request: Request):
        return {'root': request.scope['root_path'], 'cookie_path': cookie_path(request)}

    app.mount('/static', StaticFiles(directory=STATIC))
    return TestClient(app)


@pytest.mark.parametrize('prefix', ['', '/kvido'])
def test_same_api_and_static_available_at_both_roots(prefix):
    client = panel()
    response = client.get(prefix + '/api/probe')
    assert response.status_code == 200
    assert response.json() == {'root': prefix, 'cookie_path': prefix or '/'}
    for asset in ('app.js', 'live-audio.js', 'pcm-worklet.js', 'manifest.json', 'style.css'):
        assert client.get(prefix + '/static/' + asset).status_code == 200


def test_prefix_redirect_retains_query_and_never_uses_forwarded_prefix():
    client = panel()
    response = client.get('/kvido?from=phone', follow_redirects=False)
    assert response.status_code == 307
    assert response.headers['location'] == '/kvido/?from=phone'
    assert client.get('/kvidoevil/api/probe').status_code == 404
    assert client.get('/api/probe', headers={'x-forwarded-prefix': '/evil'}).json()['cookie_path'] == '/'
    assert SESSION_COOKIE == 'kvido_session'


def test_frontend_resolves_endpoints_from_module_root():
    script = (STATIC / 'app.js').read_text(encoding='utf-8')
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert "new URL('../',import.meta.url)" in script
    assert "'/api/" not in script
    assert '"/static/' not in html
    assert 'href="/"' not in html
    manifest = json.loads((STATIC / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['start_url'] == manifest['scope'] == '../'
