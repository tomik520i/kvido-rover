"""Run the dashboard against a fake DOM and transport; never contact a car."""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
APP = Path(__file__).resolve().parents[1] / "server/static/app.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js required")


def run_js(tmp_path, case):
    source = APP.read_text(encoding="utf-8")
    source = source.replace("import {startMicrophone} from './live-audio.js';", "")
    source = source.replace("openGarage().catch(()=>showLogin());", "")
    harness = r"""
import assert from 'node:assert/strict';
const elements=new Map(),listeners={},windowListeners={},calls=[],spoken=[];
function element(id){
 if(!elements.has(id))elements.set(id,{
  id,value:'',textContent:'',hidden:false,disabled:false,checked:false,style:{},options:[],children:[],
  classList:{toggle(){},add(){},remove(){},contains(){return false}},
  addEventListener(name,fn){this[name]=fn},setAttribute(){},removeAttribute(name){delete this[name]},
  append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},
  querySelector(){return element(id+' child')},getBoundingClientRect(){return {width:100,left:0,top:0}},
 });return elements.get(id);
}
const document={hidden:false,activeElement:null,body:element('body'),
 getElementById:element,createElement:()=>element('new'+elements.size),
 querySelector:element,querySelectorAll:()=>[],addEventListener:(name,fn)=>{listeners[name]=fn}};
const window={isSecureContext:true,addEventListener:(name,fn)=>{windowListeners[name]=fn}};
const navigator={};
let latestEvents;
class EventSource {constructor(){latestEvents=this}close(){}}
const setTimeout=()=>1,clearTimeout=()=>{},setInterval=()=>1,clearInterval=()=>{};
let remote={mode:'ai',mine:false,armed:false,telemetry:{connected:true,telemetry_age_ms:0},autonomy:{status:'idle'}};
const fetch=async(url,options={})=>{
 const path=new URL(url).pathname.split('/api/')[1];const body=options.body?JSON.parse(options.body):undefined;
 calls.push({path,body,options});
 if(path==='autonomy')remote={...remote,autonomy:{id:'new',status:'running',goal:body.goal,history:[]}};
 if(path==='stop')remote={...remote,autonomy:{...remote.autonomy,status:'cancelled'}};
 if(path==='autonomy/cancel')remote={...remote,autonomy:{...remote.autonomy,status:'cancelled'}};
 return {ok:true,json:async()=>path==='status'?remote:remote.autonomy};
};
element('cameraSource').value='car';
"""
    script = harness + source + "\nsay=async text=>spoken.push(text);\n" + "async function main(){\n" + case + "\n}\nawait main();"
    path = tmp_path / "frontend.mjs"
    path.write_text(script, encoding="utf-8")
    subprocess.run([NODE, str(path)], check=True, capture_output=True, text=True, timeout=10)


def test_reload_restores_server_goal_progress_without_old_speech(tmp_path):
    run_js(tmp_path, r"""
remote.autonomy={id:'persisted',status:'reconnecting',phase:'reconnecting',goal:'Najdi misku',current_step:7,
 history:[{at:1,kind:'step',text:'Hledám misku',say:true}]};
await refresh();
assert.equal(element('missionGoal').value,'Najdi misku');
assert.match(element('taskStep').textContent,/7/);
assert.match(element('taskResult').textContent,/Hledám misku/);
assert.match(element('taskLease').textContent,/Stránku můžeš zavřít/);
assert.equal(element('arm').disabled,true);assert.equal(canDrive(),false);
assert.deepEqual(spoken,[]);assert.ok(!calls.some(c=>['arm','drive','autonomy'].includes(c.path)));
remote.autonomy.history.push({at:2,kind:'step',text:'Pokračuji',say:'Pokračuji dál'});
await refresh();await refresh();assert.deepEqual(spoken,['Pokračuji dál']);
""")


def test_start_authorizes_server_without_manual_arm_or_radar_calls(tmp_path):
    run_js(tmp_path, r"""
await refresh();await startTask('  Najdi misku  ');
assert.deepEqual(calls.filter(c=>c.path==='autonomy').map(c=>c.body),[{goal:'Najdi misku'}]);
assert.ok(!calls.some(c=>c.path==='arm'||c.path==='drive'||c.path?.startsWith('autopilot')));
assert.equal(element('startMission').disabled,true);
await startTask('Jiný cíl');assert.equal(calls.filter(c=>c.path==='autonomy').length,1);
""")


def test_pagehide_releases_browser_without_stopping_server_task(tmp_path):
    run_js(tmp_path, r"""
remote.autonomy={id:'persisted',status:'running',goal:'Miska',history:[]};
await refresh();windowListeners.pagehide();await Promise.resolve();
assert.ok(calls.some(c=>c.path==='release'&&c.options.keepalive));
assert.ok(!calls.some(c=>['stop','drive','autonomy/cancel','autonomy/pause'].includes(c.path)));
assert.equal(remote.autonomy.status,'running');
await element('cancelMission').onclick();assert.equal(remote.autonomy.status,'cancelled');
assert.ok(calls.some(c=>c.path==='autonomy/cancel'));
""")


def test_stop_and_manual_deadman_remain_explicit(tmp_path):
    run_js(tmp_path, r"""
remote={...remote,mode:'manual',mine:true,armed:true};await refresh();
assert.equal(canDrive(),true);await drive('forward',100);stopDrive();
assert.ok(calls.some(c=>c.path==='drive'&&c.body.action==='forward'&&c.body.duration_ms===250));
assert.ok(calls.some(c=>c.path==='drive'&&c.body.action==='stop'));
remote.autonomy={status:'running',id:'task',history:[]};await refresh();
assert.equal(canDrive(),false);const before=calls.length;await element('arm').onclick();
assert.equal(calls.length,before);await emergency();
assert.ok(calls.some(c=>c.path==='stop'));assert.equal(remote.autonomy.status,'cancelled');
""")


def test_autonomy_events_render_without_fetching_vehicle_status(tmp_path):
    run_js(tmp_path, r"""
remote.autonomy={id:'persisted',status:'running',goal:'Miska',history:[]};
await openGarage();const before=calls.filter(c=>c.path==='status').length;
latestEvents.onmessage({data:JSON.stringify({type:'autonomy',autonomy:{...remote.autonomy,phase:'acting',current_step:4}})});
assert.equal(calls.filter(c=>c.path==='status').length,before,'server progress must not trigger an ESP telemetry request');
assert.equal(element('taskPhase').textContent,'Provádím krok');assert.match(element('taskStep').textContent,/4/);
latestEvents.onmessage({data:JSON.stringify({type:'autonomy',autonomy:{...remote.autonomy,status:'paused'}})});
assert.match(element('taskLease').textContent,/pozastavený/);
assert.equal(element('resumeMission').disabled,false);
assert.equal(element('taskComparison').querySelector('summary').textContent,'Průběh úkolu na serveru');
""")


def test_retry_shows_current_wait_reason_and_server_observation_state(tmp_path):
    run_js(tmp_path, r"""
remote={...remote,frame_count:0,telemetry:{connected:true,telemetry_age_ms:0,servo:72},
 autonomy:{id:'persisted',status:'running',phase:'observing',current_step:3,last_decision:'Rozhlížím se (1/14)',history:[]}};
await openGarage();
latestEvents.onmessage({data:JSON.stringify({type:'autonomy',autonomy:{...remote.autonomy,status:'reconnecting',reason:'Čekám na nový snímek po výpadku kamery.'}})});
assert.equal(element('taskDecision').textContent,'Čekám na nový snímek po výpadku kamery.');
assert.match(element('taskPhase').textContent,/Obnovuji spojení/);
assert.match(element('taskPhase').textContent,/nový pohled/);
assert.equal(element('frames').textContent,3);
assert.equal(element('tilt').disabled,true);assert.equal(Number(element('tilt').value),72);
assert.equal(element('tiltValue').textContent,'72°');assert.equal(element('angle').textContent,'72°');
assert.ok(!calls.some(c=>c.path==='drive'||c.path==='arm'),'rendering server state cannot move the camera');
""")
