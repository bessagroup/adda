"""Editing a study's problem statement and config through git (spec 14 §3.1/3.2).

The zero-shot launcher copies only the *committed* study tree, so an edit that
is saved but never committed silently never reaches the agents. The editor
therefore has one write: **commit this text with this message**. Nothing here
leaves a dirty file behind.

What makes the write safe:

* only the two named files can be edited; the path is never a request value;
* the commit takes ``--only -- <file>``, so nothing else (staged or not) can
  ride along, and it is refused outright when other files in the study have
  uncommitted changes, because the operator would then be committing in a
  tree they have not looked at;
* ``base`` (the committed blob id the editor opened on) must still be the
  committed one, so a commit made meanwhile elsewhere is not overwritten;
* a config is validated first: it must parse, ``runtime:`` may only hold
  known knobs, and ``budget`` must be parseable;
* the commit is authored as the user's own git identity; hooks and signing
  are disabled and prompts off, as in ``safe_git``.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from . import safe_git

__all__ = ["FILES", "StudyEditError", "commit", "committed_summary", "diff_rows",
           "read", "repo_of", "validate_config"]

FILES = {"problem_statement": "PROBLEM_STATEMENT.md", "config": "config.yaml"}
_IGNORED_TOP = ("runs/",)
_TIMEOUT_S = 30


class StudyEditError(Exception):
    """A refusal with a message the operator can act on; ``status`` is the
    HTTP status the route returns."""

    def __init__(self, message: str, status: int = 409, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def repo_of(study_dir: Path | str) -> tuple[Path, str]:
    """``(repo root, study path relative to it)``: the nearest ancestor with a
    ``.git``. Raises StudyEditError when the study is under no repository."""
    study = Path(study_dir).resolve()
    root = next((p for p in (study, *study.parents) if (p / ".git").exists()), None)
    if root is None:
        raise StudyEditError(
            "this study is not under a git repository, so there is nothing to "
            "commit to", status=409)
    return root, study.relative_to(root).as_posix() or "."


def _rel(name: str, base: str) -> tuple[str, str]:
    if name not in FILES:
        raise StudyEditError(f"unknown file {name!r}; one of {sorted(FILES)}", 404)
    fname = FILES[name]
    return fname, fname if base == "." else f"{base}/{fname}"


def _blob_id(root: Path, relpath: str) -> str | None:
    try:
        out = safe_git._run(root, "rev-parse", f"HEAD:{relpath}").strip()
    except safe_git.GitViewError:
        return None
    return out or None


def read(study_dir: Path | str, name: str) -> dict[str, Any]:
    """``{name, file, committed, working, base, dirty}``: the committed text
    (None when the file is not in HEAD), what is on disk, the committed blob
    id the editor must send back as ``base``, and whether disk differs."""
    root, base = repo_of(study_dir)
    fname, rel = _rel(name, base)
    committed = safe_git.show_blob(root, "HEAD", rel)
    try:
        working = (Path(study_dir) / fname).read_text(encoding="utf-8")
    except OSError:
        working = None
    return {"name": name, "file": fname, "committed": committed,
            "working": working, "base": _blob_id(root, rel),
            "dirty": working != committed}


def committed_summary(study_dir: Path | str) -> dict[str, Any]:
    """What a run would be configured with, read from the COMMITTED config:
    ``{model, budget_s, uncommitted[]}`` (``uncommitted`` names the edited
    files not yet in HEAD). Fields are None when they cannot be read; an
    unreadable repository gives None for all of them."""
    from ..runtime.run_setup import _parse_budget_str
    out: dict[str, Any] = {"model": None, "budget_s": None, "uncommitted": []}
    try:
        for name in FILES:
            if read(study_dir, name)["dirty"]:
                out["uncommitted"].append(FILES[name])
        text = read(study_dir, "config")["committed"]
        cfg = yaml.safe_load(text) if text else None
        if isinstance(cfg, dict):
            out["model"] = cfg.get("model")
            try:
                out["budget_s"] = _parse_budget_str(cfg.get("budget"))
            except (TypeError, ValueError):
                pass
    except (StudyEditError, yaml.YAMLError):
        pass
    return out


def diff_rows(old: str | None, new: str) -> list[dict[str, Any]]:
    """Side-by-side rows ``{op, a, b, na, nb}`` for a diff, with unchanged
    runs of more than six lines folded to a single ``skip`` row."""
    a = (old or "").splitlines()
    b = new.splitlines()
    rows: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            span = list(range(i2 - i1))
            if len(span) > 6:
                for k in span[:3]:
                    rows.append({"op": "same", "a": a[i1 + k], "b": b[j1 + k],
                                 "na": i1 + k + 1, "nb": j1 + k + 1})
                rows.append({"op": "skip", "n": len(span) - 6})
                span = span[-3:]
            for k in span:
                rows.append({"op": "same", "a": a[i1 + k], "b": b[j1 + k],
                             "na": i1 + k + 1, "nb": j1 + k + 1})
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            has_a, has_b = k < i2 - i1, k < j2 - j1
            rows.append({"op": {"replace": "change", "delete": "del",
                                "insert": "add"}[tag],
                         "a": a[i1 + k] if has_a else None,
                         "b": b[j1 + k] if has_b else None,
                         "na": i1 + k + 1 if has_a else None,
                         "nb": j1 + k + 1 if has_b else None})
    return rows


def validate_config(text: str) -> dict[str, Any]:
    """``{ok, errors[], warnings[]}``. Errors block a commit."""
    from ..runtime.run_setup import _parse_budget_str
    from ..runtime.settings import KNOWN_KEYS

    errors: list[str] = []
    warnings: list[str] = []
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {"ok": False, "errors": [f"not valid YAML: {exc}"], "warnings": []}
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        return {"ok": False, "errors": ["config.yaml must be a mapping of settings"],
                "warnings": []}
    runtime = cfg.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, dict):
            errors.append("`runtime:` must be a mapping of knob: value")
        else:
            for key in sorted(set(runtime) - KNOWN_KEYS):
                close = difflib.get_close_matches(str(key), KNOWN_KEYS, n=1)
                errors.append(
                    f"runtime.{key} is not a known knob"
                    + (f" (did you mean {close[0]}?)" if close else ""))
    from ..runtime.node_tools import validate_nodes_block
    errors.extend(validate_nodes_block(cfg.get("nodes")))
    if "budget" in cfg:
        try:
            if _parse_budget_str(cfg["budget"]) is None:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"budget {cfg['budget']!r} is not seconds or HH:MM:SS")
    else:
        warnings.append("no `budget:`; the watchdog needs one to start a run")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _git(root: Path, *argv: str) -> str:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") or k in ("GIT_CONFIG_GLOBAL",)}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        done = subprocess.run(
            ["git", f"--git-dir={root / '.git'}", f"--work-tree={root}",
             "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "core.quotepath=false", "--no-pager", *argv],
            capture_output=True, timeout=_TIMEOUT_S, env=env, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StudyEditError(f"git unavailable: {type(exc).__name__}", 502) from exc
    if done.returncode != 0:
        raise StudyEditError(
            done.stderr.decode("utf-8", "replace").strip()[:400]
            or f"git exited {done.returncode}", 502)
    return done.stdout.decode("utf-8", "replace")


def commit(study_dir: Path | str, name: str, text: str, message: str,
           base: str | None, audit_name: str = "viewer_actions.jsonl") -> dict[str, Any]:
    """Write ``text`` to the named file and commit just that file. Returns
    ``{sha, subject, diff}``. Leaves the file as it was if anything fails."""
    message = (message or "").strip()
    if not message:
        raise StudyEditError("a commit message is required", 400)
    root, srel = repo_of(study_dir)
    fname, rel = _rel(name, srel)
    if name == "config":
        v = validate_config(text)
        if not v["ok"]:
            raise StudyEditError("config.yaml is not valid: " + "; ".join(v["errors"]),
                                 422, errors=v["errors"])
    current = read(study_dir, name)
    if current["base"] != base:
        raise StudyEditError(
            "the committed file changed since you opened it; reload the editor "
            "to see the new version", 409, base=current["base"])
    if text == current["committed"]:
        raise StudyEditError("nothing to commit: the text equals the committed file", 409)
    skip = {rel, f"{srel}/{audit_name}" if srel != "." else audit_name}
    others = [p for p in safe_git.dirty_paths(root, srel)
              if p not in skip
              and not any(p.startswith((srel + "/" if srel != "." else "") + t)
                          for t in _IGNORED_TOP)]
    if others:
        raise StudyEditError(
            "other files in this study have uncommitted changes; commit or "
            "stash them first: " + ", ".join(others[:8]), 409, unrelated=others)
    path = Path(study_dir) / fname
    before = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(text, encoding="utf-8")
    try:
        _git(root, "add", "--", rel)
        _git(root, "commit", "--only", "-m", message, "--", rel)
    except StudyEditError:
        try:
            _git(root, "reset", "-q", "--", rel)
        except StudyEditError:
            pass
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")
        raise
    sha = _git(root, "rev-parse", "HEAD").strip()
    return {"sha": sha, "subject": message.splitlines()[0], "file": fname}


_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def sibling_studies(study_dir: Path | str) -> list[str]:
    """Names of the studies next to this one (they hold a problem statement or
    a config), the candidates for a template."""
    parent = Path(study_dir).resolve().parent
    return sorted(
        p.name for p in parent.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_"))
        and any((p / f).is_file() for f in FILES.values()))


def create_study(study_dir: Path | str, name: str, problem_statement: str,
                 config: str, message: str) -> dict[str, Any]:
    """``mkdir <studies>/<name>`` beside this study, write the two files and
    commit exactly them. Removes what it made if the commit fails."""
    message = (message or "").strip()
    if not message:
        raise StudyEditError("a commit message is required", 400)
    if not _NAME_RE.fullmatch(name or ""):
        raise StudyEditError(
            "a study name is letters, digits, '_' and '-' (it starts with a "
            "letter or digit, at most 64 characters)", 400)
    v = validate_config(config)
    if not v["ok"]:
        raise StudyEditError("config.yaml is not valid: " + "; ".join(v["errors"]),
                             422, errors=v["errors"])
    if not problem_statement.strip():
        raise StudyEditError("the problem statement is empty", 400)
    new = Path(study_dir).resolve().parent / name
    if new.exists():
        raise StudyEditError(f"a study named {name!r} already exists", 409)
    root, _ = repo_of(new.parent)
    rels = [new.relative_to(root).as_posix() + "/" + f for f in FILES.values()]
    new.mkdir()
    try:
        (new / FILES["problem_statement"]).write_text(problem_statement, encoding="utf-8")
        (new / FILES["config"]).write_text(config, encoding="utf-8")
        _git(root, "add", "--", *rels)
        _git(root, "commit", "--only", "-m", message, "--", *rels)
    except (StudyEditError, OSError):
        try:
            _git(root, "reset", "-q", "--", *rels)
        except StudyEditError:
            pass
        shutil.rmtree(new, ignore_errors=True)
        raise
    return {"sha": _git(root, "rev-parse", "HEAD").strip(), "name": name,
            "path": str(new), "open": f"python -m adda.viewer {new}"}
