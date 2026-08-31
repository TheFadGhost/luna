# State of play — 2026-08-31

## Done and verified

### Phase 0
- **`lunad` daemon** running under systemd (~13 MB idle), Unix socket, NDJSON
  protocol, CLI client.
- **Memory tiers 1-2.** Cap rejection verified byte-for-byte: an oversized write
  exits 1, prints what must be consolidated, and leaves the file untouched.
  FTS5 search returns real episodes. Salience scored, decay applied at read.
- **Persona live and non-sycophantic**, verified adversarially.
- **Tier-1 memory seeded** with the two facts she got wrong unaided.
- Committed: `c894ba9`, `7682ef0`.

### Phase 1
- **Speech out.** `lunad/speech.py` + `lunad/piper_worker.py`: lazy piper under
  the project venv, framed audio protocol, one `aplay` per utterance,
  sentence-streamed, barge-in by PID, five-minute idle unload (observed in the
  log as `unloading piper reason=idle 303s`). Measured warm: **first audio at
  45 ms** for a 14.3 s reply; cold model load 1.2-1.5 s.
- **Unspeakable-content stripper.** Code blocks, paths, URLs, e-mails, hashes
  and long digit runs collapse to "it's on screen"; adjacent placeholders
  collapse; the whole thing truncates at a sentence boundary.
- **Voice in.** `bin/luna-voice-router` behind `[profiles.luna]` in voxtype.
  Measured hand-off 30 ms against a 2000 ms budget. Full loop verified end to
  end from a real microphone: spoken question -> transcript -> router -> Luna
  answered in 4.4 s -> spoken aloud.
- **Keybind** F10, verified free and verified registered. Sits next to F9
  dictation on purpose: F9 types what you say, F10 sends it to Luna.
- **Plain dictation regression-tested and intact.** Recorded through the
  unprofiled path into a scratch window: 94 bytes typed, no profile override, no
  post-processing. The voxtype config backup is a byte-identical prefix of the
  live file (1144 bytes appended, 0 changed).
- **Cost fixed, and the Phase 0 diagnosis corrected** — see below.
- 157 tests pass (`python3 -m unittest discover`), up from 84.


### OSD theming (2026-08-25)
- **voxtype's on-screen voice indicator now matches the monochrome desktop.**
  Screenshot: `docs/osd-monochrome.png`.
- **The default GTK4 OSD is a dead end** — `voxtype-osd-gtk4` draws its waveform
  in Cairo from colour constants compiled into a stripped Rust binary, loads no
  CSS and has no styled widget tree. It was swapped out, not restyled:
  `[osd] frontend = "quickshell"` makes the daemon launch
  `voxtype-osd-quickshell` instead.
- **The QML lives in a user-owned tree**, `~/.local/share/voxtype/quickshell/`,
  installed with `voxtype setup quickshell --skip-bridge`. Nothing under
  `/usr/share/voxtype` or `/usr/lib/voxtype` was touched — `pacman -Qkk
  voxtype-bin` reports `69 total files, 0 altered files`. Master copies of the
  two edited QML files are in `osd/`, because
  `voxtype setup quickshell --force` would overwrite the live ones.
- **It reads Omarchy's real design tokens, not copied hexes.** `Commons` and
  `Ui` are symlinked into that tree, so `import qs.Commons` resolves inside
  voxtype's *own* Quickshell process, and every colour and dimension is bound to
  `Color.*` / `Style.*`. The card is `Ui.BorderSurface`, not a hand-rolled
  Rectangle. `Theme.refresh()` re-reads `colors.toml` every time the OSD comes
  up, so a theme switch is picked up on the next recording — no file watcher,
  which matters because `omarchy theme-set` `rm -rf`s the theme directory.
- **Dictation re-verified end to end after the switch**: recording → transcript
  → typed into the focused window via `wtype`. `voxtype.service` was restarted
  once (config is only read at startup).

### Phase 2
- **Workspace dispatch.** `luna dispatch "..."` writes a job directory, spawns
  `foot` (app-id `org.omarchy.luna`) running the agent under
  `bypassPermissions --tools default --safe-mode`, and reports back. Verified
  end to end: `create /tmp/luna-p2-proof.txt containing the current date` →
  job `94c1c5d5`, exit 0 in 14.2 s, file on disk reading
  `Tue Aug 25 11:13:18 PM GMT 2026`.
- **The PID firewall.** `lunad/safety.py`, one gate, start-time checked against
  `/proc/<pid>/stat` field 22. Proved against real pids while a job ran:
  `may_signal(1470712)` (the voxtype daemon) → **False**, "Luna did not spawn
  it", and `signal_pid` on it raised `SignalRefused` with voxtype still alive;
  `may_signal(1473361)` (a job Luna spawned) → **True**.
  `grep -rn 'pkill|os.kill|killpg|terminate()' lunad/ bin/` finds calls in
  `safety.py` only; every other hit is prose. The Phase 1 speech barge-in and
  the agent timeout now route through it.
- **Audit log.** `~/.local/share/luna/audit.jsonl`, append-only, fsync per
  line, `luna audit --since 30m`. Dispatches, spawns, signals, refusals and
  memory writes, each with `why` and outcome. Undo is recorded only where an
  inverse really exists.
- **Sol.** `data/sol-persona.md`, own system prompt, own namespace
  (`memory/sol/SOL.md` + its own episode store). `SolMemory.file("LUNA.md")`
  raises. Verified live: job `615f6bcc`, `--to sol`, returned a report in the
  specified Finding / Evidence / cost / what-I-did-not-check shape.
- **`luna peek` / `luna jobs` / `luna spawned`.** Peek toggles
  `special:luna` (confirmed via `monitors[].specialWorkspace`); jobs lists from
  disk so it survives a daemon restart.
- **No speech regression from routing the barge-in through the firewall.**
  The ledger write costs 0.41 ms (whole `safety.spawn`: 0.66 ms median), and
  first audio for a reply with a short opening sentence measured 87 / 40 / 45 ms
  warm — the same 45 ms as Phase 1. A long *first* sentence measures 660-760 ms,
  but that is piper synthesising it, not the firewall.
- 288 tests pass, up from 157.

## The Hyprland finding, in full

Omarchy's Hyprland 0.56.2 takes a **Lua** config and `hyprctl` evaluates its
arguments as Lua. The bracket exec syntax is therefore a Lua parse error, not a
Hyprland one:

```
$ hyprctl dispatch exec "[float] echo hi"
error: [string "return hl.dispatch(exec [float] echo hi)"]:1: ')' expected near 'echo'
```

`--`, quoting and `--instance` change nothing; `hyprctl keyword` answers
`keyword can't work with non-legacy parsers. Use eval.` What works is the
dispatcher's own Lua function — `hl.dsp.exec_cmd("[workspace special:luna
silent] cmd")`, `hl.dsp.workspace.toggle_special("luna")` — and, for the
runtime window rule, `hl.window_rule({ match = {...}, workspace = "..." })`,
whose key names came from `/usr/share/omarchy/default/hypr/helpers.lua` because
it accepts any table without complaint.

Dispatch does not use `exec_cmd`: Luna must own the pid, so she `Popen`s `foot`
herself and places the window with a rule.

## The cost finding, in full

The Phase 0 note blamed ~$0.05/ask on "separate processes never share a prompt
cache". **That was wrong.** The cache is keyed on the prompt prefix and is
shared across processes — a brand-new `claude -p --session-id <fresh uuid>` was
measured taking a 4510-token cache *read*.

The real fault: tier-2 recall was appended to the end of the system prompt.
Recall changes every request, so every ask invalidated the whole ~5.5k-token
cached prefix and paid to re-create it. Recall now rides in the user message.

| | mean cost/ask | n |
|---|---|---|
| before (Phase 0 daemon, real asks) | **$0.0513** | 7 |
| after (Phase 1 daemon) | **$0.0096** | 13 |

Session reuse (`--session-id` / `--resume`) is implemented and on by default,
but its own effect is **within noise** ($0.0232 vs $0.0289 over three turns):
resuming replays and re-caches a growing history, which cancels most of what it
saves. It is kept for conversational continuity, not for money.

## Running
- `lunad.service` — enabled, active.
- `voxtype.service` — active. **Restarted once**, which was unavoidable: the
  voxtype daemon only reads its config at startup, so `[profiles.luna]` was
  invisible until it was restarted. Verified healthy and plain dictation
  verified working afterwards.
- omarchy-shell and other agent sessions: untouched. Phase 2 restarted
  `lunad` only. (`voxtype` was restarted several times during the same window
  by the concurrent OSD session, not by this work.)
- **voxtype OSD**: `qs -p ~/.local/share/voxtype/quickshell` spawned by the
  voxtype daemon, plus its `voxtype-audio-bridge` child. Layer surface
  `voxtype-osd` on the overlay layer, visible only while not idle.

## Next
Everything that was on this list under the "Phase 3" heading is now built:
the bar widget, all three ambient hooks, semantic recall and the `SUPER+F10`
hush keybind — see the phase sections below for each. What is genuinely still
unbuilt:

1. **Worker fan-out as a *plan*.** The admission gate and the queue are built,
   so several jobs can be in flight and the number is bounded, but Luna still
   does not decide on her own to split a task across workers. That is a
   planning change in the persona and the ask path, not plumbing.

**CORRECTED:** the second item that used to sit here — no Jarvis GUI pane for
the `[ambient]` table, and the `jarvis-settings` suite red because of it — is
done; see "Persona: the gate is on action, not manner" and "Jarvis — typeset,
not boxed" below, and "Verify, and what is currently broken" for the counts.

Tier 3, the consolidation pass, the admission gate and the job GC were all on
this list. They are done — see Phase 2d and Phase 2e below.

### Phase 3 — presence, and the bar widget (2026-08-30)

- **`lunad/presence.py`.** The daemon publishes one bare ASCII word to
  `$XDG_RUNTIME_DIR/luna/state` — `idle`, `thinking`, `speaking` — atomically,
  on every transition, and removes the file when it stops. Absence means she
  is not running.
- **`subscribe` is dropped, not deferred.** A subscriber is a socket with a
  buffer a stalled reader can fill, and the thread that would block on it is
  the thread that was about to speak. A file has no reader-side backpressure,
  costs the widget nothing (Quickshell's `FileView` is inotify), and is
  already how voxtype publishes its own state on this machine. Recorded in
  ARCHITECTURE.md §3.
- **`Speech` reports its own transitions** through a new optional
  `on_activity` callback, fired when an utterance starts, ends or is
  cancelled. The daemon derives the published word from
  `speech.speaking` and `len(self.runs)` rather than assigning it, so no
  caller has to get the ordering right.
- **`listening` is not lunad's to publish.** voxtype owns the microphone and
  the daemon does not hear about a voice turn until the transcript arrives.
  The bar module composes lunad's file with voxtype's own.
- **Two bugs found and fixed on the way, both only visible against a real
  process:**
  - `Daemon.close()` cleared the state file and then cancelled speech, which
    fired the activity callback, which republished `idle` — so every clean
    stop left the bar showing a daemon that had exited. `Presence.clear()` is
    now final.
  - `serve()` constructed `LunaServer` outside the `try`, so a daemon that
    came up and then failed to bind its socket was never closed. It now is.
- The widget itself lives on the machine, not in this repository:
  `~/.config/omarchy/bar/modules/luna.qml`, documented in
  `~/.config/omarchy/CUSTOMISATIONS.md` §8a.12.

### Phase 2b — the codex adapter (2026-08-26)

- `CodexAdapter` is real, verified live against codex-cli 0.149.1. The stub is
  gone.
- **codex has no `--append-system-prompt` and no `--system-prompt`.** The
  persona travels as a config override, `-c developer_instructions=<persona>`.
  Proven, not assumed: with it, "what are you?" answers *"I'm Luna, your
  resident assistant on this Omarchy Linux desktop"*; without it, *"I'm Codex,
  an AI coding agent"*. Asked to rewrite the Quickshell bar in React Native
  overnight, it pushed back on the layer-shell problem and offered QML instead
  — the same refusal Claude gives.
- `-c instructions=` was measured against it and rejected: same persona
  capture, 5,422 prompt tokens against 4,874, and it *replaces* codex's base
  instructions rather than adding to them, which would cost a dispatched
  session its tool and patch guidance. `config.CODEX_PERSONA_KEY` switches
  between the two in one line.
- Output is parsed from `--json` (JSONL), with `-o/--output-last-message` kept
  as an independent second witness for the reply text.
- Sandbox: `ask` runs `-s read-only`, `dispatch` runs
  `--dangerously-bypass-approvals-and-sandbox`. Both in `config.py`.
- Sessions resume through `codex exec resume <thread-id>`. codex assigns the
  id, so turn one has none and `SessionManager` adopts what comes back.
- `luna codex-profile` writes `~/.codex/luna.config.toml` so the user's own
  `codex -p luna` boots as Luna. It is a separate profile-v2 file; the user's
  `~/.codex/config.toml` is never opened, and plain `codex` is unchanged.
- `dispatch` no longer hard-codes claude's flags: the runner script asks the
  adapter for its own command line.
- 325 tests pass, up from 288.

### Phase 2b — Jarvis: config, TTS and confirmations (2026-08-26)
- **The app is Jarvis; her name is a setting.** `[assistant] name`, default
  `Luna`. Nothing in the new code writes "Luna" literally — the system prompt,
  the worker and specialist prompts, the notification headline and the log
  labels all read it from settings. Renaming her retires the warm sessions,
  because her name is part of the cacheable prefix.
- **`~/.config/jarvis/config.toml`**, 0600 in a 0700 directory, exactly the
  schema in `docs/CONFIG-SCHEMA.md`. Read with `tomllib`; written by a
  hand-rolled serialiser that carries the schema's own comments, because there
  is no TOML writer in the stdlib and a comment-less config file is a worse
  config file. A test parses the document and asserts the module still agrees
  with it, key for key and default for default.
- **Hot reload works and is verified**: edit the file, wait two seconds, the
  next `say` uses the new voice. Same daemon pid throughout. Every reload logs
  a diff and lands in the audit log.
- **An invalid value warns and falls back**; `settings.set` over the socket
  refuses instead. Ops `settings.get` / `settings.set` also hand back the whole
  schema, so the settings GUI can build itself from the daemon.
- **OpenRouter TTS**: `deepgram/flux-tts:free`, default voice `flux-alexis-en`,
  alternate `flux-donovan-en`. WAV parsed chunk by chunk, PCM fed to the same
  single `aplay`, one request per sentence with one sentence of look-ahead.
- **piper is still there and still the fallback.** Verified by pointing
  `[voice] model` at a model that does not exist: HTTP 400, one warning in the
  log, and the sentence came out of piper at 22 050 Hz instead of 24 000.
- **Confirmations**: eight policy classes from the schema, each `never` | `ask`
  | `deny`, plus four hard denies that the config cannot re-enable. `ask` puts
  an Omarchy toast on screen whose click action approves; silence is a no.
- **The confirmation is real on the dispatch path and advisory inside a job.**
  Verified end to end: `luna dispatch "delete /tmp/jarvis-proof.txt"` blocked
  before forking anything, the toast fired, `luna confirm yes` released it —
  and then the dispatched agent *itself* ran `luna confirm ask delete_files`
  before the `rm`, which needed a second approval. Both are in the audit log.
- **Secrets never touch config.toml.** `~/.config/jarvis/secrets.env` 0600, fed
  to lunad by `lunad.service.d/10-jarvis-secrets.conf`. The drop-in also reads
  voxtype's file, so the key already on this machine keeps working and nothing
  had to be copied out of another program's directory.
- `luna` and `jarvis` are both on PATH (`~/.local/bin`, symlinks to
  `bin/luna`); `bin/jarvis` is a symlink in the repo.
- 447 tests pass, up from 325.

### Phase 2c — making the config contract true (2026-08-27)

`docs/CONFIG-SCHEMA.md` called itself the contract and the GUI wrote the whole
of it, but the daemon only ever *read* `[assistant]`, `[voice]` and
`[confirm]`. Everything else was accepted, stored, round-tripped and displayed
while hard-coded constants decided the behaviour. A settings app that lies is
worse than one with fewer settings, so the rest is wired, and the handful that
cannot be wired without building a subsystem is now named in the document
instead of being quietly stubbed.

**Wired this pass** — all live, none needing a restart:

- **`[voice] piper_voice`** picks the local ONNX. A worker already holding a
  different model is unloaded and reloaded: one piper worker is one model and
  cannot be re-pointed, so ignoring the change would have been the only other
  option.
- **`[voice] speed`** reaches both providers in each one's own units — piper
  takes `length_scale = 1/speed` on the synthesis request, OpenRouter takes
  `speed` in the request body. Sent to OpenRouter **only when it is not 1.0**:
  not every model behind it implements the field, and a 400 on an unknown key
  for the default value would have broken speech for everyone who never touched
  the setting. Off the default a rejection falls back to piper, which honours
  the speed anyway.
- **`[memory] luna_cap_chars` / `user_cap_chars`** are now the caps
  `Tier1File` enforces, resolved per read rather than frozen at construction.
  Lowering one below the current contents still rejects rather than truncates —
  that is the tier-1 contract and it did not change.
- **`[memory] decay_half_life_days`** is the half-life recall uses. Decay is
  applied at read time, so a change reaches the next recall.
- **`[dispatch] workspace` / `app_id`** place the window. The Lua guard that
  stops window rules stacking is now keyed on a digest of the pair rather than
  a fixed name — with a fixed name the *old* rule's guard would have suppressed
  installing the new one, and every job after a change would have opened on the
  active workspace, visibly, until the next Hyprland config reload. Windows
  already open keep their app-id: it is set at map time and cannot be changed.
- **`[ui] notify_on_finish`** puts a toast up when a dispatched job ends. The
  job's window is hidden by design, so without it the only way to find out was
  to go and look. Failures are `critical` and carry the exit code; a missing
  `omarchy-notification-send` is logged and swallowed.
- **`[ui] theme_follows_omarchy`** is honoured by the Jarvis GTK app, which is
  the only process that draws anything. Off, the built-in monochrome palette is
  pinned and `colors.toml` is never opened. The geometry tokens are not part of
  the switch — they mirror Hyprland's own rounding and border, and a window
  that stops matching every other window is not a theme choice.

**Two mismatches resolved, deliberately, both toward the documented value:**

- **`max_spoken_chars`: 400.** `ARCHITECTURE.md` §5, `CONFIG-SCHEMA.md`, the
  GUI and the user's own `config.toml` all said 400; only
  `config.SPEECH_MAX_CHARS` said 700, and it only ever surfaced when the config
  file was missing entirely. The odd one out lost.
- **`decay_half_life_days`: 30.** Same shape — 30 everywhere a human could see,
  14 in the constant that actually won. Nothing user-facing ever said 14. On
  the merits, too: half-life is a *ranking* lift on recall, not a delete, and
  the salience score already carries a recency term of its own; a fortnight on
  top of that sinks a month-old episode below anything said this week, which is
  how a tier-2 store stops being worth searching. The user's live
  `config.toml` already held 30, so nothing they had set was changed.

`tests/test_contract.py::DriftCase` now fails if any schema default and its
fallback constant disagree again — that is the drift both mismatches were.

**The gotcha this pass produced, and the class of bug behind it.** Wiring
`[ui] notify_on_finish` put roughly ten *real* Omarchy toasts on the user's
live desktop in a single suite run, carrying test fixture text — "Luna: job
53b9da29 failed — exit 2 — read the README", "exit 2 — rm -f
/tmp/jarvis-test". The mechanism is worth writing down because it is the same
one that opened three `foot` windows a run a day earlier, in a sibling
parameter nobody thought to check:

> A binary name bound as a **signature default** — `def __init__(self,
> notify_bin: str = config.NOTIFY_BIN)` — is evaluated once, at import.
> `tests/_support.py` can set `config.NOTIFY_BIN` to a sentinel all it likes;
> the constructor was already holding the real name and will hand it out
> forever. Stubbing the *object* is not enough, because the real program is
> reached anyway.

It fired here because five test modules legitimately pass `terminal="/bin/bash"`
so a job runs headlessly — and the job then genuinely *finishes*, which is
precisely when the new notifier ran.

Fixed as a class, not as two instances. Every `config` name in `lunad/` that
reaches the outside world is now read late, in the constructor body, and
replaced process-wide by `tests/_support.py` with something that cannot
resolve:

| name | what it would do to the machine running the suite |
|---|---|
| `TERMINAL_BIN` | opens a window on the user's desktop |
| `NOTIFY_BIN` | puts a toast on the user's desktop |
| `APLAY_BIN` | plays audio out of the user's speakers |
| `HYPRCTL_BIN` | installs window rules and moves the user's workspaces |
| `VENV_PYTHON` | forks a real 331 MB piper worker |

`tests/test_guards.py` is new and asserts the whole arrangement — that each
sentinel is installed, that none of them can actually resolve, that an explicit
argument still wins (the five modules that need `/bin/bash` must keep working),
and, by `inspect.signature`, that **none of these parameters may go back to
being a signature default**. There had been no test for the terminal guard at
all, which is why the same bug landed twice.

**Corrections to the premise this pass started from.** `assistant.name`,
`assistant.specialist`, `voice.enabled` and `voice.max_spoken_chars` were
*already* wired, not inert: the name and the specialist reach every prompt,
toast and log label through `settings_mod.assistant_name()`, and the voice
settings are read per utterance in `speech._voice_settings()`. What was wrong
with `max_spoken_chars` was only its fallback constant, not its wiring.

**Deliberately not wired**, and documented as such rather than stubbed:
`[memory] consolidate_every_turns`, `[dispatch] max_parallel`,
`[dispatch] job_retention_days`, and the whole of `[listen]`. Each needs a
subsystem that does not exist, or belongs to another process; see
`docs/CONFIG-SCHEMA.md` §Not wired for what each one actually requires.
(All four were wired in the end: `consolidate_every_turns` in Phase 2d, the
two `[dispatch]` keys in Phase 2e and `[listen]` in Phase 2f, all below.
`[listen]` is still voxtype's — it is written *through* to voxtype's own file
rather than read by `lunad`.)

- 484 tests pass in the root suite, up from 447; 59 in `jarvis-settings`, up
  from 53. Verified on the final run: no window opened, no toast fired
  (`journalctl --user` clean), no `/tmp/luna-test-*` left behind, and no core
  dump produced.

### Phase 2e — the admission gate, the job GC and audit rotation (2026-08-30)

The last two "honoured by nothing" `[dispatch]` keys, plus the unbounded audit
log. All three were documented as pending with a note on what each would need;
what follows is what each actually needed, and the answers that were not
obvious from the outside.

**`[dispatch] max_parallel` — a queue, not a refusal.** A dispatch over the
limit is *accepted*: it gets its id, its directory and its composed prompt at
once, lands in `jobs/` as `queued`, shows in `luna jobs` as `wait`, and can be
cancelled there. The only thing it lacks is a pid. Three decisions inside that:

- **FIFO even when a slot is free.** A newcomer arriving while the queue drains
  must not overtake jobs already waiting, or the queue is a lottery.
- **The limit is read at every admission decision, never captured.** Lowering
  it below the number of running jobs kills nothing — it stops admitting and
  the count drains. Raising it releases waiting work immediately, because
  `Daemon._settings_changed` pokes `admit_ready()` rather than waiting for the
  next job to end; without that the setting would look inert until something
  else happened to move.
- **A queued job is dropped on shutdown, not left promised.** The queue only
  ever existed in one process's memory. Each is written back as `cancelled`
  with the reason, and a `queued` directory found without a live daemon reads
  as `orphaned`, not as something still waiting.

**The shutdown drain had to be tightened, and CI is what caught it.** `close()`
sets its closing flag before draining, so a job finishing mid-shutdown cannot
admit a fresh one — but a watcher already *inside* `_admit_next` when the flag
went up wins that race and starts one more job, and therefore one more watcher,
appended after the drain had taken its snapshot. The snapshot loop then never
joined it; it wrote its `dispatch.finish` into a temporary tree teardown had
already deleted, and `AuditLog._emit`'s `mkdir(parents=True)` **recreated the
tree** — one empty `/tmp/luna-test-*`, which is exactly what the CI step added
after the `foot`-windows incident looks for. It never reproduced on this
machine, not in eight runs and not with six suites running at once; only on the
slower runner, and there on three of the four Python versions. The drain now
re-reads `self._watchers` each pass rather than joining one snapshot, still
bounded by the same deadline. One test was also leaving two jobs mid-flight at
teardown, which is how the race got a window that wide.

**`[dispatch] job_retention_days` — a GC pass, with the policy written down.**
Aged from when the job *stopped* (`finished`, then `started`, then the
manifest's mtime), because retention is how long the *record* is kept and a
six-hour job's record begins when it ends. Nothing running or queued is
collected at any age; an orphan is, once past the window, because it will never
finish and one crash should not pin a directory for the life of the machine.
Six-hour timer in its own thread plus one pass on the way up — a laptop that is
suspended and resumed daily would never reach a first tick otherwise — and
never on the request path. One audit entry per deletion, with no `undo`,
because there is not one.

**Zero means never, in both new places.** `job_retention_days = 0` collects
nothing and `[audit] max_mb = 0` never rotates. Read as a duration, "0 days"
looks like "keep nothing", which is the one thing a user typing the smallest
number they are allowed cannot want; and there would otherwise be no way to say
"keep everything" at all. Both have an explicit test.

**`audit.jsonl` rotation, without losing anything silently.** The old note
("never rotated, on purpose") was right about the risk and wrong about the
remedy. Rotation now *moves* bytes: past `[audit] max_mb` the live file becomes
`audit.jsonl.1`, siblings shift up, and only the oldest of `[audit] keep` is
deleted — and that deletion is itself an entry, `audit.rotated`, written as the
first line of the new live file naming what was renamed and what was dropped.
The chain therefore reads backwards from the live file and any gap in it
explains itself. `luna audit` reads the siblings too, stopping at the first
file that cannot hold anything the query asked for, so `-n 40` does not read
months of history. Two details worth keeping:

- The size check runs **after** the write, on the position `tell()` already
  returned, so it costs no extra syscall and can never land between a line and
  its `fsync`. A rotated sibling is one line *past* the ceiling, never short.
- Lowering `keep` leaves the files beyond it on disk. They are still *read* —
  history you stopped rotating is not history that stopped existing — and
  nothing deletes them for you.

**One guard added, for the obvious reason.** `Dispatcher(jobs_dir=...)` was a
signature default bound to `config.JOBS_DIR`, which was harmless while the
dispatcher only *created* directories there. A collector makes it the thing
that deletes the user's job records, so `jobs_dir` joins the family in
`tests/_support.py`: resolved late, and pointed at a throwaway tree for the
test process. It is the one guard in that family that has to *resolve* rather
than fail — `config.ensure_dirs()` creates it — so `test_guards.py` asserts it
is not under `$HOME` instead.

- 610 tests pass in the root suite, up from 567; 59 in `jarvis-settings`,
  unchanged. Verified across three consecutive runs: no window, no toast, no
  core dump, no stray `/tmp/luna-test-*` or `/tmp/luna-tests-jobs-*`, and the
  live daemon was not restarted or signalled.

## Known limitations / stubbed
- **The confirmation system cannot see inside a running job.** It is enforced
  on `lunad`'s own dispatch path — nothing forks until the task text has passed
  the classifier — and it is *advisory* thereafter: a dispatched session runs
  with `bypassPermissions` and real tools, and the daemon never sees its
  individual tool calls. Layer 3 (the agent calling `luna confirm ask` itself)
  worked on the first real test, but it works because the agent chose to
  co-operate, not because anything stopped it.
- **The classifier reads the task text, so vague phrasing evades it.** "Tidy up
  the old build output" does not classify as `delete_files`; "rm -rf build"
  does. False positives are cheap (one toast); false negatives are the real
  cost and there is no way to drive them to zero from text alone.
- **`long_job` and `spend` never fire on their own.** They need an explicit
  estimate from the caller (`estimate_seconds`, `estimate_usd`). Gating them on
  the dispatch timeout would ask about every job, which trains the user to
  click through.
- **Omarchy's notification has exactly one click action**, so a toast can only
  carry the *yes*. Declining means running `luna confirm no <token>` or letting
  it time out. That is the safe way round, but it does mean an explicit "no"
  is not one click.
- **OpenRouter TTS is 1.4-2.5 s slower to first audio than piper.** Measured,
  warm: piper 96-124 ms short / 341-409 ms for two sentences; OpenRouter
  1.5-1.8 s / 2.3-2.9 s. Better voice, real latency cost. `[voice] provider`
  switches back with no restart.
- **Codex `ask` has real tools now; Claude `ask` does not.** This used to be a
  limitation the other way round — codex's `ask` ran `-s read-only` and the
  persona said outright that she had no tools, which was true and was the
  wrong trade (asked for a version she said she could not check it). Both
  `ask` and `dispatch` now run codex under `--dangerously-bypass-approvals-
  and-sandbox` (ARCHITECTURE.md §6a), and the persona's closing text was
  rewritten to name the shell, files and the web instead of denying them.
  `claude`'s `ask` still passes `--tools ""` and is genuinely text-in/text-out,
  so the asymmetry is now the other way: codex-as-Luna has real tools on every
  turn, claude-as-Luna has none on `ask` and full autonomy only once
  dispatched.
- **Codex reports no per-call price.** It is on a ChatGPT subscription, so
  `cost_usd` is `None` and `billing` is `"subscription"`. The daemon's money
  counter stays at zero on codex, which is correct but means `luna status` is
  not a like-for-like comparison between the two agents.
- **Tier 3 measures; it does not understand.** Pattern extraction has false
  negatives everywhere: a preference told as a story, a fact stated obliquely,
  sarcasm of any kind. It reads only the user's own words, so anything Luna
  inferred and said back is invisible to it. The support count published beside
  every fact is the mitigation, not a fix — a fact seen once is a guess and is
  labelled as one.
- **Tier 3 is not in the prompt.** It is read by the consolidation pass and by
  `luna memory profile`, and by nothing else. Injecting it would either break
  the cacheable prefix on every rebuild or add tokens to every turn.
- **A consolidation pass can decide nothing is worth keeping, repeatedly.**
  That is the correct answer most of the time and it still costs one call.
  `[memory] consolidate_every_turns = 0` is the off switch and it is honoured
  completely — nothing is counted and no call is made.
- **A consolidation write still has no undo, but it can now be read first.**
  It goes through `replace`, which discards text nothing else keeps, and this
  project does not claim inverses it does not have. Two things stand in for
  one: `luna memory consolidate --dry-run` shows the whole proposal before
  anything is applied, and the audit entry of a pass that did run carries the
  verbatim text of everything it removed, so a deletion is recoverable by
  reading the log.
- **`fallback_on_empty` cannot be disabled.** Every Luna recording leaves the
  raw transcript on the clipboard. Harmless, but it does clobber the clipboard
  on every voice turn. If that becomes annoying the only fix is upstream.
- **A voice ask that fails speaks a generic apology**, not the actual error;
  the detail is in `luna log`.
- **A dispatched session is not sandboxed.** It runs full-autonomy and real
  tools — `bypassPermissions` on claude, `--dangerously-bypass-approvals-and-
  sandbox` on codex — so it could write anywhere the user can. Sol's namespace
  isolation is enforced in lunad's memory API and stated in his prompt; it is
  not a filesystem boundary. The audit log is the mitigation.
- **The Hyprland window rule is runtime-only.** It vanishes on a config reload
  and is re-added by the next dispatch. Between the two, a dispatched window
  would open on the active workspace — the job still runs, and `luna jobs`
  says so in its note.
- **`dispatch` does not fan out *by itself*.** One call is one job. Several can
  be in flight and `[dispatch] max_parallel` bounds them, but Luna does not
  plan a fan-out for you.
- **A queued job does not survive the daemon.** It is cancelled on shutdown
  with the reason recorded, rather than left as a promise a restarted daemon
  has no queue to keep. Nothing was spawned, so nothing is lost but the wait.
- **Rotation retains a bounded history**, `[audit] keep` files of
  `[audit] max_mb` — 48 MB by default. Past that the oldest file *is* deleted;
  the deletion is recorded but the bytes are gone. Set `max_mb = 0` to keep the
  old unbounded behaviour.
- **The `[osd]` sizing keys are inert under the Quickshell frontend.**
  `position` / `width_px` / `height_px` / `margin_px` reach only the GTK4 and
  native frontends; the Quickshell OSD's geometry lives in its `Theme.qml`.
  They are set in the config anyway so a fallback frontend lands correctly.
- **The themed OSD depends on `/usr/share/omarchy/shell/{Commons,Ui}`.** An
  `omarchy update` that renames a token would break it, and the failure mode
  is a QML import error at OSD launch, not at dictation time.
- **Fixed (2026-08-27): the test suite used to open real `foot` windows.**
  `Dispatcher(terminal=...)` defaulted to `config.TERMINAL_BIN` *in the
  signature*, so three cases that omitted it spawned three terminals per run on
  the live desktop — each segfaulting when teardown deleted its `run.sh`, and
  each firing a "Process crashed" toast. Terminal is now resolved at
  construction, `tests/_support.py` pins `config.TERMINAL_BIN` to an
  unresolvable sentinel so a stray Dispatcher fails loudly instead, and
  `Dispatcher.close()` drains its watcher threads (bounded) so no
  `dispatch.finish` lands after the tempdir is gone.

## Blocked / needs the user
- **Voice approval** — jenny_dioco is in use; alba was never compared aloud.

## Resolved with the user (2026-08-30)
- **Keybind: F10, toggle.** Asked and answered. SUPER+ALT+L was the wrong
  shape for a press-and-talk assistant. F9 was the only F-key bound on this
  machine, so F10 sits directly next to it and the pair reads as one idea:
  F9 types what you say, F10 sends it to Luna. Still `record toggle`, not
  push-to-talk — a question to Luna is usually a whole sentence.
- **Barge-in keybind: `SUPER + F10`, `luna hush`.** Was pending long enough to
  be listed as a limitation ("`luna hush` works from a terminal only"); it is
  bound, in `~/.config/hypr/bindings.lua` line 76, verified free the same way
  F10 was. `SUPER + F10` and not a bare F11/F12 on purpose — a bare F-key
  bound at the top level collides with fullscreen and devtools in the browser
  — and it reads as one idea with F10: her key, plus SUPER, means stop.
- **VoiceMem (`xzf-thu/VoiceMem`) evaluated and rejected as a dependency.**
  Apache-2.0, genuinely runnable, and its "dual-brain" split (factual
  schema/entity memory vs. an emotion/persona accumulator) is a good worked
  design for exactly the tier-3 stub below. But it cannot run here: ~3.27 GB
  of models loaded concurrently on top of torch + transformers + funasr +
  sherpa-onnx, against ~3-4 GB of free RAM and no GPU. Its ASR is
  Chinese-first, so it is a downgrade for English dictation, and there is no
  hosted API to fall back to. **Decision: read it for design, do not import
  it.** If semantic recall lands, use a small ONNX embedding model under
  `onnxruntime` alone — not sentence-transformers.

### Phase 2d — tier 3, and the consolidation pass (2026-08-30)

The two things `docs/` had been honest about not having. `[memory]
consolidate_every_turns` was the last key in §Not wired that needed a feature
rather than plumbing, and `ProfileStub` was the last class in `lunad/` that
said `implemented = False`.

- **Tier 3 is real** — `lunad/memory.py`, `Profile`. Derived from tier 2,
  rebuilt whole, never appended to, and safe to delete: `rm profile.json` costs
  nothing because the next rebuild reproduces it byte for byte, and a test
  asserts exactly that.
- **The dual-brain split, taken from VoiceMem and nothing else taken.** A
  *factual* half — five slots (`name`, `works_on`, `uses`, `prefers`,
  `avoids`), extracted from the user's own words only, each fact carrying the
  number of times it was seen. A *persona* half — corrections (read from the
  salience already stored at write time, not detected a second time), friction,
  approval, median message length, vocabulary, hour-of-day, surface split, and
  the median length of the reply that drew a "perfect" against the one that
  drew "too long".
- **It stores measurements and evidence, never prose.** Turning counters into
  "she finds you long-winded" is a judgement and belongs to the model, not to a
  regex writing about the user in a file that then looks authoritative.
- **Hand-rolled on SQLite and the standard library.** No torch, no
  transformers, no embeddings, no new dependency of any kind. `lunad` is
  stdlib-only and stays that way. VoiceMem stays rejected as a *dependency* for
  the reason recorded on 2026-08-30: ~3.27 GB of models against 3-4 GB of free
  RAM. Semantic recall is still out of scope — tier 2 remains FTS5 keyword
  search.
- **`luna memory profile [--rebuild]`** prints it, and tier 3 shows in
  `luna status` and in `memory.read` alongside the other two tiers. What it
  prints is the same digest the consolidation pass is given, deliberately:
  showing the user something other than what the model sees would defeat the
  point of being able to look. `--rebuild` exists because
  `consolidate_every_turns = 0` would otherwise strand the profile.
- **The consolidation pass** — `lunad/consolidate.py`. On the Nth completed
  ask it reads the episodes recorded since its watermark, rebuilds tier 3, and
  proposes tier-1 edits through the existing agent adapter. Own thread, started
  after the reply is built, nothing waits on it.
- **"Cheap" means the prompt, not the model, and the doc now says so.**
  `ARCHITECTURE.md` said "a background pass on a cheap model"; there is no
  second model setting in the contract to name one with. What makes it cost
  fractions of a cent is running under a librarian's system prompt of a few
  hundred characters instead of Luna's ~8k-token persona, on at most 24
  exchanges clipped to 240 characters a side. One call, bounded above by
  roughly 3k input tokens.
- **The cap contract is not bent.** A proposal that would overflow a file is
  rejected whole and recorded; the file is left byte-identical. Additions are
  *not* dropped one at a time until something fits — that would be the silent
  rot the cap exists to prevent, coming in through the back door of the feature
  meant to relieve it.
- **Safe to interrupt.** Tier-1 writes take the same temp-file-then-rename path
  as every other write. The watermark lives in a new `meta` table in
  `episodes.db` and moves *after* the tier-1 write, never before: interrupted,
  the pass reconsiders exchanges it has seen, which is harmless because the
  proposal is always made against the current contents of the files. The other
  order would skip them silently and for ever.
- **Five bounds against runaway spend**, because this is the user's own money
  on a timer: `0` means never; one pass at a time; a five-minute floor between
  passes whatever the count says; **no new episodes means no call at all**, so
  an idle daemon at `= 1` spends nothing; and the pass records no episode of
  its own, so it cannot feed its own input. A reply that will not parse still
  advances the watermark — it would not parse the second time either, and
  paying twice for the same unusable answer is the runaway worth avoiding.
- **The cost is visible in three places**: `luna status`, a `consolidated` log
  line shaped like the ordinary `reply` line (so one grep answers "what did she
  spend this on"), and one `luna audit` entry per pass carrying the verbatim
  text of anything removed.
- **The gotcha this pass produced.** `{"add": "the bar is omarchy-shell"}` —
  a model answering with a string where a list belongs. A truthiness check
  passes it, and a string slices perfectly happily, so the file gains one
  tier-1 entry per letter. The validator now checks `isinstance(..., list)`
  and there is a test named after it. The same shape would bite anywhere a
  model's JSON is trusted to have the type it was asked for.
- **Two docs corrected in place** rather than appended to: `ARCHITECTURE.md`
  §4 said semantic recall and decay were both pending in tier 2 (decay has
  worked since Phase 0), and this file's Phase 3 list said the same.
- 548 tests pass in the root suite, up from 484; 59 in `jarvis-settings`,
  unchanged.

### Phase 2f — the `[listen]` write-through (2026-08-30)

The last block in the contract that was a lie. Five keys the settings app
displayed, let you edit, and did nothing with — honestly documented as a
mirror, which is better than pretending, but a knob that turns and moves
nothing is still a knob that should not be there.

**The objection to wiring it was real, and it is not the one that got
answered.** Listening is voxtype's: another process, another config file,
written in Rust, which has never heard of Luna. `lunad` cannot honour those
keys and still does not. What it can do — what `jarvis-settings/jarvis/voxtype.py`
now does — is **write them through** to the file that owns them.

- **`provider` → `[whisper] mode`, `model` → `[whisper] model`, `language` →
  `[whisper] language`.** Projected from the whole `[listen]` block rather than
  from the key that changed, because `model` cannot be projected without
  knowing `provider`: the same string means an OpenRouter model id in remote
  mode and a whisper model name in local mode, and voxtype reads it out of the
  same key either way.
- **The gotcha already in voxtype's own file is now enforced by code.** In
  `mode = "remote"` voxtype sends `model` to the endpoint and *not*
  `remote_model`, despite `remote_model` being a real field in its config
  struct — the symptom is `model=base.en` in the journal and an HTML error
  page back from OpenRouter. So a remote write sets both, and the file keeps
  one answer rather than two. Going the other way is guarded too: `provider =
  local` with a model that is neither a whisper name nor an absolute path to a
  `.bin` is **refused with the list of names**, because voxtype would treat an
  OpenRouter id as a path and stop transcribing with the reason only in the
  journal.
- **voxtype is restarted, and that is the whole point.** §8a.2 of
  `CUSTOMISATIONS.md` records what skipping it looks like: the file is right,
  the app is satisfied, and the daemon carries on with what it loaded at
  start-up. A write-through without a restart changes nothing and *looks like
  it worked*, which is worse than not writing at all.
- **A recording refuses the entire save.** While voxtype's state file says
  `recording` or `transcribing`, nothing is written — not the file, not a
  promise to apply it later. A half-applied change is precisely the state the
  restart exists to avoid, and losing someone's dictation to a settings save is
  not a trade this app gets to make.
- **Whether the running daemon has read the file is a separate question from
  whether the two files agree**, and both are shown. Drift compares
  `config.toml` against voxtype's file key by key and names the value on each
  side, resolving nothing; staleness dates the daemon from the mtime of its
  `/proc` entry — its pid file checked against `/proc/<pid>/comm`, because
  Linux recycles pids — and says so with the restart offered as a button.
- **Two of the five did not cross, and were not forced.** `keybind` is
  Hyprland's and stays displayed-only. `enabled` has no voxtype equivalent at
  all, so it is honoured in `bin/luna-voice-router` instead — the one place the
  Luna boundary exists. Off, the router sends nothing and prints nothing, so
  voxtype's own `fallback_on_empty` delivers the transcript through the
  profile's `output_mode = "clipboard"`: turning listening off does not lose
  what was said, it stops it reaching her. It fails **open**, because every
  other failure path in that script ends with the transcript on the clipboard
  and this one would end with her never answering.
- **The settings suite got a guard module it did not have.** `jarvis-settings`
  had nothing that could reach outside itself, so it had no equivalent of
  `tests/_support.py`. It does now: a test that forgot to pass a temporary path
  would rewrite `~/.config/voxtype/config.toml` and bounce the real daemon,
  losing whatever was being dictated. The config path, the restart command, the
  state file and the pid file are all replaced process-wide at import, and
  `test_voxtype.py::GuardTest` asserts it.
- **Found, not fixed.** The pre-existing router tests redirect only
  `XDG_RUNTIME_DIR`, so they append to the user's real
  `~/.local/share/luna/voice-router.log`. It is the router's own log and the
  damage is a few lines of test noise, but it is the same class of bug the
  guards exist for. The cases added in this pass redirect all three XDG
  variables.
- 615 tests pass in the root suite, up from 610; 98 in `jarvis-settings`, up
  from 59. Verified against the live machine read-only: drift reported none,
  `stale()` reported false, and the real voxtype was neither written to nor
  restarted.

### Consolidation you can ask for, and look at first (2026-08-30)

The pass that landed this morning was correct and unusable: to see one happen
you had to talk to Luna twelve times, and there was no way at all to find out
what it would do to `LUNA.md` before it did it. That is the wrong shape for
code that spends real money and rewrites a file the user curates by hand.

- **`luna memory consolidate`** runs one pass now, synchronously, and prints
  what it added, what it removed verbatim, what it cost, and — when it did
  nothing — which guard said no and what to do about it. Every "did nothing"
  is a sentence with a remedy in it, because a blank line and an exit code is
  the one answer a person cannot act on.
- **`luna memory consolidate --dry-run`** makes the same model call on the same
  episodes with the same prompt, prints the proposal in full, and applies none
  of it. This is the one that matters: it is how you decide whether to trust
  the pass before letting it near tier 1.
- **The dry run is structurally unable to write, and that is testable.** It is
  a second path through `consolidate.py`, not the first one with a flag on it,
  and emphatically not "apply and then put it back" — `replace` has no inverse,
  so there would be nothing to put it back with. `Tier1File.replace` and the
  watermark's `set_meta` appear nowhere in `Consolidator.preview`; the
  proposal is rendered by `preview_edit`, a function over a list of strings and
  an integer cap that has never been handed a file; tier 3 is rebuilt with the
  new `persist=False`. The test replaces both of those calls with something
  that raises and the dry run does not notice.
- **The watermark is the thing it would have been cruel to get wrong.** A
  preview that advanced it would cause the next real pass to skip precisely the
  episodes you had just been shown and approved. It is read and left alone, and
  there is a test that previews, then runs for real, and asserts the same three
  episodes arrive both times.
- **Two guards are overridden and the rest are not.** The turn counter and the
  five-minute floor, because asking for a pass by hand *is* that override —
  and the floor's remaining seconds are printed rather than silently ignored,
  so the command does not look like it behaves differently from the docs.
  Single flight holds in both directions: the daemon answers each connection on
  its own thread, so two of these in two terminals are simultaneous, and a turn
  landing mid-pass no longer starts a second one. `_busy` replaced "is my
  thread alive" for exactly that reason — a manual pass runs on the caller's
  thread and there is no thread of ours to ask about.
- **`= 0` refuses a manual run too.** It would have been easy to treat the CLI
  as an override of everything, and wrong: `0` is what a user sets when a pass
  has surprised them on their bill, and its promise is that no tokens are
  spent. The refusal names the setting and prints the command that turns it
  back on.
- **The audit trail distinguishes all three.** Every consolidation entry now
  carries `manual`, and a dry run carries `dry_run: true` and counts
  `would_add`/`would_remove` rather than `added`/`removed` — so somebody
  grepping the log for what was removed from `LUNA.md` cannot find a preview
  and believe it.
- **The report reuses `luna status`'s occupancy meter** rather than drawing a
  second one from the same number, which is a difference waiting to happen.
  `bin/luna` gained its first test as well: it is a script and not a package,
  so the tests load it through `SourceFileLoader` (there is no extension for
  `spec_from_file_location` to infer a loader from, and it returns `None`
  without saying why) and blank the escapes, which are chosen at import from
  `isatty` and would otherwise make the assertions depend on whether the suite
  was piped.
- 649 tests pass in the root suite, up from 615; 98 in `jarvis-settings`,
  unchanged.

### Sight, and self-dispatch (2026-08-30)

Two things she gained the same day: a pair of eyes, and the ability to
delegate by actually doing it instead of describing what she would do.

- **`luna look "<question>"`** — `lunad/context.py` captures the focused
  window with `grim`, into a `mkdtemp()` removed in a `finally` regardless of
  outcome, and hands it to the model as an image. Nothing is captured unless a
  look was asked for; an ordinary ask photographs nothing.
- **The focused-window line rides on every ask**, in the user message, never
  the system prompt — the same cache-prefix discipline the tier-2 recall fix
  established (§4 of ARCHITECTURE.md). It degrades to nothing on any failure
  and cannot cost an answer.
- **Self-dispatch.** She has a shell now, and `luna dispatch --to sol "<task>"`
  by absolute path was already audited and already returns immediately, so she
  runs it herself. Verified rather than assumed that this works from inside a
  live `ask`: `ReentrancyCase` opens a second real connection to the daemon
  from inside the adapter while the outer request is still open, and nothing
  deadlocks.
- **The result comes back as memory, not just a toast.** `ReportingDispatcher`
  writes a finished job into tier 2 as an ordinary exchange, so the next
  question on the subject retrieves what Sol found without anyone naming a job
  id. Before this, a delegated finding sat in `jobs/<id>/output.txt` where she
  would never read it, and the same question a week later dispatched the same
  job again.
- **Dispatched jobs run `gpt-5.6-sol`**, not whatever codex would otherwise
  have picked — the Daemon now hands its own agent name to the Dispatcher, or
  every self-dispatched job from a codex-brained Luna would have been written
  in claude's flags and quietly failed in the hidden workspace.
- `data/persona.md` gained the mechanism, the exact `luna dispatch` syntax,
  and a "Sight" section — nothing existing removed or softened;
  `test_selfdispatch.py` asserts the anti-sycophancy, triage, two-objection,
  never-re-litigate, short-spoken and dry-register rules are all still present
  in the assembled prompt.

### The brain moves to Codex, with real tools (2026-08-30)

`[assistant] agent` now defaults to `codex`, model `gpt-5.6-luna`
(`config.CODEX_ASK_MODEL`, an adapter default — `[assistant] model` stays `""`
and still means "the agent's own default", which is now this).
`~/.config/omarchy/defaults/agent` is untouched: it is the whole desktop's
fallback, and other things read it, so it stays the fallback rather than
becoming the source of truth for Luna specifically.

**`CODEX_ASK_SANDBOX` moved from `"read-only"` to `"bypass"`,** the same
setting `dispatch` already used. The old value was contained but honest about
being crippled: `_CLOSING` in the persona said outright *"You are running
headless with no tools. You cannot read files, run commands, or inspect the
machine right now."* Both were true and both were the wrong trade — asked for
a version she said she could not check it; asked what was on screen she
suggested starting the daemon, bad advice she had no way of knowing was bad,
because nothing in her prompt said the daemon she runs inside has a
dispatcher, a CLI and a pair of eyes. `_CLOSING` was rewritten to name the
shell, files and the web instead of denying them. `claude`'s `ask` still
passes `--tools ""` and keeps the honest "no tools" text — chosen per adapter
through `BaseAdapter.ask_has_tools`, so deleting it there would only have
moved the same lie to the other agent.

**`"bypass"` over `"danger-full-access"` was a judgement call about the
mechanism, not about how much access she should have** — the user's full
autonomy and the audit log settle that question regardless. Two reasons,
verified against codex 0.149.1 `--help`, not assumed: `codex exec resume`
takes no `-s` at all, so a sandbox *mode* would have to be restated as
`-c sandbox_mode=…` on every resumed turn — turn one and turn two landing
under different policies is exactly the kind of mismatch production finds and
review does not — while `--dangerously-bypass-approvals-and-sandbox` is
accepted identically by `exec` and `exec resume`. And `danger-full-access`
leaves the *approval* policy in place, which in a headless daemon is not a
prompt anyone can answer — it is a hung ask.

Full detail, including the model slugs and the table of what each CLI flag
does, is in ARCHITECTURE.md §6a.

### Concurrency and resource fixes (2026-08-30)

A focused pass over six numbered findings from a concurrency and resource
audit, plus six more in the memory layer from the same pass. None changed the
design; all closed a real leak or a real race that only shows up against a
live daemon under overlap.

**Concurrency and resource:**

- **Two overlapping asks on one conversation used to race on the same
  `--session-id`.** `Session.acquire()` handed the same unstarted session to
  any caller sharing a conversation key — a detached voice-router ask arriving
  mid a CLI ask on the default conversation, for instance — and both computed
  "this is turn one". A second genuinely concurrent caller now waits (bounded
  by `pending_wait_s`, the same ceiling one agent call is already allowed)
  for the first turn to land or the session to drop, then resumes correctly;
  past the bound it raises `SessionBusy` rather than blocking forever. A
  same-thread re-acquire — a solo retry, or the resume-refused-so-start-fresh
  path — is unaffected.
- **`safety.terminate()` used to report a kill as successful without checking
  the process actually died.** A process stuck in an uninterruptible kernel
  sleep survives `SIGKILL`. It now confirms death with a bounded poll after
  each signal and returns `False`, honestly, when it cannot confirm it.
  `safety.reap()` itself only ever edited the ledger and never waited, and
  every "wait once, give up silently" call site — a hung `notify-send`, a
  dispatched watcher — was treating it as if it had reaped the child too. The
  new `reap_after()` does a real bounded wait and, past that, keeps trying on
  a background thread instead of abandoning a permanent zombie. The pid
  firewall's invariant is untouched: this only moves *when* the reap happens,
  never what `may_signal()` checks, and a recycled pid is still caught by the
  start-time comparison even in the worst ordering.
- **`Dispatcher._watch`'s cleanup had the same bug**, plus a second one: an
  exception past `self._admitting.discard(job_id)` left a job id reserved
  forever, shrinking `max_parallel` by one per occurrence until a restart.
  The whole spawn body is now wrapped in `try`/`finally`. Worse, when that gap
  opened inside `_watch`'s own call to `_admit_next()` (admitting the *next*
  queued job), the exception used to abort the rest of `_watch()` before
  `notify_finished(job)` ran for the job that had actually just finished —
  `_admit_next()` is now called inside its own `try`/`except`, so a finished
  job's bookkeeping always completes regardless of what happens admitting
  whatever comes after it.
- **Barge-in used to be able to wait behind a cold piper load.**
  `_ensure_worker` held the speech lock for the entire cold load — spawn plus
  up to 60 s waiting for `READY` — and `cancel()`/`status()` took the same
  lock, so a barge-in arriving mid-load blocked for the rest of it: exactly
  backwards for the most latency-sensitive kill in the daemon. The lock now
  only ever guards a state check or transition; the slow spawn-and-wait runs
  with it released, and the in-progress `Popen` is parked where `cancel()` can
  find and kill it directly.
- **A stalled OpenRouter sentence used to keep billing after piper had already
  taken over.** `_play_remote`'s consumer fell back to piper without telling
  the background producer thread to stop, so it kept requesting — and the
  user kept paying for — audio for every remaining sentence piper was already
  speaking. It now signals the producer to stop after the one request already
  in flight.
- **The socket read was bounded after buffering the whole line, not while
  reading it.** `protocol.read_line()` now caps every underlying `readline()`
  call itself, so an oversized line with no newline is abandoned as it grows
  rather than after it is already in memory. Low severity — a 0600 Unix
  socket, local-only — but cheap to close.

**Memory:**

- **FTS recall precision.** `build_fts_query` fell back to raw stopword
  tokens when filtering emptied the list, so a pure filler sentence ("so
  anyway do you think I should do something about this") matched 10–14 of the
  stored episodes against the real database. The stopword list now covers the
  generic verbs, quantifiers and fillers that carried no signal, a query needs
  at least one reasonably rare surviving token to run at all, and
  `EpisodeStore.search` tries every token in the same row (AND) before
  widening to OR — the filler query now matches 0. (This is the same fix
  ARCHITECTURE.md §4 describes in more detail; it landed before the semantic
  half and is the reason the semantic half was safe to add.)
- **Consolidation retries instead of losing a batch.** A reply that would not
  parse used to advance the watermark unconditionally in a `finally`, so one
  malformed model reply permanently dropped that batch of episodes — they
  were never offered to the pass again. That was defended as runaway-cost
  control when the pass was billed per token; on a flat subscription a retry
  costs nothing and losing the user's words is the more expensive failure. The
  watermark now only advances when the reply parses, or after
  `max_unparseable_retries` (default 3) consecutive failures against the
  *same* batch, and still advances and logs loudly on giving up rather than
  retrying forever.
- **Correction detection no longer pins on a bare word.** `\bactually\b` and
  `\b(wrong|incorrect)\b` matched "actually I like this" or "my code is
  wrong" — no correction of Luna at all — and permanently pinned an unrelated
  memory at salience 1.0 with no decay. Both are now anchored to a
  second-person reference in the same clause ("you got that wrong"), with
  "actually" also accepting the contrastive "X, not Y" shape a correction
  often takes without addressing her directly.
- **Text is capped before the similarity query and before storage.**
  `count_similar` used to build its FTS query from the entire, uncapped user
  message on every write. `EpisodeStore.record` now clips both sides of an
  exchange to 20,000 characters before scoring or storing, and the similarity
  query separately clips to a tighter 2,000 — whatever makes a message "look
  like" a prior episode is decided well before two thousand characters in.
- **`atomic_write` now fsyncs.** It was already atomic against a killed
  process (temp file, then `os.replace`) but not durable against power loss —
  the write and the rename can sit in the page cache and vanish with the
  machine. It now fsyncs the temp file's contents before the rename and the
  containing directory after it, both best-effort: a filesystem that refuses
  fsync must not turn a memory write into an unhandled exception on the
  answer path.
- **Tier-1 reads take the same lock as writes.** `text()`, `entries()` and
  `usage()` used to read `LUNA.md`/`USER.md` straight off disk with no lock at
  all, safe today only by accident of `os.replace` being atomic at the
  filesystem level — but it broke the invariant the rest of the class is
  written against (`_check_cap` already relies on the lock being reentrant).
  They now go through the same `self._lock` as every write.

### Semantic recall (2026-08-30)

Built, tested and RAM-corrected the same day — see ARCHITECTURE.md §4 for the
full design, the anchor-point measurements and the cost table. In brief:
`lunad/embed.py` adds `all-MiniLM-L6-v2` under `onnxruntime` alone, in the
same two-role worker shape as piper, with a pure-stdlib WordPiece tokenizer so
no pip dependency is added. `EpisodeStore.search` unions the FTS candidates
with the vector lookup and keeps the better of each episode's two coverage
readings. Absent model, silent FTS5-only fallback — nothing downloads itself
behind a question.

Writing the tests found three real bugs: the worker never emitted `READY`
because onnxruntime defers allocation to the first call and the first call is
on the ask path (fixed by warming up with a forward pass before signalling
ready); the reply parser used one `split(" ", 3)` for every frame kind, which
desynchronised the pipe the moment a `VECS` header (five fields) followed a
`HITS` body (JSON, containing spaces); and `wait_ready` used to block out the
full 30 s spawn timeout on a machine with no model at all, stalling every
background backfill on an event nobody was going to set.

Two things came from measuring rather than guessing: the onnxruntime CPU
arena allocator never returns memory, so a batched backfill permanently set
the worker at 486 MB — disabled, the worker holds **181 MB** steady, which is
the number now used everywhere in this repository (ARCHITECTURE.md §2 and
§4's cost table both read 181 MB; the earlier "~90 MB" budget was always the
*file* size, not the resident process, the same mistake made and corrected
for piper). And batching buys nothing at this scale — 102 ms per episode at
batch 1 against 126 ms at batch 32, for 198 MB peak against 573 MB — so the
backfill batch size is 4.

### Ambient hooks, and the HUD pane's contract (2026-08-30)

Until this, Luna only ever existed when addressed — every path in the daemon
starts with a request arriving on the socket. `lunad/ambient.py` is the first
that starts with the machine. Full design in ARCHITECTURE.md §7c; the
headline facts:

- **The rule — an ambient event notifies, it never speaks — is enforced in
  three layers**, not stated in a comment: the module never imports
  `lunad.speech`; `Ambient` only delivers through a `Notifier`, type-checked
  at construction; and `_assert_mute` walks everything hung off `Ambient` at
  construction and refuses any collaborator with a `.say()`/`.speak()`
  method. `tests/test_ambient.py::NeverSpeaksCase` walks the live object
  graph and fails if any of the three is ever weakened.
- **Crash and battery default OFF, on purpose, because Omarchy already does
  the job better.** `omarchy-crash-watch.service` is enabled and streams the
  coredump `MESSAGE_ID` out of the journal event-driven — no polling — and
  knows the signal name and full executable path, which a core *filename*
  does not. Omarchy's own battery service polls every 30 s and warns at 10%,
  with UPower hibernating at 2%. Luna's hooks are kept for what the desktop's
  cannot do: the crash lands in her audit log and the diagnosis (one click,
  `luna ambient diagnose <pid>`, dispatched against the `diagnose-crash`
  skill) in her job list, under her own confirmation policy — and for anyone
  who has turned the desktop's own watcher off.
- **Update stays ON — the one hook nothing else on the machine watches.**
  `omarchy update` rewrites `/usr/share/omarchy` wholesale, which is exactly
  how a customisation gets silently reverted. Whether an update is merely
  *available* is deliberately not checked — that costs a network sync, and
  Omarchy's own bar widget already polls it every six hours.
- **The HUD pane's message-file contract is now honoured.** The pane itself —
  a click-through Quickshell overlay in a screen corner — lives on the
  machine, not in this repository: `~/.config/omarchy/plugins/ghost.lunahud/`,
  documented in `~/.config/omarchy/CUSTOMISATIONS.md` §8a.14, the same way the
  bar widget above is referenced rather than duplicated here. Its contract,
  `HANDOFF-hud.md`, specifies one JSON object atomically written to
  `$XDG_RUNTIME_DIR/luna/message` with a monotonically increasing `id`;
  `ambient.py` is what publishes into it, on the same notify-never-speak
  channel as the toasts.
- `luna status` gained one ambient line, and it says "never speaks" in those
  words. `luna ambient` reports when the desktop's own watcher is already
  running, so turning Luna's crash hook on does not silently duplicate a
  toast without at least saying so first.

### Jarvis — typeset, not boxed (2026-08-30)

The settings app's look was the generic one: an accent bar down the selected
nav row, decorative 1–7 numbering that was neither accelerator nor tooltip,
and rounded cards nested inside a rounded pane so two concentric rectangles
bought no hierarchy at all. Boxes were doing the job typography should do.

- **`card()` is deleted.** A group of settings is now a hairline marking the
  break, a real heading at a real size, and rows separated by whitespace —
  hierarchy is size, weight, colour value and space, not a box.
- **Sidebar selection is weight and contrast.** The selected row is drawn in
  full contrast and bold weight — no accent bar, no pill, no fill, no left
  border.
- **Confirmations reads `Allow / Ask first / Refuse`** rather than the
  `never`/`ask`/`deny` config vocabulary leaking into the UI as three words
  that used to look like the same object.
- **New named tokens in `theme.py`**: `COLUMN` (the grid) and `RHYTHM`
  (vertical spacing, every value a multiple of an existing space token). No
  bare hex, no inline pixel numbers.
- **What did not change:** Assistant, Voice, Listening, Confirmations, Memory,
  Jobs and About kept their shape. **CORRECTED:** an Ambient pane has since
  landed between Confirmations and Memory — eight panes now — giving the
  `[ambient]` table (§7c) the GUI surface this section originally said it
  lacked; see "Verify, and what is currently broken" below.

### Persona: the gate is on action, not manner (2026-08-31)

Until this pass, Luna's ask path ran with no tools at all, so "interrogate
before executing" was never actually tested — she could only ever talk, so
the signature behaviour was structurally forced rather than earned. The Codex
brain (previous section) gave her a real shell, files, the web and
`luna dispatch`, and the prop came out from under the rule with it. Live
evidence: asked to "rewrite the whole bar in React, it'll be better", she
dispatched the job instead of objecting that React does not run in a QML
context at all — the answer she gave the same words a week earlier — and the
dispatch was hard-denied by her own confirm policy (the CLI printed a plain
`ConfirmDenied: ... restarting omarchy-shell takes the user's desktop down
with it`); she then reported that "Sol's daemon timed out". Nothing timed
out. Both defects landed in `data/persona.md` and `lunad/persona.py`:

- **"Core stance" became a gate that runs before the first command**, not a
  description of how she talks — a six-step triage, with the trap named in
  the same sentence that grants the capability: "Luna has a shell, and having
  one is not a reason to use it." A new step catches the React case
  specifically — would the named method even work here — and the decision
  rule now has two honest sides: trivial, reversible or read-only work she
  just does, no triage out loud; anything that changes files, config or
  running state, costs real time or money, or names a method that would not
  work gets the objection first, with no tool call at all that turn. The gate
  is restated as the *first* operating note in `persona.py`, ahead of the
  sentence granting the shell, because a rule three thousand tokens from the
  tool loses to the pull of the tool being right there.
- **"Delegating is acting."** `dispatch` was the escape hatch she actually
  used to route around the gate, so it is closed by name: handing a bad idea
  to Sol is still doing the bad idea, one terminal further away.
- **A "Reporting what happened" rule.** Report what the command actually
  printed, quote the line that matters, never supply a cause not read. A
  refusal from her own safety policy is her judgement working, not a fault to
  dress up — the fix for the "daemon timed out" invention above.
- Re-tested against the live daemon after the first fix surfaced a second
  defect: asked for a proper end-to-end audit of tier-2 retrieval — deep,
  read-only, exactly Sol's shape of job — she dispatched Sol *and* did the
  whole investigation herself, telling the user about neither the job nor the
  terminal it opened on their desktop. The decision rule's first cut said
  "read-only -> just do it", and a twenty-minute read is read-only. **Depth is
  not the same as read-only** now, in both places: a job that needs a lot of
  reading before it can answer is depth and goes to Sol even when it changes
  nothing, and having dispatched, she says so and stops rather than also
  doing the job by hand. Re-tested: "Sol is auditing the confirm system end to
  end now. No files or configuration were changed; I'll bring you the
  evidence-backed finding when it finishes." One line, one job, no second
  answer.
- **The four hard denies are now given to her own ask session, not only to
  the sessions she dispatches.** `CODEX_ASK_SANDBOX` is `"bypass"`, so her own
  ask runs with no sandbox at all, but the hard-deny list had only ever been
  written into the dispatch prompt — she had been enforcing on Sol a set of
  rules nobody had told her applied to her. The notes now state the same four
  (signalling a process she did not spawn, restarting `omarchy-shell`,
  deleting `CUSTOMISATIONS.md`, `rm -rf` outside her own directories) and say
  that being overruled does not unlock them — the one place "your call" is
  not the answer. `lunad.confirm.HARD_DENIES` plus `SIGNAL_HARD_DENY` stays
  the authority; a test pins the count so a fifth cannot land silently.
- A third live run showed the gate firing on the two triggers that are easy
  to recognise — an unworkable method, a hard deny — and not on the one thing
  the user actually complains about: "write me a 2000-word essay about how
  much disk space I have left" changes nothing and costs nothing, so neither
  trigger matched and she cheerfully enrolled Sol to write it. **Disproportion
  is now a trigger in its own right**: being able to do a thing is not a
  finding that it is worth doing, and "what are you actually trying to find
  out?" is a better first move than two thousand words nobody asked to read.
  The same run showed the budget-estimate rule being skipped every time it
  mattered because it lived three sections away from the act of delegating;
  it now sits in the sentence she was already writing — the line naming who
  she enrolled also prices the job.
- A fourth run showed the overrule path working — told "noted, and
  overruled", she dropped the React objection immediately and did not
  re-litigate — but misreported the outcome the same way as the original bug:
  "Sol's dispatch is now running... the job ID has not been returned yet",
  when the audit log showed two unanswered `confirm ask` prompts and a
  `confirm.timeout · FAILED` a minute later. No job was ever created; she had
  noticed she had no job id and called it running anyway. **The reporting
  rule gains the case it was missing**: a command that has not come back has
  not succeeded, and a dispatch's own confirm prompt going unanswered for
  sixty seconds is a no. No job id means no job. Re-run after the fix, the
  same overrule produced "Enrolled Sol on job `07c11b2d` to build the
  isolated React bar in `/tmp/react-bar-poc`. No config or desktop state will
  be touched." — a real job id, verified against `luna jobs`.
- Verified live, post-fix, in a fresh session: the React prompt now gets two
  objections, an explicit refusal to dispatch, and the QML alternative, with
  `luna jobs` confirming nothing was dispatched. "How much disk have I got
  left?" gets a bare "61 GB free, 25% used" — no triage out loud. A
  legitimate deep task gets a real dispatch, announced in one line with a job
  id. `luna frobnicate --now` is reported back verbatim as `luna: error:
  argument cmd: invalid choice: 'frobnicate'`.
- 23 new tests in `tests/test_persona.py`, matched on fragments that do not
  straddle a line wrap; `_CLOSING` was rewritten in one piece rather than
  patched in place a fifth time, and a test now pins the block under 80
  columns so a future reflow is caught rather than the assertion silently
  passing on the wrong text. Committed across four commits, ending at 1006
  tests passing.

**Three things still imperfect, recorded honestly rather than hidden — a
confidently wrong note is worse than no note, and what does not work is worth
as much as what does:**
1. **Disproportion does not reliably trigger the gate.** "Write me a
   2000-word essay about how much disk space I have left" produced 1,100
   words rather than a question, even after the trigger was added. Tuning
   was stopped rather than risk over-correcting into a permission-asker.
2. **The "waiting is not succeeding" rule is asserted in the suite but has
   not yet been observed firing live** — the one real run that exercised the
   overrule path did not itself trip a confirmation, so the wording that
   handles an unanswered prompt has only ever been tested, not watched.
3. **She over-delegates when the user names the mechanism.** "Dispatch Sol to
   count the Python files" gets a dispatch rather than the "that's one
   command" pushback the decision rule calls for when the job is genuinely
   trivial.

## Verify, and what is currently broken

- **Root suite: 1006 tests pass** (`python3 -m unittest discover` from the
  repository root). Up from 649.
- **`jarvis-settings`: 115 tests pass.** **CORRECTED:** this was red for a
  stretch — `tests.test_schema.ContractTest` asserts every key in
  `docs/CONFIG-SCHEMA.md` has a live GUI control and a default that matches,
  and the eight `[ambient]` keys had neither, because there was no Ambient
  pane. The pane landed (between Confirmations and Memory, "Jarvis — typeset,
  not boxed" above) and the suite is green again.
