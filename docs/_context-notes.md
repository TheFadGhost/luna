# What happens to Luna as a conversation gets long

Working notes for the `luna-context-durability` branch. Everything here was
measured on this machine (Yoga Slim 7, 8 GB) on 2026-09-10 against
`gpt-5.6-luna` on the live Codex backend, with `codex-cli 0.151.0`. Nothing in
this file is estimated. Where a number contradicts an assumption the branch
started with, the number is kept and the assumption is written down as wrong.

The question being answered is **what degrades, and when**, in a daemon that is
meant to run for months without the user managing it.

## 1. The wall, and where it actually is

`gpt-5.6-luna` in `~/.codex/models_cache.json`:

| field | value |
|---|---|
| `context_window` | 272,000 |
| `max_context_window` | 872,000 |
| `effective_context_window_percent` | 95 |
| `auto_compact_token_limit` | `null` |
| `truncation_policy` | `{"mode": "tokens", "limit": 10000}` — per tool result, not per conversation |

Three things were measured against it.

**A single message is capped at 1,048,576 characters, and the cap is enforced
before the model is reached.** A 4,048,943-character prompt came back in under
a second, cost nothing, and produced this on stderr with `rc=1` and a single
`thread.started` line on stdout:

```
Error: turn/start: turn/start failed: Input exceeds the maximum length of
1048576 characters. (code -32602), data: {"input_error_code":"input_too_large",
"max_chars":1048576,"actual_chars":4048943}
```

That is the shape `parse_output` reads as a refused resume — non-zero exit,
effectively empty stdout — so the daemon already heals from it by starting a
fresh session. It is not reachable from a Luna prompt in any case: recall is
bounded and tier 1 is capped in the low thousands of characters.

**The advertised 272,000-token window is a soft number.** One `codex exec` with
a 1,000,004-character prompt reported:

```json
{"type":"turn.completed","usage":{"input_tokens":257133,"cached_input_tokens":9984, ...}}
```

and then `codex exec resume` on that same thread, with another 108,728
characters, **answered normally at 283,135 input tokens** — 11,135 tokens past
the advertised window. So the window in the model cache is not the cliff; the
real ceiling is somewhere between there and `max_context_window`, and it was
not worth another dollar to find exactly where.

**When a window does fill, `codex exec` has nothing to do about it.** The model
config carries a whole `token_budget` block — `reminder_threshold_tokens`,
`auto_compact_fallback_prompt`, and an instructions template that promises the
model "when you run out of context, the conversation is automatically
summarized for you... time never runs out" via `notes`, `history` and
`functions.new_context`. **None of those tools exist in `codex exec`.** Asked
directly, on the live backend:

> `get_context_remaining` is unavailable, so I cannot report its exact number.
> Tools called: none named `notes`, `history`, or `new_context`.

So the self-managing context window is an interactive/app-server feature. In
`exec` mode there is no compaction, no checkpoint, and no warning — past the
real wall there is a failed turn and nothing else.

### What this rules out

The failure everyone worries about — **codex silently dropping the front of the
context, so Luna loses her persona and tier-1 memory before anything visible
breaks** — does not happen here, and cannot. The persona and tier-1 block are
passed as `-c developer_instructions=` on *every* invocation, `resume`
included (`agent.py`'s `build_argv`), so they are re-sent rather than
remembered. There is no configuration in which they age out of the front of a
window. That was the crux question and the answer is that this one was already
fine.

## 2. What actually degrades, and it is not the window

`docs/_latency-notes.md` measured history growing ~155 tokens a turn. From a
17,602-token first turn that puts 272,000 about **1,600 turns away**.

`config.SESSION_MAX_TURNS` is **60**. So the turn cap fires roughly
twenty-seven times before the context window is ever in sight, and every time
it does, the daemon threw the conversation away and started a thread that had
never heard of the turn before it.

That is the real, everyday degradation, and it had nothing to do with tokens:

* tier 1 survives a rollover (it is rebuilt into the prefix);
* tier 2 survives it (`recall_block` searches the episode store);
* **the last thing said does not survive it**, because `recall_block` is a
  search and a pronoun matches nothing. The antecedent of "the second one" is
  in the previous turn, not in the query.

Three other paths retire a live session the same way and had the same hole: a
tier-1 write moving the prefix fingerprint, an idle timeout, and a refused
resume (`server.py`'s `_ask_agent` retry). The refused-resume path was the
worst of them — it is the one that arrives with no warning at all, and it
rebuilt the message from nothing.

Nothing in the repo detected or handled any of this before this branch. There
was no token counting anywhere, no context-length error classification, and no
use of the `usage` block the adapter has always parsed off `turn.completed`.

## 3. What was built

**The bridge.** `Memory.bridge_block()` renders the last few exchanges
verbatim — recency as the relevance criterion, which is the one place in the
recall path where that is the right rule. `SessionManager` now marks a session
that *replaced a live one* with the reason it exists (`Session.carry`), and the
daemon puts the bridge in the **user message** on exactly that turn, with an
instruction not to remark on it. It is deliberately not in the system prompt:
that is the cacheable prefix, and a block that changes on one turn in sixty
would invalidate it for all of them (ARCHITECTURE.md §4).

An **idle** retirement carries nothing on purpose. Half an hour of silence is a
new conversation, and dragging the last one into it is worse than starting
clean.

One known limitation, left in: the episode store has a `surface` column but no
*conversation* column, so "the last few exchanges" is the last few across all
conversations. Two named conversations rolling over in the same minute could
cross-contaminate by one block. Fixing it is a schema migration on the user's
real store; the symptom is one stale-sounding line in a block she is told not
to remark on. Not worth it yet.

**A token budget.** `config.SESSION_TOKEN_BUDGET = 240_000`, compared against
the `input_tokens` codex already reports. It is a backstop, not a policy: a
*conversational* turn will never reach it, but a tool-using turn can add
thousands of tokens at a time, and sixty of those can. It costs one integer
comparison and it converts the one failure nobody handles into an ordinary
rollover. It is not a settings key — like `SESSION_IDLE_S` and
`SESSION_MAX_TURNS` beside it, it is a property of the agent's limits rather
than a preference.

**The turn cap was left alone.** `_latency-notes.md` §3 says a cap costs the
~900 ms resume buys and should not exist; the code has had one at 60 the whole
time. Both are now true and neither matters, because the rollover it causes is
no longer lossy. Re-litigating the number would have been a change with a
measurable cost and no measurable benefit.

**Tier-1 eviction.** The caps reject oversized writes rather than truncating,
which is right for a human or an RPC caller. It was wrong for the consolidation
pass, which is the only writer nobody is watching: a full `LUNA.md` meant every
pass proposed four entries, every pass was refused, and Luna silently stopped
learning for the rest of the machine's life — evidence being one log line and
an `over_cap` counter. `Memory.fit_tier1` now evicts the oldest entries until
the proposal fits, writing each one into tier 2 at full salience first, so
nothing is destroyed; it stops being *always* in the prompt, which is the only
thing the cap ever bought. An entry longer than the whole cap is still refused
outright, because that is a genuine refusal and emptying the file would not
help.

Current occupancy, for scale: `LUNA.md` holds 7 entries rendering to 1,436 of
3,000 characters (48%) and `USER.md` holds 3 at 506 of 2,000 (25%). The trap
was a few months away, not today.

## 4. What was deliberately not built

* **No compaction subsystem, no summariser, no second model call at rollover.**
  The consolidation pass already turns episodes into curated memory on a timer;
  a rollover that also summarised would be a second, unbudgeted paid call on
  the latency path, to produce something the episode store already holds.
* **No episode-store GC.** There is none today and it is not needed yet: 84
  episodes in 328 KB after sixteen days. See §6.
* **No new config key**, so nothing is owed to `jarvis-settings`'s
  `ContractTest`.
* **No turn cap change**, for the reason in §3.

## 5. The other slow rot: stale embedding vectors

Found while auditing growth, fixed here because it is the same class of bug as
everything above — silent, permanent, and reported as healthy.

`episode_vectors` stores `model` and `dim` per row, and the schema comment says
why: "change the model and the old rows can be found and re-embedded".
`_sync_worker` honours it and refuses to load another model's vectors, which is
correct. `pending_vector_rows` and `vector_stats` did not — they anti-joined on
`episode_id` alone. So the day the embedding model changes, every already-
embedded episode counts as done, is never re-embedded, and is never loaded into
the search matrix again. Semantic recall quietly loses its entire back
catalogue while `luna status` reports nothing pending.

Both queries now join on `(episode_id, model, dim)`. `store_vectors` already
upserts on `episode_id`, so re-embedding replaces rather than duplicates, and
the existing battery guard on `backfill_vectors` already covers the cost of the
one big catch-up a model change would trigger. The current store has one model
(`all-MiniLM-L6-v2`, 77 of 84 episodes), so this had not bitten yet.

## 6. Long-run growth, measured

Everything with a collector has one that genuinely runs at daemon start, not
only from the CLI — verified by tracing each call chain:

| store | bound | triggered by |
|---|---|---|
| job directories | `[dispatch] job_retention_days` = 14 | `Dispatcher.start_gc()`, a `luna-job-gc` daemon thread on a 6-hour loop, started from `Daemon.__init__` |
| `audit.jsonl` | `[audit] max_mb` = 8, `keep` = 5 | `AuditLog.append()` itself, on the write path |
| `spawned.json` | 200 records, pruned every 32 spawns | `SpawnLedger.record()`, inline |
| `luna.log` | 1 MiB × 5 | stdlib `RotatingFileHandler` |
| `voice-router.log` | 256 KiB × 1 | `bin/luna-voice-router`, per write |
| **tier-2 episodes** | **none** | **nothing** |

The episode store is the only tier with no collector at all. At the observed
rate — 84 episodes over 16.3 days, ~4 KB each including the embedding vector —
it reaches single-digit megabytes in a decade. Written down as a known gap
rather than fixed: a GC that first fires in ten years is a GC nobody has ever
tested, and deleting a user's memory is not a thing to build speculatively.

## 7. Gotchas found on the way

* **`codex exec resume` without `-m` silently changes model.** Resuming a
  thread recorded under `gpt-5.6-luna` without passing the model again ran it
  on `gpt-5.6-sol`, reported `cached_input_tokens: 0` — the entire prefix cache
  lost — and said so only as an `item.completed` of `type: "error"`, which is
  *not* a turn failure and would be invisible to anything watching exit codes.
  Luna is safe here (`build_argv` passes `-m` on the resume path, defaulting to
  `config.CODEX_ASK_MODEL`), but the failure is silent, cache-destroying, and
  one missing default away.
* **`codex exec` blocks forever waiting on stdin** if stdin is a pipe that is
  never closed, even when a prompt was given as an argument. It prints
  `Reading additional input from stdin...` to stderr and nothing else. Any
  probe of it needs `< /dev/null`.
* **`lunad/memory.py` keeps no logger**, deliberately. `fit_tier1` therefore
  returns `lost` — evictions whose tier-2 write failed — rather than logging
  them, and `consolidate.py` is what says so out loud. A silent eviction would
  have recreated the bug this branch exists to fix.
