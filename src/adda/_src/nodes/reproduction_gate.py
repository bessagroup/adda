"""The reproduction gate: is ``pipeline.ipynb`` a faithful, lazy re-derivation?

``_missing_deliverables`` says which required deliverables are absent;
``_reproduction_gate`` executes the notebook in a hermetic sandbox copy of
the canonical store and checks it exits cleanly, adds zero oracle rows,
rewrites none, and (when it declares both) states the headline it computes.
Called from ``RunNotebook(gate=True)`` and ``Done`` (``nodes/tools/routing``). A
mixin on the strategizer.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime.graph_state import AgenticState


def _headline_consistency(stdout: str) -> str | None:
    """Cross-check the notebook's STATED answer against its COMPUTED one.

    If the deliverable prints BOTH a freshly-computed ``REPRODUCED: <v>`` and a
    ``CLAIMED_HEADLINE: <v>`` (the value its write-up states), assert they
    agree within a relative tolerance. Returns an error string on mismatch,
    else None. LENIENT by design: if either marker is absent it returns None,
    so it adds no new failure mode (and no wait) when the convention isn't used
    — it only catches an internally self-contradicting deliverable (run
    20260705T181941: prose said 0.3644 while an idxmax cell printed a 0.3648
    noise row, and the gate waved it through for 4 rounds).
    """
    import re as _re

    def _grab(tag: str):
        m = _re.search(
            tag + r":\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)",
            stdout or "")
        return float(m.group(1)) if m else None

    rep, claim = _grab("REPRODUCED"), _grab("CLAIMED_HEADLINE")
    if rep is None or claim is None:
        return None
    if abs(rep - claim) > 1e-9 + 1e-3 * abs(claim):
        return (
            f"Headline inconsistency: the write-up states CLAIMED_HEADLINE="
            f"{claim} but the notebook's own computation prints REPRODUCED="
            f"{rep}. The reported answer must be what the notebook computes — "
            "fix the selection (e.g. an idxmax picking a noise/near-duplicate "
            "row) or the prose so the stated and computed headlines agree.")
    return None


class ReproductionGateMixin:
    def _missing_deliverables(self, state: AgenticState) -> list[str]:
        """Return required deliverable paths not present at study_dir yet."""
        return self._deliverable_status(state)[1]

    def _deliverable_status(
        self, state: AgenticState
    ) -> tuple[list[str], list[str]]:
        """``(present, missing)``: the required deliverables at study_dir.

        The single deliverable (pipeline.ipynb) is required, authored before
        Done() is accepted, UNLESS the study turns off pipeline_deliverable —
        it is the human-readable recipe AND the reproduction in one notebook:
        the runtime executes it lazily (see _reproduction_gate) to verify the
        headline re-derives from the ledger with zero new evals, which makes
        no sense for a study with no ledger at all. Requiring it unconditionally
        (BACKLOG #30) meant a pure-derivation study's strategizer could never
        satisfy Done() regardless of what its own tools/prompt said — this was
        the third of three places that assumption was baked in (the other two,
        the injected notebook_deliverable_spec() preamble and the notebook-
        authoring tools themselves, are already gated the same way). Additional
        paths can still be declared in state['required_deliverables']
        regardless of this flag.
        """
        from ..evaluation.notebook_exec import required_deliverable_name
        from ..runtime import features
        from ..runtime.features import NOTEBOOK_TOOLS
        from ..runtime.study_config import deliverable_name
        study_dir = Path(state.get("study_dir", "."))
        # WriteDeliverable writes BARE names to study_dir/ (it rejects path
        # separators). Normalise any configured path to its basename so a stray
        # 'workspace/…' prefix in a study config can't spuriously flag a present
        # deliverable as missing.
        required = list(state.get("required_deliverables") or [])
        # Only a node that holds the notebook tools can author the notebook,
        # so only it can be required to have.
        if features.enabled("pipeline_deliverable") and (
                not self._tools_declared or self._agent_tools & NOTEBOOK_TOOLS):
            required = [required_deliverable_name()] + required
        seen: set[str] = set()
        present: list[str] = []
        missing: list[str] = []
        for p in required:
            name = deliverable_name(p)
            if name in seen:
                continue
            seen.add(name)
            (present if (study_dir / name).exists() else missing).append(name)
        return present, missing

    def _reproduction_gate(self, state: AgenticState | None = None) -> str | None:
        """Before Done() can close a run — and on every RunNotebook(gate=True)
        dry run — pipeline.ipynb must satisfy every one of these, checked in
        order. This is a MECHANICAL check this exact function executes every
        time, not a judgement call, and nothing narrative or intent-based can
        satisfy it in a check's place.

          0. The canonical store must already hold at least one oracle row.
             Zero rows means no campaign has been evaluated yet, so there is
             nothing for the notebook to reproduce FROM — the gate refuses
             outright, before even running the notebook.
          1. The notebook must finish cleanly within a time ceiling (no heavy
             from-scratch computation — a reproduction is lazy, not a re-run).
          2. It must add ZERO new oracle rows: it may only LOAD the ledger
             (``ExperimentData.from_file``) and reach the oracle through
             ``get_evaluator()``, which skips every already-FINISHED row.
          3. It must NOT modify or delete any existing ledger row — no faking
             a zero-delta by delete-then-re-add or by rewriting a value.
          4. The notebook prints a freshly-computed ``REPRODUCED: <value>``
             and the ``CLAIMED_HEADLINE: <value>`` its write-up states. The
             gate fails the notebook when the two differ. It does not fail
             a notebook for a missing print.

        Passing (0)-(4) means the deliverable is a faithful, lightweight,
        lazy reproduction of a real, already-evaluated campaign — not a
        script doing something unrelated to validating the pipeline.
        """
        # Developer notes (not part of the agent-facing contract above, see
        # gate_contract() below): returns None on PASS (and stashes
        # self._repro_ok_detail), else a problem string describing which
        # check failed. Skips silently when there is no run context; callable
        # without `state` (study dir comes from self._study_dir). (d)'s
        # REPRODUCED marker is informational for the critic/human — the
        # runtime does not gate on the value itself, only on (4)'s
        # self-consistency; an independent runtime extremum match used to
        # wrongly reject legitimate constrained optima.
        from ..runtime import features
        if not features.enabled("reproduction_gate"):
            return None

        import re
        import subprocess

        study_dir = (
            Path(self._study_dir) if getattr(self, "_study_dir", None) is not None
            else Path((state or {}).get("study_dir", "."))
        )
        # The deliverable is pipeline.ipynb. (A .py is still executable by the
        # executor-agnostic gate, kept only as a fallback for gate-logic tests;
        # the notebook is preferred when both are present.) Absence is left to
        # _missing_deliverables.
        deliverable = next(
            (study_dir / n for n in ("pipeline.ipynb", "pipeline.py")
             if (study_dir / n).exists()),
            None,
        )
        if deliverable is None:
            return None  # absence is handled by _missing_deliverables
        if deliverable.suffix == ".ipynb":
            # NOTEBOOK-LEDGER SYNC, by construction: refresh the hypotheses
            # cell's ledger-status block before anything else runs. This one
            # hook covers BOTH call sites that reach here — RunNotebook
            # (gate=True) and Done()'s pre-critic check — so a stale status
            # is impossible the moment either reads the notebook, rather
            # than merely detected once it's already been read.
            from .tools.routing.notebook import refresh_hypotheses_ledger_block
            # Reassigned every call (None on a no-op refresh) so a stale
            # rev from an EARLIER call is never re-surfaced by _gate_check.
            self._hypotheses_rev_after_refresh = refresh_hypotheses_ledger_block(
                study_dir, self._read_ledger())
        # One resolver for "where is this run", shared with the store tools:
        # they used to compute it separately and could disagree.
        if self._current_notes_dir is None:
            return None  # no run dir context (e.g. non-debug) — skip the gate
        run_dir = self._resolve_run_dir()          # …/runs/<id>
        store_dir = run_dir / "experiment_data"
        run_config = run_dir / "debug" / "run_config.json"

        from ..evaluation.notebook_exec import (
            ledger_snapshot as _ledger_snapshot,
        )

        # ── HERMETIC SANDBOX ──────────────────────────────────────────────────
        # CRITICAL: run the deliverable against a COPY of the canonical store, never
        # the live one. A faithful lazy pipeline adds nothing; a NON-lazy one
        # (re-evaluating) writes its evals into the THROWAWAY copy — we detect
        # that as "not lazy" while the real ledger stays pristine. Without this,
        # checking a non-lazy pipeline pollutes + inflates the canonical store
        # (and the gate check could be looped to balloon it without bound).
        before_n, before_hash = _ledger_snapshot(store_dir)
        if before_n == 0:
            return (
                "Canonical store has no rows — the campaign has not been "
                "evaluated yet. Run the delegation pipeline first so the "
                "ledger is populated, then the notebook can be reproduced "
                "lazily against those rows.")
        from ..evaluation.notebook_exec import replay_sandbox
        with replay_sandbox(
                store_dir, run_config, self._study_dir,
        ) as (sandbox, sb_store, env):
            _timeout = (
                max(0.1 * self._budget_seconds, 180.0)
                if self._budget_seconds else 300.0
            )
            try:
                # Executor-agnostic: a .ipynb runs via nbclient (in-env kernel),
                # a .py via subprocess — both return a CompletedProcess and raise
                # TimeoutExpired on timeout, so the asserts below are unchanged.
                from ..evaluation.notebook_exec import run_deliverable
                proc = run_deliverable(
                    deliverable, cwd=sandbox, env=env, timeout=_timeout)
            except subprocess.TimeoutExpired:
                return (
                    f"{deliverable.name} did not finish within {_timeout:.0f}s. A "
                    "reproduction must be lightweight — load the ledger and skip "
                    "finished evals and heavy refits (cache-or-load surrogates). "
                    "Make it lazy.")
            after_n, after_hash = _ledger_snapshot(sb_store)

        # (a) clean exit — surface a generous stderr tail for sighted debugging.
        if proc.returncode != 0:
            return (
                f"{deliverable.name} FAILED to run (exit {proc.returncode}). It must "
                "load the ledger and derive the headline cleanly. Stderr:\n"
                + (proc.stderr or "")[-3000:]
                + ("\n\nStdout tail:\n" + proc.stdout[-800:]
                   if proc.stdout else ""))
        # (b) zero new evals (lazy).
        if after_n != before_n:
            return (
                f"{deliverable.name} is NOT lazy: re-running it changed the ledger row "
                f"count ({before_n} → {after_n}). It must LOAD the ledger "
                "(ExperimentData.from_file) and reach the oracle only via "
                "get_evaluator() so FINISHED rows are skipped — zero new evals.")
        # (c) integrity — existing rows unchanged.
        if before_hash and after_hash and before_hash != after_hash:
            return (
                f"{deliverable.name} MODIFIED existing ledger rows. A reproduction must "
                "read the ledger READ-ONLY (it may re-store identical rows, but "
                "must not rewrite values or delete+re-add). Do not tamper with "
                "the canonical store.")
        # (d) The printed ``REPRODUCED:`` line is an informational headline
        # marker for the critic / human reader — the runtime no longer gates on
        # it. Headline GROUNDING (the value traces to a real ledger row) is
        # owned by the critic's HEADLINE PROVENANCE check; an independent
        # runtime extremum match wrongly rejected legitimate CONSTRAINED optima
        # (a constrained best is, by definition, not an objective extremum), so
        # it forced studies to headline their infeasible unconstrained extremum
        # — see audit run 20260624T021359.
        # (e) internal consistency — if the notebook declares CLAIMED_HEADLINE
        # (the value its write-up states), it must equal the freshly-computed
        # REPRODUCED. Lenient: skips when the marker is absent.
        _hc = _headline_consistency(proc.stdout or "")
        if _hc is not None:
            return _hc
        m = re.search(r"REPRODUCED:\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)",
                      proc.stdout or "")
        headline = f", REPRODUCED={m.group(1)}" if m else ""
        self._repro_ok_detail = (
            f"reproduced cleanly ({before_n} rows, unchanged, 0 new evals, "
            f"ran in <{_timeout:.0f}s{headline})")
        return None


def gate_contract() -> str:
    """The reproduction gate's exact preconditions, generated from
    ``ReproductionGateMixin._reproduction_gate``'s own docstring.

    Extracted live via ``inspect.cleandoc`` (the same idiom
    ``prompts/tool_catalog.py`` uses for every tool's agent-facing
    description) rather than duplicated by hand into a prompt template — the
    text an agent reads IS what the code enforces, so it cannot drift the way
    a hand-written paraphrase sitting beside the check can. Injected into the
    strategizer/implementer/critic system prompts by ``agent_runtime.py``,
    gated on the ``reproduction_gate`` feature (``runtime/features.py``).
    """
    import inspect
    return inspect.cleandoc(ReproductionGateMixin._reproduction_gate.__doc__)
