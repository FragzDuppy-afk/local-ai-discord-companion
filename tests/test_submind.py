import asyncio
from subsystems import submind


def test_sanitize_search_query_normalizes_gpu_and_strips_filler():
    raw = "Can you please search for where I could possibly find an RTX 5060ti in stock?"
    cleaned = submind.sanitize_search_query(raw)
    assert "5060 ti" in cleaned.lower()
    assert "please" not in cleaned.lower()
    assert "where" not in cleaned.lower()


def test_extract_store_location_query_finds_retailer_and_zip():
    query = "Is there a Best Buy near 54135 with GPUs?"
    result = submind.extract_store_location_query(query, default_location="Legend Lake, WI")
    assert result == "best buy stores 54135 Legend Lake, WI"


def test_extract_store_location_query_returns_none_without_zip():
    assert submind.extract_store_location_query("Best Buy near me") is None


def test_fetch_web_search_returns_no_results_guardrail(monkeypatch):
    # Force all DDGS backends to return empty lists
    monkeypatch.setattr(submind, "_run_ddgs_backends", lambda q, max_results=4: [])
    result = submind.fetch_web_search("nonexistent quantum flux capacitor 99999")
    assert result.startswith("NO_RESULTS_FOUND:")
    assert "do NOT invent addresses" in result


def test_fetch_web_search_triggers_two_step_store_fallback(monkeypatch):
    def fake_ddgs(query, max_results=4):
        if "stores 54135" in query:
            return ["- Best Buy Green Bay: Open until 9 PM (https://example.com)"]
        return []

    monkeypatch.setattr(submind, "_run_ddgs_backends", fake_ddgs)
    result = submind.fetch_web_search("RTX 5060 Ti at Best Buy 54135")
    assert result.startswith("PARTIAL_RESULTS")
    assert "Best Buy Green Bay" in result


def test_enrich_user_prompt_rejects_empty_manual_search():
    # Uses built-in asyncio.run so pytest-asyncio is not required
    enriched, err = asyncio.run(
        submind.enrich_user_prompt("!search   ", user_title="Operator")
    )
    assert enriched == ""
    assert "Include a search topic" in err


def test_cloud_fallback_disabled_without_api_key():
    client = submind.CloudFallbackClient(api_key="")
    assert client.is_enabled is False
    assert client.query("System prompt", [], user_text="Hello") is None
