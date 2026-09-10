# `luna update`, and the settings app — working notes

Scratch notes for the `luna-selfupdate-settings` branch. `docs/STATE-OF-PLAY.md`
is not touched from here.

## Part 1 — `luna update`

### What it does

`lunad/update.py` holds an `Updater`; `bin/luna` holds `cmd_update`, which is
thin the way every other subcommand is thin. Six steps, in this order, and the
order is the promise:

1. **Survey.** `git fetch --prune`, then read where the checkout stands. A
   fetch touches `.git/refs/remotes` and nothing else, so `--check` is exactly
   this step and stops here.
2. **Refuse, if anything is in the way.** Dirty tree, detached HEAD, no
   upstream, unreachable remote, diverged branch. Each refusal names the thing
   and, where there is one, the command that fixes it.
3. **Ask.** `lunad/confirm.py`, class `system_config`, hard denials read off
   the detail first exactly as `server.op_confirm` does it.
4. **Test.** The suite runs against the commit that has *not landed yet*, in a
   throwaway `git worktree`.
5. **Fast-forward.** `git merge --ff-only`, and nothing else.
6. **Restart and verify.** `systemctl --user restart lunad`, then wait for the
   socket to answer a real `ping` *and* for `state` to be republished.

### Three decisions worth the words

**No `reset --hard`, anywhere.** The obvious shape is fast-forward → test →
roll back if red, and the rollback is a `reset --hard`. Putting that primitive
in the self-update path is how an assistant eventually eats an afternoon of
someone's work. Running the suite in a scratch worktree instead means a red
update costs an `rm -rf` of a temporary directory and the live checkout is
never touched. Verified against a real repository: on a red suite, HEAD is
unchanged, the tree is clean, no worktree is left registered.

**No `--force` either.** The scratch worktree is cleaned with `rmtree` +
`git worktree prune` rather than `worktree remove --force`. The two are
equivalent here; the difference is that "there is no `--force` in the
self-update path" is now a fact a reader — and
`tests/test_update.py::NothingRanCase` — can check in one grep.

**The daemon has to be running.** The gate lives in the daemon: that is where
the pending queue is, and a toast answered with `luna confirm yes` talks to the
daemon, not to whichever terminal started the update. A broker built in the CLI
would put up a question nobody could answer and time out into a "no" that
looked like a policy decision. So with no daemon, `luna update` refuses (exit
69) and says to start it; `luna update --check` still works, because it asks
nothing and changes nothing. There is deliberately no flag that skips the gate
— `--no-tests` skips the suite and still has to be approved.

`Repo.available()` is deliberately *not* used: it also wants an authenticated
`gh`, because the workflow it guards ends in a pull request. A fast-forward
does not, and an update that refused because GitHub was logged out would be
lying about what stopped it.

### Skills — reported, not updated

Three kinds, three answers:

* **Repo skills** are symlinks from `~/.codex/skills/` into this checkout. A
  fast-forward updates their contents by definition. Nothing to do.
* **A skill this update ships for the first time** has no link yet, so it is
  invisible until someone makes one. That is silent breakage, so `skill_drift`
  names it and prints the `luna skills add` that fixes it. It is *not* linked
  automatically: `~/.codex/skills/` is the farm's, `luna skills` owns it, and
  one command owning one directory is worth more than the two seconds saved.
* **Cloned skills** under `~/.local/share/luna/skills/` are somebody else's
  repositories. Pulling them would run unreviewed third-party code under a
  bypass sandbox on the strength of *this* repository's green suite, which says
  nothing whatever about them. They are counted and named so that "up to date"
  never quietly means "except for those", and updated on purpose or not at all.

`skill_drift` reads directories and writes nothing.

### Two guard gaps found and closed

* `config.SYSTEMCTL_BIN` and `config.UPDATE_PYTHON` are new outward names and
  are disarmed in `tests/_support.py`. `UPDATE_PYTHON` is deliberately its own
  constant rather than a use of `VENV_PYTHON`: the suite disarms `VENV_PYTHON`
  to a path that does not exist, and an interpreter that "falls back to
  `sys.executable` when the venv is missing" would fall back to a *real*
  interpreter inside the test process and run the whole suite again,
  recursively, from a test. There is no fallback; a missing interpreter is a
  refusal.
* **`config.SOCKET_PATH` was never disarmed.** It is the live daemon's
  listener. Nothing was stopping a case that reached for it from connecting to
  the daemon the user is talking to — and, more quietly, a case that only asked
  `SOCKET_PATH.exists()` got a different answer depending on whether `lunad`
  happened to be running, which is a test that passes at a desk and fails in
  CI. Found exactly that way: two new CLI cases passed on this machine for the
  wrong reason. Now redirected into the same temp directory as `STATE_FILE`,
  with a case in `test_guards.py` asserting it.

## Part 2 — the settings app

### The audit

All ten panes construct (the module docstring says "nine"; `app.PANES` and
`panes.py` have had ten for a while). Every documented key reaches a control,
in the pane its section names. Nothing failed to build. What was wrong was
quieter.

**Three things were lying.**

1. **`[voice] voice_male` — the real one.** It renders as a dropdown next to
   "Primary voice" with its own preview button, and the doc read "The
   selectable alternate (flux-donovan-en, male)" — which reads like a live
   setting. Nothing reads it. `speech._voice_settings` builds its
   configuration from `voice` and never looks here. Worse,
   `docs/CONFIG-SCHEMA.md` explained the discrepancy with a sentence that was
   itself false — "picking it copies it into `voice`, which is what the daemon
   reads" — and no such code exists anywhere in the repository. Both now say
   what it actually is: a remembered second choice with a sample you can play,
   which changes nothing until you make it the primary.

2. **Fourteen ranges were narrower than the daemon's**, and the narrowing was
   destructive rather than conservative. `jarvis/config.py::_accept` is on the
   *read* path: a value the daemon accepts but the GUI's range rejects is not
   shown with a warning, it is **replaced by the GUI's default and written
   back** on the next save. So `luna settings set voice.speed 3.0` worked, she
   spoke faster, and it was silently reset to 1.0 the next time anyone opened
   this window and changed anything at all. Worst offenders:
   `voice.speed` 0.5–2.0 against 0.25–4.0, `memory.luna_cap_chars`
   500–20000 against 200–100000, `dispatch.max_parallel` 1–8 against 1–16.
   All fourteen are now the daemon's numbers.

3. **Two comments asserting the ranges matched reality were wrong.**
   `ambient.poll_seconds` said "there is no ceiling in the daemon — an hour is
   this app's"; there is one, and it is the same hour, so the numbers agreed
   and the stated reason did not. `ambient.battery_low_pct` justified 1–99 with
   "100 would mean always, so it is not a threshold" — a reasonable opinion,
   and not the daemon's, so a config file saying 100 was legal, worked, and was
   rewritten to 20.

**Two more, milder.** `memory.consolidate_every_turns` never mentioned the
five-minute floor `consolidate.turn()` also enforces, so setting it to 1 does
not mean one pass per turn. And the two `[ui]` keys sat in one group with no
doc between them, though one is this window's own palette and the other is a
desktop toast sent by the daemon — two different processes, nothing on screen
saying so. Both fixed.

**And the structural cause.** Nothing tested the GUI's schema against the
daemon's. `test_schema.py::ContractTest` checks the GUI against the *document*,
which records defaults — so defaults could not drift. `test_contract.py::
DriftCase` checks lunad's schema against `lunad/config.py`. Nothing compared
the two schemas to each other, so ranges drifted freely. That is what
`jarvis-settings/tests/test_daemon_contract.py` is for: keys, defaults, kinds,
choices and both ends of every range, equal in both directions.

### The slider — built

`[hud] scale` and `[voice] speed` are the two multipliers in the app, and both
were spin buttons. The comment in `schema.py` argued against a slider on the
grounds that it would be "a second control kind to keep themed for no gain".
The gain turned out to be concrete: **the `unit` these fields declared was
never drawn**. `Binder.bound_row` skipped the unit label for a `Real`, so
`[voice] speed` read `Speed  1.00` with nothing anywhere saying ×, and it had
no `doc` either. A track shows the range, a tick shows the default, and the
read-back carries the unit.

`schema.Slider` is a `Real` subclass rather than a heuristic, because "is this
range worth drawing" is a judgement about the setting and belongs next to it.
`[confirm] spend_threshold` stays a spin button: its range is 0–10000 and its
default is 0.25, so a track would put every value anyone wants inside the
first pixel. Its unit is now drawn too, spelled `USD` rather than `$` — the
unit is rendered after the number and "0.25 $" is not how anyone writes money.

**A bug fell out of it.** `theme.py` had `scale` CSS written months before
anything used it, and the handle rule set `min-width: 12px` without resetting
the stock theme's negative margin, which computes to **-4** and makes GTK warn
"sizes must be >= 0" on every allocation. Fixed with `margin: 0`.

### Visually

- A real slider for the two multipliers, with a tick at the default and a
  right-aligned read-back carrying the unit (`1.00x`). `hud.scale` no longer
  asks for the narrow column: a track needs its length, because the length *is*
  the range.
- Units drawn for every field that declares one, which had been three fields
  short.
- Marks and a disabled state for the scale, in the palette's own greys, so an
  inert track reads as inert rather than merely paler.
- Docs added to four fields that had none.

Nothing else was restyled. The app already matches §6d: typeset rather than
boxed, no cards inside cards, no accent bar on the active nav row, hierarchy
from the type scale, one colour and it means risk. `[ui] theme_follows_omarchy`
is honoured live — `ThemeWatch` holds a *callable*, re-read on every re-theme,
and `settings_applied` re-applies when the key changes.

### Testing panes at all

**GTK4 has no headless mode on this machine.** It segfaults on the first widget
constructed with no display, and `Gtk.init_check()` returns `True` anyway — the
call that looks like the guard is not one. `Gdk.Display.get_default()` is the
reliable probe. Broadway is present and works (`gtk4-broadwayd :7`, then
`BROADWAY_DISPLAY=:7` — note the *daemon's* number, not the socket's, which is
one higher), which is how the screenshots were taken without putting a window
on the user's desktop.

So `tests/test_panes.py` skips on `Gdk.Display.get_default() is None`. A
headless run is green with about thirty skips rather than red; `CONTRIBUTING.md`
and the CI comment now say so instead of claiming full headless coverage.

**A third guard gap.** `jarvis.config.CONFIG_PATH` was never disarmed. The
suite's `_support.py` reasoned that "Jarvis had nothing to reach with; it read
the user's files and wrote its own" — which quietly assumed no test would build
an `Editor`. A pane test does, and `Editor.set` writes the file back, so a case
that flips a switch to check the row below it dims would have rewritten the
user's live settings, which the daemon re-reads within two seconds. Redirected,
and asserted.

`Binder` now tags each control and each row with its dotted key, the way
`app.py` already tags a sidebar row. That is what lets a test say "turning
`voice.enabled` off dims `voice.provider`" instead of "the pane was built".
