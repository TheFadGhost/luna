# Luna

[![CI](https://github.com/TheFadGhost/luna/actions/workflows/ci.yml/badge.svg)](https://github.com/TheFadGhost/luna/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A resident personal assistant for [Omarchy](https://omarchy.org) — a supervised
daemon with a voice, a memory that compounds, and the run of the desktop.

Not a chatbot. Luna is the single point of contact: she triages, pushes back,
prices the work, and delegates the depth to specialists.

## Status

**Phase 2 — she delegates.** Daemon, socket, all three memory tiers, persona,
CLI, piper speech out, voxtype speech in, conversation sessions — plus
workspace dispatch, Sol the specialist, an append-only audit log, the PID
firewall, and a background pass that promotes what matters out of episodic
memory into the curated files.
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
bin/luna memory profile --rebuild # the derived profile (tier 3)
bin/luna memory consolidate       # one pass now, not on the 12th turn
bin/luna memory consolidate --dry-run   # ...and what it would write, first

bin/luna dispatch "go and work this out"   # a worker, in the luna workspace
bin/luna dispatch --to sol "..."           # the specialist
bin/luna jobs --output                     # what came back
bin/luna peek                              # show/hide the workspace
bin/luna audit --since 30m                 # what she did, and why
bin/luna spawned --check 12345             # ask the firewall about a pid
bin/luna settings                          # the live config, and where it lives
bin/luna settings set voice.voice flux-donovan-en
bin/luna confirm                           # the policy, and anything waiting
bin/luna confirm yes <token>               # release a pending confirmation
```

## Delegation

The app is **Jarvis**; the assistant's name is a setting (`[assistant] name`,
default `Luna`). `jarvis` and `luna` are the same command. Configuration lives
in `~/.config/jarvis/config.toml` (0600, in a 0700 directory) — see
`docs/CONFIG-SCHEMA.md` — and `lunad` watches it and hot-reloads, so a change
to the voice, the model or the confirmation policy takes effect on the next
request rather than on the next restart. That document's §Wiring table says
what reads each key and when it lands. Every key in the contract is now
honoured, including `[listen]` — which `lunad` still does not read and never
will, because listening belongs to voxtype in another process with its own
config file. The settings app **writes those keys through** to that file and
restarts voxtype, which reads its config only at start-up; two of the five had
no voxtype equivalent and were wired where they actually belong instead, or
left honestly read-only. The table says which is which rather than leaving it
to be discovered. Secrets never go in that file: the
OpenRouter key lives in `~/.config/jarvis/secrets.env` and reaches the daemon
through a systemd drop-in.

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
inverse it is recorded; where it does not, nothing is invented. It rotates to
numbered siblings rather than growing forever, and the rotation writes its own
entry so a gap in the record explains itself.

## Voice

**Out** — piper (`en_GB-jenny_dioco-medium`) in a project venv, lazy-loaded and
unloaded after five idle minutes. The reply is split on sentences so playback
starts on the first one (measured: first audio at 45 ms) while the rest is still
being synthesised. Code, paths, URLs and long numbers are never read aloud; they
become "it's on screen".

**In** — `F10` runs `voxtype record toggle --profile luna`. The profile's
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
> question into whatever window has focus. Changing the provider, model or
> language in the Jarvis **Listening** pane does the restart for you — and
> refuses the save outright if a recording is in flight, because no setting is
> worth someone's dictation.

## Memory

Three tiers, adapted from **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**
by Nous Research (MIT) — specifically its `§`-delimited curated files, hard caps
that *reject* rather than truncate, and the frozen system-prompt block that keeps
the KV-cache prefix valid.

**Tier 1** is the curated identity, always in the prompt. **Tier 2** is every
exchange in SQLite, searched with FTS5 and ranked by a salience score that
decays. **Tier 3** is a derived profile: measurements taken from tier 2 —
stable facts on one side, how the user likes to be talked to on the other —
rebuilt from scratch rather than appended to, so deleting it costs nothing.
That split is borrowed from [VoiceMem](https://github.com/xzf-thu/VoiceMem),
which was read for its design and rejected as a dependency: 3.27 GB of models
does not fit on this machine. The implementation here is regex and the standard
library.

Every N turns a background pass reads the new episodes, rebuilds tier 3, and
proposes edits to tier 1 through the model — under exactly the same cap rules
as any other write, so a proposal that does not fit is rejected whole rather
than truncated. `[memory] consolidate_every_turns = 0` turns it off completely,
by hand as well as on the counter. `luna memory consolidate` runs a pass now
instead of waiting for the count, and `--dry-run` shows you what one would
write without writing any of it.

Two deliberate departures from Hermes. It outsources semantic retrieval to a
paid cloud service (Postgres + pgvector + a second LLM), which is not viable on
this hardware — and **nor is the local embedding index this file used to claim
was already here**. Tier 2 is keyword search today; semantic recall is still
Phase 3. And Hermes has no decay, so memories only get tidied at the cap; Luna
scores salience and lets trivia age out.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for how to run the daemon and both test suites, the branch → PR → merge rule,
and the one hard rule of this codebase: **a test must never reach the real
terminal, notifier, `aplay`, `hyprctl` or piper.** It has twice failed to hold,
and CONTRIBUTING.md explains the mechanism so it does not happen a third time.

Luna runs as you, on your machine, and a dispatched job runs with permissions
bypassed. [SECURITY.md](SECURITY.md) describes that blast radius honestly, and
how to report a vulnerability privately.

## Licence

[MIT](LICENSE). Piper (GPL-3.0) is invoked as a separate binary over a pipe and
does not affect this licence.
