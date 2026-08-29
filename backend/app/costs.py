"""FR-34: cost is a view, not data. Raw units in, estimated dollars out,
priced at the CURRENT configured rates (historical usage re-prices when
rates change — accepted; every display labels these as estimates)."""

from app.config import Settings


def estimate_cost(usage: dict[str, float], settings: Settings) -> dict[str, float]:
    """`usage` maps "stage.unit" -> quantity (the admin_repo aggregate shape)."""
    stt = (usage.get("stt.seconds", 0.0) / 60.0) * settings.rate_stt_per_minute
    llm = (
        usage.get("llm.tokens_in", 0.0) / 1_000_000 * settings.rate_llm_per_1m_tokens_in
        + usage.get("llm.tokens_out", 0.0)
        / 1_000_000
        * settings.rate_llm_per_1m_tokens_out
    )
    tts = usage.get("tts.characters", 0.0) / 1_000_000 * settings.rate_tts_per_1m_chars
    return {
        "stt": round(stt, 4),
        "llm": round(llm, 4),
        "tts": round(tts, 4),
        "total": round(stt + llm + tts, 4),
    }
