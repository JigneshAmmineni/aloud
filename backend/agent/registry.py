"""FR-52/53: the session-provider registry — how row changes made over HTTP
reach the context blocks of live sessions.

Module-level and therefore SAME-PROCESS ONLY — a stated assumption, not one
to discover under scaling: deployment today is one uvicorn worker, where
every live session is reachable. A second worker silently breaks the
guarantee (a destroyed document's 🔒 content keeps shipping to sessions on
the other worker); scaling past one worker is the revisit trigger, and the
replacement is a cross-process channel (e.g. Postgres LISTEN/NOTIFY), not a
bigger dict.

Entries are registered at pipeline construction and removed in the same
`finally` that pops the live-task map — a provider left registered pins the
session's full 🔒 conversation in process memory for the container's life.
Notifications call the provider's SYNCHRONOUS mutation methods (no `await`
between reading the section map and rewriting it), the same single-thread
property that makes update_tool_result safe from a detached write task; a
no-op when no session is live, and safe after teardown exactly like the
emit callback.
"""

from loguru import logger

# session_id -> (user_id, provider)
_providers: dict[str, tuple[str, object]] = {}


def register_provider(session_id: str, user_id: str, provider) -> None:
    _providers[session_id] = (user_id, provider)


def unregister_provider(session_id: str) -> None:
    _providers.pop(session_id, None)


def live_providers_for(user_id: str) -> list:
    """Every live session's provider belonging to this user."""
    return [p for uid, p in _providers.values() if uid == user_id]


def notify_document_deleted(user_id: str, document_id: int) -> None:
    """FR-52: a destroyed row must stop shipping from EVERY live session of
    its owner — the block is re-sent on every step, so a stale section would
    keep 🔒 content the user destroyed in play for the rest of the session."""
    providers = live_providers_for(user_id)
    for provider in providers:
        provider.remove_document_section(document_id)
    logger.bind(
        component="agent.registry",
        event="registry.document_deleted",
        document_id=document_id,
        notified=len(providers),
    ).info("delete propagated to live sessions")


def notify_document_updated(
    user_id: str, document_id: int, title: str, content: str
) -> None:
    """FR-53.8: edits reconcile cross-session like deletes — two tabs with
    the same document attached must not leave one holding pre-edit text as
    authoritative system context. Sessions without this id attached ignore
    it (update_document_section is a no-op for unknown ids)."""
    for provider in live_providers_for(user_id):
        provider.update_document_section(document_id, title, content)
