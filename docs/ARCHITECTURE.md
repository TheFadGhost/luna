# Luna — architecture

A resident personal assistant for Omarchy. Not a chatbot: a supervised daemon
with a voice, a memory that compounds, and the run of the desktop.

Status: Phases 0 through 2f built and running. Memory is complete to all
three tiers, semantic recall included. Phase 3 is substantially built — bar
widget, ambient hooks, semantic recall and the barge-in keybind are all live.
What is left of it is worker fan-out as something Luna *decides* to do rather
than something the admission gate merely allows, and wiring the `[ambient]`
settings into the Jarvis GUI (Settings → jarvis-settings has no pane for them
yet; see `docs/STATE-OF-PLAY.md`).
Where reality contradicted the design, the design text has been corrected
in place and the correction is marked **CORRECTED**.

---

## 0. Constraints that shaped this

| Constraint | Consequence |
|---|---|
| Ryzen 5 4500U, 7.1 GB RAM, no GPU/NPU | No local LLM as the brain. Cloud agent does the thinking. Only small models run locally (STT ~200 MB, TTS ~60 MB, embeddings ~90 MB — all *file* sizes; resident cost is 2-4× that, which is why each is unloaded when idle). |
| ~3-4 GB RAM free in practice | Every resident process is budgeted. Total Luna footprint target: < 400 MB. |
| Other Claude sessions run concurrently | Luna must never restart omarchy-shell, never kill agent processes she didn't spawn. |
| `omarchy update` clobbers /usr/share/omarchy | Everything Luna owns lives under ~/.config, ~/.local, ~/Work/luna. |
| voxtype already provides push-to-talk STT | Extend via its `post_process` hook. Do not fork it. |
| Full autonomy (user's explicit choice) | No permission prompts. Safety comes from an append-only audit log + undo, not from asking. |

---

## 1. Component map

```
     F9 / SUPER+CTRL+X            SUPER+SPACE
        (voxtype)                  (palette)
            |                          |
            v                          v
   luna-voice-router            luna-palette (QML)
            |                          |
            +-----------+--------------+
                        v
                  lunad  (daemon, systemd --user)
                   |  |  |
        +----------+  |  +-----------+
        v             v              v
    memory/       dispatcher      luna-speak
   (Hermes-      (spawns work)    (piper TTS)
    derived)          |
                      v
          special workspace "luna"
          foot (app-id org.omarchy.luna)
          + claude, full autonomy
                      |
                      v
              Sol / worker agents
```

Everything `lunad` forks — the piper worker, `aplay`, a headless `ask`, a
dispatched terminal — goes through `safety.spawn` and lands in the signal
allowlist. Nothing else in the package may deliver a signal (section 7).

## 2. Processes and their budget

| Process | Type | Resident RAM | Notes |
|---|---|---|---|
| `lunad` | Python, systemd user unit | ~40 MB | Supervisor. Owns socket, queue, memory, state. Never calls an LLM directly for long jobs. |
| piper worker | venv python, lazy + idle-unload | ~330 MB while loaded, 0 when idle | Spawned by `lunad`, so `lunad`'s cgroup reads ~470 MB while speaking and ~13 MB otherwise. Estimate of ~60 MB was WRONG. |
| embeddings | all-MiniLM-L6-v2 / onnxruntime | 86 MB file, 181 MB resident | Loaded lazily, unloaded after 5 min idle. Absent by default; see §4. |
| voxtype | already running | ~208 MB | Untouched. We only add a post_process hook. |
| agent session | foot + claude or codex | ~150-300 MB | Transient, only while working. Which CLI comes from `defaults/agent`; the runner script is written by that agent's adapter (section 6a). |

## 3. IPC

Single Unix socket: `$XDG_RUNTIME_DIR/luna/luna.sock`, newline-delimited JSON.

Every surface (voice, palette, hooks, CLI, bar widget) is a client. The daemon
is the only thing that touches memory or spawns agents. This is what stops the
system becoming four half-integrations that each grew their own state.

Requests, as built — this list is re-derived from `LunaServer.dispatch`'s own
table in `lunad/server.py`, not carried forward from an earlier one: `ping`,
`ask`, `look`, `say`, `speak.cancel`, `session.reset`, `codex.profile`,
`status`, `memory.{read,write,search,profile,consolidate}`, `dispatch`,
`jobs`, `peek`, `audit`, `spawned`, `ambient`, `ambient.diagnose`,
`settings.get`, `settings.set`, `confirm`, `cancel`, `shutdown`.
`memory.read`/`memory.write` take an optional `namespace` (`luna` or `sol`);
`memory.profile` and `memory.consolidate` are Luna's only — Sol's namespace is
a working set for one job, not a model of a person.
`look` takes an optional `scope` (§6b); `codex.profile` writes
`~/.codex/luna.config.toml` for `luna codex-profile` (§6a); `ambient` /
`ambient.diagnose` back `luna ambient` and the crash toast's one click (§7c);
`settings.get` / `settings.set` back the whole config contract (§7b);
`confirm` carries `list`, `yes`, `no` and the tool-side `ask` gate (§7a).
`subscribe` (for the bar widget's live state) was dropped in favour of a state
file — see below. `listen_start` was dropped too: the keybind talks to voxtype
directly, so the daemon never needs to start a recording.

`ask` takes `detach: true`, which acknowledges immediately and answers on a
background thread. That exists for the voice router, which runs inside
voxtype's `post_process_timeout_ms` and cannot wait for a reply it is not
going to print.

### Presence — `subscribe` was dropped, and why

`subscribe` is **not** being built. The bar widget reads a file instead:
`$XDG_RUNTIME_DIR/luna/state`, one bare ASCII word — `idle`, `thinking` or
`speaking` — written atomically on every transition, and absent when lunad is
not running.

The reason is the one constraint the reply path has: **nothing a surface does
may be able to slow an answer down.** A subscriber is a socket the daemon has
to write to, and a socket has a buffer a stalled reader can fill; the moment
`speaking` is published down a pipe nobody is draining, the blocked thread is
the thread that was about to speak. A file has no reader-side backpressure at
all. It also costs the widget nothing: Quickshell's `FileView` is inotify
under the hood, so the bar polls nothing, and voxtype already publishes its
own state the same way, so the desktop has one idiom rather than two.

`listening` is deliberately not in that list. Luna does not own the
microphone — voxtype does, and the daemon does not hear about a voice turn
until the transcript arrives, which is after the listening is over. The widget
reads voxtype's `$XDG_RUNTIME_DIR/voxtype/state` for that and composes the two.

`lunad/presence.py` holds the writer and the contract. It cannot raise: it is
called from the middle of answering a question, and a full disk is not a
reason to fail an answer that was otherwise fine.

## 4. Memory — adapted from Hermes Agent (Nous Research, MIT)

Three tiers, deliberately separate because they have different lifetimes,
sizes and costs.

### Tier 1 — curated identity (always in the prompt)
`~/.local/share/luna/memory/LUNA.md`  — cap 3000 chars, environment/conventions
`~/.local/share/luna/memory/USER.md`  — cap 2000 chars, user model

Taken from Hermes: `§`-delimited entries, and **hard caps that reject the write
rather than truncate**. Overflow forces consolidation instead of letting the
file rot into an unreadable log. Injected once, frozen at session start, so the
KV-cache prefix stays valid for the whole session.

### Tier 2 — episodic (searched on demand) — BUILT
`~/.local/share/luna/memory/episodes.db` — SQLite.
- `episodes` table: every exchange, with timestamp, surface, salience score.
- FTS5 index for keyword recall ("what did we decide about the bar widget").
- `episode_vectors` table: one 384-float BLOB per episode, for paraphrase
  recall ("how much charge is left" against an episode that says "battery").
- a `meta` side table, holding the consolidation watermark and nothing else.

**This is our addition, not Hermes'.** Hermes has no native semantic retrieval;
it outsources that to Honcho (Postgres + pgvector + a second LLM + a paid
service). That is not viable here, and a local embedding index is strictly
better than nothing.

#### Why both halves exist

FTS5 alone has a precision problem and a recall problem, and they were fixed
in that order because the second fix is only safe once the first is in place.

*Precision, fixed first.* A pure-OR query over every non-stopword token let
the filler sentence "so anyway do you think I should do something about this"
match **14 of 19** episodes in the real database, six of them on a single
incidental word. `_content_tokens` now drops a large closed-class stopword
list and refuses a query with no reasonably rare survivor at all; `search`
requires every token in the same row (AND) before it will widen to OR; and
`recall_block` refuses any hit whose **coverage** — the fraction of the
query's content tokens actually present — is below 0.5. That query now
matches **0 of 19**.

*Recall, fixed second, and FTS5 structurally cannot do it.* "how much charge
is left" retrieved **0 of 19** episodes while two of them were about the
battery. "charge" and "battery" share no token and no stemmer relates them.
Dictated and spoken input paraphrases constantly, so this is the common case,
not an edge one.

#### The embedding index

`sentence-transformers/all-MiniLM-L6-v2` — Apache-2.0, compatible with this
project's MIT licence and credited in the README — 384 dimensions, mean
pooling, 86 MB of fp32 ONNX, run under **`onnxruntime` alone**: not
sentence-transformers, not VoiceMem, and **not `sqlite-vec`**. With episodes
in the hundreds-to-thousands a brute-force cosine over one float32 matrix is
microseconds, so a native SQLite extension would be a build dependency, a
packaging problem and an `omarchy update` hazard bought for nothing
measurable. Vectors are BLOBs in the database that already exists.

No new pip dependency either. The WordPiece tokenizer is ~100 deterministic
lines of standard library over the model's own `vocab.txt` — `BertTokenizer`
with the settings `tokenizer_config.json` actually declares — verified against
published sentence-transformers reference pairs ("a man is eating food" /
"a man is eating a piece of bread" = 0.755, "that is a happy person" / "today
is a sunny day" = 0.257). The `tokenizers` package is a Rust wheel and is not
installed.

Same two-role shape as piper (§5), for the same reason: onnxruntime and numpy
live only in `~/Work/luna/.venv` and lunad is stock system python. So
`lunad/embed.py` is *imported* by the daemon as a stdlib subprocess manager
and *executed as a script* by `config.VENV_PYTHON` as the worker that holds
the model. Its package imports are guarded so the script half runs without
them.

#### How a paraphrase gets past the coverage floor without lowering it

The floor stays at 0.5 and means the same thing on both sides: how much of
what was asked about is actually in this episode. The lexical half measures it
as a token ratio; the semantic half maps cosine onto the same line through two
measured anchor points, and each episode keeps the **better** of its two
readings.

| cosine | coverage | why that anchor |
|---|---|---|
| ≤ 0.15 | 0.0 | not a neighbour; contributes nothing at all |
| 0.29 | 0.5 | exactly the floor — see below |
| ≥ 0.70 | 1.0 | saturated |

0.29 sits in a gap that was measured rather than chosen. Over 13 labelled
queries against the real 19-episode database — 8 with known-relevant
episodes, 5 contentful-but-unrelated controls — the lowest true positive
scored **0.311** and the highest false positive **0.269**, that FP being
"what is on my screen right now?" answering "what terminal do I use", which is
a near-miss rather than nonsense. The gap is narrow because the corpus is
small; re-measure once tier 2 holds thousands of episodes.

Only the **user's turn** is embedded, not the whole exchange. Measured both
ways: including Luna's reply recovers exactly one case out of thirteen and
costs a false positive of the same magnitude, because her replies share a
great deal of boilerplate with each other. It is also the wrong division of
labour — FTS5 already indexes both sides, so making the vector cover the
asker's phrasing gives the union two halves that fail differently.

`search()` sorts in three tiers: coverage, then a hit that contains the words
ahead of one that merely means the same, then the old `bm25` lifted by decayed
salience. The middle tier is not a nicety: BM25's IDF collapses toward zero on
a small corpus, so comparing it directly against a cosine would let a
paraphrase outrank a literal keyword hit purely because the two numbers are on
incomparable scales.

#### Nothing may slow down an answer

Recall happens on the ask path, so the discipline is `presence.py`'s.

- **The first question after a restart never waits for the model.** A cold
  `search` kicks an asynchronous warm-up and returns "no opinion" immediately;
  the ask is answered on FTS5 alone. Cold start is 0.4 s and is never paid by
  a question.
- Once warm, every request carries a hard 250 ms timeout. Measured warm search
  is **5.8 ms median, 6.8 ms p95** over the real database, including SQLite.
- Nothing raises. A missing model, a broken venv, a wedged worker or a
  malformed reply all come back as `{}`, and a failed spawn is *latched* so a
  hopeless fork is not attempted once per question for the life of the daemon.
- A content-free query searches neither index. The filler gate runs before
  both, so the precision fix above cannot be undone by the semantic path, and
  no model is ever asked about "so anyway do you think I should…".

#### Cost, measured on this machine

| | |
|---|---|
| model on disk | 86 MB (+ 226 KB of vocab) |
| worker resident while loaded | **181 MB** steady, 198–296 MB peak during a backfill |
| daemon-side cost | nil — the parent is stdlib and a pipe |
| cold start to first query | 0.40 s, off the ask path |
| warm query | 5.8 ms median, 6.8 ms p95, 250 ms hard ceiling |
| per-episode indexing | 28 ms of one core (real corpus), 102 ms worst case |
| per-episode storage | 1.5 KB |

181 MB is more than the "~90 MB" this document budgeted, and the 90 MB was
always the *file*, not the process — exactly as with piper, whose 61 MB voice
costs 331 MB resident. onnxruntime and numpy are 48 MB before a model is
loaded. Two things keep it honest: the CPU arena allocator is **disabled**
(left on it never returns memory and a batched backfill permanently set the
worker at 486 MB), and the worker is **unloaded after 5 minutes idle**, same
policy as speech.

#### Backfill

Existing episodes have no vectors, and embedding them must never block an
answer. A background thread, started lazily by the first semantic query, warms
the worker, hands it the vectors it lacks, and then embeds what is missing in
batches of four.

- **Resumable by construction.** "What still needs embedding" is an anti-join
  against `episode_vectors`, and each batch is committed before the next
  starts. A kill at any point costs at most one batch; there is no cursor to
  keep consistent and nothing to reset.
- **Batches are small on purpose.** Batching buys nothing here — 102 ms per
  episode at batch 1 against 126 ms at batch 32 — and costs memory linearly,
  198 MB peak against 573 MB, because attention scales with batch × sequence².
- **Power-aware.** Above 64 outstanding episodes it waits for mains: a
  first-ever index over thousands is minutes of sustained full-core work for a
  result nobody is waiting on. Catching up the handful recorded since the last
  session runs anywhere, and a machine that reports no battery counts as
  mains. `python3 -m lunad.embed backfill --force` overrides it.
- The thread exits when it is done rather than looping, so it cannot hold the
  model against the idle unload it is meant to respect.

#### A fresh clone has no model

Nothing downloads itself behind a question. `Embedder.available()` is false,
semantic recall is silently off, FTS5 answers alone, and every test in the
suite is in exactly that state — `tests/_support` points `config.VENV_PYTHON`
at a path that cannot resolve and `Embedder.python()` reads it late, so the
suite can neither fork a worker nor load onnxruntime.

```
python3 -m lunad.embed status      # is it there, is it on, is it warm
python3 -m lunad.embed fetch       # ~86 MB, sha256-pinned, into ~/.local/share/luna/models/
python3 -m lunad.embed backfill    # index old episodes now instead of in the background
```

`fetch` pins the sha256 of both files and only renames a download into place
once it matches: a silently different model is a silently different index, and
that would look like recall slowly getting worse rather than like anything
breaking.

### Tier 3 — derived profile — BUILT
`~/.local/share/luna/memory/profile.json` — regenerated from tier 2, never
hand-written, and rebuilt whole rather than appended to. There is no
`add_fact`: the only mutation is a rebuild, which is what makes `rm
profile.json` free. Nothing may live only here.

**The design is stolen; the implementation is stdlib.** VoiceMem
(`xzf-thu/VoiceMem`, Apache-2.0) was read and rejected as a dependency — ~3.27
GB of models on torch, transformers, funasr and sherpa-onnx against 3-4 GB of
free RAM and no GPU, with a Chinese-first ASR that would be a downgrade for
English dictation and no hosted API to fall back to. What was worth taking is
its **dual-brain split**, and only that:

- a **factual** half — schema extraction over five slots (`name`, `works_on`,
  `uses`, `prefers`, `avoids`), read from the user's own words and never from
  Luna's, since she paraphrases what she was told and counting her text would
  manufacture support for anything she repeated, mistakes included. Every fact
  carries the number of times it was seen, because a fact seen once is a guess
  and has to arrive looking like one.
- a **persona** half — an accumulator, not an extractor. Corrections (taken
  from the salience score already stored at write time, not re-detected),
  friction, approval, the user's median message length, their recurring
  vocabulary, when in the day they talk to her, and the one measurement that is
  genuinely about being talked to well: the median length of the reply that
  drew a "perfect" against the median length of the reply that drew "too long".

**It stores measurements and evidence, never prose.** Turning "seven
corrections, four about length" into "she finds you long-winded" is a
judgement, and judgement is what the consolidation pass pays a model for.
Written here it would be a regex quietly editorialising about the user in a
file that then looks authoritative.

It is **not** in the prompt. Injecting it would either break the cacheable
prefix every time it was rebuilt or add tokens to every single turn, and its
real consumer is the consolidation pass below, which reads it for free. It is
readable by hand with `luna memory profile` — which prints exactly the digest
the model is given, because showing the user something else would defeat the
point of being able to look — and `--rebuild` regenerates it on demand, which
matters when `consolidate_every_turns` is `0`.

### Salience and decay — also our addition
Hermes remembers on pure model judgement, with no decay: memories only get
tidied when the cap is hit. Luna scores each candidate memory 0-1 on
(explicitness of correction, repetition, consequence, recency) and applies a
half-life so trivia ages out on its own. Corrections from the user score 1.0
and never decay.

### Prompt cost and the cacheable prefix — CORRECTED

The Phase 0 note said each ask cost ~$0.05 because "separate processes never
share a prompt cache". That diagnosis was wrong. Anthropic's prompt cache is
keyed on the prompt *prefix* and IS shared across processes: a brand-new
`claude -p --session-id <fresh uuid>` was measured taking a 4510-token cache
**read**.

The actual fault was that the tier-2 recall block sat at the *end of the system
prompt*. Recall is chosen per request, so every ask changed the tail of the
prefix and invalidated the whole ~5.5k-token cached block, paying to re-create
it at cache-write price. That is the $0.05.

The fix is structural, not a smaller prompt: the system prompt now holds only
persona + tier 1 (stable between turns), and recall moved into the user
message, where changing it costs nothing. **$0.0513/ask before (n=7),
$0.0096/ask after (n=13).**

Conversation sessions (`--session-id` on turn one, `--resume` after; see
`lunad/session.py`) are implemented and on by default, retired when tier-1
memory changes, when idle for 30 minutes, or after 60 turns. Their measured
effect on cost is within noise ($0.0232 vs $0.0289 over three turns) because
resuming replays and re-caches a growing history. They are kept for
conversational continuity, not for money. A refused resume falls back to a
fresh session automatically.

### Consolidation — `lunad/consolidate.py` — BUILT
After every `[memory] consolidate_every_turns` completed asks, a background
pass reads the tier-2 episodes recorded since the last one, rebuilds tier 3,
and proposes tier-1 edits through the model. It runs on its own thread, is
started after the reply is built, and nothing waits on it.

**Why a model and not a rule.** Salience scoring already does as much as a
heuristic honestly can — it decides what is worth *keeping*, and it ranks the
input here — but what is worth *writing down as identity* is a different
question. Every attempt to write that one as a rule ends as a keyword list that
misses the interesting cases or a scoring function that promotes noise with
confidence.

**CORRECTED: "a cheap model" is not what makes it cheap.** The original note
said a background pass "on a cheap model", and there is no second model setting
in the contract to name one with. What makes a pass cost fractions of a cent is
the *prompt*: it runs under a librarian's system prompt of a few hundred
characters rather than Luna's ~8k-token persona, on at most 24 exchanges
clipped to 240 characters a side, with tier 3 as a digest. One call, bounded
above by roughly 3k tokens in.

**The cap contract is not bent for it.** A proposal that would push a file past
its cap is rejected whole, the file is left exactly as it was, and the overflow
is recorded. Additions are *not* dropped one at a time until something fits —
that would be the silent rot the cap exists to prevent, arriving through the
back door of the feature meant to relieve it. The model is shown the cap, the
usage and the numbered entries and can propose removals instead.

**One can be asked for, and looked at first.** `luna memory consolidate`
runs a pass now, synchronously, on the daemon's request thread, and prints what
it changed; `--dry-run` makes the same model call on the same episodes and
applies none of it. Waiting twelve turns to find out what a feature does to a
curated file is the wrong shape for something that spends real money, and a
write that has no undo needs a way to be read before it happens.

A manual run is past exactly two guards, and they are the two the person typing
the command is overriding on purpose: the turn counter, and the interval floor.
Everything else holds. Single flight matters most — the daemon answers each
connection on its own thread, so two of these in two terminals are genuinely
simultaneous — and `consolidate_every_turns = 0` still refuses, because that
setting's promise is that no tokens are spent and a command that spent them
anyway would make it a lie. A manual pass does not touch the turn counter, and
lands in `on_spend`, the log and the audit log exactly like an automatic one,
with `manual: true` on the entry.

The dry run is a second path through `consolidate.py` rather than the first one
with a flag on it, and deliberately not "apply, then put it back": `replace`
has no inverse, so there would be nothing to put it back with. The two calls
that change anything — `Tier1File.replace` and the watermark's `set_meta` —
appear nowhere in `Consolidator.preview`. What it calls instead is
`preview_edit`, a function over a list of entries and an integer cap that has
never been handed a file, and tier 3 is rebuilt with `persist=False`. **The
watermark does not move**, which is the part that would be cruel to get wrong:
advanced by a preview, the episodes you had just looked at would be skipped by
the pass you were deciding whether to allow. Its audit entry says `dry_run:
true` and counts `would_add`/`would_remove`, so nobody reading the log for what
was removed from `LUNA.md` can find a preview and believe it.

**It is safe to interrupt.** Tier-1 writes go through the same
temp-file-then-rename path as any other write, so a daemon killed mid-pass
leaves either the old file or the new one. The watermark — the id of the last
episode considered, kept in a `meta` table inside `episodes.db` — is moved
*after* the tier-1 write and never before. Interrupted, the pass reconsiders
exchanges it has already seen, which is harmless because the proposal is always
made against the current contents of the files; moved first, it would skip them
silently and for ever.

**What stops it running away**, since it spends the user's own money: `0` means
never, one pass at a time, a five-minute floor between passes whatever the turn
count says, no model call at all when there is nothing new since the watermark,
and no episode recorded by the pass itself so it cannot feed its own input. A
reply that will not parse still advances the watermark — it would not parse the
second time either, and paying twice for the same unusable answer is exactly
the runaway worth avoiding. The cost lands in `luna status`, in a `consolidated`
log line shaped like the ordinary `reply` line, and in one `luna audit` entry
per pass carrying the text of anything removed. No undo is claimed: `replace`
has no inverse, and a fabricated undo command is worse than none. What is
offered instead is the look before the leap — `luna memory consolidate
--dry-run`, above.

## 5. Voice

**In** — via voxtype **profiles**, which are a first-class feature. Confirmed:
`voxtype record start|toggle --profile <NAME>` exists, and `[profiles.<name>]`
can override `post_process_command`, `post_process_timeout_ms` and
`output_mode`. (`record stop` takes no `--profile`, so the profile is fixed at
start time.)

```toml
[profiles.luna]
post_process_command = "/home/ghost/Work/luna/bin/luna-voice-router"
post_process_timeout_ms = 2000
output_mode = "clipboard"
```

Keybind: **F10** runs `voxtype record toggle --profile luna`,
verified free against `hyprctl binds` and added to the user-owned
`~/.config/hypr/bindings.lua` so it survives `omarchy update`.
**F9 and SUPER+CTRL+X keep working exactly as today** - no shared mode flag, no
race, no fork of voxtype. This is the requirement that plain dictation must not
regress, and profiles satisfy it cleanly.

Gotchas (now confirmed by running it, not only by reading the binary):
- The transcript arrives on **stdin**; the router's **stdout replaces it**.
- On non-zero exit, spawn failure, or timeout, voxtype **falls back to typing
  the original transcript**. For Luna that means a crashed router types your
  speech into whatever window has focus. The router is therefore defensive:
  it catches `BaseException`, exits 0 and prints nothing on any internal error,
  strips C0 control bytes, and hands off to `lunad` with `detach` rather than
  blocking on the reply. Measured hand-off: 30 ms against a 2000 ms budget.
- **CORRECTED: `fallback_on_empty` cannot be set per profile, and cannot be
  turned off at all without breaking plain dictation.** `struct Profile` in
  voxtype 0.7.5 has exactly three fields: `post_process_command`,
  `post_process_timeout_ms`, `output_mode`. `fallback_on_empty` lives on
  `[output.post_process]`, which refuses to parse without a `command` - and a
  global command would route plain dictation through Luna too. Luna's router
  correctly prints nothing, so the fallback ALWAYS fires
  (`Post-process command returned empty output, using original text`). The
  mitigation is `output_mode = "clipboard"` in the profile: the fallback
  transcript goes to the clipboard, not into the focused window. Verified: 66
  chars to clipboard, 0 bytes typed.
- **CORRECTED: the voxtype daemon never re-reads its config.** Adding
  `[profiles.luna]` is not enough; without `systemctl --user restart voxtype`
  the daemon logs `Profile 'luna' not found in config, using default settings`
  and types the transcript. The CLI *does* read the file live, so
  `voxtype record start --profile <bogus>` printing `Available profiles: luna`
  proves the file is right and says nothing about the running daemon.
- **CORRECTED: `voxtype config` never prints profiles.** It validates them, but
  the display omits `[profiles]` entirely. Do not use it to check the profile
  took.
- The profile reaches the daemon through `$XDG_RUNTIME_DIR/voxtype/profile_override`,
  written by the CLI and deleted on read. `record cancel` while idle leaves a
  stale `cancel` file that silently cancels the *next* recording.
- `pre_recording_command` / `pre_output_command` / `post_output_command` are
  fire-and-forget compositor hooks. They do NOT receive the transcript and
  their stdout is not captured. Do not build on them.
- There is no pure "capture without typing" output mode; `mode` is only
  type|clipboard|paste. `record start --file <F>` is the closest to silent
  capture.
- Per-profile `model` override could not be confirmed - test before relying.

User's current voxtype config differs from the shipped default in only two
values (`type_delay_ms = 1`, `on_transcription = false`). Adding a `[profiles]`
section is additive and does not disturb existing behaviour.

**Out** — `lunad/speech.py` plus `lunad/piper_worker.py`, a persistent piper
process under the project venv. Sentence-streamed so speech begins on the first
full sentence rather than after the whole reply. Measured warm: **first audio at
45 ms** for a 14.3 s reply.

Built rather than designed, because the mechanics forced it:
- **The worker runs under `.venv/bin/python`, not lunad's interpreter.** piper,
  onnxruntime and numpy exist only in the venv; the daemon is stock system
  python and imports none of them.
- **Framed wire protocol** (`BEGIN` / `AUDIO <n>` + n bytes / `END <id>
  <status>`) rather than a raw byte stream. Every `say` produces exactly one
  `END`, cancelled or not, which is what lets a barge-in drain the abandoned
  utterance and know precisely where the next one starts. A raw stream would
  bleed the tail of a cancelled reply into the next one.
- **Cancellation uses a sequence number, not a flag.** A bare "cancelled" Event
  set while nothing is speaking would cancel the *next* utterance instead of the
  one interrupted.
- **One `aplay` per utterance, not per sentence** - per-sentence processes give
  an audible gap at every full stop, and the pipe's back-pressure throttles
  synthesis for free.
- **CORRECTED: piper emits one audio chunk per sentence.** `synthesize()` yields
  one `AudioChunk` per sentence, so a long single sentence produces no audio
  until it is fully synthesised. Our own sentence splitting is therefore what
  makes streaming work at all; units are capped at 260 characters.

Spoken output is deliberately short; detail goes to screen. `strip_for_speech`
replaces code blocks, file paths, URLs, e-mail addresses, hashes and long digit
runs with one short placeholder ("it's on screen"), collapses adjacent
placeholders, and truncates at a sentence boundary. A voice `ask` also tells the
model it is being read aloud and to answer in at most two sentences.

Piper specifics (researched, not assumed):
- Upstream is **OHF-Voice/piper1-gpl** (GPL-3.0). `rhasspy/piper` went read-only
  Oct 2025. AUR: use **`piper-tts-git`**; `piper-tts-bin` tracks the dead repo.
  `pacman -Ss piper` also matches an unrelated GTK mouse tool - ignore it.
- Voice: **`en_GB-jenny_dioco-medium`**, fallback `en_GB-alba-medium`.
  Medium tier only; `high` costs more on a 15W chip for marginal gain.
- Each voice is a `.onnx` + matching `.onnx.json` with the same basename in the
  same dir. Mismatched pairs fail silently or sound wrong. No auto-download;
  fetch from HF `rhasspy/piper-voices/en/en_GB/...`.
- Output: `--output-raw | aplay -r 22050 -f S16_LE -t raw -`. Sample rate must
  match the voice config. `pw-play` needs a WAV header, so raw PCM pairs with
  aplay via PipeWire's ALSA shim.
- **Measured on this machine** (Ryzen 5 4500U, en_GB-jenny_dioco-medium):
  - model load (cold): **1.12 s** - paid once
  - synth, warm: **0.41 s** for 6.75 s of audio
  - **RTF 0.061 = 16.4x faster than real time** (research extrapolated ~0.3;
    the real figure is five times better)
  - **peak RSS 331 MB** - python + onnxruntime, NOT the 61 MB model file
- **Revised residency decision.** 331 MB is too much to hold permanently against
  3-4 GB of headroom, and the original ~60 MB estimate in this doc was wrong.
  Because cold start is only 1.12 s, `luna-speak` **lazy-loads and unloads after
  5 minutes idle**, same policy as embeddings. Cost: the first sentence after a
  lull is ~1 s slower. Everything else is unaffected.
- Installed via **project venv** (`~/Work/luna/.venv`, 198 MB), NOT the AUR -
  `sudo` needs a password here so an unattended AUR build is not possible, and
  the venv is revertible with a single `rm -rf`.
- Voice files live in `~/.local/share/luna/voices/` (61 MB .onnx + .onnx.json).
- Download helper: `python -m piper.download_voices <voice> --download-dir <d>`.
- CLI has no `--config` requirement when the `.onnx.json` sits beside the model.
- Licence note: piper is GPL-3.0 but invoked as a separate binary over a pipe,
  so it does not affect Luna's own licence.


### 5a. OpenRouter TTS — BUILT, piper kept underneath

The default voice is now **`flux-alexis-en`** (female) through
`deepgram/flux-tts:free`, with **`flux-donovan-en`** as the alternate.
`POST https://openrouter.ai/api/v1/audio/speech`, JSON `{model, input, voice}`,
answering **RIFF/WAV, 24 kHz mono 16-bit**.

- **The WAV header is parsed off, not sliced off.** The chunk list is walked to
  find `fmt ` and `data`, because a `LIST`/`INFO` chunk before `data` is legal
  and a fixed 44-byte slice would feed metadata to `aplay` as audio — audible,
  as a click. What reaches `aplay` is raw PCM, exactly as with piper, so the
  section 5 rule holds: **one `aplay` per utterance, not per sentence**, and
  there is no gap at the full stop.
- **One request per sentence**, with a producer thread running exactly one
  sentence ahead. One ahead and not all of them: a barge-in two words in should
  not have paid for the whole reply.
- **piper stays the fallback**, controlled by `[voice] fallback`. Any HTTP
  error, timeout, 502, empty body or unparseable payload hands the *remaining*
  sentences to piper — mid-utterance, not only at sentence one, because the
  failure providers actually produce is an intermittent 502 that is as likely
  on sentence three. `fallback = "none"` raises instead. A failed TTS must
  never be the reason she is silent.
- `[voice] max_spoken_chars` (400) caps the spoken form at a sentence boundary;
  the full text still goes to screen. The "never speak code, paths or URLs"
  stripper from section 5 is unchanged and runs first, for both providers.

**Measured on this machine, first audio, warm** (the number that decides
whether she feels responsive):

| | short utterance | two-sentence utterance |
|---|---|---|
| piper, local | 96–124 ms | 341–409 ms |
| OpenRouter `flux-sienna-en` (measured; alexis is the same model) | 1.5–1.8 s | 2.3–2.9 s |

So the remote voice costs roughly **1.4–2.5 s of extra latency before the first
word**, in exchange for a markedly better voice. It is a real trade, not a free
upgrade, and `[voice] provider = "piper"` switches back with no restart. (The
40–45 ms figure elsewhere in this document is the ledger-write cost inside
`safety.spawn`, not end-to-end first audio; measured end-to-end, warm piper is
about 100 ms for a short sentence.)

## 6. Delegation — Luna leads, Sol specialises

- **Luna** is the only one the user addresses. Conversational, opinionated,
  budget-aware. She triages and decides who does the work.
- **Sol** is a specialist she enrols for deep technical work: own system prompt,
  own memory namespace (`memory/sol/SOL.md` + its own episode store), reports
  back to Luna not to the user. Spec in `data/sol-persona.md`.
- **Workers** are anonymous, disposable. Fan-out grunt work. Luna still does
  not *plan* a fan-out for you — one `dispatch` call is one job — but several
  can now be in flight and the number is bounded: see the admission gate below.

Luna announces who she enrolled and why, in one line. The line is composed in
`dispatch.py`, not asked of a model — an announcement that cost four seconds
and an API call would not get made. She does not delegate what she could finish
in one step.

### Self-dispatch — BUILT

She delegates by doing it, not by offering to. `luna dispatch` already
existed, was already audited, and already returns immediately — so Luna runs
it herself, from her own shell, in the same turn: `luna dispatch --to sol
"<task>"` by absolute path, because her shell is `lunad`'s and `lunad` is a
systemd `--user` service whose `PATH` need not contain `~/.local/bin`. A bare
`luna` would have worked when tested from the user's own terminal and failed
from hers — the gap only shows up run from inside the daemon.

**It has to work from inside a live ask, and that was verified rather than
assumed.** `ReentrancyCase` stands up a real Unix socket, has the adapter open
a second connection from inside `ask`, and reads the dispatch reply back while
the outer request is still open. Nothing deadlocks:
`ThreadingUnixStreamServer`, `daemon_threads`, and no lock held around
`Daemon.dispatch`.

**The result has to come back, or delegation is disappearance.** The toast
already existed; the memory did not — a finding sat in `jobs/<id>/output.txt`
where Luna would never read it, so a delegated job that succeeded was
forgotten the moment it finished and the same question a week later
dispatched the same job again. `ReportingDispatcher` (`lunad/server.py`), a
subclass rather than a change to `dispatch.py` — the dispatcher owns
terminals, pids and exit codes, not memory — writes a finished job into tier 2
as an ordinary exchange: the task as the user's side, the output as hers. It
is then retrieved by the ordinary recall path, with nobody having to name a
job id.

Dispatched jobs run `gpt-5.6-sol`, not whatever codex would otherwise have
picked, and the Daemon hands its own agent name to the Dispatcher — without
that, a self-dispatch from a codex-brained Luna would have been written in
claude's flags, `~/.config/omarchy/defaults/agent`'s default, and every job
would have quietly failed in the hidden workspace.

### How a job actually runs — BUILT, and not as designed

`luna dispatch "..."` writes a job directory under
`~/.local/share/luna/jobs/<id>/` (`task.txt`, `system.txt`, `run.sh`,
`output.txt`, `stderr.txt`, `exit`, `job.json`), spawns `foot` running
`run.sh`, and watches the pid. `run.sh` runs whichever agent adapter is
active, full autonomy either way — on codex (the default, §6a) that is
`--dangerously-bypass-approvals-and-sandbox`; on claude it is
`--permission-mode bypassPermissions --tools default --safe-mode` — wrapped in
`timeout`, piped through `tee`. **Dispatch does not hard-code either agent's
flags**: the runner script asks the active adapter for its own command line,
which is what let the brain move from claude to codex (§6a) without touching
this path. `luna jobs` lists them newest first, from disk, so the list
survives a daemon restart; `luna peek` toggles the workspace.

Four things the design got wrong, corrected here:

- **CORRECTED: Luna spawns the terminal herself; Hyprland does not.** The
  obvious route is an exec rule — `hyprctl dispatch exec "[workspace
  special:luna silent] foot ..."` — which lets the compositor place the window.
  It also means the compositor owns the pid: Luna could not claim the process,
  wait on it, or read its exit status, and the firewall in section 7 is only
  worth something if the ledger records forks she actually performed. So `foot`
  is started with `subprocess.Popen` and placed with a *window* rule instead.
- **CORRECTED: `omarchy-launch-tui` is not used.** It ends in
  `exec setsid uwsm-app -- xdg-terminal-exec ...`, which is three layers of
  re-exec between Luna and the terminal. The pid she would get back is not the
  pid that ends up running. Same reason as above.
- **CORRECTED: the app-id is `org.omarchy.luna`, not `org.omarchy.agent`.**
  `omarchy-launch-tui` gives agent terminals the app-id `org.omarchy.agent`,
  and the user had four windows carrying it open while this was being built. A
  workspace rule matching that class would have swept live sessions into Luna's
  hidden workspace. This was the single most dangerous thing this phase could
  have got wrong.
- **CORRECTED: the workspace is `luna`, and the rule is installed at runtime.**
  `scratchpad` is bound to SUPER+S and belongs to the user. The window rule is
  added through `hyprctl repl` behind a Lua global, so repeated dispatches do
  not stack duplicates and nothing under `~/.config/hypr` is edited. It
  disappears on the next Hyprland config reload, at which point the next
  dispatch adds it again.

### The admission gate and the collector — BUILT

Two things that were pending long enough to be documented as pending, and are
now real.

**`[dispatch] max_parallel` is an admission gate with a queue, not a refusal.**
A dispatch over the limit is accepted: it gets its id, its directory and its
composed prompt immediately, is written to `jobs/` in a `queued` state, shows
in `luna jobs` as `wait`, and can be cancelled there — it simply has no pid
until a slot frees. Admission is FIFO even when a slot is free, because
admitting a newcomer past jobs already waiting turns a queue into a lottery.
The limit is read at each admission decision rather than captured, so lowering
it stops admitting without touching anything already running and the count
drains on its own; raising it releases waiting work at once, because the
settings listener pokes the queue rather than waiting for the next job to end.
A queued job is **dropped** on daemon shutdown, recorded as cancelled with the
reason: the queue only ever existed in one process's memory, and a `queued`
directory left behind by a dead daemon is a promise nobody is going to keep.
`luna jobs` says so too — such a directory reads as `orphaned`, not `queued`.

**`[dispatch] job_retention_days` is a GC pass with a stated policy.** A job
directory is aged from when the job *stopped* — `finished`, falling back to
`started`, falling back to the manifest's mtime — because retention is about
how long the record is kept and a six-hour job's record begins when it ends.
Nothing running or queued is collected at any age; an orphan is, once past the
window, because it will never finish and one crash should not pin a directory
for the life of the machine. **Zero means never**, which is the opposite of
what "zero days" reads like and is the entire reason the case exists. The pass
runs on a six-hour timer in its own thread plus once at start-up, never on the
request path, and every deletion is an audit entry with no `undo`, because
there is not one.

### The Hyprland incantation — the thing that cost the time

Omarchy's Hyprland (0.56.2 here) takes a **Lua** config, and `hyprctl` wraps
its arguments in `return hl.dispatch(<args>)` before evaluating them. So the
familiar bracket syntax is not a Hyprland error, it is a *Lua parse* error:

```
$ hyprctl dispatch exec "[float] echo hi"
error: [string "return hl.dispatch(exec [float] echo hi)"]:1: ')' expected near 'echo'
```

`--`, quoting and `hyprctl --instance` do not help: the text never reaches a
shell. Nor does `hyprctl keyword`, which answers
`keyword can't work with non-legacy parsers. Use eval.` The forms that work:

```sh
# exec with window rules — rules stay inside the Lua string
hyprctl dispatch 'hl.dsp.exec_cmd("[workspace special:luna silent] foo")'

# show/hide the special workspace
hyprctl dispatch 'hl.dsp.workspace.toggle_special("luna")'

# a window rule at runtime (this is what dispatch uses)
hyprctl repl 'hl.window_rule({ match = { class = "^org\.omarchy\.luna$" },
                               workspace = "special:luna silent" })'
```

`hl.window_rule` accepts almost any table without complaining, so its key names
could not be discovered by experiment. They were read from Omarchy's own
`/usr/share/omarchy/default/hypr/helpers.lua`.

Two more measured details: `hyprctl activeworkspace` never reports a special
workspace — read `monitors[].specialWorkspace.name` instead. And `foot` *does*
propagate its child's exit code, though `run.sh` writes `exit` to disk anyway,
because that is the copy that survives a daemon restart.

## 6a. Agent adapters — BUILT

`lunad` never calls a model API. It shells out to whichever headless agent CLI
`~/.config/omarchy/defaults/agent` names, so Luna follows the desktop's default
rather than inventing her own. Two adapters are real, both verified against the
binaries actually installed here.

The two CLIs share almost nothing, and the differences are the design:

| | `claude` (2.1.241) | `codex` (0.149.1) |
|---|---|---|
| headless entry | `claude -p <prompt>` | `codex exec -` (prompt on stdin) |
| system prompt | `--append-system-prompt` | **no such flag** — `-c developer_instructions=` |
| machine output | `--output-format json`, one object | `--json`, JSONL events |
| tool policy | `--tools ""` | the sandbox *is* the tool policy — see below |
| user config off | `--safe-mode` | `--ignore-user-config --ignore-rules` |
| session id | caller chooses, `--session-id` | codex assigns, read from `thread.started` |
| cost | dollars, metered | none — ChatGPT subscription |

**`[assistant] agent` now defaults to `codex`.** `~/.config/omarchy/defaults/
agent` is not touched — it is the whole desktop's default and other things
read it, so it stays the fallback rather than the source of truth — but
Luna's own default is `codex`, model `gpt-5.6-luna` (`config.CODEX_ASK_MODEL`,
an adapter default and deliberately not `[assistant] model`: a slug is not
portable between agents, and pinning it in the config would be wrong the
instant someone set `agent = "claude"`). Dispatched jobs, specialist or
anonymous worker alike, run `gpt-5.6-sol` (`config.CODEX_DISPATCH_MODEL`) —
Luna thinks, Sol works, and the model follows the role.

### She has real tools on the ask path, and did not used to

`CODEX_ASK_SANDBOX` was `"read-only"` and persona.py's closing text told the
model so: *"You are running headless with no tools. You cannot read files, run
commands, or inspect the machine right now."* Both were true and both were the
wrong trade — asked for a version she said she could not check it; asked what
was on screen she suggested starting the daemon, which was bad advice she had
no way of knowing was bad, because nothing in her prompt said the daemon she
runs inside has a dispatcher, a CLI and a pair of eyes. It is now `"bypass"`,
same as the dispatch path, and the persona's closing text was rewritten to
name the shell, files and the web instead of denying them. `claude`'s ask path
still passes `--tools ""` and keeps the honest "no tools" text, chosen per
adapter through `BaseAdapter.ask_has_tools` — deleting it there would only have
moved the same lie one agent to the left.

**`"bypass"` rather than `"danger-full-access"`, and the choice is about the
mechanism, not about how much access she should have** — the user chose full
autonomy and the audit log is the backstop for that regardless. Two reasons,
both about how codex's flags actually behave (verified against 0.149.1
`--help`, not assumed):

- `codex exec resume` accepts no `-s`. Under a sandbox *mode* the policy has to
  be restated as `-c sandbox_mode=…` on every resumed turn, and turn one and
  turn two ending up under different policies is exactly the kind of mismatch
  that is discovered in production, not in review.
  `--dangerously-bypass-approvals-and-sandbox` is accepted identically by
  `exec` and by `exec resume`, so there is nothing to keep in step across a
  resume.
- `danger-full-access` removes the sandbox but leaves the *approval* policy in
  place, and an approval request in a headless turn is not a prompt anyone can
  answer — it is a hung ask. `bypass` turns approvals off along with the
  sandbox, which a headless daemon needs regardless of how permissive the
  sandbox mode is.

**The missing system-prompt flag is the whole problem.** Luna's persona and her
frozen tier-1 block have to reach the model somehow. codex takes `-c key=value`
overrides whose value it parses as TOML and falls back to a raw literal when
that fails, and `developer_instructions` is the key that layers a developer
message on top of codex's own base instructions. `instructions` also works but
*replaces* them, costs more, and would strip a dispatched session's tool
guidance; `base_instructions`, `system_prompt` and `persona` are rejected
outright under `--strict-config`.

**Cost is not comparable between the two and is not pretended to be.**
`AgentReply.billing` says `"metered"` or `"subscription"`. On codex `cost_usd`
is `None` and tokens are reported instead, because tokens are what was spent.
The daemon only ever adds a truthy `cost_usd`, so a subscription reply moves
the money counter by nothing, which is the truth.

## 6b. Sight — `lunad/context.py` — BUILT

Two reads of the same compositor, sharing one module because the second needs
the first's geometry.

**The focused-window line rides on every ask.** `hyprctl -j activewindow`
gives one line — app-id, class, title, workspace — and it goes in the **user
message**, never the system prompt. The system prompt is the cacheable
prefix and must stay byte-identical between turns; a line that changes every
time the user alt-tabs would invalidate it on every single ask, which is
exactly the mistake tier-2 recall made (§4, "Prompt cost and the cacheable
prefix") and exactly the cure. It costs about twenty tokens and cannot cost an
answer: one query, a one-second ceiling, and every failure — no compositor, no
focused window, `hyprctl` missing or hanging, output that is not JSON —
degrades to `""` and the ask goes out without it.

**`luna look "<question>"` is the explicit path, and only it takes a
photograph.** `grim -g <geometry>` (geometry read from Hyprland's `at`/`size`)
captures the focused window into a `mkdtemp()`, which is removed in a
`finally` regardless of whether the call raised — an agent call that failed
must not leave a picture of the user's screen behind on disk. The image
reaches the model through `codex exec -i`, which `gpt-5.6-luna` reads
natively: no second model, no vision service, and no OpenRouter call —
OpenRouter is for text-to-speech and nothing else (§5a), and a test greps the
module to keep it that way. Nothing is captured unless a look was actually
asked for; an ordinary ask photographs nothing, on purpose, and there is a
test that fails loudly if that ever stops being true. `BaseAdapter
.accepts_images` lets a look through an agent that cannot take one say so,
rather than silently dropping the image and answering from the window title
alone — which is everything a model needs to confidently narrate a screen
nobody looked at.

## 7. Safety under full autonomy — BUILT

No permission prompts, by choice. Instead:

### Session firewall — `lunad/safety.py`

One gate, `may_signal(pid) -> bool`, and every path in `lunad` that could
deliver a signal goes through it. It answers True only if **Luna spawned that
pid** and **the process now holding it is still the one she spawned**.

The second half is not decoration. Linux recycles pids, so a ledger that
remembers only numbers hands out permission to whoever inherits one. Each
record stores field 22 of `/proc/<pid>/stat` — start time in clock ticks since
boot, stable for the life of a process — and a mismatch drops the record and
refuses. The comm field (2) can contain spaces and parentheses, so the parse
splits after the *last* `)`; getting that wrong would shift every later field
and reject processes Luna really did spawn.

Refusals **raise** (`SignalRefused`), never return False into a caller that can
ignore them. `signal_group` additionally refuses any pid that is not its own
process group leader — a child sharing the daemon's group would take the daemon
with it. Everything Luna spawns gets `start_new_session=True`, so that check is
satisfied by construction and violated only by a bug.

`safety.spawn()` is the other half of the same gate: `Popen` and ledger
registration as one operation, so there is no window in which Luna owns a
process she cannot prove is hers. The allowlist is
`~/.local/share/luna/spawned.json`. Durable records (dispatched jobs, which
outlive the daemon) are fsync'd; transient ones (the piper worker, `aplay`, a
headless `ask`) are not — an fsync measured anywhere from 0.4 to 4 ms depending
on cache state is not worth paying in the path that decides how fast Luna
starts speaking, for a record that is worthless after a crash anyway. What is
left costs 0.41 ms, and first audio still measures 40-45 ms warm, unchanged
from Phase 1.

`pkill`, `killall` and every other match-by-name kill are banned outright, and
a test reads the shipped source to prove it: `os.kill`/`os.killpg` appear in
`safety.py` and nowhere else, no `Popen.terminate()`/`.kill()` survives, and
every child is spawned through `safety.spawn`. The one deliberate exception is
`signal_self`, the daemon stopping itself — `may_signal` refuses `getpid()`
precisely so self-termination cannot be reached by accident.

**`terminate()` used to return `True` right after sending `SIGKILL`, without
checking the process actually died** — a process stuck in an uninterruptible
kernel sleep survives `SIGKILL` too, and the caller had no way to know. It now
confirms death with a bounded poll after each signal (`Popen.poll()` is a
`waitpid(WNOHANG)` under the hood, so the check that notices death is the same
call that reaps it — no window for a pid to slip through between "confirmed
dead" and "reaped") and returns `False`, honestly, when even that cannot
confirm it. `reap()` itself only ever edited the ledger; it never waited, and
every "wait once, give up silently" call site was treating it as if it also
reaped the child. `reap_after()` does an actual bounded wait and, if that is
not enough, keeps trying on a background thread rather than abandoning the
child as a permanent zombie — the fix for a bug where a hung `notify-send` or
a wedged dispatched terminal leaked one zombie per occurrence, forever, until
a restart. The pid-firewall invariant is unchanged by this: the fix only moves
*when* the real reap happens, never what `may_signal()` checks, and a recycled
pid is still caught by the start-time comparison even in the worst ordering.

### Audit log — `lunad/audit.py`

`~/.local/share/luna/audit.jsonl`. Opened `"a"`, fsync'd per line, never
truncated, never edited in place. A log Luna can rewrite is not evidence. Every
dispatch, spawn, signal, refusal and memory write, with `why` (the intent, not
the mechanics), the outcome, and the exit status. `luna audit [--since 30m]`
reads it back newest first.

**Rotation moves bytes; it does not drop them.** This file used to grow forever
on purpose, and the purpose was sound — a rotation that quietly loses the week
you are asking about is worse than a large file. So past `[audit] max_mb` the
live log becomes `audit.jsonl.1`, each sibling shifts up one, and only the
oldest of `[audit] keep` is deleted. That deletion is itself an entry,
`audit.rotated`, written as the **first line of the new live file** and naming
what was renamed and what was dropped, so the chain reads backwards from the
live file and any gap in it explains itself. The rename happens between two
whole lines, while the lock is held and after the previous line's `fsync`, so
the append-only contract is never broken mid-write; a reader that already
opened the file goes on reading the renamed inode. `luna audit` reads the
siblings too, stopping at the first file that cannot hold anything the query
asked for. `max_mb = 0` never rotates.

**Undo journal — deliberately sparse.** An inverse is recorded only where one
genuinely exists and is known at the time: a tier-1 *append* is undone by
removing the index it landed at, a dispatched job by cancelling it, `peek` by
itself. `replace` and `remove` discard text that is not kept anywhere, so no
undo is claimed for them. A fabricated undo command is worse than none,
because someone will run it.

### The five
Wiping disks, force-pushing over history, deleting the customisations log,
`rm -rf` outside her own dirs, touching another agent's session: stated risk,
second explicit instruction required. Judgement, not a prompt. This is persona,
not code — the code enforces the pid boundary, not the other four.

### What is *not* enforced by code
A dispatched session runs full-autonomy and real tools — `bypassPermissions`
on claude, `--dangerously-bypass-approvals-and-sandbox` on codex (§6a) — either
way, unsandboxed. It could write anywhere the user can. Sol's namespace
isolation is enforced in `lunad`'s
own memory API (`SolMemory.file` refuses `LUNA.md` and `USER.md` by name) and
stated in his system prompt; it is not a filesystem sandbox, and this document
says so rather than implying otherwise. The audit log is what makes that
tolerable.

## 7a. Confirmation — `lunad/confirm.py` — BUILT

The user revised the Phase-2 "full autonomy, no prompts" position to *"not
proper constraints, but just double check before doing X"*. Section 7 above is
unchanged; this is a layer on top of it, and it is a layer with three
strengths, which is the part that matters.

**Layer 1 — hard denies, in code.** Four things the config cannot re-enable:
signalling a process Jarvis did not spawn, restarting `omarchy-shell`, deleting
`~/.config/omarchy/CUSTOMISATIONS.md`, and `rm -rf` outside Jarvis's own
directories. The first is not implemented in `confirm.py` at all — it *is*
`safety.may_signal`, which every signal in the package already goes through,
and a second copy would be a rule that can disagree with itself. The other
three are text patterns checked before dispatch. They are never asked about: a
question the user cannot answer with "yes" is not a question.

**Layer 2 — policy classes, in the config.** `install_packages`,
`delete_files`, `write_outside_home`, `system_config`, `network_send`,
`git_push`, `long_job`, `spend`, each `never` | `ask` | `deny`. `ask` puts an
Omarchy toast on screen whose click action is `luna confirm yes <token>`, and
optionally a line in the journal with the same token. Omarchy's notification
takes exactly **one** click action, so the toast is the *yes* and silence is
the *no* — the asymmetry is deliberate and it is the safe way round.
`[confirm.prompt] default_on_timeout` can invert it; the default is `no`.

`long_job` and `spend` are not text-classifiable and do not pretend to be: they
fire on a number a caller supplies, and an absent number does not mean "under
the threshold", it means unmeasured, so the class does not fire. In particular
`DISPATCH_TIMEOUT_S` is a *ceiling*, not an estimate, and gating `long_job` on
it would ask about every job ever dispatched — which trains the user to click
through, and is worse than not asking.

**Layer 3 — the tool-side gate, advisory.** A dispatched agent's system prompt
lists the classes currently set to `ask` and tells it to run
`luna confirm ask <class> "<what>"` first; exit 0 is yes, exit 3 is no. This is
a real channel — it reaches the same broker and puts the same toast on screen —
but it is honoured by the agent *choosing* to run it.

**What is and is not intercepted, stated plainly.** Genuinely enforced: the
daemon's own `dispatch` path. Nothing is forked by `lunad` until the task text
has been through the classifier, and a refusal means no terminal was ever
started. Not enforced: anything an already-running dispatched session decides
to do. It has real tools and bypassed permissions, and the daemon does not see
its individual tool calls. The classifier also reads the *task text*, so a task
phrased vaguely enough ("tidy up the old build output") will not classify even
though the work is a delete. The mitigations are the audit log and layer 3, not
a guarantee.

Every decision — auto-allowed, asked, approved, declined, timed out, hard
denied — is one line in `audit.jsonl`, with the class, the policy, the token,
the channel and how long the user took.

## 7b. Configuration — `lunad/settings.py` — BUILT

The app is **Jarvis**; the assistant's name is a *setting* (`[assistant] name`,
default `Luna`), used in her system prompt, her greetings, her notifications
and her log labels. Nothing in the new code writes "Luna" literally.

`~/.config/jarvis/config.toml`, 0600, in a 0700 directory.
`docs/CONFIG-SCHEMA.md` is the contract; the schema table in `settings.py` is
its executable copy, carrying the default, the type, the allowed values and the
comment for every key. A test parses the document and asserts they still agree,
so the settings GUI and the daemon cannot drift apart silently.

- **Reading** is `tomllib` (3.14 stdlib).
- **Writing** is hand-rolled — there is no writer in the standard library, a
  dependency for this would be absurd, and a JSON-ish dump would throw away
  every comment, which is most of what makes a config file usable. The
  serialiser emits the schema's own comments in the schema's own order.
- **Hot reload** is a `stat` every 2 s on mtime-ns *and* size. Size matters:
  a GUI toggling one boolean rewrites the same length in the same second.
  Every reload logs a diff and lands in the audit log. Values are read at the
  point of use, never cached at start-up, so voice, model and confirm policy
  take effect on the next request. Two settings need more: her *name* is in
  the cacheable prefix, so a rename retires the live sessions; the *agent* is a
  different binary, so switching it swaps the adapter.
- **An invalid value warns and falls back**, never crashes — a typo from the
  GUI must not cost the user their assistant. `settings.set` over the socket
  does the opposite and *refuses*: a program asserting a value deserves to be
  told it is wrong.
- Ops `settings.get` / `settings.set` expose all of it, plus the schema, so the
  GUI can build itself from the daemon rather than from a second copy.

**Secrets are never in `config.toml`.** `~/.config/jarvis/secrets.env`, 0600,
read by systemd through `lunad.service.d/10-jarvis-secrets.conf`. The drop-in
also reads `~/.config/voxtype/secrets.env`, which already held the key on this
machine — lunad accepts `VOXTYPE_WHISPER_API_KEY` as a fallback so nothing had
to be copied out of a file that belongs to another program. `secrets_status()`
reports whether a key exists and where it came from, never the key.

## 7c. Ambient — `lunad/ambient.py` — BUILT

Until this, Luna only ever existed when addressed: every path in the daemon
starts with a request arriving on the socket. This is the first that starts
with the machine instead — three hooks, and one rule that outranks all of
them.

**The rule: an ambient event notifies, it never speaks.** In the user's own
words: *"I prefer notify only, unless I spoke to it first and it was coming
back with an answer to a task that I gave beforehand."* A coredump, a flat
battery and an `omarchy update` are none of those — they happened *to* the
machine while the user was doing something else, and a voice interrupting
that is exactly the failure mode they asked to avoid. This is enforced, not
commented: `ambient.py` does not import `lunad.speech` and never will;
`Ambient` only delivers through a `Notifier`, type-checked at construction, so
a plain callable is refused; and `_assert_mute` walks everything hung off the
`Ambient` object at construction and refuses any collaborator with a `.say()`
or `.speak()` method. `tests/test_ambient.py::NeverSpeaksCase` walks the live
object graph for a reachable speaker and fails if any of the three is ever
weakened.

**Three hooks, two of them off by default because the desktop already does
the job:**

- **Crash** (`[ambient] crash`, default **off**). `omarchy-crash-watch.service`
  ships with Omarchy, is enabled, and already streams the coredump
  `MESSAGE_ID` out of the journal — event-driven, no polling — dedupes crash
  loops on a 60 s window, and toasts with a click that runs the same
  `diagnose-crash` skill. It knows the signal name and the full executable
  path, which a core *filename* does not, so it is strictly better at the
  job. Luna's hook is kept for what the desktop's cannot do: the crash lands
  in her **audit log** and the diagnosis in her **job list**, under her own
  confirmation policy, and for anyone who has switched Omarchy's watcher off.
  When it is on: one `stat()` on `/var/lib/systemd/coredump` per tick, a
  `scandir` only when the mtime moves, and it never forks `coredumpctl`. The
  toast's one click runs `luna ambient diagnose <pid>`, which dispatches an
  agent session against the `diagnose-crash` skill — not automatic, because a
  diagnosis is a real model call and a terminal window, and running one
  unasked on every core dump would be Luna acting rather than noticing.
- **Battery** (`[ambient] battery`, default **off**). Omarchy's own
  `shell/plugins/services/battery/Service.qml` already polls every 30 s and
  warns at 10%, with UPower hibernating at 2%; a second toast about the same
  battery at the same moment is worse than none. The battery is found by
  reading each `/sys/class/power_supply/*/type` for `Battery` rather than
  assumed — on this laptop it is `BAT1`, not `BAT0`, with no `charge_now` at
  all. Turning it on gets an *earlier* warning, deliberately either side of
  Omarchy's own (20% / 5% against Omarchy's 10% and UPower's 2%).
- **Update** (`[ambient] update`, default **on** — the one hook nothing else
  on this machine watches). `omarchy update` is `pacman -Syu --overwrite
  '/usr/share/omarchy/*'`, which rewrites that whole tree — exactly how a
  customisation gets silently reverted. Two `stat()`s and a 12-byte read of
  `/usr/share/omarchy/version` (contents *and* mtime, because a same-version
  reinstall clobbers just as thoroughly) plus `/tmp/omarchy-update.log`.
  Whether an update is merely *available* is deliberately not checked here —
  that costs a `checkupdates` network sync, and Omarchy's own bar widget
  already polls it every six hours and shows the answer.

State persists in `~/.local/share/luna/ambient.json` so a restart does not
re-announce a fortnight of coredumps, and every watcher seeds silently on its
first run. Nothing in this module may raise into the daemon — the same
contract as `presence.py`, for the same reason.

**The HUD pane's message-file contract is implemented here.** The click-through
Quickshell overlay itself lives on the machine, not in this repository —
`~/.config/omarchy/plugins/ghost.lunahud/`, documented in
`~/.config/omarchy/CUSTOMISATIONS.md` §8a.14 the same way the bar widget is
referenced in `docs/STATE-OF-PLAY.md`. Its contract, `HANDOFF-hud.md`, is one
JSON object atomically written to `$XDG_RUNTIME_DIR/luna/message` (sibling of
`presence.py`'s `state` file) with a monotonically increasing `id`, which is
what makes a message *new* to the reader; `ambient.py` is what publishes into
it now, on the same notify-never-speak channel as the toasts.

## 8. Build phases

| Phase | Ships | Verifiable by |
|---|---|---|
| P0 | `lunad` + socket + CLI + memory tiers 1-2 + persona. Text only. | `luna ask "..."` returns an opinionated answer that cites remembered context. |
| P1 | **DONE.** piper TTS out, voxtype routing in, session reuse, cost fix. | F10, speak, she answers aloud. Plain dictation (F9) still types — regression-tested. |
| P2 | **DONE.** Workspace dispatch + Sol + audit log + PID firewall. | `luna dispatch "..."` runs in the `luna` special workspace and reports back; `luna spawned --check <foreign pid>` refuses. |
| P2b | **DONE.** Jarvis: config file + hot reload, OpenRouter TTS with piper fallback, confirmation policy, name as a setting. | Edit `~/.config/jarvis/config.toml`, do not restart, hear the change. |
| P2d | **DONE.** Tier 3 (the derived profile) and the tier-1 consolidation pass, wiring `[memory] consolidate_every_turns`. | `luna memory profile --rebuild` prints facts drawn from real episodes; a pass shows in `luna status` with what it cost. `luna memory consolidate [--dry-run]` runs one on demand, or shows what one would do without doing it. |
| — | **DONE.** The brain moved from claude to codex (`gpt-5.6-luna`), with real tools on the ask path — §6a. | `luna ask "what's my kernel version"` runs the command instead of declining. |
| — | **DONE.** Sight (`luna look`, the focused-window context line) and self-dispatch (she runs `luna dispatch` on herself and the result comes back as memory) — §6, §6b. | `luna look "what's on screen"` describes the focused window; ask her to delegate something and the finding surfaces unprompted later. |
| P3 | **DONE.** Bar widget (§3), ambient hooks — crash/battery/update, §7c — semantic recall (§4) and the `SUPER+F10` hush keybind. | Crash a process, she explains it unprompted (only if `[ambient] crash` is turned on — Omarchy's own watcher covers it by default). |
| — | **Not built.** Worker fan-out as something Luna *plans* rather than something the admission gate merely allows; the `[ambient]` keys have no pane yet in the Jarvis GUI. | See `docs/STATE-OF-PLAY.md` §Next. |

Each phase is independently useful and independently revertible.
