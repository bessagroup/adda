"""Re-home f3dasm-core behaviours adda needs but stock f3dasm does not ship.

adda depends on a released f3dasm and carries no copy of f3dasm core, so it
cannot patch core. Behaviours it relies on were once added to a fork of
f3dasm core; this module reinstalls them on ``ExperimentData`` at import time,
using only the public surface (``to_pandas``, ``len``, the on-disk layout):

1. ``to_numpy()`` drops underscore-prefixed metadata columns (the provenance
   stamps ``_delegation_id`` / ``_source`` / ``_ts`` / ``_wall_ms``), so the
   returned array stays float instead of decaying to ``object`` dtype.
2. ``store()`` refuses a write that would SHRINK a PROTECTED store (a project
   dir marked with ``PROTECTED_STORE_SENTINEL``), so a stray partial
   ``.store()`` cannot clobber the metered canonical store.
3. ``store()`` writes each file atomically (temp file, fsync, rename), so a
   process killed mid-store leaves the old or the new file, never a torn one.

All are idempotent. Applied on top of a f3dasm that already has them, the
underscore drop is a no-op and the guard raises identically. The patch is
applied once, guarded by a sentinel attribute on ``ExperimentData``.
"""
from __future__ import annotations

import os
from pathlib import Path

_APPLIED_FLAG = "_adda_compat_applied"

# adda owns these; they mirror f3dasm's on-disk layout constants.
PROTECTED_STORE_SENTINEL = ".f3dasm_protected"
_STORE_SUBFOLDER = "experiment_data"
_OUTPUT_CSV = "output.csv"
_JOBS_CSV = "jobs.csv"
_INPUT_CSV = "input.csv"
_DOMAIN_JSON = "domain.json"


def _atomic_store(self, subdir: Path) -> None:
    """Write the four store files so a kill never leaves a torn file.

    Stock ``store()`` streams each file with ``to_csv`` (open, truncate, write),
    so a process killed mid-write leaves an empty or half file. Here every file
    goes to a temp name in the same directory, is fsynced, then ``os.replace``d
    into place. Each file is therefore always the old or the new version.
    ``output.csv`` is renamed last: it is the metered ledger and the shrink
    guard counts it. A kill between two renames can leave ``input.csv`` one
    batch ahead of ``output.csv``; both are valid and the next store heals it.
    """
    subdir.mkdir(parents=True, exist_ok=True)
    for stale in subdir.glob(".tmp-*"):  # debris of a writer killed earlier
        stale.unlink(missing_ok=True)
    df_input, df_output = self.to_pandas(keep_references=True)
    tag = f".tmp-{os.getpid()}-"
    writers = [
        (_DOMAIN_JSON, lambda path: self._domain.store(path)),
        (_INPUT_CSV, df_input.to_csv),
        (_JOBS_CSV, self.jobs.to_csv),
        (_OUTPUT_CSV, df_output.to_csv),
    ]
    temps = []
    try:
        for name, write in writers:
            tmp = subdir / (tag + name)
            write(tmp)
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
            temps.append((tmp, subdir / name))
        for tmp, final in temps:
            os.replace(tmp, final)
    finally:
        for tmp, _ in temps:
            tmp.unlink(missing_ok=True)


def apply_f3dasm_compat() -> None:
    """Idempotently install the two ExperimentData behaviours adda needs."""
    from f3dasm import ExperimentData

    if getattr(ExperimentData, _APPLIED_FLAG, False):
        return

    def to_numpy(self):
        """Return ``(input_array, output_array)`` with metadata columns dropped.

        Underscore-prefixed columns are provenance bookkeeping, not measured
        values; keeping them (some are strings) would force an ``object`` dtype.
        """
        df_input, df_output = self.to_pandas(keep_references=False)
        df_input = df_input.loc[
            :, ~df_input.columns.astype(str).str.startswith("_")
        ]
        df_output = df_output.loc[
            :, ~df_output.columns.astype(str).str.startswith("_")
        ]
        return df_input.to_numpy(), df_output.to_numpy()

    _orig_store = ExperimentData.store

    def store(self, project_dir=None, copy_references=False):
        """``ExperimentData.store`` guarded against corrupting a PROTECTED store.

        Two monotonicity guards on a store marked with the sentinel: refuse a
        write that would SHRINK the row count, and refuse one that would reset a
        row the oracle already marked FINISHED. Both fail open on a read error so
        a genuine write is never blocked by a parse hiccup.
        """
        pdir = (
            Path(project_dir)
            if project_dir is not None
            else getattr(self, "_project_dir", None)
        )
        if pdir is not None and (pdir / PROTECTED_STORE_SENTINEL).exists():
            subdir = pdir / _STORE_SUBFOLDER

            # (1) Shrink guard. Count LOGICAL csv records, not physical lines:
            # an output value can contain embedded newlines (e.g. an array repr
            # in one quoted field), so a raw line count over-counts and would
            # falsely reject a valid superset write.
            existing_out = subdir / _OUTPUT_CSV
            if existing_out.exists():
                try:
                    import csv as _csv

                    with open(existing_out, newline="") as f:
                        existing_rows = max(
                            sum(1 for _ in _csv.reader(f)) - 1, 0)  # - header
                except OSError:
                    existing_rows = 0
                if len(self) < existing_rows:
                    raise RuntimeError(
                        "Refusing to overwrite the PROTECTED canonical store at "
                        f"{pdir}: it holds {existing_rows} rows but this store() "
                        f"would write only {len(self)}, destroying "
                        f"{existing_rows - len(self)} metered evaluations. The "
                        "canonical store is written ONLY via get_evaluator(); "
                        "store your own ExperimentData to a different project_dir."
                    )

            # (2) FINISHED is monotonic. Refuse resetting a FINISHED row to a
            # non-finished status (a worker calling .store() after gen.call()).
            existing_jobs = subdir / _JOBS_CSV
            if existing_jobs.exists():
                try:
                    import pandas as _pd

                    disk = _pd.read_csv(
                        existing_jobs, index_col=0).iloc[:, 0].astype(str)
                    mine = self.jobs.astype(str)
                    regressed = [
                        i for i, s in disk.items()
                        if s.upper() == "FINISHED"
                        and str(mine.get(i, "")).upper() != "FINISHED"
                    ]
                    if regressed:
                        raise RuntimeError(
                            "Refusing to overwrite the PROTECTED canonical store "
                            f"at {pdir}: this store() would reset {len(regressed)}"
                            " FINISHED evaluation(s) to a non-finished status "
                            "(typically a worker calling data.store() after "
                            "gen.call()). The canonical store is written ONLY via "
                            "get_evaluator(); store your own ExperimentData to a "
                            "different project_dir."
                        )
                except RuntimeError:
                    raise
                except Exception:  # noqa: BLE001 — fail open on a read error
                    pass
        if copy_references:
            return _orig_store(
                self, project_dir=project_dir, copy_references=copy_references
            )
        if project_dir is not None:
            self.set_project_dir(project_dir, in_place=True)
        return _atomic_store(self, self._project_dir / _STORE_SUBFOLDER)

    ExperimentData.to_numpy = to_numpy
    ExperimentData.store = store
    setattr(ExperimentData, _APPLIED_FLAG, True)
