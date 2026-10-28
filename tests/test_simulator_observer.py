import json
from pathlib import Path

from tools.observe_simulator import save_latest


def test_busy_windows_reader_does_not_kill_observer(tmp_path,monkeypatch):
    original=Path.replace
    attempts=[]
    def locked(path,target):
        attempts.append(True)
        if len(attempts)<3:raise PermissionError('file busy')
        return original(path,target)
    monkeypatch.setattr(Path,'replace',locked)
    assert save_latest(tmp_path,{'step':7})
    assert json.loads((tmp_path/'latest.json').read_text())=={'step':7}
    monkeypatch.setattr(Path,'replace',lambda *args:(_ for _ in ()).throw(PermissionError('file busy')))
    assert save_latest(tmp_path,{'step':8}) is False
    assert json.loads((tmp_path/'latest.json').read_text())=={'step':7}
