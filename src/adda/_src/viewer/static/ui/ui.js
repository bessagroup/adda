/* adda viewer UI, spec 15 build step 1: shell, title block, Timeline, inspector.
   Everything is read from the /api endpoints; nothing is computed that the API
   does not state (P5: absent data is an em dash with a reason). */
(function () {
"use strict";

const VIEWS = [
  ["timeline", "Timeline"], ["hypotheses", "Hypotheses"], ["data", "Data"],
  ["deliverable", "Deliverable"], ["logs", "Logs"], ["setup", "Setup"],
];
const UNBUILT = {};
const HOUR_PX = 96, MIN_CARD = 26, BREAK_S = 1800, BREAK_PX = 24, BREAK_LEAD = 16, POLL_MS = 5000;
const $ = (id) => document.getElementById(id);

const S = {
  runs: [], run: null, view: "timeline", sel: null,
  vitals: null, dels: [], ledger: { hypotheses: [], milestones: [] }, fom: null,
  reviews: [], evidence: {}, follow: false, timer: null, sig: "", loaded: false,
  error: null, questions: [], drafts: {}, errs: {}, holding: {}, toast: null, qsig: "",
  data: null, dsig: "", ns: undefined, sort: null, cols: null, tscroll: 0,
  nb: null, nbsig: "", nbrun: null, nbpick: null, codeOpen: false, hopen: {},
  log: { run: null, src: "run", paused: false, err: null, bufs: {} },
  rx: { state: "idle", run: null, lines: [], result: null, open: false },
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
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
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
  if (S.view === "deliverable") loadNotebook();
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
  S.nb = null; S.nbsig = ""; S.nbrun = null; S.nbpick = null; S.log = { run: null, src: S.log.src, paused: S.log.paused, err: null, bufs: {} };
  S.fom = null; S.data = null; S.dsig = ""; S.ns = undefined; S.xmode = "eval"; S.logy = false; S.sort = null; S.tscroll = 0;
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
    if (S.view === "deliverable") loadNotebook();
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
  paintNav(); paintTitle(); paintNotice(); paintViews(); paintInspector(); paintWork(); paintBanner(); paintDrawer();
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
    ? `<button class="btn primary" id="openstart" title="Run the pre-flight checks and start a new run of this study">Re-run study</button>`
    : `<button class="btn primary" disabled title="${t}">Note to run</button>` +
      (S.pop.stopped[S.run] ? `<button class="btn" disabled title="A stop was requested; the run closes after its retrospectives">Stop requested</button>`
        : `<button class="btn" data-stop aria-haspopup="dialog" aria-expanded="${S.pop.open}">Stop</button>`);
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
  if (S.view === "hypotheses") { w.innerHTML = hypothesesHtml(); markClamps(w); return; }
  if (S.view === "deliverable") { paintDeliverable(w, top); return; }
  if (S.view === "logs") { paintLogs(w, !!$("logbody")); loadLogs(); return; }
  if (S.view === "setup") { if ($("sutext")) paintSetupParts(); else paintSetup(); loadSetup(); return; }
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
  // Spans longer than BREAK_S in which nothing runs (and no gate falls) are drawn as a BREAK_PX band.
  const busy = items.map((i) => [i.a, i.kind === "gate" ? i.a : i.b]).sort((x, y) => x[0] - y[0]);
  const breaks = [];
  let cursor = t0;
  busy.forEach(([a, b]) => {
    if (a - cursor > BREAK_S) breaks.push({ from: cursor, to: a });
    cursor = Math.max(cursor, b);
  });
  if (end - cursor > BREAK_S) breaks.push({ from: cursor, to: end });
  breaks.forEach((k) => { k.saved = ((k.to - k.from) / 3600) * HOUR_PX - BREAK_PX - BREAK_LEAD; });
  const px = (t) => {
    let y = ((t - t0) / 3600) * HOUR_PX;
    for (const k of breaks) {
      if (t >= k.to) y -= k.saved;
      else if (t > k.from) y -= ((t - k.from) / (k.to - k.from)) * k.saved;
    }
    return y;
  };
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
  const totalPx = Math.max(px(end) + MIN_CARD, px(t0 + hours * 3600));
  const ticks = [], rules = [];
  for (let h = 0; h <= hours; h++) {
    const t = t0 + h * 3600;
    if (breaks.some((k) => t > k.from && t < k.to)) continue;
    const y = px(t);
    ticks.push(`<div class="tick" style="top:${y}px">${h === 0 ? "0" : "+" + h + " h"}</div>`);
    rules.push(`<div class="hr" style="top:${y}px"></div>`);
  }
  const bands = breaks.map((k) => `<div class="brk" style="top:${px(k.from) + BREAK_LEAD}px;height:${BREAK_PX}px"><span>${esc(fmtDur(k.to - k.from)).replace(/^(\d+ h \d+)$/, '$1 m')} with no delegation running</span></div>`);
  const placed = [];
  const els = items.sort((x, y) => x.a - y.a).map((i) => {
    const d = i.d;
    if (i.kind === "gate") {
      const [c, l] = delState(d), y = px(i.a);
      const w = 22 + 7 * ("gate · " + l).length;
      let off = 0, moved = true;
      while (moved) {
        moved = false;
        for (const q of placed) {
          if (Math.abs(q.y - y) < 24 && off < q.off + q.w + 6 && off + w + 6 > q.off) { off = q.off + q.w + 6; moved = true; }
        }
      }
      placed.push({ y, off, w });
      return `<div class="gate${S.sel === d.id ? " sel" : ""}" style="top:${y}px;--gc:var(--${c === "open" ? "ink-3" : c})">` +
        `<span role="button" tabindex="0" data-sel="${esc(d.id)}" style="right:${off}px" title="${esc(d.id)} · acceptance gate">gate · ${esc(l)}</span></div>`;
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
    `<div class="stack" style="position:relative;height:${totalPx}px">${rules.join("")}${bands.join("")}${els.join("")}${nowLine}</div></div></div>`;
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
const hypClosed = (h) => h.status !== "OPEN";
const hypRetracted = (h) => (h.retractions || 0) > 0;
function hypDelegations(h) {
  const ids = new Set(S.dels.filter((d) => (d.hypothesis_ids || []).includes(h.id)).map((d) => d.id));
  (h.status_log || []).forEach((e) => { if (e.delegation) ids.add(e.delegation); });
  return [...ids];
}
const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
function beliefBar(h) {
  const prior = h.prior, post = h.posterior;
  const tip = `Prior ${prior == null ? "not recorded" : prior} → ${h.status === "OPEN" ? "current belief" : "posterior"} ${post == null ? "not recorded" : post}. The tick marks the prior.`;
  return `<div class="belief" title="${esc(tip)}"><div class="bar"><i style="width:${post == null ? 0 : post * 100}%"></i>` +
    (prior == null ? "" : `<b style="left:${prior * 100}%"></b>`) + `</div><span>${pct(prior)} → ${pct(post)}</span></div>`;
}
function hypothesesHtml() {
  const all = S.ledger.hypotheses;
  if (!all.length) {
    return '<div class="empty"><h3>Hypotheses</h3><p>No hypothesis has been stated yet. The strategizer states each one with a prior and a falsification criterion before testing it, and it appears here with its verdict and the delegations that tested it.</p></div>';
  }
  const f = S.hfilter || "all";
  const sets = { all: all, open: all.filter((h) => !hypClosed(h)), closed: all.filter(hypClosed), retracted: all.filter(hypRetracted) };
  const chips = `<div class="seg" role="group" aria-label="Filter hypotheses">` + ["all", "open", "closed", "retracted"].map((k) =>
    `<button data-hfilter="${k}" aria-pressed="${f === k}">${k}<small>${sets[k].length}</small></button>`).join("") + `</div>`;
  const none = { open: "Every hypothesis has a verdict; none is open.", closed: "No hypothesis has a verdict yet.",
    retracted: "No hypothesis has been retracted. A retraction is a verdict the strategizer later withdrew, returning the hypothesis to open." }[f];
  const rows = sets[f].map((h) => {
    const dels = hypDelegations(h).map((id) => `<a class="mono" href="${esc(url({ sel: id }))}" data-sel="${esc(id)}">${esc(id)}</a>`).join("");
    return `<div class="hrow${S.sel === h.id ? " sel" : ""}${S.hopen[h.id] ? " open" : ""}" role="button" tabindex="0" data-sel="${esc(h.id)}">` +
      `<span class="mono hid">${esc(h.id)}</span>` +
      `<div class="hst"><div class="clamp">${esc(h.statement || "")}</div><button type="button" class="more" data-hmore="${esc(h.id)}" hidden>more</button>${hypRetracted(h) ? ` <span class="chip retr" title="This hypothesis was returned to open after a verdict.">retracted${h.retractions > 1 ? " ×" + h.retractions : ""}</span>` : ""}</div>` +
      `<div class="hv">${stMark(hypState(h))}</div>${beliefBar(h)}` +
      `<div class="hd">${dels || '<span class="none">none yet</span>'}</div></div>`;
  }).join("");
  return `<div class="data hyps"><div class="dh"><h3>Hypotheses</h3><span class="dcap">${all.length} stated · ${sets.open.length} open · ${sets.closed.length} with a verdict</span><span class="sp"></span>${chips}</div>` +
    (rows ? `<div class="hlist">${rows}</div>` : `<p class="dnote">${none}</p>`) + `</div>`;
}
function markClamps(w) {
  w.querySelectorAll(".hrow").forEach((row) => {
    const c = row.querySelector(".clamp"), b = row.querySelector(".more");
    if (!c || !b) return;
    const open = row.classList.contains("open");
    b.hidden = !(open || c.scrollHeight > c.clientHeight + 1);
    b.textContent = open ? "less" : "more";
  });
}
function historyHtml(h) {
  const log = h.status_log || [], t0 = runT0();
  if (!log.length) return '<p class="none">No status has been recorded.</p>';
  return `<ol class="hist">` + log.map((e) => {
    const t = parseT(e.ts), when = t != null && isFinite(t0) ? fmtElapsed(t - t0) : "";
    const d = e.delegation ? `<a class="mono" href="${esc(url({ sel: e.delegation }))}" data-sel="${esc(e.delegation)}">${esc(e.delegation)}</a>` : "";
    return `<li class="${e.retraction ? "stepback" : e.revision ? "revised" : ""}"><div class="hh">${e.retraction ? '<span class="arrow" aria-hidden="true">↩</span>' : e.revision ? '<span class="arrow rev" aria-hidden="true">↘</span>' : ""}${stMark(hypState(e))}` +
      `${e.retraction ? '<span class="chip retr">retracted</span>' : e.revision ? '<span class="chip rev" title="The verdict was weakened to inconclusive, not withdrawn.">revised</span>' : ""}<span class="pp">${e.posterior == null ? "" : "belief " + pct(e.posterior)}</span><span class="sp"></span><span class="when">${esc(when)}</span></div>` +
      (e.comment ? `<p>${esc(e.comment)}</p>` : "") +
      (d || e.triggered_by ? `<div class="by">${d ? "evidence " + d : ""}${d && e.triggered_by ? " · " : ""}${e.triggered_by ? "after " + esc(e.triggered_by) : ""}</div>` : "") +
      (e.validator_note ? `<div class="vnote"><b>Validator</b> ${esc(e.validator_note)}</div>` : "") + `</li>`;
  }).join("") + `</ol>`;
}
function hypothesisHtml(h) {
  const list = hypDelegations(h).map((id) => {
    const d = S.dels.find((x) => x.id === id);
    return `<a href="${esc(url({ sel: id }))}" data-sel="${esc(id)}"><span class="mono">${esc(id)}</span><span class="t">${esc(d ? d.to_node : "not in this run’s delegations")}</span>${d ? stMark(delState(d)) : ""}</a>`;
  }).join("");
  const field = (t, v) => sec(t, v ? `<p>${esc(v)}</p>` : '<p class="none">—</p>');
  return head(h.id, stMark(hypState(h)), null, (h.proposed_by ? "proposed by " + esc(h.proposed_by) : "") + (h.prior != null ? (h.proposed_by ? " · " : "") + "prior " + pct(h.prior) : "")) +
    field("Statement", h.statement) + field("Falsification criterion", h.falsification_criterion) + field("Prediction", h.prediction) +
    sec("Status history", historyHtml(h)) +
    sec("Delegations", list ? `<div class="links">${list}</div>` : '<p class="none">No delegation has been tied to this hypothesis yet.</p>');
}

/* ── Deliverable view (spec 15 4.3) ──────────────────────────────────────── */
let katexP = null;
function loadKatex() {
  if (window.katex) return Promise.resolve(true);
  if (!katexP) {
    katexP = new Promise((res) => {
      const l = document.createElement("link");
      l.rel = "stylesheet"; l.href = "/static/vendor/katex/katex.min.css"; document.head.appendChild(l);
      const sc = document.createElement("script");
      sc.src = "/static/vendor/katex/katex.min.js"; sc.onload = () => res(true); sc.onerror = () => res(false);
      document.head.appendChild(sc);
    });
  }
  return katexP;
}
function texHtml(tex, display) {
  if (window.katex) {
    try { return window.katex.renderToString(tex, { displayMode: display, throwOnError: false }); } catch (e) { /* fall through to the source */ }
  }
  return `<code class="tex">${esc(tex)}</code>`;
}
/* Math is lifted out before the markdown pass (which escapes angle brackets) and put back after. */
function mdMath(src) {
  const math = [];
  const stash = (tex, display) => { math.push([tex, display]); return "\u0001" + (math.length - 1) + "\u0002"; };
  const text = String(src || "").replace(/<!--[\s\S]*?-->\n?/g, "").split(/(```[\s\S]*?```|`[^`\n]*`)/).map((p, i) => (i % 2 ? p : p
    .replace(/\$\$([\s\S]+?)\$\$/g, (m, x) => stash(x, true))
    .replace(/\\\[([\s\S]+?)\\\]/g, (m, x) => stash(x, true))
    .replace(/\\\(([\s\S]+?)\\\)/g, (m, x) => stash(x, false))
    .replace(/(^|[^\\$\w])\$(?!\s)([^$\n]*[^$\s\\])\$(?![\w$])/g, (m, a, x) => a + stash(x, false)))).join("");
  return md(text).replace(/\u0001(\d+)\u0002/g, (m, n) => texHtml(math[n][0], math[n][1]));
}
const TBL_OK = new Set(["TABLE", "THEAD", "TBODY", "TFOOT", "TR", "TH", "TD", "CAPTION"]);
/* A notebook's HTML output is untrusted: rebuild only its tables, from escaped text. */
function safeTable(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const walk = (n) => {
    if (n.nodeType === 3) return esc(n.nodeValue);
    if (n.nodeType !== 1 || n.tagName === "SCRIPT" || n.tagName === "STYLE") return "";
    const kids = [...n.childNodes].map(walk).join("");
    if (!TBL_OK.has(n.tagName)) return kids;
    const t = n.tagName.toLowerCase();
    const span = ["colspan", "rowspan"].map((a) => (/^\d{1,3}$/.test(n.getAttribute(a) || "") ? ` ${a}="${n.getAttribute(a)}"` : "")).join("");
    return `<${t}${span}>${kids}</${t}>`;
  };
  return [...doc.body.querySelectorAll("table")].filter((t) => !t.parentElement.closest("table"))
    .map((t) => `<div class="tbl">${walk(t)}</div>`).join("");
}
const ANSI = /\u001b\[[0-9;]*[A-Za-z]/g;
function nbOutput(o) {
  const pre = (t, cls) => `<pre class="nbout${cls ? " " + cls : ""}">${esc(String(t || "").replace(ANSI, ""))}</pre>`;
  if (o.kind === "stream") return pre(o.text, o.name === "stderr" ? "err" : "");
  if (o.kind === "error") return pre(o.text || o.ename + ": " + o.evalue, "err");
  if (o.kind === "image") return `<figure class="nbfig"><img alt="Figure produced by the code cell above" src="data:${esc(o.mime)};base64,${esc(String(o.data).replace(/\s/g, ""))}"></figure>`;
  if (o.kind === "image_too_large") return '<p class="none">A figure here is too large to show (over 4 MB).</p>';
  if (o.kind === "text") return (o.html && safeTable(o.html)) || pre(o.text);
  return "";
}
function nbCell(c) {
  if (c.type === "markdown") return `<section class="md nbmd">${mdMath(c.source)}</section>`;
  const n = (c.source || "").split("\n").length;
  const code = (c.source || "").trim()
    ? `<details class="nbcode"${S.codeOpen ? " open" : ""}><summary>Code · ${n} line${n === 1 ? "" : "s"}</summary><pre>${esc(c.source)}</pre></details>` : "";
  return `<section class="nbcell">${code}${(c.outputs || []).map(nbOutput).join("")}</section>`;
}
const sameNum = (a, b) => { const x = parseFloat(a), y = parseFloat(b); return isFinite(x) && isFinite(y) && Math.abs(x - y) <= 1e-9 + 1e-3 * Math.abs(y); };
/* The run's own headline: what the notebook prints, never the store's best row (spec 15 Q1). */
function headlineHtml(nb) {
  const h = nb.headline || {}, src = nb.source && nb.source !== "stored" ? (nb.reexecutions || []).find((x) => x.id === nb.source) : null;
  const rx = S.rx.run === S.run && S.rx.state === "failed" && S.rx.result && S.rx.result.reproduced != null ? S.rx.result : null;
  const notes = [];
  let value;
  if (h.reproduced != null) {
    value = `<span class="mono">${esc(h.reproduced)}</span>`;
    if (src) notes.push(`From re-execution at ${esc(fmtStamp(src.finished_at))}. The notebook printed it as <code>REPRODUCED</code>, as written, with no unit conversion.`);
    else notes.push("Printed by the notebook as <code>REPRODUCED</code>, as written, with no unit conversion.");
    notes.push("This is the run’s own answer; the best row in the Data view is a different number, taken from the store.");
    if (h.claimed != null) notes.push(sameNum(h.claimed, h.reproduced)
      ? "The write-up states the same value."
      : `<span class="st warn">differs</span> The write-up states <span class="mono">${esc(h.claimed)}</span> (<code>CLAIMED_HEADLINE</code>).`);
  } else {
    const kept = nb.cells.some((c) => (c.outputs || []).length), ran = (nb.reexecutions || []).length > 0;
    value = dash("No REPRODUCED line in the notebook");
    notes.push((kept ? "The displayed outputs hold no <code>REPRODUCED</code> line."
      : ran ? "The stored notebook carries no outputs, and no re-execution has printed a <code>REPRODUCED</code> line."
      : "The stored notebook carries no outputs, so no <code>REPRODUCED</code> line.") +
      (ran ? "" : " Re-execute prints the headline if the notebook computes one.") + " Nothing is substituted from the store.");
  }
  if (rx) notes.push(`<span class="st bad">failed</span> The latest re-execution failed after printing <span class="mono">${esc(rx.reproduced)}</span>; it is a partial result and is not shown as the headline.`);
  return `<div class="nbhead"><div><div class="lbl">Notebook headline</div><div class="hv">${value}</div></div><div class="nhn">${notes.map((x) => `<p>${x}</p>`).join("")}</div></div>`;
}
function fmtStamp(t) { const d = new Date(t); return isNaN(d) ? String(t || "") : d.toLocaleString(); }
function nbSwitch(nb) {
  const all = nb.reexecutions || [];
  if (!all.length) return "";
  const last = all.find((x) => x.passed) || all[0], cur = nb.source || "stored";
  const b = (id, label, on) => `<button type="button" data-pick="${esc(id)}" aria-pressed="${on}">${label}</button>`;
  return `<span class="seg" role="group" aria-label="Which notebook is shown">${b("stored", "Stored", cur === "stored")}${b(last.id, `Last re-execution (${esc(fmtStamp(last.finished_at))})${last.passed ? "" : " · failed"}`, cur === last.id)}</span>`;
}
function rxMark() {
  if (S.rx.run !== S.run) return "";
  return { running: '<span class="st live">re-executing</span>', passed: '<span class="st ok">passed</span>', failed: '<span class="st bad">failed</span>' }[S.rx.state] || "";
}
function paintDeliverable(w, top) {
  const nb = S.nb;
  if (!nb || S.nbrun !== S.run) { w.innerHTML = '<div class="skel"><div></div><div></div><div></div></div>'; return; }
  if (nb.missing || nb.error) {
    w.innerHTML = `<div class="empty"><h3>Deliverable</h3><p>${nb.error ? esc(nb.error) : "This run has no pipeline notebook. The implementer writes it before the run can close, so a run that never wrote one shows nothing here; that is a finding about the run."}</p></div>`;
    return;
  }
  const name = String(nb.path || "").split("/").pop();
  const running = S.rx.state === "running" && S.rx.run === S.run;
  w.innerHTML = `<div class="data nb"><div class="dh"><h3>Deliverable</h3><span class="dcap">${esc(name)} · ${nb.cells.length} cell${nb.cells.length === 1 ? "" : "s"}</span><span class="sp"></span>` +
    nbSwitch(nb) + `<label class="toggle"><input type="checkbox" id="nbcode" ${S.codeOpen ? "checked" : ""}> Show code</label>${rxMark()}` +
    `<button type="button" class="btn" id="reexec" ${running ? "disabled" : ""} title="Run the notebook again against a copy of this run’s ledger and check that it reproduces.">Re-execute</button></div>` +
    headlineHtml(nb) + nb.cells.map(nbCell).join("") + `</div>`;
  w.scrollTop = top;
}
/* ── Logs: a tail of the run's own files, one source at a time ───────────── */
const DEFAULT_SOURCES = [{ name: "run", label: "Run log", kind: "file" }, { name: "diagnostics", label: "Monitor", kind: "file" }];
const logSources = () => (S.log.sources && S.log.run === S.run ? S.log.sources : DEFAULT_SOURCES);
const logSource = (n) => logSources().find((x) => x.name === n) || { name: n, label: n, kind: "file" };
const LOG_CAP = 5000, LOG_MS = 2000;
const logBuf = (src) => S.log.bufs[src] || (S.log.bufs[src] = { lines: [], cursor: null, exists: null, partial: "", trimmed: false, n: 0 });
function logLine(src, raw) {
  if (src === "diagnostics") {
    let r; try { r = JSON.parse(raw); } catch (e) { return { t: "", lvl: "", msg: raw }; }
    const d = new Date(r.ts), t = isNaN(d) ? "" : d.toTimeString().slice(0, 8);
    return { t, lvl: r.error_type || "", msg: [r.node, r.tool, String(r.message || "").trim().split("\n")[0]].filter(Boolean).join(" · ") };
  }
  if (src.startsWith("out:")) return { t: "", lvl: "", msg: raw };
  const m = /^\[(\d\d:\d\d:\d\d)\]\s+([A-Z]+)\s+(.*)$/.exec(raw);
  return m ? { t: m[1], lvl: m[2], msg: m[3] } : { t: "", lvl: "", msg: raw };
}
function lvlClass(l) { return /^(ERROR|CRITICAL|ERROR_RETURN)$/i.test(l) ? "bad" : /^(WARN|WARNING)$/.test(l) || /_(WARN|FLAG|NUDGE)$/.test(l) ? "warn" : "dim"; }
let logBusy = false;
let srcBusy = false;
async function loadSources() {
  const run = S.run; if (!run || srcBusy) return;
  if (S.log.run === run && S.log.sources && Date.now() - S.log.srcAt < 6000) return;
  srcBusy = true;
  try {
    const r = await get("/api/runs/" + encodeURIComponent(run) + "/log_sources");
    if (run !== S.run) return;
    const changed = JSON.stringify(r.sources) !== JSON.stringify(S.log.sources);
    S.log.run = run; S.log.sources = r.sources; S.log.srcAt = Date.now();
    if (!r.sources.some((x) => x.name === S.log.src)) S.log.src = "run";
    if (changed) paintLogs($("work"));
  } catch (e) { /* the file sources still work from their defaults */ } finally { srcBusy = false; }
}
async function loadTools() {
  const run = S.run, b = logBuf("tools");
  logBusy = true;
  try {
    const q = b.since == null ? "" : "?since=" + (b.since - 30);
    const r = await get("/api/runs/" + encodeURIComponent(run) + "/tool_calls" + q);
    if (run !== S.run || S.log.src !== "tools") return;
    S.log.err = null; b.stale = false; b.exists = true;
    b.ids = b.ids || new Set();
    const idx = new Map(b.lines.map((l, i) => [l.id, i]));
    let grew = false;
    r.calls.forEach((c) => {
      const d = c.t == null ? "" : new Date(c.t * 1000).toTimeString().slice(0, 8);
      const row = { id: c.id, tt: c.t || 0, t: d, lvl: c.ok === true ? "ok" : c.ok === false ? "error" : "…", msg: c.who.split("/")[0] + " · " + c.tool };
      if (idx.has(c.id)) { b.lines[idx.get(c.id)] = row; grew = true; return; }
      b.lines.push(row); grew = true;
      if (c.t != null && (b.since == null || c.t > b.since)) b.since = c.t;
    });
    if (grew) b.lines.sort((x, y) => x.tt - y.tt);
    if (b.lines.length > LOG_CAP) { b.lines.splice(0, b.lines.length - LOG_CAP); b.trimmed = true; }
    b.n = b.lines.length; b.cursor = b.since;
    paintLogs($("work"), true);
  } catch (e) {
    if (/404/.test(String(e.message || e))) { b.exists = false; b.cursor = 0; } else S.log.err = String(e.message || e);
    paintLogs($("work"), true);
  } finally { logBusy = false; }
}
async function loadLogs() {
  loadSources();
  const run = S.run, src = S.log.src; if (!run || logBusy) return;
  if (logSource(src).kind === "tools") {
    const b = logBuf(src);
    if (b.exists !== null && (S.log.paused || (S.vitals && S.vitals.closed && b.cursor != null && !b.stale))) return;
    return loadTools();
  }
  const b = logBuf(src);
  if (S.log.run !== run) { S.log.run = run; }
  if (b.exists !== null && (S.log.paused || (S.vitals && S.vitals.closed && b.cursor != null && !b.stale))) return;
  logBusy = true;
  try {
    const q = b.cursor == null ? "" : "&after=" + b.cursor + "&limit=1048576";
    const r = await get("/api/runs/" + encodeURIComponent(run) + "/log?name=" + src + q);
    if (run !== S.run || S.log.src !== src) return;
    S.log.err = null; b.stale = false;
    if (r.reset) { b.lines = []; b.partial = ""; b.n = 0; }
    const first = b.cursor == null || r.reset;
    const had = b.exists === null || r.text.length > 0 || r.reset;
    b.exists = r.exists;
    if (r.text || r.reset) {
      const parts = (b.partial + r.text).split("\n");
      b.partial = parts.pop();
      if (first && r.next_cursor - new TextEncoder().encode(r.text).length > 0) { parts.shift(); b.trimmed = true; }
      parts.filter((x) => x.length).forEach((x) => b.lines.push(logLine(src, x)));
      if (b.lines.length > LOG_CAP) { b.lines.splice(0, b.lines.length - LOG_CAP); b.trimmed = true; }
      b.n = b.lines.length;
    }
    b.cursor = r.next_cursor;
    if (had) paintLogs($("work"), true);
  } catch (e) { S.log.err = String(e.message || e); paintLogs($("work"), true); } finally { logBusy = false; }
}
function logRows(b) {
  return b.lines.map((l) => `<div class="ll"><span class="lt mono">${esc(l.t)}</span>` +
    `<span class="lv ${lvlClass(l.lvl)}">${esc(l.lvl)}</span><span class="lm">${linkify(esc(l.msg))}</span></div>`).join("");
}
function logNote(b, src) {
  if (S.log.err) return `<p class="none">Could not read the log (${esc(S.log.err)}).</p>`;
  const o = src.name.startsWith("out:") ? `<p class="dcap">Output of ${idLink(src.delegation)} · <span class="mono">${esc(src.name.slice(4))}</span></p>` : "";
  if (b.exists === false) return o + `<p class="none">${src.kind === "tools" ? "This run recorded no transcripts, so there are no tool calls to list." : src.name === "watchdog" ? "No watchdog log is recorded for this run." : "This run has not written " + esc(src.label) + " yet."}</p>`;
  if (b.exists === null) return o;
  return o + (b.trimmed ? `<p class="dcap">Showing the latest lines only; earlier ones are on disk.</p>` : "");
}
function paintLogs(w, partial) {
  if (S.view !== "logs") return;
  const src = logSource(S.log.src), b = logBuf(S.log.src), live = liveNow();
  if (partial && !$("logbody")) return;
  const body = $("logbody"), stick = !body || body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
  const note = logNote(b, src);
  const rows = b.lines.length ? logRows(b) : (b.exists ? '<p class="none">' + (src.kind === "tools" ? "No tool calls yet." : "The file is empty so far.") + "</p>" : "");
  const state = S.log.paused ? '<span class="st open">paused</span>' : live ? '<span class="st live">live</span>' : '<span class="st open">run closed</span>';
  if (partial && body && $("logstate")) {
    $("logstate").innerHTML = state; $("logn").textContent = b.lines.length + " " + (src.kind === "tools" ? "calls" : "lines"); $("lognote").innerHTML = note;
    body.className = "logbody" + (live ? "" : " fit"); body.innerHTML = rows; if (stick) body.scrollTop = body.scrollHeight;
    return;
  }
  const srcs = logSources(), outs = srcs.filter((x) => x.name.startsWith("out:"));
  w.innerHTML = `<div class="data logs"><div class="dh"><h3>Logs</h3><span class="seg" role="group" aria-label="Log source">` +
    srcs.filter((x) => !x.name.startsWith("out:")).map((x) => `<button type="button" data-logsrc="${esc(x.name)}" aria-pressed="${S.log.src === x.name}">${esc(x.label)}<small>${logBuf(x.name).lines.length || ""}</small></button>`).join("") +
    `</span>` +
    (outs.length ? `<select id="logout" aria-label="Delegation output"><option value="">Delegation output…</option>` +
      outs.map((x) => `<option value="${esc(x.name)}" ${S.log.src === x.name ? "selected" : ""}>${esc(x.delegation)} · ${esc(x.label)}</option>`).join("") + `</select>` : "") +
    `<span class="sp"></span><span id="logn" class="dcap">${b.lines.length} ${src.kind === "tools" ? "calls" : "lines"}</span><span id="logstate">${state}</span>` +
    `<label class="toggle"><input type="checkbox" id="logpause" ${S.log.paused ? "checked" : ""}> Pause</label></div>` +
    `<div id="lognote">${note}</div><div id="logbody" class="logbody${live ? "" : " fit"}" tabindex="0">${rows}</div></div>`;
  const nb2 = $("logbody"); if (nb2) nb2.scrollTop = nb2.scrollHeight;
}
setInterval(() => { if (S.view === "logs" && visible() && !S.log.paused) { const b = logBuf(S.log.src); if (liveNow()) { b.stale = true; loadLogs(); } } }, LOG_MS);
let nbBusy = false;
async function loadNotebook() {
  const run = S.run; if (!run || nbBusy) return;
  if (S.nb && S.nbrun === run && S.vitals && S.vitals.closed) return;
  nbBusy = true;
  try {
    const q = S.nbpick ? "?reexec=" + encodeURIComponent(S.nbpick) : "";
    const r = await fetch("/api/runs/" + encodeURIComponent(run) + "/notebook" + q, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("notebook → " + r.status);
    const text = await r.text();
    if (run !== S.run) return;
    const sig = text.length + ":" + run + ":" + (S.nbpick || "");
    S.nb = JSON.parse(text); S.nbrun = run;
    if (sig !== S.nbsig) { S.nbsig = sig; paintWork(); loadKatex().then((ok) => { if (ok && S.view === "deliverable") paintWork(); }); }
  } catch (e) {
    S.nb = { cells: [], error: "Could not read the notebook (" + String(e.message || e) + ")." }; S.nbrun = run; paintWork();
  } finally { nbBusy = false; }
}
function rxLog(text, cls) { S.rx.lines.push([text, cls || ""]); }
function rxFinish(passed, result) {
  S.rx.state = passed ? "passed" : "failed"; S.rx.result = result; paintWork(); paintDrawer();
  if (result && result.saved) { S.nbpick = null; S.nbsig = ""; S.nbrun = null; loadNotebook(); }
}
function rxFail(msg) { rxLog(msg, "bad"); rxFinish(false, null); }
function rxEvent(ev) {
  if (ev.event !== "result") { rxLog(ev.line, ev.event === "cell" && ev.errored ? "bad" : ""); paintDrawer(); return; }
  const tail = (title, t) => { if (t && t.trim()) { rxLog("— " + title + " —", "dim"); String(t).replace(ANSI, "").trim().split("\n").forEach((l) => rxLog(l)); } };
  tail("notebook output", ev.stdout_tail);
  tail("errors", ev.stderr_tail);
  if (ev.error) rxLog(ev.error, "bad");
  const facts = [ev.rows_before != null ? `ledger ${ev.rows_before} → ${ev.rows_after} rows` : "", ev.duration_s != null ? ev.duration_s + " s" : ""].filter(Boolean).join(" · ");
  rxLog((ev.passed ? "PASSED" : "FAILED") + (facts ? " · " + facts : ""), ev.passed ? "ok" : "bad");
  rxFinish(!!ev.passed, ev);
}
async function reexecute() {
  if (S.rx.state === "running" || !S.run) return;
  const run = S.run;
  S.rx = { state: "running", run, lines: [], result: null, open: true };
  paintWork(); paintDrawer();
  try {
    const r = await fetch("/api/runs/" + encodeURIComponent(run) + "/notebook/reexecute/stream", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.status === 403 || r.status === 415) return rxFail("This page is read-only: open the /session?token=… URL printed when the viewer started to be allowed to re-execute.");
    if (!r.ok) {
      let m = ""; try { m = (await r.json()).error; } catch (e) { /* no body */ }
      return rxFail(m || "The re-execution was not accepted (" + r.status + ").");
    }
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) { const ln = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (ln) rxEvent(JSON.parse(ln)); }
    }
  } catch (e) { if (S.rx.state === "running") return rxFail("Lost the connection to the viewer before a verdict arrived."); }
  if (S.rx.state === "running") rxFail("The stream ended without a verdict.");
}
function paintDrawer() {
  const d = $("drawer"), show = S.view === "deliverable" && S.rx.open && S.rx.run === S.run;
  d.hidden = !show;
  if (!show) return;
  const prev = d.querySelector(".dlog"), stick = !prev || prev.scrollHeight - prev.scrollTop - prev.clientHeight < 24;
  d.innerHTML = `<div class="dhd"><b>Re-execution</b>${rxMark()}<span class="sp"></span><button type="button" class="btn ghost" data-drawer-close aria-label="Close the log">Close</button></div>` +
    `<div class="dlog" role="log" aria-live="polite" tabindex="0">${S.rx.lines.map(([t, c]) => `<div class="${c}">${esc(t)}</div>`).join("")}</div>`;
  const log = d.querySelector(".dlog");
  if (stick) log.scrollTop = log.scrollHeight; else if (prev) log.scrollTop = prev.scrollTop;
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
  const all = stores(), f = S.fom && S.fom.declared && S.fom.row != null ? S.fom : null;
  const want = S.ns !== undefined ? S.ns : f ? f.namespace || null : null;
  return all.find((x) => nsOf(x) === want) || all.find((x) => nsOf(x) === null) || all[0] || null;
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
function chartModel() {
  const f = S.fom, names = scoredList(), all = stores().filter((x) => names.includes(nsOf(x)));
  const rows = [];
  all.forEach((st, si) => {
    const col = st.columns[f.column], fe = f.feasible && st.columns[f.feasible] ? st.columns[f.feasible].values : null;
    for (let i = 0; i < st.n; i++) {
      const raw = col ? col.values[i] : null, finite = raw != null && Math.abs(raw) < SENTINEL;
      rows.push({ st, si, i, t: parseT(st.ts[i]), y: finite ? disp(raw) : null, ok: finite && (fe ? fe[i] === 1 : !f.feasible) });
    }
  });
  rows.sort((a, b) => (a.t == null) - (b.t == null) || (a.t || 0) - (b.t || 0) || a.si - b.si || a.i - b.i);
  const t0 = runT0();
  rows.forEach((r, k) => { r.ev = k + 1; r.h = r.t != null && t0 != null ? (r.t - t0) / 3600 : null; });
  return { all, rows };
}
function chartHtml(W) {
  const f = S.fom;
  if (!f || !f.declared) return note("The study declares no objective, so there is no best-so-far to draw. Declare one in an <code>objective:</code> block of config.yaml.");
  const { all, rows } = chartModel(), focus = curStore();
  const notScored = (f.not_scored || []).map((x) => `${esc(x.namespace || "canonical")} (missing ${x.missing.map(esc).join(", ")})`);
  if (!all.length) return note(`No store records the declared objective <b>${esc(f.column)}</b> yet.` + (notScored.length ? ` Not scored: ${notScored.join("; ")}.` : ""));
  const counted = rows.filter((r) => r.ok), drawable = rows.filter((r) => r.y != null);
  if (!drawable.length) return note("No row has a finite value for the objective yet.");
  const useTime = S.xmode === "time" && rows.every((r) => r.h != null), xv = (r) => (useTime ? r.h : r.ev);
  const basis = (counted.length ? counted : drawable).map((r) => r.y);
  const pos = counted.length > 0 && counted.every((r) => r.y > 0), decades = pos ? Math.log10(Math.max(...basis) / Math.min(...basis)) : 0;
  const canLog = decades > 2, log = canLog && S.logy;
  const tf = log ? Math.log10 : (v) => v;
  let lo = tf(Math.min(...basis)), hi = tf(Math.max(...basis));
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  let x0 = Math.min(...rows.map(xv)), x1 = Math.max(...rows.map(xv));
  if (x0 === x1) { x0 -= 1; x1 += 1; }
  const xp = (x1 - x0) * 0.02; x0 -= xp; x1 += xp;
  const phone = W < 520, H = phone ? 200 : 260, L = 48, R = 12, T = 10, B = 22;
  const sx = (x) => L + ((x - x0) / (x1 - x0)) * (W - L - R), sy = (y) => T + (1 - (tf(y) - lo) / (hi - lo)) * (H - T - B);
  const inY = (y) => y != null && (!log || y > 0) && tf(y) >= lo && tf(y) <= hi;
  const yTicks = log ? niceTicks(Math.ceil(lo), Math.floor(hi), 4).map((e) => Math.pow(10, e)) : niceTicks(lo, hi, 4);
  const xTicks = niceTicks(x0 < 0 ? 0 : x0, x1, phone ? 4 : 6).filter((v) => v >= x0 && v <= x1);
  const grid = yTicks.filter(inY).map((v) => `<line class="gl" x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 6}" y="${sy(v) + 4}" text-anchor="end">${fmtNum(v)}</text>`).join("") +
    xTicks.map((v) => `<text x="${sx(v)}" y="${H - 6}" text-anchor="middle">${useTime ? "+" + v + " h" : v}</text>`).join("");
  let above = 0, below = 0;
  const refs = (f.lines || []).map((l) => {
    const y = disp(l.value);
    if (inY(y)) return `<line class="ref" x1="${L}" x2="${W - R}" y1="${sy(y)}" y2="${sy(y)}"/><text class="refl" x="${W - R - 4}" y="${sy(y) - 5}" text-anchor="end">${esc(l.label)}</text>`;
    const isUp = log ? y > 0 && tf(y) > hi : y > hi, row = isUp ? above++ : below++;
    return `<text class="refl tag" x="${W - R - 4}" y="${isUp ? T + 10 + row * 13 : H - B - 5 - row * 13}" text-anchor="end">${esc(l.label)} ${isUp ? "↑ above" : "↓ below"} range</text>`;
  }).join("");
  const better = f.direction === "max" ? Math.max : Math.min;
  let best = null, bestRow = null, d = "";
  counted.forEach((r) => {
    if (best == null) { best = r.y; bestRow = r; d = `M${sx(xv(r))} ${sy(best)}`; }
    else if (better(best, r.y) !== best) { d += `H${sx(xv(r))}V${sy(r.y)}`; best = r.y; bestRow = r; }
  });
  if (d) d += `H${sx(x1 - xp)}`;
  const focusNs = nsOf(focus), mine = rows;
  const dot = (r) => {
    const key = rowKey(r.st, r.i);
    return `<path class="dot ${r.ok ? "f" : "i"}${nsOf(r.st) === focusNs ? "" : " dim"}${S.sel === key ? " sel" : ""}" d="${markPath(MARKS[r.si % MARKS.length], sx(xv(r)), sy(r.y))}" data-sel="${key}" data-row="${r.i}" data-ns="${esc(nsOf(r.st) || "")}"/>`;
  };
  const inside = mine.filter((r) => r.y != null && inY(r.y)), edge = mine.filter((r) => r.y != null && !inY(r.y));
  const ticks = edge.map((r) => { const up = log ? r.y > 0 && tf(r.y) > hi : r.y > hi, y = up ? T : H - B;
    return `<line class="edgetick${nsOf(r.st) === focusNs ? "" : " dim"}" x1="${sx(xv(r))}" x2="${sx(xv(r))}" y1="${y}" y2="${y + (up ? 6 : -6)}"/>`; }).join("");
  let bestLab = "";
  if (bestRow) {
    const bx = W - R - 4, by = sy(best), flip = by < T + 18, bk = rowKey(bestRow.st, bestRow.i);
    bestLab = `<text class="bestlab" role="button" tabindex="0" data-sel="${bk}" x="${bx}" y="${flip ? by + 16 : by - 7}" text-anchor="end">${esc(fmtNum(best))}${unitOf() ? " " + esc(unitOf().label) : ""} · ${esc(nsOf(bestRow.st) || "canonical")} row ${bestRow.i}</text>`;
  }
  const marks = all.length > 1 ? all.map((st, si) => `<span title="Mark shape for this store"><svg width="14" height="14" viewBox="-7 -7 14 14"><path class="dot f" d="${markPath(MARKS[si % MARKS.length], 0, 0)}"/></svg>${esc(nsOf(st) || "canonical")}</span>`).join("") : "";
  const noX = rows.length - drawable.length;
  const cap = `${counted.length} counted of ${rows.length} rows` + (all.length > 1 ? ` across ${all.length} stores; the ${esc(focusNs || "canonical")} store is highlighted, the others drawn faint` : "") +
    (noX ? ` · ${noX} with no finite value are not drawn` : "") + (edge.length ? ` · ${edge.length} outside the range (ticks at the edge)` : "") +
    (S.xmode === "time" && !useTime ? " · some rows carry no time, so the axis is the evaluation number" : "") + (notScored.length ? ` · not scored: ${notScored.join("; ")}` : "");
  return `<div class="chartwrap"><svg class="cht" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Best ${esc(f.column)} so far over ${useTime ? "elapsed time" : "evaluations"}">` +
    `${grid}${refs}<path class="step" d="${d}"/>${ticks}${[...inside.filter((r) => nsOf(r.st) !== focusNs), ...inside.filter((r) => nsOf(r.st) === focusNs && !r.ok), ...inside.filter((r) => nsOf(r.st) === focusNs && r.ok)].map(dot).join("")}${bestLab}</svg><div class="tip" id="tip" hidden></div></div>` +
    `<div class="legend"><span><i class="dot f"></i>counted</span><span><i class="dot i"></i>not counted</span>${marks}<span><i class="stepkey"></i>best so far (${esc(f.direction)})</span></div>` +
    `<div class="dcap">${cap}</div>`;
}
function chartControls() {
  const f = S.fom; if (!f || !f.declared) return "";
  const { rows } = chartModel(), counted = rows.filter((r) => r.ok).map((r) => r.y);
  const canTime = rows.length > 0 && rows.every((r) => r.h != null);
  const canLog = counted.length > 0 && counted.every((v) => v > 0) && Math.log10(Math.max(...counted) / Math.min(...counted)) > 2;
  return `<div class="seg" role="group" aria-label="Chart x axis"><button data-xmode="eval" aria-pressed="${S.xmode !== "time" || !canTime}">evaluation</button>` +
    `<button data-xmode="time" aria-pressed="${S.xmode === "time" && canTime}"${canTime ? "" : " disabled title=\"Some rows record no time\""}>elapsed time</button></div>` +
    (canLog ? `<div class="seg" role="group" aria-label="Chart y scale"><button data-logy="0" aria-pressed="${!S.logy}">linear</button><button data-logy="1" aria-pressed="${!!S.logy}">log</button></div>` : "");
}
function funnelHtml(st) {
  const fn = S.data.fun && S.data.fun.stores.find((x) => x.namespace === st.namespace);
  if (!fn) return note("No 0/1 columns are recorded in this store yet.");
  if (!S.data.fun.declared) return flagsHtml(fn);
  if (!fn.stages.length) return note("None of the declared funnel stages is recorded as a 0/1 column in this store." + (fn.skipped && fn.skipped.length ? " Not 0/1 here: " + esc(fn.skipped.join(", ")) + "." : ""));
  return `<div class="funnel">` + fn.stages.map((s) =>
    `<div class="stage${fn.binding === s.column ? " bind" : ""}"><div class="sname" title="${esc(s.column)}">${esc(s.column)}</div>` +
    `<div class="snum">${s.cumulative}<small>of ${s.n}</small></div>` +
    `<div class="sbar" title="Rows passing every stage so far: ${s.cumulative}. Passing this stage alone: ${s.pass}."><i style="width:${s.n ? (100 * s.cumulative / s.n).toFixed(1) : 0}%"></i><u style="width:${s.n ? (100 * s.pass / s.n).toFixed(1) : 0}%"></u></div>` +
    `<div class="salone">${s.pass} alone${s.unrecorded ? ` · ${s.unrecorded} unrecorded` : ""}${fn.binding === s.column ? " · tightest" : ""}</div></div>`).join("") + `</div>` +
    `<div class="dcap">Each stage: rows passing every stage up to it (solid bar, large number), and rows passing it alone (line).${fn.skipped && fn.skipped.length ? " Not 0/1 in this store: " + esc(fn.skipped.join(", ")) + "." : ""}</div>`;
}
function flagsHtml(fn) {
  if (!fn.flags || !fn.flags.length) return note("No 0/1 columns are recorded in this store yet.");
  const so = S.fsort, rows = fn.flags.slice();
  if (so) rows.sort((a, b) => (so.key === "column" ? String(a.column).localeCompare(b.column) : a.ones - b.ones) * (so.dir === "asc" ? 1 : -1));
  const th = (key, label, cls) => `<button class="th${cls || ""}" data-fsort="${key}" aria-sort="${so && so.key === key ? (so.dir === "asc" ? "ascending" : "descending") : "none"}" title="Sort by ${esc(label)}">${esc(label)}<span>${so && so.key === key ? (so.dir === "asc" ? "↑" : "↓") : ""}</span></button>`;
  return `<div class="flags"><div class="fh">${th("column", "Column")}${th("ones", "Rows with 1", " num")}<span class="th num">Rows</span><span></span></div>` +
    rows.map((r) => `<div class="fr"><span class="mono" title="${esc(r.column)}">${esc(r.column)}</span><span class="num">${r.ones}</span><span class="num"${r.unrecorded ? ` title="${r.unrecorded} of these rows have no value recorded"` : ""}>${r.n}${r.unrecorded ? "*" : ""}</span>` +
      `<span class="sbar"><i style="width:${r.n ? (100 * r.ones / r.n).toFixed(1) : 0}%"></i></span></div>`).join("") + `</div>` +
    `<div class="dcap">* some rows record no value for it. These columns are independent flags, not ordered stages. Declare a <code>funnel:</code> list in config.yaml to draw them as a funnel.</div>`;
}
function colWidth(c, st) {
  if (c.id) return c.w;
  let len = String(c.label).length + 3;
  const step = Math.max(1, Math.floor(st.n / 200));
  for (let i = 0; i < st.n; i += step) len = Math.max(len, cellText(c, i).length);
  return Math.min(c.w > 200 ? c.w : 240, Math.max(64, Math.round(len * 7.6 + 28)));
}
function tableHtml(st, m) {
  const cols = visibleCols(st, m), sort = S.sort || { key: "#", dir: "desc" };
  const ws = cols.map((c) => colWidth(c, st)), W = ws.reduce((a, b) => a + b, 0), tpl = ws.map((w) => w + "px").join(" ");
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
  const selRow = ROW_RE.exec(S.sel || "");
  if (selRow && S.lastRowSel !== S.sel) S.ns = selRow[1] || null;
  const st = curStore();
  if (!st) { w.innerHTML = '<div class="empty"><h3>Data</h3><p>This run has no oracle store yet. Rows appear here once the oracle evaluates its first design.</p></div>'; return; }
  DM = new Map(S.dels.map((d) => [d.id, d.to_node]));
  const m = model(st), all = stores();
  const seg = all.length > 1 ? `<div class="seg" role="group" aria-label="Store">` + all.map((x) =>
    `<button data-store="${esc(nsOf(x) || "")}" aria-pressed="${nsOf(x) === nsOf(st)}" title="Highlight this store’s dots and show its flags and rows. The best-so-far line and every store’s dots stay drawn.">${esc(nsOf(x) || "canonical")}<small>${x.n}</small></button>`).join("") + `</div>` : "";
  const W = Math.max(320, w.clientWidth - 2 * 24);
  const f = S.fom && S.fom.declared ? S.fom : null;
  w.innerHTML = `<div class="data"><div class="dh"><h3>Best so far${f ? ` · ${esc(f.column)}${unitOf() ? " (" + esc(unitOf().label) + ")" : ""}` : ""}</h3>${f ? `<span class="dcap">${f.direction === "max" ? "higher" : "lower"} is better${f.feasible ? ", counting rows where " + esc(f.feasible) + " = 1" : ""}</span>` : ""}<span class="sp"></span>${seg}${chartControls()}</div>` +
    chartHtml(W) + `<div class="dh"><h3>${S.data.fun && S.data.fun.declared ? "Stage funnel" : "0/1 columns"}</h3></div>` + funnelHtml(st) + `<div class="dh"><h3>Store</h3></div>` + tableHtml(st, m) + `</div>`;
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
  const ls = e.target.closest("[data-logsrc]");
  if (ls) { e.stopImmediatePropagation(); S.log.src = ls.dataset.logsrc; paintLogs($("work")); loadLogs(); return; }
  const pk = e.target.closest("[data-pick]");
  if (pk) { e.stopImmediatePropagation(); S.nbpick = pk.dataset.pick; S.nbsig = ""; S.nbrun = null; paintWork(); loadNotebook(); return; }
  const hm = e.target.closest("[data-hmore]");
  if (hm) { e.stopImmediatePropagation(); S.hopen[hm.dataset.hmore] = !S.hopen[hm.dataset.hmore]; paintWork(); return; }
  if (e.target.closest("#reexec")) { reexecute(); return; }
  if (e.target.closest("[data-drawer-close]")) { S.rx.open = false; paintDrawer(); return; }
  const ns = e.target.closest("[data-store]");
  if (ns) { S.ns = ns.dataset.store || null; S.tscroll = 0; S.sort = null; paintWork(); return; }
  const hf = e.target.closest("[data-hfilter]");
  if (hf) { S.hfilter = hf.dataset.hfilter; paintWork(); return; }
  const xm = e.target.closest("[data-xmode]");
  if (xm) { S.xmode = xm.dataset.xmode; paintWork(); return; }
  const lg = e.target.closest("[data-logy]");
  if (lg) { S.logy = lg.dataset.logy === "1"; paintWork(); return; }
  const fs = e.target.closest("[data-fsort]");
  if (fs) { const cur = S.fsort; S.fsort = { key: fs.dataset.fsort, dir: cur && cur.key === fs.dataset.fsort && cur.dir === "desc" ? "asc" : "desc" }; paintWork(); return; }
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

/* ── Setup, Start-run sheet and Stop popover (spec 15 §4.3, §4.5; build step 7) ── */
const FILE_LABEL = { problem_statement: "Problem statement", config: "Config" };
const FILE_NAME = { problem_statement: "PROBLEM_STATEMENT.md", config: "config.yaml" };
const READ_ONLY = "This page is read-only: open the /session?token=… URL printed when the viewer started to be allowed to write.";
async function send(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let j = null; try { j = await r.json(); } catch (e) { j = null; }
    if (r.status === 403 || r.status === 415) return { ok: false, status: r.status, json: j, error: READ_ONLY };
    return { ok: r.ok, status: r.status, json: j, error: j && j.error };
  } catch (e) { return { ok: false, status: 0, json: null, error: "Could not reach the viewer." }; }
}
S.su = { file: "problem_statement", f: {}, hist: null, histErr: null, open: null, patches: {}, note: null };
function suf() { return S.su.f[S.su.file] || (S.su.f[S.su.file] = { data: null, buf: null, rows: null, val: null, msg: "", err: null, busy: false, loadErr: null }); }
async function loadSetup() {
  const file = S.su.file, f = suf();
  let fetched = false;
  if (!f.data && !f.loadErr) {
    fetched = true;
    try {
      const d = await get("/api/study/file/" + file);
      f.data = d; f.loadErr = null;
      if (f.buf == null) f.buf = d.working != null ? d.working : (d.committed || "");
      if (d.validation && !f.val) f.val = d.validation;
    } catch (e) { f.loadErr = String(e.message || e); }
  }
  if (!S.su.hist && !S.su.histErr) {
    fetched = true;
    try { S.su.hist = (await get("/api/study/history")).commits || []; } catch (e) { S.su.histErr = String(e.message || e); }
  }
  if (S.view === "setup" && S.su.file === file && fetched) { paintSetup(true); refreshDiff(true); }
}
let diffT = null;
function refreshDiff(now) {
  clearTimeout(diffT);
  const file = S.su.file, f = suf();
  if (!f.data) return;
  const run = async () => {
    const r = await send("/api/study/file/" + file + "/diff", { text: f.buf });
    if (r.ok) { f.rows = r.json.rows; f.changed = r.json.changed; f.val = r.json.validation || null; }
    if (S.view === "setup" && S.su.file === file) paintSetupParts();
  };
  if (now) run(); else diffT = setTimeout(run, 250);
}
function diffHtml(f) {
  if (!f.rows) return '<p class="none">Comparing…</p>';
  if (!f.changed) return '<p class="none">No changes: the buffer equals the committed file.</p>';
  return '<table class="sdiff"><tbody>' + f.rows.map((r) => r.op === "skip"
    ? `<tr class="skip"><td colspan="4">${r.n} unchanged lines</td></tr>`
    : `<tr class="${r.op}"><td class="n">${r.na == null ? "" : r.na}</td><td class="a">${r.a == null ? "" : esc(r.a)}</td><td class="n">${r.nb == null ? "" : r.nb}</td><td class="b">${r.b == null ? "" : esc(r.b)}</td></tr>`).join("") + "</tbody></table>";
}
function valHtml(f) {
  if (S.su.file !== "config" || !f.val) return "";
  const e = f.val.errors || [], w = f.val.warnings || [];
  return e.map((x) => `<p class="st bad">${esc(x)}</p>`).join("") + w.map((x) => `<p class="st warn">${esc(x)}</p>`).join("") +
    (!e.length && !w.length ? '<p class="st ok">The config is valid.</p>' : "");
}
function canCommit(f) { return !!(f.data && f.changed && f.msg.trim() && !f.busy && (S.su.file !== "config" || !f.val || f.val.ok)); }
function paintSetupParts() {
  const f = suf(), d = $("sudiff"); if (!d) return;
  d.innerHTML = diffHtml(f);
  $("suval").innerHTML = valHtml(f);
  $("suerr").innerHTML = f.err ? `<p class="st bad">${esc(f.err)}</p>` : S.su.note ? `<p class="st ok">${esc(S.su.note)}</p>` : "";
  $("sucommit").disabled = !canCommit(f);
  $("sudiscard").disabled = !f.changed;
  $("sustate").innerHTML = f.data ? (f.changed ? '<span class="st warn">edited, not committed</span>' : '<span class="st ok">matches the committed file</span>') : "";
}
function histHtml() {
  if (S.su.histErr) return `<p class="none">Could not read the study history (${esc(S.su.histErr)}).</p>`;
  if (!S.su.hist) return '<p class="none">Loading…</p>';
  const name = FILE_NAME[S.su.file];
  const rows = S.su.hist.filter((c) => c.files.some((x) => x.path === name || x.path.endsWith("/" + name)));
  if (!rows.length) return `<p class="none">No commit has touched ${esc(name)} yet.</p>`;
  return rows.map((c) => {
    const open = S.su.open === c.sha, p = S.su.patches[c.sha + ":" + S.su.file];
    const patch = open ? (p == null ? '<p class="none">Loading…</p>' : '<pre class="patch">' + p.split("\n").map((l) => `<span class="${l[0] === "+" && l[1] !== "+" ? "add" : l[0] === "-" && l[1] !== "-" ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l)}</span>`).join("\n") + "</pre>") : "";
    return `<div class="hc"><button type="button" class="hrowb" data-su-commit="${esc(c.sha)}" aria-expanded="${open}"><span class="mono">${esc(c.sha.slice(0, 7))}</span><span class="hs">${esc(c.subject)}</span><span class="dcap">${esc(c.author)} · ${esc(String(c.date).slice(0, 16).replace("T", " "))}</span></button>${patch}</div>`;
  }).join("");
}
function paintSetup(keepFocus) {
  const w = $("work"), f = suf();
  if (S.view !== "setup") return;
  if (f.loadErr) { w.innerHTML = `<div class="empty"><h3>Setup</h3><p>Could not read the study files (${esc(f.loadErr)}). The study may not be under a git repository.</p></div>`; return; }
  if (!f.data) { w.innerHTML = '<div class="skel"><div></div><div></div><div></div></div>'; return; }
  const keep = keepFocus && $("sutext") ? { s: $("sutext").selectionStart, e: $("sutext").selectionEnd, t: $("sutext").scrollTop, m: document.activeElement && document.activeElement.id } : null;
  w.innerHTML = `<div class="data setup"><div class="dh"><h3>Setup</h3><span class="seg" role="group" aria-label="File">` +
    Object.keys(FILE_LABEL).map((k) => `<button type="button" data-sufile="${k}" aria-pressed="${S.su.file === k}">${FILE_LABEL[k]}</button>`).join("") +
    `</span><span class="sp"></span><span id="sustate"></span></div>` +
    `<p class="dcap">A run only sees what is committed. The buffer is yours until you commit it; the commit takes this one file and nothing else.${f.data.working != null && f.data.committed == null ? " This file is not committed yet." : ""}</p>` +
    `<div id="suerr"></div><div class="sugrid"><div><label class="lbl" for="sutext">${esc(FILE_NAME[S.su.file])} · buffer</label>` +
    `<textarea id="sutext" spellcheck="false" aria-label="${esc(FILE_NAME[S.su.file])} buffer"></textarea></div>` +
    `<div><div class="lbl">Changes against the committed file</div><div id="sudiff"></div></div></div><div id="suval"></div>` +
    `<div class="sucommit"><input id="sumsg" type="text" placeholder="Commit message (required)" aria-label="Commit message" autocomplete="off">` +
    `<button type="button" class="btn primary" id="sucommit" disabled>Commit ${esc(FILE_NAME[S.su.file])}</button>` +
    `<button type="button" class="btn" id="sudiscard" disabled>Discard changes</button></div>` +
    `<div class="dh"><h3>History</h3><span class="dcap">Commits that touched ${esc(FILE_NAME[S.su.file])}; open one for its diff.</span></div><div id="suhist" class="hist2">${histHtml()}</div></div>`;
  $("sutext").value = f.buf; $("sumsg").value = f.msg;
  if (keep) { $("sutext").scrollTop = keep.t; if (keep.m === "sutext") { $("sutext").focus(); $("sutext").setSelectionRange(keep.s, keep.e); } }
  f.changed = f.buf !== (f.data.committed == null ? "" : f.data.committed) || (f.data.committed == null);
  paintSetupParts();
}
async function commitSetup() {
  const f = suf(), file = S.su.file;
  f.busy = true; f.err = null; S.su.note = null; paintSetupParts();
  const r = await send("/api/study/file/" + file + "/commit", { text: f.buf, message: f.msg, base: f.data.base });
  f.busy = false;
  if (r.ok) {
    S.su.note = "Committed " + r.json.sha.slice(0, 7) + " · " + r.json.subject;
    f.data = null; f.buf = null; f.msg = ""; f.rows = null; f.val = null; S.su.hist = null; S.su.patches = {};
    await loadSetup(); return;
  }
  f.err = r.error || ("The commit was refused (" + r.status + ").");
  paintSetupParts();
}
async function openCommit(sha) {
  S.su.open = S.su.open === sha ? null : sha;
  $("suhist").innerHTML = histHtml();
  const key = sha + ":" + S.su.file;
  if (S.su.open === sha && S.su.patches[key] == null) {
    try { S.su.patches[key] = (await get("/api/study/commit?sha=" + sha + "&file=" + S.su.file)).patch || "(no change to this file)"; }
    catch (e) { S.su.patches[key] = "Could not read this commit's diff (" + e.message + ")."; }
    if (S.view === "setup" && $("suhist")) $("suhist").innerHTML = histHtml();
  }
}

/* Start-run sheet */
S.sheet = { open: false, pre: null, err: null, busy: false, done: null };
async function openSheet() {
  S.sheet = { open: true, pre: null, err: null, busy: false, done: null };
  paintSheet();
  try { S.sheet.pre = await get("/api/study/preflight"); } catch (e) { S.sheet.err = "Could not run the pre-flight (" + e.message + ")."; }
  paintSheet();
}
function closeSheet() { S.sheet.open = false; paintSheet(); }
const CHECK_LINK = { problem_statement: "problem_statement", config: "config", budget: "config" };
function paintSheet() {
  const el = $("sheet"), s = S.sheet;
  el.hidden = !s.open; if (!s.open) { el.innerHTML = ""; return; }
  const p = s.pre, cf = p && p.configured;
  const checks = p ? p.checks.map((c) => {
    const m = c.ok === true ? ["ok", "pass"] : c.ok === false ? ["bad", "blocked"] : ["open", "your call"];
    const link = c.ok === false && CHECK_LINK[c.name] ? ` <a href="${esc(url({ view: "setup" }))}" data-su-goto="${CHECK_LINK[c.name]}">Fix in Setup</a>` : "";
    return `<li><span class="st ${m[0]}">${m[1]}</span><div><b>${esc(c.name.replace(/_/g, " "))}</b><div class="dcap">${esc(c.detail)}${link}</div></div></li>`;
  }).join("") : "";
  const lc = p && p.launcher;
  el.innerHTML = `<div class="dhd"><b>Start run</b><span class="sp"></span><button type="button" class="btn ghost" data-sheet-close aria-label="Close">Close</button></div><div class="sbody">` +
    (!p && !s.err ? '<p class="none">Running the pre-flight…</p>' : "") + (s.err ? `<p class="st bad">${esc(s.err)}</p>` : "") +
    (p ? `<div class="kv"><span>Model</span><b>${cf && cf.model ? esc(cf.model) : dash("The committed config.yaml names no model")}</b>` +
      `<span>Budget</span><b>${cf && cf.budget_s ? fmtH(cf.budget_s) : dash("The committed config.yaml has no parseable budget")}</b></div>` +
      '<p class="dcap">Read from the committed config.</p>' +
      (cf && cf.uncommitted.length ? `<p class="st warn">${esc(cf.uncommitted.join(" and "))} ${cf.uncommitted.length > 1 ? "have" : "has"} edits that are not committed. <a href="${esc(url({ view: "setup" }))}" data-su-goto="${cf.uncommitted[0] === "config.yaml" ? "config" : "problem_statement"}">Review in Setup</a></p>` : "") +
      `<h4>Pre-flight</h4><ul class="checks">${checks}</ul>` +
      (lc && lc.command ? `<p class="dcap">This study starts through its launcher: <span class="mono">${esc(lc.command)}</span></p>` : "") +
      (lc && lc.error ? `<p class="st bad">${esc(lc.error)}</p>` : "") : "") +
    (s.done ? `<p class="st ok">${esc(s.done)}</p>` : "") +
    `<div class="sfoot"><button type="button" class="btn primary" id="dostart" ${!p || !p.can_start || s.busy || s.done || (lc && lc.error) ? "disabled" : ""}>${s.busy ? "Starting…" : "Start run"}</button>` +
    (p && !p.can_start ? '<span class="dcap">Blocked checks must pass first.</span>' : "") + `</div></div>`;
}
async function doStart() {
  const s = S.sheet; s.busy = true; s.err = null; paintSheet();
  const lc = s.pre && s.pre.launcher && !s.pre.launcher.error;
  const r = await send(lc ? "/api/study/launch" : "/api/study/start", {});
  s.busy = false;
  if (r.ok) { s.done = "Started" + (r.json && r.json.pid ? " (pid " + r.json.pid + ")" : "") + ". The run appears in the list once it writes its first files."; tick(); }
  else s.err = (r.error || "The run was not started (" + r.status + ").") + (r.json && r.json.checks ? "" : "");
  paintSheet();
}

/* Stop popover */
S.pop = { open: false, confirmKill: false, busy: false, msg: null, bad: false, stopped: {} };
function openPop(anchor) {
  S.pop.open = !S.pop.open; S.pop.confirmKill = false; S.pop.msg = null; S.pop.anchor = anchor; S.pop.canKill = false; paintPop();
  if (!S.pop.open) return;
  get("/api/study/preflight").then(d => { S.pop.canKill = (d.launched || []).some(e => e.alive); paintPop(); }).catch(() => {});
}
function paintPop() {
  const el = $("pop"), p = S.pop;
  el.hidden = !p.open; if (!p.open) return;
  const a = document.querySelector("[data-stop]");
  if (a) { const b = a.getBoundingClientRect(); el.style.top = Math.round(b.bottom + 6) + "px"; el.style.left = Math.max(8, Math.round(Math.min(b.left, document.documentElement.clientWidth - 328))) + "px"; }
  el.innerHTML = `<p class="pt"><b>Stop this run?</b></p>` +
    (p.confirmKill
      ? `<p class="dcap">Kill now ends the run’s processes at once. No retrospectives are written, and the run is left unclosed.</p>` +
        `<div class="pb"><button type="button" class="btn" id="killnow" ${p.busy ? "disabled" : ""}>Kill now</button><button type="button" class="btn ghost" data-pop-back>Cancel</button></div>`
      : `<p class="dcap">The run finishes its current step, writes its retrospectives and closes.</p>` +
        `<div class="pb"><button type="button" class="btn primary" id="stopgrace" ${p.busy ? "disabled" : ""}>Stop gracefully</button>${p.canKill ? '<button type="button" class="btn ghost" data-pop-kill>Kill now…</button>' : ""}</div>` +
        (p.canKill ? "" : '<p class="dcap">Kill is offered only for a run this viewer started.</p>')) +
    (p.msg ? `<p class="st ${p.bad ? "bad" : "ok"}">${esc(p.msg)}</p>` : "");
}
async function doStop(kill) {
  const p = S.pop, run = S.run; p.busy = true; p.msg = null; paintPop();
  const r = kill ? await send("/api/study/kill", {}) : await send("/api/runs/" + encodeURIComponent(run) + "/stop", {});
  p.busy = false;
  if (r.ok) { p.bad = false; p.msg = kill ? "Killed." : "Stop requested. The run will close after its retrospectives."; if (!kill) p.stopped[run] = true; }
  else {
    p.bad = true;
    p.msg = kill && r.status === 404 ? "This viewer did not start a live run, so it cannot kill one. Use Stop gracefully." : (r.error || "Not accepted (" + r.status + ").");
    if (!kill && r.status === 409 && /already requested/.test(r.error || "")) { p.stopped[run] = true; }
  }
  paintPop(); paintTitle();
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
  const t = e.target;
  const sf = t.closest("[data-sufile]");
  if (sf) { S.su.file = sf.dataset.sufile; S.su.open = null; S.su.note = null; paintSetup(); loadSetup(); return; }
  if (t.closest("#sucommit")) { commitSetup(); return; }
  if (t.closest("#sudiscard")) { const f = suf(); f.buf = f.data.committed == null ? "" : f.data.committed; f.err = null; S.su.note = null; paintSetup(); refreshDiff(true); return; }
  const sc = t.closest("[data-su-commit]");
  if (sc) { openCommit(sc.dataset.suCommit); return; }
  const sg = t.closest("[data-su-goto]");
  if (sg) { e.preventDefault(); S.su.file = sg.dataset.suGoto; closeSheet(); nav({ view: "setup", sel: null }); return; }
  if (t.closest("#openstart")) { openSheet(); return; }
  if (t.closest("[data-sheet-close]")) { closeSheet(); return; }
  if (t.closest("#dostart")) { doStart(); return; }
  const st = t.closest("[data-stop]");
  if (st) { openPop(st); return; }
  if (t.closest("#stopgrace")) { doStop(false); return; }
  if (t.closest("[data-pop-kill]")) { S.pop.confirmKill = true; paintPop(); return; }
  if (t.closest("[data-pop-back]")) { S.pop.confirmKill = false; paintPop(); return; }
  if (t.closest("#killnow")) { doStop(true); return; }
  if (S.pop.open && !t.closest("#pop")) { S.pop.open = false; paintPop(); paintTitle(); }
  const sel = e.target.closest("[data-sel]");
  if (sel) { e.preventDefault(); nav({ sel: sel.dataset.sel }); return; }
  if (e.target.closest("[data-close]")) { nav({ sel: null }); return; }
  const tab = e.target.closest("[data-view]");
  if (tab) { nav({ view: tab.dataset.view }); return; }
  const r = e.target.closest("a[data-run]");
  if (r) { e.preventDefault(); nav({ run: r.dataset.run, sel: null }); return; }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "sutext") { const f = suf(); f.buf = e.target.value; f.err = null; S.su.note = null; f.changed = f.buf !== (f.data.committed == null ? "" : f.data.committed); paintSetupParts(); refreshDiff(); }
  if (e.target.id === "sumsg") { suf().msg = e.target.value; $("sucommit").disabled = !canCommit(suf()); }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "logout") { if (e.target.value) { S.log.src = e.target.value; paintLogs($("work")); loadLogs(); } return; }
  if (e.target.id === "logpause") { S.log.paused = e.target.checked; paintLogs($("work")); if (!S.log.paused) { logBuf(S.log.src).stale = true; loadLogs(); } return; }
  if (e.target.id === "nbcode") { S.codeOpen = e.target.checked; paintWork(); return; }
  if (e.target.id === "follow") { S.follow = e.target.checked; paintWork(); }
});
let gPending = false;
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input,textarea,select")) return;
  if (e.key === "Escape" && (S.sheet.open || S.pop.open)) { if (S.pop.open) { S.pop.open = false; paintPop(); paintTitle(); } else closeSheet(); return; }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-sel][role=button]")) {
    e.preventDefault(); nav({ sel: e.target.dataset.sel }); return;
  }
  if (e.key === "Escape") {
    const k = document.querySelector(".keys");
    if (k) k.remove(); else if (!$("drawer").hidden) { S.rx.open = false; paintDrawer(); } else if (S.sel) nav({ sel: null });
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
