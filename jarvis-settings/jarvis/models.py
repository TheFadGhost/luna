"""Suggested model slugs for `assistant.model`, per agent.

Not a hard allowlist. `assistant.model` stays free text — a model released
after this file was last touched must not be rejected — but the Assistant
pane offers these as suggestions and flags a value that is not among them,
so a typo (`gpt-5.6-lunar`) is visible on the spot instead of failing much
later at invocation time with no feedback in the app.

Codex's own slugs are read from ~/.codex/models_cache.json at runtime,
because Codex ships new models faster than this file gets edited by hand.
The fallback below is what codex_models() returns when that file is
missing, unreadable, or malformed — kept roughly in step with it, but not
authoritative the way the cache is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CODEX_CACHE_PATH = Path(os.path.expanduser("~/.codex/models_cache.json"))

FALLBACK_CODEX_MODELS: tuple[str, ...] = (
    "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
)

# Anthropic does not publish an equivalent machine-readable cache on this
# machine, so the Claude side is built-in only.
CLAUDE_MODELS: tuple[str, ...] = (
    "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
)


def codex_models(path: Path | None = None) -> tuple[str, ...]:
    """Slugs codex will actually accept, from its own cache.

    Filtered to `visibility: "list"` — the cache also carries internal
    entries (`codex-auto-review`, reserved slugs) nobody should be typing
    into a model field. Falls back to FALLBACK_CODEX_MODELS whenever the
    cache can't be read as the shape this expects; never raises.
    """
    target = CODEX_CACHE_PATH if path is None else path
    try:
        with open(target, "rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return FALLBACK_CODEX_MODELS
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return FALLBACK_CODEX_MODELS
    out = [m.get("slug") for m in models
           if isinstance(m, dict) and m.get("visibility") == "list"
           and isinstance(m.get("slug"), str) and m.get("slug")]
    return tuple(out) if out else FALLBACK_CODEX_MODELS


def suggestions_for(agent: str, path: Path | None = None) -> tuple[str, ...]:
    """The slugs to suggest for `assistant.agent`'s current value."""
    if agent == "codex":
        return codex_models(path)
    if agent == "claude":
        return CLAUDE_MODELS
    return ()
