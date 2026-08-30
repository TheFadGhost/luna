# Handoff — semantic recall (`lunad/embed.py`)

Written by the agent that built semantic recall, which was scoped to
`lunad/embed.py`, `lunad/memory.py`, `tests/`, `README.md`'s attribution and
`docs/ARCHITECTURE.md` §4. Everything below is a change **outside** that scope
that someone who owns those files should apply. Nothing here is required for
the feature to work — it works today without any of it.

## 1. `lunad/settings.py` — register `[memory] semantic_recall`

`Embedder.enabled()` already reads `settings.get("memory.semantic_recall")` and
treats `None` as `True`. Unknown keys return `None` from `Settings.get`, so the
switch is a documented no-op until the schema knows about it. Add to the
`memory` section's key list, beside `decay_half_life_days`:

```python
Key("semantic_recall", bool, True,
    "Search episodes by meaning as well as by keyword. Needs the embedding "
    "model: `python3 -m lunad.embed fetch`. Off falls back to keyword search "
    "with no other change in behaviour."),
```

(Match the surrounding `Key(...)` constructor exactly — the shape above is from
memory of the file, not read from it.) Nothing else needs to change: the
setting is read late, on every call, so a GUI toggle takes effect on the next
question without a restart.

## 2. `bin/luna` — surface the three commands

They exist and are documented as `python3 -m lunad.embed <cmd>`, which is fine
but is not how anything else on this machine is spelled. If `bin/luna` grows a
`memory` subcommand group, these belong in it:

| today | suggested |
|---|---|
| `python3 -m lunad.embed status` | `luna memory embed status` |
| `python3 -m lunad.embed fetch` | `luna memory embed fetch` |
| `python3 -m lunad.embed backfill [--force]` | `luna memory embed backfill` |

`lunad.embed.cli(argv)` takes an argv list and returns an exit code, so the
wiring is one call. `fetch` prints progress to stdout and errors to stderr.

## 3. `lunad/config.py` — nothing, deliberately

Every constant the feature owns is in `lunad/embed.py` instead, per the brief.
If they are ever moved, the two that other code would care about are
`models_dir()` (derived from `config.STATE_DIR`, read late so tests can
redirect it) and `IDLE_UNLOAD_SECONDS`, which is deliberately the same 300 s
speech uses.

## 4. `lunad/server.py` — an optional `memory.status` field

`EpisodeStore.stats()` now also reports `vectors` and `vectors_pending`, so
whatever renders tier-2 status gets them for free. `Episode.to_dict()` gained
`coverage` and `similarity`, which is what makes a `mem search` result explain
*why* something surfaced. Both are additive; nothing needs to change.

## 5. Known rough edge, not fixed

On the real database, "what terminal do I use" injects episode 5 — four
hundred repetitions of the word "word" — at coverage 0.50, because it OR-matched
one of two query tokens. That is pre-existing lexical behaviour, unaffected by
this pass (its cosine is 0.00). It is the strongest argument for eventually
requiring *some* semantic agreement before an OR-widened hit is injected, but
that would tighten recall for everyone and wants its own measurement.
