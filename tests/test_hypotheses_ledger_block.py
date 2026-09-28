"""NOTEBOOK-LEDGER SYNC, by construction: the hypotheses cell owns one
delimited, tool-generated status block so a stale per-hypothesis status can
no longer reach a gate check or the critic (real friction, 2 of 3 example_
study Haiku runs: 20260928T024626 call_001 REVISE, 20260928T141126 call_002
REJECT CRITICAL — both a hand-maintained status table drifting from
hypotheses.json).
"""
from __future__ import annotations

from pathlib import Path

import nbformat

from adda._src.epistemics.hypothesis_ledger import HypothesisLedger
from adda._src.nodes.tools.routing.notebook import (
    _LEDGER_BLOCK_BEGIN,
    _LEDGER_BLOCK_END,
    _refresh_ledger_block,
    _render_ledger_status_block,
    refresh_hypotheses_ledger_block,
)


def _ledger(tmp_path: Path) -> HypothesisLedger:
    notes = tmp_path / "strategizer_notes"
    notes.mkdir(parents=True, exist_ok=True)
    return HypothesisLedger(notes)


def test_render_block_empty_ledger_is_a_sane_row():
    block = _render_ledger_status_block(None)
    assert block.startswith(_LEDGER_BLOCK_BEGIN)
    assert block.endswith(_LEDGER_BLOCK_END)
    assert "(no hypotheses)" in block


def test_render_block_lists_id_status_posterior_and_one_line_statement(tmp_path):
    led = _ledger(tmp_path)
    hid = led.propose(
        "Multi-line statement\nsecond line never shown",
        "falsification criterion", "prediction", 0.6, "strategizer")
    out = led.update(
        hid, "SUPPORTED", "evidence", evidence={"delegation": "D001"},
        posterior=0.95, triggered_by=None)
    assert not out.startswith("ERROR"), out
    block = _render_ledger_status_block(led)
    assert hid in block
    assert "SUPPORTED" in block
    assert "0.95" in block
    assert "Multi-line statement" in block
    assert "second line never shown" not in block


def test_refresh_inserts_block_after_heading_when_absent():
    source = "## Hypotheses\n\nH1: the oracle is correct."
    out = _refresh_ledger_block(source, None)
    assert out.startswith("## Hypotheses\n\n" + _LEDGER_BLOCK_BEGIN)
    assert "H1: the oracle is correct." in out
    # narrative preserved verbatim, just relocated after the block
    assert out.endswith("H1: the oracle is correct.")


def test_refresh_replaces_a_stale_block_preserving_narrative_byte_for_byte():
    stale = (
        "## Hypotheses\n\n"
        + _LEDGER_BLOCK_BEGIN + "\n(stale table)\n" + _LEDGER_BLOCK_END
        + "\n\nAuthor narrative that must survive untouched."
    )
    out = _refresh_ledger_block(stale, None)
    assert "(stale table)" not in out
    assert "(no hypotheses)" in out
    assert out.endswith("Author narrative that must survive untouched.")
    # Everything outside the markers is byte-identical to the original.
    before = stale[:stale.index(_LEDGER_BLOCK_BEGIN)]
    after = stale[stale.index(_LEDGER_BLOCK_END) + len(_LEDGER_BLOCK_END):]
    assert out.startswith(before)
    assert out.endswith(after)


def test_refresh_hypotheses_ledger_block_noop_when_no_notebook(tmp_path):
    # Must not raise when pipeline.ipynb does not exist yet.
    refresh_hypotheses_ledger_block(tmp_path, None)


def test_refresh_hypotheses_ledger_block_noop_when_no_hypotheses_cell(tmp_path):
    from adda._src.evaluation.notebook_exec import build_notebook
    nbformat.write(
        build_notebook([{"type": "markdown", "source": "# Problem",
                          "name": "problem"}]),
        str(tmp_path / "pipeline.ipynb"))
    before = (tmp_path / "pipeline.ipynb").read_text()
    refresh_hypotheses_ledger_block(tmp_path, None)
    assert (tmp_path / "pipeline.ipynb").read_text() == before


def test_refresh_hypotheses_ledger_block_end_to_end_on_disk(tmp_path):
    from adda._src.evaluation.notebook_exec import build_notebook
    led = _ledger(tmp_path)
    hid = led.propose(
        "The GP surrogate reaches y < 0.1", "criterion", "prediction",
        0.5, "strategizer")
    out = led.update(
        hid, "SUPPORTED", "evidence", evidence={"delegation": "D001"},
        posterior=0.9, triggered_by=None)
    assert not out.startswith("ERROR"), out

    nb_path = tmp_path / "pipeline.ipynb"
    nbformat.write(
        build_notebook([
            {"type": "markdown", "source": "# Problem", "name": "problem"},
            {"type": "markdown",
             "source": "## Hypotheses\n\n" + _LEDGER_BLOCK_BEGIN
             + "\n(no hypotheses)\n" + _LEDGER_BLOCK_END
             + "\n\nMy own narrative stays here.",
             "name": "hypotheses"},
        ]),
        str(nb_path))

    refresh_hypotheses_ledger_block(tmp_path, led)

    nb = nbformat.read(str(nb_path), as_version=4)
    hyp_cell = next(
        c for c in nb.cells if c.get("metadata", {}).get("name") == "hypotheses")
    src = hyp_cell["source"]
    assert hid in src
    assert "SUPPORTED" in src
    assert "0.9" in src
    assert "My own narrative stays here." in src
    assert "(no hypotheses)" not in src
