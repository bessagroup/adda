"""A campaign subprocess's notices to the agent that launched it.

An agent runs its campaign through a shell tool, so the oracle wrapper lives
in a child process. Its stdout is the tool result, and the CLI keeps only the
first 2 KB of a large result, so a warning printed there can be lost. This
module is the second path: the wrapper appends the notice to a per-delegation
file under ``<run>/debug/pending_notices/``, and the backend's post-tool hook
drains it after the agent's next shell/write call and hands it to the model as
extra context, outside the tool result. stdout keeps its copy.

Only std-lib imports: the wrapper imports this inside the campaign process.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _file(debug_dir: Path | str, delegation_id: str) -> Path:
    safe = "".join(c for c in str(delegation_id) if c.isalnum() or c in "-_")
    return Path(debug_dir) / "pending_notices" / f"{safe or 'entry'}.jsonl"


def post(debug_dir: Path | str, delegation_id: str, text: str) -> None:
    """Queue *text* for the delegation. Best-effort: never raises."""
    try:
        path = _file(debug_dir, delegation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
               "text": text}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass


def drain(debug_dir: Path | str | None, delegation_id: str | None) -> list[str]:
    """Return and clear the delegation's queued notices, oldest first."""
    if not debug_dir or not delegation_id:
        return []
    try:
        path = _file(debug_dir, delegation_id)
        if not path.exists():
            return []
        taken = path.with_name(f"{path.stem}.{os.getpid()}.drained")
        os.replace(path, taken)  # a writer after this appends to a fresh file
        texts = []
        for line in taken.read_text(encoding="utf-8").splitlines():
            try:
                texts.append(str(json.loads(line)["text"]))
            except (ValueError, KeyError):
                continue
        taken.unlink(missing_ok=True)
        return texts
    except Exception:  # noqa: BLE001
        return []


def post_tool_context(nudge, tool_name: str, tool_input: dict,
                      debug_dir: Path | str | None,
                      delegation_id: str | None) -> str | None:
    """What a backend hands the model after a tool call, besides the result:
    the raw-oracle nudge (if any) and the queued campaign notices. Both
    backends call this, so their agents see the same text."""
    parts = []
    msg = nudge.check(tool_name, tool_input) if nudge is not None else None
    if msg:
        parts.append(msg)
    queued = drain(debug_dir, delegation_id)
    if queued:
        from ..nodes.notices import wrap_notice
        parts.append(wrap_notice("\n".join(queued), trailing=""))
    return "\n\n".join(parts) or None
