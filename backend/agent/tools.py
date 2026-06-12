"""LLM tools (SDD §2.5). FR-12: create_artifact.

The handler writes an artifacts row, emits an `artifact.created` server
message over the data channel (picked up by the frontend ArtifactsPanel),
and returns a result so the agent confirms verbally.
"""

from datetime import datetime, timezone

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.llm_service import FunctionCallParams

from db.engine import DEFAULT_USER_ID
from db.models import Artifact

ARTIFACT_KINDS = ("summary", "action_items", "cleaned_idea")

CREATE_ARTIFACT_SCHEMA = FunctionSchema(
    name="create_artifact",
    description=(
        "Write up an artifact of the conversation and put it on the user's "
        "screen: a structured summary, a list of action items, or a cleaned-up "
        "version of the user's idea. Use only when the user asks for a write-up."
    ),
    properties={
        "title": {
            "type": "string",
            "description": "Short, human-readable title for the artifact.",
        },
        "kind": {
            "type": "string",
            "enum": list(ARTIFACT_KINDS),
            "description": "What kind of write-up this is.",
        },
        "content": {
            "type": "string",
            "description": (
                "The artifact body. Plain text with simple structure; this is "
                "read on screen, not spoken."
            ),
        },
    },
    required=["title", "kind", "content"],
)


def tool_schemas() -> ToolsSchema:
    return ToolsSchema(standard_tools=[CREATE_ARTIFACT_SCHEMA])


def make_create_artifact_handler(session_id: str, db_sessions):
    """Build the per-session create_artifact handler (registered on the LLM)."""
    log = logger.bind(session_id=session_id, component="agent.tools")

    async def create_artifact(params: FunctionCallParams):
        title = str(params.arguments.get("title", "")).strip() or "Untitled"
        kind = params.arguments.get("kind", "summary")
        if kind not in ARTIFACT_KINDS:
            kind = "summary"
        content = str(params.arguments.get("content", "")).strip()
        if not content:
            await params.result_callback(
                {"status": "error", "error": "content is required"}
            )
            return

        created_at = datetime.now(timezone.utc)
        row = Artifact(
            session_id=session_id,
            user_id=DEFAULT_USER_ID,
            kind=kind,
            title=title,
            content=content,
        )
        try:
            async with db_sessions() as db:
                db.add(row)
                await db.commit()
                await db.refresh(row)
        except Exception as e:
            log.bind(event="tool.create_artifact_failed").error(
                f"artifact save failed: {e}"
            )
            await params.result_callback(
                {"status": "error", "error": "the artifact could not be saved"}
            )
            return

        await params.llm.push_frame(
            RTVIServerMessageFrame(
                data={
                    "type": "artifact.created",
                    "artifact": {
                        "id": row.id,
                        "title": title,
                        "kind": kind,
                        "content": content,
                        "created_at": created_at.isoformat(),
                    },
                }
            )
        )
        log.bind(event="tool.invoked", tool="create_artifact", kind=kind).info(
            f"artifact saved: {title!r}"
        )
        await params.result_callback(
            {
                "status": "created",
                "title": title,
                "note": "Artifact is now visible on the user's screen.",
            }
        )

    return create_artifact
