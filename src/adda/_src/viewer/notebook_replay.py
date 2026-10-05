"""Re-execute a run's deliverable from the viewer (spec 14 Phase 5.8).

Runs the notebook through adda's own execution path
(``evaluation/notebook_exec.py``) against a throwaway COPY of the run's ledger,
exactly as the reproduction gate does, so the live store and the notebook file
are never written. The execution happens in a child process: the notebook's
kernel needs the gate's environment variables, and patching ``os.environ`` in
a multi-threaded server would leak into every other request.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import yaml

__all__ = ["ReplayError", "replay_notebook"]

_RESULT_MARK = "ADDA_REPLAY_RESULT "
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
    study_dir: Path | str, run_id: str, *, audit=None,
) -> dict[str, Any]:
    """Replay the run's deliverable and return pass/fail plus its output.

    ``passed`` is the gate's own contract: clean exit, zero new ledger rows,
    existing rows unchanged. ``audit(action, **fields)`` gets the exact
    command line before the child starts and the outcome after.
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
        args = {"notebook": str(notebook), "store_dir": str(store_dir),
                "run_config": str(run_dir / "debug" / "run_config.json"),
                "study_root": str(study_dir), "timeout": timeout}
        cmd = [sys.executable, "-m", __name__, json.dumps(args)]
        if audit:
            audit("reexecute", run_id=run_id, phase="run",
                  command=shlex.join(cmd))
        t0 = time.time()
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd, cwd=study_dir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout + _OVERHEAD_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            result: dict[str, Any] = {
                "passed": False, "timed_out": True,
                "error": f"did not finish within {timeout:.0f}s"}
        else:
            result = _parse(out, err, proc.returncode)
        result.update(run_id=run_id, notebook=notebook.name,
                      duration_s=round(time.time() - t0, 1))
        if audit:
            audit("reexecute", run_id=run_id, phase="done",
                  passed=result["passed"], error=result.get("error"))
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


def _worker(args: dict[str, Any]) -> dict[str, Any]:
    from ..evaluation.notebook_exec import (
        ledger_snapshot,
        replay_sandbox,
        run_deliverable,
    )

    with replay_sandbox(
            Path(args["store_dir"]), Path(args["run_config"]),
            args["study_root"]) as (sandbox, sb_store, env):
        before_n, before_hash = ledger_snapshot(sb_store)
        if before_n == 0:
            return {"passed": False, "timed_out": False,
                    "error": "the run's ledger has no rows to replay against"}
        try:
            proc = run_deliverable(
                Path(args["notebook"]), cwd=sandbox, env=env,
                timeout=args["timeout"])
        except subprocess.TimeoutExpired:
            return {"passed": False, "timed_out": True,
                    "error": f"did not finish within {args['timeout']:.0f}s"}
        after_n, after_hash = ledger_snapshot(sb_store)
    m = re.search(r"REPRODUCED:\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)",
                  proc.stdout or "")
    lazy = after_n == before_n
    unchanged = not (before_hash and after_hash and before_hash != after_hash)
    return {
        "passed": proc.returncode == 0 and lazy and unchanged,
        "timed_out": False, "returncode": proc.returncode,
        "rows_before": before_n, "rows_after": after_n,
        "lazy": lazy, "unchanged": unchanged,
        "reproduced": m.group(1) if m else None,
        "stdout_tail": (proc.stdout or "")[-_TAIL:],
        "stderr_tail": (proc.stderr or "")[-_TAIL:],
    }


if __name__ == "__main__":
    print(_RESULT_MARK + json.dumps(_worker(json.loads(sys.argv[1]))))
