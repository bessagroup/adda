"""The redesigned viewer UI (/ui): served, and working in a real browser.

Function only, never appearance (see test_viewer_browser.py for why). Uncaught
JavaScript errors fail every browser test through the shared ``page`` fixture.
"""
from __future__ import annotations

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
        page.goto(f"{srv.url}/ui?run={RUN}&view=hypotheses")
        page.wait_for_selector(".empty")
        assert "isn't available" in page.locator(".empty").inner_text()


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
