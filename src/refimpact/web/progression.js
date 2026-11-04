"use strict";
const chance = n => n == null ? '—' : `${(n*100).toFixed(0)}%`;
function oriented(n,team,home) {return n==null?null:n*(team===home?1:-1);}
function teamChance(n,team,home) {return n==null?null:team===home?n:1-n;}
function penaltyView(source,team) {
  if(![source.home_team,source.away_team].includes(team)||team===source.reference_team)return source;
  const flip=n=>n==null?null:-n,range=r=>r?{...r,min:-r.max,max:-r.min}:r;
  const branch=b=>({...b,epa:flip(b.epa),ep:flip(b.ep),wp:b.wp==null?null:1-b.wp,wp_vegas:b.wp_vegas==null?null:1-b.wp_vegas,actual_minus_branch_wp_vegas_pp:flip(b.actual_minus_branch_wp_vegas_pp),
    wp_before:b.wp_before==null?null:1-b.wp_before,wpa_pp:flip(b.wpa_pp),
    clock_epa_component:flip(b.clock_epa_component),clock_wp_component_pp:flip(b.clock_wp_component_pp),
    actual_minus_branch_epa:flip(b.actual_minus_branch_epa),actual_minus_branch_wp_pp:flip(b.actual_minus_branch_wp_pp)});
  const timing=b=>({...b,actual_minus_branch_wp_vegas_pp:flip(b.actual_minus_branch_wp_vegas_pp),actual_wp:b.actual_wp==null?null:1-b.actual_wp,alternative_wp:b.alternative_wp==null?null:1-b.alternative_wp,
    actual_wpa_pp:flip(b.actual_wpa_pp),alternative_wpa_pp:flip(b.alternative_wpa_pp),
    actual_minus_branch_epa:flip(b.actual_minus_branch_epa),actual_minus_branch_wp_pp:flip(b.actual_minus_branch_wp_pp),
    additional_clock_epa:flip(b.additional_clock_epa),additional_clock_wp_pp:flip(b.additional_clock_wp_pp)});
  return {...source,reference_team:team,enforcement_epa:flip(source.enforcement_epa),
    enforcement_wp_swing_pp:flip(source.enforcement_wp_swing_pp),enforcement_wp_vegas_pp:flip(source.enforcement_wp_vegas_pp),enforcement_wp_common_clock_pp:flip(source.enforcement_wp_common_clock_pp),enforcement_wp_vegas_common_clock_pp:flip(source.enforcement_wp_vegas_common_clock_pp),enforcement_wp_range:range(source.enforcement_wp_range),observed_play_epa:flip(source.observed_play_epa),
    choice_edge_epa:flip(source.choice_edge_epa),choice_edge_wp_pp:flip(source.choice_edge_wp_pp),
    choice_edge_scenarios:range(source.choice_edge_scenarios),choice_edge_wp_scenarios:range(source.choice_edge_wp_scenarios),
    nullified_play:source.nullified_play?{...source.nullified_play,reference_team:team,impact_epa:flip(source.nullified_play.impact_epa),wp_swing_pp:flip(source.nullified_play.wp_swing_pp)}:null,
    decision:source.decision?{...source.decision,reference_team:team,wp_before:source.decision.wp_before==null?null:1-source.decision.wp_before,branches:source.decision.branches.map(branch),timing_scenarios:source.decision.timing_scenarios.map(branch),timing_comparisons:(source.decision.timing_comparisons||[]).map(timing)}:null};
}
function penaltyProgress(e) {
  const p=e.progression;if(!p)return '';
  const team=e.reference_team||e.home_team,home=e.home_team;
  const before=teamChance(p.home_wp_before,team,home),after=teamChance(p.home_wp_after,team,home);
  return `<section class="actual-progress"><h4>Observed play / flag · ${escapeHTML(team)}</h4><p>Win chance: <strong>${chance(before)} → ${chance(after)}</strong> · change ${wholePP(oriented(p.home_wp_change_pp,team,home))}</p><p>Cumulative observed offense EPA · ${escapeHTML(team)}: ${signed(p.cumulative_offense_epa?.[team])} <span class="muted">(already includes penalties)</span></p><p>Expected margin shift so far, ignores score · ${escapeHTML(team)}: ${signed(oriented(p.cumulative_home_penalty_benefit_epa,team,home))} <span class="muted">(${p.penalty_effects_known} priced, ${p.penalty_effects_missing} unpriced; both possessions)</span></p>${heatLine(p.heat)}<p class="footnote">The observed change includes the actual action and enforcement. The ruling's swing versus an alternative is shown separately below.</p></section>`;
}
function heatLine(h) {
  if(!h||!Object.values(h.snaps_last_5).some(Boolean))return '';
  const teams=Object.keys(h.epa_last_5),run=h.scoring_run;
  return `<p class="muted">Game so far · last 5 snaps EPA: ${teams.map(t=>`${escapeHTML(t)} ${signed(h.epa_last_5[t],1)}`).join(', ')}${run?.team?` · scoring run ${escapeHTML(run.team)} ${run.points}`:''}. Not a forecast.</p>`;
}
function ledgerSummary(g) {
  // A stored run from before these fields has priced flags but no summary of them.
  if(g.top_swings===undefined)return `<section class="actual-progress"><h3>Biggest flags at the moment</h3><p class="empty">This stored run has no per-flag summary. Refresh the data to see the swings here; the flags below are still priced.</p></section>`;
  const rows=g.top_swings;
  const table=rows.length?`<div class="table-wrap compact-table"><table><thead><tr><th>FLAG</th><th>WHEN</th><th>WIN CHANCE · ${escapeHTML(g.home_team)}</th><th class="hide-phone">VEGAS MODEL</th><th></th></tr></thead><tbody>${rows.map(s=>`<tr><td>${escapeHTML(s.ruling)}${s.approximate?' <span class="status-tag">approx.</span>':''}${s.basis==='restart_aware'?'<span class="subline">Assumes a ${s.delay_seconds??20} s wait where the clock would run</span>':''}</td><td>Q${s.qtr??'?'} ${escapeHTML(s.time||'')}</td><td class="number ${colorClass(s.home_wp_swing_pp)}">${swingPP(s.home_wp_swing_pp)}</td><td class="number hide-phone">${wholePP(s.home_wp_vegas_swing_pp)}</td><td><button class="row-open" data-penalty="${escapeHTML(s.id)}" aria-label="Inspect ${escapeHTML(s.ruling)}">↗</button></td></tr>`).join('')}</tbody></table></div>`:'<p class="empty">No priced flags yet.</p>';
  return `<section class="actual-progress"><h3>Biggest flags at the moment</h3>${table}<p>Largest swing: <strong>${g.largest_call_swing_pp==null?'—':Math.round(g.largest_call_swing_pp)+' pp'}</strong> · Swings of 5+ pp: <strong>${g.swings_over_5pp??'—'}</strong> · Unpriced flags: <strong>${g.unpriced_penalties??'—'}</strong> · Declined: ${g.declined_penalties??'—'}</p><p>Expected margin shift, ignores score · ${escapeHTML(g.home_team)}: <strong class="${colorClass(g.net_home_epa)}">${signed(g.net_home_epa)}</strong></p><p class="footnote">Each swing is one flag's effect on win chance when it happened, against keeping the described play. Swings are not added together. The margin shift adds expected points; those have no score input, so a flag in a blowout counts in full. Neither says whether a call was right.</p></section>`;
}
function heatPanel(data) {
  const timeline=data.progression?.timeline||[],last=[...timeline].reverse().find(p=>p.heat);
  if(!last)return '';
  const h=last.heat,g=data.game,run=h.scoring_run;
  return `<section class="actual-progress"><h3>How the game went</h3><div class="table-wrap compact-table"><table><thead><tr><th>TEAM</th><th>EPA, LAST 5 SNAPS</th><th>BIG PLAYS, LAST 10 SNAPS</th></tr></thead><tbody>${[g.home_team,g.away_team].map(t=>`<tr><td>${escapeHTML(t)}</td><td class="number ${colorClass(h.epa_last_5[t])}">${signed(h.epa_last_5[t],1)}<span class="subline">${h.snaps_last_5[t]} snaps</span></td><td class="number">${h.big_plays_last_10[t]}</td></tr>`).join('')}</tbody></table></div><p>Scoring run at the end: ${run?.team?`${escapeHTML(run.team)} ${run.points} unanswered`:'none'}. Pick “Last-5 EPA” in the chart to see it through the game.</p><p class="footnote">${escapeHTML(data.progression.heat_note||'')}</p></section>`;
}
function opportunityPanel(o) {
  if(!o)return '';
  const labels={before:'Before the snap',no_flag:'Without the flag',actual:'With the ruling',recorded:'Recorded next',yours:'Your version'};
  const place=s=>s.possession===o.team?`${s.down}&${s.ydstogo>=s.yardline_100?'G':s.ydstogo} at ${s.yardline_100} to goal`:`${escapeHTML(s.possession)} ball`;
  const rows=Object.entries(o.states).map(([k,s])=>`<tr><td>${labels[k]||escapeHTML(k)}<span class="subline">${place(s)}${s.points_scored?` · ${s.points_scored>0?'+':''}${s.points_scored} scored`:''}</span></td><td class="number">${s.downs_available}</td><td class="number">${chance(s.td)}</td><td class="number">${chance(s.fg)}</td><td class="number hide-phone">${chance(s.no_score)}</td><td class="number hide-phone">${chance(s.other)}</td></tr>`).join('');
  const gained=o.downs_gained>0?`<p><strong>+${o.downs_gained} down${o.downs_gained===1?'':'s'} in hand.</strong> Next-score touchdown chance ${wholePP(o.td_change_pp)}, field goal ${wholePP(o.fg_change_pp)}. Extra downs flip field goals into touchdowns.</p>`:'';
  return `<section class="actual-progress"><h4>Opportunity · ${escapeHTML(o.team)}</h4>${gained}<div class="table-wrap compact-table"><table><thead><tr><th>STATE</th><th>DOWNS IN HAND</th><th>NEXT SCORE TD</th><th>NEXT SCORE FG</th><th class="hide-phone">NO SCORE</th><th class="hide-phone">OTHER</th></tr></thead><tbody>${rows}</tbody></table></div><p class="footnote">${escapeHTML(o.note)} Other is an opponent score or a safety. Downs in hand count the chances available, not plays run, because a drive stops counting plays when it scores.</p></section>`;
}
function teamEvidenceCard(data,team) {
  const g=data.game,p=g.production?.[team]||{},benefit=g.penalty_benefit||{};
  const home=team===g.home_team;
  const off=home?benefit.home_on_offense_epa:oriented(benefit.home_on_defense_epa,team,g.home_team);
  const def=home?benefit.home_on_defense_epa:oriented(benefit.home_on_offense_epa,team,g.home_team);
  const initial=teamChance(g.win_probability?.initial_home_wp,team,g.home_team);
  const latest=teamChance(g.win_probability?.latest_home_wp,team,g.home_team);
  const tie=g.status==='final'&&g.home_score===g.away_score;
  return `<article class="evidence-team"><h3>${escapeHTML(team)}</h3><div class="evidence-metric"><span>Observed offense EPA</span><strong class="${colorClass(p.offensive_epa)}">${signed(p.offensive_epa)}</strong><small>Scrimmage only · includes penalties · ${p.snaps??'—'} snaps</small></div><div class="evidence-metric separate"><span>Expected margin shift, ignores score</span><strong class="${colorClass(oriented(benefit.home_net_epa,team,g.home_team))}">${signed(oriented(benefit.home_net_epa,team,g.home_team))}</strong><small>Across both possessions · ${benefit.known_events??0} priced · ${benefit.missing_events??0} unpriced</small><p>On offense ${signed(off)} · On defense ${signed(def)}</p></div><p>Defense EPA allowed: ${signed(p.defensive_epa_allowed)} <span class="muted">(lower is better)</span><br>Cumulative observed drive EPA: ${signed(p.drive_epa)} <span class="muted">(includes punt/FG endings)</span></p><p class="win-progression">Win chance: <strong>${chance(initial)} → ${tie?'Final tie':chance(latest)}</strong><br><span class="muted">First recorded → ${g.status==='final'?'final result':'latest state'} · ${wholePP(oriented(g.win_probability?.home_wp_change_from_start_pp,team,g.home_team))}</span></p></article>`;
}
function renderGameExplorer(data) {
  const g=data.game;
  $('#game-detail').innerHTML=`<h2 id="dialog-title">${escapeHTML(g.away_team)} ${g.away_score??'—'} <span class="muted">at</span> ${escapeHTML(g.home_team)} ${g.home_score??'—'}</h2><p class="muted">${escapeHTML(g.gameday)} · ${g.season}, Week ${g.week} · ${escapeHTML(g.status)}</p><div class="evidence-teams">${[g.home_team,g.away_team].map(t=>teamEvidenceCard(data,t)).join('')}</div><p class="accounting-explainer">Observed offense EPA already includes penalties. The expected margin shift compares rulings with alternatives; it is separate, and cannot be added to or subtracted from production to replay the game.</p>${ledgerSummary(g)}${heatPanel(data)}
  <h3>Cumulative EPA & win-chance history</h3><div class="history-controls"><label>Perspective <select id="history-team" aria-label="History perspective">${[g.home_team,g.away_team].map(t=>`<option value="${escapeHTML(t)}">${escapeHTML(t)}</option>`).join('')}</select></label><label>Chart <select id="history-metric" aria-label="History metric"><option value="wp">Win chance</option><option value="offense">Cumulative observed offense EPA</option><option value="penalty">Cumulative expected margin shift</option><option value="drive">Cumulative observed drive EPA</option><option value="heat">Last-5 EPA (game so far)</option></select></label></div><div id="history-chart" class="history-chart"></div><p id="history-caption" class="footnote"></p><div id="history-focus"></div><div class="history-controls"><label>Show <select id="history-kind" aria-label="History events"><option value="all">All plays / events</option><option value="penalties">Penalties only</option><option value="drives">Drives</option></select></label><span class="muted">Select a chart point or event to inspect it.</span></div><div id="history-list"></div><button id="history-more" class="button ghost more-button hidden">Show more events</button>`;
  $('#game-detail').insertAdjacentHTML('beforeend','<section id="whatif" class="whatif"></section>');
  if(data.progression)initWhatIf(data);
  if(!data.progression){$('#history-chart').innerHTML='<p class="empty">Game history will appear when play-by-play is available.</p>';return;}
  state.historyLimit=35;
  drawHistory();renderHistoryList();
}
function historyState() {
  return {data:state.detailGame,team:$('#history-team').value,metric:$('#history-metric').value,kind:$('#history-kind').value};
}
function historyValues(data,team,metric) {
  const home=data.game.home_team, rows=data.progression.timeline;
  let driveTotal=null;
  const ends=new Map(data.progression.drives.filter(d=>d.team===team).map(d=>[d.end_play_id,d]));
  return rows.map(p=>{
    if(ends.has(p.play_id))driveTotal=ends.get(p.play_id).cumulative_team_drive_epa;
    const value=metric==='wp'?teamChance(p.home_wp_after??p.home_wp_before,team,home):metric==='offense'?p.cumulative_offense_epa[team]:metric==='penalty'?oriented(p.cumulative_home_penalty_benefit_epa,team,home):metric==='heat'?(p.heat?.epa_last_5?.[team]??null):driveTotal;
    return {p,value};
  });
}
function drawHistory() {
  const {data,team,metric}=historyState(),values=historyValues(data,team,metric);
  const valid=values.filter(v=>v.value!=null);
  if(!valid.length){$('#history-chart').innerHTML='<p class="empty">This metric has no recorded values.</p>';$('#history-caption').textContent='Missing values are not assumed to be zero.';return;}
  const W=Math.max(280,Math.min(740,$("#history-chart").clientWidth-18||740)),H=Math.max(230,Math.min(280,W*.38)),L=42,R=15,T=22,B=37;
  const lower=metric==='wp'?0:Math.min(0,...valid.map(v=>v.value)),upper=metric==='wp'?1:Math.max(0,...valid.map(v=>v.value));
  const pad=metric==='wp'?0:Math.max(1,(upper-lower)*.08),lo=lower-pad,hi=upper+pad;
  const x=i=>L+i/Math.max(1,values.length-1)*(W-L-R),y=v=>T+(hi-v)/(hi-lo)*(H-T-B);
  let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHTML(team)} ${escapeHTML(metric)} through the game">`;
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;svg+=`<line x1="${L}" y1="${y(v)}" x2="${W-R}" y2="${y(v)}" stroke="#334036"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end" fill="#a2b1a5" font-size="10">${metric==='wp'?Math.round(v*100)+'%':v.toFixed(1)}</text>`;}
  const quarters=new Set();let path='',previous=null;
  values.forEach(({p,value},i)=>{if(!quarters.has(p.qtr)){quarters.add(p.qtr);svg+=`<text x="${x(i)}" y="${H-12}" fill="#a2b1a5" font-size="10">Q${p.qtr}</text>`;}
    if(value==null){previous=null;return;}
    path+=previous==null?`M${x(i)},${y(value)}`:`H${x(i)}V${y(value)}`;previous=value;
  });
  svg+=`<path d="${path}" fill="none" stroke="#d1f482" stroke-width="2"/>`;
  values.forEach(({p,value},i)=>{if(value==null)return;const name=`Q${p.qtr} ${p.time||''} · ${metric==='wp'?chance(value):signed(value)} · ${p.description||''}`;svg+=`<circle cx="${x(i)}" cy="${y(value)}" r="${p.penalty_id?4:2}" fill="${p.penalty_id?'#c6b2f6':'#d1f482'}" class="history-point" data-history-play="${escapeHTML(p.play_id)}" tabindex="0" role="button" aria-label="${escapeHTML(name)}"><title>${escapeHTML(name)}</title></circle>`;});
  svg+='</svg>';$('#history-chart').innerHTML=svg;
  $('#history-caption').textContent=metric==='wp'?`${team} model win chance at recorded states. Purple points mark flags. Changes are percentage points; ruling comparisons are never added into a hypothetical win chance.`:metric==='penalty'?`${team}'s cumulative expected margin shift from priced flags, across both possessions. It ignores score, leaves out unpriced flags, and is separate from observed production.`:metric==='heat'?`${team}'s native EPA over its last 5 scrimmage snaps at each point. Describes the game so far; not a forecast, and it feeds no other value.`:metric==='offense'?`${team}'s cumulative observed offensive EPA, including actual penalties. Only this team's scrimmage production is accumulated; missing EPA rows make the total partial.`:`${team}'s observed drive EPA accumulated at each drive ending, including punt/field-goal endings. Purple points mark flags.`;
}
function historyPlay(p,team,home) {
  const pre=teamChance(p.home_wp_before,team,home),post=teamChance(p.home_wp_after,team,home);
  const playEPA=p.epa_reference_team==null?null:p.epa_reference_team===team?p.epa:p.epa==null?null:-p.epa;
  return `<button class="history-event ${p.penalty_id?'flagged':''}" data-history-play="${escapeHTML(p.play_id)}"><span class="history-event-head"><strong>Q${p.qtr} ${escapeHTML(p.time||'')} · ${escapeHTML(p.possession||'Administration')}${p.penalty_id?' · FLAG':''}</strong><span>${escapeHTML(team)} win chance ${chance(pre)} → ${chance(post)} · ${wholePP(oriented(p.home_wp_change_pp,team,home))}</span></span><span class="history-description">${escapeHTML(p.description||'Event')}</span><span class="history-event-values">Play EPA · ${escapeHTML(team)}: ${signed(playEPA)}<span>Offense EPA so far · ${escapeHTML(team)}: ${signed(p.cumulative_offense_epa[team])}</span><span>Expected margin shift so far · ${escapeHTML(team)}: ${signed(oriented(p.cumulative_home_penalty_benefit_epa,team,home))}</span></span></button>`;
}
function renderHistoryList() {
  const {data,team,kind}=historyState(),home=data.game.home_team;
  if(kind==='drives'){
    const rows=(data.drives||[]).filter(d=>d.team===team);
    $('#history-list').innerHTML=`<div class="table-wrap"><table><thead><tr><th>DRIVE</th><th>OBSERVED EPA</th><th>CUMULATIVE EPA</th><th>${escapeHTML(team)} WIN CHANCE</th><th>CHANGE</th><th></th></tr></thead><tbody>${rows.map(d=>`<tr><td>${d.drive} · ${escapeHTML(d.result||'unreported')}</td><td class="number">${signed(d.drive_epa)}</td><td class="number">${signed(d.cumulative_team_drive_epa)}</td><td>${chance(d.wp_before)} → ${chance(d.wp_after)}</td><td class="number">${wholePP(d.wp_change_pp)}</td><td><button class="row-open" data-drive="${escapeHTML(d.id)}" aria-label="Open drive ${escapeHTML(d.id)}">↗</button></td></tr>`).join('')}</tbody></table></div>`;
    $('#history-more').classList.add('hidden');return;
  }
  const rows=data.progression.timeline.filter(p=>kind!=='penalties'||p.penalty_id);
  $('#history-list').innerHTML=rows.length?`<p class="footnote">Play EPA is from ${escapeHTML(team)}'s perspective on either side of the ball. Cumulative offense EPA counts only ${escapeHTML(team)}'s offense; the expected margin shift is a separate ledger.</p>`+rows.slice(0,state.historyLimit).map(p=>historyPlay(p,team,home)).join(''):'<p class="empty">No matching events.</p>';
  $('#history-more').classList.toggle('hidden',rows.length<=state.historyLimit);
}
function focusHistoryPlay(id) {
  const {data,team}=historyState(),p=data.progression.timeline.find(p=>p.play_id===id);if(!p)return;
  const event=data.penalties.find(e=>e.id===p.penalty_id);
  $('#history-focus').innerHTML=`<section class="history-focus"><h3>Selected event · Q${p.qtr} ${escapeHTML(p.time||'')}</h3>${historyPlay(p,team,data.game.home_team)}<p class="footnote">${escapeHTML(team)} win chance changed ${wholePP(oriented(p.home_wp_change_from_start_pp,team,data.game.home_team))} from the first recorded state through this event. ${p.missing_offense_epa_rows[team]} offensive rows lack EPA; ${p.penalty_effects_missing} flags so far are unpriced.</p>${heatLine(p.heat)}${event?renderPenalty(event,team):''}</section>`;
  if(whatIf.plays.some(play=>play.play_id===p.play_id))$('#history-focus .history-focus').insertAdjacentHTML('beforeend',`<button class="whatif-button" data-whatif-play="${escapeHTML(p.play_id)}">Edit this play in What if?</button>`);
  $('#history-focus').scrollIntoView({behavior:'smooth',block:'nearest'});
}
document.addEventListener('change',e=>{
  if(!['history-team','history-metric','history-kind'].includes(e.target.id))return;
  if(e.target.id!=='history-metric'){state.historyLimit=35;renderHistoryList();$('#history-focus').innerHTML='';}
  drawHistory();
});
document.addEventListener('click',e=>{const point=e.target.closest('[data-history-play]');if(point){focusHistoryPlay(point.dataset.historyPlay);return;}if(e.target.id==='history-more'){state.historyLimit+=35;renderHistoryList();}});
document.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target.matches('circle[data-history-play]')){e.preventDefault();focusHistoryPlay(e.target.dataset.historyPlay);}});

// Resize can fire before app.js has initialized its helpers during page load.
window.addEventListener('resize',()=>{if(document.querySelector('#history-chart')&&state.detailGame?.progression)drawHistory();});
