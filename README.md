# Luna

A resident personal assistant for [Omarchy](https://omarchy.org) — a supervised
daemon with a voice, a memory that compounds, and the run of the desktop.

Not a chatbot. Luna is the single point of contact: she triages, pushes back,
prices the work, and delegates the depth to specialists.

## Status

**Phase 2 — she delegates.** Daemon, socket, memory tiers 1–2, persona, CLI,
piper speech out, voxtype speech in, conversation sessions — plus workspace
dispatch, Sol the specialist, an append-only audit log and the PID firewall.
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

bin/luna dispatch "go and work this out"   # a worker, in the luna workspace
bin/luna dispatch --to sol "..."           # the specialist
bin/luna jobs --output                     # what came back
bin/luna peek                              # show/hide the workspace
bin/luna audit --since 30m                 # what she did, and why
bin/luna spawned --check 12345             # ask the firewall about a pid
```

## Delegation

`luna dispatch` opens a `foot` terminal in a hidden Hyprland special workspace
called `luna` and runs the configured agent there with full autonomy. Luna
starts the terminal herself rather than asking the compositor to, because she
has to own the pid — see the firewall below. `luna peek` brings the workspace
into view; `luna jobs` lists what has run, from disk, so it survives a daemon
restart.

**Sol** is the specialist she enrols for depth (`--to sol`): his own system
prompt (`data/sol-persona.md`), his own memory namespace
(`~/.local/share/luna/memory/sol/`), and a report back to Luna rather than to
you. He cannot write `LUNA.md` or `USER.md` — the memory API refuses by name.

## Safety, since she asks no permission

**She signals only what she spawned.** One gate, `lunad/safety.py`, and every
path in the daemon goes through it. A pid is signallable only if Luna forked it
*and* the process holding that pid is still the one she forked — checked
against the start time in `/proc/<pid>/stat`, because pids get recycled.
Refusals raise. `pkill` and friends appear nowhere, and a test reads the
shipped source to keep it that way.

```sh
$ luna spawned --check "$(systemctl --user show -p MainPID --value voxtype)"
pid 1470712: REFUSED
  reason:  Luna did not spawn it
  cmdline: /usr/bin/voxtype daemon
```

**Everything is written down.** `~/.local/share/luna/audit.jsonl` is
append-only and fsync'd per line: every dispatch, spawn, signal, refusal and
memory write, with what it was for and how it ended. Where an action has a real
inverse it is recorded; where it does not, nothing is invented.

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

**On screen** — the voice indicator that appears while recording is voxtype's
Quickshell frontend (`[osd] frontend = "quickshell"`), restyled to the desktop's
own design tokens. `osd/` holds the master copy of the two QML files; see its
README for why the stock GTK4 indicator cannot be themed and how to restore this
one after a voxtype update.

![The monochrome voice indicator](docs/osd-monochrome.png)

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
