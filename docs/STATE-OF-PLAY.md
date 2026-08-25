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
- omarchy-shell and other agent sessions: untouched.
- **voxtype OSD**: `qs -p ~/.local/share/voxtype/quickshell` spawned by the
  voxtype daemon, plus its `voxtype-audio-bridge` child. Layer surface
  `voxtype-osd` on the overlay layer, visible only while not idle.

## Next (Phase 2)
1. Workspace dispatch: a special workspace, `foot` + agent with full autonomy.
2. Sol — the specialist agent, own prompt, own memory namespace.
3. Audit log (`~/.local/share/luna/audit.jsonl`) + undo journal.
4. Wire `luna hush` to a keybind so a spoken reply can be cut off by hand.

## Known limitations / stubbed
- **Codex adapter is still a declared stub.** Its headless flags were never
  verified and were deliberately not guessed.
- **Tier 3 (derived profile) not implemented.** Phase 3.
- **Semantic recall not implemented** — tier 2 is FTS5 keyword only. Phase 3.
- **`fallback_on_empty` cannot be disabled.** Every Luna recording leaves the
  raw transcript on the clipboard. Harmless, but it does clobber the clipboard
  on every voice turn. If that becomes annoying the only fix is upstream.
- **Barge-in has no keybind yet.** `luna hush` works from a terminal only.
- **A voice ask that fails speaks a generic apology**, not the actual error;
  the detail is in `luna log`.
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
