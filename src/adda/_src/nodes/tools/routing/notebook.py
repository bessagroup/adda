"""Notebook/deliverable-authoring tools: WriteCell, ShowNotebook, RunNotebook,
RunScratch, WriteDeliverable.

Four cell tools (create code / create markdown / edit / delete) were one
operation on one object, told apart by which arguments were passed — so they
are one tool, ``WriteCell``, and the create-only / rev-guarded rules that made
them safe live on inside it. Tracing the notebook cell by cell and dry-running
the Done() gate were both "execute it against a copy", so they are one tool,
``RunNotebook``, with ``gate=True`` selecting the gate.

``NotebookTools`` holds them, bound to one node (``self.node``);
``build_notebook_closures(node)`` at the bottom is the registration table.
Tools stay PascalCase methods so their docstrings remain the model-facing
description (``prompts.tool_catalog``) and stay readable from source by the
viewer's AST scan; ``inspect.signature`` drops ``self``, so the JSON schema the
backends infer is unchanged.

The canonical cell vocabulary — the four f3dasm pillars plus the Popperian
spine — is module-level: ``_PILLARS``, ``_NB_ORDER``, ``_NARRATIVE``. It is the
same for every run, so it is stated once here rather than rebuilt per node.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ....prompts.tool_catalog import tool_examples

# Tool names whose entire purpose is authoring/checking pipeline.ipynb.
# The notebook-authoring surface is owned by the `pipeline_deliverable`
# feature and declared once, with it, in runtime.features (BACKLOG #30) —
# alongside the knob that switches it, so the two cannot drift apart. Done is
# deliberately NOT in that set: a run must be able to close regardless of
# whether a notebook exists.

# ── The deliverable's canonical structure ────────────────────────────────────
# The four f3dasm pillars + the Popperian spine are PLUMBING: the cell name
# (= pillar) and the WHY-explainer are REQUIRED arguments, so the agent cannot
# author a structureless notebook or forget the rationale. Each authoring call
# builds/updates study_dir/pipeline.ipynb in this order via nbformat — no
# hand-written JSON, no live kernel.
_PILLARS = ("doe", "data_generation", "ml", "optimization", "analysis")


def _canonical_cell_order() -> list[str]:
    """The order cells are re-emitted in: narrative spine, then pillars."""
    order = ["problem", "hypotheses"]
    for p in _PILLARS:
        # <deliverable_format> step 7 substitutes a literal '## Verdict &
        # result' heading for the usual WHY-explainer immediately ahead of
        # the analysis pillar's code cell — same "narrative heading then
        # code" shape as every other pillar, just with a fixed name/text
        # instead of a free-form WHY paragraph.
        if p == "analysis":
            order.append("verdict")
        order += [f"{p}__why", p]
    return order


_NB_ORDER = _canonical_cell_order()

# The standalone narrative (markdown-only) cells + their heading. "verdict"
# is <deliverable_format>'s step 7 ('## Verdict & result', immediately
# ahead of the analysis pillar) — without an entry here, the markdown tool
# rejected any name a strategizer guessed for it (e.g. "verdict_heading"),
# a real, reproducible spec/tool mismatch (2 independent critic reviews,
# run 20260820T003819) since the spec instructs this cell in exactly the
# same literal-heading-text shape as "problem"/"hypotheses" but named
# neither.
_NARRATIVE = {
    "problem": "# Problem & objective",
    "hypotheses": "## Hypotheses",
    "verdict": "## Verdict & result",
}


def _strip_leading_md_header(text: str) -> str:
    """Drop a single leading markdown header line from author-supplied cell text.

    The notebook cell tools PREPEND the canonical heading themselves (e.g.
    ``## Hypotheses`` for the hypotheses cell, ``### doe`` for a pillar's
    WHY-explainer). When the author also opens their content with a header, the
    cell renders a duplicated heading (observed in run 20260623T212346:
    ``## Hypotheses\\n\\n## Hypotheses``, ``### analysis\\n\\n### analysis``). The
    tool owns the heading, so we strip a leading ``#``-header here to guarantee
    exactly one. Only a LEADING header is removed — sub-headings inside the body
    are preserved.
    """
    return re.sub(r"^\s*#{1,6}[^\n]*(?:\n+|$)", "", (text or "").lstrip(), count=1)


def _by_name(nb) -> dict:
    """The notebook's named cells, keyed by their metadata name."""
    out = {}
    for c in nb.cells:
        nm = (c.get("metadata", {}) or {}).get("name")
        if nm:
            out[nm] = c
    return out


def _rev(source: str) -> str:
    """Stateless content revision tag — a short hash of a cell's source.
    Same source → same rev (survives reloads/checkpoints); computed
    on-demand, never stored, so the gate's name/order metadata is untouched.
    Used as an optimistic-concurrency token: an edit/delete must present the
    rev it last saw, so it cannot blindly overwrite a cell that changed."""
    import hashlib
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()[:8]


def _integrity_error_message(tool_name: str, changed: dict) -> str:
    """Render ``_canonical_integrity_guard``'s report as the ERROR text
    RunScratch/RunNotebook prepend to their own result. Reverted and
    left-in-place paths are reported for different reasons: the first is a
    destructive change (a rewrite of existing bytes, or a deletion) with no
    other possible author — pipeline.ipynb because only WriteCell may touch
    it, the store
    because no delegation was running to have written it; the second is
    EITHER a pure append (never reverted, regardless of delegation state —
    it may be a real evaluation a concurrent delegation just wrote) OR a
    destructive change left in place because a delegation WAS running and
    may legitimately still be writing."""
    parts = [
        f"ERROR: {tool_name} changed real canonical-store/pipeline.ipynb "
        "paths. Always load the store via F3DASM_CANONICAL_STORE / adda's "
        "helpers, never a hardcoded run path."
    ]
    if changed["reverted"]:
        parts.append(f"Reverted (no other legitimate writer): {changed['reverted']}.")
    if changed["left_in_place"]:
        parts.append(
            "NOT reverted — either a pure append (may be a real evaluation "
            "a concurrent delegation wrote) or a change made while a "
            "delegation was running (may be its legitimate write); left in "
            f"place: {changed['left_in_place']}."
        )
    return " ".join(parts)


def _classify_file_change(before: bytes | None, after: bytes | None) -> str:
    """'unchanged' | 'append' | 'other' (rewrite of existing bytes, or a
    deletion).

    f3dasm's store append is exactly a bytes-level append (get_evaluator()
    only ever grows output.csv/input.csv/jobs.csv), so "old content is an
    exact prefix of new content" is sufficient to recognise a real evaluation
    landing mid-call — no CSV row parsing needed. A brand-new file (``before``
    is None) is ALSO an append — trivially, absent content is an exact
    (empty) prefix of anything — because a late writer's FIRST evaluation in
    a store, or a first write to a brand-new design-namespace store, creates
    its files rather than growing them; deleting those is the same data-loss
    case as reverting a normal append. Used by
    ``NotebookTools._canonical_integrity_guard`` to tell a concurrent
    delegation's legitimate write (report only, always) from snippet damage
    (revert candidate, but ONLY when no delegation was running — see the
    guard)."""
    if before == after:
        return "unchanged"
    if after is not None and (before is None or after.startswith(before)):
        return "append"
    return "other"


class NotebookTools:
    """The deliverable-authoring tools, bound to one node."""

    def __init__(self, node: Any) -> None:
        self.node = node

    # ── Notebook I/O shared by every authoring tool ──────────────────────────

    def _load_or_new_notebook(self):
        """The run's pipeline.ipynb, or a fresh one. A corrupt file starts clean."""
        import nbformat

        from ....evaluation.notebook_exec import repair_code_cells
        nb_path = Path(self.node._study_dir) / "pipeline.ipynb"
        if nb_path.exists():
            try:
                nb = nbformat.read(str(nb_path), as_version=4)
                repair_code_cells(nb)
                return nb, nb_path
            except Exception:  # noqa: BLE001 — corrupt → start clean
                pass
        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {"name": "python3",
                                     "display_name": "Python 3",
                                     "language": "python"}
        return nb, nb_path

    def _emit_notebook(self, by_name: dict, nb, nb_path) -> None:
        """Re-emit cells: canonical pillars first, then any CUSTOM named cells
        (a confirmed non-pillar section, in insertion order), then any unnamed
        extras (e.g. a runtime provenance stamp) preserved at the end."""
        import nbformat
        ordered = [by_name[k] for k in _NB_ORDER if k in by_name]
        custom = [by_name[k] for k in by_name if k not in _NB_ORDER]
        extras = [c for c in nb.cells
                  if (c.get("metadata", {}) or {}).get("name") not in by_name]
        nb.cells = ordered + custom + extras
        nbformat.write(nb, str(nb_path))

    @contextmanager
    def _ledger_sandbox(self, prefix: str):
        """A throwaway copy of the canonical store, and an env pointing at it.

        Both execution tools (RunScratch, RunNotebook) run against a COPY so
        nothing they do can touch the real store or pipeline.ipynb. Yields
        ``(sandbox_dir, env)``; the directory is removed on the way out.
        """
        import json as _json
        import shutil as _shutil
        import tempfile as _tempfile

        from ....evaluation.notebook_exec import sandbox_env
        run_dir = self.node._resolve_run_dir()
        store_dir = run_dir / "experiment_data"
        run_config = run_dir / "debug" / "run_config.json"
        sandbox = Path(_tempfile.mkdtemp(prefix=prefix))
        try:
            sb_store = sandbox / "experiment_data"
            if store_dir.exists():
                _shutil.copytree(store_dir, sb_store)
            else:
                sb_store.mkdir(parents=True, exist_ok=True)
            _cfg = (_json.loads(run_config.read_text())
                    if run_config.exists() else {})
            _cfg["store_dir"] = str(sb_store)
            sb_cfg = sandbox / "run_config.json"
            sb_cfg.write_text(_json.dumps(_cfg))
            yield sandbox, sandbox_env(
                sb_store, sb_cfg, study_root=self.node._study_dir)
        finally:
            _shutil.rmtree(sandbox, ignore_errors=True)

    def _any_delegation_running(self) -> bool:
        """True while at least one delegation is Working/FollowUp.

        A running delegation may be writing metered evals into the canonical
        store through get_evaluator() at the same moment RunScratch/RunNotebook
        is fingerprinting it — see ``_canonical_integrity_guard``, which
        checks this at both ends of the call before deciding whether a
        detected store change is safe to revert."""
        node = self.node
        with node._registry_lock:
            return any(
                e.get("status") in ("Working", "FollowUp")
                for e in node._registry.values()
            )

    @contextmanager
    def _canonical_integrity_guard(self):
        """Detect any write the sandboxed subprocess made to the REAL
        canonical store or pipeline.ipynb, and undo it WHERE THAT IS SAFE.

        ``_ledger_sandbox`` runs the snippet/notebook against a COPY, but
        that only redirects ``F3DASM_CANONICAL_STORE`` — the subprocess is a
        plain OS process with full filesystem access, and ``cwd=sandbox``
        restricts nothing. Agent code using an absolute path can still read,
        rewrite or delete the real files (run 20260926T124841: a RunScratch
        call ``os.remove()``'d real experiment_data rows and wrote a
        fabricated replacement in their place). This is the backstop for
        that class of damage.

        pipeline.ipynb is reverted UNCONDITIONALLY when changed: only the
        strategizer writes it, and only through WriteCell, so any other
        change to it is definitionally the snippet/notebook run, never a
        legitimate concurrent writer.

        The canonical store is NEVER reverted on a PURE APPEND (old content
        an exact byte prefix of new — see ``_classify_file_change``), no
        matter what the delegation registry says. A delegation's own registry
        entry is not proof it is the only writer that can still be running:
        a worker's backgrounded shell (``Bash(run_in_background=True)``,
        reaped only by the RUN's watchdog at exit — see
        ``infra/watchdog_cleanup.py`` / ``watchdog_launcher.py`` — never per
        delegation) or an async solve pool (e.g. a SlurmAsyncPool backing a
        long external solver call) can flush a real, metered evaluation into the
        store after its delegation already reads Done, or after a
        cooperative-cancellation Cancelled. Reverting one of those rows —
        priority 2's supercompressible study is exactly this shape, one
        solve running minutes to hours — is worse than the damage this
        guard exists to catch, so a pure append is always reported (the
        changed path, left for the critic/analysis to see) and NEVER touched.

        A DESTRUCTIVE store change (a rewrite of existing bytes, or a
        deletion — anything that is not a pure append, where a brand-new
        file counts as an append: a late writer's FIRST evaluation in a
        store, or the first write to a brand-new design-namespace store,
        creates files rather than growing them, and deleting one of those
        is the same data-loss case) is the only thing ever reverted, and
        only when no delegation was
        Working/FollowUp at EITHER end of the call (``_any_delegation_running``,
        checked before AND after — a delegation that starts and finishes
        entirely inside the call's window without being caught by either
        check is a residual gap this accepts, matching the same before/after
        check used everywhere else in this file, e.g. WriteCell's rev guard).
        This is the actual damage observed in run 20260926T124841
        (os.remove() + a hand-written replacement) and stays fully caught.

        Residual gap, by design: a FABRICATED row appended (not rewritten) by
        the snippet is indistinguishable from a real concurrent eval and so
        is reported, never reverted. Acceptable — it is visible in the error
        and diagnostics.jsonl, not silent — but real; see the fix commit's
        DEFERRED section.

        Each store snapshot (before and after) is taken under that store's
        OWN ``.lock`` (the same ``FileLock`` ``InstrumentedDataGenerator``
        flushes evals under — see evaluation/instrumented.py) so a snapshot
        can never land mid-write and read a torn CSV. The lock is held only
        for the snapshot, not across the snippet's run, so a live campaign's
        writes are never stalled by it. Best-effort: a lock that cannot be
        acquired within 5s is skipped (never blocks a run on a diagnostic
        backstop) and that file is simply read unlocked.

        Yields a one-item list; after the block it holds ``None`` if nothing
        changed, else ``{"reverted": [...], "left_in_place": [...]}`` — the
        caller only needs it to report what happened."""
        from filelock import FileLock
        from filelock import Timeout as _LockTimeout

        node = self.node
        store_dirs: list[Path] = []
        run_dir = node._resolve_run_dir()
        if run_dir is not None:
            from ....evaluation.ledger_summary import experiment_stores
            for _root in experiment_stores(run_dir):
                _data_dir = _root / "experiment_data"
                if _data_dir.exists():
                    store_dirs.append(_data_dir)
        nb_paths: list[Path] = []
        if node._study_dir is not None:
            nb_path = Path(node._study_dir) / "pipeline.ipynb"
            if nb_path.exists():
                nb_paths.append(nb_path)

        def _snapshot() -> dict[Path, bytes]:
            contents: dict[Path, bytes] = {}
            for d in store_dirs:
                lock = FileLock(str(d / ".lock"))
                try:
                    with lock.acquire(timeout=5):
                        for f in sorted(d.rglob("*")):
                            if f.is_file():
                                contents[f] = f.read_bytes()
                except _LockTimeout:
                    for f in sorted(d.rglob("*")):
                        if f.is_file():
                            contents[f] = f.read_bytes()
            for n in nb_paths:
                if n.exists():
                    contents[n] = n.read_bytes()
            return contents

        before = _snapshot()
        was_running_before = self._any_delegation_running()
        report: list = [None]
        try:
            yield report
        finally:
            after = _snapshot()
            was_running_after = self._any_delegation_running()
            may_be_concurrent = was_running_before or was_running_after

            reverted_paths: list[str] = []
            left_in_place: list[str] = []
            for p in set(before) | set(after):
                b, a = before.get(p), after.get(p)
                kind = _classify_file_change(b, a)
                if kind == "unchanged":
                    continue
                is_nb = any(str(p) == str(n) for n in nb_paths)
                if kind == "append" and not is_nb:
                    left_in_place.append(str(p))
                    continue
                # 'other' (rewrite/delete/new-file), or ANY pipeline.ipynb
                # change (never a legitimate concurrent writer).
                if is_nb or not may_be_concurrent:
                    if b is None:
                        p.unlink(missing_ok=True)
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(b)
                    reverted_paths.append(str(p))
                else:
                    left_in_place.append(str(p))

            if reverted_paths or left_in_place:
                report[0] = {
                    "reverted": sorted(reverted_paths),
                    "left_in_place": sorted(left_in_place),
                }

    # ── Writing and checking the deliverable ─────────────────────────────────

    @tool_examples("WriteDeliverable('replicate.py', content='...')")
    def WriteDeliverable(self, filename: str, content: str) -> str:
        """Write an extra deliverable file that the study's config declares
        (config.yaml required_deliverables, e.g. replicate.py), verbatim, into
        the study directory. `filename` is a bare name. The notebook is not
        written here — it has its own cell tools, which keep its structure."""
        node = self.node
        if node._study_dir is None:
            return "ERROR: study_dir not available."
        p = Path(filename)
        if "/" in filename or "\\" in filename:
            return "ERROR: filename must be a bare name (no path separators)."
        # Only the AUX deliverables the study declared: the Done() gate
        # REQUIRES those, so a tool must be able to write them or the run
        # deadlocks (audit run 20260624T021359). The notebook is NOT one of
        # them — writing it raw would bypass the structure the cell tools
        # enforce (named pillars, required WHY-explainers, rev guards).
        _required_aux = {
            Path(x).name for x in (getattr(node, "_required_deliverables", None) or [])
        }
        if p.name not in _required_aux:
            declared = sorted(_required_aux) or "none"
            return (
                f"ERROR: {filename!r} is not a declared extra deliverable "
                f"(declared: {declared}). The notebook is written cell by cell "
                "with its own tools, not as a file."
            )
        target = Path(node._study_dir) / p.name
        target.write_text(content, encoding="utf-8")
        return f"Written: {target}"

    def _gate_check(self) -> str:
        """RunNotebook(gate=True): the Done() reproduction gate as a dry run."""
        from ....evaluation.notebook_exec import required_deliverable_name
        node = self.node
        _dname = required_deliverable_name()
        if not (Path(node._study_dir) / _dname).exists():
            # A no-op (nothing to check) does NOT consume the budget.
            return (f"No {_dname} yet — author it first with "
                    "WriteCell, then RunNotebook(gate=True).")
        empty = self._empty_store_refusal()
        if empty is not None:
            return empty
        _BUDGET = 10
        prior = getattr(node, "_check_deliverable_calls", 0)
        if prior >= _BUDGET:
            return (f"Gate-check budget exhausted ({_BUDGET}/"
                    f"{_BUDGET} used). Stop iterating — write a correct lazy "
                    "pipeline in ONE decisive edit (re-read the LAST error; the "
                    "fix is usually 'load the store and skip finished rows', "
                    "not a fresh rewrite), or call Done() to close now (the run "
                    "is recorded FAILED if it still doesn't reproduce).")
        node._check_deliverable_calls = prior + 1
        used = node._check_deliverable_calls
        left = _BUDGET - used
        # Show the budget on EVERY call so the agent paces itself and never hits
        # an unseen wall.
        footer = (
            f"\n\n[RunNotebook(gate=True): {used}/{_BUDGET} used — {left} check"
            f"{'s' if left != 1 else ''} left before you must close with "
            "Done().]")
        problem = node._reproduction_gate()
        if problem is None:
            ok = getattr(node, "_repro_ok_detail", "reproduces cleanly")
            return ("PASS — pipeline.ipynb " + ok
                    + ". Call Done() now to close." + footer)
        return ("NOT YET — pipeline.ipynb failed the reproduction gate. "
                "Fix the exact problem below and RunNotebook(gate=True) again:\n\n"
                + problem + footer)

    def _empty_store_refusal(self) -> str | None:
        """Refuse to check a notebook that has nothing to reproduce, else None.

        Keys on POPULATED output.csv rows, NOT jobs.csv 'FINISHED' status:
        reproduction loads via ExperimentData.from_file() (output.csv), which
        carries values regardless of job status, and a stray worker
        ``data.store()`` can reset FINISHED→IN_PROGRESS without dropping any
        data. Checking jobs.csv here made this guard misfire on a store that is
        fully present but whose statuses were clobbered (the "no FINISHED rows"
        false positive).

        Checked across EVERY store (default + every design namespace) via
        experiment_stores() — a namespace-only run's default output.csv can
        exist but be empty, which false-blocked the gate check even though
        the store was populated (backlog #21's sibling gap).
        """
        _run_dir = self.node._resolve_run_dir()
        if _run_dir is None:
            return None
        from ....evaluation.ledger_summary import (
            RunStateSummary,
            experiment_stores,
        )
        _store_root = _run_dir / "experiment_data"
        _has_rows = any(
            RunStateSummary.from_store(s) is not None
            for s in experiment_stores(_store_root)
        )
        if _has_rows:
            return None
        return (
            "RunNotebook(gate=True): canonical store has no evaluations yet. "
            "Run at least one evaluation campaign before checking the gate.")

    # ── Structured notebook authoring ────────────────────────────────────────

    @tool_examples(
        "WriteCell('doe', why='Latin hypercube over the 3 design variables — "
        "space-filling before any surrogate.', code='data = ...')",
        "WriteCell('problem', content='Maximise the buckling load at fixed "
        "mass.')",
        "WriteCell('doe', old='n_samples=50', new='n_samples=80')",
        "WriteCell('ml', code='...', expected_rev='3f9a1c')",
        "WriteCell('ml', delete=True, expected_rev='3f9a1c')",
    )
    def WriteCell(self, name: str, code: str = None, why: str = None,
                  content: str = None, old: str = None, new: str = None,
                  expected_rev: str = None, delete: bool = False) -> str:
        """Create, edit or delete ONE named cell of pipeline.ipynb.

        `name` is the cell: a pillar (doe, data_generation, ml, optimization,
        analysis), its '<pillar>__why' explainer, a narrative cell — 'problem',
        'hypotheses' and 'verdict' get a canonical heading added for you — or
        any custom name (a bespoke section, appended after the standard
        cells). Cells stay in canonical order whatever the call order;
        pipeline.ipynb is created if absent.

        CREATE (the cell does not exist yet):
          • a CODE cell: pass `code` and `why` — the rationale markdown shown
            above it (cite the literature; if a pillar was not run, say 'NOT
            executed (budget)' and why). The analysis cell must derive the
            headline from the store and print exactly 'REPRODUCED: <value>'.
          • a MARKDOWN cell: pass `content`.
        EDIT (the cell exists):
          • SURGICAL: `old`/`new` — literal find/replace; `old` must occur
            EXACTLY once, so no rev is needed.
          • FULL: `code`/`why` for a code cell or `content` for a markdown
            cell, PLUS `expected_rev` — the rev from ShowNotebook or your last
            write. A stale rev is rejected, so you never overwrite a cell that
            changed since you last saw it.
        DELETE: `delete=True` + `expected_rev`. A pillar goes with its
          explainer. Drop what you decided not to keep rather than leaving
          dead or placeholder content."""
        node = self.node
        if node._study_dir is None:
            return ("ERROR: no study directory is set for this run — an "
                    "infrastructure condition, not something you did. The "
                    "notebook can't be written; report it if unexpected.")
        name = (name or "").strip()
        if not name:
            return ("ERROR: `name` is required — the cell to write: a pillar "
                    "(doe, data_generation, ml, optimization, analysis), a "
                    "narrative cell (problem, hypotheses, verdict) or a custom "
                    "name.")
        if delete:
            if any(v is not None for v in (code, why, content, old, new)):
                return ("ERROR: delete=True takes only `name` and "
                        "`expected_rev`.")
            return self._delete_cell(name, expected_rev)
        nb, _ = self._load_or_new_notebook()
        if name in _by_name(nb):
            return self._edit_cell(name, why, code, content, old, new,
                                   expected_rev)
        if old is not None or new is not None or expected_rev is not None:
            return (f"ERROR: {name!r} is not in pipeline.ipynb yet, so there is "
                    "nothing to edit. Create it (no rev needed): `code` + `why` "
                    "for a code cell, `content` for a markdown cell.")
        if code is not None or why is not None:
            if content is not None:
                return ("ERROR: pass `code` + `why` (a code cell) OR `content` "
                        "(a markdown cell), not both.")
            return self._add_code_cell(name, why, code)
        if content is not None:
            return self._add_markdown_cell(name, content)
        return ("ERROR: nothing to write. Create a code cell with `code` + "
                "`why`, a markdown cell with `content`.")

    def _add_markdown_cell(self, name: str, content: str) -> str:
        """WriteCell's markdown CREATE path. The three reserved names get a
        canonical heading; any other name is a bespoke section appended after
        the standard cells, `content` verbatim — the deliverable's structure
        must not block what the agent needs to say. A pillar name or a
        `<pillar>__why` name belongs to a code cell and is refused."""
        import nbformat
        node = self.node
        if node._study_dir is None:
            return "ERROR: study_dir not available."
        name = (name or "").strip()
        if not name:
            return "ERROR: `name` is required."
        if name in _PILLARS or (name.endswith("__why")
                                and name[:-len("__why")] in _PILLARS):
            return (f"ERROR: {name!r} belongs to a pillar's code cell or its "
                    "WHY-explainer — create it with `code` + `why`.")
        if not (content or "").strip():
            return f"ERROR: `content` is empty for {name!r}."
        nb, nb_path = self._load_or_new_notebook()
        by = _by_name(nb)
        if name in by:
            return (f"ERROR: {name!r} already exists "
                    f"(rev {_rev(by[name].get('source', ''))}) — edit it "
                    "instead.")
        _custom_name = name not in _NARRATIVE
        source = (
            content if _custom_name
            else _NARRATIVE[name] + "\n\n" + _strip_leading_md_header(content)
        )
        cell = nbformat.v4.new_markdown_cell(source)
        cell.metadata["name"] = name
        by[name] = cell
        self._emit_notebook(by, nb, nb_path)
        if _custom_name:
            return (f"Added custom {name!r} markdown cell (rev "
                    f"{_rev(cell['source'])}) to pipeline.ipynb, after the "
                    "standard cells. Edit or delete it any time with WriteCell.")
        return f"Added {name} cell (rev {_rev(cell['source'])}) to pipeline.ipynb."

    def _add_code_cell(self, phase: str, why: str, code: str) -> str:
        """WriteCell's code CREATE path: one cell plus its WHY-explainer. A
        non-pillar `phase` is a custom section appended after the pillars —
        adding a cell is fully reversible, so it proceeds with a tip, never a
        refusal: the structure must not constrain what science can say."""
        import nbformat
        node = self.node
        if node._study_dir is None:
            return ("ERROR: no study directory is set for this run — an "
                    "infrastructure condition, not something you did. The "
                    "notebook can't be written; report it if unexpected.")
        phase = (phase or "").strip()
        if not phase:
            return ("ERROR: `phase` is required — the section this cell belongs "
                    "to. Use a standard pillar (doe, data_generation, ml, "
                    "optimization, analysis) or a custom name for a bespoke "
                    "design/analysis section.")
        # The five pillars are the USUAL shape, not a fence. A custom design or
        # analysis can warrant its own section. Adding a cell is fully reversible
        # (edit/delete it), so a non-pillar phase just PROCEEDS with a tip — never
        # a refusal or a confirm. The deliverable's structure must not constrain
        # what science can be expressed.
        _custom_phase = phase not in _PILLARS
        if not (why or "").strip():
            return ("ERROR: `why` is required — every pillar cell needs its "
                    "rationale (the WHY-explainer the writeup always lacked).")
        if not (code or "").strip():
            return f"ERROR: `code` is empty for phase {phase!r}."
        nb, nb_path = self._load_or_new_notebook()
        by = _by_name(nb)
        if phase in by:
            return (f"ERROR: phase {phase!r} already exists "
                    f"(rev {_rev(by[phase].get('source', ''))}) — edit it "
                    "instead.")
        wc = nbformat.v4.new_markdown_cell(
            f"### {phase}\n\n" + _strip_leading_md_header(why))
        wc.metadata["name"] = f"{phase}__why"
        cc = nbformat.v4.new_code_cell(code)
        cc.metadata["name"] = phase
        cc.metadata["tags"] = [phase]
        by[f"{phase}__why"], by[phase] = wc, cc
        self._emit_notebook(by, nb, nb_path)
        if _custom_phase:
            return (f"Added custom '{phase}' cell (rev {_rev(code)}) to "
                    f"pipeline.ipynb, after the standard pillars. '{phase}' isn't "
                    f"one of the usual pillars {_PILLARS} — fine for a custom "
                    "design/analysis section; edit or delete it any time with "
                    "WriteCell.")
        present = [p for p in _PILLARS if p in by]
        missing = [p for p in _PILLARS if p not in by]
        return (f"Added {phase} cell (rev {_rev(code)}) to "
                f"pipeline.ipynb. Pillars present: {present}."
                + (f" Still missing: {missing}." if missing else
                   " All pillars present — verify with RunNotebook(gate=True)."))

    def _edit_cell(self, name: str, why: str = None, code: str = None,
                   content: str = None, old: str = None, new: str = None,
                   expected_rev: str = None) -> str:
        """WriteCell's EDIT path: surgical `old`/`new`, or a full-field value
        guarded by `expected_rev`."""
        node = self.node
        if node._study_dir is None:
            return "ERROR: study_dir not available."
        name = (name or "").strip()
        surgical = old is not None or new is not None
        full = [k for k, v in (("code", code), ("why", why), ("content", content))
                if v is not None]
        if surgical and full:
            return ("ERROR: pass EITHER surgical `old`/`new` OR a full-field value "
                    f"({'/'.join(full)}), not both.")
        if not surgical and not full:
            return ("ERROR: pass `old`/`new` (surgical), or a full-field value: "
                    "`code`/`why` for a pillar, `content` for a markdown cell.")
        nb, nb_path = self._load_or_new_notebook()
        by = _by_name(nb)
        # No static name-whitelist gate here: a custom cell (a free-form
        # markdown name, or a custom code phase) is a real cell in the
        # notebook but isn't in the
        # canonical _NB_ORDER list — this check, against the notebook's ACTUAL
        # contents, is what correctly distinguishes "doesn't exist yet" from
        # "exists, edit it" for both canonical and custom names alike.
        if name not in by:
            return (f"ERROR: {name!r} is not in pipeline.ipynb yet — "
                    "create it first. "
                    f"Present: {[k for k in _NB_ORDER if k in by]}.")
        cur_rev = _rev(by[name].get("source", ""))
        if surgical:
            problem = self._apply_surgical_edit(by, name, old, new, cur_rev)
        else:
            problem = self._apply_full_field_edit(
                by, name, why, code, content, expected_rev, cur_rev)
        if problem is not None:
            return problem
        self._emit_notebook(by, nb, nb_path)
        new_rev = _rev(by[name].get("source", ""))
        return f"Edited {name} in pipeline.ipynb (rev {cur_rev} → {new_rev})."

    def _apply_surgical_edit(
        self, by: dict, name: str, old: str | None, new: str | None, cur_rev: str
    ) -> str | None:
        """Literal find/replace on one cell. Returns a refusal, or None on success.

        Self-guarding: if the cell changed since the author read it, `old` will
        not match, so no expected_rev is needed.
        """
        if old is None or new is None:
            return "ERROR: surgical edit needs BOTH `old` and `new`."
        src = by[name].get("source", "")
        cnt = src.count(old)
        if cnt == 0:
            return (f"ERROR: `old` not found in {name!r} (it may have "
                    f"changed; current rev {cur_rev}). ShowNotebook('{name}') and retry.")
        if cnt > 1:
            return (f"ERROR: `old` occurs {cnt}× in {name!r} — include "
                    "surrounding context so it matches exactly once.")
        by[name]["source"] = src.replace(old, new, 1)
        return None

    def _apply_full_field_edit(
        self,
        by: dict,
        name: str,
        why: str | None,
        code: str | None,
        content: str | None,
        expected_rev: str | None,
        cur_rev: str,
    ) -> str | None:
        """Replace a whole field of one cell. Returns a refusal, or None.

        Optimistic concurrency: the caller must present the rev it last saw, so
        it cannot blindly overwrite a cell that changed underneath it.
        """
        import nbformat
        if expected_rev is None:
            return (f"ERROR: full-field edit requires `expected_rev` "
                    f"({name!r} is at rev {cur_rev}). ShowNotebook('{name}') to "
                    "confirm the content, then pass that rev.")
        if expected_rev != cur_rev:
            return (f"ERROR: {name!r} changed since rev {expected_rev} "
                    f"(now {cur_rev}). ShowNotebook('{name}') to see the current "
                    "content, then retry.")
        if name in _PILLARS:
            if content is not None:
                return (f"ERROR: {name!r} is a pillar — use `code=` and/or "
                        "`why=`, not `content=`.")
            if code is not None:
                if not code.strip():
                    return f"ERROR: `code` is empty for {name!r}."
                by[name]["source"] = code
            if why is not None:
                if not why.strip():
                    return "ERROR: `why` is empty."
                wname = f"{name}__why"
                body = f"### {name}\n\n" + _strip_leading_md_header(why)
                if wname in by:
                    by[wname]["source"] = body
                else:
                    wc = nbformat.v4.new_markdown_cell(body)
                    wc.metadata["name"] = wname
                    by[wname] = wc
            return None
        # markdown cell: problem / hypotheses / verdict / <pillar>__why /
        # a free-form custom name
        if code is not None or why is not None:
            return (f"ERROR: {name!r} is a markdown cell — use `content=`, "
                    "not `code=`/`why=`.")
        if not content.strip():
            return f"ERROR: `content` is empty for {name!r}."
        if name in _NARRATIVE:
            heading = _NARRATIVE[name]
        elif name.endswith("__why"):
            heading = f"### {name[:-len('__why')]}"
        else:
            heading = None  # free-form custom cell — no forced heading
        by[name]["source"] = (
            heading + "\n\n" + _strip_leading_md_header(content)
            if heading is not None else content
        )
        return None

    def _delete_cell(self, name: str, expected_rev: str = None) -> str:
        """WriteCell's DELETE path, guarded by `expected_rev`. A pillar drops
        with its WHY-explainer; any other named cell drops alone."""
        import nbformat
        node = self.node
        if node._study_dir is None:
            return "ERROR: study_dir not available."
        name = (name or "").strip()
        nb, nb_path = self._load_or_new_notebook()
        by = _by_name(nb)
        if name not in by:
            return f"Nothing to delete: {name!r} not in pipeline.ipynb."
        cur_rev = _rev(by[name].get("source", ""))
        if expected_rev is None:
            return (f"ERROR: delete requires `expected_rev` ({name!r} is at "
                    f"rev {cur_rev}). ShowNotebook('{name}') to confirm, then pass that rev.")
        if expected_rev != cur_rev:
            return (f"ERROR: {name!r} changed since rev {expected_rev} "
                    f"(now {cur_rev}). ShowNotebook('{name}') to see the current "
                    "content, then retry the delete.")
        # A pillar drops with its WHY-explainer; any other named cell drops alone.
        targets = {name, f"{name}__why"} if name in _PILLARS else {name}
        nb.cells = [c for c in nb.cells
                    if (c.get("metadata", {}) or {}).get("name") not in targets]
        nbformat.write(nb, str(nb_path))
        present = [p for p in _PILLARS if p in _by_name(nb)]
        return (f"Deleted {name} from pipeline.ipynb. "
                f"Pillars present: {present}.")

    @tool_examples("ShowNotebook()", "ShowNotebook('analysis')")
    def ShowNotebook(self, name: str = None) -> str:
        """Read pipeline.ipynb back. NO argument → a BRIEF table of contents
        LISTING EVERY CELL BY NAME in canonical order, each with its type, rev,
        and first source line, plus which pillars are present/missing — call this
        to see what exists before editing, and to remind yourself how the
        different cells are ordered.
        With `name` (problem, hypotheses, verdict, a custom cell, a
        pillar, or a '<pillar>__why' explainer) → the FULL source of that cell and
        its rev (the rev you then pass as `expected_rev` to WriteCell).
        Read-only; never creates the file; free."""
        node = self.node
        if node._study_dir is None:
            return "ERROR: study_dir not available."
        nb_path = Path(node._study_dir) / "pipeline.ipynb"
        if not nb_path.exists():
            return ("pipeline.ipynb does not exist yet — author it with "
                             "WriteCell.")
        nb, _ = self._load_or_new_notebook()
        by = _by_name(nb)
        if name is not None:
            name = name.strip()
            if name not in by:
                _present = ([k for k in _NB_ORDER if k in by]
                            + [k for k in by if k not in _NB_ORDER])
                return (f"ERROR: no cell named {name!r}. Present: "
                        f"{_present}.")
            c = by[name]
            src = c.get("source", "")
            ctype = c.get("cell_type", "?")
            return (f"--- {name} ({ctype}, rev {_rev(src)}) ---\n"
                    + (src or "(empty)"))
        # Brief: every named cell, canonical order then extras.
        names = [k for k in _NB_ORDER if k in by]
        names += [k for k in by if k not in _NB_ORDER]
        lines = []
        for nm in names:
            c = by[nm]
            src = c.get("source", "")
            first = (src.splitlines()[0] if src.strip() else "(empty)")[:80]
            lines.append(f"  {nm} ({c.get('cell_type', '?')}, rev {_rev(src)}): "
                         f"{first}")
        present = [p for p in _PILLARS if p in by]
        missing = [p for p in _PILLARS if p not in by]
        return ("pipeline.ipynb cells (canonical order):\n"
                + "\n".join(lines)
                + f"\n\nPillars present: {present}."
                + (f" Missing: {missing}." if missing else " All present."))

    # ── Running things against a copy of the store ───────────────────────────

    @tool_examples(
        "RunScratch(\"from adda import load_experiments; "
        "print({k: len(v) for k, v in load_experiments().items()})\")")
    def RunScratch(self, code: str) -> str:
        """Run a short Python snippet and return its stdout/stderr — your
        scratchpad for INSPECTING state before committing it to
        pipeline.ipynb. f3dasm and adda are importable and
        F3DASM_CANONICAL_STORE points at a temp COPY of the store, so the
        store-loading idiom below reads that copy, not the real one, and
        does NOT count toward the eval budget: e.g. ``from adda import
        load_experiments; experiments = load_experiments()`` (returns every
        store — default AND every design namespace — as ``{name:
        ExperimentData}``; a bare
        ``ExperimentData.from_file(os.environ['F3DASM_CANONICAL_STORE'])``
        only sees the default store and silently misses namespace evals),
        print best values, check a path resolves, or verify a DataFrame
        populates.

        This redirects the STORE-LOADING PATH — it is NOT a filesystem
        sandbox: the snippet is a real OS process with full filesystem
        access, so code using an ABSOLUTE path (``os.remove``,
        ``open(..., 'w')``, ``shutil.*``) still reaches the real
        experiment_data or pipeline.ipynb. Always go through the env var /
        relative-import idiom above, never a hardcoded run path. As a
        backstop, not a substitute for that — the real canonical store and
        pipeline.ipynb are hashed before and after every call; any change is
        reverted and reported as an ERROR here rather than left in place.
        Use it to debug instead of guessing (e.g. 'does hypotheses.json
        load? does h_dict populate?') rather than discovering a silent bug
        only at the Done() gate."""
        import subprocess as _sub
        node = self.node
        if not (code or "").strip():
            return "ERROR: `code` is empty."
        if getattr(node, "_current_notes_dir", None) is None:
            return "ERROR: no run context available for scratch execution."
        with self._ledger_sandbox("f3dasm_scratch_") as (sandbox, env), \
                self._canonical_integrity_guard() as changed:
            snippet = sandbox / "_scratch.py"
            snippet.write_text(code)
            try:
                from ....evaluation.notebook_exec import run_deliverable
                proc = run_deliverable(
                    snippet, cwd=sandbox, env=env, timeout=120)
                out = (proc.stdout or "")[-4000:]
                err = (proc.stderr or "")[-2000:]
                result = (f"[scratch exit {proc.returncode}]\n--- stdout ---\n"
                          + (out or "(empty)")
                          + (f"\n--- stderr ---\n{err}" if err.strip() else ""))
            except _sub.TimeoutExpired:
                result = ("Scratch snippet exceeded 120s and was killed. "
                          "Keep it lightweight — load the store and print; "
                          "do not re-run a campaign.")
        if changed[0]:
            return _integrity_error_message("RunScratch", changed[0]) + "\n\n" + result
        return result

    @tool_examples("RunNotebook()", "RunNotebook(upto='ml')",
                   "RunNotebook(gate=True)")
    def RunNotebook(self, upto: str = None, gate: bool = False) -> str:
        """Execute pipeline.ipynb's cells; does NOT count toward the eval
        budget. F3DASM_CANONICAL_STORE points at a temp COPY of the store
        while it runs, so the notebook's normal store-loading calls read
        that copy. This is NOT a filesystem sandbox (see RunScratch) — the
        real pipeline.ipynb and canonical store are hashed before/after and
        any change is reverted and reported as an ERROR, as a backstop.

        Default → a PER-CELL trace: every code cell with its stdout, and the
        first failure with its exact traceback. `upto` (a code cell's name)
        runs top-to-bottom up to and including that cell — cells share kernel
        state, so this pinpoints WHICH cell breaks reproduction.

        gate=True → the SAME reproduction gate Done() applies, as a dry run
        that does not close the run: the notebook must run cleanly, add zero
        new evals and leave the store unchanged, and its printed
        'REPRODUCED: <value>' headline is surfaced (the critic checks its
        provenance). On a pass, Done()'s gate will pass. Limited to 10 per run,
        so localize a failure with the trace before re-checking the gate."""
        if gate:
            if upto is not None:
                return ("ERROR: gate=True checks the whole notebook — drop "
                        "`upto`.")
            return self._gate_check()
        return self._trace_cells(upto)

    def _trace_cells(self, name: str = None) -> str:
        """RunNotebook's default path: the per-cell trace."""
        import subprocess as _sub
        node = self.node
        nb_path = Path(node._study_dir) / "pipeline.ipynb"
        if not nb_path.exists():
            return "No pipeline.ipynb yet — author it first with WriteCell."
        if getattr(node, "_current_notes_dir", None) is None:
            return "ERROR: no run context available for cell execution."
        with self._ledger_sandbox("f3dasm_cell_") as (sandbox, env), \
                self._canonical_integrity_guard() as changed:
            try:
                from ....evaluation.notebook_exec import diagnose_notebook
                trace = diagnose_notebook(
                    nb_path, cwd=sandbox, env=env, timeout=180, upto_name=name)
                if trace.get("missing_name"):
                    result = (f"No CODE cell named {name!r} — this can be a "
                              "standard pillar (doe, data_generation, ml, "
                              "optimization, analysis) or a custom code cell "
                              "you added, but it must be a code cell (a "
                              "markdown-only cell like 'problem' or "
                              "'verdict' has no execution state to run up "
                              "to). Use ShowNotebook() to see the cells.")
                else:
                    result = _render_cell_trace(trace, name)
            except _sub.TimeoutExpired:
                result = ("Notebook diagnosis exceeded 180s and was killed "
                          "— a cell is running a real campaign; it should "
                          "load the store lazily, not recompute.")
        if changed[0]:
            return _integrity_error_message("RunNotebook", changed[0]) + "\n\n" + result
        return result


def _render_cell_trace(trace: dict, name: str | None) -> str:
    """Format a per-cell execution trace: one line per code cell, then a verdict."""
    lines = []
    scope = f"up to & including '{name}'" if name else "whole notebook"
    for c in trace["cells"]:
        if c["cell_type"] != "code":
            continue
        tag = "ERROR" if c["errored"] else "ok"
        nm = c["name"] or f"cell{c['index']}"
        lines.append(f"  [{tag}] {nm}")
        out = (c["stdout"] or "").strip()
        if out:
            lines.append("        stdout: " + out[-300:].replace("\n", "\n        "))
        if c["errored"]:
            lines.append("        " + (c["error"] or "").strip()[-700:].replace("\n", "\n        "))
    fe = trace["first_error"]
    head = (
        f"{'TIMED OUT — ' if trace['timed_out'] else ''}"
        f"per-cell trace ({scope}, against a COPY of the store):\n"
        + ("\n".join(lines) or "  (no code cells)")
    )
    verdict = (
        f"\n\nFIRST FAILURE: cell '{fe['name'] or fe['index']}' — fix this "
        "cell, then RunNotebook() again, or RunNotebook(gate=True)."
        if fe else
        "\n\nAll code cells ran without error against the copy. If "
        "the gate still fails, the issue is the gate's checks "
        "(zero-new-evals / REPRODUCED line / store unchanged), not a cell "
        "exception."
    )
    return head + verdict


def build_notebook_closures(node) -> dict:
    """The notebook-authoring tools for one node, by registered name."""
    t = NotebookTools(node)
    return {
        "WriteDeliverable": t.WriteDeliverable,
        "WriteCell": t.WriteCell,
        "ShowNotebook": t.ShowNotebook,
        "RunNotebook": t.RunNotebook,
        "RunScratch": t.RunScratch,
    }
