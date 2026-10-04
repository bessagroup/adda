"""The real per-role prompt, per ablation arm.

`tests/test_features.py` checks each stripping mechanism on one string. This
assembles what each agent is actually handed -- preamble, stripped system
prompt, deliverable spec, gate contract, and the tool catalog -- through the
runtime's own path, so a feature-owned sentence added anywhere later is caught
by the arm that should have removed it.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import pytest

from adda._src.runtime import features, settings

_STUDY = Path(__file__).resolve().parent.parent / "studies" / "example_study"
_GATE_MARKER = re.compile(r"\[\[(?:if \w+|else|/if)\]\]")

# arm override -> strings that must not survive in ANY role's assembled prompt
_ARMS = {
    "hypothesis_ledger": (
        {"hypothesis_ledger": False},
        ("HypothesisPropose", "HypothesisUpdate", "hypotheses.json"),
    ),
    "milestones_enabled": (
        {"milestones_enabled": False},
        ("milestone", "Milestone"),
    ),
    "reproduction_gate": (
        {"reproduction_gate": False},
        ("CLAIMED_HEADLINE", "ZERO new oracle evals"),
    ),
    "pipeline_and_reproduction": (
        {"pipeline_deliverable": False, "reproduction_gate": False},
        ("pipeline.ipynb", "CLAIMED_HEADLINE", "NOTEBOOK-LEDGER SYNC"),
    ),
}


@pytest.fixture(autouse=True)
def _clean_settings():
    settings.configure(None)
    yield
    settings.configure(None)


def _assemble(tmp_path: Path, override: dict, graph=None) -> dict[str, tuple[str, set]]:
    from adda._src.runtime.agent_runtime import AgenticRun
    from adda._src.runtime.graph_builder import build_graph

    logging.disable(logging.CRITICAL)
    try:
        study = tmp_path / "study"
        study.mkdir()
        for f in ("PROBLEM_STATEMENT.md", "config.yaml"):
            shutil.copy(_STUDY / f, study / f)
        run = AgenticRun(study_dir=study, review_statement=False,
                         interactive=False, runtime=override, graph=graph)
        settings.configure(run._study_runtime, run._runtime_override)
        ctx = run._prepare_run()
        live: dict = {}
        build_graph(run._graph_spec, run._make_adapter,
                    study_dir=run.study_dir, interactive=False,
                    notes_dir=ctx.notes_dir, workspace_dir=ctx.workspace_dir,
                    delegation_log=ctx.delegation_log, node_registry=live)
        return {n: (node.adapter._render_system_prompt(),
                    set(node.adapter.closure_tools))
                for n, node in live.items()}
    finally:
        logging.disable(logging.NOTSET)


def test_all_on_prompts_carry_no_gate_markers_and_every_feature_text(tmp_path):
    assembled = _assemble(tmp_path, {})
    blob = "\n".join(p for p, _ in assembled.values())
    assert not _GATE_MARKER.findall(blob)
    for _, (_, tells) in _ARMS.items():
        for tell in tells:
            assert tell in blob, f"all-on prompts lost {tell!r}"


@pytest.mark.parametrize("arm", sorted(_ARMS))
def test_a_disabled_arm_reads_nothing_of_its_feature(arm, tmp_path):
    override, tells = _ARMS[arm]
    assembled = _assemble(tmp_path, override)
    assert assembled
    for role, (prompt, tools) in assembled.items():
        assert not _GATE_MARKER.findall(prompt), role
        for tell in tells:
            assert tell not in prompt, f"{arm}: {role} still mentions {tell!r}"
        assert not (features.disabled_tool_names() & tools), (arm, role)
