# Stream C — the GitHub workflow

What landed on `luna-github-workflow` (parent `35e89cc`): a full
branch → commit → push → pull request → wait-for-CI → merge-on-green
workflow, driven either from the CLI (`bin/luna vcs ...`) or from the daemon
op `vcs` that the CLI talks to.

## What landed

- **`lunad/vcs.py`** (new, ~1.3k lines) — `vcs.Repo`, built per request against
  a caller's working directory (never the daemon's own cwd, which is
  meaningless for `git`). Covers:
  - `branch()` — creates `<prefix>/<topic>-<yymmdd>`, or adopts an existing
    branch with the same name instead of suffixing it, so a re-run of the same
    job resumes rather than littering the repo with abandoned branches.
  - `commit()` — stages only the paths it was given; anyone else's dirty file
    is reported back as `left_alone`, never swept in.
  - `push()`, `pull_request()`, `open_issue()`, `comment()`.
  - `checks()` / `evaluate()` — the merge gate, as a pure function
    (`evaluate`) over a `gh pr view --json ...` payload, plus a bounded
    poll loop (`checks()`) that waits only while a verdict could plausibly
    still change.
  - `merge()` — refuses anything not green, confirmation gate checked only
    *after* the green gate (asking a question whose answer cannot matter
    trains the user to click through it).
  - `ship()` — the whole thing end to end, returning a report rather than
    raising when the merge is refused: everything up to the open pull request
    is real work and must not be thrown away by an exception.
- **`bin/luna`** — `luna vcs status|branch|commit|push|pr|checks|merge|issue|
  comment|ship`, exit code 3 (not 1) for "the workflow refused" so a script can
  tell a refusal from a crash.
- **`lunad/server.py`** — `op_vcs`, one op with an `action` field (matching the
  shape of `confirm`), plus a `vcs.VcsError` handler that turns a refusal into
  a normal reply instead of an error-counted exception.
- **`lunad/confirm.py`** — a new `git_merge` classifier, deliberately separate
  from `git_push`: pushing a branch is reversible and private, merging writes
  the default branch and (by default) deletes the branch that held the
  evidence.
- **`lunad/settings.py`** — `[policy] git_merge` (default `never`, since the
  user chose auto-merge-on-green) and a new `[vcs]` section: `branch_prefix`,
  `auto_merge`, `merge_method`, `delete_branch`, `notify_on_refusal`,
  `check_wait_seconds` (default 900s, `0` = read once and decide, not "wait
  forever" and not an off switch for the gate).
- **`docs/CONFIG-SCHEMA.md`** — documents the above, including the point that
  matters most: *what "green" means lives in code, not in settings, and
  nothing in `settings.py` can turn it off.*

## What "green" resolves to

`vcs.evaluate()` turns one `gh pr view` payload into a `CheckStatus` with a
list of refusal reasons, collected rather than short-circuited so a caller
learns everything wrong in one round trip. A merge is refused if any of:

- the PR is not `OPEN`, or is a draft;
- `mergeable` is not exactly `MERGEABLE` (`CONFLICTING` or `UNKNOWN` both
  refuse — `UNKNOWN` means GitHub has not finished computing it, which is not
  evidence the merge is safe);
- `mergeStateStatus` is not `CLEAN`;
- review decision is `CHANGES_REQUESTED` or `REVIEW_REQUIRED`;
- any check is failing, still pending, or in a state the gate does not
  recognise;
- **no check reported at all** — a repository with no CI is never merged
  automatically, deliberately, even though nothing failed;
- checks exist but none of them actually passed (all skipped/neutral).

`_still_settling()` decides whether waiting longer could change the verdict:
yes for a pending check, yes for an empty rollup (workflows take a few
seconds to register after a push, and refusing on the very first poll would
fail every fast `ship` against a repo that does have CI), yes for a merge
state GitHub is still computing. Everything else (a failed check, a draft) is
final immediately, so `checks()` never sleeps waiting on those.

`checks()`'s poll loop is time-bounded by `deadline = clock() + wait`
(`wait` defaults to `[vcs] check_wait_seconds`, 900s) and always terminates —
a PR whose checks never start costs the whole wait and is then refused,
which is the intended, documented behaviour (an unattended merge that gave
up waiting and merged anyway is the exact failure this module exists to
prevent). It is the one loop in the module (`grep -n "while True"
lunad/vcs.py` finds exactly one), and every subprocess call underneath it
goes through `subprocess.run(..., timeout=...)`, so nothing in `vcs.py` can
block indefinitely.

## Bugs found and fixed while finishing this branch

1. **The hang was in the test, not a real infinite loop in production.**
   `RepoCase.repo()` builds a `vcs.Repo` with the *real* `time.monotonic` /
   `time.sleep` (no `FakeClock` injected, unlike the `checks()`-specific
   tests that do pass `clock=`/`sleep=`).
   `test_ship_that_cannot_merge_still_leaves_the_pull_request` gave `ship()`
   an eternally-empty `statusCheckRollup`, which `_still_settling()` — correctly
   — treats as always worth waiting on. With no `wait=` argument, `ship()`
   used the real default patience (900s), so the test genuinely slept in
   wall-clock time until the deadline before returning — indistinguishable
   from a hang under a 120s test timeout. Fixed by passing `wait=0.0`
   explicitly in that one test (its assertions only care about the shape of
   the report, not timing; the "costs the whole wait" behaviour with a
   controllable clock is already covered separately by
   `test_checks_that_never_appear_cost_the_whole_wait_and_still_refuse`).
   Production code needed no change here — `checks()` was already bounded.

2. **Real production bug, found and fixed in the process:**
   `Repo.default_branch()` calls `self.gh("repo", "view", ...)` as its
   second-tier fallback, with `check=False`. `check=False` only suppresses
   `VcsError` for a non-zero exit; it does **not** suppress `VcsUnavailable`,
   which `run_process()` raises unconditionally when the binary itself can't
   be found. So on a repo with no remote configured and no `gh` reachable —
   exactly the situation the method's docstring says the final guess-loop
   fallback (`main`/`master`/`trunk`) exists for — `default_branch()` blew up
   instead of degrading gracefully, and every method that calls it
   (`branch()`, `commit()`, `push()`, `status()`) blew up with it. This is
   what `RealGitCase` was actually exercising (it drives real `git` against a
   remote-less temp repo, `gh` deliberately unreachable) and it caught five
   tests. Fixed by catching `VcsUnavailable` around that one `gh()` call and
   falling through to the guess loop, matching the documented three-tier
   fallback.

3. **Test bug, unrelated to the hang:**
   `test_the_default_branch_falls_back_to_gh_then_to_a_guess` tried to
   override `setUp`'s canned `symbolic-ref --short refs/remotes/origin/HEAD`
   answer with a shorter prefix (`"symbolic-ref"`). `FakeRunner` matches
   longest-prefix-first specifically so a case can narrow one command without
   restating the rest — which means a short override can never beat a longer
   entry already registered for the same call. The override never took
   effect; `default_branch()` kept answering from `setUp`'s `origin/main`.
   Fixed by overriding the same, fully-specific prefix `setUp` used.

None of the above touched `evaluate()`, `_still_settling()`, `merge()`,
`ship()`'s control flow, or any of the timeout wiring — the actual merge gate
is unchanged from what the previous pass built.

## Safety, verified rather than assumed

- `config.GIT_BIN` / `config.GH_BIN` are process-wide unresolvable sentinels
  set in `tests/_support.py` (`shutil.which(...)` is `None` for both), and
  `tests/test_guards.py::LateReadCase` asserts by `inspect.signature` that
  `vcs.Repo.__init__`'s `git_bin`, `gh_bin`, `notify_bin` parameters default
  to `None` — i.e. cannot be bound at import as signature defaults, which is
  the actual failure mode that let three `foot` windows and ten real toasts
  reach the desktop earlier in this project's life. Confirmed by reading and
  running both files; 22/22 guard tests pass.
- Instrumented `subprocess.Popen.__init__` and `subprocess.run` process-wide
  and ran the full `tests.test_vcs` module (137 tests) under it. Every
  distinct program name actually invoked: `/usr/bin/git` (real git, against
  `RealGitCase`'s throwaway temp repo with no remote), and the two forbidden
  sentinel names (`luna-tests-must-pass-git_bin=fake-runner`,
  `luna-tests-must-pass-gh_bin=fake-runner`, `luna-tests-must-pass-
  notify_bin=/bin/true`) — each of which is *attempted* and immediately fails
  with `FileNotFoundError`/`VcsUnavailable`, which is the guard doing its job,
  not a leak. The real `gh` (`/home/ghost/.local/share/mise/installs/gh/
  latest/.../gh`) never appears.

## Left unbuilt / out of scope for this branch

- No retry or backoff on a transient `gh` failure (rate limit, a flaky
  `502`) — a failed `gh` call inside `checks()`/`merge()` surfaces as
  whatever `Ran` it produced; there is no distinction yet between "gh said
  no" and "the network hiccuped once."
- `existing_pr()` and `slug()` still assume `gh` is reachable and don't
  degrade the way `default_branch()` now does; unlike `default_branch()`
  they aren't in the "must work even offline" path (`Repo.available()`
  already preflights `shutil.which(gh_bin)` before anything that needs gh
  for real), so this was left alone rather than expanded beyond what the
  failing tests required.
- No support for merge queues, required-reviewers beyond the single
  `reviewDecision` field GitHub returns, or repositories using rulesets
  instead of classic branch protection — `evaluate()` reads exactly the
  fields in `PR_FIELDS` and nothing else.
- `[vcs] check_wait_seconds` has one global default; there is no per-call
  override surfaced anywhere except `bin/luna vcs ship --wait` /
  `checks --wait` — a caller cannot configure "wait longer for this repo
  specifically" via settings.

## Test counts

Base (`35e89cc`): 1006 tests. This branch: 1145 tests (`tests/test_vcs.py`
alone contributes 137; the rest come from the `test_contract.py` and
`test_guards.py` additions). Full root suite
(`python3 -m unittest discover -s tests -t .`) run three times after the
fixes above: 1145 / 1145 / 1145, all green, ~19s each, no flakes observed.
