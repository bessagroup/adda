"""The wall-clock rules: the declared budget is the real limit.

Thresholds 0.75 / 0.80 / 0.85 / 0.90 (cutoff) / 0.95 / 1.0 (wind-down) of the
budget, a fake clock, no live model. The wind-down itself is in
``test_wind_down.py``.
"""
from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
import threading
import time

import pytest

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.infra import interrupt
from adda._src.nodes import Node
from adda._src.runtime import settings
from adda._src.runtime import time_rules as rules

BUDGET = 1800.0  # 30 min


class _Clock:
    def __init__(self, t0: float = 1_000_000.0) -> None:
        self.t0, self.t = t0, t0

    def __call__(self) -> float:
        return self.t

    def at(self, frac: float) -> None:
        self.t = self.t0 + frac * BUDGET


class _Stub:
    def __init__(self) -> None:
        self.closure_tools: dict = {}
        self.last_usage: dict = {}
        self.model = "m"
        self.interrupted = 0

    def invoke(self, messages):
        return "ok"

    def copy(self):
        return _Stub()

    def interrupt(self) -> list:
        self.interrupted += 1
        return []


def teardown_function(_fn) -> None:
    settings.configure(None)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(rules, "now", c)
    return c


@pytest.fixture
def entry(tmp_path, clock):
    class A(Agent):
        role = "strategizer"
        tools = frozenset({"Done", "Delegate", "Wait"})
        description = "s"

    class B(Agent):
        role = "implementer"
        description = "i"

    spec = Graph(nodes={"strategizer": A(), "implementer": B()},
                 edges=(Edge("strategizer", "implementer"),),
                 entry="strategizer")
    run_dir = tmp_path / "study" / "runs" / "T"
    (run_dir / "debug").mkdir(parents=True)
    n = Node(_Stub(), name="strategizer", outgoing=["implementer"],
             spec=spec, worker_adapters={"implementer": _Stub()})
    n._current_notes_dir = run_dir / "debug" / "strategizer_notes"
    n._budget_seconds = BUDGET
    n._run_start = clock.t0
    return n


def _diag(node) -> list[str]:
    p = node._current_run_dir / "debug" / "diagnostics.jsonl"
    if not p.exists():
        return []
    return [json.loads(x).get("error_type") for x in p.read_text().splitlines()]


# -- validation -------------------------------------------------------------

def test_validate_accepts_the_defaults():
    rules.validate({}, {})


@pytest.mark.parametrize("key,new", [
    ("run_backstop_multiple", "wind_down_at"),
    ("delegate_cutoff_multiple", "delegation_cutoff_at"),
    ("wrapup_at", "wind_down_at"),
    ("graceful_stop_at", "wind_down_at"),
    ("hard_stop_at", "wind_down_at"),
    ("stop_retrospective_s", "wind_down_turns"),
])
def test_a_removed_key_is_rejected_and_names_its_replacement(key, new):
    with pytest.raises(ValueError, match=new):
        rules.validate({key: 2.0}, {})


@pytest.mark.parametrize("bad", [
    {"budget_warn_from": 0.95},
    {"delegation_cutoff_at": 1.0},
    {"delegation_cutoff_at": 0.5},
    {"wind_down_at": 0.9},
    {"budget_warn_from": 0},
    {"budget_warn_every": 0},
    {"wind_down_tool_calls": 0},
    {"wind_down_turns": 0},
    {"wind_down_interrupt_after_s": -1},
    {"wind_down_at": "late"},
])
def test_thresholds_out_of_order_or_not_numbers_are_rejected(bad):
    with pytest.raises(ValueError):
        rules.validate(bad, {})


def test_the_wind_down_knobs_are_accepted():
    rules.validate({"wind_down_tool_calls": 30, "wind_down_turns": 3,
                    "wind_down_interrupt_after_s": 60, "wind_down_at": 1.1}, {})


def test_no_budget_means_no_rules():
    assert rules.TimeRules.from_settings(None) is None
    assert rules.TimeRules.from_settings(0) is None


# -- the schedule and the text ---------------------------------------------

def test_the_default_schedule_is_the_notices_the_cutoff_and_the_wind_down():
    r = rules.TimeRules.from_settings(BUDGET)
    assert [(n, round(f, 2)) for n, f in r.schedule()] == [
        ("notice:0.75", 0.75), ("notice:0.8", 0.8), ("notice:0.85", 0.85),
        ("cutoff", 0.9), ("notice:0.95", 0.95), ("wind_down", 1.0)]


def test_every_notice_states_the_time_left_in_minutes(clock):
    r = rules.TimeRules.from_settings(BUDGET)
    text = r.notice("notice:0.75", r.progress(0.75 * BUDGET, 0),
                    can_call_done=True)
    assert text == ("Budget: Time 75% (7 min left). The wind-down begins at "
                    "time budget of 30 min. Plan so you can call Done().")
    worker = r.notice("notice:0.8", r.progress(0.8 * BUDGET, 0),
                      can_call_done=False)
    assert "Time 80% (6 min left)" in worker
    assert "report what you have and return" in worker
    cut = r.notice(rules.CUTOFF, r.progress(0.9 * BUDGET, 0),
                   can_call_done=True)
    assert cut.startswith("No new delegations: Time 90% (3 min left)")


def test_the_cutoff_refusal_says_the_new_rule():
    r = rules.TimeRules.from_settings(BUDGET)
    t = r.cutoff_refusal(r.progress(0.92 * BUDGET, 0))
    assert "No new delegations" in t and "NOT started" in t
    assert "wind-down begins at time budget of 30 min" in t


def test_the_wall_and_token_budgets_are_in_the_notice_and_the_first_one_leads():
    r = rules.TimeRules.from_settings(BUDGET, tokens=1000, evals=50)
    p = r.progress(0.5 * BUDGET, 640, 20)
    assert p.driver == rules.TOKENS and round(p.fraction, 2) == 0.64
    text = r.notice("notice:0.75", p, can_call_done=True)
    assert "Time 50%" in text and "Tokens 64% (360 output tokens left)" in text
    assert "Evaluations" not in text
    assert "when the first of them is reached" in text


def test_a_tie_goes_to_wall_then_tokens():
    r = rules.TimeRules.from_settings(BUDGET, tokens=1000, evals=50)
    assert r.progress(BUDGET, 1000, 50).driver == rules.WALL
    assert r.progress(0, 1000, 50).driver == rules.TOKENS


def test_the_evaluation_fraction_never_drives_the_schedule():
    r = rules.TimeRules.from_settings(BUDGET, evals=100)
    p = r.progress(0.1 * BUDGET, 0, 250)
    assert p.driver == rules.WALL and p.fraction == pytest.approx(0.1)
    alone = rules.TimeRules.from_settings(None, evals=100)
    p = alone.progress(0, 0, 250)
    assert p.driver is None and p.fraction == 0.0
    assert alone.schedule() == [] and alone.due(p.fraction) == []


def test_the_evaluation_notice_names_what_is_left_and_says_nothing_of_stopping():
    r = rules.TimeRules.from_settings(None, evals=100)
    assert r.eval_notice(85) == (
        "Budget: Evaluations: 85% of the budget used (15 left).")
    assert r.eval_notice(110).endswith("(10 over).")
    for text in (r.eval_notice(85), r.eval_notice(110)):
        assert not any(w in text.lower() for w in
                       ("wind", "stop", "soft", "hard", "refus", "cutoff"))


def test_the_evaluation_notice_steps_are_the_notice_steps():
    r = rules.TimeRules.from_settings(None, evals=100)
    assert r.eval_step(0.74) is None
    assert [r.eval_step(f) for f in (0.75, 0.79, 0.80, 0.90, 1.0, 1.2)] == [
        0, 0, 1, 3, 5, 9]


# -- the thresholds fire at their fractions ---------------------------------

def test_each_threshold_fires_once_at_its_fraction(entry, clock):
    settings.configure({})
    clock.at(0.74)
    entry._time_rules_tick()
    assert _diag(entry) == []
    for frac, kind in ((0.75, "TIME_NOTICE"), (0.8, "TIME_NOTICE"),
                       (0.9, "TIME_CUTOFF")):
        before = _diag(entry).count(kind)
        clock.at(frac)
        entry._time_rules_tick()
        entry._time_rules_tick()
        assert _diag(entry).count(kind) == before + 1
    sent = [m for m in entry._notifications if m.startswith("[TIME")]
    assert len(sent) == 3
    assert "TIME_WIND_DOWN" not in _diag(entry)


def test_the_cutoff_is_enforced_with_budget_notes_off(entry, clock):
    settings.configure({"budget_notes": False})
    clock.at(0.8)
    assert entry._time_cutoff_refusal() is None
    clock.at(0.91)
    refusal = entry._time_cutoff_refusal()
    assert refusal.startswith("ERROR: No new delegations")
    entry._time_rules_tick()
    assert not [m for m in entry._notifications if m.startswith("[TIME")]


def test_no_budget_means_nothing_fires(entry, clock):
    entry._budget_seconds = None
    entry._run_start = None
    clock.at(5.0)
    entry._time_rules_tick()
    assert entry._time_cutoff_refusal() is None
    assert _diag(entry) == []


def test_the_wind_down_fires_once_at_the_budget(entry, clock):
    from adda._src.infra import wind_down
    clock.at(1.0)
    entry._time_rules_tick()
    entry._time_rules_tick()
    assert _diag(entry).count("TIME_WIND_DOWN") == 1
    assert wind_down.active()
    entry._time_rules_cancel()
    assert not wind_down.active()


def _evals(monkeypatch, used):
    from adda._src.runtime import constraint_snapshot
    monkeypatch.setattr(constraint_snapshot, "evals_for_node",
                        lambda node: used[0])


def test_the_evaluation_budget_only_sends_notices(entry, clock, monkeypatch):
    """Elvis 2026-10-09: evals send notices from 75% every 5% and never reach
    the cutoff or the wind-down, even far over the budget."""
    from adda._src.infra import wind_down
    settings.configure({})
    entry._eval_budget = 100
    used = [0]
    _evals(monkeypatch, used)
    clock.at(0.2)
    entry._time_rules_tick()
    assert _diag(entry) == []
    for n, count in ((74, 0), (75, 1), (79, 1), (80, 2), (200, 3)):
        used[0] = n
        entry._time_rules_tick()
        entry._time_rules_tick()
        assert _diag(entry).count("TIME_NOTICE") == count
    sent = [m for m in entry._notifications if m.startswith("[TIME")]
    assert sent[0] == "[TIME — Budget: Evaluations: 75% of the budget used (25 left).]"
    assert "TIME_WIND_DOWN" not in _diag(entry)
    assert "TIME_CUTOFF" not in _diag(entry)
    assert entry._time_cutoff_refusal() is None
    assert not wind_down.active()
    entry._time_rules_cancel()


def test_with_only_an_evaluation_budget_nothing_winds_down(
        entry, clock, monkeypatch):
    from adda._src.infra import wind_down
    settings.configure({})
    entry._budget_seconds = None
    entry._eval_budget = 10
    _evals(monkeypatch, [50])
    entry._time_rules_tick()
    assert _diag(entry) == ["TIME_NOTICE"]
    assert entry._time_cutoff_refusal() is None and not wind_down.active()
    entry._time_rules_cancel()


# -- interrupt reaches processes safely ------------------------------------

class _Owner:
    def __init__(self, pids):
        self.pids = pids

    def interrupt(self):
        return interrupt.interrupt_pids(self.pids)


def test_interrupt_signals_only_recorded_pids():
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with interrupt.in_flight(_Owner([ours.pid])):
            hit = interrupt.interrupt_all()
        assert ours.wait(timeout=10) is not None
        assert [h["pid"] for h in hit] == [ours.pid]
        assert other.poll() is None
    finally:
        for p in (ours, other):
            if p.poll() is None:
                p.kill()
            p.wait()


def _shell_in_sleep():
    """A child that has finished starting up and is sleeping. A SIGINT that
    lands during interpreter start-up (site, .pth hooks) is not the case the
    wind-down interrupts: it targets work already running."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import time; print('up', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "up"
    return proc


def _wait_after_interrupt(sess, proc, timeout):
    """SIGINT the session's shells, then wait for ``proc``. A hang reports what
    the session recorded and what the kernel shows for the process, so a
    failure on a runner we cannot reach names its own cause."""
    from adda._src.infra.resource_backend import get_resource_backend
    hit = sess.interrupt()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            sig = [ln for ln in open(f"/proc/{proc.pid}/status")
                   if ln.startswith(("Sig", "State"))]
        except OSError:
            sig = ["(no /proc)"]
        raise AssertionError(
            f"SIGINT did not end pid {proc.pid}: interrupt() signalled {hit}; "
            f"recorded start {sess._started.get(proc.pid)}, now "
            f"{get_resource_backend().proc_start_time(proc.pid)}; "
            f"{''.join(sig)}") from None


def test_a_wall_clock_step_does_not_disarm_the_wind_down_interrupt(monkeypatch):
    """psutil's epoch create_time adds boot_time(), which Linux re-reads from
    the wall clock on every call. A one-second clock step (a CI VM syncing)
    made the ownership check call a live shell a recycled pid, and the
    SIGINT went to nobody."""
    import psutil

    from adda._src.backends.openai_compatible import _BashSession
    sess = _BashSession(None)
    mine = _shell_in_sleep()
    try:
        sess.track(mine.pid)
        if hasattr(psutil, "_pslinux"):  # only Linux reads the wall clock
            real = psutil._pslinux.boot_time
            monkeypatch.setattr(psutil._pslinux, "boot_time",
                                lambda: real() + 1.0)
        _wait_after_interrupt(sess, mine, 30)
    finally:
        if mine.poll() is None:
            mine.kill()
        mine.wait()


def test_a_bash_session_interrupts_only_its_own_shells():
    from adda._src.backends.openai_compatible import _BashSession
    sess = _BashSession(None)
    mine = _shell_in_sleep()
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        sess.track(mine.pid)
        _wait_after_interrupt(sess, mine, 10)
        assert bystander.poll() is None
    finally:
        for p in (mine, bystander):
            if p.poll() is None:
                p.kill()
            p.wait()


def test_a_session_token_that_no_process_carries_signals_nothing():
    assert interrupt.interrupt_session(None) == []
    assert interrupt.interrupt_session(interrupt.new_token()) == []


def test_both_backends_expose_interrupt():
    from adda._src.backends.claude import ClaudeAdapter
    from adda._src.backends.openai_compatible import OpenAICompatibleAdapter
    for cls in (ClaudeAdapter, OpenAICompatibleAdapter):
        assert callable(getattr(cls, "interrupt", None))


# -- interrupt during a store flush -----------------------------------------

def _flusher(lock_path: str, target: str, started) -> None:
    """Write the store the way the flush does: under the lock, in two steps."""
    from filelock import FileLock
    with FileLock(lock_path):
        started.set()
        with open(target, "w") as fh:
            fh.write("a,b\n1,2\n")
            fh.flush()
            time.sleep(1.0)
            fh.write("3,4\n")


def test_interrupt_waits_for_a_flush_so_the_store_stays_loadable(tmp_path):
    data = tmp_path / "experiment_data"
    data.mkdir()
    lock, target = data / ".lock", data / "input.csv"
    interrupt.register_store_lock(lock)
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    proc = ctx.Process(target=_flusher, args=(str(lock), str(target), started))
    proc.start()
    try:
        assert started.wait(30)
        with interrupt.in_flight(_Owner([proc.pid])):
            interrupt.interrupt_all()
        proc.join(10)
        assert target.read_text() == "a,b\n1,2\n3,4\n"
    finally:
        interrupt._store_locks.discard(str(lock))
        if proc.is_alive():
            proc.kill()


def test_a_lock_that_cannot_be_won_does_not_block_the_wind_down(
        tmp_path, monkeypatch):
    from filelock import FileLock
    lock = tmp_path / ".lock"
    monkeypatch.setattr(interrupt, "STORE_LOCK_WAIT_S", 0.2)
    interrupt.register_store_lock(lock)
    held = FileLock(str(lock))
    held.acquire()
    done = threading.Event()
    try:
        t = threading.Thread(target=lambda: (interrupt.interrupt_all(), done.set()))
        t.start()
        assert done.wait(5)
    finally:
        held.release()
        interrupt._store_locks.discard(str(lock))
