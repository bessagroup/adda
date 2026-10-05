"""The viewer, driven by a real browser.

WHY THIS FILE EXISTS
    ``test_viewer_app.py`` (52 tests) and ``test_viewer_readers.py`` (62) stop
    at the HTTP boundary: they prove the server answers correctly. The viewer's
    behaviour does not live there. ``templates/graph.html`` is 118 KB and 2,814
    lines carrying ~90 functions, and every one of them was unexecuted by the
    suite — the SSE subscription that makes watching a run "live", the node
    status the watcher reads, and the answer box a blocked run waits on.

    A run that stalls because the answer box silently stopped posting is a
    functional failure with a real cost: the agent waits, the wall clock burns,
    and every server test still passes.

WHAT IT ASSERTS, AND WHAT IT DELIBERATELY DOES NOT
    Function only, never appearance. No screenshots, no layout, no colours, no
    element positions. Each test names a thing the viewer is FOR: the page
    renders the run's nodes; a delegation written while the page is open
    changes what is on screen without a reload; an answer typed into the box
    reaches the file the run reads; a closed run stops soliciting answers. A
    test that fails when someone restyles a button would be worse than no test,
    because it would be deleted the first time it cried wolf.

    Uncaught JavaScript errors fail every test here, whatever else passed. A
    page that renders and throws is broken in a way an HTTP assertion cannot
    see.

IF THIS IS SKIPPED, IT IS PROTECTING NOTHING
    It needs Playwright and a Chromium binary. Both are present in the
    development image (PLAYWRIGHT_BROWSERS_PATH), and the module skips rather
    than errors where they are not — so a green run on a machine without them
    means "not checked", not "checked and fine".
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from adda._src.infra import operator_channel
from adda._src.viewer.app import create_app

from .test_viewer_app import _LiveServer, _make_run, _make_study, _write_jsonl

sync_playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed; the viewer's browser behaviour is "
           "UNCHECKED in this run",
).sync_playwright


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        for launch in ({"executable_path": "/opt/pw-browsers/chromium"}, {}):
            try:
                b = pw.chromium.launch(
                    args=["--ignore-certificate-errors"], **launch)
                break
            except Exception:                            # noqa: BLE001
                b = None
        if b is None:
            pytest.skip("no chromium binary; viewer browser behaviour UNCHECKED")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A page that fails its test on any uncaught JavaScript error."""
    # The page pulls Alpine and marked from CDNs (see the note in
    # test_the_viewer_needs_the_internet_to_work). Behind this environment's
    # TLS-inspecting proxy that is a certificate error, not a network one, so
    # the context accepts it -- otherwise every test here would be measuring
    # the proxy rather than the viewer.
    ctx = browser.new_context(ignore_https_errors=True)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.errors = errors                                   # type: ignore[attr-defined]
    yield pg
    ctx.close()
    assert not errors, f"uncaught JavaScript on the page: {errors}"


def _run_with_delegations(tmp_path: Path, rows: list[dict]) -> tuple[Path, str]:
    study = _make_study(tmp_path)
    run_id = "20260917T120000"
    run_dir = _make_run(study, run_id)
    _write_jsonl(run_dir / "debug" / "delegation_log.jsonl", rows)
    return study, run_id


def _delegation(did: str, to_node: str, status: str = "DONE") -> dict:
    """A row with the fields DelegationLog.record actually writes.

    The timestamps are not decoration: the timeline places a delegation by
    ``started_at``, and a row without one is ingested (the "no delegations"
    notice goes away) but never drawn — which is itself worth knowing, and is
    why this fixture mirrors the real writer rather than the minimum the HTTP
    tests need.
    """
    return {"id": did, "status": status, "from_node": "strategizer",
            "to_node": to_node, "task": "do the thing", "deliverable": "",
            "hypothesis_ids": [], "started_at": "2026-09-17T12:00:00+00:00",
            "completed_at": "2026-09-17T12:05:00+00:00",
            "tokens_in": 10, "tokens_out": 20, "evals": 0}


def _open(page, server, run_id: str):
    page.goto(f"{server.url}/runs/{run_id}")
    page.wait_for_selector("text=strategizer", timeout=15_000)


# --- the page works at all --------------------------------------------------

def test_the_run_page_renders_the_graphs_nodes(tmp_path, page):
    """The bluntest check there is, and the one that catches a page whose
    framework threw before painting: every node in the run's graph must be on
    screen."""
    study, run_id = _run_with_delegations(tmp_path, [])

    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        body = page.inner_text("body")

    assert "strategizer" in body
    assert "critic" in body


def test_a_delegation_in_the_log_is_shown(tmp_path, page):
    study, run_id = _run_with_delegations(
        tmp_path, [_delegation("D001", "critic")])

    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        page.wait_for_selector("text=D001", timeout=15_000)


# --- the thing the viewer is FOR: watching, live ----------------------------

def test_a_delegation_written_while_watching_appears_without_a_reload(
        tmp_path, page):
    """The whole point of the viewer. The SSE subscription is what makes it
    "watching" rather than "a page you refresh", and nothing below the browser
    can tell you it still works."""
    study, run_id = _run_with_delegations(
        tmp_path, [_delegation("D001", "critic")])
    log = study / "runs" / run_id / "debug" / "delegation_log.jsonl"

    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        page.wait_for_selector("text=D001", timeout=15_000)

        with log.open("a", encoding="utf-8") as fh:      # the run, still going
            fh.write(json.dumps(_delegation("D002", "critic", "RUNNING")) + "\n")

        page.wait_for_selector("text=D002", timeout=20_000)


# --- the human-in-the-loop path ---------------------------------------------

def test_an_answer_typed_in_the_browser_reaches_the_run(tmp_path, page):
    """A blocked run waits on this box. If it stops posting, the agent waits,
    the wall clock burns, and every server-side test still passes."""
    study, run_id = _run_with_delegations(tmp_path, [])
    run_dir = study / "runs" / run_id
    qid = operator_channel.ask_question(
        run_dir, "strategizer", "Which objective should I minimise?")
    assert qid, "fixture failed to record a question"

    with _LiveServer(create_app(study, token="t0k3n")) as server:
        page.goto(f"{server.url}/session?token=t0k3n&next=/runs/{run_id}")
        page.wait_for_selector("text=strategizer", timeout=15_000)
        page.wait_for_selector("text=Which objective", timeout=15_000)

        box = page.locator("textarea, input[type=text]").filter(visible=True)
        box.first.fill("Minimise the Branin function.")
        page.get_by_role("button", name="Send", exact=False).first.click()

        for _ in range(100):                             # the file is the truth
            if not operator_channel.pending_questions(run_dir):
                break
            time.sleep(0.1)

    assert not operator_channel.pending_questions(run_dir), (
        "the answer never reached the run: the question is still pending")


def test_a_question_is_shown_before_it_is_answered(tmp_path, page):
    """The half that fails silently: a question the watcher never sees is a
    run that waits until it gives up."""
    study, run_id = _run_with_delegations(tmp_path, [])
    run_dir = study / "runs" / run_id
    operator_channel.ask_question(run_dir, "strategizer", "UNIQUE-QUESTION-TEXT")

    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        page.wait_for_selector("text=UNIQUE-QUESTION-TEXT", timeout=15_000)


def test_reading_the_page_marks_the_run_as_watched(tmp_path, page):
    """A run only waits for an answer while a watcher is present. If opening
    the viewer stopped counting as watching, every question would be given up
    on with someone sitting in front of it."""
    study, run_id = _run_with_delegations(tmp_path, [])
    run_dir = study / "runs" / run_id

    assert not operator_channel.is_watched(run_dir)
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        for _ in range(100):
            if operator_channel.is_watched(run_dir):
                break
            time.sleep(0.1)

    assert operator_channel.is_watched(run_dir)


# --- the dependency that decides where this viewer can run ------------------

def test_the_viewer_needs_the_internet_to_work(tmp_path, page):
    """RECORDED, because it is a property of the product, not a bug in a test.

    graph.html pulls Alpine from unpkg and marked from cdnjs. Blocked, the
    page still paints its static shell -- tab labels, headings -- so it looks
    alive while showing NO run data at all. On an air-gapped compute node,
    which is where these runs execute, watching a run is not possible and
    nothing says so.

    This test pins the failure mode so it is a decision rather than a
    surprise. Vendoring the two files into the package would let it be
    deleted, and would make every other test in this file hermetic.
    """
    study, run_id = _run_with_delegations(tmp_path, [])

    with _LiveServer(create_app(study)) as server:
        page.route("**unpkg.com/**", lambda route: route.abort())
        page.route("**cdnjs.cloudflare.com/**", lambda route: route.abort())
        page.goto(f"{server.url}/runs/{run_id}")
        page.wait_for_timeout(2500)
        body = page.inner_text("body")

    assert "Overview" in body, "the static shell should still paint"
    assert "strategizer" not in body, (
        "the viewer now renders without its CDN scripts -- if they were "
        "vendored, delete this test and drop the route blocking above")


# --- markdown with math ------------------------------------------------------

def _render(page, text: str) -> str:
    return page.evaluate(
        "t => Alpine.$data(document.querySelector('[x-data]')).renderMarkdown(t)",
        text)


def test_a_formula_is_not_eaten_as_markdown_emphasis(tmp_path, page):
    """`*` and `_` inside $…$ are formula, not emphasis: the Result tab used to
    show "P_max*1000/(pi*D1^2" as "P_max1000/(piD1^2"."""
    study, run_id = _run_with_delegations(tmp_path, [])
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        html = _render(page, "so $P_max*1000/(pi*D1^2/4) + a*b$ holds, *really*")
        assert "<em>really</em>" in html          # real emphasis still works
        assert "<em>" not in html.replace("<em>really</em>", "")
        text = page.evaluate(
            "h => new DOMParser().parseFromString(h, 'text/html').body.textContent",
            html)
        assert "1000" in text and "D1" in text and "a∗b" in text and "pi∗D1" in text
        # a currency amount is not a formula
        assert "$5" in _render(page, "costs $5 and $6 each")
        # code spans are left alone
        assert "$a*b$" in _render(page, "run `$a*b$` now")


# --- vertical timeline -------------------------------------------------------

def _timeline(page, cards: int | None = None) -> dict:
    """The live timeline model. ``cards`` waits for that many cards first: the
    graph nodes paint before the delegation log has been fetched, so reading
    the model on that signal alone sees an empty timeline."""
    if cards is not None:
        page.wait_for_function(
            "(n) => Alpine.$data(document.querySelector('[x-data]'))"
            ".vtl().cards.length === n", arg=cards, timeout=15_000)
    return page.evaluate(
        "() => JSON.parse(JSON.stringify(Alpine.$data(document.querySelector('[x-data]')).vtl()))")


def test_overlapping_delegations_get_their_own_columns(tmp_path, page):
    """Parallel same-role work used to be drawn on top of itself in one lane;
    each concurrent delegation takes its own column, a later one reuses a
    freed column, and a gate review is a run-level rule rather than a card."""
    a = _delegation("D001", "implementer")
    b = {**_delegation("D002", "implementer"),
         "started_at": "2026-09-17T12:01:00+00:00",
         "completed_at": "2026-09-17T12:06:00+00:00", "hypothesis_ids": ["H1"]}
    c = {**_delegation("D003", "implementer"),
         "started_at": "2026-09-17T13:20:00+00:00",
         "completed_at": "2026-09-17T13:25:00+00:00"}
    gate = {**_delegation("GATE-1", "critic", "GATE:PASS"),
            "started_at": "2026-09-17T13:26:00+00:00",
            "completed_at": "2026-09-17T13:27:00+00:00"}
    study, run_id = _run_with_delegations(tmp_path, [a, b, c, gate])
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        tl = _timeline(page)
        cards = {x["id"]: x for x in tl["cards"]}
        assert set(cards) == {"D001", "D002", "D003"}
        assert cards["D001"]["left"] != cards["D002"]["left"]      # concurrent
        assert cards["D003"]["left"] == cards["D001"]["left"]      # column reused
        assert cards["D002"]["hyps"] == ["H1"]
        assert [g["id"] for g in tl["gates"]] == ["GATE-1"]
        assert tl["gates"][0]["label"] == "passed"
        assert tl["gates"][0]["y"] > cards["D003"]["top"]


def test_columns_count_true_concurrency_not_drawn_height(tmp_path, page):
    """A short delegation is drawn at its true height, so back-to-back short
    work shares one column; drawing never changes the column assignment."""
    rows = []
    for i in range(4):
        rows.append({**_delegation(f"D00{i + 1}", "implementer"),
                     "started_at": f"2026-09-17T12:{i * 6:02d}:00+00:00",
                     "completed_at": f"2026-09-17T12:{i * 6 + 5:02d}:00+00:00"})
    study, run_id = _run_with_delegations(tmp_path, rows)
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        tl = _timeline(page, cards=4)
        assert tl["cols"] == 1
        assert all(c["compact"] for c in tl["cards"])
        page.wait_for_function(
            "() => document.querySelectorAll('.vcard.compact').length === 4",
            timeout=15_000)


def test_feedback_audits_are_cards_with_a_feedback_chip_and_gates_stay_readable(tmp_path, page):
    fb = {**_delegation("FB1", "critic", "FEEDBACK"),
          "started_at": "2026-09-17T12:00:00+00:00",
          "completed_at": "2026-09-17T12:30:00+00:00",
          "deliverable": "## Report\n\n### Verdict\nREJECT. FEEDBACK mode.\n\nverdict: REJECT\n"}
    fb2 = {**_delegation("FB2", "critic", "FEEDBACK"),
           "started_at": "2026-09-17T13:00:00+00:00",
           "completed_at": "2026-09-17T13:30:00+00:00",
           "deliverable": "no verdict line here"}
    g1 = {**_delegation("GATE-1", "critic", "GATE:REVISE"),
          "started_at": "2026-09-17T12:40:00+00:00",
          "completed_at": "2026-09-17T12:41:00+00:00"}
    g2 = {**_delegation("GATE-2", "critic", "GATE:PASS"),
          "started_at": "2026-09-17T12:42:00+00:00",
          "completed_at": "2026-09-17T12:43:00+00:00"}
    study, run_id = _run_with_delegations(tmp_path, [fb, fb2, g1, g2])
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        tl = _timeline(page)
        assert [c["key"] for c in tl["cards"]] == ["feedback", "feedback"]
        by_id = {c["id"]: c for c in tl["cards"]}
        assert by_id["FB1"]["label"] == "feedback · reject"
        assert by_id["FB1"]["glyph"] == "◈✕"
        assert by_id["FB2"]["label"] == "feedback · verdict —"
        assert [g["id"] for g in tl["gates"]] == ["GATE-1", "GATE-2"]
        assert tl["gates"][0]["slot"] != tl["gates"][1]["slot"]


def test_revise_and_reject_gates_are_told_apart_and_a_delegation_is_open_by_default(tmp_path, page):
    a = _delegation("D001", "implementer")
    rev = {**_delegation("GATE-1", "critic", "GATE:REVISE"),
           "started_at": "2026-09-17T12:30:00+00:00",
           "completed_at": "2026-09-17T12:31:00+00:00"}
    rej = {**_delegation("GATE-2", "critic", "GATE:REJECT"),
           "started_at": "2026-09-17T12:40:00+00:00",
           "completed_at": "2026-09-17T12:41:00+00:00"}
    study, run_id = _run_with_delegations(tmp_path, [a, rev, rej])
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        tl = _timeline(page)
        assert [g["label"] for g in tl["gates"]] == ["revise", "reject"]
        assert page.locator(".vgate.revise").count() == 1
        assert page.locator(".vgate.failed").count() == 1
        assert "Pick a delegation" not in page.content()


def test_a_queued_delegation_is_a_hatched_wait_before_its_card(tmp_path, page):
    first = _delegation("D001", "implementer")
    waited = {**_delegation("D002", "implementer"),
              "started_at": "2026-09-17T12:00:30+00:00",
              "session_started_at": "2026-09-17T12:05:00+00:00",
              "completed_at": "2026-09-17T12:09:00+00:00"}
    study, run_id = _run_with_delegations(tmp_path, [first, waited])
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        tl = _timeline(page)
        (q,) = tl["queued"]
        card = next(x for x in tl["cards"] if x["id"] == "D002")
        assert q["id"] == "D002"
        assert q["top"] + q["height"] <= card["top"] + 1


def test_hovering_a_hypothesis_highlights_the_delegations_that_carry_it(tmp_path, page):
    a = {**_delegation("D001", "implementer"), "hypothesis_ids": ["H1"]}
    b = _delegation("D002", "datagenerator")
    study, run_id = _run_with_delegations(tmp_path, [a, b])
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        page.evaluate("() => Alpine.$data(document.querySelector('[x-data]')).setTab('overview')")
        page.wait_for_selector(".vcard", state="attached")
        page.evaluate("() => { Alpine.$data(document.querySelector('[x-data]')).hoverHyp = ['H1']; }")
        page.wait_for_selector(".vcard.hl", state="attached")
        assert page.locator(".vcard.hl").count() == 1
        assert page.locator(".vcard.dim").count() == 1


def test_the_oracle_tab_draws_best_so_far_for_the_chosen_objective(tmp_path, page):
    study, run_id = _run_with_delegations(tmp_path, [_delegation("D001", "implementer")])
    data = study / "runs" / run_id / "experiment_data" / "experiment_data"
    data.mkdir(parents=True)
    (data / "domain.json").write_text('{"input_space": {"x": {}}}', encoding="utf-8")
    (data / "input.csv").write_text(",x\n0,1\n1,2\n2,3\n", encoding="utf-8")
    (data / "output.csv").write_text(
        ",sigma,feasible,_delegation_id,_ts\n"
        "0,5.0,True,D001,2026-09-17T12:00:00+00:00\n"
        "1,1.0,False,D001,2026-09-17T12:01:00+00:00\n"
        "2,3.0,True,D001,2026-09-17T12:02:00+00:00\n", encoding="utf-8")
    (data / "jobs.csv").write_text(",0\n0,FINISHED\n1,FINISHED\n2,FINISHED\n", encoding="utf-8")
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        page.click("text=Oracle")
        page.wait_for_selector(".traj-svg", timeout=10_000)
        assert page.locator(".traj-ok").count() == 2
        assert page.locator(".traj-no").count() == 1
        assert "best feasible 5 in canonical store at #0" in page.locator(".traj-sum.m").inner_text()
        page.select_option("select[aria-label='direction']", "min")
        assert "best feasible 3 in canonical store at #2" in page.locator(".traj-sum.m").inner_text()


def test_the_oracle_chart_overlays_namespaces_and_marks_salvaged_rows(tmp_path, page):
    study, run_id = _run_with_delegations(tmp_path, [_delegation("D001", "implementer")])
    exp = study / "runs" / run_id / "experiment_data"
    (study / "runs" / run_id / "debug" / "run_config.json").write_text(
        json.dumps({"store_dir": str(exp), "oracles": {"freeform": {
            "evaluator_output_names": ["sigma_peak", "feasible", "salvaged"]}}}),
        encoding="utf-8")
    for ns, rows in (("experiment_data", "0,1.0,True,0,D001,2026-09-17T12:00:00+00:00\n"
                                          "1,4.0,False,0,D001,2026-09-17T12:01:00+00:00\n"),
                     ("freeform", "0,2.0,True,1,D001,2026-09-17T12:02:00+00:00\n"
                                  "1,6.0,True,0,D001,2026-09-17T12:03:00+00:00\n")):
        data = exp / ns if ns == "experiment_data" else exp / ns / "experiment_data"
        if ns == "experiment_data":
            data = exp / "experiment_data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "domain.json").write_text('{"input_space": {"x": {}}}', encoding="utf-8")
        (data / "input.csv").write_text(",x\n0,1\n1,2\n", encoding="utf-8")
        (data / "output.csv").write_text(
            ",sigma_peak,feasible,salvaged,_delegation_id,_ts\n" + rows, encoding="utf-8")
        (data / "jobs.csv").write_text(",0\n0,FINISHED\n1,FINISHED\n", encoding="utf-8")
    with _LiveServer(create_app(study)) as server:
        _open(page, server, run_id)
        page.click("text=Oracle")
        page.wait_for_selector(".traj-svg", timeout=10_000)
        assert page.locator("select[aria-label='objective column']").input_value() == "sigma_peak"
        assert page.locator("select[aria-label='direction']").input_value() == "max"
        assert page.locator("path.traj-ok, path.traj-no").count() == 1  # one salvaged diamond
        assert page.locator("circle.traj-ok, circle.traj-no").count() == 3
        assert page.locator(".traj-best").count() == 2  # a line per namespace
        legend = page.locator(".traj-legend").inner_text()
        assert "freeform" in legend and "canonical store" in legend
        assert "best feasible 6 (incl. salvaged) in freeform" in page.locator(".traj-sum.m").inner_text()
        assert page.locator(".traj-default").is_visible()
        page.select_option("select[aria-label='direction']", "min")
        page.locator(".traj-default").wait_for(state="hidden", timeout=3000)
