"""Which machine a process ran on: hostname plus the Slurm job, when set."""
from __future__ import annotations

import os
import socket


def host_provenance() -> dict:
    """``{"host": ..., "slurm_job_id": ..., "slurmd_nodename": ...}``.

    The Slurm keys are present only when their variable is set, so a record
    from a laptop is not padded with nulls.
    """
    rec: dict = {"host": socket.gethostname()}
    for key, var in (("slurm_job_id", "SLURM_JOB_ID"),
                     ("slurmd_nodename", "SLURMD_NODENAME")):
        value = os.environ.get(var)
        if value:
            rec[key] = value
    return rec
