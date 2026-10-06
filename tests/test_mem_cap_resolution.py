"""Hard per-delegation RAM cap resolution: config > env > SLURM > default.

The watchdog cap must reflect the real HPC budget. Previously it read only
config.yaml `mem_cap` (else a 4 GB default) despite a comment claiming env +
SLURM support, so on SLURM it kept a small hardcoded ceiling that silently
throttled worker concurrency (run 20260706T204732: ~3x wall-clock, D005).
"""
from __future__ import annotations

from adda._src.runtime.run_setup import (
    DEFAULT_MEM_CAP_BYTES,
    resolve_mem_cap_bytes,
)

GB = 1024 ** 3
MB = 1024 ** 2


def test_explicit_config_wins():
    assert resolve_mem_cap_bytes(12 * GB, env={"SLURM_MEM_PER_NODE": "4096"}) == 12 * GB


def test_the_environment_does_not_set_the_cap():
    assert resolve_mem_cap_bytes(None, env={"F3DASM_MEM_CAP": str(8 * GB),
                                             "SLURM_MEM_PER_NODE": "4096"}) == 4096 * MB


def test_an_exported_mem_cap_is_refused(monkeypatch):
    import pytest

    from adda._src.runtime import settings

    monkeypatch.setenv("F3DASM_MEM_CAP", str(8 * GB))
    with pytest.raises(ValueError, match="F3DASM_MEM_CAP"):
        settings.reject_stale_env()


def test_slurm_per_node_used_when_no_config_or_env():
    assert resolve_mem_cap_bytes(None, env={"SLURM_MEM_PER_NODE": "131072"}) == 131072 * MB


def test_slurm_per_cpu_times_cpus():
    env = {"SLURM_MEM_PER_CPU": "4096", "SLURM_CPUS_ON_NODE": "16(x2)"}
    assert resolve_mem_cap_bytes(None, env=env) == 4096 * 16 * MB


def test_falls_back_to_default():
    assert resolve_mem_cap_bytes(None, env={}) == DEFAULT_MEM_CAP_BYTES
    assert resolve_mem_cap_bytes("garbage", env={}) == DEFAULT_MEM_CAP_BYTES
