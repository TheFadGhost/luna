"""Confirmation: every policy branch, both classifiers, and the hard denies.

The asker is injected throughout. A test that put a real toast on the user's
screen would be a test nobody could run twice, and one that shelled out to
``omarchy-notification-send`` would fail on a machine with no session — so the
delivery mechanism is exercised by asserting the *command line* it would build,
and the decision logic is exercised against a fake that answers instantly.
"""

from __future__ import annotations

import threading
import time
import unittest

from ._support import TempMemoryCase

from lunad import confirm as confirm_mod, config


class _Asker:
    """A user who always answers the same way, immediately."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.seen: list[tuple[confirm_mod.Pending, str]] = []

    def __call__(self, pending: confirm_mod.Pending, channel: str) -> None:
        self.seen.append((pending, channel))
        if self.answer is None:
            return                       # nobody clicks; the prompt times out
        pending.answer = self.answer
        pending.answered_by = "test"
        pending.event.set()


class ConfirmCase(TempMemoryCase):
    def broker(self, answer: bool | None = True,
               **policies: str) -> confirm_mod.ConfirmBroker:
        for key, value in policies.items():
            self.settings.set(f"confirm.{key}", value)
        self.asker = _Asker(answer)
        return confirm_mod.ConfirmBroker(settings=self.settings,
                                         audit=self.audit, asker=self.asker)

    def actions(self) -> list[str]:
        return [e["action"] for e in self.audit.read(newest_first=False)]


class PolicyBranchCase(ConfirmCase):
    def test_never_just_does_it_without_asking(self) -> None:
        broker = self.broker(delete_files="never")
        decision = broker.check("delete_files", "rm a.txt")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.outcome, "auto")
        self.assertEqual(self.asker.seen, [])
        self.assertIn("confirm.auto", self.actions())

    def test_deny_refuses_without_asking(self) -> None:
        broker = self.broker(delete_files="deny")
        decision = broker.check("delete_files", "rm a.txt")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, "denied")
        self.assertEqual(self.asker.seen, [])
        self.assertIn("confirm.denied", self.actions())

    def test_ask_approved(self) -> None:
        broker = self.broker(answer=True, delete_files="ask")
        decision = broker.check("delete_files", "rm a.txt", why="tidying up")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.outcome, "approved")
        self.assertEqual(len(self.asker.seen), 1)
        self.assertIn("confirm.asked", self.actions())
        self.assertIn("confirm.approved", self.actions())

    def test_ask_declined(self) -> None:
        broker = self.broker(answer=False, delete_files="ask")
        decision = broker.check("delete_files", "rm a.txt")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, "denied")
        self.assertIn("confirm.denied", self.actions())

    def test_ask_timing_out_is_a_no(self) -> None:
        self.settings.set("confirm.prompt.timeout_seconds", 1)
        broker = self.broker(answer=None, delete_files="ask")
        broker.clock = _fast_clock()
        decision = broker.check("delete_files", "rm a.txt")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, "timeout")
        self.assertIn("confirm.timeout", self.actions())

    def test_default_on_timeout_yes_is_honoured(self) -> None:
        self.settings.set("confirm.prompt.timeout_seconds", 1)
        self.settings.set("confirm.prompt.default_on_timeout", "yes")
        broker = self.broker(answer=None, delete_files="ask")
        broker.clock = _fast_clock()
        decision = broker.check("delete_files", "rm a.txt")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.outcome, "timeout")

    def test_an_unknown_class_is_asked_about_not_waved_through(self) -> None:
        broker = self.broker(answer=False)
        self.assertEqual(broker.policy("something_new"), "ask")
        self.assertFalse(broker.check("something_new").allowed)

    def test_every_documented_class_has_a_policy(self) -> None:
        broker = self.broker()
        for name in confirm_mod.CLASSES:
            self.assertIn(broker.policy(name), ("never", "ask", "deny"))

    def test_a_broken_asker_does_not_auto_approve(self) -> None:
        self.settings.set("confirm.prompt.timeout_seconds", 1)

        def explode(pending, channel):  # noqa: ANN001
            raise RuntimeError("no notification daemon")

        broker = confirm_mod.ConfirmBroker(settings=self.settings,
                                           audit=self.audit, asker=explode)
        broker.clock = _fast_clock()
        self.assertFalse(broker.check("delete_files", "rm a.txt").allowed)


class AnsweringCase(ConfirmCase):
    def test_an_answer_from_another_thread_unblocks_the_wait(self) -> None:
        self.settings.set("confirm.prompt.timeout_seconds", 30)
        held: list[confirm_mod.Pending] = []

        def hold(pending, channel):  # noqa: ANN001
            held.append(pending)

        broker = confirm_mod.ConfirmBroker(settings=self.settings,
                                           audit=self.audit, asker=hold)
        out: list[confirm_mod.Decision] = []
        worker = threading.Thread(
            target=lambda: out.append(broker.check("git_push", "git push")))
        worker.start()
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(held)
        self.assertEqual(len(broker.pending()), 1)
        self.assertTrue(broker.answer(held[0].token, True, by="user"))
        worker.join(timeout=5)
        self.assertTrue(out[0].allowed)
        self.assertEqual(out[0].outcome, "approved")
        self.assertEqual(broker.pending(), [])

    def test_answering_an_unknown_token_is_false_not_an_error(self) -> None:
        broker = self.broker()
        self.assertFalse(broker.answer("nope", True))


class ClassifierCase(TempMemoryCase):
    def assertClass(self, text: str, expected: str) -> None:
        self.assertIn(expected, confirm_mod.classify(text),
                      f"{text!r} should classify as {expected}")

    def test_installs(self) -> None:
        for text in ("pacman -S ripgrep", "run pip install requests",
                     "yay -S foo", "npm install --save-dev vitest",
                     "cargo install just"):
            self.assertClass(text, "install_packages")

    def test_deletes(self) -> None:
        for text in ("rm -f /tmp/x", "rm notes.txt",
                     "please delete the file called notes.txt",
                     "find . -name '*.log' -delete"):
            self.assertClass(text, "delete_files")

    def test_writes_outside_home(self) -> None:
        for text in ("write a unit to /etc/systemd/system/foo.service",
                     "echo hi > /usr/local/share/x"):
            self.assertClass(text, "write_outside_home")

    def test_system_config(self) -> None:
        for text in ("systemctl --user restart lunad",
                     "edit ~/.config/hypr/bindings.lua", "sudo anything"):
            self.assertClass(text, "system_config")

    def test_network_send(self) -> None:
        for text in ("curl -X POST https://example.com/api",
                     "scp report.txt server:/tmp",
                     "upload the log to the api"):
            self.assertClass(text, "network_send")

    def test_git_push(self) -> None:
        for text in ("git push origin main", "gh pr create --fill"):
            self.assertClass(text, "git_push")

    def test_ordinary_work_classifies_as_nothing(self) -> None:
        for text in ("read the README and summarise it",
                     "what is the uptime", "run the test suite",
                     "git status and git log"):
            self.assertEqual(confirm_mod.classify(text), [], text)

    def test_long_job_needs_an_estimate_not_a_guess(self) -> None:
        self.assertEqual(confirm_mod.classify("a big job"), [])
        self.assertIn("long_job",
                      confirm_mod.classify("a big job", seconds=900))
        self.assertNotIn("long_job",
                         confirm_mod.classify("a small job", seconds=10))

    def test_spend_uses_the_threshold(self) -> None:
        self.assertIn("spend", confirm_mod.classify("x", usd=1.0))
        self.assertNotIn("spend", confirm_mod.classify("x", usd=0.01))
        self.settings.set("confirm.spend_threshold", 5.0)
        self.assertNotIn("spend", confirm_mod.classify("x", usd=1.0))


class HardDenyCase(ConfirmCase):
    def test_restarting_omarchy_shell_is_refused(self) -> None:
        broker = self.broker(answer=True)
        for text in ("restart omarchy-shell",
                     "systemctl --user restart omarchy-shell",
                     "omarchy restart shell"):
            with self.assertRaises(confirm_mod.ConfirmDenied) as caught:
                broker.gate(text)
            self.assertEqual(caught.exception.decision.outcome, "hard")
        self.assertEqual(self.asker.seen, [], "a hard deny must not be asked")

    def test_deleting_customisations_is_refused(self) -> None:
        broker = self.broker(answer=True)
        with self.assertRaises(confirm_mod.ConfirmDenied):
            broker.gate("rm ~/.config/omarchy/CUSTOMISATIONS.md")
        with self.assertRaises(confirm_mod.ConfirmDenied):
            broker.gate("overwrite the CUSTOMISATIONS.md file")

    def test_reading_customisations_is_fine(self) -> None:
        broker = self.broker(answer=True)
        broker.gate("read ~/.config/omarchy/CUSTOMISATIONS.md and summarise it")

    def test_rm_rf_outside_own_dirs_is_refused(self) -> None:
        broker = self.broker(answer=True, delete_files="never")
        for text in ("rm -rf ~/Work/luna", "rm -rf /", "rm -rf $HOME/Documents",
                     "rm -rf"):
            with self.assertRaises(confirm_mod.ConfirmDenied):
                broker.gate(text)

    def test_rm_rf_inside_own_dirs_is_allowed(self) -> None:
        broker = self.broker(answer=True, delete_files="never")
        inside = str(config.JOBS_DIR / "abc123")
        self.assertEqual(confirm_mod.hard_denials(f"rm -rf {inside}"), [])
        broker.gate(f"rm -rf {inside}")

    def test_the_signal_deny_is_delegated_not_duplicated(self) -> None:
        """The fourth hard deny is lunad.safety and must stay there."""
        names = [rule.name for rule in confirm_mod.HARD_DENIES]
        self.assertNotIn("signal_unspawned", names)
        self.assertIn("lunad.safety", confirm_mod.SIGNAL_HARD_DENY)
        self.assertIn("signal_unspawned",
                      self.broker().snapshot()["hard_denies"])

    def test_a_hard_deny_is_audited(self) -> None:
        broker = self.broker(answer=True)
        with self.assertRaises(confirm_mod.ConfirmDenied):
            broker.gate("restart omarchy-shell please")
        entry = [e for e in self.audit.read()
                 if e["action"] == "confirm.hard_deny"]
        self.assertEqual(len(entry), 1)
        self.assertFalse(entry[0]["ok"])


class GateCase(ConfirmCase):
    def test_gate_raises_on_the_first_refusal(self) -> None:
        broker = self.broker(answer=False, delete_files="ask")
        with self.assertRaises(confirm_mod.ConfirmDenied) as caught:
            broker.gate("rm -f notes.txt")
        self.assertEqual(caught.exception.decision.action, "delete_files")

    def test_gate_returns_the_decisions_when_everything_is_allowed(self) -> None:
        broker = self.broker(answer=True, delete_files="ask", git_push="never")
        decisions = broker.gate("rm notes.txt and then git push origin main")
        self.assertEqual({d.action for d in decisions},
                         {"delete_files", "git_push"})
        self.assertTrue(all(d.allowed for d in decisions))

    def test_an_unclassified_task_asks_nothing(self) -> None:
        broker = self.broker(answer=False)
        self.assertEqual(broker.gate("summarise the README"), [])
        self.assertEqual(self.asker.seen, [])


class DeliveryCase(ConfirmCase):
    def test_the_notification_command_carries_a_click_to_approve(self) -> None:
        sent: list[list[str]] = []
        broker = confirm_mod.ConfirmBroker(settings=self.settings,
                                           audit=self.audit)
        broker._run_notify = lambda argv, pending: sent.append(argv)  # type: ignore[method-assign]
        pending = confirm_mod.Pending(token="tok123", action="delete_files",
                                      detail="rm notes.txt", why="", timeout_s=60)
        broker._ask_on_desktop(pending, "notification")
        self.assertEqual(len(sent), 1)
        argv = sent[0]
        self.assertEqual(argv[0], config.NOTIFY_BIN)
        self.assertIn("--exec", argv)
        exec_cmd = argv[argv.index("--exec") + 1]
        self.assertIn("confirm yes tok123", exec_cmd)
        self.assertIn("bin/luna", exec_cmd)
        self.assertIn("Luna needs a yes", argv)

    def test_the_headline_uses_the_configured_name(self) -> None:
        self.settings.set("assistant.name", "Jarvis")
        sent: list[list[str]] = []
        broker = confirm_mod.ConfirmBroker(settings=self.settings,
                                           audit=self.audit)
        broker._run_notify = lambda argv, pending: sent.append(argv)  # type: ignore[method-assign]
        broker._ask_on_desktop(
            confirm_mod.Pending(token="t", action="git_push", detail="", why=""),
            "notification")
        self.assertIn("Jarvis needs a yes", sent[0])

    def test_the_terminal_channel_sends_no_notification(self) -> None:
        sent: list[list[str]] = []
        broker = confirm_mod.ConfirmBroker(settings=self.settings,
                                           audit=self.audit)
        broker._run_notify = lambda argv, pending: sent.append(argv)  # type: ignore[method-assign]
        broker._ask_on_desktop(
            confirm_mod.Pending(token="t", action="git_push", detail="", why=""),
            "terminal")
        self.assertEqual(sent, [])

    def test_both_sends_the_notification_too(self) -> None:
        sent: list[list[str]] = []
        broker = confirm_mod.ConfirmBroker(settings=self.settings,
                                           audit=self.audit)
        broker._run_notify = lambda argv, pending: sent.append(argv)  # type: ignore[method-assign]
        broker._ask_on_desktop(
            confirm_mod.Pending(token="t", action="git_push", detail="", why=""),
            "both")
        self.assertEqual(len(sent), 1)

    def test_snapshot_reports_the_live_policy(self) -> None:
        broker = self.broker(git_push="never")
        snap = broker.snapshot()
        self.assertEqual(snap["policies"]["git_push"], "never")
        self.assertEqual(snap["policies"]["delete_files"], "ask")
        self.assertEqual(snap["channel"], "notification")
        self.assertEqual(snap["default_on_timeout"], "no")


def _fast_clock():
    """A monotonic clock that runs 200x, so a 60 s timeout costs 0.3 s."""
    start = time.monotonic()
    return lambda: start + (time.monotonic() - start) * 200.0


if __name__ == "__main__":
    unittest.main()
