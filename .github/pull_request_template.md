## What this changes

<!-- What the change is, in a sentence or two. -->

## Why

<!--
For a bug fix, explain the *mechanism*, not just the symptom: what the code
actually did, and why the fix is the fix. Someone reading this in a year needs
to be able to tell whether a later change would reintroduce it.
-->

## How it was verified

<!--
The commands you ran and what they said. "Tests pass" is not verification;
"484 tests OK, three consecutive runs, no core dumps" is.
-->

---

## Checklist

- [ ] **Both suites green** — `python3 -m unittest discover -s tests -t .` from
      the root, and the same from `jarvis-settings/`. Give the counts.
- [ ] **No new core dumps and no desktop notifications during the run.**
      Check `coredumpctl list` before and after, and watch the screen. This
      repository has twice shipped code that reached the real desktop from a
      test run — three `foot` windows once, ten notification toasts the next
      time — so this is checked explicitly, every time, not assumed.
- [ ] No stray `/tmp/luna-test-*` directories left behind after the run.
- [ ] **No binary name is bound as a signature default.** Outward-reaching
      names (terminal, notifier, `aplay`, `hyprctl`, piper) are read late so
      they can be patched. `tests/test_guards.py` enforces this — if you
      changed it, say why in the description.
- [ ] The guard sentinels in `tests/_support.py` were not weakened, skipped or
      overridden to make anything pass.
- [ ] If this fixes something that had no test, **a test was added**. The
      terminal guard had to be fixed twice because nothing asserted it.
- [ ] If a key was added to `docs/CONFIG-SCHEMA.md`, it is **wired** — or
      explicitly documented as unwired, with the reason. The schema is a
      contract.
- [ ] Docs updated in the same change if behaviour changed
      (`docs/ARCHITECTURE.md`, `docs/STATE-OF-PLAY.md`, `README.md`).
- [ ] No secrets, keys, tokens or private absolute paths in the diff.
- [ ] This branch contains one coherent change. Unrelated work is on its own
      branch.
