"""`luna update` — every refusal, and the one path that goes through.

Nothing in this file runs `git`, `systemctl` or a second copy of the suite,
and that is not a claim resting on care. `tests/_support.py` replaces
``config.GIT_BIN``, ``config.SYSTEMCTL_BIN`` and ``config.UPDATE_PYTHON``
process-wide with names and paths that cannot resolve, and
``tests/test_guards.py`` asserts both the values and the shape. Every case here
injects the same :class:`~tests.test_vcs.FakeRunner` the version-control suite
uses — one git double for the whole repository, not two that drift — and then
asserts on the argv it recorded.

The negative is checked directly rather than assumed: :class:`NothingRanCase`
walks every command every case in this module produced and fails if a real
``merge``, ``systemctl`` or ``unittest`` ever appeared on a run that was
supposed to have refused. A self-update test that quietly restarted the user's
daemon would be the single worst bug this suite could ship.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from pathlib import Path

from ._support import (FORBIDDEN_SYSTEMCTL, FORBIDDEN_UNIT,
                       FORBIDDEN_UPDATE_PYTHON, TempMemoryCase)
from .test_vcs import FakeClock, FakeRunner

from .test_cli import cli

from lunad import confirm as confirm_mod
from lunad import config, update

HEAD = "a1b2c3d"
TARGET = "e4f5a6b"
UPSTREAM = "origin/main"

#: Every argv any case in this module produced, so `NothingRanCase` can check
#: the negative across the module rather than case by case.
SEEN: list[list[str]] = []


class Recorder(FakeRunner):
    def __call__(self, argv, cwd, timeout):
        SEEN.append([str(a) for a in argv])
        return super().__call__(argv, cwd, timeout)


class Asker:
    """Stands in for the desktop. Answers every question the same way."""

    def __init__(self, answer: bool | None = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def __call__(self, pending, channel) -> None:
        self.asked.append(pending.action)
        if self.answer is None:
            return                                   # never answered: a timeout
        pending.answer = self.answer
        pending.answered_by = "test"
        pending.event.set()


class UpdateCase(TempMemoryCase):
    """A checkout three commits behind a clean upstream, by default."""

    def setUp(self) -> None:
        super().setUp()
        self.runner = Recorder()
        self.clock = FakeClock()
        self.script()

    def script(self, *, branch: str = "main", upstream: str = UPSTREAM,
               ahead: int = 0, behind: int = 3, dirty: str = "",
               head: str = HEAD, target: str = TARGET) -> None:
        r = self.runner
        r.on("rev-parse --show-toplevel", str(self.root))
        r.on("rev-parse --abbrev-ref HEAD", branch or "HEAD")
        r.on("rev-parse --abbrev-ref --symbolic-full-name @{upstream}",
             upstream, rc=0 if upstream else 1)
        r.on("fetch", "")
        r.on("rev-parse --short HEAD", head)
        r.on(f"rev-parse --short {upstream}", target)
        r.on("rev-list --left-right --count", f"{ahead}\t{behind}")
        r.on("status --porcelain", dirty)
        r.on("log --no-color",
             "\n".join(f"{sha}\x1f{subject}" for sha, subject in
                       (("aaa1111", "teach her to speak first"),
                        ("bbb2222", "stop the dispatch suite racing itself"),
                        ("ccc3333", "docs: the suite is 1325, not 1212"))))
        r.on("merge --ff-only", "Fast-forward")
        r.on("worktree add", "")
        r.on("worktree prune", "")
        r.on("-m unittest", "OK")
        r.on("--user restart", "")

    def updater(self, *, answer: bool | None = True, policy: str = "ask",
                python: Path | None = None, probe: bool = True,
                republished: bool = True, **kw) -> update.Updater:
        self.settings.set("confirm.system_config", policy)
        self.asker = Asker(answer)
        broker = confirm_mod.ConfirmBroker(settings=self.settings,
                                           audit=self.audit,
                                           asker=self.asker)
        state = self.root / "state"
        if republished:
            state.write_text("idle")
        kw.setdefault("confirm", broker)
        return update.Updater(
            self.root, run=self.runner, audit=self.audit,
            settings=self.settings, state_file=state,
            python_bin=python if python is not None else _a_real_looking_path(),
            probe=lambda path, timeout: probe,
            clock=self.clock, sleep=self.clock.sleep, **kw)

    # -- helpers -----------------------------------------------------------

    def ran(self, prefix: str) -> list[list[str]]:
        return self.runner.ran(prefix)

    def actions(self) -> list[str]:
        return [e["action"] for e in self.audit.read()]

    def entry(self, action: str) -> dict:
        for e in self.audit.read():
            if e["action"] == action:
                return e
        self.fail(f"no {action} in the audit log: {self.actions()}")


def _a_real_looking_path() -> Path:
    """An interpreter path that exists, so the *runner* decides the verdict.

    The suite's own `config.UPDATE_PYTHON` sentinel does not exist, which is
    the guard: `test_target` refuses outright rather than forking anything.
    That is asserted on its own below. The cases that want to exercise what
    happens *after* the suite runs need to get past it, and they do it with a
    path that exists and is never executed, because the injected runner
    answers instead of `subprocess`.
    """
    return Path(__file__).resolve()


# =========================================================================
# Looking: `luna update --check`
# =========================================================================


class SurveyCase(UpdateCase):
    def test_a_clean_checkout_behind_its_upstream_has_an_update(self) -> None:
        survey = self.updater().survey()
        self.assertTrue(survey.available)
        self.assertFalse(survey.blocked)
        self.assertEqual(survey.behind, 3)
        self.assertEqual(survey.head, HEAD)
        self.assertEqual(survey.target, TARGET)
        self.assertEqual(survey.upstream, UPSTREAM)
        self.assertEqual(survey.remote, "origin")
        self.assertEqual([c.sha for c in survey.changes],
                         ["aaa1111", "bbb2222", "ccc3333"])
        self.assertEqual(survey.changes[0].subject,
                         "teach her to speak first")
        self.assertIn("3 commits behind origin/main", survey.summary())

    def test_a_survey_fetches_and_writes_nothing(self) -> None:
        self.updater().survey()
        self.assertTrue(self.ran("fetch"))
        for forbidden in ("merge", "reset", "stash", "clean", "checkout",
                          "pull"):
            self.assertEqual(self.ran(forbidden), [],
                             f"a survey ran `git {forbidden}`")

    def test_an_up_to_date_checkout_is_neither_available_nor_blocked(self) -> None:
        self.script(behind=0)
        survey = self.updater().survey()
        self.assertFalse(survey.available)
        self.assertFalse(survey.blocked)
        self.assertEqual(survey.summary(), "already up to date")
        self.assertEqual(survey.changes, ())

    def test_a_dirty_tree_blocks_and_names_the_paths(self) -> None:
        self.script(dirty=" M lunad/speech.py\n?? notes.md\n")
        survey = self.updater().survey()
        self.assertFalse(survey.available)
        self.assertTrue(survey.blocked)
        self.assertEqual(survey.dirty, ("lunad/speech.py", "notes.md"))
        blocked = survey.refusals[0]
        self.assertIn("lunad/speech.py", blocked)
        self.assertIn("notes.md", blocked)
        self.assertIn("will not touch unfinished work", blocked)

    def test_a_dirty_tree_is_blocked_even_though_commits_are_waiting(self) -> None:
        """The two must not read the same. A blocked update is not an
        available one, and `--check` has to say so."""
        self.script(dirty=" M lunad/speech.py\n")
        survey = self.updater().survey()
        self.assertEqual(survey.behind, 3)
        self.assertFalse(survey.available)

    def test_a_diverged_branch_blocks_and_says_it_is_the_user_s_call(self) -> None:
        self.script(ahead=2, behind=3)
        survey = self.updater().survey()
        self.assertTrue(survey.blocked)
        self.assertIn("both moved on", survey.refusals[0])
        self.assertIn("2 local commit", survey.refusals[0])
        self.assertIn("yours to make", survey.refusals[0])

    def test_local_commits_with_nothing_incoming_are_not_a_refusal(self) -> None:
        self.script(ahead=2, behind=0)
        survey = self.updater().survey()
        self.assertFalse(survey.blocked)
        self.assertFalse(survey.available)
        self.assertEqual(survey.ahead, 2)

    def test_a_detached_head_blocks_before_anything_is_fetched(self) -> None:
        self.script(branch="")
        survey = self.updater().survey()
        self.assertTrue(survey.blocked)
        self.assertIn("detached", survey.refusals[0])
        self.assertEqual(self.ran("fetch"), [])

    def test_a_branch_with_no_upstream_blocks_and_names_the_fix(self) -> None:
        self.script(upstream="")
        survey = self.updater().survey()
        self.assertTrue(survey.blocked)
        self.assertIn("tracks nothing", survey.refusals[0])
        self.assertIn("--set-upstream-to", survey.refusals[0])
        self.assertEqual(self.ran("fetch"), [])

    def test_an_unreachable_remote_blocks_and_says_which(self) -> None:
        self.runner.on("fetch", rc=1, err="could not resolve host github.com")
        survey = self.updater().survey()
        self.assertTrue(survey.blocked)
        self.assertIn("could not reach origin", survey.refusals[0])
        self.assertIn("could not resolve host", survey.refusals[0])

    def test_not_a_checkout_blocks_rather_than_raising(self) -> None:
        self.runner.on("rev-parse --show-toplevel", rc=128,
                       err="not a git repository")
        survey = self.updater().survey()
        self.assertTrue(survey.blocked)
        self.assertIn("not inside a git repository", survey.refusals[0])

    def test_a_logged_out_gh_does_not_stop_an_update(self) -> None:
        """`Repo.available()` wants an authenticated `gh` because its workflow
        ends in a pull request. A fast-forward does not, and an update that
        refused because GitHub was logged out would be lying."""
        self.updater().survey()
        self.assertEqual(self.ran("auth status"), [])

    def test_the_survey_serialises_for_json(self) -> None:
        payload = self.updater().survey().to_dict()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["behind"], 3)
        self.assertEqual(len(payload["changes"]), 3)
        self.assertEqual(payload["refusals"], [])


# =========================================================================
# Refusing
# =========================================================================


class RefusalCase(UpdateCase):
    def assert_untouched(self) -> None:
        """The checkout is exactly as it was, and she is still running."""
        self.assertEqual(self.ran("merge"), [])
        self.assertEqual(self.ran("reset"), [])
        self.assertEqual(self.runner.ran("--user restart"), [])

    def test_a_dirty_tree_refuses_without_asking_anybody(self) -> None:
        self.script(dirty=" M lunad/speech.py\n")
        out = self.updater().update(why="the user asked")
        self.assertFalse(out.ok)
        self.assertFalse(out.updated)
        self.assert_untouched()
        self.assertEqual(self.asker.asked, [],
                         "a refusal it could decide alone still put a "
                         "question on the user's screen")
        entry = self.entry("update.refused")
        self.assertIs(entry["ok"], False)
        self.assertEqual(entry["why"], "the user asked")
        self.assertIn("lunad/speech.py", entry["refusals"][0])

    def test_a_diverged_branch_refuses(self) -> None:
        self.script(ahead=2, behind=3)
        out = self.updater().update()
        self.assertFalse(out.ok)
        self.assert_untouched()
        self.assertIn("both moved on", out.refusals[0])

    def test_being_up_to_date_is_a_success_that_changes_nothing(self) -> None:
        self.script(behind=0)
        out = self.updater().update()
        self.assertTrue(out.ok)
        self.assertFalse(out.updated)
        self.assert_untouched()
        self.assertEqual(out.notes, ("already up to date",))
        self.assertIs(self.entry("update.checked")["ok"], True)

    def test_a_declined_confirmation_stops_everything(self) -> None:
        out = self.updater(answer=False).update()
        self.assertFalse(out.ok)
        self.assert_untouched()
        self.assertEqual(self.runner.ran("-m unittest"), [],
                         "the suite ran before the user had said yes")
        self.assertEqual(out.decision["outcome"], "denied")
        self.assertIn("declined", out.refusals[0])
        self.assertEqual(self.entry("update.refused")["outcome"], "denied")

    def test_a_deny_policy_stops_everything(self) -> None:
        out = self.updater(policy="deny").update()
        self.assertFalse(out.ok)
        self.assert_untouched()
        self.assertEqual(self.asker.asked, [])
        self.assertEqual(out.decision["policy"], confirm_mod.DENY)

    def test_no_answer_in_time_is_a_no(self) -> None:
        self.settings.set("confirm.prompt.timeout_seconds", 1)
        out = self.updater(answer=None).update()
        self.assertFalse(out.ok)
        self.assert_untouched()
        self.assertEqual(out.decision["outcome"], "timeout")

    def test_a_hard_deny_is_checked_before_the_broker_is_even_asked(self) -> None:
        """The unit name is what makes this reachable, and that is the point:
        the hard denials are read off the *detail* the way `server.op_confirm`
        reads them, so no broker — local or remote — can soften one."""
        out = self.updater(unit="omarchy-shell").update()
        self.assertFalse(out.ok)
        self.assert_untouched()
        self.assertEqual(out.decision["outcome"], "hard")
        self.assertEqual(self.asker.asked, [])
        self.assertEqual(self.entry("update.refused")["outcome"], "hard")

    def test_a_red_suite_refuses_and_leaves_the_checkout_alone(self) -> None:
        self.runner.on("-m unittest", rc=1,
                       err="FAILED (failures=2)\nRan 1325 tests")
        out = self.updater().update()
        self.assertFalse(out.ok)
        self.assertFalse(out.updated)
        self.assertTrue(out.tested)
        self.assertFalse(out.tests_ok)
        self.assert_untouched()
        self.assertIn("the suite is red", out.refusals[0])
        self.assertIn("exactly as you left it", out.refusals[0])
        self.assertIn("FAILED (failures=2)", out.tests_note)
        self.assertEqual(self.entry("update.refused")["outcome"], "red")

    def test_the_suite_runs_in_a_throwaway_worktree_not_this_one(self) -> None:
        """The reason there is no `reset --hard` anywhere in this module."""
        self.runner.on("-m unittest", rc=1)
        self.updater().update()
        added = self.ran("worktree add")
        self.assertEqual(len(added), 1)
        self.assertIn("--detach", added[0])
        self.assertIn(UPSTREAM, added[0])
        checked_out = Path(added[0][added[0].index("--detach") + 1])
        self.assertFalse(str(checked_out).startswith(str(self.root)),
                         "the update was checked out inside the live tree")
        self.assertTrue(self.ran("worktree prune"))
        self.assertFalse(checked_out.parent.exists())
        self.assertEqual(self.ran("worktree remove"), [],
                         "`--force` reached the self-update path")

    def test_a_missing_interpreter_refuses_instead_of_forking_one(self) -> None:
        """With no interpreter there is no verdict, and with no verdict there
        is no update. It never falls back to `sys.executable`: that is how a
        test would run the whole suite again from inside itself."""
        self.assertFalse(Path(config.UPDATE_PYTHON).exists())
        out = self.updater(python=Path(config.UPDATE_PYTHON)).update()
        self.assertFalse(out.ok)
        self.assert_untouched()
        self.assertIn("no interpreter at", out.tests_note)
        self.assertEqual(self.runner.ran("-m unittest"), [])

    def test_a_refused_fast_forward_is_reported_not_forced(self) -> None:
        self.runner.on("merge --ff-only", rc=1,
                       err="fatal: Not possible to fast-forward, aborting.")
        out = self.updater().update()
        self.assertFalse(out.ok)
        self.assertFalse(out.updated)
        self.assertEqual(self.runner.ran("--user restart"), [])
        self.assertIn("Not possible to fast-forward", out.refusals[0])
        for command in self.runner.calls:
            self.assertNotIn("--force", command)
            self.assertNotIn("-f", command)


# =========================================================================
# Going through
# =========================================================================


class AppliedCase(UpdateCase):
    def test_a_green_update_lands_restarts_and_verifies(self) -> None:
        out = self.updater().update(why="the user asked for an update")
        self.assertTrue(out.ok)
        self.assertTrue(out.updated)
        self.assertTrue(out.tested)
        self.assertTrue(out.tests_ok)
        self.assertTrue(out.restarted)
        self.assertTrue(out.ready)
        self.assertEqual(out.from_sha, HEAD)
        self.assertEqual(len(out.changes), 3)

    def test_it_fast_forwards_and_nothing_else(self) -> None:
        self.updater().update()
        merged = self.ran("merge --ff-only")
        self.assertEqual(len(merged), 1)
        self.assertIn(UPSTREAM, merged[0])
        for forbidden in ("reset", "stash", "clean", "rebase", "push"):
            self.assertEqual(self.ran(forbidden), [],
                             f"the update ran `git {forbidden}`")

    def test_the_order_is_ask_then_test_then_merge_then_restart(self) -> None:
        """The whole promise in one assertion: nothing is applied until the
        suite has been green on the commit that has not landed yet."""
        self.updater().update()
        order = [self.runner.line(c) for c in self.runner.calls]
        test_at = next(i for i, c in enumerate(order)
                       if c.startswith("-m unittest"))
        merge_at = next(i for i, c in enumerate(order)
                        if c.startswith("merge --ff-only"))
        restart_at = next(i for i, c in enumerate(order)
                          if c.startswith("--user restart"))
        self.assertLess(test_at, merge_at)
        self.assertLess(merge_at, restart_at)

    def test_the_restart_names_the_unit_and_the_user_bus(self) -> None:
        self.updater().update()
        restart = self.runner.ran("--user restart")[0]
        self.assertEqual(restart[0], FORBIDDEN_SYSTEMCTL)
        self.assertEqual(restart[1:], ["--user", "restart", FORBIDDEN_UNIT])

    def test_the_audit_records_the_range_the_why_and_the_outcome(self) -> None:
        self.updater().update(why="a new release landed")
        entry = self.entry("update.applied")
        self.assertIs(entry["ok"], True)
        self.assertEqual(entry["why"], "a new release landed")
        self.assertEqual(entry["from"], HEAD)
        self.assertEqual(entry["commits"], 3)
        self.assertIs(entry["restarted"], True)
        self.assertIs(entry["ready"], True)

    def test_a_daemon_that_never_answers_is_reported_not_assumed(self) -> None:
        out = self.updater(probe=False).update()
        self.assertFalse(out.ok)
        self.assertTrue(out.updated)
        self.assertTrue(out.restarted)
        self.assertFalse(out.ready)
        self.assertIn("nothing is listening", out.ready_note)
        self.assertIs(self.entry("update.applied")["ok"], False)

    def test_a_daemon_that_answers_but_never_republishes_is_a_failure(self) -> None:
        """Two questions, not one. A listener that never republishes `state`
        is a daemon that came up part-way, and one check would miss it."""
        out = self.updater(republished=False).update()
        self.assertFalse(out.ok)
        self.assertTrue(out.restarted)
        self.assertFalse(out.ready)
        self.assertIn("came up part-way", out.ready_note)

    def test_a_restart_that_fails_is_reported_against_a_landed_update(self) -> None:
        self.runner.on("--user restart", rc=1, err="Unit lunad not found.")
        out = self.updater().update()
        self.assertFalse(out.ok)
        self.assertTrue(out.updated)
        self.assertFalse(out.restarted)
        self.assertIn("did not restart", out.refusals[0])
        self.assertIn("Unit lunad not found", out.refusals[0])

    def test_no_restart_lands_the_code_and_leaves_her_running(self) -> None:
        out = self.updater().update(restart=False)
        self.assertTrue(out.ok)
        self.assertTrue(out.updated)
        self.assertFalse(out.restarted)
        self.assertEqual(self.runner.ran("--user restart"), [])

    def test_no_tests_still_has_to_pass_the_confirmation(self) -> None:
        out = self.updater(answer=False).update(run_tests=False)
        self.assertFalse(out.ok)
        self.assertFalse(out.updated)
        self.assertEqual(self.runner.ran("-m unittest"), [])
        self.assertEqual(self.ran("merge"), [])

    def test_the_outcome_serialises_for_json(self) -> None:
        payload = self.updater().update().to_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["from"], HEAD)
        self.assertEqual(len(payload["changes"]), 3)
        self.assertEqual(payload["decision"]["action"], "system_config")


# =========================================================================
# The gate cannot be got around
# =========================================================================


class GateCase(UpdateCase):
    def test_an_updater_given_no_broker_still_builds_one(self) -> None:
        """There is no "ungated" construction. The default is a real broker,
        not `None` and not a permissive stub."""
        up = update.Updater(self.root, run=self.runner, audit=self.audit,
                            settings=self.settings)
        self.assertIsInstance(up.confirm, confirm_mod.ConfirmBroker)

    def test_the_class_asked_under_is_the_one_the_user_already_set(self) -> None:
        self.assertEqual(update.CONFIRM_CLASS, "system_config")
        self.assertIn(update.CONFIRM_CLASS, confirm_mod.CLASSES)

    def test_the_question_says_what_will_happen_to_which_checkout(self) -> None:
        self.updater(answer=False).update()
        pending = [e for e in self.audit.read() if e["action"].startswith("confirm")]
        self.assertTrue(pending)
        detail = next(e for e in pending if e.get("detail"))["detail"]
        self.assertIn(str(self.root), detail)
        self.assertIn(HEAD, detail)
        self.assertIn(TARGET, detail)
        self.assertIn("3 commits", detail)
        self.assertIn("restart", detail)

    def test_every_outcome_reaches_the_audit_log_with_a_why(self) -> None:
        for setup, action in ((dict(answer=False), "update.refused"),
                              (dict(answer=True), "update.applied")):
            with self.subTest(action=action):
                self.setUp()
                self.updater(**setup).update(why="because")
                entry = self.entry(action)
                self.assertEqual(entry["why"], "because")
                self.assertIn("ok", entry)


# =========================================================================
# Skills
# =========================================================================


class SkillDriftCase(UpdateCase):
    def setUp(self) -> None:
        super().setUp()
        self.shipped = self.root / "skills"
        self.store = self.root / "store"
        self.shipped.mkdir()
        self.store.mkdir()

    def drift(self) -> tuple[str, ...]:
        return update.Updater(
            self.root, run=self.runner, audit=self.audit,
            settings=self.settings, skills_dir=self.shipped,
            store_dir=self.store).skill_drift()

    def test_a_newly_shipped_skill_with_no_link_is_named(self) -> None:
        (self.shipped / "diagnose-crash").mkdir()
        notes = self.drift()
        self.assertTrue(notes)
        self.assertIn("diagnose-crash", notes[0])
        self.assertIn("luna skills add", notes[0])

    def test_a_cloned_skill_is_counted_and_not_updated(self) -> None:
        clone = self.store / "somebody-elses-skill"
        (clone / ".git").mkdir(parents=True)
        notes = self.drift()
        self.assertTrue(any("other people's repositories" in n for n in notes))
        self.assertTrue(any("somebody-elses-skill" in n for n in notes))
        self.assertEqual(self.ran("-C " + str(clone)), [])
        self.assertEqual(self.ran("pull"), [])

    def test_reporting_drift_writes_to_nothing(self) -> None:
        (self.shipped / "omarchy").mkdir()
        before = sorted(p.name for p in self.shipped.iterdir())
        self.drift()
        self.assertEqual(sorted(p.name for p in self.shipped.iterdir()), before)
        self.assertFalse(config.CODEX_SKILLS_DIR.joinpath("omarchy").exists())

    def test_a_farm_with_everything_linked_says_nothing(self) -> None:
        self.assertEqual(self.drift(), ())


# =========================================================================
# The command line
# =========================================================================


class CliCase(UpdateCase):
    """`luna update` in a terminal: the text, the exit codes, the --json."""

    def setUp(self) -> None:
        super().setUp()
        self.cli = cli()

    def daemon_is_up(self) -> None:
        """A socket file where the command looks, and nothing listening on it.

        `cmd_update` only asks whether the path exists before deciding whether
        the gate is reachable; the gate itself is the injected broker. A real
        listener here would be a test connecting to something, which is the
        thing `tests/_support.py` redirects this path to prevent.
        """
        config.SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.SOCKET_PATH.touch()
        self.addCleanup(config.SOCKET_PATH.unlink, True)

    def run_cmd(self, updater: update.Updater | None = None,
                **flags) -> tuple[int, str, str]:
        args = argparse.Namespace(
            check=flags.pop("check", False), why=flags.pop("why", ""),
            no_tests=flags.pop("no_tests", False),
            no_restart=flags.pop("no_restart", False),
            timeout=300.0, json=flags.pop("json", False))
        made = updater if updater is not None else self.updater()
        out, err = io.StringIO(), io.StringIO()
        # `Updater()` is constructed inside the command, so the double goes in
        # by patching the class the command reaches for -- not by threading an
        # argument through a signature nothing else would ever use.
        original = update.Updater
        update.Updater = lambda *a, **kw: made          # noqa: ARG005
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.cli.cmd_update(args)
        finally:
            update.Updater = original
        return code, out.getvalue(), err.getvalue()

    def test_check_reports_an_available_update_and_exits_zero(self) -> None:
        code, out, _ = self.run_cmd(check=True)
        self.assertEqual(code, 0)
        self.assertIn("3 commit(s) waiting", out)
        self.assertIn("teach her to speak first", out)
        self.assertIn("luna update", out)

    def test_check_changes_nothing(self) -> None:
        self.run_cmd(check=True)
        self.assertEqual(self.ran("merge"), [])
        self.assertEqual(self.runner.ran("--user restart"), [])

    def test_check_exits_three_when_something_is_in_the_way(self) -> None:
        self.script(dirty=" M lunad/speech.py\n")
        code, out, _ = self.run_cmd(check=True)
        self.assertEqual(code, 3)
        self.assertIn("blocked", out)
        self.assertIn("lunad/speech.py", out)

    def test_check_says_so_when_there_is_nothing_to_do(self) -> None:
        self.script(behind=0)
        code, out, _ = self.run_cmd(check=True)
        self.assertEqual(code, 0)
        self.assertIn("already up to date", out)

    def test_check_json_is_clean_json(self) -> None:
        code, out, _ = self.run_cmd(check=True, json=True)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["behind"], 3)
        self.assertNotIn("\x1b", out)

    def test_it_refuses_when_she_is_not_running_to_answer_the_question(self) -> None:
        """No daemon, no gate, no update. There is deliberately no flag that
        turns this into "just do it": the whole point of routing through the
        broker is that a self-update cannot happen unasked."""
        self.assertFalse(config.SOCKET_PATH.exists())
        code, out, err = self.run_cmd()
        self.assertEqual(code, 69)
        self.assertIn("confirmation gate", err)
        self.assertIn("systemctl --user start lunad", err)
        self.assertEqual(self.ran("merge"), [])
        self.assertEqual(self.ran("fetch"), [])

    def test_check_still_works_with_no_daemon(self) -> None:
        self.assertFalse(config.SOCKET_PATH.exists())
        code, out, _ = self.run_cmd(check=True)
        self.assertEqual(code, 0)
        self.assertIn("3 commit(s) waiting", out)

    def test_a_successful_update_prints_the_range_and_exits_zero(self) -> None:
        self.daemon_is_up()
        code, out, _ = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertIn(HEAD, out)
        self.assertIn("suite green", out)
        self.assertIn("restarted, and she answered", out)

    def test_a_refusal_exits_three_and_says_why_on_stderr(self) -> None:
        self.daemon_is_up()
        self.script(dirty=" M lunad/speech.py\n")
        code, out, err = self.run_cmd()
        self.assertEqual(code, 3)
        self.assertIn("lunad/speech.py", err)
        self.assertIn("refused", out)

    def test_the_parser_wires_every_flag(self) -> None:
        ns = self.cli.build_parser().parse_args(
            ["update", "--check", "--why", "because", "--no-tests",
             "--no-restart"])
        self.assertEqual(ns.func.__name__, "cmd_update")
        self.assertTrue(ns.check)
        self.assertEqual(ns.why, "because")
        self.assertTrue(ns.no_tests)
        self.assertTrue(ns.no_restart)

    def test_json_survives_being_put_before_the_subcommand(self) -> None:
        for argv in (["--json", "update", "--check"],
                     ["update", "--json", "--check"]):
            with self.subTest(argv=argv):
                self.assertTrue(
                    self.cli.build_parser().parse_args(argv).json)


# =========================================================================
# The negative, across the whole module
# =========================================================================


class NothingRanCase(unittest.TestCase):
    """No case in this file produced an argv that could reach the machine."""

    def test_every_command_named_a_disarmed_binary(self) -> None:
        self.assertTrue(SEEN, "no commands were recorded at all")
        allowed = {config.GIT_BIN, FORBIDDEN_SYSTEMCTL,
                   str(_a_real_looking_path()), str(FORBIDDEN_UPDATE_PYTHON)}
        for argv in SEEN:
            self.assertIn(argv[0], allowed,
                          f"{argv[0]} is not a disarmed name: {argv}")

    def test_no_command_could_have_destroyed_work(self) -> None:
        for argv in SEEN:
            line = " ".join(argv)
            for weapon in (" reset ", " stash ", " clean ", " --force",
                           " push ", " -f "):
                self.assertNotIn(weapon, f" {line} ",
                                 f"a test produced `{line}`")

    def test_the_only_interpreter_ever_named_was_never_the_running_one(self) -> None:
        import sys  # noqa: PLC0415
        for argv in SEEN:
            self.assertNotEqual(argv[0], sys.executable,
                                f"a test nearly ran the suite again: {argv}")


if __name__ == "__main__":
    unittest.main()
