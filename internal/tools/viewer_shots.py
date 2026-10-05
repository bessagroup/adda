"""Screenshot set for the viewer UI (spec 15 section 8): views x widths x themes.

    uv run python internal/tools/viewer_shots.py STUDY_DIR OUT_DIR [--run RUN_ID] [--sel ID]

Also reports the visual-bug checks it can make mechanically: horizontal scroll at
400 px, uncaught JS errors, and any id-looking text that is clipped.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from adda._src.viewer.app import create_app

VIEWS = ["timeline", "hypotheses", "data", "deliverable", "logs", "setup"]
WIDTHS = {"1600": (1600, 1000), "400": (400, 900)}
THEMES = ["light", "dark"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("study")
    ap.add_argument("out")
    ap.add_argument("--run")
    ap.add_argument("--sel", help="selection for the timeline shot")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    cfg = uvicorn.Config(create_app(Path(a.study)), host="127.0.0.1",
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
