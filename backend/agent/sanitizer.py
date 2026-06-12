"""TTS text sanitization (SDD §2.3): strip markdown from LLM output before TTS.

Implemented as a Pipecat TTS text filter rather than a standalone frame
processor: streaming LLM text frames can split a markdown token (e.g. ``**``)
across two frames, while TTS text filters run on sentence-aggregated text
where stripping is reliable. Toggled by TTS_SANITIZE_ENABLED.
"""

from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter


def make_text_filters(enabled: bool) -> list:
    return [MarkdownTextFilter()] if enabled else []
