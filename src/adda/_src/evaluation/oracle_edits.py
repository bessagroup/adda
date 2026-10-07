"""Tell the run when the registered oracle's source changed under it.

Delegations edit the oracle file mid-run. Rows keep the revision that made
them (``_oracle_rev``), so nothing is lost, but a delegation that evaluates
after someone else's edit is measuring a different oracle than the one it
read about. This module notices that and says so. It never blocks.

The run records the last revision it saw per oracle in
``debug/oracle_revisions.json``. An evaluation that runs under a different
revision is an edit. The edit is reported once per new revision unless the
editor is known and is the one expected to edit (the oracle's owner, or, for
an oracle nobody registered, the delegation that is evaluating). The editor
is attributed from the delegation log: the delegations running when the
oracle's files last changed. One candidate names the editor; any other count
is reported as unknown, with the candidates listed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

_SLACK = timedelta(seconds=2)


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _active_delegations(log_path: Path, at: datetime) -> list[str]:
    """Ids of delegations whose run window contains *at*, last row per id."""
    rows: dict[str, dict] = {}
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("id"):
                rows[row["id"]] = row
    except OSError:
        return []
    active = []
    for did, row in rows.items():
        start = _parse(row.get("session_started_at") or row.get("started_at"))
        end = _parse(row.get("completed_at"))
        if start is None or start - _SLACK > at:
            continue
        if end is not None and end + _SLACK < at:
            continue
        active.append(did)
    return sorted(active)


def _editor_clause(candidates: list[str]) -> tuple[str | None, str]:
    if len(candidates) == 1:
        return candidates[0], f"Edited by {candidates[0]}."
    if candidates:
        return None, ("Editor unknown (delegations running when the file last "
                      f"changed: {', '.join(candidates)}).")
    return None, ("Editor unknown (no delegation was running when the file "
                  "last changed).")


def check_oracle_edit(cfg: dict, study_dir: Path, namespace: str | None,
                      rev: str, delegation_id: str) -> None:
    """Record *rev* as the run's current oracle revision and report an edit.

    Best-effort: a notice must never break the evaluation path.
    """
    try:
        _check(cfg, Path(study_dir), namespace or "default", rev, delegation_id)
    except Exception:  # noqa: BLE001
        pass


def _check(cfg: dict, study_dir: Path, key: str, rev: str,
           delegation_id: str) -> None:
    from ..infra import pending_notices
    from .oracle_resolution import _oracle_source_files

    debug = Path(cfg["store_dir"]).parent / "debug"
    if not debug.exists():
        return
    record_path = debug / "oracle_revisions.json"
    with FileLock(str(debug / "oracle_revisions.lock")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        old = (record.get(key) or {}).get("rev")
        record[key] = {"rev": rev}
        tmp = record_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(record_path)
    if old is None or old == rev:
        return

    owner = cfg.get("evaluator_owner")
    mtimes = []
    for p in _oracle_source_files(cfg, study_dir):
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:
            continue
    candidates = (
        _active_delegations(
            debug / "delegation_log.jsonl",
            datetime.fromtimestamp(max(mtimes), tz=timezone.utc))
        if mtimes else [])
    editor, clause = _editor_clause(candidates)

    expected = owner if owner else delegation_id
    if editor is not None and editor == expected:
        return

    owner_txt = f"owner {owner}" if owner else "no registered owner"
    text = (
        f"[ORACLE EDITED — {delegation_id}] the registered oracle ({owner_txt}) "
        f"changed from revision {old} to {rev}; rows from {old} stay in the "
        f"store as a superseded revision. {clause}")
    print(text, flush=True)
    pending_notices.post(debug, delegation_id, text)
    pending_notices.post(debug, "entry", text)
    diag = debug / "diagnostics.jsonl"
    rec = {
        "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "node": delegation_id,
        "error_type": "ORACLE_EDITED",
        "message": text,
        "detail": {"owner": owner, "editor": editor,
                   "candidates": candidates, "old_revision": old,
                   "new_revision": rev, "namespace": key},
    }
    with diag.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
