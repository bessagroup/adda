"""Graph-wide append-only delegation log for agentic runs.

Stores all inter-node delegations in ``debug/delegation_log.jsonl``.
Thread-safe via a single lock.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["DelegationLog"]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


class DelegationLog:
    """Graph-wide append-only log at debug/delegation_log.jsonl.

    Every inter-node delegation — regardless of which node fired it — is
    recorded here. Stores FULL task text and FULL deliverable text (not truncated).
    Thread-safe via a single lock.
    """

    def __init__(self, log_path: Path) -> None:
        self._path = Path(log_path)
        self._lock = threading.Lock()
        # Ensure parent directory exists
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Monotonic sequence counter for globally-unique delegation IDs.
        # Shared across all orchestrating nodes that hold a reference to
        # this log — guarantees D### uniqueness even when datagenerator
        # and implementer both delegate to literature_reviewer.
        # RESUME-SAFE: seed PAST any D### ids already in the log, so a resumed
        # session does not restart at D001 and collide with a crashed turn's
        # D001/D003/… workspaces (run 20260715T191329). A fresh run (no/empty
        # log) starts at 0 → first id D001, unchanged.
        self._seq: int = self._max_existing_seq()

    def _max_existing_seq(self) -> int:
        """Highest D### number already recorded in the log (0 if none/absent),
        so next_id() continues from the true high-water mark on resume."""
        import re as _re
        if not self._path.exists():
            return 0
        hi = 0
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rid = json.loads(line).get("id") or ""
                except json.JSONDecodeError:
                    continue
                m = _re.fullmatch(r"D(\d+)", str(rid))
                if m:
                    hi = max(hi, int(m.group(1)))
        except OSError:
            return 0
        return hi

    def next_id(self) -> str:
        """Return the next globally-unique delegation ID (e.g. ``"D001"``).

        Thread-safe: increments the shared monotonic counter under
        ``self._lock``.
        """
        with self._lock:
            self._seq += 1
            return f"D{self._seq:03d}"

    def record(
        self,
        *,
        id: str,
        from_node: str,
        to_node: str,
        task: str,
        deliverable: str,
        hypothesis_ids: list[str],
        started_at: str,
        completed_at: str,
        status: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float | None = None,
        is_falsification_attempt: bool = False,
        evals: int = 0,
        phase: str | None = None,
        constraints: dict | None = None,
        workspace_sha: str | None = None,
        critic_review: str | None = None,
        reply: str | None = None,
    ) -> None:
        """Append one delegation record.

        ``reply`` is a worker's answer to a review question. It is kept
        beside the deliverable, never in place of it.

        ``critic_review`` is the ``debug/critic_reviews/`` file a critic
        delegation (GATE or FEEDBACK) produced, e.g. ``call_003.md``; None
        for every other delegation and for runs that predate it.

        ``workspace_sha`` is the commit this delegation produced in the run's
        workspace repository (spec 11) — the mechanical answer to "which files
        did this delegation change", as opposed to the deliverable's own
        account of it. Additive and default None: a run whose workspace could
        not be version-controlled, and every record written before spec 11,
        simply carries no sha.

        task and deliverable are stored in full. ``phase`` is the optional
        f3dasm process phase this delegation belongs to (DoE / DataGeneration /
        ML / Optimization / …); additive, default None for old/untagged records.
        ``constraints`` is the run's ConstraintSnapshot.as_dict() at completion
        (eval/wall-clock budget and usage) — additive, default None for
        old/untagged records; see constraint_snapshot.py for the single source
        of truth this comes from (every node-node interaction, human->
        strategizer included, is a delegation and gets the same snapshot).
        """
        record: dict[str, Any] = {
            "id": id,
            "from_node": from_node,
            "to_node": to_node,
            "task": task,
            "deliverable": deliverable,
            "hypothesis_ids": hypothesis_ids,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "is_falsification_attempt": is_falsification_attempt,
            "evals": evals,
            "phase": phase,
            "constraints": constraints,
            "workspace_sha": workspace_sha,
            "critic_review": critic_review,
        }
        if reply is not None:
            record["reply"] = reply
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def record_started(
        self,
        *,
        id: str,
        from_node: str,
        to_node: str,
        task: str,
        hypothesis_ids: list[str],
        started_at: str,
        is_falsification_attempt: bool = False,
        phase: str | None = None,
        constraints: dict | None = None,
        session_started_at: str | None = "__unset__",
    ) -> None:
        """Append a RUNNING entry at DISPATCH, before the worker runs.

        A delegation flushes provenance-stamped rows into the canonical ledger
        DURING execution, but record() (the terminal DONE/FAILED entry) is only
        written at COMPLETION. A delegation cancelled or killed mid-flight (e.g.
        by the wall/eval budget) would otherwise leave ledgered rows with NO log
        entry — orphan evals that break the provenance audit trail and escape
        the eval-budget counter. This RUNNING entry guarantees every dispatched
        delegation is traceable; the terminal record (same id) supersedes it via
        the last-wins collapse in _load_all. ``constraints`` is the
        ConstraintSnapshot.as_dict() at DISPATCH time (see constraint_snapshot.py).

        ``session_started_at``: pass explicit ``None`` for a delegation
        dispatched QUEUED (a same-role delegation already holds the shared
        adapter's serializing lock) -- its real start is unknown until
        ``mark_session_started`` patches it in. Left unset (the sentinel
        default), it is the same as ``started_at`` -- the ordinary, not-
        queued case every caller that doesn't track queueing gets for free.
        """
        if session_started_at == "__unset__":
            session_started_at = started_at
        record: dict[str, Any] = {
            "id": id,
            "from_node": from_node,
            "to_node": to_node,
            "task": task,
            "deliverable": "",
            "hypothesis_ids": hypothesis_ids,
            "started_at": started_at,
            # None means this delegation was QUEUED at dispatch (a same-role
            # delegation already held the shared adapter's serializing lock)
            # -- its real session start is unknown until mark_session_started
            # patches it in. Not queued: same as started_at (no wait).
            "session_started_at": session_started_at,
            "completed_at": None,
            "status": "RUNNING",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": None,
            "is_falsification_attempt": is_falsification_attempt,
            "evals": 0,
            "phase": phase,
            "constraints": constraints,
        }
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def mark_session_started(
        self, delegation_id: str, session_started_at: str
    ) -> bool:
        """Patch in the REAL session-start time for a delegation dispatched
        while QUEUED (record_started's session_started_at was None).

        Same append-only PATCH pattern as mark_attempt -- see its docstring
        for why a rewrite-in-place is unsafe. Returns True iff a matching
        record exists to patch.
        """
        with self._lock:
            records = self._load_all()
            if not any(r.get("id") == delegation_id for r in records):
                return False
            patch_row = {
                "id": delegation_id,
                "patch": {"session_started_at": session_started_at},
                "ts": _now_iso(),
            }
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(patch_row) + "\n")
            return True

    def query_received(
        self,
        node_name: str,
        n: int | None = None,
        include_in_flight: bool = False,
    ) -> list[dict]:
        """Return last n records where to_node == node_name, oldest-first.

        Returns all matching records if n is None.

        Still-RUNNING records are excluded by default. record_started() appends
        a RUNNING row with an empty deliverable BEFORE the worker is invoked,
        so the delegation a node is executing right now is already in the log
        when that node calls RecallHistory — without this filter it recalls
        its own in-flight task as "prior work" with a blank deliverable.
        _load_all collapses by id last-wins, so a finished delegation's DONE
        row supersedes its RUNNING row and is never filtered here; only
        genuinely in-flight records are. Terminal statuses other than DONE
        (FAILED, the critic's GATE:*) are history and are kept.
        """
        with self._lock:
            records = self._load_all()

        matching = [r for r in records if r.get("to_node") == node_name]
        if not include_in_flight:
            matching = [r for r in matching if r.get("status") != "RUNNING"]
        if n is not None:
            # MCP string-in tools may pass n as "6"; `matching[-n:]` would raise
            # "bad operand type for unary -: 'str'". Coerce here so every caller
            # (routing.py and worker.py RecallHistory) is covered at once.
            try:
                n = int(n)
            except (TypeError, ValueError):
                return matching
            # Return the last n, oldest-first
            matching = matching[-n:]
        return matching

    def last_completed_id(self, from_node: str) -> str | None:
        """Return the ID of the most recently completed (DONE) delegation
        sent BY from_node. Used for triggered_by injection in HypothesisUpdate."""
        with self._lock:
            records = self._load_all()

        # Filter for DONE delegations from this node, return last one's ID
        done = [
            r for r in records
            if r.get("from_node") == from_node and r.get("status") == "DONE"
        ]
        if not done:
            return None
        return done[-1]["id"]

    def mark_attempt(self, delegation_id: str, hypothesis_id: str) -> bool:
        """Retroactively flag an existing record as a falsification ATTEMPT of
        hypothesis_id (the read-time post-hoc link).

        Appends a PATCH row -- ``{"id", "patch": {...}, "ts"}`` -- rather than
        rewriting the log. A full-row rewrite (this method's original
        implementation) reads ``_load_all()``'s collapsed view, so a
        concurrent write racing it is a real regression, not a hypothetical:
        if delegation D003 is still RUNNING when this method's own
        ``_load_all()`` call loads it, and D003's real terminal DONE row
        lands on disk before this method's rewrite completes, the rewrite
        would silently regress D003 back to RUNNING, discarding a real
        completion. A patch row can never regress anything -- it carries no
        ``status`` of its own, so it is never mistaken for one (see
        ``_load_all``'s explicit skip), and it is invisible to nothing: every
        other id's RUNNING/OPEN_FOR_REVIEW/DONE history stays exactly as
        appended, which the previous rewrite-in-place implementation
        silently destroyed for every OTHER delegation in the log too, not
        just this one (real run: 20260928T012945 -- one post-hoc link on
        D003 erased D001's and D002's own RUNNING/OPEN_FOR_REVIEW rows).

        Sets is_falsification_attempt=True, adds hypothesis_id to its
        hypothesis_ids (merged at read time in ``_load_all``, not here --
        the patch carries the single ``hypothesis_id``, not a pre-merged
        list, so it means the same thing regardless of read order relative
        to other patches), and stamps attempt_linked_post_hoc=True so the
        critic scrutinises adequacy harder than a flag declared up front at
        delegate time. Returns True iff a matching record exists to patch.
        """
        with self._lock:
            records = self._load_all()
            if not any(r.get("id") == delegation_id for r in records):
                return False
            patch_row = {
                "id": delegation_id,
                "patch": {
                    "is_falsification_attempt": True,
                    "attempt_linked_post_hoc": True,
                    "hypothesis_id": hypothesis_id,
                },
                "ts": _now_iso(),
            }
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(patch_row) + "\n")
            return True

    def deliverable_versions(self, delegation_id: str) -> list[tuple[int, str]]:
        """Every deliverable written for ``delegation_id``, oldest first, as
        (version, text). The version is the row's 1-based position among that
        id's rows in the log, so it names the same row a reader sees there."""
        out: list[tuple[int, str]] = []
        with self._lock:
            if not self._path.exists():
                return out
            n = 0
            for line in self._path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") != delegation_id or "patch" in r:
                    continue
                n += 1
                if r.get("deliverable"):
                    out.append((n, r["deliverable"]))
        return out

    def query_all(self) -> list[dict]:
        """Return every record, oldest-first."""
        with self._lock:
            return self._load_all()

    def _load_all(self) -> list[dict]:
        """Load all records, COLLAPSED to one per delegation id (last write wins),
        with every PATCH row for that id merged on top.

        A delegation is logged with a RUNNING entry at dispatch and a terminal
        (DONE/FAILED) entry at completion. Collapsing last-wins gives every
        consumer one record per id — the terminal record if it exists, else the
        RUNNING entry that keeps a cancelled/killed delegation's ledger rows
        traceable. Ordered by the position of each id's latest STATUS write so
        completion order is preserved (last_completed_id stays correct).

        A PATCH row (``mark_attempt``'s ``{"id", "patch": {...}, "ts"}``) never
        counts as a status write — it carries no ``status`` of its own, so it
        is excluded from the collapse above and instead applied on TOP of
        whichever status row wins, regardless of the patch's own position in
        the file relative to that row (a link that lands while a delegation is
        still RUNNING, followed later by its real DONE, still ends up DONE
        with the attempt flags set — the patch is never itself mistaken for
        the delegation's current state). ``hypothesis_id`` in a patch is
        merged into the collapsed record's ``hypothesis_ids`` list rather than
        replacing it, since more than one patch can target the same id.

        Must be called under lock or in a read-only context.
        """
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").strip().splitlines()
        latest: dict[str, dict] = {}
        order: dict[str, int] = {}
        patches: dict[str, list[dict]] = {}
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = r.get("id")
            if rid is None:
                continue
            if "patch" in r:
                patches.setdefault(rid, []).append(r["patch"])
                continue
            latest[rid] = r
            order[rid] = i
        out = []
        for k in sorted(latest, key=lambda k: order[k]):
            rec = dict(latest[k])
            for patch in patches.get(k, ()):
                hid = patch.get("hypothesis_id")
                if hid is not None:
                    hids = rec.get("hypothesis_ids") or []
                    if hid not in hids:
                        hids = [*hids, hid]
                    rec["hypothesis_ids"] = hids
                for pk, pv in patch.items():
                    if pk != "hypothesis_id":
                        rec[pk] = pv
            out.append(rec)
        return out
