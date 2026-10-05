"""Append one row of run telemetry to studies/run_ledger.csv.

Longitudinal record so we can measure whether changes make agentic runs better
or worse over time. Each row pins the commit SHA + run id and the headline
process/outcome metrics.

Usage:
    python studies/run_ledger.py <run_dir> [--commit <sha>]

<run_dir> is a study's runs/<timestamp>/ directory. --commit overrides the
detected HEAD (use it to backfill a row for a run that executed on an older
commit).
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

LEDGER = Path(__file__).resolve().parent / "run_ledger.csv"
COLUMNS = [
    # `outcome` is what the conclusions are worth; `termination` is HOW the run
    # stopped; `reviewed` is whether a critic gate actually ran. Three facts,
    # not one: a run can terminate `done` and still be UNGATED (no critic
    # reviewed it), and a halted run may carry real science. Analysis MUST read
    # `termination` before comparing outcomes — a killed run is censored, not
    # failed. Blank in rows predating runtime.terminal.
    "commit", "study", "run_id", "outcome", "termination", "reviewed",
    "critic_consults", "delegations",
    "ledger_rows", "mean_wall_ms", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens",
    "cost_usd", "time_used", "wall_s",
    "milestones_done", "milestones_skipped",
    "milestones_pending", "diagnostics",
    # Computed from exact tokens x model_prices.yaml, alongside (never merged
    # into) the SDK's `cost_usd`; covers calls the SDK never priced (the
    # strategizer). Blank = unknown.
    "cost_usd_computed",
    # The ablation arms this run actually ran under, defaults included (from
    # run_config.json["arms"]). Blank = the run predates arm recording.
    "arm_hypothesis_ledger", "arm_milestones_enabled", "arm_science_monitor",
    "arm_f3dasm_api", "arm_doe_playbook", "arm_verdict_validator",
    "arm_pipeline_deliverable", "arm_reproduction_gate",
    "arm_peer_interaction", "arm_max_awake_nodes",
    # Process KPIs (CLAUDE.md §1 step 5). error_returns = ERROR_RETURN events
    # (target 0). `objective` = what the study declared in config.yaml
    # (`column:direction[:feasible=col]`), else "undeclared". first_feasible_* =
    # position/time of the first canonical-store row that COUNTS under that
    # declaration (finite objective, and feasible==1 when declared); blank =
    # none ever. best_trace = running best at <=20 evenly spaced eval counts:
    # {"obj","n","best"} when declared, {"obj","n","min","max"} when not (the
    # direction is not ours to guess).
    "error_returns", "objective", "first_feasible_eval", "first_feasible_s",
    "best_trace",
]

_TRACE_POINTS = 20


def _objective_kpis(run_dir: Path) -> dict:
    """first-feasible position/time and best-so-far trace under the study's
    declared objective (run_config.json["objective"]), in timestamp order.

    Declared: over EVERY store that records the declared columns (canonical
    plus each namespace), because a run's result may live in a namespace; a
    store lacking one is named under ``stores.not_scored`` in best_trace.
    Undeclared: the canonical store's first output column, as before."""
    from datetime import datetime

    from adda._src.evaluation.ledger_summary import experiment_stores
    from adda._src.evaluation.objective import (
        best_so_far,
        label,
        objective_values,
        score_stores,
    )
    try:
        cfg = json.loads((run_dir / "debug" / "run_config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    objective = cfg.get("objective") or None
    out: dict = {"objective": label(objective)}
    root = run_dir / "experiment_data"
    try:
        loaded = []
        for store in experiment_stores(root):
            oc = store / "experiment_data" / "output.csv"
            if not oc.exists():
                continue
            rows = list(csv.DictReader(oc.open()))
            head = list(rows[0]) if rows else []
            loaded.append((None if store == root else store.name, head, rows))
        if not loaded:
            return out
        stores_info = None
        if objective:
            scored, not_scored = score_stores(loaded, objective)
            obj = objective["column"]
            pairs = [(r, v) for st in scored
                     for r, v in zip(st["rows"], st["values"], strict=False)]
            stores_info = {"scored": [s["namespace"] for s in scored],
                           "not_scored": {s["namespace"]: s["missing"] for s in not_scored}}
        else:
            rows = loaded[0][2] if loaded[0][0] is None else []
            cols = [c for c in (rows[0] if rows else {}) if c and not c.startswith("_")]
            if not cols:
                return out
            obj = cols[0]
            pairs = list(zip(rows, objective_values(rows, None, obj), strict=False))
        pairs.sort(key=lambda p: p[0].get("_ts") or "")
        rows = [p[0] for p in pairs]
        vals = [p[1] for p in pairs]
        first = next((i for i, v in enumerate(vals) if v is not None), None)
        if first is not None:
            out["first_feasible_eval"] = first + 1
            try:
                t0 = float((run_dir / "debug" / "run_started_at").read_text())
                ts = datetime.fromisoformat(rows[first]["_ts"]).timestamp()
                out["first_feasible_s"] = round(ts - t0, 1)
            except (OSError, ValueError, KeyError):
                pass
        n = len(vals)
        if n:
            k = min(_TRACE_POINTS, n)
            idx = sorted({round((j + 1) * n / k) - 1 for j in range(k)})
            series = best_so_far(vals, objective)
            out["best_trace"] = json.dumps({
                "obj": obj, "n": [i + 1 for i in idx],
                **{name: [seq[i] for i in idx] for name, seq in series.items()},
                **({"stores": stores_info} if stores_info else {})})
        return out
    except Exception:
        return out


def _migrate_header() -> None:
    """Rewrite an existing ledger whose header predates a column added to
    COLUMNS, so appended rows stay aligned with it (old rows get blanks)."""
    if not LEDGER.exists():
        return
    with LEDGER.open(newline="") as f:
        rows = list(csv.DictReader(f))
        header = f.seek(0) or next(csv.reader(f), [])
    if header == COLUMNS:
        return
    with LEDGER.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, restval="",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _git_short_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _solution_field(text: str, key: str) -> str:
    # matches "- key: value" or "| key | value |" style metadata lines
    m = re.search(rf"{re.escape(key)}\s*[:|]\s*([^\n|]+)", text)
    return m.group(1).strip() if m else ""


def extract(run_dir: Path) -> dict:
    debug = run_dir / "debug"
    study = run_dir.parent.parent.name  # studies/<study>/runs/<id>
    row = {c: "" for c in COLUMNS}
    row["study"] = study
    row["run_id"] = run_dir.name

    try:
        _arms = json.loads((debug / "run_config.json").read_text()).get("arms")
    except (OSError, ValueError):
        _arms = None
    for _k, _v in (_arms or {}).items():
        if f"arm_{_k}" in row:
            row[f"arm_{_k}"] = str(_v).lower()

    # outcome
    status_f = debug / "run_status.json"
    sol = run_dir.parent.parent / "solution.md"
    nb = run_dir.parent.parent / "pipeline.ipynb"
    if status_f.exists():
        try:
            _status = json.loads(status_f.read_text())
            row["outcome"] = _status.get("status", "halted")
            # Recorded by the run itself (runtime.terminal), never re-derived
            # here. Absent for runs that predate it.
            row["termination"] = _status.get("termination", "")
            _rev = _status.get("reviewed")
            row["reviewed"] = "" if _rev is None else str(bool(_rev)).lower()
        except Exception:
            row["outcome"] = "halted?"
    elif sol.exists() and (
        "FAILED RUN" in (_s := sol.read_text()) or "⛔" in _s
    ):
        # Distinct, loud terminal state: the deliverable never reproduced.
        row["outcome"] = "FAILED"
    elif sol.exists() and "UNGATED" in sol.read_text():
        row["outcome"] = "UNGATED"
    elif sol.exists():
        row["outcome"] = "GATED"
    elif nb.exists():
        # Notebook deliverable (no solution.md). The stamp records the TRUE gate
        # outcome (GATED/UNGATED/FAILED) — see agent_runtime stamping. A stamped
        # notebook without that field is a pre-fix run: fall back to GATED. No
        # stamp at all = the run never closed cleanly = FAILED.
        try:
            import nbformat as _nbf
            _ag = _nbf.read(str(nb), as_version=4).metadata.get("agentic", {})
            if not _ag.get("run"):
                row["outcome"] = "FAILED"
            else:
                row["outcome"] = _ag.get("gate_outcome", "GATED")
        except Exception:
            row["outcome"] = "FAILED"
    else:
        row["outcome"] = "no_solution"

    # critic consults
    cr = debug / "critic_reviews"
    row["critic_consults"] = len(list(cr.glob("*.md"))) if cr.exists() else 0

    # delegations + phases
    dlog = debug / "delegation_log.jsonl"
    if dlog.exists():
        recs = [json.loads(line) for line in dlog.read_text().splitlines()
                if line.strip()]
        row["delegations"] = len(recs)

    # ledger rows + mean wall_ms across the canonical store AND every design
    # namespace (Axis 3a). The canonical data dir is <run>/experiment_data/
    # experiment_data; each namespace store is a sibling <run>/experiment_data/
    # <ns> with its OWN /experiment_data data dir (different depths, so mirror
    # total_ledgered_evals's dir walk rather than a single glob).
    store_root = run_dir / "experiment_data"
    out_csvs = []
    _canon = store_root / "experiment_data" / "output.csv"
    if _canon.exists():
        out_csvs.append(_canon)
    if store_root.is_dir():
        for _sub in sorted(store_root.iterdir()):
            if _sub.is_dir() and _sub.name != "experiment_data":
                _f = _sub / "experiment_data" / "output.csv"
                if _f.exists():
                    out_csvs.append(_f)
    if out_csvs:
        rows = [r for f in out_csvs for r in csv.DictReader(f.open())]
        row["ledger_rows"] = len(rows)
        wm = [float(r["_wall_ms"]) for r in rows
              if r.get("_wall_ms") not in (None, "", "nan")
              and r.get("_source") != "precomputed_pool"]
        row["mean_wall_ms"] = round(sum(wm) / len(wm), 3) if wm else ""

    # Tokens / cost / wall come from the telemetry summary — the subsystem
    # that exists for exactly this ("where did the budget go; does the
    # machinery improve outcomes", infra/telemetry.py). It is written per LLM
    # call regardless of deliverable shape, so it is present for runs the
    # deliverable-derived path below cannot see at all: a study with
    # `pipeline_deliverable: false` has no notebook and no solution.md, and so
    # used to record NO cost data whatsoever (BACKLOG #31). It also carries the
    # two cache token fields the deliverable never reported — the ones a
    # prompt-section change actually moves.
    _tel = debug / "telemetry" / "summary.json"
    if _tel.exists():
        try:
            _tot = json.loads(_tel.read_text()).get("totals") or {}
            row["input_tokens"] = _tot.get("input_tokens", "")
            row["output_tokens"] = _tot.get("output_tokens", "")
            row["cache_read_tokens"] = _tot.get("cache_read_input_tokens", "")
            row["cache_creation_tokens"] = _tot.get(
                "cache_creation_input_tokens", "")
            # None (no backend reported a price) stays BLANK, not 0 — an
            # unpriced run is unmeasured, not free.
            _cost = _tot.get("total_cost_usd")
            row["cost_usd"] = "" if _cost is None else _cost
            _comp = _tot.get("cost_usd_computed")
            row["cost_usd_computed"] = "" if _comp is None else _comp
        except Exception:
            pass

    # wall_s: the run's own duration, recorded at close. Preferred over the
    # telemetry first-call-to-last-call span, which excludes setup and the
    # final write.
    if status_f.exists():
        try:
            row["wall_s"] = json.loads(status_f.read_text()).get("wall_s", "")
        except Exception:
            pass

    # Legacy/secondary: tokens, cost and the HH:MM:SS duration as stamped into
    # the deliverable. Kept because it is the only source for runs predating
    # telemetry, and it owns `time_used`. It must not overwrite a telemetry
    # value that is already present.
    def _keep(key, value):
        if value and not row.get(key):
            row[key] = value

    if sol.exists():
        t = sol.read_text()
        _keep("input_tokens", _solution_field(t, "input_tokens").replace(",", ""))
        _keep("output_tokens",
              _solution_field(t, "output_tokens").replace(",", ""))
        _keep("cost_usd", _solution_field(t, "estimated_cost").lstrip("$"))
        row["time_used"] = _solution_field(t, "time_used")
    elif nb.exists():
        try:
            import nbformat as _nbf
            _nb = _nbf.read(str(nb), as_version=4)
            # Token usage cell is the last markdown cell appended by agent_runtime
            for _c in reversed(_nb.cells):
                if _c.cell_type == "markdown" and "## Token usage" in _c.source:
                    _t = _c.source
                    _keep("input_tokens",
                          _solution_field(_t, "input_tokens").replace(",", ""))
                    _keep("output_tokens",
                          _solution_field(_t, "output_tokens").replace(",", ""))
                    _keep("cost_usd",
                          _solution_field(_t, "estimated_cost").lstrip("$"))
                    row["time_used"] = _solution_field(_t, "time_used")
                    break
        except Exception:
            pass

    # milestones
    ms = debug / "strategizer_notes" / "milestones.json"
    if ms.exists():
        try:
            mvals = list(json.loads(ms.read_text()).values())
            for st in ("DONE", "SKIPPED", "PENDING"):
                row[f"milestones_{st.lower()}"] = sum(
                    1 for m in mvals if m.get("status") == st)
        except Exception:
            pass

    # diagnostics histogram
    diag = debug / "diagnostics.jsonl"
    if diag.exists():
        from collections import Counter
        rows = [json.loads(line) for line in diag.read_text().splitlines()
                if line.strip()]
        c = Counter(r.get("error_type") or r.get("type")
                    or r.get("intervention") or "other" for r in rows)
        row["diagnostics"] = json.dumps(dict(c))
        row["error_returns"] = c.get("ERROR_RETURN", 0)
    else:
        row["error_returns"] = 0
    row.update(_objective_kpis(run_dir))
    return row


def _delegation_proxies(run_dir: Path) -> list[dict]:
    """Per-delegation transcript PROXIES for possible trouble — 'look here'
    signals, never verdicts. Each is mechanical/semi-structured:

    - tool_errors: count of ``<tool_use_error>`` tags the SDK emits when a tool
      call fails (robust — not a free-text 'error' grep that matches code).
    - max_gap_s: the largest inter-event gap (a stall proxy).
    - errs: a few DISTINCT tool-error messages verbatim (the structured error
      text, like ERROR_RETURN — shows what tripped, not why the run failed).

    A high count or a long stall means READ that transcript; the proxy is
    allowed to over-fire (a false 'look here' costs one read) and says nothing
    about what actually happened — that judgement stays with the reader.
    """
    tdir = run_dir / "debug" / "transcripts"
    if not tdir.exists():
        return []
    out: list[dict] = []
    for f in sorted(tdir.glob("D*.jsonl")):
        errs: list[str] = []
        max_gap = 0.0
        try:
            for ln in f.read_text().splitlines():
                if not ln.strip():
                    continue
                r = json.loads(ln)
                g = r.get("gap_s") or 0
                max_gap = max(max_gap, float(g))
                if r.get("type") == "tool_result":
                    for item in (r.get("results") or []):
                        c = (item.get("content", "") if isinstance(item, dict)
                             else str(item))
                        if "<tool_use_error>" in c:
                            errs.append(c.replace("<tool_use_error>", "")
                                        .replace("</tool_use_error>", "").strip())
        except Exception:
            continue
        # distinct error messages, capped
        seen, distinct = set(), []
        for e in errs:
            key = e[:80]
            if key not in seen:
                seen.add(key)
                distinct.append(e)
        out.append({"id": f.stem, "tool_errors": len(errs),
                    "max_gap_s": round(max_gap, 1), "errs": distinct[:2]})
    # rank: most tool-errors first, then longest stall
    out.sort(key=lambda d: (-d["tool_errors"], -d["max_gap_s"]))
    return out


def _store_csvs(run_dir: Path) -> list[Path]:
    """Every output.csv of the run: the canonical store and each design namespace."""
    root = run_dir / "experiment_data"
    found = [root / "experiment_data" / "output.csv"]
    if root.is_dir():
        found += [d / "experiment_data" / "output.csv" for d in sorted(root.iterdir())
                  if d.is_dir() and d.name != "experiment_data"]
    return [f for f in found if f.exists()]


def verdict_audit(run_dir: Path) -> list[str]:
    """Closing hypothesis verdicts next to the evidence they cite (read-only).

    One block per closing status entry in strategizer_notes/hypotheses.json, with
    the statement, criterion, evidence and validator note verbatim. N_NEW_EVALS is
    the number of store rows stamped with the cited delegation, over all stores;
    rows ingested from the precomputed pool are not new evaluations and are
    reported apart. NO_NEW_EVIDENCE is a mechanical flag for a human to review,
    never a verdict. Nothing here classifies the claims.
    """
    from adda._src.epistemics.hypothesis_ledger import CLOSING_STATUSES

    f = Path(run_dir) / "debug" / "strategizer_notes" / "hypotheses.json"
    if not f.exists():
        return ["- no hypothesis ledger"]
    try:
        ledger = json.loads(f.read_text())
    except Exception as exc:
        return [f"- hypothesis ledger unreadable: {exc}"]
    new: dict[str, int] = {}
    pool: dict[str, int] = {}
    for csv_path in _store_csvs(Path(run_dir)):
        for r in csv.DictReader(csv_path.open()):
            d = r.get("_delegation_id") or ""
            bucket = pool if r.get("_source") == "precomputed_pool" else new
            bucket[d] = bucket.get(d, 0) + 1
    blocks: list[str] = []
    flagged = total = 0
    for hid, h in ledger.items():
        prev_status, prev_p = "OPEN", h.get("prior")
        for e in h.get("status_log") or []:
            if e.get("status") in CLOSING_STATUSES:
                total += 1
                ev = e.get("evidence") or {}
                cited = ev.get("delegation") or e.get("triggered_by")
                n_new = new.get(cited, 0) if cited else 0
                n_pool = pool.get(cited, 0) if cited else 0
                flag = n_new == 0
                flagged += flag
                blocks += [
                    f"### {h.get('id', hid)}: {prev_status} -> {e['status']}"
                    f"  (posterior {prev_p} -> {e.get('posterior')})"
                    + ("  NO_NEW_EVIDENCE" if flag else ""),
                    f"- statement: {h.get('statement')}",
                    f"- falsification_criterion: {h.get('falsification_criterion')}",
                    f"- cited delegation: {cited or '(none)'}; N_NEW_EVALS: {n_new}"
                    + (f" (plus {n_pool} precomputed-pool rows)" if n_pool else ""),
                    f"- evidence numbers: {json.dumps(ev.get('numbers'), sort_keys=True)}",
                    f"- validator_note: {e.get('validator_note')}",
                    ""]
            if e.get("status"):
                prev_status = e["status"]
            if e.get("posterior") is not None:
                prev_p = e["posterior"]
    head = [f"- {flagged} of {total} closing verdicts cite no new evaluations "
            "(a flag for review, not an error)", ""]
    return head + blocks if total else ["- no closing verdicts"]


def analysis_brief(run_dir: Path) -> str:
    """Mechanical post-run digest for the CLAUDE.md run-analysis protocol.

    Offloads the MECHANICAL steps so the analyst reads ONE artifact instead of
    grepping several: the Step-5 KPI baseline (this run vs the previous ledger
    row for the same study), the Step-2 ScienceMonitor diagnostics tally, and
    the verbatim ERROR_RETURN events (the KPI whose target is 0).

    It deliberately does NOT interpret the prose artifacts — retrospectives,
    critic reviews, delegation transcripts. Failure modes live in prose that a
    keyword scan misses, so those are COUNTED and POINTED TO, never classified
    here. The retrospectives are surfaced verbatim (a relocation for one read,
    Step 1 / highest signal) — read and judge them; do not trust a proxy grep.
    """
    run_dir = Path(run_dir)
    debug = run_dir / "debug"
    row = extract(run_dir)

    # previous run of THIS study (read-only baseline; this run not yet appended)
    prev = None
    if LEDGER.exists():
        with LEDGER.open() as f:
            prior = [r for r in csv.DictReader(f)
                     if r.get("study") == row["study"]]
        if prior:
            prev = prior[-1]

    L: list[str] = []
    L.append(f"# Analysis brief — {row['study']}/{run_dir.name}")
    L.append("MECHANICAL facts below (read as given). Prose artifacts that need")
    L.append("JUDGEMENT are counted + pointed to, never scanned for proxies.")
    L.append("")
    L.append("## KPIs — this run vs previous (Step 5)")

    def _kv(k: str) -> str:
        cur = row.get(k, "")
        return f"- {k}: {cur}" + (f"   (prev {prev.get(k, '')})" if prev else "")

    for k in ("outcome", "critic_consults", "error_returns",
              "first_feasible_eval", "first_feasible_s", "delegations",
              "ledger_rows",
              "mean_wall_ms", "time_used", "input_tokens", "output_tokens",
              "cost_usd", "milestones_done", "milestones_skipped",
              "milestones_pending"):
        L.append(_kv(k))
    L.append("  (critic_consults = gate attempts: 1 = clean, >2 = friction)")

    # Headline + hypothesis ledger — the science RESULT. Structured facts only:
    # min/max of the objective column (which end is 'good' is the analyst's call,
    # never asserted here), and each hypothesis' current status from its
    # status_log. A hypothesis still OPEN/INCONCLUSIVE at close is flagged as a
    # place to LOOK (a named failure mode), not classified as right or wrong.
    L.append("")
    L.append("## Headline & hypotheses (Step 5 — the science result)")
    oc = run_dir / "experiment_data" / "experiment_data" / "output.csv"
    declared = None
    try:
        declared = json.loads((debug / "run_config.json").read_text()).get("objective")
    except (OSError, ValueError):
        pass
    if declared:
        # The result may live in a namespace: report every store that records
        # the declared columns, and name the ones that cannot be scored.
        from adda._src.evaluation.ledger_summary import experiment_stores
        from adda._src.evaluation.objective import score_stores
        root = run_dir / "experiment_data"
        loaded = []
        for store in experiment_stores(root):
            f = store / "experiment_data" / "output.csv"
            if f.exists():
                rows_ = list(csv.DictReader(f.open()))
                loaded.append((None if store == root else store.name,
                               list(rows_[0]) if rows_ else [], rows_))
        scored, not_scored = score_stores(loaded, declared)
        pick = max if declared["direction"] == "max" else min
        for st in scored:
            cs = [(v, i) for i, v in enumerate(st["values"]) if v is not None]
            where = st["namespace"] or "canonical"
            if cs:
                v, i = pick(cs, key=lambda t: t[0])
                L.append(f"- {where}: best counted '{declared['column']}' = {v:.4g} "
                         f"(row {i}; {len(cs)} counted of {st['n']})")
            else:
                L.append(f"- {where}: no counted row of {st['n']}")
        for st in not_scored:
            L.append(f"- {st['namespace'] or 'canonical'}: not scored, missing "
                     f"{', '.join(st['missing'])}")
    elif oc.exists():
        try:
            rws = list(csv.DictReader(oc.open()))
            outcols = [c for c in (rws[0] if rws else {})
                       if c and not c.startswith("_")]  # skip index + provenance
            if outcols and rws:
                obj = outcols[0]
                vals = [float(r[obj]) for r in rws
                        if r.get(obj) not in (None, "", "nan")]
                if vals:
                    L.append(f"- objective '{obj}': min={min(vals):.4g} "
                             f"max={max(vals):.4g} (n={len(vals)}; which end is "
                             "'good' is yours to judge)")
        except Exception:
            L.append("- (objective unreadable → open output.csv)")
    hf = debug / "strategizer_notes" / "hypotheses.json"
    if hf.exists():
        try:
            for hid, h in json.loads(hf.read_text()).items():
                slog = h.get("status_log") or []
                cur = slog[-1].get("status") if slog else "(no status)"
                open_at_close = cur in ("OPEN", "INCONCLUSIVE", None, "(no status)")
                L.append(f"- {hid}: {cur}"
                         + ("   ← still open at close (look here)"
                            if open_at_close else ""))
        except Exception:
            L.append("- (hypotheses.json unreadable → open it)")

    L.append("")
    L.append("## ScienceMonitor diagnostics tally (Step 2)")
    try:
        d = json.loads(row.get("diagnostics") or "{}")
    except Exception:
        d = {}
    L.extend([f"- {ev}: {n}" for ev, n in sorted(d.items(), key=lambda kv: -kv[1])]
             or ["- (none recorded)"])

    # verbatim ERROR_RETURN events (structured; Step-5 KPI — target is 0)
    errs: list[str] = []
    diagf = debug / "diagnostics.jsonl"
    if diagf.exists():
        for ln in diagf.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if "ERROR_RETURN" in (
                e.get("error_type"), e.get("type"), e.get("intervention")
            ):
                errs.append(ln)
    L.append("")
    L.append(f"## ERROR_RETURN events verbatim ({len(errs)}; target 0)")
    L.extend(errs or ["- (none)"])

    L.append("")
    L.append("## Verdict audit (closing hypothesis verdicts vs the evidence they cite)")
    L.extend(verdict_audit(run_dir))

    # prose artifacts — READ; do not trust a grep
    retro = debug / "retrospectives.jsonl"
    cr = debug / "critic_reviews"
    deleg = debug / "delegations"
    retro_lines = [l for l in (retro.read_text().splitlines()
                               if retro.exists() else []) if l.strip()]
    ncr = len(list(cr.glob("*.md"))) if cr.exists() else 0
    ndeleg = len([p for p in deleg.iterdir() if p.is_dir()]) if deleg.exists() else 0
    L.append("")
    L.append("## Prose artifacts — READ these (a scan cannot classify them)")
    L.append(f"- Step 1 retrospectives: {len(retro_lines)} entries → {retro} "
             "(read FIRST; verbatim below)")
    # verdict per call is a structured token — shows WHERE the gate bounced
    # (e.g. REJECT→PASS = one fix cycle) without summarising the critic's prose.
    verdicts: list[str] = []
    if cr.exists():
        for f in sorted(cr.glob("call_*.md")):
            m = re.search(r"verdict:\s*\**(PASS|REVISE|REJECT)",
                          f.read_text(), re.IGNORECASE)
            verdicts.append(f"{f.stem}={m.group(1).upper() if m else '?'}")
    seq = ", ".join(verdicts) if verdicts else "none"
    L.append(f"- Step 3 critic_reviews: {ncr} calls [{seq}] → {cr} "
             "(read in call order)")
    L.append(f"- Step 4 delegations: {ndeleg} → {deleg} "
             "(targeted only, on a hypothesis)")

    # Per-delegation transcript proxies — ranked 'look here', never verdicts.
    proxies = _delegation_proxies(run_dir)
    if proxies:
        L.append("")
        L.append("## Delegation transcript proxies (ranked; look here, not verdicts)")
        L.append("  (tool_errors = <tool_use_error> tags the SDK emitted; a high "
                 "count or long")
        L.append("   stall means READ that transcript — it does not say what went "
                 "wrong.)")
        for p in proxies:
            L.append(f"- {p['id']}: {p['tool_errors']} tool-errors, "
                     f"max stall {p['max_gap_s']}s → "
                     f"debug/transcripts/{p['id']}.jsonl")
            for e in p["errs"]:
                L.append(f"    e.g. {e[:120]}")

    if retro_lines:
        L.append("")
        L.append("## Retrospectives verbatim (Step 1 — judge, don't grep)")
        for ln in retro_lines:
            try:
                e = json.loads(ln)
            except Exception:
                L.append(ln)
                continue
            L.append(f"### {e.get('source_id', '?')} [{e.get('role', '?')}] "
                     f"flagged={e.get('flagged')}")
            L.append(str(e.get("text", "")).strip())
            L.append("")

    return "\n".join(L)


def main() -> None:
    args = sys.argv[1:]
    commit = None
    if "--commit" in args:
        i = args.index("--commit")
        commit = args[i + 1]
        args = args[:i] + args[i + 2:]
    run_dir = Path(args[0]).resolve()
    row = extract(run_dir)
    row["commit"] = commit or _git_short_sha()

    _migrate_header()
    new = not LEDGER.exists()
    with LEDGER.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"appended row for {row['study']}/{row['run_id']} "
          f"(commit {row['commit']}, outcome {row['outcome']}) -> {LEDGER}")


if __name__ == "__main__":
    main()
