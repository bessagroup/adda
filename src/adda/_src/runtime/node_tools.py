"""Per-node tool sets: the ``Default`` token and the config.yaml ``nodes:`` block.

A node's tools come from its Agent class. ``config.yaml`` may replace that set
for one node, and may also set the node's ``model``, ``backend`` and
``base_url``::

    nodes:
      implementer:
        tools: [Default, ReadNote]
        model: claude-haiku-4-5

The list REPLACES the class set (no merge). ``Default`` means the backend's own
full built-in set; on the Claude backend that is the CLI default set with none
of adda's usual blocks (see ``ClaudeAdapter``). A node that does not name
``Default`` keeps today's behaviour exactly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..backends.base import DEFAULT_TOOLS as DEFAULT

#: Keys a ``nodes.<name>`` entry may hold.
NODE_KEYS = frozenset({"tools", "model", "backend", "base_url"})

#: Built-ins that were reviewed (what each does, whether it can leave adda's
#: paths). A built-in outside this set in the first init record is reported,
#: never blocked.
REVIEWED_BUILTINS = frozenset({
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "Glob", "Grep",
    "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent", "TaskOutput",
    "TaskStop", "TodoWrite", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion",
    "Skill", "SlashCommand", "ListMcpResourcesTool", "ReadMcpResourceTool",
})

#: Closures every node receives whether or not its Agent declares them. A
#: ``nodes.<name>.tools`` list withholds those it does not name.
DROPPABLE_CLOSURES = frozenset({
    "ConsultHandbook", "ConsultLiterature", "ReportEvals", "RecallHistory", "Write",
})

#: What a Default node can reach that adda normally gates.
BYPASS_NOTICE = (
    "node {node} holds Default: it receives the CLI's full built-in tool set. "
    "It can bypass Delegate (Task/Agent), the literature rate limiter and cache "
    "(WebSearch/WebFetch), the reproduction gate (NotebookEdit) and FollowUp "
    "(AskUserQuestion). Informational; nothing is blocked."
)
DIFF_NOTICE = ("node {node}: config.yaml tools differ from the class declaration: "
               "added {added}, removed {removed}, always-on closures withheld {withheld}")


def withheld_closures(agent: Any) -> frozenset[str]:
    """Always-on closures a config ``tools`` list leaves out for this agent.

    Empty unless the list came from config.yaml. ``Default`` names the
    backend's built-in set, which includes ``Write``; the sandboxed ``Write``
    then stays, so a Default node never falls back to an unrestricted one.
    """
    if agent is None or not getattr(agent, "_tools_pinned", False):
        return frozenset()
    held = frozenset(agent.tools)
    out = DROPPABLE_CLOSURES - held
    return out - {"Write"} if DEFAULT in held else out


def validate_nodes_block(nodes: Any) -> list[str]:
    """Structural errors in a ``nodes:`` block; ``[]`` when well-formed."""
    if nodes is None:
        return []
    if not isinstance(nodes, dict):
        return ["`nodes:` must be a mapping of node name to settings"]
    errors: list[str] = []
    for name, entry in nodes.items():
        if not isinstance(entry, dict):
            errors.append(f"nodes.{name} must be a mapping")
            continue
        for key in sorted(set(entry) - NODE_KEYS):
            errors.append(f"nodes.{name}.{key} is not a known setting "
                          f"(known: {sorted(NODE_KEYS)})")
        tools = entry.get("tools")
        if "tools" in entry and (
                not isinstance(tools, list)
                or not all(isinstance(t, str) and t for t in tools)):
            errors.append(f"nodes.{name}.tools must be a list of tool names")
        for key in ("model", "backend", "base_url"):
            if key in entry and not (isinstance(entry[key], str) and entry[key]):
                errors.append(f"nodes.{name}.{key} must be a non-empty string")
    return errors


def apply_node_config(graph: Any, nodes_cfg: Any) -> list[dict]:
    """Resolve every node's tool set and install it on the agent.

    Returns one record per node: ``node``, ``source`` (``class`` or ``config``),
    ``declared`` (class set), ``resolved``, ``added``, ``removed``.

    The class declaration is remembered on the agent, so applying a second
    config to the same graph object starts again from the class set. Raises
    ``ValueError`` on a malformed block or a node the graph does not have.
    """
    errors = validate_nodes_block(nodes_cfg)
    unknown = sorted(set(nodes_cfg or {}) - set(graph.nodes)) if not errors else []
    errors += [f"nodes.{n}: the graph has no such node (nodes: {sorted(graph.nodes)})"
               for n in unknown]
    if errors:
        raise ValueError("config.yaml `nodes:` is invalid: " + "; ".join(errors))
    records = []
    for name, agent in graph.nodes.items():
        declared = frozenset(getattr(agent, "_declared_tools", agent.tools))
        agent._declared_tools = declared
        entry = (nodes_cfg or {}).get(name) or {}
        declared_id = getattr(agent, "_declared_identity", None) or (
            agent.model, agent.backend, agent.base_url)
        agent._declared_identity = declared_id
        agent.model = entry.get("model", declared_id[0])
        agent.backend = entry.get("backend", declared_id[1])
        agent.base_url = entry.get("base_url", declared_id[2])
        override = entry.get("tools")
        resolved = frozenset(override) if override is not None else declared
        agent.tools = resolved
        agent._tools_pinned = override is not None
        records.append({
            "node": name,
            "source": "config" if override is not None else "class",
            "declared": sorted(declared), "resolved": sorted(resolved),
            "added": sorted(resolved - declared),
            "removed": sorted(declared - resolved),
            "withheld": sorted(withheld_closures(agent)),
        })
    return records


def uses_default(tools: Any) -> bool:
    return DEFAULT in (tools or ())


def _append(debug_dir: Path, record: dict) -> None:
    rec = {"ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
           "tool": "Tools", "fault": "nudge", **record}
    try:
        with (Path(debug_dir) / "diagnostics.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


#: Tools the runtime grants every node from the topology, not from its
#: declared set. A disabled feature withholds them all the same.
_TOPOLOGY_GRANTED = frozenset({"SendMessage"})


def feature_withheld(resolved: Any) -> dict[str, str]:
    """``{tool: feature key}`` for tools a disabled ablation feature takes away.

    Only tools the node would otherwise hold: its resolved set plus the tools
    the topology grants to every node.
    """
    from . import features
    held = frozenset(resolved) | _TOPOLOGY_GRANTED
    return {t: f.key for f in features.FEATURES if not features.enabled(f.key)
            for t in sorted(f.tools & held)}


def record_resolution(debug_dir: Path, records: list[dict], log: logging.Logger) -> None:
    """Write ``node_tools.json`` and the run-start notices (console + diagnostics).

    ``effective`` is what the node really has: ``resolved`` minus the tools a
    disabled feature withholds (``feature_withheld``, each with its feature
    key). ``withheld`` is the separate ``nodes:`` list effect on always-on
    closures.
    """
    for r in records:
        r["feature_withheld"] = feature_withheld(r["resolved"])
        r["effective"] = sorted(set(r["resolved"]) - set(r["feature_withheld"]))
    try:
        (Path(debug_dir) / "node_tools.json").write_text(
            json.dumps({r["node"]: r for r in records}, indent=2), encoding="utf-8")
    except OSError:
        pass
    for r in records:
        if r["source"] == "config" and (r["added"] or r["removed"] or r["withheld"]):
            msg = DIFF_NOTICE.format(node=r["node"], added=r["added"],
                                     removed=r["removed"], withheld=r["withheld"])
            log.warning(msg)
            _append(debug_dir, {"node": r["node"], "error_type": "TOOLS_CONFIG_DIFFERS",
                                "message": msg, "added": r["added"], "removed": r["removed"],
                                "withheld": r["withheld"]})
        if uses_default(r["resolved"]):
            msg = BYPASS_NOTICE.format(node=r["node"])
            log.warning(msg)
            _append(debug_dir, {"node": r["node"], "error_type": "DEFAULT_TOOLS_BYPASS",
                                "message": msg})


def record_builtins(debug_dir: Path, node: str, tools: list[str]) -> None:
    """The built-ins a Default node actually received, from the CLI's init record."""
    builtins = sorted(t for t in tools if not t.startswith("mcp__"))
    unreviewed = sorted(set(builtins) - REVIEWED_BUILTINS)
    msg = f"node {node} received built-ins {builtins}"
    if unreviewed:
        msg += f"; not reviewed by adda: {unreviewed}"
    _append(debug_dir, {"node": node, "error_type": "TOOLS_RESOLVED", "message": msg,
                        "builtins": builtins, "unreviewed": unreviewed})


def record_native_expansion(debug_dir: Path, node: str, backend: str,
                            native: list[str], log: logging.Logger) -> None:
    """Default on a non-Claude backend is that adapter's native set."""
    msg = (f"node {node}: Default on backend {backend!r} means that adapter's "
           f"native tools {sorted(native)}, not the Claude CLI set. Informational.")
    log.warning(msg)
    _append(debug_dir, {"node": node, "error_type": "DEFAULT_TOOLS_EXPANDED",
                        "message": msg, "native": sorted(native)})
