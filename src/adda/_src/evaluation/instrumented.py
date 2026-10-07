"""Instrumented DataGenerator: canonical concurrency-safe eval ledger.

Provides :class:`InstrumentedDataGenerator`, a wrapper that:

1. Delegates execution to an inner ``DataGenerator``.
2. Stamps provenance metadata (delegation_id, source, UTC timestamp)
   onto each returned ``ExperimentSample``.
3. Buffers samples and flushes them to a shared ``ExperimentData`` store
   under a ``FileLock`` so concurrent delegations cannot corrupt the
   ledger.

The factory that builds a configured instance from ``run_config.json`` is
:func:`.oracle_resolution.get_evaluator`; the read-only ledger summaries
live in :mod:`.ledger_summary`.

The ``fidelity_column`` parameter is accepted but unused; it exists for
forward-compatibility when fidelity-aware stamping is added.
"""
from __future__ import annotations

#                                                                      Modules
# ==========================================================================
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from f3dasm import DataGenerator, ExperimentData, ExperimentSample

# Not yet re-exported by the public f3dasm API. They exist in stock f3dasm,
# only under _src. Flip to `from f3dasm import ...` once bessagroup/f3dasm#351
# lands and is pinned.
from f3dasm._src.errors import EmptyFileError, ReachMaximumTriesError
from f3dasm.design import Domain
from filelock import FileLock

#                                                         Authorship & Credits
# ==========================================================================
__author__ = "Elvis Aguero (elvis_alexander_aguero_vera@brown.edu)"
__credits__ = ["Elvis Aguero"]
__status__ = "Experimental"
# ==========================================================================

# Columns the wrapper stamps on every ledgered row. Everything else in an
# output frame is a real output; everything else in an input frame is a
# design coordinate.
_PROVENANCE_COLS = frozenset({
    "_delegation_id", "_source", "_ts", "_wall_ms", "_oracle_rev"})


def _known_rev(v) -> str | None:
    """A stored ``_oracle_rev`` as a string, or None when the row has none
    (written before revisions were stamped, or by a seeding path)."""
    return v if isinstance(v, str) and v else None


def _round_coord(v):
    """The one float-tolerance rule every coordinate-key comparison in this
    module uses: 10dp, unchanged by the submitted-key fix below — the
    known float-precision behavior stays exactly what it was."""
    try:
        return round(float(v), 10)
    except (TypeError, ValueError):
        return v


def _row_matches_submitted_key(key: tuple, row: dict) -> bool:
    """True iff ``row`` (a dict of column -> value, e.g. one canonical store
    row) agrees with ``key`` (an ``InstrumentedDataGenerator._coord_key``-
    shaped tuple) on every column the key names -- ignoring any OTHER
    column ``row`` happens to carry.

    This is the asymmetric half of the submitted-key fix: a canonical row
    written when an evaluator stamped an extra column (e.g. a solver
    config kwarg) still has that column forever, but the CALLER's
    identity for a design was only ever what it submitted. A stored row
    with an extra stamped column must still be found by a submitted key
    that does not mention it -- the opposite of exact-tuple equality,
    which is why this can't reuse ``==`` against ``_coord_key(row)``."""
    for col, val in key:
        if col not in row:
            return False
        if _round_coord(row[col]) != val:
            return False
    return True


# ==========================================================================


class InstrumentedDataGenerator(DataGenerator):
    """Wrap an inner DataGenerator, stamp provenance, and flush to disk.

    Parameters
    ----------
    inner : DataGenerator
        The wrapped evaluator.  Callers are responsible for decorating a
        plain function with ``@datagenerator`` before passing it here.
    store_dir : Path or str
        Canonical project_dir — the directory that *contains*
        ``experiment_data/``.  Every delegation shares the same
        ``store_dir`` so their rows end up in one ledger.
    delegation_id : str
        Identifier for this delegation, e.g. ``"D003"``.
    source : str, optional
        Human-readable label stamped in the ``source`` provenance column
        (e.g. the evaluator name).  Default ``""``.
    fidelity_column : str or None, optional
        Name of the study's fidelity input column if any.  Unused in
        Phase 1 — accepted only for forward-compatibility.
    lock_path : Path or str or None, optional
        Path for the ``FileLock``.  Defaults to
        ``<store_dir>/experiment_data/.lock``.
    flush_every : int, optional
        Number of samples to buffer before a locked flush.  Default 1
        (flush on every execute).
    dedup_scope : str, optional
        ``"delegation"`` (default): dedup-on-write only recognizes a design
        as already-seen if THIS delegation itself wrote it — a genuinely
        concurrent campaign delegation may legitimately re-measure a design
        another delegation also measured; collapsing across delegations
        would corrupt that. ``"all"``: dedup against the WHOLE ledger
        regardless of which delegation wrote each row — the correct
        semantics for a validation replay of already-generated data (e.g.
        the reproduction gate re-executing a notebook's ``data_generation``
        cell, which must add ZERO new rows to satisfy the "LAZY"
        reproduction invariant) rather than a live campaign delegation
        genuinely exploring in parallel.
    oracle_rev : str or None, optional
        Revision of the registered oracle's source (see
        ``oracle_resolution.oracle_revision``), stamped on every row as
        ``_oracle_rev``. Under ``dedup_scope="delegation"`` a stored row only
        counts as already-seen when it came from the SAME revision: after the
        oracle changes, the same design is a new evaluation, not a repeat.
        Rows with no stored revision match any revision.
    """

    def __init__(
        self,
        inner: DataGenerator,
        store_dir: Path | str,
        delegation_id: str,
        *,
        source: str = "",
        fidelity_column: Optional[str] = None,
        lock_path: Optional[Path | str] = None,
        flush_every: int = 1,
        extra_provenance: Optional[dict] = None,
        eval_budget: Optional[int] = None,
        dedup_scope: str = "delegation",
        oracle_rev: Optional[str] = None,
    ) -> None:
        self.inner = inner
        self.oracle_rev = oracle_rev
        self.store_dir = Path(store_dir)
        if dedup_scope not in ("delegation", "all"):
            raise ValueError(
                f"dedup_scope must be 'delegation' or 'all', got {dedup_scope!r}")
        self.dedup_scope = dedup_scope
        self.delegation_id = delegation_id
        self.source = source
        # SOFT eval-budget governor (resource-governance L1). Fires at the flush
        # boundary — i.e. MID-delegation, where the strategizer's turn-gated check
        # is blind. eval_budget is soft (§4): it NUDGES the offender, never stops
        # the campaign. `_nudge_bands_hit` caps the nudge at one per threshold band
        # (flush_every defaults to 1 → per-row → uncapped would spam).
        self.eval_budget = eval_budget
        self._nudge_bands_hit: set[int] = set()
        # Cumulative count of dedup-on-write skips across this delegation's
        # lifetime. Real compute was spent on each one even though no row
        # landed, so the SOFT budget nudge (which exists to protect against
        # burning real compute) must count it alongside store rows -- the
        # canonical evals_used tally stays store-row-based on purpose
        # (delegation.py's _reconcile_evals: "believe the store, not the
        # worker's self-report"), this only feeds the soft nudge.
        self._dedup_skipped_total = 0
        self._dedup_skipped_revs: set[str] = set()
        self._skip_notes: list[tuple] = []
        self._rev_changed: list[tuple] = []
        self.fidelity_column = fidelity_column  # unused Phase 1
        self.flush_every = flush_every
        # Extensible, oracle-stamped provenance: arbitrary {column: value}
        # declared per run (config 'provenance' block, or set by the runtime).
        # Stamped into EVERY evaluated row at the metered call — so the schema
        # is open (any future problem can add columns: fidelity, regime, seed,
        # mesh, …) and the VALUES come from the oracle wrapper, never from the
        # agent (which keeps the audit trail trustworthy).
        self.extra_provenance: dict = dict(extra_provenance or {})

        if lock_path is None:
            lock_path = (
                self.store_dir / "experiment_data" / ".lock"
            )
        self.lock_path = Path(lock_path)

        self._buffer: list[ExperimentSample] = []
        # Parallel to self._buffer: each entry's SUBMITTED coord key, captured
        # in execute() BEFORE inner.execute runs -- never recomputed from the
        # buffered sample afterward, which could include a column the
        # evaluator stamped as a side effect (see _coord_key's own docstring).
        self._buffer_keys: list[tuple] = []
        # Designs (coord keys) queued for SUPERSEDE via supersede(): their stale
        # canon rows are dropped at flush so a corrected re-eval replaces them.
        self._supersede_keys: set = set()

    # ------------------------------------------------------------------

    def call(self, data, mode: str = "sequential", pass_id: bool = False,
              **kwargs):
        """Same as f3dasm's ``DataGenerator.call`` — EXCEPT ``mode="parallel"``
        is refused outright, never delegated to ``super().call()``.

        f3dasm's own ``mode="parallel"`` falls through to a LOCAL
        ``multiprocessing.Pool`` — it spawns every solve as a subprocess on
        THIS process's own host, which in this architecture is the run's
        shared, resource-constrained orchestration node (not a SLURM
        allocation). N concurrent Abaqus solves there is CPU oversubscription
        and OOM that kills the whole run, not just this evaluation — a
        documentation warning telling agents "never use this" is not a
        control; a host-safety hard cap that never lets the call start is
        (mem_cap_bytes is the other one, §4 of the working contract — a run
        must not be able to OOM the shared node it runs on). Real
        parallelism belongs on a cluster scheduler (e.g. one evaluation per
        SLURM array task, each with its own node's resources) — whatever
        submission helper the study provides for that — never a local pool.
        """
        if mode == "parallel":
            raise ValueError(
                "gen.call(mode='parallel', ...) is disallowed — it falls "
                "through to f3dasm's local multiprocessing.Pool, spawning "
                "every solve as a subprocess on THIS run's own shared "
                "orchestration node (CPU oversubscription + OOM that kills "
                "the whole run, not just this evaluation). Use "
                "mode='sequential' here; get real parallelism through the "
                "study's cluster-array submission path (one evaluation per "
                "SLURM array task, each with its own node's resources) "
                "instead."
            )
        return super().call(data, mode=mode, pass_id=pass_id, **kwargs)

    def execute(
        self, experiment_sample: ExperimentSample, **kwargs
    ) -> ExperimentSample:
        """Run inner generator, stamp provenance, buffer, maybe flush.

        Parameters
        ----------
        experiment_sample : ExperimentSample
            Sample to evaluate.
        **kwargs
            Forwarded to ``inner.execute``.

        Returns
        -------
        ExperimentSample
            The evaluated sample (with provenance stamped into
            ``_output_data``).
        """
        # Captured BEFORE inner.execute runs -- an evaluator that stamps a
        # solver-config kwarg into experiment_sample._input_data as a side
        # effect must not make that column part of this design's identity
        # (see _coord_key's own docstring; real bug, run 20260927T012131).
        _submitted_key = self._coord_key(experiment_sample._input_data)
        _t0 = time.perf_counter()
        out = self.inner.execute(experiment_sample, **kwargs)
        _wall_ms = (time.perf_counter() - _t0) * 1000.0

        # Stamp provenance into the output dict.
        ts = datetime.now(tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        out._output_data["_delegation_id"] = self.delegation_id
        out._output_data["_source"] = self.source
        out._output_data["_ts"] = ts
        # Generic per-eval wall-time (ms). A plain underscore-prefixed column:
        # to_numpy() drops it and it's excluded from value stats. Any grouping
        # (per-phase, per-fidelity) is df.groupby(col)["_wall_ms"] downstream —
        # no timing-specific code special-cases a dimension here.
        out._output_data["_wall_ms"] = round(_wall_ms, 3)
        if self.oracle_rev:
            out._output_data["_oracle_rev"] = self.oracle_rev
        # Extensible declared provenance (oracle-stamped, not agent-authored).
        for _col, _val in self.extra_provenance.items():
            out._output_data[_col] = _val

        # The inner generator returned normally, so this evaluation COMPLETED:
        # stamp FINISHED on the copy we buffer for the canonical store. f3dasm's
        # _run_sample marks finished on the agent's *working* ExperimentData, not
        # on this buffered deepcopy — without this, completed rows persist as
        # IN_PROGRESS in the canonical jobs.csv, defeating the FINISHED-regression
        # store guard, is_all_finished(), and resumption logic. (Errors raise out
        # of inner.execute before this line and are marked elsewhere.)
        out.mark("finished")

        self._buffer.append(deepcopy(out))
        self._buffer_keys.append(_submitted_key)

        if len(self._buffer) >= self.flush_every:
            self._flush()

        return out

    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Flush any remaining buffered samples to the store.

        Call at the end of a delegation to ensure no samples are lost.
        """
        if self._buffer:
            self._flush()

    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Flush the current buffer to disk under a FileLock.

        The entire read → merge → write sequence is executed inside the
        lock so concurrent threads/processes cannot interleave.
        """
        if not self._buffer:
            return

        # Ensure the lock parent directory exists.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        n_skipped = 0
        _n_total = 0
        with FileLock(str(self.lock_path)):
            # Absent store (FileNotFoundError) OR a torn/empty CSV from an
            # interrupted prior write (EmptyFileError / retry-exhausted):
            # treat as fresh and let this locked write heal it. We do NOT
            # catch broader errors — a populated store that fails to parse
            # must propagate, never be silently overwritten with the batch.
            try:
                canon = ExperimentData.from_file(
                    project_dir=self.store_dir
                )
            except (
                FileNotFoundError,
                EmptyFileError,
                ReachMaximumTriesError,
            ):
                canon = None

            # Supersede (opt-in correction): drop the stale rows for any design
            # queued via supersede() BEFORE dedup, so the corrected re-eval
            # REPLACES them instead of being skipped as a duplicate. Net-count-
            # preserving (old row out, new row in) so the PROTECTED-store guard
            # still holds (it blocks only SHRINK and FINISHED-regression).
            if canon is not None and self._supersede_keys:
                canon = self._drop_canon_rows(canon, self._supersede_keys)

            # Dedup-on-write: drop buffered samples whose design (the
            # non-provenance input coords, rounded 10dp — the SAME key the
            # DUPLICATE_EVALUATION detector uses) is already in the canonical
            # store, or repeated within this batch. Keep-first: an existing row
            # is never mutated (correcting a stale row is a separate, opt-in
            # affordance, deliberately out of scope). This stops a re-launched
            # or retried campaign from re-appending designs already evaluated —
            # the retry-duplication that burned ~30h/2-designs in an earlier run.
            # NOTE: this hard-prevents a duplicate row (the DUPLICATE_EVALUATION
            # signal was previously only a soft nudge); safe because the oracle
            # is deterministic, so a repeat is pure waste, not a confirm-probe.
            n_skipped = self._drop_duplicate_buffer(canon)

            if self._buffer:
                batch_domain = self._build_batch_domain()
                batch = self._build_batch_experimentdata(batch_domain)
                if canon is None:
                    canon = ExperimentData(domain=batch_domain)

                # Ensure provenance columns are declared on the canon domain
                # (the fixed four + any extensible declared columns).
                for col in ("_delegation_id", "_source", "_ts", "_wall_ms",
                            *(("_oracle_rev",) if self.oracle_rev else ()),
                            *self.extra_provenance):
                    canon._domain.add_output(col, exist_ok=True)

                # Re-type the batch's input parameters to the canonical domain's:
                # the canonical store is authoritative on types. _build_batch_domain
                # declares inputs as untyped base Parameter(); merging one into a
                # typed canonical param (e.g. add_int → DiscreteParameter) raises
                # "Cannot add non-continuous parameter to continuous!". Adopting the
                # canonical typed param makes the per-key merge typed+typed.
                for key, cparam in canon._domain.input_space.items():
                    if key in batch._domain.input_space:
                        batch._domain.input_space[key] = cparam

                merged = canon + batch
                merged.store(project_dir=self.store_dir)
                _n_total = len(merged)
            elif canon is not None:
                _n_total = len(canon)

        # SOFT eval-budget nudge (lock released): fire MID-delegation at the eval
        # boundary, to THE OFFENDER (this campaign's own stdout → the implementer's
        # delegation report). Never stops the campaign; capped at one per band.
        if n_skipped:
            self._dedup_skipped_total += n_skipped
        if _n_total or self._dedup_skipped_total:
            self._maybe_nudge_budget(_n_total + self._dedup_skipped_total)
        if n_skipped:
            self._record_dedup(n_skipped)
            self._notify_dedup_skips()
        self._skip_notes.clear()
        if self._rev_changed:
            self._notify_rev_changed()

        self._buffer.clear()
        self._buffer_keys.clear()
        self._supersede_keys.clear()

    @staticmethod
    def _coord_key(input_data: dict) -> tuple:
        """Order-independent design key: (col, value) pairs over the
        non-provenance inputs, values rounded to 10dp — the same convention
        ``duplicate_eval_stats`` and the reproduction gate use, so dedup-on-write
        matches duplicate DETECTION exactly.

        MUST be called on ``input_data`` as the CALLER submitted it, before
        ``inner.execute`` runs — never on a row already written to the
        canonical store, and never on a buffered sample after execute() has
        returned. An evaluator that stamps a solver-config kwarg into
        ``experiment_sample._input_data`` as a side effect (e.g. ``if
        "num_eigen" in kwargs: sample._input_data["num_eigen"] = ...``) makes
        that column present ONLY after execute() -- a key computed from the
        post-execute sample would include it, but the key that gets matched
        against (see ``_row_matches_submitted_key``) must not, or a corrected
        re-evaluation with a different stamped value can never match its own
        stale row (real bug, run 20260927T012131: supersede() silently left
        the stale row in place because its own tracked key, captured after
        the stamp, no longer equaled the stored row's)."""
        return tuple(sorted(
            (str(k), _round_coord(v)) for k, v in input_data.items()
            if k not in _PROVENANCE_COLS))

    def _drop_duplicate_buffer(self, canon) -> int:
        """Filter ``self._buffer`` in place to designs not already present in
        ``canon`` and not repeated earlier in the buffer (keep-first). Returns
        the count dropped. Best-effort: any failure leaves the buffer intact so
        the eval path never loses data to a dedup bug.

        Matches canon rows via ``_row_matches_submitted_key`` (subset match
        on each buffered sample's PRE-STAMP ``self._buffer_keys`` entry, not
        a freshly-recomputed key from the possibly-stamped buffered sample)
        for the same reason ``_drop_canon_rows`` (supersede) does: a re-
        execute of the same submitted design with a DIFFERENT evaluator-
        stamped kwarg must still be recognised as a duplicate of what was
        SUBMITTED, or a retried/relaunched campaign re-appends it as a
        "new" design every time — the retry-duplication class that burned
        ~30h/2-designs in an earlier run, now reachable again through any
        evaluator that stamps kwargs (run 20260927T012131)."""
        try:
            canon_rows: list[dict] = []
            canon_outputs: list[dict] = []
            canon_revs: list[str | None] = []
            self._dedup_skipped_revs = set()
            self._skip_notes = []
            self._rev_changed = []
            if canon is not None:
                df_in, df_out = canon.to_pandas()
                if df_in is not None and not df_in.empty:
                    # Per-delegation dedup — matches duplicate_eval_stats's own
                    # per-delegation definition of "duplicate". Only THIS
                    # delegation's prior rows count: a DIFFERENT delegation
                    # legitimately re-measuring the same design is not waste
                    # (and collapsing it would corrupt concurrent campaigns).
                    # dedup_scope="all" (reproduction-gate/deliverable replay
                    # of already-generated data, stamped with a synthetic id
                    # that never matches the real generating delegation(s))
                    # skips this narrowing entirely — every row in the
                    # ledger counts as already-seen, regardless of who wrote
                    # it, since the whole point of that replay is to add
                    # ZERO new rows.
                    if (self.dedup_scope == "delegation"
                            and "_delegation_id" in df_out.columns):
                        mine = (df_out["_delegation_id"].astype(str)
                                == str(self.delegation_id)).to_numpy()
                        df_in = df_in[mine]
                        df_out = df_out[mine]
                    canon_revs = (
                        [_known_rev(v) for v in df_out["_oracle_rev"]]
                        if "_oracle_rev" in df_out.columns
                        else [None] * len(df_out))
                    cols = [c for c in df_in.columns
                            if c not in _PROVENANCE_COLS]
                    canon_rows = df_in[cols].to_dict("records")
                    out_cols = [c for c in df_out.columns
                                if c not in _PROVENANCE_COLS]
                    canon_outputs = df_out[out_cols].to_dict("records")
            survivors: list[ExperimentSample] = []
            survivor_keys: list[tuple] = []
            seen_in_batch: set = set()
            seen_in_batch_outputs: dict[tuple, dict] = {}
            # A stored row from a DIFFERENT oracle revision is not the same
            # evaluation: it does not shadow this one (it stays in the store,
            # distinguishable by its _oracle_rev). Only a delegation-scoped
            # campaign is revision-aware; a replay (scope "all") adds no rows.
            revision_aware = (
                self.dedup_scope == "delegation" and bool(self.oracle_rev))
            for s, k in zip(self._buffer, self._buffer_keys, strict=True):
                stored_outputs = seen_in_batch_outputs.get(k)
                stored_rev = self.oracle_rev if stored_outputs is not None else None
                superseded_rev = None
                if stored_outputs is None:
                    for row, out_row, rev in zip(
                            canon_rows, canon_outputs, canon_revs, strict=True):
                        if not _row_matches_submitted_key(k, row):
                            continue
                        if (revision_aware and rev is not None
                                and rev != self.oracle_rev):
                            superseded_rev = rev
                            continue
                        stored_outputs, stored_rev = out_row, rev
                        break
                if k in seen_in_batch or stored_outputs is not None:
                    self._skip_notes.append(
                        (k, s._output_data, stored_outputs, stored_rev))
                    if stored_rev:
                        self._dedup_skipped_revs.add(stored_rev)
                    continue
                if superseded_rev is not None:
                    self._rev_changed.append((k, superseded_rev))
                seen_in_batch.add(k)
                seen_in_batch_outputs[k] = {
                    c: v for c, v in s._output_data.items()
                    if c not in _PROVENANCE_COLS}
                survivors.append(s)
                survivor_keys.append(k)
            n = len(self._buffer) - len(survivors)
            self._buffer = survivors
            self._buffer_keys = survivor_keys
            return n
        except Exception:  # noqa: BLE001
            return 0

    def _skip_summary(self) -> dict | None:
        """Aggregate of the skips collected by the last dedup pass: counts, the
        stored revisions, and the first few skipped designs with whether the
        discarded output differs from the stored row. ``None`` when nothing
        was skipped."""
        notes = self._skip_notes
        if not notes:
            return None
        examples, n_differs = [], 0
        for key, new_outputs, stored_outputs, stored_rev in notes:
            new_clean = {c: v for c, v in new_outputs.items()
                         if c not in _PROVENANCE_COLS}
            differs = stored_outputs is not None and (
                any(_round_coord(new_clean.get(c)) != _round_coord(v)
                    for c, v in stored_outputs.items())
                or set(new_clean) != set(stored_outputs))
            n_differs += bool(differs)
            if len(examples) < 3 and (differs or not examples):
                examples.append({
                    "design": {c: v for c, v in key}, "differs": bool(differs),
                    "new": new_clean if differs else None,
                    "stored": stored_outputs if differs else None,
                    "stored_rev": stored_rev})
        return {"n_skipped": len(notes), "n_differs": n_differs,
                "examples": examples}

    def _notify_dedup_skips(self) -> None:
        """Never let a computed-but-discarded evaluation pass silently: tell
        the calling agent's own script, via the same stdout channel
        ``_maybe_nudge_budget`` uses (Channel 1 — the campaign's OWN stdout,
        captured into the offender's delegation report). ONE bounded line per
        flush, however many designs were skipped: a line per design made a
        150-design campaign print 185 KB, which the CLI cut to a 2 KB preview
        that never reached the warning. Best-effort: a notice must never break
        the eval path."""
        try:
            summ = self._skip_summary()
            if summ is None:
                return
            n, nd = summ["n_skipped"], summ["n_differs"]
            ex = next((e for e in summ["examples"] if e["differs"]),
                      summ["examples"][0])
            design = ", ".join(f"{c}={v}" for c, v in ex["design"].items())
            msg = (
                f"[EVAL NOT STORED — {self.delegation_id}] {n} evaluation(s) "
                "were NOT written: their design already has a row in the "
                "canonical store (dedup-on-write keeps the first row for a "
                "design and never mutates it). The compute still counts "
                "against this delegation's SOFT eval-budget but adds no row, "
                "so it is not in the store's evals_used count. "
                + (f"NEW output differs from the STORED row for {nd} of "
                   f"the {n}. " if nd else "Every output matches its stored "
                   "row. ")
                + "If this was a deliberate correction, call supersede(...) "
                "instead of execute() to REPLACE the stored row."
            )
            revs = sorted(self._dedup_skipped_revs)
            if revs:
                msg += (
                    f" Stored by oracle revision {', '.join(revs)}"
                    f" (this run: {self.oracle_rev}). Editing the oracle's "
                    "source is not a correction in this sense: a changed "
                    "revision is stored as a new row beside the old one, so "
                    "a skip means the oracle source did not change.")
            msg += f" Example: ({design})"
            if ex["differs"]:
                msg += f" new={ex['new']!r}, stored={ex['stored']!r}"
            if len(msg) > 1500:
                msg = msg[:1500] + " …"
            print(msg, flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _notify_rev_changed(self) -> None:
        """One line per flush: designs stored again because the oracle's
        source changed since their stored row. Best-effort, like every notice."""
        try:
            n = len(self._rev_changed)
            key, old = self._rev_changed[0]
            design = ", ".join(f"{c}={v}" for c, v in key)
            old_revs = sorted({r for _, r in self._rev_changed})
            print(
                f"[ORACLE CHANGED — {self.delegation_id}] {n} design(s) "
                f"already have a row from oracle revision "
                f"{', '.join(old_revs)}; this evaluation ran revision "
                f"{self.oracle_rev}, so it was stored as a new row. The older "
                "row stays in the store, marked by its _oracle_rev column, "
                f"and came from a superseded oracle. First: ({design}).",
                flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _record_dedup(self, n_skipped: int) -> None:
        """Best-effort DEDUP_SKIPPED audit line (never breaks the eval path)."""
        try:
            import json as _json
            from datetime import datetime, timezone
            diag = self.store_dir.parent / "debug" / "diagnostics.jsonl"
            if diag.parent.exists():
                rec = {
                    "ts": datetime.now(tz=timezone.utc).isoformat(
                        timespec="seconds"),
                    "node": self.delegation_id,
                    "error_type": "DEDUP_SKIPPED",
                    "message": (
                        f"{n_skipped} buffered eval(s) skipped: design already "
                        "in the store (dedup-on-write)"
                        + (f"; stored by oracle revision "
                           f"{', '.join(sorted(self._dedup_skipped_revs))}"
                           if self._dedup_skipped_revs else "")),
                    **({"detail": summ} if (summ := self._skip_summary())
                       else {}),
                }
                with diag.open("a", encoding="utf-8") as f:
                    f.write(_json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------

    def supersede(self, experiment_sample: ExperimentSample, **kwargs):
        """Re-evaluate a design and REPLACE its existing FINISHED store row(s).

        The opt-in correction for a stale/wrong row (e.g. a pre-oracle-fix read):
        dedup-on-write otherwise keep-firsts, so a plain re-eval would be dropped.
        This runs the oracle fresh and, at flush, drops the design's prior canon
        row(s) and writes the new one — net-count-preserving, so the PROTECTED-
        store guard still holds (it blocks only SHRINK and FINISHED-regression,
        neither of which a same-design replace does). Use sparingly; the store
        is otherwise append-only by design."""
        self._supersede_keys.add(
            self._coord_key(experiment_sample._input_data))
        out = self.execute(experiment_sample, **kwargs)
        self.flush()   # force: the drop + the new row must land in one write
        # Provenance-mutating op → leave an audit line (the old value is replaced
        # in-ledger, so the FACT of the correction must be traceable).
        self._record_supersede(dict(experiment_sample._input_data))
        return out

    def _record_supersede(self, input_data: dict) -> None:
        """Best-effort SUPERSEDE audit line (never breaks the eval path)."""
        try:
            import json as _json
            from datetime import datetime, timezone
            diag = self.store_dir.parent / "debug" / "diagnostics.jsonl"
            if diag.parent.exists():
                coords = {k: v for k, v in input_data.items()
                          if k not in _PROVENANCE_COLS}
                rec = {
                    "ts": datetime.now(tz=timezone.utc).isoformat(
                        timespec="seconds"),
                    "node": self.delegation_id,
                    "error_type": "SUPERSEDE",
                    "message": (
                        f"re-evaluated and REPLACED the store row for design "
                        f"{coords} (prior value overwritten in-ledger)"),
                }
                with diag.open("a", encoding="utf-8") as f:
                    f.write(_json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _drop_canon_rows(self, canon, keys: set):
        """canon minus every row that MATCHES (subset match, via
        ``_row_matches_submitted_key`` -- not exact-tuple equality) one of
        ``keys``, rebuilt via the same from_data(samples, domain) idiom the
        flush path uses. Kept rows are re-stamped FINISHED (they were) so
        the FINISHED-regression guard passes. Best-effort: on any error
        returns canon unchanged (the new row then merely appends — visible
        and safe, never lost).

        Subset match, not ``_coord_key(row) == key``: a canon row's OWN
        recorded columns can include one an evaluator stamped as a side
        effect (e.g. a solver config kwarg written into
        ``experiment_sample._input_data`` during ``inner.execute``), which
        was never part of what the caller SUBMITTED and so is never part of
        a key in ``keys`` either (those are captured pre-stamp — see
        ``execute``/``supersede``). Comparing on exactly the key's own
        columns, ignoring whatever else the row carries, is what lets a
        corrected re-evaluation with a DIFFERENT stamped value still find
        and replace its own stale row (real bug, run 20260927T012131: the
        old exact-tuple match never found it, so the stale row survived
        alongside the corrected one)."""
        try:
            df_in, df_out = canon.to_pandas()
            if df_in is None or df_in.empty:
                return canon
            in_cols = list(df_in.columns)
            out_cols = list(df_out.columns)
            samples: dict[int, ExperimentSample] = {}
            j = 0
            for i in range(len(df_out)):
                row = df_in.iloc[i].to_dict()
                if any(_row_matches_submitted_key(k, row) for k in keys):
                    continue
                s = ExperimentSample(
                    _input_data={c: df_in.iloc[i][c] for c in in_cols},
                    _output_data={c: df_out.iloc[i][c] for c in out_cols})
                s.mark("finished")
                samples[j] = s
                j += 1
            return ExperimentData.from_data(
                data=samples, domain=canon._domain)
        except Exception:  # noqa: BLE001
            return canon

    # Soft eval-budget nudge bands (fraction of eval_budget). One nudge per band
    # max → at most 3 nudges per delegation (the fixed cap).
    _NUDGE_BANDS = (0.8, 1.0, 1.5)

    def _maybe_nudge_budget(self, n_total: int) -> None:
        """SOFT, capped, offender-directed eval-budget nudge. Best-effort: a
        governor must never break the eval path, so it swallows everything."""
        try:
            budget = self.eval_budget
            if not budget or budget <= 0:
                return
            # Bands crossed by this flush that we haven't nudged yet.
            crossed = [
                int(b * 100) for b in self._NUDGE_BANDS
                if n_total >= b * budget and int(b * 100) not in self._nudge_bands_hit
            ]
            if not crossed:
                return
            # Mark ALL crossed bands hit (so a big batch that jumps two bands
            # still nudges only once) and nudge for the highest.
            self._nudge_bands_hit.update(crossed)
            pct = round(100 * n_total / budget)
            msg = (
                f"[EVAL BUDGET — {self.delegation_id}] {n_total}/{budget} ledgered "
                f"evals ({pct}% of the SOFT budget). The budget is soft (not "
                "enforced), but this is the SHARED canonical store: every campaign "
                "re-run APPENDS to it, so re-running a full campaign to debug burns "
                "the budget fast. Debug on RunScratch / a stub, not the real oracle; "
                "re-plan rather than spend more real evaluations."
            )
            # Channel 1 — the campaign's OWN stdout → captured into the offender's
            # (implementer's) delegation report. This is the cross-process path to
            # the offender (the governor runs in the campaign subprocess).
            print(msg, flush=True)
            # Channel 2 — a BUDGET_WARN diagnostic line for the audit trail ONLY
            # (NOT the nudge). store_dir is <run_dir>/experiment_data.
            try:
                import json as _json
                from datetime import datetime, timezone
                diag = self.store_dir.parent / "debug" / "diagnostics.jsonl"
                if diag.parent.exists():
                    rec = {
                        "ts": datetime.now(tz=timezone.utc).isoformat(
                            timespec="seconds"),
                        "node": self.delegation_id,
                        "error_type": "BUDGET_WARN",
                        "message": f"{n_total}/{budget} evals ({pct}%)",
                    }
                    with diag.open("a", encoding="utf-8") as f:
                        f.write(_json.dumps(rec) + "\n")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _build_batch_domain(self) -> Domain:
        """Build a Domain that covers inner inputs + outputs + provenance cols.

        Input columns are declared as base Parameter() (no bounds). This is
        intentional: the batch domain is merged with the canonical store's
        domain on every flush, which already carries the correct typed
        parameters (ContinuousParameter with bounds, etc.). Declaring the
        input keys here ensures that on the very first flush — when no
        canonical store exists yet — the written domain.json at least has
        the input column names, preventing a later from_file() load from
        silently omitting them and causing samplers to no-op without error.
        """
        d = Domain()
        # Declare input columns (base Parameter — bounds come from the
        # canonical domain on merge, not from this batch).
        all_input_keys: set[str] = set()
        for sample in self._buffer:
            all_input_keys.update(sample._input_data.keys())
        for key in sorted(all_input_keys):
            # Public API for a bounds-less base input column (constructs the
            # Parameter and registers it, replacing the private d._add).
            d.add_parameter(key)
        # Collect all output keys from the buffer.
        all_keys: set[str] = set()
        for sample in self._buffer:
            all_keys.update(sample._output_data.keys())
        for key in sorted(all_keys):
            d.add_output(key, exist_ok=True)
        return d

    def _build_batch_experimentdata(
        self, domain: Domain
    ) -> ExperimentData:
        """Build an ExperimentData from the current buffer."""
        data: dict[int, ExperimentSample] = {
            i: sample for i, sample in enumerate(self._buffer)
        }
        return ExperimentData.from_data(data=data, domain=domain)


__all__ = [
    "InstrumentedDataGenerator",
]
