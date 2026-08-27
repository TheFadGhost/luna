# Contributing to Luna

Luna is a resident assistant that runs on a live Linux desktop, with a voice,
real tools and the run of the machine. That shapes almost everything below: the
rules that look fussy are the ones that stop a test run from talking to you, or
opening windows, or killing a process it did not start.

Read `docs/ARCHITECTURE.md` for the design and `docs/STATE-OF-PLAY.md` for what
is actually built before proposing anything structural.

## Requirements

- **Python 3.11 or newer.** The floor is `tomllib`, which landed in 3.11.
  Development happens on 3.14; CI runs 3.11 through 3.14.
- **`lunad` and its test suite need no third-party packages.** Everything it
  imports is stdlib. If you are about to add a dependency, say why in the PR.
- The **`jarvis-settings`** GTK4 app needs PyGObject and GTK 4 from your
  system package manager (`python-gobject` + `gtk4` on Arch,
  `python3-gi` + `gir1.2-gtk-4.0` on Debian/Ubuntu). These are *system*
  packages, not pip installs — a virtualenv will not find `gi` unless it was
  created with `--system-site-packages`.
- Speech out additionally needs the piper venv described in
  `docs/ARCHITECTURE.md`. It is a runtime dependency only; no test needs it.

## Running the daemon

```sh
# Under systemd, the way it normally runs:
systemctl --user status lunad
journalctl --user -u lunad -f

# In the foreground, for development — logs to stderr, easy to Ctrl-C:
python3 -m lunad --debug
```

`lunad` refuses to start if SQLite was built without FTS5 (exit 78): tier-2
recall has no degraded mode worth having, so that is a start failure rather
than a warning.

Then talk to it:

```sh
bin/luna status
bin/luna ask "..."
bin/luna memory show
```

Configuration lives in `~/.config/jarvis/config.toml` and is documented in
`docs/CONFIG-SCHEMA.md`. That file is a **contract**: if you add a key there,
wire it, or mark it explicitly unwired in the same change. A settings app that
lies is worse than one with fewer settings.

## Running the tests

There are two suites. Both are plain `unittest` — no pytest, no runner script.

```sh
# The lunad suite, from the repository root:
python3 -m unittest discover -s tests -t .

# The jarvis-settings suite, from jarvis-settings/:
cd jarvis-settings && python3 -m unittest discover -s tests -t .
```

Both must be green before a PR is merged. The `jarvis-settings` suite runs
fully headless — it needs no display, no D-Bus and no session — but it does
need `gi` importable, so run it with the interpreter that can see your system
GTK packages.

## The outward-binary guard — do not weaken it

**This is the single most important rule in the repository.**

`tests/_support.py` disarms, process-wide, every configured binary name that
`lunad` uses to reach outside its own process: the **terminal**, the
**notifier**, **`aplay`**, **`hyprctl`**, and the **piper interpreter**. Each
is replaced with a name that cannot resolve, so a test that forgets to stub one
fails loudly, naming the fix, instead of doing the real thing.

It exists because both halves of it have already gone wrong on a live desktop:

- A `Dispatcher` whose terminal was bound as a **signature default** — which is
  evaluated once at import and therefore cannot be patched — opened three real
  `foot` windows per test run. Each segfaulted when teardown deleted the script
  it was executing, leaving three core dumps, three "Process crashed"
  notifications and stray `/tmp/luna-test-*` directories behind.
- The same mechanism, on the notifier, later put ten real desktop toasts
  carrying test-fixture text in front of the user.

So:

1. **Never bind a binary name as a default in a function signature.** Read it
   late, inside the function or constructor, so it can be patched.
   `tests/test_guards.py` asserts this with `inspect.signature` and will fail
   the build if you regress it.
2. **Never relax, skip or locally override the sentinels in `_support.py`** to
   make a test pass. If a test needs a working terminal, pass it `/bin/bash`
   explicitly, per test.
3. **A test must never reach the real terminal, notifier, `aplay`, `hyprctl`
   or piper.** Not "should usually not" — never.

Relatedly, `tests/test_safety.py` reads the actual source of `lunad/*.py` and
`bin/luna*` and fails if it finds a raw `subprocess.Popen(` outside
`safety.py`/`dispatch.py`, or a `.terminate()`/`.kill()` on a child. Every
spawn goes through the PID firewall and its ledger. `pkill -f` and anything
that matches a process by *name* is banned outright: pattern matching cannot
tell Luna's agent from the user's own shell, and a previous agent on this
machine killed its own terminal that way.

After any test run, a clean result means all of:

- both suites green,
- no new entries in `coredumpctl list`,
- no desktop notifications appeared,
- no `/tmp/luna-test-*` left behind.

If a run produces any of those, it is a bug in the test harness, not noise.

## Branching and pull requests

**No direct commits to `main`.** Every change — including one-line fixes and
documentation — goes:

```
branch  →  pull request  →  review  →  merge
```

Branch naming: `fix/`, `feat/`, `docs/`, `chore/`, `refactor/` plus a short
hyphenated description of the change, e.g. `fix/dispatch-watcher-leak`.

For the pull request:

- Fill in the template. The checklist is short and every item on it maps to a
  failure this repository has actually had.
- Explain the *mechanism* of a bug, not just the symptom. The commit messages
  in this repository are long on purpose; a future reader needs to know why the
  fix is the fix.
- If you found something that had no test, add the test. The terminal guard
  had to be fixed twice because nothing asserted it.
- Keep unrelated changes out. Stack a second branch instead.

## Style

- Follow the surrounding code. It is deliberate and it is consistent.
- Comments explain *why*, not what. Several of the longest comments in the
  codebase are load-bearing warnings; do not compress them away.
- Document dead ends. "We tried X, it does not work because Y" saves the next
  person the same afternoon.

## Security

Please read `SECURITY.md` before working on `dispatch.py`, `agent.py`,
`safety.py` or `audit.py`. Those four files are the whole of Luna's blast
radius, and a dispatched job runs with permissions bypassed.
