"""Files a run's page offers to copy off the machine (spec 14, Phase 5.11).

Every answer is a file that already exists on disk, or a zip of files that do:
nothing is generated, so a download is exactly what a terminal user would
``cp``/``zip`` themselves.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from . import readers

KINDS = ("notebook", "debug", "store")
_STORE_FILES = ("input.csv", "output.csv", "jobs.csv", "domain.json")


def _zip(pairs: list[tuple[str, Path]]) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        prefix="adda_dl_", suffix=".zip", delete=False
    )
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, f in pairs:
            try:
                z.write(f, arcname=arc)
            except OSError:
                continue
    return Path(tmp.name)


def prepare(
    study_dir: Path, run_id: str, kind: str
) -> tuple[Path, str, bool] | None:
    """``(file, download name, delete_after)`` for *kind*, or ``None`` when the
    run has nothing of that kind."""
    run_dir = Path(study_dir) / "runs" / run_id
    if kind == "notebook":
        nb = readers.notebook_path(study_dir, run_id)
        return (nb, f"{run_id}_pipeline.ipynb", False) if nb else None
    if kind == "debug":
        debug = run_dir / "debug"
        pairs = [
            (f"debug/{p.relative_to(debug)}", p)
            for p in sorted(debug.rglob("*"))
            if p.is_file()
        ]
        return (_zip(pairs), f"{run_id}_debug.zip", True) if pairs else None
    if kind == "store":
        _cfg, _base, found = readers._oracle_stores(run_dir)
        pairs = []
        for ns, root in found:
            data = root / readers._DATA_DIR
            pairs += [
                (f"{ns or 'store'}/{n}", data / n)
                for n in _STORE_FILES
                if (data / n).is_file()
            ]
        return (_zip(pairs), f"{run_id}_store.zip", True) if pairs else None
    return None
