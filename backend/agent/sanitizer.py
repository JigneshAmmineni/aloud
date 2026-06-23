"""TTS text sanitization (SDD §2.3): clean LLM output before it reaches TTS.

Two spoken-text filters, both Pipecat TTS text filters (they run on
sentence-aggregated text, where stripping/rewriting is reliable — streaming
LLM frames can split a token across two frames):

- MarkdownTextFilter strips markdown (asterisks, lists, headings) so the voice
  doesn't read the symbols aloud. Toggled by TTS_SANITIZE_ENABLED for A/B
  listening.
- IdentifierTextFilter reads snake_case identifiers naturally — e.g. the tool
  name "create_artifact" becomes "create artifact" instead of "create
  underscore artifact". Always on; it's a correctness fix, not an A/B toggle.
"""

import re

from pipecat.utils.text.base_text_filter import BaseTextFilter
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

# Underscores only between word characters → spaces, so snake_case identifiers
# are spoken as words. Leaves other underscores (rare in prose) alone.
_SNAKE_UNDERSCORE = re.compile(r"(?<=\w)_(?=\w)")


class IdentifierTextFilter(BaseTextFilter):
    """Make snake_case identifiers speakable (underscore → space)."""

    async def filter(self, text: str) -> str:
        return _SNAKE_UNDERSCORE.sub(" ", text)


def make_text_filters(enabled: bool) -> list:
    filters: list = []
    if enabled:
        filters.append(MarkdownTextFilter())
    filters.append(IdentifierTextFilter())  # always on
    return filters
