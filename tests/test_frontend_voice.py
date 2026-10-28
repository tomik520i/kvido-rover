"""Isolated voice scheduling checks; no browser, audio service or rover requests."""
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'server/static/app.js'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='Node.js is required for isolated frontend checks')


def run_js(tmp_path, code, *, include_pause=False):
    source = APP.read_text(encoding='utf-8')
    classes = source[source.index('class SpeechQueue {'):source.index('// End of independent voice scheduling.')]
    if include_pause:
        classes += source[source.index('async function pauseForVoice(){'):source.index('\nasync function startMic(){')]
    test = tmp_path / 'voice-check.cjs'
    test.write_text("const assert=require('node:assert/strict');\n" + classes +
                    '\nasync function main(){\n' + code + '\n}\nmain().catch(error=>{console.error(error);process.exitCode=1});\n',
                    encoding='utf-8')
    subprocess.run([NODE, str(test)], check=True, capture_output=True, text=True, timeout=15)


def test_speech_queue_serial_coalesced_bounded_and_deduplicated(tmp_path):
    run_js(tmp_path, """
const played=[], release=[];let active=0,maxActive=0;
const queue=new SpeechQueue(async text=>{played.push(text);active++;maxActive=Math.max(active,maxActive);await new Promise(resolve=>release.push(resolve));active--});
const first=queue.enqueue('first',{key:'1',kind:'progress',group:'task'});
queue.enqueue('old progress',{key:'2',kind:'progress',group:'task'});
queue.enqueue('new progress',{key:'3',kind:'progress',group:'task'});
queue.enqueue('new progress',{key:'3',kind:'progress',group:'task'});
assert.equal(queue.pending.length,1);assert.equal(queue.pending[0].text,'new progress');
queue.enqueue('completed',{key:'done',kind:'status',group:'task'});
assert.deepEqual(queue.pending.map(item=>item.text),['completed']);
release.shift()();await new Promise(setImmediate);assert.deepEqual(played,['first','completed']);
release.shift()();await first;await new Promise(setImmediate);assert.equal(maxActive,1);
for(let index=0;index<10;index++)queue.enqueue('reply '+index,{key:'reply'+index});
assert.ok(queue.pending.length<=3);queue.cancel();
""")


def test_cancel_aborts_current_and_discards_pending_without_replay(tmp_path):
    run_js(tmp_path, """
const played=[];let signal;
const queue=new SpeechQueue(async(text,current)=>{played.push(text);signal=current;await new Promise(resolve=>current.addEventListener('abort',resolve,{once:true}))});
const first=queue.enqueue('speaking',{key:'first'}),second=queue.enqueue('queued',{key:'second'});
queue.cancel();await Promise.all([first,second]);await new Promise(setImmediate);
assert.equal(signal.aborted,true);assert.deepEqual(played,['speaking']);assert.equal(queue.pending.length,0);
await queue.enqueue('queued',{key:'second'});assert.deepEqual(played,['speaking']);
let enabled=false;const muted=new SpeechQueue(async text=>played.push(text),{enabled:()=>enabled});
await muted.enqueue('muted',{key:'mute'});enabled=true;await muted.enqueue('muted',{key:'mute'});assert.deepEqual(played,['speaking']);
""")


def test_task_narration_uses_verified_results_and_real_status_transitions(tmp_path):
    run_js(tmp_path, """
const spoken=[];const narrator=new TaskNarrator((text,meta)=>spoken.push({text,meta}));
narrator.update({status:'idle'});
const task={id:'task-a',status:'running',phase:'planning',current_step:0};narrator.update(task);assert.equal(spoken.length,0);
const result={id:'step1',step:1,acceptance:'accepted',has_before:true,has_after:false,assessment:{status:'continue',summary:'Výsledek snímku.'}};
narrator.update({...task,last_result:result});assert.equal(spoken.length,0,'missing after image must not claim verification');
result.id='step2';result.has_after=true;result.acceptance='unknown';narrator.update({...task,last_result:result});assert.equal(spoken.length,0,'unknown command acceptance is not performed progress');
result.id='step3';result.acceptance='accepted';narrator.update({...task,last_result:result});narrator.update({...task,last_result:result});assert.equal(spoken.length,1);assert.match(spoken[0].text,/vyhodnocen/);
narrator.update({...task,status:'reconnecting',last_result:result});narrator.update({...task,status:'reconnecting',last_result:result});assert.equal(spoken.length,2);assert.match(spoken[1].text,/Stojím/);
narrator.update({...task,last_result:result});narrator.update({...task,status:'reconnecting',last_result:result});assert.equal(spoken.length,3,'a new disconnect episode should be reported once');
narrator.update({...task,status:'completed',outcome:'Dojetí ověřeno.',last_result:{...result,id:'step4'}});narrator.update({...task,status:'completed',last_result:{...result,id:'step4'}});assert.equal(spoken.length,4,'terminal result and completion must not repeat summary');assert.match(spoken[3].text,/dokončený/);
const historical=[];const fresh=new TaskNarrator(text=>historical.push(text));fresh.update({...task,status:'completed',last_result:result});assert.equal(historical.length,0,'page load should not replay historical task speech');
""")


def test_silent_observation_consumes_progress_and_reports_blocked_once(tmp_path):
    run_js(tmp_path, """
const spoken=[];const narrator=new TaskNarrator(text=>spoken.push(text));narrator.update({status:'idle'});
const task={id:'a',status:'running',last_result:{id:'r',step:1,acceptance:'accepted',has_before:true,has_after:true,assessment:{summary:'Nový pohled.'}}};
narrator.update(task,{silent:true});narrator.update(task);assert.equal(spoken.length,0,'hidden or microphone mode must not replay progress later');
narrator.update({...task,status:'blocked',outcome:'Čidlo není dostupné.'});narrator.update({...task,status:'blocked',outcome:'Čidlo není dostupné.'});assert.equal(spoken.length,1);assert.match(spoken[0],/Čidlo/);
""")


def test_voice_pause_requires_server_confirmation_before_proceeding(tmp_path):
    run_js(tmp_path, """
global.status={autonomy:{id:'a',status:'running'}};let releasePause;const calls=[];
global.setVoiceStatus=()=>{};global.renderTask=()=>{};
global.api=async(path,body)=>{calls.push(path);return new Promise(resolve=>releasePause=resolve)};
let ready=false;const pause=pauseForVoice().then(result=>{ready=result});await Promise.resolve();assert.deepEqual(calls,['autonomy/pause']);assert.equal(ready,false);
releasePause({id:'a',status:'paused'});await pause;assert.equal(ready,true);assert.equal(status.autonomy.status,'paused');
status.autonomy.status='running';global.api=async()=>({status:'running'});await assert.rejects(pauseForVoice(),/pozastavený/);
""", include_pause=True)
