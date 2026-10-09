"""Make background jobs that outlive a delegation's session visible.

A worker's session can end while a job it started in the background is still
running (the CLI backend may stop it when the session closes). This module
only OBSERVES: it never kills, and it does not change the reaping policy.

``BackgroundJobWatch`` takes a snapshot when the session starts and another
when it ends. A job is a process that

* is a descendant of this process,
* carries ``F3DASM_DELEGATION_ID`` equal to the delegation, and
* was not in the start snapshot.

Only the root of each job's process subtree is reported. When psutil cannot
read a process's environment, the process cannot be attributed. It is
reported as ``environ unreadable`` and never guessed.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

DELEGATION_ENV = "F3DASM_DELEGATION_ID"
ENVIRON_UNREADABLE = "environ unreadable"
_CMD_CHARS = 200


@dataclass(frozen=True)
class BackgroundJob:
    pid: int
    start_time: float
    command: str
    attributed: bool  # False: environ could not be read


def _psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _command(proc) -> str:
    try:
        parts = proc.cmdline()
    except Exception:  # noqa: BLE001
        parts = []
    if not parts:
        try:
            parts = [proc.name()]
        except Exception:  # noqa: BLE001
            parts = []
    return " ".join(parts)[:_CMD_CHARS]


def _scan(delegation_id: str, min_depth: int, ignore: tuple[str, ...]):
    """Candidate processes: ``{pid: (BackgroundJob, parent_pid)}``."""
    psutil = _psutil()
    if psutil is None:
        return {}
    found: dict[int, tuple[BackgroundJob, int]] = {}
    try:
        root = psutil.Process(os.getpid())
        procs = root.children(recursive=True)
    except Exception:  # noqa: BLE001
        return {}
    for proc in procs:
        try:
            depth, p = 1, proc
            while p.ppid() != root.pid:
                p = p.parent()
                if p is None:
                    break
                depth += 1
            if depth < min_depth:
                continue
            cmd = _command(proc)
            if any(sig and sig in cmd for sig in ignore):
                continue
            try:
                attributed = True
                if proc.environ().get(DELEGATION_ENV) != delegation_id:
                    continue
            except Exception:  # noqa: BLE001
                attributed = False
            found[proc.pid] = (
                BackgroundJob(proc.pid, _start_time(proc.pid), cmd, attributed),
                proc.ppid())
        except Exception:  # noqa: BLE001
            continue
    return found


def _start_time(pid: int) -> float | None:
    from .resource_backend import get_resource_backend
    return get_resource_backend().proc_start_time(pid)


def _roots(found: dict[int, tuple[BackgroundJob, int]]) -> list[BackgroundJob]:
    return [job for pid, (job, parent) in found.items()
            if parent not in found]


def job_state(job: BackgroundJob) -> str:
    """``alive``, ``dead`` or ``environ unreadable`` for one reported job.

    Alive means the same pid with the same start time, so a recycled pid is
    never taken for the job.
    """
    if not job.attributed:
        return ENVIRON_UNREADABLE
    psutil = _psutil()
    if psutil is None:
        return "dead"
    try:
        proc = psutil.Process(job.pid)
        now = _start_time(job.pid)
        if now is None or job.start_time is None or abs(now - job.start_time) > 1e-3:
            return "dead"
        if proc.status() == psutil.STATUS_ZOMBIE:
            return "dead"
        return "alive"
    except Exception:  # noqa: BLE001
        return "dead"


class BackgroundJobWatch:
    """Start/end snapshots for one delegation. Thread-safe, best-effort."""

    def __init__(self, delegation_id: str,
                 ignore: tuple[str, ...] = ()) -> None:
        self.delegation_id = delegation_id
        self._ignore = tuple(ignore)
        self._lock = threading.Lock()
        self._before: set[tuple[int, float]] | None = None
        self.jobs: list[BackgroundJob] = []
        self.ended_at: datetime | None = None

    def start(self) -> None:
        """Take the baseline once; later calls (a retry, a resume) keep it."""
        with self._lock:
            if self._before is not None:
                return
            try:
                self._before = {
                    (j.pid, j.start_time) for j, _ in _scan(
                        self.delegation_id, 1, self._ignore).values()}
            except Exception:  # noqa: BLE001
                self._before = set()

    def end(self, *, min_depth: int) -> None:
        """Record what is running now that was not running at ``start``.

        ``min_depth`` is how far below this process a job sits: 1 for a shell
        this process spawns itself, 2 when a CLI sits between them (the CLI
        is then depth 1 and is not a job).
        """
        with self._lock:
            try:
                found = _scan(self.delegation_id, min_depth, self._ignore)
                before = self._before or set()
                fresh = {pid: v for pid, v in found.items()
                         if (pid, v[0].start_time) not in before}
                self.jobs = _roots(fresh)
            except Exception:  # noqa: BLE001
                self.jobs = []
            self.ended_at = datetime.now(tz=timezone.utc)

    def report_lines(self) -> list[tuple[BackgroundJob, str, str]]:
        """``(job, state, text)`` per job, state re-checked now."""
        with self._lock:
            jobs, ended = list(self.jobs), self.ended_at
        stamp = ended.strftime("%H:%M:%S UTC") if ended else "an unknown time"
        out = []
        for job in jobs:
            state = job_state(job)
            head = (f"background job `{job.command}` (pid {job.pid}) was "
                    f"still running when this delegation ended at {stamp}")
            if state == "dead":
                text = (f"{head}, and it is no longer running. Results it "
                        "had not saved by then are not in the store.")
            elif state == "alive":
                text = (f"{head}, and it still is. It may write to the "
                        "store after this report.")
            else:
                text = (f"{head}. Its state now is unknown: its environment "
                        "could not be read.")
            out.append((job, state, text))
        return out
