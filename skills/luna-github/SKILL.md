---
name: luna-github
description: How to move a change through GitHub with `luna vcs` — branch, commit named paths, push, open a pull request, wait for CI, and merge only on a genuinely green gate. Covers the rule that no checks at all is NOT green, why commit takes explicit paths, what exit code 3 means, and what to write in a PR body. Read before committing, pushing, opening a PR, merging, or shipping. Triggers - git, gh, commit, branch, push, pull request, PR, merge, squash, CI, checks, green, "is it green", "merge it", "ship it", luna vcs, luna vcs ship, main, default branch, rebase, issue, code review.
---

# The GitHub workflow

The whole flow is `lunad/vcs.py`, reached from the terminal as `luna vcs <verb>`. It shells out to
`git` and `gh`; there is no HTTP client and no token handling here.

## Never commit to the default branch
Branch first, always. `luna vcs branch <topic>` creates `luna/<topic>-<yymmdd>`; `--exact` uses the
name verbatim, `--base` cuts it from somewhere other than the current HEAD. For work in this repo
that a human will read as a contribution, `CONTRIBUTING.md` wants the prefixes `fix/`, `feat/`,
`docs/`, `chore/`, `refactor/` — pass those with `--exact`.

`luna vcs status` prints the branch in **red** when it is the default one. That is not decoration;
it is the one state where the next command is wrong.

## Commit named paths, never everything dirty
`luna vcs commit <paths...> -m "…"` requires the paths. There is deliberately no "commit
everything dirty" mode, because Luna may be standing in somebody else's working tree — another
agent session, or a half-finished experiment of the user's. `.` is accepted when all of it really
is hers, and that is a decision to make consciously each time.

One coherent change per branch. Unrelated fixes noticed along the way go on their own branch.

## The green gate, and the part everyone gets wrong
`luna vcs merge` calls `checks()` first and refuses unless the rollup is genuinely green. The
verdict is a pure function, `vcs.evaluate()`, and `CheckStatus.green` is simply *no refusals*.

**Missing checks are not green.** These three all refuse:

- no checks reported on the pull request at all — *"a repository with no CI is never merged
  automatically"*;
- a rollup field that is absent entirely;
- every check skipped or neutral — *"the empty rollup wearing a hat"*.

Green means **at least one check actually passed**, and nothing failing, pending, or unknown. A
skipped check beside a passing one is fine.

The gate runs **before** the confirmation broker asks the user anything. A merge that would be
refused is never turned into a question — the user is not asked to approve something the rule
already rejected.

Do not work around a red or empty gate by merging in the GitHub UI, and do not report "CI passed"
on the strength of a repository that has no CI.

## Exit codes carry the verdict
- `0` — done.
- `3` — **the workflow refused**: a merge that was not green, a `ship` whose PR is open and
  unmerged. A refusal is not a crash and does not exit 1.
- `1` — something actually failed.

So `luna vcs checks --wait 600; echo $?` is a usable gate in a script. `--wait 0` looks once.

## `ship` is the whole thing, and a refused merge is not a failure
`luna vcs ship <paths...> --topic … -m … --title … --body …` does branch → commit → push → PR →
merge-if-green in one call. When the gate refuses, the rest still happened: **the pull request
stays open and the reasons are printed.** Do not re-run `ship` to "try again" — read the refusals,
fix the cause, push, and re-check.

`--no-merge` opens the PR and stops. Use it when a human should look first.

## Write a real pull request body
`--body` is required for a reason. A PR body that restates the diff is worse than none: the diff
is already there. Say what changed, **why**, what was considered and rejected, and what a reviewer
should be suspicious of. A generated body is explicitly worse than a written one.

This repo's `.github/pull_request_template.md` is a checklist to actually satisfy, not a form to
delete: both suites green with the counts stated, no stray core dumps or notifications, no
`/tmp/luna-test-*` left behind, no binary bound as a signature default, guard sentinels in
`tests/_support.py` not weakened, new tests for any previously-untested fix, `docs/CONFIG-SCHEMA.md`
keys wired or explicitly noted unwired, docs updated in the same change, no secrets.

Note that CI runs the **lunad** suite only, on Python 3.11–3.14. The `jarvis-settings` suite needs
GTK4 and stays a local requirement — so "CI is green" does not mean both suites are. Run the
second one and say the number.

## The rest
`luna vcs pr` shows the PR already open for this branch rather than opening a second one.
`luna vcs issue <title>` and `luna vcs comment <number> <body> --on pr|issue` cover the rest.
`git` and `gh` are both disarmed under test with unresolvable sentinels — `gh` is authenticated on
this machine, and a merge is the one action here that cannot be taken back.
