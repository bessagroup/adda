"""The run watchdog itself (BACKLOG #41): an external, out-of-process wall-clock
timer that force-exits a genuinely wedged run, reaps its whole process tree,
and leaves a labelled post-mortem.

Deterministic and fast throughout: every child under test is a tiny helper
script driven with a sub-second deadline, never a real agentic run.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from adda._src.infra.watchdog_launcher import (
    DEFAULT_WATCHDOG_MULTIPLE,
    EXIT_TIMEOUT,
    resolve_deadline_seconds,
    run_under_watchdog,
)

# ── deadline arithmetic: the 2x floor cannot be tightened without a test going red ──

def test_resolve_deadline_is_exactly_the_floor_multiple_by_default():
    assert resolve_deadline_seconds(100.0) == 200.0
    assert DEFAULT_WATCHDOG_MULTIPLE == 2.0


def test_resolve_deadline_honours_a_larger_explicit_multiple():
    assert resolve_deadline_seconds(100.0, multiple=3.0) == 300.0


def test_resolve_deadline_refuses_a_multiple_below_the_floor():
    with pytest.raises(ValueError, match="floor"):
        resolve_deadline_seconds(100.0, multiple=1.5)


def test_resolve_deadline_refuses_a_nonpositive_budget():
    with pytest.raises(ValueError):
        resolve_deadline_seconds(0.0)
    with pytest.raises(ValueError):
        resolve_deadline_seconds(None)


# ── a child that finishes in time is left alone, exit status propagated ──

def test_fast_child_is_not_killed_and_its_exit_status_is_propagated(tmp_path):
    script = tmp_path / "fast.py"
    script.write_text("import sys; sys.exit(7)\n")
    result = run_under_watchdog(
        [sys.executable, str(script)], deadline_s=5.0,
    )
    assert result.timed_out is False
    assert result.returncode == 7


# ── a child that overruns is killed, and the timeout is reported distinguishably ──

def test_slow_child_is_killed_and_timeout_is_distinguishable(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(30)\n")
    start = time.monotonic()
    result = run_under_watchdog(
        [sys.executable, str(script)], deadline_s=0.3, kill_grace_s=0.5,
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    # Killed promptly, nowhere near the child's real 30s sleep.
    assert elapsed < 10
    assert result.returncode != 0  # killed, not a clean exit


# ── the whole process TREE dies, not just the direct child ──

def test_grandchild_is_also_reaped(tmp_path):
    grandchild_pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(30)'])\n"
        f"open({str(grandchild_pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    # The deadline must outlast interpreter start-up plus the grandchild
    # spawn on a slow runner, or the pid file is not written before the
    # kill and there is no grandchild to check (macos CI, 20260929).
    result = run_under_watchdog(
        [sys.executable, str(script)], deadline_s=4.0, kill_grace_s=0.5,
    )
    assert result.timed_out is True

    # Give the OS a brief instant to finish reaping, then confirm the
    # grandchild pid is genuinely gone (not just the direct child).
    grandchild_pid = int(grandchild_pid_file.read_text().strip())
    deadline = time.monotonic() + 3.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert alive is False, "grandchild survived the watchdog kill"


# ── a post-mortem lands in retrospectives.jsonl, labelled as the watchdog's ──

def test_timeout_writes_a_labelled_watchdog_retrospective(tmp_path):
    run_dir = tmp_path / "runs" / "T1"
    (run_dir / "debug").mkdir(parents=True)

    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(30)\n")
    result = run_under_watchdog(
        [sys.executable, str(script)],
        deadline_s=0.3,
        kill_grace_s=0.5,
        run_dir=run_dir,
    )
    assert result.timed_out is True

    retro_path = run_dir / "debug" / "retrospectives.jsonl"
    assert retro_path.exists()
    lines = [json.loads(ln) for ln in retro_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = lines[0]
    # Labelled as the watchdog's own synthesized entry, never as first-person
    # agent text.
    assert rec["role"] == "watchdog"
    assert rec["source_id"] == "WATCHDOG"
    assert rec["flagged"] is False
    assert "WATCHDOG POST-MORTEM" in rec["text"]


def test_fast_child_writes_no_retrospective(tmp_path):
    run_dir = tmp_path / "runs" / "T2"
    (run_dir / "debug").mkdir(parents=True)

    script = tmp_path / "fast.py"
    script.write_text("import sys; sys.exit(0)\n")
    result = run_under_watchdog(
        [sys.executable, str(script)], deadline_s=5.0, run_dir=run_dir,
    )
    assert result.timed_out is False
    retro_path = run_dir / "debug" / "retrospectives.jsonl"
    assert not retro_path.exists()


# ── run_dir discovery via a hint (the CLI's actual usage shape) ──

def test_run_dir_is_discovered_via_hint_when_not_known_up_front(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    pre_existing = runs_dir / "20200101T000000"
    (pre_existing / "debug").mkdir(parents=True)
    existing = frozenset({pre_existing.name})

    new_run_dir = runs_dir / "20260101T000000"

    script = tmp_path / "slow_that_makes_a_run_dir.py"
    script.write_text(
        "import os, time\n"
        f"os.makedirs({str(new_run_dir / 'debug')!r})\n"
        "time.sleep(30)\n"
    )
    result = run_under_watchdog(
        [sys.executable, str(script)],
        deadline_s=0.5,
        kill_grace_s=0.5,
        run_dir_hint=runs_dir,
        existing_run_dirs=existing,
    )
    assert result.timed_out is True

    # The post-mortem landed in the NEW run dir, not the pre-existing one.
    new_retro = new_run_dir / "debug" / "retrospectives.jsonl"
    assert new_retro.exists()
    old_retro = pre_existing / "debug" / "retrospectives.jsonl"
    assert not old_retro.exists()


# ── stop_grace_s: ask the run to wind down ahead of the kill ──

def _run_dir_child(tmp_path, body):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    new_run_dir = runs_dir / "20260101T000000"
    script = tmp_path / "child.py"
    script.write_text(
        "import os, sys, time\n"
        f"run = {str(new_run_dir)!r}\n"
        "os.makedirs(os.path.join(run, 'debug'))\n" + body
    )
    return script, runs_dir, new_run_dir


def test_stop_request_is_written_ahead_of_the_deadline_and_kill_is_unchanged(tmp_path):
    from adda._src.infra.stop_request import read_stop_request

    script, runs_dir, run = _run_dir_child(tmp_path, "time.sleep(30)\n")
    t0 = time.monotonic()
    result = run_under_watchdog(
        [sys.executable, str(script)], deadline_s=2.0, kill_grace_s=0.5,
        run_dir_hint=runs_dir, existing_run_dirs=frozenset(),
        stop_grace_s=1.0,
    )
    assert result.timed_out is True
    assert time.monotonic() - t0 >= 2.0  # the deadline did not move
    req = read_stop_request(run)
    assert req is not None
    assert req["by"] == "watchdog"
    assert req["grace_s"] == 0.5


def test_a_run_that_closes_on_the_request_is_not_a_timeout(tmp_path):
    script, runs_dir, _ = _run_dir_child(
        tmp_path,
        "p = os.path.join(run, 'debug', 'stop_request.json')\n"
        "while not os.path.exists(p):\n"
        "    time.sleep(0.02)\n"
        "sys.exit(0)\n",
    )
    result = run_under_watchdog(
        [sys.executable, str(script)], deadline_s=3.0,
        run_dir_hint=runs_dir, existing_run_dirs=frozenset(),
        stop_grace_s=1.5,
    )
    assert result.timed_out is False
    assert result.returncode == 0


def test_no_run_dir_means_no_request_and_the_same_kill(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(30)\n")
    result = run_under_watchdog(
        [sys.executable, str(script)], deadline_s=0.6, kill_grace_s=0.5,
        stop_grace_s=0.3,
    )
    assert result.timed_out is True


def test_stop_grace_must_fit_inside_the_deadline(tmp_path):
    cmd = [sys.executable, "-c", "pass"]
    with pytest.raises(ValueError, match="shorter"):
        run_under_watchdog(cmd, deadline_s=1.0, stop_grace_s=1.0)
    with pytest.raises(ValueError, match=">= 0"):
        run_under_watchdog(cmd, deadline_s=1.0, stop_grace_s=-1.0)


# ── the watchdog itself being signalled must not orphan the run ──

@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signalling_the_watchdog_reaps_the_child_instead_of_orphaning_it(
        tmp_path, signum):
    import psutil

    child = tmp_path / "child.py"
    child.write_text(
        "import os, time\n"
        f"open({str(tmp_path / 'child.pid')!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import sys\n"
        "from adda._src.infra.watchdog_launcher import run_under_watchdog\n"
        f"r = run_under_watchdog([sys.executable, {str(child)!r}],"
        " deadline_s=60.0, kill_grace_s=1.0)\n"
        "sys.exit(r.interrupted_by or 0)\n"
    )
    proc = subprocess.Popen([sys.executable, str(harness)])
    pid_file = tmp_path / "child.pid"
    deadline = time.monotonic() + 15
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    child_pid = int(pid_file.read_text())
    assert psutil.pid_exists(child_pid)

    proc.send_signal(signum)
    assert proc.wait(timeout=15) == int(signum)

    gone = time.monotonic() + 5
    while psutil.pid_exists(child_pid) and time.monotonic() < gone:
        time.sleep(0.05)
    try:
        status = psutil.Process(child_pid).status()
    except psutil.NoSuchProcess:
        status = psutil.STATUS_ZOMBIE
    assert status == psutil.STATUS_ZOMBIE  # dead (a zombie awaiting its reaper)


# ── the CLI: budget resolution, deadline derivation, distinguishable exit codes ──

def test_cli_refuses_to_run_without_a_resolvable_budget(tmp_path, capsys):
    from adda._src.infra.watchdog_launcher import main

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text("x")
    # No config.yaml `budget:` and no --budget flag: nothing to derive a
    # deadline from. Must fail fast, before any process is spawned.
    rc = main([str(study_dir)])
    assert rc == 2
    assert "budget" in capsys.readouterr().err.lower()


def test_cli_reports_timeout_distinguishably(tmp_path, monkeypatch):
    import adda._src.infra.watchdog_launcher as wl

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text("x")
    (study_dir / "config.yaml").write_text("budget: 100\n")

    captured = {}

    def fake_run_under_watchdog(cmd, *, deadline_s, **kwargs):
        captured["cmd"] = cmd
        captured["deadline_s"] = deadline_s
        return wl.WatchdogResult(timed_out=True, returncode=None, pgid=1234)

    monkeypatch.setattr(wl, "run_under_watchdog", fake_run_under_watchdog)
    rc = wl.main([str(study_dir)])

    assert rc == EXIT_TIMEOUT
    # The deadline really is the 2x floor of the resolved 100s budget — the
    # arithmetic a caller cannot silently tighten.
    assert captured["deadline_s"] == 200.0
    assert "--budget" in captured["cmd"]
    assert "100.0" in captured["cmd"]


def test_cli_propagates_the_childs_own_exit_status_when_not_timed_out(
    tmp_path, monkeypatch,
):
    import adda._src.infra.watchdog_launcher as wl

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text("x")

    def fake_run_under_watchdog(cmd, *, deadline_s, **kwargs):
        return wl.WatchdogResult(timed_out=False, returncode=0, pgid=1234)

    monkeypatch.setattr(wl, "run_under_watchdog", fake_run_under_watchdog)
    rc = wl.main([str(study_dir), "--budget", "60"])

    assert rc == 0


def test_cli_entrypoint_spawns_the_studys_own_script(tmp_path, monkeypatch):
    """python -m adda <study-dir> always builds AgenticRun's built-in default
    graph (no graph= is ever forwarded) — a study whose run.py declares its
    OWN Graph (extra roles, different edges) silently gets a different,
    smaller one through the CLI. --entrypoint launches that script directly
    instead."""
    import adda._src.infra.watchdog_launcher as wl

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text("x")
    (study_dir / "config.yaml").write_text("budget: 100\n")
    (study_dir / "run.py").write_text("print('hi')\n")

    captured = {}

    def fake_run_under_watchdog(cmd, *, deadline_s, **kwargs):
        captured["cmd"] = cmd
        captured["deadline_s"] = deadline_s
        return wl.WatchdogResult(timed_out=False, returncode=0, pgid=1234)

    monkeypatch.setattr(wl, "run_under_watchdog", fake_run_under_watchdog)
    rc = wl.main([str(study_dir), "--entrypoint", "run.py"])

    assert rc == 0
    assert captured["cmd"] == [sys.executable, str(study_dir / "run.py")]
    # Budget still comes from config.yaml and still derives the deadline —
    # only the launched command changes.
    assert captured["deadline_s"] == 200.0


def test_cli_entrypoint_rejects_model_and_budget_flags(tmp_path, capsys):
    from adda._src.infra.watchdog_launcher import main

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text("x")
    (study_dir / "config.yaml").write_text("budget: 100\n")
    (study_dir / "run.py").write_text("print('hi')\n")

    rc = main([str(study_dir), "--entrypoint", "run.py", "--budget", "60"])
    assert rc == 2
    assert "--entrypoint" in capsys.readouterr().err

    rc = main([str(study_dir), "--entrypoint", "run.py", "--model", "x"])
    assert rc == 2
    assert "--entrypoint" in capsys.readouterr().err


def test_cli_entrypoint_missing_script_errors_before_spawning(tmp_path, monkeypatch, capsys):
    import adda._src.infra.watchdog_launcher as wl

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text("x")
    (study_dir / "config.yaml").write_text("budget: 100\n")
    # No run.py written.

    def fake_run_under_watchdog(*args, **kwargs):
        raise AssertionError("must not spawn anything for a missing entrypoint")

    monkeypatch.setattr(wl, "run_under_watchdog", fake_run_under_watchdog)
    rc = wl.main([str(study_dir), "--entrypoint", "run.py"])
    assert rc == 2
    assert "run.py" in capsys.readouterr().err


def test_cli_without_entrypoint_behaviour_is_unchanged(tmp_path, monkeypatch):
    """Backward compatibility: omitting --entrypoint still builds the
    `python -m adda <study-dir>` command exactly as before."""
    import adda._src.infra.watchdog_launcher as wl

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "PROBLEM_STATEMENT.md").write_text("x")
    (study_dir / "config.yaml").write_text("budget: 100\n")

    captured = {}

    def fake_run_under_watchdog(cmd, *, deadline_s, **kwargs):
        captured["cmd"] = cmd
        return wl.WatchdogResult(timed_out=False, returncode=0, pgid=1234)

    monkeypatch.setattr(wl, "run_under_watchdog", fake_run_under_watchdog)
    rc = wl.main([str(study_dir), "--model", "some-model"])

    assert rc == 0
    assert captured["cmd"] == [
        sys.executable, "-m", "adda", str(study_dir),
        "--budget", "100.0", "--model", "some-model",
    ]
