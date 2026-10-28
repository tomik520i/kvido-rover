import {startMicrophone} from './live-audio.js';
// Derive the dashboard root from this module, including a public mount prefix.
const panelRoot=new URL('../',import.meta.url);
const apiUrl=path=>new URL('api/'+path,panelRoot).href;
// Voice scheduling is independent of DOM and never schedules vehicle actions.
class SpeechQueue {
 constructor(play,{enabled=()=>true,onError=()=>{}}={}){this.play=play;this.enabled=enabled;this.onError=onError;this.pending=[];this.running=false;this.active=null;this.seen=new Set()}
 enqueue(text,{key='',kind='reply',group=''}={}){
  text=String(text||'').trim().slice(0,600);if(!text)return Promise.resolve();
  if(key){if(this.seen.has(key))return Promise.resolve();this.seen.add(key);while(this.seen.size>128)this.seen.delete(this.seen.values().next().value)}
  if(!this.enabled())return Promise.resolve();
  return new Promise(resolve=>{
   if(group==='task')this.pending=this.pending.filter(item=>{const drop=item.group===group&&(kind==='status'||item.kind==='progress');if(drop)item.resolve();return !drop});
   this.pending.push({text,key,kind,group,resolve});while(this.pending.length>3)this.pending.shift().resolve();this.drain();
  });
 }
 async drain(){
  if(this.running)return;this.running=true;
  try{while(this.pending.length){const item=this.pending.shift();if(!this.enabled()){item.resolve();continue}const controller=new AbortController();this.active=controller;try{await this.play(item.text,controller.signal)}catch(error){if(!controller.signal.aborted)this.onError(error)}finally{item.resolve();if(this.active===controller)this.active=null}}}
  finally{this.running=false}
 }
 cancel(){for(const item of this.pending)item.resolve();this.pending=[];this.active?.abort()}
}
class TaskNarrator {
 constructor(emit){this.emit=emit;this.initialized=false;this.taskId='';this.resultId='';this.taskStatus='';this.transition=0}
 update(task,{silent=false}={}){
  const id=String(task.id||''),state=task.status||'idle',result=task.last_result;
  const resultId=String(result?.id||'');
  if(!this.initialized){this.initialized=true;this.taskId=id;this.resultId=resultId;this.taskStatus=state;return}
  if(id!==this.taskId){this.taskId=id;this.resultId='';this.taskStatus='';this.transition=0}
  const newResult=!!resultId&&resultId!==this.resultId,statusChanged=state!==this.taskStatus;
  if(statusChanged)this.transition++;
  this.resultId=resultId;this.taskStatus=state;
  if(silent||!id)return;
  const summary=String(result?.assessment?.summary||'').trim();
  const terminal=['completed','blocked'].includes(state);
  if(statusChanged&&(terminal||state==='reconnecting')){
   const prefix=state==='completed'?'Úkol je dokončený.':state==='blocked'?'Zastavil jsem. Úkol nemůže pokračovat.':'Spojení vypadlo. Stojím a obnovuji spojení se stejným cílem.';
   const detail=state==='reconnecting'?'':String(task.outcome||task.reason||summary||'').trim();
   this.emit([prefix,detail].filter(Boolean).join(' '),{key:`${id}:status:${state}:${this.transition}`,kind:'status',group:'task'});
  }else if(newResult&&!terminal&&result.has_before&&result.has_after&&result.assessment&&result.acceptance==='accepted'){
   this.emit(`Krok ${result.step||task.current_step||''} vyhodnocen. ${summary||'Nový pohled je ověřený.'}`,{key:`${id}:result:${resultId}`,kind:'progress',group:'task'});
  }
 }
}
// End of independent voice scheduling.
const $=id=>document.getElementById(id);
let micTimer=null, idleTimer=null;
let distanceTimer=null,telemetryReceivedAt=0,telemetryRequestMs=0;
function distanceReading(t,elapsed=0){
 const age=t?.telemetry_age_ms;
 if(!t||t.connected===false||typeof age!=='number'||!Number.isFinite(age)||age<0||(age>=999999&&t.distance_cm==null))return {text:'— cm',note:'Čekám na čidlo'};
 const effectiveAge=age+Math.max(0,elapsed);
 if(effectiveAge>3000)return {text:'— cm',note:'Měření není aktuální'};
 const cm=t.distance_cm;
 if(typeof cm!=='number'||!Number.isFinite(cm)||cm<=0||cm>400)return {text:'— cm',note:'Bez platného měření'};
 if(effectiveAge>500)return {text:Math.round(cm)+' cm',note:'Poslední měření',previous:true,remaining:3001-effectiveAge};
 return {text:Math.round(cm)+' cm',note:'Aktuální měření',remaining:501-effectiveAge};
}
function displayDistance(reading){$('distance').textContent=reading.text;$('distance').classList.toggle('distance-previous',!!reading.previous);$('distanceNote').textContent=reading.note}

function updateDistance(){
 clearTimeout(distanceTimer);distanceTimer=null;
 const reading=distanceReading(status.telemetry,performance.now()-telemetryReceivedAt+telemetryRequestMs);
 displayDistance(reading);
 if(reading.remaining!==undefined)distanceTimer=setTimeout(updateDistance,Math.max(1,reading.remaining));
}

let cameraRetry=null,cameraDeadline=null,cameraReadyCheck=null,cameraAttempt=0,cameraState='idle',cameraEnabled=false;
function isCarCamera(){return $('cameraSource').value!=='phone'}
function cameraBadge(){
 const phone=!isCarCamera();
 $('sourceBadge').textContent=phone?'TELEFON · TEST':cameraState==='live'?(status.simulation?'SIMULACE · ŽIVĚ':'KAMERA · ŽIVĚ'):cameraState==='retry'?'PŘENOS PŘERUŠEN':'KAMERA · OBNOVUJI';
 document.querySelector('.camera-card').classList.toggle('camera-offline',!phone&&cameraState!=='live');
}
function stopCamera(){
 clearTimeout(cameraRetry);clearTimeout(cameraDeadline);clearTimeout(cameraReadyCheck);cameraRetry=null;cameraDeadline=null;cameraReadyCheck=null;
 const image=$('carView');image.onload=null;image.onerror=null;image.removeAttribute('src');
 cameraState='idle';cameraBadge();
}
function cameraFailed(){
 if(!cameraEnabled||!isCarCamera())return;
 clearTimeout(cameraDeadline);clearTimeout(cameraReadyCheck);cameraDeadline=null;cameraReadyCheck=null;
 $('carView').onload=null;$('carView').onerror=null;$('carView').removeAttribute('src');
 cameraState='retry';$('cameraPlaceholder').hidden=false;
 $('cameraPlaceholder').textContent='Přenos přerušen. Obnovuji spojení s kamerou…';cameraBadge();
 if(!cameraRetry&&!document.hidden){
  const delay=Math.min(15000,1000*2**Math.min(cameraAttempt++,4));
  cameraRetry=setTimeout(()=>{cameraRetry=null;connectCamera()},delay);
 }
}
function connectCamera(){
 if(!cameraEnabled||!isCarCamera()||document.hidden)return;
 clearTimeout(cameraRetry);cameraRetry=null;clearTimeout(cameraDeadline);clearTimeout(cameraReadyCheck);
 const image=$('carView');image.onload=null;image.onerror=null;image.removeAttribute('src');
 cameraState='connecting';$('cameraPlaceholder').hidden=false;
 $('cameraPlaceholder').textContent='Obnovuji spojení s kamerou…';cameraBadge();
 image.onload=()=>{if(!cameraEnabled||!isCarCamera())return;clearTimeout(cameraDeadline);clearTimeout(cameraReadyCheck);cameraDeadline=null;cameraReadyCheck=null;cameraAttempt=0;cameraState='live';$('cameraPlaceholder').hidden=true;cameraBadge()};
 image.onerror=cameraFailed;
 cameraDeadline=setTimeout(cameraFailed,25000);
 image.src=apiUrl('stream?t='+Date.now());
 // MJPEG browsers may expose the first frame before firing load.
 const checkReady=()=>{if(cameraState!=='connecting'||!isCarCamera())return;if(image.naturalWidth>0)image.onload?.();else cameraReadyCheck=setTimeout(checkReady,250)};
 cameraReadyCheck=setTimeout(checkReady,250);
}
function restartCamera(){cameraAttempt=0;stopCamera();if(isCarCamera())connectCamera()}

let voiceTaskSuppressed=false,voicePauseRequired=false;
let providerName='AI';
let status={}, busy=false, phoneStream=null, mic=null, micStarting=false, micFinishing=false;
let audioContext=null, playing=null, events=null, polling=null, heartbeatTimer=null, driveTimer=null, driveBusy=false, audioGeneration=0;
const actionNames={stop:'Stát',forward:'Jízda vpřed',backward:'Couvnout',left:'Posun vlevo',right:'Posun vpravo',rotate_left:'Otočit vlevo',rotate_right:'Otočit vpravo',tilt:'Naklonit kameru',light_on:'Rozsvítit',light_off:'Zhasnout'};
function toast(text){$('toast').textContent=text;$('toast').classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').classList.remove('show'),5500)}
async function api(path,body,method='POST'){
 const r=await fetch(apiUrl(path),{method:body===undefined&&method==='POST'?'GET':method,headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 let d;try{d=await r.json()}catch{throw Error('Server neodpovídá.')}
 if(!r.ok){if(r.status===401&&path!=='login')showLogin();throw Error(typeof d.detail==='string'?d.detail:d.error||'Požadavek se nepodařil.')}return d;
}
function bubble(text,who='robot'){const el=document.createElement('div');el.className='bubble '+who;el.textContent=text;$('chat').append(el);$('chat').scrollTop=$('chat').scrollHeight;return el}
function setBusy(value){busy=value;$('send').disabled=value;$('snapshot').disabled=value;$('aiStep').disabled=value||taskActive();$('startMission').disabled=value||taskActive();$('mood').textContent=value?'Přemýšlím nad tím, co vidím…':'Připravený na malý výlet';document.querySelector('.avatar').classList.toggle('thinking',value)}
function showLogin(){voiceTaskSuppressed=true;stopSpeech();clearTimeout(distanceTimer);displayDistance(distanceReading(null));cameraEnabled=false;stopCamera();stopTimers();$('login').hidden=false;$('shell').hidden=true}
function stopTimers(){clearInterval(polling);clearInterval(heartbeatTimer);clearInterval(driveTimer);events?.close()}
async function refresh(){const requestAt=performance.now();try{status=await api('status');telemetryReceivedAt=performance.now();telemetryRequestMs=telemetryReceivedAt-requestAt;renderStatus()}catch(e){$('connection').textContent='Spojení přerušeno';$('connDot').classList.remove('on');clearTimeout(distanceTimer);displayDistance(distanceReading(null));cameraFailed()}}
function renderStatus(){
 $('carView').style.transform=status.camera_rotate_180?'rotate(180deg)':'none';
 const t=status.telemetry||{};
 $('lightToggle').disabled=taskActive()||!t.light_supported||!status.mine||!status.armed||status.mode!=='manual';$('lightToggle').textContent=t.light_supported?(t.light_on?'☼ Zhasnout':'☼ Rozsvítit'):'☼ Světlo nedostupné';
const route=status.transport==='relay'?'přes internet':'domácí síť';$('connection').textContent=status.simulation?'Simulátor připojen':(t.connected===false?'Auto nedostupné':'Auto připojeno')+' · '+route;$('connDot').classList.toggle('on',status.simulation||t.connected!==false);
 updateDistance();$('angle').textContent=(t.servo??90)+'°';$('rssi').textContent=t.rssi==null?'— dBm':t.rssi+' dBm';$('frames').textContent=status.autonomy?.id?(status.autonomy.current_step||0):(status.frame_count||0);
 $('environmentLabel').textContent=status.simulation?'SIMULÁTOR':'SKUTEČNÉ AUTO';cameraBadge();if(!status.simulation&&t.connected===false)cameraFailed();$('robotName').textContent=status.name;renderTask(status.autonomy||{});
 $('manualMode').classList.toggle('selected',status.mode==='manual');$('aiMode').classList.toggle('selected',status.mode==='ai');document.querySelector('.control-body').hidden=status.mode!=='manual';$('aiControls').hidden=status.mode!=='ai';
 $('controlTitle').textContent=status.mode==='manual'?'Ty řídíš, já se dívám.':'Kvído postupuje po jednotlivých krocích.';
 $('arm').textContent=status.mine&&status.armed?'Řízení aktivní':'Převzít řízení';$('arm').disabled=taskActive()||!!(status.mine&&status.armed);$('tilt').disabled=taskActive()||!status.mine||!status.armed||status.mode!=='manual';
 if($('tilt').disabled&&Number.isFinite(t.servo)){$('tilt').value=t.servo;$('tiltValue').textContent=t.servo+'°'}
 $('controlStatus').textContent=status.estop?'STOP je aktivní. Pro pokračování převezmi řízení.':status.mine&&status.armed?(status.simulation?'Řízení v simulaci. Skutečné motory se nehýbou.':'Řízení aktivní. Drž telefon otevřený a auto pod dohledem.'):'Řízení je vypnuté.';
 if(taskActive()){$('arm').disabled=true;$('arm').textContent='Úkol běží na serveru';$('controlStatus').textContent='Úkol zůstává aktivní i po zavření stránky. Pro ruční řízení stiskni STOP nebo změň režim.'}
 if(status.last_frame_at)$('lastFrame').textContent=new Date(status.last_frame_at*1000).toLocaleTimeString('cs-CZ');
}
async function openGarage(){
 await refresh();if(!status.mode)return;
 $('login').hidden=true;$('shell').hidden=false;
 cameraEnabled=true;restartCamera();
 stopTimers();polling=setInterval(refresh,1800);heartbeatTimer=setInterval(()=>{if(!document.hidden&&(status.mine&&status.armed&&!taskActive()))api('heartbeat',{}).catch(()=>{})},400);
 events=new EventSource(apiUrl('events'));events.onmessage=e=>{const d=JSON.parse(e.data);if(d.type==='frame_sent')shutter(d);if(d.type==='task'){refresh()}if(d.type==='autonomy'&&d.autonomy){status.autonomy=d.autonomy;renderStatus()}if(d.type==='replanning'){bubble('Řadič: '+d.message);$('mood').textContent='Opakovaný návrh odmítnut. Hledám jinou možnost…'}if(d.type==='stopped'){toast(d.reason);refresh()}if(d.type==='error'){toast(d.message);if(/kamery|kamera/i.test(d.message||''))cameraFailed()}};
 api('services').then(d=>{for(const k of ['llm','stt','tts']){$(k+'Service').classList.toggle('ok',!!d[k]);$(k+'Service').textContent=(d[k]?'● ':'○ ')+({llm:providerName,stt:'Sluch',tts:'Hlas'}[k])}}).catch(()=>{});
 await loadSettings();await loadMemory(false);
 $('secureStatus').textContent=window.isSecureContext?'✓ Zabezpečené spojení. Telefon může zpřístupnit mikrofon a kameru.':'Pro hlas na telefonu otevři důvěryhodnou HTTPS adresu. Na počítači lze testovat přes localhost.';
 if(!navigator.mediaDevices?.getUserMedia||!window.isSecureContext)$('voiceNote').textContent='Mikrofon potřebuje důvěryhodné HTTPS (nebo localhost). Text a kamera auta fungují i bez něj.';
}
$('loginForm').onsubmit=async e=>{e.preventDefault();try{await api('login',{key:$('loginKey').value});$('loginKey').value='';$('loginError').textContent='';await openGarage()}catch(e){$('loginError').textContent=e.message}};
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.page').forEach(p=>p.hidden=p.id!==b.dataset.tab);document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n===b));if(b.dataset.tab==='memory')loadMemory();if(b.dataset.tab!=='drive')stopDrive()});
async function unlockAudio(){audioContext||=new AudioContext();if(audioContext.state==='suspended')await audioContext.resume()}
function shutter(d){$('flash').classList.remove('fire');void $('flash').offsetWidth;$('flash').classList.add('fire');$('lastFrame').textContent=new Date().toLocaleTimeString('cs-CZ')+' · '+(status.ai_provider==='openai'?status.ai_model:'Gemma');$('frames').textContent=status.autonomy?.id?(status.autonomy.current_step||0):d.count;
 if($('shutterSound').checked&&audioContext?.state==='running'){const o=audioContext.createOscillator(),g=audioContext.createGain();o.type='square';o.frequency.setValueAtTime(650,audioContext.currentTime);o.frequency.exponentialRampToValueAtTime(100,audioContext.currentTime+.05);g.gain.setValueAtTime(.025,audioContext.currentTime);g.gain.exponentialRampToValueAtTime(.001,audioContext.currentTime+.07);o.connect(g).connect(audioContext.destination);o.start();o.stop(audioContext.currentTime+.08)}
}
const speechQueue=new SpeechQueue(playSpeech,{enabled:()=>!voiceTaskSuppressed&&!document.hidden&&$('speak').checked&&!mic&&!micStarting&&!micFinishing,onError:error=>{setVoiceStatus('Hlas není dostupný. Text zůstává v panelu.');toast(error.message)}});
const taskNarrator=new TaskNarrator((text,options)=>{bubble(text);speechQueue.enqueue(text,options)});
function stopSpeech(){audioGeneration++;speechQueue.cancel();if(playing){try{playing.stop()}catch{}playing=null}if(!mic&&!micStarting&&!micFinishing)setVoiceStatus($('speak').checked?'Mikrofon připravený · STT':'Hlas vypnutý · mikrofon STT je připravený')}
function say(text,options={}){return speechQueue.enqueue(text,options)}
async function playSpeech(text,signal){
 if(signal.aborted)return;
 setVoiceStatus('Kvído mluví…');
 const response=await fetch(apiUrl('tts'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text}),signal});
 if(!response.ok)throw Error('Hlas je nedostupný. Odpověď máš v panelu.');
 const bytes=await response.arrayBuffer();if(signal.aborted)return;
 await unlockAudio();const buffer=await audioContext.decodeAudioData(bytes);if(signal.aborted)return;
 await new Promise(resolve=>{
  const source=audioContext.createBufferSource();source.buffer=buffer;source.connect(audioContext.destination);playing=source;
  const finish=()=>{signal.removeEventListener('abort',cancel);if(playing===source)playing=null;resolve()};
  const cancel=()=>{try{source.stop()}catch{}finish()};
  source.onended=finish;signal.addEventListener('abort',cancel,{once:true});if(signal.aborted){cancel();return}source.start();
 });
 if(!signal.aborted)setVoiceStatus('Mikrofon připravený · STT');
}
function setVoiceStatus(text){$('voiceStatus').textContent=text;$('voiceNote').textContent=text}
function syncMute(){const enabled=$('speak').checked;$('cockpitMute').textContent=enabled?'Hlas zapnutý':'Hlas vypnutý';$('cockpitMute').setAttribute('aria-pressed',String(!enabled));if(!enabled)stopSpeech()}
$('speak').onchange=syncMute;$('cockpitMute').onclick=async()=>{$('speak').checked=!$('speak').checked;syncMute();if($('speak').checked){try{await unlockAudio()}catch{}}};

async function cameraPayload(){
 if($('cameraSource').value!=='phone')return {source:'car'};
 const v=$('phoneView');if(!phoneStream||!v.videoWidth)throw Error('Kamera telefonu ještě není připravená.');
 const c=document.createElement('canvas');c.width=640;c.height=Math.round(v.videoHeight*640/v.videoWidth);c.getContext('2d').drawImage(v,0,0,c.width,c.height);return {source:'phone',image:c.toDataURL('image/jpeg',.8)};
}
async function turn(text,vision=true,act=false){
 if(busy||(act&&taskActive()))return;voiceTaskSuppressed=false;stopDrive();stopSpeech();
 if(mic){clearTimeout(micTimer);clearInterval(idleTimer);const m=mic;mic=null;await m.cancel();renderMic(false)}
 setBusy(true);bubble(text,'user');
 try{const input=vision?await cameraPayload():{source:'car'};const d=await api('turn',{text,vision,intent:act?'act':'observe',...input});bubble(d.say);$('decision').replaceChildren();const label=document.createElement('span');label.textContent=(d.simulation?'SIMULACE · ':'')+(actionNames[d.executed_action]||d.executed_action).toUpperCase();const p=document.createElement('p');p.textContent=d.reason+' '+d.execution_reason;$('decision').append(label,p);await say(d.say)}catch(e){bubble(e.message,'error');toast(e.message)}finally{setBusy(false);await refresh();scheduleNext()}
}
function explorationRequest(){return status.navigation?.policy==='sensor'?'Jsi zvědavý průzkumník: pokračuj v uloženém úkolu a získej nový pohled jízdou nebo otočením. Podle aktuálního čidla zvol jeden pohyb kol; další náklon ani světlo nejsou povinné. Dodrž STOP, konkrétní viditelné nebezpečí a limity řadiče.':'Rozhlédni se a zvol jeden krátký opatrný krok. Při nejistotě zůstaň stát.'}
function scheduleNext(){
 if(document.hidden)return;
 // Conversation can listen again; task progression belongs entirely to the server.
 if($('conversation').checked&&!mic&&!micFinishing)setTimeout(()=>{if(!busy&&!playing&&$('conversation').checked&&!document.hidden)startMic()},450);
}
async function submitText(text){await pauseForVoice();if(status.mode==='ai')await startTask(text);else await turn(text,true,false)}
$('chatForm').onsubmit=async e=>{e.preventDefault();if(mic){await finishMic(true);return}const text=$('message').value.trim();if(!text||busy)return;try{await unlockAudio();$('message').value='';await submitText(text)}catch(error){toast(error.message)}};
$('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('chatForm').requestSubmit()}};
$('snapshot').onclick=async()=>{await unlockAudio();turn('Popiš, co právě vidíš. Zohledni čidlo vzdálenosti. Tohle je jen prohlídka, zůstaň stát.')};
$('aiStep').onclick=async()=>{await unlockAudio();turn(explorationRequest(),true,true)};
for(const [id,mode] of [['manualMode','manual'],['aiMode','ai']])$(id).onclick=async()=>{stopDrive();try{await api('mode',{mode});await refresh()}catch(e){toast(e.message)}};
$('arm').onclick=async()=>{if(taskActive())return;try{await api('arm',{});await refresh()}catch(e){toast(e.message)}};
let joystickPointer=null,driveIntent=null,driveGeneration=0;
function canDrive(){return status.mode==='manual'&&status.mine&&status.armed&&!status.estop&&!taskActive()}
function joystickMotion(x,y,cap){
 const magnitude=Math.min(1,Math.hypot(x,y));
 if(magnitude<.18)return null;
 const directions=['right','back_right','backward','back_left','left','forward_left','forward','forward_right'];
 const index=(Math.round(Math.atan2(y,x)/(Math.PI/4))+8)%8;
 const maxSpeed=Math.max(80,Math.min(180,Number(cap)||130));
 return {action:directions[index],speed:Math.round(55+(maxSpeed-55)*(magnitude-.18)/.82)};
}
async function drive(action,speed=Number($('speed').value)){
 if(driveBusy||!canDrive())return;
 driveBusy=true;const generation=driveGeneration;
 try{await api('drive',{action,speed:Math.max(0,Math.min(180,speed)),duration_ms:250,tilt:Number($('tilt').value)})}
 catch(e){stopDrive();toast(e.message)}
 finally{driveBusy=false;if(generation!==driveGeneration&&status.mine&&status.mode==='manual'&&!taskActive())api('drive',{action:'stop'}).catch(()=>{})}
}
function stopDrive(){
 driveGeneration++;clearInterval(driveTimer);driveTimer=null;driveIntent=null;
 $('joystickKnob').style.transform='translate(0px,0px)';$('joystick').classList.remove('dragging');
 if(status.mine&&status.armed&&status.mode==='manual'&&!taskActive())api('drive',{action:'stop'}).catch(()=>{});
}
function keepDriving(intent){
 driveIntent=intent;
 if(!intent){stopDrive();return}
 if(!driveTimer){drive(intent.action,intent.speed);driveTimer=setInterval(()=>{if(!canDrive()){stopDrive();return}if(driveIntent)drive(driveIntent.action,driveIntent.speed)},180)}
}
function joystickPosition(e){
 const bounds=$('joystick').getBoundingClientRect(),radius=bounds.width*.35;
 const x=(e.clientX-bounds.left-bounds.width/2)/radius,y=(e.clientY-bounds.top-bounds.height/2)/radius;
 const scale=Math.max(1,Math.hypot(x,y)),nx=x/scale,ny=y/scale;
 keepDriving(joystickMotion(nx,ny,$('speed').value));
 $('joystickKnob').style.transform=`translate(${nx*radius}px,${ny*radius}px)`;
 $('joystick').classList.add('dragging');
}
$('joystick').onpointerdown=e=>{if(!canDrive()){toast('Nejdřív v ručním režimu převezmi řízení.');return}e.preventDefault();joystickPointer=e.pointerId;$('joystick').setPointerCapture(e.pointerId);joystickPosition(e)};
$('joystick').onpointermove=e=>{if(e.pointerId===joystickPointer){e.preventDefault();joystickPosition(e)}};
function releaseJoystick(e){if(e.pointerId!==joystickPointer)return;joystickPointer=null;stopDrive()}
for(const event of ['pointerup','pointercancel','lostpointercapture'])$('joystick').addEventListener(event,releaseJoystick);
document.querySelectorAll('[data-move]').forEach(b=>{b.onpointerdown=e=>{e.preventDefault();if(!canDrive()){toast('Nejdřív v ručním režimu převezmi řízení.');return}b.setPointerCapture(e.pointerId);stopDrive();keepDriving({action:b.dataset.move,speed:Number($('speed').value)})};b.onpointerup=stopDrive;b.onpointercancel=stopDrive;b.onlostpointercapture=stopDrive});
$('speed').oninput=()=>{$('speedValue').textContent=$('speed').value;if(driveIntent)driveIntent.speed=Math.min(driveIntent.speed,Number($('speed').value))};$('tilt').oninput=()=>{$('tiltValue').textContent=$('tilt').value+'°'};$('tilt').onchange=()=>drive('tilt');
async function emergency(){voiceTaskSuppressed=true;voicePauseRequired=false;stopDrive();stopSpeech();cancelMicrophone();try{await api('stop',{});await refresh()}catch(e){toast(e.message)}}
$('emergency').onclick=emergency;
function leave(){voiceTaskSuppressed=true;voicePauseRequired=false;joystickPointer=null;stopDrive();stopCamera();clearTimeout(micTimer);clearInterval(idleTimer);clearInterval(driveTimer);$('conversation').checked=false;stopSpeech();cancelMicrophone();phoneStream?.getTracks().forEach(t=>t.stop());phoneStream=null;fetch(apiUrl('release'),{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',keepalive:true}).catch(()=>{})}
document.addEventListener('visibilitychange',()=>{if(document.hidden)leave();else{voiceTaskSuppressed=false;refresh();updateDistance();if(cameraEnabled&&isCarCamera())connectCamera()}});window.addEventListener('pagehide',leave);window.addEventListener('blur',stopDrive);
$('cameraSource').onchange=async()=>{stopCamera();phoneStream?.getTracks().forEach(t=>t.stop());phoneStream=null;const phone=$('cameraSource').value==='phone';$('phoneView').hidden=!phone;$('carView').hidden=phone;
 try{if(phone){if(!window.isSecureContext)throw Error('Kamera telefonu potřebuje důvěryhodné HTTPS.');phoneStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:1280}},audio:false});$('phoneView').srcObject=phoneStream;await $('phoneView').play();$('cameraPlaceholder').hidden=true}else restartCamera()}catch(e){toast(e.message);$('cameraPlaceholder').hidden=false;$('cameraPlaceholder').textContent='Povol kameru telefonu, nebo přepni na autíčko.'}renderStatus()};
function renderMic(recording){
 $('message').readOnly=recording;
 for(const id of ['mic','cockpitMic']){const button=$(id);button.classList.toggle('recording',recording);button.querySelector('span').textContent=micFinishing?'Přepisuji…':micStarting?'Zapínám…':recording?'Dokončit':'Mluvit';button.setAttribute('aria-label',recording?'Dokončit nahrávání a získat přepis STT':'Mluvit: zapnout mikrofon a přepis STT');button.setAttribute('aria-pressed',String(recording));button.disabled=micStarting||micFinishing}
 $('cockpitMic').classList.toggle('processing',micFinishing||micStarting);
 if(!recording)$('micLevel').style.width='0';
}
function cancelMicrophone(){clearTimeout(micTimer);clearInterval(idleTimer);mic?.cancel();mic=null;renderMic(false);setVoiceStatus('Mikrofon připravený · STT')}
async function pauseForVoice(){
 if(!['running','reconnecting'].includes(status.autonomy?.status))return false;
 setVoiceStatus('Pozastavuji úkol, potom zapnu mikrofon…');
 const task=await api('autonomy/pause',{});
 // The server confirms a pause before we acquire any microphone or submit text.
 if(task.status!=='paused')throw Error('Úkol není pozastavený. Mikrofon se nezapnul.');
 status.autonomy={...status.autonomy,...task};renderTask(status.autonomy);return true;
}
async function startMic(){
 if(mic||micStarting||micFinishing||busy||document.hidden)return;
 if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia){setVoiceStatus('Mikrofon potřebuje HTTPS a povolení telefonu.');toast('Mikrofon potřebuje HTTPS. Na počítači můžeš použít localhost.');return}
 micStarting=true;stopSpeech();renderMic(false);
 try{
  await unlockAudio();voicePauseRequired=await pauseForVoice();
  if(document.hidden||voiceTaskSuppressed){micStarting=false;renderMic(false);return}
  setVoiceStatus('Povoluji mikrofon…');
  const recorder=await startMicrophone({endpoint:apiUrl('stt?live=1'),onText:text=>{$('message').value=text;$('voiceTranscript').value=text},onState:(state,error)=>{setVoiceStatus(state);if(error)toast(state)},onLevel:data=>{$('micLevel').style.width=Math.round(data.level*55)+'px'},onPause:()=>{if($('conversation').checked)finishMic(true)}});
  if(document.hidden||voiceTaskSuppressed){await recorder.cancel();return}
  mic=recorder;micStarting=false;renderMic(true);setVoiceStatus('Nahrávám · STT přepisuje řeč. Stiskni Dokončit.');
  micTimer=setTimeout(()=>finishMic($('conversation').checked),90000);
  idleTimer=setInterval(()=>{if(mic&&mic.engine.elapsed>15&&mic.engine.speechSeconds<.3){cancelMicrophone();toast('Je tu ticho. Až budeš chtít mluvit, zapni mikrofon.')}},500);
 }catch(error){setVoiceStatus(error.message);toast(error.message)}finally{micStarting=false;renderMic(!!mic)}
}
async function finishMic(send){
 if(!mic||micFinishing)return;clearTimeout(micTimer);clearInterval(idleTimer);micFinishing=true;const recorder=mic;mic=null;renderMic(false);setVoiceStatus('Zpracovávám řeč · STT…');
 try{const text=await recorder.stop();if(document.hidden||voiceTaskSuppressed)return;$('message').value=text;$('voiceTranscript').value=text;
  if(!text.trim()){setVoiceStatus('Nerozpoznal jsem řeč. Zkus to znovu.');return}
  $('voiceDraft').hidden=false;
  if(voicePauseRequired){setVoiceStatus('Úkol je pozastavený. Zkontroluj přepis a odešli ho ručně.');$('conversation').checked=false}
  else if(send){micFinishing=false;renderMic(false);$('message').value='';$('voiceDraft').hidden=true;await submitText(text);setVoiceStatus('Zpráva odeslaná. Mikrofon je připravený.')}
  else setVoiceStatus('Přepis je připravený. Zkontroluj ho a odešli.');
 }catch(error){setVoiceStatus(error.message);toast(error.message)}finally{micFinishing=false;renderMic(false);if(send&&!voicePauseRequired)scheduleNext()}
}
async function toggleMicrophone(){if(!mic&&!micStarting&&!micFinishing)voiceTaskSuppressed=false;if(mic)await finishMic(false);else await startMic()}
$('mic').onclick=toggleMicrophone;$('cockpitMic').onclick=toggleMicrophone;
$('conversation').onchange=()=>{if($('conversation').checked){voiceTaskSuppressed=false;startMic()}else if(mic)finishMic(false)};
$('voiceSend').onclick=async()=>{const text=$('voiceTranscript').value.trim();if(!text||busy)return;try{await unlockAudio();await pauseForVoice();$('voiceDraft').hidden=true;$('message').value='';voicePauseRequired=false;await submitText(text)}catch(error){setVoiceStatus(error.message);toast(error.message)}};
$('voiceDismiss').onclick=()=>{$('voiceDraft').hidden=true;$('voiceTranscript').value='';setVoiceStatus('Mikrofon připravený · STT')};

async function loadSettings(){const s=await api('settings');$('name').value=s.name;$('deviceUrl').value=s.device_url;$('transport').value=s.transport||'lan';$('simulation').checked=s.simulation;$('navigationPolicy').value=s.navigation_policy||'strict';$('thinkingEnabled').checked=s.thinking_enabled===true;$('floorAngle').value=s.floor_angle;$('floorCalibrated').checked=s.floor_calibrated;$('cameraRotate180').checked=s.camera_rotate_180;$('deviceKey').placeholder=s.has_device_key?'Klíč je uložený. Prázdné pole jej zachová.':'Párovací klíč auta';$('aiProvider').value=s.ai_provider||'gemma';updateProviderLabel();selectModel(s.openai_model||'');$('keyNote').textContent=s.has_openai_key?'Klíč OpenAI je uložený na serveru. Nový klíč ho nahradí.':'Klíč OpenAI zatím není uložený.';$('llmUrl').textContent=s.llm_url;$('voiceUrl').textContent=s.voice_url}
$('settingsForm').onsubmit=async e=>{e.preventDefault();try{await api('settings',{name:$('name').value,transport:$('transport').value,device_url:$('deviceUrl').value,device_key:$('deviceKey').value,simulation:$('simulation').checked,navigation_policy:$('navigationPolicy').value,thinking_enabled:$('thinkingEnabled').checked,ai_provider:$('aiProvider').value,openai_model:$('openaiModel').value,floor_angle:Number($('floorAngle').value),floor_calibrated:$('floorCalibrated').checked,camera_rotate_180:$('cameraRotate180').checked});$('deviceKey').value='';updateProviderLabel();$('saveNote').textContent='Uloženo. Před jízdou znovu převezmi řízení.';await refresh();restartCamera()}catch(e){toast(e.message)}};
$('importConnection').onchange=async e=>{try{const file=e.target.files[0];if(!file||file.size>10000)throw Error('Neplatný soubor připojení.');const d=JSON.parse(await file.text());if(d.kind!=='rover-connection-v1'||!d.device_url||!d.device_key)throw Error('Tohle není párovací soubor auta.');$('deviceUrl').value=d.device_url;$('deviceKey').value=d.device_key;toast('Připojení načteno. Zkontroluj simulaci a ulož nastavení.')}catch(e){toast(e.message)}};
$('downloadLink').onclick=()=>{const data='[InternetShortcut]\r\nURL='+panelRoot.href+'\r\n';const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([data],{type:'application/octet-stream'}));a.download='Kvido-garaz.url';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
async function loadMemory(renderHistory=true){try{const d=await api('memory');$('memories').replaceChildren();if(!d.items.length)$('memories').textContent='Zatím prázdno. První vzpomínky teprve přijdou.';for(const m of d.items){const el=document.createElement('article');el.className='card memory-item';const small=document.createElement('small');small.textContent=m.source+' · '+new Date(m.at*1000).toLocaleString('cs-CZ');const p=document.createElement('p');p.textContent=m.text;const b=document.createElement('button');b.textContent='Zapomenout';b.onclick=async()=>{await api('memory/'+m.id,undefined,'DELETE');loadMemory()};el.append(small,p,b);$('memories').append(el)}if(!renderHistory&&d.history.length){$('chat').replaceChildren();for(const m of d.history)bubble(m.content,m.role==='user'?'user':'robot')}}catch(e){toast(e.message)}}
$('memoryForm').onsubmit=async e=>{e.preventDefault();try{await api('memory',{text:$('memoryText').value});$('memoryText').value='';loadMemory()}catch(e){toast(e.message)}};
$('forget').onclick=async()=>{if(confirm('Vymazat všechny vzpomínky, historii rozhovoru i rozhodnutí?'))try{await api('forget',{});$('chat').replaceChildren();loadMemory()}catch(e){toast(e.message)}};
$('logout').onclick=async()=>{leave();await api('logout',{});showLogin()};
openGarage().catch(()=>showLogin());

function taskActive(){return ['running','reconnecting','paused'].includes(status.autonomy?.status)}
const autonomyHistory={initialized:false,id:'',seen:new Set()};
function textValue(value){if(value==null)return '';if(typeof value==='string'||typeof value==='number')return String(value);return value.description||value.summary||value.reason||value.action||value.status||''}
function renderTask(task){
 const phases={idle:'Připravený',running:'Probíhá',starting:'Začínám',acting:'Provádím krok',planning:'Přemýšlím',observing:'Rozhlížím se',executing:'Provádím krok',reconnecting:'Obnovuji spojení',paused:'Pozastaveno',completed:'Dokončeno',failed:'Potřebuji pomoc',cancelled:'Zrušeno'};
 const active=['running','reconnecting','paused'].includes(task.status);
 $('taskPhase').textContent=task.status==='reconnecting'?(task.phase==='observing'?'Obnovuji spojení · nový pohled':'Obnovuji spojení'):(phases[task.phase]||phases[task.status]||task.phase||'Připravený');
 $('missionStatus').textContent=task.goal||'Zadej cíl. Úkol poběží na serveru.';
 if(task.goal&&document.activeElement!==$('missionGoal'))$('missionGoal').value=task.goal;
 $('taskStep').textContent=task.current_step?`Krok ${textValue(task.current_step)}`:'';
 $('taskLease').textContent=task.status==='paused'?'Úkol je pozastavený na serveru. Stránku můžeš zavřít; pokračování spustíš tlačítkem Pokračovat.':active?'Úkol běží na serveru. Stránku můžeš zavřít; po výpadku se auto znovu připojí. STOP nebo Zrušit úkol jej ukončí.':'Úkol se ukládá na serveru a může pokračovat i po zavření stránky.';
 $('pauseMission').disabled=!['running','reconnecting'].includes(task.status);
 $('resumeMission').disabled=task.status!=='paused';$('cancelMission').disabled=!active;
 $('startMission').disabled=busy||active;$('aiStep').disabled=busy||active;
 $('message').placeholder=status.mode==='ai'?'Zadej Kvídovi úkol…':'Co je přede mnou?';
 const decision=task.status==='reconnecting'?(task.reason||textValue(task.last_decision)||''):(textValue(task.last_decision)||task.reason||'');
 $('taskDecision').textContent=decision;$('taskDecision').hidden=!decision;$('taskController').hidden=true;
 $('taskComparison').querySelector('summary').textContent='Průběh úkolu na serveru';
 $('taskComparison').querySelector('.frame-comparison').hidden=true;
 const history=task.history||[];
 $('taskResult').textContent=history.map(item=>item.text).filter(Boolean).slice(-8).join(' · ')||task.reason||'Průběh úkolu se objeví po spuštění.';
 $('beforeFrame').removeAttribute('src');$('afterFrame').removeAttribute('src');
 const first=!autonomyHistory.initialized;
 if(autonomyHistory.id!==String(task.id||'')){autonomyHistory.id=String(task.id||'');autonomyHistory.seen.clear()}
 for(const item of history){
  const key=JSON.stringify([item.at,item.kind,item.text,item.say]);if(autonomyHistory.seen.has(key))continue;
  autonomyHistory.seen.add(key);
  if(!first&&item.say&&!document.hidden&&!voiceTaskSuppressed&&!$('shell').hidden)say(typeof item.say==='string'?item.say:item.text,{key:`${task.id}:${key}`,group:'task',kind:'progress'});
 }
 autonomyHistory.initialized=true;
}
async function startTask(goal){
 if(!goal.trim()||busy)return;
 if(!isCarCamera()){toast('Pro úkol přepni na kameru auta.');return}
 if(taskActive()){toast('Nejdřív zastav nebo zruš běžící úkol.');return}
 voiceTaskSuppressed=false;stopDrive();setBusy(true);bubble(goal,'user');
 try{$('missionGoal').value=goal.trim();await api('autonomy',{goal:goal.trim()});await refresh()}
 catch(e){bubble(e.message,'error');toast(e.message)}finally{setBusy(false)}
}
$('taskForm').onsubmit=async e=>{e.preventDefault();await unlockAudio();await startTask($('missionGoal').value)};
for(const [id,action] of [['pauseMission','pause'],['resumeMission','resume'],['cancelMission','cancel']])$(id).onclick=async()=>{try{if(action==='resume')voiceTaskSuppressed=false;else stopSpeech();await api('autonomy/'+action,{});await refresh()}catch(e){toast(e.message)}};
function setChatOpen(open){$('companion').hidden=!open;$('chatToggle').setAttribute('aria-expanded',String(open));$('chatToggle').classList.toggle('selected',open)}
$('chatToggle').onclick=()=>setChatOpen($('companion').hidden);$('chatClose').onclick=()=>setChatOpen(false);
async function exitCockpitFullscreen(){if(document.fullscreenElement){try{await document.exitFullscreen()}catch{}}$('cockpit').classList.remove('is-fullscreen');document.body.classList.remove('fullscreen-active');$('fullscreen').setAttribute('aria-label','Celá obrazovka')}
$('fullscreen').onclick=async()=>{stopDrive();if(document.fullscreenElement||$('cockpit').classList.contains('is-fullscreen')){await exitCockpitFullscreen();return}$('cockpit').classList.add('is-fullscreen');document.body.classList.add('fullscreen-active');$('fullscreen').setAttribute('aria-label','Ukončit celou obrazovku');try{await $('cockpit').requestFullscreen?.()}catch{}};
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement){$('cockpit').classList.remove('is-fullscreen');document.body.classList.remove('fullscreen-active')}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){stopDrive();exitCockpitFullscreen()}});
$('lightToggle').onclick=async()=>{try{await api('drive',{action:status.telemetry?.light_on?'light_off':'light_on'});await refresh()}catch(e){toast(e.message)}};

function selectModel(id){const select=$('openaiModel');if(id&&![...select.options].some(option=>option.value===id))select.add(new Option(id,id));select.value=id}
$('saveAiSettings').onclick=()=>$('settingsForm').requestSubmit();
$('aiProvider').onchange=()=>{$('modelNote').textContent=$('aiProvider').value==='openai'?'Text a snímky se budou posílat do OpenAI. Ulož volbu poskytovatele a modelu.':'Gemma používá domácí server. Ulož změnu poskytovatele.'};
$('aiKeyForm').onsubmit=async e=>{
 e.preventDefault();const key=$('openaiKey').value.trim();if(!key){$('keyNote').textContent='Zadej nový klíč.';return}
 const button=$('aiKeyForm').querySelector('button');button.disabled=true;
 try{await api('ai/key',{key});$('keyNote').textContent='Klíč je uložený na serveru. Můžeš načíst modely.'}
 catch(e){$('keyNote').textContent=e.message}
 finally{$('openaiKey').value='';button.disabled=false}
};
$('refreshModels').onclick=async()=>{
 $('refreshModels').disabled=true;$('modelNote').textContent='Načítám dostupné modely…';const selected=$('openaiModel').value;
 try{const data=await api('ai/models');$('openaiModel').replaceChildren(new Option('Vyber model',''));for(const model of data.models||[]){const label={verified:'obraz ověřen',documented:'podpora obrazu',unknown:'obraz neověřen'}[model.vision]||'obraz neověřen';$('openaiModel').add(new Option(model.id+' · '+label,model.id))}selectModel(selected);$('modelNote').textContent='Modely načteny. Neověřenou podporu obrazu lze otestovat.'}
 catch(e){$('modelNote').textContent=e.message}finally{$('refreshModels').disabled=false}
};
$('testModel').onclick=async()=>{
 const model=$('openaiModel').value;if(!model){$('modelNote').textContent='Nejdřív vyber model.';return}
 $('testModel').disabled=true;$('modelNote').textContent='Ověřuji, zda model přijímá obraz…';
 try{const result=await api('ai/test',{model});$('modelNote').textContent=result.message||result.detail||(result.ok===false?'Ověření se nepodařilo.':'Model prošel ověřením obrazu.')}
 catch(e){$('modelNote').textContent=e.message}finally{$('testModel').disabled=false}
};

function updateProviderLabel(){providerName=$('aiProvider').value==='openai'?'OpenAI':'Gemma';$('llmService').textContent=($('llmService').classList.contains('ok')?'● ':'○ ')+providerName}
