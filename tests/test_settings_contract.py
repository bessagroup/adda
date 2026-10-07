"""The `runtime:` config block is an explicit, documented contract.

Two failure modes this file exists to prevent, both of the same kind — a
setting that silently does nothing:

1. A knob is read in the code but missing from ``KNOWN_KEYS``, so putting it
   in a study's ``config.yaml`` triggers the "unrecognised key" warning even
   though it works.
2. A knob is documented or listed but nothing reads it, so a user sets it and
   nothing happens.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from adda._src.runtime import settings

_SRC = Path(__file__).resolve().parent.parent / "src" / "adda" / "_src"
_DOC = (Path(__file__).resolve().parent.parent
        / "docs" / "config-reference.md")

# get_bool("debug", False) / get_str("semantic_scholar_api_key", "") / ...
# `_?` so aliased imports (`from ...settings import get_int as _get_int`)
# are matched too — missing those made three live knobs look orphaned.
_CALL = re.compile(
    r"_?get_(?:bool|int|float|str)\(\s*[\"']([a-z0-9_]+)[\"']\s*,\s*([^)]*)\)")


# A default computed from other settings cannot be a literal at the call site:
# the key is read with this sentinel and the documented rule replaces it.
_DERIVED_DEFAULTS = {"stop_grace_s": ("math.nan", "min(900, deadline/10)")}


_VIEWER_READ = {"launch": "none"}


def _keys_read_in_source() -> set[str]:
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        if path.name == "settings.py":
            continue  # its own signatures, not call sites
        found |= {m[0] for m in _CALL.findall(path.read_text(encoding="utf-8"))}
    # runtime.features reads its knobs through get_bool(f.key, f.default) —
    # one call site, many keys — so the literal-argument grep above cannot see
    # them. The registry IS the read site: declaring a Feature is what makes
    # the knob live, and features.enabled() raises on a key it does not know.
    from adda._src.runtime import features
    found |= set(features.FEATURE_KEYS)
    # `launch` is a mapping the viewer reads from config.yaml itself
    # (viewer/run_control.py::launcher_config), not through get_*.
    found |= set(_VIEWER_READ)
    return found


def test_every_knob_read_in_the_code_is_a_known_key():
    """Otherwise setting it in config.yaml warns that it is unrecognised
    while quietly working — the config lying about itself."""
    missing = _keys_read_in_source() - settings.KNOWN_KEYS
    assert not missing, (
        f"read via settings.get_* but absent from KNOWN_KEYS: {sorted(missing)}"
    )


def test_every_known_key_is_actually_read_somewhere():
    """A knob nothing reads is a setting a user can set with no effect."""
    orphans = settings.KNOWN_KEYS - _keys_read_in_source()
    assert not orphans, (
        f"in KNOWN_KEYS but never read: {sorted(orphans)}"
    )


def test_unrecognised_runtime_key_warns(caplog):
    with caplog.at_level("WARNING"):
        settings.configure({"debug": True, "dbeug": True})
    assert "dbeug" in caplog.text
    assert "unrecognised" in caplog.text.lower()
    settings.configure(None)


def test_recognised_keys_do_not_warn(caplog):
    with caplog.at_level("WARNING"):
        settings.configure({"debug": True, "recursion_limit": 100})
    assert "unrecognised" not in caplog.text.lower()
    settings.configure(None)


@pytest.mark.parametrize("key", sorted(settings.KNOWN_KEYS))
def test_runtime_block_is_documented_for_study_authors(key):
    """Explicit over implicit: a knob a user is expected to set belongs in the
    study-authoring docs, not only in the source that reads it."""
    text = _DOC.read_text(encoding="utf-8")
    assert "`runtime`" in text or "runtime:" in text, (
        "docs/config-reference.md does not document the runtime: block at all"
    )
    assert key in text, f"runtime knob {key!r} is undocumented for study authors"


def _defaults_in_source() -> dict[str, str]:
    """Every knob's real default, from BOTH places one can be declared.

    Call sites (``get_bool("debug", False)``) were the only place when this
    was written. The Feature registry then moved every ablation knob's
    default into its ``Feature(...)`` declaration, so the call site reads
    ``get_bool(f.key, f.default)`` and this scraper found no literal --
    reporting six of the seven feature knobs as SKIPPED rather than failing.

    Skips are the dangerous outcome: a suite that is green while checking
    nothing looks exactly like a suite that is green because everything is
    right. ``test_no_feature_knob_is_silently_unchecked`` below now makes
    that state impossible to reach again.
    """
    from adda._src.runtime import features

    out: dict[str, str] = {}
    for path in _SRC.rglob("*.py"):
        if path.name == "settings.py":
            continue
        for key, default in _CALL.findall(path.read_text(encoding="utf-8")):
            out[key] = default.strip()
    for feature in features.FEATURES:
        out[feature.key] = str(feature.default)
    out.update(_VIEWER_READ)
    return out


def test_no_feature_knob_is_silently_unchecked():
    """The guard on the guard.

    ``test_documented_default_matches_the_source`` SKIPS a knob whose default
    it cannot find, and a skip is not a failure -- so the refactor that moved
    feature defaults into the registry turned six real checks into green
    no-ops and nothing said a word. Seven of this table's defaults were wrong
    when it was first written; that is the failure mode the parametrised test
    exists to catch, and for a while it could not have caught it.
    """
    found = _defaults_in_source()
    from adda._src.runtime import features

    missing = sorted(f.key for f in features.FEATURES if f.key not in found)
    assert not missing, (
        f"these feature knobs have no discoverable default, so their "
        f"documented value is unchecked: {missing}")


@pytest.mark.parametrize("key", sorted(settings.KNOWN_KEYS))
def test_documented_default_matches_the_source(key):
    """A documented default that is not the real one is worse than no docs:
    the reader makes a decision on it. Seven of this table's defaults were
    wrong when it was first written, which is why this test exists."""
    src_default = _defaults_in_source().get(key)
    if src_default is None:
        pytest.skip(f"{key} read without a literal default")

    row = next((ln for ln in _DOC.read_text(encoding="utf-8").splitlines()
                if ln.startswith(f"| `{key}` |")), None)
    assert row, f"{key} has no row in the runtime table"
    documented = row.rsplit("|", 2)[1].strip().strip("`")

    norm = {"False": "false", "True": "true", '""': "none", "''": "none"}
    if key in _DERIVED_DEFAULTS:
        sentinel, expected = _DERIVED_DEFAULTS[key]
        assert src_default == sentinel, (
            f"{key}: source reads {src_default!r}, the derived-default "
            f"sentinel is {sentinel!r}")
        assert documented == expected
        return
    expected = norm.get(src_default, src_default)
    # A non-empty string default reads naturally in the table without its
    # quote characters (`auto`, not `"auto"`); the empty string keeps its
    # "none" spelling above.
    if len(expected) >= 2 and expected[0] == expected[-1] and expected[0] in "\"'":
        expected = expected[1:-1]
    assert documented == expected, (
        f"{key}: docs say {documented!r}, source default is {src_default!r}"
    )


# ---------------------------------------------------------------------------
# The knob table serves three audiences; it must not read as one list
#
# `test_runtime_block_is_documented_for_study_authors` only asks that a key
# appear SOMEWHERE in the doc, so every knob landed in one flat table: study
# settings, infrastructure tuning and ablation arms interleaved, 24 rows deep.
# `context_policy` sat directly above `context_window` — "never touch this"
# adjacent to "raise this if your server is small", indistinguishable.
#
# That is a real cost to a study author, who reads the table to find out what
# they may set and is shown seven knobs that can only make their run worse.
# The fix is a heading, and this is what keeps the heading true.
# ---------------------------------------------------------------------------

_ABLATION_HEADING = "## Ablation switches"


def _ablation_table() -> str:
    """Everything under the ablation heading, to the end of its table."""
    text = _DOC.read_text(encoding="utf-8")
    assert _ABLATION_HEADING in text, (
        "the ablation switches no longer have their own heading — every knob "
        "is back in one flat table")
    return text[text.index(_ABLATION_HEADING):]


def test_every_ablation_switch_is_documented_as_one():
    """A new Feature documented in the infrastructure table reads to a study
    author as something they may tune. It is an experimental arm."""
    from adda._src.runtime import features

    table = _ablation_table()
    for f in features.FEATURES:
        assert f"`{f.key}`" in table, (
            f"{f.key} is a Feature but is not documented under "
            f"{_ABLATION_HEADING!r}")


def test_no_ordinary_knob_is_filed_as_an_ablation_switch():
    """The converse, and the easier mistake: a timeout listed among the arms
    invites someone to 'ablate' it and report the result as a finding."""
    from adda._src.runtime import features

    keys = set(re.findall(r"^\| `([a-z0-9_]+)` \|", _ablation_table(),
                          re.MULTILINE))
    stray = keys - {f.key for f in features.FEATURES}
    assert not stray, (
        f"{sorted(stray)} are documented as ablation switches but own no "
        "feature — they are settings, and belong in the run-knobs table")
