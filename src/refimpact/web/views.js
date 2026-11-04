"use strict";

function matchesTeam(e) { const team=$("#team").value;return !team || [e.home_team,e.away_team].includes(team); }
function displayChoice(e) {
  if(e.choice_edge_epa!=null)return `${signed(e.choice_edge_epa)} pts`;
  if(e.choice_edge_scenarios)return `${signed(e.choice_edge_scenarios.min)} to ${signed(e.choice_edge_scenarios.max)} (scenarios)`;
  return "unpriced";
}
function displayChoiceWP(e) {
  if(e.choice_edge_wp_pp!=null)return `${signed(e.choice_edge_wp_pp,1)} pp`;
  if(e.choice_edge_wp_scenarios)return `${signed(e.choice_edge_wp_scenarios.min,1)} to ${signed(e.choice_edge_wp_scenarios.max,1)} pp (scenarios)`;
  return "unpriced";
}
function nullifiedLabel(e) {
  const n=e.nullified_play;if(!n)return '';
  return n.status==='valued'?`Erased-result effect: ${signed(n.impact_epa)} pts · ${signed(n.wp_swing_pp,1)} pp`:
    n.status==='not_recorded'?'Erased action not recorded':'Written erased action; value unavailable';
}
function renderNullified(e) {
  const n=e.nullified_play;if(!n)return '';
  return `<section class="nullified-result"><h4>${n.status==='valued'?'Effect vs written nullified result':'Nullified result: '+(n.status==='not_recorded'?'not recorded':'unpriced')}</h4>
    ${n.status==='valued'?`<div class="impact-values"><span class="${colorClass(n.impact_epa)}">${escapeHTML(e.reference_team)}: ${signed(n.impact_epa)} EPA</span><span>${signed(n.wp_swing_pp,1)} pp win chance</span></div>`:''}
    <p>${escapeHTML(n.reason)}</p>${n.recorded?`<blockquote>${escapeHTML(n.source.text)}</blockquote>`:''}
    <p>${n.status==='valued'?'Conditional on the written action counting, with both alternatives valued at a common clock. Whistle timing is not recorded.':'The enforcement estimate above does not supply a value for the missing or unsupported action.'}</p>
    <p class="subline">Automatic text extraction · ${escapeHTML(n.source.dataset)} · Play ${escapeHTML(n.source.play_id)}. Separate from enforcement and drive EPA.</p></section>`;
}
function ruling(e) { return e.fouls.map(f=>f.type).join(" + "); }
function renderCalls() {
  const disposition=$("#disposition").value;
  const events=state.penalties.filter(e=>matchesTeam(e)&&(!disposition||e.fouls.some(f=>f.disposition===disposition)));
  const wpImpact=e=>Math.max(Math.abs(e.enforcement_wp_swing_pp||0),Math.abs(e.choice_edge_wp_pp||0));
  // Rank WP swings in percentage points; use EPA only to break ties, never mix units.
  events.sort((a,b)=>wpImpact(b)-wpImpact(a)||Math.abs(b.enforcement_epa||0)-Math.abs(a.enforcement_epa||0));
  $("#call-rows").innerHTML=events.length?events.slice(0,state.penaltyLimit).map(source=>{const e=penaltyView(source,$("#team").value||source.home_team);return `<tr><td>${escapeHTML(e.away_team)} @ ${escapeHTML(e.home_team)}<span class="subline">Q${e.qtr??"?"} ${escapeHTML(e.time||"")}</span></td><td>${escapeHTML(ruling(e))}<span class="subline"><span class="status-tag ${e.disposition==='declined'?'declined':''}">${escapeHTML(e.disposition)}</span>${e.runoff.applied_seconds?` · ${e.runoff.applied_seconds}s RUNOFF`:e.runoff.avoided?' · RUNOFF AVOIDED':''}</span></td><td>${escapeHTML(e.reference_team||"—")}</td><td class="number ${colorClass(e.enforcement_epa)}">${signed(e.enforcement_epa)} pts<span class="subline">${e.enforcement_epa==null?'missing alternative':e.enforcement_baseline==='fresh_down'?'vs fresh down without yardage':'vs no flag at common clock'}</span>${e.nullified_play?`<span class="subline">${escapeHTML(nullifiedLabel(e))}</span>`:''}</td><td class="number">${escapeHTML(displayChoice(e))}<span class="subline">Choice WP: ${escapeHTML(displayChoiceWP(e))}<br>Enforcement WP: ${signed(e.enforcement_wp_swing_pp,1)} pp</span></td><td><button class="row-open" data-penalty="${escapeHTML(e.id)}" aria-label="Inspect penalty options for ${escapeHTML(e.id)}">↗</button></td></tr>`;}).join(""):'<tr><td colspan="6" class="empty">No reported flags match this filter.</td></tr>';
  $("#more-penalties").classList.toggle("hidden",events.length<=state.penaltyLimit);
}

function renderProduction() {
  const team=$("#team").value;
  const rows=state.games.filter(matchesTeam).flatMap(g=>Object.values(g.production||{}).filter(p=>!team||p.team===team).map(p=>({g,p})));
  $("#production-rows").innerHTML=rows.length?rows.map(({g,p})=>`<tr><td><button class="inline-button" data-game="${escapeHTML(g.game_id)}">${escapeHTML(p.team)} <span class="subline">${escapeHTML(g.away_team)} @ ${escapeHTML(g.home_team)}</span></button></td><td class="number ${colorClass(p.offensive_epa)}">${signed(p.offensive_epa)}</td><td class="number ${colorClass(p.offensive_epa_per_snap)}">${signed(p.offensive_epa_per_snap,3)}<span class="subline">${p.snaps} snaps</span></td><td class="number ${colorClass(p.defensive_epa_allowed_per_snap==null?null:-p.defensive_epa_allowed_per_snap)}">${signed(p.defensive_epa_allowed_per_snap,3)}</td><td class="number ${colorClass(p.drive_epa)}">${signed(p.drive_epa)}<span class="subline">${p.drives} drives${p.missing_epa_rows?` · ${p.missing_epa_rows} missing EPA rows`:''}</span></td></tr>`).join(""):'<tr><td colspan="5" class="empty">Production will appear when play-by-play is available.</td></tr>';
}

function driveRows(drives) {
  return drives.map(d=>`<tr><td>${escapeHTML(d.game_id.split('_').slice(2).join(' @ '))}<span class="subline">Drive ${d.drive}</span></td><td>${escapeHTML(d.team)}</td><td>Q${d.qtr_start} ${escapeHTML(d.clock_start||"")}</td><td>${escapeHTML(d.result||"unreported")}</td><td class="number ${colorClass(d.drive_epa)}">${signed(d.drive_epa)}${d.missing_epa_rows?`<span class="subline">${d.missing_epa_rows} missing EPA rows</span>`:''}</td><td class="number">${signed(d.cumulative_team_drive_epa)}</td><td class="number">${chance(d.wp_before)} → ${chance(d.wp_after)}<span class="subline">${signed(d.wp_change_pp,1)} pp · ${escapeHTML(d.team)}</span></td><td class="number ${colorClass(d.known_penalty_enforcement_epa)}">${signed(d.known_penalty_enforcement_epa)}<span class="subline">${d.penalty_effects_valued} / ${d.penalty_events} events valued</span></td><td><button class="row-open" data-drive="${escapeHTML(d.id)}" aria-label="Open drive ${escapeHTML(d.id)}">↗</button></td></tr>`).join("");
}
function renderDrives() {
  const team=$("#team").value,filter=$("#drive-filter").value;
  const rows=state.drives.filter(d=>(!team||d.team===team)&&(filter==='all'||d.penalty_events>0));
  $("#drive-rows").innerHTML=rows.length?driveRows(rows.slice(0,state.driveLimit)):'<tr><td colspan="9" class="empty">No drives match this filter.</td></tr>';
  $("#more-drives").classList.toggle("hidden",rows.length<=state.driveLimit);
}

function branchTable(branches, timing=false) {
  return `<div class="table-wrap branch-table"><table><thead><tr><th>OUTCOME / STATE</th><th>EPA</th><th>WIN CHANCE / WPA</th><th>CLOCK / TIMEOUT</th></tr></thead><tbody>${branches.map(b=>`<tr><td>${escapeHTML(b.label)}<span class="subline">${escapeHTML(b.evidence)}${timing?` · ${b.next_snap_delay_seconds}s next-snap delay`:''}</span>${b.state?`<span class="subline">${escapeHTML(b.state.possession)} ball · ${b.state.down} & ${b.state.ydstogo} · ${b.state.yardline_100} yards to goal</span>`:''}</td><td class="number">${signed(b.epa)}<span class="subline">EP after ${signed(b.ep)}</span></td><td class="number">${chance(b.wp)}<span class="subline">WPA ${signed(b.wpa_pp,2)} pp</span></td><td>${b.state?`${b.state.half_seconds_remaining}s in half`:escapeHTML(b.phase)}<span class="subline">${escapeHTML(b.restart)} · ${b.runoff_seconds}s runoff${b.timeout_team?` · ${escapeHTML(b.timeout_team)} timeout`:''}</span></td></tr>`).join("")}</tbody></table></div>`;
}

function penaltyMath(e) {
  const d=e.decision;if(!d)return '';
  const a=d.branches.find(b=>b.id===d.actual_branch),n=d.branches.find(b=>b.id==='no_flag');
  if(!a||!n)return '';
  return `<section class="actual-progress"><h4>Ruling comparison · ${escapeHTML(e.reference_team)}</h4><p>Model win chance before: <strong>${chance(d.wp_before)}</strong>. With ruling: <strong>${chance(a.wp)}</strong>. Without enforcement: <strong>${chance(n.wp)}</strong>.</p><p>With-ruling WPA ${signed(a.wpa_pp,2)} pp − alternative WPA ${signed(n.wpa_pp,2)} pp = <strong>penalty swing ${signed(e.enforcement_wp_swing_pp,2)} pp</strong>.</p><p>With-ruling EPA ${signed(a.epa,3)} − alternative EPA ${signed(n.epa,3)} = <strong>penalty effect ${signed(e.enforcement_epa,3)} expected points</strong>.</p><p class="footnote">Both branches use the same reference team, starting state and declared evaluation horizon. These are conditional model comparisons; observed play WPA above also reflects the recorded action and clock. Percentage points measure an absolute change: 40% → 45% is +5 pp.</p></section>`;
}

function timingComparisons(d) {
  const rows=(d.timing_comparisons||[]).filter(b=>b.alternative_branch==='no_flag');
  if(!rows.length)return '';
  return `<h4>Matched clock scenarios: ruling versus no enforcement</h4><div class="table-wrap"><table><thead><tr><th>DELAY / RESTART</th><th>WITH RULING</th><th>ALTERNATIVE</th><th>PENALTY SWING</th></tr></thead><tbody>${rows.map(b=>`<tr><td>${b.next_snap_delay_seconds}s opportunity<span class="subline">${escapeHTML(b.actual_restart)} vs ${escapeHTML(b.alternative_restart)}${b.conditional_clock_assumption?' · conditional restart':''}</span></td><td>${chance(b.actual_wp)}<span class="subline">WPA ${signed(b.actual_wpa_pp,2)} pp</span></td><td>${chance(b.alternative_wp)}<span class="subline">WPA ${signed(b.alternative_wpa_pp,2)} pp</span></td><td>${signed(b.actual_minus_branch_wp_pp,2)} pp<span class="subline">${signed(b.actual_minus_branch_epa,3)} EPA · extra clock effect ${signed(b.additional_clock_wp_pp,2)} pp</span></td></tr>`).join('')}</tbody></table></div>`;
}

function renderPenalty(e,viewTeam=null) {
  if(typeof viewTeam==="string")e=penaltyView(e,viewTeam);
  const decision=e.decision;
  return `<article class="detail-call"><h3><span>${escapeHTML(ruling(e))}</span><span class="status-tag ${e.disposition==='declined'?'declined':''}">${escapeHTML(e.disposition)}</span></h3><p>${escapeHTML(e.away_team)} @ ${escapeHTML(e.home_team)} · Q${e.qtr??"?"} ${escapeHTML(e.time||"")} · Play ${escapeHTML(e.play_id)}</p><p>${escapeHTML(e.description)}</p><div class="impact-values"><span class="${colorClass(e.enforcement_epa)}">${escapeHTML(e.reference_team||"—")} enforcement EPA: ${signed(e.enforcement_epa)}</span><span>Choice edge: ${escapeHTML(displayChoice(e))} · ${escapeHTML(displayChoiceWP(e))}</span></div><p>Observed play EPA · ${escapeHTML(e.reference_team||"offense")}: ${signed(e.observed_play_epa)}. This already includes actual enforcement and is not added to the penalty estimate.</p>${e.runoff.mentioned?`<p class="clock-note">Reported 10-second runoff: ${e.runoff.avoided?'avoided or declined':`${e.runoff.applied_seconds}s applied`}. Runoff is deducted once. Half/regulation expiration is handled explicitly.</p>`:''}
  ${penaltyProgress(e)}
  ${penaltyMath(e)}
  ${renderNullified(e)}
  ${decision?`<p>Actual branch: <strong>${escapeHTML(decision.actual_branch)}</strong>. EPA and WPA are relative to the common starting state, from ${escapeHTML(e.reference_team)}'s perspective.</p>${branchTable(decision.branches)}<details><summary>Clock-stop / next-snap sensitivity (${decision.timing_scenarios.length} cases)</summary><p>${escapeHTML(decision.timing_note)}</p>${timingComparisons(decision)}${decision.timing_scenarios.length?branchTable(decision.timing_scenarios,true):'<p>No extra delay scenarios were requested for this play.</p>'}</details><details><summary>Branch assumptions</summary>${decision.branches.map(b=>`<p><strong>${escapeHTML(b.label)}</strong></p><ul>${b.assumptions.map(a=>`<li>${escapeHTML(a)}</li>`).join('')}</ul>`).join('')}</details>`:'<p>Numerical alternatives are incomplete. The source event is retained rather than treated as zero penalty value.</p>'}
  ${e.issues.length?`<details><summary>Evidence limits (${e.issues.length})</summary><ul>${e.issues.map(i=>`<li>${escapeHTML(i)}</li>`).join('')}</ul></details>`:''}${e.drive_id?`<button class="button ghost more-button" data-drive="${escapeHTML(e.drive_id)}">Inspect the drive ↗</button>`:''}</article>`;
}

function startDetail() {
  const request=++state.detailRequest,dialog=$("#game-dialog");
  $("#game-detail").innerHTML='<p class="muted">Loading evidence…</p>';
  if(!dialog.open)dialog.showModal();
  return request;
}

async function openPenalty(id) {
  const request=startDetail();
  try {
    const {penalty}=await get(`/api/v1/penalties/${encodeURIComponent(id)}`);
    if(request!==state.detailRequest||!$("#game-dialog").open)return;
    $("#game-detail").innerHTML=`<h2 id="dialog-title">The penalty decision</h2>${renderPenalty(penalty,$("#team").value||penalty.home_team)}`;
  } catch(e) {if(request===state.detailRequest)$("#game-detail").textContent=e.message;}
}

async function openGame(gameId) {
  const request=startDetail();
  try{
    const data=await get(`/api/v1/games/${encodeURIComponent(gameId)}`);
    if(request!==state.detailRequest||!$("#game-dialog").open)return;
    state.detailGame=data;
    renderGameExplorer(data);
  }catch(e){if(request===state.detailRequest)$("#game-detail").textContent=e.message;}
}

async function openDrive(id) {
  const request=startDetail();
  try{
    const data=await get(`/api/v1/drives/${encodeURIComponent(id)}`);
    if(request!==state.detailRequest||!$("#game-dialog").open)return;
    const d=data.drive;
    $("#game-detail").innerHTML=`<h2 id="dialog-title">${escapeHTML(d.team)} · Drive ${d.drive}</h2><p class="muted">${escapeHTML(d.game_id)} · ${escapeHTML(d.result||'Result unreported')}</p><button class="button ghost" data-game="${escapeHTML(d.game_id)}">← Back to game</button><div class="detail-summary"><span>Observed drive EPA: <strong>${signed(d.drive_epa)}</strong><br>Cumulative drive EPA · ${escapeHTML(d.team)}: ${signed(d.cumulative_team_drive_epa)}</span><span>${escapeHTML(d.team)} win chance: ${chance(d.wp_before)} → ${chance(d.wp_after)}<br>Change over drive: ${signed(d.wp_change_pp,1)} pp<br>Separate penalty benefit: ${signed(d.known_penalty_enforcement_epa)}</span></div><p class="footnote">${escapeHTML(d.accounting_note)}${d.missing_epa_rows?` ${d.missing_epa_rows} action rows have no native EPA; the sum is partial.`:''}</p><h3>Play-by-play EPA</h3><div class="timeline">${d.timeline.map(p=>`<article class="timeline-play ${p.flagged?'flagged':''}"><div class="timeline-values"><span>Q${p.qtr} ${escapeHTML(p.time||'')}</span><span class="${colorClass(p.epa)}">EPA ${signed(p.epa)}</span><span>Drive cumulative EPA ${signed(p.cumulative_epa)}</span><span>${escapeHTML(d.team)} win chance ${chance(p.wp_before)} → ${chance(p.wp_after)} (${signed(p.wp_change_pp,1)} pp)</span></div><p>${escapeHTML(p.description)}</p></article>`).join('')}</div><h3>Penalty alternatives on this drive</h3>${data.penalties.map(e=>renderPenalty(e,d.team)).join('')||'<p>No reported flags on this drive.</p>'}`;
  }catch(e){if(request===state.detailRequest)$("#game-detail").textContent=e.message;}
}
