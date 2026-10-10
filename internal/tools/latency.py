"""Where a run's wall time goes, from its ``debug/`` records.

    python internal/tools/latency.py PATH [PATH ...] [--json]

``PATH`` is a ``debug/`` directory, a run directory, or any parent of runs. The
tool reads only structured records (never greps):

* ``transcripts/<role>/turn_NNN.jsonl`` and ``transcripts/D###.jsonl``: the
  per-turn stream of ``system/status``, ``stream_evt`` (``message_start`` /
  ``message_stop``), ``assistant`` (its ``tools``) and ``tool_result`` records.
* ``transcripts/critic/call_NNN.jsonl``: one critic review.
* ``run_status.json`` (``wall_s``) and ``run_started_at``: the run window.

Each record carries a timestamp to the second, so one call is good to about a
second; a run's total is not biased by it.

Intervals, per transcript:

* model: from the ``requesting`` status to the last content record of the call
  (the first-token wait is inside it). ``message_stop`` is not used: a call
  that ends in a blocking tool gets its ``message_stop`` only when the tool
  returns.
* tool: from the assistant record that carries the call to the ``tool_result``
  that answers it, by the tool's kind (below).
* critic: the whole span of a critic transcript.

Tool kinds, which sum into four groups:

    model          generation by any node except the critic
    tool           shell (Bash, Read, Edit, ...: the simulator runs here) and
                   lit (SearchPapers, CorpusAdd, ...: network and embedding)
    orchestration  critic, wait (Wait; Delegate, which returns when the child
                   reports; Done, which holds the reproduction gate) and adda (every other adda tool)
    idle           no record covers the second

Nodes run at the same time, so the wall clock is split by a sweep: at each
instant the highest-ranked kind that is running takes the second (model, shell,
lit, critic, adda, wait, in that order). A ``Wait`` that child nodes fill is
counted as their work; what is left of it is wait. ``node_s`` is the plain sum
of every model, shell, lit and adda interval (no overlap removed), so
``par = node_s / wall`` is the mean number of intervals running; the second
table gives node-seconds by kind.

Not covered: a time cost that leaves no record, for example the verdict
validator's call (it falls in the tool call that triggered it, or in idle).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ADDA_PREFIX = "mcp__f3dasm_agent_tools__"
LIT_TOOLS = {"SearchPapers", "CorpusAdd", "PaperDetails", "ConsultLiterature"}
WAIT_TOOLS = {"Wait", "Done", "Delegate"}

KINDS = ["model", "shell", "lit", "critic", "adda", "wait", "idle"]
RANK = {k: i for i, k in enumerate(KINDS)}
GROUPS = {
    "model": ["model"],
    "tool": ["shell", "lit"],
    "orchestration": ["critic", "adda", "wait"],
    "idle": ["idle"],
}


def _t(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def tool_kind(name: str | None) -> str:
    if name is None or not name.startswith(ADDA_PREFIX):
        return "shell"
    short = name[len(ADDA_PREFIX):]
    if short in LIT_TOOLS:
        return "lit"
    if short in WAIT_TOOLS:
        return "wait"
    return "adda"


def transcript_intervals(rows: list[dict]) -> list[tuple[float, float, str]]:
    """Model and tool intervals of one worker transcript.

    A model call runs from its ``requesting`` status to the last content record
    it wrote (``assistant``, ``partial`` or a ``content_block_*`` event). The
    ``message_stop`` record is NOT the end: when the call ends in a blocking tool
    (``Wait``, ``Done``, a long ``Bash``) the CLI logs ``message_stop`` only after
    the tool returns, so it would count the tool's time as the model's.

    A tool starts when the assistant record that carries its call is written
    and ends at the ``tool_result`` that answers its id; the CLI runs tools
    while the model is still streaming, so a tool interval may overlap the
    model interval of the same node.
    """
    out: list[tuple[float, float, str]] = []
    started: dict[str, tuple[float, str]] = {}
    requested: float | None = None
    begun_at: float | None = None
    last_content: float | None = None
    stopped: float | None = None

    def close() -> None:
        nonlocal begun_at, last_content, stopped
        if begun_at is not None:
            end = last_content if last_content is not None else stopped
            if end is not None:
                out.append((begun_at, end, "model"))
        begun_at = last_content = stopped = None

    for r in rows:
        kind = r.get("type")
        if kind == "system" and r.get("subtype") == "status" and (
                (r.get("data") or {}).get("status") == "requesting"):
            close()
            requested = _t(r["ts"])
        elif kind == "stream_evt":
            evt = r.get("evt") or ""
            if evt == "message_start":
                close()
                begun_at = requested if requested is not None else _t(r["ts"])
                requested = None
            elif evt == "message_stop":
                stopped = _t(r["ts"])
            elif evt.startswith("content_block") and begun_at is not None:
                last_content = _t(r["ts"])
        elif kind in ("assistant", "partial") and begun_at is not None:
            last_content = _t(r["ts"])
        if kind == "assistant":
            for tool in r.get("tools") or []:
                started[tool["tool_use_id"]] = (
                    _t(r["ts"]), tool_kind(tool["name"]))
        elif kind == "tool_result":
            for x in r.get("results") or []:
                begun = started.pop(x.get("tool_use_id"), None)
                if begun:
                    out.append((begun[0], _t(r["ts"]), begun[1]))
    close()
    return out


def critic_interval(rows: list[dict]) -> list[tuple[float, float, str]]:
    return [(_t(rows[0]["ts"]), _t(rows[-1]["ts"]), "critic")] if rows else []


def sweep(intervals: list[tuple[float, float, str]], start: float,
          end: float) -> dict[str, float]:
    """Seconds of ``[start, end]`` per kind, the highest rank winning."""
    cuts = {start, end}
    clipped = []
    for a, b, k in intervals:
        a, b = max(a, start), min(b, end)
        if b > a:
            clipped.append((a, b, k))
            cuts.update((a, b))
    edges = sorted(cuts)
    secs = dict.fromkeys(KINDS, 0.0)
    for lo, hi in zip(edges, edges[1:], strict=False):
        live = [k for a, b, k in clipped if a <= lo and b >= hi]
        secs[min(live, key=RANK.__getitem__) if live else "idle"] += hi - lo
    return secs


def analyse(debug: Path) -> dict:
    status = json.loads((debug / "run_status.json").read_text())
    start = float((debug / "run_started_at").read_text().strip())
    wall = float(status["wall_s"])
    end = start + wall
    intervals: list[tuple[float, float, str]] = []
    transcripts = sorted((debug / "transcripts").rglob("*.jsonl"))
    for path in transcripts:
        rows = _rows(path)
        if path.parent.name == "critic":
            intervals += critic_interval(rows)
        else:
            intervals += transcript_intervals(rows)
    secs = sweep(intervals, start, end)
    node_kinds = dict.fromkeys(KINDS[:-1], 0.0)
    for a, b, k in intervals:
        node_kinds[k] += max(min(b, end) - max(a, start), 0.0)
    node_s = sum(v for k, v in node_kinds.items()
                 if k not in ("critic", "wait"))
    groups = {g: sum(secs[k] for k in ks) for g, ks in GROUPS.items()}
    return {
        "run": (debug.parents[2].name if debug.parent.parent.name == "runs"
                else debug.parent.parent.name) + "/" + debug.parent.name,
        "status": status.get("status"), "wall_s": wall,
        "transcripts": len(transcripts), "kinds": secs, "groups": groups,
        "node_kinds": node_kinds, "node_s": node_s, "par": node_s / wall if wall else 0.0,
    }


def find_debug_dirs(paths: list[Path]) -> list[Path]:
    found: set[Path] = set()
    for p in paths:
        if (p / "run_status.json").exists():
            found.add(p)
        elif (p / "debug" / "run_status.json").exists():
            found.add(p / "debug")
        else:
            found.update(f.parent for f in p.rglob("run_status.json")
                         if f.parent.name == "debug")
    return sorted(found)


def table(results: list[dict]) -> str:
    head = ("| run | status | wall s | model % | tool % (shell/lit) | "
            "orch % (critic/adda/wait) | idle % | par |")
    lines = [head, "|" + "---|" * 8]
    for r in results:
        w, k, g = r["wall_s"], r["kinds"], r["groups"]

        def pc(x, w=w):
            return f"{100 * x / w:.0f}"
        lines.append(
            f"| {r['run']} | {r['status']} | {w:.0f} | {pc(g['model'])} | "
            f"{pc(g['tool'])} ({pc(k['shell'])}/{pc(k['lit'])}) | "
            f"{pc(g['orchestration'])} "
            f"({pc(k['critic'])}/{pc(k['adda'])}/{pc(k['wait'])}) | "
            f"{pc(g['idle'])} | {r['par']:.1f} |")
    if results:
        tot = sum(r["wall_s"] for r in results)
        agg = defaultdict(float)
        for r in results:
            for kk, v in r["kinds"].items():
                agg[kk] += v
        lines.append(
            f"| **all ({len(results)} runs, {tot:.0f} s)** | | | "
            f"{100 * agg['model'] / tot:.0f} | "
            f"{100 * (agg['shell'] + agg['lit']) / tot:.0f} "
            f"({100 * agg['shell'] / tot:.0f}/{100 * agg['lit'] / tot:.0f}) | "
            f"{100 * (agg['critic'] + agg['adda'] + agg['wait']) / tot:.0f} "
            f"({100 * agg['critic'] / tot:.0f}/{100 * agg['adda'] / tot:.0f}/"
            f"{100 * agg['wait'] / tot:.0f}) | "
            f"{100 * agg['idle'] / tot:.0f} | |")
    return "\n".join(lines)


def node_table(results: list[dict]) -> str:
    """Node-seconds by kind: every interval counted in full, overlap kept."""
    cols = ["model", "shell", "lit", "adda", "wait", "critic"]
    lines = ["| run | " + " | ".join(f"{c} s" for c in cols) + " |",
             "|" + "---|" * (len(cols) + 1)]
    tot = dict.fromkeys(cols, 0.0)
    for r in results:
        lines.append(f"| {r['run']} | " + " | ".join(
            f"{r['node_kinds'][c]:.0f}" for c in cols) + " |")
        for c in cols:
            tot[c] += r["node_kinds"][c]
    lines.append("| **all** | " + " | ".join(
        f"{tot[c]:.0f}" for c in cols) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    dirs = find_debug_dirs(args.paths)
    if not dirs:
        print("no run_status.json found", file=sys.stderr)
        return 1
    results = [analyse(d) for d in dirs]
    if args.json:
        print(json.dumps(results, indent=1))
    else:
        print(table(results) + "\n\n" + node_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
