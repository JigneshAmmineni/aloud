"""Pipeline assembly contracts (SDD §2.3, §2.5, §2.6; §4.10).

These pin the invariants that future features (context engine, memory)
are most likely to break when they touch the per-session setup.
"""

import pytest
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
    from db.documents_repo import AttachedDocument

    docs = [AttachedDocument(1, "arch.md", "markdown", "cascade pipeline")]
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


def test_greeting_trigger_wiring_and_failure_fallback(monkeypatch):
    """PR #19 review: the lookup → trigger seam, including the bounded
    failure path — a broken lookup degrades to the nameless greeting."""
    import asyncio

    import agent.companion as companion
    from agent.prompts import GREETING_TRIGGER

    async def found(user_id):
        assert user_id == "uid-x"
        return "Jignesh"

    monkeypatch.setattr(companion, "get_preferred_name", found)
    from loguru import logger

    trigger = asyncio.run(companion._greeting_trigger_for("uid-x", logger))
    assert "Jignesh" in trigger

    async def broken(user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(companion, "get_preferred_name", broken)
    trigger = asyncio.run(companion._greeting_trigger_for("uid-x", logger))
    assert trigger == GREETING_TRIGGER  # nameless fallback, no raise


def test_greeting_name_lookup_is_time_bounded(monkeypatch):
    """Round-2 review: the 1s bound itself — a hung pool degrades to the
    nameless greeting instead of delaying the session."""
    import asyncio
    import time

    from loguru import logger

    import agent.companion as companion
    from agent.prompts import GREETING_TRIGGER

    async def hung(user_id):
        await asyncio.sleep(30)

    monkeypatch.setattr(companion, "get_preferred_name", hung)
    t0 = time.monotonic()
    trigger = asyncio.run(companion._greeting_trigger_for("uid-x", logger))
    assert trigger == GREETING_TRIGGER
    assert time.monotonic() - t0 < 5  # the wait_for bound, not the hang

    asyncio.run(asyncio.sleep(0))  # nothing pending leaks


def test_run_registers_provider_and_unregisters_in_finally(
    make_settings, monkeypatch, tmp_path
):
    """FR-52: the provider registers at pipeline construction and is
    removed in the SAME finally as the live-task pop — a provider left
    registered pins the session's full conversation in process memory for
    the container's life. Covers the error path: even a crashing run
    unregisters."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import agent.companion as companion
    import agent.registry as registry_mod
    from db.engine import init_db
    from db.users_repo import provision_user

    seen: dict = {}

    class StubRunner:
        def __init__(self, *a, **kw):
            pass

        async def run(self, task):
            seen["registered_during_run"] = "s-reg-test" in registry_mod._providers
            raise RuntimeError("pipeline crashed")

    stub_transport = MagicMock()
    stub_transport.input.return_value = MagicMock()
    stub_transport.output.return_value = MagicMock()
    stub_transport.event_handler.return_value = lambda fn: fn

    stub_task = MagicMock()
    stub_task.turn_tracking_observer = None
    stub_task.queue_frames = AsyncMock()

    monkeypatch.setattr(companion, "SmallWebRTCTransport", lambda **kw: stub_transport)
    monkeypatch.setattr(companion, "Pipeline", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(companion, "PipelineTask", lambda *a, **kw: stub_task)
    monkeypatch.setattr(companion, "PipelineRunner", StubRunner)

    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/reg.db")
        await provision_user("uid-reg", None)
        agent = companion.CompanionAgent(
            make_settings(), None, user_id="uid-reg", session_id="s-reg-test"
        )
        conn = MagicMock()
        conn.pc_id = "pc-reg"
        with pytest.raises(RuntimeError):
            await agent.run(conn)

    asyncio.run(run())
    assert seen["registered_during_run"] is True
    assert "s-reg-test" not in registry_mod._providers  # the finally held
