"""Regression: browser alpha startup was replaced by the persisted server runner."""
from test_frontend_autonomy import APP, run_js


def test_browser_start_only_launches_server_runner(tmp_path):
    source = APP.read_text(encoding="utf-8")
    assert "alphaStart" not in source
    assert "alphaRecover" not in source
    assert "autopilot/step" not in source
    run_js(tmp_path, r"""
await refresh();await startTask('Prozkoumej pokoj');
assert.deepEqual(calls.filter(c=>c.path==='autonomy').map(c=>c.body),[{goal:'Prozkoumej pokoj'}]);
assert.ok(!calls.some(c=>c.path==='drive'||c.path==='arm'));
windowListeners.pagehide();await Promise.resolve();
assert.equal(remote.autonomy.status,'running');
assert.ok(calls.some(c=>c.path==='release'));
assert.ok(!calls.some(c=>c.path==='stop'||c.path==='autonomy/cancel'));
""")
