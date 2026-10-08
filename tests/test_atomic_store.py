"""A writer killed mid-store must leave a readable canonical store.

Run 20261008T170240: the Claude CLI reaped a delegation's background script at
session end, in the middle of ``ExperimentData.store()``. Stock f3dasm opens
``input.csv`` (truncating it) before it writes, so the store kept a complete
``output.csv`` and an empty ``input.csv``. Every reader then saw an empty store.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from adda._src.evaluation._f3dasm_compat import PROTECTED_STORE_SENTINEL
from adda._src.evaluation.ledger_summary import (
    experiment_stores, load_experiments, total_ledgered_evals)
from adda._src.viewer.readers import _read_one_store
from f3dasm._src.design.domain import Domain
from f3dasm._src.experimentdata import ExperimentData
from f3dasm._src.experimentsample import ExperimentSample, JobStatus

_CHILD = '''
import os, sys
sys.path.insert(0, {tests!r})
import adda  # installs the compat layer
from test_atomic_store import _ed
from pathlib import Path
{kill}
_ed({n}).store(project_dir=Path({store!r}))
'''

# Die inside the first to_csv of the second store, right after the file opens.
_KILL_IN_WRITE = '''
    import pandas
    _orig = pandas.DataFrame.to_csv
    def _die(self, path=None, *a, **k):
        if "input" in str(path):
            open(path, "w").close()
            os._exit(9)
        return _orig(self, path, *a, **k)
    pandas.DataFrame.to_csv = _die
'''
# Die after the first {k} renames of the second store.
_KILL_AFTER_RENAMES = '''
    _rep, _cnt = os.replace, [0]
    def _die(a, b):
        if _cnt[0] == {k}:
            os._exit(9)
        _cnt[0] += 1
        _rep(a, b)
    os.replace = _die
'''


def _ed(n: int) -> ExperimentData:
    domain = Domain()
    domain.add_float("a", 0.0, 1e6)
    domain.add_output("z", exist_ok=True)
    domain.add_output("_delegation_id", exist_ok=True)
    rows = {
        i: ExperimentSample(
            _input_data={"a": float(i)},
            _output_data={"z": float(-i), "_delegation_id": "D001"},
            job_status=JobStatus.FINISHED,
        )
        for i in range(n)
    }
    return ExperimentData.from_data(data=rows, domain=domain)


def _store_killed(store: Path, n: int, kill: str) -> int:
    code = _CHILD.format(
        tests=str(Path(__file__).parent), kill=textwrap.dedent(kill), n=n, store=str(store))
    return subprocess.run([sys.executable, "-c", code]).returncode


@pytest.fixture
def store(tmp_path):
    s = tmp_path / "canonical"
    s.mkdir()
    (s / PROTECTED_STORE_SENTINEL).touch()
    _ed(5).store(project_dir=s)
    return s


def test_kill_inside_a_file_write_keeps_the_old_store(store):
    assert _store_killed(store, 8, _KILL_IN_WRITE) == 9
    data = store / "experiment_data"
    assert (data / "input.csv").stat().st_size > 0
    assert len(ExperimentData.from_file(project_dir=store)) == 5
    assert total_ledgered_evals(store) == 5


@pytest.mark.parametrize("renames_done", [0, 1, 2, 3])
def test_kill_between_renames_leaves_a_store_every_reader_agrees_on(
        store, renames_done):
    kill = _KILL_AFTER_RENAMES.format(k=renames_done)
    assert _store_killed(store, 8, kill) == 9
    # renames run domain, input, jobs, output; output is last, so the ledger
    # still holds the old 5 rows after any kill before the fourth rename.
    assert len(ExperimentData.from_file(project_dir=store)) in (5, 8)
    assert total_ledgered_evals(store) == 5
    assert len(load_experiments(store)["default"]) in (5, 8)
    assert _read_one_store(store, None)["n_evals"] == 5
    assert experiment_stores(store) == [store]


def test_a_later_store_removes_the_debris_of_a_killed_writer(store):
    _store_killed(store, 8, _KILL_AFTER_RENAMES.format(k=1))
    _ed(8).store(project_dir=store)
    data = store / "experiment_data"
    assert not list(data.glob(".tmp-*"))
    assert total_ledgered_evals(store) == 8


def test_the_shrink_guard_still_refuses_with_atomic_writes(store):
    with pytest.raises(RuntimeError, match="PROTECTED"):
        _ed(2).store(project_dir=store)
    assert len(ExperimentData.from_file(project_dir=store)) == 5
