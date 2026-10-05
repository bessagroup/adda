"""Spec 15 section 8 acceptance checks on the UI stylesheet: contrast (AA) over the
token pairs in both themes, no colour literal outside the token blocks, and every
font-size and spacing value drawn from the scale."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parents[1] / "src/adda/_src/viewer/static/ui/ui.css").read_text()

_LIGHT = CSS[CSS.index(":root{"):CSS.index("@media (prefers-color-scheme:dark)")]
_DARK = CSS[CSS.index(':root[data-theme="dark"]{'):CSS.index("*{box-sizing")]
_MEDIA_DARK = CSS[CSS.index("@media (prefers-color-scheme:dark)"):
                  CSS.index(':root[data-theme="dark"]{')]


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):(#[0-9A-Fa-f]{6})\b", block))


def _lum(hexc: str) -> float:
    ch = [int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


_FG = ["ink", "ink-2", "ink-3", "ok", "warn", "bad", "select", "live",
       "r-strategizer", "r-literature_reviewer", "r-datagenerator",
       "r-implementer", "r-critic"]
_BG = ["ground", "surface", "surface-2"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_tokens_meet_aa_on_every_surface(theme):
    light = _tokens(_LIGHT)
    t = light if theme == "light" else {**light, **_tokens(_DARK)}
    bad = [(f, b, round(_ratio(t[f], t[b]), 2)) for f in _FG for b in _BG
           if _ratio(t[f], t[b]) < 4.5]
    assert not bad, f"below AA 4.5:1 in {theme}: {bad}"


def test_the_two_dark_blocks_agree():
    media = _tokens(_MEDIA_DARK)
    assert media == _tokens(_DARK)


def test_no_colour_literal_outside_the_token_blocks():
    rest = CSS.replace(_LIGHT, "").replace(_MEDIA_DARK, "").replace(
        CSS[CSS.index(':root[data-theme="dark"]{'):CSS.index("*{box-sizing")], "")
    hits = re.findall(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(|hsla?\(", rest)
    assert not hits, f"colour literals outside :root: {hits}"


def test_font_sizes_and_spacing_come_from_the_scale():
    sizes = set(re.findall(r"font(?:-size)?:[^;{}/]*?(?<![/\d])(\d+)px", CSS))
    assert sizes <= {"11", "12", "13", "14", "16", "20", "26"}, sizes
    # tokens themselves are defined in the :root block; usage must go through var()
    outside = CSS[CSS.index("*{box-sizing"):]
    raw = re.findall(r"(?:padding|margin|gap)[^;{}]*?(?<![\w-])(\d+)px", outside)
    off = [v for v in raw if v not in {"0", "1", "2", "3", "4", "6", "8", "12", "16", "24", "32"}]
    assert not off, f"off-scale spacing: {off}"
