"""§4.12 FR-56: PUT /documents/{id} — the CAS save. Route-level through
the real app (the token round-trips as the opaque string the client will
echo), repo-level where the precedence needs pinning."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import agent.registry as registry_mod
import app.main as main
from db.documents_repo import save_document_content
from db.engine import session_factory
from db.models import UsageEvent


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _upload(client, name="notes.md", content=b"v1", mime="text/markdown"):
    return client.post(
        "/documents", files={"file": (name, content, mime)}
    ).json()


def test_save_chain_fresh_null_base_and_rename(client, auth_as):
    """The 200's updated_at is the next base — saving twice in a row with
    each response's EXACT string succeeds (the self-409 regression, pinned
    where the client lives: on the serialized token)."""
    auth_as("uid-save")
    doc = _upload(client)
    base = client.get(f"/documents/{doc['id']}").json()["updated_at"]  # None

    r1 = client.put(
        f"/documents/{doc['id']}",
        json={"content": "v2", "base_updated_at": base},
    )
    assert r1.status_code == 200
    assert r1.json()["char_count"] == 2

    # chain: the echoed string is the next base, byte-for-byte
    r2 = client.put(
        f"/documents/{doc['id']}",
        json={
            "content": "v3 longer",
            "base_updated_at": r1.json()["updated_at"],
            "title": "renamed.md",
        },
    )
    assert r2.status_code == 200
    got = client.get(f"/documents/{doc['id']}").json()
    assert (got["content"], got["title"]) == ("v3 longer", "renamed.md")

    # empty-after-strip title: the one field FR-57.1 leaves on screen
    r = client.put(
        f"/documents/{doc['id']}",
        json={
            "content": "x",
            "base_updated_at": r2.json()["updated_at"],
            "title": "   ",
        },
    )
    assert r.status_code == 400
    r = client.put(
        f"/documents/{doc['id']}",
        json={
            "content": "x",
            "base_updated_at": r2.json()["updated_at"],
            "title": "t" * 201,
        },
    )
    assert r.status_code == 400 and "limit" in r.json()["detail"]


def test_stale_base_409s_with_current_and_writes_nothing(client, auth_as):
    auth_as("uid-stale")
    doc = _upload(client)
    r1 = client.put(
        f"/documents/{doc['id']}",
        json={"content": "first", "base_updated_at": None},
    )
    assert r1.status_code == 200

    # a second writer with the OLD (null) base: refused, handed the truth
    r2 = client.put(
        f"/documents/{doc['id']}",
        json={"content": "second", "base_updated_at": None},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["content"] == "first"
    assert body["updated_at"] == r1.json()["updated_at"]
    assert client.get(f"/documents/{doc['id']}").json()["content"] == "first"

    # merge-and-retry with the 409's base: lands
    r3 = client.put(
        f"/documents/{doc['id']}",
        json={"content": "merged", "base_updated_at": body["updated_at"]},
    )
    assert r3.status_code == 200


def test_precedence_pdf_ceiling_and_stale_over_ceiling(client, auth_as, monkeypatch):
    """FR-56's stated order: format → stale(409) → ceiling — a save both
    stale and over-ceiling gets the 409 first, and after one merge the
    ceiling refusal, never a loop."""
    import app.main as main_mod

    auth_as("uid-prec")
    doc = _upload(client, name="big.txt", content=b"seed", mime="text/plain")

    monkeypatch.setattr(main_mod, "MAX_DOC_CHARS", 10)
    base = None
    r1 = client.put(
        f"/documents/{doc['id']}",
        json={"content": "123456789", "base_updated_at": base},
    )
    assert r1.status_code == 200

    # stale AND over-ceiling: 409 wins first
    r2 = client.put(
        f"/documents/{doc['id']}",
        json={"content": "x" * 50, "base_updated_at": None},
    )
    assert r2.status_code == 409

    # retry with the fresh base: now the ceiling answers, naming the limit
    r3 = client.put(
        f"/documents/{doc['id']}",
        json={"content": "x" * 50, "base_updated_at": r2.json()["updated_at"]},
    )
    assert r3.status_code == 400 and "size limit" in r3.json()["detail"]

    # shrink on an over-ceiling row succeeds (repo-level, seeded over cap)
    async def shrink():
        from db.documents_repo import insert_upload_row

        row = await insert_upload_row("uid-prec", "over.txt", "text", "y" * 50)
        out = await save_document_content(
            "uid-prec", row.id, "y" * 20,
            base_updated_at=None, title=None, ceiling=10,
        )
        assert out.row is not None and len(out.row.content) == 20

    asyncio.run(shrink())


def test_pdf_save_refuses(client, auth_as):
    async def run():
        from db.documents_repo import insert_upload_row

        row = await insert_upload_row("uid-pdf", "scan.pdf", "pdf", "extracted")
        out = await save_document_content(
            "uid-pdf", row.id, "new",
            base_updated_at=None, title=None, ceiling=1000,
        )
        assert out.reason == "not_editable"

    auth_as("uid-pdf")
    _upload(client)  # provisions uid-pdf
    asyncio.run(run())


def test_nfr8_negative_and_no_usage_event(client, auth_as):
    auth_as("uid-owner")
    doc = _upload(client)

    auth_as("uid-intruder")
    r = client.put(
        f"/documents/{doc['id']}",
        json={"content": "hijack", "base_updated_at": None},
    )
    assert r.status_code == 404

    auth_as("uid-owner")
    assert client.get(f"/documents/{doc['id']}").json()["content"] == "v1"
    ok = client.put(
        f"/documents/{doc['id']}",
        json={"content": "mine", "base_updated_at": None},
    )
    assert ok.status_code == 200

    async def check():
        async with session_factory()() as db:
            events = (
                (
                    await db.execute(
                        select(UsageEvent).where(UsageEvent.stage == "artifact")
                    )
                )
                .scalars()
                .all()
            )
        assert events == []  # the user's own typing spends nothing

    asyncio.run(check())


def test_save_reconciles_registered_providers_with_title(client, auth_as):
    """FR-56: a user save reaches live attach blocks like an agent edit —
    content AND title (a rename shipping only content leaves live blocks
    addressing the old name)."""

    class FakeProvider:
        def __init__(self):
            self.updates = []

        def update_document_section(self, doc_id, title, content):
            self.updates.append((doc_id, title, content))
            return True

        def remove_document_section(self, doc_id):
            return True

    auth_as("uid-live")
    doc = _upload(client)
    provider = FakeProvider()
    registry_mod.register_provider("s-live-save", "uid-live", provider)
    try:
        r = client.put(
            f"/documents/{doc['id']}",
            json={
                "content": "fresh",
                "base_updated_at": None,
                "title": "renamed.md",
            },
        )
        assert r.status_code == 200
        assert provider.updates == [(doc["id"], "renamed.md", "fresh")]
    finally:
        registry_mod.unregister_provider("s-live-save")
