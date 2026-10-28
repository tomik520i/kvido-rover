import asyncio
import json
import os
import stat

import httpx
import pytest

from server.ai_provider import AIProvider,AIProviderError,OpenAIKeyManager,preferred_model,responses_input

SCHEMA={'type':'object','properties':{'action':{'type':'string','enum':['stop']},
                                    'safe':{'type':'boolean'}},
        'required':['action','safe'],'additionalProperties':False}
ANSWER={'action':'stop','safe':True}
SECRET='test-secret-NEVER-EXPOSE'


def test_gemma_perception_can_request_zero_temperature(tmp_path):
    async def run():
        def handler(request):
            assert json.loads(request.content)['temperature'] == 0
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(ANSWER)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=AIProvider(client,tmp_path/'data',tmp_path)
            result,_=await provider.infer([],SCHEMA,gemma_temperature=0)
            assert result==ANSWER
            for invalid in (True,float('nan'),-1,3):
                with pytest.raises(AIProviderError):
                    await provider.infer([],SCHEMA,gemma_temperature=invalid)
    asyncio.run(run())


def completed(answer=ANSWER,**updates):
    result={'status':'completed','output':[{'type':'message','role':'assistant','status':'completed',
                'content':[{'type':'output_text','text':json.dumps(answer)}]}],
            'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15,'unsafe_extra':SECRET}}
    return {**result,**updates}


def adapter(tmp_path,handler):
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler),trust_env=False)
    provider=AIProvider(client,tmp_path/'data',tmp_path)
    provider.set_key(SECRET)
    return provider,client


def test_key_storage_priorities_and_dev_fallback(tmp_path,monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    (tmp_path/'openai_key.txt').write_text('development-only\n')
    keys=OpenAIKeyManager(tmp_path/'data',tmp_path)
    assert not keys.configured()
    dev=OpenAIKeyManager(tmp_path/'data',tmp_path,allow_dev_key=True)
    assert dev.configured() and dev._load()=='development-only'
    monkeypatch.setenv('OPENAI_API_KEY','environment-only')
    assert keys.configured() and keys._load()=='environment-only'
    assert keys.set_key(SECRET) is None
    assert keys.path.read_text()==SECRET and keys._load()==SECRET
    if os.name!='nt':assert stat.S_IMODE(keys.path.stat().st_mode)==0o600
    assert not (tmp_path/'data/openai.key.tmp').exists()


@pytest.mark.parametrize('invalid',['',' ','abc def','abc\n','abc\tdef',None])
def test_invalid_key_has_safe_error_and_does_not_write(tmp_path,invalid):
    keys=OpenAIKeyManager(tmp_path/'data',tmp_path)
    with pytest.raises(AIProviderError) as caught:keys.set_key(invalid)
    assert caught.value.category=='key_invalid'
    assert not keys.path.exists()


def test_models_are_actual_account_ids_with_conservative_vision_catalog(tmp_path):
    async def run():
        def handler(request):
            assert request.url=='https://api.openai.com/v1/models'
            assert request.headers['authorization']=='Bearer '+SECRET
            return httpx.Response(200,json={'data':[{'id':name} for name in [
                'gpt-6-luna','gpt-6.1-sol','gpt-6-luna-2026-10-01',
                'gpt-6-luna-audio','gpt-realtime','other-model']]})
        provider,client=adapter(tmp_path,handler)
        async with client:
            rows=await provider.models()
            assert len(rows)==6
            modalities={row['id']:row['vision'] for row in rows}
            assert modalities['gpt-6-luna']=='documented'
            assert modalities['gpt-6.1-sol']=='documented'
            assert modalities['gpt-6-luna-2026-10-01']=='documented'
            assert modalities['gpt-6-luna-audio']==modalities['gpt-realtime']==modalities['other-model']=='unknown'
            assert preferred_model(rows)=='gpt-6-luna'
            assert preferred_model([{'id':'unknown','vision':'unknown'}]) is None
    asyncio.run(run())


@pytest.mark.parametrize('status,category',[(401,'authentication_failed'),(403,'access_denied'),
                                          (429,'rate_limited'),(503,'upstream_error')])
def test_models_api_errors_never_invent_models_or_echo_secret(tmp_path,status,category):
    async def run():
        provider,client=adapter(tmp_path,lambda request:httpx.Response(status,json={'error':{'message':SECRET}}))
        async with client:
            with pytest.raises(AIProviderError) as caught:await provider.models()
            assert caught.value.category==category and SECRET not in str(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize('model,effort',[('gpt-6-luna','none'),('gpt-6.1-sol','low')])
def test_responses_conversion_multiple_images_and_strict_stateless_output(tmp_path,model,effort):
    async def run():
        messages=[{'role':'system','content':'Rules'}, {'role':'assistant','content':'Previous observation'},
                  {'role':'user','content':[{'type':'text','text':'Compare images'},
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,AA==','detail':'low'}},
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,BB=='}}]}]
        def handler(request):
            assert request.url=='https://api.openai.com/v1/responses'
            body=json.loads(request.content)
            assert body['store'] is False and body['reasoning']['effort']==effort
            assert 'temperature' not in body
            assert body['text']['format']=={'type':'json_schema','name':'rover_decision','strict':True,'schema':SCHEMA}
            assert body['input'][1]['content'][0]['type']=='output_text'
            assert [part['type'] for part in body['input'][2]['content']]==['input_text','input_image','input_image']
            return httpx.Response(200,json=completed())
        provider,client=adapter(tmp_path,handler)
        async with client:
            answer,metadata=await provider.infer(messages,SCHEMA,provider='openai',model=model)
            assert answer==ANSWER and metadata['provider']=='openai' and metadata['model']==model
            assert metadata['usage']=={'input_tokens':10,'output_tokens':5,'total_tokens':15}
            assert SECRET not in json.dumps(metadata)
    asyncio.run(run())


@pytest.mark.parametrize('response,category',[
    (completed(status='incomplete',incomplete_details={'reason':'max_output_tokens'}),'incomplete_response'),
    (completed(output=[{'type':'message','role':'assistant','content':[{'type':'refusal','refusal':SECRET}]}]),'refused_response'),
    (completed(answer={'action':'forward','safe':True}),'invalid_structured_output'),
    (completed(answer={'action':'stop','safe':1}),'invalid_structured_output'),
    (completed(answer={'action':'stop'}),'invalid_structured_output'),
    (completed(error={'message':SECRET}),'invalid_response'),
    (completed(output=None),'invalid_response'),
])
def test_incomplete_refusal_and_invalid_decisions_fail_closed(tmp_path,response,category):
    async def run():
        provider,client=adapter(tmp_path,lambda request:httpx.Response(200,json=response))
        async with client:
            with pytest.raises(AIProviderError) as caught:
                await provider.infer([{'role':'user','content':'Go'}],SCHEMA,provider='openai')
            assert caught.value.category==category and SECRET not in str(caught.value)
    asyncio.run(run())


def test_unknown_model_needs_successful_probe_and_positive_result_is_cached(tmp_path):
    async def run():
        calls=[]
        def handler(request):
            calls.append(request)
            if request.method=='GET':return httpx.Response(200,json={'data':[{'id':'unknown-vision'}]})
            body=json.loads(request.content)
            if body['text']['format']['schema'].get('properties',{}).get('dominant_color'):
                assert body['input'][0]['content'][1]['type']=='input_image'
                return httpx.Response(200,json=completed({'dominant_color':'red'}))
            return httpx.Response(200,json=completed())
        provider,client=adapter(tmp_path,handler)
        async with client:
            with pytest.raises(AIProviderError) as caught:
                await provider.infer([{'role':'user','content':'Hi'}],SCHEMA,provider='openai',model='unknown-vision')
            assert caught.value.category=='model_requires_vision_probe' and not calls
            first,second=await asyncio.gather(provider.probe('unknown-vision'),provider.probe('unknown-vision'))
            assert first['vision']==second['vision']=='verified' and len(calls)==1
            assert second['cached'] is True
            assert (await provider.models())[0]['vision']=='verified'
            answer,_=await provider.infer([{'role':'user','content':'Hi'}],SCHEMA,provider='openai',model='unknown-vision')
            assert answer==ANSWER
            provider.set_key('new-test-key')
            assert (await provider.models())[0]['vision']=='unknown'
    asyncio.run(run())


def test_transient_probe_failure_does_not_cache_negative_capability(tmp_path):
    async def run():
        counter=[]
        def handler(request):
            counter.append(True)
            if len(counter)==1:return httpx.Response(429,json={'error':{'message':SECRET}})
            return httpx.Response(200,json=completed({'dominant_color':'red'}))
        provider,client=adapter(tmp_path,handler)
        async with client:
            with pytest.raises(AIProviderError):await provider.probe('unknown-model')
            result=await provider.probe('unknown-model')
            assert result['vision']=='verified' and result['cached'] is False
            assert len(counter)==2
    asyncio.run(run())


def test_gemma_keeps_chat_format_thinking_and_does_not_require_openai_key(tmp_path):
    async def run():
        def handler(request):
            body=json.loads(request.content)
            assert request.url=='http://gemma.local:8080/v1/chat/completions'
            assert 'authorization' not in request.headers
            assert body['messages']==[{'role':'user','content':'Hello'}]
            assert body['chat_template_kwargs']=={'enable_thinking':False} and body['max_tokens']==700
            return httpx.Response(200,json={'model':'gemma-local','choices':[{
                'finish_reason':'stop','message':{'content':json.dumps(ANSWER)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=AIProvider(client,tmp_path/'data',tmp_path,gemma_url='http://gemma.local:8080')
            answer,meta=await provider.infer([{'role':'user','content':'Hello'}],SCHEMA)
            assert answer==ANSWER and meta['model']=='gemma-local'
    asyncio.run(run())


def test_transport_exception_is_sanitized(tmp_path):
    async def run():
        def handler(request):raise httpx.ConnectError(SECRET,request=request)
        provider,client=adapter(tmp_path,handler)
        async with client:
            with pytest.raises(AIProviderError) as caught:await provider.models()
            assert caught.value.category=='connection_failed' and SECRET not in str(caught.value)
    asyncio.run(run())


def test_explicit_bounded_output_budget_reaches_responses_and_incomplete_reason_is_safe(tmp_path):
    async def run():
        calls=[]
        def handler(request):
            body=json.loads(request.content);calls.append(body['max_output_tokens'])
            return httpx.Response(200,json=completed(status='incomplete',incomplete_details={'reason':'max_output_tokens','private':SECRET}))
        provider,client=adapter(tmp_path,handler)
        async with client:
            with pytest.raises(AIProviderError) as caught:
                await provider.infer([{'role':'user','content':'Compare'}],SCHEMA,provider='openai',max_output_tokens=2400)
            assert calls==[2400]
            assert caught.value.category=='incomplete_response'
            assert caught.value.incomplete_reason=='max_output_tokens'
            assert SECRET not in str(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize('budget',[True,0,4097])
def test_invalid_budget_is_rejected_before_provider_request(tmp_path,budget):
    async def run():
        def unexpected(request):raise AssertionError('Invalid budget must not issue a request')
        provider,client=adapter(tmp_path,unexpected)
        async with client:
            with pytest.raises(AIProviderError) as caught:
                await provider.infer([],SCHEMA,provider='openai',max_output_tokens=budget)
            assert caught.value.category=='request_rejected'
    asyncio.run(run())


@pytest.mark.parametrize('code,category',[
    ('insufficient_quota','insufficient_quota'),('rate_limit_exceeded','rate_limited'),
    (SECRET,'rate_limited'),
])
def test_429_code_allowlist_distinguishes_quota_without_logging_provider_message(tmp_path,code,category):
    async def run():
        provider,client=adapter(tmp_path,lambda request:httpx.Response(429,json={
            'error':{'code':code,'type':code,'message':SECRET}}))
        async with client:
            with pytest.raises(AIProviderError) as caught:await provider.models()
            assert caught.value.category==category
            assert caught.value.api_error_code==(code if code!=SECRET else None)
            assert SECRET not in str(caught.value) and SECRET not in repr(caught.value.__dict__)
    asyncio.run(run())
