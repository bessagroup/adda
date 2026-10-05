"""Screenshot set for the viewer UI (spec 15 section 8): views x widths x themes.

    uv run python internal/tools/viewer_shots.py STUDY_DIR OUT_DIR [--run RUN_ID] [--sel ID]

Also reports the visual-bug checks it can make mechanically: horizontal scroll at
400 px, uncaught JS errors, and any id-looking text that is clipped.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from adda._src.viewer.app import create_app

VIEWS = ["timeline", "hypotheses", "data", "deliverable", "logs", "setup"]
WIDTHS = {"1600": (1600, 1000), "400": (400, 900)}
THEMES = ["light", "dark"]


def question_fixture(study: Path) -> Path:
    """A copy of `study` whose newest run is open and has one pending question."""
    from adda._src.infra import operator_channel

    root = Path(tempfile.mkdtemp(prefix="adda_q_")) / study.name
    shutil.copytree(study, root)
    run_dir = sorted((root / "runs").iterdir())[-1]
    (run_dir / "debug" / "run_status.json").unlink(missing_ok=True)
    (run_dir / "run_status.json").unlink(missing_ok=True)
    qid = operator_channel.ask_question(
        run_dir, "strategizer",
        "H17 is falsified on two of three seeds. Should I spend the remaining 120 "
        "evaluations re-testing it on the third seed, or move to H18?")
    qfile = next((run_dir / "debug").rglob(f"{qid}.json"))
    data = json.loads(qfile.read_text())
    data["asked_at"] = time.time() - 4 * 60
    qfile.write_text(json.dumps(data))
    return root


def data_fixture(study: Path, declaration: dict) -> Path:
    """A copy of `study` whose newest run records `declaration` (a study's
    ``objective:`` block, read from a JSON file), so a run that predates the
    declaration can be shot with it. The study's own config is untouched."""
    root = Path(tempfile.mkdtemp(prefix="adda_data_")) / study.name
    shutil.copytree(study, root)
    run_dir = sorted((root / "runs").iterdir())[-1]
    cfg_path = run_dir / "debug" / "run_config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["objective"] = declaration
    cfg_path.write_text(json.dumps(cfg))
    return root


def data_shots(base: str, out: Path, problems: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for wname, (w, h) in WIDTHS.items():
            for theme in THEMES:
                ctx = browser.new_context(viewport={"width": w, "height": h},
                                          color_scheme=theme)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
                pg.goto(f"{base}/ui?view=data")
                pg.wait_for_selector(".cht, .dnote", timeout=8000)
                pg.wait_for_selector(".trow", timeout=8000)
                pg.wait_for_timeout(400)
                pg.screenshot(path=str(out / f"data-{wname}-{theme}.png"))
                if w > 400 and pg.locator(".cht .dot.f").count() > 3:
                    pg.locator(".cht .dot.f").nth(3).hover(force=True)
                    pg.wait_for_selector("#tip:not([hidden])")
                    pg.screenshot(path=str(out / f"data-hover-{wname}-{theme}.png"))
                    pg.locator(".cht .dot.f").nth(3).click(force=True)
                    pg.wait_for_selector(".ih")
                    pg.wait_for_timeout(300)
                    pg.screenshot(path=str(out / f"data-selected-{wname}-{theme}.png"))
                    pg.goto(f"{base}/ui?view=data")
                    pg.wait_for_selector(".seg, .dnote")
                    if pg.locator(".seg button").count() > 1:
                        pg.locator(".seg button").nth(1).click()
                        pg.wait_for_timeout(300)
                        pg.screenshot(path=str(out / f"data-ns2-{wname}-{theme}.png"))
                if w > 400:
                    gap = pg.evaluate("(()=>{const t=document.getElementById('tbl'),h=t&&t.querySelector('.thead');"
                                      "return t&&h?t.clientWidth-h.getBoundingClientRect().width:0})()")
                    if gap > 1:
                        problems.append(f"data-{wname}-{theme}: table stops {gap:.0f}px short of its panel")
                sw = pg.evaluate("document.documentElement.scrollWidth - "
                                 "document.documentElement.clientWidth")
                if w <= 400 and sw > 0:
                    problems.append(f"data-{wname}-{theme}: horizontal scroll by {sw}px")
                ctx.close()
        browser.close()


def hypothesis_shots(base: str, out: Path, problems: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for wname, (w, h) in WIDTHS.items():
            for theme in THEMES:
                ctx = browser.new_context(viewport={"width": w, "height": h},
                                          color_scheme=theme)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
                pg.goto(f"{base}/ui?view=hypotheses")
                pg.wait_for_selector(".hrow, .empty", timeout=8000)
                pg.wait_for_timeout(300)
                pg.screenshot(path=str(out / f"hypotheses-{wname}-{theme}.png"))
                sw = pg.evaluate("document.documentElement.scrollWidth - "
                                 "document.documentElement.clientWidth")
                if w <= 400 and sw > 0:
                    problems.append(f"hypotheses-{wname}-{theme}: horizontal scroll by {sw}px")
                if pg.locator(".hrow").count():
                    pg.locator("[data-hfilter=retracted]").click()
                    pg.wait_for_timeout(200)
                    pg.screenshot(path=str(out / f"hypotheses-retracted-{wname}-{theme}.png"))
                    pg.locator("[data-hfilter=all]").click()
                    pick = pg.locator(".hrow:has(.retr)")
                    row = pick.first if pick.count() else pg.locator(".hrow").first
                    row.click()
                    pg.wait_for_selector(".ih")
                    pg.wait_for_timeout(300)
                    pg.screenshot(path=str(out / f"hypotheses-selected-{wname}-{theme}.png"))
                ctx.close()
        browser.close()


def deliverable_shots(base: str, out: Path, problems: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for wname, (w, h) in WIDTHS.items():
            for theme in THEMES:
                ctx = browser.new_context(viewport={"width": w, "height": h},
                                          color_scheme=theme)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
                pg.goto(f"{base}/session?token=shots&next=/ui?view=deliverable")
                pg.wait_for_selector(".nbhead, .empty", timeout=8000)
                pg.wait_for_timeout(800)
                pg.screenshot(path=str(out / f"deliverable-{wname}-{theme}.png"))
                sw = pg.evaluate("document.documentElement.scrollWidth - "
                                 "document.documentElement.clientWidth")
                if w <= 400 and sw > 0:
                    problems.append(f"deliverable-{wname}-{theme}: horizontal scroll by {sw}px")
                if pg.locator(".katex").count() == 0 and pg.locator("code.tex").count():
                    problems.append(f"deliverable-{wname}-{theme}: math not rendered")
                if pg.locator("#reexec").count() and theme == "light":
                    pg.locator("#nbcode").check()
                    pg.locator("#reexec").click()
                    try:
                        pg.wait_for_selector("#drawer:not([hidden]) .st.ok, #drawer:not([hidden]) .st.bad",
                                             timeout=120000)
                    except Exception:  # noqa: BLE001
                        problems.append(f"deliverable-{wname}: no verdict from the re-execution")
                    pg.wait_for_timeout(300)
                    pg.screenshot(path=str(out / f"deliverable-reexec-{wname}-{theme}.png"))
                ctx.close()
        browser.close()


def live_fixture(study: Path) -> tuple[Path, Path]:
    """A copy of `study` whose newest run is open, plus that run's run.log,
    for a writer thread to append to while the tail is shot."""
    root = Path(tempfile.mkdtemp(prefix="adda_live_")) / study.name
    shutil.copytree(study, root)
    run_dir = sorted(d for d in (root / "runs").iterdir() if (d / "debug" / "run.log").is_file())[-1]
    (run_dir / "debug" / "run_status.json").unlink(missing_ok=True)
    (run_dir / "run_status.json").unlink(missing_ok=True)
    return root, run_dir / "debug" / "run.log"


def logs_shots(base: str, out: Path, problems: list[str], live_log: Path | None = None) -> None:
    from playwright.sync_api import sync_playwright

    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.is_set() and live_log is not None:
            n += 1
            with open(live_log, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] {'WARNING' if n % 4 == 0 else 'INFO'} live line {n}\n")
            time.sleep(0.5)

    if live_log is not None:
        threading.Thread(target=writer, daemon=True).start()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for wname, (w, h) in WIDTHS.items():
            for theme in THEMES:
                ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=theme)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
                pg.goto(f"{base}/ui?view=logs")
                pg.wait_for_selector(".logbody", timeout=8000)
                pg.wait_for_timeout(4500 if live_log else 600)
                pg.screenshot(path=str(out / f"logs-{wname}-{theme}.png"))
                sw = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if w <= 400 and sw > 0:
                    problems.append(f"logs-{wname}-{theme}: horizontal scroll by {sw}px")
                if theme == "light" and wname == "1600":
                    pg.locator('[data-logsrc=diagnostics]').click()
                    pg.wait_for_timeout(800)
                    pg.screenshot(path=str(out / f"logs-monitor-{wname}-{theme}.png"))
                ctx.close()
        browser.close()
    stop.set()


def setup_fixture(study: Path) -> Path:
    """A live copy of `study` under its own throwaway git repo, so Setup can commit."""
    import os
    import subprocess

    root, _ = live_fixture(study)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Shots", "GIT_AUTHOR_EMAIL": "s@x",
           "GIT_COMMITTER_NAME": "Shots", "GIT_COMMITTER_EMAIL": "s@x",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    (root / ".gitignore").write_text("runs/\nviewer_actions.jsonl\n", encoding="utf-8")
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "first draft"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True, env=env)
    os.environ.update({k: v for k, v in env.items() if k.startswith("GIT_")})
    return root


def setup_shots(base: str, out: Path, problems: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for wname, (w, h) in WIDTHS.items():
            for theme in THEMES:
                ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=theme)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
                tag = f"{wname}-{theme}"
                pg.goto(f"{base}/session?token=shots&next=/ui?view=setup")
                pg.wait_for_selector("#sutext")
                pg.wait_for_timeout(300)
                pg.screenshot(path=str(out / f"setup-clean-{tag}.png"))
                pg.fill("#sutext", pg.input_value("#sutext") + "\nSuccess: reach the stated target.\n")
                pg.wait_for_selector(".sdiff tr.add")
                pg.fill("#sumsg", "state the success criterion")
                pg.screenshot(path=str(out / f"setup-diff-{tag}.png"))
                pg.click("[data-sufile=config]")
                pg.wait_for_function("(document.querySelector('#sutext')||{value:''}).value.includes('model')")
                pg.fill("#sutext", "model: m\nruntime:\n  max_awake_nodez: 2\n")
                pg.wait_for_selector("#suval .st.bad")
                pg.screenshot(path=str(out / f"setup-invalid-{tag}.png"))
                pg.goto(f"{base}/ui?view=timeline")
                pg.wait_for_selector("#openstart, [data-stop]")
                if pg.locator("[data-stop]").count():
                    pg.click("[data-stop]")
                    pg.wait_for_selector("#pop:not([hidden]) #stopgrace")
                    pg.screenshot(path=str(out / f"stop-pop-{tag}.png"))
                    if pg.locator("[data-pop-kill]").count():
                        pg.click("[data-pop-kill]")
                        pg.screenshot(path=str(out / f"stop-kill-{tag}.png"))
                else:
                    problems.append(f"{tag}: no Stop control on the live fixture")
                ctx.close()
        browser.close()


def banner_shots(base: str, out: Path, problems: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for wname, (w, h) in WIDTHS.items():
            for theme in THEMES:
                ctx = browser.new_context(viewport={"width": w, "height": h},
                                          color_scheme=theme)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
                pg.goto(f"{base}/ui?view=timeline")
                pg.wait_for_selector("#banner:not([hidden]) .q", timeout=8000)
                pg.wait_for_timeout(400)
                pg.screenshot(path=str(out / f"banner-{wname}-{theme}.png"))
                if w > 400:
                    pg.fill("#banner textarea", "Re-test H17 on the third seed.")
                    pg.click("#banner button")
                    pg.wait_for_selector("#toast")
                    pg.screenshot(path=str(out / f"banner-answered-{wname}-{theme}.png"))
                    pg.click("#undo")
                ctx.close()
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("study")
    ap.add_argument("out")
    ap.add_argument("--run")
    ap.add_argument("--sel", help="selection for the timeline shot")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", action="store_true", help="shoot the Data view")
    ap.add_argument("--objective", metavar="JSON",
                    help="objective declaration (JSON file) to record on a copy of the run")
    ap.add_argument("--hypotheses", action="store_true", help="shoot the Hypotheses view")
    ap.add_argument("--deliverable", action="store_true", help="shoot the Deliverable view")
    ap.add_argument("--logs", action="store_true", help="shoot the Logs view")
    ap.add_argument("--live", action="store_true", help="with --logs: append lines to run.log while shooting")
    ap.add_argument("--setup", action="store_true", help="shoot Setup, the Stop popover")
    ap.add_argument("--banner", action="store_true",
                    help="shoot the question banner on a fixture with a pending question")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    live_log = None
    if a.logs and a.live:
        _root, live_log = live_fixture(Path(a.study))
        a.study = str(_root)
    study = (setup_fixture(Path(a.study)) if a.setup else question_fixture(Path(a.study)) if a.banner
             else data_fixture(Path(a.study), json.loads(Path(a.objective).read_text()))
             if a.objective else Path(a.study))
    cfg = uvicorn.Config(create_app(study, token="shots"), host="127.0.0.1",
                         port=a.port, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{a.port}"
    for _ in range(60):
        try:
            httpx.get(base + "/api/runs", timeout=0.3)
            break
        except httpx.TransportError:
            time.sleep(0.2)

    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    if a.setup or a.banner or a.data or a.hypotheses or a.deliverable or a.logs:
        if a.setup:
            setup_shots(base, out, problems)
        elif a.logs:
            logs_shots(base, out, problems, live_log)
        else:
            (banner_shots if a.banner else hypothesis_shots if a.hypotheses
             else deliverable_shots if a.deliverable else data_shots)(base, out, problems)
        server.should_exit = True
        for p in problems:
            print("PROBLEM:", p)
        print(f"{len(list(out.glob('*.png')))} shots in {out}")
        return 1 if problems else 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for wname, (w, h) in WIDTHS.items():
            for theme in THEMES:
                ctx = browser.new_context(
                    viewport={"width": w, "height": h}, color_scheme=theme)
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: problems.append(f"JS error: {e}"))
                for view in VIEWS:
                    q = f"?view={view}" + (f"&run={a.run}" if a.run else "")
                    pg.goto(f"{base}/ui{q}")
                    pg.wait_for_selector(".views [role=tab]")
                    pg.wait_for_timeout(500)
                    name = f"{view}-{wname}-{theme}"
                    pg.screenshot(path=str(out / f"{name}.png"))
                    if w <= 400:
                        sw = pg.evaluate(
                            "document.documentElement.scrollWidth - "
                            "document.documentElement.clientWidth")
                        if sw > 0:
                            problems.append(f"{name}: horizontal scroll by {sw}px")
                    if view == "timeline" and a.sel:
                        pg.goto(f"{base}/ui{q}&sel={a.sel}")
                        pg.wait_for_selector(".ih")
                        pg.wait_for_timeout(300)
                        pg.screenshot(
                            path=str(out / f"timeline-selected-{wname}-{theme}.png"))
                        hits = pg.evaluate(
                            "(()=>{const i=document.querySelector('#insp');"
                            "if(!i||getComputedStyle(i).position==='fixed')return [];"
                            "const r=i.getBoundingClientRect();"
                            "return [...document.querySelectorAll('.card,.gate span')]"
                            ".filter(c=>{const b=c.getBoundingClientRect();"
                            "return b.width>0&&b.left<r.right&&b.right>r.left&&b.top<r.bottom&&b.bottom>r.top})"
                            ".map(c=>c.textContent.trim().slice(0,12))})()")
                        if hits:
                            problems.append(f"timeline-selected-{wname}-{theme}: "
                                            f"content under the inspector {hits}")
                        pg.goto(f"{base}/ui{q}")
                        pg.wait_for_selector(".views [role=tab]")
                    if view == "timeline":
                        pg.wait_for_selector(".gate", state="attached", timeout=5000)
                        pg.locator(".gate").first.scroll_into_view_if_needed()
                        pg.evaluate("document.getElementById('work').scrollTop-=200")
                        pg.wait_for_timeout(200)
                        pg.screenshot(path=str(out / f"timeline-gate-{wname}-{theme}.png"))
                        pg.evaluate("document.getElementById('work').scrollTop=0")
                    clipped = pg.evaluate(
                        "[...document.querySelectorAll('.mono,.id')]"
                        ".filter(e=>e.scrollWidth>e.clientWidth+1&&e.clientWidth>0)"
                        ".map(e=>e.textContent.trim().slice(0,20))")
                    if clipped:
                        problems.append(f"{name}: clipped ids {clipped}")
                ctx.close()
        browser.close()
    server.should_exit = True
    print(f"{len(list(out.glob('*.png')))} shots in {out}")
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
