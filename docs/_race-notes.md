# The dispatch flake, and what was under it

Working notes from chasing the `luna-p5-integration` CI failure on PR #32:
`AdmissionLeakTests.test_watch_finishes_its_own_bookkeeping_despite_a_bad_admission`
failed on the Python 3.12 job only, `'running' != 'queued'`, with 3.11, 3.13
and 3.14 green on the same commit. Everything below was measured on this
machine (Yoga Slim 7, 6 cores, 8 GB) on 2026-09-10. Nothing here is estimated.

## 1. Reproducing it

It does not reproduce bare. Repeating the single test is useless — 40 runs at
`nice 19` under 36 busy processes gave 0 failures, and so did 96 concurrent
copies of it on 6 cores.

**CPU contention alone cannot reproduce it, and this is the interesting part.**
The job's process is a chain of four forks (`bash run.sh` → `timeout` →
the fake agent → `tee`). Starve the cores and that chain is starved *with* the
Python process, because it inherits its scheduling. Measured under
`chrt --idle 0` with 10 spinners: the second `dispatch()` call took 0.5 s, but
the first job's process took 3–7 s to finish. The margin the test depends on
got *wider* under CPU load, not narrower.

What reproduces it is **I/O latency on the temp tree**. This laptop's `/tmp` is
tmpfs; a CI runner's is a real filesystem. The load model that works:

- `TMPDIR` pointed at a directory on the real disk (btrfs on `/home`),
- 6 busy-loop processes saturating the 6 cores,
- 4 processes doing 64 × 64 KiB `write` + `fsync` in a loop on that same
  filesystem,
- and the whole `tests.test_dispatch` module (89 cases) run in a loop.

Rates over 30 module runs, the same load both times — HEAD (`git archive` of
`2b823cf` into a scratch tree) against the working tree:

| case | before | after |
|---|---|---|
| `AdmissionLeakTests.test_watch_finishes_its_own_bookkeeping_despite_a_bad_admission` | **14 / 30** | 0 / 30 |
| `PlanTests.test_the_plan_id_reaches_disk_so_the_group_survives_a_restart` | **19 / 30** | 0 / 30 |
| `AuditTrailTests.test_a_dispatch_writes_spawn_and_finish_entries` | 2 / 30 | 0 / 30 |
| `AuditTrailTests.test_the_finish_entry_carries_the_exit_status` | 2 / 30 (`ValueError`) | 0 / 30 |
| module runs that were not `OK` | **24 / 30** | **0 / 30** |

Two more turned up under a different load — 12 concurrent full-suite runs on
tmpfs, 36 runs total — and are fixed here as well:
`test_a_dispatch_writes_spawn_and_finish_entries` (1 / 36) and, in a separate
pass of 7 contended full-suite runs,
`QueueTests.test_simultaneous_dispatches_cannot_both_take_the_last_slot`
(1 / 7). That last one was not a test artefact. See §3.

## 2. The margin, measured

`_runner_script` writes `sleep {int(linger)}`. The case passed `linger=0.3`,
and `int(0.3)` is `0` — so job A never lingered at all. Its whole life was the
time its own four-process chain took to start and exit:

| | job A's whole life | the next `dispatch()` call |
|---|---|---|
| tmpfs, idle | 12–38 ms | ~1 ms |
| real disk, idle | 15–27 ms | ~4 ms |
| real disk, 6 spinners + 4 fsync writers | 37–61 ms | 5–13 ms |

There is nothing holding those apart. The case was a coin flip on which of two
unsynchronised things finished first, and the coin was weighted by whatever the
filesystem was doing. Injecting a stall between the two dispatches settles the
ordering: 0.00 s → job B `queued` 5/5; 0.05 s → job B `running` 5/5.

The tell that it was never an over-admission bug: the same load also failed the
case one line *earlier*, at `assertEqual(job_a.state, "running")` with
`'finished' != 'running'`. That assertion has nothing to do with the admission
gate — it is job A's own process having already exited.

## 3. `_taken()` counted one job as two slots

Found sweeping the neighbours, and it is production code, not a test.

`_taken()` was `len(self._procs) + len(self._admitting)`. A job is in
`_admitting` from the admission decision until the `finally` at the end of
`_start`, and in `_procs` from the moment it holds a `Popen`. Those overlap —
and everything `_start` does after the spawn happens inside the overlap:
clearing the queue marker, writing `job.json`, the `dispatch.spawn` audit
entry, `log.info`, creating and starting the watcher thread. All disk, all
slow on a contended runner.

For the whole of that window one job occupied two slots. Deterministic proof —
spy on `_write_job`, which is called inside the window, and ask `_taken()` what
a dispatch arriving there would have seen, with exactly one job admitted and
`max_parallel = 2`:

```
HEAD                _taken() == 2   -> "limit reached", queues behind a free slot
this branch         _taken() == 1   -> correct
```

It never over-admits — the error is always conservative — but under
`max_parallel = 2` it leaves a genuinely free slot idle until something else
finishes and `_admit_next` runs, and with four simultaneous dispatches it
admitted one where it should have admitted two. That is the flake in
`test_simultaneous_dispatches_cannot_both_take_the_last_slot`, whose message
(`the gate let more than max_parallel through`) was pointing the wrong way:
the count was 1, not 3.

Fixed by counting the union of the two sets of job ids rather than adding
their lengths, with a deterministic regression case,
`test_a_job_handed_from_admitting_to_running_occupies_one_slot`, which fails
`[2] != [1]` against HEAD's `dispatch.py`.

## 4. What the tests were doing wrong

- **`test_watch_finishes...`** held its slot with `linger=0.3`, i.e. not at
  all. It now holds it on a condition: the fake agent honours
  `LUNA_TEST_GATE` and blocks until that file appears, so job A occupies the
  only slot for exactly as long as the case wants and then finishes on its
  own, exit code and all. Bounded by `run.sh`'s own `timeout`, and each job is
  registered for cleanup before the assertion about it, so a failing assertion
  cannot abandon a held job into a temp tree teardown is deleting.
- **`run_job`** returned as soon as `job.state` stopped being `running`. But
  `_watch` publishes the state *before* it writes the finish record, so a
  caller reading the audit log at that moment could find `dispatch.spawn` and
  not `dispatch.finish`. It now waits for the record. (The ordering in
  `_watch` is right and was left alone: the durable `job.json` before the log
  line.)
- **`test_the_plan_id_reaches_disk...`** dispatched a two-job plan with
  `linger=0` against `max_parallel = 1` and then asserted one of them was
  queued. Now `linger=20`, as its neighbour
  `test_a_fan_out_past_the_limit_queues_rather_than_stampedes` already did.

## 5. Dead ends, so they are not retried

- Repeating the failing test bare: 0 / 40. Do not bother.
- `nice 19` + 36 spinners: 0 / 40.
- 24 concurrent copies of the single test: 0 / 96.
- `chrt --idle 0` + spinners: 0 / 10, and see §1 for why it cannot work.
- 12 concurrent full-suite runs on tmpfs: 0 / 36 for this case (it did catch
  one of the audit siblings).
- Pointing `TMPDIR` at a directory under `$HOME` for a *full*-suite run fails
  12 guard cases in `test_guards`/`test_skills` — correctly:
  `test_every_ambient_path_is_redirected` asserts no redirected sentinel path
  starts with `Path.home()`. Use `/tmp` for full-suite runs and the on-disk
  `TMPDIR` only for `tests.test_dispatch`.

## 6. Where it stands

`python -m unittest discover -s tests -t .` — 1325 tests, six runs watched:
three clean (23.1 s, 23.2 s, 23.4 s) and three under 6 spinners + 4 fsync
writers (31.9 s, 31.5 s, 31.2 s). All `OK`. No stray `/tmp/luna-test-*` from
any run in this session.

`tests/_support.py` was not touched: `TERMINAL_BIN`, `NOTIFY_BIN` and
`APLAY_BIN` are still disarmed, `test_guards.py` still passes, and the
`TMPDIR`-under-`$HOME` mistake above is direct evidence those guards still
fire.
