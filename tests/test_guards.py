"""The suite must not reach the machine it is running on.

There was no test for this, which is why it broke twice. First the terminal:
three ``foot`` windows on the live desktop every run, each segfaulting when
teardown deleted the ``run.sh`` it was executing. Then the notifier: wiring
`[ui] notify_on_finish` put roughly ten real Omarchy toasts on the user's
screen in one run, carrying test fixture text -- "fail please", "exit 2 — rm -f
/tmp/jarvis-test" -- because five test modules legitimately pass
``terminal="/bin/bash"`` so a job runs headlessly, and the job then genuinely
*finishes*.

Both were the same bug in the same shape: a binary name bound as a **signature
default**, which is fixed at import and therefore unpatchable. Stubbing the
object was not enough; the real program was reached anyway.

So this file asserts the shape, not just the values. A guard nobody tests is a
guard that is one refactor from being gone.
"""

from __future__ import annotations

import inspect
import shutil
import unittest
from pathlib import Path

from ._support import (FORBIDDEN_APLAY, FORBIDDEN_HYPRCTL, FORBIDDEN_NOTIFIER,
                       FORBIDDEN_PYTHON, FORBIDDEN_TERMINAL, FakeHyprland,
                       TempMemoryCase)

from lunad import config, confirm, dispatch, speech

#: Every ``config`` name that reaches the outside world, and what it would do
#: to the machine running the suite if it were the real thing.
DISARMED = {
    "TERMINAL_BIN": (FORBIDDEN_TERMINAL, "opens a window on the user's desktop"),
    "NOTIFY_BIN": (FORBIDDEN_NOTIFIER, "puts a toast on the user's desktop"),
    "APLAY_BIN": (FORBIDDEN_APLAY, "plays audio out of the user's speakers"),
    "HYPRCTL_BIN": (FORBIDDEN_HYPRCTL, "moves the user's workspaces"),
    "VENV_PYTHON": (FORBIDDEN_PYTHON, "forks a real 331 MB piper worker"),
}


class SentinelCase(unittest.TestCase):
    def test_every_outward_binary_is_disarmed(self) -> None:
        for name, (sentinel, harm) in DISARMED.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(config, name), sentinel,
                                 f"config.{name} is live; it {harm}")

    def test_no_sentinel_can_actually_resolve(self) -> None:
        # The point of the sentinel is that it fails loudly. One that happened
        # to name a real program would be worse than no guard at all, because
        # it would look disarmed and not be.
        for name, (sentinel, harm) in DISARMED.items():
            with self.subTest(name=name):
                if isinstance(sentinel, Path):
                    self.assertFalse(sentinel.exists(),
                                     f"config.{name} resolves; it {harm}")
                else:
                    self.assertIsNone(shutil.which(sentinel),
                                      f"config.{name} resolves; it {harm}")


class LateReadCase(unittest.TestCase):
    """No outward binary may be a signature default.

    This is the actual regression. A default is evaluated once, at import, so
    ``config.NOTIFY_BIN = <sentinel>`` in the test scaffolding cannot reach it
    and the constructor keeps handing out the real program's name forever.
    """

    SIGNATURES = (
        (dispatch.Dispatcher.__init__, ("terminal", "notify_bin")),
        (dispatch.Hyprland.__init__, ("hyprctl",)),
        (confirm.ConfirmBroker.__init__, ("notify_bin",)),
        (speech.Speech.__init__, ("aplay", "python")),
    )

    def test_outward_parameters_default_to_none(self) -> None:
        for func, names in self.SIGNATURES:
            params = inspect.signature(func).parameters
            for name in names:
                with self.subTest(func=func.__qualname__, param=name):
                    self.assertIn(name, params)
                    self.assertIsNone(
                        params[name].default,
                        f"{func.__qualname__}({name}=...) is bound at import; "
                        "read it from `config` in the body instead, or the "
                        "test suite cannot stop it reaching the real machine")


class ConstructionCase(TempMemoryCase):
    """Built with nothing specified, every object holds a sentinel."""

    def test_the_dispatcher_takes_both_sentinels(self) -> None:
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                agent_bin="/bin/true")
        self.assertEqual(d.terminal, FORBIDDEN_TERMINAL)
        self.assertEqual(d.notify_bin, FORBIDDEN_NOTIFIER)

    def test_the_confirm_broker_takes_the_notifier_sentinel(self) -> None:
        broker = confirm.ConfirmBroker(settings=self.settings, audit=self.audit)
        self.assertEqual(broker.notify_bin, FORBIDDEN_NOTIFIER)

    def test_the_compositor_handle_takes_the_hyprctl_sentinel(self) -> None:
        self.assertEqual(dispatch.Hyprland().hyprctl, FORBIDDEN_HYPRCTL)

    def test_speech_takes_the_aplay_and_python_sentinels(self) -> None:
        s = speech.Speech(settings=self.settings)
        self.addCleanup(s.close)
        self.assertEqual(s.aplay, FORBIDDEN_APLAY)
        self.assertEqual(s.python, FORBIDDEN_PYTHON)

    def test_an_explicit_value_still_wins(self) -> None:
        # The guard must not take the argument away from callers that need it:
        # five modules pass terminal="/bin/bash" on purpose, to run a job
        # headlessly. It is only the *default* that has to be unreachable.
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                agent_bin="/bin/true", terminal="/bin/bash",
                                notify_bin="/bin/true")
        self.assertEqual(d.terminal, "/bin/bash")
        self.assertEqual(d.notify_bin, "/bin/true")


class LiveFireCase(TempMemoryCase):
    """The path that actually toasted the user, run for real.

    `[ui] notify_on_finish` defaults to *on*, so this is what every finished
    job in the suite does. Nothing is stubbed here on purpose: the spawn is
    genuinely attempted and has to fail on the sentinel, be swallowed, and
    leave the desktop alone.
    """

    def test_a_finished_job_notifies_nothing_and_still_reports_finished(self) -> None:
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                agent_bin="/bin/true")
        self.assertTrue(self.settings.get("ui.notify_on_finish"),
                        "the risky default is what this case exists to cover")
        job = dispatch.Job(id="deadbeef", task="fail please", to="worker",
                           dir=self.root / "jobs" / "deadbeef",
                           exit_code=2, state="failed")
        # False: it tried, the sentinel did not resolve, and it gave up
        # quietly. A missing notifier is a desktop that cannot show a toast,
        # never a job that did not finish.
        self.assertFalse(d.notify_finished(job))

    def test_the_broker_survives_an_unreachable_notifier(self) -> None:
        broker = confirm.ConfirmBroker(settings=self.settings, audit=self.audit)
        self.settings.set("confirm.delete_files", "never")
        # "never" means no prompt at all, so this proves the ordinary path is
        # untouched by the guard; the notifier only matters on "ask", which
        # every other confirm test drives with its own stub.
        decisions = broker.gate("rm -f /tmp/nothing.txt", why="guard test",
                                actor="tests")
        self.assertTrue(all(d.allowed for d in decisions))


if __name__ == "__main__":
    unittest.main()
