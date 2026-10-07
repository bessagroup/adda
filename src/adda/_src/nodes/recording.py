"""Cross-cutting recording for the strategizer: token/usage accounting,
tool-error counts, retrospectives, interventions, science-drift diagnostics.
A mixin so call sites (self._record_x / node._record_x) stay unchanged."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..infra.telemetry import NORMALIZED_FIELDS, has_normalized_usage
from .parsing import _extract_report_section

# Max chars persisted per retrospective. The strategizer's end-of-run exit
# interview (CONSISTENCY/DECISION/FRICTION/BLOCKED) is the highest-signal
# first-person record there is; the old 2000-char cap truncated it mid-sentence
# (run 20260624T021359). Uniform across roles — no node-specific special-casing.
_RETRO_ITEMS = ("CONSISTENCY", "DECISION", "FRICTION", "BLOCKED")
_RETRO_TEXT_CAP = 16000

# Fault classification (system vs agent) for the diagnostics KPI. The exception
# TYPE is authoritative. Message matching is a fallback for string-only errors
# (e.g. an adapter that returns "429 rate limit" as text), so it uses only
# HIGH-PRECISION tokens — NOT natural-language words like "retry", "connection",
# "timeout", "network", that routinely appear in a tool's own advice text and
# caused agent errors to be mis-tagged "system" (run 20260630T164908: an
# a notebook edit's "...and retry." advice tagged the agent error as system).
_SYSTEM_EXC_TYPES = frozenset({
    "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout",
    "HTTPError", "ChunkedEncodingError", "ProxyError", "SSLError",
})
_SYSTEM_MSG_PATTERNS = (
    "429", "rate limit", "rate-limit", "overloaded",
    "500 internal server error", "502 bad gateway", "503 service unavailable",
)


def _classify_fault(error_type: str, message: str) -> str:
    """"system" (transient API/network) vs "agent" (bad args / wrong usage).

    Authoritative on the exception type; message match is a high-precision
    fallback so tool-advice wording never mis-tags an agent error.
    """
    if error_type in _SYSTEM_EXC_TYPES:
        return "system"
    msg_lower = (message or "").lower()
    if any(p in msg_lower for p in _SYSTEM_MSG_PATTERNS):
        return "system"
    return "agent"


class RecordingMixin:
    def _role_of(self, target: str) -> str:
        """Configured role of a connected node, falling back to its name."""
        return getattr(self._spec.nodes.get(target), "role", None) or target

    def _record_usage(
        self,
        usage: dict,
        *,
        role: str | None,
        model: str | None,
        phase: str | None,
        delegation_id: str | None,
    ) -> None:
        """Accumulate token totals (decision path) AND emit one telemetry row
        (additive, off the decision path).  Telemetry failures are swallowed
        inside ``record_call`` so they can never break a run."""
        self._accumulate_usage(usage)
        if self._telemetry is not None:
            self._telemetry.record_call(
                role=role, model=model, phase=phase,
                delegation_id=delegation_id, usage=usage,
            )

    def _accumulate_usage(self, usage: dict) -> None:
        """Thread-safe accumulation of token counts from adapter.last_usage."""
        with self._registry_lock:
            self._token_totals["input_tokens"] += usage.get("input_tokens", 0) or 0
            self._token_totals["output_tokens"] += usage.get("output_tokens", 0) or 0
            self._token_totals["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0) or 0
            self._token_totals["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0) or 0
            t = self._token_totals
            if has_normalized_usage(usage):
                t["normalized_calls"] = t.get("normalized_calls", 0) + 1
                for f in NORMALIZED_FIELDS:
                    t[f] = t.get(f, 0) + int(usage[f])
            else:
                t["legacy_calls"] = t.get("legacy_calls", 0) + 1
            cost = usage.get("total_cost_usd")
            if cost is not None:
                self._token_totals["total_cost_usd"] += cost
                self._cost_observed = True

    def _record_tool_error(
        self,
        node_name: str,
        tool_name: str,
        error_type: str,
        message: str,
        tb: str | None = None,
        args: dict | None = None,
    ) -> None:
        """Increment error counter and append to diagnostics.jsonl (thread-safe).

        ``args`` are the call's arguments; each value is capped at 2,000 chars.
        """
        import json as _json

        # Classify fault: system (transient API/network) vs agent (bad usage).
        fault = _classify_fault(error_type, message)

        with self._registry_lock:
            self._error_counts[node_name] = self._error_counts.get(node_name, 0) + 1
        notes = self._current_notes_dir
        if notes is None:
            return
        # _current_notes_dir is debug/strategizer_notes/; parent is debug/
        debug_dir = Path(notes).parent
        record: dict = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "node": node_name,
            "tool": tool_name,
            "error_type": error_type,
            "fault": fault,
            "message": message,
        }
        if tb:
            record["traceback"] = tb
        if args:
            record["args"] = {k: str(v)[:2000] for k, v in args.items()}
        try:
            with (debug_dir / "diagnostics.jsonl").open("a", encoding="utf-8") as f:
                f.write(_json.dumps(record) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _record_llm_retry(
        self, attempt: int, max_attempts: int, exc: BaseException,
        delay: float,
    ) -> None:
        """One diagnostics.jsonl row per transient-error retry of an LLM call.

        Not an error (no ``_error_counts`` bump): the call is retried and may
        succeed. The row exists so a retry is visible -- how often, which
        exception, how long the backoff -- instead of silent.
        """
        import json as _json

        from ..backends.base import get_delegation_id

        notes = self._current_notes_dir
        if notes is None:
            return
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "node": self._name,
            "delegation_id": get_delegation_id(),
            "error_type": "LLM_RETRY",
            "event": "LLM_RETRY",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "exception": type(exc).__name__,
            "message": str(exc)[:300],
            "delay_s": round(delay, 2),
        }
        try:
            with (Path(notes).parent / "diagnostics.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(_json.dumps(record) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _record_retrospective(
        self, role: str, source_id: str, report_text: str,
        deliverable_version: int | None = None,
    ) -> None:
        """Capture a node's end-of-life ### Retrospective.

        Every node has a 'my job is done' moment — workers at delegation
        completion, the strategizer (and any future orchestrator) at Done().
        Parse the section, persist to retrospectives.jsonl, and surface a
        diagnostic + strategizer notification the instant it flags
        contradictory system instructions (the cheapest, highest-value
        failure mode to catch). Best-effort; never raises.

        A Done() call that ACTUALLY ARRIVED with non-empty text is always
        recorded here, even when ``_extract_report_section`` fails to find
        a ``### Retrospective`` heading in it — never silently dropped. Only
        genuinely empty text (nothing submitted at all) returns without
        writing anything. This matters because ``write_fallback_retrospective``
        (infra/watchdog_cleanup.py) trusts "no entry for this role in
        retrospectives.jsonl" to mean "the retrospective never arrived" and
        synthesizes a claim saying so; if a real, substantive reply arrived
        but merely failed to parse (run 20260926T124841's regex bug, now
        fixed — see _extract_report_section), silently dropping it here made
        that downstream claim FALSE — the run's own record said a retrospective
        "never arrived" when one, in fact, had. Recording a parse-failure
        entry (with the raw text preserved) keeps that claim honest without
        needing write_fallback_retrospective itself to know the difference.
        """
        try:
            report_text = report_text or ""
            retro = _extract_report_section(
                report_text, "Retrospective", _RETRO_ITEMS)
            parse_failed = bool(report_text.strip()) and not retro
            if not retro and not parse_failed:
                return
            notes = self._current_notes_dir
            if notes is None:
                return
            import json as _json
            import re as _re
            debug_dir = Path(notes).parent
            scan_text = retro or report_text
            flagged = bool(_re.search(r"CONSISTENCY:\s*flagged", scan_text, _re.I))
            now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
            if parse_failed:
                body = (
                    "PARSE FAILURE: the response contained no ### Retrospective "
                    f"section. Raw text follows.\n\n{report_text}"
                )
            else:
                body = retro
            rec = {
                "ts": now, "source_id": source_id, "role": role,
                "flagged": flagged, "text": body[:_RETRO_TEXT_CAP],
                "parse_failed": parse_failed,
            }
            if deliverable_version is not None:
                rec["deliverable_version"] = deliverable_version
            with (debug_dir / "retrospectives.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec) + "\n")
            if flagged:
                drec = {
                    "ts": now, "node": role, "tool": "Retrospective",
                    "error_type": "CONSISTENCY_FLAG", "fault": "system",
                    "message": body[:300],
                }
                with (debug_dir / "diagnostics.jsonl").open(
                        "a", encoding="utf-8") as f:
                    f.write(_json.dumps(drec) + "\n")
                with self._notifications_lock:
                    self._notifications.append(
                        f"[CONSISTENCY FLAG — {role} ({source_id}) reported "
                        "contradictory system instructions; see "
                        "debug/retrospectives.jsonl]"
                    )
        except Exception:  # noqa: BLE001
            pass

    def _record_intervention(
        self, kind: str, target: str, message: str, **extra
    ) -> None:
        """Log a scientific-correction event (a nudge/bounce firing) to
        diagnostics.jsonl — direct evidence the self-healing layer acted,
        and, when ``extra`` carries before/after state, whether it worked.

        Neutral classification: fault='nudge' — a nudge is a correction, not
        an agent error, so this must NOT bump the error/escalation counters
        (unlike _record_tool_error). Best-effort; never raises.
        """
        notes = self._current_notes_dir
        if notes is None:
            return
        import json as _json
        debug_dir = Path(notes).parent
        record: dict = {
            "ts": datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds"),
            "node": target,
            "tool": kind,
            "error_type": kind,
            "fault": "nudge",
            "message": message,
        }
        record.update(extra)
        try:
            with (debug_dir / "diagnostics.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(_json.dumps(record) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _record_science_drift(self, payload: dict) -> None:
        """Append a SCIENCE_DRIFT record to diagnostics.jsonl."""
        import json as _json
        notes = self._current_notes_dir
        if notes is None:
            return
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds"),
            "node": self._name,
            "error_type": "SCIENCE_DRIFT",
            **payload,
        }
        try:
            path = Path(notes).parent / "diagnostics.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(record) + "\n")
        except Exception:  # noqa: BLE001
            pass
