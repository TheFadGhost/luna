"""Assembly of Luna's system prompt.

The system prompt holds only what is stable:

1. the persona spec (``data/persona.md``) — identical for every request;
2. the frozen tier-1 memory block — changes only when memory is written.

Tier-2 recall used to sit at the end of the system prompt. It no longer does.
Recall is chosen per request, so it changed the prefix on every turn, and once
Phase 1 started resuming one conversation instead of spawning a fresh process
per ask (see ``session.py``) that made the prompt cache useless: the prefix
diverged at the recall block every time. Recall now rides in the *user* message
where it belongs — it is context for this question, not for who Luna is.

So: system prompt = the part that must stay byte-identical between turns; user
message = the part that is allowed to change.
"""

from __future__ import annotations

from pathlib import Path

from . import config

# Framing that turns a general coding agent into Luna. Kept here rather than in
# persona.md because persona.md is the *spec* — a document the user edits about
# who Luna is — while this is the harness that makes a particular backend obey
# it. The two have different authors and different reasons to change.
_PREAMBLE = """\
You are Luna. This is not a role you are playing on top of another assistant:
for the whole of this exchange, Luna is who you are. You are the user's
resident assistant on their Omarchy Linux desktop, reached through a daemon
called lunad. You are not a general coding assistant, you are not Claude Code,
and you do not describe yourself as either. If asked what you are, answer as
Luna.

The specification below is binding. Where it conflicts with your default
manner — particularly its ban on opening with agreement or praise — the
specification wins.
"""

_CLOSING = """\
Operating notes for this exchange:

- You are running headless with no tools. You cannot read files, run commands,
  or inspect the machine right now. If answering properly needs that, say what
  you would need rather than guessing or pretending you looked.
- Reply in plain prose for a terminal. No markdown headings, no bullet lists
  unless the answer genuinely is a list. Short.
- The memory below is yours. Treat it as things you know, not as a document
  someone handed you. If something in it looks wrong, say so.
"""


def load_spec(path: Path = config.PERSONA_PATH) -> str:
    """Read the persona spec. A missing spec is fatal: Luna without her spec is
    just a coding agent with a name, which is worse than nothing."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"persona spec missing at {path}. Luna will not run without it."
        ) from exc


def build_system_prompt(
    tier1_block: str,
    spec: str | None = None,
) -> str:
    """Compose the cacheable prefix: persona + tier-1 memory. Nothing volatile."""
    parts = [_PREAMBLE, "## Luna — persona specification\n", spec or load_spec()]
    parts.append("## Curated memory (tier 1, always loaded)\n\n" + tier1_block.strip())
    parts.append(_CLOSING)
    return "\n\n".join(p.strip() for p in parts if p.strip()) + "\n"


def build_user_message(prompt: str, recall_block: str = "",
                       surface: str = "cli") -> str:
    """Wrap the user's message with anything retrieved for *this* turn.

    Recall goes first and the question last, so the model reads the context
    before the request rather than having to hold the request in mind while it
    reads. The surface is named because a spoken answer has to be shorter than
    a typed one and Luna has no other way to know which she is giving.
    """
    parts: list[str] = []
    if recall_block.strip():
        parts.append("## Recalled context (tier 2, retrieved for this message)\n\n"
                     + recall_block.strip())
    if surface == "voice":
        parts.append(
            "This message was spoken aloud and your answer will be read back "
            "by a speech synthesiser. Answer in at most two short sentences, "
            "in plain words. No lists, no code, no file paths, no URLs: if the "
            "answer needs any of those, say the one-line version and note that "
            "the detail is on screen."
        )
    parts.append(prompt.strip())
    return "\n\n".join(parts)
