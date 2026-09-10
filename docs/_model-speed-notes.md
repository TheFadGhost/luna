# How fast the model turn can be made, and what it is actually made of

Working notes for the `luna-model-speed` branch. Everything here was measured
on this machine (Yoga Slim 7, 8 GB) on 2026-09-10 against the live Codex
backend through `codex exec`, codex-cli **0.151.0**. Nothing is estimated.

`docs/_latency-notes.md` covers everything *around* the model call and takes it
from ~6 s to ~2.9 s of floor. This file is about the floor itself: the
2.4–3.6 s that file calls "the model turn". The headline is a negative
result, and the reason is in §1.

## 1. What the model turn is made of

Two prompts at a fixed `low` effort, three runs each, differing only in how
much output they ask for. `thread.started` → `turn.completed`:

| prompt | output tokens | turn, ms (3 runs) | median |
|---|---|---|---|
| "reply with exactly the two words: all good" | 6 | 3231, 2843, 2683 | **2,843** |
| "reply with the numbers one to sixty, spelled as words" | 171 | 5821, 5398, 5518 | **5,518** |

Two points, one line:

* **~16.2 ms per output token** (≈62 tokens/s)
* **~2,746 ms of time-to-first-token** — request setup, queueing and prefill,
  before a single token of the answer exists.

A typical spoken answer is 40–45 output tokens. So a typical model turn is
**2,746 ms of waiting and ~650 ms of writing**. Roughly **80% of the turn
happens before the first token**, and nothing on Luna's side of `codex exec`
touches it. That single ratio explains every other result in this file.

## 2. Reasoning effort — it works, and it is not the lever

`model_reasoning_effort` is a real codex config key. It is not documented in
`codex exec --help`; it was found in the binary's own config-key table and
then confirmed by measuring it. `gpt-5.6-luna` advertises
`low, medium, high, xhigh, max` with a default of **medium**
(`~/.codex/models_cache.json`, `default_reasoning_level`).

It plainly does something. Same trivial question, one run each on a quiet
backend:

| `-c model_reasoning_effort=` | `agent_message` at |
|---|---|
| *(omitted — the model's default)* | 3,530 ms |
| `low` | 3,524 ms |
| `high` | 5,677 ms |
| `max` | 6,741 ms |

So it was worth a real experiment. **60 live asks**: five questions covering
the mix she actually gets, three arms, four blocks, interleaved with the *arm
varying innermost* so no arm gets the good half of the backend's day.

The five questions:

| id | asked |
|---|---|
| `trivial` | "what's the boiling point of water?" |
| `chat` | "remind me what you actually are, in one line" |
| `pushback` | "I need my whole website rebuilt by tomorrow morning. Can you do that?" |
| `tool` | "how much free disk space have I got?" (really runs a command) |
| `judgement` | "should I use SQLite or Postgres for a desktop notes app?" |

Paired within each (question, block), so backend drift cancels:

| arm | turn vs default, median | output tokens vs default |
|---|---|---|
| `low` | **+139 ms** (n=20) | −2 |
| `high` | **+393 ms** (n=20) | +18 |

And unpaired, all 20 runs per arm:

| arm | turn median | p25 | p75 | output tokens | reply chars |
|---|---|---|---|---|---|
| `low` | 4,220 ms | 3,630 | 5,878 | 41 | 94 |
| default (medium) | 4,440 ms | 3,084 | 6,141 | 44 | 92 |
| `high` | 5,312 ms | 3,565 | 6,979 | 94 | 108 |

**`low` is not faster.** At the median it was 139 ms *slower* than doing
nothing, which is noise — the paired differences ranged from −2,590 ms to
+5,246 ms. The reason is visible in the usage counters: at the default effort
she already spends **0 reasoning tokens** on a conversational question. There
is no thinking to switch off. Effort only starts costing time when the
question is hard enough to make her use it, and her questions mostly are not.

Note also how wide p25→p75 is — **the backend's own variance (2.7 s to 14 s on
the same question) is several times larger than any effort setting's effect.**
Any claim about the model turn that rests on fewer than a dozen interleaved
runs is measuring the weather.

### Quality at each level

The persona is the point of the project, so the answers matter more than the
milliseconds. She is meant to interrogate, price work and push back. At every
level she does:

> **`low`** — "Not responsibly without seeing the site and knowing what must
> stay; a whole rebuild by tomorrow is likely to produce a rushed result. I can
> first assess it and give you a realistic overnight plan, but I need the
> website or its address."
>
> **default** — "A whole website rebuilt overnight is unlikely to be reliable;
> I can help with a focused redesign or working prototype by morning. What is
> the single most important page or outcome?"
>
> **`high`** — "A full rebuild by tomorrow morning is only realistic as a
> focused first pass, not a polished whole site. What is the website, and which
> pages must be ready first?"

All three refuse the premise and ask the scoping question back. On the
`tool` question all three returned the *same sentence* — "You have about 61
gigabytes of free disk space" — off the same command, at 78 / 96 / 144 output
tokens. `low` is not measurably worse at anything she is actually asked.

**So `low` does not lobotomise her. It also does not speed her up.** Both
halves of that are why the default did not move.

### What `high` really costs

`high` roughly **doubles the length of the answer** (94 output tokens against
44). On the voice path that is the expensive half: remote TTS runs at ~17 ms
per character (`_latency-notes.md` §4), so a longer answer costs far more in
speech than in generation. Raising effort is a worse trade than it looks.

## 3. Is there a faster slug? No.

`gpt-5.6-terra` sits in `models_cache.json` beside luna and sol —
"Balanced agentic coding model for everyday work", default effort `medium`.
24 asks, three questions, four arms, two blocks, interleaved:

| arm | turn vs luna-default, median | turn median | output tokens |
|---|---|---|---|
| `gpt-5.6-luna` `low` | −210 ms | 4,229 ms | 40 |
| `gpt-5.6-luna` default | 0 | 4,352 ms | 72 |
| `gpt-5.6-terra` `low` | −320 ms | 4,349 ms | 32 |
| `gpt-5.6-sol` `low` | **+1,355 ms** | 5,478 ms | 38 |

Terra is indistinguishable from Luna (−320 ms at n=6, against a noise floor
several times that). **Sol is materially slower** — which is worth knowing on
its own, because sol is the dispatch model, and it confirms that the coding
workhorse is not a shortcut for conversation. Terra's answers held the persona
as well as Luna's did; it just is not faster. **Nothing here justifies moving
her default slug.**

## 4. Streaming — the deltas exist, but not on this path

She speaks at `turn.completed`. The question was whether `codex exec --json`
emits partial text that could start TTS on the first clause instead.

**It does not.** The complete event stream from a real run, every type seen
across 84 live asks including ones that ran commands:

```
thread.started → turn.started → [item.started → item.completed/command_execution]* 
                              → item.completed/agent_message → turn.completed
```

`item.started` *is* forwarded, for command executions. Agent messages arrive
whole, once, at `item.completed`. There is no content delta anywhere in the
`exec --json` stream.

The events do exist in codex. `codex app-server generate-json-schema --out DIR`
lists `AgentMessageDeltaNotification`, `ReasoningTextDeltaNotification` and
friends — the **app-server** protocol streams, and the TUI is what consumes it.
`codex exec --json` is a different, coarser projection of the same events.

### What it would buy, if it were taken

From §1: 16.2 ms per output token. A typical 40-token answer is ~650 ms of
generation, and the first clause is perhaps 15 tokens in. So speaking at the
first clause instead of at `turn.completed` is worth **roughly 400 ms, with a
hard ceiling of ~650 ms** on a typical answer. It is not the 2–3 s it feels
like it should be, because 2,746 ms of the turn happens before any token
exists.

Reaching it means driving `codex app-server` (JSON-RPC over stdio, marked
`[experimental]`) instead of `codex exec`, which is a rewrite of the adapter's
whole spawn-and-parse path. The same rewrite would also make the codex process
**resident**, which removes the process start measured at a median of **253 ms**
(p25 232, p75 268, n=60) — so the honest figure for the whole exercise is
**~650–900 ms**, for an experimental protocol and a new class of failure.

And it makes the existing correctness argument worse, not better. `_latency-notes.md`
fires at `turn.completed` and explicitly not at `agent_message`, because a turn
can complete more than one message and can still fail after one. Streaming
would have her speak half of a message that might yet be retracted — the same
risk, taken earlier and with less information. **Not taken. Recorded here so it
is a decision rather than an oversight.**

## 5. Everything else that was tried

| tried | measured | verdict |
|---|---|---|
| `-c service_tier=priority` — the "Fast, 1.5x speed" tier every slug advertises in `models_cache.json` | 4,091 ms against a 3,530 ms baseline on the same question | Accepted without error and does nothing. Not entitled on this subscription; it fails silently rather than loudly. Not a lever. |
| `-c model_verbosity=low` | not run | `default_verbosity` for all three 5.6 slugs is **already `low`**. Nothing to win. |
| an unsupported effort value (`ultra`, which luna does not advertise) | ran to a clean answer, rc 0 | **codex does not reject a level the model does not list.** A wrong value here fails *silently*. This is why `settings.Key` validates the choices before they reach argv. |
| codex process start | median 253 ms, p25 232, p75 268, max 367 (n=60) | Tight and not removable under `codex exec`. Only the app-server daemon in §4 touches it. |
| the very first run after a long idle | one observation: `thread.started` at **1,014 ms** against a 253 ms steady state | Unconfirmed — it was a single cold run and the page cache could not be dropped safely on a shared machine. If it reproduces, prewarming the codex binary is worth ~750 ms on the first ask after idle. **Left as a lead, not a claim.** |
| prompt size | see `_latency-notes.md` §3 | Already settled: cutting 4,000 persona tokens measured *slower*. §1 explains why — the prefix is cached and codex's own base context is 13,512 tokens. |
| routing easy questions to a cheaper setting | — | There is nothing to route *to*. §2 shows `low` is not faster, so a classifier would spend its own latency and its own mistakes to buy a measured **+139 ms**. A wrong route that makes her dumb costs far more than that. **Rejected on the numbers, not on taste.** |

## 6. What shipped

One key, `[assistant] reasoning_effort`, default `"auto"` — which sends no
override and leaves the model on its own default, exactly as before. **The
measured behaviour of a stock Luna is unchanged.**

It ships because the control turned out to be real and undocumented, and
because `high`/`xhigh`/`max` are a legitimate thing to want on a slow, hard
question even though they are the wrong default. It is wired the way `model`
is: read from settings per ask in `Daemon._ask_agent`, forwarded to
`CodexAdapter.ask`, and turned into `-c model_reasoning_effort=` in
`build_argv` — read late from `config.CODEX_ASK_REASONING_EFFORT`, never bound
as a signature or class-attribute default.

Two things it deliberately does not do:

* **It never reaches dispatch.** `dispatch_argv` does not take it. A dispatched
  session is a coding agent with nobody waiting on the first spoken syllable,
  and speeding up the conversation must not quietly make the worker that writes
  code think less.
* **claude ignores it.** `ClaudeAdapter.ask` takes `**_`. It is a codex config
  key with no claude equivalent, and inventing one would be worse than dropping it.

## 7. The honest summary

The model turn is **~2,750 ms of time-to-first-token plus ~16 ms per output
token**, and `codex exec` exposes no control over the first number. Reasoning
effort, a different slug and the priority service tier were each measured
properly and none of them moved it. The one real remaining win — streaming the
first clause from a resident `codex app-server` — is worth **~650–900 ms**, and
costs an experimental protocol, an adapter rewrite and a weaker correctness
story than the one `turn.completed` currently gives.

Everything cheap was already taken on `luna-latency`. What is left is not cheap.

## Reproducing

Two short scripts driving the real objects, no fixtures and no mocks: one
timestamps every JSONL line of `codex exec --json` from `Popen` to exit, the
other loops that over an interleaved (question × arm × block) matrix and writes
one JSON record per ask. Both are described by what they do rather than
shipped, in the same spirit as `_latency-notes.md`.
