"""Upload text extraction (§4.11 FR-51). Storage lives in
db/documents_repo.py — the swap the old InMemoryDocumentStore's docstring
promised; this module keeps the pure extraction half: format detection,
decoding, and the size caps.

Extraction is synchronous CPU work (pypdf holds the GIL), so the route runs
it through run_in_threadpool bounded by EXTRACT_SEMAPHORE — the pool keeps
the event loop (and every live voice pipeline) unblocked; the semaphore
keeps 20/min of permitted uploads from parsing concurrently on 2 shared
vCPUs.
"""

import asyncio
import io

from pypdf import PdfReader

# Size caps. Bytes are checked on upload; chars are checked after extraction.
# MAX_TOTAL_CHARS bounds a whole session's document context (enforced where the
# context block is built AND in the repo's attach fetch) to protect the
# latency budget — more input tokens raise time-to-first-token.
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB per file
MAX_DOC_CHARS = 200_000  # per document, after extraction
MAX_TOTAL_CHARS = 400_000  # across all docs attached to one session

# FR-51: at most this many extractions run at once; excess uploads queue.
EXTRACT_CONCURRENCY = 2
EXTRACT_SEMAPHORE = asyncio.Semaphore(EXTRACT_CONCURRENCY)

_TRUNCATION_MARKER = "\n\n[document truncated]"


class DocumentError(Exception):
    """Upload could not be accepted (unsupported type, empty, oversize, or
    unreadable). Routes map this to HTTP 400 with the message shown to the user."""


def detect_format(filename: str, content_type: str | None) -> str | None:
    """Resolve to "markdown" | "text" | "pdf" | None — the FR-50 three-way
    split (collapsing markdown into text would render an uploaded plan.md
    preformatted under FR-54, for the file type this product most expects).
    Extension wins (browsers send inconsistent content types — e.g.
    application/octet-stream for .md); content type is the fallback."""
    name = filename.lower()
    if name.endswith((".md", ".markdown")):
        return "markdown"
    if name.endswith(".txt"):
        return "text"
    if name.endswith(".pdf"):
        return "pdf"
    ct = (content_type or "").lower()
    if ct == "text/markdown":
        return "markdown"
    if ct == "text/plain":
        return "text"
    if ct == "application/pdf":
        return "pdf"
    return None


def _extract_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # encrypted, corrupt, etc.
        raise DocumentError("could not read this PDF") from e


def extract_text(filename: str, content_type: str | None, data: bytes) -> tuple[str, str]:
    """Extract plain text from an uploaded file; returns (text, format).
    Raises DocumentError for unsupported types, oversize files, or files
    with no readable text."""
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(
            f"file is too large (limit {MAX_FILE_BYTES // (1024 * 1024)} MB)"
        )

    format = detect_format(filename, content_type)
    if format in ("markdown", "text"):
        text = data.decode("utf-8", errors="replace")
    elif format == "pdf":
        text = _extract_pdf(data)
    else:
        raise DocumentError("unsupported file type — upload .txt, .md, or .pdf")

    text = text.strip()
    if not text:
        raise DocumentError("no readable text found in this file")

    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + _TRUNCATION_MARKER
    return text, format
