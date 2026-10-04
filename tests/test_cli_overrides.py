"""`--set key=value` is the command-line spelling of AgenticRun(runtime=...).

A campaign runs one committed study under many arms; the arm has to arrive
without editing the study, outrank env and config.yaml, fail loudly on a typo
(a typo'd override would run the baseline under an arm's label), and survive
the watchdog's hop to the child process.
"""
from __future__ import annotations

import argparse
import json

import pytest

from adda import __main__ as cli
from adda._src.infra import watchdog_launcher as wl
from adda._src.runtime import cli_overrides, settings


@pytest.fixture(autouse=True)
def _clean_settings():
    settings.configure(None)
    yield
    settings.configure(None)


def test_values_are_read_as_yaml():
    assert cli_overrides.set_item("hypothesis_ledger=false") == (
        "hypothesis_ledger", False)
    assert cli_overrides.set_item("max_awake_nodes=3") == (
        "max_awake_nodes", 3)
    assert cli_overrides.set_item("thinking_display=omitted") == (
        "thinking_display", "omitted")


@pytest.mark.parametrize("bad", ["nonsense=1", "hypothesis_ledger", "=1"])
def test_a_bad_override_is_rejected_before_launch(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        cli_overrides.set_item(bad)


def test_a_repeated_key_is_an_error():
    items = [("science_monitor", False), ("science_monitor", True)]
    with pytest.raises(ValueError, match="twice"):
        cli_overrides.as_runtime(items)


def test_model_defaults_to_none_so_the_study_config_wins():
    args = cli._build_parser().parse_args(["study"])
    assert args.model is None
    assert args.overrides == []


def test_overrides_reach_the_run_and_are_recorded(tmp_path, monkeypatch):
    study = tmp_path / "s"
    study.mkdir()
    (study / "PROBLEM_STATEMENT.md").write_text("# t\n")
    (study / "config.yaml").write_text(
        "model: claude-sonnet-5\nruntime:\n  science_monitor: true\n")

    seen = {}

    class _Run:
        def __init__(self, **kw):
            seen.update(kw)

        def execute(self):
            return "ok"

    import adda._src.runtime.agent_runtime as ar
    monkeypatch.setattr(ar, "AgenticRun", _Run)
    rc = cli.main([str(study), "--set", "science_monitor=false",
                   "--set", "max_awake_nodes=2"])

    assert rc == 0
    assert seen["model"] is None
    assert seen["runtime"] == {"science_monitor": False, "max_awake_nodes": 2}


def test_the_explicit_override_outranks_the_study_config(tmp_path):
    from adda._src.runtime import features
    from adda._src.runtime.agent_runtime import AgenticRun

    study = tmp_path / "s"
    study.mkdir()
    (study / "PROBLEM_STATEMENT.md").write_text("# t\n")
    (study / "config.yaml").write_text(
        "runtime:\n  science_monitor: true\n")
    AgenticRun(study_dir=study, interactive=False,
               runtime={"science_monitor": False})
    assert features.enabled("science_monitor") is False
    assert "science_monitor" in settings.resolved()


def test_the_watchdog_forwards_overrides_to_the_child(tmp_path, monkeypatch):
    study = tmp_path / "s"
    study.mkdir()
    (study / "PROBLEM_STATEMENT.md").write_text("x")
    (study / "config.yaml").write_text("budget: 100\n")
    captured = {}

    class _R:
        interrupted_by = None
        timed_out = False
        returncode = 0

    def fake(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _R()

    monkeypatch.setattr(wl, "run_under_watchdog", fake)
    rc = wl.main([str(study), "--set", "hypothesis_ledger=false",
                  "--set", "stop_grace_s=7"])

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[cmd.index("--set") + 1] == "hypothesis_ledger=false"
    assert "stop_grace_s=7" in cmd
    # the launcher's own knobs honour the override too
    assert captured["kw"]["stop_grace_s"] == 7


def test_the_watchdog_refuses_overrides_with_an_entrypoint(tmp_path, capsys):
    study = tmp_path / "s"
    study.mkdir()
    (study / "run.py").write_text("")
    rc = wl.main([str(study), "--entrypoint", "run.py",
                  "--set", "science_monitor=false"])
    assert rc == 2
    assert "--set" in capsys.readouterr().err


def test_a_duplicate_override_is_a_clean_error_in_the_watchdog(
        tmp_path, capsys):
    study = tmp_path / "s"
    study.mkdir()
    (study / "config.yaml").write_text("budget: 100\n")
    rc = wl.main([str(study), "--set", "science_monitor=false",
                  "--set", "science_monitor=true"])
    assert rc == 2
    assert "twice" in capsys.readouterr().err


def test_overrides_are_not_json_garbled_by_format_set():
    out = cli_overrides.format_set([("science_monitor", False),
                                    ("thinking_display", "omitted")])
    assert out == ["--set", "science_monitor=false",
                   "--set", "thinking_display=omitted"]
    assert json.dumps(out)
