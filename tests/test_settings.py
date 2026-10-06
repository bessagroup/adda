"""Central run-knob accessor: config.yaml is the source of truth.

Precedence per knob: explicit > configured config.yaml value > default.
"""
from __future__ import annotations

import pytest

from adda._src.runtime import settings


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    # Each test starts from an empty config and no relevant env vars.
    settings.configure({})
    for var in ("F3DASM_FOO", "F3DASM_FLAG", "F3DASM_N", "F3DASM_X"):
        monkeypatch.delenv(var, raising=False)
    yield
    settings.configure({})


def test_default_when_unset():
    assert settings.get_int("n", 7) == 7
    assert settings.get_float("x", 1.5) == 1.5
    assert settings.get_bool("flag", True) is True
    assert settings.get_str("foo", "d") == "d"


def test_config_value_used_over_default():
    settings.configure({"n": 12, "x": 2.5, "flag": False, "foo": "bar"})
    assert settings.get_int("n", 7) == 12
    assert settings.get_float("x", 1.5) == 2.5
    assert settings.get_bool("flag", True) is False
    assert settings.get_str("foo", "d") == "bar"


def test_bool_accepts_yaml_native_and_string_forms():
    settings.configure({"flag": True})
    assert settings.get_bool("flag", False) is True
    settings.configure({"flag": "on"})
    assert settings.get_bool("flag", False) is True
    settings.configure({"flag": "0"})
    assert settings.get_bool("flag", True) is False


def test_int_tolerates_float_like_string():
    settings.configure({"n": "12.0"})
    assert settings.get_int("n", 0) == 12


def test_configure_replaces_not_merges():
    settings.configure({"n": 1})
    settings.configure({"x": 2.0})
    assert settings.get_int("n", 0) == 0  # prior key gone
    assert settings.get_float("x", 0.0) == 2.0
