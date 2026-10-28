"""Gemma/OpenAI transport adapter. Never logs keys, images, or provider bodies.

OpenAI capability catalog intentionally names only documented image-input
families, not all GPT IDs. GET /models reports account access, not modalities.
Unknown models require a successful image+structured-output probe first.
Docs checked 2026-10-23:
https://developers.openai.com/api/docs/models/gpt-6-luna
https://developers.openai.com/api/docs/models/gpt-6.1-sol
https://developers.openai.com/api/docs/guides/images-vision
https://developers.openai.com/api/docs/guides/structured-outputs
"""
import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import time

import httpx
from PIL import Image


class AIProviderError(RuntimeError):
    def __init__(self, category, status_code=502):
        self.category, self.status_code = category, status_code
        super().__init__('AI provider: ' + category)


def _key_valid(value):
    return isinstance(value,str) and 1 <= len(value) <= 8192 and not any(ch.isspace() or ord(ch)<33 or ord(ch)>126 for ch in value)


class OpenAIKeyManager:
    """Persistent key overrides env. Development fallback must be opted into."""
    def __init__(self,data_dir,project_root,allow_dev_key=False,key_file=None):
        self.path = Path(key_file) if key_file else Path(data_dir)/'openai.key'
        self.project_root, self.allow_dev_key = Path(project_root), allow_dev_key

    def _load(self):
        try:
            if self.path.exists():
                key=self.path.read_text(encoding='utf-8-sig').strip()
            elif os.environ.get('OPENAI_API_KEY'):
                key=os.environ['OPENAI_API_KEY'].strip()
            elif self.allow_dev_key and (self.project_root/'openai_key.txt').exists():
                key=(self.project_root/'openai_key.txt').read_text(encoding='utf-8-sig').strip()
            else:
                raise AIProviderError('key_not_configured',409)
        except (OSError,UnicodeError):
            raise AIProviderError('key_unreadable',503) from None
        if not _key_valid(key):
            raise AIProviderError('key_invalid',422)
        return key

    def configured(self):
        try:
            self._load()
            return True
        except AIProviderError:
            return False

    def set_key(self,key):
        # A trailing newline pasted into a form is whitespace, not a valid key.
        if not _key_valid(key):
            raise AIProviderError('key_invalid',422)
        temporary=self.path.with_name(self.path.name+'.tmp')
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                stream.write(key)
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.path.chmod(0o600)
        except OSError:
            raise AIProviderError('key_storage_failed',503) from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _documented_family(model):
    if not isinstance(model,str):return None
    match=re.fullmatch(r'(gpt-6-luna|gpt-6\.1-sol)(?:-\d{4}-\d{2}-\d{2})?',model)
    return match.group(1) if match else None


def preferred_model(models):
    """Choose only an actual account-listed, documented/verified model."""
    eligible=[entry['id'] for entry in models if entry.get('vision') in ('documented','verified')]
    return next((name for name in ('gpt-6-luna','gpt-6.1-sol') if name in eligible),eligible[0] if eligible else None)


def responses_input(messages):
    output=[]
    for message in messages:
        role=message.get('role')
        if role not in ('system','developer','user','assistant'):
            raise AIProviderError('unsupported_message',422)
        source=message.get('content')
        source=[{'type':'text','text':source}] if isinstance(source,str) else source
        if not isinstance(source,list):
            raise AIProviderError('unsupported_message',422)
        content=[]
        for part in source:
            if not isinstance(part,dict):
                raise AIProviderError('unsupported_message',422)
            if part.get('type')=='text' and isinstance(part.get('text'),str):
                item={'type':'output_text' if role=='assistant' else 'input_text','text':part['text']}
                if role=='assistant':item['annotations']=[]
                content.append(item)
            elif part.get('type')=='image_url' and role=='user':
                image=part.get('image_url')
                if not isinstance(image,dict) or not isinstance(image.get('url'),str):
                    raise AIProviderError('unsupported_message',422)
                item={'type':'input_image','image_url':image['url'],'detail':image.get('detail','auto')}
                if item['detail'] not in ('auto','low','high','original'):
                    raise AIProviderError('unsupported_message',422)
                content.append(item)
            else:
                raise AIProviderError('unsupported_message',422)
        item={'role':role,'content':content}
        if role=='assistant':item.update(type='message',status='completed')
        output.append(item)
    return output


def _validate(value,schema):
    """Validate the small JSON-schema subset used by rover decisions/probes."""
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError()
    kind=schema.get('type')
    if kind=='object':
        if not isinstance(value,dict) or not set(schema.get('required',[]))<=value.keys():raise ValueError()
        properties=schema.get('properties',{})
        if schema.get('additionalProperties') is False and not value.keys()<=properties.keys():raise ValueError()
        for name,item in value.items():
            if name in properties:_validate(item,properties[name])
    elif kind=='array':
        if not isinstance(value,list):raise ValueError()
        for item in value:_validate(item,schema.get('items',{}))
    elif kind in ('integer','number'):
        if type(value) not in ((int,) if kind=='integer' else (int,float)) or not math.isfinite(value):raise ValueError()
        if value<schema.get('minimum',-math.inf) or value>schema.get('maximum',math.inf):raise ValueError()
    elif kind=='boolean':
        if type(value) is not bool:raise ValueError()
    elif kind=='string':
        if not isinstance(value,str) or len(value)<schema.get('minLength',0) or len(value)>schema.get('maxLength',math.inf):raise ValueError()


class AIProvider:
    def __init__(self,client,data_dir,project_root,gemma_url='http://127.0.0.1:8080',key_file=None,allow_dev_key=False):
        self.client=client
        self.gemma_url=gemma_url.rstrip('/')
        self._keys=OpenAIKeyManager(data_dir,project_root,allow_dev_key,key_file)
        self._verified=set()
        self._probe_locks={}

    def configured(self):
        return self._keys.configured()

    def set_key(self,key):
        self._keys.set_key(key)
        self._verified.clear()

    def _identity(self,model):
        if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}',model):
            raise AIProviderError('invalid_model',422)
        key=self._keys._load()
        return hashlib.sha256(key.encode()).digest(),model

    async def _request(self,method,url,**kwargs):
        try:
            response=await self.client.request(method,url,follow_redirects=False,timeout=90,**kwargs)
        except httpx.TimeoutException:
            raise AIProviderError('timeout',504) from None
        except httpx.HTTPError:
            raise AIProviderError('connection_failed',502) from None
        if response.status_code>=300:
            status=response.status_code
            category={401:'authentication_failed',403:'access_denied',404:'model_unavailable',
                      429:'rate_limited'}.get(status,'upstream_error' if status>=500 else 'request_rejected')
            code=None
            if status==429:
                try:
                    details=response.json().get('error',{})
                    code=next((details.get(name) for name in ('code','type')
                               if details.get(name) in ('insufficient_quota','rate_limit_exceeded')),None)
                except (ValueError,AttributeError,TypeError):pass
                if code=='insufficient_quota':category='insufficient_quota'
            error=AIProviderError(category,status if status in (401,403,404,429) else 502)
            error.api_error_code=code
            raise error from None
        try:
            return response.json()
        except (ValueError,UnicodeError):
            raise AIProviderError('invalid_response',502) from None

    async def models(self):
        key=self._keys._load()
        result=await self._request('GET','https://api.openai.com/v1/models',headers={'Authorization':'Bearer '+key})
        if not isinstance(result,dict) or not isinstance(result.get('data'),list):
            raise AIProviderError('invalid_response',502)
        fingerprint=hashlib.sha256(key.encode()).digest()
        rows=[]
        for entry in result['data']:
            model=entry.get('id') if isinstance(entry,dict) else None
            if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}',model):continue
            vision=('verified' if (fingerprint,model) in self._verified else
                    'documented' if _documented_family(model) else 'unknown')
            rows.append({'id':model,'vision':vision,'available':True})
        return sorted(rows,key=lambda entry:(entry['vision']=='unknown',entry['id']))

    async def _responses(self,messages,schema,model,thinking=False,max_tokens=None):
        if max_tokens is not None and (type(max_tokens) is not int or not 128<=max_tokens<=4096):
            raise AIProviderError('request_rejected',422)
        if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}',model):
            raise AIProviderError('invalid_model',422)
        key=self._keys._load()
        family=_documented_family(model)
        effort='low' if family=='gpt-6.1-sol' or thinking else 'none'
        payload={'model':model,'store':False,'input':responses_input(messages),
                 'max_output_tokens':max_tokens or (2200 if effort=='low' and 'route_choice' in schema.get('properties',{}) else 1600 if effort=='low' else 1100 if 'route_choice' in schema.get('properties',{}) else 700),
                 'text':{'format':{'type':'json_schema','name':'rover_decision','strict':True,'schema':copy.deepcopy(schema)}}}
        if family:payload['reasoning']={'effort':effort}
        start=time.monotonic()
        result=await self._request('POST','https://api.openai.com/v1/responses',
                                   headers={'Authorization':'Bearer '+key},json=payload)
        if not isinstance(result,dict) or result.get('error'):
            raise AIProviderError('invalid_response',502)
        if result.get('status')!='completed' or result.get('incomplete_details'):
            error=AIProviderError('incomplete_response',502)
            details=result.get('incomplete_details')
            reason=details.get('reason') if isinstance(details,dict) else None
            error.incomplete_reason=reason if reason in ('max_output_tokens','content_filter') else 'unknown'
            raise error
        texts=[]
        if not isinstance(result.get('output'),list):raise AIProviderError('invalid_response',502)
        for item in result['output']:
            if not isinstance(item,dict):raise AIProviderError('invalid_response',502)
            if item.get('type')=='message':
                if item.get('role')!='assistant' or item.get('status','completed')!='completed':
                    raise AIProviderError('invalid_response',502)
                if not isinstance(item.get('content'),list):raise AIProviderError('invalid_response',502)
                for part in item['content']:
                    if not isinstance(part,dict):raise AIProviderError('invalid_response',502)
                    if part.get('type')=='refusal':raise AIProviderError('refused_response',502)
                    if part.get('type')=='output_text' and isinstance(part.get('text'),str):texts.append(part['text'])
        try:
            decision=json.loads(''.join(texts))
            if not isinstance(decision,dict):raise ValueError()
            _validate(decision,schema)
        except (ValueError,TypeError,OverflowError):
            raise AIProviderError('invalid_structured_output',502) from None
        metadata={'provider':'openai','model':model,'latency_ms':round((time.monotonic()-start)*1000),
                  'usage':self._usage(result.get('usage',{}))}
        return decision,metadata

    @staticmethod
    def _usage(raw):
        if not isinstance(raw,dict):return {}
        return {name:value for name,value in raw.items() if name in
                ('input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens')
                and type(value) is int and value>=0}

    async def probe(self,model):
        identity=self._identity(model)
        lock=self._probe_locks.setdefault(identity,asyncio.Lock())
        async with lock:
            if identity in self._verified:return {'id':model,'vision':'verified','cached':True}
            image=io.BytesIO()
            Image.new('RGB',(64,64),(255,0,0)).save(image,format='PNG')
            data='data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()
            schema={'type':'object','properties':{'dominant_color':{'type':'string','enum':['red','green','blue']}},
                    'required':['dominant_color'],'additionalProperties':False}
            answer,metadata=await self._responses([{'role':'user','content':[
                {'type':'text','text':'Inspect the image. Return its dominant color: red, green, or blue.'},
                {'type':'image_url','image_url':{'url':data,'detail':'low'}}]}],schema,model,max_tokens=512)
            if answer['dominant_color']!='red':
                raise AIProviderError('vision_probe_unverified',422)
            self._verified.add(identity)
            return {'id':model,'vision':'verified','cached':False,**metadata}

    async def infer(self,messages,schema,provider='gemma',model='gpt-6-luna',thinking=False,max_output_tokens=None,gemma_temperature=.45):
        if max_output_tokens is not None and (type(max_output_tokens) is not int or not 128<=max_output_tokens<=4096):
            raise AIProviderError('request_rejected',422)
        if provider=='openai':
            if not _documented_family(model) and self._identity(model) not in self._verified:
                raise AIProviderError('model_requires_vision_probe',409)
            return await self._responses(messages,schema,model,thinking,max_tokens=max_output_tokens)
        if provider!='gemma':raise AIProviderError('invalid_provider',422)
        if type(gemma_temperature) not in (int,float) or not math.isfinite(gemma_temperature) or not 0<=gemma_temperature<=2:
            raise AIProviderError('request_rejected',422)
        start=time.monotonic()
        result=await self._request('POST',self.gemma_url+'/v1/chat/completions',json={
            'messages':messages,'stream':False,'temperature':gemma_temperature,'max_tokens':max_output_tokens or (4096 if thinking else 700),
            'chat_template_kwargs':{'enable_thinking':bool(thinking)},
            'response_format':{'type':'json_schema','json_schema':{'name':'rover_decision','strict':True,'schema':schema}}})
        try:
            choice=result['choices'][0]
            if choice.get('finish_reason')!='stop' or choice['message'].get('refusal'):
                error=AIProviderError('incomplete_response',502)
                error.incomplete_reason='max_output_tokens' if choice.get('finish_reason')=='length' else 'content_filter' if choice['message'].get('refusal') or choice.get('finish_reason')=='content_filter' else 'unknown'
                raise error
            decision=json.loads(choice['message']['content'])
            if not isinstance(decision,dict):raise ValueError()
            _validate(decision,schema)
        except (ValueError,TypeError,KeyError,IndexError,OverflowError):
            raise AIProviderError('invalid_structured_output',502) from None
        return decision,{'provider':'gemma','model':result.get('model','gemma'),
                         'latency_ms':round((time.monotonic()-start)*1000),'usage':self._usage(result.get('usage',{}))}
