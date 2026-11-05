"use strict";
// Ref Impact: week list, game page, drives, plays and the outcome editor. All math is on the server.
(() => {
  const app = document.getElementById("app");
  const MINUS = "−";
  const ORDINAL = ["", "1st", "2nd", "3rd", "4th"];
  const NOISE_PP = 2;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const S = {
    weeks: null,
    season: null,
    week: null,
    view: null, // the open game
    team: null,
    vegasLine: false,
    open: new Set(), // expanded drives
    showOut: new Set(), // edited drives showing their dropped plays
    edits: new Map(), // gameId -> Map(driveId -> {play_id, request, result})
    editor: null, // {play_id, kind, yards, spot_team, spot_yard, result, error}
    pinned: null,
    request: 0,
    outcomeRequest: 0,
    debounce: null,
  };

  // ---------- formatting ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  const sign = (r) => (r > 0 ? "+" : r < 0 ? MINUS : "");
  const pp = (x) => { const r = Math.round(x); return `${sign(r)}${Math.abs(r)} pp`; };
  const points = (x) => `${x >= 0 ? "+" : MINUS}${Math.abs(x).toFixed(1)}`;
  const qlabel = (q, t) => (q > 4 ? "OT" : `Q${q}`) + (t ? ` ${String(t).replace(/^0(?=\d)/, "")}` : "");
  const dayText = (iso, options) => new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", options);
  const cleanDesc = (d) => String(d ?? "").replace(/^\(\s*:?\d*:?\d*\)\s*/, "").replace(/\s+/g, " ").trim();
  function delta(value) {
    if (value == null) return '<span class="delta delta--quiet">—</span>';
    const r = Math.round(value), quiet = Math.abs(r) < NOISE_PP;
    const cls = quiet ? "delta--quiet" : r > 0 ? "delta--pos" : "delta--neg";
    return `<span class="delta ${cls}">${sign(r)}${Math.abs(r)}<span class="sr-only"> percentage points</span></span>`;
  }
  const spot = (y, team, rival) => (y == null ? "" : y === 50 ? "50" : y > 50 ? `${team} ${Math.round(100 - y)}` : `${rival} ${Math.round(y)}`);

  async function get(path) {
    const response = await fetch(path, { cache: "no-cache" });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
    return data;
  }

  // ---------- routes ----------
  async function route() {
    const hash = location.hash.replace(/^#/, "") || "/";
    let match;
    if ((match = hash.match(/^\/game\/([\w]+)$/))) await renderGame(match[1]);
    else {
      match = hash.match(/^\/week\/(\d+)\/(\d+)$/);
      await renderWeek(match ? Number(match[1]) : null, match ? Number(match[2]) : null);
    }
  }
  window.addEventListener("hashchange", async () => { await route(); window.scrollTo(0, 0); });

  function notice(message) { app.innerHTML = `<p class="notice">${esc(message)}</p>`; }

  // ================= Week =================
  async function renderWeek(season, week) {
    const request = ++S.request;
    try {
      S.weeks = S.weeks || (await get("/api/v1/weeks"));
      S.season = season ?? S.season ?? S.weeks.default_season;
      S.week = week ?? S.week ?? S.weeks.default_week;
      const data = await get(`/api/v1/weeks/${S.season}/${S.week}`);
      if (request !== S.request) return;
      const list = S.weeks.weeks, at = list.findIndex((w) => w.season === S.season && w.week === S.week);
      const period = list[at], prev = list[at - 1], next = list[at + 1];
      const link = (w, label, arrow) => `<a class="weeknav__btn" ${w ? `href="#/week/${w.season}/${w.week}"` : 'aria-disabled="true"'} aria-label="${w ? `${label}: week ${w.week}` : label}"><span aria-hidden="true">${arrow}</span></a>`;
      const range = period ? `${dayText(period.start, { month: "short", day: "numeric" })} – ${dayText(period.end, { month: "short", day: "numeric" })} · ${period.season}` : "";
      const byDay = {};
      data.games.forEach((g) => (byDay[g.gameday] ||= []).push(g));
      const days = Object.keys(byDay).sort().map((d) => `
        <section class="day" aria-labelledby="d-${d}">
          <h2 class="day__title" id="d-${d}">${dayText(d, { weekday: "long", month: "short", day: "numeric" })}</h2>
          <ul>${byDay[d].map(gameRow).join("")}</ul>
        </section>`).join("");
      app.innerHTML = `
        <nav class="weeknav" aria-label="Choose week">
          ${link(prev, "Previous week", "←")}
          <div><h1>Week ${S.week}</h1><p>${esc(range)}</p></div>
          ${link(next, "Next week", "→")}
        </nav>
        ${days || '<p class="notice">No games this week.</p>'}`;
      document.title = `Week ${S.week} · Ref Impact`;
    } catch (e) {
      if (request === S.request) notice(`${e.message}. To load results, run “make refresh” in the project.`);
    }
  }

  // Whole points for each team, leader first. Net is the difference of these, so the page adds up.
  function swingRows(card) {
    return [card.away_team, card.home_team].map((team) => {
      const t = card.swing_totals[team];
      return { team, gained: Math.round(t.gained), lost: Math.round(t.lost) };
    }).sort((x, y) => (y.gained - y.lost) - (x.gained - x.lost));
  }

  function swingTable(card) {
    if (!card || !card.biggest) return "";
    const signed = (n) => (n > 0 ? `+${n}` : n < 0 ? `−${-n}` : "0");
    const rows = [card.away_team, card.home_team].map((team) => swingRows(card).find((r) => r.team === team));
    return `<table class="totals"><caption>Win chance from flags, pp</caption>
      <thead><tr><th scope="col">Team</th><th scope="col">Gained</th><th scope="col">Lost</th><th scope="col">Net</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><th scope="row">${esc(r.team)}</th><td>${signed(r.gained)}</td><td>${signed(-r.lost)}</td><td>${signed(r.gained - r.lost)}</td></tr>`).join("")}</tbody></table>
      <p class="scorehead__net">Each flag's swing when it happened, added up. Not what the final win chance would have been.</p>`;
  }

  function impactHtml(card, detail = false) {
    if (!card) return "";
    if (card.status !== "final" && !card.pbp_available) return `<div class="impact"><span class="impact__none">${card.status === "scheduled" ? "Upcoming" : "Not available yet"}</span></div>`;
    const big = card.biggest;
    if (!big) return `<div class="impact"><span class="impact__label">Ref impact</span><span class="impact__none">${card.flags ? "No priced flags" : "No flags"}</span></div>`;
    const unpriced = card.unpriced && !detail ? `<span class="impact__sub">${card.unpriced} not priced</span>` : "";
    const [lead, other] = swingRows(card);
    const net = lead.gained - lead.lost;
    const value = net === 0 ? "Even" : `${esc(lead.team)} +${net} pp`;
    const gained = detail ? "" : `<span class="impact__sub">Gained: ${esc(lead.team)} ${lead.gained}, ${esc(other.team)} ${other.gained}</span>`;
    return `<div class="impact"><span class="impact__label">Ref impact</span><span class="impact__value">${value}</span>${gained}<span class="impact__sub">Biggest call ${esc(big.team)} +${Math.round(big.pp)}, ${qlabel(big.qtr, big.time)}</span>${unpriced}</div>`;
  }

  function gameRow(g) {
    const final = g.status === "final";
    const awayWin = final && g.away_score > g.home_score, homeWin = final && g.home_score > g.away_score;
    const team = (abbr, score, win) => `<span class="team ${win ? "team--win" : ""}"><span class="team__abbr">${esc(abbr)}</span>${final ? `<span class="team__score">${score}</span>` : ""}</span>`;
    const when = !final && g.gametime ? `<span class="team__time">${new Date(`2000-01-01T${g.gametime}`).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })} ET</span>` : "";
    return `<li><a class="game" href="#/game/${esc(g.game_id)}" aria-label="${esc(g.away_team)} at ${esc(g.home_team)}${final ? `, ${g.away_score} to ${g.home_score}` : ""}">
      <span class="game__teams">${team(g.away_team, g.away_score, awayWin)}${team(g.home_team, g.home_score, homeWin)}${when}</span>
      ${impactHtml(g)}
    </a></li>`;
  }

  // ================= Game =================
  const G = () => S.view.game;
  const home = () => G().home_team;
  const wpT = (h) => (h == null ? null : S.team === home() ? h : 1 - h);
  const ppT = (h) => (h == null ? null : S.team === home() ? h : -h);
  const rivalOf = (team) => (team === G().home_team ? G().away_team : G().home_team);
  const edits = () => { if (!S.edits.has(G().game_id)) S.edits.set(G().game_id, new Map()); return S.edits.get(G().game_id); };

  async function renderGame(id) {
    const request = ++S.request;
    try {
      const view = S.view?.game.game_id === id ? S.view : await get(`/api/v1/games/${encodeURIComponent(id)}/view`);
      if (request !== S.request) return;
      if (S.view?.game.game_id !== id) { S.open.clear(); S.showOut.clear(); S.editor = null; S.pinned = null; S.team = null; }
      S.view = view;
      S.season = G().season;
      S.week = G().week;
      S.team ||= home();
      drawGameShell();
      document.title = `${G().away_team} at ${G().home_team} · Ref Impact`;
    } catch (e) {
      if (request === S.request) notice(e.message);
    }
  }

  function drawGameShell() {
    const g = G(), final = g.status === "final";
    const awayWin = final && g.away_score > g.home_score, homeWin = final && g.home_score > g.away_score;
    const score = (team, pts, win) => `<span class="scoreteam ${win ? "scoreteam--win" : ""}"><span class="scoreteam__abbr">${esc(team)}</span><span class="scoreteam__pts">${pts ?? "—"}</span></span>`;
    const net = g.net_home_epa, netTeam = net >= 0 ? g.home_team : g.away_team;
    const netLine = net == null ? "" : `<p class="scorehead__net">Flags net, ignoring score: ${esc(netTeam)} +${Math.abs(net).toFixed(1)} pts · ${g.valued_penalties ?? 0} priced, ${g.unpriced_penalties ?? 0} not priced</p>`;
    const meta = [dayText(g.gameday, { weekday: "long", month: "short", day: "numeric" }), g.stadium, final ? "Final" : g.status === "scheduled" ? "Upcoming" : "In progress"].filter(Boolean).join(" · ");
    const back = `<a class="back" href="#/week/${g.season}/${g.week}">← Week ${g.week}</a>`;
    const head = `<header class="scorehead">${back}
      <p class="scorehead__meta">${esc(meta)}</p>
      <h1 class="scorehead__score" aria-label="${esc(g.away_team)} ${g.away_score ?? ""}, ${esc(g.home_team)} ${g.home_score ?? ""}">${score(g.away_team, g.away_score, awayWin)}${score(g.home_team, g.home_score, homeWin)}</h1>
      ${impactHtml(S.view.card, true)}${swingTable(S.view.card)}${netLine}</header>`;
    if (!S.view.chart.length) { app.innerHTML = `${head}<p class="notice">Plays appear after the game, when play-by-play is published.</p>`; return; }
    app.innerHTML = `${head}
      <section class="chart" id="chart-sec" aria-labelledby="chart-h">
        <div class="section-head">
          <h2 id="chart-h">Win chance</h2>
          <fieldset class="seg-wrap"><legend class="sr-only">Show win chance for</legend>
            <div class="seg seg--tight" role="radiogroup">${[g.away_team, g.home_team].map((t) => `<label class="seg__opt"><input type="radio" name="team" value="${esc(t)}" ${t === S.team ? "checked" : ""}><span>${esc(t)}</span></label>`).join("")}</div>
          </fieldset>
        </div>
        <div class="chart__caption"><span id="chart-cap"></span><span class="chart__key" id="chart-key"></span></div>
        <div id="chart-host"></div>
        <div class="tip" id="tip" hidden></div>
        <label class="chart__toggle"><input type="checkbox" id="vegas-line" ${S.vegasLine ? "checked" : ""}> Show Vegas-informed line</label>
      </section>
      <section class="drives" id="drives" aria-labelledby="drives-h">
        <div class="section-head"><h2 id="drives-h">Drives</h2></div>
        <p class="drives__legend" id="drives-legend"></p>
        <div id="drives-host"></div>
      </section>`;
    drawChart();
    drawDrives();
    if (!window.__chartObserver) window.__chartObserver = new ResizeObserver(() => { if (document.getElementById("chart-host")) drawChart(); });
    window.__chartObserver.disconnect();
    window.__chartObserver.observe(document.getElementById("chart-host"));
  }

  // ---------- chart ----------
  let FLAGS = [];
  const chartY = (id) => S.view.chart.find((p) => p.play_id === id)?.wp ?? null;
  function playById(id) {
    for (const drive of S.view.drives) for (const play of drive.plays) if (play.play_id === id) return { drive, play };
    return null;
  }

  function drawChart() {
    const host = document.getElementById("chart-host");
    if (!host) return;
    const rows = S.view.chart, W = Math.max(280, host.clientWidth), H = W < 520 ? 210 : 250;
    const m = { l: W < 520 ? 34 : 40, r: 8, t: 12, b: 28 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    const xMax = Math.max(3600, rows[rows.length - 1].x);
    const X = (t) => m.l + (t / xMax) * pw, Y = (w) => m.t + (1 - w) * ph;
    const line = (key) => rows.filter((r) => r[key] != null).map((r, i) => `${i ? "L" : "M"}${X(r.x).toFixed(1)} ${Y(wpT(r[key])).toFixed(1)}`).join("");
    const first = wpT(rows[0].wp), last = wpT(rows[rows.length - 1].wp);
    document.getElementById("chart-cap").textContent = `${S.team} starts at ${pct(first)}, ends at ${pct(last)}`;

    let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="group" aria-label="${esc(S.team)} win chance across the game, with flags marked">`;
    [0, 0.5, 1].forEach((w) => {
      svg += `<line class="${w === 0.5 ? "chart__base" : "chart__grid"}" x1="${m.l}" x2="${W - m.r}" y1="${Y(w)}" y2="${Y(w)}"/>`;
      svg += `<text class="chart__label" x="${m.l - 8}" y="${Y(w) + 4}" text-anchor="end">${Math.round(w * 100)}%</text>`;
    });
    for (let q = 0; q <= 4; q++) svg += `<line class="chart__tick" x1="${X(q * 900)}" x2="${X(q * 900)}" y1="${H - m.b}" y2="${H - m.b + 5}"/>`;
    for (let q = 1; q <= 4; q++) svg += `<text class="chart__label" x="${X((q - 0.5) * 900)}" y="${H - 6}" text-anchor="middle">Q${q}</text>`;
    if (xMax > 3600) svg += `<text class="chart__label" x="${X((3600 + xMax) / 2)}" y="${H - 6}" text-anchor="middle">OT</text>`;
    if (S.vegasLine) svg += `<path class="chart__line chart__line--vegas" d="${line("vegas")}"/>`;
    svg += `<path class="chart__line" d="${line("wp")}"/>`;

    // Each edit is one hollow marker on a short stub; the line itself stays as recorded.
    edits().forEach((edit) => {
      const found = playById(edit.play_id);
      const yours = wpT(edit.result.yours.home_wp);
      const point = rows.find((r) => r.play_id === edit.play_id);
      if (!found || yours == null || !point) return;
      const start = wpT(found.play.home_wp_after ?? point.wp), y0 = Y(start), dir = yours >= start ? -1 : 1;
      const length = clamp(Math.abs(Y(yours) - y0), 16, 34), y1 = clamp(y0 + dir * length, m.t + 6, H - m.b - 6), x = X(point.x);
      svg += `<g class="edit-mark" role="img" aria-label="Your version at ${qlabel(found.play.qtr, found.play.time)}: ${esc(S.team)} win chance ${pct(yours)}"><line class="edit-mark__stub" x1="${x}" x2="${x}" y1="${y0}" y2="${y1}"/><circle class="edit-mark__ring" cx="${x}" cy="${y1}" r="5.5"/></g>`;
    });

    FLAGS = S.view.flags.filter((f) => f.elapsed != null && chartY(f.play_id) != null).sort((a, b) => a.elapsed - b.elapsed);
    FLAGS.forEach((f, i) => {
      const swing = ppT(f.home_swing), declined = f.disposition === "declined", unpriced = !declined && swing == null;
      const quiet = swing != null && Math.abs(Math.round(swing)) < NOISE_PP;
      const r = declined || unpriced || quiet ? 3.5 : clamp(4 + Math.abs(swing) / 5, 4, 7.5);
      const cx = X(f.elapsed), cy = Y(wpT(chartY(f.play_id)));
      f._x = cx; f._y = cy;
      const cls = `dot${declined || unpriced ? " dot--declined" : quiet ? " dot--quiet" : ""}`;
      const words = declined ? ", declined" : unpriced ? ", not priced" : `, ${S.team} ${pp(swing)}`;
      svg += `<g class="${cls}" data-i="${i}" tabindex="0" role="button" aria-label="${esc(f.ruling)}, ${qlabel(f.qtr, f.time)}${words}. Press Enter to jump to the play."><circle class="dot__ring" cx="${cx}" cy="${cy}" r="${r + 4}"/><circle class="dot__mark" cx="${cx}" cy="${cy}" r="${r}"/></g>`;
    });
    svg += `<rect id="hit" x="${m.l}" y="0" width="${pw}" height="${H - m.b}" fill="transparent"/></svg>`;
    host.innerHTML = svg;
    document.getElementById("chart-key").innerHTML = `<span class="k-flag"><i></i>Flag</span>${edits().size ? '<span class="k-edit"><i></i>Your version</span>' : ""}`;
    if (S.pinned != null && FLAGS[S.pinned]) showTip(S.pinned, true); else hideTip();
  }

  function nearestFlag(clientX) {
    const svg = document.querySelector("#chart-host svg"), box = svg.getBoundingClientRect();
    const x = (clientX - box.left) * (svg.viewBox.baseVal.width / box.width);
    let best = -1, distance = 26;
    FLAGS.forEach((f, i) => { const d = Math.abs(f._x - x); if (d < distance) { distance = d; best = i; } });
    return best;
  }

  function showTip(i, pinned) {
    const f = FLAGS[i], tip = document.getElementById("tip"), host = document.getElementById("chart-host"), sec = document.getElementById("chart-sec");
    document.querySelectorAll(".dot.is-active").forEach((n) => n.classList.remove("is-active"));
    document.querySelector(`.dot[data-i="${i}"]`)?.classList.add("is-active");
    const swing = ppT(f.home_swing), vegas = ppT(f.home_swing_vegas), found = playById(f.play_id);
    const moved = found ? ppT(found.play.delta_pp) : null;
    let body;
    if (f.disposition === "declined") body = '<div class="tip__swing tip__meta">Declined: no change</div>';
    else if (swing == null) body = `<div class="tip__swing tip__meta">Not priced</div>`;
    else body = `<div class="tip__swing">${esc(S.team)} <span class="delta ${Math.abs(Math.round(swing)) < NOISE_PP ? "delta--quiet" : swing > 0 ? "delta--pos" : "delta--neg"}">${pp(swing)}</span>${vegas != null ? ` <span class="tip__meta">· Vegas ${pp(vegas)}</span>` : ""}</div><div class="tip__meta">Against keeping the play as written${f.approximate ? ", approximate" : ""}</div>`;
    const movedLine = moved != null ? `<div class="tip__meta">Win chance moved ${pp(moved)} on this play</div>` : "";
    tip.classList.toggle("is-pinned", !!pinned);
    tip.innerHTML = `<div class="tip__title">${esc(f.ruling)}${f.on ? ` on ${esc(f.on)}` : ""}</div><div class="tip__meta">${qlabel(f.qtr, f.time)}</div>${body}${movedLine}${pinned ? `<div class="tip__nav"><button type="button" data-tip="prev" aria-label="Previous flag">←</button><button type="button" class="tip__go" data-tip="go">Go to play</button><button type="button" data-tip="next" aria-label="Next flag">→</button></div>` : ""}`;
    tip.hidden = false;
    const hostBox = host.getBoundingClientRect(), secBox = sec.getBoundingClientRect();
    const scale = hostBox.width / (host.querySelector("svg")?.viewBox.baseVal.width || hostBox.width);
    const w = tip.offsetWidth, h = tip.offsetHeight, ox = hostBox.left - secBox.left, oy = hostBox.top - secBox.top;
    let top = oy + f._y * scale - h - 14;
    if (top < oy - 4) top = oy + f._y * scale + 16;
    tip.style.left = `${clamp(ox + f._x * scale - w / 2, 0, Math.max(0, sec.clientWidth - w))}px`;
    tip.style.top = `${top}px`;
  }
  function hideTip() {
    const tip = document.getElementById("tip");
    if (tip) tip.hidden = true;
    document.querySelectorAll(".dot.is-active").forEach((n) => n.classList.remove("is-active"));
  }

  function jumpToFlag(i) {
    const f = FLAGS[i];
    const found = playById(f.play_id);
    if (!found) return;
    S.open.add(found.drive.id);
    const edit = edits().get(found.drive.id);
    if (edit && found.drive.plays.findIndex((p) => p.play_id === f.play_id) > found.drive.plays.findIndex((p) => p.play_id === edit.play_id)) S.showOut.add(found.drive.id);
    S.pinned = null; hideTip();
    drawDrives();
    const el = document.getElementById(`play-${f.play_id}`);
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
    el.classList.remove("is-target"); void el.offsetWidth; el.classList.add("is-target");
    el.querySelector("button")?.focus({ preventScroll: true });
  }

  // ---------- drives ----------
  function drawDrives() {
    const host = document.getElementById("drives-host");
    if (!host) return;
    document.getElementById("drives-legend").innerHTML = `Win chance is ${esc(S.team)}'s at the end of each drive. <b>Bold</b> is the main model, lighter is the Vegas-informed one. <span class="flagmark">Flags in drive</span>`;
    host.innerHTML = `<div class="drives__cols" aria-hidden="true"><span>Quarter</span><span>Team</span><span>Start</span><span>Result</span><span class="r">Plays</span><span class="r">Win % end</span><span class="r">Chg</span><span></span></div><ol>${S.view.drives.map(driveRow).join("")}</ol>`;
  }

  function driveRow(d) {
    const edit = edits().get(d.id), open = S.open.has(d.id);
    const index = edit ? d.plays.findIndex((p) => p.play_id === edit.play_id) : -1;
    const kept = edit ? d.plays.slice(0, index + 1) : d.plays;
    const wpEnd = edit ? edit.result.yours.home_wp : d.home_wp_end;
    const vegasEnd = edit ? edit.result.yours.home_wp_vegas : d.vegas_wp_end;
    const change = wpEnd != null && d.home_wp_start != null ? ppT(100 * (wpEnd - d.home_wp_start)) : null;
    const result = edit ? edit.result.outcome.drive_result || "Stops here" : d.result;
    const flags = kept.filter((p) => p.flag).length;
    const rival = rivalOf(d.team);
    return `<li class="drive ${edit ? "drive--edited" : ""}" id="drive-${d.number}">
      <h3><button type="button" class="drive__head" data-drive="${esc(d.id)}" aria-expanded="${open}" aria-controls="body-${d.number}">
        <span class="drive__time">${qlabel(d.qtr, d.time)}</span>
        <span class="drive__team">${esc(d.team)}</span>
        <span class="drive__start"><span class="sr-only">Starts at </span>${esc(spot(d.start_yardline_100, d.team, rival))}</span>
        <span class="drive__result"><span>${esc(result)}</span>${edit ? '<span class="drive__yours">Edited · your version</span>' : ""}${flags ? `<span class="flagmark"><span class="sr-only">${flags} flag${flags > 1 ? "s" : ""}</span><span aria-hidden="true">${flags > 1 ? flags : ""}</span></span>` : ""}</span>
        <span class="drive__plays">${kept.length} play${kept.length === 1 ? "" : "s"}</span>
        <span class="wp drive__wp"><b class="wp__main">${pct(wpT(wpEnd))}</b><span class="wp__alt"><span class="sr-only">Vegas-informed </span>${pct(wpT(vegasEnd))}</span></span>
        <span class="drive__delta">${delta(change)}</span>
        <span class="drive__chev" aria-hidden="true"></span>
      </button></h3>
      <div class="drive__body" id="body-${d.number}" role="region" aria-label="Drive ${d.number} plays" ${open ? "" : "hidden"}>${open ? playsHtml(d, edit, index) : ""}</div>
    </li>`;
  }

  function playsHtml(d, edit, index) {
    const kept = edit ? d.plays.slice(0, index + 1) : d.plays, dropped = edit ? d.plays.slice(index + 1) : [];
    let html = `<ol class="plays">${kept.map((p, i) => playHtml(d, p, edit && i === index ? edit : null, false)).join("")}</ol>`;
    if (dropped.length) {
      const show = S.showOut.has(d.id);
      html += `<button type="button" class="dropped" data-out="${esc(d.id)}" aria-expanded="${show}"><span>${dropped.length} recorded play${dropped.length > 1 ? "s" : ""} not in this version</span><span>${show ? "Hide" : "Show"}</span></button>`;
      if (show) html += `<ol class="plays">${dropped.map((p) => playHtml(d, p, null, true)).join("")}</ol>`;
    }
    return html;
  }

  function situation(p) {
    if (p.kind === "kickoff") return "Kickoff";
    if (p.kind === "extra_point") return "Extra point";
    if (p.kind === "two_point") return "Two-point try";
    const where = spot(p.yardline_100, p.team, p.rival);
    if (!p.down) return where;
    const togo = p.ydstogo >= p.yardline_100 ? "Goal" : Math.round(p.ydstogo);
    return `${ORDINAL[p.down]} & ${togo}, ${where}`;
  }

  function flagLine(flagId) {
    const f = S.view.flags.find((x) => x.id === flagId);
    if (!f) return "";
    const declined = f.disposition === "declined", swing = ppT(f.home_swing);
    const name = `Flag · ${esc(f.ruling)}${f.on ? ` on ${esc(f.on)}` : ""}${declined ? ", declined" : ""}`;
    let value = "";
    if (!declined && swing != null) value = `<span class="delta ${Math.abs(Math.round(swing)) < NOISE_PP ? "delta--quiet" : swing > 0 ? "delta--pos" : "delta--neg"}"><span class="sr-only">${esc(S.team)} win chance swing </span>${pp(swing)}</span>`;
    else if (!declined) value = '<span class="delta delta--quiet">not priced</span>';
    return `<div class="play__flag"><span class="flagmark ${declined ? "flagmark--declined" : ""}">${name}</span>${value}</div>`;
  }

  function playHtml(d, p, edit, out) {
    let wp = p.home_wp_after, vegas = p.vegas_wp_after, change = ppT(p.delta_pp);
    let body = `<p class="play__desc">${esc(cleanDesc(p.description))}</p>`;
    if (edit) {
      const yours = edit.result.yours;
      wp = yours.home_wp; vegas = yours.home_wp_vegas;
      change = wp != null && p.home_wp != null ? ppT(100 * (wp - p.home_wp)) : null;
      body = `<p class="play__desc play__yours">Your version: ${esc(edit.result.outcome.label)}</p><p class="play__recorded">Recorded: ${esc(cleanDesc(p.description))}</p>`;
    }
    const label = cleanDesc(p.description).slice(0, 60);
    const action = out ? "" : edit
      ? `<button type="button" class="link" data-act="open-editor" data-play="${esc(p.play_id)}">Edit</button> <button type="button" class="link" data-act="reset" data-drive-id="${esc(d.id)}">Reset</button>`
      : `<button type="button" class="link" data-act="open-editor" data-play="${esc(p.play_id)}" aria-label="Change outcome: ${esc(label)}">Change outcome</button>`;
    const open = !out && S.editor?.play_id === p.play_id;
    return `<li class="play ${edit ? "play--edited" : ""} ${out ? "play--out" : ""}" id="play-${esc(p.play_id)}">
      <div class="play__row">
        <span class="play__sit">${esc(situation(p))}</span>
        <div class="play__main">${body}${p.flag ? flagLine(p.flag) : ""}</div>
        <span class="wp"><b class="wp__main">${pct(wpT(wp))}</b><span class="wp__alt"><span class="sr-only">Vegas-informed </span>${pct(wpT(vegas))}</span></span>
        ${delta(change)}
        <span class="play__act">${action}</span>
      </div>
      ${open ? editorHtml(d, p) : ""}
    </li>`;
  }

  // ---------- outcome editor ----------
  function spotDefault(kind, p) {
    const y = p.yardline_100, here = y == null ? null : { team: y > 50 ? p.team : p.rival, yard: Math.round(y > 50 ? 100 - y : y) };
    if ((kind === "turnover" || kind === "blocked") && here) return here;
    if (kind === "punt_to") return { team: p.rival, yard: 25 };
    if (kind === "return_to") return { team: p.team, yard: 30 };
    if (kind === "muffed") return { team: p.rival, yard: 25 };
    if (kind === "onside") return { team: p.rival, yard: 45 };
    return { team: p.team, yard: 30 };
  }

  function chooseOption(kind, p) {
    const option = S.view.options[kind], e = S.editor;
    e.kind = kind;
    if (option.input === "yards" && e.yards == null) e.yards = 5;
    if (option.input === "spot") { const d = spotDefault(kind, p); e.spot_team = d.team; e.spot_yard = d.yard; }
  }

  function inputHtml(p) {
    const e = S.editor, option = S.view.options[e.kind];
    if (option.input === "yards") return `<label class="yards"><span>Yards (negative for a loss)</span><input type="number" inputmode="numeric" min="-99" max="99" data-yards value="${e.yards ?? ""}"></label>`;
    if (option.input === "spot") {
      const teams = [G().away_team, G().home_team];
      return `<div class="yards"><span id="spot-${esc(p.play_id)}">Yard line</span><select data-spot-team aria-labelledby="spot-${esc(p.play_id)}">${teams.map((t) => `<option value="${esc(t)}" ${t === e.spot_team ? "selected" : ""}>${esc(t)}</option>`).join("")}</select><input type="number" inputmode="numeric" min="1" max="50" data-spot-yard aria-label="Yard number" value="${e.spot_yard ?? ""}"></div>`;
    }
    return "";
  }

  function editorHtml(d, p) {
    const e = S.editor;
    const options = p.menu.map((k) => `<label class="seg__opt"><input type="radio" name="opt-${esc(p.play_id)}" value="${esc(k)}" ${k === e.kind ? "checked" : ""}><span>${esc(S.view.options[k].label)}</span></label>`).join("");
    const edited = edits().get(d.id)?.play_id === p.play_id;
    return `<section class="editor" aria-labelledby="ed-h-${esc(p.play_id)}" data-play="${esc(p.play_id)}">
      <div class="editor__head"><h4 id="ed-h-${esc(p.play_id)}">Change outcome</h4><button type="button" class="link" data-act="close-editor">Close</button></div>
      <fieldset><legend>What happens instead</legend>
        <div class="seg" role="radiogroup">${options}</div>
        <div data-input>${inputHtml(p)}</div>
      </fieldset>
      <div class="result" aria-live="polite" data-result>${resultHtml(p)}</div>
      <div class="editor__foot">
        <button type="button" class="btn btn--primary" data-act="apply" ${e.result ? "" : "disabled"}>Use this version</button>
        ${edited ? `<button type="button" class="link" data-act="reset" data-drive-id="${esc(d.id)}">Reset to recorded</button>` : '<button type="button" class="link" data-act="close-editor">Cancel</button>'}
      </div></section>`;
  }

  function scoreLine(score) { return `${esc(G().away_team)} ${score.away}, ${esc(G().home_team)} ${score.home}`; }

  function resultHtml(p) {
    const e = S.editor;
    if (e.error) return `<p class="editor__error" role="alert">${esc(e.error)}</p>`;
    const r = e.result;
    if (!r) return '<p class="measure__desc">Calculating…</p>';
    const before = wpT(r.before.home_wp);
    const column = (title, cls, side) => {
      const wp = wpT(side.home_wp), change = wp != null && before != null ? 100 * (wp - before) : null;
      return `<div class="compare__col ${cls}"><h5>${title}</h5><p class="compare__sit">${esc(side.text)}</p><p class="compare__score">${scoreLine(side.score)}</p>
        <p class="compare__wp"><b class="wp__main">${pct(wp)}</b><span class="wp__alt">Vegas ${pct(wpT(side.home_wp_vegas))}</span>${delta(change)}</p></div>`;
    };
    const swing = r.swing;
    const gap = swing ? `Your version against recorded: <b>${pp(ppT(swing.wp_pp))}</b>${swing.vegas_pp != null ? `, Vegas ${pp(ppT(swing.vegas_pp))}` : ""}, ${points(ppT(swing.points))} points.` : "";
    const warnings = r.warnings.map((w) => `<p class="compare__note">${esc(w)}</p>`).join("");
    return `<section><div class="compare">
        ${column("Recorded", "", r.recorded)}
        ${column("Your version", "compare__col--yours", r.yours)}
        <p class="compare__note">${esc(S.team)}'s win chance, with the change from before the snap. ${gap}</p>${warnings}
      </div></section>
      ${firstDownHtml(r)}
      ${scoringHtml(r)}`;
  }

  function factorRow(label, value, note, zero) {
    return `<li class="factor ${zero ? "factor--zero" : ""}"><span class="factor__name">${esc(label)}</span><span class="factor__val">${value}</span><span class="factor__note">${esc(note)}</span></li>`;
  }
  function wordsText(words) {
    if (!words) return "";
    const marks = [...words.strengths, ...words.weaknesses];
    const span = words.games ? `last ${words.games} game${words.games === 1 ? "" : "s"}, ${words.plays} plays` : "no games yet this season";
    return `${marks.length ? `${marks.join("; ")}. ` : ""}${span}`;
  }

  function firstDownHtml(r) {
    if (r.outcome.scored) return `<section><h5>Chance of a first down</h5><p class="measure__desc">Drive ended: ${esc(r.outcome.label.toLowerCase())}.</p></section>`;
    const f = r.first_down, go = f.kind === "go";
    const big = f.range && !f.applied ? `${Math.round(f.range[0] * 100)}–${Math.round(f.range[1] * 100)}%` : pct(f.chance);
    const title = go ? `If ${f.team} goes for it` : `${f.team} keeps the ball this series`;
    const rows = [];
    rows.push(factorRow("League from here", pct(f.league), `${f.n.toLocaleString()} plays from this down, distance and zone in 2024-25`, false));
    f.factors.forEach((x) => {
      const small = Math.abs(x.pp) < 1.5;
      const note = x.key === "momentum" ? f.momentum_line : `${x.sd >= 0 ? "+" : MINUS}${Math.abs(x.sd).toFixed(1)} SD. ${wordsText(x.words)}`;
      rows.push(factorRow(x.label, pp(x.pp), note, small));
    });
    if (!f.applied && f.words) {
      rows.push(factorRow(`${f.team} offense, last 4 games`, "", wordsText(f.words.offense), true));
      rows.push(factorRow(`${f.rival} defense, last 4 games`, "", wordsText(f.words.defense), true));
    }
    const extra = go
      ? `The league goes for it here ${pct(f.go_rate)} of the time. Teams that go chose to, so this is the rate for teams like them.`
      : `This snap converts: ${pct(f.snap)}. Counts a new first down or a touchdown; series cut off by the half are left out.`;
    return `<section><h5>Chance of a first down</h5><p class="metric__label">${esc(title)}</p><p class="metric__big">${big}</p><p class="measure__desc metric__note">${esc(extra)}${f.note ? ` ${esc(f.note)}` : ""}</p><ul class="factors">${rows.join("")}</ul></section>`;
  }

  function scoringHtml(r) {
    if (r.outcome.scored) return `<section><h5>Chance the offense scores from here</h5><p class="measure__desc">The drive ended with this result, so nothing is left to score from.</p></section>`;
    const items = r.scoring.measures.map((m) => {
      let value = "—";
      if (m.value != null) value = pct(m.value);
      else if (m.points != null) value = points(m.points);
      else if (m.pp != null) value = pp(m.pp);
      const missing = value === "—";
      return `<li class="measure ${missing ? "measure--na" : ""}"><span class="measure__label">${esc(m.label)}</span><span class="measure__val">${value}</span><span class="measure__desc">${esc(m.note)}</span></li>`;
    }).join("");
    return `<section><h5>Chance ${esc(r.scoring.team)} scores from here</h5><ul class="measures">${items}</ul></section>`;
  }

  function refreshEditor() {
    const section = document.querySelector(".editor");
    if (!section) return;
    section.querySelector("[data-result]").innerHTML = resultHtml(playById(S.editor.play_id).play);
    const apply = section.querySelector('[data-act="apply"]');
    if (apply) apply.disabled = !S.editor.result;
  }

  async function runOutcome() {
    const e = S.editor;
    if (!e) return;
    const request = ++S.outcomeRequest;
    const option = S.view.options[e.kind];
    const body = { game_id: G().game_id, play_id: e.play_id, kind: e.kind };
    if (option.input === "yards") {
      if (!Number.isInteger(e.yards)) { e.result = null; e.error = "Enter the yards as a whole number."; refreshEditor(); return; }
      body.yards = e.yards;
    }
    if (option.input === "spot") {
      if (!(Number.isInteger(e.spot_yard) && e.spot_yard >= 1 && e.spot_yard <= 50)) { e.result = null; e.error = "Enter a yard line from 1 to 50."; refreshEditor(); return; }
      body.spot_team = e.spot_team; body.spot_yard = e.spot_yard;
    }
    e.error = null;
    try {
      const response = await fetch("/api/v1/plays/outcome", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await response.json();
      if (request !== S.outcomeRequest || S.editor !== e) return;
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg || "That result can't be read");
      e.result = data; e.body = body;
    } catch (err) {
      if (request !== S.outcomeRequest || S.editor !== e) return;
      e.result = null; e.error = err.message;
    }
    refreshEditor();
  }
  function queueOutcome() { clearTimeout(S.debounce); S.debounce = setTimeout(runOutcome, 200); }

  function openEditor(id) {
    const found = playById(id);
    if (!found) return;
    const existing = edits().get(found.drive.id);
    if (existing?.play_id === id) S.editor = { play_id: id, ...existing.request, result: existing.result, error: null };
    else {
      S.editor = { play_id: id, kind: null, yards: null, spot_team: null, spot_yard: null, result: null, error: null };
      chooseOption(found.play.menu[0], found.play);
    }
    S.open.add(found.drive.id);
    drawDrives();
    document.querySelector(`#play-${CSS.escape(id)} .editor input:checked`)?.focus({ preventScroll: true });
    if (!S.editor.result) runOutcome();
  }
  function closeEditor() {
    const id = S.editor?.play_id;
    S.editor = null;
    drawDrives();
    if (id) document.querySelector(`#play-${CSS.escape(id)} [data-act="open-editor"]`)?.focus({ preventScroll: true });
  }

  // ---------- events ----------
  app.addEventListener("click", (event) => {
    const t = event.target;
    const head = t.closest(".drive__head");
    if (head) {
      const id = head.dataset.drive;
      S.open.has(id) ? S.open.delete(id) : S.open.add(id);
      drawDrives();
      document.querySelector(`.drive__head[data-drive="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
      return;
    }
    const out = t.closest("[data-out]");
    if (out) { const id = out.dataset.out; S.showOut.has(id) ? S.showOut.delete(id) : S.showOut.add(id); drawDrives(); return; }
    const action = t.closest("[data-act]");
    if (action) {
      const name = action.dataset.act;
      if (name === "open-editor") openEditor(action.dataset.play);
      else if (name === "close-editor") closeEditor();
      else if (name === "apply" && S.editor?.result) {
        const e = S.editor, found = playById(e.play_id);
        edits().set(found.drive.id, { play_id: e.play_id, request: { kind: e.kind, yards: e.yards, spot_team: e.spot_team, spot_yard: e.spot_yard }, result: e.result });
        S.editor = null;
        drawChart(); drawDrives();
        document.querySelector(`#play-${CSS.escape(e.play_id)} [data-act="open-editor"]`)?.focus({ preventScroll: true });
      } else if (name === "reset") {
        edits().delete(action.dataset.driveId);
        S.showOut.delete(action.dataset.driveId);
        S.editor = null;
        drawChart(); drawDrives();
      }
      return;
    }
    const tipButton = t.closest("[data-tip]");
    if (tipButton) {
      const kind = tipButton.dataset.tip;
      if (kind === "go") jumpToFlag(S.pinned);
      else { S.pinned = (S.pinned + (kind === "next" ? 1 : -1) + FLAGS.length) % FLAGS.length; showTip(S.pinned, true); document.querySelector(`#tip [data-tip="${kind}"]`)?.focus(); }
      return;
    }
    if (t.closest("#hit")) {
      const i = nearestFlag(event.clientX);
      if (i < 0) { S.pinned = null; hideTip(); return; }
      if (lastPointer === "touch") { S.pinned = i; showTip(i, true); } else jumpToFlag(i);
    }
  });
  let lastPointer = "mouse";
  app.addEventListener("pointerdown", (event) => { lastPointer = event.pointerType; });
  app.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch" || !event.target.closest("#hit") || S.pinned != null) return;
    const i = nearestFlag(event.clientX);
    if (i < 0) hideTip(); else showTip(i, false);
  });
  app.addEventListener("pointerleave", () => { if (S.pinned == null) hideTip(); });
  app.addEventListener("focusin", (event) => {
    const dot = event.target.closest?.(".dot");
    if (dot) { S.pinned = Number(dot.dataset.i); showTip(S.pinned, true); }
  });
  app.addEventListener("keydown", (event) => {
    const dot = event.target.closest?.(".dot");
    if (dot && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); jumpToFlag(Number(dot.dataset.i)); }
    if (event.key === "Escape") {
      if (S.pinned != null) { S.pinned = null; hideTip(); }
      else if (S.editor) closeEditor();
    }
  });
  app.addEventListener("change", (event) => {
    const t = event.target;
    if (t.name === "team") { S.team = t.value; S.pinned = null; drawChart(); drawDrives(); return; }
    if (t.id === "vegas-line") { S.vegasLine = t.checked; drawChart(); return; }
    if (t.name?.startsWith("opt-")) {
      const section = t.closest(".editor"), p = playById(section.dataset.play).play;
      chooseOption(t.value, p);
      S.editor.result = null; S.editor.error = null;
      section.querySelector("[data-input]").innerHTML = inputHtml(p);
      refreshEditor();
      runOutcome();
      return;
    }
    if (t.matches("[data-spot-team]")) { S.editor.spot_team = t.value; queueOutcome(); }
  });
  app.addEventListener("input", (event) => {
    const t = event.target;
    if (t.matches("[data-yards]")) { S.editor.yards = t.value === "" ? null : Number(t.value); queueOutcome(); }
    if (t.matches("[data-spot-yard]")) { S.editor.spot_yard = t.value === "" ? null : Number(t.value); queueOutcome(); }
  });
  document.addEventListener("click", (event) => {
    if (S.pinned != null && !event.target.closest("#tip, #chart-host, .dot")) { S.pinned = null; hideTip(); }
  });

  route();
})();
