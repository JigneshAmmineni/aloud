"""LLM tools. FR-45: the provider-neutral registry the agent loop consults
— each tool is a name, JSON-schema parameters, an async handler, and a
read/write classification (consumed by FR-46's interruption rules).
Handlers receive verified identity and the panel-announce emit callback
from session state via ToolContext — never from the model. Queries live in
db/artifacts_repo; tools stay schema-shaped.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger

from db.artifacts_repo import (
    append_artifact_content,
    create_artifact_row,
    get_artifact_row,
    list_artifact_rows,
    replace_artifact_content,
)
from db.models import Artifact

ARTIFACT_KINDS = ("summary", "action_items", "cleaned_idea")

# FR-45: read_artifact truncation — a tool concern, not a query one. Also
# the edit_artifact replace-refusal threshold: content the model provably
# never saw whole (through the truncating read) must be undeletable.
READ_ARTIFACT_MAX_CHARS = 8_000


@dataclass
class ToolContext:
    """What handlers receive from SESSION STATE, never from the model:
    verified identity, the current turn for usage attribution (FR-45), and
    the loop-injected server-message emit callback — the panel-announce
    seam that keeps Pipecat types out of this module and is safe to call
    after the pipeline closed (FR-46: silent no-op then)."""

    session_id: str
    user_id: str
    turn_id: int | None
    emit: Callable[[dict], Awaitable[None]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema; agent/providers.py translates
    handler: Callable[[dict, ToolContext], Awaitable[dict]]
    is_write: bool  # FR-46: writes finish, reads are abandoned


def _panel_artifact(row: Artifact) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "kind": row.kind,
        "content": row.content,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _create_artifact(args: dict, ctx: ToolContext) -> dict:
    log = logger.bind(session_id=ctx.session_id, component="agent.tools")
    title = str(args.get("title", "")).strip() or "Untitled"
    kind = args.get("kind", "summary")
    if kind not in ARTIFACT_KINDS:
        kind = "summary"
    content = str(args.get("content", "")).strip()
    if not content:
        return {"status": "error", "error": "content is required"}
    try:
        row = await create_artifact_row(
            ctx.user_id, ctx.session_id, kind, title, content, ctx.turn_id
        )
    except Exception as e:
        log.bind(event="tool.create_artifact_failed").error(
            f"artifact save failed: {e}"
        )
        return {"status": "error", "error": "the artifact could not be saved"}
    await ctx.emit({"type": "artifact.created", "artifact": _panel_artifact(row)})
    # No title in logs: artifacts.title is 🔒 and INFO ships to Cloud
    # Logging (FR-39/NFR-9).
    log.bind(event="tool.create_artifact", kind=kind).info(
        f"artifact saved ({kind}, {len(content)} chars)"
    )
    return {
        "status": "created",
        "artifact_id": row.id,
        "title": title,
        "note": "Artifact is now visible on the user's screen.",
    }


async def _list_artifacts(args: dict, ctx: ToolContext) -> dict:
    rows = await list_artifact_rows(ctx.user_id)
    return {
        "artifacts": [
            {
                "id": r.id,
                "title": r.title,
                "kind": r.kind,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


async def _read_artifact(args: dict, ctx: ToolContext) -> dict:
    try:
        artifact_id = int(args.get("artifact_id"))
    except (TypeError, ValueError):
        return {"status": "error", "error": "artifact_id must be an integer"}
    row = await get_artifact_row(ctx.user_id, artifact_id)
    if row is None:
        return {"status": "not_found", "artifact_id": artifact_id}
    truncated = len(row.content) > READ_ARTIFACT_MAX_CHARS
    content = row.content
    if truncated:
        content = content[:READ_ARTIFACT_MAX_CHARS] + "\n[truncated]"
    return {
        "artifact_id": row.id,
        "title": row.title,
        "kind": row.kind,
        "content": content,
        "truncated": truncated,
    }


async def _edit_artifact(args: dict, ctx: ToolContext) -> dict:
    log = logger.bind(session_id=ctx.session_id, component="agent.tools")
    try:
        artifact_id = int(args.get("artifact_id"))
    except (TypeError, ValueError):
        return {"status": "error", "error": "artifact_id must be an integer"}
    mode = args.get("mode")
    if mode not in ("replace", "append"):
        return {"status": "error", "error": "mode must be 'replace' or 'append'"}
    content = str(args.get("content", ""))
    if not content.strip():
        return {"status": "error", "error": "content is required"}
    title = str(args.get("title", "")).strip() or None

    try:
        if mode == "replace":
            current = await get_artifact_row(ctx.user_id, artifact_id)
            if current is None:
                return {"status": "not_found", "artifact_id": artifact_id}
            if len(current.content) > READ_ARTIFACT_MAX_CHARS:
                # The model would be replacing a tail it provably never saw
                # (the read truncates); no versioning/undo in v1, so unseen
                # content must be undeletable. Steer, don't except.
                return {
                    "status": "refused",
                    "error": (
                        "this artifact is longer than the readable window; "
                        "replace would delete content you have not seen — "
                        "use mode 'append' instead"
                    ),
                }
            row = await replace_artifact_content(
                ctx.user_id, artifact_id, content, title, ctx.turn_id
            )
        else:
            row = await append_artifact_content(
                ctx.user_id, artifact_id, "\n" + content, title, ctx.turn_id
            )
    except Exception as e:
        log.bind(event="tool.edit_artifact_failed").error(
            f"artifact edit failed: {e}"
        )
        return {"status": "error", "error": "the edit could not be saved"}
    if row is None:
        return {"status": "not_found", "artifact_id": artifact_id}
    await ctx.emit({"type": "artifact.updated", "artifact": _panel_artifact(row)})
    log.bind(event="tool.edit_artifact", kind=row.kind, mode=mode).info(
        f"artifact {artifact_id} edited ({mode}, {len(content)} chars)"
    )
    return {
        "status": "edited",
        "artifact_id": row.id,
        "mode": mode,
        "title": row.title,
        "note": "The updated artifact is visible on the user's screen.",
    }


def build_registry() -> list[Tool]:
    """FR-45's v1 inventory. Static per process — identity and turn travel
    in ToolContext at call time, not baked into handlers."""
    return [
        Tool(
            name="create_artifact",
            description=(
                "Write up an artifact of the conversation and put it on the "
                "user's screen: a structured summary, a list of action "
                "items, or a cleaned-up version of the user's idea. Use "
                "only when the user asks for a write-up."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short, human-readable title.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(ARTIFACT_KINDS),
                        "description": "What kind of write-up this is.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The artifact body. Plain text with simple "
                            "structure; read on screen, not spoken."
                        ),
                    },
                },
                "required": ["title", "kind", "content"],
            },
            handler=_create_artifact,
            is_write=True,
        ),
        Tool(
            name="list_artifacts",
            description=(
                "List the user's recent artifacts (including past "
                "sessions): id, title, kind, timestamps. Use when the user "
                "refers to an earlier write-up, to find its id."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_list_artifacts,
            is_write=False,
        ),
        Tool(
            name="read_artifact",
            description=(
                "Read the full content of one of the user's artifacts by "
                "id (long artifacts are truncated)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "integer",
                        "description": "The artifact's id (from list_artifacts).",
                    }
                },
                "required": ["artifact_id"],
            },
            handler=_read_artifact,
            is_write=False,
        ),
        Tool(
            name="edit_artifact",
            description=(
                "Edit one of the user's artifacts: mode 'replace' rewrites "
                "the whole content (read it first), mode 'append' adds to "
                "the end. Optionally retitle. Use when the user asks to "
                "change, fix, or extend an existing write-up."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "integer",
                        "description": "The artifact's id (from list_artifacts).",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "append"],
                        "description": (
                            "replace = rewrite the whole content; append = "
                            "add to the end."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The new content (or the text to append).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional new title.",
                    },
                },
                "required": ["artifact_id", "mode", "content"],
            },
            handler=_edit_artifact,
            is_write=True,
        ),
    ]


def registry_schemas(registry: list[Tool]) -> list[dict]:
    """The declarations agent/providers.py translates for the LLM call."""
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in registry
    ]
