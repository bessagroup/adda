"""Phase 2: the datagenerator -> oracle auto-registration hook.

When a delegation to a node with role=="datagenerator" completes and left a
registration.json manifest in its workspace, the runtime points the canonical
evaluator entrypoint at the authored generator (best-effort).

The hook fires from ``_finish_ok`` (delegation.py), which under the default
(peer_interaction on) only runs on approval, not on collection (spec 12 item
8 -- the workspace commit and every side effect that rides with it move to
AFTER approval). Every test here collects via Delegate(wait=True) and then
approves (see ``approve_delegation``) before checking the registration
side effect, matching how a real delegator reaches it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from adda._src.backends.base import Agent, Edge, Graph
from adda._src.nodes import Node

from tests.test_evaluator_resolution import _write_run_config

from .fixtures import approve_delegation


def _delegate_and_approve(closures, **kw):
    """Delegate(wait=True) then approve the resulting open review, the way
    a real delegator would -- returns the APPROVE call's own result, since
    that is what carries any notice a finalize-time hook (like the
    registration handoff) appends."""
    out = closures["Delegate"](wait=True, **kw)
    did = re.search(r"\[(D\d+)\]", out)
    if did:
        return approve_delegation(closures, did.group(1))
    return out


class _Strat(Agent):
    role = "strategizer"
    tools = frozenset({"Done", "FollowUp", "WriteNote", "ReadNote"})
    description = "Test strategizer."


class _DataGen(Agent):
    role = "datagenerator"
    description = "Test datagenerator."


class _Impl(Agent):
    role = "implementer"
    description = "Test implementer."


class _Worker:
    closure_tools: dict = {}

    def invoke(self, messages):
        return (
            "## Report\n\n### Actions taken\n- built\n\n"
            "### Files touched\n- gen\n\n### Conclusions\nok\n\n"
            "### Numbers\n- n: 0\n"
        )

    def copy(self):
        return self


def _build(tmp_path, target_name, target_agent):
    """Return (node, closures, run_dir, cfg_path) wired for one Delegate."""
    study_dir = tmp_path / "study"
    run_dir = study_dir / "runs" / "T"
    debug_dir = run_dir / "debug"
    store_dir = run_dir / "experiment_data"
    store_dir.mkdir(parents=True, exist_ok=True)
    _write_run_config(debug_dir, store_dir, study_dir)

    spec = Graph(
        nodes={"strat": _Strat(), target_name: target_agent},
        edges=(Edge("strat", target_name),),
        entry="strat",
    )
    strat_adapter = MagicMock()
    strat_adapter.closure_tools = {}
    strat_adapter.route_watcher = None
    worker = _Worker()
    node = Node(
        adapter=strat_adapter,
        name="strat",
        outgoing=[target_name],
        spec=spec,
        study_dir=study_dir,
        worker_adapters={target_name: worker},
    )
    node._current_notes_dir = debug_dir / "strategizer_notes"
    return node, strat_adapter.closure_tools, run_dir, debug_dir / "run_config.json"


def _drop_manifest(run_dir, delegation_id="D001", attr="x_gen"):
    gens = run_dir / "debug" / "delegations" / delegation_id / "generators"
    gens.mkdir(parents=True, exist_ok=True)
    (gens / "x.py").write_text(
        f"def {attr}(**kw):\n    return float(sum(kw.values()))\n"
    )
    (gens / "registration.json").write_text(json.dumps({
        "generator_file": "x.py", "attr": attr, "output_names": ["f"],
    }))


def test_datagenerator_manifest_triggers_registration(tmp_path):
    node, closures, run_dir, cfg_path = _build(
        tmp_path, "datagen", _DataGen()
    )
    _drop_manifest(run_dir, "D001")  # first Delegate id is D001
    _delegate_and_approve(
        closures, target="datagen", intent="build oracle",
        expected_report="",
    )
    cfg = json.loads(cfg_path.read_text())
    assert cfg["evaluator_entrypoint"].endswith(
        "runs/T/debug/delegations/D001/generators/x.py:x_gen"
    )
    assert cfg["evaluator_output_names"] == ["f"]
    assert cfg["evaluator_lookup"] is None


def test_non_datagenerator_target_does_not_register(tmp_path):
    node, closures, run_dir, cfg_path = _build(tmp_path, "impl", _Impl())
    _drop_manifest(run_dir, "D001")  # manifest present but target is implementer
    before = cfg_path.read_text()
    _delegate_and_approve(
        closures, target="impl", intent="run", expected_report="",
    )
    assert cfg_path.read_text() == before  # unchanged


def test_missing_manifest_no_crash(tmp_path):
    node, closures, run_dir, cfg_path = _build(
        tmp_path, "datagen", _DataGen()
    )
    before = cfg_path.read_text()
    result = _delegate_and_approve(
        closures, target="datagen", intent="no manifest",
        expected_report="",
    )
    assert "Errored" not in result  # delegation still completed, no crash
    assert cfg_path.read_text() == before  # config untouched


# ---------------------------------------------------------------------------
# Extending the oracle that is already canonical
#
# A delegation told to extend the existing oracle used to face three
# instructions it could not jointly satisfy: the role contract makes the
# manifest mandatory, dropping a manifest repoints the canonical entrypoint,
# and the brief said not to repoint. Run 20260907T212358's D002 and D009 each
# independently invented the same escape — name the already-canonical file by
# ABSOLUTE path so the repoint lands where it already pointed. Stating the
# intent replaces the path trick.
# ---------------------------------------------------------------------------

def _drop_in_place_manifest(run_dir, study_dir, delegation_id="D001"):
    """A manifest that points at a canonical file OUTSIDE the delegation
    folder and declares the extend-in-place intent."""
    canonical = study_dir / "scripts" / "canonical_gen.py"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("def canon(**kw):\n    return 1.0\n")
    gens = run_dir / "debug" / "delegations" / delegation_id / "generators"
    gens.mkdir(parents=True, exist_ok=True)
    (gens / "registration.json").write_text(json.dumps({
        "generator_file": str(canonical),
        "attr": "canon",
        "output_names": ["f"],
        "extends_canonical": True,
    }))
    return canonical


def test_extends_canonical_does_not_repoint_the_entrypoint(tmp_path):
    node, closures, run_dir, cfg_path = _build(
        tmp_path, "datagen", _DataGen()
    )
    _drop_in_place_manifest(run_dir, tmp_path / "study", "D001")
    before = cfg_path.read_text()

    _delegate_and_approve(
        closures, target="datagen", intent="extend the canonical generator",
        expected_report="",
    )

    assert cfg_path.read_text() == before, (
        "extends_canonical means the file is ALREADY canonical — repointing "
        "is exactly what the delegation was told not to do"
    )


def test_extends_canonical_still_records_who_touched_the_oracle(tmp_path):
    """Skipping the repoint must not skip the RECORD: which delegation
    extended the canonical source is part of the run's provenance."""
    node, closures, run_dir, cfg_path = _build(
        tmp_path, "datagen", _DataGen()
    )
    _drop_in_place_manifest(run_dir, tmp_path / "study", "D001")

    # The notice reaches the agent through the approve call's own result:
    # every tool call delivers what is queued, via the dispatch wrapper --
    # and the registration hook itself only fires at approval (spec 12
    # item 8), not at collection.
    out = _delegate_and_approve(
        closures, target="datagen", intent="extend the canonical generator",
        expected_report="",
    )

    with node._notifications_lock:
        notes = out + "\n".join(node._notifications)
    assert "extended in place" in notes
    assert "D001" in notes
    assert "canonical_gen.py" in notes


def test_a_normal_manifest_still_repoints_and_names_its_author(tmp_path):
    """The ordinary authoring path is unchanged, and now also records which
    delegation did the authoring."""
    node, closures, run_dir, cfg_path = _build(
        tmp_path, "datagen", _DataGen()
    )
    _drop_manifest(run_dir, "D001")

    # The notice reaches the agent through the approve call's own result --
    # the registration hook fires at approval (spec 12 item 8), not at
    # collection.
    out = _delegate_and_approve(
        closures, target="datagen", intent="build oracle",
        expected_report="",
    )

    cfg = json.loads(cfg_path.read_text())
    assert cfg["evaluator_entrypoint"].endswith("D001/generators/x.py:x_gen")
    with node._notifications_lock:
        notes = out + "\n".join(node._notifications)
    assert "registered" in notes and "D001" in notes
