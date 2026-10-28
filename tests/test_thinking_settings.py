import json
from html.parser import HTMLParser

import httpx
import pytest

from test_safety import a, c, decision


def test_thinking_setting_persists_and_is_exposed(c,monkeypatch,tmp_path):
    monkeypatch.setattr(a,'settings',dict(a.settings))
    monkeypatch.setattr(a,'CONFIG_PATH',tmp_path/'settings.json')
    for enabled in (False,True,False):
        response = c.post('/api/settings',json={'thinking_enabled':enabled,'simulation':True})
        assert response.status_code == 200,response.text
        assert c.get('/api/settings').json()['thinking_enabled'] is enabled
        assert json.loads(a.CONFIG_PATH.read_text(encoding='utf-8'))['thinking_enabled'] is enabled
    assert c.post('/api/settings',json={'thinking_enabled':'false'}).status_code == 422


def test_inference_uses_current_persistent_thinking_toggle(c,monkeypatch):
    monkeypatch.setattr(a,'settings',dict(a.settings))
    calls = []
    async def post(method,url,**kwargs):
        calls.append(kwargs['json'])
        return httpx.Response(200,request=httpx.Request('POST',url),json={
            'choices':[{'finish_reason':'stop','message':{'content':json.dumps({k:v for k,v in decision(action='stop').model_dump().items() if k in kwargs['json']['response_format']['json_schema']['schema']['properties']})}}]})
    monkeypatch.setattr(a.ai_provider.client,'request',post)
    for enabled in (False,True,False):
        monkeypatch.setitem(a.settings,'thinking_enabled',enabled)
        c.portal.call(a.infer,[{'role':'user','content':'Describe the current view'}])
        assert calls[-1]['chat_template_kwargs']['enable_thinking'] is enabled
        assert calls[-1]['max_tokens'] == (4096 if enabled else 700)


@pytest.mark.parametrize('fallback',[False,True])
def test_missing_thinking_setting_preserves_environment_fallback(c,monkeypatch,fallback):
    monkeypatch.setattr(a,'settings',dict(a.settings))
    monkeypatch.delitem(a.settings,'thinking_enabled',raising=False)
    monkeypatch.setattr(a,'LLM_THINKING',fallback)
    async def post(method,url,**kwargs):
        assert kwargs['json']['chat_template_kwargs']['enable_thinking'] is fallback
        return httpx.Response(200,request=httpx.Request('POST',url),json={
            'choices':[{'finish_reason':'stop','message':{'content':json.dumps({k:v for k,v in decision(action='stop').model_dump().items() if k in kwargs['json']['response_format']['json_schema']['schema']['properties']})}}]})
    monkeypatch.setattr(a.ai_provider.client,'request',post)
    c.portal.call(a.infer,[{'role':'user','content':'Describe'}])


def test_settings_ui_contains_unchecked_thinking_checkbox(c):
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inputs=[]
        def handle_starttag(self,tag,attrs):
            if tag=='input':self.inputs.append(dict(attrs))
    parser=Inputs()
    parser.feed(c.get('/').text)
    control=next(item for item in parser.inputs if item.get('id')=='thinkingEnabled')
    assert control['type']=='checkbox' and 'checked' not in control
