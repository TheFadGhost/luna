---
name: luna-codebase
description: How to change Luna's own source without breaking her. The daemon/CLI split, why some state is a socket and some is a file, the external-binary guard that has escaped three times, where state lives on disk, and what must be read before touching the blast radius. Read before editing anything under ~/Work/luna. Triggers - lunad, bin/luna, luna daemon, luna.sock, SkillFarm, memory.py, dispatch.py, safety.py, audit.py, vcs.py, config.py, tests/_support.py, test_guards, "add a config key", "add a subcommand", "why is this test failing on CI but not here", 1212 tests, guard sentinel, signature default.
---

# Working on lunad

## Read these first, in this order
- `docs/ARCHITECTURE.md` — the design, and the reasons behind it.
- `docs/STATE-OF-PLAY.md` — what is *actually built*, which is not the same document.
- `SECURITY.md` — **required** before touching `dispatch.py`, `agent.py`, `safety.py` or
  `audit.py`. Those four are the whole of Luna's blast radius.
- `CONTRIBUTING.md` — the branch/PR rule, and the guard contract restated.

Never edit `docs/STATE-OF-PLAY.md` as a side effect of a change; it is written deliberately.

## The shape: a thin client and a daemon that owns everything
- `bin/luna` is a **dependency-free socket client**. It holds no state and knows nothing about
  memory or agents. What it does own is how output looks in a terminal — the rules are in
  `lunad/render.py`, and the short version is *alignment instead of boxes, weight instead of
  colour, one colour and it means something is wrong, and nothing but bytes when stdout is not a
  terminal*. Use `render.columns`, `Style.bold/dim/alert`, `render.fit`; never a raw box-drawing
  character, never a hardcoded width.
- `lunad/server.py` is the daemon. It is the only thing that touches memory or spawns an agent.
- The wire is NDJSON, one request and one response per line, over
  `$XDG_RUNTIME_DIR/luna/luna.sock` (`lunad/protocol.py`).
- `lunad/config.py` holds every path and constant. **Nothing else hard-codes a location.** If you
  find yourself typing a path anywhere else, that is the bug.

A new subcommand is normally a new daemon op plus a `cmd_*` in `bin/luna`. The exception is work
that touches nothing the daemon owns — `luna skills` is filesystem-only, so it runs in-process,
and it says so in its docstring. State-owning work in the client is always wrong.

## Socket for control, file for broadcast — and why
This trips people up because both exist and they look inconsistent. They are not.

- **CLI → daemon is a socket**, because it is a request expecting an answer.
- **Daemon → desktop is a plain file**, written atomically (temp file, then `os.replace`):
  `presence.py` writes `STATE_FILE`, `hud.py` writes `HUD_SETTINGS_FILE`, ambient writes
  `HUD_MESSAGE_FILE`. All in the tmpfs runtime dir.

A `subscribe` op over the socket was tried and **rejected**: a subscriber is a socket the daemon
has to write to, a socket has a buffer, and a stalled reader fills it — so the moment `speaking`
is published down a pipe nobody is draining, the publishing thread is the thread that was about
to speak. A file has no reader-side backpressure, Quickshell's `FileView` watches one with inotify
for free, and voxtype already uses the same convention on this machine.

Dispatched jobs follow the same instinct: a job is a **directory** under `JOBS_DIR` (`job.json`,
`queued.json`, the prompt, an `exit` file), not a queue in memory, so queued work survives a
daemon restart.

## The guard contract — the rule that has escaped three times
**Every name of an external binary is read late, in the function or constructor body, from
`config`. Never as a signature default, and never as a class attribute.**

```python
def __init__(self, git_bin: str | None = None) -> None:
    self.git_bin = git_bin or config.GIT_BIN      # late read; patchable
```

A default is evaluated once at import, so `tests/_support.py` setting `config.GIT_BIN` to a
sentinel cannot reach it and the object hands out the real program's name forever.

What that has actually cost, each time it escaped:
- three `foot` windows per suite run on the live desktop, each segfaulting when teardown deleted
  the script it was executing;
- roughly ten real Omarchy toasts in one run, carrying test fixture text;
- most recently a **class attribute** in `agent.py` (`CodexAdapter._CANDIDATES` plus a literal
  `shutil.which("codex")`), which resolved to the real CLI on this laptop and only failed on CI —
  the worst shape, because it passes locally.

`tests/test_guards.py` asserts the shape with `inspect.signature`, not just the values. When you
add an outward name: put it in `config`, read it late, add a `FORBIDDEN_*` sentinel in
`tests/_support.py`, and add it to `DISARMED` and `LateReadCase.SIGNATURES`. **Never weaken a
sentinel to make a test pass** — pass an explicit working binary (`terminal="/bin/bash"`) in that
one case instead.

The same rule covers paths that are not binaries but reach the machine anyway: `STATE_FILE`,
`JOBS_DIR`, `HUD_MESSAGE_FILE`, `CODEX_SKILLS_DIR`, the ambient coredump/power paths. `JOBS_DIR`
and `CODEX_SKILLS_DIR` are the sharp ones — one is deleted from, the other is unlinked from, and
neither failure has a visible symptom.

## Process safety
- `tests/test_safety.py` greps the source and **fails the build** on a raw `subprocess.Popen(`
  outside `safety.py`/`dispatch.py`, on `.terminate()`/`.kill()` against a child, and on any
  `pkill -f` or other name-based process matching.
- Everything spawned or signalled goes through `safety.may_signal`, which verifies pid *and*
  start time against the ledger. Luna signals only what she spawned.

## Where state lives
`STATE_DIR = $XDG_DATA_HOME/luna` (`~/.local/share/luna`):
`memory/LUNA.md` + `memory/USER.md` (tier 1), `memory/episodes.db` (tier 2, SQLite + FTS5),
`memory/profile.json` (tier 3, derived), `memory/sol/` (Sol's isolated namespace — Sol never
writes LUNA.md or USER.md), `audit.jsonl` (append-only, fsync'd, rotates), `spawned.json` (the
signal ledger), `jobs/`, `voices/`, `luna.log`.
Config is `~/.config/jarvis/config.toml` (dir 0700, file 0600) with `secrets.env` beside it.
Runtime, on tmpfs: `$XDG_RUNTIME_DIR/luna/{luna.sock,state,message,hud.json}`.

`docs/CONFIG-SCHEMA.md` is a **contract**: a new config key is wired in the same change, or
explicitly marked unwired. `lunad/settings.py` is its executable copy.

## Tests
- Plain `unittest`. No pytest, no runner, **no third-party dependency in `lunad`** (stdlib only,
  Python 3.11 floor for `tomllib`; CI runs 3.11–3.14).
- Root suite: `python3 -m unittest discover -s tests -t .` from the repo root.
- Second suite, local only because CI cannot import GTK4:
  `cd jarvis-settings && python3 -m unittest discover -s tests -t .`
- Every test module imports `tests/_support.py`; `TempMemoryCase` gives a throwaway tree and
  redirects the global ledger, audit log and settings singletons.
- The bar for "done": both suites green **with the counts stated**, no new `coredumpctl` entries,
  no desktop notification fired, no `/tmp/luna-test-*` left behind.

## Comment style
Comments here explain *why*, at length, and several of them are load-bearing warnings paid for in
real damage. Do not compress them away. When an approach is tried and abandoned, write down that
it does not work and why — the `subscribe` paragraph above exists because somebody did.
