"""Offline DOM/transport checks; no browser, running server or model calls."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest

PAGE = Path(__file__).resolve().parents[1] / 'server/static/sensor_experiment.html'
NODE = shutil.which('node')


class Assets(HTMLParser):
    def __init__(self):
        super().__init__(); self.external = []
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ('script', 'link') and (values.get('src') or values.get('href')):
            self.external.append(values)


def test_page_is_self_contained_and_camera_controls_are_in_overlay():
    source = PAGE.read_text(encoding='utf-8')
    parser = Assets(); parser.feed(source)
    assert not parser.external
    assert 'grid-template-columns:minmax(0,1fr) 320px' in source
    assert 'height:calc(100dvh - 72px)' in source
    assert source.index('id="pause"') < source.index('class="bottom"')
    assert 'žádné fyzické auto' in source
    assert 'ODHAD SONARU' in source and 'MAPA SVĚTA' in source


HARNESS = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const calls=[],timers=new Map(),elements=new Map(),listeners={},draws={};let timerId=0;
function element(id){
  const e={id,textContent:'',value:id==='goal'?'Najdi modré dveře':'',disabled:false,children:[],listeners:{},srcWrites:[],
    classList:{add(){}},getBoundingClientRect:()=>({width:320,height:210}),
    addEventListener(kind,fn){this.listeners[kind]=fn;},append(child){this.children.push(child);},
    replaceChildren(){this.children=[];},getContext(){return new Proxy({}, {get:(obj,key)=>obj[key]||((...args)=>{(draws[id]??=[]).push([key,...args]);}),set:(obj,key,value)=>{obj[key]=value;return true;}});}};
  Object.defineProperty(e,'src',{set(value){this.srcWrites.push(value);},get(){return this.srcWrites.at(-1);}});
  Object.defineProperty(e,'innerHTML',{set(){throw new Error('Unsafe HTML insertion');}});return e;
}
const state={status:'idle',phase:'ready',step:0,goal:'Najdi modré dveře',events:[]};
const context={console,AbortController,Date,Math,Number,JSON,encodeURIComponent,
 document:{getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);},
   createElement:tag=>element(tag),addEventListener:(kind,fn)=>listeners[kind]=fn,activeElement:null},
 window:{devicePixelRatio:1,addEventListener(){}},
 setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimeout:id=>timers.delete(id),
 fetch:async(path,options)=>{calls.push({path,options});return{ok:true,json:async()=>state};}};
vm.createContext(context);
'''


def run_js(tmp_path, checks):
    if not NODE:
        pytest.skip('Node.js required for isolated DOM checks')
    source = re.search(r'<script>(.*?)</script>', PAGE.read_text(encoding='utf-8'), re.S).group(1)
    import json
    program = HARNESS + '\nvm.runInContext(' + json.dumps(source) + ',context);\n' + '''
async function main(){await new Promise(setImmediate);
''' + checks + '\n}\nmain().catch(e=>{console.error(e);process.exitCode=1;});'
    path = tmp_path / 'sensor-ui.cjs'; path.write_text(program, encoding='utf-8')
    subprocess.run([NODE, str(path)], check=True, capture_output=True, text=True, timeout=10)


def test_initial_load_only_reads_status_images_and_deduplicates_image_refresh(tmp_path):
    run_js(tmp_path, r'''
assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/status');
assert.equal(calls[0].options.method,undefined,'loading must not start/resume/pause');
assert.ok([...timers.values()].some(t=>t.delay===500),'polling interval is 500 ms');
assert.match(elements.get('camera').src,/^\/camera.jpg\?v=/);
assert.match(elements.get('before').src,/^\/before.jpg\?v=/);
assert.match(elements.get('after').src,/^\/after.jpg\?v=/);
const writes=elements.get('camera').srcWrites.length;
context.render({...state});assert.equal(elements.get('camera').srcWrites.length,writes);
context.render({...state,phase:'observing'});assert.equal(elements.get('camera').srcWrites.length,writes+1);
''')


def test_explicit_start_resume_and_escape_pause_use_only_experiment_endpoints(tmp_path):
    run_js(tmp_path, r'''
elements.get('goal').value='Jiný cíl';await context.control('start');
assert.equal(calls.at(-1).path,'/api/start');
assert.deepEqual(JSON.parse(calls.at(-1).options.body),{goal:'Jiný cíl'});
await context.control('resume');assert.equal(calls.at(-1).path,'/api/resume');
assert.equal(calls.at(-1).options.body,'{}');
let prevented=false;listeners.keydown({key:'Escape',preventDefault(){prevented=true;}});
await new Promise(setImmediate);assert.ok(prevented);assert.equal(calls.at(-1).path,'/api/pause');
assert.equal(calls.at(-1).options.body,'{}');
assert.ok(calls.every(c=>['/api/status','/api/start','/api/resume','/api/pause'].includes(c.path)));
''')


def test_pause_is_not_blocked_by_pending_start_and_api_text_is_never_html(tmp_path):
    run_js(tmp_path, r'''
let release;context.fetch=(path,options)=>{calls.push({path,options});return path==='/api/start'
  ?new Promise(resolve=>release=()=>resolve({ok:true,json:async()=>state}))
  :Promise.resolve({ok:true,json:async()=>state});};
const start=context.control('start');await context.control('pause');
assert.equal(calls.at(-1).path,'/api/pause');release();await start;
const hostile='<img src=x onerror=alert(1)>';
context.render({...state,reason:hostile,perception:{description:hostile,evidence:hostile},events:[{step:1,action:'stop',reason:hostile}]});
assert.equal(elements.get('reason').textContent,hostile);
assert.equal(elements.get('description').textContent,hostile);
assert.equal(elements.get('events').children[0].children[1].textContent,hostile);
''')


def test_observer_geometry_does_not_change_estimated_map_or_navigator_data(tmp_path):
    run_js(tmp_path, r'''
const nav={estimated_pose:{x:.3,y:.4,heading:0},resolution_m:.2,
 cells:[{x:0,y:0,occupancy:-1},{x:3,y:0,occupancy:4}],visited_estimates:[[0,0],[.3,.4]],estimated_target:[3,0]};
const original=JSON.stringify(nav);draws.estimateMap=[];context.drawEstimate(nav);
const estimate=JSON.stringify(draws.estimateMap);draws.estimateMap=[];
context.drawWorld({geometry:{bounds:[0,0,9.4,6],walls:[[3,0,3,6]],boxes:[]},pose:{x:9,y:5,heading:2},checkpoints:[],collisions:4,goal_reached:true});
assert.equal(draws.estimateMap.length,0,'observer renderer must only touch observer canvas');
context.drawEstimate(nav);assert.equal(JSON.stringify(draws.estimateMap),estimate);
assert.equal(JSON.stringify(nav),original);assert.equal(elements.get('worldGoal').textContent,'ANO');
''')
