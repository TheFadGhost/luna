---
name: luna-dispatch
description: The mechanics of handing work to a worker - what `luna dispatch` actually does, how a job moves through queued/running/finished/interrupted/orphaned, why max_parallel makes most fan-outs a queue with extra steps, how to write a worker prompt that comes back usable, and what to do when a job is orphaned or the daemon restarted mid-run. Triggers - dispatch, fan out, fanout, worker, workers, in parallel, "split this up", "run these at once", luna jobs, plan id, max_parallel, queued, orphaned, interrupted, job directory, Sol, background task, "how long will this take".
---

# Dispatching work

Whether a split is worth making at all is settled in the persona (`data/persona.md`, "Fanning out
to several workers") — price the serial version, refuse a split that shares files or ordering,
one objection then the user's call. This is the other half: what the machinery actually does, and
the ways it disappoints someone who has not read it.

## What a dispatch is
`luna dispatch "<task>"` spawns a real agent in a terminal, on a hidden Hyprland special
workspace (`special:luna`, app-id `org.omarchy.luna`). Luna spawns it **herself** with `Popen` —
never `hyprctl dispatch exec` — because that is the only way she owns the pid and can honestly
claim it under the signal ledger. Window placement is a runtime Hyprland rule, digest-guarded so
it is not installed twice.

`--and "<second>" --and "<third>"` puts the group out under one **plan id**. Report the plan id
once, not a line per worker.

## `max_parallel` is the fact that decides most of this
`[dispatch] max_parallel` defaults to **1** and is read live at every admission decision, not
captured at startup. So a fan-out of four short jobs on the default setting is not four things
happening at once — it is a FIFO queue, with three windows' worth of overhead, finishing later
than doing the work serially would have. Check the setting before promising wall-clock.

Over-limit jobs are admitted immediately in the sense that matters — they get an id, a directory
and their prompt — and then sit `queued` in `luna jobs`. First in, first out. No lottery.

## The lifecycle, and the two states people misread
`queued → running → finished | interrupted | orphaned`

- **interrupted** — the daemon died while the job was running. It is recorded, and **never
  re-run**. Silently re-running a job that may have half-applied its changes is worse than
  reporting that it stopped, so nothing here retries on your behalf.
- **orphaned** — the process is gone and left no `exit` file. Treat it as "outcome unknown", not
  as failure: go and look at what it actually did to the tree before deciding.

`[dispatch] requeue_on_start` controls whether *queued-only* jobs rehydrate after a restart —
never running ones. `[dispatch] job_retention_days` ages out finished job directories on a
six-hour timer; `0` means never collect.

## Jobs are directories, so they survive
Each job is a directory under `~/.local/share/luna/jobs/`: `job.json`, `queued.json`, the prompt
as sent, and an `exit` file written when the process ends. That is on purpose — a queue held in
the daemon's memory would evaporate on restart, and the job directory is often the **only** record
of what a dispatched agent did. `luna jobs` lists them, `luna peek <id>` shows one,
`luna jobs --cancel <plan>` stops a whole group.

## Writing a prompt a worker can actually finish
A worker starts with none of the conversation. It has the repository and the sentence it was
given, and it cannot ask a follow-up question. So:

- **Name the files.** "Fix the tests" is a research project; "fix `tests/test_vcs.py::…`, the
  green gate now refuses an empty rollup" is a task.
- **State the definition of done**, including the command that proves it — the suite command and
  the expected count, not "make sure it works".
- **Say what not to touch.** Another worker, or another agent session, may be in the same tree.
  This is the single most common way a fan-out produces a merge conflict instead of parallelism.
- **Ask for a conclusion, not a transcript.** The report is read by a person; a worker that pastes
  files back has spent a session producing something nobody reads.

Workers are anonymous and disposable. **Sol is not a worker** — Sol is a named specialist with his
own persona (`data/sol-persona.md`) and his own memory namespace, reached with
`luna dispatch --to sol`, and he reports to Luna rather than to the user. Do not describe a
dispatched worker as "Sol" unless it was one.

## After the fan-out
Report the group's outcome **once**, when it has landed — not a narration per job. Having
dispatched, say so and stop; do not also do the job yourself while it runs, because two workers on
one file is a merge conflict that was asked for.

And verify before repeating a worker's claim. A report that says "I ran the suite and it passes"
is a claim, not a result: re-run the command and quote the real number. That has been wrong
before.
