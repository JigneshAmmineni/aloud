"""FR-34: cost is derived from raw units at read time, never stored."""

from app.costs import estimate_cost


def test_estimate_cost_math(make_settings):
    s = make_settings(
        rate_stt_per_minute=0.0060,
        rate_llm_per_1m_tokens_in=0.30,
        rate_llm_per_1m_tokens_out=2.50,
        rate_tts_per_1m_chars=30.0,
    )
    usage = {
        "stt.seconds": 600.0,  # 10 minutes
        "llm.tokens_in": 1_000_000.0,
        "llm.tokens_out": 100_000.0,
        "tts.characters": 50_000.0,
    }
    cost = estimate_cost(usage, s)
    assert cost["stt"] == 0.06
    assert cost["llm"] == round(0.30 + 0.25, 4)
    assert cost["tts"] == 1.5
    assert cost["total"] == round(cost["stt"] + cost["llm"] + cost["tts"], 4)
    assert cost["configured"] is True


def test_estimate_cost_handles_missing_units_and_unconfigured_rates(make_settings):
    # Default rates are 0.0 ("not configured"): estimates are $0, never a
    # crash, and configured=False lets the UI show "—" instead of $0.0000.
    assert estimate_cost({}, make_settings()) == {
        "stt": 0.0,
        "llm": 0.0,
        "tts": 0.0,
        "total": 0.0,
        "configured": False,
    }
    # Unknown keys (e.g. artifact.count) are simply not priced.
    s = make_settings(rate_llm_per_1m_tokens_in=1.0)
    cost = estimate_cost({"artifact.count": 3.0, "llm.tokens_in": 500_000.0}, s)
    assert cost["llm"] == 0.5
    assert cost["total"] == 0.5
