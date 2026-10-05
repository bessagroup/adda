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
        page.goto(f"{srv.url}/ui?run={RUN}&view=data")
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
