"""FR-34: cost is a view, not data. Raw units in, estimated dollars out,
priced at the CURRENT configured rates (historical usage re-prices when
rates change — accepted; every display labels these as estimates)."""

from app.config import Settings


def estimate_cost(usage: dict[str, float], settings: Settings) -> dict:
    """`usage` maps "stage.unit" -> quantity (the admin_repo aggregate shape).

    `configured` distinguishes "$0 because rates aren't set" (all four RATE_*
    at their 0.0 default) from "genuinely cost nothing" — the UI renders the
    former as em-dashes, never as a free-looking $0.0000."""
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
        "configured": any(
            (
                settings.rate_stt_per_minute,
                settings.rate_llm_per_1m_tokens_in,
                settings.rate_llm_per_1m_tokens_out,
                settings.rate_tts_per_1m_chars,
            )
        ),
    }
