"use strict";
const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const signed = (n, digits = 2) => n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(digits)}`;
// Win chance is shown in whole points: the trees step, so finer digits are not known.
const percent = (n) => n == null ? "unpriced" : `${(n * 100).toFixed(0)}%`;
const wholePP = (n) => { if (n == null) return "—"; const r = Math.round(n); return r === 0 ? "0 pp" : `${r > 0 ? "+" : ""}${r} pp`; };
const NOISE_PP = 2;
const swingPP = (n) => n == null ? "—" : Math.abs(n) < NOISE_PP ? `${wholePP(n)} (within model noise)` : wholePP(n);
const colorClass = (n) => n == null || Math.abs(n) < 0.000001 ? "neutral" : n > 0 ? "positive" : "negative";
const shortDate = (s) => new Date(`${s}T12:00:00`).toLocaleDateString("en-US", {month:"short",day:"numeric"});
const colors = {ARI:"#7d2736",ATL:"#842b36",BAL:"#4a367d",BUF:"#284f9a",CAR:"#176b8c",CHI:"#a34b22",CIN:"#a54824",CLE:"#7b4734",DAL:"#334c6e",DEN:"#a44e20",DET:"#276f9a",GB:"#436149",HOU:"#5e2c3f",IND:"#34569b",JAX:"#26706b",KC:"#9c303b",LA:"#365fa1",LAC:"#246c9f",LV:"#4a5350",MIA:"#29847f",MIN:"#694b91",NE:"#314e7e",NO:"#8b7d4e",NYG:"#345a94",NYJ:"#2e6652",PHI:"#306862",PIT:"#8b7833",SEA:"#3e6581",SF:"#983f44",TB:"#9e3d36",TEN:"#4b708b",WAS:"#723d47"};
const state = {season:null,week:null,weeks:[],games:[],calls:[],scope:"week",request:0,plotRows:[],analysisRequest:0,detailRequest:0,penalties:[],drives:[],metric:"penalty_epa",penaltyLimit:25,driveLimit:20,metricLabel:"Expected margin shift for home team, ignores score"};

async function get(path) {
  const response = await fetch(path, {cache:"no-cache"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `API returned ${response.status}`);
  return data;
}
function error(message) {
  $("#error").textContent = message;
  $("#error").classList.toggle("hidden", !message);
}
function query() { return `season=${state.season}&week=${state.week}`; }
function clock(c) { return stateClock(c.comparison.before); }
function mark(team) { return `<span class="team-mark" style="--team:${colors[team] || "#34493d"}">${escapeHTML(team)}</span>`; }

async function init() {
  try {
    const [status,weeks] = await Promise.all([get("/api/v1/status"),get("/api/v1/weeks")]);
    if (status.status !== "ready") throw new Error(status.instruction);
    state.weeks = weeks.weeks;
    state.season = weeks.default_season;
    state.week = weeks.default_week;
    const params = new URLSearchParams(location.search);
    const requested = state.weeks.find(w => w.season === Number(params.get("season")) && w.week === Number(params.get("week")));
    if (requested) { state.season=requested.season;state.week=requested.week; }
    $("#week").innerHTML = state.weeks.map(w => `<option value="${w.season}:${w.week}" ${w.season===state.season && w.week===state.week ? "selected" : ""}>${w.season} · Week ${String(w.week).padStart(2,"0")}${w.final_games===0 ? " · upcoming" : ""}</option>`).join("");
    const time = new Date(status.generated_at).toLocaleString("en-US", {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});
    $("#freshness").textContent = `POSTGAME DATA · UPDATED ${time.toUpperCase()}`;
    await loadWeek();
  } catch (e) { error(`${e.message}. To load results, run “uv run refimpact refresh” in the project.`); }
}

async function loadWeek() {
  const request = ++state.request;
  $("#reload").disabled = true;
  error("");
  try {
    const [games,calls,drives] = await Promise.all([get(`/api/v1/games?${query()}`),get(`/api/v1/penalties?${query()}&limit=1000&detail=false`),get(`/api/v1/drives?${query()}`)]);
    if (request !== state.request) return;
    state.games = games.games;state.penalties=calls.penalties;state.drives=drives.drives;state.penaltyLimit=25;state.driveLimit=20;
    const period = state.weeks.find(w => w.season===state.season && w.week===state.week);
    $("#date-range").textContent = `${shortDate(period.start)} – ${shortDate(period.end)}`;
    $("#hero-week").textContent = `${state.season} / WEEK ${String(state.week).padStart(2,"0")}`;
    $("#hero-count").textContent = games.penalty_rows;
    const pricedShare = games.flags_took_effect ? ` (${Math.round(100*games.valued_penalties/games.flags_took_effect)}% of flags that took effect)` : "";
    $("#hero-coverage").textContent = `${games.valued_penalties} priced${pricedShare}${games.unpriced_penalties == null ? "" : ` · ${games.unpriced_penalties} unpriced`} · ${games.penalty_dispositions.declined || 0} declined · ${games.games.length} games`;
    $("#export").href = `/api/v1/export.csv?${query()}`;
    const previousTeam = $("#team").value;
    const teams = [...new Set(games.games.flatMap(g => [g.home_team,g.away_team]))].sort();
    $("#team").innerHTML = '<option value="">All teams</option>' + teams.map(t => `<option value="${escapeHTML(t)}">${escapeHTML(t)}</option>`).join("");
    if (teams.includes(previousTeam)) $("#team").value = previousTeam;
    history.replaceState({},"",`?${query()}`);
    renderGames();renderCalls();renderProduction();renderDrives();
    await loadAnalysis();
  } catch(e) { if (request===state.request) error(e.message); }
  finally { if(request===state.request) $("#reload").disabled=false; }
}

function renderGames() {
  const team = $("#team").value;
  const games = state.games.filter(g => !team || [g.home_team,g.away_team].includes(team));
  $("#game-grid").innerHTML = games.length ? games.map(g => {
    const final = g.status === "final";
    const homeWin = final && g.home_score>g.away_score, awayWin=final && g.away_score>g.home_score;
    const epa = g.net_home_epa;
    const top = g.top_swings?.[0];
    const biggest = top ? `${swingPP(top.home_wp_swing_pp)} · Q${top.qtr ?? "?"} ${top.time || ""}` : g.largest_call_swing_pp != null ? `${Math.round(g.largest_call_swing_pp)} pp` : "—";
    const availability = !g.pbp_available ? "PBP pending" : !g.pbp_complete ? "Partial PBP" : `${g.valued_penalties ?? g.scored_calls} priced${g.unpriced_penalties == null ? "" : ` · ${g.unpriced_penalties} unpriced`}`;
    return `<button class="game-card" data-game="${escapeHTML(g.game_id)}" aria-label="Inspect ${escapeHTML(g.away_team)} at ${escapeHTML(g.home_team)}">
      <div class="card-top"><span>${escapeHTML(shortDate(g.gameday))} · ${escapeHTML(g.gametime || "TBD")} ET</span><span class="${final?"final-tag":""}">${final?"FINAL":g.status==="scheduled"?"UPCOMING":"PENDING"}</span></div>
      <div class="team-line ${homeWin?"loser":""}">${mark(g.away_team)}<span class="team-name">${escapeHTML(g.away_team)}</span><span class="score">${final?g.away_score:"—"}</span></div>
      <div class="team-line ${awayWin?"loser":""}">${mark(g.home_team)}<span class="team-name">${escapeHTML(g.home_team)}</span><span class="score">${final?g.home_score:"—"}</span></div>
      <div class="net"><span>Biggest flag swing · ${escapeHTML(g.home_team)}</span><span class="${colorClass(top?.home_wp_swing_pp)}">${escapeHTML(biggest)}</span></div>
      <div class="card-production"><span>Flag swings of 5+ pp</span><span>${g.swings_over_5pp ?? "needs data refresh"}</span></div>
      <div class="card-production"><span>Expected margin shift, ignores score</span><span class="${colorClass(epa)}">${escapeHTML(g.home_team)} ${signed(epa)}</span></div>
      <div class="card-production"><span>Observed offense EPA · ${escapeHTML(g.home_team)}</span><span>${signed(g.production?.[g.home_team]?.offensive_epa)}</span></div><div class="card-explanation">Swings are win chance at the moment of each flag. The margin shift counts blowout flags in full. Offense already includes penalties.</div>
      <div class="card-bottom"><span>${escapeHTML(availability)}</span><span>Inspect ↗</span></div></button>`;
  }).join("") : '<div class="empty">No games match this selection.</div>';
}

async function loadAnalysis() {
  const request=++state.analysisRequest;
  try {
    const data=await get(`/api/v1/analysis?season=${state.season}&metric=${state.metric}${state.scope==="week"?`&week=${state.week}`:""}`);
    if(request!==state.analysisRequest) return;
    state.plotRows=data.points;state.metricLabel=data.metric_label;$("#plot-metric-label").textContent=state.metric.replaceAll("_"," ").toUpperCase()+" × FINAL MARGIN";$("#plot-caption").textContent="Right: a higher home-team metric. Up: a larger home win. Same-game production is descriptive accounting, not a causal penalty adjustment.";
    $("#correlation").textContent=data.pearson_r_margin==null?"N/A":signed(data.pearson_r_margin,2);
    $("#interval").textContent=data.bootstrap_95_interval?`95% exploratory interval: ${signed(data.bootstrap_95_interval[0],2)} to ${signed(data.bootstrap_95_interval[1],2)}`:"Not enough varied completed games to estimate a correlation.";
    $("#win-rate").innerHTML=data.decisive_games_with_nonzero_impact?`The team with a higher ${state.metric === "penalty_epa" ? "expected margin shift" : "production metric"} won <span class="positive">${data.benefited_team_wins} of ${data.decisive_games_with_nonzero_impact}</span> games.`:"No completed games with a nonzero measured benefit yet.";
    const coverage=data.flags_took_effect?`${Math.round(100*data.scored_calls/data.flags_took_effect)}% of flags that took effect are priced.`:"No scored evidence available.";
    $("#analysis-note").textContent=`${data.games} game-level observations · ${coverage} ${state.scope==="season"?"Season to date.":"Selected slate."}`;
    drawPlot(data.points);
  }catch(e){if(request===state.analysisRequest) error(e.message);}
}

function drawPlot(rows) {
  if(!rows.length){$("#scatter").innerHTML='<div class="empty">Completed games with values for this metric will appear here.</div>';return;}
  const W=760,H=350,left=55,right=22,top=28,bottom=54;
  const xmax=Math.max(1,...rows.map(r=>Math.abs(r.net_home_epa)))*1.15;
  const ymax=Math.max(7,...rows.map(r=>Math.abs(r.home_margin)))*1.13;
  const x=v=>left+(v+xmax)/(2*xmax)*(W-left-right);
  const y=v=>top+(ymax-v)/(2*ymax)*(H-top-bottom);
  let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${rows.length} games: ${escapeHTML(state.metricLabel)} versus home final score margin"><title>Selected EPA metric and final margin, one point per game</title>`;
  for(const fraction of [-1,-.5,0,.5,1]){
    const xv=fraction*xmax,yv=fraction*ymax;
    svg+=`<line x1="${x(xv)}" y1="${top}" x2="${x(xv)}" y2="${H-bottom}" stroke="${fraction===0?"#6b7d6d":"#334036"}" stroke-dasharray="${fraction===0?"4 4":"0"}"/><text x="${x(xv)}" y="${H-bottom+20}" text-anchor="middle" fill="#a2b1a5" font-family="monospace" font-size="10">${xv.toFixed(1)}</text>`;
    svg+=`<line x1="${left}" y1="${y(yv)}" x2="${W-right}" y2="${y(yv)}" stroke="${fraction===0?"#6b7d6d":"#334036"}" stroke-dasharray="${fraction===0?"4 4":"0"}"/><text x="${left-12}" y="${y(yv)+3}" text-anchor="end" fill="#a2b1a5" font-family="monospace" font-size="10">${Math.round(yv)}</text>`;
  }
  svg+=`<text x="${left}" y="15" fill="#a2b1a5" font-size="10">Home final margin (points)</text><text x="${(W+left-right)/2}" y="${H-10}" text-anchor="middle" fill="#a2b1a5" font-size="10">${escapeHTML(state.metricLabel)}</text>`;
  rows.forEach(r=>{
    const name=`${r.away_team} @ ${r.home_team}: EPA ${signed(r.net_home_epa)}, margin ${signed(r.home_margin,0)}`;
    svg+=`<circle class="point" data-game="${escapeHTML(r.game_id)}" tabindex="0" role="button" aria-label="${escapeHTML(name)}" cx="${x(r.net_home_epa)}" cy="${y(r.home_margin)}" r="${rows.length>40?4:5.5}" fill="${r.home_margin>=0?"#d1f482":"#c6b2f6"}" fill-opacity=".75"><title>${escapeHTML(name)}</title></circle>`;
  });
  svg+="</svg>";$("#scatter").innerHTML=svg;
}

$("#week").addEventListener("change",()=>{[state.season,state.week]=$("#week").value.split(":").map(Number);loadWeek();});
$("#team").addEventListener("change",()=>{renderGames();renderCalls();renderProduction();renderDrives();});
$("#reload").addEventListener("click",()=>init());
for(const scope of ["week","season"]){$("#scope-"+scope).addEventListener("click",()=>{state.scope=scope;for(const s of ["week","season"]){$("#scope-"+s).classList.toggle("selected",scope===s);$("#scope-"+s).setAttribute("aria-pressed",String(scope===s));}loadAnalysis();});}
document.addEventListener("click",e=>{const target=e.target.closest("[data-penalty],[data-drive],[data-game]");if(!target)return;if(target.dataset.penalty)openPenalty(target.dataset.penalty);else if(target.dataset.drive)openDrive(target.dataset.drive);else openGame(target.dataset.game);});
document.addEventListener("keydown",e=>{if(["Enter"," "].includes(e.key)&&e.target.matches("circle[data-game]")){e.preventDefault();openGame(e.target.dataset.game);}});
$("#close-dialog").addEventListener("click",()=>$("#game-dialog").close());
$("#game-dialog").addEventListener("click",e=>{if(e.target===$("#game-dialog")){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
$("#disposition").addEventListener("change",()=>{state.penaltyLimit=25;renderCalls();});
$("#drive-filter").addEventListener("change",()=>{state.driveLimit=20;renderDrives();});
$("#more-penalties").addEventListener("click",()=>{state.penaltyLimit+=25;renderCalls();});
$("#more-drives").addEventListener("click",()=>{state.driveLimit+=20;renderDrives();});
$("#metric").addEventListener("change",()=>{state.metric=$("#metric").value;loadAnalysis();});
init();
