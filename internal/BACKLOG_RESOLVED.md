# Agentic system — resolved backlog

The archive for [`BACKLOG.md`](BACKLOG.md). Every item here is fixed; the
write-ups stay because they carry the evidence, the mechanism and the commit
that closed them, and because several were found only by reading a run that has
since been wiped.

Nothing here needs action. If an item below starts misbehaving again, open a new
number rather than reopening an old one — the write-up is the record of what was
true when it was closed.

## Resolved

- [x] **#60** Sandboxed Write routes a required deliverable to the wrong directory — *RESOLVED (`2afcad8`)* — see §60 below
- [x] **#62** ConsultLiterature docstring names a paper_id argument that does not exist — *RESOLVED (`e9b7b04`)* — see §62 below
- [x] **#63** The 2x wall-clock stop did not fire in a Haiku run — *RESOLVED (`d39325f`)* — see §63 below
- [x] **#41** The whole watchdog subsystem had no local caller — a watchdog kill in this repo left no retrospective, no process reap, and a hung run ran forever — *RESOLVED 2026-09-21.* Verified directly against every function in `infra/watchdog_cleanup.py` before fixing (not inferred): `write_watchdog_retrospective`, `reap_process_group`, `read_governor_pids`/`check_memory_and_kill`, and `reap_governor_pids` all existed, were tested, and had zero production callers anywhere in the package — they ran only because the separate out-of-repo `f3dasm-agentic-benchmarks` harness called them from its own launcher; none of the 8 local `studies/*/run.py` wired any of it, and `internal/FEATURES.md`'s own "Synthetic watchdog retrospective (#12)" entry said so honestly. The maintainer's explicit decision (`paper/sections/03-method.tex`, "A wall-clock watchdog outside the run"): the timer must live OUTSIDE the run process — a backstop that is a thread inside the very process it is meant to catch cannot be trusted, since the thing most likely to be stuck (a hung model call, a wedged simulation) can wedge the watchdog thread right alongside it. Shipped `python -m adda.watchdog <study-dir> --budget <duration>` (`src/adda/_src/infra/watchdog_launcher.py`, thin top-level forwarding package `src/adda/watchdog/` mirroring `adda/viewer/`'s own convention): it runs the study as a CHILD process in its own process group, waits with a hard deadline derived from the SAME budget value the run itself resolves (`run_setup._parse_budget_str`) at a 2× floor that cannot be tightened (`resolve_deadline_seconds` raises below it), and on timeout SIGTERMs the whole child process group, escalates to SIGKILL after a short grace period, reaps any registered campaign PIDs via the existing `reap_governor_pids` (catches detached/new-session descendants a plain process-group signal misses), and appends a labelled post-mortem via the existing `write_watchdog_retrospective` — reusing every existing cleanup primitive rather than writing a second implementation of any of them. Exits `124` (distinguishable from any real exit code the run itself could produce) on a kill; propagates the run's own exit status otherwise. `python -m adda <study-dir>` itself is completely unchanged and still works exactly as before — this is an additional, safer way to launch the same run, not a replacement. Headless-tested against a trivial sub-second child (`tests/test_watchdog_launcher.py`) covering: a fast child is left alone and its exit status propagates; a slow child is killed and the timeout reported distinguishably; a GRANDCHILD process is also reaped (not just the direct child); a post-mortem lands in `retrospectives.jsonl` labelled `role="watchdog"`/`source_id="WATCHDOG"`, never as first-person text; and the 2× arithmetic itself is asserted directly so it cannot be silently tightened later. **What is still NOT wired by this fix, deliberately:** BACKLOG #22 (stall detection via `seconds_since_last_activity` — "liveness = file written, not progress made") is a *separate* signal this launcher does not consult; the deadline here is a flat 2×-budget hard stop, not a liveness check, so a run that stalls early but stays within budget is not caught until the deadline — matching CLAUDE.md §4's "science budgets stay SOFT... the one approved hard cap" framing, since this timer is the one approved exception and #22 was never part of its scope.
- [x] **#27** `pipeline_deliverable: false` doesn't suppress the notebook-deliverable prompt injection — *RESOLVED (`744f38b`)*. `notebook_deliverable_spec()` (`notebook_exec.py:92`) was appended to the strategizer's system prompt unconditionally (no `pipeline_deliverable` check) and states "DELIVERABLE = pipeline.ipynb... this SUPERSEDES every... instruction above" — overriding even a `PROBLEM_STATEMENT.md` that explicitly says there is no pipeline deliverable. The knob only suppressed the `CRAFT_PIPELINE` milestone nudge (`nodes/strategizer.py:137`), a soft nudge, not this harder-worded injection. Found running `mathexpert_kinematic_matching_test` (run `20260903T130138`): the strategizer wrote `pipeline.ipynb` anyway and failed the reproduction gate. Fixed by gating the injection itself on `settings.get_bool("pipeline_deliverable", True)` (`agent_runtime.py`).
- [x] **#28** `nbformat.write` crashes with `AttributeError: outputs` on a hand-authored notebook — *RESOLVED (`bf2c9f1`)*. `WriteDeliverable`'s `.ipynb` validation used only `nbformat.reads()`, which accepts a code cell missing `outputs` with no validation error (confirmed empirically — `normalize()` doesn't backfill it either); the malformed cell reached disk verbatim and crashed every later `nbformat.write` through that file (`split_lines` in nbformat's own `rwbase.py` unconditionally iterates `cell.outputs` for `cell_type=="code"`). Found in `mathexpert_kinematic_matching_test` run `20260903T130138` (cost $1.84, 0 evals, ended FAILED) — downstream of #27 (the strategizer authored `pipeline.ipynb` raw, bypassing `AddPipelineCell`, which never has this gap). Fixed with a shared `repair_code_cells()` helper (`notebook_exec.py`) called at every `nbformat.read`/`reads()` site that can observe an externally-authored notebook.
- [x] **#29** Per-delegation Write-sandbox permanently stuck to the FIRST delegation, for every OpenAI-compatible-backend worker — *RESOLVED (`27e878f`)*. `OpenAICompatibleAdapter.copy()` deliberately returns `self` ("concurrent delegations share this adapter instance"), and `_invoke_once()` caches `self._agent` forever after the first build; `_build_tools()` baked whichever `closure_tools["Write"]` callable existed at that first build directly into a `StructuredTool`, so every later delegation's fresh `closure_tools["Write"]` reassignment (the per-delegation sandbox setup in `nodes/tools/routing.py`) was inert — the cached agent's tool list never re-read the dict. Confirmed via 3 real `_sandboxed_write` `ERROR_RETURN` diagnostics in `mathexpert_kinematic_matching_test` run `20260903T163807`, two of them attributing D002's and D004's writes to delegation D001's own sandbox folder. NOT specific to MathExpert — any repeatedly-delegated worker on Ollama/vLLM/OpenRouter had this. Fixed by dispatching through a live dict lookup instead of a baked-in callable.
- [x] **#30** `pipeline_deliverable: false` still left the notebook-authoring TOOLS in the strategizer's toolset (and `_missing_deliverables()` still required `pipeline.ipynb` unconditionally) — *RESOLVED*. #27 gated only the injected `notebook_deliverable_spec()` preamble text; the strategizer still wrote `pipeline.ipynb` and hit `REPRO_GATE_BOUNCE` in `mathexpert_kinematic_matching_test` run `20260903T163807` despite that fix and despite `PROBLEM_STATEMENT.md` explicitly saying there is no pipeline deliverable, because (a) `StrategizerAgent.tools` unconditionally declared `WriteDeliverable`/`AddPipelineCell`/`AddPipelineMarkdownCell`/`EditPipelineCell`/`DeletePipelineCell`/`ShowNotebook`/`CheckDeliverable` regardless of the flag, and (b) `_missing_deliverables()` (`nodes/strategizer.py`) required `pipeline.ipynb` unconditionally too — a study with no ledger has nothing to lazily reproduce in the first place. Fixed both: `nodes/tools/routing.py` strips the notebook-authoring tool names (`Done` excluded — still needed to close a run) from the effective toolset when `pipeline_deliverable` is false, and `_missing_deliverables()` only requires `pipeline.ipynb` when the flag is on (`state['required_deliverables']` is still honoured regardless).
- [x] **#31** `run_ledger.csv`'s `input_tokens`/`output_tokens`/`cost_usd` go blank for a legitimately notebook-less run — *RESOLVED*. `studies/run_ledger.py` read these three fields exclusively from `pipeline.ipynb`'s stamped metadata cell (or a `solution.md` fallback) — safe before #30, when every run produced one; no longer true once `pipeline_deliverable: false` correctly allowed a run to close without a notebook. Found on the first real #30-fixed run (`mathexpert_kinematic_matching_test/20260903T191706`, GATED). Fixed by reading `debug/telemetry/summary.json` instead: telemetry is written per LLM call regardless of deliverable shape, exists for exactly this purpose ("where did the budget go; does the machinery improve outcomes"), and carries all FOUR token fields — the ledger now also records `cache_read_tokens`/`cache_creation_tokens`, which the deliverable-derived path never reported and which are precisely what a prompt-section change moves. The deliverable path stays as a fallback for runs predating telemetry and never overwrites a telemetry value; `wall_s` comes from the run's own close. One faithfulness bug fixed alongside it: `Telemetry.merge` summed unpriced calls as `0.0`, so a whole run on an open-weight backend (which never reports a price) recorded `total_cost_usd: 0.0` — indistinguishable from a run that genuinely cost nothing. It now reports `None` with a `cost_calls` count, and the ledger leaves the column blank rather than claiming zero.
- [x] **#32** `CorpusSearch`/`CorpusAdd`/`CorpusGetPaper` closures had no type annotations, silently breaking on any OpenAI-compatible backend (Ollama/vLLM/OpenRouter) — *RESOLVED*. `StructuredTool.from_function` (what these backends use to build each tool's JSON schema for the model) omits the `"type"` key entirely from a parameter's schema when the closure's own Python signature carries no type annotation (confirmed empirically: `CorpusSearch(query, top_k=10)` produces `{"query": {"title": "Query"}}`, no `"type"`, vs `{"query": {"title": "Query", "type": "string"}}` once annotated) — Claude's own native adapter never goes through this schema-inference path, so the bug was invisible there. Found via a user bug report investigating a literature-corpus tool failure on Qwen3.8:27b (Ollama). Audited the entire closure-tool surface (routing.py, worker.py, strategizer.py, every other literature.py closure) for the same pattern — confirmed isolated to exactly 5 call sites across `backends/base.py` (the universal `CorpusSearch`/`CorpusGetPaper` every agent gets) and `agents/literature.py` (`LiteratureReviewAgent`'s own duplicate `CorpusAdd`/`CorpusSearch`/`CorpusGetPaper` trio, which overrides the base default entirely). Fixed by adding the missing annotations at all 5 sites.
- [x] **#33** `LiteratureReviewAgent`'s `asyncable()` wrapper (adds a `wait=` kwarg to every external discovery tool — `search_semantic_scholar`, `search_openalex`, `arxiv_search_papers`, etc.) broke tool-schema generation on every OpenAI-compatible backend — *RESOLVED*. The wrapper hand-set `wrapper.__signature__` (so `inspect.signature()` shows the real params) but never set `wrapper.__annotations__` to match; `inspect.signature()` (parameter NAMES) and `typing.get_type_hints()` (parameter TYPES) are independent lookups — the latter reads `__annotations__` directly and ignores `__signature__` — so pydantic's schema builder (`ValidatedFunction.__init__`, via `StructuredTool.from_function`) found a real param name from the signature but nothing for it in `type_hints`, raising a bare `KeyError` (e.g. `KeyError: 'query'`) for the first param of every async-wrapped tool. This aborted the entire `_build_tools()` loop before any tool — including `CorpusSearch`/`CorpusAdd` (#32) — could be reached, so the #32 fix could never be verified end-to-end. Found verifying #32 for real via a `literature_reviewer` delegation on a local Ollama model (`corpus_search_ollama_test/20260903T234452`, `D001` FAILED with the exact `KeyError('query')` traceback); reproduced directly in-process against 10 affected closures (`search_semantic_scholar`, `get_semantic_scholar_paper_details`, `search_openalex`, `get_openalex_citations`, `get_openalex_references`, `get_semantic_scholar_recommendations`, `DownloadPdf`, `arxiv_search_papers`, `arxiv_download_paper`, `arxiv_read_paper`) before fixing. Fixed by also setting `wrapper.__annotations__` from `base_sig`'s own parameter annotations (plus `wait: bool`) alongside the existing `__signature__` override. Reran the same delegation post-fix (`corpus_search_ollama_test/20260903T235500`) — all three corpus tools now dispatch correctly on Ollama; the only remaining diagnostic was an unrelated, self-corrected strategizer usage error (missing `hypothesis_ids`).
- [x] **#34** A run stopped by an org-level Claude billing cap was indistinguishable from an ordinary UNGATED close — *RESOLVED*. The Claude Code CLI backend returns a billing-cap notice as ORDINARY assistant text ("You've hit your org's monthly spend limit · run /usage-credits to ask your admin for a higher limit"), not a raised exception — so `retry_on_transient`'s exception-based retry (which already correctly classifies `"rate limit"`/`"429"` text as transient) never even saw it: the strategizer just produced this as its final reply, made no more tool calls, and the run closed `UNGATED` with nothing distinguishing an externally-caused stop from the strategizer genuinely deciding it was done or a real gate/science failure. Found reading `supercompressible-material/runs/20260903T233207` (the most recent Oscar run — 67 evals legitimately completed, only 14% of its 12h wall budget used, cut off by one $16.24/213k-output-token strategizer turn) via its `debug/transcripts/strategizer/turn_001.jsonl`, not the retrospectives (this run wrote none for the terminal turn) — ground-truth read past the initial too-narrow theory ("5-hour rate limit", the user's framing) to the literal stop text. `AgenticRun(resume_from=...)` (checkpoint replay via `checkpoints.sqlite` + `debug/thread_id`) already existed and needed no change — the gap was detection + surfacing only (user-scoped explicitly, ruled out both a full new resumability system and blocking in-process backoff, since a monthly cap has no known reset window to wait out). Fixed in `agent_runtime.py`: `_EXTERNAL_STOP_SIGNATURES` matches this text in the terminal `last_report`, records `stop_reason` in `run_status.json` (null on an ordinary close), and logs the exact `resume_from=` invocation to resume the SAME run once the cap clears.
- [x] **#35** `AgenticRun(resume_from=...)` — the very mechanism BACKLOG #34's guidance points users at — was a silent no-op for every run it was meant to help — *RESOLVED*. `graph.invoke(None, config)` (adda's resume convention) is a real LangGraph no-op once a checkpoint has reached its terminal `Command(goto=END)`: no node re-runs, no new model call, the persisted `last_report` is handed back byte-identical. Every NORMAL close reaches that terminal state (GATED/UNGATED/FAILED all share the same terminal branch in `strategizer.py`) — including every #34-flagged externally-stopped run, which is exactly the case #34's resume guidance targets. Confirmed empirically with a minimal `StateGraph` + `SqliteSaver` reproduction outside adda entirely: a node's own call counter stayed at 1 across a second `invoke(None, ...)` on the same completed thread, and confirmed separately that passing NEW non-None input on that same thread DOES force real re-execution (counter went to 2). Found because a "resume" of `supercompressible-material/20260903T233207` (the #34 run) appeared to fail again with the exact same 19-hour-old text — mistaken by a second agent (correctly, on reflection) for live confirmation that the account was still blocked, when it was actually just replaying stale state; user then set the actual requirement: resume must genuinely retry both stopped AND interrupted runs, refusing only when the run already closed cleanly (GATED, an accepted `Done()`) with `PROBLEM_STATEMENT.md` unchanged since — nothing new to do in that one case. Fixed in `agent_runtime.py`: on resume, `graph.get_state(config).next` distinguishes a genuinely mid-flight interruption (non-empty — unaffected, still `invoke(None, ...)`) from a terminal checkpoint (empty — now builds fresh `{"messages": [HumanMessage(...)], "done": False}` input, explaining why it's resuming, so the entry node genuinely re-executes with the accumulated message history intact via `MessagesState`'s `add_messages` reducer); raises `AgenticRunError` instead of silently no-opping for the one case explicitly not worth resuming. A prior bug in the same patch, caught before shipping: the "did PROBLEM_STATEMENT.md change" hash comparison was comparing the LIVE text (already carrying the per-run constraint-snapshot preamble — budgets/elapsed time, which always differs between runs) against the original snapshot's raw-content hash, so it reported "changed" on every resume even with a byte-identical problem statement; fixed by capturing the raw-content hash before the preamble is prepended.
- [x] **#36** Reproduction-gate/deliverable execution's dedup-on-write silently fails to recognize ANY row as already-in-ledger, so re-executing `data_generation` can re-add every design point instead of the documented zero — *RESOLVED*. `notebook_exec.py`'s `sandbox_env()` (the environment for reproduction-gate/deliverable/scratch execution against a sandbox ledger copy) stamps a FIXED synthetic `F3DASM_DELEGATION_ID` (default `"D999"`) that never matches the real delegation(s) that actually generated the ledger's rows (`D005`, `D009`, ...). `InstrumentedDataGenerator._drop_duplicate_buffer()`'s dedup-on-write is deliberately scoped per-delegation (`mine = df_out["_delegation_id"] == self.delegation_id`) — correct for a real concurrent campaign, where a different delegation legitimately re-measuring a design is not waste and collapsing across delegations would corrupt that — but this reproduction-gate execution is a validation REPLAY of already-generated data, not a live campaign delegation, so per-delegation scoping is the wrong semantics for it specifically: `mine` matches zero rows (nothing in the ledger was ever written by `"D999"`), so `data_generation` re-adds every design point, violating `notebook_exec.py`'s own documented "LAZY: zero new oracle rows" reproduction invariant. Lands in a sandbox copy (not the real store), so this doesn't corrupt real data — but it can spuriously fail the reproduction gate on a genuinely reproducible notebook. Reported secondhand by a peer agent monitoring a separate, currently-running Oscar campaign ("D020/D021 worked around it manually, diffing against the full ledger by hand each time") — verified independently against the actual source before fixing, not trusted as stated. Two of that same report's other three claims did NOT hold up under direct code inspection at the time (`QueryStore`'s namespace mask is correctly built and applied — no bug found; left untouched, flagged unconfirmed) — but its THIRD claim, that the namespace registry doesn't survive resume, initially looked unconfirmable from a first code read and was WRONGLY left as "can't confirm the mechanism" here; the peer agent kept digging and found the real, confirmed mechanism — see #37. Fixed #36 itself with a new `dedup_scope` parameter (`"delegation"` default, unchanged; `"all"` dedupes against the whole ledger regardless of who wrote each row) threaded from `InstrumentedDataGenerator.__init__` through `get_evaluator()`'s new `F3DASM_DEDUP_SCOPE` env read, set to `"all"` by `sandbox_env()` specifically (real campaign delegations never set this env var, so they keep today's per-delegation behavior unchanged).
- [x] **#37** Resuming a run silently wipes any dynamically-registered oracle (namespace or canonical) — *RESOLVED*. `_init_canonical_store()` (`agent_runtime.py`) is called unconditionally at the top of EVERY `execute()` — fresh run and resume alike, no `if _resume is None` guard anywhere near it — and rebuilt `run_config.json` from a literal dict sourced only from `config.yaml`, with no `"oracles"` key at all and no read-modify-write against whatever was already on disk; it just overwrote the file outright. Any oracle registered mid-run via `register_evaluator_entrypoint()` — a namespaced `oracles[namespace]` block (Delegate(namespace=...) opening a new design) OR the canonical `evaluator_entrypoint` itself (the common "an agent authors + registers its own evaluator" pattern, where `config.yaml` declares no entrypoint at all) — was silently destroyed the moment `execute()` ran again for the same `run_dir`. Not a resume-specific in-memory cache, and not something `register_evaluator_entrypoint()`'s own atomic write could ever protect against, since the DESTRUCTIVE step was a completely separate function's unconditional overwrite. Found by a peer agent (monitoring a separate, currently-running Oscar campaign) who kept digging after their first framing ("namespace registry doesn't survive resume") couldn't be confirmed here from an initial code read alone — they read `_init_canonical_store` directly and traced the real mechanism, then confirmed it against that run's own `run_config.json` and the `ValueError: Unknown design namespace` D016 actually hit (9 wasted SLURM submissions before it traced the cause and self-healed by re-registering). Verified directly here before fixing (not trusted as stated): confirmed `_init_canonical_store` really is unconditional and really does write a fresh literal dict with no `oracles` key. Fixed by reading back any existing `run_config.json` first and preserving `evaluator_entrypoint`/`evaluator_output_names`/`evaluator_lookup`/`oracles` from it when present, falling back to `config.yaml`-derived values only when nothing exists yet (a genuinely fresh run) — `eval_budget`/`mem_cap_bytes` still refresh unconditionally from the current call's arguments, preserving the existing, deliberate "user raises the budget, resume can progress" behavior alongside the fix.
- [x] **#38** `viewer.readers.tail_jsonl()` could silently swallow a line split across two writes, forever — *RESOLVED*. The generator's initial offset (for a pre-existing file: "skip current content, tail only new growth") was only ever captured on the FIRST `next()` call, not when `tail_jsonl()` itself was invoked — generators are lazy. A caller whose first iteration is even slightly delayed (any real consumer driven from a separate thread, not a bare `for` loop starting instantly) could observe the file already mid-line at that moment, if a writer's append of a line's first half happened to land in the gap. Naively setting `offset = current file size` then treated that in-flight prefix as "already seen" forever: once the remainder arrived, reading from `offset` onward yielded only the trailing fragment, never valid JSON on its own — confirmed by direct reproduction to fail non-deterministically (~25% of runs, even with a 10s timeout margin, ruling out plain flakiness) before this fix, and 100% of runs once the race was replaced with a deterministic construction (file already ends mid-line before `tail_jsonl` is even called). Fixed by backing the initial offset up to the position right after the LAST newline in the existing file (0 if there is none) instead of raw file size — any trailing unterminated line is then treated as new, unconsumed content, exactly as if tailing had started before it was ever written.
- [x] **#39** Flaky CI test `tests/test_operator_channel.py::test_a_note_queued_while_draining_is_not_lost` — failed on `macos-latest / 3.13` for commit `01193b3` (CI run 34690958061, 2026-09-12; `assert ['first'] == ['first', 'second']`), a commit that touched only `tests/test_literature_guardrails.py`. Timing race between the draining thread and the queued note; passes locally and on the other matrix cells. Not investigated — needs a deterministic handshake (event/barrier) instead of a sleep, then verify the fix against the same matrix cell. — *RESOLVED 2026-09-20 (verified).* Fixed by construction rather than by a handshake: `drain_notes` now CLAIMS the file by rename (`path.replace(claimed)`, `operator_channel.py:310`) and reads the claimed copy, so a note either joins this batch or waits in a fresh file for the next. The test's assertion is correspondingly race-free — `sorted(drained + oc.drain_notes(run)) == ["first", "second"]` — instead of the order-dependent equality that flaked. No sleep, no barrier needed.
- [x] **#40** A mid-tier delegating node was cut off from the hypothesis ledger and the science monitor by its POSITION in the graph, not by what its Agent declared — *RESOLVED 2026-09-21.* `graph_builder.build_graph` passed `notes_dir=notes_dir if name == spec.entry else None`, and `Node._init_orchestration`/`_install_epistemics` read a missing `notes_dir` as "no ledger": `_ledger`, `_milestones`, `_science_monitor` and `_telemetry` all stayed `None` for any orchestrating node that was not the entry — the one remaining `name == spec.entry` special case left in the node layer once it collapsed to a single `Node` class, and it contradicted the system's own single-source-of-truth rule that a capability comes from an Agent's declared `tools`, not from its type or topology. Latent while every shipped graph is one tier deep (strategizer entry + leaf workers), so no node had ever actually been in this position in a real run; surfaced repeatedly in review as BACKLOG item #40, awaiting the §4 call on who may read/mutate the shared ledger. **Decision:** all nodes get hypothesis-ledger READ, science-monitor and telemetry access; hypothesis-ledger and milestone-ledger WRITE (`HypothesisPropose`/`HypothesisUpdate`, `Milestone*`) stays exactly where it already was — gated by each Agent's own declared `tools` (`nodes/tools/routing/__init__.py`'s `build_routing_tools`), today declared only by the strategizer. Verified this before shipping: `HypothesisList`/`HypothesisGet` (READ) were already declared by critic/datagenerator/implementer/debugger/math_expert (`agents/*.py`), so passing the real `notes_dir` to every node grants no capability any node had not already declared — it makes an existing declaration true rather than opening a new one. Also found, and worth recording: `HypothesisList`/`HypothesisGet` were not fully "dead" for a non-entry node even before this fix — `Node._read_ledger()` already had a same-run disk-based fallback (via the shared `delegation_log` path) that made them work for a LEAF node always, and for a non-entry ORCHESTRATING node after its first turn (`_absorb_state` re-points `_current_notes_dir` from `state["run_dir"]` on every turn, unconditionally). The genuinely dead capabilities were `_milestones`, `_science_monitor` and `_telemetry`, which have no equivalent fallback. Fixed in `graph_builder.py` (pass `notes_dir` to every node, not only the entry) — a leaf's own constructor never even reads the argument (`Node.__init__` forwards `notes_dir` only into `_init_orchestration`, never into `_init_leaf`), so every shipped single-tier graph is provably unaffected byte-for-byte; only a node with its own outgoing edges (a 2-tier-or-deeper graph, none shipped today) changes behaviour. Regression-guarded in `tests/test_graph_builder.py`.
- [x] **#25** MathExpert: a symbolic-derivation verification node — *BUILT (`12c882b`)* — shipped as `agents/math_expert.py` + `math_dsl`, exported as `MathExpertAgent`, handbook chapter `symbolic-derivation-patterns`, tests `test_math_expert_agent.py` / `test_math_dsl.py`. The spec below is kept for the record. — see [`specs/10-math-expert-agent.md`](specs/10-math-expert-agent.md). New delegate node giving an agent SymPy-backed, mechanically-verified symbolic math by default (an early modeling decision cascades into algebra an LLM's own arithmetic can't be trusted on); opt-in (delegate target, not a ledger gate). A derivation is a `.py` script written against a small first-party `math_dsl.Workspace` library, one file per edition under `runs/math_workspace/` (mirrors `LiteratureCorpus`'s persistence location); durability and "revise an assumption" are both plain filesystem operations (read/rerun; copy-edit-rerun for a counterfactual), not a bespoke trace-replay or dependency-graph mechanism — both were tried and dropped in favor of this during spec iteration. Four-valued verdict (`CONFIRMED`/`REFUTED`/`INCONCLUSIVE`/`ASSERTED`) keeps "is the algebra right" separate from "is the physical claim right." Verified derivations citable as prose-level hypothesis-ledger evidence (nothing mechanically enforced, per explicit user decision 2026-09-02); stress-tested against a real published derivation (Galeano-Rios et al. 2017, §2.1-2.30d).
- [x] **#24** Duplicate/redundant design-point evaluations go undetected — *RESOLVED (simplified from spec)*. See [`specs/07-duplicate-evaluation-detection.md`](specs/07-duplicate-evaluation-detection.md) for the evidence (run `example_study/20260713T221841`: D003 spent 76% of its 160 evals re-evaluating 38 unique points, up to 10x each). Shipped design (user-simplified from the spec's ratio-threshold proposal): `DUPLICATE_EVALUATION` fires after 3 NEW duplicate rows since the last check, resets its counter, caps at 2 nudges per delegation, rate-limited to 1/60s (`science_monitor.py` `_check_duplicate_evaluations`, `instrumented.py` `duplicate_eval_stats`). `Wait()`'s poll loop now also drains the monitor every 10s so the nudge reaches a strategizer blocked on a live campaign, not just on its next tool call.
- [x] **#21** RecallStore/QueryStore are namespace-blind — *RESOLVED*. The "informational, not a metering gap" severity call was wrong: it nearly produced a false CRITICAL fabrication verdict and forced 6/7 critics into raw-CSV fallbacks in a real run. Fixed by giving both tools the same `experiment_stores()` aggregation `LedgerBreakdown`/`ScienceMonitor`/`GetStatus`/`CancelDelegation` already used (`_derive_store_dir` → `_all_store_dirs` in `routing.py`); no per-namespace stat merging (rejected as "muddy" per `OPEN_DESIGN_SPACE_FRAMEWORK.md`) — `RecallStore` prints one block per store, `QueryStore` concatenates rows across stores before filtering. The same audit found the identical single-store blind spot in the `Done()` reproduction gate's laziness/integrity check, `CheckDeliverable`'s row-count fail-fast, and two guidance texts — fixed alongside this one.
- [x] **#14** Watchdog reap (#11) MISSES detached campaign processes — *open, HIGH (real CPU leak)* — they escape the process-group kill (new session) and outlive the run — *RESOLVED 2026-09-20 (verified).* `infra/watchdog_cleanup.py` now reaps via the ResourceBackend's RECURSIVE tree kill driven off `debug/governor_pids.jsonl`, which the module's own comment names as catching "the new-session/detached campaigns that os.killpg misses (the #14 escape)". `_owned_pids()` verifies ownership by process start-time (±1s) before signalling, so a recycled pid is excluded — fail-safe by construction. `reap_process_group`'s docstring still records the plain-killpg limit it superseded.
- [x] **#13** Per-cell notebook debugger — *DONE* (`RunPipelineCell` closure + `diagnose_notebook`; per-cell trace localizes a repro failure by cell name + traceback; runs against a ledger copy; kill-switch-free read-only diagnostic)
- [x] **#12** Watchdog kill loses the strategizer's retrospective — `5199b593` (synthetic post-mortem entry)
- [x] **#11** Orphaned background process survives watchdog kill — `5199b593` (process-group reap)
- [x] **#9** Orchestrator-owned live validator for HypothesisUpdate — *DONE* (advise-with-teeth, charter-grounded, kill-switchable via `F3DASM_VERDICT_VALIDATOR`; merged into dev/confer; validated live run `20260623T002417` — fired, judged H1 correctly vs the gate critic, non-blocking)
- [x] **#8** Literature str/int error (lit-bug #3) — `64a8230a` (coerce arxiv `max_results` to int)
- [x] **#7** Remove the literature hand-listed tool docs (BF-13a) — *open* (needs corpus closures to carry docstrings first) — *RESOLVED 2026-09-20 (verified, no single commit).* The stated blocker is gone: `CorpusAdd`/`ConsultLiterature`/`CorpusGetPaper`/`CorpusList`/`CorpusRank`/`DownloadPdf` are now real named functions in `agents/literature_tools/corpus.py`, each carrying a docstring (68–688 chars), so `render_tool_catalog` no longer falls back to "(no description)" for any of them. The hand-lists themselves are also gone: `<corpus_tools>`, `<discovery_tools>` and the implementer's "Available tools" block no longer appear anywhere in `src/adda`. Both halves of the principled fix landed across the `Consult<Corpus>` rename (`666c85a`) and the knowledge-protocol work.
- [x] **#5** KB entry: running a study on SLURM — *DONE* (`entries/0010-running-a-study-on-slurm.md`; the deliverable pipeline runs on SLURM, the agent graph stays local)
- [x] **#4** Single Jupyter-notebook deliverable — *DONE* (the live system; gate runs the notebook via nbclient; toolset completed `db288d5b`)

---

## 4. Single Jupyter-notebook deliverable
**Status:** RESOLVED — this IS the live system: `pipeline.ipynb` is the sole deliverable,
the gate executes it via nbclient (`notebook_exec.run_deliverable` / `_reproduction_gate`)
with the zero-new-evals + `REPRODUCED:` asserts, no `solution.md`/`pipeline.py`, and the
agent authors the cells through the structured tools (the CRUD set completed in `db288d5b`).
Every implication below was implemented. Original write-up kept for the record.
**Status (orig):** deferred. Raised 2026-06-15.

Merge the two deliverables (`solution.md` prose + `pipeline.py` executable) into
**one `.ipynb`** — markdown cells for the writeup, code cells for the lazy
create→run→analyze pipeline. One human-readable, runnable artifact.

**Implications to design when picked up:**
- The reproduction gate (`Node._reproduction_gate`) would execute the
  *notebook* lazily (e.g. `jupyter nbconvert --execute` / `nbclient`) instead of
  `python pipeline.py`, keeping the same asserts: exit clean + **zero new oracle
  evals** + headline self-assert (now a pre-critic gate, timeout = max(10% of
  the time budget, 180s)).
- `WriteDeliverable` accepts `.ipynb`; `_missing_deliverables` requires it.
- The runtime currently auto-writes `solution.md` from the `Done()` summary —
  that prose would instead become the notebook's leading markdown cells (decide
  who authors it: agent vs runtime injection).
- Keep the lazy + cache-or-load contract intact (notebook re-run = reproduction,
  no sims, no heavy refit).

---

## 5. KB entry: running a study on SLURM
**Status:** RESOLVED 2026-06-23 — `entries/0010-running-a-study-on-slurm.md`.
Raised 2026-06-15.

KB entry added: `Pipeline.run(mode="slurm", cluster=SlurmCluster(...))`, per-step
`SlurmResources`, `parallel=True` → `cluster_array` striping, canonical-store
FileLock making concurrent array writes + lazy FINISHED-skip resume safe, and the
key boundary — the deliverable pipeline runs on SLURM, the agent graph stays
LOCAL (no slurm path in agent_runtime/run.py). Lightweight by request; deeper
how-to docs deferred (none requested).

---

## 7. Remove the literature hand-listed tool docs (BF-13a)
**Status: RESOLVED 2026-09-20** (verified, no single commit). Both halves of
"the principled fix" below have landed: every corpus/discovery closure is now a
real named function in `agents/literature_tools/corpus.py` carrying a docstring,
and the `<corpus_tools>`/`<discovery_tools>`/"Available tools" hand-lists no
longer appear anywhere in `src/adda`. The original write-up follows.

**Status:** raised 2026-06-21. Follow-through on BF-13(b) (commit 6b9c8dd8).

`literature.py` still hand-lists every tool in `<corpus_tools>`/`<discovery_tools>`
with bare signatures; the implementer's `<role>` "Available tools" list does the
same. BF-13(b) made the auto-generated `<tools>` catalog the authoritative,
MCP-qualified source, so these hand-lists are now a second source that can drift
from it.

**Why not done yet:** the corpus closures (`CorpusAdd`/`CorpusSearch`/
`CorpusGetPaper`/`CorpusList`) are docstring-less lambdas in
`build_closure_tools`, and `render_tool_catalog` falls back to "(no description)"
for them. Deleting the hand-list before the closures carry real docstrings would
DEGRADE the catalog (lose the BM25-weighting note, the "ERROR if no full-text"
semantics, the acquisition workflow). The implementer list also mixes native
tools (Read/Bash — bare-correct) with closures (qualified) — a blanket delete
would lose the native-tool descriptions too.

**The principled fix:** give each corpus/discovery closure a real docstring (one
line is enough — the SDK already reads `fn.__doc__` for its tool schema), then
delete the hand-lists and let the qualified auto-catalog be the single source.
Test: assert the literature catalog (qualified) carries a non-"(no description)"
entry for every corpus tool, and that no bare hand-list signature survives.

## 8. Localize literature str/int error (lit-bug #3)
**Status:** RESOLVED 2026-06-22 (commit 64a8230a) — localized via the
string-`max_results` repro in run 20260622T165943; `arxiv_search_papers` /
`arxiv_list_papers` now coerce `max_results` to int. Original write-up below.
**Status (orig):** raised 2026-06-21. Observed in a wet literature run (prior session),
exact site not captured.

A wet literature delegation hit a `str`/`int` type error twice ("str-int error
×2"), separate from the dense-ranking crash (fixed 33d8d3f4) and the name
mismatch (fixed 6b9c8dd8). No traceback was captured this session — the wet test
was killed to save resources before it streamed output.

**Likely neighbourhood:** the citation-count / BM25 weighting path
(`log10(c+1)`), where a `citationCount` arriving as a string from S2/OpenAlex
JSON would break arithmetic; `build_closure_tools` already guards
`int(citation_count or 0)` on `CorpusAdd`, so the unguarded site is probably in
`CorpusRank` or the corpus's internal ranking, or in an OpenAlex/S2 field read.
**Needs a real wet run with the traceback** (`uv run pytest
tests/test_literature_wet.py -s --no-cov`) to localize before fixing —
do not guess-patch without the stack.

## 9. Orchestrator-owned live validator for HypothesisUpdate
**Status:** IMPLEMENTED (advisory) 2026-06-22 — the #9 live verdict validator
(`verdict_validator.py` + `nodes/critic_gate.py` `_run_verdict_validator`)
validates each closing verdict's substance against the charter at the
HypothesisUpdate boundary, ADVISES (never blocks), escalates on repeat flags.
Runs on the **critic's** model (one refereeing standard, decoupled from the
strategizer it judges). Given prior-rulings **MEMORY** 2026-06-23 (commit
88856cb9) so a borderline verdict can't oscillate between calls — see #15.
**§4 — user owns it.** The DEEPER architectural separation below (dedicated
verdict-adjudicator node / freeze the criterion at proposal time / binding
`LinkFalsificationAttempt`) remains OPEN — the advisory validator is one
realization, not the full separation.

Today the strategizer carries a triple burden for every hypothesis: it (1) states
the hypothesis + falsification criterion, (2) frames the falsification attempt,
and (3) judges the attempt's results — proposer, experiment-designer, and judge in
one agent. The only live guard on `HypothesisUpdate` is RULE-based (the Popperian
charter's `ERROR_RETURN`: cannot mark SUPPORTED without a falsification attempt on
record; cite only completed delegations). It checks *form*, not *substance*.

**Idea:** an **orchestrator-owned LLM** that validates each `HypothesisUpdate`
call LIVE at the tool boundary (where `ERROR_RETURN` fires now), independent of the
strategizer's own reasoning — offloading the judging role. It would check the
substance a rule cannot:
- the falsification attempt actually PROBES the registered prediction (severity —
  could it have refuted?);
- the verdict (SUPPORTED / FALSIFIED / INCONCLUSIVE) FOLLOWS from the cited
  evidence/ledger numbers;
- prediction and falsification_criterion test the SAME claim (catches goalpost-moving).

**Evidence motivating it (n=5 audit, `_audit_preserved/n5_20260621_222224`):**
- run03 strategizer moved its own goalposts — registered prediction ("reach f ≤ −1.0")
  ≠ falsification_criterion (relative budget test); the critic flagged it Charter §4.
  The strategizer self-reported the CONSISTENCY contradiction in its retrospective.
- `LinkFalsificationAttempt` was used only 2× across 6 runs — falsification linkage
  is effectively advisory/bypassed (a run02 critic: "the flag is advisory, not binding").
- verdicts are non-reproducible: the same H2 question (multi-start vs BO) came back
  FALSIFIED (r1, r2) and SUPPORTED (r4, r5) depending on the run's self-chosen budget.

**Open design questions (resume-cold):**
- BLOCK vs ADVISE: does it reject the update (hard, like `ERROR_RETURN`) or annotate
  it for the critic (soft)? Budgets are soft elsewhere — lean advise-then-escalate.
- Relation to the critic: the critic judges the DELIVERABLE at gate-time; this judges
  HYPOTHESIS verdicts LIVE as asserted — complementary, not a replacement.
- Cost/latency: one extra LLM call per HypothesisUpdate; pick a model tier; cache.
- Where it lives: `science_monitor.py` / the HypothesisUpdate tool wrapper — the
  Popperian-rules surface the user owns. Do NOT implement without the user's design call.
- Relates to the deferred "separate the falsification-judge from the hypothesis-author"
  options (freeze criterion at proposal time; dedicated verdict-adjudicator node;
  binding LinkFalsificationAttempt + INCONCLUSIVE as valid closure).

**Done-when (KPI):** verdict reproducibility across same-problem runs improves;
goalpost-moves caught LIVE (not only post-hoc by the critic); the falsification-attempt
linkage becomes load-bearing rather than advisory.

## 11. Orphaned background process survives the watchdog kill (resource leak)
**Status:** RESOLVED 2026-06-22 (commit 5199b593) — run.py is now a process-group
leader (`os.setpgrp`) and the watchdog reaps the group (`reap_process_group` in
`watchdog_cleanup.py`) before `os._exit`. Residual: a child that `setsid`'s escapes
the group (documented). Original write-up below.

The implementer backgrounds its BO campaign as a detached process
(`uv run python …/optimize_1000eval.py &`-style). When the watchdog force-exits
run.py via os._exit(2), that detached child is NOT reaped — it kept running at ~230%
CPU after the run died (had to be killed manually). Backgrounded children outlive the
run. **Fix direction:** the watchdog should kill its whole process group (or the
implementer's Bash backgrounding should be tracked and reaped on close); alternatively
discourage detaching the campaign. No reasonable reading justifies a live orphan after
a kill.

## 12. Watchdog kill loses the strategizer's retrospective (blinds §1 Step 1)
**Status:** RESOLVED 2026-06-22 (commit 5199b593) — the watchdog now appends a synthetic
`role="watchdog"` post-mortem (`write_watchdog_retrospective`) with the last delegation
state + diagnostics + a transcript pointer, so §1 Step 1 gets a breadcrumb. (A real
first-person strategizer retro on watchdog remains impossible — the agent is killed
mid-flight.) Original write-up below.

Retrospectives are written at clean run close; the watchdog's os._exit kills the
strategizer BEFORE it writes one. So EVERY watchdog-killed run has ZERO first-person
signal from the orchestrator — the very runs we most need to diagnose (its DECISION/
FRICTION on delegation, budgeting, gating). Observed: run 20260622T165943 has
retrospectives only for D001/D004, none for the strategizer, so its campaign-
decomposition reasoning had to be reconstructed from the transcript (§1 Step 4) instead
of read directly (Step 1). **Fix direction:** have the watchdog handler flush a
best-effort strategizer retrospective (or a partial "interrupted" one) before os._exit.

---

## 13. Per-cell notebook debugger (agent can't see WHICH cell failed reproduction)
**Status:** RESOLVED 2026-06-23 — `RunPipelineCell` closure (`nodes/tools/routing.py`)
+ `diagnose_notebook` (`notebook_exec.py`); 5 headless tests + live closure smoke.
Original spec below. Surfaced by run 20260623T002417 (FAILED).

**Problem.** `CheckDeliverable` runs `pipeline.ipynb` top-to-bottom via nbclient and
returns a BINARY pass/fail with a high-level message. When reproduction fails the
strategizer cannot see *which* cell raised, its traceback, or its stdout/state — so it
iterates blindly. Primary evidence: run 20260623T002417 burned ~10 gate attempts
(`REPRO_GATE_BOUNCE`×6 → `REPRO_GATE_FAILED`) and FAILED; the strategizer's DONE
retrospective BLOCKED field reads verbatim: *"No tool to execute and debug pipeline.ipynb
cell-by-cell in isolation… the CheckDeliverable gate was binary fail/pass with high-level
error messages only. I needed a cell-level executor (run cell 'analysis' and return
stdout/stderr/state) to pinpoint whether the failure was in data loading, ledger path
resolution, output formatting, or the gate's expectations."* It never diagnosed that its
own pillar cells were stubs (`print('doe')`, …), so the REJECT was unavoidable.

**Why this is parsimonious (not overfit).** "An agent must be able to observe the failure
it is asked to fix" is a general observability principle — a philosopher nods. The binary
gate throws away per-cell information nbclient ALREADY produces. This is not a workaround
for one run; it is the diagnostic counterpart the repro gate has always lacked.

**Proposed tool (DRY — reuse the gate's runner).** Add a strategizer closure, e.g.
`RunPipelineCell(name: str | None = None)`, registered alongside `ShowNotebook` /
`RunScratch` / `CheckDeliverable` (`nodes/tools/routing.py`), backed by the SAME nbclient
execution `notebook_exec.py` already uses for the gate:
- `name` given → execute `pipeline.ipynb` top-to-bottom up to AND INCLUDING the cell with
  that `metadata.name` (fresh kernel, the gate's environment + canonical store), return that
  cell's stdout / stderr / traceback + an `errored` flag. (Top-to-bottom because cells share
  state — imports/vars from earlier cells; "run cell N in isolation" would spuriously fail.)
- `name` omitted → execute the whole notebook and return a PER-CELL trace (each cell:
  ok | errored, the first failing cell's name + traceback, stdout tail) — the granular
  version of `CheckDeliverable`.

**Boundary / safety.** Read-only diagnostic: run in the same sandboxed, zero-new-eval mode
the gate uses (must NOT mutate the canonical ledger — a notebook that calls
`get_evaluator()` against a full store is lazy/no-op; guard the same way the gate does).
Pure diagnostic — does not change the gate's accept/reject criteria (not §4 critic
substance). Likely the minimal change is to expose what nbclient already captures, so much
of the work is surfacing, not new execution.

**Done-when (KPI).** A subsequent run that hits a repro failure resolves it WITHOUT
exhausting gate attempts — i.e. `REPRO_GATE_BOUNCE` count on a recovered run drops to ≤2
(vs 6 in 20260623T002417), or the strategizer's retrospective no longer lists the
cell-level executor as a BLOCKED gap. Headless: a test that a deliberately-broken cell is
pinpointed by name + traceback (not a binary fail).

**Reuse.** `notebook_exec.py` (nbclient runner behind `CheckDeliverable`), the notebook
closures in `nodes/tools/routing.py` (`ShowNotebook`/`RunScratch` registration pattern),
`metadata.name` cell addressing (already the notebook CRUD convention).

---

## 14. Watchdog reap (#11) misses detached campaign processes (real CPU leak)
**Status: RESOLVED 2026-09-20** (verified). `infra/watchdog_cleanup.py` reaps via
the ResourceBackend's RECURSIVE tree kill driven off `debug/governor_pids.jsonl`,
which catches the new-session/detached escape described below. `_owned_pids()`
verifies ownership by process start-time (±1s) before signalling, so a recycled
pid is never touched. The original write-up follows.

**Status:** RESOLVED 2026-06-23 — recursive reap via per-delegation PID
self-registration at the oracle entry (`governor_pids.jsonl`) +
`watchdog_cleanup.reap_governor_pids`, which kills each campaign's process tree
(incl. detached/new-session escapees the `killpg` missed). Wired into
`studies/.../run.py` `_watchdog` and the memory watcher; see FEATURES.md §E.
Validated on the resource-governance wet run (zero orphans at close).
Original write-up kept for the record.
Discovered live during audit run 20260623T015907.
HIGH — leaks a full CPU core per watchdog-killed run; orphans accumulate for days.

**Problem.** #11's reap (`watchdog_cleanup.reap_process_group` -> `os.killpg(pgid)`)
kills only run.py's process GROUP. The implementer launches its optimization
campaign as a DETACHED background process (`/tmp/campaign_v2.py`) with poll-loop
bashes waiting on `campaign_report.json` -- these run in a NEW session, so they
escape the group kill (the residual risk the code comments already flagged) and
outlive the watchdog `os._exit(2)`.

**Primary evidence (audit run 20260623T015907, watchdog_killed at 60min).** Minutes
after the watchdog fired, `ps` showed PID 47561 `python3 /tmp/campaign_v2.py` at
125% CPU / 42min CPU still running, plus two `until [ -f .../D003/
campaign_report.json ]` poll bashes (63962, 59331), AND an ancient zombie from a
2-day-old run (26085, "Waiting for 975 evaluations", polling run 20260621T155717).
The leak accumulates across runs/days. Had to `kill` them by hand to free the CPU
before the next run.

**Fix direction (touches the watchdog -- needs approval).** At reap time the
campaign's parent chain is still intact (it reparents to launchd only AFTER run.py
exits), so a recursive process-TREE walk by PPID at reap time catches it where the
process-group kill does not. Options: (a) dependency-free recursive `pgrep -P`
BFS from os.getpid(), SIGTERM each (recommended); (b) `psutil.children(recursive=True)`
(adds dep); (c) a spawned-PID registry. Keep the walk BEFORE `os._exit` (chain intact).

**Done-when.** After a watchdog kill, `ps aux | grep -E 'campaign|campaign_report'`
returns zero leftovers. Headless: extend `test_reap_kills_a_detached_background_process`
to a GRANDCHILD in a new session that the group kill misses (the current test's
child is a session leader pid==pgid, so the group kill happens to catch it).

**Reuse.** `watchdog_cleanup.reap_process_group`, `studies/agentic_black_box_3d/run.py`
`_watchdog`, `tests/test_watchdog_cleanup.py`.

---

## 15. Verdict oscillation on borderline cases — finding + resolved trigger
**Status:** RESOLVED 2026-06-23. Root cause FIXED (commit 88856cb9, the #9
validator memory); the §4 trigger question was RESOLVED **with no Charter change**
(see "RESOLVED" below). Captured here because the run artifacts that surfaced it
are ephemeral (wiped on the next run).

**Finding (two wet runs, bb3d).** Haiku run `20260623T194849` GATED but took 4 gate
attempts; H1/H2 each oscillated FALSIFIED↔INCONCLUSIVE 4-5× (`VERDICT_SUBSTANCE_FLAG`=4).
The live verdict validator was **stateless** — it re-judged each verdict from
scratch, with no view of its own prior rulings, so on a *borderline* result it
flipped. The Sonnet-strategizer A/B `20260623T212346` GATED in 2 attempts with
**0 oscillation** (best_f ≈ −1.000, the true optimum; cheaper: $1.34 vs $1.61).

**Crucial nuance (don't over-read the A/B).** The validator ran on **Haiku in BOTH
runs** (it reuses the CRITIC adapter, telemetry-confirmed: `verdict_validation |
model=claude-haiku-4-5`), so validator model strength was a *constant*. The Sonnet
win came from the **strategizer** writing grounded, calibrated predictions
(`f ≤ −0.998` vs the known prior-best −0.9958) that produced CLEAR met→SUPPORTED
outcomes — it avoided the borderline trigger UPSTREAM, it did not make the referee
more consistent. So: **oscillation is triggered by borderline verdicts**, not by a
weak referee per se.

**What was fixed.** #9 validator now gets its prior rulings on the same hypothesis
(ledger `status_log`) + a justify-any-reversal guard → can't silently flip. This is
INSURANCE: on harder problems (supercompressible), a severe test that misses a
*well-calibrated* prediction by a hair is genuine knife-edge science, not a
calibration artifact — so the borderline case WILL recur regardless of model
strength, and the stateless defect would have bitten again.

**RESOLVED (§4, user's call 2026-06-23) — NO Charter change.** The question was
whether a near-miss on a severe test is FALSIFIED or INCONCLUSIVE. Conclusion: the
existing Charter §3 already answers it by the correct variable — **adequacy, not
margin** — so a new "boundary" rule would be overfit AND a claim-protecting
loophole:
- near-miss from an **inadequate/under-budgeted** search → INCONCLUSIVE via the
  existing §3 confound/Duhem–Quine clause (did the hypothesis fail, or did the
  optimizer just not get there?);
- near-miss from a **genuinely adequate** test → FALSIFIED, and that is correct
  (the registered prediction was simply false; declining it would "protect a
  favoured claim", which §3/§4 forbid).
A blanket "near-miss → INCONCLUSIVE" collapses those two and lets an agent dodge a
real refutation — a step away from Popper, not a refinement. The campaign
delegations' "zone-based" logic is the loophole, not the intent. If a threshold
turns out to be a bad operationalization, §4 already prescribes the fix: revise the
prediction explicitly and re-test — don't reinterpret the old result. The
oscillation itself is handled by the validator-memory fix + calibration, not by
softening §3.

**Minor tool friction (1 occurrence, note-only).** `EditPipelineCell` rejected a
full-field edit lacking `expected_rev` (an optimistic-concurrency guard) → one
`ERROR_RETURN`; the agent had to `ShowNotebook` first to get the rev. Watch for
recurrence before treating as a fix.

---

## 60. Sandboxed Write routes a required deliverable to the wrong directory
The entry node's sandboxed Write (`build_sandboxed_write`) roots a bare relative path at
`<run>/debug/delegations`. A config `required_deliverables` name such as `design.json` is
checked at `<study>/<name>`, so the write reports "Written" and the file lands where the
check never looks. Evidence: the 2026-10-08 solo `design.json` misplacement. Wanted: the
sandboxed Write maps a required-deliverable name to the study dir. Not needed for a
Default node, which now keeps the native Write.

**Resolved 2026-10-09 (`2afcad8`).** The sandboxed Write maps a bare name equal to a required deliverable's basename to the study dir (`build_sandboxed_write`: `deliverable_dir`, `deliverable_names`).

## 62. ConsultLiterature docstring names a paper_id argument that does not exist
The docstring (`agents/literature_tools/corpus.py`, "A paper_id from that list → the full
extracted ... text") reads as if the tool takes `paper_id=`. The parameter is `query`. A
Haiku node called `ConsultLiterature(paper_id=...)` 3 times: TypeError, ERROR_RETURN.
Evidence: campaign H-on r1, arm `truss-10bar__baseline__r1__20261008T222037`,
diagnostics 02:28:25-26Z. Wanted (after the freeze): say "pass the paper_id as the
query", or rename the parameter. Affects every adda arm equally. Local note only.

**Resolved 2026-10-09 (`e9b7b04`).** `ConsultLiterature` now takes `paper_id`; the docstring and examples match.

## 63. The 2x wall-clock stop did not fire in a Haiku run
adda's 2x wall-clock stop did not fire in Haiku run doe_playbook r1: wall_s 5435 (90.6 min),
3.0x the 30-minute limit. Only one DELEGATE_CUTOFF (1.5x) is in diagnostics.jsonl; a
delegation and the gate outlived the 2x mark. Evidence: Oscar (read-only),
`arms-haiku/truss-10bar__doe_playbook__r1__20261008T222058`, run 20261009T022110,
run_status.json wall_s and diagnostics.jsonl error_type counts (DELEGATE_CUTOFF 1,
no 2x event). Wanted: find why the 2x check did not run while a delegation and the
gate were in flight. Local note only; no fix yet.

**Resolved 2026-10-09 (`d39325f`).** Cause: a CLI-backend turn is one long session, so the between-turn check never ran with a delegation or gate in flight. The time backstop is now also checked at every stop checkpoint and by one daemon timer per run.
