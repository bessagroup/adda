"""Starting and killing a run from the viewer (spec 14 Phase 5.3 / 5.4).

No real run is ever started: ``run_control._launch_argv`` is swapped for a tiny
sleeping script, so these test the safety rules (pre-flight blocks, one live
run per study, a registry that survives, kill touching only what the viewer
started) and not the watchdog itself, which has its own tests.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from adda._src.viewer import run_control
from tests.test_viewer_app import _make_run, _make_study, _writer


@pytest.fixture
def study(tmp_path, monkeypatch):
    s = _make_study(tmp_path)
    (s / "PROBLEM_STATEMENT.md").write_text("# minimise y\nSuccess: y < 1\n")
    (s / "config.yaml").write_text(
        "budget: '00:10:00'\n"
        "evaluator:\n  entrypoint: workspace/evaluator.py:evaluate\n"
        "  output_names: [y]\n")
    (s / "workspace").mkdir()
    (s / "workspace" / "evaluator.py").write_text(
        "def evaluate(x):\n    return x\n")
    monkeypatch.setattr(run_control.shutil, "which", lambda _n: "/bin/claude")
    return s


@pytest.fixture
def sleeper(tmp_path, monkeypatch):
    """Make 'start a run' launch a process that sleeps until killed."""
    script = tmp_path / "fake_watchdog.py"
    script.write_text("import time\ntime.sleep(120)\n")
    monkeypatch.setattr(
        run_control, "_launch_argv",
        lambda _study: [sys.executable, str(script)])
    started: list[int] = []
    yield started
    for entry in run_control.launched(tmp_path / "study"):
        try:
            psutil.Process(entry["pid"]).kill()
        except psutil.Error:
            pass


def _checks(study, name):
    return next(c for c in run_control.preflight(study) if c["name"] == name)


def test_preflight_passes_a_well_formed_study(study):
    checks = run_control.preflight(study)
    assert not [c for c in checks if c["ok"] is False]
    assert {c["name"] for c in checks} >= {
        "problem_statement", "config", "budget", "evaluator", "backend",
        "no_live_run"}


def test_preflight_names_each_thing_that_is_wrong(study):
    (study / "PROBLEM_STATEMENT.md").write_text("   \n")
    (study / "config.yaml").write_text("evaluator:\n  entrypoint: nope.py:f\n")
    assert _checks(study, "problem_statement")["ok"] is False
    assert _checks(study, "budget")["ok"] is False
    ev = _checks(study, "evaluator")
    assert ev["ok"] is False and "nope.py" in ev["detail"]


def test_evaluator_attr_must_exist_but_is_never_imported(study):
    marker = study / "imported"
    (study / "workspace" / "evaluator.py").write_text(
        f"open({str(marker)!r}, 'w')\ndef other(x):\n    return x\n")
    assert _checks(study, "evaluator")["ok"] is False
    assert not marker.exists()


def test_the_unchecked_items_are_reported_as_unchecked_not_passed(study):
    assert _checks(study, "evaluator_one_sample")["ok"] is None


def test_a_failed_preflight_blocks_start_and_spawns_nothing(study, sleeper):
    (study / "config.yaml").write_text("evaluator: {}\n")
    client = _writer(study)
    resp = client.post("/api/study/start", json={})
    assert resp.status_code == 409
    assert any(c["name"] == "budget" and c["ok"] is False
               for c in resp.json()["checks"])
    assert run_control.launched(study) == []


def test_start_registers_the_process_and_audits_the_exact_command(study, sleeper):
    client = _writer(study)
    resp = client.post("/api/study/start", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert psutil.pid_exists(body["pid"])
    reg = json.loads((study / "runs" / "_viewer" / "registry.json").read_text())
    assert reg[0]["pid"] == body["pid"]
    rows = [json.loads(line) for line in
            (study / "viewer_actions.jsonl").read_text().splitlines()]
    assert rows[-1]["action"] == "start"
    assert rows[-1]["command"] == body["cmdline"]


def test_a_second_start_is_refused_while_one_is_alive(study, sleeper):
    client = _writer(study)
    assert client.post("/api/study/start", json={}).status_code == 200
    again = client.post("/api/study/start", json={})
    assert again.status_code == 409
    assert any(c["name"] == "no_live_run" and c["ok"] is False
               for c in again.json()["checks"])


def test_the_registry_outlives_the_viewer(study, sleeper):
    _writer(study).post("/api/study/start", json={})
    fresh = _writer(study)  # a new app, as after a viewer restart
    assert fresh.get("/api/study/preflight").json()["launched"][0]["alive"]
    assert fresh.post("/api/study/start", json={}).status_code == 409


def test_kill_ends_the_process_the_viewer_started_and_audits_it(study, sleeper):
    client = _writer(study)
    pid = client.post("/api/study/start", json={}).json()["pid"]
    resp = client.post("/api/study/kill", json={})
    assert resp.status_code == 200
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the started process survived Kill")
    actions = [json.loads(line) for line in
               (study / "viewer_actions.jsonl").read_text().splitlines()]
    assert any(a["action"] == "kill" and a["signal"] == "SIGTERM"
               and a["pid"] == pid for a in actions)


def test_kill_with_nothing_started_is_a_404(study):
    assert _writer(study).post("/api/study/kill", json={}).status_code == 404


def test_kill_never_touches_a_process_it_did_not_start(study):
    """A registry entry whose PID now belongs to something else (matching PID,
    different start time) must not be signalled."""
    import subprocess

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        d = study / "runs" / "_viewer"
        d.mkdir(parents=True)
        (d / "registry.json").write_text(json.dumps([{
            "pid": other.pid, "create_time": 1.0, "cmdline": "stale"}]))
        assert _writer(study).post("/api/study/kill", json={}).status_code == 404
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_start_and_kill_need_the_write_token(study, sleeper):
    from starlette.testclient import TestClient

    from adda._src.viewer.app import create_app

    anon = TestClient(create_app(study))
    assert anon.post("/api/study/start", json={}).status_code == 403
    assert anon.post("/api/study/kill", json={}).status_code == 403
    assert run_control.launched(study) == []


def test_a_run_with_no_status_that_is_still_writing_is_live(study):
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "run.log").write_text("working\n")
    assert run_control.live_run(study)["run_id"] == "20260904T120000"


def test_a_closed_run_is_not_live(study):
    run = _make_run(study, "20260904T120000")
    (run / "debug" / "run.log").write_text("done\n")
    (run / "debug" / "run_status.json").write_text('{"status": "GATED"}')
    assert run_control.live_run(study) is None


def test_a_crashed_run_that_stopped_writing_does_not_block_forever(study):
    run = _make_run(study, "20260904T120000")
    log = run / "debug" / "run.log"
    log.write_text("then it died\n")
    old = time.time() - run_control.LIVE_WINDOW_S - 60
    os.utime(log, (old, old))
    os.utime(run / "debug", (old, old))
    assert run_control.live_run(study) is None


def test_viewer_refuses_a_network_bind_without_the_flag(study):
    from adda._src.viewer.app import run_viewer

    with pytest.raises(ValueError, match="allow-network"):
        run_viewer(study, host="0.0.0.0", port=0)


def test_cli_exits_2_on_a_network_bind_without_the_flag(study, capsys):
    from adda._src.viewer.__main__ import main

    assert main([str(study), "--host", "0.0.0.0"]) == 2
    assert "allow-network" in capsys.readouterr().err
