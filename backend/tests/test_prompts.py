"""Prompt contracts (SDD §2.5): C-3 word ban + the spoken-style rules the
product depends on (FR-4, FR-9). String-level checks are deliberate — if a
prompt rewrite drops one of these behaviors, a human should re-confirm it."""

from agent.prompts import BANNED_WORDS, build_system_prompt


def test_system_prompt_contains_no_banned_words():
    prompt = build_system_prompt().lower()
    for word in BANNED_WORDS:
        assert word not in prompt


def test_system_prompt_is_nonempty():
    assert build_system_prompt().strip()


def test_prompt_keeps_one_question_at_a_time_rule():
    """FR-9."""
    assert "one question at a time" in build_system_prompt().lower()


def test_prompt_keeps_spoken_output_rules():
    """SDD §2.5 block 2: output is read aloud — no markdown/lists/emoji."""
    prompt = build_system_prompt().lower()
    assert "read aloud" in prompt
    assert "markdown" in prompt
    assert "emoji" in prompt


def test_prompt_keeps_greeting_instruction():
    """The greeting kick (LLMRunFrame on connect) relies on this."""
    assert "greet" in build_system_prompt().lower()


def test_prompt_keeps_artifact_instructions():
    """FR-12: tool named, on-request only, never read aloud."""
    prompt = build_system_prompt().lower()
    assert "create_artifact" in prompt
    assert "never read the artifact" in prompt
