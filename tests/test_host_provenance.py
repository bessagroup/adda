"""Records name the machine: run_config.json once, governor_pids.jsonl per row."""
import json
import socket

from adda._src.evaluation import oracle_resolution as inst
from adda._src.infra.host_provenance import host_provenance
from adda._src.runtime.run_setup import _init_canonical_store


def test_slurm_keys_only_when_set(monkeypatch):
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("SLURMD_NODENAME", raising=False)
    assert host_provenance() == {"host": socket.gethostname()}
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    monkeypatch.setenv("SLURMD_NODENAME", "gpu2001")
    assert host_provenance() == {
        "host": socket.gethostname(),
        "slurm_job_id": "123", "slurmd_nodename": "gpu2001"}


def test_run_config_records_host_and_keeps_it_on_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    run_dir = tmp_path / "runs" / "t"
    (run_dir / "debug").mkdir(parents=True)
    cfg = _init_canonical_store(run_dir, tmp_path / "study")
    assert cfg["host"]["slurm_job_id"] == "123"
    monkeypatch.setenv("SLURM_JOB_ID", "999")
    cfg2 = _init_canonical_store(run_dir, tmp_path / "study")
    assert cfg2["host"]["slurm_job_id"] == "123"
    on_disk = json.loads((run_dir / "debug" / "run_config.json").read_text())
    assert on_disk["host"] == cfg["host"]


def test_governor_row_records_host(tmp_path, monkeypatch):
    class _Be:
        def set_self_limit(self, cap):
            return False
        def proc_start_time(self, pid):
            return 1.0

    monkeypatch.setattr(inst, "_GOVERNOR_PID_APPLIED", False)
    monkeypatch.setattr("adda._src.infra.resource_backend.get_resource_backend",
                        lambda: _Be())
    monkeypatch.setenv("SLURMD_NODENAME", "node7")
    (tmp_path / "debug").mkdir()
    inst._apply_process_governor({}, tmp_path / "experiment_data", "D001")
    rec = json.loads((tmp_path / "debug" / "governor_pids.jsonl")
                     .read_text().splitlines()[-1])
    assert rec["host"] == socket.gethostname()
    assert rec["slurmd_nodename"] == "node7"
