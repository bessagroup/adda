"""RunScratch/RunNotebook's integrity guard must never erase rows the call
did not write (run 20260928T225501: a concurrent campaign's append was
reverted because its flush also extended the schema)."""
import threading
from pathlib import Path
from types import SimpleNamespace

from f3dasm import ExperimentData
from f3dasm.design import Domain

from adda._src.nodes.tools.routing.notebook import (
    NotebookTools,
    _classify_store_file,
)


def _domain(*outs):
    d = Domain()
    d.add_float("x", 0, 1)
    for o in outs:
        d.add_output(o)
    return d


def _store(root: Path, xs, outs=("y",), extra=None):
    d = _domain(*outs)
    ed = ExperimentData(
        input_data=[{"x": x} for x in xs],
        output_data=[{o: float(x) for o in outs} for x in xs],
        domain=d,
    )
    if extra is not None:
        ed = ExperimentData.from_file(project_dir=root) + ed
    ed.store(project_dir=root)


def _tools(root: Path, running=False):
    node = SimpleNamespace(
        _resolve_run_dir=lambda: root,
        _study_dir=None,
        _registry_lock=threading.Lock(),
        _registry={"D001": {"status": "Working" if running else "Done"}},
    )
    return NotebookTools(node)


def _read(root):
    return {p.name: p.read_bytes()
            for p in (root / "experiment_data").iterdir()
            if p.name != ".lock"}


def test_schema_extending_flush_is_an_append(tmp_path):
    _store(tmp_path, [0.1, 0.2])
    before = _read(tmp_path)
    old = ExperimentData.from_file(project_dir=tmp_path)
    old._domain.add_output("_ts", exist_ok=True)
    new = ExperimentData(input_data=[{"x": 0.3}],
                         output_data=[{"y": 0.3, "_ts": 5.0}],
                         domain=_domain("y", "_ts"))
    (old + new).store(project_dir=tmp_path)
    after = _read(tmp_path)
    for name in ("output.csv", "domain.json"):
        assert not after[name].startswith(before[name])
        assert _classify_store_file(
            Path(name), before[name], after[name]) == "append"


def test_row_rewrite_is_still_other(tmp_path):
    b = b",y\n0,1.0\n1,2.0\n"
    assert _classify_store_file(Path("output.csv"), b,
                                b",y\n0,9.0\n1,2.0\n2,3.0\n") == "other"
    assert _classify_store_file(Path("output.csv"), b, b",y\n0,1.0\n") == "other"


def test_concurrent_schema_extending_flush_is_not_reverted(tmp_path):
    _store(tmp_path, [0.1, 0.2])
    tools = _tools(tmp_path, running=False)
    with tools._canonical_integrity_guard() as changed:
        old = ExperimentData.from_file(project_dir=tmp_path)
        old._domain.add_output("_ts", exist_ok=True)
        new = ExperimentData(input_data=[{"x": 0.3}],
                             output_data=[{"y": 0.3, "_ts": 5.0}],
                             domain=_domain("y", "_ts"))
        (old + new).store(project_dir=tmp_path)
    assert len(ExperimentData.from_file(project_dir=tmp_path)) == 3
    assert changed[0]["reverted"] == []
    assert changed[0]["left_in_place"]


def test_snippet_damage_is_still_reverted(tmp_path):
    _store(tmp_path, [0.1, 0.2])
    original = _read(tmp_path)
    tools = _tools(tmp_path, running=False)
    with tools._canonical_integrity_guard() as changed:
        (tmp_path / "experiment_data" / "output.csv").write_bytes(
            b",y\n0,99.0\n1,2.0\n")
    assert _read(tmp_path) == original
    assert changed[0]["reverted"]


def test_revert_skips_a_file_that_moved_after_the_call(tmp_path, monkeypatch):
    _store(tmp_path, [0.1, 0.2])
    out = tmp_path / "experiment_data" / "output.csv"
    tools = _tools(tmp_path, running=False)
    late = b",y\n0,99.0\n1,2.0\n2,3.0\n"
    real = tools._any_delegation_running

    def racing_check():
        # runs between the after-snapshot and the revert: a foreign writer
        out.write_bytes(late)
        return real()

    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return real() if calls["n"] == 1 else racing_check()

    monkeypatch.setattr(tools, "_any_delegation_running", check)
    with tools._canonical_integrity_guard() as changed:
        out.write_bytes(b",y\n0,99.0\n1,2.0\n")
    assert out.read_bytes() == late
    assert changed[0]["reverted"] == []
