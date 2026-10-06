"""Milestone ledger (process policy): the strategizer's backlog.

DISTINCT from the hypothesis ledger (epistemics — what's true, closed by
evidence). Milestones are PROCESS steps — engage with the task, assess where you
might be wrong, get the oracle right — closed by COMPLETION. They are
SOFT-escapable (MilestoneSet(id, "SKIPPED", note=reason)). Only closing the run
(Done) is HARD on them. A node holding the milestone tools gets a one-time
reminder, per design, on its first Delegate to any target while items are open;
a re-delegate proceeds. The items are independent and may be done concurrently.

Auto-satisfy predicates (code, keyed by the milestone's ``key``) tick a
milestone the moment its structural condition holds — no busywork. A milestone
with no predicate (the literature assessment) is a deliberate reflection and is
ticked by hand with a required brief.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "MilestoneLedger",
    "DEFAULT_MILESTONES",
    "open_milestones",
    "render_backlog",
    "VALID_STATUSES",
]

VALID_STATUSES = frozenset({"PENDING", "DONE", "SKIPPED"})


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Default backlog. Auto-satisfy predicates (node -> bool) live in code; a
# predicate of None means the milestone is ticked by hand (with a brief).
# --------------------------------------------------------------------------

def _canonical_source_ready(node) -> bool:
    """A canonical ground-truth oracle is registered/validated."""
    fn = getattr(node, "_canonical_source_registered", None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def _pipeline_drafted(node) -> bool:
    """The deliverable notebook (pipeline.ipynb) exists in the study dir."""
    sd = getattr(node, "_study_dir", None)
    if sd is None:
        return False
    try:
        from ..evaluation.notebook_exec import required_deliverable_name
        return (Path(sd) / required_deliverable_name()).exists()
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class DefaultMilestone:
    key: str
    description: str
    predicate: Callable | None   # None => manual tick (with a brief)


# Order = the order the agent reads them (natural workflow); none gates another.
# M1 (pipeline) seeds only when runtime.pipeline_deliverable is on (switchable);
# M2 + M3 always seed.
CRAFT_PIPELINE = DefaultMilestone(
    "craft_pipeline",
    "Engage deeply with the scientific task and author pipeline.ipynb — the "
    "f3dasm Pipeline notebook that is your top-down plan AND your single "
    "deliverable. Start with scaffold cells; grow them into the real, lazy "
    "four-pillar pipeline (loads the ledger, oracle via get_evaluator(), caches "
    "heavy blocks) that the runtime executes to reproduce the headline. Not a "
    "throwaway.",
    _pipeline_drafted,
)
ASSESS_LITERATURE = DefaultMilestone(
    "assess_literature_need",
    "Identify any element of the problem where you would otherwise fall back "
    "on memory or a default instead of evidence — surrogate/kernel choice, "
    "acquisition function, sampler, convergence criteria, or the problem "
    "framing itself. Those are where to consult the literature; be sharp about "
    "where you might be wrong (an honest outcome may be 'no review needed').",
    None,  # manual: a reflection (its honest outcome may be 'no review needed')
)
ORACLE_GOLD_STATE = DefaultMilestone(
    "oracle_gold_state",
    "Get the datagenerator/oracle into gold state — registered and "
    "validated — before generating any data. Required because Done()'s "
    "reproduction gate refuses outright when the canonical store holds zero "
    "oracle rows (see <reproduction_gate_contract>, check 0) — the ledger is "
    "only as trustworthy as the oracle behind it. If your study genuinely "
    "runs no delegated campaign at all, close this SKIPPED with a reason; "
    "the gate will still require at least one store row before it closes.",
    _canonical_source_ready,
)

# Always-seeded backlog (M2). The pipeline milestone (M1) is added by
# seed_defaults(include_pipeline=True); the oracle milestone (M3, owned by
# the reproduction_gate feature — its whole reason to exist is that gate's
# store-row precondition) by seed_defaults(include_reproduction_gate=True).
# Both seeded in that order so numbering is unchanged from before either was
# switchable.
DEFAULT_MILESTONES: list[DefaultMilestone] = [ASSESS_LITERATURE]

_PREDICATES: dict[str, Callable] = {
    d.key: d.predicate
    for d in (CRAFT_PIPELINE, ORACLE_GOLD_STATE, *DEFAULT_MILESTONES)
    if d.predicate is not None
}


class MilestoneLedger:
    """Append-only, status-tracked process ledger persisted as JSON."""

    def __init__(self, notes_dir: Path | str) -> None:
        self._path = Path(notes_dir) / "milestones.json"
        self._lock = threading.Lock()

    # -- persistence -------------------------------------------------------
    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def _next_id(data: dict) -> str:
        n = 0
        for k in data:
            try:
                n = max(n, int(k[1:]))
            except (ValueError, IndexError):
                pass
        return f"M{n + 1:03d}"

    # -- seeding + authoring ----------------------------------------------
    def seed_defaults(self, disabled: frozenset[str] = frozenset(),
                      include_pipeline: bool = False,
                      include_reproduction_gate: bool = False) -> None:
        """Seed the backlog. The pipeline milestone is included only when
        ``include_pipeline`` (runtime.pipeline_deliverable); the oracle
        milestone only when ``include_reproduction_gate``
        (runtime.reproduction_gate) — its only reason to exist is that
        gate's store-row precondition. Idempotent."""
        ordered = (
            ([CRAFT_PIPELINE] if include_pipeline else [])
            + DEFAULT_MILESTONES
            + ([ORACLE_GOLD_STATE] if include_reproduction_gate else [])
        )
        with self._lock:
            data = self._load()
            existing_keys = {m.get("key") for m in data.values()}
            for d in ordered:
                if d.key in disabled or d.key in existing_keys:
                    continue
                mid = self._next_id(data)
                data[mid] = {
                    "id": mid, "key": d.key, "description": d.description,
                    "source": "default", "status": "PENDING", "note": "",
                    "opened_at": _now(), "closed_at": None,
                    "manual": d.predicate is None,
                }
            self._save(data)

    def propose(self, description: str) -> str:
        if not description or not description.strip():
            return "ERROR: milestone description must be non-empty."
        with self._lock:
            data = self._load()
            mid = self._next_id(data)
            data[mid] = {
                "id": mid, "key": None, "description": description.strip(),
                "source": "agent", "status": "PENDING", "note": "",
                "opened_at": _now(), "closed_at": None, "manual": True,
            }
            self._save(data)
        return mid

    def _close(self, mid: str, status: str, note: str) -> str:
        with self._lock:
            data = self._load()
            m = data.get(mid)
            if m is None:
                return f"ERROR: unknown milestone {mid!r}."
            m["status"] = status
            m["note"] = note
            m["closed_at"] = _now()
            self._save(data)
        return f"{mid} marked {status}."

    def complete(self, mid: str, note: str) -> str:
        return self._close(mid, "DONE", note)

    def skip(self, mid: str, reason: str) -> str:
        return self._close(mid, "SKIPPED", reason)

    # -- queries -----------------------------------------------------------
    def list_all(self) -> list[dict]:
        return list(self._load().values())

    def get(self, mid: str) -> dict | None:
        return self._load().get(mid)

    def pending(self) -> list[dict]:
        """All milestones still PENDING."""
        return [m for m in self._load().values()
                if m.get("status") == "PENDING"]

    def auto_satisfy(self, node) -> None:
        """Tick default milestones whose structural predicate now holds."""
        with self._lock:
            data = self._load()
            changed = False
            for m in data.values():
                if (m.get("source") == "default"
                        and m.get("status") == "PENDING"):
                    pred = _PREDICATES.get(m.get("key"))
                    if pred is not None and pred(node):
                        m["status"] = "DONE"
                        m["note"] = "auto-satisfied (condition met)"
                        m["closed_at"] = _now()
                        changed = True
            if changed:
                self._save(data)

    def format(self) -> str:
        items = self.list_all()
        if not items:
            return "No milestones."
        return "\n".join(
            f"- {m['id']} [{m['status']}]: {m['description']}"
            for m in items
        )


def open_milestones(ledger: MilestoneLedger, node) -> list[dict]:
    """The still-pending milestones, after auto-satisfying the met ones.

    Returns [] when the backlog is clear.
    """
    ledger.auto_satisfy(node)
    return ledger.pending()


def render_backlog(ledger: MilestoneLedger) -> str:
    """The backlog announcement injected once at the start of the run, so the
    agent cannot claim it didn't know the backlog exists."""
    items = ledger.list_all()
    if not items:
        return ""
    lines = "\n".join(
        f"  {m['id']} [{m['status']}]: {m['description']}" for m in items)
    return (
        "<process_backlog>\n"
        "This process backlog is open. Do each item, or close it SKIPPED with a "
        "reason (MilestoneSet) if your study genuinely doesn't need it. They're "
        "independent (do them in any order, even concurrently). Delegating to "
        "the agent built for an item is the way to do it — match the task to "
        "the role built for it (oracle standardization belongs to a dedicated "
        "oracle/datagenerator agent when your graph has one, not the generic "
        "implementer). Your first delegation in each design while items are "
        "open meets a one-time reminder; re-delegate to proceed. Close each "
        "with MilestoneSet(id, 'DONE', note=…).\n\n"
        f"{lines}\n"
        "</process_backlog>"
    )
