"""The redesigned viewer UI (/ui): served, and working in a real browser.

Function only, never appearance (see test_viewer_browser.py for why). Uncaught
JavaScript errors fail every browser test through the shared ``page`` fixture.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from starlette.testclient import TestClient

from adda._src.viewer.app import create_app

from .test_viewer_app import _LiveServer, _make_run, _make_study, _write_jsonl
from .test_viewer_browser import _delegation, browser, page  # noqa: F401

RUN = "20260917T120000"
UI = Path(__file__).parents[1] / "src/adda/_src/viewer/static/ui"


def _study(tmp_path, n=3):
    study = _make_study(tmp_path)
    run_dir = _make_run(study, RUN)
    rows = []
    for i in range(n):
        d = _delegation(f"D{i + 1:03d}", ["implementer", "datagenerator"][i % 2])
        d["started_at"] = f"2026-09-17T12:{i * 10:02d}:00+00:00"
        d["completed_at"] = f"2026-09-17T12:{i * 10 + 5:02d}:00+00:00"
        d["hypothesis_ids"] = ["H1"]
        rows.append(d)
    _write_jsonl(run_dir / "debug" / "delegation_log.jsonl", rows)
    return study, run_dir


def test_ui_and_its_assets_are_served(tmp_path):
    study, _ = _study(tmp_path)
    c = TestClient(create_app(study))
    page_ = c.get("/ui")
    assert page_.status_code == 200 and "ui.js" in page_.text
    css = c.get("/static/ui/ui.css")
    assert css.status_code == 200
    assert c.get("/static/ui/ui.js").status_code == 200
    for url in re.findall(r'url\("\.\./(fonts/[^"]+)"\)', css.text):
        assert c.get(f"/static/{url}").status_code == 200, url


def test_selection_lives_in_the_url_and_esc_closes(tmp_path, page):
    study, _ = _study(tmp_path)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=timeline")
        page.wait_for_selector(".card")
        assert page.locator(".card").count() == 3
        page.locator(".card", has_text="D002").click()
        page.wait_for_selector(".ih")
        assert "sel=D002" in page.url
        page.keyboard.press("Escape")
        page.wait_for_function("!location.search.includes('sel=')")
        assert page.locator(".ih").count() == 0


def test_deep_link_opens_the_inspector_and_ids_are_links(tmp_path, page):
    study, _ = _study(tmp_path)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=timeline&sel=D001")
        page.wait_for_selector(".ih")
        assert page.locator(".ih h2").inner_text() == "D001"


def test_no_horizontal_scroll_on_a_phone(tmp_path, page):
    study, _ = _study(tmp_path)
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}")
        page.wait_for_selector(".card")
        assert page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0


def test_unbuilt_views_say_what_will_appear(tmp_path, page):
    study, _ = _study(tmp_path)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=setup")
        page.wait_for_selector(".empty")
        assert "isn't available" in page.locator(".empty").inner_text()


def _ledger(run_dir, hyps):
    notes = run_dir / "debug" / "strategizer_notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "hypotheses.json").write_text(json.dumps(hyps), encoding="utf-8")


def _hyps():
    return {
        "H1": {"id": "H1", "statement": "A long statement " * 8, "prior": 0.4,
               "prediction": "p1", "falsification_criterion": "fc1",
               "status_log": [{"status": "OPEN", "posterior": 0.4, "ts": "2026-09-17T12:00:00+00:00"}]},
        "H2": {"id": "H2", "statement": "second", "prior": 0.5,
               "prediction": "p2", "falsification_criterion": "fc2",
               "status_log": [
                   {"status": "OPEN", "posterior": 0.5, "ts": "2026-09-17T12:00:00+00:00"},
                   {"status": "SUPPORTED", "posterior": 0.9, "ts": "2026-09-17T12:20:00+00:00",
                    "evidence": {"delegation": "D002"}, "validator_note": "numbers match"},
                   {"status": "OPEN", "posterior": 0.6, "ts": "2026-09-17T12:40:00+00:00",
                    "comment": "withdrawn: no falsification attempt"}]},
        "H3": {"id": "H3", "statement": "third", "prior": 0.3, "status_log": [
            {"status": "OPEN", "posterior": 0.3}, {"status": "FALSIFIED", "posterior": 0.05}]},
    }


def test_hypotheses_are_rows_with_status_belief_and_linked_delegations(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _ledger(run_dir, _hyps())
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.wait_for_selector(".hrow")
        assert page.locator(".hrow").count() == 3
        h1 = page.locator(".hrow", has_text="H1")
        assert "A long statement" in h1.inner_text() and h1.inner_text().count("A long statement") == 8
        assert "open" in h1.locator(".hv").inner_text()
        assert "40% → 40%" in h1.locator(".belief").inner_text()
        assert "D001" in h1.locator(".hd").inner_text()
        assert "falsified" in page.locator(".hrow", has_text="H3").locator(".hv").inner_text()


def test_hypothesis_filter_chips_split_open_closed_and_retracted(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _ledger(run_dir, _hyps())
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.wait_for_selector(".hrow")
        for chip, ids in {"open": {"H1", "H2"}, "closed": {"H3"}, "retracted": {"H2"}}.items():
            page.locator(f"[data-hfilter={chip}]").click()
            got = {t.split()[0] for t in page.locator(".hrow").all_inner_texts()}
            assert got == ids, chip
        page.locator("[data-hfilter=all]").click()
        assert page.locator(".hrow").count() == 3


def test_selecting_a_hypothesis_shows_its_history_with_the_retraction_as_a_back_step(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _ledger(run_dir, _hyps())
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.locator(".hrow", has_text="H2").click()
        page.wait_for_selector(".ih")
        assert "sel=H2" in page.url
        text = page.locator("#insp").inner_text()
        assert "fc2" in text and "p2" in text and "numbers match" in text
        assert page.locator(".hist li").count() == 3
        assert page.locator(".hist li.stepback").count() == 1
        assert page.locator(".hist li.stepback").is_visible()
        assert "withdrawn" in page.locator(".hist li.stepback").inner_text()


def test_hypotheses_with_no_ledger_or_no_match_say_what_would_appear(tmp_path, page):
    study, run_dir = _study(tmp_path)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.wait_for_selector(".empty")
        assert "No hypothesis has been stated" in page.locator(".empty").inner_text()
    _ledger(run_dir, {"H1": _hyps()["H1"]})
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.locator("[data-hfilter=retracted]").click()
        assert "No hypothesis has been retracted" in page.locator(".dnote").inner_text()


def test_hypotheses_do_not_scroll_sideways_on_a_phone(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _ledger(run_dir, _hyps())
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.wait_for_selector(".hrow")
        assert page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0


def _operator_hits(page):
    hits = []
    page.on("request", lambda r: hits.append(r.url) if "/operator" in r.url else None)
    return hits


def test_a_pending_question_is_a_banner_and_an_answer_is_sent_after_the_undo_window(tmp_path, page):
    from adda._src.infra import operator_channel
    study, run_dir = _study(tmp_path)
    operator_channel.ask_question(run_dir, "strategizer", "Retest H1 on a third seed?")
    with _LiveServer(create_app(study, token="t")) as srv:
        page.clock.install()
        page.goto(f"{srv.url}/session?token=t&next=/ui?run={RUN}")
        page.wait_for_selector("#banner:not([hidden]) .q")
        assert "strategizer" in page.locator("#banner .qh").inner_text()
        page.fill("#banner textarea", "No, move on.")
        page.click("#banner button")
        page.wait_for_selector("#toast")
        page.click("#undo")                                   # undo: nothing is sent
        page.clock.run_for(12_000)
        assert page.locator("#banner textarea").input_value() == "No, move on."
        assert operator_channel.pending_questions(run_dir)
        page.click("#banner button")
        page.clock.run_for(11_000)
        for _ in range(100):
            if not operator_channel.pending_questions(run_dir):
                break
            page.wait_for_timeout(100)
        assert not operator_channel.pending_questions(run_dir)
        assert page.locator("#banner").is_hidden()


def test_the_heartbeat_is_only_polled_by_a_visible_tab_on_an_open_run(tmp_path, page):
    study, _ = _study(tmp_path)
    with _LiveServer(create_app(study)) as srv:
        page.clock.install()
        hits = _operator_hits(page)
        page.goto(f"{srv.url}/ui?run={RUN}")
        page.wait_for_selector(".card")
        page.wait_for_function("document.querySelector('#title').textContent.includes('Running')")
        page.clock.run_for(6_000)
        page.wait_for_timeout(300)
        assert hits, "a visible tab on an open run must poll /operator"
        hits.clear()
        page.evaluate("Object.defineProperty(document,'visibilityState',{value:'hidden',configurable:true});"
                      "document.dispatchEvent(new Event('visibilitychange'))")
        page.clock.run_for(20_000)
        page.wait_for_timeout(300)
        assert not hits, "a hidden tab must never poll /operator"


def test_a_closed_run_never_polls_the_heartbeat(tmp_path, page):
    study, run_dir = _study(tmp_path)
    (run_dir / "debug" / "run_status.json").write_text('{"status": "GATED"}')
    with _LiveServer(create_app(study)) as srv:
        hits = _operator_hits(page)
        page.goto(f"{srv.url}/ui?run={RUN}")
        page.wait_for_selector(".card")
        page.wait_for_timeout(800)
        assert not hits


OBJECTIVE = {
    "column": "score", "direction": "max", "feasible": "feasible",
    "lines": [{"value": 2.0, "label": "reference"}],
    "unit_label": {"divide_by": 2.0, "label": "x unit"},
}


def _with_store(run_dir, n=6, objective=OBJECTIVE):
    """A canonical store of `n` rows: score = row index, odd rows infeasible."""
    import json
    cfg = {"objective": objective} if objective else {}
    (run_dir / "debug" / "run_config.json").write_text(json.dumps(cfg))
    data = run_dir / "experiment_data" / "experiment_data"
    data.mkdir(parents=True)
    (data / "domain.json").write_text(json.dumps({"input_space": {"x": {}}}))
    (data / "input.csv").write_text(",x\n" + "".join(f"{i},{i * 0.5}\n" for i in range(n)))
    (data / "output.csv").write_text(
        ",score,feasible,note,_delegation_id,_ts\n" + "".join(
            f"{i},{i + 1},{int(i % 2 == 0)},n{i},D00{i % 3 + 1},2026-09-17T12:{i:02d}:00+00:00\n"
            for i in range(n)))
    (data / "jobs.csv").write_text(",0\n" + "".join(f"{i},FINISHED\n" for i in range(n)))


def test_data_view_draws_the_declared_objective_in_display_units(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector("svg.cht")
        assert page.locator(".cht .dot.f").count() == 3
        # the axis follows the counted rows; the infeasible row above it is reported, not drawn
        assert page.locator(".cht .dot.i").count() == 2
        assert page.locator(".cht .edgetick").count() == 1
        assert "1 outside the range" in page.locator(".dcap", has_text="outside the range").inner_text()
        assert "reference" in page.locator(".cht .refl").text_content()
        assert "(x unit)" in page.locator(".dh h3").first.inner_text()   # one scaling, in one place
        assert "2.5 x unit · canonical row 4" in page.locator(".cht .bestlab").text_content()
        page.locator(".cht .dot.f").first.click(force=True)
        page.wait_for_selector(".ih")
        assert "sel=row%3A" in page.url
        assert page.locator(".ih h2").inner_text().startswith("row ")


def test_data_view_draws_no_chart_and_no_lines_when_nothing_is_declared(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir, objective=None)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector(".dnote")
        assert page.locator("svg.cht").count() == 0
        assert "declares no objective" in page.locator(".dnote").first.inner_text()


def test_the_store_table_is_virtual_sorts_and_remembers_its_columns(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir, n=600)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector(".trow")
        assert 0 < page.locator(".trow").count() < 80
        assert page.locator(".trow").first.get_attribute("data-row") == "599"
        page.click('.th[data-sort="#"]')
        assert page.locator(".trow").first.get_attribute("data-row") == "0"
        page.click(".picker summary")
        page.check('[data-col="in:x"]')
        page.wait_for_selector('.th[data-sort="in:x"]')
        page.reload()
        page.wait_for_selector('.th[data-sort="in:x"]')


def test_the_wall_clock_fill_warns_past_one_and_a_half_and_fails_past_two(tmp_path, page):
    import time
    for hours, tone in ((0.5, ""), (1.7, "warn"), (2.3, "bad")):
        (tmp_path / str(hours)).mkdir()
        study, run_dir = _study(tmp_path / str(hours))
        (study / "config.yaml").write_text("budget: '01:00:00'\n")
        (run_dir / "debug" / "run_started_at").write_text(str(time.time() - hours * 3600))
        with _LiveServer(create_app(study)) as srv:
            page.goto(f"{srv.url}/ui?run={RUN}")
            page.wait_for_selector(".clock b")
            assert (page.locator(".clock b").get_attribute("class") or "") == tone, hours


def _with_namespace(run_dir):
    """A 'alpha' store whose best counted row (score 8, row 1) beats every canonical one."""
    ff = run_dir / "experiment_data" / "alpha" / "experiment_data"
    ff.mkdir(parents=True)
    (ff / "domain.json").write_text('{"input_space": {"x": {}}}')
    (ff / "input.csv").write_text(",x\n0,1\n1,2\n2,3\n")
    (ff / "output.csv").write_text(
        ",score,feasible,note,_delegation_id,_ts\n"
        "0,3,1,a,D001,2026-09-17T12:20:00+00:00\n"
        "1,8,1,b,D002,2026-09-17T12:30:00+00:00\n"
        "2,9,0,c,D002,2026-09-17T12:40:00+00:00\n")
    (ff / "jobs.csv").write_text(",0\n0,FINISHED\n1,FINISHED\n2,FINISHED\n")


def test_the_best_row_and_chart_span_every_store_and_name_the_namespace(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir)
    _with_namespace(run_dir)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector("svg.cht")
        best = page.locator("#title button.vital.link")
        assert "4 x unit" in best.inner_text() and "alpha row 1" in best.inner_text()
        # the switcher opens on the store holding the best row; it highlights, it never hides
        assert page.locator(".cht .dot.f[data-ns='alpha']:not(.dim)").count() == 2
        assert page.locator(".cht .dot.f[data-ns='alpha'].dim").count() == 0
        assert page.locator(".cht .dot.f[data-ns=''].dim").count() == 3
        assert page.locator(".cht .dot[data-ns='']:not(.dim)").count() == 0
        line = page.locator(".cht .step").get_attribute("d")
        page.click("[data-store='']")
        assert page.locator(".cht .dot.f[data-ns='']:not(.dim)").count() == 3
        assert page.locator(".cht .dot.f[data-ns='alpha'].dim").count() == 2
        assert page.locator(".cht .step").get_attribute("d") == line
        best.click()
        page.wait_for_selector(".ih")
        assert "sel=row%3Aalpha%3A1" in page.url
        assert "alpha" in page.locator(".ih").inner_text()


def test_a_store_without_the_declared_columns_is_named_not_scored(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir)
    lg = run_dir / "experiment_data" / "beta" / "experiment_data"
    lg.mkdir(parents=True)
    (lg / "output.csv").write_text(",energy,_delegation_id,_ts\n0,1,D001,2026-09-17T12:20:00+00:00\n")
    (lg / "input.csv").write_text(",x\n0,1\n")
    (lg / "jobs.csv").write_text(",0\n0,FINISHED\n")
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector("svg.cht")
        assert "not scored: beta (missing score, feasible)" in page.locator(".dcap", has_text="not scored").inner_text()


def test_the_chart_fits_its_data_and_fills_its_panel(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir, n=40)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector("svg.cht")
        assert page.locator("svg.cht").get_attribute("height") == "260"
        # the y axis hugs the data (8% pad), it is not stretched to a round bound
        ticks = [float(t) for t in page.locator(".cht .gl + text").all_text_contents()]
        assert max(ticks) <= 21 and min(ticks) >= -2     # counted values span 0.5 to 19.5
        # x is the evaluation number by default, elapsed time on request
        assert "+" not in "".join(page.locator(".cht text:not(.bestlab):not(.refl)").all_text_contents())
        page.click("[data-xmode='time']")
        assert "h" in "".join(page.locator(".cht text").all_text_contents())


def test_a_reference_line_outside_the_range_becomes_an_edge_tag(tmp_path, page):
    study, run_dir = _study(tmp_path)
    far = {**OBJECTIVE, "lines": [{"value": 500.0, "label": "far goal"}]}
    _with_store(run_dir, objective=far)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector("svg.cht")
        assert page.locator(".cht line.ref").count() == 0
        assert "far goal ↑ above range" in page.locator(".cht .refl.tag").text_content()


def test_a_log_toggle_appears_only_when_the_values_span_two_decades(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector("svg.cht")
        assert page.locator("[data-logy]").count() == 0
    data = run_dir / "experiment_data" / "experiment_data"
    n = 6
    (data / "output.csv").write_text(
        ",score,feasible,note,_delegation_id,_ts\n" + "".join(
            f"{i},{10 ** i},1,n{i},D001,2026-09-17T12:{i:02d}:00+00:00\n" for i in range(n)))
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector("[data-logy]")
        page.click("[data-logy='1']")
        assert page.locator("[data-logy='1'][aria-pressed='true']").count() == 1


def test_without_a_funnel_declaration_the_zero_one_columns_are_a_flag_table(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector(".flags")
        assert page.locator(".funnel").count() == 0
        assert page.locator(".dh h3", has_text="0/1 columns").count() == 1
        assert page.locator(".fr").first.inner_text().split() == ["feasible", "3", "6"]
        page.click("[data-fsort='ones']")
        assert page.locator(".flags .th[aria-sort='descending']").count() == 1


def test_a_declared_funnel_is_drawn_as_stages(tmp_path, page):
    import json
    study, run_dir = _study(tmp_path)
    _with_store(run_dir, objective=dict(OBJECTIVE))
    cfg = json.loads((run_dir / "debug" / "run_config.json").read_text())
    cfg["funnel"] = ["feasible"]
    (run_dir / "debug" / "run_config.json").write_text(json.dumps(cfg))
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector(".funnel")
        assert page.locator(".flags").count() == 0
        assert page.locator(".stage").count() == 1


def test_the_store_table_is_sized_to_its_content_not_stretched(tmp_path, page):
    study, run_dir = _study(tmp_path)
    _with_store(run_dir, objective=None)
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1600, "height": 1000})
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
        page.wait_for_selector(".trow")
        tbl, panel = page.locator("#tbl").bounding_box(), page.locator(".data .dh").first.bounding_box()
        assert tbl["width"] < panel["width"] * 0.6
        assert abs(tbl["x"] - panel["x"]) < 2


def test_a_long_statement_is_clamped_to_four_lines_with_a_more_toggle(tmp_path, page):
    study, run_dir = _study(tmp_path)
    hyps = _hyps()
    hyps["H1"]["statement"] = "A long statement " * 60
    _ledger(run_dir, hyps)
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.wait_for_selector(".hrow")
        h1, h2 = page.locator(".hrow", has_text="H1"), page.locator(".hrow", has_text="second")
        assert h2.locator(".more").is_hidden()
        lh = h1.locator(".clamp").evaluate("e => parseFloat(getComputedStyle(e).lineHeight)")
        short = h1.locator(".clamp").evaluate("e => e.clientHeight")
        assert short <= 4 * lh + 1
        h1.locator(".more").click()
        assert h1.locator(".more").inner_text() == "less"
        assert h1.locator(".clamp").evaluate("e => e.clientHeight") > short
        assert "sel=" not in page.url
        h1.locator(".more").click()
        assert h1.locator(".clamp").evaluate("e => e.clientHeight") == short


def test_a_downgrade_is_revised_not_retracted(tmp_path, page):
    study, run_dir = _study(tmp_path)
    hyps = _hyps()
    hyps["H3"]["status_log"] = [
        {"status": "OPEN", "posterior": 0.3}, {"status": "SUPPORTED", "posterior": 0.8},
        {"status": "INCONCLUSIVE", "posterior": 0.5}]
    _ledger(run_dir, hyps)
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses&sel=H3")
        page.wait_for_selector(".hist li")
        assert page.locator(".hist li.revised").count() == 1
        assert page.locator(".hist li.stepback").count() == 0
        assert "revised" in page.locator(".hist li.revised").inner_text()
        page.locator("[data-hfilter=retracted]").click()
        assert {t.split()[0] for t in page.locator(".hrow").all_inner_texts()} == {"H2"}


def _notebook(study, cells, run=RUN):
    import nbformat

    nb = nbformat.v4.new_notebook(cells=cells)
    nb.metadata["agentic"] = {"run": run}
    nbformat.write(nb, str(study / "pipeline.ipynb"))


def _nb_cells():
    import nbformat

    code = nbformat.v4.new_code_cell("print('REPRODUCED: 0.25')")
    code.outputs = [
        nbformat.v4.new_output("stream", name="stdout",
                               text="REPRODUCED: 0.25\nCLAIMED_HEADLINE: 0.25\n"),
        nbformat.v4.new_output("display_data", data={
            "text/plain": "frame",
            "text/html": "<table><tr><th>a</th></tr><tr><td>1<script>window.pwn=1</script></td></tr></table>"})]
    return [nbformat.v4.new_markdown_cell(
                "# Title\n\nThe energy is $E = mc^2$ and a [link](https://example.org).\n\n"
                "$$\\int_0^1 x\\,dx = \\tfrac12$$\n\n" + "Prose sentence. " * 40), code]


def test_the_deliverable_is_prose_at_75ch_with_local_math_and_the_notebook_headline(tmp_path, page):
    study, _ = _study(tmp_path)
    _notebook(study, _nb_cells())
    external = []
    page.on("request", lambda r: external.append(r.url) if not r.url.startswith("http://127.0.0.1") and not r.url.startswith("data:") else None)
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=deliverable")
        page.wait_for_selector(".nbmd .katex", timeout=8000)
        assert page.locator(".nbmd .katex-display").count() == 1
        width = page.locator(".nbmd p").last.evaluate("e => e.getBoundingClientRect().width")
        ch = page.evaluate("(() => { const s = document.createElement('span'); s.style.cssText = 'font:400 14px Instrument Sans,system-ui,sans-serif;position:absolute;visibility:hidden'; s.textContent = '0'.repeat(75); document.body.appendChild(s); const w = s.getBoundingClientRect().width; s.remove(); return w })()")
        assert width < ch * 1.15 and width < 900
        assert page.locator(".nbhead").inner_text().count("0.25") >= 1
        assert "not" in page.locator(".nbhead").inner_text().lower()
        assert page.locator(".nb .tbl table").count() == 1
        assert page.evaluate("window.pwn") is None
        assert page.locator("a[href='https://example.org']").count() == 1
        assert page.locator("details.nbcode").count() == 1 and not page.locator("details.nbcode[open]").count()
        page.locator("#nbcode").check()
        assert page.locator("details.nbcode[open]").count() == 1
    assert external == []


def test_a_notebook_without_a_headline_marker_states_none_instead_of_borrowing_one(tmp_path, page):
    import nbformat

    study, _ = _study(tmp_path)
    _notebook(study, [nbformat.v4.new_markdown_cell("only prose")])
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=deliverable")
        page.wait_for_selector(".nbhead")
        assert "REPRODUCED" in page.locator(".nbhead").inner_text() or \
            page.locator(".nbhead .dash, .nbhead [title]").count() >= 1


def test_a_run_without_a_notebook_says_so(tmp_path, page):
    study, _ = _study(tmp_path)
    with _LiveServer(create_app(study)) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=deliverable")
        page.wait_for_selector(".empty")
        assert "no pipeline notebook" in page.locator(".empty").inner_text()


def _fake_replay(study, reproduced="0.25"):
    import json

    import nbformat

    def fake(study_dir, run_id, *, audit=None, on_event=None):
        on_event({"event": "started", "line": "started"})
        for i in (1, 2):
            on_event({"event": "cell", "line": f"cell {i}/2", "done": i, "total": 2, "errored": False})
        folder = Path(study_dir) / "runs" / run_id / "debug" / "viewer_reexec"
        folder.mkdir(parents=True, exist_ok=True)
        code = nbformat.v4.new_code_cell("print('x')")
        code.outputs = [nbformat.v4.new_output("stream", name="stdout", text=f"REPRODUCED: {reproduced}\n")]
        nbformat.write(nbformat.v4.new_notebook(cells=_nb_cells()[:1] + [code]), str(folder / "pipeline_T1.ipynb"))
        (folder / "reexec_T1.json").write_text(json.dumps({
            "id": "T1", "passed": True, "notebook": "pipeline_T1.ipynb", "finished_at": "2026-01-02T03:04:05+00:00",
            "adda_commit": "abc", "reproduced": reproduced}))
        return {"passed": True, "run_id": run_id, "reproduced": reproduced, "saved": "pipeline_T1.ipynb",
                "reexec_id": "T1", "rows_before": 3, "rows_after": 3, "duration_s": 1.5,
                "stdout_tail": "", "stderr_tail": ""}

    return fake


def test_re_execute_streams_into_a_drawer_then_the_headline_comes_from_the_re_execution(tmp_path, page, monkeypatch):
    from adda._src.viewer import notebook_replay

    study, _ = _study(tmp_path)
    monkeypatch.setattr(notebook_replay, "replay_notebook", _fake_replay(study))
    _notebook(study, _nb_cells()[:1])
    with _LiveServer(create_app(study, token="t")) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/session?token=t&next=/ui?run={RUN}%26view=deliverable")
        page.wait_for_selector("#reexec")
        assert "no outputs" in page.locator(".nbhead").inner_text()
        assert page.locator("[data-pick]").count() == 0
        page.click("#reexec")
        page.wait_for_selector("#drawer:not([hidden]) .st.ok", timeout=8000)
        text = page.locator("#drawer").inner_text()
        assert "cell 2/2" in text and "PASSED" in text
        page.wait_for_selector("[data-pick]", timeout=8000)
        head = page.locator(".nbhead").inner_text()
        assert "0.25" in head and "From re-execution at" in head
        assert page.locator("[data-pick][aria-pressed=true]").inner_text().startswith("Last re-execution")
        page.locator("[data-pick=stored]").click()
        page.wait_for_function("(document.querySelector('.nbhead')||{innerText:''}).innerText.includes('no outputs')")
        assert page.locator("[data-pick=stored][aria-pressed=true]").count() == 1
        page.locator("[data-drawer-close]").click()
        assert page.locator("#drawer").is_hidden()
    assert (study / "pipeline.ipynb").read_bytes() and "REPRODUCED" not in (study / "pipeline.ipynb").read_text()


def test_notebook_tables_get_their_own_wider_measure_and_headers_never_break_inside_a_word(tmp_path, page):
    import nbformat

    study, _ = _study(tmp_path)
    rows = "".join(f"| H{i} | {'a long cell of prose about the claim ' * 3} | OPEN | 0.5 |\n" for i in range(4))
    md = "Intro.\n\n| Hypothesis | Statement | Status | Posterior |\n|---|---|---|---|\n" + rows
    _notebook(study, [nbformat.v4.new_markdown_cell(md)])
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=deliverable")
        page.wait_for_selector(".nbmd table")
        tw = page.locator(".nbmd table").evaluate("e => e.getBoundingClientRect().width")
        pw = page.locator(".nbmd p").first.evaluate("e => e.getBoundingClientRect().width")
        assert tw > pw * 1.3 and tw <= 1001
        heights = page.locator(".nbmd th").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
        assert max(heights) < 40, heights
        page.set_viewport_size({"width": 500, "height": 900})
        assert page.locator(".nbmd table").evaluate("e => e.scrollWidth > e.clientWidth || e.getBoundingClientRect().width <= e.parentElement.clientWidth")


def test_re_execute_without_the_write_token_says_the_page_is_read_only(tmp_path, page):
    study, _ = _study(tmp_path)
    _notebook(study, _nb_cells())
    with _LiveServer(create_app(study, token="t")) as srv:
        page.goto(f"{srv.url}/ui?run={RUN}&view=deliverable")
        page.wait_for_selector("#reexec")
        page.click("#reexec")
        page.wait_for_selector("#drawer:not([hidden]) .st.bad", timeout=8000)
        assert "read-only" in page.locator("#drawer").inner_text()


def test_logs_tail_a_live_file_link_ids_pause_and_switch_source(tmp_path, page):
    study, run_dir = _study(tmp_path)
    (run_dir / "debug" / "run_status.json").unlink(missing_ok=True)
    log = run_dir / "debug" / "run.log"
    log.write_text("[10:00:00] INFO Run starting\n[10:00:01] WARNING D001 is slow\n", encoding="utf-8")
    (run_dir / "debug" / "diagnostics.jsonl").write_text(json.dumps(
        {"ts": "2026-09-17T12:00:00+00:00", "node": "implementer", "tool": "QueryStore",
         "error_type": "ERROR_RETURN", "message": "ERROR: no such column\nsecond line"}) + "\n", encoding="utf-8")
    with _LiveServer(create_app(study)) as srv:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{srv.url}/ui?run={RUN}&view=logs")
        page.wait_for_selector(".ll")
        assert page.locator(".ll").count() == 2
        assert page.locator(".ll .lm a").first.inner_text() == "D001"
        assert page.locator(".ll .lv.warn").count() == 1
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("[10:00:02] INFO appended while open\n")
        page.wait_for_function("document.querySelectorAll('.ll').length === 3", timeout=8000)
        page.locator("#logpause").check()
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("[10:00:03] INFO written while paused\n")
        page.wait_for_timeout(2600)
        assert page.locator(".ll").count() == 3 and "paused" in page.locator("#logstate").inner_text()
        page.locator("#logpause").uncheck()
        page.wait_for_function("document.querySelectorAll('.ll').length === 4", timeout=8000)
        page.locator("[data-logsrc=diagnostics]").click()
        page.wait_for_function("document.querySelectorAll('.ll').length === 1 && document.querySelector('.ll .lv.bad')")
        text = page.locator(".ll").inner_text()
        assert "implementer" in text and "no such column" in text and "second line" not in text
