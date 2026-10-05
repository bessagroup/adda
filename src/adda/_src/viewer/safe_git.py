"""A read-only git view for the viewer: history and per-commit file stats.

The viewer serves ``git log``, ``git show --stat`` and ``git diff --stat``
for a run's workspace repository (and, later, a study's history). Nothing here
can write, and nothing here can be steered by a request:

* argv is a fixed list built from constants plus a commit id that matches
  ``[0-9a-f]{7,40}`` (so it can never be an option), never a shell string;
* ``--git-dir``/``--work-tree`` are passed explicitly and the repository must
  have its own ``.git`` at the given path, so git never discovers a parent
  repository (the failure mode ``infra/workspace_vcs`` documents);
* the environment is rebuilt from scratch (no user/system config, no pager,
  no prompts, no optional index locks), with hooks disabled;
* every call has a timeout and its output is capped.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ..infra.workspace_vcs import _HERMETIC_CONFIG

__all__ = ["GitViewError", "commit_files", "diff_stat", "log", "path_history",
           "show_stat"]

_SHA = re.compile(r"[0-9a-f]{7,40}")
_TIMEOUT_S = 10
_MAX_BYTES = 2_000_000
_FIELD = "\x1f"
_RECORD = "\x1e"


class GitViewError(Exception):
    """The view could not be produced (no repository, bad id, git failed)."""


def _run(repo: Path, *argv: str) -> str:
    repo = Path(repo).resolve()
    git_dir = repo / ".git"
    if not git_dir.exists():
        raise GitViewError("not a git repository")
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    try:
        done = subprocess.run(
            ["git", f"--git-dir={git_dir}", f"--work-tree={repo}",
             *_HERMETIC_CONFIG, "-c", "core.quotepath=false", "--no-pager",
             *argv],
            cwd=str(repo), env=env, capture_output=True,
            timeout=_TIMEOUT_S, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitViewError(f"git unavailable: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise GitViewError(done.stderr.decode("utf-8", "replace").strip()[:300]
                           or f"git exited {done.returncode}")
    return done.stdout[:_MAX_BYTES].decode("utf-8", errors="replace")


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise GitViewError(f"not a commit id: {value!r}")
    return value


def log(repo: Path, limit: int = 200) -> list[dict[str, Any]]:
    """``git log``: newest first, ``{sha, author, date, subject}``."""
    out = _run(repo, "log", f"-n{max(1, int(limit))}",
               f"--format=%H{_FIELD}%an{_FIELD}%aI{_FIELD}%s{_RECORD}")
    rows = []
    for rec in out.split(_RECORD):
        parts = rec.strip("\n").split(_FIELD)
        if len(parts) == 4:
            rows.append({"sha": parts[0], "author": parts[1],
                         "date": parts[2], "subject": parts[3]})
    return rows


def _parse_numstat(text: str) -> list[dict[str, Any]]:
    files = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        ins, dele, path = parts
        files.append({"path": path,
                      "insertions": int(ins) if ins.isdigit() else None,
                      "deletions": int(dele) if dele.isdigit() else None})
    return files


def commit_files(repo: Path) -> dict[str, list[dict[str, Any]]]:
    """``{sha: [{path, insertions, deletions}]}`` for every commit, from one
    ``git log --numstat`` call. Insertions/deletions are None for binaries."""
    out = _run(repo, "log", "--numstat", f"--format={_RECORD}%H")
    files: dict[str, list[dict[str, Any]]] = {}
    for chunk in out.split(_RECORD)[1:]:
        sha, _, rest = chunk.partition("\n")
        files[sha.strip()] = _parse_numstat(rest)
    return files


def show_stat(repo: Path, sha: str) -> str:
    """``git show --stat`` for one commit, as git prints it."""
    return _run(repo, "show", "--stat", "--format=fuller", _sha(sha))


def diff_stat(repo: Path, older: str, newer: str) -> str:
    """``git diff --stat <older> <newer>``, as git prints it."""
    return _run(repo, "diff", "--stat", _sha(older), _sha(newer))


def path_history(repo: Path, relpath: str, limit: int = 200) -> list[dict[str, Any]]:
    """Commits touching ``relpath`` (newest first), each with its changed
    files: ``{sha, author, date, subject, files[{path, insertions,
    deletions}]}``. ``relpath`` is chosen by the server (never a request
    value) and goes after ``--`` so it can only be a pathspec."""
    out = _run(repo, "log", f"-n{max(1, int(limit))}", "--numstat",
               f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%aI{_FIELD}%s",
               "--", relpath)
    rows = []
    for chunk in out.split(_RECORD)[1:]:
        head, _, rest = chunk.partition("\n")
        parts = head.split(_FIELD)
        if len(parts) == 4:
            rows.append({"sha": parts[0], "author": parts[1],
                         "date": parts[2], "subject": parts[3],
                         "files": _parse_numstat(rest)})
    return rows
