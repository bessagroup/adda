"""Re-execute a run's deliverable from the viewer (spec 14 Phase 5.8).

Runs the notebook through adda's own execution path
(``evaluation/notebook_exec.py``) against a throwaway COPY of the run's ledger,
exactly as the reproduction gate does, so the live store and the notebook file
are never written. The execution happens in a child process: the notebook's
kernel needs the gate's environment variables, and patching ``os.environ`` in
a multi-threaded server would leak into every other request.
"""
from __future__ import annotations

import datetime
import json
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import yaml

__all__ = ["REEXEC_DIR", "ReplayError", "replay_notebook"]

# Beside the run's own records; the run's pipeline.ipynb is never written.
REEXEC_DIR = ("debug", "viewer_reexec")

_RESULT_MARK = "ADDA_REPLAY_RESULT "
_EVENT_MARK = "ADDA_REPLAY_EVENT "
_DEFAULT_TIMEOUT_S = 300.0
# Sandbox copy + snapshots around the notebook's own time limit.
_OVERHEAD_S = 120.0
_TAIL = 3000
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class ReplayError(Exception):
    """The notebook could not be replayed (nothing to run, or already running)."""


def _timeout_s(study_dir: Path) -> float:
    """The gate's own rule: a tenth of the wall budget, at least 180 s."""
    from ..runtime.run_setup import _parse_budget_str
    try:
        cfg = yaml.safe_load(
            (study_dir / "config.yaml").read_text(encoding="utf-8")) or {}
        budget = _parse_budget_str(cfg.get("budget"))
    except (OSError, yaml.YAMLError, TypeError, ValueError, AttributeError):
        budget = None
    return max(0.1 * budget, 180.0) if budget else _DEFAULT_TIMEOUT_S


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the worker this module started and its descendants, nothing else."""
    try:
        tree = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        tree = []
    for p in [*tree, proc]:
        try:
            p.kill()
        except (psutil.Error, OSError):
            pass


def replay_notebook(
    study_dir: Path | str, run_id: str, *, audit=None, on_event=None,
) -> dict[str, Any]:
    """Replay the run's deliverable and return pass/fail plus its output.

    ``passed`` is the gate's own contract: clean exit, zero new ledger rows,
    existing rows unchanged. ``audit(action, **fields)`` gets the exact
    command line before the child starts and the outcome after.
    ``on_event(event)`` is called from this thread as the worker progresses:
    ``{"event": "started" | "phase" | "cell", "line": <display text>, ...}``.
    """
    from . import readers

    study_dir = Path(study_dir).resolve()
    run_dir = study_dir / "runs" / run_id
    notebook = readers.notebook_path(study_dir, run_id)
    if notebook is None:
        raise ReplayError(f"run {run_id!r} has no pipeline notebook")
    store_dir = run_dir / "experiment_data"
    if not store_dir.is_dir():
        raise ReplayError(
            f"run {run_id!r} has no experiment_data/ to replay against")

    with _locks_guard:
        lock = _locks.setdefault(str(study_dir), threading.Lock())
    if not lock.acquire(blocking=False):
        raise ReplayError("a re-execution is already running for this study")
    try:
        timeout = _timeout_s(study_dir)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args = {"notebook": str(notebook), "store_dir": str(store_dir),
                "save_dir": str(run_dir.joinpath(*REEXEC_DIR)), "stamp": stamp,
                "run_config": str(run_dir / "debug" / "run_config.json"),
                "study_root": str(study_dir), "timeout": timeout}
        cmd = [sys.executable, "-m", __name__, json.dumps(args)]
        if audit:
            audit("reexecute", run_id=run_id, phase="run",
                  command=shlex.join(cmd))
        emit = on_event or (lambda e: None)
        emit({"event": "started", "run_id": run_id, "notebook": notebook.name,
              "line": f"Re-executing {notebook.name} against a copy of the run's ledger"})
        t0 = time.time()
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd, cwd=study_dir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        out_lines: list[str] = []
        err_parts: list[str] = []

        def _pump_out() -> None:
            for line in proc.stdout:
                out_lines.append(line)
                if line.startswith(_EVENT_MARK):
                    try:
                        emit(json.loads(line[len(_EVENT_MARK):]))
                    except ValueError:
                        pass

        pumps = [threading.Thread(target=_pump_out, daemon=True),
                 threading.Thread(
                     target=lambda: err_parts.append(proc.stderr.read()),
                     daemon=True)]
        for t in pumps:
            t.start()
        try:
            proc.wait(timeout=timeout + _OVERHEAD_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait()
            for t in pumps:
                t.join(5)
            result: dict[str, Any] = {
                "passed": False, "timed_out": True,
                "error": f"did not finish within {timeout:.0f}s"}
        else:
            for t in pumps:
                t.join(5)
            result = _parse("".join(out_lines), "".join(err_parts),
                            proc.returncode)
        result.update(run_id=run_id, notebook=notebook.name,
                      duration_s=round(time.time() - t0, 1))
        if result.get("saved"):
            result["reexec_id"] = stamp
        if audit:
            audit("reexecute", run_id=run_id, phase="done",
                  passed=result["passed"], error=result.get("error"),
                  saved=result.get("saved"))
        return result
    finally:
        lock.release()


def _parse(out: str, err: str, returncode: int) -> dict[str, Any]:
    for line in reversed(out.splitlines()):
        if line.startswith(_RESULT_MARK):
            return json.loads(line[len(_RESULT_MARK):])
    return {"passed": False, "timed_out": False,
            "error": f"the replay worker exited {returncode} without a result",
            "stderr_tail": err[-_TAIL:]}


def _event(event: str, line: str, **fields: Any) -> None:
    print(_EVENT_MARK + json.dumps({"event": event, "line": line, **fields}),
          flush=True)


def _adda_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _worker(args: dict[str, Any]) -> dict[str, Any]:
    from ..evaluation.notebook_exec import (
        ledger_snapshot,
        parse_headline,
        replay_sandbox,
        run_deliverable,
    )

    _event("phase", "Copying the run's ledger to a throwaway sandbox", name="sandbox")

    with replay_sandbox(
            Path(args["store_dir"]), Path(args["run_config"]),
            args["study_root"]) as (sandbox, sb_store, env):
        before_n, before_hash = ledger_snapshot(sb_store)
        _event("phase", f"Ledger snapshot: {before_n} rows", name="ledger", rows=before_n)
        if before_n == 0:
            return {"passed": False, "timed_out": False,
                    "error": "the run's ledger has no rows to replay against"}
        _event("phase", "Starting the notebook kernel", name="execute")

        def _on_cell(done: int, total: int, errored: bool) -> None:
            _event("cell", f"Cell {done} of {total} " + ("raised an error" if errored else "done"),
                   done=done, total=total, errored=errored)
        save_dir, stamp = Path(args["save_dir"]), args["stamp"]
        saved_nb = save_dir / f"pipeline_{stamp}.ipynb"
        try:
            proc = run_deliverable(
                Path(args["notebook"]), cwd=sandbox, env=env,
                timeout=args["timeout"], on_cell=_on_cell, save_to=saved_nb)
        except subprocess.TimeoutExpired:
            return {"passed": False, "timed_out": True,
                    "error": f"did not finish within {args['timeout']:.0f}s"}
        _event("phase", "Checking the ledger: no new rows, existing rows unchanged", name="check")
        after_n, after_hash = ledger_snapshot(sb_store)
    headline = parse_headline(proc.stdout)
    lazy = after_n == before_n
    unchanged = not (before_hash and after_hash and before_hash != after_hash)
    passed = proc.returncode == 0 and lazy and unchanged
    saved = saved_nb.name if saved_nb.exists() else None
    if saved:
        (save_dir / f"reexec_{stamp}.json").write_text(json.dumps({
            "id": stamp, "passed": passed, "notebook": saved,
            "source": Path(args["notebook"]).name,
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "adda_commit": _adda_commit(),
            "reproduced": headline["reproduced"], "claimed": headline["claimed"],
            "rows_before": before_n, "rows_after": after_n,
        }), encoding="utf-8")
    return {
        "passed": passed, "saved": saved,
        "timed_out": False, "returncode": proc.returncode,
        "rows_before": before_n, "rows_after": after_n,
        "lazy": lazy, "unchanged": unchanged,
        "reproduced": headline["reproduced"],
        "claimed": headline["claimed"],
        "stdout_tail": (proc.stdout or "")[-_TAIL:],
        "stderr_tail": (proc.stderr or "")[-_TAIL:],
    }


if __name__ == "__main__":
    print(_RESULT_MARK + json.dumps(_worker(json.loads(sys.argv[1]))))
