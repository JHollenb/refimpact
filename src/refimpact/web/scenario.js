"use strict";

// "What if?": edit the state after any play, read the alternate win chance, then hold later
// plays as played to draw an alternate timeline. All math stays on the server.
const whatIf = {game:null,home:null,away:null,plays:[],context:null,edits:[],current:null,result:null,request:0};
const DRIVE_RESULTS = {td_xp:"TD + XP (7)",td_2pt:"TD + 2-pt (8)",fg:"Field goal (3)",none:"No score"};
const pct = (p) => p == null ? "—" : `${(p * 100).toFixed(1)}%`;
const clockText = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2,"0")}`;
const whatIfTeam = () => $("#history-team")?.value || whatIf.home;
const forTeam = (value) => value == null ? null : whatIfTeam() === whatIf.home ? value : 1 - value;
const swingFor = (value) => value == null ? null : whatIfTeam() === whatIf.home ? value : -value;

async function sendJSON(path,method,body) {
  const response = await fetch(path,{method,headers:{"Content-Type":"application/json"},body:body===undefined?undefined:JSON.stringify(body)});
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(Array.isArray(data.detail) ? data.detail.map(d => {const field = d.loc?.slice(1).join("."),message = d.msg.replace(/^Value error, /,"");return field?`${field}: ${message}`:message;}).join("; ") : data.detail || `API returned ${response.status}`);
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
  Object.assign(whatIf,{game:game.game_id,home:game.home_team,away:game.away_team,plays:[],edits:[],current:null,result:null});
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
    <label>Quarter <select data-field="qtr">${[1,2,3,4].map(q => fieldOption(q,`Q${q}`,s.qtr)).join("")}</select></label>
    <label>Clock <input data-field="clock" inputmode="numeric" pattern="[0-9]{1,2}:[0-9]{2}" value="${clockText(s.quarter_seconds)}"></label>
    <label>${escapeHTML(whatIf.away)} score <input data-field="away_score" type="number" min="0" value="${s.away_score}"></label>
    <label>${escapeHTML(whatIf.home)} score <input data-field="home_score" type="number" min="0" value="${s.home_score}"></label>
    <label>${escapeHTML(whatIf.away)} timeouts <select data-field="away_timeouts">${counts(s.away_timeouts)}</select></label>
    <label>${escapeHTML(whatIf.home)} timeouts <select data-field="home_timeouts">${counts(s.home_timeouts)}</select></label>
    ${c.blank?"":`<label>Drive ends <select data-field="drive_result"><option value="">Moment only</option>${Object.entries(DRIVE_RESULTS).map(([k,v]) => fieldOption(k,v,c.drive_result)).join("")}</select></label>`}
  </div>${c.blank?"":`<div class="whatif-actions"><button class="whatif-button" id="whatif-add">Add and edit another play</button><button class="whatif-button quiet" id="whatif-reset">Reset fields</button></div>`}`;
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
function showWhatIfError(message) {
  $("#whatif-error").textContent = message || "";
  $("#whatif-error").classList.toggle("hidden",!message);
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
    if (c) {
      c.drive_result = $(`#whatif-form [data-field="drive_result"]`)?.value || null;
      c.changes = changesFrom(readForm(),c.shown);
      edits.push({play_id:c.play_id,changes:c.changes,drive_result:c.drive_result});
    }
    if (!edits.length) { whatIf.result=null;$("#whatif-result").innerHTML="";renderStack();renderSaveForm();return; }
    const result = await sendJSON("/api/v1/scenarios/evaluate","POST",{game_id:whatIf.game,edits});
    if (request !== whatIf.request) return;
    whatIf.result = result;
    renderStack();renderScenario();renderSaveForm();
  } catch (e) {
    if (request === whatIf.request) showWhatIfError(e.message);
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
  whatIf.current = {play_id:id,description:play.description,shown:{...shown,...(existing?.changes||{})},base:shown,drive_result:existing?.drive_result||null};
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
  const saved = whatIf.edits;
  $("#whatif-stack").innerHTML = saved.length ? `<div class="whatif-stack"><span class="mini-label">IN THIS TIMELINE</span>${saved.map(e => {
    const play = whatIf.plays.find(p => p.play_id === e.play_id);
    return `<span class="whatif-chip"><button class="whatif-chip-open" data-whatif-open="${escapeHTML(e.play_id)}">Q${play?.qtr} ${escapeHTML(play?.time||"")} · ${escapeHTML(DRIVE_RESULTS[e.drive_result]||"")}</button><button class="whatif-chip-x" data-whatif-remove="${escapeHTML(e.play_id)}" aria-label="Remove this edit">×</button></span>`;
  }).join("")}</div>` : "";
}
function renderBlankResult(read) {
  const team = whatIfTeam(), p = forTeam(read.home_wp);
  $("#whatif-result").innerHTML = `<div class="whatif-cards"><div class="whatif-card"><span class="mini-label">${escapeHTML(team)} WIN CHANCE</span><strong>${pct(p)}</strong><p>${escapeHTML(stateText(read.state))}</p></div></div><p class="footnote">${escapeHTML(read.note)}</p>`;
}
function renderScenario() {
  const r = whatIf.result;if(!r)return;
  const team = whatIfTeam(), focus = r.edits.find(e => e.play_id === whatIf.current?.play_id) || r.edits[r.edits.length-1];
  const alt = forTeam(focus.alternate.home_wp), act = forTeam(focus.actual.home_wp), swing = swingFor(focus.wp_swing_pp), points = swingFor(focus.points_swing), next = swingFor(focus.aftermath_pp);
  const finalAlt = r.final.alternate, finalAct = r.final.actual;
  const outcome = !finalAlt ? "" : finalAlt.result==="tie_ot_not_modeled" ? "Tied; OT not modeled" : finalAlt.result==="home_win" ? `${whatIf.home} would finish ahead` : finalAlt.result==="away_win" ? `${whatIf.away} would finish ahead` : "Game not final";
  $("#whatif-result").innerHTML = `<div class="whatif-cards">
    <div class="whatif-card"><span class="mini-label">${escapeHTML(team)} WIN CHANCE AT THAT MOMENT</span><strong>${pct(act)} → ${pct(alt)}</strong><p class="${colorClass(swing)}">${signed(swing,1)} pp · ${signed(points)} points</p></div>
    <div class="whatif-card"><span class="mini-label">RECORDED NEXT</span><p>${escapeHTML(stateText(focus.actual.state))}</p><span class="mini-label">YOUR VERSION</span><p>${escapeHTML(stateText(focus.alternate.state))}</p></div>
    ${next==null?"":`<div class="whatif-card"><span class="mini-label">WHAT THE REAL GAME DID NEXT</span><strong class="${colorClass(next)}">${signed(next,1)} pp</strong><p>To the end of that drive, from the recorded state. Separate from the call.</p></div>`}
    ${finalAlt?`<div class="whatif-card"><span class="mini-label">FINAL · LATER PLAYS HELD AS PLAYED</span><strong>${escapeHTML(whatIf.away)} ${finalAlt.away}–${escapeHTML(whatIf.home)} ${finalAlt.home}</strong><p>Actual: ${escapeHTML(whatIf.away)} ${finalAct.away}–${escapeHTML(whatIf.home)} ${finalAct.home}. ${escapeHTML(outcome)}.</p></div>`:`<div class="whatif-card"><span class="mini-label">NEXT STEP</span><p>Choose how this drive ends to draw the alternate timeline.</p></div>`}
  </div><div id="whatif-chart" class="whatif-chart"></div><p class="footnote">${escapeHTML(r.note)} ${r.parity.max_abs_wp_error==null?"":r.parity.max_abs_wp_error<0.00001?`Without edits, the model matches the published win chance within 0.001 pp across ${r.parity.rows} recorded snaps.`:`Warning: without edits, the model differs from the published win chance by up to ${(r.parity.max_abs_wp_error*100).toFixed(2)} pp.`}</p>`;
  drawWhatIfChart();
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
  box.innerHTML = `${svg}</svg><p class="whatif-legend"><span class="key actual"></span> Actual <span class="key alternate"></span> Alternate, later plays held as played</p>`;
}
function renderSaveForm() {
  const box = $("#whatif-save");if(!box)return;
  if (!whatIf.result || whatIf.current?.blank) { box.innerHTML="";return; }
  const open = $("#whatif-save details")?.open;
  if ($("#whatif-save details")) return;
  box.innerHTML = `<details class="whatif-details" ${open?"open":""}><summary>Save this scenario</summary><div class="whatif-fields">
    <label class="wide">Label <input id="whatif-label" maxlength="120" placeholder="Smith-Njigba OPI"></label>
    <label>Type <select id="whatif-kind"><option value="flag">Flag</option><option value="no_call">No-call</option><option value="what_if">What-if</option></select></label>
    <label class="wide">Source link <input id="whatif-source" type="url" placeholder="https://…"></label>
    <label class="wide">Assumptions, one per line <textarea id="whatif-assumptions" rows="2"></textarea></label>
    <label class="check"><input id="whatif-disputed" type="checkbox"> Disputed call (needs a source)</label>
  </div><div class="whatif-actions"><button class="whatif-button" id="whatif-store">Save</button></div><p id="whatif-save-note" class="footnote"></p></details>`;
}
function currentEdits() {
  const c = whatIf.current, edits = whatIf.edits.filter(e => e.play_id !== c?.play_id);
  if (c && !c.blank) edits.push({play_id:c.play_id,changes:c.changes||{},drive_result:c.drive_result});
  return edits;
}
async function loadSaved() {
  try {
    const [saved,report] = await Promise.all([get(`/api/v1/scenarios?game_id=${encodeURIComponent(whatIf.game)}`),get(`/api/v1/scenarios/disputes?game_id=${encodeURIComponent(whatIf.game)}`)]);
    if (!$("#whatif-saved")) return;
    const signedFor = (v,digits) => signed(swingFor(v),digits);
    const margin = (m) => m==null?"—":m===0?"tied":`${m>0?whatIf.home:whatIf.away} by ${Math.abs(m).toFixed(1).replace(/\.0$/,"")}`;
    const disputed = report.calls.length ? `<section class="whatif-report"><h4>Disputed calls</h4><div class="table-wrap"><table><thead><tr><th>CALL</th><th>WHEN</th><th>${escapeHTML(whatIfTeam())} WIN CHANCE</th><th>POINTS</th><th>SOURCE</th></tr></thead><tbody>${report.calls.map(c => `<tr><td>${escapeHTML(c.label)}${c.stale?' <span class="muted">(older data)</span>':""}${c.stacked?' <span class="muted">(valued after earlier edits)</span>':""}</td><td>Q${c.qtr} ${escapeHTML(c.time||"")}</td><td>${signedFor(c.wp_swing_pp,1)} pp</td><td>${signedFor(c.points_swing)}</td><td>${c.source_url?`<a class="text-link" href="${escapeHTML(c.source_url)}" target="_blank" rel="noopener">link</a>`:""}</td></tr>`).join("")}</tbody></table></div><p><strong>If none were wrong: ${margin(report.actual_home_margin)}. If all were wrong: ${margin(report.home_margin_if_all_wrong)}.</strong></p><p class="footnote">${escapeHTML(report.note)}</p></section>` : "";
    $("#whatif-saved").innerHTML = disputed + (saved.scenarios.length ? `<section class="whatif-report"><h4>Saved scenarios</h4>${saved.scenarios.map(s => `<div class="whatif-saved-row"><button class="text-link" data-whatif-load="${escapeHTML(s.scenario_id)}">${escapeHTML(s.label)}</button><span class="muted">${escapeHTML(s.kind.replace("_","-"))}${s.disputed?" · disputed":""}</span><button class="whatif-chip-x" data-whatif-delete="${escapeHTML(s.scenario_id)}" aria-label="Delete ${escapeHTML(s.label)}">×</button></div>`).join("")}</section>` : "");
  } catch (e) {
    showWhatIfError(e.message);
  }
}
async function storeScenario() {
  const note = $("#whatif-save-note"), source = $("#whatif-source").value.trim();
  try {
    const body = {game_id:whatIf.game,edits:currentEdits(),label:$("#whatif-label").value.trim(),kind:$("#whatif-kind").value,disputed:$("#whatif-disputed").checked,source_url:source||null,assumptions:$("#whatif-assumptions").value.split("\n").map(s => s.trim()).filter(Boolean)};
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
    $("#whatif-play").value = "";renderForm();
    await scoreWhatIf();
  } catch (e) { showWhatIfError(e.message); }
}

document.addEventListener("change",e => {
  if (e.target.id === "whatif-play") { selectWhatIfPlay(e.target.value);return; }
  if (e.target.closest("#whatif-form")) { scoreWhatIf();return; }
  if (e.target.id === "history-team" && whatIf.game) {
    if (whatIf.current?.blank) scoreWhatIf(); else renderScenario();
    loadSaved();
  }
});
document.addEventListener("click",e => {
  const button = e.target.closest("button,[data-whatif-play]");if(!button)return;
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
