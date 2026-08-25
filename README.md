# Luna

A resident personal assistant for [Omarchy](https://omarchy.org) — a supervised
daemon with a voice, a memory that compounds, and the run of the desktop.

Not a chatbot. Luna is the single point of contact: she triages, pushes back,
prices the work, and delegates the depth to specialists.

## Status

**Phase 1 — she talks back.** Daemon, socket, memory tiers 1–2, persona, CLI,
piper speech out, voxtype speech in, conversation sessions.
See `docs/ARCHITECTURE.md` for the design and `docs/STATE-OF-PLAY.md` for what
is actually built.

## Quick start

```sh
systemctl --user status lunad
bin/luna status
bin/luna ask "..."
bin/luna say "read this aloud"
bin/luna hush                     # stop speaking now
bin/luna memory show
bin/luna memory search "bar widget"
```

## Voice

**Out** — piper (`en_GB-jenny_dioco-medium`) in a project venv, lazy-loaded and
unloaded after five idle minutes. The reply is split on sentences so playback
starts on the first one (measured: first audio at 45 ms) while the rest is still
being synthesised. Code, paths, URLs and long numbers are never read aloud; they
become "it's on screen".

**In** — `SUPER+ALT+L` runs `voxtype record toggle --profile luna`. The profile's
`post_process_command` is `bin/luna-voice-router`, which forwards the transcript
to the daemon and prints nothing.

Plain dictation (F9, SUPER+CTRL+X) is untouched and stays untouched: it names no
profile, so it takes none of this path.

> **If you edit `~/.config/voxtype/config.toml`, restart voxtype.** The daemon
> only reads its config at startup. Without a restart it logs
> `Profile 'luna' not found in config, using default settings` and types your
> question into whatever window has focus.

## Memory

Three tiers, adapted from **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**
by Nous Research (MIT) — specifically its `§`-delimited curated files, hard caps
that *reject* rather than truncate, and the frozen system-prompt block that keeps
the KV-cache prefix valid.

Two deliberate departures: Hermes outsources semantic retrieval to a paid cloud
service (Postgres + pgvector + a second LLM), which is not viable on this
hardware — Luna uses a local embedding index instead. And Hermes has no decay,
so memories only get tidied at the cap; Luna scores salience and lets trivia age
out.

## Licence

MIT. Piper (GPL-3.0) is invoked as a separate binary over a pipe and does not
affect this licence.
