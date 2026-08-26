# State of play — 2026-08-25

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
- **Keybind** SUPER+ALT+L, verified free and verified registered.
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
1. Bar widget + `subscribe` for live state.
2. Ambient hooks: crash, battery, `omarchy update`.
3. Semantic recall (sqlite-vec + local embeddings) and decay in tier 2.
4. Tier 3 derived profile.
5. Wire `luna hush` to a keybind so a spoken reply can be cut off by hand.
6. Parallel worker fan-out — `dispatch` is one job per call today.

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

## Known limitations / stubbed
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
- **Tier 3 (derived profile) not implemented.** Phase 3.
- **Semantic recall not implemented** — tier 2 is FTS5 keyword only. Phase 3.
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
- **`dispatch` does not fan out.** One job per call.
- **Job directories are never garbage-collected.** `~/.local/share/luna/jobs/`
  grows one directory per dispatch.
- **The audit log is never rotated**, on purpose. It grows without bound.
- **The `[osd]` sizing keys are inert under the Quickshell frontend.**
  `position` / `width_px` / `height_px` / `margin_px` reach only the GTK4 and
  native frontends; the Quickshell OSD's geometry lives in its `Theme.qml`.
  They are set in the config anyway so a fallback frontend lands correctly.
- **The themed OSD depends on `/usr/share/omarchy/shell/{Commons,Ui}`.** An
  `omarchy update` that renames a token would break it, and the failure mode
  is a QML import error at OSD launch, not at dictation time.

## Blocked / needs the user
- **Voice approval** — jenny_dioco is in use; alba was never compared aloud.
- **Whether SUPER+ALT+L is the right key.** Picked because it was free and
  mnemonic, not because it was asked for.
