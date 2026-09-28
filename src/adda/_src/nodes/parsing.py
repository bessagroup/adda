"""Pure helper functions for parsing and classifying agentic node responses.

These functions have no side effects, no class coupling, and no ``self``
parameter — they are extracted here from ``core.py`` to keep that module
focused on node class definitions.
"""

from __future__ import annotations

from pathlib import Path

from ..prompts.tool_catalog import tool_examples

_REQUIRED_SUBSECTIONS = [
    "### Actions taken",
    "### Conclusions",
    "### Numbers",
]

_CAPABILITY_PHRASES = [
    "i cannot", "i can't", "i don't have access",
    "unable to", "not able to", "i am unable",
]

# The critic emits exactly these three (agents/critic.py); no others.
_VALID_VERDICTS = {"PASS", "REVISE", "REJECT"}


def _resolve_delegation_evals(
    run_exp_dir: Path | None,
    delegation_id: str,
    reported: int,
) -> int:
    """Return the eval count for a delegation.

    Prefers the row count from the canonical store (authoritative, summed across
    every experiment store) over the honour-system ReportEvals self-report. Falls
    back to *reported* for delegations that wrote no rows (e.g. lookup-direct
    studies).

    Parameters
    ----------
    run_exp_dir:
        The run-level ``experiment_data/`` root (``run_config["store_dir"]``).
        Counting aggregates across the default store and every design experiment
        store beneath it.  ``None`` disables ledger counting and returns *reported*.
    delegation_id:
        E.g. ``"D003"``.
    reported:
        The value from ``ReportEvals`` (honour-system fallback).
    """
    if run_exp_dir is None:
        return reported
    try:
        from ..evaluation.ledger_summary import delegation_evals
        ledgered = delegation_evals(run_exp_dir, delegation_id)
        if ledgered > 0:
            return ledgered
    except Exception:  # noqa: BLE001
        pass
    return reported


def _stamped_eval_count(run_exp_dir: Path | None, delegation_id: str) -> int:
    """Rows stamped with this delegation_id across EVERY experiment store under
    the run's ``experiment_data/`` root (0 if none).

    Provenance-based and experiment-selection-agnostic: a delegation's rows are
    found by its stamp wherever they landed, so a worker that reached the oracle
    via ``get_evaluator(namespace=...)`` at the call site is counted correctly,
    not falsely flagged off-ledger. Unlike _resolve_delegation_evals there is no
    honour-system fallback — this reports ONLY provenance-stamped rows.
    """
    if run_exp_dir is None:
        return 0
    try:
        from ..evaluation.ledger_summary import delegation_evals
        return delegation_evals(run_exp_dir, delegation_id)
    except Exception:  # noqa: BLE001
        return 0


def _reconcile_delegation_evals(
    run_exp_dir: Path | None,
    delegation_id: str,
    claimed: int,
    source_registered: bool,
) -> tuple[int, bool, int]:
    """Reconcile a worker's claimed eval count against the store.

    The ledger (summed across every experiment store) is the single source of
    truth. When a ground-truth source is registered and the worker CLAIMED
    evaluations but NONE are provenance-stamped in ANY store, the delegation
    evaluated off-ledger: the truthful count is 0. Otherwise fall back to the
    usual resolution (store rows if any, else the honour-system claim — e.g.
    lookup-direct studies). Provenance-based, so a worker that selected its
    experiment at the call site is reconciled correctly (run 20260627T045747).

    Used on BOTH the normal-return and the cancel/detach path so an off-ledger
    delegation cannot silently keep a claimed-but-unledgered eval count.

    Returns
    -------
    (evals, off_ledger, stamped)
        ``evals`` — the truthful count to record; ``off_ledger`` — True when the
        claim could not be backed by stamped rows; ``stamped`` — stamped row
        count (for diagnostics).
    """
    stamped = _stamped_eval_count(run_exp_dir, delegation_id)
    if source_registered and claimed > 0 and stamped == 0:
        return 0, True, stamped
    return (
        _resolve_delegation_evals(run_exp_dir, delegation_id, claimed),
        False,
        stamped,
    )


def _parse_verdict(text: str) -> str:
    """Extract the critic's GATE verdict (PASS/REVISE/REJECT/…) from its text.

    Tolerant of markdown emphasis and punctuation around the token — critics
    write ``### Verdict\\n\\n**PASS**`` — so the leading ``**`` no longer makes
    a bare ``(\\w+)`` capture the asterisk and fall through to UNKNOWN (the bug
    that turned an earned PASS into an infinite revise loop). Falls back to a
    ``verdict: X`` line (e.g. in a ### Numbers block). Returns the UPPER token
    or ``"UNKNOWN"``.
    """
    import re as _re
    m = _re.search(
        r"###\s*Verdict\b[\s:>*_`\"'\-]*([A-Za-z]+)", text, _re.IGNORECASE)
    if m and m.group(1).upper() in _VALID_VERDICTS:
        return m.group(1).upper()
    for mm in _re.finditer(
        r"verdict\s*[:=]\s*[*_`\"']*([A-Za-z]+)", text, _re.IGNORECASE
    ):
        if mm.group(1).upper() in _VALID_VERDICTS:
            return mm.group(1).upper()
    return "UNKNOWN"


def _to_adapter_messages(lc_messages: list) -> list[dict]:
    """Convert LangChain message objects to adapter-format dicts.

    Block content is flattened by ``_flatten_content``, which prefers a
    block's ``text`` and falls back to its ``content``. The previous join on
    ``c.get("text", "")`` alone turned any non-text block into the empty
    string, so a HumanMessage carrying tool-result blocks arrived here as
    ``{"role": "user", "content": ""}`` — its payload destroyed, and the
    request downstream left with a user turn holding nothing.

    Empty turns are dropped for the same reason they are dropped in
    ``_to_lc_messages``: they carry nothing, and a provider that counts
    non-empty user turns rejects the whole request over one.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from ..backends.openai_compatible import _flatten_content

    result: list[dict] = []
    for msg in lc_messages:
        if isinstance(msg, HumanMessage):
            role = "user"
        elif isinstance(msg, AIMessage):
            role = "ai"
        else:
            continue
        content = _flatten_content(msg.content)
        if not content.strip():
            continue
        result.append({"role": role, "content": content})
    return result


def _classify_response(
    text: str,
    required_sections: list[str] | None = None,
) -> str | None:
    """Return a REFLECT diagnosis string if text is malformed, else None.

    Parameters
    ----------
    text : str
        The raw response text from a worker agent.
    required_sections : list[str] or None
        Subsection headers that must appear in the ``## Report`` block.
        When ``None`` the four default sections from
        ``_REQUIRED_SUBSECTIONS`` are used.
    """
    from ..prompts.agent_prompts import (
        REFLECT_DIAGNOSIS_CAPABILITY_LIMIT,
        REFLECT_DIAGNOSIS_MISSING_SUBSECTIONS_TEMPLATE,
        REFLECT_DIAGNOSIS_NO_REPORT_HEADING,
        REFLECT_DIAGNOSIS_SHORT,
    )

    sections = _REQUIRED_SUBSECTIONS if required_sections is None else required_sections

    if len(text.strip()) < 100:
        return REFLECT_DIAGNOSIS_SHORT
    low = text.lower()
    # A capability-limit only counts when the worker produced NO report at all
    # (it gave up). A valid report may legitimately say "I cannot reproduce X"
    # in its Conclusions without being a capability failure (audit O41: the
    # phrase-match used to fire on correct reports).
    if "## report" not in low:
        if any(p in low for p in _CAPABILITY_PHRASES):
            return REFLECT_DIAGNOSIS_CAPABILITY_LIMIT
        return REFLECT_DIAGNOSIS_NO_REPORT_HEADING
    missing = [s for s in sections if s.lower() not in low]
    if missing:
        return REFLECT_DIAGNOSIS_MISSING_SUBSECTIONS_TEMPLATE.format(
            missing_subsections=", ".join(f"'{s}'" for s in missing)
        )
    return None


def _extract_report_section(text: str, name: str) -> str:
    """Return the body of the section called ``name``, or '' if absent.

    The one section extractor for every heading-based parse (retrospectives,
    the critic's prior-review digest). Models write the same section several
    ways, so all of these open a section: ``### X`` / ``## X`` (any trailing
    text on the heading line is elaboration and is ignored, so "### X (Y)"
    matches but "### XNotes" does not), ``**X**`` / ``**X:**``, and a line
    starting ``X:``. A section whose opener carries nothing after the name runs
    to the next heading, bold-only heading line, horizontal rule or end of
    text. A bold or ``X:`` opener that carries text on its own line ("Verdict:
    REVISE. I found ...") is a paragraph: it runs to the first blank line.
    """
    import re as _re
    n = _re.escape(name)
    opener = _re.compile(
        rf"^\s*(?:(?P<hash>\#{{1,6}})\s+{n}\b(?P<hrest>.*)"
        rf"|(?P<bold>\*\*|__){n}\b\s*:?\s*(?:\*\*|__)?\s*:?(?P<brest>.*)"
        rf"|{n}\s*:(?P<prest>.*))$",
        _re.IGNORECASE,
    )
    stop = _re.compile(
        r"^\s*(?:\#{1,6}\s|---\s*$|(?:\*\*|__)[^*_\n]+(?:\*\*|__)\s*:?\s*$)")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = opener.match(line)
        if not m:
            continue
        rest = (m.group("brest") if m.group("bold") else m.group("prest")
                if m.group("prest") is not None else "").strip()
        inline = bool(rest)
        body = [rest] if inline else []
        for nxt in lines[i + 1:]:
            if stop.match(nxt) or (inline and not nxt.strip()):
                break
            body.append(nxt)
        return "\n".join(body).strip()
    return ""


@tool_examples(
    'ConsultHandbook()',
    "ConsultHandbook('falsification-charter')",
)
def _consult_handbook(query: str = "") -> str:
    """ConsultHandbook tool: browse the curated handbook of project conventions.

    Call with NO argument to get the table of contents (every chapter's id +
    title). Pass a chapter id (e.g. "falsification-charter") to read that one
    chapter in full. Pass free-text keywords to search when you don't know the
    id. Read-only and best-effort — never raises into the agent loop.
    """
    try:
        from ..knowledge import KnowledgeBase
        kb = KnowledgeBase.load()
    except Exception as exc:  # noqa: BLE001
        return f"(handbook unavailable: {exc})"
    q = str(query).strip()
    if not q:
        return kb.toc()
    entry = kb.get(q)  # exact chapter id → full chapter
    if entry is not None:
        return entry.render()
    hits = kb.search(q, k=3)  # otherwise keyword search
    if not hits:
        return (
            "No chapter id or keyword matched. Call ConsultHandbook() with no "
            "argument to list the available chapters."
        )
    return "\n\n---\n\n".join(e.render() for e in hits)
