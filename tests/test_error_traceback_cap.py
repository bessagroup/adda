"""An errored delegation's traceback is capped for the delegator, kept whole on disk."""
from types import SimpleNamespace

from adda._src.nodes.tools.routing.delegation import WorkerSession


def _self(tmp_path):
    notes = tmp_path / "debug" / "strategizer_notes"
    notes.mkdir(parents=True)
    return SimpleNamespace(
        node=SimpleNamespace(_current_notes_dir=notes), delegation_id="D007",
        _TB_HEAD=WorkerSession._TB_HEAD, _TB_TAIL=WorkerSession._TB_TAIL)


def test_long_traceback_keeps_head_and_tail_and_names_the_full_file(tmp_path):
    tb = "Traceback HEAD\n" + "x" * 100_000 + "\nRuntimeError: the root cause"
    out = WorkerSession._cap_traceback(_self(tmp_path), tb)
    full = tmp_path / "debug" / "delegations" / "D007" / "error.txt"
    assert len(out) < 6000
    assert out.startswith("Traceback HEAD") and out.endswith("RuntimeError: the root cause")
    assert str(full) in out and full.read_text() == tb


def test_short_traceback_is_untouched(tmp_path):
    assert WorkerSession._cap_traceback(_self(tmp_path), "boom") == "boom"
