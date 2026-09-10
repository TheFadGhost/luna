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
