"""Document extraction, the ephemeral store, and context-block formatting."""

import pytest

import app.documents as documents
from agent.prompts import BANNED_WORDS, build_document_context_block
from app.documents import (
    Document,
    DocumentError,
    InMemoryDocumentStore,
    extract_text,
)


def _make_pdf(text: str) -> bytes:
    """A minimal single-page PDF with one extractable text run. Offsets are
    computed so the xref table is valid (no reliance on pypdf's recovery)."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = b"BT /F1 24 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objects) + 1
    out += b"xref\n0 " + str(n).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    return bytes(out)


def test_extract_text_decodes_plain_text():
    assert extract_text("notes.txt", "text/plain", b"hello world") == "hello world"


def test_extract_text_decodes_markdown_by_extension():
    # browsers often send octet-stream for .md — extension must still win
    out = extract_text("design.md", "application/octet-stream", b"# Title\nbody")
    assert "Title" in out and "body" in out


def test_extract_text_reads_pdf():
    out = extract_text("doc.pdf", "application/pdf", _make_pdf("Hello PDF document"))
    assert "Hello PDF document" in out


def test_extract_text_rejects_unsupported_type():
    with pytest.raises(DocumentError):
        extract_text("pic.png", "image/png", b"\x89PNG\r\n\x1a\n")


def test_extract_text_rejects_empty_after_strip():
    with pytest.raises(DocumentError):
        extract_text("blank.txt", "text/plain", b"   \n\t  ")


def test_extract_text_rejects_pdf_with_no_text():
    # a valid-looking but unreadable PDF blob -> DocumentError, not a crash
    with pytest.raises(DocumentError):
        extract_text("scan.pdf", "application/pdf", b"%PDF-1.4\nnot really a pdf")


def test_extract_text_enforces_byte_cap(monkeypatch):
    monkeypatch.setattr(documents, "MAX_FILE_BYTES", 10)
    with pytest.raises(DocumentError):
        extract_text("big.txt", "text/plain", b"x" * 11)


def test_extract_text_truncates_long_text(monkeypatch):
    monkeypatch.setattr(documents, "MAX_DOC_CHARS", 5)
    out = extract_text("long.txt", "text/plain", b"abcdefghij")
    assert out.startswith("abcde")
    assert out.endswith(documents._TRUNCATION_MARKER)


def test_store_add_and_get_round_trip():
    store = InMemoryDocumentStore()
    a = store.add("local-user", "a.md", "text/markdown", "alpha")
    b = store.add("local-user", "b.md", "text/markdown", "beta")
    got = store.get("local-user", [a.id, b.id])
    assert [d.content for d in got] == ["alpha", "beta"]
    assert a.char_count == len("alpha")


def test_store_get_skips_unknown_and_other_users():
    store = InMemoryDocumentStore()
    a = store.add("local-user", "a.md", "text/markdown", "alpha")
    assert store.get("local-user", ["nope"]) == []
    assert store.get("someone-else", [a.id]) == []


def test_context_block_includes_filename_and_content():
    docs = [Document("d1", "arch.md", "text/markdown", "cascade pipeline", 16)]
    block = build_document_context_block(docs)
    assert "arch.md" in block
    assert "cascade pipeline" in block


def test_context_block_framing_has_no_banned_words():
    # the static framing is product copy (C-3); user content is exempt
    docs = [Document("d1", "x.txt", "text/plain", "", 0)]
    block = build_document_context_block(docs).lower()
    for word in BANNED_WORDS:
        assert word not in block
