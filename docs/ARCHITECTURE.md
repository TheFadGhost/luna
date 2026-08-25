# Luna — architecture

A resident personal assistant for Omarchy. Not a chatbot: a supervised daemon
with a voice, a memory that compounds, and the run of the desktop.

Status: DESIGN. Nothing here is built yet.

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
          foot + codex/claude, full autonomy
                      |
                      v
              Sol / worker agents
```

## 2. Processes and their budget

| Process | Type | Resident RAM | Notes |
|---|---|---|---|
| `lunad` | Python, systemd user unit | ~40 MB | Supervisor. Owns socket, queue, memory, state. Never calls an LLM directly for long jobs. |
| `luna-speak` | piper (venv), lazy + idle-unload | ~330 MB while loaded, 0 when idle | See measured numbers below. Estimate of ~60 MB was WRONG. |
| embeddings | sentence-transformers / ONNX | ~90 MB | Loaded lazily, unloaded after 5 min idle. |
| voxtype | already running | ~208 MB | Untouched. We only add a post_process hook. |
| agent session | foot + codex | ~150-300 MB | Transient, only while working. |

## 3. IPC

Single Unix socket: `$XDG_RUNTIME_DIR/luna/luna.sock`, newline-delimited JSON.

Every surface (voice, palette, hooks, CLI, bar widget) is a client. The daemon
is the only thing that touches memory or spawns agents. This is what stops the
system becoming four half-integrations that each grew their own state.

Requests: `ask`, `say`, `listen_start`, `status`, `memory.{read,write,search}`,
`cancel`, `subscribe` (for the bar widget's live state).

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

Keybind: a new bind runs `voxtype record toggle --profile luna`.
**F9 and SUPER+CTRL+X keep working exactly as today** - no shared mode flag, no
race, no fork of voxtype. This is the requirement that plain dictation must not
regress, and profiles satisfy it cleanly.

Gotchas (confirmed from the binary, not assumed):
- The transcript arrives on **stdin**; the router's **stdout replaces it**.
- On non-zero exit, spawn failure, or timeout, voxtype **falls back to typing
  the original transcript**. For Luna that means a crashed router types your
  speech into whatever window has focus. The router must therefore be
  defensive: exit 0 and print nothing on any internal error, and hand off to
  `lunad` asynchronously rather than blocking on the reply.
- Empty stdout does NOT reliably suppress output - there is a
  `fallback_on_empty` field governing it. Must be tested on the real config.
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

**Out** — `luna-speak`, a persistent piper process. Sentence-streamed so speech
begins on the first full sentence rather than after the whole reply.
Spoken output is deliberately short; detail goes to screen.

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

## 6. Delegation — Luna leads, Sol specialises

- **Luna** is the only one the user addresses. Conversational, opinionated,
  budget-aware. She triages and decides who does the work.
- **Sol** is a specialist she enrols for deep technical work: own system prompt,
  own skill set, own memory namespace, reports back to Luna not to the user.
- **Workers** are anonymous, parallel, disposable. Fan-out grunt work.

Luna announces who she enrolled and why, in one line. She does not delegate
what she could finish in one step.

## 7. Safety under full autonomy

No permission prompts, by choice. Instead:
- **Audit log** — `~/.local/share/luna/audit.jsonl`, append-only, every command
  she runs with cwd, exit code, and what she was trying to achieve.
- **Undo journal** — reversible actions record their inverse where one exists.
- **Session firewall** — Luna refuses to signal PIDs she did not spawn, and
  never restarts omarchy-shell. Both are recorded dead-ends in CUSTOMISATIONS.md.
- **The five** — wiping disks, force-pushing over history, deleting the
  customisations log, `rm -rf` outside her own dirs, touching another agent's
  session: stated risk, second explicit instruction required. Judgement, not a prompt.

## 8. Build phases

| Phase | Ships | Verifiable by |
|---|---|---|
| P0 | `lunad` + socket + CLI + memory tiers 1-2 + persona. Text only. | `luna ask "..."` returns an opinionated answer that cites remembered context. |
| P1 | piper TTS out, voxtype routing in. | Hold F9, speak, she answers aloud. Plain dictation still types. |
| P2 | Workspace dispatch + Sol + audit log. | "go build X" -> works in special workspace, reports back. |
| P3 | Bar widget, ambient hooks (crash/battery/update), semantic recall + decay. | Crash a process, she explains it unprompted. |

Each phase is independently useful and independently revertible.
