"""A generated index of a run's delegations, handed to the critic.

A reviewer gets pointers to the evidence, not only the conclusions. Built
from the run's own records (``delegation_log.jsonl`` and the per-run
workspace git history), never hand-written. Each delegation's report is
materialised as a file the critic can Read directly, since the log keeps it
only inside one JSONL row.
"""

from __future__ import annotations

import json
from pathlib import Path

from .workspace_vcs import files_by_commit

__all__ = ["evidence_index_block"]

_INLINE_CHARS = 6000
_MAX_FILES_SHOWN = 8


def _rows(log_path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if "patch" in row:
            continue  # a patch carries no status; it must not replace the row
        rows[row["id"]] = row  # last status row per id is its final state
    return rows


def _intent(task: str) -> str:
    first = next((ln.strip() for ln in (task or "").splitlines() if ln.strip()), "")
    return first if len(first) <= 140 else first[:137] + "..."


def evidence_index_block(debug_dir: Path | None) -> str:
    """The ``<evidence_index>`` block; empty when the run has no delegations."""
    if debug_dir is None:
        return ""
    debug_dir = Path(debug_dir)
    rows = _rows(debug_dir / "delegation_log.jsonl")
    if not rows:
        return ""
    workspace = debug_dir / "delegations"
    touched = files_by_commit(workspace)
    reports = debug_dir / "delegation_reports"

    entries: list[str] = []
    for did in sorted(rows):
        row = rows[did]
        report = ""
        text = row.get("deliverable") or ""
        if text:
            try:
                reports.mkdir(parents=True, exist_ok=True)
                path = reports / f"{did}.md"
                path.write_text(text, encoding="utf-8")
                report = str(path)
            except OSError:
                report = ""
        files = touched.get(row.get("workspace_sha") or "", [])
        shown = ", ".join(str(workspace / f) for f in files[:_MAX_FILES_SHOWN])
        if len(files) > _MAX_FILES_SHOWN:
            shown += f", ... (+{len(files) - _MAX_FILES_SHOWN} more)"
        entries.append(
            f"{did} | {row.get('to_node', '?')} | {row.get('status', '?')} | "
            f"{_intent(row.get('task', ''))}\n"
            f"  report: {report or '(none)'}\n"
            f"  touched: {shown or '(no files)'}")

    full = "\n".join(entries)
    full_path = debug_dir / "evidence_index.md"
    try:
        full_path.write_text(full + "\n", encoding="utf-8")
    except OSError:
        pass
    body = full
    if len(full) > _INLINE_CHARS:
        body = (full[:_INLINE_CHARS].rsplit("\n", 1)[0]
                + f"\n[... index truncated; full index: {full_path} ...]")
    return (
        "<evidence_index>\n"
        "Every delegation, its report file and the files it touched -- Read "
        "these directly rather than relying on summaries.\n"
        + body + "\n</evidence_index>\n\n")
