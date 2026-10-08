"""f3dasm agentic runtime — thin LangGraph wrapper."""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from ..agents import ImplementerAgent, StrategizerAgent, _default_graph
from ..backends.base import DEFAULT_PROMPT
from ..backends.base import Agent, Graph
from ..infra import run_abandon as abandon
from ..infra.container_runner import ContainerRunner
from ..infra.delegation_log import DelegationLog
from ..infra.stop_request import write_stop_request
from ..infra.telemetry import NORMALIZED_FIELDS
from ..infra.workspace_vcs import init_workspace_repo
from ..nodes._constants import (
    backstop_enabled,
    delegate_cutoff_enabled,
    delegate_cutoff_multiple,
    run_backstop_multiple,
)
from ..prompts.agent_prompts import (
    RUN_PATHS_PREAMBLE_TEMPLATE,
    TEAM_ROSTER_TEMPLATE,
    WORKSPACE_PREAMBLE_TEMPLATE,
)
from . import features, settings, terminal
from .graph_builder import build_graph
from .graph_state import AgenticState, Delegation, Report, StudyConfig, Task
from .node_tools import (
    apply_node_config,
    held_tools,
    record_builtins,
    record_native_expansion,
    record_resolution,
    uses_default,
)
from .run_setup import (
    AgenticRunError,
    _archive_prior_pipeline_notebook,
    _ingest_precomputed_pool,
    _init_canonical_store,
    _load_study_config,
    _parse_budget_str,
    resolve_mem_cap_bytes,
)
from .study_config import validate_top_level

__all__ = [
    "AgenticRun",
    "AgenticRunError",
    "DEFAULT_MODEL",
    "DEFAULT_OLLAMA_MODEL",
    "Delegation",
    "ImplementerAgent",
    "Report",
    "StrategizerAgent",
    "StudyConfig",
    "Task",
]

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_OLLAMA_MODEL = "qwen2.5:1.5b"

# Text the Claude Code CLI backend returns as an ORDINARY assistant message
# (not a raised exception) when an org-level billing cap stops it mid-run —
# confirmed for real: a run's strategizer turn ended with this exact text as
# its final reply, made no further tool calls, and the run closed UNGATED
# indistinguishable from the strategizer simply deciding it was done. This
# is a distinct, externally-caused stop condition (not a science/critic
# failure and not a bug in the agent's reasoning) that deserves its own
# stop_reason and explicit resume guidance rather than being silently
# folded into an ordinary UNGATED close (BACKLOG #34).
_ABANDON_GRACE_S = 30.0

_EXTERNAL_STOP_SIGNATURES = {
    "org_spend_limit": "org's monthly spend limit",
}



def _warn_if_delegate_cutoff_unreachable() -> None:
    """Startup guard: delegate_cutoff_multiple must sit strictly below
    run_backstop_multiple or it can never fire — the run closes at the
    backstop first. Not fatal (a stale/typo'd knob shouldn't make a study
    unstartable, matching settings.configure's own leniency for unknown
    keys); just tells the operator loudly, once, at the top of the run."""
    if not delegate_cutoff_enabled() or not backstop_enabled():
        return
    _cutoff, _backstop = delegate_cutoff_multiple(), run_backstop_multiple()
    if _cutoff >= _backstop:
        logging.getLogger("adda").warning(
            "runtime: delegate_cutoff_multiple (%.2gx) >= "
            "run_backstop_multiple (%.2gx) — the run-level backstop closes "
            "the run before the delegate cutoff can ever refuse a "
            "delegation. Set delegate_cutoff_multiple below "
            "run_backstop_multiple for it to take effect.",
            _cutoff, _backstop,
        )


def resolve_node_identity(
    agent: Any, default_model: str | None, default_backend: str | None
) -> tuple[str | None, str | None]:
    """The model and backend a node will ACTUALLY run on.

    An agent may override either; otherwise the run's defaults apply. Used
    both to build the adapter and to RECORD what was used, so the record can
    never disagree with the thing it describes.
    """
    return (agent.model or default_model, agent.backend or default_backend)


def _comparable_tokens(tokens: dict) -> dict | None:
    """The run's four schema counts, or None when any call lacked them (a
    mixed total would not be comparable across backends)."""
    if not tokens.get("normalized_calls") or tokens.get("legacy_calls"):
        return None
    return {f: int(tokens.get(f, 0) or 0) for f in NORMALIZED_FIELDS}


def _token_line(tokens: dict) -> str:
    n = _comparable_tokens(tokens)
    if n is None:
        return "legacy (not comparable across backends)"
    return (f"fresh {n['fresh_input']:,} / cache read {n['cache_read']:,} / "
            f"cache write {n['cache_write']:,} / output {n['output']:,}")


def _normalized_token_rows(tokens: dict, legacy_total: int) -> str:
    """Token-table rows after the four legacy ones. `input_tokens` above is
    cache-inclusive on openai-compatible backends and cache-exclusive on
    Claude, so `total_tokens` is the schema's sum when the run has it."""
    n = _comparable_tokens(tokens)
    if n is None:
        return (f"| total_tokens | {legacy_total:,} |\n"
                "| token_schema | legacy, not comparable |\n")
    return (f"| fresh_input_tokens | {n['fresh_input']:,} |\n"
            f"| cache_write_tokens | {n['cache_write']:,} |\n"
            f"| total_tokens | {sum(n.values()):,} |\n"
            "| token_schema | fresh + cache read + cache write + output |\n")


@dataclass
class _RunContext:
    """Everything ``AgenticRun._prepare_run`` establishes for one run.

    Built once, then read by the graph invocation and the finalisation. It is
    the answer to "what does this run consist of": where it writes, what it
    was asked, when it started, and which conversation thread it is.
    """

    ts: str
    run_dir: Path
    debug_dir: Path
    notes_dir: Path
    workspace_dir: Path
    problem: str
    #: sha256 of the frozen snapshot this run actually answered
    problem_sha256: str
    #: sha256 of what PROBLEM_STATEMENT.md says right now
    live_problem_sha256: str
    resume_from: Path | None
    start_time: float
    thread_id: str
    log: logging.Logger
    log_handler: logging.Handler
    delegation_log: DelegationLog
    canonical_cfg: dict
    study_cfg: dict
    initial_state: AgenticState
    graph_config: dict

    @property
    def resuming(self) -> bool:
        """Whether this run continues a prior run's checkpoint."""
        return self.resume_from is not None


class AgenticRun:
    """Run an agentic loop over a study directory.

    The entry point of adda. It reads ``PROBLEM_STATEMENT.md`` from the study
    directory, builds the agent graph, runs the strategizer's open loop to a
    gated deliverable, and returns the final report. Configuration not passed
    here is read from ``<study_dir>/config.yaml``; explicit arguments win.

    Parameters
    ----------
    study_dir : Path
        Root of the study tree. Must contain ``PROBLEM_STATEMENT.md``.
    graph : Graph, optional
        Custom agent graph. Defaults to the built-in strategizer-hub graph.
    model : str, optional
        LLM model identifier. Defaults to ``config.yaml`` ``model`` or
        ``DEFAULT_MODEL`` (``DEFAULT_OLLAMA_MODEL`` when the backend is Ollama).
    budget : float, optional
        Wall-clock budget in seconds, or an ``"HH:MM:SS"`` string in
        ``config.yaml``. ``None`` means unlimited. Soft: it nudges, it does not
        hard-kill the science.
    budget_usd : float, optional
        Hard USD cost ceiling. Honoured only when the backend reports per-call
        cost (the Claude backend); ``None`` means no ceiling.
    eval_budget : int, optional
        Soft cap on oracle evaluations across all delegations. Nudges the
        strategizer when approached; never stops a run on its own.
    interactive : bool, default True
        Whether the pre-run problem-statement review and in-graph FollowUp may
        prompt on stdin. Forced off automatically when there is no TTY, so a
        headless run never blocks on input.
    max_ask : int, default 1
        Maximum number of clarifying questions the interactive review may ask.
    container : bool, default False
        Run the loop inside a container via ``ContainerRunner`` instead of
        in-process.
    container_image : str, default "f3dasm-agentic:latest"
        Image used when ``container`` is True.
    resume_from : Path, optional
        A prior run directory to resume from (replays the LangGraph checkpoint).
        The run must have a ``debug/thread_id``.
    review_statement : bool, optional
        Run the advisory pre-run problem-statement review. Never blocks an
        autonomous run. ``None`` (default) defers to the top-level
        ``review_statement`` key of ``config.yaml``, then to on.
    runtime : dict, optional
        Explicit run knobs, overriding the study's ``runtime:`` block AND the
        environment — the precedence a caller's deliberate argument deserves.
        An unrecognised key raises (unlike ``config.yaml``, where a stale key
        only warns): a sweep that misspells a knob would otherwise run the
        baseline under an arm's label.

    Examples
    --------
    >>> from adda import AgenticRun
    >>> report = AgenticRun(study_dir="studies/my_study").execute()
    """

    # Knob sources, declared at class level so an instance built without
    # __init__ (several tests construct partial runs via __new__) still has
    # them, meaning "nothing configured, no overrides". Rebound per instance
    # by __init__; never mutated in place.
    _study_runtime: dict = {}
    _runtime_override: dict = {}

    def __init__(
        self,
        study_dir: Path,
        *,
        graph: Graph | None = None,
        model: str | None = None,
        budget: float | None = None,
        budget_usd: float | None = None,
        eval_budget: int | None = None,
        interactive: bool = True,
        max_ask: int = 1,
        container: bool = False,
        container_image: str = "f3dasm-agentic:latest",
        resume_from: Path | None = None,
        review_statement: bool | None = None,
        runtime: dict | None = None,
    ) -> None:
        self.study_dir = Path(study_dir).resolve()
        cfg = _load_study_config(self.study_dir)
        # config.yaml is the source of truth for run knobs (debug, timeouts,
        # retry, backstop, recursion_limit, …); `runtime=` is a caller's
        # explicit override of it and outranks both it and the environment.
        #
        # Installed in execute(), NOT here: settings holds one process-global
        # mapping, so constructing a second AgenticRun used to silently
        # reconfigure the first. Only an unknown key in `runtime=` is rejected
        # now, at construction, where the traceback points at the caller.
        settings.reject_stale_env()
        _cfg_errors = validate_top_level(cfg)
        if _cfg_errors:
            raise ValueError("config.yaml is invalid: " + "; ".join(_cfg_errors))
        self._base_url = cfg.get("base_url")
        self._served_base_url: str | None = None
        self._runtime_override: dict = dict(runtime or {})
        self._study_runtime: dict = dict(cfg.get("runtime") or {})
        settings.configure(self._study_runtime, self._runtime_override)

        _backend_cfg = cfg.get("backend", "claude")
        self._backend = _backend_cfg
        self._model = model or cfg.get("model") or (
            DEFAULT_OLLAMA_MODEL if _backend_cfg == "ollama" else DEFAULT_MODEL
        )
        self._eval_budget = (
            eval_budget if eval_budget is not None else cfg.get("eval_budget")
        )
        # Hard per-campaign memory cap (bytes) — the single HARD resource boundary
        # (host safety, not a science budget). config.yaml `mem_cap` wins; else
        # the SLURM allocation (real HPC budget); else
        # the default. See resolve_mem_cap_bytes.
        self._mem_cap_bytes = resolve_mem_cap_bytes(cfg.get("mem_cap"))
        self._required_deliverables = cfg.get("required_deliverables") or []

        # budget from config is HH:MM:SS string or seconds float
        if budget is not None:
            self._budget = budget
        elif "budget" in cfg:
            self._budget = _parse_budget_str(cfg["budget"])
        else:
            self._budget = None

        # Hard USD cost ceiling (None = no ceiling). Honoured only when the
        # backend reports per-call cost (claude); ollama has no cost data.
        self._budget_usd = (
            budget_usd if budget_usd is not None else cfg.get("budget_usd")
        )

        self._graph_spec = graph or _default_graph()
        # config.yaml `nodes:` replaces a node's class tool set; a bad block or
        # an unknown node fails here, at startup, not mid-run.
        self._node_tool_records = apply_node_config(
            self._graph_spec, cfg.get("nodes"))
        # `interactive` requires a real terminal: a headless/background run (no
        # TTY) has a stdin that blocks on read but never EOFs, so any input()
        # would hang the whole run forever. The in-graph FollowUp path already
        # guards on isatty(); the pre-run problem-statement review keys off
        # self._interactive alone, so fold the TTY check in HERE so EVERY
        # input() path is non-interactive when there is no terminal.
        import sys as _sys
        self._interactive = bool(interactive) and (
            getattr(_sys.stdin, "isatty", lambda: False)()
        )
        self._max_ask = max_ask
        self._container = container
        self._container_image = container_image
        self._resume_from = (
            Path(resume_from) if resume_from is not None else None
        )
        # Pre-run problem-statement review (Item B). User-controllable; cfg can
        # also disable it. Default on.
        self._review_statement = (
            review_statement
            if review_statement is not None
            else cfg.get("review_statement", True)
        )
        self._run_dir = None  # set in execute()

    @staticmethod
    def _write_run_status(debug_dir: Path, **payload) -> None:
        """Persist ``debug/run_status.json`` — the terminal status the §1 analysis
        protocol reads FIRST. Written on every close (normal gate outcome, crash,
        watchdog kill) so a run's outcome is always on disk, not only in the
        notebook metadata + the longitudinal ledger. Best-effort: a status write
        must never fail a run."""
        try:
            from . import features as _features
            payload.setdefault("arms", _features.arm_config())
            (debug_dir / "run_status.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _maybe_start_slurm_llm(self, full_cfg, debug_dir, log):
        """Optionally own a vLLM server on a SLURM GPU node for this run.

        Guarded by the ``llm_slurm.enabled`` config block. Submits a
        ``vllm serve`` job (reusing f3dasm's ``SlurmCluster`` + the plain
        ``sbatch`` submit idiom — a persistent server is not an eval array),
        waits for the granted node and a ready server, then publishes
        the endpoint on this run (``_served_base_url``) so the
        vllm/openai-compatible adapters reach it over the cluster network. Returns the SLURM job id (for teardown) or None
        when disabled.

        No silent fallback: if the feature is enabled and the server cannot be
        brought up, this raises — a run the user asked to serve locally must not
        quietly fall back to a hosted API. The jobid is persisted to disk so the
        study watchdog can reap a leaked allocation even if this process dies.
        """
        cfg = (full_cfg or {}).get("llm_slurm") or {}
        if not cfg.get("enabled"):
            return None
        if self._base_url:
            raise ValueError(
                "config.yaml sets both `base_url` and `llm_slurm.enabled`: the "
                "served endpoint would be ignored. Remove one.")

        from f3dasm import SlurmCluster

        from ..infra import slurm_llm

        model = cfg.get("model") or self._model
        spec = slurm_llm.resolve_serve_spec(model, cfg)
        warn = slurm_llm.serve_throughput_warning(spec)
        if warn:
            log.warning("llm_slurm: %s", warn)

        cluster_cfg = cfg.get("cluster") or {}
        cluster = SlurmCluster(
            partition=cluster_cfg.get("partition", "batch"),
            account=cluster_cfg.get("account", "default"),
            env_setup=list(cluster_cfg.get("env_setup", []) or []),
            env_vars=dict(cluster_cfg.get("env_vars", {}) or {}),
            runner=cluster_cfg.get("runner", "python"),
        )
        port = spec.profile.port
        script = slurm_llm.render_serve_script(
            spec, cluster, port, str(debug_dir))
        script_path = debug_dir / "vllm_serve.sh"
        script_path.write_text(script, encoding="utf-8")

        jobid = slurm_llm.submit_serve_job(str(script_path))
        (debug_dir / "serve_job.jobid").write_text(jobid, encoding="utf-8")
        log.info("llm_slurm: submitted serve job %s (model=%s)", jobid, model)

        queue_timeout = float(cfg.get("queue_timeout", 3600))
        serve_timeout = float(cfg.get("serve_timeout", 900))
        node = slurm_llm.wait_until_running(jobid, queue_timeout)
        base_url = f"http://{node}:{port}/v1"
        log.info("llm_slurm: job %s RUNNING on %s; waiting for vLLM at %s",
                 jobid, node, base_url)
        slurm_llm.wait_until_ready(base_url, serve_timeout)
        self._served_base_url = base_url
        log.info("llm_slurm: server ready at %s", base_url)
        if self._backend not in ("vllm", "openai", "openai_compatible"):
            log.warning(
                "llm_slurm.enabled but backend=%r (not vllm) — the served "
                "endpoint will be IGNORED. Set `backend: vllm` in config.",
                self._backend)
        return jobid

    def render_architecture(self, out_path: Path | str | None = None) -> Path:
        """Render this run's agent graph — nodes, roles, tools, descriptions,
        edges — as a self-contained SVG and write it to disk.

        Works before or after ``execute()`` (the graph is fixed at
        construction). Default location: ``run_dir/debug/architecture.svg``
        once a run has started (``self._run_dir`` set), else
        ``study_dir/architecture.svg``. Returns the path written.
        """
        from .run_diagram import render_architecture_svg

        svg = render_architecture_svg(
            self._graph_spec,
            model=self._model,
            backend=self._backend,
            study_dir=self.study_dir,
        )
        if out_path is None:
            base = (
                self._run_dir / "debug" if self._run_dir is not None
                else self.study_dir
            )
            out_path = base / "architecture.svg"
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(svg, encoding="utf-8")
        return out_path

    def serve_viewer(
        self, host: str = "127.0.0.1", port: int = 8765,
        allow_network: bool = False,
    ) -> None:
        """Launch the live web viewer for this study's runs
        (blocking — run in a separate terminal/process from ``execute()``,
        the same way ``render_architecture()`` is a separate opt-in step,
        never called automatically). Requires the ``viewer``
        optional-dependency group (``pip install adda[viewer]``); lazily
        imported here so the core package never depends on Starlette.

        Passes this run's own live ``Graph`` object (``self._graph_spec``)
        straight through — no reconstruction needed, unlike the standalone
        ``python -m adda.viewer <study-dir>`` CLI, which runs as a
        separate process with no in-memory Graph and falls back to
        recovering one from the study's own ``run.py``/``build_graph()`` (or
        the stock default graph) instead.
        """
        from ..viewer.app import run_viewer

        run_viewer(
            self.study_dir, host=host, port=port, graph=self._graph_spec,
            allow_network=allow_network)

    def execute(self) -> str:
        """Run the agentic loop; return the final report text.

        Reads ``PROBLEM_STATEMENT.md`` from the study directory and passes it
        as the initial user message to the entry node.

        Three phases, in order: establish the run (:meth:`_prepare_run` — run
        directory, canonical store, budgets, the state the graph starts from),
        drive it (:meth:`_invoke_graph`), then record what happened
        (:meth:`_finalize_run` — gate outcome, provenance, KPI row).
        """
        if getattr(self, "_container", False):
            return self._execute_in_container()
        # Install this run's knobs HERE, not at construction: the mapping is
        # process-global, so building two AgenticRun objects before running
        # either would leave both executing under the second one's config.
        settings.configure(self._study_runtime, self._runtime_override)
        _warn_if_delegate_cutoff_unreachable()
        ctx = self._prepare_run()
        result = self._invoke_graph(ctx)
        return self._finalize_run(ctx, result)

    def _execute_in_container(self) -> str:
        """Hand the whole run to ContainerRunner instead of running in-process."""
        runner = ContainerRunner(
            self.study_dir,
            model=self._model,
            budget=getattr(self, "_budget", None),
            backend=getattr(self, "_backend", "claude"),
            image=getattr(self, "_container_image", "f3dasm-agentic:latest"),
        )
        exit_code = runner.run()
        if exit_code != 0:
            raise AgenticRunError(f"Container exited with code {exit_code}")
        return runner._latest_solution()

    # ── Phase 1: establish the run ───────────────────────────────────────────

    def _prepare_run(self) -> _RunContext:
        """Everything that must exist before the graph is invoked."""
        problem_path = self.study_dir / "PROBLEM_STATEMENT.md"
        if not problem_path.exists():
            raise AgenticRunError(
                f"PROBLEM_STATEMENT.md not found in {self.study_dir}"
            )
        problem = problem_path.read_text(encoding="utf-8")

        ts, run_dir, resume = self._resolve_run_dir()
        debug_dir = run_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        problem_sha256, live_problem_sha256 = self._snapshot_problem_statement(
            debug_dir, problem)
        notes_dir = debug_dir / "strategizer_notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir = run_dir

        # Canonical store: experiment_data/ + run_config.json
        study_cfg = _load_study_config(self.study_dir)
        _eval_cfg = study_cfg.get("evaluator")
        canonical_cfg = _init_canonical_store(
            run_dir, self.study_dir, evaluator_config=_eval_cfg,
            eval_budget=getattr(self, "_eval_budget", None),
            mem_cap_bytes=getattr(self, "_mem_cap_bytes", None),
            objective_config=study_cfg.get("objective"),
            funnel_config=study_cfg.get("funnel"),
        )
        ingest_note = self._ingest_pool(study_cfg, canonical_cfg)

        log, handler = self._open_run_log(debug_dir, ts)
        if ingest_note is not None:
            level, msg = ingest_note
            log.log(level, msg)

        start_time = self._anchor_start_time(debug_dir, resume)

        # Create graph-wide delegation log for episodic memory.
        delegation_log = DelegationLog(debug_dir / "delegation_log.jsonl")

        # workspace_dir for worker delegations
        workspace_dir = debug_dir / "delegations"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        # One git repo per run, one commit per delegation (spec 11): makes
        # "which files did this delegation change" evidence instead of the
        # agent's own testimony. Never fatal — a run whose workspace cannot be
        # version-controlled records no sha and proceeds unchanged.
        init_workspace_repo(workspace_dir)

        thread_id = self._resolve_thread_id(debug_dir, resume)
        self._record_node_models(debug_dir)
        self._run_log = log
        record_resolution(debug_dir, getattr(self, "_node_tool_records", []), log)

        # Pre-run problem-statement review (advisory; interactive-refine when
        # enabled). Fresh runs only — a resume replays the checkpoint and must
        # not re-prompt. Skipped when a graph is injected programmatically
        # (a test affordance — _run_dir is set above so _make_adapter can build
        # the ephemeral reviewer session on real runs).
        if (
            resume is None
            and getattr(self, "_review_statement", True)
            and getattr(self, "_graph", None) is None
        ):
            problem = self._review_problem_statement(problem, debug_dir)

        # The constraint snapshot is NOT rendered into this message. It used
        # to be: a snapshot computed here was concatenated onto the problem
        # statement, and this string is the standing first user turn, re-sent
        # verbatim every turn — so those numbers froze at run start and the
        # orchestrator read "5% used" 23 minutes in. Node._constraint_refresh
        # now injects a fresh block on every orchestration turn instead,
        # including the first, so the budgets are still stated as facts in
        # the very first thing the strategizer reads AND they advance.
        # Workers already worked this way (snapshot_for_node at dispatch and
        # at completion); this was the one call site that cached.

        initial_state = AgenticState(
            messages=[HumanMessage(content=problem)],
            study_dir=str(self.study_dir),
            done=False,
            last_report=None,
            total_delegations=0,
            budget_seconds=getattr(self, "_budget", None),
            budget_usd=getattr(self, "_budget_usd", None),
            run_dir=str(run_dir),
            eval_budget=getattr(self, "_eval_budget", None),
            evals_used=0,
            start_time=start_time,
            required_deliverables=(
                getattr(self, "_required_deliverables", None) or None
            ),
            experiment_data_dir=canonical_cfg["store_dir"],
        )

        graph_config: dict[str, Any] = {
            "configurable": {"thread_id": thread_id},
            # 2000 ≈ hundreds of delegations; the old 500 (and the legacy 25 on
            # some branches) could crash a long multi-delegation run mid-flight
            # (GraphRecursionError). Knob: recursion_limit (config.yaml runtime
            # block; F3DASM_RECURSION_LIMIT overrides).
            "recursion_limit": settings.get_int("recursion_limit", 2000),
        }

        return _RunContext(
            ts=ts,
            run_dir=run_dir,
            debug_dir=debug_dir,
            notes_dir=notes_dir,
            workspace_dir=workspace_dir,
            problem=problem,
            problem_sha256=problem_sha256,
            live_problem_sha256=live_problem_sha256,
            resume_from=resume,
            start_time=start_time,
            thread_id=thread_id,
            log=log,
            log_handler=handler,
            delegation_log=delegation_log,
            canonical_cfg=canonical_cfg,
            study_cfg=study_cfg,
            initial_state=initial_state,
            graph_config=graph_config,
        )

    def _resolve_run_dir(self) -> tuple[str, Path, Path | None]:
        """This run's directory: a fresh timestamped one, or the one resumed.

        getattr default: some tests build AgenticRun via __new__.
        """
        _resume = getattr(self, "_resume_from", None)
        if _resume is not None:
            run_dir = _resume.resolve()
            if not (run_dir / "debug" / "thread_id").exists():
                raise AgenticRunError(
                    f"resume_from={run_dir} is not a resumable run dir "
                    "(no debug/thread_id)"
                )
            return run_dir.name, run_dir, _resume
        # Run ids are second-resolution timestamps, so two runs started in the
        # same second used to SHARE a directory — each overwriting the other's
        # debug output, ledger and status. Rare by hand, routine under a sweep.
        # The timestamp stays the prefix (it is the sort key everything orders
        # by); a suffix is added only on collision, so ordinary sequential runs
        # keep their historical names exactly.
        _base = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        ts = _base
        _runs = self.study_dir / "runs"
        while (_runs / ts).exists():
            ts = f"{_base}-{uuid.uuid4().hex[:6]}"
        run_dir = _runs / ts
        _archive_prior_pipeline_notebook(self.study_dir)
        return ts, run_dir, None

    def _snapshot_problem_statement(
        self, debug_dir: Path, problem: str
    ) -> tuple[str, str]:
        """Freeze the statement this run answered; return (snapshot, live) hashes.

        The live study_dir/PROBLEM_STATEMENT.md drifts between runs (a study is
        meant to be run once per statement; when it isn't, a human auditing a
        later run needs to see the statement THAT run actually answered, not
        whatever the file has since been edited to say). The notebook's Run
        metadata cell carries only the hash so a reader can confirm which
        snapshot matches; the snapshot is the recoverable copy.

        Write-if-absent: a resumed run must keep its ORIGINAL snapshot, not
        overwrite it with whatever PROBLEM_STATEMENT.md says at resume time.
        The snapshot hash is derived from the SNAPSHOT's content, never re-read
        from the live file, so a resume's stamp always matches what this run
        actually answered even if PROBLEM_STATEMENT.md has since drifted.

        The live hash is taken BEFORE the constraint-snapshot preamble
        (budgets, elapsed time — always different between runs) is prepended to
        `problem`, so a resume's "did PROBLEM_STATEMENT.md change" check
        compares the same kind of content on both sides instead of always
        reporting "changed" (BACKLOG #35).
        """
        import hashlib
        _ps_snapshot_path = debug_dir / "PROBLEM_STATEMENT_snapshot.md"
        if not _ps_snapshot_path.exists():
            _ps_snapshot_path.write_text(problem, encoding="utf-8")
        snapshot_sha = hashlib.sha256(
            _ps_snapshot_path.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        live_sha = hashlib.sha256(problem.encode("utf-8")).hexdigest()
        return snapshot_sha, live_sha

    def _ingest_pool(
        self, study_cfg: dict, canonical_cfg: dict
    ) -> tuple[int, str] | None:
        """Ingest a precomputed pool as D000 ground-truth rows.

        Two sources, one ingestion path — D000 rows are never counted as
        evaluations (_resolve_delegation_evals runs only for real delegations
        D001+):
          evaluator.lookup.pool  → pool IS the oracle (queried via
                                   LookupDataGenerator) AND training data.
          training_data          → pool is ONLY training data; there is NO
                                   live oracle (e.g. surrogate-only studies
                                   where new evaluations cannot be run).

        Returns a ``(level, message)`` pair for the run log, or None when there
        is no pool. The log is not open yet at this point in the run.
        """
        _eval_cfg = study_cfg.get("evaluator")
        _lookup_cfg = (_eval_cfg or {}).get("lookup")
        _training_data = study_cfg.get("training_data")
        _pool_cfg = _lookup_cfg or (
            {"pool": _training_data} if _training_data else None
        )
        if not _pool_cfg:
            return None
        _store_dir = Path(canonical_cfg["store_dir"])
        try:
            _n_ingested = _ingest_precomputed_pool(
                _store_dir, self.study_dir, _pool_cfg
            )
        except Exception as _exc:  # noqa: BLE001
            return logging.WARNING, f"D000 pool ingest failed: {_exc}"
        return logging.INFO, (
            f"D000: ingested {_n_ingested} precomputed pool rows"
            f" from {_pool_cfg.get('pool', '?')}"
        )

    def _open_run_log(
        self, debug_dir: Path, ts: str
    ) -> tuple[logging.Logger, logging.Handler]:
        """Open debug/run.log for this run."""
        log = logging.getLogger(f"adda.{ts}")
        log.setLevel(logging.INFO)
        handler = logging.FileHandler(debug_dir / "run.log")
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        log.addHandler(handler)
        log.info(f"Run starting: model={self._model}, study={self.study_dir}")
        return log, handler

    def _anchor_start_time(self, debug_dir: Path, resume: Path | None) -> float:
        """The run's wall-clock anchor, persisted in its OWN file.

        For exactly the reason thread_id is: run_config.json is rewritten
        mid-run, so it cannot carry a start time. A resume MUST charge the wall
        time the run has already spent — re-anchoring to now makes every budget
        check, every constraint snapshot and the critic's run-adequacy
        judgement restart from zero, so a run that has been going for a day
        reports hours.
        """
        _start_path = debug_dir / "run_started_at"
        if resume is not None:
            try:
                return float(_start_path.read_text().strip())
            except (OSError, ValueError):
                pass                        # unreadable: fall back to now
        start_time = time.time()
        try:
            _start_path.write_text(repr(start_time))
        except OSError:
            pass                            # anchor is best-effort, never fatal
        return start_time

    def _watch_default_node(self, name: str, adapter: Any, backend: str) -> None:
        """Report what a Default node really received (informational only)."""
        debug_dir = self._run_dir / "debug" if self._run_dir is not None else None
        if debug_dir is None:
            return
        if backend == "claude":
            seen: set[str] = getattr(self, "_default_init_seen", set())
            self._default_init_seen = seen

            def _on_init(tools: list[str]) -> None:
                if name in seen:
                    return
                seen.add(name)
                record_builtins(debug_dir, name, tools)

            adapter.on_init_tools = _on_init
        else:
            record_native_expansion(
                debug_dir, name, backend, adapter.native_tools,
                getattr(self, "_run_log", logging.getLogger("adda")))

    def _record_node_models(self, debug_dir: Path) -> None:
        """Record which model/backend each node actually runs on.

        The viewer cannot re-derive this: it reconstructs the graph by
        re-executing the study's ``build_graph()`` in its OWN process, and a
        graph whose composition depends on runtime state (an env var naming a
        local endpoint, say) then rebuilds DIFFERENTLY there — silently
        reporting the study's default model for a node the run actually put on
        another one. Same principle as the delegation log and the
        problem-statement snapshot: what a run did is a record, not something
        recomputed later from inputs that have since changed.

        Best-effort: a run must never fail over its own bookkeeping.
        """
        try:
            record = {}
            for name, agent in self._graph_spec.nodes.items():
                model, backend = resolve_node_identity(
                    agent, self._model, self._backend)
                record[name] = {"model": model, "backend": backend}
            (debug_dir / "node_models.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _resolve_thread_id(self, debug_dir: Path, resume: Path | None) -> str:
        """Stable thread_id, persisted so a crashed run can be resumed.

        Resume reads it back; a fresh run mints and stores it. Its own file:
        run_config.json is rewritten mid-run.
        """
        _tid_path = debug_dir / "thread_id"
        if resume is not None:
            return _tid_path.read_text().strip()
        thread_id = str(uuid.uuid4())
        _tid_path.write_text(thread_id)
        return thread_id

    # ── Phase 2: drive the graph ─────────────────────────────────────────────

    def _invoke_graph(self, ctx: _RunContext) -> dict:
        """Build the graph and run it to termination against a disk checkpoint."""
        from langgraph.checkpoint.sqlite import SqliteSaver
        log = ctx.log
        ckpt_path = ctx.debug_dir / "checkpoints.sqlite"
        log.info("Invoking graph")
        # Optionally own a vLLM server on a SLURM GPU node for this run; the
        # jobid is torn down in the finally on EVERY exit path (normal close,
        # crash, KeyboardInterrupt) so a killed run never leaks a GPU
        # allocation. None when llm_slurm is disabled — the common path.
        _serve_jobid = None
        try:
            _serve_jobid = self._maybe_start_slurm_llm(
                ctx.study_cfg, ctx.debug_dir, log)
            with SqliteSaver.from_conn_string(str(ckpt_path)) as saver:
                # Populated in place by build_graph as each Node is
                # constructed -- an explicit, caller-owned way to reach
                # them afterward (spec 12 edge 2's open-review sweep),
                # never through the COMPILED graph's own internals.
                self._live_nodes: dict[str, Any] = {}
                graph = getattr(self, "_graph", None) or build_graph(
                    self._graph_spec, self._make_adapter,
                    study_dir=self.study_dir,
                    interactive=self._interactive, max_ask=self._max_ask,
                    notes_dir=ctx.notes_dir,
                    workspace_dir=ctx.workspace_dir,
                    delegation_log=ctx.delegation_log,
                    checkpointer=saver,
                    node_registry=self._live_nodes,
                )
                graph_input = (
                    None if ctx.resuming else ctx.initial_state
                )
                if ctx.resuming and hasattr(graph, "get_state"):
                    graph_input = self._resumed_graph_input(graph, ctx)
                self._refresh_resumed_budgets(graph, ctx)
                if ctx.resuming and graph_input is None:
                    self._ask_crashed_run_for_retrospective(ctx)
                try:
                    return self._invoke_abandonable(graph, graph_input, ctx)
                except BaseException as _exc:  # noqa: BLE001
                    # Any unhandled crash (GraphRecursionError,
                    # KeyboardInterrupt, OOM, …): record a resumable status so
                    # resume_from is always an option after a break, then
                    # re-raise (we do not swallow).
                    self._write_run_status(
                        ctx.debug_dir, status="crashed",
                        reason=f"{type(_exc).__name__}: {_exc}"[:500],
                        resumable=True, thread_id=ctx.thread_id,
                        outcome=terminal.UNGATED,
                        termination=terminal.CRASHED, reviewed=False,
                        # A crashed run's duration is exactly what the next
                        # resume needs to charge, so record it here too.
                        wall_s=round(time.time() - ctx.start_time, 1),
                    )
                    # A crash never reaches _finalize_run's normal close, so
                    # it never reaches the _fallback_retrospective call there
                    # either — this is the same "exit interview reply never
                    # arrived" gap on a second path (GraphRecursionError,
                    # KeyboardInterrupt, OOM, …). Call it here too, BEFORE
                    # re-raising, so the record lands regardless. Never let a
                    # failure in here mask the real crash.
                    try:
                        self._fallback_retrospective(
                            ctx.run_dir,
                            reason=(
                                f"Run crashed ({type(_exc).__name__}) before "
                                "reaching a normal close, so the exit "
                                "interview was never reached."
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # spec 12 edge 2: a crash reaches neither Done() nor
                    # _finalize_run's own sweep call below, so it needs
                    # this SAME status-update append here too -- the
                    # OPEN_FOR_REVIEW row itself was already written at
                    # review-open time regardless (durable before this
                    # crash, not written by this call).
                    try:
                        self._sweep_open_reviews(ctx.run_dir)
                    except Exception:  # noqa: BLE001
                        pass
                    raise
        finally:
            if _serve_jobid:
                from ..infra.slurm_llm import cancel_job
                try:
                    cancel_job(_serve_jobid)
                    log.info("llm_slurm: scancel'd serve job %s", _serve_jobid)
                except Exception:  # noqa: BLE001
                    log.warning("llm_slurm: teardown failed", exc_info=True)

    def _invoke_abandonable(self, graph: Any, graph_input: Any,
                            ctx: _RunContext) -> Any:
        """``graph.invoke`` on a worker thread, so an interrupt still returns.

        LangGraph joins its node threads while an exception unwinds out of
        ``invoke``. A node stuck in a call nobody can cancel (the strategizer
        in Wait, joined on a delegation inside an LLM read) then held the run
        open until that call came back. Here the calling thread only waits,
        so Ctrl-C or a timeout reaches it at once.
        """
        box: dict[str, Any] = {}
        finished = threading.Event()
        abandon.reset_stop()

        def _go() -> None:
            try:
                box["out"] = graph.invoke(graph_input, config=ctx.graph_config)
            except BaseException as exc:  # noqa: BLE001
                box["exc"] = exc
            finally:
                finished.set()

        try:
            threading.Thread(target=_go, name="adda-graph", daemon=True).start()
            while not finished.wait(timeout=0.5):
                pass
        except BaseException:
            self._abandon_graph(finished, ctx)
            raise
        if "exc" in box:
            raise box["exc"]
        return box["out"]

    def _abandon_graph(self, finished: threading.Event,
                       ctx: _RunContext) -> None:
        """Tell every node thread to stop, wait a bounded time, record the rest.

        A thread cannot be killed safely. Each one ends itself at its next
        tool call or wait loop (``RunAbandoned``); one stuck inside a model
        call ends when that call returns.
        """
        nodes = getattr(self, "_live_nodes", None) or {}
        abandon.request_stop()
        for node in nodes.values():
            node._abandon.set()
        finished.wait(timeout=_ABANDON_GRACE_S)
        left: list[str] = []
        for name, node in nodes.items():
            with node._registry_lock:
                left += [f"{name}:{did}" for did, e in node._registry.items()
                         if e.get("status") == "Working"]
        if finished.is_set() and not left:
            return
        rec = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "node": "run", "tool": "RUN_ABANDONED",
            "error_type": "RUN_ABANDONED", "fault": "nudge",
            "message": (
                "the run stopped waiting for its nodes after "
                f"{_ABANDON_GRACE_S:.0f}s; graph thread "
                f"{'ended' if finished.is_set() else 'still running'}; "
                f"delegations left Working: {left or 'none'}; "
                f"calls blocked by the stop signal: {abandon.blocked_calls()}"),
            "detail": {"graph_thread_alive": not finished.is_set(),
                       "delegations_left": left,
                       "calls_blocked": abandon.blocked_calls()},
        }
        try:
            with (ctx.debug_dir / "diagnostics.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _ask_crashed_run_for_retrospective(self, ctx: _RunContext) -> bool:
        """Resuming a run whose process was lost (its checkpoint is mid-flight):
        when ``runtime.resume_close_with_retrospectives`` is on, wind the
        resumed run down at once instead of continuing it, so the entry node
        gives the retrospective the crash cost it. The run closes CRASHED."""
        if not settings.get_bool("resume_close_with_retrospectives", False):
            return False
        return write_stop_request(
            ctx.run_dir, by="resume", termination=terminal.CRASHED,
            reason="the process that ran this was lost before it could close")

    def _resumed_graph_input(self, graph: Any, ctx: _RunContext) -> Any:
        """What to feed a resumed graph: None to replay, or fresh input to re-run.

        A run that reached a terminal Command(goto=END) — i.e. EVERY normal
        close (GATED/UNGATED/FAILED all go through the same terminal branch in
        the orchestrating node) — leaves the checkpoint with an empty ``.next``.
        LangGraph's invoke(None, config) on such a checkpoint is a genuine
        no-op: no node re-runs, no new model call happens, it just hands back
        the stale last_report verbatim (confirmed empirically: a minimal
        StateGraph reproduction showed the node's own call counter never
        incremented on a second invoke(None) against an already-END'd thread).
        BACKLOG #34's resume_from guidance was silently useless for exactly the
        runs it targeted (externally-stopped, therefore terminal) until this
        fix — found because a "resumed" run replayed 19-hour-old cached text
        and was mistaken for a live re-test of the same stop condition
        (BACKLOG #35).

        Only a genuinely mid-flight interruption (crash, kill — ``.next``
        non-empty, real pending tasks) should still use the plain invoke(None)
        replay-from-checkpoint path. A terminal checkpoint needs FRESH input to
        force real re-execution from the entry node (confirmed empirically too:
        invoke() with new non-None input on an already-terminal thread DOES
        re-run the node). The one case explicitly NOT worth resuming: the run
        already closed cleanly (GATED, an accepted Done()) and
        PROBLEM_STATEMENT.md hasn't changed since — there is nothing new to do,
        so this refuses loudly rather than silently no-op or silently redo
        finished work.
        """
        _resume_state = graph.get_state(ctx.graph_config)
        if _resume_state.next:          # mid-flight: plain replay
            return None
        _prior_status: dict = {}
        try:
            _prior_status = json.loads(
                (ctx.debug_dir / "run_status.json")
                .read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            pass
        _resume_ps_changed = ctx.live_problem_sha256 != ctx.problem_sha256
        # "Is there anything left to do?" is a question about HOW the run
        # stopped, not about what its conclusions are worth. It keyed on
        # status == "GATED" because that was the only terminal fact recorded;
        # a deliberately-closed but unreviewed run (no critic in the graph) is
        # equally finished. Run dirs written before `termination` existed fall
        # back to the old key so an older run can still be resumed.
        _finished = not _prior_status.get("stop_reason") and (
            _prior_status["termination"] == terminal.DONE
            if "termination" in _prior_status
            else _prior_status.get("status") == "GATED"
        )
        if _finished and not _resume_ps_changed:
            raise AgenticRunError(
                f"resume_from={ctx.run_dir} closed cleanly "
                "(an accepted Done()) and "
                "PROBLEM_STATEMENT.md is unchanged since — "
                "there is nothing new for this run to do. "
                "Resume is for a run that was interrupted or "
                "stopped short of a real close; edit "
                "PROBLEM_STATEMENT.md first if you want it "
                "reconsidered, or start a fresh run instead."
            )
        _reason_bits = []
        if _prior_status.get("stop_reason"):
            _reason_bits.append(
                "it was stopped by an external cause "
                f"({_prior_status['stop_reason']}), not by "
                "its own choice"
            )
        elif not _finished:
            _reason_bits.append(
                f"it closed {_prior_status.get('status', 'UNGATED')} "
                "without an accepted Done()"
            )
        if _resume_ps_changed:
            _reason_bits.append(
                "PROBLEM_STATEMENT.md has been edited since "
                "this run's original snapshot — the current "
                "text follows below"
            )
        _resume_note = (
            "[RESUME] This run previously closed, but "
            + "; and ".join(
                _reason_bits or ["you asked to resume it"]
            )
            + ". Continue using the accumulated conversation"
            " history above — do not restart from scratch."
        )
        if _resume_ps_changed:
            _resume_note += (
                f"\n\nCurrent PROBLEM_STATEMENT.md:\n\n"
                f"{ctx.problem}"
            )
        return {
            "messages": [HumanMessage(content=_resume_note)],
            "done": False,
        }

    def _refresh_resumed_budgets(self, graph: Any, ctx: _RunContext) -> None:
        """Re-seed budgets and start_time into a resumed checkpoint.

        On resume, the checkpointed state still carries the OLD budgets and
        start_time. Re-seed them from this AgenticRun so a run that halted on a
        budget can actually make progress after the user raises it (cumulative
        token_totals persist in the checkpoint, so the spend-so-far is still
        counted against the new ceiling).
        """
        if not ctx.resuming or not hasattr(graph, "update_state"):
            return
        try:
            graph.update_state(ctx.graph_config, {
                "budget_seconds": getattr(self, "_budget", None),
                "budget_usd": getattr(self, "_budget_usd", None),
                "eval_budget": getattr(self, "_eval_budget", None),
                "start_time": ctx.start_time,
            })
        except Exception:  # noqa: BLE001
            ctx.log.warning("resume state refresh failed", exc_info=True)

    # ── Phase 3: record what happened ────────────────────────────────────────

    def _finalize_run(self, ctx: _RunContext, result: dict) -> str:
        """Persist the run's outcome and provenance; return the report."""
        log = ctx.log
        # Merge per-call telemetry into an analysis-ready summary.json (additive,
        # off the decision path — a failure here must not fail the run).
        try:
            from ..infra.telemetry import Telemetry
            Telemetry.merge(ctx.debug_dir)
        except Exception:  # noqa: BLE001
            log.warning("telemetry merge failed", exc_info=True)

        report = result.get("last_report") or ""
        # The terminal triple comes from the state, recorded by whichever path
        # ended the run. It is NOT re-derived from the report's banner: that
        # grep defaulted to GATED, so a backstop halt (whose banner matches no
        # pattern) and a critic-less close (which emits no banner at all) both
        # logged as validated successes. resolve() fails safe to UNGATED and
        # refuses GATED for a halt or an unreviewed run.
        gate_outcome, termination, reviewed = terminal.resolve(
            result.get("outcome"),
            result.get("termination"),
            result.get("reviewed"),
        )
        stop_reason = self._warn_if_externally_stopped(report, ctx)
        evals = self._ledgered_eval_count(ctx, result)
        tokens = result.get("token_totals") or {}

        now_ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        elapsed = time.time() - ctx.start_time
        cost = tokens.get("total_cost_usd")
        cost_str = f"${cost:.4f}" if cost is not None else "n/a"

        meta_md = self._run_metadata_markdown(
            ctx, result, gate_outcome, evals, tokens, now_ts, elapsed)
        nb_path = self._stamp_notebook_provenance(
            ctx, meta_md, gate_outcome, now_ts)

        # Capture, don't request: every close — GATED, UNGATED, FAILED, or an
        # external stop — passes through here, so this is the one place that
        # can guarantee the entry node's retrospective exists even when its
        # post-Done exit-interview reply never arrived (a model that answers
        # in prose instead of calling Done() again silently lost the record
        # before this existed). No-op on a compliant close.
        self._fallback_retrospective(ctx.run_dir)

        # spec 12 edge 2: Done()/the watchdog/a budget cutoff can all close
        # a run while a delegation is still OPEN-FOR-REVIEW (peer_interaction).
        # Record every one honestly -- open, never approved -- rather than
        # silently dropping it or letting it read as accepted.
        self._sweep_open_reviews(ctx.run_dir)

        # Persist the terminal gate outcome to run_status.json on the NORMAL
        # close too (the crash path writes its own). Without this a
        # cleanly-closed run leaves no run_status.json and the §1 protocol's
        # first KPI (gate outcome) is unreadable — the outcome would live only in
        # the notebook metadata + the ledger. (audit: 3 GATED runs, none had it.)
        # A stop is not a verdict on the work: it is recorded as STOPPED, with
        # the outcome (UNGATED) alongside and the run marked resumable.
        _stopped = termination == terminal.STOPPED
        # A backstop or crash that wound down for retrospectives is a halt, not
        # a verdict either: it keeps the "halted" status it had before it went
        # through the wind-down, and stays resumable.
        _halted = termination in (
            terminal.BACKSTOP_TIME, terminal.BACKSTOP_USD,
            terminal.REPEATED_ERRORS, terminal.CRASHED)
        self._write_run_status(
            ctx.debug_dir,
            status=("STOPPED" if _stopped else "halted" if _halted
                    else gate_outcome),
            **({"outcome": gate_outcome, "resumable": True}
               if _stopped or _halted else {}),
            model=self._model,
            evals_used=evals, timestamp=now_ts, run=str(ctx.run_dir),
            thread_id=ctx.thread_id, stop_reason=stop_reason,
            # HOW the run stopped, kept separate from what its conclusions are
            # worth: a run can terminate `done` and still be UNGATED (no critic
            # reviewed it), and a halted run may carry real science. `reviewed`
            # distinguishes "the critic passed it" from "no critic looked",
            # which an ablation removing the critic has to be able to tell.
            termination=termination, reviewed=reviewed,
            # The §1 KPI table asks for wall clock and this file is what it
            # reads first; without it every consumer re-derives the duration
            # from file mtimes and gets a different answer.
            wall_s=round(elapsed, 1),
        )
        self._append_kpi_ledger(ctx)

        log.info(
            f"Run complete. Evals: {evals}. "
            f"Tokens: {_token_line(tokens)}. "
            f"Cost: {cost_str}. "
            + ("pipeline.ipynb stamped." if nb_path.exists()
               else "pipeline.ipynb was NEVER WRITTEN — the agent never "
               "called WriteDeliverable().")
        )
        log.removeHandler(ctx.log_handler)
        ctx.log_handler.close()
        return report

    def _fallback_retrospective(
        self, run_dir: Path, reason: str | None = None
    ) -> None:
        """Ensure the graph's entry node has a retrospective, even if it
        never answered the post-Done exit interview.

        ``feedback.py``'s ``_enter_retrospective_round`` (called on every
        UNGATED/FAILED close, and after a critic PASS) sets
        ``node._awaiting_retro`` and asks for ONE more Done() call carrying a
        ``### Retrospective`` block — but nothing enforces that the reply
        actually arrives. A model that answers in prose instead just ends the
        turn, the graph reaches END, and the record is silently lost (run
        20260920T005201, studies/tube_buckling_sensitivity: closed UNGATED
        with 2 worker retrospectives on disk and none from the strategizer).
        The same gap exists on the CRASH path (``_invoke_graph``'s
        ``except BaseException`` around ``graph.invoke``): a
        GraphRecursionError/KeyboardInterrupt/OOM never reaches this method's
        normal call site in ``_finalize_run`` either, so that path calls this
        directly, with its own ``reason``, right before re-raising.

        This is the code-level guarantee CLAUDE.md §2 calls for instead of a
        prompt rule: capture, don't request. It is idempotent
        (``write_fallback_retrospective``'s ``skip_if_present``) so a
        compliant close — the real entry lands via
        ``FeedbackTools._capture_retrospective`` before this ever runs — is a
        no-op here, never a duplicate.

        Best-effort: never raises, so it can't break a run's close (or mask
        the exception on the crash path, which wraps this call in its own
        try/except too, belt-and-suspenders).
        """
        try:
            from ..infra.watchdog_cleanup import write_fallback_retrospective
            entry_name = getattr(self._graph_spec, "entry", None)
            entry_agent = (self._graph_spec.nodes or {}).get(entry_name) \
                if entry_name else None
            role = getattr(entry_agent, "role", None) or entry_name \
                or "strategizer"
            write_fallback_retrospective(
                run_dir, role=role,
                reason=reason or (
                    f"Run closed without the {role}'s post-Done "
                    "retrospective turn arriving — either it was never "
                    "reached, or the model answered in prose instead of "
                    "calling Done() again."
                ),
            )
        except Exception:  # noqa: BLE001
            pass

    def _sweep_open_reviews(self, run_dir: Path) -> None:
        """A small close-time status update for any delegation still
        OPEN-FOR-REVIEW (spec 12, peer_interaction, edge 2).

        The HONEST, durable record is actually written much earlier, at
        the review's own OPEN, not here: ``WorkerSession._open_for_review``
        appends an ``OPEN_FOR_REVIEW`` delegation-log row the instant the
        report exists, so a crash or a watchdog kill with no code running
        on the way out still leaves that row as the delegation's LAST one
        on disk -- exactly what a post-mortem reader needs (see
        ``write_watchdog_retrospective``). This method exists only for
        the compliant-close case: it appends ONE MORE row (``last-wins``
        collapse, same convention as RUNNING -> DONE) recording that the
        run ended before anyone approved it, and notes it in the
        delegating node's retrospective too. ``Done()``, the watchdog
        (were an in-process one to exist), or a budget/backstop cutoff
        can all reach this on the NORMAL close path; the crash path calls
        it too (see the ``except BaseException`` in ``_invoke_graph``).

        Iterates ``self._live_nodes`` -- populated by ``build_graph`` as
        it constructs each Node, an explicit reference this class owns,
        not LangGraph's own compiled-graph internals (a prior version of
        this method read ``compiled.nodes[name].bound.func``, silently
        able to break on any LangGraph upgrade). A node this genuinely
        cannot reach is recorded as a diagnostic, not silently skipped.
        """
        nodes = getattr(self, "_live_nodes", None) or {}
        for name, node in nodes.items():
            try:
                with node._registry_lock:
                    open_reviews = [
                        (did, dict(e)) for did, e in node._registry.items()
                        if e.get("status") == "OpenForReview"
                    ]
            except Exception as exc:  # noqa: BLE001
                self._record_sweep_failure(run_dir, name, exc)
                continue
            for delegation_id, entry in open_reviews:
                self._record_unapproved_review(node, delegation_id, entry)

    @staticmethod
    def _record_sweep_failure(run_dir: Path, node_name: str, exc: Exception) -> None:
        """The open-review sweep could not read one node's registry --
        recorded as a diagnostic (never silently skipped), so a reader
        knows the sweep may be incomplete rather than assuming it covered
        every node."""
        try:
            debug = Path(run_dir) / "debug"
            debug.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.now(tz=timezone.utc).isoformat(
                    timespec="seconds"),
                "node": node_name,
                "error_type": "OPEN_REVIEW_SWEEP_FAILED",
                "message": f"{type(exc).__name__}: {exc}"[:300],
            }
            with (debug / "diagnostics.jsonl").open(
                    "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _record_unapproved_review(
        node: Any, delegation_id: str, entry: dict
    ) -> None:
        """One OPEN-FOR-REVIEW delegation's honest close-out record."""
        try:
            if node._delegation_log is not None:
                node._delegation_log.record(
                    id=delegation_id,
                    from_node=node._name,
                    to_node=entry.get("target", "") or "",
                    task="",
                    deliverable=entry.get("result", "") or "",
                    hypothesis_ids=entry.get("hypothesis_ids", []) or [],
                    started_at=entry.get("started_at", "") or "",
                    completed_at=datetime.now(
                        tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                    # Distinct from DONE/FAILED on purpose -- this was
                    # never approved, and must never read as if it were.
                    status="OPEN_UNAPPROVED",
                    is_falsification_attempt=bool(
                        entry.get("is_falsification_attempt")),
                    evals=entry.get("evals", 0) or 0,
                    phase=entry.get("phase"),
                )
            node._record_retrospective(
                node._role_of(entry.get("target", "")), delegation_id,
                f"[system] Run closed with {delegation_id}'s report still "
                "OPEN FOR REVIEW -- nobody called SendMessage(..., "
                "approve=True). Recorded honestly as open/never-approved, "
                "not as accepted.",
            )
        except Exception:  # noqa: BLE001
            pass

    def _warn_if_externally_stopped(
        self, report: str, ctx: _RunContext
    ) -> str | None:
        """Name an external stop cause in the log, with resume guidance."""
        stop_reason = next(
            (name for name, sig in _EXTERNAL_STOP_SIGNATURES.items()
             if sig in report),
            None,
        )
        if stop_reason is not None:
            ctx.log.warning(
                "Run stopped by an external cause (%s), not a normal close "
                "— resume it once the cause clears:\n"
                "    from adda import AgenticRun\n"
                "    AgenticRun(study_dir=%r, graph=build_graph(),\n"
                "               interactive=False,\n"
                "               resume_from=%r).execute()",
                stop_reason, str(self.study_dir), str(ctx.run_dir),
            )
        return stop_reason

    def _ledgered_eval_count(self, ctx: _RunContext, result: dict) -> int:
        """Authoritative eval count = provenance-stamped rows in the ledger.

        NOT the run-state counter: evals_used is summed from a registry that
        clears Done entries on loop-back, so it under-reports (0) on any run
        that re-prompts (e.g. every UNGATED run). The ledger never loses rows —
        and it also captures cancelled-but-completed delegations whose evals
        are real. Summed across the canonical store AND every design namespace
        (Axis 3a): namespace evals live in sibling stores the canonical-only
        count missed (run 20260627T013812 reported 100 while 200 real evals
        ran).
        """
        evals = result.get("evals_used", 0)
        try:
            from ..evaluation.ledger_summary import total_ledgered_evals
            _total = total_ledgered_evals(
                ctx.debug_dir.parent / "experiment_data")
            if _total:
                return _total
        except Exception:  # noqa: BLE001
            ctx.log.warning("ledger eval-count failed; using state counter",
                            exc_info=True)
        return evals

    def _run_metadata_markdown(
        self,
        ctx: _RunContext,
        result: dict,
        gate_outcome: str,
        evals: int,
        tokens: dict,
        now_ts: str,
        elapsed: float,
    ) -> str:
        """Run metadata + token table — provenance appended to the deliverable."""
        h, m, s = (
            int(elapsed // 3600), int((elapsed % 3600) // 60), int(elapsed % 60))
        tokens_in = tokens.get("input_tokens", 0) or 0
        tokens_out = tokens.get("output_tokens", 0) or 0
        cache_read = tokens.get("cache_read_input_tokens", 0) or 0
        cache_create = tokens.get("cache_creation_input_tokens", 0) or 0
        cost = tokens.get("total_cost_usd")
        cost_str = f"${cost:.4f}" if cost is not None else "n/a"
        error_counts = result.get("error_counts") or {}
        return (
            f"## Run metadata\n\n"
            f"- timestamp: {now_ts}\n"
            f"- model: {self._model}\n"
            f"- gate: {gate_outcome}\n"
            f"- total_delegations: {len(ctx.delegation_log.query_all())}\n"
            f"- evals_used: {evals}\n"
            f"- run_dir: {ctx.run_dir}\n"
            f"- time_used: {h:02d}:{m:02d}:{s:02d}\n"
            f"- problem_statement_sha256: {ctx.problem_sha256}\n"
            f"  (verbatim snapshot: {ctx.debug_dir}/PROBLEM_STATEMENT_snapshot.md — "
            f"study_dir/PROBLEM_STATEMENT.md may since have been edited)\n\n"
            f"## Token usage\n\n"
            f"| Metric | Value |\n"
            f"|--------|-------|\n"
            f"| input_tokens | {tokens_in:,} |\n"
            f"| output_tokens | {tokens_out:,} |\n"
            f"| cache_read_tokens | {cache_read:,} |\n"
            f"| cache_creation_tokens | {cache_create:,} |\n"
            + _normalized_token_rows(tokens, tokens_in + tokens_out)
            + f"| estimated_cost | {cost_str} |\n"
            + (
                "\n## Tool-call errors per node\n\n"
                + "| node | error_count |\n"
                + "|------|-------------|\n"
                + "".join(
                    f"| {node} | {count} |\n"
                    for node, count in sorted(error_counts.items())
                )
                if error_counts else ""
            )
        )

    def _stamp_notebook_provenance(
        self, ctx: _RunContext, meta_md: str, gate_outcome: str, now_ts: str
    ) -> Path:
        """Stamp run provenance into pipeline.ipynb; return its path.

        The agent-authored pipeline.ipynb IS the deliverable (its leading
        markdown cells hold the writeup). There is no solution.md — provenance
        goes in as a trailing metadata cell + notebook metadata.
        """
        nb_path = self.study_dir / "pipeline.ipynb"
        if not nb_path.exists():
            return nb_path
        try:
            import nbformat

            from ..evaluation.notebook_exec import (
                repair_code_cells,
                stamp_run_provenance,
            )
            nb = nbformat.read(str(nb_path), as_version=4)
            repair_code_cells(nb)
            # Replace (not append) the provenance cell — the notebook is
            # study-scoped and persists across runs; appending accumulated
            # a prior run's stale metadata cell.
            stamp_run_provenance(nb, meta_md)
            nb.metadata.setdefault("agentic", {}).update(
                {"model": self._model, "run": str(ctx.run_dir),
                 "timestamp": now_ts, "gate_outcome": gate_outcome,
                 "problem_statement_sha256": ctx.problem_sha256})
            nbformat.write(nb, str(nb_path))
        except Exception:  # noqa: BLE001
            ctx.log.warning("notebook provenance stamp failed", exc_info=True)
        return nb_path

    def _append_kpi_ledger(self, ctx: _RunContext) -> None:
        """Append a KPI row to the longitudinal ledger (best effort).

        The extraction logic lives in studies/run_ledger.py (the one source of
        truth, writing studies/run_ledger.csv); we invoke it as a subprocess
        when present so a run is always recorded without a manual step. Absent
        (e.g. a non-studies install) → silently skipped.
        """
        try:
            import subprocess
            import sys as _sys
            ledger_script = self.study_dir.parent / "run_ledger.py"
            if not ledger_script.exists():
                return
            proc = subprocess.run(
                [_sys.executable, str(ledger_script), str(ctx.run_dir)],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0:
                ctx.log.info("KPI ledger: %s", proc.stdout.strip())
            else:
                ctx.log.warning(
                    "KPI ledger append failed (rc=%s): %s",
                    proc.returncode, proc.stderr.strip())
        except Exception:
            ctx.log.warning("KPI ledger append errored", exc_info=True)


    def _review_problem_statement(
        self, problem: str, debug_dir: Path, *, adapter=None
    ) -> str:
        """Advisory pre-run well-posedness review (Item B).

        Always writes ``debug/problem_statement_review.md``.  When the run is
        interactive and gaps are found, offers a per-gap refine via the same
        ``input()`` channel the in-graph FollowUp uses, appending accepted
        clarifications to the statement (and to a saved addendum).  Returns the
        (possibly augmented) problem text.  NEVER blocks an autonomous run: any
        reviewer failure falls back to the original statement unchanged.
        """
        from ..backends.base import bind_run_context as _bind_rc
        from ..epistemics.reviewer import (
            ProblemStatementReviewerAgent,
            format_review_markdown,
            parse_review,
            review_gaps,
        )

        try:
            if adapter is None:
                adapter = self._make_adapter(
                    "problem_statement_reviewer",
                    ProblemStatementReviewerAgent(),
                )
            _rc_path = str(debug_dir / "run_config.json")
            with _bind_rc("problem_statement_reviewer", _rc_path):
                raw = adapter.invoke([{"role": "user", "content": problem}])
            review = parse_review(raw)
        except Exception:  # noqa: BLE001 — advisory, never blocks
            return problem

        try:
            (debug_dir / "problem_statement_review.md").write_text(
                format_review_markdown(review, problem), encoding="utf-8"
            )
        except OSError:
            pass

        gaps = review_gaps(review)
        if not (gaps and self._interactive):
            return problem

        clarifications: list[tuple[str, str]] = []
        print(
            "\nThe problem statement may be under-specified. For each gap, "
            "type a clarification (or leave blank to skip):"
        )
        for g in gaps:
            try:
                ans = input(f"  [{g['element']}] {g['note']}\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans:
                clarifications.append((g["element"], ans))

        if not clarifications:
            return problem

        addendum = "\n\n## Clarifications (added pre-run via HITL review)\n" + (
            "\n".join(f"- **{el}**: {ans}" for el, ans in clarifications)
        )
        try:
            (debug_dir / "problem_statement_addendum.md").write_text(
                addendum.strip() + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        return problem + addendum

    def _team_roster(self, name: str) -> str:
        """This run's agents, read off the live graph: one list, the same for
        every agent, closed by the one line that differs ("You are <name>").

        Generated, not typed — the same reason ``render_tool_catalog`` is
        generated. The static strategizer prompt describes a full cast and
        once designated "the general implementer" as the fallback for any
        block no specialist matches; run a two-node graph, or an ablation arm
        that drops a node, and that fallback names an agent which does not
        exist. A campaign logged 15 delegations to an absent 'implementer'.

        Lists the nodes REACHABLE from the entry, in edge-declaration order,
        each with who hands it work and who IT may hand work to. A node
        declared but wired to nothing is left out: listing it would be a
        name an agent can see but not reach. Returns "" for a graph of one
        node, which has no one to name.

        Topology, not prose, is what fixes a specific failure (run
        20260927T034538, Elvis's diagnosis): a strategizer reasoned "no
        MATLAB specialist... implementer is for f3dasm pipelines" and did a
        MATLAB implementation task itself rather than delegating it. The
        outgoing edge ("delegates to: ...") is GENERATED from the live
        graph, so it can never go stale the way hand-written prose can.

        Deliberately NOT a per-node tool list (an earlier draft added one,
        generated via a throwaway Node construction against a stub
        adapter — see git history if that mechanism is ever needed again):
        Elvis's call was that generality beats exhaustiveness here — a full
        tool enumeration is exactly the kind of detail an agent should not
        need memorized from a roster to reason about what a peer can do.
        `description` stays short and general on purpose, capability not
        domain, and is shown after the objective topology facts, not in
        place of them.
        """
        spec = self._graph_spec
        entry = getattr(spec, "entry", None)
        nodes = getattr(spec, "nodes", {}) or {}
        edges = list(getattr(spec, "edges", ()))
        order = [entry] if entry in nodes else []
        for member in order:                  # grows while it is walked: BFS
            for e in edges:
                if e.source == member and e.target not in order:
                    order.append(e.target)
        if len(order) <= 1:
            return ""
        lines = []
        for n in order:
            agent = nodes.get(n)
            role = getattr(agent, "role", "") or "worker"
            senders = list(dict.fromkeys(
                e.source for e in edges if e.target == n))
            where = ("entry" if n == entry
                     else "tasks from " + ", ".join(senders))
            delegates_to = list(dict.fromkeys(
                e.target for e in edges if e.source == n and e.target in order))
            desc = (getattr(agent, "description", "") or "").strip()
            header = f"  {n}  (role: {role}; {where}"
            header += (f"; delegates to: {', '.join(delegates_to)})"
                       if delegates_to else ")")
            lines.append(
                header + (f"\n      {desc}" if desc else ""))
        return TEAM_ROSTER_TEMPLATE.format(members="\n".join(lines), name=name)

    def _resource_stanza(self, run_dir, *, for_worker: bool) -> str:
        """The static resource-envelope stanza — "what you HAVE" — injected into a
        worker/strategizer preamble at delegation start, so the agent stops
        running-and-hoping. O(1): cpu_count + one statvfs (no directory walk).
        Empty string on any failure (never fatal).

        ROLE-AWARE parallelism (deliberate): the cores/RAM/disk facts are shared,
        but only the WORKER is primed to parallelize — and only its EVALUATIONS
        within a campaign (compute speedup, same experiment/budget, epistemically
        neutral). The strategizer is NOT resource-nudged to fan out experiments:
        running multiple arms concurrently is an experimental-design decision with
        epistemic weight (budget splits, comparison validity) that lives in its own
        guidance — resource-priming it nudges breadth over disciplined comparison
        (observed run 20260628T224159: a 3-arm, unequal-budget, INCONCLUSIVE run)."""
        try:
            from ..infra.watchdog_cleanup import resource_envelope
            env = resource_envelope(run_dir or self.study_dir, self._mem_cap_bytes)
            cores = env["cores"]
            ram = (f"{env['ram_cap_bytes'] / 1024 ** 3:.1f} GB"
                   if env["ram_cap_bytes"] else "unset")
            disk = (f"{env['disk_free_bytes'] / 1024 ** 3:.0f} GB"
                    if env["disk_free_bytes"] is not None else "unknown")
            facts = (
                "resources: "
                f"~{cores} CPU cores · RAM cap {ram} per delegation. Using it is "
                "encouraged; exceeding it kills the process, so stream anything "
                f"larger · disk free {disk}.\n"
            )
            if for_worker:
                facts += (
                    "Use the cores: parallelize the EVALUATIONS within your "
                    "campaign (e.g. gen.call(mode='parallel'), or concurrent "
                    "candidate evaluations) to finish faster — same experiment, "
                    "just quicker. Size concurrency to the RAM cap.\n"
                )
            # Non-campaign roles (strategizer, critic, datagenerator, literature)
            # get the facts only — NO parallelism imperative. Fanning out
            # experiments is the strategizer's design call (KB 0004: one
            # delegation = one experiment), not something to resource-nudge.
            return facts
        except Exception:  # noqa: BLE001 — telemetry must never break a run
            return ""

    def _kb_menu(self, role, agent=None) -> str:
        """Audience-filtered handbook MENU injected at the head of an agent's
        prompt — so it always SEES the latent knowledge it can pull (mirroring
        how it always sees its tool list), instead of only discovering a chapter
        if it already thought to call ConsultHandbook. Cached; empty on failure.
        Empty too for a node whose ``nodes:`` list withholds ConsultHandbook: a
        menu that names a tool the node lacks is a prompt-vs-tool contradiction."""
        from .node_tools import withheld_closures
        if "ConsultHandbook" in withheld_closures(agent):
            return ""
        try:
            if getattr(self, "_kb", None) is None:
                from ..knowledge import KnowledgeBase
                self._kb = KnowledgeBase.load()
            return self._kb.menu(audience=role)
        except Exception:  # noqa: BLE001 — a missing menu must never break a run
            return ""

    def _resolve_base_url(self, name: str, agent: Agent, adapter_cls) -> str | None:
        """The endpoint a node's adapter uses: node config, then the top-level
        config, then this run's SLURM-served server; ``None`` leaves the
        adapter's own env/default. A node-level URL on a backend with no
        endpoint is an error; the run-wide ones apply only where one exists."""
        if "base_url" not in inspect.signature(adapter_cls.__init__).parameters:
            if agent.base_url:
                raise ValueError(
                    f"nodes.{name}.base_url is set but backend "
                    f"{agent.backend or self._backend!r} has no endpoint")
            return None
        return agent.base_url or self._base_url or self._served_base_url

    def _make_adapter(self, name: str, agent: Agent):
        run_dir = self._run_dir
        _role = getattr(agent, "role", None)

        # The run-aware cwd=study_dir + full entry <workspace> preamble is for the
        # graph's ENTRY/orchestrator node ONLY — it alone needs full-repo
        # visibility and doesn't itself write delegation-scoped worker files.
        # This used to key off "has ANY outgoing edge", which also matched
        # datagenerator/implementer (each has its own edge to
        # literature_reviewer, for sub-delegating a lookup — see _graphs.py)
        # even though both are sandboxed WORKERS everywhere else in their
        # contract (Delegate's own docstring: "writes exclusively to {id}/
        # relative to their workspace in debug/delegations/"). That mismatch
        # split delegation output across TWO physical trees for these two
        # roles — study_dir/debug/delegations/D### (this cwd) vs the
        # run-scoped run_dir/debug/delegations/D### their own preamble
        # promised — different inodes, same D### ids, no single source of
        # truth (run 20260718T132852, D010's retrospective: "cost an extra
        # stat/inode-comparison round-trip to notice").
        is_entry = run_dir and name == getattr(self._graph_spec, "entry", None)
        from ..backends.registry import get_adapter_class
        _backend_name = resolve_node_identity(
            agent, self._model, self._backend)[1]
        _adapter_cls = get_adapter_class(_backend_name)
        held = held_tools(
            agent, native=_adapter_cls.select_native_tools(agent.tools),
            native_set=getattr(_adapter_cls, "NATIVE_TOOLS", ()),
            delegates=bool(self._graph_spec.outgoing(name)))
        _own_base = agent.base_prompt == DEFAULT_PROMPT
        if _own_base and not getattr(_adapter_cls, "HAS_BASE_PROMPT", False):
            raise ValueError(
                f"node {name}: base_prompt: {DEFAULT_PROMPT} needs a backend "
                f"with its own default system prompt; {_backend_name!r} has "
                "none. Use the claude backend or remove base_prompt.")
        if is_entry:
            notes_dir = Path(run_dir) / "debug" / "strategizer_notes"
            debug_dir = Path(run_dir) / "debug"
            _path_tools = [f"{t}()" for t in ("Read", "WriteNote") if t in held]
            preamble = features.resolve_gates(
                RUN_PATHS_PREAMBLE_TEMPLATE, holds=held).format(
                path_tools=("Use these absolute paths when calling "
                            + " and ".join(_path_tools) + ".\n"
                            if _path_tools else ""),
                study_dir=self.study_dir,
                run_dir=run_dir,
                debug_dir=debug_dir,
                notes_dir=notes_dir,
                experiment_data_dir=Path(run_dir) / "experiment_data",
                resources=self._resource_stanza(run_dir, for_worker=False),
                knowledge=self._kb_menu(_role, agent),
                roster=self._team_roster(name),
            )
            system_prompt = ("" if _own_base else preamble
                             ) + features.strip_disabled_sections(
                agent.system_prompt)
            cwd = self.study_dir
        else:
            if run_dir is not None:
                workspace_dir = Path(run_dir) / "debug" / "delegations"
                workspace_dir.mkdir(parents=True, exist_ok=True)
            else:
                workspace_dir = self.study_dir
            # Only the implementer runs evaluation campaigns → only it gets the
            # eval-parallelism nudge; the critic/datagenerator/literature get the
            # resource facts alone.
            _is_campaign = getattr(agent, "role", None) == "implementer"
            preamble = features.resolve_gates(
                WORKSPACE_PREAMBLE_TEMPLATE, holds=held).format(
                workspace_dir=workspace_dir,
                study_dir=self.study_dir,
                entry=getattr(self._graph_spec, "entry", None) or "the entry agent",
                roster=self._team_roster(name),
                resources=self._resource_stanza(run_dir, for_worker=_is_campaign),
                knowledge=self._kb_menu(_role, agent),
            )
            system_prompt = ("" if _own_base else preamble
                             ) + features.strip_disabled_sections(
                agent.system_prompt)
            # Critics read from the study tree, not from a delegation subfolder.
            if getattr(agent, "role", None) == "critic":
                cwd = self.study_dir
            else:
                cwd = workspace_dir

        # The deliverable is pipeline.ipynb. Inject its contract — ROLE-AWARE:
        # only the strategizer authors it (it alone has the notebook tools); the
        # implementer/critic get the same structure framed for their job (fit
        # your code to it / judge against it), never an "author it" imperative.
        # Gated on pipeline_deliverable (default True): its injected text is an
        # unconditional imperative ("this SUPERSEDES every ... instruction
        # above") that previously overrode even a PROBLEM_STATEMENT.md saying
        # there is no pipeline deliverable (BACKLOG #27) — a study that
        # explicitly turns this off has no notebook contract to inject at all.
        from ..evaluation.notebook_exec import notebook_deliverable_spec
        _role = getattr(agent, "role", None)
        if _role in ("strategizer", "implementer", "critic"):
            system_prompt = (system_prompt + "[[if pipeline_deliverable]]"
                             + notebook_deliverable_spec(_role) + "[[/if]]")

        # The reproduction gate's exact preconditions — generated from
        # nodes/reproduction_gate.py::gate_contract(), itself extracted from
        # the gate's own docstring, so this can never drift into a paraphrase
        # of what the code enforces. Same role set and off-switch pattern as
        # pipeline_deliverable above; independent knob (reproduction_gate) —
        # a study can require a notebook without requiring it to reproduce,
        # or vice versa.
        if _role in ("strategizer", "implementer", "critic"):
            from ..nodes.reproduction_gate import gate_contract
            system_prompt = system_prompt + (
                "[[if reproduction_gate]]\n<reproduction_gate_contract>\n"
                + gate_contract()
                + "\n</reproduction_gate_contract>\n[[/if]]"
            )

        model, backend = resolve_node_identity(
            agent, self._model, self._backend)

        _persistent = not agent.reset_on_checkpoint
        _max_history_pairs = getattr(agent, "max_history_pairs", 5)

        # Study-scoped, NOT per-run: a study is typically run many times
        # (the same domain, evolving problem statement), and re-downloading
        # + re-embedding the same papers every run is pure waste with no
        # corresponding staleness risk — unlike cross-run FINDINGS/hypothesis
        # memory (rejected earlier as too risky, since a study's actual
        # scientific question genuinely can drift run to run), a paper's
        # relevance to a domain does not. Lives under runs/ (not the study
        # root) so it stays out of the user-facing study folder alongside
        # PROBLEM_STATEMENT.md/config.yaml/pipeline.ipynb — see
        # LiteratureCorpus for the cross-process FileLock this now requires
        # (two runs of the same study can genuinely overlap and both write).
        lit_reviewer_notes_dir = self.study_dir / "runs" / "lit_reviewer_notes"

        # Registry-driven, forward-compatible dispatch: resolve the adapter
        # class by backend name and let it choose its own native tools. Adding
        # a backend to backends/registry.py makes it dispatchable here with no
        # change to this method. Backend-specific endpoint/auth (base_url,
        # api_key) is resolved inside each adapter; base_url comes from
        # config.yaml (node, then top level), else the run's SLURM-served
        # endpoint, else the adapter's own env/default.
        from ..backends.registry import get_adapter_class

        adapter_cls = get_adapter_class(backend)
        native = adapter_cls.select_native_tools(agent.tools)

        _mcp = dict(getattr(agent, "mcp_servers", {}))
        _allowed = list(getattr(agent, "extra_allowed_tools", frozenset()))

        _endpoint = self._resolve_base_url(name, agent, adapter_cls)
        adapter = adapter_cls(
            model=model,
            system_prompt=system_prompt,
            study_dir=cwd,
            native_tools=native,
            extra_mcp_servers=_mcp,
            extra_allowed_tools=_allowed,
            persistent=_persistent,
            max_history_pairs=_max_history_pairs,
            **({"base_url": _endpoint} if _endpoint else {}),
        )
        adapter.base_prompt = agent.base_prompt
        # Universal read-only handbook lookup: EVERY node's adapter gets it
        # here, equally, at construction (copy() returns self, so the
        # per-invocation worker/critic paths inherit it). Single injection
        # point — do not duplicate it per path. The tool's description is owned
        # by _consult_handbook's docstring (the backend infers the schema from
        # the callable).
        from ..nodes.parsing import _consult_handbook
        adapter.closure_tools["ConsultHandbook"] = _consult_handbook

        extra_closures = agent.build_closure_tools(
            self.study_dir,
            lit_reviewer_notes_dir=lit_reviewer_notes_dir,
        )
        if extra_closures:
            adapter.closure_tools.update(extra_closures)
        if uses_default(agent.tools):
            self._watch_default_node(name, adapter, backend)
        return adapter
