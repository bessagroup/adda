"""Summarising compaction for OpenAI-compatible backends.

WHY THIS EXISTS ALONGSIDE TRIMMING
    ``context_budget`` keeps a turn inside the served window by DROPPING
    messages. That is free and deterministic, and it is also blind: the oldest
    message it evicts may be the delegation report the whole experiment turns
    on, and nothing downstream can tell that it went. Compaction replaces that
    span with a summary instead, so the decisions survive and only the prose
    around them is lost.

    The cost is one generation per compaction event — not per turn, because the
    result is memoised on the span it was built from. An earlier version of
    this argument put the cost far higher by confusing output throughput with
    total latency: prefill is much faster than generation, and continuous
    batching and prefix caching make re-reading a long input cheaper still. The
    real bill is the summary's own output tokens.

WHAT IT CANNOT PROMISE
    A summary is model output, so it can be wrong in ways a dropped message
    cannot. Trimming loses information visibly; compaction can quietly restate
    a number. That is why the summary is fenced and labelled in the message it
    produces, why the full transcript is still what lands on disk, and why the
    run records which policy it used — an analysis can condition on it.

THE INVARIANTS IT SHARES WITH TRIMMING
    The first user turn is never summarised away: evicting it is the server bug
    this whole subsystem exists to avoid. The kept tail never opens on an
    orphaned ToolMessage. And a failure to summarise is never a failed turn —
    it falls back to trimming, which always works.
"""
from __future__ import annotations

import hashlib

from . import context_budget

__all__ = ["SUMMARY_INSTRUCTION", "compact_to_budget", "span_key"]

#: Share of the budget the kept tail may occupy. Smaller than the trim's,
#: because the summary itself has to fit alongside it and a compaction that
#: keeps almost everything verbatim has not compacted anything.
_TAIL_SHARE = 0.55

#: What the summary is asked for. Written for THIS system rather than in
#: general: an adda run's conclusions have to trace to ledgered numbers, so a
#: summary that keeps the narrative and drops the figures is worse than
#: useless — it reads as if the evidence is still there.
SUMMARY_INSTRUCTION = (
    "Summarise the conversation below so that work can continue from it "
    "without the original text.\n\n"
    "KEEP, in this order of priority:\n"
    "  1. Every numerical result, and the delegation id it came from.\n"
    "  2. Decisions taken and the reason given for each.\n"
    "  3. What was tried and FAILED, so it is not retried.\n"
    "  4. Open questions and anything explicitly deferred.\n"
    "DROP: restated instructions, tool-call mechanics, file listings, and "
    "anything already visible in the messages that follow this summary.\n\n"
    "Do not speculate and do not resolve anything left open. If a number is "
    "not stated in the text, do not supply one.\n\n"
    "CONVERSATION:\n"
)

#: Prepended to the summary in the message that replaces the span, so neither
#: the agent nor a person reading the transcript mistakes it for something
#: that was said.
_SUMMARY_HEADER = (
    "[adda compacted {n} earlier message(s) to stay within the context "
    "window. What follows is a SUMMARY, not a transcript — treat any number "
    "in it as a pointer back to the ledger, not as a fresh measurement.]\n\n"
)


def span_key(messages: list) -> str:
    """Stable identity for a span, so an unchanged span is summarised once.

    The hook runs on every model call inside a turn, and the span it wants to
    compact is usually the same one it compacted a moment ago. Hashing the
    span's own text is what turns "one generation per compaction" into a true
    statement rather than an aspiration.
    """
    h = hashlib.sha256()
    for m in messages:
        h.update(context_budget._text_of(m).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _plan(messages: list, *, context_window: int, reserve_tokens: int):
    """``(head, span, tail, budget)`` — what to keep, and what to summarise.

    Shares ``context_budget``'s arithmetic deliberately: two subsystems that
    disagree about how much room there is would each be right about a
    different window.
    """
    budget = int((context_window - reserve_tokens)
                 * (1 - context_budget._RESPONSE_HEADROOM))
    if budget <= 0 or not messages:
        return [], [], list(messages), budget

    pin_at = next((i for i, m in enumerate(messages)
                   if m.__class__.__name__ == "HumanMessage"), None)
    head = [messages[pin_at]] if pin_at is not None else []
    spent = sum(context_budget.estimate_tokens(m) for m in head)

    tail_budget = int(budget * _TAIL_SHARE)
    tail: list = []
    for i in range(len(messages) - 1, -1, -1):
        if i == pin_at:
            continue
        cost = context_budget.estimate_tokens(messages[i])
        if spent + cost > tail_budget:
            break
        tail.append(messages[i])
        spent += cost
    tail.reverse()

    # Never open the tail on an orphaned tool result: its parent AIMessage is
    # in the span about to be replaced, and a ToolMessage with no call to
    # answer is a malformed request in its own right.
    while tail and context_budget._is_tool_message(tail[0]):
        tail.pop(0)

    kept = {id(m) for m in head} | {id(m) for m in tail}
    span = [m for m in messages if id(m) not in kept]
    return head, span, tail, budget


def summary_header(n: int) -> str:
    """The exact prefix of the note that replaces a compacted span of ``n``."""
    return _SUMMARY_HEADER.format(n=n)


def compact_to_budget(messages: list, *, context_window: int,
                      reserve_tokens: int, summarize, cache: dict):
    """Replace the middle of the conversation with a summary of it.

    ``summarize`` takes the span's rendered text and returns a summary string;
    it is injected so this module stays testable without a model, and so the
    caller decides which model pays for it.

    Falls back to :func:`context_budget.trim_to_budget` when there is nothing
    to compact or when summarising fails. A context policy that can fail the
    turn is worse than the truncation it replaced.
    """
    fits = context_budget.trim_to_budget(
        messages, context_window=context_window,
        reserve_tokens=reserve_tokens)
    if not fits[1].fired:
        return fits                                   # already inside the window

    head, span, tail, budget = _plan(
        messages, context_window=context_window, reserve_tokens=reserve_tokens)
    if len(span) < 2:
        return fits              # nothing worth a generation; trimming is honest

    key = span_key(span)
    summary = cache.get(key)
    if summary is None:
        body = "\n\n".join(
            f"[{m.__class__.__name__}] {context_budget._text_of(m)}"
            for m in span)
        try:
            summary = (summarize(SUMMARY_INSTRUCTION + body) or "").strip()
        except Exception:                             # noqa: BLE001
            summary = ""
        if not summary:
            return fits                               # fall back to trimming
        cache[key] = summary

    from langchain_core.messages import SystemMessage
    note = SystemMessage(content=_SUMMARY_HEADER.format(n=len(span)) + summary)
    kept = head + [note] + tail

    before = sum(context_budget.estimate_tokens(m) for m in messages)
    after = sum(context_budget.estimate_tokens(m) for m in kept)
    report = context_budget.TrimReport(
        dropped=len(span), truncated=0, before=before, after=after,
        budget=budget)
    return kept, report
