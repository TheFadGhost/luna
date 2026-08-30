# Luna — architecture

A resident personal assistant for Omarchy. Not a chatbot: a supervised daemon
with a voice, a memory that compounds, and the run of the desktop.

Status: Phases 0, 1 and 2 built and running. Phase 3 is design.
Where reality contradicted the design, the design text has been corrected
in place and the correction is marked **CORRECTED**.

---

## 0. Constraints that shaped this

| Constraint | Consequence |
|---|---|
| Ryzen 5 4500U, 7.1 GB RAM, no GPU/NPU | No local LLM as the brain. Cloud agent does the thinking. Only small models run locally (STT ~200 MB, TTS ~60 MB, embeddings ~90 MB). |
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
| embeddings | sentence-transformers / ONNX | ~90 MB | Loaded lazily, unloaded after 5 min idle. |
| voxtype | already running | ~208 MB | Untouched. We only add a post_process hook. |
| agent session | foot + claude or codex | ~150-300 MB | Transient, only while working. Which CLI comes from `defaults/agent`; the runner script is written by that agent's adapter (section 6a). |

## 3. IPC

Single Unix socket: `$XDG_RUNTIME_DIR/luna/luna.sock`, newline-delimited JSON.

Every surface (voice, palette, hooks, CLI, bar widget) is a client. The daemon
is the only thing that touches memory or spawns agents. This is what stops the
system becoming four half-integrations that each grew their own state.

Requests, as built: `ping`, `ask`, `say`, `speak.cancel`, `session.reset`,
`status`, `memory.{read,write,search}`, `dispatch`, `jobs`, `peek`, `audit`,
`spawned`, `cancel`, `shutdown`. `memory.read`/`memory.write` take an optional
`namespace` (`luna` or `sol`). `subscribe`
(for the bar widget's live state) is Phase 3. `listen_start` was dropped: the
keybind talks to voxtype directly, so the daemon never needs to start a
recording.

`ask` takes `detach: true`, which acknowledges immediately and answers on a
background thread. That exists for the voice router, which runs inside
voxtype's `post_process_timeout_ms` and cannot wait for a reply it is not
going to print.

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

### Tier 2 — episodic (searched on demand)
`~/.local/share/luna/memory/episodes.db` — SQLite.
- `episodes` table: every exchange, with timestamp, surface, salience score.
- FTS5 index for keyword recall ("what did we decide about the bar widget").
- `sqlite-vec` + a local 90 MB embedding model for semantic recall.

**This is our addition, not Hermes'.** Hermes has no native semantic retrieval;
it outsources that to Honcho (Postgres + pgvector + a second LLM + a paid
service). That is not viable here, and a local embedding index is strictly
better than nothing.

### Tier 3 — derived profile
`~/.local/share/luna/memory/profile.json` — regenerated periodically from
Tier 2, never hand-written. Working style, recurring frustrations, vocabulary,
what the user says they'll do vs what they do.

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

### Consolidation nudge
After N turns, a background pass on a cheap model reviews recent episodes and
proposes writes to Tier 1. Runs off the critical path; never blocks a reply.

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

The default voice is now **`flux-sienna-en`** (female) through
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
| OpenRouter `flux-sienna-en` | 1.5–1.8 s | 2.3–2.9 s |

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
- **Workers** are anonymous, disposable. Fan-out grunt work. Parallel fan-out
  is not built: `dispatch` is one job per call. Nothing stops several being in
  flight, but Luna does not plan a fan-out for you.

Luna announces who she enrolled and why, in one line. The line is composed in
`dispatch.py`, not asked of a model — an announcement that cost four seconds
and an API call would not get made. She does not delegate what she could finish
in one step.

### How a job actually runs — BUILT, and not as designed

`luna dispatch "..."` writes a job directory under
`~/.local/share/luna/jobs/<id>/` (`task.txt`, `system.txt`, `run.sh`,
`output.txt`, `stderr.txt`, `exit`, `job.json`), spawns `foot` running
`run.sh`, and watches the pid. `run.sh` runs the configured agent with
`--permission-mode bypassPermissions --tools default --safe-mode`, wrapped in
`timeout`, piped through `tee`. `luna jobs` lists them newest first, from disk,
so the list survives a daemon restart; `luna peek` toggles the workspace.

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
| tool policy | `--tools ""` | the sandbox: `-s read-only` |
| user config off | `--safe-mode` | `--ignore-user-config --ignore-rules` |
| session id | caller chooses, `--session-id` | codex assigns, read from `thread.started` |
| cost | dollars, metered | none — ChatGPT subscription |

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

### Audit log — `lunad/audit.py`

`~/.local/share/luna/audit.jsonl`. Opened `"a"`, fsync'd per line, never
truncated, never rotated. A log Luna can rewrite is not evidence. Every
dispatch, spawn, signal, refusal and memory write, with `why` (the intent, not
the mechanics), the outcome, and the exit status. `luna audit [--since 30m]`
reads it back newest first.

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
A dispatched session runs with `bypassPermissions` and real tools. It could
write anywhere the user can. Sol's namespace isolation is enforced in `lunad`'s
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

## 8. Build phases

| Phase | Ships | Verifiable by |
|---|---|---|
| P0 | `lunad` + socket + CLI + memory tiers 1-2 + persona. Text only. | `luna ask "..."` returns an opinionated answer that cites remembered context. |
| P1 | **DONE.** piper TTS out, voxtype routing in, session reuse, cost fix. | F10, speak, she answers aloud. Plain dictation (F9) still types — regression-tested. |
| P2 | **DONE.** Workspace dispatch + Sol + audit log + PID firewall. | `luna dispatch "..."` runs in the `luna` special workspace and reports back; `luna spawned --check <foreign pid>` refuses. |
| P2b | **DONE.** Jarvis: config file + hot reload, OpenRouter TTS with piper fallback, confirmation policy, name as a setting. | Edit `~/.config/jarvis/config.toml`, do not restart, hear the change. |
| P3 | Bar widget, ambient hooks (crash/battery/update), semantic recall + decay. | Crash a process, she explains it unprompted. |

Each phase is independently useful and independently revertible.
