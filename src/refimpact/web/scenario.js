"use strict";

// "What if?": edit the state after any play, read the alternate win chance, then hold later
// plays as played to draw an alternate timeline. A chain leaves that timeline at one node and
// continues through states the user authors. All math stays on the server.
const whatIf = {game:null,home:null,away:null,plays:[],context:null,edits:[],chain:null,current:null,result:null,request:0};
const DRIVE_RESULTS = {td_xp:"TD + XP (7)",td_2pt:"TD + 2-pt (8)",fg:"Field goal (3)",none:"No score"};
const STEP_HINT = "You choose this state. It starts from the node before it: after a score, the other team's ball at the season's mean spot after a kickoff; after no score, the other team's ball at the same spot, with no punt assumed. No time comes off the clock until you set it.";
const pct = (p) => p == null ? "—" : `${(p * 100).toFixed(0)}%`;
const clockText = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2,"0")}`;
const whatIfTeam = () => $("#history-team")?.value || whatIf.home;
const forTeam = (value) => value == null ? null : whatIfTeam() === whatIf.home ? value : 1 - value;
const swingFor = (value) => value == null ? null : whatIfTeam() === whatIf.home ? value : -value;

const FIELD_NAMES = {quarter_seconds:"clock",yardline_100:"yard line",ydstogo:"yards to go",qtr:"quarter",home_score:"score",away_score:"score",home_timeouts:"timeouts",away_timeouts:"timeouts",drive_result:"drive result",drive_team:"scoring team"};
async function sendJSON(path,method,body) {
  const response = await fetch(path,{method,headers:{"Content-Type":"application/json"},body:body===undefined?undefined:JSON.stringify(body)});
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    const details = Array.isArray(data.detail) ? data.detail : null;
    const error = new Error(details ? details.map(d => {const field = d.loc?.slice(1).join(".").replace(/^chain\.steps\.(\d+)\.?/,(_,n) => `Step ${Number(n)+1} `).replace(/\w+$/,name => FIELD_NAMES[name]||name).trim(),message = d.msg.replace(/^Value error, /,"");return field?`${field}: ${message}`:message;}).join("; ") : data.detail || `API returned ${response.status}`);
    error.fields = details ? details.map(d => d.loc?.[d.loc.length-1]) : [];
    throw error;
  }
  return data;
}
function fieldOption(value,label,selected) { return `<option value="${escapeHTML(value)}" ${String(value)===String(selected)?"selected":""}>${escapeHTML(label)}</option>`; }
function playOption(p) {
  const situation = p.down ? `${p.possession} ${p.down}&${p.ydstogo} · ` : p.possession ? `${p.possession} · ` : "";
  return fieldOption(p.play_id,`Q${p.qtr} ${p.time||""} · ${situation}${(p.description||"").slice(0,70)}`,whatIf.current?.play_id);
}
function yardFields(state) {
  const own = state.yardline_100 >= 50, other = state.possession === whatIf.home ? whatIf.away : whatIf.home;
  return {side:own?state.possession:other,yard:own?100-state.yardline_100:state.yardline_100};
}
function shownState(play) {
  // A stacked edit starts from the shifted scoreboard the earlier edits produced.
  const state = {...play.default}, point = whatIf.result?.timeline.find(p => p.play_id === play.play_id);
  if (point?.alternate_home_score != null && point.status === "modeled") {
    state.home_score += point.alternate_home_score - point.actual_home_score;
    state.away_score += point.alternate_away_score - point.actual_away_score;
  }
  return state;
}
function blankState() {
  return {possession:whatIf.home,down:1,ydstogo:10,yardline_100:75,qtr:1,quarter_seconds:900,home_score:0,away_score:0,home_timeouts:3,away_timeouts:3};
}

async function initWhatIf(data) {
  const game = data.game, box = $("#whatif");
  Object.assign(whatIf,{game:game.game_id,home:game.home_team,away:game.away_team,plays:[],edits:[],chain:null,current:null,result:null});
  box.innerHTML = `<h3>What if?</h3><p class="footnote">Pick a play, including one with no flag for a no-call. The fields start at what was recorded next; change them to what you think should have happened.</p><p class="muted">Loading plays…</p>`;
  try {
    const plays = await get(`/api/v1/scenarios/plays?game_id=${encodeURIComponent(game.game_id)}`);
    if (whatIf.game !== game.game_id) return;
    whatIf.plays = plays.plays;whatIf.context = plays.context;
    renderWhatIfShell();
    await loadSaved();
  } catch (e) {
    box.innerHTML = `<h3>What if?</h3><p class="empty">${escapeHTML(e.message)}</p>`;
  }
}
function renderWhatIfShell() {
  const flagged = whatIf.plays.filter(p => p.flagged);
  $("#whatif").innerHTML = `<h3>What if?</h3><p class="footnote">Pick a play, including one with no flag for a no-call. The fields start at what was recorded next; change them to what you think should have happened.</p>
    <label class="whatif-play">Play <select id="whatif-play"><option value="">Choose a play…</option><option value="blank">Blank state (no play or timeline)</option>
      <optgroup label="Flags">${flagged.map(playOption).join("")}</optgroup><optgroup label="All plays">${whatIf.plays.map(playOption).join("")}</optgroup></select></label>
    <div id="whatif-form"></div><div id="whatif-error" class="error hidden" role="alert"></div><div id="whatif-stack"></div><div id="whatif-result" aria-live="polite"></div>
    <div id="whatif-save"></div><div id="whatif-saved"></div>`;
}
function renderForm() {
  const c = whatIf.current;
  if (!c) { $("#whatif-form").innerHTML=""; return; }
  const s = c.shown, yard = yardFields(s), teams = [whatIf.away,whatIf.home];
  const team = (v) => teams.map(t => fieldOption(t,t,v)).join(""), counts = (v,n=3) => Array.from({length:n+1},(_,i) => fieldOption(i,i,v)).join("");
  $("#whatif-form").innerHTML = `<div class="whatif-fields">
    <label>Ball <select data-field="possession">${team(s.possession)}</select></label>
    <label>Down <select data-field="down">${[1,2,3,4].map(d => fieldOption(d,d,s.down)).join("")}</select></label>
    <label>Distance <input data-field="ydstogo" type="number" min="1" max="99" value="${s.ydstogo}"></label>
    <label>Yard line <span class="whatif-pair"><select data-field="side">${team(yard.side)}</select><input data-field="yard" type="number" min="1" max="50" value="${yard.yard}" aria-label="Yard number"></span></label>
    <label>Quarter <select data-field="qtr">${[1,2,3,4].map(q => fieldOption(q,`Q${q}`,s.qtr)).join("")}${c.step?fieldOption(5,"OT",s.qtr):""}</select></label>
    <label>Clock <input data-field="clock" inputmode="numeric" pattern="[0-9]{1,2}:[0-9]{2}" value="${clockText(s.quarter_seconds)}"></label>
    <label>${escapeHTML(whatIf.away)} score <input data-field="away_score" type="number" min="0" value="${s.away_score}"></label>
    <label>${escapeHTML(whatIf.home)} score <input data-field="home_score" type="number" min="0" value="${s.home_score}"></label>
    <label>${escapeHTML(whatIf.away)} timeouts <select data-field="away_timeouts">${counts(s.away_timeouts)}</select></label>
    <label>${escapeHTML(whatIf.home)} timeouts <select data-field="home_timeouts">${counts(s.home_timeouts)}</select></label>
    ${c.blank?"":`<label>Drive ends <select data-field="drive_result"><option value="">${c.step?"Drive continues":"Moment only"}</option>${Object.entries(DRIVE_RESULTS).map(([k,v]) => fieldOption(k,v,c.drive_result)).join("")}</select></label><label>Result goes to <select data-field="drive_team"><option value="">Team with the ball</option>${team(c.drive_team)}</select></label>`}
  </div>${c.pre_play?'<p class="clock-note">No later snap in this half, so these fields start at this play\'s own pre-snap state.</p>':""}${c.step?`<p class="clock-note">${escapeHTML(STEP_HINT)}</p><div class="whatif-actions"><button class="whatif-button" id="whatif-step-add">Add next step</button><button class="whatif-button quiet" id="whatif-step-remove">Remove last step</button><button class="whatif-button quiet" id="whatif-chain-clear">Clear the chain</button></div>`:c.blank?"":`<div class="whatif-actions"><button class="whatif-button" id="whatif-add">Add and edit another play</button><button class="whatif-button quiet" id="whatif-reset">Reset fields</button></div>`}`;
}
function readForm() {
  const value = (name) => $(`#whatif-form [data-field="${name}"]`)?.value;
  const clock = /^(\d{1,2}):(\d{2})$/.exec(value("clock") || "");
  if (!clock || Number(clock[2]) > 59) throw new Error("Enter the clock as minutes:seconds, for example 9:01");
  const possession = value("possession"), yard = Number(value("yard"));
  if (!(yard >= 1 && yard <= 50)) throw new Error("Yard line must be between 1 and 50");
  return {possession,down:Number(value("down")),ydstogo:Number(value("ydstogo")),yardline_100:value("side")===possession?100-yard:yard,qtr:Number(value("qtr")),quarter_seconds:Number(clock[1])*60+Number(clock[2]),home_score:Number(value("home_score")),away_score:Number(value("away_score")),home_timeouts:Number(value("home_timeouts")),away_timeouts:Number(value("away_timeouts"))};
}
function changesFrom(fields,shown) {
  // Send only what the user changed, so the server keeps the rest of the recorded state.
  return Object.fromEntries(Object.entries(fields).filter(([k,v]) => v !== shown[k]));
}
function showWhatIfError(message,fields) {
  $("#whatif-error").textContent = message || "";
  $("#whatif-error").classList.toggle("hidden",!message);
  document.querySelectorAll("#whatif-form .field-error").forEach(input => input.classList.remove("field-error"));
  const names = {quarter_seconds:"clock",yardline_100:"yard"};
  (fields||[]).forEach(field => $(`#whatif-form [data-field="${names[field]||field}"]`)?.classList.add("field-error"));
}
function readStep() {
  const value = (name) => $(`#whatif-form [data-field="${name}"]`)?.value || null, team = value("drive_team"), result = value("drive_result");
  return {...readForm(),drive_result:result,...(result&&team?{drive_team:team}:{})};
}
function chainRequest() {
  const chain = whatIf.chain;if(!chain)return null;
  const steps = whatIf.current?.step ? [...chain.steps,readStep()] : chain.steps;
  return steps.length ? {from_play_id:chain.from_play_id,steps,end:chain.end,...(chain.end==="rejoin"?{rejoin_play_id:chain.rejoin_play_id}:{})} : null;
}
function openStep(shown,result=null,team=null) {
  whatIf.current = {step:true,shown,drive_result:result,drive_team:team};
  renderForm();
  return scoreWhatIf();
}
function branchFrom(id) {
  const r = whatIf.result, edit = r.edits.find(e => e.play_id === id), point = r.timeline.find(p => p.play_id === id);
  whatIf.edits = currentEdits();
  whatIf.chain = {from_play_id:id,steps:[],end:"prediction",rejoin_play_id:null};
  $("#whatif-play").value = "";
  showWhatIfError("");
  return openStep(edit ? edit.next_state || edit.alternate.state : point.alternate_state);
}
async function addStep() {
  if (!await scoreWhatIf()) return;
  const last = whatIf.result.chain.nodes[whatIf.result.chain.nodes.length-1], step = readStep();
  whatIf.chain.steps.push(step);
  await openStep(last.next_state || last.state);
}
function clearChain() {
  whatIf.chain = null;whatIf.current = null;
  renderForm();showWhatIfError("");
  return scoreWhatIf();
}
function removeStep() {
  const chain = whatIf.chain, drafting = whatIf.current?.step, previous = chain.steps.pop();
  if (!previous) return clearChain();
  if (drafting) return openStep(previous,previous.drive_result,previous.drive_team);
  if (!chain.steps.length) return clearChain();
  return scoreWhatIf();
}
function continueFrom(index) {
  // Going back replaces the tail: one active path, no tree.
  const chain = whatIf.chain, node = whatIf.result.chain.nodes[index];
  if (whatIf.current?.step && index > chain.steps.length) chain.steps.push(readStep());
  chain.steps = chain.steps.slice(0,index);
  return openStep(node.next_state || node.state);
}
async function scoreWhatIf() {
  const c = whatIf.current, request = ++whatIf.request;
  try {
    showWhatIfError("");
    if (c?.blank) {
      const fields = readForm(), half = fields.quarter_seconds + ([1,3].includes(fields.qtr) ? 900 : 0);
      const {quarter_seconds:_,...rest} = fields;
      const read = await sendJSON("/api/v1/scenarios/state","POST",{...rest,...whatIf.context,home_team:whatIf.home,away_team:whatIf.away,half_seconds_remaining:half,game_seconds_remaining:half+(fields.qtr<=2?1800:0)});
      if (request === whatIf.request) renderBlankResult(read);
      return;
    }
    const edits = whatIf.edits.filter(e => e.play_id !== c?.play_id);
    if (c && !c.step) {
      c.drive_result = $(`#whatif-form [data-field="drive_result"]`)?.value || null;
      c.drive_team = $(`#whatif-form [data-field="drive_team"]`)?.value || null;
      c.changes = changesFrom(readForm(),c.shown);
      edits.push({play_id:c.play_id,changes:c.changes,drive_result:c.drive_result,...(c.drive_result&&c.drive_team?{drive_team:c.drive_team}:{})});
    }
    if (!edits.length) { whatIf.result=null;$("#whatif-result").innerHTML="";renderStack();renderSaveForm();return false; }
    const chain = chainRequest();
    const result = await sendJSON("/api/v1/scenarios/evaluate","POST",{game_id:whatIf.game,edits,...(chain?{chain}:{})});
    if (request !== whatIf.request) return false;
    whatIf.result = result;
    renderStack();renderScenario();renderSaveForm();
    return true;
  } catch (e) {
    if (request === whatIf.request) showWhatIfError(e.message,e.fields);
    return false;
  }
}
function selectWhatIfPlay(id) {
  showWhatIfError("");
  if (id === "blank") { whatIf.current = {blank:true,shown:blankState()};renderForm();scoreWhatIf();return; }
  const play = whatIf.plays.find(p => p.play_id === id);
  if (!play) { whatIf.current = null;renderForm();scoreWhatIf();return; }
  const existing = whatIf.edits.find(e => e.play_id === id);
  // Reopening a stacked edit shows it from its own starting scoreboard, not its own result.
  const shown = shownState(play);
  if (existing) {
    const start = whatIf.result?.edits.find(e => e.play_id === id)?.actual.state;
    if (start) { shown.home_score = start.home_score;shown.away_score = start.away_score; }
  }
  // A drive result belongs to the team that ran this play unless the user says otherwise.
  whatIf.current = {play_id:id,description:play.description,shown:{...shown,...(existing?.changes||{})},base:shown,drive_result:existing?.drive_result||null,drive_team:existing?.drive_team||null,pre_play:play.default_is_pre_play};
  const c = whatIf.current;
  renderForm();
  c.shown = c.base;
  scoreWhatIf();
}
function stateText(s) {
  const yard = s.yardline_100 === 50 ? "50" : s.yardline_100 > 50 ? `${s.possession} ${100-s.yardline_100}` : `${s.possession===whatIf.home?whatIf.away:whatIf.home} ${s.yardline_100}`;
  return `${s.possession} ${s.down}&${s.ydstogo>=s.yardline_100?"G":s.ydstogo} at ${yard}, Q${s.qtr} ${clockText(s.quarter_seconds)}, ${whatIf.away} ${s.away_score}–${whatIf.home} ${s.home_score}`;
}
function renderStack() {
  const saved = whatIf.edits, chain = whatIf.chain, locked = chain?"disabled":"";
  if ($("#whatif-play")) $("#whatif-play").disabled = !!chain;
  $("#whatif-stack").innerHTML = saved.length ? `<div class="whatif-stack"><span class="mini-label">IN THIS TIMELINE</span>${saved.map(e => {
    const play = whatIf.plays.find(p => p.play_id === e.play_id);
    return `<span class="whatif-chip"><button class="whatif-chip-open" data-whatif-open="${escapeHTML(e.play_id)}" ${locked}>Q${play?.qtr} ${escapeHTML(play?.time||"")} · ${escapeHTML(DRIVE_RESULTS[e.drive_result]||"Moment only")}</button><button class="whatif-chip-x" data-whatif-remove="${escapeHTML(e.play_id)}" aria-label="Remove this edit" ${locked}>×</button></span>`;
  }).join("")}${chain?'<span class="muted">Clear the chain to change these edits or pick another play.</span>':""}</div>` : "";
}
function nodeLabel(node,r) {
  const when = r.timeline.find(p => p.play_id === r.chain.from_play_id);
  if (node.kind === "edit") return `Your edit, after the play at Q${when?.qtr} ${when?.time||""}`;
  if (node.kind === "modeled") return `Alternate timeline, snap at Q${when?.qtr} ${when?.time||""}`;
  return "Your step";
}
function renderNode(node,r,previous) {
  const team = whatIfTeam(), o = node.opportunity, isDraft = whatIf.current?.step && node.index === r.chain.nodes.length-1;
  const ending = node.drive_result ? `Drive ends: ${DRIVE_RESULTS[node.drive_result]}${node.drive_result==="none"?"":` for ${node.drive_team}`} → ${whatIf.away} ${node.score_after.away}–${whatIf.home} ${node.score_after.home}` : "";
  const cell = (label,value,tone="") => `<div><dt>${escapeHTML(label)}</dt><dd class="${tone}">${value}</dd></div>`;
  const swing = swingFor(node.wp_swing_pp), vegas = swingFor(node.wp_vegas_swing_pp);
  return `<li class="chain-node ${node.kind==="step"?"":"source"} ${isDraft?"draft":""}"><div class="chain-head"><span class="chain-index">${node.index}</span><div class="chain-state"><strong>${escapeHTML(stateText(node.state))}</strong><span class="subline">${escapeHTML(nodeLabel(node,r))}${isDraft?" · in the form above":""}</span>${ending?`<span class="subline">${escapeHTML(ending)}</span>`:""}</div>${isDraft?"":`<button class="whatif-button quiet chain-from" data-chain-from="${node.index}">Continue from here</button>`}</div>
    <dl class="chain-metrics">${cell(`${team} WIN CHANCE`,pct(forTeam(node.home_wp)))}${cell("VEGAS MODEL",pct(forTeam(node.home_wp_vegas)))}${cell(`EP · ${team}`,signed(swingFor(node.home_ep)))}${cell("VS PREVIOUS NODE",previous?swingPP(swing):"start",colorClass(swing))}${cell("VEGAS VS PREVIOUS",previous&&vegas!=null?wholePP(vegas):"—")}${cell(`DOWNS · ${o.team}`,o.downs_available)}${cell(`NEXT SCORE TD · ${o.team}`,pct(o.td))}${cell(`NEXT SCORE FG · ${o.team}`,pct(o.fg))}</dl></li>`;
}
function renderChain() {
  const r = whatIf.result, chain = r.chain, end = chain.end, last = chain.nodes[chain.nodes.length-1], team = whatIfTeam();
  const finalAlt = r.final.alternate, finalAct = r.final.actual;
  const standing = end.mode === "prediction"
    ? `<div class="whatif-card"><span class="mini-label">WHERE THE CHAIN STANDS · CONDITIONAL ON YOUR STEPS</span><strong>${escapeHTML(whatIf.away)} ${end.score.away}–${escapeHTML(whatIf.home)} ${end.score.home}</strong><p>${escapeHTML(team)} win chance ${pct(forTeam(end.home_wp))}${end.home_wp_vegas==null?"":` · Vegas model ${pct(forTeam(end.home_wp_vegas))}`}${end.reads_before_drive_result?", read at the start of this step. Add the next step to read it after the drive":""}.</p><p class="muted">No final score is given. Nothing follows the last step.</p></div>`
    : `<div class="whatif-card"><span class="mini-label">FINAL · RECORDED PLAYS HELD · HINDSIGHT</span><strong>${escapeHTML(whatIf.away)} ${finalAlt.away}–${escapeHTML(whatIf.home)} ${finalAlt.home}</strong><p>${escapeHTML(end.label)}</p><p class="muted">Actual: ${escapeHTML(whatIf.away)} ${finalAct.away}–${escapeHTML(whatIf.home)} ${finalAct.home}.</p></div>`;
  const options = chain.rejoin_options.map(p => fieldOption(p.play_id,`Q${p.qtr} ${p.time||""} · ${p.possession||""} · ${p.description}`,whatIf.chain.rejoin_play_id)).join("");
  $("#whatif-result").innerHTML = `<div class="whatif-cards">${standing}<div class="whatif-card"><span class="mini-label">AFTER THE LAST STEP</span><label class="chain-end">Ending <select id="whatif-end">${fieldOption("prediction","Stop at the last step",end.mode)}${chain.rejoin_options.length?fieldOption("rejoin","Rejoin recorded plays (hindsight)",end.mode):""}</select></label>${end.mode==="rejoin"?`<label class="chain-end">Rejoin at <select id="whatif-rejoin">${options}</select></label>`:""}</div></div>
    ${chain.warnings.map(w => `<p class="clock-note">${escapeHTML(w)}</p>`).join("")}
    <h4 class="chain-title">Timeline</h4><ol class="chain-list">${chain.nodes.map((n,i) => renderNode(n,r,chain.nodes[i-1])).join("")}</ol>
    <div id="whatif-chart" class="whatif-chart"></div><p class="footnote">${escapeHTML(r.note)} Win chance and points are read by the same pinned models as every other state here, with nothing added. Next-score chances are in the frame of the team with the ball.</p>`;
  drawWhatIfChart();
}
function renderBlankResult(read) {
  const team = whatIfTeam(), p = forTeam(read.home_wp);
  $("#whatif-result").innerHTML = `<div class="whatif-cards"><div class="whatif-card"><span class="mini-label">${escapeHTML(team)} WIN CHANCE</span><strong>${pct(p)}</strong><p>${escapeHTML(stateText(read.state))}</p></div></div><p class="footnote">${escapeHTML(read.note)}</p>`;
}
function renderScenario() {
  const r = whatIf.result;if(!r)return;
  if (r.chain) { renderChain();return; }
  const team = whatIfTeam(), focus = r.edits.find(e => e.play_id === whatIf.current?.play_id) || r.edits[r.edits.length-1];
  const alt = forTeam(focus.alternate.home_wp), act = forTeam(focus.actual.home_wp), swing = swingFor(focus.wp_swing_pp), points = swingFor(focus.points_swing), next = swingFor(focus.aftermath_pp);
  const finalAlt = r.final.alternate, finalAct = r.final.actual, regulation = r.final.actual_regulation;
  const vegas = swingFor(focus.wp_vegas_swing_pp);
  const outcome = !finalAlt ? "" : finalAlt.result==="tie_ot_not_modeled" ? "Tied; OT not modeled" : finalAlt.result==="home_win" ? `${whatIf.home} would finish ahead` : finalAlt.result==="away_win" ? `${whatIf.away} would finish ahead` : "Game not final";
  $("#whatif-result").innerHTML = `<div class="whatif-cards">
    <div class="whatif-card"><span class="mini-label">${escapeHTML(team)} WIN CHANCE AT THAT MOMENT</span><strong>${pct(act)} → ${pct(alt)}</strong><p class="${colorClass(swing)}">${swingPP(swing)} · ${signed(points)} expected points, ignores score</p>${vegas==null?"":`<p class="muted">Vegas model: ${wholePP(vegas)}</p>`}</div>
    <div class="whatif-card"><span class="mini-label">RECORDED NEXT</span><p>${escapeHTML(stateText(focus.actual.state))}</p><span class="mini-label">YOUR VERSION</span><p>${escapeHTML(stateText(focus.alternate.state))}</p></div>
    ${next==null?"":`<div class="whatif-card"><span class="mini-label">WHAT THE REAL GAME DID NEXT</span><strong class="${colorClass(next)}">${wholePP(next)}</strong><p>To the end of that drive, from the recorded state. Separate from the call.</p></div>`}
    ${finalAlt?`<div class="whatif-card"><span class="mini-label">FINAL · LATER PLAYS HELD AS PLAYED</span><strong>${escapeHTML(whatIf.away)} ${finalAlt.away}–${escapeHTML(whatIf.home)} ${finalAlt.home}</strong><p>Actual: ${escapeHTML(whatIf.away)} ${finalAct.away}–${escapeHTML(whatIf.home)} ${finalAct.home}${regulation?` after overtime; ${escapeHTML(whatIf.away)} ${regulation.away}–${escapeHTML(whatIf.home)} ${regulation.home} at the end of regulation, which is what the alternate compares with`:""}. ${escapeHTML(outcome)}.</p></div>`:`<div class="whatif-card"><span class="mini-label">NEXT STEP</span><p>Choose how this drive ends to draw the alternate timeline.</p></div>`}
  </div>${(focus.warnings||[]).map(w => `<p class="clock-note">${escapeHTML(w)}</p>`).join("")}${branchActions(r)}${opportunityPanel(focus.opportunity)}<div id="whatif-chart" class="whatif-chart"></div><p class="footnote">${escapeHTML(r.note)} ${r.parity.max_abs_wp_error==null?"":r.parity.max_abs_wp_error<0.00001?`Without edits, the model reproduces the published win chance on all ${r.parity.rows} recorded snaps.`:`Warning: without edits, the model differs from the published win chance by up to ${r.parity.max_abs_wp_error*100<1?"under 1":Math.round(r.parity.max_abs_wp_error*100)} pp.`}</p>`;
  drawWhatIfChart();
}
function branchActions(r) {
  const edit = r.edits[r.edits.length-1], start = r.timeline.findIndex(p => p.play_id === edit.play_id);
  const later = r.timeline.slice(start+1).filter(p => p.alternate_state);
  return `<div class="whatif-actions chain-branch"><button class="whatif-button" data-whatif-branch="${escapeHTML(edit.play_id)}">What if from here?</button>${later.length?`<label class="chain-end">Or from a later alternate snap <select id="whatif-branch-later"><option value="">Choose a snap…</option>${later.map(p => fieldOption(p.play_id,`Q${p.qtr} ${p.time||""} · ${stateText(p.alternate_state)}`,"")).join("")}</select></label>`:""}</div>`;
}
function chainPoints(rows,x,W,R,L) {
  // Steps sit at their game clock on the recorded axis, in order, never stacked on one another.
  const nodes = whatIf.result.chain.nodes, gap = 15, right = W-R-4;
  const at = (seconds) => {const k = rows.findIndex(p => p.game_seconds != null && p.game_seconds <= seconds);return x(k < 0 ? rows.length-1 : k);};
  const start = rows.findIndex(p => p.play_id === whatIf.result.chain.from_play_id), xs = [x(start)];
  nodes.slice(1).forEach((n,i) => xs.push(Math.max(at(n.state.game_seconds_remaining),xs[i]+gap)));
  for (let i = xs.length-1;i > 0;i--) xs[i] = Math.min(xs[i],i===xs.length-1?right:xs[i+1]-gap);
  xs[0] = Math.max(L,Math.min(xs[0],xs[1]??xs[0]));
  return nodes.map((n,i) => ({x:xs[i],value:forTeam(n.home_wp),node:n}));
}
function drawWhatIfChart() {
  const r = whatIf.result, box = $("#whatif-chart");if(!r||!box)return;
  const rows = r.timeline, W = Math.max(280,Math.min(740,box.clientWidth||740)), H = 240, L = 42, R = 15, T = 18, B = 34;
  const x = (i) => L+i/Math.max(1,rows.length-1)*(W-L-R), y = (v) => T+(1-v)*(H-T-B);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHTML(whatIfTeam())} actual and alternate win chance through the game">`;
  [0,.25,.5,.75,1].forEach(v => {svg += `<line x1="${L}" y1="${y(v)}" x2="${W-R}" y2="${y(v)}" stroke="#334036"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end" fill="#a2b1a5" font-size="10">${v*100}%</text>`;});
  const quarters = new Set();
  rows.forEach((p,i) => {if(p.qtr!=null&&!quarters.has(p.qtr)){quarters.add(p.qtr);svg += `<text x="${x(i)}" y="${H-12}" fill="#a2b1a5" font-size="10">${p.qtr>4?"OT":`Q${p.qtr}`}</text>`;}});
  const line = (field) => {let path="",open=false;rows.forEach((p,i) => {const value = forTeam(p[field]);if(value==null){open=false;return;}path += open?`L${x(i)},${y(value)}`:`M${x(i)},${y(value)}`;open=true;});return path;};
  svg += `<path d="${line("actual_home_wp")}" fill="none" stroke="#d1f482" stroke-width="2"/><path d="${line("alternate_home_wp")}" fill="none" stroke="#c6b2f6" stroke-width="2" stroke-dasharray="5 3"/>`;
  rows.forEach((p,i) => {if(p.status!=="edit")return;const value = forTeam(p.alternate_home_wp);svg += `<circle cx="${x(i)}" cy="${y(value)}" r="5" fill="#c6b2f6" stroke="#111713" tabindex="0"><title>Q${p.qtr} ${escapeHTML(p.time||"")} · your version: ${pct(value)}</title></circle>`;});
  const chain = r.chain;
  if (chain) {
    const points = chainPoints(rows,x,W,R,L);
    svg += `<path d="${points.map((p,i) => `${i?"L":"M"}${p.x},${y(p.value)}`).join("")}" fill="none" stroke="#efb18c" stroke-width="2"/>`;
    if (chain.end.mode === "rejoin") {
      const k = rows.findIndex(p => p.play_id === chain.end.rejoin_play_id), last = points[points.length-1];
      svg += `<path d="M${last.x},${y(last.value)}L${x(k)},${y(forTeam(rows[k].alternate_home_wp))}" fill="none" stroke="#efb18c" stroke-width="1" stroke-dasharray="2 3"/>`;
    }
    points.slice(1).forEach(p => {const cy = y(p.value);svg += `<g tabindex="0"><title>Step ${p.node.index} · ${escapeHTML(stateText(p.node.state))} · ${pct(p.value)}</title><rect x="${p.x-6}" y="${cy-6}" width="12" height="12" transform="rotate(45 ${p.x} ${cy})" fill="#efb18c" stroke="#111713"/><text x="${p.x}" y="${cy+3}" text-anchor="middle" fill="#111713" font-size="9" font-weight="700">${p.node.index}</text></g>`;});
  }
  const alternate = !chain ? "Alternate, later plays held as played" : chain.end.mode === "rejoin" ? "Alternate, recorded plays held (hindsight)" : rows.filter(p => p.alternate_home_wp != null).length > 1 ? "Alternate up to where the chain starts" : "Your edit";
  box.innerHTML = `${svg}</svg><p class="whatif-legend"><span class="key actual"></span> Actual <span class="key alternate"></span> ${alternate}${chain?' <span class="key chain"></span> Your steps, in order; your choices, not a forecast':""}</p>`;
}
function renderSaveForm() {
  const box = $("#whatif-save");if(!box)return;
  if (!whatIf.result || whatIf.current?.blank) { box.innerHTML="";return; }
  const open = $("#whatif-save details")?.open;
  if ($("#whatif-save details")) { syncSaveForm();return; }
  box.innerHTML = `<details class="whatif-details" ${open?"open":""}><summary>Save this scenario</summary><div class="whatif-fields">
    <label class="wide">Label <input id="whatif-label" maxlength="120" placeholder="Smith-Njigba OPI"></label>
    <label>Type <select id="whatif-kind"><option value="flag">Flag</option><option value="no_call">No-call</option><option value="what_if">What-if</option></select></label>
    <label class="wide">Source link <input id="whatif-source" type="url" placeholder="https://…"></label>
    <label class="wide">Assumptions, one per line <textarea id="whatif-assumptions" rows="2"></textarea></label>
    <label class="check"><input id="whatif-disputed" type="checkbox"> Disputed call (needs a source)</label>
  </div><div class="whatif-actions"><button class="whatif-button" id="whatif-store">Save</button></div><p id="whatif-save-note" class="footnote"></p></details>`;
  syncSaveForm();
}
function syncSaveForm() {
  // A chain's steps are hypotheticals, not disputed calls.
  const chain = !!whatIf.chain;
  $("#whatif-disputed")?.closest("label").classList.toggle("hidden",chain);
  if (chain && $("#whatif-kind")) $("#whatif-kind").value = "what_if";
}
function currentEdits() {
  const c = whatIf.current, edits = whatIf.edits.filter(e => e.play_id !== c?.play_id);
  if (c && !c.blank && !c.step) edits.push({play_id:c.play_id,changes:c.changes||{},drive_result:c.drive_result,...(c.drive_result&&c.drive_team?{drive_team:c.drive_team}:{})});
  return edits;
}
async function loadSaved() {
  try {
    const [saved,report] = await Promise.all([get(`/api/v1/scenarios?game_id=${encodeURIComponent(whatIf.game)}`),get(`/api/v1/scenarios/disputes?game_id=${encodeURIComponent(whatIf.game)}`)]);
    if (!$("#whatif-saved")) return;
    const signedFor = (v,digits) => signed(swingFor(v),digits);
    const disputed = report.calls.length ? `<section class="whatif-report"><h4>Disputed calls</h4><div class="table-wrap"><table><thead><tr><th>CALL</th><th>WHEN</th><th>${escapeHTML(whatIfTeam())} WIN CHANCE</th><th>POINTS</th><th>SOURCE</th></tr></thead><tbody>${report.calls.map(c => `<tr><td>${escapeHTML(c.label)}${c.stale?' <span class="muted">(older data)</span>':""}${c.stacked?' <span class="muted">(valued after earlier edits)</span>':""}</td><td>Q${c.qtr} ${escapeHTML(c.time||"")}</td><td>${swingPP(swingFor(c.wp_swing_pp))}</td><td>${signedFor(c.points_swing)}</td><td>${c.source_url?`<a class="text-link" href="${escapeHTML(c.source_url)}" target="_blank" rel="noopener">link</a>`:""}</td></tr>`).join("")}</tbody></table></div><p><strong>Sum of at-the-moment expected points · ${escapeHTML(whatIfTeam())}: ${signedFor(report.home_sum_at_moment_expected_points)}.</strong></p><p class="footnote">${escapeHTML(report.note)}</p></section>` : "";
    $("#whatif-saved").innerHTML = disputed + (saved.scenarios.length ? `<section class="whatif-report"><h4>Saved scenarios</h4>${saved.scenarios.map(s => `<div class="whatif-saved-row"><button class="text-link" data-whatif-load="${escapeHTML(s.scenario_id)}">${escapeHTML(s.label)}</button><span class="muted">${escapeHTML(s.kind.replace("_","-"))}${s.disputed?" · disputed":""}${s.request.chain?` · ${s.request.chain.steps.length}-step chain, ${s.request.chain.end==="rejoin"?"rejoins":"prediction"}`:""}</span><button class="whatif-chip-x" data-whatif-delete="${escapeHTML(s.scenario_id)}" aria-label="Delete ${escapeHTML(s.label)}">×</button></div>`).join("")}</section>` : "");
  } catch (e) {
    showWhatIfError(e.message);
  }
}
async function storeScenario() {
  const note = $("#whatif-save-note"), source = $("#whatif-source").value.trim();
  try {
    const chain = chainRequest();
    const body = {game_id:whatIf.game,edits:currentEdits(),...(chain?{chain}:{}),label:$("#whatif-label").value.trim(),kind:$("#whatif-kind").value,disputed:!chain&&$("#whatif-disputed").checked,source_url:source||null,assumptions:$("#whatif-assumptions").value.split("\n").map(s => s.trim()).filter(Boolean)};
    await sendJSON("/api/v1/scenarios","POST",body);
    note.textContent = "Saved.";
    await loadSaved();
  } catch (e) {
    note.textContent = e.message;
  }
}
async function openSaved(id) {
  try {
    const record = await get(`/api/v1/scenarios/${encodeURIComponent(id)}`);
    whatIf.edits = record.request.edits;whatIf.current = null;
    whatIf.chain = record.request.chain ? {...record.request.chain,steps:[...record.request.chain.steps]} : null;
    $("#whatif-play").value = "";renderForm();
    await scoreWhatIf();
  } catch (e) { showWhatIfError(e.message); }
}

document.addEventListener("change",e => {
  if (e.target.id === "whatif-play") { selectWhatIfPlay(e.target.value);return; }
  if (e.target.id === "whatif-branch-later" && e.target.value) { branchFrom(e.target.value);return; }
  if (e.target.id === "whatif-end") {
    whatIf.chain.end = e.target.value;
    whatIf.chain.rejoin_play_id = e.target.value === "rejoin" ? whatIf.result.chain.rejoin_options[0]?.play_id ?? null : null;
    scoreWhatIf();return;
  }
  if (e.target.id === "whatif-rejoin") { whatIf.chain.rejoin_play_id = e.target.value;scoreWhatIf();return; }
  if (e.target.closest("#whatif-form")) { scoreWhatIf();return; }
  if (e.target.id === "history-team" && whatIf.game) {
    if (whatIf.current?.blank) scoreWhatIf(); else renderScenario();
    loadSaved();
  }
});
document.addEventListener("click",e => {
  const button = e.target.closest("button,[data-whatif-play]");if(!button)return;
  if (button.dataset.whatifBranch) { branchFrom(button.dataset.whatifBranch);return; }
  if (button.dataset.chainFrom) { continueFrom(Number(button.dataset.chainFrom));return; }
  if (button.id === "whatif-step-add") { addStep();return; }
  if (button.id === "whatif-step-remove") { removeStep();return; }
  if (button.id === "whatif-chain-clear") { clearChain();return; }
  if (button.dataset.whatifPlay) {
    $("#whatif-play").value = button.dataset.whatifPlay;selectWhatIfPlay(button.dataset.whatifPlay);
    $("#whatif").scrollIntoView({behavior:"smooth",block:"start"});
  } else if (button.id === "whatif-add") {
    const c = whatIf.current;
    if (!c.drive_result) { showWhatIfError("Choose how this drive ends before adding another play.");return; }
    whatIf.edits = currentEdits();whatIf.current = null;
    $("#whatif-play").value = "";renderForm();scoreWhatIf();
  } else if (button.id === "whatif-reset") {
    selectWhatIfPlay(whatIf.current.play_id);
  } else if (button.dataset.whatifOpen) {
    $("#whatif-play").value = button.dataset.whatifOpen;selectWhatIfPlay(button.dataset.whatifOpen);
  } else if (button.dataset.whatifRemove) {
    whatIf.edits = whatIf.edits.filter(edit => edit.play_id !== button.dataset.whatifRemove);
    if (whatIf.current?.play_id === button.dataset.whatifRemove) { whatIf.current = null;$("#whatif-play").value = "";renderForm(); }
    scoreWhatIf();
  } else if (button.id === "whatif-store") {
    storeScenario();
  } else if (button.dataset.whatifLoad) {
    openSaved(button.dataset.whatifLoad);
  } else if (button.dataset.whatifDelete) {
    sendJSON(`/api/v1/scenarios/${encodeURIComponent(button.dataset.whatifDelete)}`,"DELETE").then(loadSaved).catch(error => showWhatIfError(error.message));
  }
});
