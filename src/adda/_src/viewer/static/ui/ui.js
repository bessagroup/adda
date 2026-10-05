/* adda viewer UI, spec 15 build step 1: shell, title block, Timeline, inspector.
   Everything is read from the /api endpoints; nothing is computed that the API
   does not state (P5: absent data is an em dash with a reason). */
(function () {
"use strict";

const VIEWS = [
  ["timeline", "Timeline"], ["hypotheses", "Hypotheses"], ["data", "Data"],
  ["deliverable", "Deliverable"], ["logs", "Logs"], ["setup", "Setup"],
];
const UNBUILT = {
  hypotheses: ["Hypotheses", "This view isn't available in this version of the viewer yet."],
  deliverable: ["Deliverable", "This view isn't available in this version of the viewer yet."],
  logs: ["Logs", "This view isn't available in this version of the viewer yet."],
  setup: ["Setup", "This view isn't available in this version of the viewer yet."],
};
const HOUR_PX = 96, MIN_CARD = 26, POLL_MS = 5000;
const $ = (id) => document.getElementById(id);

const S = {
  runs: [], run: null, view: "timeline", sel: null,
  vitals: null, dels: [], ledger: { hypotheses: [], milestones: [] }, fom: null,
  reviews: [], evidence: {}, follow: false, timer: null, sig: "", loaded: false,
  error: null, questions: [], drafts: {}, errs: {}, holding: {}, toast: null, qsig: "",
  data: null, dsig: "", ns: null, sort: null, cols: null, tscroll: 0,
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
function fmtH(sec) { return sec == null ? "—" : (sec / 3600).toFixed(1) + " h"; }
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
const SHORT = { literature_reviewer: "literature", datagenerator: "datagen" };
const shortRole = (r) => SHORT[r] || r;
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
  if (wantsData()) loadData();
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
  S.fom = null; S.data = null; S.dsig = ""; S.ns = null; S.hide = {}; S.sort = null; S.tscroll = 0;
  S.reviews = []; S.evidence = {}; S.sig = ""; S.loaded = false; S.error = null; S.questions = []; S.qsig = "";
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
    const [runs, vitals, dels, ledger, fom, rev, oracle] = await Promise.all([
      get("/api/runs"), get(base + "/vitals"), get(base + "/delegations"),
      get(base + "/ledger"), get(base + "/figure_of_merit"), get(base + "/critic_reviews"), get(base + "/oracle"),
    ]);
    if (run !== S.run) return;
    // Only the viewed run is polled. /operator IS the human heartbeat (a question
    // is only worth asking someone who is looking), so polling the other runs in
    // the nav for their dots would make this tab a false heartbeat for runs
    // nobody is watching. The dot therefore marks the viewed run only.
    let qs = null;
    if (!vitals.closed && visible()) {
      try { qs = (await get(base + "/operator")).questions || []; } catch (e) { qs = null; }
      if (run !== S.run) return;
    }
    S.error = null; S.loaded = true;
    S.runs = runs; S.vitals = vitals; S.dels = dels; S.ledger = ledger; S.fom = fom;
    S.reviews = rev.reviews || []; S.oracle = oracle;
    const sig = JSON.stringify([runs, vitals.closed, vitals.cost_usd, vitals.calls, dels, ledger, fom, S.reviews, oracle]);
    if (vitals.closed) S.questions = [];
    else if (qs) S.questions = qs;
    paintBanner();
    if (sig !== S.sig) { S.sig = sig; render(); }
    if (wantsData()) loadData();
  } catch (e) {
    S.error = String(e.message || e);
    paintNotice();
  }
  if (visible() && !(S.vitals && S.vitals.closed)) S.timer = setTimeout(tick, POLL_MS);
}
function wantsData() { return S.view === "data" || (S.sel && S.sel.startsWith("row:")); }
async function loadData() {
  const run = S.run; if (!run) return;
  const base = "/api/runs/" + encodeURIComponent(run);
  try {
    const [traj, fun] = await Promise.all([get(base + "/trajectory"), get(base + "/funnel")]);
    if (run !== S.run) return;
    const sig = JSON.stringify([traj.stores.map((x) => [x.namespace, x.n, x.ts[x.n - 1]]), fun]);
    S.data = { traj, fun, cache: {} };
    if (sig === S.dsig) return;
    S.dsig = sig;
    paintWork(); paintInspector();
  } catch (e) { S.data = S.data || { error: String(e.message || e) }; paintWork(); }
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
  paintNav(); paintTitle(); paintNotice(); paintViews(); paintInspector(); paintWork(); paintBanner();
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
  const frac = budget && el != null ? Math.min(1, el / (2 * budget)) : null;
  const done = S.dels.filter((d) => delState(d)[0] === "ok").length;
  const running = S.dels.filter((d) => delState(d)[0] === "live").length;
  const fom = S.fom && S.fom.declared && S.fom.n_counted ? S.fom : null;
  const who = [v.study, v.model].filter(Boolean).join(" · ") || "—";
  const unknown = v.unknown_cost_calls ? `<small>+${v.unknown_cost_calls} unknown</small>` : "";
  $("title").innerHTML =
    `<div><div class="lbl">Run</div><div class="runid"><span class="mono">${esc(S.run)}</span>${runPill(status)}</div>` +
    `<div class="who" title="Study and model, from the study's config.yaml">${esc(who)}</div></div>` +
    `<div><div class="lbl">Wall clock</div><div class="vital" id="elapsed">${fmtH(el)}` +
    (budget ? `<small>/ ${fmtH(budget)}</small>` : `<small title="The study's config.yaml declares no parseable budget">no budget</small>`) + `</div>` +
    (frac != null ? `<div class="clock" title="Track spans 2× the budget: 1× is the budget, 1.5× no new delegations, 2× stop"><b class="${clockTone(el, budget)}" style="width:${(frac * 100).toFixed(1)}%"></b>` +
      [["1×", 50], ["1.5×", 75], ["2×", 100]].map((t) => `<u style="left:${t[1]}%"></u><em style="left:${t[1]}%">${t[0]}</em>`).join("") + `</div>` : "") + `</div>` +
    `<div><div class="lbl">Cost</div><div class="vital" title="Summed over metered calls; calls with no price are counted separately">${fmtCost(v.cost_usd)}${unknown}</div></div>` +
    `<div><div class="lbl">Delegations</div><div class="vital">${S.dels.filter((d) => !isGate(d) && !isFB(d)).length}` +
    `<small>${running ? done + " done · " + running + " running" : "all done"}</small></div></div>` +
    `<div><div class="lbl">Best row · ${fom ? esc(fom.column) : "objective"}</div>` +
    (fom ? `<button class="vital link" data-sel="row:${fom.namespace ? fom.namespace + ":" : ""}${fom.row}" title="The best counted row by the declared objective (${esc(fom.direction)}), over every store that records it. It is not the run's headline claim.">${esc(fmtVal(fom.value))}<small>${esc(fom.namespace || "canonical")} row ${fom.row}</small></button>`
      : `<div class="vital">${dash(!S.fom || !S.fom.declared ? "No objective declared for this study" : S.oracle && S.oracle.registered === false ? "No oracle registered for this run" : "No counted rows in the store yet")}</div>`) + `</div>` +
    `<div>${actionsHtml(v.closed)}</div>`;
}
function clockTone(el, budget) { return el > 2 * budget ? "bad" : el > 1.5 * budget ? "warn" : ""; }
function actionsHtml(closed) {
  const t = "This action isn't available in this version of the viewer yet.";
  return closed
    ? `<button class="btn primary" disabled title="${t}">Re-run study</button>`
    : `<button class="btn primary" disabled title="${t}">Note to run</button><button class="btn" disabled title="${t}">Stop</button>`;
}
function agoText(t) {
  const m = Math.max(0, Math.floor((Date.now() / 1000 - t) / 60));
  return m < 1 ? "just now" : m + " min ago";
}
function paintBanner() {
  const b = $("banner");
  const qs = (S.vitals && S.vitals.closed) ? [] : S.questions.filter((q) => !S.holding[q.id]);
  const sig = qs.map((q) => q.id + "|" + q.node + "|" + q.question).join("\n");
  b.hidden = !qs.length;
  const dot = qs.length ? '<span class="qdot" title="A question is waiting for you"></span>' : "";
  document.querySelectorAll(".nav a.on .qdot").forEach((e) => e.remove());
  const on = document.querySelector(".nav a.on");
  if (on && dot) on.insertAdjacentHTML("beforeend", dot);
  if (sig === S.qsig) return;
  S.qsig = sig;
  b.innerHTML = qs.map((q) =>
    `<div class="q" data-q="${esc(q.id)}"><div class="qh">Asked by <b>${esc(q.node)}</b> · <span class="ago" data-t="${esc(q.asked_at || 0)}">${agoText(q.asked_at || 0)}</span></div>` +
    `<div class="qt">${esc(q.question)}</div>` +
    `<form class="qa" data-q="${esc(q.id)}"><textarea rows="2" aria-label="Your answer to ${esc(q.id)}" placeholder="Your answer">${esc(S.drafts[q.id] || "")}</textarea>` +
    `<button class="btn primary" type="submit">Send</button></form><div class="qe" role="alert">${esc(S.errs[q.id] || "")}</div></div>`).join("");
}
function showToast(q) {
  S.toast = q.id;
  let t = $("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
  t.innerHTML = '<span>Answered · undo for <span id="tleft">10</span> s</span><button type="button" id="undo">Undo</button>';
}
function hideToast() { const t = $("toast"); if (t) t.remove(); S.toast = null; }
const sendTimers = {};
async function postAnswer(q, text) {
  clearInterval(sendTimers[q.id]); delete sendTimers[q.id];
  if (S.toast === q.id) hideToast();
  const fail = (m) => { delete S.holding[q.id]; S.drafts[q.id] = text; S.errs[q.id] = m; S.qsig = ""; paintBanner(); };
  try {
    const r = await fetch("/api/runs/" + encodeURIComponent(S.run) + "/answer", {
      method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
      body: JSON.stringify({ id: q.id, answer: text }),
    });
    if (r.status === 403 || r.status === 415) return fail("This page is read-only: open the /session?token=… URL printed when the viewer started to be allowed to write.");
    if (r.status === 409) { S.questions = S.questions.filter((x) => x.id !== q.id); delete S.holding[q.id]; S.errs[q.id] = ""; S.qsig = ""; paintBanner(); return; }
    if (!r.ok) return fail("The answer was not accepted (" + r.status + "). Your text is kept.");
    S.questions = S.questions.filter((x) => x.id !== q.id); delete S.holding[q.id]; delete S.drafts[q.id]; delete S.errs[q.id];
  } catch (e) { return fail("Could not reach the run. Your text is kept."); }
  S.qsig = ""; paintBanner();
}
function sendAnswer(q, text) {
  S.holding[q.id] = text; S.errs[q.id] = ""; delete S.drafts[q.id]; S.qsig = ""; paintBanner();
  showToast(q);
  let left = 10;
  sendTimers[q.id] = setInterval(() => {
    left -= 1; const e = $("tleft"); if (e) e.textContent = left;
    if (left <= 0) postAnswer(q, text);
  }, 1000);
}
function undoAnswer() {
  const id = S.toast; if (!id) return;
  clearInterval(sendTimers[id]); delete sendTimers[id];
  S.drafts[id] = S.holding[id]; delete S.holding[id]; hideToast(); S.qsig = ""; paintBanner();
}
function flushPending() {
  Object.keys(sendTimers).forEach((id) => { const q = S.questions.find((x) => x.id === id); if (q) postAnswer(q, S.holding[id]); });
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
  if (S.view === "data") { paintData(w); return; }
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
  const v = S.vitals, t0 = runT0(), now = Date.now() / 1000;
  const items = [];
  S.dels.forEach((d) => {
    const posted = parseT(d.started_at); if (posted == null) return;
    const done = d.completed_at ? parseT(d.completed_at) : now;
    if (isGate(d)) { items.push({ d, kind: "gate", a: done ?? posted }); return; }
    const queued = "session_started_at" in d && d.session_started_at === null;
    const begun = queued ? now : (parseT(d.session_started_at) ?? posted);
    items.push({ d, kind: "card", queued, posted, a: begun, b: Math.max(done ?? begun, begun) });
  });
  const end = Math.max(...items.map((i) => i.b ?? i.a), v.closed && v.elapsed_s ? t0 + v.elapsed_s : 0, v.closed ? 0 : now);
  const hours = Math.max(1, Math.ceil((end - t0) / 3600));
  const px = (t) => ((t - t0) / 3600) * HOUR_PX;
  // slots from the true session intervals; a card is never drawn shorter than MIN_CARD
  const cards = items.filter((i) => i.kind === "card").sort((x, y) => x.a - y.a);
  const slotEnd = [];
  cards.forEach((c) => {
    c.h = Math.max(MIN_CARD, px(c.b) - px(c.a));
    let k = slotEnd.findIndex((e) => e <= px(c.a) - 2);
    if (k < 0) { k = slotEnd.length; slotEnd.push(0); }
    slotEnd[k] = px(c.a) + c.h; c.slot = k;
  });
  const slots = Math.max(1, slotEnd.length);
  const avail = $("work").clientWidth - 72 - 2 * 16;
  const COL_PX = Math.max(150, Math.min(260, Math.floor(avail / slots)));
  const stackW = slots * COL_PX;
  const totalPx = Math.max(px(end) + MIN_CARD, hours * HOUR_PX);
  const ticks = [], rules = [];
  for (let h = 0; h <= hours; h++) {
    ticks.push(`<div class="tick" style="top:${h * HOUR_PX}px">${h === 0 ? "0" : "+" + h + " h"}</div>`);
    rules.push(`<div class="hr" style="top:${h * HOUR_PX}px"></div>`);
  }
  let lastGateY = -1e9, stagger = 0;
  const els = items.sort((x, y) => x.a - y.a).map((i) => {
    const d = i.d;
    if (i.kind === "gate") {
      const [c, l] = delState(d), y = px(i.a);
      stagger = y - lastGateY < 28 ? stagger + 1 : 0; lastGateY = y;
      return `<div class="gate${S.sel === d.id ? " sel" : ""}" style="top:${y}px;--gc:var(--${c === "open" ? "ink-3" : c})">` +
        `<span role="button" tabindex="0" data-sel="${esc(d.id)}" style="right:${stagger * 124}px" title="${esc(d.id)} · acceptance gate">gate · ${esc(l)}</span></div>`;
    }
    const st = delState(d), left = i.slot * COL_PX + 4, h = Math.round(i.h);
    const queue = i.posted < i.a - 60
      ? `<div class="queued" style="left:${left}px;width:${COL_PX - 8}px;top:${px(i.posted)}px;height:${Math.max(4, px(i.a) - px(i.posted))}px" title="${esc(d.id)} waited ${fmtDur(i.a - i.posted)} for a free slot"></div>` : "";
    const mark = st[1] === "done" ? "" : `<span class="st ${st[0]}" role="img" title="${esc(st[1])}" aria-label="${esc(st[1])}"></span>`;
    const fa = d.is_falsification_attempt ? '<span class="chip f" title="A falsification attempt">falsify</span>' : "";
    const hs = (d.hypothesis_ids || []).map((x) => `<span class="chip h">${esc(x)}</span>`).join("");
    const ev = d.evals ? `<span class="chip" title="Oracle evaluations">${esc(d.evals)} evals</span>` : "";
    const dur = i.queued ? "queued" : fmtDur(i.b - i.a);
    const body = (h >= 56 ? `<div class="intent">${esc(firstLine(d.task))}</div>` : "") +
      (h >= 84 && (fa || hs || ev) ? `<div class="chips">${fa}${hs}${ev}</div>` : "");
    return queue + `<div class="card enter${S.sel === d.id ? " sel" : ""}${i.queued ? " isqueued" : ""}" role="button" tabindex="0" data-sel="${esc(d.id)}" ` +
      `style="--role:var(--r-${esc(d.to_node)});top:${px(i.a)}px;height:${h}px;left:${left}px;width:${COL_PX - 8}px">` +
      `<div class="top"><span class="id">${esc(d.id)}</span><span class="role" title="${esc(d.to_node)}">${esc(shortRole(d.to_node))}</span>${mark}<span class="dur">${dur}</span></div>${body}</div>`;
  });
  const nowLine = liveNow() ? `<div class="now" style="top:${px(now)}px"><span>now ${fmtElapsed(now - t0)}</span></div>` : "";
  return `<div class="tl"><div class="ruler"><div class="rs" style="height:${totalPx}px">${ticks.join("")}</div></div>` +
    `<div class="lanes" style="width:${stackW}px"><div class="lanehead" style="grid-template-columns:repeat(${slots},${COL_PX}px)">` +
    Array.from({ length: slots }, (_, k) => `<span>slot ${k + 1}</span>`).join("") + `</div>` +
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
  if (id.startsWith("row:")) return rowHtml(id);
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
    `<div><div class="lbl">Out tokens</div><b>${fmtTokens(d.tokens_out)}</b></div>` +
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

/* ── Data view (spec 15 4.3) ─────────────────────────────────────────────── */
const SENTINEL = 1e8, ROW_H = 28, TBL_H = 420, HEAD_H = 44;
const unitOf = () => { const u = S.fom && S.fom.declared && S.fom.unit_label; return u && u.divide_by > 0 ? u : null; };
/* The ONE place a raw objective value becomes a display value: the chart axis,
   its reference lines, the tooltip, the table cell and the title all go through
   disp()/fmtVal(). The store and the API stay in raw units. */
function disp(v) { const u = unitOf(); return u ? v / u.divide_by : v; }
function fmtNum(x) { return x == null || typeof x !== "number" || !isFinite(x) ? "—" : String(+x.toPrecision(4)); }
function fmtVal(v) { const u = unitOf(); return fmtNum(disp(v)) + (u ? " " + u.label : ""); }
const stores = () => (S.data && S.data.traj ? S.data.traj.stores : []);
const nsOf = (st) => st.namespace || null;
function curStore() {
  const all = stores();
  return all.find((x) => nsOf(x) === S.ns) || all.find((x) => nsOf(x) === null) || all[0] || null;
}
const rowKey = (st, i) => "row:" + (nsOf(st) ? nsOf(st) + ":" : "") + i;
const ROW_RE = /^row:(?:([^:]+):)?(\d+)$/;
/* The objective is scored on every store that records its declared columns, not only the canonical one. */
const scoredList = () => (S.fom && S.fom.declared ? (S.fom.scored || []).map((x) => x.namespace || null) : []);
const declaredFor = (st) => (scoredList().includes(nsOf(st)) ? S.fom : null);
const MARKS = ["circle", "square", "diamond", "triangle"];
let DM = new Map();

function model(st) {
  const cache = S.data.cache, k = nsOf(st) || "";
  if (cache[k]) return cache[k];
  const t0 = runT0(), f = declaredFor(st), cols = [];
  cols.push({ key: "#", label: "Row", get: (i) => i, w: 64, num: true });
  cols.push({ key: "delegation", label: "Delegation", get: (i) => st.delegation[i] || null, w: 156, id: true });
  cols.push({ key: "when", label: "When", get: (i) => { const t = parseT(st.ts[i]); return t == null || t0 == null ? null : t - t0; }, fmt: fmtElapsed, w: 96, num: true });
  Object.entries(st.inputs || {}).forEach(([n, v]) => cols.push({ key: "in:" + n, label: n, group: "Inputs", get: (i) => v[i], w: 112, num: v.some((x) => typeof x === "number") }));
  Object.entries(st.columns).forEach(([n, o]) => {
    const obj = !!f && n === f.column;
    cols.push({ key: "out:" + n, label: obj ? n + (unitOf() ? " (" + unitOf().label + ")" : "") : n, group: "Outputs", get: (i) => o.values[i],
      fmt: obj ? (v) => fmtNum(disp(v)) : null, w: obj ? 184 : 128, num: true });
  });
  Object.entries(st.text || {}).forEach(([n, v]) => cols.push({ key: "tx:" + n, label: n, group: "Text", get: (i) => v[i] || null, w: n === "note" ? 280 : 112 }));
  const byKey = Object.fromEntries(cols.map((c) => [c.key, c]));
  return (cache[k] = { cols, byKey, t0 });
}
function defaultCols(st, m) {
  const f = declaredFor(st), keys = ["#", "delegation", "when"];
  if (f) { keys.push("out:" + f.column); if (f.feasible) keys.push("out:" + f.feasible); }
  else keys.push(...m.cols.filter((c) => c.group === "Outputs").slice(0, 2).map((c) => c.key));
  const fn = S.data.fun && S.data.fun.stores.find((x) => x.namespace === st.namespace);
  ((fn && fn.stages) || []).forEach((s) => keys.push("out:" + s.column));
  if (m.byKey["tx:status"]) keys.push("tx:status");
  return [...new Set(keys)].filter((k) => m.byKey[k]);
}
const colStoreKey = (st) => "adda.cols." + ((S.vitals && S.vitals.study) || "") + "." + (nsOf(st) || "");
function visibleCols(st, m) {
  let saved = null;
  try { saved = JSON.parse(store(colStoreKey(st)) || "null"); } catch (e) { saved = null; }
  const keys = Array.isArray(saved) ? saved.filter((k) => m.byKey[k]) : [];
  return (keys.length ? keys : defaultCols(st, m)).map((k) => m.byKey[k]);
}
function cellText(c, i) {
  const v = c.get(i);
  if (v == null || v === "") return "—";
  if (c.fmt) return c.fmt(v);
  return typeof v === "number" ? fmtNum(v) : String(v);
}
function tableOrder(st, m) {
  const sort = S.sort || { key: "#", dir: "desc" }, c = m.byKey[sort.key] || m.byKey["#"];
  const idx = Array.from({ length: st.n }, (_, i) => i), sg = sort.dir === "asc" ? 1 : -1;
  idx.sort((a, b) => {
    const x = c.get(a), y = c.get(b);
    if (x == null && y == null) return a - b;
    if (x == null) return 1;
    if (y == null) return -1;
    const d = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
    return d ? d * sg : a - b;
  });
  return idx;
}
function niceTicks(lo, hi, n) {
  const span = hi - lo || 1, raw = span / n, p = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw) || 10 * p;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toPrecision(12));
  return out;
}
const note = (t) => `<p class="dnote">${t}</p>`;

function markPath(kind, x, y) {
  const r = 5;
  if (kind === "square") return `M${x - r + 0.5} ${y - r + 0.5}h${2 * r - 1}v${2 * r - 1}h${-(2 * r - 1)}z`;
  if (kind === "diamond") return `M${x} ${y - r - 1}L${x + r + 1} ${y}L${x} ${y + r + 1}L${x - r - 1} ${y}z`;
  if (kind === "triangle") return `M${x} ${y - r - 1}L${x + r + 1} ${y + r}L${x - r - 1} ${y + r}z`;
  return `M${x - r + 1} ${y}a${r - 1} ${r - 1} 0 1 0 ${2 * (r - 1)} 0a${r - 1} ${r - 1} 0 1 0 ${-2 * (r - 1)} 0z`;
}
function chartHtml(W) {
  const f = S.fom;
  if (!f || !f.declared) return note("The study declares no objective, so there is no best-so-far to draw. Declare one in an <code>objective:</code> block of config.yaml.");
  const names = scoredList(), all = stores().filter((x) => names.includes(nsOf(x)));
  const notScored = (f.not_scored || []).map((x) => `${esc(x.namespace || "canonical")} (missing ${x.missing.map(esc).join(", ")})`);
  if (!all.length) return note(`No store records the declared objective <b>${esc(f.column)}</b> yet.` + (notScored.length ? ` Not scored: ${notScored.join("; ")}.` : ""));
  const t0 = runT0(), pts = []; let undrawn = 0, total = 0;
  all.forEach((st, si) => {
    const col = st.columns[f.column], fe = f.feasible && st.columns[f.feasible] ? st.columns[f.feasible].values : null;
    total += st.n;
    for (let i = 0; i < st.n; i++) {
      const y = col ? col.values[i] : null, t = parseT(st.ts[i]);
      if (y == null || Math.abs(y) >= SENTINEL || t == null || t0 == null) { undrawn++; continue; }
      pts.push({ st, si, i, x: (t - t0) / 3600, y: disp(y), ok: fe ? fe[i] === 1 : !f.feasible });
    }
  });
  const lines = (f.lines || []).map((l) => ({ y: disp(l.value), label: l.label }));
  const counted = pts.filter((p) => p.ok);
  const basis = counted.length || lines.length ? [...counted.map((p) => p.y), ...lines.map((l) => l.y)] : pts.map((p) => p.y);
  if (!pts.length) return note("No row has a finite value for the objective yet.");
  let lo = Math.min(...basis), hi = Math.max(...basis);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const hidden = S.hide || {}, vis = pts.filter((p) => !hidden[nsOf(p.st) || ""]);
  const shown = vis.filter((p) => p.y >= lo && p.y <= hi), clipped = vis.length - shown.length;
  const xEnd = Math.max(...pts.map((p) => p.x), (elapsedNow() || 0) / 3600, 0.1) * 1.02;
  const H = 300, L = 56, R = 16, T = 28, B = 32;
  const sx = (x) => L + (x / xEnd) * (W - L - R), sy = (y) => T + (1 - (y - lo) / (hi - lo)) * (H - T - B);
  const grid = niceTicks(lo, hi, 5).map((v) => `<line class="gl" x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 8}" y="${sy(v) + 4}" text-anchor="end">${fmtNum(v)}</text>`).join("") +
    niceTicks(0, xEnd, 6).map((v) => `<text x="${sx(v)}" y="${H - B + 18}" text-anchor="middle">${v === 0 ? "0" : "+" + v + " h"}</text>`).join("");
  const refs = lines.map((l) => `<line class="ref" x1="${L}" x2="${W - R}" y1="${sy(l.y)}" y2="${sy(l.y)}"/><text class="refl" x="${W - R - 4}" y="${sy(l.y) - 6}" text-anchor="end">${esc(l.label)}</text>`).join("");
  const better = f.direction === "max" ? Math.max : Math.min;
  let best = null, d = "";
  counted.slice().sort((a, b) => a.x - b.x).forEach((p) => {
    if (best == null) { best = p.y; d = `M${sx(p.x)} ${sy(best)}`; }
    else if (better(best, p.y) !== best) { d += `H${sx(p.x)}V${sy(p.y)}`; best = p.y; }
  });
  if (d) d += `H${sx(xEnd)}`;
  const dot = (p) => {
    const dm = DM.get(p.st.delegation[p.i]), key = rowKey(p.st, p.i);
    return `<path class="dot ${p.ok ? "f" : "i"}${S.sel === key ? " sel" : ""}" d="${markPath(MARKS[p.si % MARKS.length], sx(p.x), sy(p.y))}" data-sel="${key}" data-row="${p.i}" data-ns="${esc(nsOf(p.st) || "")}"` +
      (p.ok ? ` style="--role:var(${dm ? "--r-" + esc(dm) : "--ink-2"})"` : "") + `/>`;
  };
  const roles = [...new Set(counted.map((p) => DM.get(p.st.delegation[p.i])).filter(Boolean))];
  const unit = unitOf() ? unitOf().label : f.column;
  const nsKey = all.map((st, si) => `<button class="nskey" data-hide="${esc(nsOf(st) || "")}" aria-pressed="${!hidden[nsOf(st) || ""]}" title="Show or hide this store’s dots. The best-so-far line always spans every scored store."><svg width="14" height="14" viewBox="-7 -7 14 14"><path class="dot f" style="--role:var(--ink-2)" d="${markPath(MARKS[si % MARKS.length], 0, 0)}"/></svg>${esc(nsOf(st) || "canonical")}<small>${st.n}</small></button>`).join("");
  const cap = `${counted.length} counted of ${total} rows` + (all.length > 1 ? ` across ${all.length} stores` : "") + (undrawn ? ` · ${undrawn} with no finite value or time are not drawn` : "") + (clipped ? ` · ${clipped} outside the axis` : "") + (notScored.length ? ` · not scored: ${notScored.join("; ")}` : "");
  return `<div class="chartwrap"><svg class="cht" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Best ${esc(f.column)} so far over elapsed time">` +
    `<text class="ylab" x="${L - 8}" y="${T - 12}" text-anchor="start">${esc(unit)}</text>${grid}${refs}` +
    `<path class="step" d="${d}"/>${shown.filter((p) => !p.ok).map(dot).join("")}${shown.filter((p) => p.ok).map(dot).join("")}</svg><div class="tip" id="tip" hidden></div></div>` +
    `<div class="legend">${all.length > 1 ? nsKey : ""}<span><i class="dot f" style="--role:var(--ink-2)"></i>feasible, in its role’s colour</span><span><i class="dot i"></i>infeasible</span>` +
    roles.map((r) => `<span><i class="sw" style="--role:var(--r-${esc(r)})"></i>${esc(shortRole(r))}</span>`).join("") + `<span><i class="stepkey"></i>best so far (${esc(f.direction)})</span></div>` +
    `<div class="dcap">${cap}</div>`;
}
function funnelHtml(st) {
  const fn = S.data.fun && S.data.fun.stores.find((x) => x.namespace === st.namespace);
  if (!fn || !fn.stages.length) return note("No 0/1 stage columns are recorded in this store yet, so there is no funnel to draw.");
  return `<div class="funnel">` + fn.stages.map((s) =>
    `<div class="stage${fn.binding === s.column ? " bind" : ""}"><div class="sname" title="${esc(s.column)}">${esc(s.column)}</div>` +
    `<div class="snum">${s.cumulative}<small>of ${s.n}</small></div>` +
    `<div class="sbar" title="Rows passing every stage so far: ${s.cumulative}. Passing this stage alone: ${s.pass}."><i style="width:${s.n ? (100 * s.cumulative / s.n).toFixed(1) : 0}%"></i><u style="width:${s.n ? (100 * s.pass / s.n).toFixed(1) : 0}%"></u></div>` +
    `<div class="salone">${s.pass} alone${s.unrecorded ? ` · ${s.unrecorded} unrecorded` : ""}${fn.binding === s.column ? " · tightest" : ""}</div></div>`).join("") + `</div>` +
    `<div class="dcap">Each stage: rows passing every stage up to it (solid bar, large number), and rows passing it alone (line).${fn.skipped && fn.skipped.length ? " Not 0/1 in this store: " + esc(fn.skipped.join(", ")) + "." : ""}</div>`;
}
function tableHtml(st, m) {
  const cols = visibleCols(st, m), sort = S.sort || { key: "#", dir: "desc" };
  const W = cols.reduce((a, c) => a + c.w, 0), tpl = cols.map((c) => `minmax(${c.w}px,1fr)`).join(" ");
  const groups = ["Inputs", "Outputs", "Text"].map((g) => {
    const cs = m.cols.filter((c) => c.group === g); if (!cs.length) return "";
    return `<div class="pg"><h4>${g}</h4>` + cs.map((c) => `<label><input type="checkbox" data-col="${esc(c.key)}" ${cols.includes(c) ? "checked" : ""}> ${esc(c.label)}</label>`).join("") + `</div>`;
  }).join("");
  const head = cols.map((c) => `<button class="th${c.num ? " num" : ""}" data-sort="${esc(c.key)}" aria-sort="${sort.key === c.key ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}" title="Sort by ${esc(c.label)}">${esc(c.label)}<span>${sort.key === c.key ? (sort.dir === "asc" ? "↑" : "↓") : ""}</span></button>`).join("");
  return `<div class="tbar"><span class="dcap">${st.n} rows · click a header to sort · click a row to inspect it</span><span class="sp"></span>` +
    `<details class="picker"${S.pickOpen ? " open" : ""}><summary class="btn">Columns · ${cols.length} of ${m.cols.length}</summary><div class="pickmenu">${groups}</div></details></div>` +
    `<div class="tbl" id="tbl" tabindex="0" aria-label="Rows of the store"><div class="thead" style="min-width:${W}px;grid-template-columns:${tpl};height:${HEAD_H}px">${head}</div>` +
    `<div class="tbody" id="tbody" style="min-width:${W}px;height:${st.n * ROW_H}px" data-tpl="${tpl}"></div></div>`;
}
let TBL = null;
function paintRows() {
  const box = $("tbl"), body = $("tbody"); if (!box || !body || !TBL) return;
  const { st, m, order, cols } = TBL, top = Math.max(0, box.scrollTop - HEAD_H);
  const first = Math.max(0, Math.floor(top / ROW_H) - 6), last = Math.min(st.n, first + Math.ceil(TBL_H / ROW_H) + 14);
  const tpl = body.dataset.tpl, out = [];
  for (let k = first; k < last; k++) {
    const i = order[k], key = rowKey(st, i);
    out.push(`<div class="trow${S.sel === key ? " sel" : ""}" role="button" tabindex="0" data-sel="${key}" data-row="${i}" style="top:${k * ROW_H}px;grid-template-columns:${tpl}">` +
      cols.map((c) => {
        const v = cellText(c, i);
        if (c.id && c.get(i)) { const r = DM.get(c.get(i)); return `<span class="td"><a class="idlink" href="${esc(url({ sel: c.get(i) }))}" data-sel="${esc(c.get(i))}">${esc(c.get(i))}</a>${r ? `<em style="color:var(--r-${esc(r)})"> ${esc(shortRole(r))}</em>` : ""}</span>`; }
        return `<span class="td${c.num ? " num" : ""}" title="${esc(v)}">${esc(v)}</span>`;
      }).join("") + `</div>`);
  }
  body.innerHTML = out.join("");
}
function paintData(w) {
  if (!S.data) { w.innerHTML = '<div class="skel"><div></div><div></div><div></div></div>'; return; }
  if (S.data.error) { w.innerHTML = `<div class="empty"><h3>Data</h3><p>Could not read the store (${esc(S.data.error)}). Retrying.</p></div>`; return; }
  const st = curStore();
  if (!st) { w.innerHTML = '<div class="empty"><h3>Data</h3><p>This run has no oracle store yet. Rows appear here once the oracle evaluates its first design.</p></div>'; return; }
  DM = new Map(S.dels.map((d) => [d.id, d.to_node]));
  const m = model(st), all = stores();
  const seg = all.length > 1 ? `<div class="seg" role="group" aria-label="Store">` + all.map((x) =>
    `<button data-ns="${esc(nsOf(x) || "")}" aria-pressed="${nsOf(x) === nsOf(st)}">${esc(nsOf(x) || "canonical")}<small>${x.n}</small></button>`).join("") + `</div>` : "";
  const W = Math.max(320, w.clientWidth - 2 * 24);
  const f = S.fom && S.fom.declared ? S.fom : null;
  w.innerHTML = `<div class="data"><div class="dh"><h3>Best so far${f ? ` · ${esc(f.column)}` : ""}</h3>${f ? `<span class="dcap">${f.direction === "max" ? "higher" : "lower"} is better${f.feasible ? ", counting rows where " + esc(f.feasible) + " = 1" : ""}</span>` : ""}<span class="sp"></span>${seg}</div>` +
    chartHtml(W) + `<div class="dh"><h3>Stage funnel</h3></div>` + funnelHtml(st) + `<div class="dh"><h3>Store</h3></div>` + tableHtml(st, m) + `</div>`;
  const cols = visibleCols(st, m);
  TBL = { st, m, cols, order: tableOrder(st, m) };
  const box = $("tbl");
  box.scrollTop = S.tscroll || 0;
  const sel = ROW_RE.exec(S.sel || "");
  if (sel && (sel[1] || null) === nsOf(st) && S.lastRowSel !== S.sel) {
    const k = TBL.order.indexOf(+sel[2]);
    if (k >= 0) { const y = HEAD_H + k * ROW_H; if (y < box.scrollTop + HEAD_H || y > box.scrollTop + TBL_H - ROW_H) box.scrollTop = Math.max(0, y - TBL_H / 2); }
  }
  S.lastRowSel = S.sel;
  paintRows();
}
function tipHtml(st, i) {
  const m = model(st), r = DM.get(st.delegation[i]);
  const keys = visibleCols(st, m).filter((c) => c.key !== "#" && c.key !== "delegation").slice(0, 5);
  return `<b>Row ${i}</b> · ${esc(st.delegation[i] || "no delegation")}${r ? " · " + esc(shortRole(r)) : ""}` +
    keys.map((c) => `<div><span>${esc(c.label)}</span>${esc(cellText(c, i))}</div>`).join("");
}
function rowHtml(id) {
  const mm = ROW_RE.exec(id);
  if (!S.data) return head(id, "", null, "") + sec("Row", '<p class="none">Loading…</p>');
  const ns = mm && mm[1] || null, i = mm ? +mm[2] : -1;
  const st = stores().find((x) => nsOf(x) === ns);
  if (!st || i < 0 || i >= st.n) return head(id, "", null, "") + sec("Not found", `<p class="none">${esc(id)} is not in this run’s store.</p>`);
  const m = model(st), f = declaredFor(st), did = st.delegation[i], d = S.dels.find((x) => x.id === did);
  const kv = (g) => m.cols.filter((c) => c.group === g).map((c) => ({ c, v: cellText(c, i) })).filter((x) => x.v !== "—");
  const list = (g) => { const xs = kv(g); return xs.length ? `<div class="kv">` + xs.map((x) => `<span>${esc(x.c.label)}</span><b>${esc(x.v)}</b>`).join("") + `</div>` : '<p class="none">Nothing recorded for this row.</p>'; };
  let why = "";
  if (f) {
    const col = st.columns[f.column], v = col ? col.values[i] : null;
    const fe = f.feasible && st.columns[f.feasible] ? st.columns[f.feasible].values[i] : null;
    const counts = v != null && Math.abs(v) < SENTINEL && (!f.feasible || fe === 1);
    why = f.row === i && (f.namespace || null) === ns
      ? `<p>The store’s best row by the declared objective <b>${esc(f.column)}</b> (${esc(f.direction)}): <b>${esc(fmtVal(v))}</b>, row ${i} of ${st.n} in the ${ns ? esc(ns) : "canonical"} store.</p>`
      : `<p><b>${esc(f.column)}</b> = <b>${v == null ? "—" : esc(fmtVal(v))}</b>. ${counts ? "This row counts toward best-so-far." : "This row does not count: " + (v == null || Math.abs(v) >= SENTINEL ? "no finite value" : "it is infeasible") + "."}</p>`;
  }
  return head("row " + i, "", null, "A row of the " + (ns ? "“" + esc(ns) + "”" : "canonical") + " oracle store" + (m.t0 != null && st.ts[i] && parseT(st.ts[i]) != null ? " · evaluated " + fmtElapsed(parseT(st.ts[i]) - m.t0) : "")) +
    (why ? sec("Objective", why) : "") +
    sec("Produced by", did ? `<div class="links"><a href="${esc(url({ sel: did }))}" data-sel="${esc(did)}"><span class="mono">${esc(did)}</span><span class="t">${d ? esc(shortRole(d.to_node)) + " · " + esc(firstLine(d.task)) : "not in this run’s delegations"}</span>${d ? stMark(delState(d)) : ""}</a></div>` : '<p class="none">The store does not record which delegation produced this row.</p>') +
    sec("Outputs", list("Outputs")) + sec("Inputs", list("Inputs")) + (kv("Text").length ? sec("Notes", `<div class="kv">` + kv("Text").map((x) => `<span>${esc(x.c.label)}</span><b class="wrap">${esc(st.text[x.c.key.slice(3)][i])}</b>`).join("") + `</div>`) : "");
}
document.addEventListener("scroll", (e) => {
  if (e.target && e.target.id === "tbl") { S.tscroll = e.target.scrollTop; paintRows(); }
}, true);
document.addEventListener("click", (e) => {
  const ns = e.target.closest("[data-ns]");
  if (ns) { S.ns = ns.dataset.ns || null; S.tscroll = 0; S.sort = null; paintWork(); return; }
  const hd = e.target.closest("[data-hide]");
  if (hd) { S.hide = { ...(S.hide || {}), [hd.dataset.hide]: !(S.hide || {})[hd.dataset.hide] }; paintWork(); return; }
  const so = e.target.closest("[data-sort]");
  if (so) {
    const cur = S.sort || { key: "#", dir: "desc" };
    S.sort = { key: so.dataset.sort, dir: cur.key === so.dataset.sort && cur.dir === "asc" ? "desc" : "asc" };
    S.tscroll = 0; paintWork();
  }
});
document.addEventListener("change", (e) => {
  const c = e.target.closest("[data-col]"); if (!c) return;
  const st = curStore(), m = model(st), keys = visibleCols(st, m).map((x) => x.key);
  const next = c.checked ? [...keys, c.dataset.col] : keys.filter((k) => k !== c.dataset.col);
  store(colStoreKey(st), JSON.stringify(m.cols.map((x) => x.key).filter((k) => next.includes(k))));
  S.pickOpen = true; paintWork();
});
document.addEventListener("toggle", (e) => { if (e.target.classList && e.target.classList.contains("picker")) S.pickOpen = e.target.open; }, true);
document.addEventListener("pointerover", (e) => {
  const c = e.target.closest && e.target.closest(".dot[data-row]"), tip = $("tip");
  if (!tip) return;
  if (!c) { tip.hidden = true; return; }
  const st = stores().find((x) => nsOf(x) === (c.dataset.ns || null)); if (!st) return;
  tip.innerHTML = tipHtml(st, +c.dataset.row); tip.hidden = false;
  const wrap = tip.parentElement.getBoundingClientRect(), b = c.getBoundingClientRect();
  const x = b.left - wrap.left + b.width / 2, flip = x > wrap.width - 220;
  const above = b.top - wrap.top > tip.offsetHeight + 16;
  tip.style.left = (flip ? x - 12 : x + 12) + "px";
  tip.style.top = (above ? b.top - wrap.top - 8 : b.bottom - wrap.top + 8) + "px";
  tip.style.transform = `translate(${flip ? "-100%" : "0"},${above ? "-100%" : "0"})`;
});
let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (S.view === "data" && S.loaded) paintWork(); }, 150); });

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
document.addEventListener("visibilitychange", () => { if (visible()) tick(); else { clearTimeout(S.timer); flushPending(); } });
window.addEventListener("pagehide", flushPending);
$("banner").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target.closest("form"); if (!f) return;
  const q = S.questions.find((x) => x.id === f.dataset.q), text = f.querySelector("textarea").value.trim();
  if (q && text) sendAnswer(q, text);
});
$("banner").addEventListener("input", (e) => {
  const f = e.target.closest("form"); if (f) S.drafts[f.dataset.q] = e.target.value;
});
$("banner").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { const f = e.target.closest("form"); if (f) f.requestSubmit(); }
});
document.addEventListener("click", (e) => { if (e.target.id === "undo") undoAnswer(); });

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
  document.querySelectorAll("#banner .ago").forEach((a) => { a.textContent = agoText(+a.dataset.t); });
  const e = $("elapsed"); if (!e || !liveNow()) return;
  e.firstChild.nodeValue = fmtH(elapsedNow());
  const b = document.querySelector(".clock b"), bud = S.vitals && S.vitals.budget_s;
  if (b && bud) { b.className = clockTone(elapsedNow(), bud); b.style.width = (Math.min(1, elapsedNow() / (2 * bud)) * 100).toFixed(1) + "%"; }
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
