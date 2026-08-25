"""Assembly of Luna's system prompt.

The prompt has three parts, in a fixed order chosen so the cacheable prefix is
as long as possible:

1. the persona spec (``data/persona.md``) — identical for every request;
2. the frozen tier-1 memory block — changes only when memory is written;
3. tier-2 recall for this specific prompt — changes every request.

Anything volatile therefore sits at the end, where invalidating it costs the
least.
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
    recall_block: str = "",
    spec: str | None = None,
) -> str:
    """Compose the full system prompt from persona + memory."""
    parts = [_PREAMBLE, "## Luna — persona specification\n", spec or load_spec()]
    parts.append("## Curated memory (tier 1, always loaded)\n\n" + tier1_block.strip())
    if recall_block.strip():
        parts.append("## Recalled context (tier 2, retrieved for this message)\n\n"
                     + recall_block.strip())
    parts.append(_CLOSING)
    return "\n\n".join(p.strip() for p in parts if p.strip()) + "\n"
