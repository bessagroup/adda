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
        assert "will appear" in page.locator(".empty").inner_text()
