"""Uploaded-document handling: text extraction + an ephemeral per-user store.

Storage is in-memory for the MVP — nothing is persisted (REQUIREMENTS.md §6).
The `DocumentStore` surface (`add` / `get`) is the seam where a DB-backed
repo slots in when the memory layer lands: a future `db/documents_repo.py`
(mirroring `db/sessions_repo.py`) implements the same two methods and persists
documents alongside the conversation. Routes, the agent, and the prompt code
depend only on this surface, so that swap is localized here.
"""

import io
import uuid
from dataclasses import dataclass

from pypdf import PdfReader

# Size caps. Bytes are checked on upload; chars are checked after extraction.
# MAX_TOTAL_CHARS bounds a whole session's document context (enforced where the
# context block is built) to protect the latency budget — more input tokens
# raise time-to-first-token.
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB per file
MAX_DOC_CHARS = 200_000  # per document, after extraction
MAX_TOTAL_CHARS = 400_000  # across all docs attached to one session

_TRUNCATION_MARKER = "\n\n[document truncated]"


class DocumentError(Exception):
    """Upload could not be accepted (unsupported type, empty, oversize, or
    unreadable). Routes map this to HTTP 400 with the message shown to the user."""


@dataclass
class Document:
    id: str
    filename: str
    mime_type: str
    content: str
    char_count: int


def _kind(filename: str, content_type: str | None) -> str | None:
    """Resolve to "text" | "pdf" | None. Extension wins (browsers send
    inconsistent content types — e.g. application/octet-stream for .md)."""
    name = filename.lower()
    if name.endswith((".txt", ".md", ".markdown")):
        return "text"
    if name.endswith(".pdf"):
        return "pdf"
    ct = (content_type or "").lower()
    if ct in ("text/plain", "text/markdown"):
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


def extract_text(filename: str, content_type: str | None, data: bytes) -> str:
    """Extract plain text from an uploaded file. Raises DocumentError for
    unsupported types, oversize files, or files with no readable text."""
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(
            f"file is too large (limit {MAX_FILE_BYTES // (1024 * 1024)} MB)"
        )

    kind = _kind(filename, content_type)
    if kind == "text":
        text = data.decode("utf-8", errors="replace")
    elif kind == "pdf":
        text = _extract_pdf(data)
    else:
        raise DocumentError("unsupported file type — upload .txt, .md, or .pdf")

    text = text.strip()
    if not text:
        raise DocumentError("no readable text found in this file")

    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + _TRUNCATION_MARKER
    return text


class InMemoryDocumentStore:
    """Ephemeral, per-process document store. Documents linger until process
    restart (no eviction) — acceptable for the single-container MVP. Not shared
    across workers; the future DB store removes both limits."""

    def __init__(self) -> None:
        self._docs: dict[str, tuple[str, Document]] = {}  # id -> (user_id, doc)

    def add(self, user_id: str, filename: str, mime_type: str, content: str) -> Document:
        doc = Document(
            id=str(uuid.uuid4()),
            filename=filename,
            mime_type=mime_type,
            content=content,
            char_count=len(content),
        )
        self._docs[doc.id] = (user_id, doc)
        return doc

    def get(self, user_id: str, ids: list[str]) -> list[Document]:
        """Return the requested documents owned by user_id, preserving order.
        Unknown ids are silently skipped (a stale/expired reference must not
        break a session)."""
        out: list[Document] = []
        for doc_id in ids:
            entry = self._docs.get(doc_id)
            if entry is not None and entry[0] == user_id:
                out.append(entry[1])
        return out


# Module-level singleton — the swap point for a DB-backed store later.
document_store = InMemoryDocumentStore()
