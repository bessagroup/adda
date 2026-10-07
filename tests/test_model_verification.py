"""model_verification: on by default; off removes the correctness check and its chapter."""

from __future__ import annotations

from adda._src.agents.datagenerator import DATA_GENERATOR_SYSTEM_PROMPT
from adda._src.knowledge.kb import KnowledgeBase
from adda._src.runtime import features, settings

_ID = "verify-before-you-trust"


def teardown_function():
    settings.configure({})


def _prompt() -> str:
    return features.resolve_gates(DATA_GENERATOR_SYSTEM_PROMPT)


def test_the_feature_is_registered_and_default_on():
    settings.configure({})
    assert "model_verification" in features.FEATURE_KEYS
    assert features.enabled("model_verification")


def test_the_correctness_check_is_in_the_prompt_when_on():
    settings.configure({})
    text = _prompt()
    assert "INTERFACE CHECK: ONE VALIDATED SAMPLE" in text
    assert "CORRECTNESS CHECK" in text
    assert "not metered" in text
    assert "The correctness checks below are not samples" in text
    assert f'ConsultHandbook("{_ID}")' in text
    assert "correctness_checks:" in text


def test_the_correctness_check_is_gone_from_the_prompt_when_off():
    settings.configure({"model_verification": False})
    text = _prompt()
    assert "2. ONE VALIDATED SAMPLE" in text
    for gone in ("CORRECTNESS CHECK", "INTERFACE CHECK", _ID,
                 "correctness_checks", "independent expectations",
                 "are not samples"):
        assert gone not in text


def test_the_chapter_is_for_the_data_generator_and_implementer():
    settings.configure({})
    entry = KnowledgeBase.load().get(_ID)
    assert entry is not None
    assert entry.audience == ["datagenerator", "implementer"]
    assert entry.feature == "model_verification"


def test_the_chapter_is_hidden_when_off():
    settings.configure({"model_verification": False})
    kb = KnowledgeBase.load()
    assert kb.get(_ID) is None
    assert _ID not in kb.menu(audience="datagenerator")
