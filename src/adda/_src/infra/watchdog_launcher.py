"""The run watchdog itself (BACKLOG #41): a wall-clock timer that lives OUTSIDE
the run process and force-exits a genuinely wedged one.

Why outside the process. A backstop that runs as a thread inside the very
process it is meant to catch cannot be trusted — the thing most likely to be
stuck (a hung model call, a wedged simulation binding the GIL or blocked in
native code) can just as easily wedge the watchdog thread alongside it. See
``paper/sections/03-method.tex``, "A wall-clock watchdog outside the run".
So this module starts the study run as a CHILD process, in its own process
group, and owns the clock from the parent — the parent's only job is to wait
with a deadline and, on timeout, kill.

Everything :func:`run_under_watchdog` does to a wedged run on timeout reuses
the existing, already-tested cleanup primitives in ``watchdog_cleanup.py``
(``reap_process_group``, ``reap_governor_pids``, ``write_watchdog_retrospective``)
— this module adds the CALLER those functions never had inside this package,
not a second implementation of what they already do.

HEADROOM is deliberate and non-negotiable in one direction: every other budget
in this system (eval budget, cost budget) is a SOFT nudge to the agents; this
timer is the one approved hard stop, and it exists only to catch a run that
has genuinely wedged — never to police a merely slow one. The floor is
2x the run's own stated wall-clock budget (``DEFAULT_WATCHDOG_MULTIPLE`` — see
``paper/sections/03-method.tex``, "A hard timer at twice the run's time
budget"). :func:`resolve_deadline_seconds` refuses a multiple below that floor
— tightening it needs a maintainer decision, not a flag.
"""
from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..runtime import settings
from .stop_request import write_stop_request
from .watchdog_cleanup import (
    reap_governor_pids,
    reap_process_group,
    write_watchdog_retrospective,
)

__all__ = [
    "DEFAULT_WATCHDOG_MULTIPLE",
    "EXIT_TIMEOUT",
    "WatchdogResult",
    "default_stop_grace_s",
    "resolve_deadline_seconds",
    "run_under_watchdog",
    "main",
]

# The floor, per the paper's method section. Never lower this — see module
# docstring and CLAUDE.md §4 ("any new hard cap needs approval"; this one IS
# approved, at this floor, and only at this floor).
DEFAULT_WATCHDOG_MULTIPLE = 2.0

# Distinguishable from any real exit code the study run itself could produce
# (0 on success, 1 on AgenticRunError, a Python traceback's 1, or an argparse
# usage error's 2) — mirrors the ``timeout(1)`` coreutil's own convention, so
# a caller (a shell, a Slurm epilog, a benchmarks harness) can tell "the
# watchdog killed this" from "the run failed on its own" without parsing text.
EXIT_TIMEOUT = 124
# The watchdog itself was told to stop (SIGINT / SIGTERM): shell convention,
# 128 + signal number.
EXIT_INTERRUPTED = {signal.SIGINT: 130, signal.SIGTERM: 143}


class _Interrupted(BaseException):
    """Raised by the watchdog's own signal handler to leave the poll loop."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


@dataclass(frozen=True)
class WatchdogResult:
    """The outcome of one watched invocation.

    ``timed_out=True`` means the watchdog itself killed the run; ``returncode``
    is then whatever the child reported after being signalled (often ``None``
    if it never even got to exit cleanly), not a verdict on the work. When
    ``timed_out=False``, ``returncode`` is the child's own real exit status,
    propagated unchanged. ``interrupted_by`` is the signal number when the
    watchdog itself was signalled (SIGINT/SIGTERM) and took the run down with
    it instead of leaving it orphaned.
    """

    timed_out: bool
    returncode: int | None
    pgid: int | None = None
    interrupted_by: int | None = None


# A run that is asked to stop needs time for its workers to report and for the
# entry node to take its retrospective round; a tenth of the deadline, never
# more than a quarter of an hour, so a short study still has a deadline.
STOP_GRACE_CAP_S = 900.0


def default_stop_grace_s(deadline_s: float) -> float:
    """The stop window used when ``runtime.stop_grace_s`` is not set."""
    return min(STOP_GRACE_CAP_S, deadline_s / 10)


def resolve_deadline_seconds(
    budget_seconds: float, multiple: float = DEFAULT_WATCHDOG_MULTIPLE
) -> float:
    """The watchdog deadline, derived from the SAME budget value the run
    itself was given — so the run's stated time budget and the watchdog's
    deadline can never silently disagree (one read, one arithmetic, both
    sides use it).

    ``multiple`` must be >= :data:`DEFAULT_WATCHDOG_MULTIPLE` (2x) — that is a
    floor, not a default a caller can dial down; see the module docstring.
    """
    if budget_seconds is None or budget_seconds <= 0:
        raise ValueError(
            "resolve_deadline_seconds requires a positive time budget "
            f"(seconds) to derive a watchdog deadline from; got {budget_seconds!r}"
        )
    if multiple < DEFAULT_WATCHDOG_MULTIPLE:
        raise ValueError(
            f"watchdog multiple must be >= {DEFAULT_WATCHDOG_MULTIPLE}x the "
            f"run's own time budget (the approved floor — see CLAUDE.md §4 "
            f"and paper/sections/03-method.tex); got {multiple}"
        )
    return float(budget_seconds) * float(multiple)


def _discover_new_run_dir(runs_dir: Path, existing: frozenset[str]) -> Path | None:
    """The run dir the watched child created, if any: the newest directory
    under ``runs_dir`` that was NOT already there before the child was
    spawned. Best-effort — a run dir is timestamp-named and this package
    never runs two studies against the same ``runs_dir`` concurrently, so
    "newest new name" is unambiguous in practice. Never raises."""
    try:
        if not runs_dir.is_dir():
            return None
        candidates = [
            p for p in runs_dir.iterdir() if p.is_dir() and p.name not in existing
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.name)
    except OSError:
        return None


def run_under_watchdog(
    cmd: list[str],
    *,
    deadline_s: float,
    run_dir: str | Path | None = None,
    run_dir_hint: str | Path | None = None,
    existing_run_dirs: frozenset[str] = frozenset(),
    cwd: str | Path | None = None,
    env: dict | None = None,
    poll_interval: float = 0.05,
    kill_grace_s: float = 1.0,
    stop_grace_s: float = 0.0,
) -> WatchdogResult:
    """Run ``cmd`` as a child process, in its own process group, with a hard
    wall-clock ``deadline_s``.

    A child that finishes inside the deadline is left alone and its real exit
    status is returned unchanged. A child that overruns is killed — the whole
    process TREE, not just the direct child, by ``SIGTERM``'ing its entire
    process group (``reap_process_group``, reused) and escalating to
    ``SIGKILL`` if anything is still alive after ``kill_grace_s``. Any
    registered campaign PIDs the run itself spawned (``reap_governor_pids``)
    are reaped too — the recursive kill that reaches a detached/new-session
    descendant a plain process-group signal cannot. On a kill, a labelled
    post-mortem is appended to the run's ``retrospectives.jsonl``
    (``write_watchdog_retrospective``) so CLAUDE.md §1 Step 1 has a breadcrumb
    instead of silence.

    ``run_dir`` is the run directory to write the post-mortem into, if already
    known. When it is NOT known up front (the normal case for a study launch,
    whose run dir is timestamped and created by the child itself once it
    starts) pass ``run_dir_hint`` (the study's ``runs/`` directory) and
    ``existing_run_dirs`` (the run-dir names that existed BEFORE this child
    was spawned) instead; the new run dir is discovered opportunistically
    during the wait, with one final attempt right before escalation.

    ``stop_grace_s`` (default 0 = today's behaviour) asks the run to wind down
    BEFORE the kill: ``stop_grace_s`` ahead of the deadline a stop request is
    written into the run's ``debug/`` (``infra/stop_request.py``), so a run
    that is merely slow can hand over its agents' real retrospectives instead
    of losing them to SIGTERM. The deadline itself does not move and the kill
    below is unchanged; a run that closes inside the grace is simply a
    finished run. The run gets half the window to wind its workers down and
    the other half to take its own retrospective round. A run directory not
    found by then is not guessed at — the kill proceeds as before.
    """
    if stop_grace_s < 0:
        raise ValueError(f"stop_grace_s must be >= 0; got {stop_grace_s}")
    if stop_grace_s and stop_grace_s >= deadline_s:
        raise ValueError(
            f"stop_grace_s ({stop_grace_s:g}s) must be shorter than the "
            f"deadline ({deadline_s:g}s): the request is written "
            "stop_grace_s BEFORE the deadline, so it would predate the run")
    proc = subprocess.Popen(  # noqa: S603 — cmd is caller-constructed, not shell text
        cmd, cwd=cwd, env=env, start_new_session=True,
    )
    pgid = proc.pid  # start_new_session makes the child its own session+group leader

    resolved_run_dir = Path(run_dir) if run_dir is not None else None
    hint_dir = Path(run_dir_hint) if run_dir_hint is not None else None

    deadline = time.monotonic() + deadline_s
    stop_at = deadline - stop_grace_s if stop_grace_s else None

    def _raise_interrupted(signum, _frame):
        raise _Interrupted(signum)

    # The child is its own session, so without this a SIGTERM/SIGINT to the
    # watchdog (a terminal Ctrl-C, the viewer's Kill) ends the watchdog and
    # orphans the run. Handlers can only be installed from the main thread.
    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, _raise_interrupted)
    interrupted_by: int | None = None
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                return WatchdogResult(timed_out=False, returncode=rc, pgid=pgid)
            if resolved_run_dir is None and hint_dir is not None:
                resolved_run_dir = _discover_new_run_dir(
                    hint_dir, existing_run_dirs)
            if stop_at is not None and time.monotonic() >= stop_at:
                stop_at = None  # once, whether or not it could be written
                if resolved_run_dir is not None:
                    write_stop_request(
                        resolved_run_dir, by="watchdog",
                        reason=f"the {deadline_s:g}s wall-clock deadline is "
                               f"{stop_grace_s:g}s away",
                        grace_s=stop_grace_s / 2,
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
    except _Interrupted as exc:
        interrupted_by = exc.signum
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    # TIMEOUT — the run has genuinely wedged (or is merely slow; the deadline
    # is a 2x-floor multiple of the run's own budget precisely so a merely
    # slow run never reaches this branch). Escalate.
    if resolved_run_dir is None and hint_dir is not None:
        resolved_run_dir = _discover_new_run_dir(hint_dir, existing_run_dirs)

    reap_process_group(pgid)  # SIGTERM the whole tree first
    grace_deadline = time.monotonic() + kill_grace_s
    while proc.poll() is None and time.monotonic() < grace_deadline:
        time.sleep(min(poll_interval, max(0.0, grace_deadline - time.monotonic())))
    if proc.poll() is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=kill_grace_s)
        except subprocess.TimeoutExpired:
            pass

    if resolved_run_dir is not None:
        # Detached/new-session campaign processes escape a plain process-group
        # signal (the #14 escape reap_process_group's own docstring names) —
        # reap_governor_pids's recursive, ownership-verified kill catches them.
        reap_governor_pids(resolved_run_dir)
        if interrupted_by is None:
            write_watchdog_retrospective(resolved_run_dir, int(deadline_s))

    return WatchdogResult(
        timed_out=interrupted_by is None, returncode=proc.poll(), pgid=pgid,
        interrupted_by=interrupted_by)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m adda.watchdog",
        description=(
            "Run a study under an external wall-clock watchdog: the study "
            "runs as a child process, and a hard timer force-exits it (and "
            "everything it spawned) if it genuinely wedges. `python -m adda "
            "<study-dir>` itself stays unprotected and unchanged — this is a "
            "safer way to launch the same run, not a replacement for it."
        ),
    )
    parser.add_argument(
        "study_dir",
        metavar="study-dir",
        type=Path,
        help="Path to the study directory. Must contain PROBLEM_STATEMENT.md.",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Forwarded to `python -m adda` unchanged (default: its own default).",
    )
    parser.add_argument(
        "--budget",
        default=None,
        metavar="DURATION",
        help="Wall-clock time budget for the RUN: seconds, or HH:MM:SS. "
             "Required here (directly, or as config.yaml's `budget:`) — the "
             "watchdog deadline is derived from it and cannot be set on its "
             "own.",
    )
    parser.add_argument(
        "--watchdog-multiple",
        type=float,
        default=DEFAULT_WATCHDOG_MULTIPLE,
        metavar="X",
        help=f"Deadline = X * budget. Floor {DEFAULT_WATCHDOG_MULTIPLE} "
             "(twice the run's own budget) — a merely slow run must never be "
             "killed, so this can be raised but never lowered.",
    )
    parser.add_argument(
        "--entrypoint",
        default=None,
        metavar="SCRIPT",
        help=(
            "Run the study's OWN entrypoint script (path relative to "
            "study-dir, e.g. `run.py`) instead of `python -m adda "
            "<study-dir>`. `python -m adda` never accepts a custom graph= — "
            "it always builds AgenticRun's built-in default graph — so a "
            "study whose run.py declares its own Graph (extra roles like "
            "math_expert, different edges) silently gets a DIFFERENT, "
            "smaller graph when launched through the CLI. Without this flag "
            "such a study can only be run-protected by hand-invoking "
            "run_under_watchdog, which is exactly why it wasn't (the Oscar "
            "zero-shot harness launched bare `python run.py`, and a hang "
            "went unwatched). Incompatible with --model/--budget: an "
            "arbitrary entrypoint script takes no CLI arguments of its own "
            "to forward them to, so the watchdog deadline is derived from "
            "the study's config.yaml `budget:` only."
        ),
    )
    from ..runtime.cli_overrides import add_set_argument
    add_set_argument(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    from ..runtime.cli_overrides import as_runtime, format_set
    from ..runtime.run_setup import _load_study_config, _parse_budget_str

    parser = _build_parser()
    args = parser.parse_args(argv)
    study_dir = Path(args.study_dir).resolve()

    entry_path: Path | None = None
    if args.entrypoint is not None:
        if args.model or args.budget or args.overrides:
            print(
                "Error: --entrypoint can't be combined with "
                "--model/--budget/--set "
                "— an arbitrary entrypoint script takes no CLI arguments of "
                "its own to forward them to. Set `budget:` in the study's "
                "config.yaml instead (the entrypoint's own AgenticRun call "
                "resolves its model the same way).",
                file=sys.stderr,
            )
            return 2
        entry_path = (study_dir / args.entrypoint).resolve()
        if not entry_path.is_file():
            print(
                f"Error: --entrypoint {args.entrypoint!r} not found at "
                f"{entry_path}.",
                file=sys.stderr,
            )
            return 2

    cfg = _load_study_config(study_dir)
    token_run = cfg.get("budget_clock") == "output_tokens"
    if token_run:
        # No wall budget to take a multiple of: the host-safety deadline is
        # its own explicit, absolute number.
        if args.budget is not None:
            print("Error: budget_clock: output_tokens has no wall budget; "
                  "drop --budget. The deadline is `watchdog_wall_s:` in "
                  f"{study_dir / 'config.yaml'}.", file=sys.stderr)
            return 2
        try:
            deadline_s = float(_parse_budget_str(cfg.get("watchdog_wall_s")))
            if deadline_s <= 0:
                raise ValueError
        except (TypeError, ValueError):
            print("Error: budget_clock: output_tokens needs `watchdog_wall_s:` "
                  "(seconds or HH:MM:SS, > 0) in "
                  f"{study_dir / 'config.yaml'}: the wall-clock limit after "
                  "which the watchdog kills the run. There is no wall budget "
                  "to derive it from.", file=sys.stderr)
            return 2
        budget_s = None
    else:
        budget_raw = args.budget if args.budget is not None else cfg.get(
            "budget")
        try:
            budget_s = _parse_budget_str(budget_raw)
        except (TypeError, ValueError):
            budget_s = None
        if budget_s is None:
            print(
                "Error: adda.watchdog needs a wall-clock budget to derive its "
                "deadline from — pass --budget or set `budget:` in "
                f"{study_dir / 'config.yaml'}.",
                file=sys.stderr,
            )
            return 2
        try:
            deadline_s = resolve_deadline_seconds(
                budget_s, args.watchdog_multiple)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    try:
        overrides = as_runtime(args.overrides)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        settings.configure(cfg.get("runtime") or {}, overrides)
        stop_grace_s = settings.get_float("stop_grace_s", math.nan)
    except (TypeError, ValueError):
        print("Error: runtime.stop_grace_s must be a number of seconds.",
              file=sys.stderr)
        return 2
    if math.isnan(stop_grace_s):
        stop_grace_s = default_stop_grace_s(deadline_s)
    if stop_grace_s < 0 or (stop_grace_s and stop_grace_s >= deadline_s):
        print(
            f"Error: runtime.stop_grace_s ({stop_grace_s:g}s) must be >= 0 "
            f"and shorter than the watchdog deadline ({deadline_s:g}s).",
            file=sys.stderr,
        )
        return 2

    runs_dir = study_dir / "runs"
    existing: frozenset[str] = frozenset(
        p.name for p in runs_dir.iterdir() if p.is_dir()
    ) if runs_dir.is_dir() else frozenset()

    if entry_path is not None:
        cmd = [sys.executable, str(entry_path)]
    else:
        cmd = [sys.executable, "-m", "adda", str(study_dir)]
        if budget_s is not None:
            cmd += ["--budget", str(budget_s)]
        if args.model:
            cmd += ["--model", args.model]
        cmd += format_set(args.overrides)

    print(
        f"adda.watchdog: launching {' '.join(cmd)} under a "
        f"{deadline_s:.0f}s deadline ("
        + ("watchdog_wall_s, output-token budget"
           if budget_s is None else
           f"{args.watchdog_multiple}x the {budget_s:.0f}s run budget")
        + ")",
        file=sys.stderr,
    )

    result = run_under_watchdog(
        cmd,
        deadline_s=deadline_s,
        run_dir_hint=runs_dir,
        existing_run_dirs=existing,
        stop_grace_s=stop_grace_s,
    )

    if result.interrupted_by is not None:
        print(
            f"adda.watchdog: interrupted by signal {result.interrupted_by} — "
            "the run's process tree was reaped rather than left orphaned.",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED.get(result.interrupted_by, 1)

    if result.timed_out:
        print(
            f"adda.watchdog: TIMED OUT after {deadline_s:.0f}s — the run was "
            "force-killed (process tree reaped; a WATCHDOG post-mortem was "
            "appended to its retrospectives.jsonl if its run dir was found).",
            file=sys.stderr,
        )
        return EXIT_TIMEOUT

    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
