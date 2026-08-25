# State of play — 2026-08-25

## Done and verified
- **Phase 0 shipped.** `lunad` daemon running under systemd (12 MB), Unix socket,
  NDJSON protocol, CLI client. 84 tests passing.
- **Memory tiers 1-2 working.** Cap rejection verified byte-for-byte: an oversized
  write exits 1, prints what must be consolidated, and leaves the file untouched.
  FTS5 search returns real episodes. Salience scored, decay applied at read.
- **Persona live and non-sycophantic.** Verified adversarially: given a deliberately
  bad plan ("rewrite the bar in React Native tonight") she refused the premise on
  technical grounds, priced it, offered the cheaper path, and asked what problem
  it solved. No agreement, no preamble.
- **Tier-1 memory seeded** with the two facts she got wrong unaided: this desktop
  is Quickshell not Waybar, and the hardware ceiling.
- **Piper installed and benchmarked** (see CUSTOMISATIONS.md 8a.1). Voice sample
  synthesised and sent to the user for approval.
- Committed: `c894ba9`.

## Running
- `lunad.service` — enabled, active. Untouched: voxtype, omarchy-shell, other sessions.

## Next (Phase 1)
1. `luna-speak`: resident-with-idle-unload piper worker, sentence-streamed to aplay.
2. `luna-voice-router`: voxtype `[profiles.luna]` post-process hook. MUST be
   defensive - on any failure voxtype types the raw transcript into the focused
   window.
3. Keybind for `voxtype record toggle --profile luna`.
4. `say` / `listen_start` ops on the daemon.

## Blocked / needs the user
- **No git remote.** Cannot push. Needs a decision: own repo, or fold into
  `omarchy-monochrome`.
- **Voice approval** — jenny_dioco vs alba.
- **Cost.** Each `ask` is ~$0.05: ~8k system-prompt tokens are cache-*created*
  every call because separate processes never share a prompt cache. Recommend
  routing conversational turns to a cheaper model and reserving the default model
  for dispatched work. Not yet done.
- **Codex adapter stubbed.** Its headless flags were never verified and were
  deliberately not guessed. Needs a session to confirm them against `codex --help`.
