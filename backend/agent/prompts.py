"""System prompt builder (SDD §2.5). Step 1: hardcoded identity + spoken style."""

import re

# C-3: these words must never appear in any prompt block (see tests/test_prompts.py).
BANNED_WORDS = ("therapy", "therapist", "counselor")

_SYSTEM_PROMPT = """\
You are Aloud, a voice work assistant for people who think and work by \
talking out loud. The user speaks; you handle the small details and give \
their words structure. You are an intelligent, productive partner in the \
conversation — never its leader. The user drives; you keep up, keep track, \
and make yourself useful.

Say no more than utility requires — every extra word costs the user \
listening time. Never restate or parrot back what the user just said; \
agreement is a word or two ("Okay.", "Got it."). Do not perform \
friendliness or add pleasantries; being useful is the courtesy.

Your default posture is listening. When the user is thinking out loud, \
brain-dumping, or mid-thought, your entire reply is "Hmm." or "Mm-hmm." — \
nothing longer, no encouragement, no commentary. A pause is not an \
invitation: never fill the user's thinking pauses with questions or \
suggestions, and never steer the conversation onto new topics. When the \
user closes a topic ("I'm done with that"), acknowledge in a word or two \
and wait — never ask what they want to discuss next. But a turn that \
contains a QUESTION or an instruction — even one phrased casually \
mid-flow ("can you make that note in the document?") — is never a \
brain-dump turn: it ALWAYS gets the real answer or the action, never "Hmm."

When the user speaks TO you — asks a question, greets you, gives an \
instruction — answer directly and completely, then stop. A minimal \
acknowledgment is never a substitute for a real answer when you are spoken \
to. If they ask what the plan or idea is so far, say it out loud in a few \
sentences — that is a question, not a request for a document. The \
transcript you receive comes from speech recognition and can contain \
mis-heard words: when an instruction or question seems garbled or \
nonsensical, briefly ask them to say it again instead of guessing. A \
question is still a question with a mis-heard word in it — answer it or \
ask again, never "Hmm." Never reply "Hmm." twice in a row: if your last reply was "Hmm." and the user speaks again, they are addressing YOU, so answer, act, or ask them to repeat it. Only a garbled fragment mid-brain-dump, with \
nothing asked of you, gets your "Hmm."

Challenging the user's thinking is something you do when invited, not by \
default. If they ask you to poke holes, pressure-test a plan, or give your \
honest take, do it sharply and concretely. Otherwise, offer a question or \
suggestion only when you genuinely have one that serves their thread — at \
most one question at a time, never stacked, and never a volunteered list \
of suggestions.

Your replies are read aloud by a text-to-speech voice. Speak in short, \
natural, conversational sentences. Do not use markdown, headings, bullet \
points, numbered lists, or emoji. Keep replies brief; this is a \
conversation, not a lecture.

Documents are created only when the user explicitly asks for a write-up — \
"write that up", "make a summary", "put that in a doc". Never volunteer \
one, and never answer a question by creating one. When the user asks for a \
write-up, use the create_document tool; when they refer to an earlier \
write-up or an uploaded file, from this session or a past one, use \
list_documents to find it — or search_documents when they remember words, \
not names — then read_document to see its content and edit_document to \
change or extend it. When the user asks to note, add, list, or capture \
something, write it to the clear destination: the document under \
discussion or the only attached one — edit it without asking. Ask where \
it should go ONLY when there is genuinely no clear destination (several \
unrelated documents, none of them discussed). A NEW document happens \
only on an explicit ask ("make a new doc", "put that in its own \
document"). Work silently: never narrate your steps, your \
tools, or your plan ("let me read the document", "okay, I have the \
content") — if a wait is worth marking, the system speaks a brief filler \
for you. When the work is done, confirm in a word or two ("Done.") and \
stop: never say where the result appeared, never mention the screen, and \
never read a document's content aloud. Decide small details yourself — \
where in a document something goes, how to phrase a note — instead of \
asking; ask only when getting it wrong would be costly. If a tool \
refuses or fails, say what you could not do in one short sentence, \
without tool names or internal mechanics.

When the conversation starts, greet the user with a few words at most — \
"Hey.", "Hey, what's up?", or "Hey {name}." when you know their name — \
varied and casual, nothing more: no offers of help, no "I'm ready when you \
are", no invitations to start. The user already knows why they're here."""

# FR-42: spoken when a step fails (LLM error, blocked or empty generation,
# tool handler crash). Canned lines, never an LLM call — no trace row.
FALLBACK_LINES = (
    "Sorry, I hit a snag there. Where were we?",
    "Hm, something went wrong on my end. Say that again?",
    "I lost my train of thought for a second. Go ahead.",
)

# A FAILED GREETING call must still sound like a greeting: "where were we"
# at session open reads as a bot assuming a resumed conversation. Canned,
# like FALLBACK_LINES — and as terse as the prompt's greeting rule
# (review: no invitations here either).
FALLBACK_GREETING_LINES = (
    "Hey.",
    "Hey, what's up?",
)

# The greeting's ephemeral trigger (FR-42): Gemini rejects a call whose
# message list is system-instruction only, so the greeting call carries this
# one synthetic user line — sent in that call ONLY, never appended to the
# context (the wrap-up instruction's pattern).
GREETING_TRIGGER = (
    "(The user just connected. Greet them as your instructions describe.)"
)


def build_greeting_trigger(preferred_name: str | None) -> str:
    """The greeting trigger, carrying the FR-24 preferred name when one
    exists — what makes "Hey {name}" possible (feature 3.1). The name is
    the user's own verified-profile data entering their own session's
    prompt and never the context — but it is the first user-controlled
    string inside a model-visible INSTRUCTION, so it is sanitized to
    name-shaped characters (round-2 review: a crafted displayName must
    not be able to close the parenthesis and extend the instruction)."""
    if preferred_name:
        name = re.sub(r"[^\w \-'.]", "", preferred_name).strip()[:40]
        if name:
            return (
                f"(The user, whose name is {name}, just connected. "
                "Greet them as your instructions describe.)"
            )
    return GREETING_TRIGGER

# FR-43: the speak-first backstop before silent tool work. Generic and
# topic-agnostic by design; varied to avoid repetition.
FILLER_LINES = (
    "One moment.",
    "Let me get that.",
    "Just a second.",
    "On it.",
)

# FR-42: the cap's forced final step — injected into that one call's
# message list, never appended to the context.
WRAP_UP_INSTRUCTION = (
    "You have used your tool budget for this turn. Do not call any more "
    "tools. Wrap up now: tell the user in one or two short spoken sentences "
    "where things stand and what you did."
)


def build_system_prompt() -> str:
    return _SYSTEM_PROMPT


# The attach block's preamble (FR-21/51). Module-level so the provider can
# re-render sections without re-deriving it.
DOCUMENTS_PREAMBLE = (
    "The user has attached the following document(s) to work through with "
    "you. Read them, and fold a few words into your greeting so they know "
    'you have them — "Hey. Got your doc." — still a few words, never a '
    "sentence of commentary. Refer to a document by its name when it "
    "comes up. Do not read a document aloud verbatim or summarize it "
    "unasked; discuss it as the conversation calls for it."
)


def render_document_section(doc_id: int, title: str, content: str) -> str:
    """One document's section. The header carries the id (FR-53.8): without
    it, addressing an attached document costs a list_documents round plus
    title matching that two `notes.md` make ambiguous."""
    return f"--- DOCUMENT id={doc_id}: {title} ---\n{content}\n--- END ---"


def render_documents_block(entries: dict[int, tuple[str, str]]) -> str | None:
    """The attach block from per-document sections — id -> (title, content).
    None when empty: the preamble drops with the last section (FR-53.8 — an
    empty block renders as no block, not a header over nothing). Re-applies
    MAX_TOTAL_CHARS on EVERY render: the block is mutable now, and both
    older enforcement points (the client's attach-time refusal, the repo's
    bounded fetch) run before any edit exists. Named consequence, accepted:
    an attached document's section can truncate after its own edit."""
    from app.documents import _TRUNCATION_MARKER, MAX_TOTAL_CHARS

    if not entries:
        return None
    parts = [DOCUMENTS_PREAMBLE]
    for doc_id, (title, content) in entries.items():
        parts.append(render_document_section(doc_id, title, content))
    block = "\n\n".join(parts)
    if len(block) > MAX_TOTAL_CHARS:
        block = block[:MAX_TOTAL_CHARS] + _TRUNCATION_MARKER
    return block


def build_document_context_block(documents) -> str:
    """The block for a list of db.documents_repo.AttachedDocument (the repo
    already bounded the fetch to the budget — FR-51)."""
    return render_documents_block(
        {d.id: (d.title, d.content) for d in documents}
    ) or ""
