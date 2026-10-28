// Standalone simulator cockpit. Every request stays on the current server.
const $ = id => document.getElementById(id);
const root = new URL('../', import.meta.url);
const endpoint = path => new URL(`api/${path}`, root).href;
const actionNames = {forward:'Jízda vpřed',backward:'Jízda vzad',left:'Vlevo',right:'Vpravo',rotate_left:'Otočení vlevo',rotate_right:'Otočení vpravo',stop:'Zastavení',tilt:'Náklon kamery',light_on:'Rozsvícení',light_off:'Zhasnutí'};
const phases = {idle:'Čekám na tvůj cíl',starting:'Připravuji výpravu',next_step:'Plánuji další krok',planning:'Dívám se a přemýšlím',observing:'Pozoruji okolí',executing:'Provádím krátký pohyb',acting:'Provádím krátký pohyb',settling:'Čekám na zastavení',observing_after:'Snímám nový pohled',evaluating:'Ověřuji výsledek',verifying:'Porovnávám oba pohledy',recovering_result:'Obnovuji ověření kroku',reconnecting:'Obnovuji spojení',paused:'Výprava pozastavena',completed:'AI označila cíl za splněný',succeeded:'AI označila cíl za splněný',blocked:'Potřebuji tvoji pomoc',failed:'Krok se nepodařil',cancelled:'Výprava zrušena'};
const taskNames = {idle:'Připraveno',running:'AI pracuje',reconnecting:'Obnovuji',paused:'Pauza',completed:'Dokončeno',succeeded:'Dokončeno',blocked:'Zastaveno',failed:'Chyba',cancelled:'Zrušeno'};
let status = {}, world = {}, scenarios = [], authenticated = false, simulationVerified = false;
let busy = false, pollTimer = null, cameraTimer = null, cameraPending = false, toastTimer = null, lastResult = '', lastLog = '', lastScenario = '', cameraLoaded = false;
let held = null, holdTimer = null, drivePending = false, driveGeneration = 0, joystickPointer = null;
let pollGeneration = 0, operationGeneration = 0;
let aiSettingsLoading = false, aiSettingsReady = false;
let missionRecordView = 'log', seenRouteTask = null, lastRouteMemoryKey = '', seenGraphTask = null, lastGraphKey = '';
let initialGoalRestoreDone = false, goalEdited = false;
const keyActions = {KeyW:'forward',ArrowUp:'forward',KeyS:'backward',ArrowDown:'backward',KeyA:'rotate_left',ArrowLeft:'rotate_left',KeyD:'rotate_right',ArrowRight:'rotate_right',KeyQ:'left',KeyE:'right'};
const activeTask = () => ['running','reconnecting'].includes(status.task?.status);
const scenarioId = () => typeof world.scenario === 'object' ? world.scenario?.id : world.scenario;
const checkpoints = () => Array.isArray(world.checkpoints) ? world.checkpoints : Array.isArray(world.geometry?.checkpoints) ? world.geometry.checkpoints : [];
const metres = value => Number.isFinite(value) ? `${value.toLocaleString('cs-CZ',{minimumFractionDigits:2,maximumFractionDigits:2})} m` : '—';
const canDrive = () => authenticated && simulationVerified && status.mine && status.armed && status.mode === 'manual' && !busy && !document.hidden;
function toast(message) { $('toast').textContent = message; $('toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 4800); }
async function api(path, body, options = {}) {
  const response = await fetch(endpoint(path), {method:body === undefined ? 'GET' : 'POST',credentials:'same-origin',cache:'no-store',headers:body === undefined ? {} : {'Content-Type':'application/json'},body:body === undefined ? undefined : JSON.stringify(body),signal:AbortSignal.timeout(15000),...options});
  let data; try { data = await response.json(); } catch { throw new Error('Server nevrátil platnou odpověď.'); }
  if (!response.ok) { if (response.status === 401 && path !== 'login') showLogin(); throw new Error(typeof data.detail === 'string' ? data.detail : data.error || 'Požadavek se nepodařil.'); }
  return data;
}
function setBusy(value) { busy = value; renderControls(); }
async function operation(fn) { if (busy || !simulationVerified) return; setBusy(true); const generation = operationGeneration; try { await fn(() => { if (generation !== operationGeneration || document.hidden) throw new Error('Akce byla zastavena.'); }); } catch (error) { toast(error.message); } finally { if (generation !== operationGeneration && authenticated) await api('stop',{}).catch(()=>{}); setBusy(false); if (authenticated) await refresh(); } }
function showLogin() { authenticated = false; simulationVerified = false; operationGeneration++; pollGeneration++; clearTimeout(pollTimer); clearTimeout(cameraTimer); releaseHold(false); $('cockpit').hidden = true; $('loginScreen').hidden = false; }
function verifySimulation(data) { if (data.simulation_only !== true) throw new Error('Tento server není uzamčený pro simulaci. Otevři samostatnou instanci simulátoru.'); simulationVerified = true; }
function restoreInitialTaskGoal(task) {
  if (initialGoalRestoreDone) return;
  initialGoalRestoreDone = true;
  const input = $('goal');
  if (!goalEdited && input.value === input.defaultValue && typeof task?.goal === 'string' && task.goal.trim()) input.value = task.goal;
}
async function enterCockpit() {
  const [nextStatus, sim] = await Promise.all([api('status'), api('simulator')]);
  verifySimulation(sim); status = nextStatus; world = sim.state || {}; scenarios = sim.scenarios || [];
  restoreInitialTaskGoal(status.task);
  authenticated = true; $('loginScreen').hidden = true; $('cockpit').hidden = false; render();
  pollGeneration++; schedulePoll(pollGeneration); loadCamera();
}
async function refresh() {
  if (!authenticated || document.hidden) return;
  try {
    const [nextStatus, sim] = await Promise.all([api('status'), api('simulator')]);
    verifySimulation(sim); status = nextStatus; world = sim.state || {}; scenarios = sim.scenarios || scenarios;
    $('connectionText').textContent = 'Simulátor připojen'; $('connectionDot').className = 'connected'; render();
  } catch (error) {
    $('connectionText').textContent = 'Spojení přerušeno'; $('connectionDot').className = 'disconnected';
    releaseHold(false); if (authenticated) { status.armed = false; renderControls(); }
    if (!simulationVerified) showLogin();
  }
}
function schedulePoll(generation) { clearTimeout(pollTimer); pollTimer = setTimeout(async () => { if (!authenticated || generation !== pollGeneration) return; await refresh(); if (generation === pollGeneration) schedulePoll(generation); }, 400); }
function loadCamera() {
  clearTimeout(cameraTimer); if (!authenticated || document.hidden || cameraPending) return;
  const image = $('camera'); const started = performance.now(); cameraPending = true;
  const done = success => { cameraPending = false; image.onload = null; image.onerror = null; if (!authenticated) return; if (success) { $('cameraPlaceholder').hidden = true; cameraLoaded = true; $('frameRate').textContent = `SIM / ${Math.round(performance.now() - started)} MS`; } else { $('cameraPlaceholder').hidden = false; $('cameraPlaceholder').querySelector('p').textContent = 'Kamera se obnovuje…'; } if (!document.hidden) cameraTimer = setTimeout(loadCamera, success ? 125 : 1200); };
  image.onload = () => done(true); image.onerror = () => done(false); image.src = endpoint(`camera.jpg?t=${Date.now()}`);
}
setInterval(() => { if (authenticated && simulationVerified && status.mine && status.armed && !document.hidden) api('heartbeat',{}).catch(() => { releaseHold(false); }); }, 500);

function render() {
  renderControls(); renderTask(); renderTruth(); renderScenarios(); drawMap();
  $('provider').textContent = `${status.ai_provider === 'openai' ? 'OpenAI' : status.ai_provider === 'gemma' ? 'Gemma' : 'AI'}${status.ai_model ? ` / ${status.ai_model}` : ''} · obraz → krátký krok`;
  const heading = Number(world.pose?.heading ?? 0); const degrees = world.pose?.heading_degrees ?? heading * 180 / Math.PI;
  $('headingLabel').textContent = `${String(Math.round((degrees % 360 + 360) % 360)).padStart(3,'0')}°`;
  const movement = held?.action || world.action || world.motor || 'stop';
  $('driveCaption').textContent = movement !== 'stop' ? (actionNames[movement] || movement).toUpperCase() : status.armed ? 'STOJÍM / ŘÍZENÍ AKTIVNÍ' : 'PŘIPRAVENO K PRŮZKUMU';
}
function renderControls() {
  const running = activeTask(), armed = status.mine && status.armed;
  $('manualMode').classList.toggle('selected',status.mode !== 'ai'); $('aiMode').classList.toggle('selected',status.mode === 'ai');
  $('manualMode').disabled = busy; $('aiMode').disabled = busy;
  $('armButton').textContent = armed ? '● Řízení aktivní' : '○ Aktivovat řízení'; $('armButton').classList.toggle('active',!!armed); $('armButton').disabled = busy || !!armed;
  $('controlState').textContent = status.mode === 'ai' ? (running ? 'Řídí AI pilot' : 'AI čeká na výpravu') : armed ? 'Ruční řízení aktivní' : 'Řízení vypnuté';
  for (const button of document.querySelectorAll('[data-drive]')) { button.disabled = !canDrive(); button.classList.toggle('pressed',held?.action === button.dataset.drive); }
  $('joystick').setAttribute('aria-disabled',String(!canDrive()));
  $('startTask').disabled = busy || running; $('pauseTask').disabled = busy || !running;
  $('resumeTask').disabled = busy || status.task?.status !== 'paused'; $('cancelTask').disabled = busy || !['running','reconnecting','paused','blocked'].includes(status.task?.status);
  $('tilt').disabled = !canDrive();
  $('lightButton').disabled = !canDrive() || status.telemetry?.light_supported === false;
  $('lightButton').textContent = (world.telemetry?.light_on ?? status.telemetry?.light_on) ? '☼ Zhasnout světlo' : '☼ Rozsvítit světlo';
  if (document.activeElement !== $('tilt')) { const angle=world.servo??status.telemetry?.servo??90;$('tilt').value=angle;$('tiltValue').textContent=`${angle}°`; }
  renderAiSettingsControls();
}
function renderAiSettingsControls() {
  const locked=busy||activeTask()||aiSettingsLoading||!aiSettingsReady;
  $('aiProvider').disabled=locked; $('aiModel').disabled=locked||$('aiProvider').value!=='openai';
  $('saveAiSettings').disabled=locked; $('aiSettingsLock').hidden=!activeTask();
}
async function loadAiSettings() {
  if(aiSettingsLoading)return;
  aiSettingsLoading=true;aiSettingsReady=false;renderAiSettingsControls();$('aiSettingsNote').textContent='Načítám aktuální volbu…';
  try { const config=await api('settings');const model=config.openai_model||'gpt-6-luna';
    $('aiProvider').value=config.ai_provider==='openai'?'openai':'gemma';
    $('aiModel').replaceChildren();
    for(const value of new Set(['gpt-6-luna','gpt-6.1-sol',model])){const option=document.createElement('option');option.value=value;option.textContent=value;$('aiModel').append(option);}
    $('aiModel').value=model;aiSettingsReady=true;$('aiSettingsNote').textContent='Změna se použije po uložení.';
  } catch(error){$('aiSettingsNote').textContent=error.message;}
  finally{aiSettingsLoading=false;renderAiSettingsControls();}
}
function aiSettingsPayload(config, provider, model) {
  const fields=['ai_provider','openai_model','thinking_enabled','navigation_policy','transport','device_url','simulation','name','floor_angle','floor_calibrated','camera_rotate_180'];
  const payload=Object.fromEntries(fields.filter(field=>Object.hasOwn(config,field)).map(field=>[field,config[field]]));
  return {...payload,device_key:'',ai_provider:provider,openai_model:model};
}
function renderTask() {
  const task = status.task || {}, result = task.last_result;
  $('taskBadge').textContent = taskNames[task.status] || 'Připraveno'; $('taskBadge').className = `badge ${activeTask() ? 'running' : task.status === 'blocked' ? 'blocked' : ''}`;
  $('taskPhase').textContent = phases[task.phase] || phases[task.status] || task.phase || phases.idle;
  $('thinkingDot').classList.toggle('active',activeTask()); $('stepCount').textContent = String(task.current_step || 0).padStart(2,'0');
  $('taskReason').textContent = task.outcome || task.reason || result?.decision_reason || 'Můžeš řídit ručně nebo spustit první AI výpravu.';
  renderNavigationMemory(task.navigation_state);
  const entries = Array.isArray(task.completed_steps) ? task.completed_steps : [];
  $('logCount').textContent = `${entries.length} KROKŮ`;
  const logKey = JSON.stringify(entries);
  if (logKey !== lastLog) { lastLog = logKey; const list = $('taskLog'); list.replaceChildren(); if (!entries.length) { const item = document.createElement('li'); item.className = 'empty-log'; const arrow = document.createElement('span'); arrow.textContent = '↳'; const text = document.createElement('p'); text.textContent = 'První pozorování se objeví po spuštění úkolu.'; item.append(arrow,text); list.append(item); } else { for (const entry of [...entries].reverse()) { const item = document.createElement('li'), number = document.createElement('span'), text = document.createElement('div'), title = document.createElement('strong'), body = document.createElement('p'); number.textContent = String(entry.step ?? '·'); title.textContent = actionNames[entry.action || entry.command?.action] || entry.action || 'Pozorování'; body.textContent = entry.assessment?.summary || entry.decision_reason || entry.execution_reason || 'Krok zaznamenán.'; text.append(title,body); if(typeof entry.navigation_state?.reason === 'string' && entry.navigation_state.reason.trim()){const memory=document.createElement('small');memory.className='log-memory';memory.textContent='Paměť: '+entry.navigation_state.reason.trim();memory.title=memory.textContent;text.append(memory);} item.append(number,text); list.append(item); } } }
  const resultId = result ? String(result.id || '') : '';
  if (resultId !== lastResult) { lastResult = resultId; for (const role of ['before','after']) { const image = $(`${role}Frame`), button = $(`${role}Button`); image.removeAttribute('src'); button.classList.remove('has-frame'); if (result && result[`has_${role}`] && resultId) { image.onload = () => button.classList.add('has-frame'); image.onerror = () => button.classList.remove('has-frame'); image.src = endpoint(`task/frames/${role}?v=${encodeURIComponent(resultId)}`); } } }
  $('comparisonStep').textContent = result ? `KROK ${result.step || task.current_step || 1}` : 'ČEKÁM NA KROK';
  $('actionLabel').textContent = result ? `${actionNames[result.action || result.command?.action] || result.action || 'Pozorování'} · ${result.acceptance === 'accepted' ? 'povel přijat' : result.acceptance === 'unknown' ? 'přijetí nejisté' : 'ověření řadičem'}` : 'Pozorovat → jednat → ověřit';
  $('assessmentText').textContent = result?.assessment?.summary || result?.execution_reason || 'AI srovná oba pohledy a vysvětlí, co se změnilo.';
  renderRouteMemory(task.route_memory,task.id);
}
function visibleRoutes(memory) {
  const routes=new Map();
  for(const [index,route] of (Array.isArray(memory?.routes)?memory.routes:[]).entries()){
    if(!route || !['string','number'].includes(typeof route.id) || !String(route.id).trim())continue;
    const id=String(route.id),step=Number.isFinite(route.last_seen_step)?route.last_seen_step:-1;
    if(!routes.has(id)||step>=routes.get(id).step)routes.set(id,{...route,id,step,index});
  }
  const sorted=[...routes.values()].sort((a,b)=>b.step-a.step||b.index-a.index),activeId=String(memory?.active_route_id??'');
  const selected=sorted.slice(0,6),active=routes.get(activeId);
  if(active&&!selected.some(route=>route.id===activeId))selected.push(active);
  return selected.sort((a,b)=>Number(b.id===activeId)-Number(a.id===activeId)||b.step-a.step||b.index-a.index);
}
function setMissionRecordView(view) {
  const showRoutes=view==='routes'&&!$('routeMemoryTab').hidden,showGraph=view==='graph'&&!$('aiMapTab').hidden;missionRecordView=showGraph?'graph':showRoutes?'routes':'log';
  for(const [id,panel,selected] of [['taskLogTab','taskLog',!showRoutes&&!showGraph],['routeMemoryTab','routeMemoryPanel',showRoutes],['aiMapTab','aiMapPanel',showGraph]]){$(panel).hidden=!selected;$(id).classList.toggle('selected',selected);$(id).setAttribute('aria-pressed',String(selected));}
  $('logCount').textContent=showGraph?`${$('aiMapPlaces').childElementCount} MÍST`:showRoutes?`${$('routeMemoryList').childElementCount} CEST`:`${status.task?.completed_steps?.length||0} KROKŮ`;
}
function renderRouteMemory(memory, taskId) {
  const routes=visibleRoutes(memory),activeId=String(memory?.active_route_id??''),key=JSON.stringify([taskId,memory]);
  renderAiMap(memory,taskId);
  $('routeMemoryTab').hidden=!routes.length;
  if(!routes.length){$('routeMemoryList').replaceChildren();lastRouteMemoryKey=key;setMissionRecordView($('aiMapTab').hidden?'log':missionRecordView);return;}
  const taskKey=String(taskId??'current');if(seenRouteTask!==taskKey){seenRouteTask=taskKey;if($('aiMapTab').hidden)missionRecordView='routes';}
  if(key!==lastRouteMemoryKey){lastRouteMemoryKey=key;const list=$('routeMemoryList');list.replaceChildren();
    const stateNames={candidate:'Kandidát',visible:'Viditelná',closer:'Blíž',passed:'Projetá',blocked:'Blokovaná',lost:'Ztracená',unknown:'Neověřená'};
    const bearings={left:'vlevo',right:'vpravo',ahead:'vpředu',forward:'vpředu',behind:'vzadu',unknown:'směr neověřen'};
    for(const route of routes){const item=document.createElement('li'),header=document.createElement('div'),id=document.createElement('span'),state=document.createElement('span'),name=document.createElement('strong'),detail=document.createElement('p'),meta=document.createElement('small');
      const active=route.id===activeId;item.className=active?'route-memory-item active':'route-memory-item';
      header.className='route-memory-item-heading';id.textContent=`${route.id}${active?' · AKTIVNÍ MEZICÍL':''}`;
      const routeStatus=Object.hasOwn(stateNames,route.status)?route.status:'unknown';state.textContent=stateNames[routeStatus];state.title=`Modelový stav: ${routeStatus}`;header.append(id,state);
      name.textContent=typeof route.label==='string'&&route.label.trim()?route.label:typeof route.landmark==='string'&&route.landmark.trim()?route.landmark:'Neznámá cesta';
      const landmark=typeof route.landmark==='string'&&route.landmark!==name.textContent?route.landmark.trim():'',feedback=typeof route.feedback_evidence==='string'?route.feedback_evidence.trim():'',evidence=feedback||(typeof route.evidence==='string'?route.evidence.trim():'');
      detail.textContent=[landmark,evidence].filter(Boolean).join(' · ');detail.hidden=!detail.textContent;detail.title=detail.textContent;
      const plannedBearing=typeof route.bearing==='string'?route.bearing.trim():'',bearing=plannedBearing?`směr při posledním plánu: ${bearings[plannedBearing]||plannedBearing}`:'',attempts=Number.isInteger(route.attempts)&&route.attempts>=0?`pokusy: ${route.attempts}`:'',step=Number.isInteger(route.last_seen_step)?`krok ${route.last_seen_step}`:'';
      meta.textContent=[bearing,attempts,step].filter(Boolean).join(' · ');meta.hidden=!meta.textContent;item.append(header,name,detail,meta);list.append(item);
    }
  }
  setMissionRecordView(missionRecordView);
}
function aiGraphData(memory) {
  const validId=id=>['string','number'].includes(typeof id)&&String(id).trim();
  const unique=items=>{const map=new Map();for(const item of Array.isArray(items)?items:[]){if(item&&validId(item.id)){const previous=map.get(String(item.id));if(!previous||(item.last_seen_step??-1)>=(previous.last_seen_step??-1))map.set(String(item.id),{...item,id:String(item.id)});}}return [...map.values()];};
  const places=unique(memory?.places),edges=unique(memory?.routes),activePlace=String(memory?.active_place_id??''),activeRoute=String(memory?.active_route_id??''),alternatives=new Set((Array.isArray(memory?.alternatives)?memory.alternatives:[]).filter(validId).map(String));
  places.sort((a,b)=>Number(b.id===activePlace)-Number(a.id===activePlace)||(b.last_seen_step??-1)-(a.last_seen_step??-1));
  edges.sort((a,b)=>Number(b.id===activeRoute)-Number(a.id===activeRoute)||Number(alternatives.has(b.id))-Number(alternatives.has(a.id))||(b.last_seen_step??-1)-(a.last_seen_step??-1));
  return {places,edges,activePlace,activeRoute,alternatives};
}
function renderAiMap(memory,taskId) {
  const graph=aiGraphData(memory),visible=graph.places.length>0,key=JSON.stringify([taskId,memory]);$('aiMapTab').hidden=!visible;
  if(!visible){$('aiMapPlaces').replaceChildren();$('aiMapEdges').replaceChildren();$('aiMapCurrentPlace').textContent='';$('aiMapAlternative').textContent='';$('aiMapAlternative').hidden=true;lastGraphKey=key;return;}
  const taskKey=String(taskId??'current');if(seenGraphTask!==taskKey){seenGraphTask=taskKey;missionRecordView='graph';}
  if(key===lastGraphKey)return;lastGraphKey=key;
  const labels=new Map(graph.places.map(place=>[place.id,typeof place.label==='string'?place.label:''])),current=labels.get(graph.activePlace);
  $('aiMapCurrentPlace').textContent=graph.activePlace?`Odhad aktuálního místa: ${graph.activePlace}${current?' · '+current:''}`:'Aktuální místo zatím není rozpoznané.';
  const reason=typeof memory?.alternative_reason==='string'?memory.alternative_reason.trim():'';
  $('aiMapAlternative').textContent=reason?`Proč jiná cesta: ${reason}`:'';$('aiMapAlternative').hidden=!reason;
  const places=$('aiMapPlaces'),edges=$('aiMapEdges');places.replaceChildren();edges.replaceChildren();
  for(const place of graph.places){const item=document.createElement('li'),name=document.createElement('strong'),visits=document.createElement('span');item.className=place.id===graph.activePlace?'active':'';name.textContent=`${place.id}${labels.get(place.id)?' · '+labels.get(place.id):''}`;visits.textContent=Number.isInteger(place.visits)&&place.visits>0?`návštěvy podle AI: ${place.visits}`:'oblast zatím neověřena';item.title=typeof place.evidence==='string'?place.evidence:'';item.append(name,visits);places.append(item);}
  const edgeStates={untried:'Nevyzkoušená',available:'Dostupná podle AI',visited:'Navštívená podle AI',stalled:'Pokus bez postupu',blocked:'Blokovaná podle AI',exhausted:'Možnosti vyčerpány'};
  for(const edge of graph.edges){const item=document.createElement('li'),chain=document.createElement('div'),description=document.createElement('p'),state=Object.hasOwn(edgeStates,edge.exploration_status)?edge.exploration_status:edge.visited?'visited':'untried';item.className=`ai-map-edge ${state}${edge.id===graph.activeRoute?' active':''}${graph.alternatives.has(edge.id)?' alternative':''}`;chain.className='ai-map-chain';
    for(const [index,id] of [edge.source,edge.id,edge.target].entries()){if(index){const arrow=document.createElement('span');arrow.className='ai-map-arrow';arrow.textContent='→';chain.append(arrow);}const chip=document.createElement('span');chip.className=index===1?'ai-map-route':'ai-map-place';chip.textContent=id==null||String(id)===''?'?':String(id);chip.title=index===1?(typeof edge.label==='string'?edge.label:'Pozorovaný průchod'):id==null?'Cílové místo zatím neověřeno':labels.get(String(id))||'Místo v paměti AI';chain.append(chip);}
    const name=typeof edge.label==='string'?edge.label:'',failures=Number.isInteger(edge.failures)&&edge.failures>0?`bez postupu: ${edge.failures}`:'';description.textContent=[edge.id===graph.activeRoute?'Aktivní mezicíl':graph.alternatives.has(edge.id)?'Jiná možnost':'',edgeStates[state],name,failures].filter(Boolean).join(' · ');item.append(chain,description);edges.append(item);
  }
  if(!graph.edges.length){const empty=document.createElement('li');empty.className='ai-map-empty';empty.textContent='Propojení míst zatím není pozorované.';edges.append(empty);}
}
function renderNavigationMemory(state) {
  const phaseNames={scan:'Rozhlížení',advance:'Postup vpřed',route:'Sleduji mezicíl',escape:'Zkouším vyproštění',exhausted:'Průzkum vyčerpán',observe:'Pozorování'};
  const phase=phaseNames[state?.phase]||'', direction={rotate_left:'↶ vlevo',rotate_right:'↷ vpravo'}[state?.scan_direction]||'';
  const count=state?.scan_steps;
  const steps=Number.isInteger(count)&&count>=0?`${count} ${count===1?'krok':count>=2&&count<=4?'kroky':'kroků'}`:'';
  const reason=typeof state?.reason==='string'?state.reason.trim():'';
  const scan=[direction,steps].filter(Boolean).join(' · ');
  const visible=!!(phase||scan||reason);
  $('navigationMemory').hidden=!visible; $('navigationPhase').textContent=phase;
  $('navigationScan').textContent=scan; $('navigationScan').hidden=!scan;
  $('navigationReason').textContent=reason; $('navigationReason').hidden=!reason; $('navigationReason').title=reason;
  $('missionPanel').classList.toggle('has-navigation-memory',visible);
}
function renderTruth() {
  const truth = world.groundtruth || {};
  const sensor = world.distance_cm ?? truth.distance_cm ?? world.telemetry?.distance_cm ?? status.telemetry?.distance_cm;
  $('sensorDistance').textContent = Number.isFinite(sensor) ? Math.round(sensor) : '—';
  const doorCm = truth.door_distance_cm ?? world.door_distance_cm;
  const doorMeters = truth.door_distance ?? truth.distance_to_door ?? world.door_distance;
  $('doorDistance').textContent = Number.isFinite(doorCm) ? `${(doorCm / 100).toFixed(2)} m` : Number.isFinite(doorMeters) ? `${doorMeters.toFixed(2)} m` : '—';
  $('travelledDistance').textContent = metres(world.travelled);
  renderCheckpoints();
  const collisions = truth.collisions ?? world.collisions ?? 0; $('collisionCount').textContent = String(collisions); $('collisionCount').classList.toggle('collision',collisions > 0);
  const reached = truth.goal_reached ?? world.goal_reached ?? false; $('goalReached').textContent = reached ? 'Splněn ✓' : 'Zatím ne'; $('goalReached').classList.toggle('reached',!!reached);
}
function renderScenarios() {
  const key = JSON.stringify(scenarios);
  if (key !== lastScenario) { const previous = $('scenario').value; lastScenario = key; $('scenario').replaceChildren(); for (const item of scenarios) { const option = document.createElement('option'); option.value = item.id; option.textContent = item.title || item.name || item.id; $('scenario').append(option); } $('scenario').value = scenarios.some(item=>item.id===previous) ? previous : scenarioId() || scenarios[0]?.id || ''; }
  const current = scenarios.find(item => item.id === scenarioId());
  const title = current?.title || world.title || scenarioId() || 'Virtuální místnost';
  $('scenarioLabel').textContent = title.toUpperCase(); $('mapTitle').textContent = title;
  $('scenarioSummary').textContent = current?.description || 'Virtuální místnost pro samostatný průzkum.';
  $('scenarioSummary').title = $('scenarioSummary').textContent;
  renderScenarioPreview();
}
function renderScenarioPreview() { $('scenarioDescription').textContent = scenarios.find(item=>item.id===$('scenario').value)?.description || ''; }
function applyScenarioGoal(id, previousText) {
  const goal = scenarios.find(item=>item.id===id)?.goal;
  if (typeof goal === 'string' && goal.trim() && !activeTask() && $('goal').value === previousText) $('goal').value = goal;
}
function renderCheckpoints() {
  const points = checkpoints(), visited = points.filter(point=>point.visited).length;
  $('checkpointProgress').hidden = !points.length; $('checkpointCount').textContent = `${visited} / ${points.length}`;
  $('checkpointBar').max = Math.max(1,points.length); $('checkpointBar').value = visited;
  const list = $('checkpointList'); list.replaceChildren();
  for (const [index,point] of points.entries()) { const item=document.createElement('li'),number=document.createElement('span'),title=document.createElement('span');item.className=point.visited?'visited':'';number.textContent=point.visited?'✓':String(index+1);title.textContent=point.title||`Checkpoint ${index+1}`;item.append(number,title);list.append(item); }
  const description = `Mapa světa. Projeté checkpointy ${visited} z ${points.length}. ${points.map((point,index)=>`${index+1}: ${point.title||'Checkpoint'}, ${point.visited?'projeto':'neprojeto'}`).join('. ')}`;
  $('minimap').setAttribute('aria-label',description); $('expandedMap').setAttribute('aria-label',description);
}
function drawMap() { drawMapOn($('minimap')); if ($('mapDialog').open) drawMapOn($('expandedMap')); }
function drawMapOn(canvas) {
  const rect=canvas.getBoundingClientRect(); if (rect.width<=0 || rect.height<=0) return;
  const W = Math.max(1,Math.round(rect.width*2)), H = Math.max(1,Math.round(rect.height*2));
  if (canvas.width!==W || canvas.height!==H) { canvas.width=W;canvas.height=H; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,W,H); ctx.fillStyle = '#0d191f'; ctx.fillRect(0,0,W,H);
  const geometry = world.geometry || world.world || {}, rawBounds = geometry.bounds || {};
  const bounds = Array.isArray(rawBounds) ? {x:rawBounds[0],y:rawBounds[1],max_x:rawBounds[2],max_y:rawBounds[3]} : rawBounds;
  const minX = bounds.x ?? bounds.min_x ?? bounds.minX ?? 0, minY = bounds.y ?? bounds.min_y ?? bounds.minY ?? 0;
  const width = bounds.width ?? (bounds.max_x != null ? bounds.max_x - minX : null) ?? geometry.width ?? 8;
  const height = bounds.height ?? (bounds.max_y != null ? bounds.max_y - minY : null) ?? geometry.height ?? 6;
  const scale = Math.min((W - 42) / Math.max(.1,width),(H - 34) / Math.max(.1,height));
  const ox = (W-width*scale)/2, oy = (H-height*scale)/2;
  const point = (x,y) => [ox+(x-minX)*scale,H-oy-(y-minY)*scale];
  ctx.strokeStyle = '#a5c79e0d'; ctx.lineWidth = 1;
  for (let x = Math.ceil(minX); x < minX+width; x++) { const a=point(x,minY),b=point(x,minY+height);ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke(); }
  for (let y = Math.ceil(minY); y < minY+height; y++) { const a=point(minX,y),b=point(minX+width,y);ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke(); }
  const rectangle = (item, color) => { if (!item || !Number.isFinite(Number(item.x))) return; const x=Number(item.x),y=Number(item.y||0),w=Number(item.width??item.w??.25),h=Number(item.h??item.height??.25),p=point(x,y+h);ctx.fillStyle=color;ctx.fillRect(p[0],p[1],w*scale,h*scale);ctx.strokeStyle='#c7d7ba33';ctx.strokeRect(p[0],p[1],w*scale,h*scale); };
  ctx.strokeStyle='#9ab79b66';ctx.lineWidth=3;ctx.strokeRect(ox,H-oy-height*scale,width*scale,height*scale);
  for (const item of geometry.walls || []) { const wall=Array.isArray(item)?{x1:item[0],y1:item[1],x2:item[2],y2:item[3]}:item; if (wall.x1 != null) { ctx.strokeStyle='#8eac95';ctx.lineWidth=Math.max(3,(wall.thickness||.1)*scale);ctx.beginPath();ctx.moveTo(...point(wall.x1,wall.y1));ctx.lineTo(...point(wall.x2,wall.y2));ctx.stroke(); } else rectangle(wall,'#7a9480'); }
  for (const box of geometry.boxes || geometry.objects || []) rectangle(box,box.color || '#7e7460');
  const door = geometry.door || world.door;
  if (door) { const x=Number(door.x),y=Number(door.y),p=point(x,y); if (Number.isFinite(x+y)) { ctx.fillStyle='#a8d3dc';ctx.shadowColor='#a8d3dc66';ctx.shadowBlur=10;ctx.fillRect(p[0]-4,p[1]-(door.width||.4)*scale/2,8,(door.width||.4)*scale);ctx.shadowBlur=0;ctx.fillStyle='#c1e3e7';ctx.font='15px system-ui';ctx.textAlign='center';ctx.fillText('CÍL',p[0]+21,p[1]-13); } }
  if ($('showPath').checked && world.path?.length > 1) { ctx.strokeStyle='#c8ef8c88';ctx.lineWidth=2;ctx.setLineDash([4,5]);ctx.beginPath();world.path.forEach((item,index)=>{const p=point(item.x??item[0],item.y??item[1]);index?ctx.lineTo(...p):ctx.moveTo(...p);});ctx.stroke();ctx.setLineDash([]); }
  for (const checkpoint of checkpoints()) { const gate=checkpoint.gate; if (!gate) continue; const a=point(gate.x,gate.ymin),b=point(gate.x,gate.ymax);ctx.strokeStyle=checkpoint.visited?'#c9ef8c':'#e4c78b';ctx.lineWidth=2;ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke();ctx.setLineDash([]); }
  for (const [index,checkpoint] of checkpoints().entries()) { if (!Number.isFinite(checkpoint.x) || !Number.isFinite(checkpoint.y)) continue;const p=point(checkpoint.x,checkpoint.y),radius=Math.max(10,Math.min(28,(checkpoint.radius||.25)*scale));ctx.beginPath();ctx.arc(...p,radius,0,Math.PI*2);ctx.fillStyle=checkpoint.visited?'#c9ef8c30':'#e4c78b12';ctx.fill();ctx.strokeStyle=checkpoint.visited?'#c9ef8c':'#e4c78b';ctx.lineWidth=2;ctx.stroke();ctx.fillStyle=checkpoint.visited?'#d5f98a':'#f1d6a2';ctx.font='bold 16px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(checkpoint.visited?'✓':String(index+1),p[0],p[1]);ctx.textBaseline='alphabetic'; }
  if (world.pose) { const p=point(world.pose.x,world.pose.y),angle=Number(world.pose.heading||0);ctx.save();ctx.translate(...p);ctx.rotate(-angle);ctx.fillStyle='#d5f98a';ctx.shadowColor='#c9f78370';ctx.shadowBlur=14;ctx.beginPath();ctx.moveTo(11,0);ctx.lineTo(-7,-7);ctx.lineTo(-4,0);ctx.lineTo(-7,7);ctx.closePath();ctx.fill();ctx.shadowBlur=0;ctx.restore(); }
}

async function pauseBeforeChange() { releaseHold(); if (activeTask()) { await api('task/pause',{}); status.task.status='paused'; } }
async function switchMode(mode) { await pauseBeforeChange(); await api('mode',{mode}); status.mode=mode;status.armed=false;renderControls(); }
async function arm() { const generation=operationGeneration;await api('arm',{});if(generation!==operationGeneration||document.hidden){await api('stop',{});throw new Error('Aktivace řízení byla zastavena.');}status.armed=true;status.mine=true;await api('heartbeat',{});renderControls(); }
function driveStop(options = {}) { if (authenticated && simulationVerified && status.armed && status.mine && status.mode === 'manual') return api('drive',{action:'stop'},options).catch(()=>{}); return Promise.resolve(); }
function releaseHold(send = true) { held=null;driveGeneration++;clearInterval(holdTimer);holdTimer=null; $('joystickKnob').style.transform='';for(const button of document.querySelectorAll('[data-drive]'))button.classList.remove('pressed'); if(send) return driveStop();return Promise.resolve(); }
async function drivePacket() {
  if (!held || !canDrive() || drivePending) return;
  drivePending=true;const generation=driveGeneration, action=held.action;
  try { await api('drive',{action,speed:Number($('speed').value),duration_ms:250}); }
  catch(error) { releaseHold();toast(error.message); }
  finally { drivePending=false; if(generation!==driveGeneration)await driveStop(); }
}
function hold(action, source) { if(!canDrive()){toast(status.mode==='ai'?'Pro ruční jízdu přepni na režim Ručně.':'Nejdřív aktivuj řízení.');return;}if(held?.action===action&&held.source===source)return;held={action,source};driveGeneration++;clearInterval(holdTimer);drivePacket();holdTimer=setInterval(drivePacket,180);renderControls(); }
function stopAll(keepalive = false) { operationGeneration++;releaseHold(false); if (!authenticated || !simulationVerified) return Promise.resolve();status.armed=false;renderControls(); return api('stop',{},keepalive?{keepalive:true}:{}).catch(error=>{if(!keepalive)toast(error.message);}); }

$('loginForm').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('loginError').textContent='';try{await api('login',{key:$('accessKey').value});$('accessKey').value='';await enterCockpit();}catch(error){$('loginError').textContent=error.message;}finally{button.disabled=false;}});
$('taskForm').addEventListener('submit',event=>{event.preventDefault();const goal=$('goal').value.trim();if(!goal){toast('Napiš, kam má Kvído dojet.');return;}operation(async check=>{await pauseBeforeChange();check();await api('mode',{mode:'ai'});status.mode='ai';status.armed=false;check();await arm();check();await api('task',{goal});$('missionPanel').classList.remove('open');toast('Výprava začala. Sleduj rozhodování AI v deníku.');});});
$('manualMode').onclick=()=>operation(()=>switchMode('manual'));
$('aiMode').onclick=()=>operation(()=>switchMode('ai'));
$('armButton').onclick=()=>operation(async check=>{if(status.mode!=='manual'){await switchMode('manual');check();}await arm();check();toast('Ruční řízení je aktivní. Drž směr nebo klávesu WASD.');});
$('pauseTask').onclick=()=>operation(()=>api('task/pause',{}));
$('cancelTask').onclick=()=>operation(()=>api('task/cancel',{}));
$('resumeTask').onclick=()=>operation(async check=>{await api('mode',{mode:'ai'});status.mode='ai';status.armed=false;check();await arm();check();await api('task/resume',{});});
$('stopButton').onclick=async()=>{await stopAll();toast('Zastaveno. Pro další pohyb znovu aktivuj řízení.');await refresh();};
$('goal').addEventListener('input',()=>{goalEdited=true;});
for(const button of document.querySelectorAll('[data-goal]'))button.onclick=()=>{goalEdited=true;$('goal').value=button.dataset.goal;$('goal').focus();};
for(const button of document.querySelectorAll('[data-drive]')){button.onpointerdown=event=>{event.preventDefault();button.setPointerCapture(event.pointerId);hold(button.dataset.drive,`pointer:${event.pointerId}`);};for(const name of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(name,()=>{if(held?.source.startsWith('pointer:'))releaseHold();});button.addEventListener('contextmenu',event=>event.preventDefault());}
const joystick=$('joystick');
function moveJoystick(event){if(event.pointerId!==joystickPointer)return;const rect=joystick.getBoundingClientRect(),dx=event.clientX-rect.left-rect.width/2,dy=event.clientY-rect.top-rect.height/2,r=Math.hypot(dx,dy),limit=rect.width*.3,factor=r>limit?limit/r:1;$('joystickKnob').style.transform=`translate(${dx*factor}px,${dy*factor}px)`;if(r<9){releaseHold();return;}hold(Math.abs(dy)>Math.abs(dx)?(dy<0?'forward':'backward'):(dx<0?'rotate_left':'rotate_right'),'joystick');}
joystick.onpointerdown=event=>{event.preventDefault();if(!canDrive()){toast('Pro joystick aktivuj ruční řízení.');return;}joystickPointer=event.pointerId;joystick.setPointerCapture(event.pointerId);moveJoystick(event);};joystick.onpointermove=moveJoystick;
for(const name of ['pointerup','pointercancel','lostpointercapture'])joystick.addEventListener(name,event=>{if(event.pointerId===joystickPointer){joystickPointer=null;releaseHold();}});
joystick.addEventListener('contextmenu',event=>event.preventDefault());
window.addEventListener('keydown',event=>{const editing=/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)||event.target.isContentEditable;if(event.code==='Escape'){if(authenticated){stopAll();toast('STOP · řízení vypnuté');}return;}if(editing||document.querySelector('dialog[open]'))return;if(event.code==='Space'){event.preventDefault();stopAll();if(!event.repeat)toast('STOP · řízení vypnuté');return;}if(keyActions[event.code]){event.preventDefault();if(!event.repeat)hold(keyActions[event.code],`key:${event.code}`);}});
window.addEventListener('keyup',event=>{if(held?.source===`key:${event.code}`){event.preventDefault();releaseHold();}});
window.addEventListener('blur',()=>{if(authenticated)stopAll(true);});
document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(cameraTimer);stopAll(true);}else if(authenticated){refresh();loadCamera();}});
window.addEventListener('pagehide',()=>stopAll(true));
$('settingsButton').onclick=()=>{$('settingsDialog').showModal();loadAiSettings();};
$('aiProvider').onchange=renderAiSettingsControls;
$('saveAiSettings').onclick=()=>{if(activeTask()||!aiSettingsReady)return;const provider=$('aiProvider').value,model=$('aiModel').value;operation(async check=>{const current=await api('settings');check();if(activeTask())throw new Error('Pro změnu modelu nejdřív pozastav AI výpravu.');await releaseHold();check();await api('settings',aiSettingsPayload(current,provider,model));status.armed=false;$('aiSettingsNote').textContent='AI nastavení uloženo. Řízení je vypnuté.';toast('AI nastavení uloženo.');});};
$('expandMap').onclick=()=>{$('mapDialog').showModal();drawMap();};
$('scenario').onchange=renderScenarioPreview;
$('missionToggle').onclick=()=>{$('missionPanel').classList.toggle('open');};
$('taskLogTab').onclick=()=>setMissionRecordView('log');$('routeMemoryTab').onclick=()=>setMissionRecordView('routes');$('aiMapTab').onclick=()=>setMissionRecordView('graph');
for(const button of document.querySelectorAll('[data-close]'))button.onclick=()=>$(button.dataset.close).close();
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
$('speed').oninput=()=>{$('speedValue').textContent=`${$('speed').value} / 180`;};
$('tilt').oninput=()=>{$('tiltValue').textContent=`${$('tilt').value}°`;};
$('tilt').onchange=()=>{if(canDrive())api('drive',{action:'tilt',tilt:Number($('tilt').value),duration_ms:250}).catch(error=>toast(error.message));};
$('lightButton').onclick=()=>{if(canDrive()){const on=world.telemetry?.light_on??status.telemetry?.light_on;api('drive',{action:on?'light_off':'light_on',duration_ms:250}).then(refresh).catch(error=>toast(error.message));}};
$('showMap').onchange=()=>{$('mapPanel').hidden=!$('showMap').checked;};$('showPath').onchange=drawMap;
$('resetButton').onclick=()=>operation(async check=>{const seed=Number($('seed').value), selected=$('scenario').value, previousText=$('goal').value;if(!Number.isInteger(seed)||seed<0||seed>1000000)throw new Error('Seed musí být celé číslo od 0 do 1000000.');await pauseBeforeChange();check();await api('stop',{});status.armed=false;check();const reset=await api('simulator/reset',{scenario:selected,seed});check();if(reset.state)world=reset.state;await refresh();check();applyScenarioGoal(selected,previousText);$('settingsDialog').close();toast('Svět je obnovený. Auto stojí na startu.');});
$('fullscreenButton').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('cockpit').requestFullscreen();}catch{toast('Celou obrazovku tento prohlížeč nepovolil.');}};
for(const role of ['before','after'])$(`${role}Button`).onclick=()=>{const image=$(`${role}Frame`);if(!image.getAttribute('src')||!image.naturalWidth){toast('Snímek se objeví po prvním ověřeném kroku.');return;}$('frameTitle').textContent=role==='before'?'Před posledním krokem AI':'Po posledním kroku AI';$('expandedFrame').src=image.src;$('frameDialog').showModal();};
// A valid cookie may open the cockpit; this never activates driving or starts a task.
enterCockpit().catch(error=>{showLogin();if(!/přihlaš|unauthor|oprávně|401/i.test(error.message))$('loginError').textContent=error.message;});
