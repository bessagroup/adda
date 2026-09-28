"""The prompt/gate provenance map must keep telling the truth.

``internal/tools/promptmap.py`` renders a map that cites, for every piece of
text an agent sees and every gate that can stop it, an exact ``file:line``.
Such a map is only worth anything if a stale one FAILS rather than quietly
citing code that moved. These tests are that failure:

* every gate symbol in the registry still resolves (the generator raises
  ``SystemExit`` when one does not);
* every section of every assembled prompt still resolves to a real file and
  a line inside it — so a prompt that moves takes its citation along;
* the Done() sequence is still parsed out of ``feedback.py``'s own tuple
  rather than hand-listed.

No network, no model, no API cost: everything is read off the live graph
objects and the AST.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The full build (see `data` below) assembles all 5 roles' prompts from the
# live corpus -- ~170s, measured. Every test in this module needs the SAME
# build, so it is session-scoped (not just module-scoped): the only two
# fixtures below are the sole source of that build in this file, and
# ``test_the_committed_map_matches_the_code`` used to bypass them entirely
# and call `promptmap_sync.live_data()`, which re-imports promptmap.py and
# reruns build() a SECOND time -- two ~170s builds in one file, one of them
# invisible to `data`'s cache. Marked `promptmap` so CI can run this module's
# tests once (in check_promptmap) instead of once per matrix leg (see
# pyproject.toml's markers and .github/workflows/pull_request.yml).
# The session-scoped `data` fixture rebuilds the whole prompt corpus
# (~100-170s, whichever test happens to be collected first pays for it --
# see pyproject.toml's own marker description) and this module's newest
# test (test_the_maps_tools_match_the_real_wiring_under_both_knob_values)
# does a SECOND full rebuild inside itself for the peer_interaction=False
# arm. Both comfortably exceed the global 120s per-test timeout on a
# loaded CI runner (observed: check_promptmap, ubuntu-latest/3.12, CI run
# for 23ff170 -- 103s locally for the first `data`-consuming test alone,
# no margin left for a slower shared runner). Matches the same
# @pytest.mark.timeout(600) pattern already used for real-inference tests
# in test_literature_corpus.py/test_literature_guardrails.py.
pytestmark = [pytest.mark.promptmap, pytest.mark.timeout(300)]

_ROOT = Path(__file__).resolve().parents[1]
_GEN = _ROOT / "internal" / "tools" / "promptmap.py"


def _load():
    spec = importlib.util.spec_from_file_location("_promptmap", _GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def promptmap():
    if not _GEN.exists():  # pragma: no cover - the generator is checked in
        # internal/tools/promptmap.py is committed to this repo, not an
        # optional dependency or a piece of local environment -- its absence
        # means the checkout is broken, not that some external condition is
        # unmet. A skip here would hide that behind a green-looking run.
        pytest.fail("internal/tools/promptmap.py is missing from the "
                    "checkout -- this is a repo defect, not an environment "
                    "condition to skip past")
    return _load()


@pytest.fixture(scope="session")
def data(promptmap):
    return promptmap.build()


def test_every_gate_symbol_still_exists(promptmap):
    """A renamed or deleted gate must break the build, not the meeting."""
    gates = promptmap.build_gates()
    assert gates, "the gate registry is empty"
    for gate in gates:
        assert gate["line"] > 0
        assert (_ROOT / gate["file"]).exists(), gate["symbol"]


def test_done_sequence_is_read_from_the_source_tuple(promptmap):
    chain = promptmap.done_chain()
    assert chain[0] == "_pending_refusal", (
        "Done()'s first gate changed; the map's ordering claim is now wrong")
    assert len(chain) >= 3


def test_every_prompt_section_resolves_to_a_real_line(data):
    """No section may be UNRESOLVED: a map with a blank citation is worse
    than no map, because the blank is the one a reviewer will ask about."""
    unresolved = []
    for role in data["roles"]:
        for layer in role["layers"]:
            for section in layer["sections"]:
                source = section.get("source")
                if not source:
                    # A tool the backend SDK supplies has no definition HERE; the
                    # map says so rather than inventing a citation for it.
                    if not section.get("external"):
                        unresolved.append(
                            f"{role['id']}/{layer['kind']}/{section['tag']}")
                    continue
                path = _ROOT / source["file"]
                assert path.exists(), source["file"]
                n_lines = path.read_text(encoding="utf-8").count("\n") + 1
                assert 1 <= source["line"] <= n_lines, source
    assert not unresolved, f"unresolved prompt sections: {unresolved}"


def test_shared_blocks_are_cited_at_their_definition_not_their_use(data):
    """The charter is injected into two roles; both must cite knowledge/,
    which is what makes "cite a clause number" a shared contract."""
    charter = [b for b in data["shared"] if b["name"] == "FALSIFICATION_CHARTER"]
    assert charter, "FALSIFICATION_CHARTER vanished from knowledge/charter.py"
    citing = [
        section["injects"]["file"]
        for role in data["roles"]
        for layer in role["layers"]
        for section in layer["sections"]
        if section.get("injects", {}).get("name") == "FALSIFICATION_CHARTER"
    ]
    assert len(citing) >= 2, "the charter should reach at least two roles"
    assert all(path.endswith("knowledge/charter.py") for path in citing), citing


def test_entry_node_carries_the_run_paths_preamble(data):
    entry = [r for r in data["roles"] if r["entry"]]
    assert len(entry) == 1
    kinds = [layer["label"] for layer in entry[0]["layers"]]
    assert "RUN_PATHS_PREAMBLE_TEMPLATE" in kinds
    workers = [r for r in data["roles"] if not r["entry"]]
    for worker in workers:
        labels = [layer["label"] for layer in worker["layers"]]
        assert "WORKSPACE_PREAMBLE_TEMPLATE" in labels, worker["id"]


def _sources(data):
    for role in data["roles"]:
        for layer in role["layers"]:
            for key in ("definition", "assembled_at"):
                if layer.get(key):
                    yield f"{role['id']}/{layer['kind']}/{key}", layer[key]
            for section in layer["sections"]:
                if section.get("source"):
                    yield f"{role['id']}/{layer['kind']}/{section['tag']}", section["source"]


def test_a_citation_never_claims_a_span_it_did_not_verify(data):
    """The map may under-resolve; it may not overstate.

    An earlier generator searched a fixed 240-character head of each template
    and then reported that head's line count as the block's end — labelled
    ``exact``. Every template longer than seven lines was cited short while
    looking precise, which is the one failure mode a provenance map cannot
    have. A span is now only reported by a probe that matched the whole text
    (``exact``), read the syntax tree (``ast``), or matched line by line and
    says which end is which (``lines``); anything else is an anchor with no
    end line at all.
    """
    for where, source in _sources(data):
        assert source["match"] in {
            "exact", "ast", "lines", "line", "tag", "symbol", "docstring",
            "literal"}, where
        if source.get("span"):
            assert source["line_end"] >= source["line"], where
        else:
            assert "line_end" not in source, f"{where} reports an unverified end line"


def test_an_exact_citation_spans_exactly_the_text_it_cites(data):
    """``exact`` means the block is verbatim at those lines — so the line
    count of the cited text and of the cited span must agree."""
    for role in data["roles"]:
        for layer in role["layers"]:
            for section in layer["sections"]:
                source = section.get("source") or {}
                if source.get("match") != "exact":
                    continue
                span = source["line_end"] - source["line"]
                assert span == section["text"].strip("\n").count("\n"), (
                    f"{role['id']}/{section['tag']}: cited {source['file']}:"
                    f"{source['line']}-{source['line_end']} for a "
                    f"{section['text'].count(chr(10)) + 1}-line block")


def test_text_built_elsewhere_is_cited_to_the_code_that_builds_it(data):
    """The ``.format()`` fields in the preambles are whole stanzas written in
    ``agent_runtime.py``. Folding them into the template's own citation is how
    a resource stanza ends up attributed to ``agent_prompts.py``, where nobody
    searching for it will ever find it. The preamble stays ONE readable block,
    so the attribution lives in its parts.

    ``{roster}`` is in every preamble: one team roster, the same for every
    agent (it was the entry node's delegation targets alone, and workers were
    told nothing about the team).
    """
    for role in data["roles"]:
        preamble = role["layers"][0]
        assert len(preamble["sections"]) == 1, (
            f"{role['id']}: the preamble is one prompt and must read as one block")
        parts = preamble["sections"][0]["parts"]
        fields = {part["field"]: part for part in parts if part.get("field")}
        assert set(fields) == {"{resources}", "{knowledge}", "{roster}"}, role["id"]
        for field, part in fields.items():
            assert part["source"]["file"].endswith("runtime/agent_runtime.py"), field


def test_the_preamble_parts_reassemble_into_the_preamble(data):
    """Whatever the map says about where each stretch comes from, the stretches
    in order ARE the prompt — a reader may assume concatenation, because that
    is what ``.format()`` does."""
    for role in data["roles"]:
        section = role["layers"][0]["sections"][0]
        assert "".join(p["text"] for p in section["parts"]) == section["text"]
        assert section["chars"] == len(section["text"])


def test_an_editable_section_is_exactly_the_lines_it_cites(data):
    """The page offers to edit a section only when the map can write the edit
    back. That promise is only safe if the cited lines ARE the section: read
    ``file`` from ``line`` to ``line_end`` and you must get the text back,
    character for character, or applying an edit would corrupt the source.

    Sections the map located by a weaker probe are marked not-editable with a
    reason instead — the same discipline as the citations themselves: never
    offer precision the generator cannot actually deliver.
    """
    seen: set[str] = set()
    for role in data["roles"]:
        for layer in role["layers"]:
            for section in layer["sections"]:
                edit = section["edit"]
                if not edit["ok"]:
                    assert edit["why"], f"{role['id']}: not-editable with no reason"
                    continue
                if edit.get("mode") == "literal":
                    # A different promise, just as checkable: the text is a
                    # stretch INSIDE the cited literal, appearing exactly once,
                    # so an edit replaces that stretch and nothing else.
                    import ast as _ast
                    src = (_ROOT / edit["file"]).read_text(encoding="utf-8")
                    node = next(
                        n for n in _ast.walk(_ast.parse(src))
                        if isinstance(n, _ast.Constant) and isinstance(n.value, str)
                        and n.lineno == edit["line"] and n.end_lineno == edit["line_end"]
                    )
                    assert node.value.count(section["text"].strip("\n")) == 1, (
                        f"{role['id']}/{section['tag']}: the cited literal does not "
                        f"hold this text exactly once, so an edit would have to guess")
                    continue
                if edit.get("mode") == "docstring":
                    # A docstring span is the LITERAL — quotes and indentation
                    # included — so the promise is different but just as checkable:
                    # the text shown must be what Python reads out of it.
                    import ast as _ast
                    src = (_ROOT / edit["file"]).read_text(encoding="utf-8")
                    node = next(
                        n for n in _ast.walk(_ast.parse(src))
                        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                        and n.body and getattr(n.body[0], "lineno", None) == edit["line"]
                    )
                    assert _ast.get_docstring(node) == section["doc"], (
                        f"{role['id']}/{section['label']}: the catalog shows text the "
                        f"cited docstring does not contain")
                    continue
                lines = (_ROOT / edit["file"]).read_text(encoding="utf-8").splitlines()
                span = "\n".join(lines[edit["line"] - 1:edit["line_end"]])
                assert span == section["text"].strip("\n"), (
                    f"{role['id']}/{section['tag']}: {edit['file']}:{edit['line']}-"
                    f"{edit['line_end']} is not the text the page would let you edit")
                seen.add(edit["key"])
    assert seen, "no section is editable — the page has nothing to offer"


def test_shared_text_says_who_else_reads_it(data):
    """The charter is one block injected into several roles. Editing it from one
    role's page changes what the others see, so the page must say so before the
    edit, not after it lands."""
    charter = [
        section["edit"]
        for role in data["roles"]
        for layer in role["layers"]
        for section in layer["sections"]
        if section.get("injects", {}).get("name") == "FALSIFICATION_CHARTER"
        and section["edit"]["ok"]
    ]
    assert charter, "the charter is no longer an editable shared block"
    for edit in charter:
        assert edit["shared"]["also"], "a shared block that names no other reader"


def test_a_gate_offers_only_what_it_tells_the_agent(promptmap):
    """A gate card shows, and offers to change, only its messages -- the text
    that lands in the agent's context. Its docstring is not prompt (no agent
    reads it), so the card carries none; its code and the map's one-line
    effect summary are not editable either."""
    for gate in promptmap.build_gates():
        assert "doc" not in gate and "edit" not in gate, gate["symbol"]
        for m in gate["messages"]:
            want = "constant" if m.get("constant") else "message"
            assert m["edit"]["mode"] == want, gate["symbol"]


def test_a_gate_message_is_what_its_cited_expression_renders(promptmap):
    """A gate's nudge is prompt: it lands in the agent's context and steers it.

    The map shows it, so the map must be able to prove it. Re-parse the cited
    expression and render it the same way — it must come back identical, or the
    page is showing text the source does not produce.
    """
    import ast

    total = 0
    for gate in promptmap.build_gates():
        for message in gate["messages"]:
            total += 1
            if message.get("constant"):
                continue    # proved by test_a_named_constant_shows_its_own_text
            # The segment is a multi-line expression lifted out of its
            # parentheses, so give it its own before re-parsing.
            expr = ast.parse("(" + message["source_expr"] + ")", mode="eval").body
            assert promptmap._render_message(expr) == message["text"], (
                f"{gate['symbol']}: the card shows a message its source does not render")
            assert message["edit"]["ok"] and message["edit"]["mode"] == "message"
    assert total >= 10, "the gates stopped saying anything to the agent — suspicious"


def test_a_named_constant_shows_its_own_text(promptmap):
    """``return prefix + _EXIT_INTERVIEW`` used to render as the placeholder
    ``<prefix><_EXIT_INTERVIEW>``, so the interview text the strategizer reads
    appeared nowhere on the map. A message handed over by name must show the
    constant's value, cited to its own assignment."""
    import importlib

    seen = set()
    for gate in promptmap.build_gates():
        for m in gate["messages"]:
            if not m.get("constant"):
                continue
            mod = m["file"].removeprefix("src/").removesuffix(".py").replace("/", ".")
            assert getattr(importlib.import_module(mod), m["constant"]) == m["text"]
            seen.add(m["constant"])
    assert {"_EXIT_INTERVIEW", "_FAILED_RETROSPECTIVE"} <= seen


def test_reflection_covers_every_role_and_cites_real_lines(data):
    """Every role in the map has a reflection row, and every citation in it
    points inside a real file. A worker whose report_sections ask for a
    ### Retrospective must have the line in its prompt that asks."""
    rows = {r["role"]: r for r in data["reflection"]["rows"]}
    assert set(rows) == {r["id"] for r in data["roles"]}
    for row in rows.values():
        for cite in row["asks"] + row["enforced"] + row["recorded"]:
            if "file" not in cite:
                continue
            lines = (ROOT / cite["file"]).read_text(encoding="utf-8").splitlines()
            assert 1 <= cite["line"] <= len(lines), cite
        if not row["when"].startswith("Not asked"):
            assert row["asks"], row["role"]
            for cite in row["asks"]:
                lines = (ROOT / cite["file"]).read_text(encoding="utf-8").splitlines()
                span = lines[cite["line"] - 1:cite.get("line_end", cite["line"])]
                assert "Retrospective" in "\n".join(span), cite


def test_a_computed_block_is_never_silently_reported_as_absent(data):
    """A generated map that claims a prompt section is empty, when every real
    run injects one, is worse than a map that fails to build.

    The `{roster}` helper first guessed at an import, failed, and hit its own
    `except Exception: return ""`. The map then printed "roster is empty for
    strategizer — nothing is injected" and looked perfectly fine. The entry
    role's roster is non-empty on the default graph, so an empty one means the
    builder broke, not that the prompt changed.
    """
    entry = [r for r in data["roles"]
             if r["layers"][0]["label"] == "RUN_PATHS_PREAMBLE_TEMPLATE"]
    assert entry, "no entry role found"
    for role in entry:
        parts = {p["field"]: p for p in role["layers"][0]["sections"][0]["parts"]
                 if p.get("field")}
        roster = parts["{roster}"]
        assert not roster["empty"], (
            f"{role['id']}: the roster rendered empty — the builder is broken, "
            "since the default graph wires four delegation targets")
        assert "<team>" in roster["text"]


def test_the_rendered_roster_matches_the_graph_it_claims_to_describe(data):
    """The map enumerates its roles from `_graphs._default_graph()`; the roster
    must come from the same graph, or the page shows two different systems."""
    from adda._src.agents import _graphs

    graph = _graphs._default_graph()
    targets = {e.target for e in graph.edges if e.source == graph.entry}

    entry = next(r for r in data["roles"]
                 if r["layers"][0]["label"] == "RUN_PATHS_PREAMBLE_TEMPLATE")
    text = next(p["text"] for p in entry["layers"][0]["sections"][0]["parts"]
                if p.get("field") == "{roster}")

    for t in targets:
        assert t in text, f"{t} is wired but missing from the rendered roster"


# ---------------------------------------------------------------------------
# A tool the agent gets must be a tool the map shows
#
# render_tool_catalog builds the <tools> block from the LIVE closure dict's
# keys, so ConsultHandbook -- bound at agent_runtime.py as
# closure_tools["ConsultHandbook"] = _consult_handbook -- is really in every
# agent's prompt. The map built its catalog by scanning PascalCase `def`s, and
# that function is snake_case, so the one tool every single node is given was
# absent from the page that claims to be the agent's-eye view.
# ---------------------------------------------------------------------------


def test_every_role_shows_the_universally_injected_handbook_tool(data):
    for role in data["roles"]:
        assert "ConsultHandbook" in role["tools"], (
            f"{role['id']}: gets ConsultHandbook at runtime but the map omits it"
        )


def test_the_injected_tool_carries_its_real_docstring(data):
    """Showing the name without the text would be a different lie: the agent
    reads _consult_handbook's docstring, so the map must cite that."""
    for role in data["roles"]:
        catalog = next(lyr for lyr in role["layers"] if lyr["kind"] == "catalog")
        entry = next((s for s in catalog["sections"]
                      if s.get("label") == "ConsultHandbook"), None)
        assert entry is not None, f"{role['id']}: no ConsultHandbook catalog entry"
        assert "handbook" in entry["text"].lower()
        assert "chapter" in entry["text"].lower()


def test_a_per_delegation_rebind_is_not_treated_as_universal():
    """delegation.py's _sandbox_worker_writes rebinds closure_tools["Write"]
    per delegation. That is not a universal injection, and adding it would
    put Write in the strategizer's catalog -- a tool it does not have."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                           / "internal" / "tools"))
    import promptmap as pm

    assert "Write" in pm.injected_tool_docs(), "the Write rebind should still resolve"
    assert "Write" not in pm.universal_tool_names()


# ---------------------------------------------------------------------------
# A .format()-assembled block has no single home. Its pieces do.
#
# annotate_edits refused the WHOLE run_paths preamble because it carries
# parts -- so the template's own prose, which is a verbatim span of
# agent_prompts.py, and the prose inside a computed stanza, which is a string
# constant in the module that builds it, were both unreachable from the page.
# Only the formatted-in values have no source text to edit.
# ---------------------------------------------------------------------------


def _all_pieces(data):
    return [(role["id"], sec, pc)
            for role in data["roles"]
            for lyr in role["layers"]
            for sec in lyr.get("sections", [])
            for pc in (sec.get("pieces") or [])]


def test_the_assembled_preamble_offers_its_written_pieces(data):
    for role in data["roles"]:
        sec = next(s for lyr in role["layers"] for s in lyr.get("sections", [])
                   if s.get("parts"))
        assert sec["pieces"], f"{role['id']}: assembled section offers nothing"
        assert not sec["edit"]["ok"], "the block as a whole still has no one home"


def test_no_piece_is_offered_without_somewhere_to_write_it(data):
    """The map's standing rule: editability is derived from the citation,
    never asserted. A box with no span behind it is the failure being avoided."""
    for role_id, _sec, pc in _all_pieces(data):
        assert pc["edit"]["ok"] and pc["edit"].get("key"), role_id
        # A docstring is its own kind of home: one literal, indented in the
        # source, whose cleaned text is what the model reads.
        assert pc["edit"]["mode"] in ("span", "literal", "docstring")
        assert pc["edit"]["file"] and pc["edit"]["line"]


def test_every_pieces_citation_actually_holds_its_text(data):
    """Re-resolve each piece against the tree. A citation the map cannot
    reproduce is exactly the overstatement this page exists to avoid."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "internal" / "tools"))
    import promptmap as pm

    for role_id, _sec, pc in _all_pieces(data):
        text = pc["text"].strip("\n")
        if pc["edit"]["mode"] == "literal":
            held = pm.containing_literal(text, [root / pc["edit"]["file"]])
            assert held is not None, f"{role_id}: {pc['label']!r} no longer resolves"
            assert held["file"] == pc["edit"]["file"]
        elif pc["edit"]["mode"] == "docstring":
            import ast
            import inspect
            tree = ast.parse((root / pc["edit"]["file"]).read_text(encoding="utf-8"))
            docs = [inspect.cleandoc(n.value) for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.lineno == pc["edit"]["line"]
                    and n.end_lineno == pc["edit"]["line_end"]]
            assert text in docs, (
                f"{role_id}: {pc['label']!r} is not the docstring at its citation")
        else:
            src = (root / pc["edit"]["file"]).read_text(encoding="utf-8")
            assert text in src, f"{role_id}: {pc['label']!r} is not in its cited file"


def test_the_computed_resource_stanza_is_reachable(data):
    """Its prose is an f-string constant with one home, even though its
    numbers are supplied per run -- so the wording IS editable from the page."""
    fields = {pc.get("field") for _r, _s, pc in _all_pieces(data)}
    assert "{resources}" in fields


# --- the gap these tests had ------------------------------------------------

def test_the_committed_map_matches_the_code(data):
    """Every test above builds the map FRESH and asserts against that, so a
    stale ``internal/promptmap.html`` passed all of them.

    It did. The committed map named a tool in the literature reviewer's
    prompt that had been renamed three commits earlier, and the suite was
    green throughout -- the map is the artifact a person reads to see what
    the agents are told, so a stale one is a wrong answer delivered
    confidently.

    This is the arrow a script CAN check: code -> committed file. The other
    arrow, committed file -> published artifact, goes through the Artifact
    tool and cannot be reached from here; ``promptmap_sync.py`` reports that
    one, and a scheduled session acts on it.

    ``live`` is the SAME ``data`` every other test in this module already
    built (the ``data`` fixture above IS ``promptmap.build()``, byte-for-byte
    what ``promptmap_sync.live_data()`` would also produce by re-importing
    and re-running the same generator) -- calling ``live_data()`` here used
    to rebuild the whole prompt corpus a second time (measured ~170s) for a
    result identical to what ``data`` already held.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_promptmap_sync", _ROOT / "internal" / "tools" / "promptmap_sync.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

    committed, live = sync.committed_data(), data
    drift = sync.differences(committed, live)
    assert sync.content_hash(committed) == sync.content_hash(live), (
        "internal/promptmap.html is stale. Run `make promptmap` and commit it."
        + ("\n  " + "\n  ".join(drift) if drift else
           "\n  (no section-level drift: a citation moved, or the generator "
           "changed shape)"))


@pytest.fixture(scope="module")
def sync():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_promptmap_sync", _ROOT / "internal" / "tools" / "promptmap_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_citation_only_shift_does_not_move_the_hash(sync, data):
    """AST line attribution differs across Python versions (3.10/3.11 vs.
    3.12/3.13); zero prompt text differs between them, only where the
    compiler says a line begins. A provenance-map hash that fails on that is
    failing the build for the wrong reason -- this is the regression test for
    the CI flip that motivated ``_scrub_citations``."""
    import copy

    shifted = copy.deepcopy(data)

    def walk(value):
        if isinstance(value, dict):
            for field in ("line", "line_end", "doc_line", "doc_line_end"):
                if field in value:
                    value[field] += 1
            if "key" in value and "line" in value:
                value["key"] = "{}:{}-{}".format(
                    value.get("file", "x"), value["line"], value["line_end"])
            if isinstance(value.get("why"), str):
                value["why"] = re.sub(r"\d+", lambda m: str(int(m.group()) + 1),
                                       value["why"])
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(shifted)
    assert sync.content_hash(data) == sync.content_hash(shifted)


def test_a_citation_key_beside_no_line_sibling_still_hashes(sync):
    """The ``_switches()`` block uses ``key`` for a config-key NAME, not a
    line citation -- it never sits beside a ``line`` field. Renaming one must
    still move the hash, or the scrub is too broad."""
    old = {"switches": [{"key": "eval_budget", "kind": "config.yaml runtime:"}]}
    new = {"switches": [{"key": "budget", "kind": "config.yaml runtime:"}]}
    assert sync.content_hash(old) != sync.content_hash(new)


def test_prompt_text_drift_still_fails(sync, data):
    """The anti-weakening check: real content changes -- section text, a
    section's tag/label, a tool roster, or a gate -- must still move the
    hash. Only citations may move for free."""
    import copy

    base_hash = sync.content_hash(data)

    text_changed = copy.deepcopy(data)
    text_changed["roles"][0]["layers"][0]["sections"][0]["text"] += " extra"
    assert sync.content_hash(text_changed) != base_hash

    tag_changed = copy.deepcopy(data)
    tag_changed["roles"][0]["layers"][0]["sections"][0]["tag"] = "zzz-changed"
    assert sync.content_hash(tag_changed) != base_hash

    tools_changed = copy.deepcopy(data)
    role = tools_changed["roles"][0]
    role["tools"] = list(role["tools"]) + ["NotARealTool"]
    assert sync.content_hash(tools_changed) != base_hash

    gate_added = copy.deepcopy(data)
    gate_added["gates"] = list(gate_added["gates"]) + [dict(gate_added["gates"][0])]
    assert sync.content_hash(gate_added) != base_hash


def test_the_publication_record_is_present_and_well_formed():
    """``promptmap.published.json`` is how the repository remembers what is
    on claude.ai, since it cannot ask. If it rots, the sync report starts
    lying in the reassuring direction."""
    import json

    record = _ROOT / "internal" / "promptmap.published.json"
    assert record.exists(), (
        "internal/promptmap.published.json is missing -- without it nothing "
        "knows whether the published artifact is current")
    data = json.loads(record.read_text(encoding="utf-8"))
    for field in ("url", "artifact_version", "content_hash", "chrome_hash",
                  "published_at"):
        assert data.get(field), f"the publication record has no {field!r}"
    assert len(data["content_hash"]) == 64, "content_hash is not a sha256"
    assert len(data["chrome_hash"]) == 64, "chrome_hash is not a sha256"


def test_the_two_hashes_see_different_halves_of_the_page(tmp_path):
    """Why there are two of them.

    ``content_hash`` reads the DATA block and ``chrome_hash`` reads
    everything else, so each must be blind to the other's half -- otherwise
    one of them is redundant and the pair gives false confidence. The
    presentation half is the one that went unwatched: a collapse control
    shipped to the repository, the artifact never got it, and the sync
    report said IN SYNC the whole time because no prompt had changed.

    ``sync.MAP`` is repointed at a copy rather than the committed file being
    edited in place: a test that rewrites a tracked file leaves the working
    tree dirty if it dies between the write and the restore.
    """
    import json

    spec = importlib.util.spec_from_file_location(
        "_promptmap_sync", _ROOT / "internal" / "tools" / "promptmap_sync.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

    def data_of(text):
        return json.loads(re.search(r"const DATA = (\{.*?\});\n", text, re.S).group(1))

    html = (_ROOT / "internal" / "promptmap.html").read_text(encoding="utf-8")
    copy = tmp_path / "promptmap.html"
    copy.write_text(html, encoding="utf-8")
    sync.MAP = copy
    before = sync.chrome_hash()

    restyled = html.replace("--r:3px;", "--r:7px;", 1)
    assert restyled != html, "the stylesheet no longer carries --r"
    copy.write_text(restyled, encoding="utf-8")
    assert sync.chrome_hash() != before, (
        "chrome_hash did not move when the stylesheet did -- it is not "
        "watching presentation")
    assert sync.content_hash(data_of(restyled)) == sync.content_hash(data_of(html)), (
        "content_hash moved on a restyle -- it is supposed to be blind to "
        "how the page looks")

    # ... and the other way round: a prompt edit must move content_hash and
    # leave chrome_hash alone, or the two are not a partition of the file.
    copy.write_text(html.replace("You are the Strategizer", "You are the STRATEGIZER", 1),
                    encoding="utf-8")
    assert sync.chrome_hash() == before, (
        "chrome_hash moved on a prompt edit -- it is reading the DATA block")
    assert sync.content_hash(data_of(copy.read_text(encoding="utf-8"))) != \
        sync.content_hash(data_of(html))


def test_every_tool_written_in_this_repository_is_editable(promptmap):
    """A tool whose description lives in this repository can be edited from
    the map -- whole, or as the pieces it is assembled from.

    Refusal used to be the default for anything the name scan could not pin
    down: a closure registered under a name its function does not have, a
    name defined twice. Adding the tools agents build for themselves to the
    map turned a dozen entries into "not editable from this page" at once.
    Only tools the backend SDK supplies have no source here to edit --
    and the two whose docstring is legitimately rebuilt PER NODE at
    dispatch (``_PER_NODE_DOC``: Delegate, AskForFeedback), which name the
    exact builder to edit instead. Delegate started actually appearing in a
    role's tools only once the catalog stopped hand-mirroring the routing
    wiring (it used to be silently absent, so this exception was dormant).
    """
    import json

    html = (_ROOT / "internal" / "promptmap.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const DATA = (\{.*?\});\n", html, re.S).group(1))
    refused = []
    for role in data["roles"]:
        for layer in role["layers"]:
            if layer["kind"] != "catalog":
                continue
            for sec in layer["sections"]:
                if sec.get("label") in promptmap._PER_NODE_DOC:
                    continue
                ok = (sec.get("edit") or {}).get("ok")
                pieces_ok = sec.get("pieces") and all(
                    p["edit"]["ok"] for p in sec["pieces"])
                if not (ok or pieces_ok or sec.get("external")):
                    refused.append((role["id"], sec.get("label"),
                                    (sec.get("edit") or {}).get("why", "")[:80]))
    assert not refused, refused



def _real_routing_tools(graph, out_edges: dict, name: str, agent, keep: set) -> set:
    """Independently re-derive a role's granted routing tool names via a
    real Node -- NOT by calling promptmap.py's own ``_routing_tool_names``,
    so this can catch a future drift inside that function too, not only in
    whatever still hand-mirrors the wiring around it."""
    from adda._src.nodes import Node

    class _Stub:
        def __init__(self) -> None:
            self.closure_tools: dict = {}

    node = Node(
        _Stub(), name=name, outgoing=out_edges.get(name, []), spec=graph,
        agent_tools=agent.tools,
    )
    return set(node.adapter.closure_tools) & keep


def test_the_maps_tools_match_the_real_wiring_under_both_knob_values(promptmap, data):
    """The <tools> catalog's routing-wired subset (Delegate/Wait/SendMessage/
    RecallHistory/Confer/Reply/FollowUp) must equal what a real Node
    actually grants, under BOTH ``peer_interaction`` values -- the guard
    against a third drift (a hand-maintained mirror already missed
    ConsultHandbook once and SendMessage entirely a second time)."""
    from adda._src.runtime import settings

    routing_tool_names = {
        "Delegate", "Wait", "SendMessage", "RecallHistory", "Confer",
        "Reply", "FollowUp",
    }
    graph = promptmap._map_graph()
    out_edges: dict[str, list[str]] = {}
    for edge in graph.edges:
        out_edges.setdefault(edge.source, []).append(edge.target)

    # The default arm: `data` (session-scoped, already built under today's
    # ambient/unconfigured settings -- the shipped default) is the map's
    # OWN output, checked as-is rather than rebuilt.
    for role in data["roles"]:
        name, agent = role["id"], graph.nodes[role["id"]]
        mapped = set(role["tools"]) & routing_tool_names
        real = _real_routing_tools(graph, out_edges, name, agent, routing_tool_names)
        assert mapped == real, (
            f"{name} (default arm): map shows {mapped}, real wiring grants {real}"
        )

    # The off arm: peer_interaction is the one Feature whose OFF state
    # changes this list. Checked against promptmap._routing_tool_names
    # directly rather than a second full promptmap.build_roles() rebuild
    # (~100-200s) -- the default arm's check above already confirms
    # build_roles() correctly wires in whatever _routing_tool_names()
    # returns (that delegation is knob-independent code in build_roles()
    # itself), so the off arm's own risk is entirely inside
    # _routing_tool_names()'s knob-handling, which this still exercises
    # directly and fully. Keeps this test's own cost near-instant instead
    # of a second corpus rebuild.
    settings.configure({"peer_interaction": False})
    try:
        for name, agent in graph.nodes.items():
            mapped = set(promptmap._routing_tool_names(
                graph, out_edges, name, agent)) & routing_tool_names
            real = _real_routing_tools(
                graph, out_edges, name, agent, routing_tool_names)
            assert mapped == real, (
                f"{name} (peer_interaction=False): map shows {mapped}, "
                f"real wiring grants {real}"
            )
    finally:
        settings.configure(None)
