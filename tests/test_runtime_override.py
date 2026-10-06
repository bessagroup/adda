"""Explicit argument > config.yaml > default; the environment sets no knob.

Two defects fixed here, both of which a sweep would have run straight into.

`settings._raw` checked `F3DASM_<KEY>` FIRST, ahead of everything. The docs
already say that channel "is for secrets and one-off overrides — it is not
where a study's settings belong", so the implementation contradicted the
documented intent: a stale export left in a shell beat the study's own config
and would have beaten a caller passing knobs programmatically, which for an
ablation means an arm running under a label it does not have.

And `settings` holds ONE process-global mapping installed at construction, so
building a second AgenticRun before running the first silently reconfigured it.
"""
from __future__ import annotations

import pytest

from adda._src.runtime import settings


@pytest.fixture(autouse=True)
def _clean():
    settings.configure(None)
    yield
    settings.configure(None)


def test_explicit_beats_the_study_config():
    settings.configure({"debug": False}, {"debug": True})
    assert settings.get_bool("debug", False) is True


def test_a_stale_environment_variable_sets_nothing_and_is_refused(monkeypatch):
    """The one that matters for a sweep: a forgotten shell export must not
    relabel an arm. It changes no value, and AgenticRun refuses to start."""
    monkeypatch.setenv("F3DASM_RECURSION_LIMIT", "7")
    settings.configure({"recursion_limit": 99})
    assert settings.get_int("recursion_limit", 1) == 99
    with pytest.raises(ValueError, match="F3DASM_RECURSION_LIMIT"):
        settings.reject_stale_env()


def test_the_variables_adda_hands_to_subprocesses_are_not_refused(monkeypatch):
    for name in ("F3DASM_NAMESPACE", "F3DASM_DELEGATION_ID", "F3DASM_RUN_CONFIG",
                 "F3DASM_CANONICAL_STORE", "F3DASM_DEDUP_SCOPE", "F3DASM_MEM_CAP"):
        monkeypatch.setenv(name, "x")
    settings.reject_stale_env()


def test_config_beats_the_default():
    settings.configure({"recursion_limit": 42})
    assert settings.get_int("recursion_limit", 1) == 42


def test_an_unknown_explicit_knob_raises():
    """A misspelled override must not quietly resolve to the default: that is
    the baseline running under an arm's label, reported as a null result."""
    with pytest.raises(ValueError, match="milstones_enabled"):
        settings.configure({}, {"milstones_enabled": False})


def test_an_unknown_config_knob_still_only_warns(caplog):
    """Deliberately lenient — a stale config block should not make a study
    unstartable."""
    with caplog.at_level("WARNING"):
        settings.configure({"dbeug": True})
    assert "dbeug" in caplog.text


def test_resolved_reports_what_the_run_actually_ran_with():
    """The condition recorded from the run itself, rather than asserted by
    whatever launched it."""
    settings.configure({"debug": True, "recursion_limit": 42},
                       {"milestones_enabled": False})

    out = settings.resolved()

    assert out["milestones_enabled"] is False   # explicit
    assert out["debug"] is True                # config
    assert out["recursion_limit"] == 42         # config
    assert "llm_retry_max" not in out           # untouched → absent, not faked


def test_constructing_a_second_run_does_not_reconfigure_the_first(tmp_path):
    """settings is process-global, so configure() belongs in execute(), not in
    __init__ where merely building another run would retarget this one."""
    from adda._src.runtime.agent_runtime import AgenticRun

    def _study(name, limit):
        d = tmp_path / name
        d.mkdir()
        (d / "PROBLEM_STATEMENT.md").write_text("# test\n")
        (d / "config.yaml").write_text(
            f"model: claude-haiku-4-5-20251001\nruntime:\n  recursion_limit: {limit}\n")
        return d

    first = AgenticRun(study_dir=_study("a", 11), interactive=False)
    AgenticRun(study_dir=_study("b", 22), interactive=False)

    # Before execute() the process-global mapping is whatever was installed
    # last; what matters is that running the FIRST one installs its own.
    settings.configure(first._study_runtime, first._runtime_override)
    assert settings.get_int("recursion_limit", 0) == 11


def _review_flag(tmp_path, config_text, **kw):
    from adda._src.runtime.agent_runtime import AgenticRun

    study = tmp_path / "s"
    study.mkdir(parents=True)
    (study / "PROBLEM_STATEMENT.md").write_text("p")
    (study / "config.yaml").write_text(config_text)
    return AgenticRun(study_dir=study, interactive=False, **kw)._review_statement


def test_review_statement_follows_config_unless_the_argument_is_given(tmp_path):
    assert _review_flag(tmp_path / "a", "review_statement: false\n") is False
    assert _review_flag(tmp_path / "b", "{}\n") is True
    assert _review_flag(tmp_path / "c", "review_statement: false\n",
                        review_statement=True) is True


def test_constructing_a_run_with_a_stale_knob_export_fails_at_construction(
        tmp_path, monkeypatch):
    monkeypatch.setenv("F3DASM_SCIENCE_MONITOR", "false")
    with pytest.raises(ValueError, match="F3DASM_SCIENCE_MONITOR"):
        _review_flag(tmp_path, "{}\n")
