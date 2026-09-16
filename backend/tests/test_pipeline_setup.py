"""Pipeline assembly contracts (SDD §2.3, §2.5, §2.6; §4.10).

These pin the invariants that future features (context engine, memory)
are most likely to break when they touch the per-session setup.
"""

from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from agent.companion import build_pipeline_parts
from agent.prompts import build_system_prompt


def test_context_starts_with_exactly_one_system_message(make_settings):
    """FR-20 guard: with no attached documents, nothing but the system prompt
    is injected at session start — no transcripts, no memory. The future memory
    layer must change this test consciously, not by accident."""
    *_, provider, _scratch, _ua = build_pipeline_parts(make_settings())
    messages = provider.build()
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == build_system_prompt()


def test_attached_documents_are_injected_into_context(make_settings):
    """Document upload feature: attached docs ride in alongside the base prompt
    (and only then). The base identity prompt stays intact."""
    from app.documents import Document

    docs = [Document("d1", "arch.md", "text/markdown", "cascade pipeline", 16)]
    *_, provider, _scratch, _ua = build_pipeline_parts(make_settings(), docs)
    combined = " ".join(
        m["content"] for m in provider.build() if isinstance(m.get("content"), str)
    )
    assert build_system_prompt() in combined
    assert "arch.md" in combined
    assert "cascade pipeline" in combined


def test_aggregator_scratch_context_starts_empty(make_settings):
    """FR-44: the user aggregator's LLMContext is turn-assembly scratch
    ONLY — the ContextProvider owns the conversation (system prompt
    included). A scratch that starts with messages would be a second copy
    the reset can't fully clear."""
    *_, scratch, _ua = build_pipeline_parts(make_settings())
    assert scratch.get_messages() == []


def test_user_aggregator_defers_to_external_turn_detection(make_settings):
    """FR-3/FR-13: Flux owns turn detection; the aggregator must not run its
    own VAD-based strategies."""
    *_, user_agg = build_pipeline_parts(make_settings())
    assert isinstance(user_agg._params.user_turn_strategies, ExternalUserTurnStrategies)


def test_sanitizer_toggle_reaches_tts(make_settings):
    """SDD §2.3: TTS_SANITIZE_ENABLED routes the markdown filter into TTS; the
    identifier filter rides along always (snake_case → spoken words)."""
    from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

    from agent.sanitizer import IdentifierTextFilter

    _stt, tts_enabled, *_ = build_pipeline_parts(
        make_settings(tts_sanitize_enabled=True)
    )
    _stt2, tts_disabled, *_ = build_pipeline_parts(
        make_settings(tts_sanitize_enabled=False)
    )
    enabled = tts_enabled._text_filters
    disabled = tts_disabled._text_filters
    assert any(isinstance(f, MarkdownTextFilter) for f in enabled)
    assert any(isinstance(f, IdentifierTextFilter) for f in enabled)
    assert not any(isinstance(f, MarkdownTextFilter) for f in disabled)
    assert any(isinstance(f, IdentifierTextFilter) for f in disabled)


def test_default_providers_are_the_documented_stack(make_settings):
    """SDD §0 decision record: Flux / Cartesia on the audio chassis (the
    LLM slot holds our loop, whose Gemini client is asserted in
    test_loop_llm_client)."""
    stt, tts, *_ = build_pipeline_parts(make_settings())
    assert isinstance(stt, DeepgramFluxSTTService)
    assert isinstance(tts, CartesiaTTSService)


def test_drain_live_sessions_says_goodbye_then_cancels():
    """Graceful-shutdown goodbye: every live pipeline gets a session.ending
    server message over the data channel, then a cancel — and the module
    flag makes those sessions close as 'interrupted', not 'user'."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame

    import agent.companion as companion

    task = MagicMock()
    task.queue_frames = AsyncMock()
    task.cancel = AsyncMock()
    companion._live_tasks["drain-test"] = task
    try:
        assert companion.live_session_count() == 1
        drained = asyncio.run(companion.drain_live_sessions())
        assert drained == 1
        assert companion._draining is True
        frame = task.queue_frames.call_args.args[0][0]
        assert isinstance(frame, RTVIServerMessageFrame)
        assert frame.data == {"type": "session.ending"}
        task.cancel.assert_awaited_once()
    finally:
        companion._live_tasks.pop("drain-test", None)
        companion._draining = False


def test_drain_awaits_inflight_writes_before_cancelling():
    """FR-46 SIGTERM twin, order half: the drain awaits every session's
    in-flight writes BEFORE task.cancel() — cancelling first would tear
    down the recorders a completed write's events need."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import agent.companion as companion

    order: list[str] = []

    async def write():
        await asyncio.sleep(0.05)
        order.append("write_done")

    task = MagicMock()
    task.queue_frames = AsyncMock()

    async def cancel():
        order.append("cancel")

    task.cancel = cancel

    async def run():
        write_task = asyncio.create_task(write())
        companion._live_tasks["drain-w"] = task
        companion._inflight_writes["drain-w"] = {write_task}
        await companion.drain_live_sessions()

    try:
        asyncio.run(run())
        assert order == ["write_done", "cancel"]
    finally:
        companion._live_tasks.pop("drain-w", None)
        companion._inflight_writes.pop("drain-w", None)
        companion._draining = False
