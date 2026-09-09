# Stream E notes

## Flaky test: `test_terminate_gives_up_cleanly_on_a_process_that_will_not_die`

Reproduced at ~5-9% failure (`assertFalse(confirmed)` seeing `True`) by running
the test 80-300 times in a loop.

Root cause: `lunad/safety.py`'s `_wait_dead(proc, grace)` always performed one
unconditional `proc.poll()` at the end, even when `grace <= 0`. With
`grace=0.0` (used only by this test, never by production code, which always
passes 1.5-5.0s), that "free" check raced the kernel's own SIGKILL delivery
and reap: a fast-dying `sleep 30` occasionally showed as already dead by the
time the immediate poll ran, most often on the post-escalation (SIGKILL) call
rather than the initial SIGTERM one (confirmed with an instrumented harness:
16/300 races were on the second `_wait_dead` call, 1/300 on the first).

Fix: `_wait_dead` now returns `False` immediately, with no poll at all, when
`grace <= 0`. "No time budget to observe death" now means exactly that,
deterministically, instead of "usually no time, unless the kernel got there
first." No production call site ever passes `grace=0`, so behavior there is
unchanged. Verified 200/200 on the target test and 3x 1006/1006 on the full
suite (`python3 -m unittest discover -s tests -t .`).

Checked the rest of `tests/test_safety.py` for the same pattern (grep for
`grace=0`): no other call site uses a zero grace. Also loop-tested
`test_terminate_confirms_death_even_when_it_has_to_escalate` (the other test
that races a signal against a just-spawned child's readiness) 80x with no
failures — its `time.sleep(0.2)` before signalling is a soft race in
principle, but the assertion (`assertTrue`) holds either way the race
resolves, so it isn't actually flaky. Left untouched.
