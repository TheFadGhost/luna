# Luna

A resident personal assistant for [Omarchy](https://omarchy.org) — a supervised
daemon with a voice, a memory that compounds, and the run of the desktop.

Not a chatbot. Luna is the single point of contact: she triages, pushes back,
prices the work, and delegates the depth to specialists.

## Status

**Phase 0 — text in, text out.** Daemon, socket, memory tiers 1–2, persona, CLI.
See `docs/ARCHITECTURE.md` for the full design and the phase plan.

## Quick start

```sh
systemctl --user status lunad
bin/luna status
bin/luna ask "..."
bin/luna memory show
bin/luna memory search "bar widget"
```

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
