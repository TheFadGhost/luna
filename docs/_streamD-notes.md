# Stream D — fan-out as a plan, and jobs that survive a restart

Working notes for the branch `luna-fanout-durable-jobs`. `docs/STATE-OF-PLAY.md`
is deliberately untouched — sibling agents are in this repo concurrently. The
two entries there this closes are §Next item 1 ("Worker fan-out as a *plan*")
and the known-limitation bullets "`dispatch` does not fan out *by itself*" and
"A queued job does not survive the daemon".

Base: rebased onto `35e89cc` (`luna-codex-brain`). The worktree was created from
`91d971b`, 55 commits behind; see §Rebase below.

---

## 1. Fan-out as a plan

### The criterion, and where it lives

`data/persona.md` §"Fanning out to several workers", appended as its own section
at the end of the file so it does not collide with the sibling agent's work in
§Delegation. The short form of it:

> Usually it is not worth it, and she says so. It only pays when the pieces are
> genuinely independent — no shared file, no ordering, no waiting on each
> other's output — and each piece is substantial enough to earn its own session.
> Two workers on one file is a merge conflict she asked for; four two-minute
> jobs behind `max_parallel = 1` is a queue with extra steps.

She prices the split against the serial version before proposing it, refuses the
split rather than splitting it badly when the pieces touch the same state, and
applies the same test to a fan-out the *user* asks for — one objection, then
their call, no re-litigating. That is the existing voice, not a new one.

`lunad/persona.py`'s `_CLOSING` names the `--and` flag as well. That block is the
only place she learns a command exists; a flag that is not named there may as
well not be built.

### The primitive

Small, and deliberately not a scheduler — the queue already is one.

| API | What it does |
|---|---|
| `Job.plan`, `Job.plan_index`, `Job.plan_size` | The group id and position, carried on the job so they reach `job.json` and survive a restart. `None` for an ordinary dispatch. |
| `Dispatcher.dispatch_plan(tasks, to=…, …) -> Plan` | Dispatches each task through the ordinary `dispatch()` under one generated `plan-xxxxxx` id. |
| `Plan` (dataclass) | `id`, `to`, `jobs`, `errors`, `to_dict()`. |
| `Dispatcher.plans()` / `plan_jobs(id)` | Plan id → job ids, in dispatch order. |
| `Dispatcher.cancel_plan(id) -> dict` | Stops every member; `{plan, found, cancelled, job_ids, already_over, refused, note}`. |
| `Dispatcher.announce_plan(plan)` | One line naming the group, not one line per job. |
| socket `dispatch` with `tasks: [...]` | The fan-out over the wire. Never waits. |
| socket `jobs` with `cancel: <plan id>` | Routed by looking the id up in `plans()`, not by its shape. |
| `luna dispatch "a" --and "b" --and "c"` | The CLI. `--and` is repeatable. |
| `luna jobs` | Shows `[plan-abc123 2/3]` and `[resumed]` on the timestamp column, and `INTR` in alert colour for an interrupted job. |

Two refusals are built into `dispatch_plan`, because a primitive that groups
anything makes the grouping meaningless:

- **A plan of one is refused.** That is a dispatch, and a group id on it would
  let `luna jobs` report a fan-out that did not happen.
- **A plan over `config.DISPATCH_PLAN_MAX` (8) is refused.** Not a resource
  limit — `max_parallel` is the resource limit — but a limit on how big a thing
  may be *called* a plan. Deliberately not a setting: past eight it is a to-do
  list, and the right answer is to say so, not to let the number grow in a
  config file.

**A plan is not atomic, and says so.** By the time a later task is refused the
earlier ones are real, running jobs. The first failure or denial stops the
fan-out where it is (the alternative is asking the user to say "no" five more
times), the jobs already dispatched stand, and the reason is in `Plan.errors`
and in the `dispatch.plan` audit entry.

**The gate still bounds the total.** Every job goes through the unchanged
`dispatch()`, so a plan of six against `max_parallel = 2` starts two and queues
four — asserted in
`PlanTests.test_a_fan_out_past_the_limit_queues_rather_than_stampedes`, which
also checks the ledger, so "queued" means "did not fork".

### A race found while building this

Cancelling a plan job-by-job frees a slot per cancel, and a freed slot admits
the next member **of the very plan being cancelled** — and a job between
"popped off the queue" and "holding a pid" sits in `_admitting`, where `cancel()`
cannot see it. So `cancel_plan` now marks the plan cancelled under the lock
*before* touching a single member, and `_start` refuses to fork a job whose plan
is in `_cancelled_plans`. `_cancelled_plans` is never cleared: a plan that was
cancelled stays cancelled. Test:
`test_a_plan_cancel_beats_a_job_that_is_already_being_admitted`, which
reproduces the window by hand rather than racing for it.

---

## 2. Durable queued jobs

### The store

The job directory, extended — not a second store. A queued job writes
`queued.json` beside its prompt holding the only two things that live nowhere
else: the watcher's `timeout`, and the `confirmed` decisions the user gave at
accept time. Everything else (`task.txt`, `system.txt`, `run.sh`, `job.json`) is
already written before a job is queued, precisely because a queued job is a real
job. The marker's *presence* is the claim: it is removed on admission, on
cancel, and on drop.

`Dispatcher.rehydrate(admit=True)` reads the tree at start-up.
`Daemon.__init__` calls it once, after construction and before the GC thread —
not in `Dispatcher.__init__`, because a constructor that spawns terminals is a
constructor no test can build safely.

### The decision: interrupted ≠ queued

**A job that was queued is requeued. A job that was running is never re-run.**

- **Queued, process never existed.** Nothing was spawned and no side effect
  exists, so resuming it cannot repeat anything. Requeued in original `started`
  order, carrying the confirmations *verbatim* — re-gating would date a human's
  decision to a restart, and the policy may have changed since. Marked
  `rehydrated`, audited as `dispatch.rehydrated`.
- **Running, process gone.** Never re-run, whatever the setting says. Its
  process is gone but its side effects are not: it may have written half a file,
  pushed a branch or installed something, and *nothing on disk records how far
  it got*. Re-running would repeat whatever it did do, on the user's machine,
  unattended. Recorded as `interrupted` — a terminal state whose whole content
  is "the outcome is unknown" — and re-dispatching is left to a human who can
  look at what it left behind. The brief's phrasing, "requeue only if it is
  known not to have started", is exactly the rule: a running job fails that test
  by definition.
- **Running, but `exit` is on disk.** Not interrupted at all. `run.sh` writes
  that file after the agent returns, so the job finished and the daemon died
  before the watcher wrote it down. The exit code is the job's own, so it is
  recorded as `finished`/`failed` rather than libelled as interrupted.
- **Running, process still alive.** Left completely alone. `close()`
  deliberately does not kill running jobs, so this is the normal case after a
  restart, and there is no repair to make. It still reads as `running`, and as
  `orphaned` once it dies, because this daemon never owned its pid.
- **Queued with no marker, or with no `run.sh`.** Not resumed on a guess:
  recorded as cancelled with the reason. Nothing invents a timeout.

Why not adopt a still-running job (poll its pid, write its outcome)? Because it
would be a second, weaker watcher for a process the daemon cannot `wait()` on,
and the existing `orphaned` state already tells the truth. Left unbuilt on
purpose.

### Visibility

- `luna jobs` shows `[resumed]` for anything rehydrated and `INTR` (in alert
  colour) for `interrupted`. The `rehydrated` flag is sticky and lives in
  `job.json`, so it survives further restarts.
- Audit: `dispatch.rehydrated` (resumed), `job.interrupted` (recorded, with a
  `resolution` field saying which of the two running cases it was),
  `dispatch.deferred` (left on disk by `close()`), `dispatch.cancel` (dropped,
  with the reason).

### The config key

`[dispatch] requeue_on_start = true`, `config.DISPATCH_REQUEUE_ON_START`.

- Read late, at both ends of a restart: `close()` and `rehydrate()`.
- In `docs/CONFIG-SCHEMA.md`'s toml block and its `[dispatch]` wiring table.
- In `lunad/settings.py`'s `SCHEMA`, so `tests/test_settings.py::ContractCase`
  covers it both ways (doc→module and module→doc).
- In `tests/test_contract.py::DriftCase.PAIRS`, so the default and its fallback
  constant cannot drift.
- Off restores the old behaviour *exactly*: the queue is cancelled on shutdown
  with the reason, and `rehydrate` drops what it finds. That path still has its
  own test.

`config.DISPATCH_PLAN_MAX` is deliberately **not** a setting; the reason is in
the constant's comment and above.

---

## 3. The desktop guard

Verified, not asserted. Every `subprocess.Popen`/`subprocess.run` argv across a
full run of the four modules this branch touches — `test_dispatch`,
`test_server`, `test_cli`, `test_persona`, 276 tests — was recorded on the final
tree. 166 forks, three distinct program names:

    /bin/bash
    luna-tests-must-pass-hypr=FakeHyprland
    luna-tests-must-pass-notify_bin=/bin/true

The last two are `tests/_support.py`'s unresolvable sentinels; each raises
`FileNotFoundError` inside `safety.spawn`, which `notify_finished` logs and
swallows. No `foot`, no `omarchy-notification-send`, no real `hyprctl`, no
`grim`, no `aplay`, no `systemctl`. The rehydration cases that assert on the
queue use `rehydrate(admit=False)` and fork nothing at all; the one end-to-end
restart case runs `run.sh` under `/bin/bash` in the temporary tree.

---

## 4. Rebase

The worktree was created from `91d971b`, 55 commits behind `luna-codex-brain`.
The work was committed, then `git rebase --onto 35e89cc 91d971b
luna-fanout-durable-jobs`. Two conflicts, both resolved to the newer base with
the Stream D additions re-applied on top:

- `lunad/dispatch.py` — `_start` had been rewritten to wrap its body in a
  `try/finally` that releases the `_admitting` reservation on *any* exception.
  Kept, and the plan-cancel guard was moved *inside* that `try` so the same
  `finally` releases the slot. The two `_clear_queue_marker` calls were
  re-applied to the new success and failure paths.
- `bin/luna` — the CLI had moved to `lunad/render`'s `Style` (`S.dim`, `S.bold`,
  `S.alert`, `render.columns`). The `_job_line` function this branch had edited
  no longer exists; the plan/resumed tags were re-applied to `_jobs_text`'s
  timestamp column so the column count stays six, and the fan-out output was
  rewritten off the deleted `DIM`/`RESET` constants.

Nothing in the 55 commits duplicated this work — `git grep rehydrat\|requeue\|
dispatch_plan` at `35e89cc` finds only the "not built" notes in the docs.

Suite: **1006 → 1046** (+40), each number from a run watched end to end —
1006 on `35e89cc` with this branch's tree checked out to the base, 1046 on
the branch.

---

## 4a. Found, not fixed

`Dispatcher._watch` overwrites a job's state with `finished`/`failed` from the
exit code whenever the terminal exits — including a job the user has just
*cancelled*, whose watcher then wakes and relabels it `failed`. So `luna jobs`
can show `FAIL` for something the user stopped on purpose. That is pre-existing
behaviour, not something this branch introduced, and the fix (have `_watch`
leave a `cancelled` state alone) touches the finish path that several other
tests and the `dispatch.finish` audit entry depend on. Left alone deliberately;
`PlanTests.test_one_cancel_stops_the_whole_group` asserts the guarantee a group
cancel actually makes — nothing left queued or running — and says why.

A second, milder one: a job that finishes and is reaped between a group cancel
starting and reaching that member is *refused* by the spawn firewall — "Luna did
not spawn it" — because the ledger entry has already been released. That is the
firewall working as designed on a job that is already over, so `cancel_plan`
records it in `refused` and carries on rather than aborting the group. Both
group-cancel tests assert the state the group ends in, not the route each member
took, and say so.

## 5. Deliberately left unbuilt

- **No adoption of a job that outlived its daemon.** See above.
- **No plan-level cost gate.** `estimate_seconds` / `estimate_usd` are per task
  and are gated per task, as documented on `dispatch_plan`. A plan of six costs
  six times what one of them says; the honest fix is a plan-level estimate the
  caller actually makes, and no caller makes one yet.
- **No dependencies between plan members.** A plan is a set, not a graph. The
  criterion says fan-out is only for genuinely independent pieces, so a plan
  that needs ordering is a plan that should not have been one.
- **No cross-daemon queue.** Rehydration is start-up only; two daemons against
  one jobs tree is not a supported arrangement and this does not make it one.
- **No `luna status` line for plans.** `snapshot()` carries `plans` and
  `requeue_on_start` for anything that wants them; the status renderer was left
  alone to keep the diff off a file two other streams are editing.
