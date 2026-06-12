"""C-3 word-ban test (SDD §8): banned words never appear in any prompt block."""

from agent.prompts import BANNED_WORDS, build_system_prompt


def test_system_prompt_contains_no_banned_words():
    prompt = build_system_prompt().lower()
    for word in BANNED_WORDS:
        assert word not in prompt


def test_system_prompt_is_nonempty():
    assert build_system_prompt().strip()
