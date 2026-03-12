"""Unit tests for browser learning extraction config loading."""

from __future__ import annotations

from app.config import Settings


def test_browser_learning_extraction_defaults():
    settings = Settings()
    extraction = settings.browser_learning.extraction
    assert extraction.dedup_enabled is True
    assert extraction.variable_extraction_enabled is True
    assert extraction.llm.enabled is False


def test_browser_learning_extraction_env_prefix(monkeypatch):
    monkeypatch.setenv("BAY_BROWSER_LEARNING__EXTRACTION__DEDUP_ENABLED", "false")
    monkeypatch.setenv(
        "BAY_BROWSER_LEARNING__EXTRACTION__VARIABLE_EXTRACTION_ENABLED",
        "false",
    )
    monkeypatch.setenv("BAY_BROWSER_LEARNING__EXTRACTION__LLM__ENABLED", "true")
    monkeypatch.setenv(
        "BAY_BROWSER_LEARNING__EXTRACTION__LLM__API_BASE",
        "https://llm.example/v1",
    )
    monkeypatch.setenv("BAY_BROWSER_LEARNING__EXTRACTION__LLM__API_KEY", "sk-test")
    monkeypatch.setenv("BAY_BROWSER_LEARNING__EXTRACTION__LLM__MODEL", "gpt-test")
    monkeypatch.setenv("BAY_BROWSER_LEARNING__EXTRACTION__LLM__TIMEOUT_SECONDS", "9")

    settings = Settings()
    extraction = settings.browser_learning.extraction
    assert extraction.dedup_enabled is False
    assert extraction.variable_extraction_enabled is False
    assert extraction.llm.enabled is True
    assert extraction.llm.api_base == "https://llm.example/v1"
    assert extraction.llm.api_key == "sk-test"
    assert extraction.llm.model == "gpt-test"
    assert extraction.llm.timeout_seconds == 9
