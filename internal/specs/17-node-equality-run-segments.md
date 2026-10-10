# 17 — A node's own work is evidence too: run segments, and no role-gated machinery

Status: spec for review. No code until reviewed. Owner decision (Elvis, 2026-10-10):
a node with no delegations closes a hypothesis by citing its own run segment.

## Why (the principle, not one arm)

"All nodes are equal." Today the epistemic machinery keys on facts about a node
that are not its tools or its prompt: whether it has outgoing edges, and which
role string its delegation target carries. A node that does its own work — a
strategizer with no workers, a specialist asked to run a loop alone — meets
refusals no reasonable reading of the spec justifies. Each edit below removes one
such key. None adds a feature for one study.

## The four blockers (verified against main 687b038, by reading the code)

1. **Ownership keys on edges.** `Node._init_orchestration` sets
   `_owns_epistemics = bool(outgoing) and notes_dir is not None`
   (`nodes/orchestration.py`). A node with no outgoing edge gets no ledger, no
   milestones, no science monitor. The edge test was added so a leaf does not
   gain a ledger merely because `graph_builder` hands every node the same
   `notes_dir`. `_install_epistemics` already narrows to the records whose tools
   the node holds, so the tool test alone is the right key.
2. **The closing rule cites only delegations.**
   `LedgerTools._check_cited_delegation` accepts `D000` (with a precomputed pool)
   or a `DONE` row in the delegation log. A node that works alone has no such row
   and cannot close any hypothesis.
3. **The evaluator needs a delegation id.** `oracle_resolution._resolve_delegation_id`
   reads `F3DASM_DELEGATION_ID` or a `D###` cwd name, else raises. The entry node
   has neither.
4. **Evaluator registration is wired to one role.**
   `DelegationRun._register_authored_evaluator` runs only when the delegated
   target's role is `datagenerator`. An entry node that authors the oracle itself
   has no registration path.

Two further role keys, same family:

5. `_LEDGER_GUARD_ROLES = {"implementer", "debugger"}` gates `enforce_ledger`
   (raw-oracle nudge, unledgered bounce, the duplicate check) by the *target's role
   string*, not by what the work did.
6. The checks in `epistemics/science_monitor.py` (`_check_unledgered`,
   `_check_duplicate_evaluations`, `UNSTAMPED_ROWS`) read per-delegation ids, so
   they never see work that has no delegation id.

## Design

### A. Run segment: the node's own work as a citable record

A **run segment** is a named, recorded stretch of a node's own work: scripts plus
outputs. It is logged exactly like a delegation row: an id, a status, a report.
It reuses `DelegationLog`; nothing parallel is built.

- Id: `S###` (own counter per node), so a reader can tell a segment from a
  delegation. The id regex in `oracle_resolution` and the log accept both.
- Lifecycle: `OpenSegment(title)` -> `RUNNING`; `CloseSegment(id, report)` ->
  `OPEN_FOR_REVIEW`; the node's own approval, or the critic's, -> `DONE`. The
  open/close pair is explicit. Inferring a segment from Bash calls would be a
  guess, so no inference.
- Workspace: `debug/delegations/S###/` holds the scripts and outputs, the same
  layout a delegation uses, so the viewer and the reproduction gate read it with no
  change.
- Open question for the boss: should a segment need approval (the delegator's
  `approve=True` step) or close straight to `DONE`? With no delegator, I propose
  `CloseSegment` writes `DONE` only when the report names the scripts and the
  numbers it cites; the validator then judges the substance as for any delegation.

### B. Citation check accepts segments (blocker 2)

`_check_cited_delegation` looks the cited id up in the same log; a `DONE` segment
passes like a `DONE` delegation. The single-source rule stays: one id per verdict.
The verdict validator receives the segment report as it receives a delegation
report. No new substance rule (§4: the validator's criteria stay as they are).

### C. Ownership follows tools (blocker 1)

`_owns_epistemics = notes_dir is not None`; `_install_epistemics` already keeps
only the records whose tools the node holds. Check that a leaf with no ledger tools
still ends with `_ledger is None`, so the original reason for the edge test holds.

### D. Evaluation id for the entry node (blocker 3)

An evaluation must stamp its provenance with the id of the unit of work that made
it. The entry node's work runs inside an open segment, so the backend injects
`F3DASM_DELEGATION_ID=S###` for that segment's Bash. With no open segment the
existing error stays, with the sentence on opening one. `D000` stays as is.

### E. Registration is by capability, not role (blocker 4)

Replace the `role == "datagenerator"` test with: the unit of work left a
`registration.json` in its `generators/` workspace. The role string carries no
information the file does not. The segment workspace is checked the same way.

### F. Ledger guards by evidence, not by role (blockers 5, 6)

`enforce_ledger` becomes: a canonical oracle is registered AND the unit of work has
the tools that evaluate (it can run code). `_LEDGER_GUARD_ROLES` is deleted. The
monitor reads segment ids as well as `D###` ids. Check first that no
non-evaluating role (literature reviewer, critic) now gets a false positive: those
never reach `get_evaluator()`, so the nudge never fires on them. The test below
pins this.

## Tests (named first, TDD)

1. `test_leaf_node_with_ledger_tools_owns_ledger`: no outgoing edge, ledger tools
   in the toolset, ledger present. `test_leaf_node_without_ledger_tools_owns_none`:
   same node, no ledger tools, `_ledger is None`.
2. `test_open_close_segment_logs_row`: a row with id, status, report; `query_all`
   returns it; the layout under `debug/delegations/S###/` exists.
3. `test_close_hypothesis_citing_done_segment_passes`; `..._citing_running_segment_refused`;
   `..._citing_two_segments_refused` (the single-source rule).
4. `test_cited_segment_report_reaches_validator`: the validator input contains the
   segment report.
5. `test_get_evaluator_inside_open_segment_stamps_segment_id`; without a segment the
   error names `OpenSegment`.
6. `test_registration_json_in_any_workspace_registers`: a non-datagenerator role with
   a manifest registers; a datagenerator with none does not.
7. `test_enforce_ledger_follows_tools_not_role`: a worker with another role string
   and the evaluating tools is guarded; a literature worker is not.
8. `test_science_monitor_sees_segment_rows`: unledgered and duplicate checks fire on
   a segment id.
9. Regression: the existing suites for delegations, ledger and monitor pass
   unchanged (`pytest -m "not integration and not ollama and not corpus and not promptmap and not docling"`, then `-m promptmap`).
10. `internal/FEATURES.md` entries for `OpenSegment` and `CloseSegment`
    (`tests/test_features_documented.py` enforces the tool half).

## Done when (KPI)

A one-node study (entry node, no workers, ledger and evaluation tools) runs the
headless smoke suite to a closed hypothesis that cites its own `DONE` segment, with
zero `ERROR_RETURN` from the four blockers above, and every existing study's
headless result is unchanged (same ledger rows, same gate outcome).

## Deferred / not decided here

- The single-node prompt: the boss writes it.
- Whether approval of a segment needs a second party (see A).
- The strategizer-prompt passages that assume workers exist: the boss's prompt
  covers those; they are not code.
