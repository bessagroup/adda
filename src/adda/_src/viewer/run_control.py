"""Starting and killing a run from the viewer (spec 14 Phase 5.3 / 5.4).

This makes the viewer an execution surface, so the rules are narrow:

* it runs exactly one thing, ``python -m adda.watchdog <study>`` (the budget
  and model come from the study's committed config);
* a study has at most one live run, decided from the run's own records, not
  from the viewer's memory;
* what the viewer started is written to ``runs/_viewer/registry.json``, so a
  restarted viewer still knows. Kill touches only those entries, matched by
  PID *and* process start time (a recycled PID is not ours), plus the
  descendants of that exact PID;
* every start and kill is returned to the caller with the exact command or
  signal, for the audit log.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import yaml

from . import readers

__all__ = [
    "LIVE_WINDOW_S",
    "KILL_GRACE_S",
    "preflight",
    "live_run",
    "start_run",
    "kill_run",
    "launched",
    "StartRefused",
    "LauncherError",
    "launcher_config",
    "start_via_launcher",
    "stop_via_launcher",
]

# A run directory with no run_status.json is a run that has not closed — or
# one that crashed without closing. Told apart by whether it is still writing.
LIVE_WINDOW_S = 600.0
# SIGTERM to the watchdog reaps the run's tree; this is how long it gets before
# the viewer escalates to SIGKILL.
KILL_GRACE_S = 30.0

_STATE_DIR = "_viewer"
_REGISTRY = "registry.json"


def _state_dir(study_dir: Path) -> Path:
    return Path(study_dir) / "runs" / _STATE_DIR


def _read_registry(study_dir: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(
            (_state_dir(study_dir) / _REGISTRY).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write_registry(study_dir: Path, entries: list[dict[str, Any]]) -> None:
    d = _state_dir(study_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{_REGISTRY}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    os.replace(tmp, d / _REGISTRY)


def _alive(entry: dict[str, Any]) -> psutil.Process | None:
    """The process this entry names, or None. The start time must match, so a
    PID the OS has since handed to something else is never mistaken for ours."""
    try:
        proc = psutil.Process(int(entry["pid"]))
        if abs(proc.create_time() - float(entry["create_time"])) > 1.0:
            return None
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc
    except (psutil.Error, KeyError, TypeError, ValueError):
        return None


def launched(study_dir: Path | str) -> list[dict[str, Any]]:
    """Everything the viewer started for this study, each with ``alive``."""
    out = []
    for entry in _read_registry(Path(study_dir)):
        out.append({**entry, "alive": _alive(entry) is not None})
    return out


def _last_activity(run_dir: Path) -> float:
    newest = 0.0
    try:
        for p in (run_dir / "debug").iterdir():
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return newest


def live_run(study_dir: Path | str) -> dict[str, Any] | None:
    """The study's live run, or None.

    Live = something the viewer started is still running, or a run directory
    has not written ``run_status.json`` and is still writing to ``debug/``.
    """
    study_dir = Path(study_dir)
    for entry in launched(study_dir):
        if entry["alive"]:
            return {"reason": "started by this viewer", "pid": entry["pid"]}
    now = time.time()
    for run in readers.read_runs(study_dir):
        if run["status"] != "running":
            continue
        age = now - _last_activity(Path(run["path"]))
        if age < LIVE_WINDOW_S:
            return {"reason": "no run_status.json and still writing",
                    "run_id": run["run_id"], "idle_s": round(age, 1)}
    return None


def _check(name: str, ok: bool | None, detail: str) -> dict[str, Any]:
    """``ok=None`` means "not checked here", which never blocks a start."""
    return {"name": name, "ok": ok, "detail": detail}


def _evaluator_check(study_dir: Path, cfg: dict) -> dict[str, Any]:
    ev = cfg.get("evaluator")
    if not ev:
        return _check("evaluator", None, "config.yaml declares no evaluator")
    lookup = ev.get("lookup")
    if lookup:
        pool = (study_dir / str(lookup.get("pool", ""))).resolve()
        ok = pool.is_relative_to(study_dir.resolve()) and pool.exists()
        return _check("evaluator", ok, f"lookup pool {lookup.get('pool')!r} "
                      + ("found" if ok else "not found inside the study"))
    entry = str(ev.get("entrypoint", ""))
    if ":" not in entry:
        return _check("evaluator", False,
                      f"entrypoint must be 'path/to/file.py:attr', got {entry!r}")
    rel, attr = entry.rsplit(":", 1)
    path = (study_dir / rel).resolve()
    if not path.is_relative_to(study_dir.resolve()) or not path.is_file():
        return _check("evaluator", False, f"evaluator file {rel!r} not found")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        return _check("evaluator", False, f"{rel} does not parse: {exc}")
    names = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    names |= {t.id for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    if attr not in names:
        return _check("evaluator", False, f"{rel} defines no {attr!r}")
    return _check("evaluator", True, f"{rel}:{attr} is present and parses")


def preflight(study_dir: Path | str) -> list[dict[str, Any]]:
    """What ``docs/author-a-study.md`` "Before a long run, check" asks for,
    as ``{name, ok, detail}`` rows. A failed row (``ok`` False) blocks Start;
    ``ok`` None is something that cannot be decided without side effects."""
    from ..runtime.run_setup import _parse_budget_str

    study_dir = Path(study_dir)
    checks: list[dict[str, Any]] = []

    ps = study_dir / "PROBLEM_STATEMENT.md"
    try:
        has_ps = ps.is_file() and bool(ps.read_text(encoding="utf-8").strip())
    except OSError:
        has_ps = False
    checks.append(_check(
        "problem_statement", has_ps,
        "PROBLEM_STATEMENT.md present" if has_ps
        else "PROBLEM_STATEMENT.md is missing or empty"))
    if has_ps:
        checks.append(_check(
            "problem_statement_content", None,
            "that it states success criteria, the design space and "
            "deliverables is for a person to judge"))

    cfg: dict = {}
    try:
        loaded = yaml.safe_load(
            (study_dir / "config.yaml").read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config.yaml is not a mapping")
        cfg = loaded
        checks.append(_check("config", True, "config.yaml parses"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        checks.append(_check("config", False, f"config.yaml: {exc}"))

    try:
        budget = _parse_budget_str(cfg.get("budget"))
    except (TypeError, ValueError):
        budget = None
    checks.append(_check(
        "budget", budget is not None,
        f"wall-clock budget {budget:g}s (the watchdog derives its deadline "
        "from it)" if budget is not None
        else "config.yaml has no resolvable `budget:`; the watchdog needs one"))

    checks.append(_evaluator_check(study_dir, cfg))
    checks.append(_check(
        "evaluator_one_sample", None,
        "not run automatically: an evaluator can be an expensive simulation. "
        "Run it once on a sample before a long run"))

    backend = str(cfg.get("backend") or "claude")
    if backend == "claude":
        found = shutil.which("claude") is not None
        checks.append(_check(
            "backend", found,
            "the `claude` CLI is on PATH (login is not verified here)" if found
            else "the `claude` CLI is not on PATH"))
    else:
        checks.append(_check(
            "backend", None, f"{backend!r} reachability is not checked here"))

    live = live_run(study_dir)
    checks.append(_check(
        "no_live_run", live is None,
        "no run is live" if live is None
        else f"a run is already live ({live['reason']})"))
    return checks


def _launch_argv(study_dir: Path) -> list[str]:
    return [sys.executable, "-m", "adda.watchdog", str(study_dir)]


def start_run(study_dir: Path | str) -> dict[str, Any]:
    """Start one run under the watchdog. Raises ``StartRefused`` (with the
    checks) when pre-flight fails, which includes a run already being live."""
    study_dir = Path(study_dir).resolve()
    checks = preflight(study_dir)
    failed = [c for c in checks if c["ok"] is False]
    if failed:
        raise StartRefused(failed, checks)

    cmd = _launch_argv(study_dir)
    state = _state_dir(study_dir)
    state.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    log_path = state / f"watchdog_{stamp}.log"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd, cwd=study_dir, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    entry = {
        "kind": "watchdog", "pid": proc.pid,
        "create_time": psutil.Process(proc.pid).create_time(),
        "started_at": time.time(), "cmd": cmd, "cmdline": shlex.join(cmd),
        "log": str(log_path.relative_to(study_dir)),
    }
    _write_registry(study_dir, _read_registry(study_dir) + [entry])
    return entry


class StartRefused(Exception):
    def __init__(self, failed: list[dict], checks: list[dict]):
        super().__init__("pre-flight failed: "
                         + "; ".join(c["name"] for c in failed))
        self.failed, self.checks = failed, checks


def _signal_all(procs: list[psutil.Process], sig: int) -> list[int]:
    sent = []
    for p in procs:
        try:
            p.send_signal(sig)
            sent.append(p.pid)
        except psutil.Error:
            continue
    return sent


def kill_run(
    study_dir: Path | str, *, grace_s: float = KILL_GRACE_S, audit=None,
) -> dict[str, Any] | None:
    """SIGTERM the watchdog the viewer started, then SIGKILL what survives.

    The watchdog turns SIGTERM into a reap of the whole run, so the first
    signal is normally enough. The descendants are collected from that exact
    PID *before* signalling (after it they are re-parented and unfindable) and
    are the only other processes ever signalled. Returns None when the viewer
    started nothing that is still alive. ``audit(action, **fields)`` is called
    for each signal sent.
    """
    study_dir = Path(study_dir)
    for entry in _read_registry(study_dir):
        proc = _alive(entry)
        if proc is None:
            continue
        try:
            tree = proc.children(recursive=True)
        except psutil.Error:
            tree = []
        sent = _signal_all([proc], signal.SIGTERM)
        if audit:
            audit("kill", signal="SIGTERM", pid=entry["pid"],
                  cmdline=entry.get("cmdline"), signalled=sent)

        def escalate(proc=proc, tree=tree, entry=entry):
            _, alive = psutil.wait_procs([proc], timeout=grace_s)
            survivors = [p for p in tree if p.is_running()] + alive
            if not survivors:
                return
            sent = _signal_all(survivors, signal.SIGKILL)
            if audit:
                audit("kill", signal="SIGKILL", pid=entry["pid"],
                      cmdline=entry.get("cmdline"), signalled=sent)

        threading.Thread(target=escalate, daemon=True).start()
        return {"pid": entry["pid"], "signal": "SIGTERM",
                "escalates_after_s": grace_s}
    return None


# --- study launcher (spec 14 Phase 5.5) -------------------------------------
#
# A study on a cluster is started by its own script (``sbatch``, a wrapper),
# not by the local watchdog. The study declares it in ``config.yaml``::
#
#   runtime:
#     launch:
#       command: [bash, launch.sh, --nodes, "2"]   # argv, run in the study dir
#       stop_command: [scancel, "{id}"]            # optional; needs id_pattern
#       id_pattern: "Submitted batch job (\\d+)"   # one group, searched in stdout
#       timeout_s: 120                             # optional
#
# The viewer runs exactly these argvs (no shell) and never composes a cluster
# command of its own. Stop substitutes ``{id}`` with the id the launcher
# printed and stored, and only ever for an id it stored.

_LAUNCH_KEYS = {"command", "stop_command", "id_pattern", "timeout_s"}
_LAUNCH_TIMEOUT_S = 120.0
# A launcher submits and returns before the run writes anything; this is how
# long a second Start is refused on the strength of the viewer's own record.
_SUBMITTED_WINDOW_S = LIVE_WINDOW_S


class LauncherError(Exception):
    """The launcher is declared wrongly, or running it failed."""


def _argv(value: Any, what: str) -> list[str]:
    if isinstance(value, str):
        value = shlex.split(value)
    if (not isinstance(value, list) or not value
            or not all(isinstance(a, (str, int, float)) for a in value)):
        raise LauncherError(f"runtime.launch.{what} must be a non-empty "
                            "argument list (or a command string)")
    return [str(a) for a in value]


def launcher_config(study_dir: Path | str) -> dict[str, Any] | None:
    """The study's declared launcher, validated, or None when it declares none.
    A malformed declaration raises ``LauncherError``: a typo must not read as
    "no launcher" and quietly fall back to something else."""
    try:
        cfg = yaml.safe_load(
            (Path(study_dir) / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    runtime = cfg.get("runtime") if isinstance(cfg, dict) else None
    launch = runtime.get("launch") if isinstance(runtime, dict) else None
    if launch is None:
        return None
    if not isinstance(launch, dict):
        raise LauncherError("runtime.launch must be a mapping")
    unknown = sorted(set(launch) - _LAUNCH_KEYS)
    if unknown:
        raise LauncherError(
            f"unknown runtime.launch key(s) {unknown}; known: "
            f"{sorted(_LAUNCH_KEYS)}")
    out: dict[str, Any] = {"command": _argv(launch.get("command"), "command")}
    pattern = launch.get("id_pattern")
    if pattern is not None:
        try:
            if re.compile(str(pattern)).groups != 1:
                raise LauncherError(
                    "runtime.launch.id_pattern needs exactly one capture group")
        except re.error as exc:
            raise LauncherError(f"runtime.launch.id_pattern: {exc}") from exc
        out["id_pattern"] = str(pattern)
    if launch.get("stop_command") is not None:
        out["stop_command"] = _argv(launch["stop_command"], "stop_command")
        if "id_pattern" not in out:
            raise LauncherError(
                "runtime.launch.stop_command needs id_pattern: the viewer "
                "cannot know which id to stop otherwise")
        if "{id}" not in " ".join(out["stop_command"]):
            raise LauncherError(
                "runtime.launch.stop_command must contain {id}")
    try:
        out["timeout_s"] = float(launch.get("timeout_s", _LAUNCH_TIMEOUT_S))
    except (TypeError, ValueError) as exc:
        raise LauncherError("runtime.launch.timeout_s must be a number") from exc
    return out


def _run_declared(
    argv: list[str], study_dir: Path, timeout_s: float, audit, action: str,
) -> dict[str, Any]:
    """Run one declared argv in the study directory and capture its output.
    The audit row is written whatever happens, so a failed launch still shows
    the exact command line that was run."""
    if audit:
        audit(action, command=shlex.join(argv), phase="run")
    try:
        proc = subprocess.run(  # noqa: S603 — declared argv, no shell
            argv, cwd=study_dir, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout_s, start_new_session=True)
    except subprocess.TimeoutExpired as exc:
        if audit:
            audit(action, command=shlex.join(argv), phase="timeout")
        raise LauncherError(
            f"{shlex.join(argv)} did not return within {timeout_s:g}s") from exc
    except OSError as exc:
        raise LauncherError(f"{shlex.join(argv)} could not run: {exc}") from exc
    if audit:
        audit(action, command=shlex.join(argv), phase="done",
              returncode=proc.returncode, stdout=proc.stdout[-2000:],
              stderr=proc.stderr[-2000:])
    return {"returncode": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "cmdline": shlex.join(argv)}


def _launcher_entries(study_dir: Path) -> list[dict[str, Any]]:
    return [e for e in _read_registry(study_dir) if e.get("kind") == "launcher"]


def start_via_launcher(study_dir: Path | str, *, audit=None) -> dict[str, Any]:
    """Run the study's declared launcher and record what it printed.

    Raises ``LauncherError`` when no launcher is declared or it is declared
    wrongly, ``StartRefused`` when a run is already live or was submitted a
    moment ago, and ``LauncherError`` when the launcher exits non-zero or
    prints no id although it was asked to.
    """
    study_dir = Path(study_dir).resolve()
    cfg = launcher_config(study_dir)
    if cfg is None:
        raise LauncherError("this study declares no runtime.launch")
    live = live_run(study_dir)
    recent = [e for e in _launcher_entries(study_dir)
              if not e.get("stopped_at")
              and time.time() - e["started_at"] < _SUBMITTED_WINDOW_S]
    if live is not None or recent:
        check = _check("no_live_run", False,
                       f"a run is already live ({live['reason']})" if live
                       else "a launch was submitted moments ago")
        raise StartRefused([check], [check])

    result = _run_declared(
        cfg["command"], study_dir, cfg["timeout_s"], audit, "launch")
    entry: dict[str, Any] = {
        "kind": "launcher", "started_at": time.time(),
        "cmd": cfg["command"], "cmdline": result["cmdline"],
        "returncode": result["returncode"],
        "stdout": result["stdout"], "stderr": result["stderr"],
    }
    if result["returncode"] != 0:
        raise LauncherError(
            f"{result['cmdline']} exited {result['returncode']}: "
            f"{(result['stderr'] or result['stdout']).strip()[-500:]}")
    if "id_pattern" in cfg:
        m = re.search(cfg["id_pattern"], result["stdout"], re.MULTILINE)
        if not m:
            raise LauncherError(
                f"the launcher ran but its output did not match id_pattern "
                f"{cfg['id_pattern']!r}; nothing is recorded, so it cannot "
                "be stopped from here")
        entry["launch_id"] = m.group(1)
    _write_registry(study_dir, _read_registry(study_dir) + [entry])
    return entry


def stop_via_launcher(
    study_dir: Path | str, *, audit=None,
) -> dict[str, Any] | None:
    """Run ``stop_command`` with the id of the latest launch the viewer
    recorded and has not yet stopped. None when there is none."""
    study_dir = Path(study_dir).resolve()
    cfg = launcher_config(study_dir)
    if cfg is None or "stop_command" not in cfg:
        raise LauncherError("this study declares no runtime.launch.stop_command")
    entries = _read_registry(study_dir)
    target = next((e for e in reversed(entries)
                   if e.get("kind") == "launcher" and e.get("launch_id")
                   and not e.get("stopped_at")), None)
    if target is None:
        return None
    argv = [a.replace("{id}", str(target["launch_id"]))
            for a in cfg["stop_command"]]
    result = _run_declared(
        argv, study_dir, cfg["timeout_s"], audit, "launch_stop")
    if result["returncode"] == 0:
        target["stopped_at"] = time.time()
        _write_registry(study_dir, entries)
    return {**result, "launch_id": target["launch_id"]}
