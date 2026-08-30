# State of play — 2026-08-30

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

## Next (Phase 3)
1. ~~Bar widget + `subscribe` for live state.~~ Built — see below. `subscribe`
   was dropped rather than built.
2. Ambient hooks: crash, battery, `omarchy update`.
3. Semantic recall. Tier 2 is FTS5 keyword only. If it lands, use a small ONNX
   model under `onnxruntime` alone — not sentence-transformers, and not
   VoiceMem. Decay was done in Phase 0 and the old wording here implied
   otherwise.
4. Wire `luna hush` to a keybind so a spoken reply can be cut off by hand.
5. Worker fan-out as a *plan* — the gate and the queue are built, so several
   jobs can be in flight and the number is bounded, but Luna still does not
   decide on her own to split a task across workers. That is a planning change
   in the persona and the ask path, not plumbing.

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
(All four were wired in the end: `consolidate_every_turns` in Phase 2d and the
two `[dispatch]` keys in Phase 2e, both below. `[listen]` is still voxtype's.)

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
- **Codex `ask` has tools; Claude `ask` does not.** `claude` takes `--tools ""`
  and is genuinely text-in/text-out. codex has no equivalent flag, so the
  sandbox *is* the tool policy: an `ask` runs `-s read-only`, which still lets
  it read files and run read-only commands. It is contained, not inert, and the
  persona's "you are running headless with no tools" line is therefore not
  strictly true on codex.
- **Codex reports no per-call price.** It is on a ChatGPT subscription, so
  `cost_usd` is `None` and `billing` is `"subscription"`. The daemon's money
  counter stays at zero on codex, which is correct but means `luna status` is
  not a like-for-like comparison between the two agents.
- **Semantic recall not implemented** — tier 2 is FTS5 keyword only. Phase 3.
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
- **A consolidation write has no undo.** It goes through `replace`, which
  discards text nothing else keeps, and this project does not claim inverses it
  does not have. The audit entry carries the verbatim text of everything a pass
  removed, so a deletion is recoverable by reading the log; that is the most
  that can honestly be offered.
- **`fallback_on_empty` cannot be disabled.** Every Luna recording leaves the
  raw transcript on the clipboard. Harmless, but it does clobber the clipboard
  on every voice turn. If that becomes annoying the only fix is upstream.
- **Barge-in has no keybind yet.** `luna hush` works from a terminal only.
- **A voice ask that fails speaks a generic apology**, not the actual error;
  the detail is in `luna log`.
- **A dispatched session is not sandboxed.** It runs with `bypassPermissions`
  and real tools, so it could write anywhere the user can. Sol's namespace
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
