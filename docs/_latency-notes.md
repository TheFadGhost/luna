# Where the time goes when Luna answers

Working notes for the `luna-latency` branch. Everything here was measured on
this machine (Yoga Slim 7, 8 GB) on 2026-09-09/10 against `gpt-5.6-luna` on the
live Codex backend, unless a row says otherwise. Nothing in this file is
estimated.

The question being answered is **wall-clock from the user finishing their
question to Luna starting to speak** — not throughput, and not time to the full
answer.

## 1. The budget, before

A voice ask with a short factual answer, end to end. The four rows in bold are
the only ones that are not noise.

| stage | ms | how measured |
|---|---|---|
| `luna` CLI process start (typed path only) | 102 | 10 runs of `luna --help`; bare `python3 -c pass` is 13 ms, `+ import lunad.config, render` is 41 ms |
| daemon: `memory.tier1_block()` | 0.04 | median of 7, real store |
| daemon: `memory.recall_block()` (tier 2 + semantic) | 0.43 | median of 7, real store |
| daemon: `context.context_line()` (`hyprctl -j activewindow`) | 8.3 | median of 7 |
| daemon: `persona.build_system_prompt()` | 0.02 | median of 7 |
| daemon: `persona.build_user_message()` | <0.01 | median of 7 |
| daemon: `sessions.fingerprint()` + `acquire()` | 0.02 | median of 7 |
| daemon: `adapter.binary()` + `build_argv()` | 0.03 | median of 7 |
| **everything the daemon does before the spawn** | **~9** | sum of the above |
| **codex process start → `thread.started`** | **250–257** | n=5, timestamped JSONL |
| **model turn → `item.completed`/`agent_message`** | **2,391–3,573** | n=8 |
| `agent_message` → `turn.completed` | 21–37 | n=5 |
| **`turn.completed` → stdout EOF / process exit** | **359–687 (median ~470)** | n=5 — codex writing its session rollout |
| `parse_output` + `episodes.record()` + counters | ~1 | `episodes.record()` median 0.26, max 1.38 |
| `strip_for_speech` + `split_sentences` | 0.09 | median of 50 |
| **TTS first audio (OpenRouter `deepgram/flux-tts:free`)** | **1,047–5,647** | see §4 — it scales with sentence length |

`import lunad` (111 ms) and `Memory()` (15.8 ms) are paid once per daemon start,
not per ask. `luna` the CLI is a thin socket client; the daemon is long-lived,
so no per-ask import cost exists on the daemon side at all.

## 2. What was changed

### Speech is dispatched at `turn.completed`, not at process exit — **−350 to −490 ms**

`codex exec` emits `turn.completed` and then spends a further 359–687 ms writing
its session rollout before it closes stdout and exits. `proc.communicate()`
waited for the exit, so every ask paid that half second after the answer was
already on the wire.

`BaseAdapter._spawn_and_wait` now takes an optional `on_line`, which reads
stdout as it arrives; `CodexAdapter._early_reply_watcher` fires the reply at
`turn.completed`, and `Daemon._ask` speaks from there. Measured live, three
asks:

| | `on_reply` fired | `ask()` returned | saved |
|---|---|---|---|
| "largest ocean" | 3,070 ms | 3,560 ms | **490 ms** |
| "Apollo 11" | 3,426 ms | 3,776 ms | **350 ms** |
| "name an instrument" | 2,391 ms | 2,811 ms | **420 ms** |

And the same thing measured through the *whole* daemon ask path — real
`CodexAdapter`, real memory, real persona, live backend, temp state, speech
timestamped rather than played:

| ask | speech dispatched | `op_ask` returned | head start |
|---|---|---|---|
| "boiling point of water" | 3,197 ms | 3,636 ms | **439 ms** |
| "name a European river" | 2,774 ms | 3,231 ms | **458 ms** |

Deliberately `turn.completed` and not `agent_message`, which lands 21–37 ms
sooner: a turn can complete more than one message and can still fail after one,
so a message is a candidate and only the completed turn makes it the answer.
Those 30 ms are not worth speaking something the model went on to retract.

Everything after the callback is unchanged — the full parse, the return-code
check, the `-o` second witness, and every exception `parse_output` can raise.
When `on_line` is not passed the old `communicate()` path runs byte for byte, so
`ClaudeAdapter` is untouched. If the callback never fires, `_ask` still speaks
the reply the old way; "she answered but said nothing" is the worst failure this
path has and it is guarded on both sides.

### `Speech.prewarm()` overlaps piper's cold load with the model call — **−1,609 ms**, piper only

The piper worker costs 1,467–1,609 ms to load and 212 ms to produce its first
audio frame once loaded. That load happened *after* the model had answered.
Started when the agent subprocess is spawned instead, it finishes inside the
2.4–3.6 s the model spends thinking:

| | ms |
|---|---|
| `_ensure_worker()` cold, inline (today) | 1,609 |
| `_ensure_worker()` after `prewarm()` + 2.4 s of model time | **0** |
| `prewarm()` with `provider = openrouter` | no-op, returns `False` |

Narrow on purpose: it fires only when piper is the *configured provider*, never
when it is merely the configured fallback. See §5.

## 3. Two things that turned out not to be problems

**Prompt size does not drive latency here.** Cutting the system prompt from
16,490 characters to 200 — 17,604 input tokens down to 13,653, a 4,000-token
saving — made the ask *slower*, within noise:

| system prompt | input tokens | `agent_message` at |
|---|---|---|
| full persona (16,490 c) | 17,604 | 2,624 ms |
| tiny persona (200 c) | 13,653 | 2,735 ms |
| full persona, repeat | 17,604 | 2,947 ms |

So the persona is not costing time and must not be trimmed for speed. Note also
that codex's own base context is 13,512 tokens before Luna adds anything — the
persona is about 3,000 tokens on top of that, and the floor is not ours to move.

**Session resume does not degrade as a session grows.** Five turns on one
thread:

| turn | `agent_message` at | input tokens | cached |
|---|---|---|---|
| 1 (fresh) | 3,915 ms | 17,602 | 9,984 |
| 2 (resume) | 2,997 ms | 17,877 | 17,152 |
| 3 (resume) | 2,995 ms | 18,031 | 17,152 |
| 4 (resume) | 3,167 ms | 18,186 | 17,152 |
| 5 (resume) | 2,659 ms | 18,343 | 17,152 |

Resume is worth ~900 ms on turn two (the cached prefix goes from 9,984 to
17,152 tokens) and there is no upward trend after that; history grows about 155
tokens a turn and stays cached. **No turn or size cap is needed**, and adding
one would cost the ~900 ms that resume buys.

## 4. The biggest remaining win, not taken

**Remote TTS latency scales with the length of the first sentence**, and the
first sentence is on the critical path alone:

| sentence | synth | audio produced |
|---|---|---|
| 4 chars | 1,072 ms | 0.88 s |
| 21 chars | 1,047 ms | 1.12 s |
| 61 chars | 2,731 ms | 3.28 s |
| 153 chars | 3,655 ms | 8.16 s |
| 274 chars | 5,647 ms | 14.96 s |

A ~1,050 ms floor, then roughly 17 ms per character. A realistic 60–150
character opening sentence therefore costs **2.7–3.7 s of silence** — more than
the model itself. (The 1,153/1,293 ms figures a `luna say "Testing one."`
reports are the 12-character best case and are not representative.)

Splitting the first sentence at a clause boundary would start the audio at
~1,050 ms instead of ~2,731 ms — about **−1.7 s**, the largest single saving
left. It is not taken here because it does not stand on its own:

- `_play_remote`'s producer runs strictly one request behind playback, so
  chunk 2 would only be requested *after* chunk 1 returned. Chunk 1's audio
  (1.12 s) is shorter than chunk 2's synthesis (~2 s), which would put a ~0.9 s
  gap in the middle of her first sentence — worse than starting late.
- Fixing that means making the first two requests concurrent, which changes the
  "one ahead" rule that exists so a barge-in two words in has not already paid
  for the whole reply.
- And a clause split is a real prosody seam in a voice the user chose.

Concurrent-first-two plus a clause split would give ~1,050 ms first audio with
no gap. That is a voice-quality decision, so it was written down here rather
than taken — and then taken, deliberately, in §4a.

## 4a. The win in §4, taken

Everything §4 predicted held up, so it was built: the opening sentence is cut
at a clause boundary and **both halves are requested at the same time**.
Measured on this machine on 2026-09-10, live against
`deepgram/flux-tts:free`, six runs of each interleaved so that neither mode
gets the good half of the provider's day. Audio was routed to a sink for the
timing runs, so nothing here was heard; the arithmetic is the same either way.

Opening sentence: *"I checked the logs, and the service came back up about ten
minutes ago."* (71 characters, cut 19/51 at the comma.)

| | time to first audio, ms |
|---|---|
| before (one request, whole sentence) | 2165, 2218, 2773, 2906, 3127, 3270 — **median 2,840** |
| after (two concurrent requests) | 1052, 1211, 1236, 1313, 1452, 1621 — **median 1,275** |

**−1,565 ms, median, on every spoken reply whose first sentence is long enough
to split.** A longer opener (94 characters, cut 39/54) went 3,907 → 2,566 ms,
−1,341 ms. Through the real `aplay` rather than a sink, one live utterance
each: 2,315 → 1,400 ms.

**No gap at the seam.** The measure is whether the tail's audio is in hand
before the head's runs out — `t_ready(tail) - (first_audio + duration(head))`,
with the head's duration taken after trimming. Negative is slack:

| | seam slack, ms (negative = no gap) |
|---|---|
| concurrent (shipped) | −263, −255, −860, −280, −531, −907 |
| naive split, one-ahead producer | **+352, +122, +625** |

So the concurrency is not an optimisation on top of the split, it is the thing
that makes the split legal. The serial version puts a 122–625 ms hole in the
middle of her first sentence, exactly as §4 predicted.

**The short case did not regress.** *"It is already running."* (22 characters)
is under the threshold and is never cut, so both columns are the same code
path; the difference is provider noise:

| | ms |
|---|---|
| split disabled | 1159, 1171, 1254, 1354, 1454 — median 1,254 |
| split enabled (never fires) | 1056, 1172, 1384, 1476, 1709 — median 1,384 |

### When it splits, and where

`split_lead_sentence` declines unless all of these hold. Declining is the
common case.

* The first sentence is **at least 50 characters**. At 17 ms/character the most
  any split of a shorter one could save is ~0.5 s, and the head would be so
  short that its audio could not cover the tail. 21 characters already
  synthesises at the 1,047 ms floor (§4), so there is nothing there to win.
* The cut is at a **real clause boundary** — comma, semicolon, colon, or a
  spaced dash — never at a character count. The whitespace lookahead keeps
  "1,234" and "3:15" out; a hyphen only counts when spaced, so "well-known" is
  safe. Nothing in the sentence with a boundary is split at an arbitrary point,
  and a sentence with no boundary is left whole.
* The **tail is at least 24 characters**, or the saving is under 0.4 s.
* The head's audio **covers the tail's synthesis**: `(54 + 17) x head >= 17 x
  tail + 250`, from the two measured slopes in §4 (54 ms/char of speech,
  17 ms/char of synthesis; the fixed floor is the same for both halves and
  cancels). A head covers a tail about 4.2 times its length. A boundary too
  early to cover the tail is *passed over*, not given up on — that is what
  splits "The build is green, the tests all pass, and the branch is ready…" at
  the second comma rather than abandoning it at the first.

### The seam is real, and it is bounded

The provider pads every request: 200–400 ms of near-silence before the first
word, and a decay after the last. Mid-sentence that padding is the seam. Across
nine renderings of three sentences:

| | seam pause, ms |
|---|---|
| untrimmed | 20, 60, 60, 90, 230, 240, 390, 390, 620 — median 230 |
| trimmed (`trim_seam`) | 20, 60, 60, 70, 70, 80, 140, 140, 140 — median 70 |
| the same voice's longest *internal* pause, whole sentence | 80–180 |

So `trim_seam` cuts the facing edges back to 80 ms each, which puts the seam
inside the range of pauses the voice already makes on its own. It only ever
looks at samples under 3% of the chunk's own peak, never cuts more than 400 ms,
and is a no-op on a chunk with nothing to spare — it cannot clip a word. It
costs 3.2 ms per chunk, twice, which is noise against 1,275 ms.

**Honest caveat: nobody has listened to it.** The numbers above say the pause
between the halves is now no longer than the pauses the voice makes inside a
sentence, and shorter than it was in six of nine renderings. They do not say
whether the *intonation* matches across the cut — the head is synthesised as a
complete utterance and gets a terminal contour the whole sentence would not
have had. That is a judgement for ears. `[voice] provider = "piper"` turns the
whole thing off, and deleting the `split_lead_sentence` call in `say()` turns
it off for OpenRouter too.

### What a barge-in costs now

Two requests are in flight only while the opening sentence is out. Timestamped
live, barging in 500 ms after the first audio frame:

```
    3 ms  start  "The build is green, the te…"
    4 ms  start  "and the branch is ready to…"
 2106 ms  done   "and the branch is ready to…"
 2514 ms  done   "The build is green, the te…"
 ~3000 ms cancel() returned True in 52 ms; aplay dead; played_bytes 0
```

Sentence two was never requested, and the tail — which had already arrived —
was never played. The wasted synthesis is the *two halves of one sentence*.
Before this change the request in flight during sentence one was sentence
*two*, so an interruption there wasted two sentences' worth. A barge-in is now
cheaper, not dearer, and it still goes through `safety.terminate` — no new
signal path was added, and the ledger came back empty.

### Piper

Untouched. The split is decided in `say()` behind
`provider == "openrouter"`, so with piper as the *provider* the sentence is
never cut: it synthesises in ~212 ms pre-warmed and a seam there buys nothing.
With piper as the *fallback*, the two halves simply arrive as two of the units
it already streams into one `aplay` — verified live by forcing the remote
provider to fail: one player, one `said`, all three units spoken, 1,609 ms to
first audio (the cold load).

### Measured and not taken

The head chunk arrives with 190–390 ms of silence before the first word, and
the whole-sentence rendering has the same lead-in. Trimming *that* would take
another ~200 ms off time to first audio, but it is the onset of her first word
rather than padding between two of them, and getting it wrong clips her. Left
alone.

## 5. Other things measured and deliberately left alone

| thing | measured | why it was left |
|---|---|---|
| pre-warming piper when `provider = openrouter` | would save 1,467 ms | the remote path fell back to piper 2 times in 77 asks (2.6%); expected saving ~38 ms against 331 MB resident on an 8 GB laptop. Bad trade. |
| pooling the TLS connection to OpenRouter | handshake is 36–47 ms warm, 195 ms with cold DNS | ≤195 ms, on the one path that spends money, for a rewrite of `synthesise()`. Not worth the risk. |
| `context.context_line()` (`hyprctl` subprocess) | 8.3 ms | already bounded at 1 s and fails open. 8 ms is not where the time is. |
| semantic recall / the embedding worker | 0.43 ms | already right: a separate worker process, a 0.25 s search ceiling, brute-force numpy over 51 vectors, and it degrades to FTS5 alone rather than blocking. `import lunad.embed` pulls in no numpy or onnxruntime. |
| `episodes.record()` before speech | 0.26 ms median, 1.38 ms max | it is on the path before the callback fires now anyway, and 0.26 ms is not worth reordering a memory write for. |
| `audit.jsonl` fsync | not on the pre-speech path | the only audit write before the answer is `process.spawned` at spawn time, with `durable=False`, so it does not fsync. |
| codex process start (250 ms) | 250–257 ms | only removable by keeping a codex process resident, which `codex exec` does not support. |
| `luna` CLI start (102 ms) | 102 ms | the typed path only; voice goes through the daemon socket with `detach`. Most of it is stdlib `argparse`/`_colorize`, not Luna's code. |

## 6. The budget, after

Same short factual voice ask:

| | before | after |
|---|---|---|
| daemon work before the spawn | ~9 ms | ~9 ms |
| codex process start | ~250 ms | ~250 ms |
| model turn | 2,400–3,600 ms | 2,400–3,600 ms |
| codex shutdown, waited for | **~470 ms** | **0** |
| TTS first audio, OpenRouter | 1,050–3,700 ms | **1,050–1,650 ms** (§4a) |
| TTS first audio, piper after 5 min idle | 1,609 + 212 ms | **212 ms** |

So: **−420 ms (median) on every ask**, **−1,565 ms more (median) whenever the
opening sentence is long enough to split**, and **−1,609 ms on the piper
path**.
Against a ~6 s total that is the whole of what is addressable on our side of the
call; the model turn and codex's own 250 ms startup are ~2.9 s of floor that
Luna does not control, and §3 shows the prompt is not the lever it looks like.

## Reproducing

Everything above came from short scripts driving the real objects — no fixtures
and no mocks except where a row says the backend was stubbed. The two live
harnesses worth keeping are described by what they do rather than shipped: time
each stage of `Daemon._ask` against the real memory store, and timestamp each
line of `codex exec --json` from `Popen` to exit.
