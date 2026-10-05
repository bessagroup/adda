"""Execute the run deliverable — `pipeline.ipynb` — under the reproduction gate.

The system is committed to the notebook: `pipeline.ipynb` is THE deliverable.
The gate's contract (exit-clean + ZERO new oracle evals + the `REPRODUCED:`
headline grounded in the ledger) executes it lazily via nbclient. The nbclient/
nbformat/ipykernel deps ship with the `agentic` extra; `run_deliverable()`
returns a `subprocess.CompletedProcess`-shaped result and raises
`subprocess.TimeoutExpired` on timeout, so the gate branches on nothing.

Agents author the notebook cell-by-cell via the structured WriteCell /
ShowNotebook closures (pure nbformat, name-addressed) — there is no live kernel.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

__all__ = [
    "notebook_available",
    "required_deliverable_name",
    "run_deliverable",
    "build_notebook",
    "notebook_deliverable_spec",
    "repair_code_cells",
    "sandbox_env",
    "stamp_run_provenance",
    "ledger_snapshot",
    "replay_sandbox",
]

# Stable name for the run-provenance metadata cell (see stamp_run_provenance).
RUN_PROVENANCE_CELL = "_run_provenance"


def stamp_run_provenance(nb, meta_md: str):
    """Stamp the run-provenance metadata cell onto the deliverable notebook,
    REPLACING any prior one instead of appending.

    pipeline.ipynb is study-scoped and persists across runs, so appending an
    UNNAMED stamp cell each close accumulated stale metadata — a run's
    deliverable carried the PREVIOUS run's run_dir / evals_used (run
    20260630T164908 shipped 20260629's stamp). The cell is given a stable
    ``metadata["name"]`` so (a) the next close replaces it rather than piling
    up, and (b) it is visible/removable via ShowNotebook / WriteCell
    like every other named cell.
    """
    import nbformat
    nb.cells = [
        c for c in nb.cells
        if (getattr(c, "metadata", None) or {}).get("name")
        != RUN_PROVENANCE_CELL
    ]
    cell = nbformat.v4.new_markdown_cell(meta_md)
    cell.metadata["name"] = RUN_PROVENANCE_CELL
    nb.cells.append(cell)
    return nb


def repair_code_cells(nb) -> None:
    """Backfill `outputs`/`execution_count` on any code cell missing them, in
    place. `nbformat.reads()`/`nbformat.read()` accept a code cell missing
    `outputs` with no validation error (confirmed empirically — `normalize()`
    doesn't add it either), but `nbformat.write`'s `split_lines` (nbformat's
    own rwbase.py) unconditionally iterates `cell.outputs` for every
    `cell_type == "code"` cell and raises `AttributeError: outputs` the next
    time ANYTHING writes that notebook. A hand-authored notebook (the cell
    tools always use `nbformat.v4.new_code_cell` and never have this gap) is
    the way a cell reaches disk missing this field — call this right after every
    `nbformat.read`/`reads()` so no downstream write can crash on it."""
    for cell in nb.cells:
        if cell.get("cell_type") == "code":
            cell.setdefault("outputs", [])
            cell.setdefault("execution_count", None)


def sandbox_env(
    sb_store,
    sb_run_config,
    study_root=None,
    delegation_id: str = "D999",
    base=None,
) -> dict:
    """Build the environment for running a deliverable / cell / scratch snippet
    against a SANDBOX copy of the ledger.

    F3DASM_CANONICAL_STORE / F3DASM_RUN_CONFIG point at the sandbox copy so no
    execution can touch the real store. F3DASM_STUDY_ROOT is a READ-ONLY anchor
    to the real study repo, so pipeline cells can locate non-ledger resources
    (e.g. ``bo/cei_core.py`` for a surrogate self-check) deterministically
    instead of hand-rolling multi-candidate path searches relative to the
    store. Isolation is unaffected: only the store is a copy; the study root is
    read-only reference code.

    F3DASM_DEDUP_SCOPE=all: this is a validation REPLAY of already-generated
    data (re-executing ``data_generation`` must add ZERO new rows to satisfy
    the "LAZY" reproduction invariant below), not a live campaign delegation
    doing new science — but this process is stamped with a fixed synthetic
    ``delegation_id`` (default "D999") that never matches the real
    delegation(s) that actually generated the ledger's rows (D005, D009,
    ...). ``get_evaluator()``'s default dedup scope only recognizes a design
    as already-seen when the SAME delegation id wrote it (correct for real
    concurrent campaigns, where a different delegation legitimately
    re-measuring a design is not waste) — under that default here, dedup
    would silently fail to recognize ANY existing row, so replaying
    ``data_generation`` would re-add every design point as if new. Forcing
    "all" scope here means dedup checks the WHOLE ledger regardless of which
    delegation wrote each row, matching what this replay actually needs.
    """
    env = dict(os.environ if base is None else base)
    env["F3DASM_CANONICAL_STORE"] = str(sb_store)
    env["F3DASM_RUN_CONFIG"] = str(sb_run_config)
    if study_root is not None:
        env["F3DASM_STUDY_ROOT"] = str(study_root)
    env.setdefault("F3DASM_DELEGATION_ID", str(delegation_id))
    env.setdefault("F3DASM_DEDUP_SCOPE", "all")
    return env


# The canonical notebook structure — ONE source, referenced by the agent prompt
# (notebook_deliverable_spec), KB 0009, and any fallback builder. The spine is the
# Popperian loop (hypotheses → falsification attempt → verdict); the body mirrors
# f3dasm's four pillars (Phase enum: doe / data_generation / ml / optimization),
# each a name-tagged code cell preceded by a WHY explainer markdown cell.
def notebook_deliverable_spec(role: str = "strategizer") -> str:
    """The deliverable contract — injected into the relevant agent prompts so
    the .ipynb is a reproducible scientific narrative, not a ported .py.

    Role-aware: ONLY the strategizer authors the notebook (it alone is granted
    the notebook tools), so only it gets the "author with these tools"
    section. The implementer (writes phase code to its workspace) and the
    critic (judges the notebook) get the same STRUCTURE + RULES so their work
    fits / is judged against it — but no instruction to call tools they do
    not have. The text itself lives in ``prompts/deliverable_format.py``, one
    named constant per section."""
    from ..prompts.deliverable_format import (
        DELIVERABLE_AUTHORING_STRATEGIZER,
        DELIVERABLE_CONTEXT_CRITIC,
        DELIVERABLE_CONTEXT_IMPLEMENTER,
        DELIVERABLE_FORMAT,
    )
    if role == "strategizer":
        head = ("\n<deliverable_authoring>\n" + DELIVERABLE_AUTHORING_STRATEGIZER
                + "</deliverable_authoring>\n")
    else:
        body = (DELIVERABLE_CONTEXT_IMPLEMENTER if role == "implementer"
                else DELIVERABLE_CONTEXT_CRITIC)
        head = "\n<deliverable_context>\n" + body + "</deliverable_context>\n"
    return head + "<deliverable_format>\n" + DELIVERABLE_FORMAT + "</deliverable_format>\n"


def notebook_available() -> bool:
    """True iff nbclient + nbformat are importable (ship with the agentic extra)."""
    try:
        import nbclient  # noqa: F401
        import nbformat  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def required_deliverable_name() -> str:
    """The single run deliverable. The system is committed to the notebook —
    pipeline.ipynb is THE deliverable (there is no pipeline.py / solution.md)."""
    return "pipeline.ipynb"


@contextlib.contextmanager
def _patched_environ(env: dict | None):
    """Temporarily replace os.environ with `env` so a freshly-spawned Jupyter
    kernel (which inherits os.environ at launch) sees the gate's injected vars
    (F3DASM_CANONICAL_STORE / F3DASM_RUN_CONFIG / F3DASM_STUDY_ROOT /
    F3DASM_DELEGATION_ID)."""
    if env is None:
        yield
        return
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _execute_notebook(path: Path, cwd: Path, env: dict, timeout: float):
    """Execute a notebook in THIS interpreter's env (so f3dasm/get_evaluator
    import) and return a CompletedProcess-shaped result. Raises
    subprocess.TimeoutExpired on timeout (mirrors the .py path)."""
    import nbformat
    from jupyter_client.manager import KernelManager
    from nbclient import NotebookClient
    from nbclient.exceptions import CellTimeoutError, DeadKernelError

    nb = nbformat.read(str(path), as_version=4)
    # Pin the kernel to THIS interpreter so the notebook runs in the env that
    # has f3dasm installed (the global "python3" kernelspec may point elsewhere).
    km = KernelManager(kernel_name="python3")
    try:
        km.kernel_spec.argv[0] = sys.executable
    except Exception:  # noqa: BLE001 — fall back to the spec's python
        pass
    client = NotebookClient(
        nb, km=km, timeout=int(timeout),
        allow_errors=True,                      # capture errors, don't raise
        resources={"metadata": {"path": str(cwd)}},
    )
    with _patched_environ(env):
        try:
            client.execute()
        except (CellTimeoutError, DeadKernelError) as exc:
            raise subprocess.TimeoutExpired(cmd=str(path), timeout=timeout) from exc

    out_parts, err_parts, errored = [], [], False
    for cell in nb.cells:
        for o in cell.get("outputs", []):
            ot = o.get("output_type")
            if ot == "stream":
                (out_parts if o.get("name") == "stdout" else err_parts).append(
                    o.get("text", ""))
            elif ot == "error":
                errored = True
                err_parts.append(
                    f"{o.get('ename', '')}: {o.get('evalue', '')}\n"
                    + "\n".join(o.get("traceback", [])))
    return subprocess.CompletedProcess(
        args=[str(path)],
        returncode=1 if errored else 0,
        stdout="".join(out_parts),
        stderr="".join(err_parts),
    )


def ledger_snapshot(store_root: Path) -> tuple[int, str]:
    """(row_count, content_hash) across EVERY store under store_root:
    the canonical/default store PLUS every design-namespace sibling
    (store_root/<namespace>/), via the same experiment_stores()
    aggregation QueryStore()/ScienceMonitor already use. A single-
    store read here would miss a non-lazy write into a namespace
    store during "reproduction" — the sandbox copy this is called
    against is already namespace-complete (namespace stores nest
    under store_root), only the read needs to look past the default.

    content_hash is order-independent (sorted rounded values, tagged
    by store so identical values in two different stores can't
    false-collide) so a faithful lazy re-store doesn't false-trip it.
    """
    import hashlib

    from .ledger_summary import experiment_stores

    total_rows = 0
    all_rows: list[tuple] = []
    for store in experiment_stores(store_root):
        try:
            from f3dasm import ExperimentData
            data = ExperimentData.from_file(project_dir=store)
            _, out = data.to_pandas()
        except Exception:  # noqa: BLE001
            continue
        total_rows += len(out)
        cols = [c for c in out.columns if not str(c).startswith("_")]
        if not cols:
            continue
        vals = out[cols].round(10)
        all_rows.extend(
            (store.name,) + tuple(r) for r in vals.to_numpy().tolist()
        )
    # key=repr, not a bare sort: a row is (store.name, *column values),
    # and a design space can legitimately mix numeric columns with
    # non-numeric ones (e.g. a string mechanism/family label). Two
    # same-namespace rows that diverge at a non-numeric column crash
    # Python's tuple comparison (float < str is undefined) the moment
    # they're compared — repr() is always string-comparable regardless
    # of what each column holds, and dropping non-numeric columns
    # instead would silently weaken the reproduction hash itself.
    h = hashlib.sha256(
        repr(sorted(all_rows, key=repr)).encode()
    ).hexdigest()
    return total_rows, h


@contextlib.contextmanager
def replay_sandbox(store_dir: Path, run_config: Path, study_root):
    """A throwaway copy of the run's ledger to replay a deliverable against.

    CRITICAL: a deliverable is only ever run against a COPY of the canonical
    store, never the live one. A faithful lazy pipeline adds nothing; a
    NON-lazy one (re-evaluating) writes its evals into the copy, which is how
    it is detected, while the real ledger stays pristine. Yields
    ``(sandbox_dir, sandbox_store, env)``; the directory is removed on exit.
    """
    store_dir, run_config = Path(store_dir), Path(run_config)
    sandbox = Path(tempfile.mkdtemp(prefix="f3dasm_repro_"))
    try:
        sb_store = sandbox / "experiment_data"
        if store_dir.exists():
            shutil.copytree(store_dir, sb_store)
        else:
            sb_store.mkdir(parents=True, exist_ok=True)
        # A sandbox run_config so get_evaluator() also writes to the COPY (it
        # resolves the store from run_config["store_dir"], not the env).
        sb_run_config = sandbox / "run_config.json"
        cfg = json.loads(run_config.read_text()) if run_config.exists() else {}
        cfg["store_dir"] = str(sb_store)
        cfg["lock_path"] = str(sb_store / "experiment_data" / ".lock")
        sb_run_config.write_text(json.dumps(cfg))
        yield sandbox, sb_store, sandbox_env(
            sb_store, sb_run_config, study_root=study_root)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def run_deliverable(path: Path, *, cwd: Path, env: dict, timeout: float):
    """Run the deliverable; return a subprocess.CompletedProcess. `.ipynb` →
    nbclient (in-env kernel); anything else → `python <file>` subprocess.
    Raises subprocess.TimeoutExpired on timeout in BOTH paths."""
    path = Path(path)
    if path.suffix == ".ipynb" and notebook_available():
        return _execute_notebook(path, Path(cwd), env, timeout)
    return subprocess.run(
        [sys.executable, str(path)],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
    )


def diagnose_notebook(path: Path, *, cwd: Path, env: dict, timeout: float,
                      upto_name: str | None = None) -> dict:
    """Execute a notebook and return a PER-CELL execution trace — the granular
    diagnostic the binary ``run_deliverable`` gate aggregates away (#13).

    Mirrors ``_execute_notebook`` exactly (same interpreter-pinned kernel + env
    patch + ``allow_errors=True``) so it reproduces the gate's environment, but
    keeps each cell's outputs instead of merging them. With ``upto_name`` it runs
    only the cells up to AND INCLUDING the code cell carrying that
    ``metadata.name`` — cells share kernel state top-to-bottom, so this localizes
    WHERE reproduction breaks without spuriously failing on missing prior state.

    Returns ``{"cells": [{"index","name","cell_type","errored","stdout",
    "error"}], "first_error": <that cell|None>, "executed": int,
    "truncated": bool, "timed_out": bool, "missing_name": bool}``.
    """
    import re as _re

    import nbformat
    from jupyter_client.manager import KernelManager
    from nbclient import NotebookClient
    from nbclient.exceptions import CellTimeoutError, DeadKernelError

    _ansi = _re.compile(r"\x1b\[[0-9;]*m")  # strip terminal colour codes from tracebacks

    nb = nbformat.read(str(path), as_version=4)
    truncated = False
    if upto_name is not None:
        idx = next(
            (i for i, c in enumerate(nb.cells)
             if c.get("cell_type") == "code"
             and (c.get("metadata", {}) or {}).get("name") == upto_name),
            None,
        )
        if idx is None:
            return {"cells": [], "first_error": None, "executed": 0,
                    "truncated": False, "timed_out": False, "missing_name": True}
        nb.cells = nb.cells[:idx + 1]
        truncated = True

    km = KernelManager(kernel_name="python3")
    try:
        km.kernel_spec.argv[0] = sys.executable
    except Exception:  # noqa: BLE001 — fall back to the spec's python
        pass
    client = NotebookClient(
        nb, km=km, timeout=int(timeout), allow_errors=True,
        resources={"metadata": {"path": str(cwd)}},
    )
    timed_out = False
    with _patched_environ(env):
        try:
            client.execute()
        except (CellTimeoutError, DeadKernelError):
            timed_out = True

    cells, first_error = [], None
    for i, cell in enumerate(nb.cells):
        out_parts, err_parts, errored = [], [], False
        for o in cell.get("outputs", []):
            ot = o.get("output_type")
            if ot == "stream":
                (out_parts if o.get("name") == "stdout" else err_parts).append(
                    o.get("text", ""))
            elif ot == "error":
                errored = True
                err_parts.append(_ansi.sub(
                    "",
                    f"{o.get('ename', '')}: {o.get('evalue', '')}\n"
                    + "\n".join(o.get("traceback", []))))
        rec = {
            "index": i,
            "name": (cell.get("metadata", {}) or {}).get("name"),
            "cell_type": cell.get("cell_type"),
            "errored": errored,
            "stdout": "".join(out_parts),
            "error": "".join(err_parts),
        }
        cells.append(rec)
        if errored and first_error is None:
            first_error = rec
    return {"cells": cells, "first_error": first_error, "executed": len(cells),
            "truncated": truncated, "timed_out": timed_out, "missing_name": False}


def build_notebook(cells: list[dict]):
    """Assemble a notebook from a simple cell list (avoids hand-written JSON).

    Each cell: {"type": "markdown"|"code", "source": str, "name": str|None}.
    The optional ``name`` is written to cell.metadata.name AND cell.metadata.tags
    (mirrors the f3dasm Phase: doe/data_generation/ml/optimization/analysis), so
    phase-presence is machine-checkable. Returns an nbformat NotebookNode.
    """
    import nbformat

    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3",
                                 "language": "python"}
    built = []
    for c in cells:
        src = c.get("source", "")
        if c.get("type") == "markdown":
            cell = nbformat.v4.new_markdown_cell(src)
        else:
            cell = nbformat.v4.new_code_cell(src)
        name = c.get("name")
        if name:
            cell.metadata["name"] = name
            cell.metadata["tags"] = [name]
        built.append(cell)
    nb.cells = built
    return nb
