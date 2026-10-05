/* adda viewer UI, spec 15 build step 1: shell, title block, Timeline, inspector.
   Everything is read from the /api endpoints; nothing is computed that the API
   does not state (P5: absent data is an em dash with a reason). */
(function () {
"use strict";

const VIEWS = [
  ["timeline", "Timeline"], ["hypotheses", "Hypotheses"], ["data", "Data"],
  ["deliverable", "Deliverable"], ["logs", "Logs"], ["setup", "Setup"],
];
const LANES = [
  ["literature_reviewer", "Literature"], ["datagenerator", "Data"],
  ["implementer", "Implementer"], ["critic", "Critic"],
];
const UNBUILT = {
  hypotheses: ["Hypotheses", "Every hypothesis with its prediction, falsification criterion and verdict history will appear here, arriving with build step 4."],
  data: ["Data", "The oracle store's rows, the trajectory and the funnel will appear here, arriving with build step 3."],
  deliverable: ["Deliverable", "The run's pipeline notebook, rendered, will appear here once the run has one, arriving with build step 5."],
  logs: ["Logs", "The run log, diagnostics and agent transcripts will appear here, arriving with build step 6."],
  setup: ["Setup", "The study's problem statement, configuration and launch controls will appear here, arriving with build step 7."],
};
const HOUR_PX = 84, MIN_CARD = 76, POLL_MS = 5000;
const $ = (id) => document.getElementById(id);

const S = {
  runs: [], run: null, view: "timeline", sel: null,
  vitals: null, dels: [], ledger: { hypotheses: [], milestones: [] }, fom: null,
  reviews: [], evidence: {}, follow: false, timer: null, sig: "", loaded: false,
  error: null,
};

/* ── helpers ─────────────────────────────────────────────────────────────── */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function store(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
  return null;
}
function parseT(s) { const t = Date.parse(s); return isNaN(t) ? null : t / 1000; }
function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return "—";
  sec = Math.max(0, Math.round(sec));
  if (sec >= 3600) return Math.floor(sec / 3600) + " h " + String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
  if (sec >= 60) return Math.floor(sec / 60) + " m";
  return sec + " s";
}
function fmtElapsed(sec) { return sec == null ? "—" : "+" + fmtDur(sec); }
function fmtCost(v) { return v == null ? "—" : "$" + (v >= 10 ? Math.round(v) : v.toFixed(2)); }
function fmtTokens(n) { return n == null ? "—" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n); }
function dash(reason) { return `<span title="${esc(reason)}">—</span>`; }

function delState(d) {
  const st = String(d.status || "");
  if (!d.completed_at || st === "RUNNING") return ["live", "running"];
  if (st === "DONE" || st === "GATE:PASS") return ["ok", st === "DONE" ? "done" : "pass"];
  if (st === "GATE:REVISE") return ["warn", "revise"];
  if (st === "GATE:REJECT") return ["bad", "reject"];
  if (st === "FAILED") return ["bad", "failed"];
  if (st === "FEEDBACK") return ["warn", "feedback"];
  if (st === "OPEN_FOR_REVIEW") return ["open", "open for review"];
  return ["open", st.toLowerCase() || "unknown"];
}
function hypState(h) {
  return { SUPPORTED: ["ok", "supported"], INCONCLUSIVE: ["warn", "inconclusive"],
    FALSIFIED: ["bad", "falsified"] }[h.status] || ["open", String(h.status || "open").toLowerCase()];
}
function stMark(pair) { return `<span class="st ${pair[0]}">${esc(pair[1])}</span>`; }
function runPill(status) {
  const m = { GATED: ["ok", "Gated · closed"], STOPPED: ["warn", "Stopped"], halted: ["warn", "Halted"],
    crashed: ["bad", "Crashed"], OPEN_UNAPPROVED: ["warn", "Closed · unapproved"] }[status];
  const [c, t] = m || ["live", "Running"];
  return `<span class="pill ${c}"><i></i>${t}</span>`;
}
const isGate = (d) => String(d.id).startsWith("GATE");
const isFB = (d) => String(d.id).startsWith("FB");
function known() {
  const ids = new Set();
  S.dels.forEach((d) => ids.add(d.id));
  S.ledger.hypotheses.forEach((h) => ids.add(h.id));
  return ids;
}

/* ── minimal safe markdown: escape first, then structure ─────────────────── */
function inline(t) {
  return t.replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
}
function md(src) {
  const lines = esc(src || "").replace(/\r/g, "").split("\n");
  const out = []; let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push("<pre><code>" + buf.join("\n") + "</code></pre>"); continue;
    }
    let m;
    if ((m = /^(#{1,4})\s+(.*)$/.exec(l))) { out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
    if (/^---+\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^\s*[-*]\s+/.test(l)) {
      const it = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) it.push("<li>" + inline(lines[i++].replace(/^\s*[-*]\s+/, "")) + "</li>");
      out.push("<ul>" + it.join("") + "</ul>"); continue;
    }
    if (/^\s*\d+\.\s+/.test(l)) {
      const it = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) it.push("<li>" + inline(lines[i++].replace(/^\s*\d+\.\s+/, "")) + "</li>");
      out.push("<ol>" + it.join("") + "</ol>"); continue;
    }
    if (/^\|.*\|\s*$/.test(l) && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const cells = (r) => r.replace(/^\||\|\s*$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push("<table><thead><tr>" + head.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + r.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>"); continue;
    }
    if (/^&gt;\s?/.test(l)) {
      const q = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) q.push(lines[i++].replace(/^&gt;\s?/, ""));
      out.push("<blockquote>" + inline(q.join(" ")) + "</blockquote>"); continue;
    }
    if (!l.trim()) { i++; continue; }
    const p = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*[-*]\s|\s*\d+\.\s|---+\s*$|&gt;)/.test(lines[i])) p.push(lines[i++]);
    out.push("<p>" + inline(p.join(" ")) + "</p>");
  }
  return linkify(out.join("\n"));
}
/* P4: every id is a link that selects it. Applied to text outside code. */
function linkify(html) {
  const ids = known(); let code = 0;
  return html.split(/(<[^>]+>)/).map((seg) => {
    if (seg[0] === "<") {
      if (/^<(pre|code)[ >]/.test(seg)) code++;
      else if (/^<\/(pre|code)>/.test(seg)) code = Math.max(0, code - 1);
      return seg;
    }
    return code ? seg : seg.replace(/\b(?:GATE\d+|FB\d+|D\d{3,}|H\d+)\b/g,
      (m) => ids.has(m) ? idLink(m) : m);
  }).join("");
}
function idLink(id) {
  return `<a class="idlink" href="${esc(url({ sel: id }))}" data-sel="${esc(id)}">${esc(id)}</a>`;
}

/* ── URL: view and selection live in the address (P1) ────────────────────── */
function url(over) {
  const q = new URLSearchParams();
  const o = Object.assign({ run: S.run, view: S.view, sel: S.sel }, over || {});
  if (o.run) q.set("run", o.run);
  if (o.view) q.set("view", o.view);
  if (o.sel) q.set("sel", o.sel);
  return "?" + q.toString();
}
function readUrl() {
  const q = new URLSearchParams(location.search);
  S.run = q.get("run") || S.run;
  const v = q.get("view");
  S.view = VIEWS.some((x) => x[0] === v) ? v : "timeline";
  S.sel = q.get("sel") || null;
}
function nav(over, replace) {
  if ("run" in (over || {}) && over.run !== S.run) {
    S.run = over.run; resetRun();
  }
  if (over && "view" in over) S.view = over.view;
  if (over && "sel" in over) S.sel = over.sel;
  history[replace ? "replaceState" : "pushState"](null, "", url());
  render();
  if (S.sel) ensureEvidence(S.sel);
}

/* ── data ────────────────────────────────────────────────────────────────── */
async function get(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}
function resetRun() {
  lastRun = S.run;
  S.vitals = null; S.dels = []; S.ledger = { hypotheses: [], milestones: [] };
  S.fom = null; S.reviews = []; S.evidence = {}; S.sig = ""; S.loaded = false; S.error = null;
  clearTimeout(S.timer);
  tick();
}
function visible() { return document.visibilityState === "visible"; }
async function tick() {
  clearTimeout(S.timer);
  if (!S.run) return;
  const run = S.run;
  try {
    const base = "/api/runs/" + encodeURIComponent(run);
    const [runs, vitals, dels, ledger, fom, rev] = await Promise.all([
      get("/api/runs"), get(base + "/vitals"), get(base + "/delegations"),
      get(base + "/ledger"), get(base + "/figure_of_merit"), get(base + "/critic_reviews"),
    ]);
    if (run !== S.run) return;
    S.error = null; S.loaded = true;
    S.runs = runs; S.vitals = vitals; S.dels = dels; S.ledger = ledger; S.fom = fom;
    S.reviews = rev.reviews || [];
    const sig = JSON.stringify([runs, vitals.closed, vitals.cost_usd, vitals.calls, dels, ledger, fom, S.reviews]);
    if (sig !== S.sig) { S.sig = sig; render(); }
  } catch (e) {
    S.error = String(e.message || e);
    paintNotice();
  }
  if (visible() && !(S.vitals && S.vitals.closed)) S.timer = setTimeout(tick, POLL_MS);
}
async function ensureEvidence(id) {
  const d = S.dels.find((x) => x.id === id);
  if (!d || S.evidence[id] !== undefined) return;
  S.evidence[id] = null;
  try {
    const ev = await get("/api/runs/" + encodeURIComponent(S.run) + "/evidence");
    (ev.delegations || []).forEach((e) => { S.evidence[e.id] = e; });
    paintInspector();
  } catch (e) { S.evidence[id] = false; paintInspector(); }
}

/* ── rendering ───────────────────────────────────────────────────────────── */
function render() {
  paintNav(); paintTitle(); paintNotice(); paintViews(); paintInspector(); paintWork();
}
function paintNav() {
  $("studyname").textContent = S.vitals && S.vitals.study ? S.vitals.study : "";
  $("runlist").innerHTML = S.runs.map((r) => {
    const m = { GATED: ["ok", "gated"], STOPPED: ["warn", "stopped"], halted: ["warn", "halted"],
      crashed: ["bad", "crashed"], OPEN_UNAPPROVED: ["warn", "unapproved"] }[r.status] || ["live", "running"];
    return `<a href="${esc(url({ run: r.run_id, sel: null }))}" data-run="${esc(r.run_id)}" class="${r.run_id === S.run ? "on" : ""}" title="${esc(r.run_id + " · " + m[1])}">` +
      `<span class="mono rid">${esc(r.run_id)}</span><span class="st ${m[0]}"><span class="rid">${m[1]}</span></span></a>`;
  }).join("") || '<div class="empty"><p>No runs yet. A run appears here as soon as it starts.</p></div>';
}
function liveNow() { return !!(S.vitals && !S.vitals.closed); }
function elapsedNow() {
  const v = S.vitals; if (!v) return null;
  if (!v.closed && v.started_at) { const t = parseT(v.started_at); if (t != null) return Date.now() / 1000 - t; }
  return v.elapsed_s;
}
function paintTitle() {
  const v = S.vitals, run = S.runs.find((r) => r.run_id === S.run);
  if (!v) { $("title").innerHTML = `<div><div class="lbl">Run</div><div class="runid mono">${esc(S.run || "—")}</div></div>`; return; }
  const status = run ? run.status : null;
  const budget = v.budget_s, el = elapsedNow();
  const frac = budget && el != null ? Math.min(1, el / budget) : null;
  const done = S.dels.filter((d) => delState(d)[0] === "ok").length;
  const running = S.dels.filter((d) => delState(d)[0] === "live").length;
  const fom = S.fom && S.fom.declared && S.fom.n_counted ? S.fom : null;
  const who = [v.study, v.model].filter(Boolean).join(" · ") || "—";
  const unknown = v.unknown_cost_calls ? `<small>+${v.unknown_cost_calls} unknown</small>` : "";
  $("title").innerHTML =
    `<div><div class="lbl">Run</div><div class="runid"><span class="mono">${esc(S.run)}</span>${runPill(status)}</div>` +
    `<div class="who" title="Study and model, from the study's config.yaml">${esc(who)}</div></div>` +
    `<div><div class="lbl">Elapsed</div><div class="vital" id="elapsed">${fmtDur(el)}` +
    (budget ? `<small>of ${fmtDur(budget)}</small>` : `<small title="The study's config.yaml declares no parseable budget">no budget</small>`) + `</div>` +
    (frac != null ? `<div class="clock" title="${Math.round(frac * 100)}% of the wall-clock budget"><b style="width:${(frac * 100).toFixed(1)}%"></b></div>` : "") + `</div>` +
    `<div><div class="lbl">Cost</div><div class="vital" title="Summed over metered calls; calls with no price are counted separately">${fmtCost(v.cost_usd)}${unknown}</div></div>` +
    `<div><div class="lbl">Delegations</div><div class="vital">${S.dels.filter((d) => !isGate(d) && !isFB(d)).length}` +
    `<small>${running ? done + " done · " + running + " running" : "all done"}</small></div></div>` +
    `<div><div class="lbl">Best row · ${fom ? esc(fom.column) : "objective"}</div>` +
    (fom ? `<button class="vital link mono" data-sel="row:${fom.row}" title="The store's best row by the declared objective (${esc(fom.direction)}). It is not the run's headline claim.">${esc(String(+(+fom.value).toPrecision(4)))}</button>`
      : `<div class="vital">${dash(S.fom && S.fom.declared ? "No counted rows in the store yet" : "The study declares no objective column")}</div>`) + `</div>` +
    `<div><button class="btn" disabled title="Arrives with build step 7 (write actions)">Note</button>` +
    `<button class="btn" disabled title="Arrives with build step 7 (write actions)">Stop</button>` +
    `<button class="btn" disabled title="Arrives with build step 7 (write actions)">Re-run</button></div>`;
}
function paintNotice() {
  const n = $("notice");
  n.hidden = !S.error;
  n.textContent = S.error ? "Could not reach the viewer API (" + S.error + "). Retrying while this tab is visible." : "";
}
function paintViews() {
  $("views").innerHTML = VIEWS.map(([k, t]) =>
    `<button role="tab" data-view="${k}" aria-selected="${k === S.view}" tabindex="${k === S.view ? 0 : -1}">${t}</button>`).join("") +
    `<span class="sp"></span>` +
    (S.view === "timeline" ? `<label class="toggle"><input type="checkbox" id="follow" ${S.follow ? "checked" : ""} ${liveNow() ? "" : "disabled"}> Follow live</label>` : "") +
    `<span class="hint">Press <kbd>?</kbd> for keys</span>`;
}

function paintWork() {
  const w = $("work"), top = w.scrollTop;
  if (!S.loaded) { w.innerHTML = '<div class="skel"><div></div><div></div><div></div></div>'; return; }
  if (S.view !== "timeline") {
    const [t, d] = UNBUILT[S.view];
    w.innerHTML = `<div class="empty"><h3>${t}</h3><p>${d}</p></div>`; return;
  }
  if (!S.dels.length) {
    w.innerHTML = '<div class="empty"><h3>Timeline</h3><p>Delegations will appear here as the strategizer makes them, one card per delegation on its role’s lane.</p></div>'; return;
  }
  w.innerHTML = timelineHtml();
  w.scrollTop = top;
  if (S.follow && liveNow()) w.scrollTop = w.scrollHeight;
}
function runT0() {
  const v = S.vitals;
  return (v && v.started_at && parseT(v.started_at)) ??
    Math.min(...S.dels.map((d) => parseT(d.started_at)).filter((x) => x != null));
}
function timelineHtml() {
  const v = S.vitals;
  const t0 = runT0();
  const now = Date.now() / 1000;
  const items = [];
  S.dels.forEach((d) => {
    const a = parseT(d.started_at); if (a == null) return;
    const b = d.completed_at ? parseT(d.completed_at) : now;
    if (isGate(d)) { items.push({ d, kind: "gate", a: b ?? a, b: b ?? a }); return; }
    const lane = LANES.findIndex((l) => l[0] === d.to_node);
    if (lane < 0) return;
    items.push({ d, kind: "card", lane, a, b: Math.max(b ?? a, a) });
  });
  const end = Math.max(...items.map((i) => i.b), v.closed && v.elapsed_s ? t0 + v.elapsed_s : 0, now * (v.closed ? 0 : 1));
  const hours = Math.max(1, Math.ceil((end - t0) / 3600));
  const px = (t) => ((t - t0) / 3600) * HOUR_PX;
  // pack overlapping cards into sub-columns within each lane; a lane is as wide as its busiest moment
  const laneSubs = LANES.map(() => 1);
  LANES.forEach((_, li) => {
    const col = []; const cs = items.filter((i) => i.kind === "card" && i.lane === li).sort((x, y) => x.a - y.a);
    cs.forEach((c) => {
      c.h = Math.max(MIN_CARD, px(c.b) - px(c.a));
      let k = col.findIndex((endPx) => endPx <= px(c.a) - 2);
      if (k < 0) { k = col.length; col.push(0); }
      col[k] = px(c.a) + c.h + 4; c.sub = k;
    });
    laneSubs[li] = Math.max(1, col.length);
  });
  const avail = $("work").clientWidth - 72;
  const COL_PX = Math.max(132, Math.min(176, Math.floor(avail / laneSubs.reduce((a, b) => a + b, 0))));
  const laneX = []; let acc = 0;
  laneSubs.forEach((n) => { laneX.push(acc); acc += n * COL_PX; });
  const stackW = acc;
  const totalPx = Math.max(px(end) + MIN_CARD, hours * HOUR_PX);
  const ticks = [], rules = [];
  for (let h = 0; h <= hours; h++) {
    ticks.push(`<div class="tick" style="top:${h * HOUR_PX}px">${h === 0 ? "0" : "+" + h + " h"}</div>`);
    rules.push(`<div class="hr" style="top:${h * HOUR_PX}px"></div>`);
  }
  const els = items.sort((x, y) => x.a - y.a).map((i) => {
    const d = i.d;
    if (i.kind === "gate") {
      const [c, l] = delState(d);
      const sel = S.sel === d.id ? " sel" : "";
      return `<div class="gate${sel}" style="top:${px(i.a)}px;--gc:var(--${c === "open" ? "ink-3" : c})"><span role="button" tabindex="0" data-sel="${esc(d.id)}" title="Acceptance gate · ${esc(l)}">${esc(d.id)} · ${esc(l)}</span></div>`;
    }
    const st = delState(d);
    const hs = (d.hypothesis_ids || []).map((h) => `<span class="chip h">${esc(h)}</span>`).join("");
    const fa = d.is_falsification_attempt ? '<span class="chip f" title="A falsification attempt">falsify</span>' : "";
    const cost = d.cost_usd ? `<span class="chip">${fmtCost(d.cost_usd)}</span>` : "";
    const dur = d.completed_at ? fmtDur(i.b - i.a) : fmtDur(now - i.a);
    return `<div class="card enter${S.sel === d.id ? " sel" : ""}" role="button" tabindex="0" data-sel="${esc(d.id)}" ` +
      `style="--role:var(--r-${esc(d.to_node)});top:${px(i.a)}px;height:${i.h}px;left:${laneX[i.lane] + i.sub * COL_PX + 4}px;width:${COL_PX - 8}px">` +
      `<div class="top"><span class="id">${esc(d.id)}</span>${stMark(st)}</div>` +
      `<div class="intent">${esc(firstLine(d.task))}</div><div class="chips">${fa}${hs}${cost}<span class="dur">${dur}</span></div></div>`;
  });
  const nowLine = liveNow() ? `<div class="now" style="top:${px(now)}px"><span>now ${fmtElapsed(now - t0)}</span></div>` : "";
  return `<div class="tl" style="min-width:${56 + stackW + 16}px"><div class="ruler"><div class="rs" style="height:${totalPx}px">${ticks.join("")}</div></div>` +
    `<div class="lanes" style="width:${stackW}px"><div class="lanehead" style="grid-template-columns:${laneSubs.map((n) => n * COL_PX + "px").join(" ")}">${LANES.map((l) => `<span>${l[1]}</span>`).join("")}</div>` +
    `<div class="stack" style="position:relative;height:${totalPx}px">${rules.join("")}${els.join("")}${nowLine}</div></div></div>`;
}
function firstLine(t) {
  const s = String(t || "").replace(/\s+/g, " ").trim();
  return s.length > 220 ? s.slice(0, 220) : s;
}

/* ── inspector ───────────────────────────────────────────────────────────── */
function paintInspector() {
  const body = $("body"), el = $("insp");
  const open = !!S.sel && S.loaded;
  body.classList.toggle("closed", !open);
  el.classList.toggle("open", open);
  if (!open) { el.innerHTML = ""; return; }
  el.innerHTML = inspectorHtml(S.sel);
  if (!body.querySelector(".grip")) {
    const g = document.createElement("div");
    g.className = "grip"; g.setAttribute("role", "separator"); g.setAttribute("aria-orientation", "vertical");
    g.setAttribute("aria-label", "Resize the inspector"); g.tabIndex = 0;
    body.appendChild(g);
  }
}
const sec = (h, inner) => `<div class="sec"><h3>${h}</h3>${inner}</div>`;
function head(idText, stateHtml, role, meta) {
  return `<div class="ih"><div class="row"><button class="btn ghost back" data-close="1" aria-label="Back">← Back</button>` +
    `<h2>${esc(idText)}</h2>${stateHtml}${role ? `<span class="role" style="--role:var(--r-${esc(role)})">${esc(role)}</span>` : ""}` +
    `<button class="btn ghost x" data-close="1" aria-label="Close the inspector" title="Close (Esc)">✕</button></div>` +
    (meta ? `<div class="meta">${meta}</div>` : "") + `</div>`;
}
function inspectorHtml(id) {
  if (id.startsWith("row:")) {
    const f = S.fom;
    return head(id, "", null, "A row of the oracle store") +
      sec("Why this row", f && f.declared ? `<p>The store’s best row by the declared objective <span class="mono">${esc(f.column)}</span> (${esc(f.direction)}): <b class="mono">${esc(f.value)}</b>, row ${esc(f.row)} of ${esc(f.n)}.</p>` : '<p class="none">The study declares no objective.</p>') +
      sec("Row detail", '<p class="none">The full row, the trajectory and the funnel will appear here once the Data view is built (build step 3).</p>');
  }
  const d = S.dels.find((x) => x.id === id);
  if (d) return delegationHtml(d);
  const h = S.ledger.hypotheses.find((x) => x.id === id);
  if (h) return hypothesisHtml(h);
  return head(id, "", null, "") + sec("Not found", `<p class="none">${esc(id)} is not in this run.</p>`);
}
function delegationHtml(d) {
  const st = delState(d), a = parseT(d.started_at), b = parseT(d.completed_at);
  const t0 = runT0();
  const meta = `${t0 != null && a != null ? "started " + fmtElapsed(a - t0) : "start unknown"} · ${b != null && a != null ? "took " + fmtDur(b - a) : "running"} · from ${esc(d.from_node)}`;
  const facts = `<div class="facts"><div><div class="lbl">Cost</div><b>${d.cost_usd == null ? dash("No price for this call") : fmtCost(d.cost_usd)}</b></div>` +
    `<div><div class="lbl">Tokens out</div><b>${fmtTokens(d.tokens_out)}</b></div>` +
    `<div><div class="lbl">Evals</div><b>${d.evals == null ? dash("Not recorded") : esc(d.evals)}</b></div></div>`;
  const hyps = (d.hypothesis_ids || []).map((hid) => {
    const h = S.ledger.hypotheses.find((x) => x.id === hid);
    return `<a href="${esc(url({ sel: hid }))}" data-sel="${esc(hid)}"><span class="mono">${esc(hid)}</span><span class="t">${esc(h ? firstLine(h.statement) : "")}</span>${h ? stMark(hypState(h)) : ""}</a>`;
  }).join("");
  const rv = S.reviews.find((r) => r.delegation_id === d.id);
  const ev = S.evidence[d.id];
  const files = ev && ev.files && ev.files.length
    ? `<div class="files">${ev.files.map((f) => `<div><span>${esc(f.path)}</span><span class="d">+${f.insertions} −${f.deletions}</span></div>`).join("")}</div>`
    : `<p class="none">${ev === null ? "Loading…" : ev === false ? "Could not read the workspace history." : "No files changed in this delegation’s commit."}</p>`;
  return head(d.id, stMark(st), d.to_node, meta) + facts +
    sec("Task", `<p>${esc(d.task || "")}</p>`) +
    sec(d.completed_at ? "Report" : "Report", d.deliverable ? `<div class="md">${md(d.deliverable)}</div>` : `<p class="none">${d.completed_at ? "This delegation returned no report." : "The report appears when the delegation completes."}</p>`) +
    (hyps ? sec("Hypotheses", `<div class="links">${hyps}</div>`) : "") +
    (rv ? sec("Critic review " + esc(rv.verdict || ""), `<div class="md">${md(rv.findings || "")}</div>`) : "") +
    (isGate(d) || isFB(d) ? "" : sec("Changes", files));
}
function hypothesisHtml(h) {
  const dels = S.dels.filter((d) => (d.hypothesis_ids || []).includes(h.id));
  const list = dels.map((d) => `<a href="${esc(url({ sel: d.id }))}" data-sel="${esc(d.id)}"><span class="mono">${esc(d.id)}</span><span class="t">${esc(d.to_node)}</span>${stMark(delState(d))}</a>`).join("");
  const field = (t, v) => sec(t, v ? `<p>${esc(v)}</p>` : '<p class="none">—</p>');
  return head(h.id, stMark(hypState(h)), null, h.proposed_by ? "proposed by " + esc(h.proposed_by) : "") +
    field("Statement", h.statement) + field("Prediction", h.prediction) +
    field("Falsification criterion", h.falsification_criterion) +
    (h.comment ? field("Latest comment", h.comment) : "") +
    sec("Delegations", list ? `<div class="links">${list}</div>` : '<p class="none">No delegation has been tied to this hypothesis yet.</p>');
}

/* ── interaction ─────────────────────────────────────────────────────────── */
function orderIds() {
  return S.dels.slice().sort((x, y) => String(x.started_at).localeCompare(String(y.started_at))).map((d) => d.id);
}
function setWidth(px) {
  px = Math.max(360, Math.min(640, Math.round(px)));
  document.documentElement.style.setProperty("--insp-w", px + "px");
  store("adda.inspw", String(px));
  return px;
}
document.addEventListener("click", (e) => {
  const sel = e.target.closest("[data-sel]");
  if (sel) { e.preventDefault(); nav({ sel: sel.dataset.sel }); return; }
  if (e.target.closest("[data-close]")) { nav({ sel: null }); return; }
  const tab = e.target.closest("[data-view]");
  if (tab) { nav({ view: tab.dataset.view }); return; }
  const r = e.target.closest("a[data-run]");
  if (r) { e.preventDefault(); nav({ run: r.dataset.run, sel: null }); return; }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "follow") { S.follow = e.target.checked; paintWork(); }
});
let gPending = false;
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input,textarea,select")) return;
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-sel][role=button]")) {
    e.preventDefault(); nav({ sel: e.target.dataset.sel }); return;
  }
  if (e.key === "Escape") {
    const k = document.querySelector(".keys");
    if (k) k.remove(); else if (S.sel) nav({ sel: null });
    return;
  }
  if (e.target.classList.contains("grip") && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--insp-w"), 10) || 440;
    setWidth(cur + (e.key === "ArrowLeft" ? 24 : -24)); e.preventDefault(); return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (gPending) {
    gPending = false;
    const v = { t: "timeline", h: "hypotheses", d: "data", l: "logs", s: "setup" }[e.key];
    if (v) nav({ view: v });
    return;
  }
  if (e.key === "g") { gPending = true; setTimeout(() => { gPending = false; }, 1200); return; }
  if (e.key === "j" || e.key === "k") {
    const ids = orderIds(); if (!ids.length) return;
    const i = ids.indexOf(S.sel);
    nav({ sel: ids[Math.max(0, Math.min(ids.length - 1, i + (e.key === "j" ? 1 : -1)))] }, true);
    return;
  }
  if (e.key === "?") {
    const k = document.querySelector(".keys");
    if (k) { k.remove(); return; }
    const d = document.createElement("div"); d.className = "keys";
    d.innerHTML = [["j / k", "next / previous delegation"], ["Esc", "close the inspector"], ["g t", "Timeline"],
      ["g h", "Hypotheses"], ["g d", "Data"], ["g l", "Logs"], ["g s", "Setup"], ["?", "this list"]]
      .map((r) => `<kbd>${r[0]}</kbd><span>${r[1]}</span>`).join("");
    document.body.appendChild(d);
  }
});
let drag = false;
document.addEventListener("pointerdown", (e) => {
  const g = e.target.closest(".grip"); if (!g) return;
  drag = true; g.classList.add("drag"); g.setPointerCapture(e.pointerId); e.preventDefault();
});
document.addEventListener("pointermove", (e) => {
  if (!drag) return;
  setWidth(document.documentElement.clientWidth - e.clientX);
});
document.addEventListener("pointerup", () => {
  drag = false; const g = document.querySelector(".grip"); if (g) g.classList.remove("drag");
});
window.addEventListener("popstate", () => { readUrl(); resetRunIfChanged(); render(); if (S.sel) ensureEvidence(S.sel); });
let lastRun = null;
function resetRunIfChanged() { if (S.run !== lastRun) { lastRun = S.run; resetRun(); } }
document.addEventListener("visibilitychange", () => { if (visible()) tick(); else clearTimeout(S.timer); });

$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark"; store("adda.theme", root.dataset.theme);
});
$("rail").addEventListener("click", () => {
  const on = $("app").classList.toggle("rail"); store("adda.rail", on ? "1" : "0");
  $("rail").textContent = on ? "›" : "‹";
});
setInterval(() => {
  const e = $("elapsed"); if (!e || !liveNow()) return;
  e.firstChild.nodeValue = fmtDur(elapsedNow());
}, 1000);

/* ── boot ────────────────────────────────────────────────────────────────── */
(async function boot() {
  const th = store("adda.theme"); if (th === "light" || th === "dark") document.documentElement.dataset.theme = th;
  if (store("adda.rail") === "1") { $("app").classList.add("rail"); $("rail").textContent = "›"; }
  const w = parseInt(store("adda.inspw"), 10); if (w) setWidth(w);
  readUrl();
  try {
    S.runs = await get("/api/runs");
    if (!S.run && S.runs.length) { S.run = S.runs[0].run_id; history.replaceState(null, "", url()); }
  } catch (e) { S.error = String(e.message || e); }
  paintNav(); paintViews(); paintNotice(); paintTitle(); paintWork();
  lastRun = S.run;
  if (!S.run) { $("work").innerHTML = '<div class="empty"><h3>No runs yet</h3><p>Start a run in this study and it will appear here and in the run list.</p></div>'; return; }
  await tick();
  if (S.sel) ensureEvidence(S.sel);
})();
})();
