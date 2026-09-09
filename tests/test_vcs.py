"""The GitHub workflow, and every way it refuses.

Nothing in this file touches the network, the user's repositories, or their
GitHub account, and that is not a claim resting on care alone:
``tests/_support.py`` replaces ``config.GIT_BIN`` and ``config.GH_BIN``
process-wide with names that cannot resolve, and ``tests/test_guards.py``
asserts both the values and the shape. Every case here injects a
:class:`FakeRunner` instead, which answers ``git`` and ``gh`` from a script and
records what it was asked. :class:`NeverGhCase` then checks the negative
directly: across the whole module, no case ever produced an argv that would
have reached a real ``gh``.

The one exception is :class:`RealGitCase`, which builds a throwaway repository
in a temporary directory and drives the real ``git`` against it. It has no
remote, so there is nothing for a push to reach even if one were attempted, and
``gh`` is not involved at any point. It exists because the staging rules — the
ones that stop her committing somebody else's dirty working tree — are only
worth anything if they hold against the real ``git status`` output rather than
against a fixture written by the same person who wrote the parser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

from ._support import FORBIDDEN_GH, TempMemoryCase

from lunad import config, confirm as confirm_mod, vcs

GIT = shutil.which("git")


# =========================================================================
# Doubles
# =========================================================================


class FakeRunner:
    """Answers ``git`` and ``gh`` from a script, and remembers the questions.

    Matching is on the arguments with the binary name and any ``-C <root>``
    removed, longest prefix first, so a case can override one narrow command
    without restating the rest of the repository.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.replies: dict[str, Any] = {}

    def on(self, prefix: str, out: str = "", rc: int = 0,
           err: str = "") -> "FakeRunner":
        self.replies[prefix] = (rc, out, err)
        return self

    def sequence(self, prefix: str, outs: Sequence[str]) -> "FakeRunner":
        """Answer differently each time, for the polling cases."""
        pending = list(outs)

        def answer() -> tuple[int, str, str]:
            return (0, pending.pop(0) if len(pending) > 1 else pending[0], "")

        self.replies[prefix] = answer
        return self

    def line(self, argv: Sequence[str]) -> str:
        args = [str(a) for a in argv][1:]
        if args[:1] == ["-C"]:
            args = args[2:]
        return " ".join(args)

    def ran(self, prefix: str) -> list[list[str]]:
        return [c for c in self.calls if self.line(c).startswith(prefix)]

    def __call__(self, argv: Sequence[str], cwd: Path,
                 timeout: float) -> vcs.Ran:
        self.calls.append([str(a) for a in argv])
        line = self.line(argv)
        for prefix in sorted(self.replies, key=len, reverse=True):
            if line.startswith(prefix):
                reply = self.replies[prefix]
                rc, out, err = reply() if callable(reply) else reply
                return vcs.Ran(tuple(str(a) for a in argv), rc, out, err)
        return vcs.Ran(tuple(str(a) for a in argv), 0, "", "")


class FakeClock:
    """Monotonic time that only moves when somebody sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


BRANCH = "luna/a-thing-260101"
PR_URL = "https://github.com/ghost/luna/pull/7"


def payload(**over: Any) -> dict[str, Any]:
    """A pull request GitHub would let you merge, before it is spoiled."""
    base: dict[str, Any] = {
        "number": 7, "title": "A thing", "url": PR_URL, "state": "OPEN",
        "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "reviewDecision": "", "headRefName": BRANCH, "baseRefName": "main",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "lunad suite (3.11)",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    }
    base.update(over)
    return base


def check(name: str = "ci", **over: Any) -> dict[str, Any]:
    entry = {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
             "conclusion": "SUCCESS"}
    entry.update(over)
    return entry


# =========================================================================
# The gate, as a pure function
# =========================================================================


class EvaluateCase(unittest.TestCase):
    """What "green" resolves to. No I/O of any kind reaches this."""

    def verdict(self, **over: Any) -> vcs.CheckStatus:
        return vcs.evaluate(payload(**over))

    def assertRefused(self, status: vcs.CheckStatus, fragment: str) -> None:
        self.assertFalse(status.green, f"expected a refusal: {status.summary()}")
        self.assertTrue(any(fragment in r for r in status.refusals),
                        f"no refusal mentioning {fragment!r}; got "
                        f"{list(status.refusals)}")

    # -- the one shape that passes ---------------------------------------

    def test_a_clean_open_pull_request_with_a_passing_check_is_green(self) -> None:
        status = self.verdict()
        self.assertTrue(status.green, status.summary())
        self.assertEqual(status.refusals, ())
        self.assertEqual(status.counts()["passing"], 1)
        self.assertIn("is green", status.summary())

    # -- the refusals the user asked for by name --------------------------

    def test_no_checks_at_all_is_not_green(self) -> None:
        """A repository with no CI configured must never auto-merge.

        The tempting reading of an empty rollup is "nothing failed". It is the
        reading that merges unreviewed work into main on every repository that
        has not got round to CI yet.
        """
        self.assertRefused(self.verdict(statusCheckRollup=[]), "no checks")

    def test_a_missing_rollup_field_is_not_green_either(self) -> None:
        # Absent is not the same as empty, and neither is evidence of success.
        self.assertRefused(self.verdict(statusCheckRollup=None), "no checks")
        self.assertRefused(vcs.evaluate({}), "no checks")

    def test_a_pending_check_is_not_green(self) -> None:
        status = self.verdict(statusCheckRollup=[
            check("suite", status="IN_PROGRESS", conclusion="")])
        self.assertRefused(status, "have not finished")
        self.assertEqual(status.counts()["pending"], 1)

    def test_a_queued_check_is_pending_not_absent(self) -> None:
        self.assertRefused(
            self.verdict(statusCheckRollup=[
                check("suite", status="QUEUED", conclusion="")]),
            "have not finished")

    def test_a_failing_check_is_not_green(self) -> None:
        status = self.verdict(statusCheckRollup=[
            check("suite", conclusion="FAILURE")])
        self.assertRefused(status, "failed")
        self.assertIn("suite", status.summary())

    def test_every_failing_conclusion_is_treated_as_a_failure(self) -> None:
        for bad in vcs.FAILED:
            with self.subTest(conclusion=bad):
                self.assertRefused(
                    self.verdict(statusCheckRollup=[
                        check("suite", conclusion=bad)]), "failed")

    def test_a_draft_is_not_green(self) -> None:
        self.assertRefused(self.verdict(isDraft=True), "draft")

    def test_a_closed_or_merged_pull_request_is_not_green(self) -> None:
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                self.assertRefused(self.verdict(state=state), "not open")

    def test_an_unmergeable_pull_request_is_not_green(self) -> None:
        self.assertRefused(self.verdict(mergeable="CONFLICTING"), "conflicts")

    def test_an_unknown_mergeable_state_is_not_green(self) -> None:
        # GitHub is still computing it. Waiting costs one poll; guessing costs
        # a merge of something that does not apply.
        self.assertRefused(self.verdict(mergeable="UNKNOWN"), "has not confirmed")
        self.assertRefused(self.verdict(mergeable=""), "has not confirmed")

    def test_a_merge_state_that_is_not_clean_is_not_green(self) -> None:
        for state in ("BLOCKED", "BEHIND", "DIRTY", "UNSTABLE", "UNKNOWN"):
            with self.subTest(merge_state=state):
                self.assertRefused(self.verdict(mergeStateStatus=state),
                                   "not CLEAN")

    def test_a_missing_merge_state_is_not_by_itself_a_refusal(self) -> None:
        # Older gh versions and thinner tokens do not return it. The check
        # rollup carries the weight; this field only ever adds a reason.
        self.assertTrue(self.verdict(mergeStateStatus="").green)

    def test_requested_changes_and_required_reviews_are_not_green(self) -> None:
        self.assertRefused(self.verdict(reviewDecision="CHANGES_REQUESTED"),
                           "asked for changes")
        self.assertRefused(self.verdict(reviewDecision="REVIEW_REQUIRED"),
                           "required review")

    def test_an_approval_does_not_stop_anything(self) -> None:
        self.assertTrue(self.verdict(reviewDecision="APPROVED").green)

    # -- the subtle ones --------------------------------------------------

    def test_an_all_skipped_rollup_is_the_empty_rollup_wearing_a_hat(self) -> None:
        """Something has to have actually passed.

        Skipped and neutral are tolerated, because path-filtered workflows skip
        constantly and a repository whose matrix skips two of six jobs should
        still be mergeable. But a rollup in which *everything* was skipped
        carries exactly as much evidence as no rollup at all, and letting it
        through would reopen the no-CI hole through a different door.
        """
        status = self.verdict(statusCheckRollup=[
            check("a", conclusion="SKIPPED"), check("b", conclusion="NEUTRAL")])
        self.assertRefused(status, "no check actually passed")

    def test_a_skipped_check_beside_a_passing_one_is_fine(self) -> None:
        status = self.verdict(statusCheckRollup=[
            check("a", conclusion="SKIPPED"), check("b", conclusion="SUCCESS")])
        self.assertTrue(status.green, status.summary())
        self.assertEqual(status.counts(), {"passing": 1, "tolerated": 1,
                                           "failing": 0, "pending": 0,
                                           "unknown": 0})

    def test_a_conclusion_nobody_recognises_is_refused_not_assumed(self) -> None:
        status = self.verdict(statusCheckRollup=[
            check("a", conclusion="SOMETHING_NEW")])
        self.assertRefused(status, "does not recognise")

    def test_status_contexts_are_read_as_well_as_check_runs(self) -> None:
        # gh returns a flat list mixing the two GraphQL types.
        passing = vcs.evaluate(payload(statusCheckRollup=[
            {"__typename": "StatusContext", "context": "buildkite",
             "state": "SUCCESS", "targetUrl": "https://x/1"}]))
        self.assertTrue(passing.green, passing.summary())
        self.assertEqual(passing.checks[0].name, "buildkite")
        pending = vcs.evaluate(payload(statusCheckRollup=[
            {"__typename": "StatusContext", "context": "buildkite",
             "state": "PENDING"}]))
        self.assertFalse(pending.green)

    def test_every_reason_is_collected_not_the_first_one(self) -> None:
        status = self.verdict(isDraft=True, mergeable="CONFLICTING",
                              statusCheckRollup=[check("a", conclusion="FAILURE")])
        self.assertGreaterEqual(len(status.refusals), 3)
        self.assertIn("not green", status.summary())

    def test_the_verdict_survives_a_round_trip_through_json(self) -> None:
        # It travels over the socket, so it has to be JSON-safe.
        status = self.verdict(statusCheckRollup=[check("a", conclusion="FAILURE")])
        again = json.loads(json.dumps(status.to_dict()))
        self.assertFalse(again["green"])
        self.assertEqual(again["counts"]["failing"], 1)


class BranchNameCase(unittest.TestCase):
    def test_names_are_prefixed_slugged_and_dated(self) -> None:
        name = vcs.branch_name("Stream C: GitHub workflow!",
                               prefix="luna", when=date(2026, 1, 2))
        self.assertEqual(name, "luna/stream-c-github-workflow-260102")

    def test_a_name_is_always_a_legal_ref(self) -> None:
        for nasty in ("", "   ", "..", "@{now}", "a//b", "~^:?*[", "-x-",
                      "x" * 200):
            with self.subTest(topic=nasty):
                name = vcs.branch_name(nasty, prefix="luna",
                                       when=date(2026, 1, 2))
                self.assertTrue(name.startswith("luna/"))
                for bad in vcs._BAD_REF_BITS:
                    self.assertNotIn(bad, name)
                self.assertNotIn(" ", name)
                self.assertLess(len(name), 80)


# =========================================================================
# The repository
# =========================================================================


class RepoCase(TempMemoryCase):
    """A repository made entirely of canned answers."""

    def setUp(self) -> None:
        super().setUp()
        self.runner = FakeRunner()
        self.runner.on("rev-parse --show-toplevel", str(self.root))
        self.runner.on("symbolic-ref --short refs/remotes/origin/HEAD",
                       "origin/main")
        self.runner.on("rev-parse --abbrev-ref HEAD", BRANCH)
        self.runner.on("rev-parse --short HEAD", "abc1234")
        self.runner.on("rev-parse --verify --quiet refs/heads/", rc=1)
        self.runner.on("status --porcelain", "")
        self.runner.on("diff --cached --name-only", "lunad/vcs.py\n")
        self.runner.on("repo view --json nameWithOwner",
                       '{"nameWithOwner": "ghost/luna"}')
        self.runner.on("pr list --head", "[]")
        self.runner.on("pr create", PR_URL + "\n")
        self.runner.on("pr view", json.dumps(payload()))
        self.runner.on("issue create",
                       "https://github.com/ghost/luna/issues/12\n")
        # Both gates open by default; the cases that care set their own.
        self.settings.set("confirm.git_push", "never")
        self.settings.set("confirm.git_merge", "never")

    def repo(self, **kw: Any) -> vcs.Repo:
        return vcs.Repo(self.root, run=self.runner, audit=self.audit,
                        settings=self.settings,
                        confirm=confirm_mod.ConfirmBroker(
                            settings=self.settings, audit=self.audit,
                            asker=lambda pending, channel: None),
                        **kw)

    def audited(self, action: str) -> list[dict[str, Any]]:
        return [e for e in self.audit.read() if e.get("action") == action]

    # -- where we are -----------------------------------------------------

    def test_the_default_branch_comes_from_git_before_gh(self) -> None:
        repo = self.repo()
        self.assertEqual(repo.default_branch(), "main")
        self.assertEqual(self.runner.ran("repo view --json defaultBranchRef"),
                         [], "gh was asked something git had already answered")

    def test_the_default_branch_falls_back_to_gh_then_to_a_guess(self) -> None:
        # Matching is longest-prefix-first (see `FakeRunner`), and `setUp`
        # already answers "symbolic-ref --short refs/remotes/origin/HEAD".
        # Overriding with the shorter "symbolic-ref" would lose to that more
        # specific entry and never take effect, so the override has to name
        # the same call `setUp` answered.
        self.runner.on("symbolic-ref --short refs/remotes/origin/HEAD",
                       rc=128, err="not a symbolic ref")
        self.runner.on("repo view --json defaultBranchRef",
                       '{"defaultBranchRef": {"name": "trunk"}}')
        self.assertEqual(self.repo().default_branch(), "trunk")

    def test_a_directory_that_is_not_a_repository_says_so(self) -> None:
        self.runner.on("rev-parse --show-toplevel", rc=128,
                       err="fatal: not a git repository")
        with self.assertRaises(vcs.NotARepository):
            _ = self.repo().root

    def test_status_answers_everything_in_one_call(self) -> None:
        self.runner.on("status --porcelain", " M lunad/vcs.py\n?? scratch.md\n")
        state = self.repo().status()
        self.assertEqual(state["branch"], BRANCH)
        self.assertEqual(state["default_branch"], "main")
        self.assertFalse(state["on_default"])
        self.assertEqual(state["dirty"], ["lunad/vcs.py", "scratch.md"])

    def test_a_rename_is_reported_at_its_new_path(self) -> None:
        self.runner.on("status --porcelain", "R  old.py -> new.py\n")
        self.assertEqual(self.repo().dirty(), ("new.py",))

    # -- branching --------------------------------------------------------

    def test_a_new_branch_is_created_from_where_she_is(self) -> None:
        result = self.repo().branch("a thing")
        self.assertTrue(result.created)
        self.assertTrue(result.name.startswith("luna/a-thing-"))
        self.assertEqual(len(self.runner.ran("switch --create")), 1)
        self.assertEqual(self.audited("vcs.branch")[0]["created"], True)

    def test_an_existing_branch_is_adopted_not_forked(self) -> None:
        """A re-run resumes; it does not leave `-2` and `-3` behind.

        Adopting is wrong only if two unrelated jobs picked the same topic on
        the same day. Suffixing is wrong every time a job is re-run, which is
        far more often, and it litters the repository with abandoned branches
        nobody deletes.
        """
        name = vcs.branch_name("a thing", prefix="luna")
        self.runner.on(f"rev-parse --verify --quiet refs/heads/{name}", name)
        self.runner.on("rev-parse --abbrev-ref HEAD", "main")
        result = self.repo().branch("a thing")
        self.assertFalse(result.created)
        self.assertEqual(result.previous, "main")
        self.assertEqual(self.runner.ran("switch --create"), [])
        self.assertEqual(len(self.runner.ran(f"switch {name}")), 1)

    def test_adopting_can_be_refused(self) -> None:
        name = vcs.branch_name("a thing", prefix="luna")
        self.runner.on(f"rev-parse --verify --quiet refs/heads/{name}", name)
        with self.assertRaises(vcs.VcsError):
            self.repo().branch("a thing", adopt=False)

    def test_a_generated_name_is_accepted_back_unchanged(self) -> None:
        # So `branch(result.name)` on the next run lands on the same branch.
        name = vcs.branch_name("a thing", prefix="luna")
        self.runner.on(f"rev-parse --verify --quiet refs/heads/{name}", name)
        self.assertEqual(self.repo().branch(name).name, name)

    def test_she_will_not_branch_onto_the_default_branch(self) -> None:
        with self.assertRaises(vcs.ProtectedBranch):
            self.repo().branch("main", exact=True)

    # -- committing -------------------------------------------------------

    def test_a_commit_stages_only_the_paths_it_was_given(self) -> None:
        commit = self.repo().commit("wire the merge gate", ["lunad/vcs.py"])
        self.assertEqual(commit.sha, "abc1234")
        add = self.runner.ran("add --")
        self.assertEqual(add, [[config.GIT_BIN, "-C", str(self.root), "add",
                                "--", "lunad/vcs.py"]])

    def test_nothing_in_this_module_ever_runs_git_add_all(self) -> None:
        """The rule, asserted rather than promised.

        `git add -A` is how another person's half-finished change ends up in
        her pull request, and there is no code path here that reaches it.
        """
        self.repo().commit("wire the merge gate", ["lunad/vcs.py"])
        for call in self.runner.calls:
            self.assertNotIn("-A", call)
            self.assertNotIn("--all", call)

    def test_a_commit_with_no_paths_is_refused_and_names_what_is_dirty(self) -> None:
        self.runner.on("status --porcelain",
                       " M someone-elses.py\n?? their-scratch.md\n")
        with self.assertRaises(vcs.VcsError) as caught:
            self.repo().commit("sweep it all up", [])
        message = str(caught.exception)
        self.assertIn("someone-elses.py", message)
        self.assertIn("their-scratch.md", message)
        self.assertEqual(self.runner.ran("commit"), [])

    def test_a_commit_reports_the_work_it_left_alone(self) -> None:
        self.runner.on("status --porcelain",
                       " M lunad/vcs.py\n M someone-elses.py\n")
        commit = self.repo().commit("mine only", ["lunad/vcs.py"])
        self.assertEqual(commit.files, ("lunad/vcs.py",))
        self.assertEqual(commit.left_alone, ("someone-elses.py",))
        self.assertEqual(self.audited("vcs.commit")[0]["left_alone"],
                         ["someone-elses.py"])

    def test_an_empty_message_is_refused(self) -> None:
        with self.assertRaises(vcs.VcsError):
            self.repo().commit("   ", ["lunad/vcs.py"])

    def test_staging_nothing_is_not_an_empty_commit(self) -> None:
        self.runner.on("diff --cached --name-only", "")
        with self.assertRaises(vcs.NothingToCommit):
            self.repo().commit("nothing changed", ["lunad/vcs.py"])
        self.assertEqual(self.runner.ran("commit"), [])

    def test_she_will_not_commit_on_the_default_branch(self) -> None:
        self.runner.on("rev-parse --abbrev-ref HEAD", "main")
        with self.assertRaises(vcs.ProtectedBranch):
            self.repo().commit("straight to main", ["lunad/vcs.py"])
        self.assertEqual(self.runner.ran("commit"), [])

    # -- pushing ----------------------------------------------------------

    def test_a_push_sets_the_upstream_and_is_audited(self) -> None:
        self.assertEqual(self.repo().push(), BRANCH)
        self.assertEqual(len(self.runner.ran(
            f"push --set-upstream origin {BRANCH}")), 1)
        entry = self.audited("vcs.push")[0]
        self.assertEqual(entry["branch"], BRANCH)
        self.assertIn("undo", entry)

    def test_she_will_not_push_the_default_branch(self) -> None:
        self.runner.on("rev-parse --abbrev-ref HEAD", "main")
        with self.assertRaises(vcs.ProtectedBranch):
            self.repo().push()
        self.assertEqual(self.runner.ran("push"), [])

    def test_a_denied_git_push_policy_stops_the_push(self) -> None:
        self.settings.set("confirm.git_push", "deny")
        with self.assertRaises(confirm_mod.ConfirmDenied):
            self.repo().push()
        self.assertEqual(self.runner.ran("push"), [])

    # -- pull requests ----------------------------------------------------

    def test_opening_a_pull_request_parses_its_number(self) -> None:
        pr = self.repo().pull_request(
            "Wire the merge gate",
            "The merge gate now refuses a pull request whose checks have not "
            "reported, including a repository with no CI at all.")
        self.assertEqual(pr.number, 7)
        self.assertEqual(pr.url, PR_URL)
        self.assertTrue(pr.created)
        self.assertEqual(self.audited("vcs.pr.opened")[0]["number"], 7)

    def test_a_second_pull_request_is_never_opened_for_one_branch(self) -> None:
        """Two review threads over one change is a mess a human has to clear.

        GitHub will let you; the right answer for a re-run is the pull request
        that is already there, with its description left exactly as it is —
        by then it may have been edited by the person reviewing it.
        """
        self.runner.on("pr list --head", json.dumps(
            [{"number": 7, "url": PR_URL, "title": "Wire the merge gate",
              "isDraft": False, "headRefName": BRANCH, "baseRefName": "main"}]))
        pr = self.repo().pull_request(
            "A different title", "A different body, long enough to be real "
                                 "prose about what changed and why.")
        self.assertFalse(pr.created)
        self.assertEqual(pr.number, 7)
        self.assertEqual(pr.title, "Wire the merge gate")
        self.assertEqual(self.runner.ran("pr create"), [])

    def test_a_pull_request_needs_a_real_description(self) -> None:
        with self.assertRaises(vcs.VcsError):
            self.repo().pull_request("Wire the merge gate", "fixes")
        self.assertEqual(self.runner.ran("pr create"), [])

    def test_a_denied_git_push_policy_stops_the_pull_request(self) -> None:
        self.settings.set("confirm.git_push", "deny")
        with self.assertRaises(confirm_mod.ConfirmDenied):
            self.repo().pull_request(
                "Wire the merge gate",
                "A description long enough to say what changed and why it did.")
        self.assertEqual(self.runner.ran("pr create"), [])

    # -- checks -----------------------------------------------------------

    def test_reading_the_checks_asks_gh_for_every_field_it_parses(self) -> None:
        status = self.repo().checks(7)
        self.assertTrue(status.green)
        asked = self.runner.ran("pr view 7")[0]
        for wanted in vcs.PR_FIELDS:
            self.assertIn(wanted, asked[-1])

    def test_waiting_stops_as_soon_as_the_checks_go_green(self) -> None:
        clock = FakeClock()
        self.runner.sequence("pr view", [
            json.dumps(payload(statusCheckRollup=[
                check("suite", status="IN_PROGRESS", conclusion="")])),
            json.dumps(payload()),
        ])
        status = self.repo(clock=clock, sleep=clock.sleep).checks(
            7, wait=600.0, poll=20.0)
        self.assertTrue(status.green, status.summary())
        self.assertEqual(len(self.runner.ran("pr view")), 2)
        self.assertEqual(clock.slept, [20.0])

    def test_waiting_does_not_wait_on_a_verdict_that_cannot_change(self) -> None:
        clock = FakeClock()
        self.runner.on("pr view", json.dumps(payload(
            statusCheckRollup=[check("suite", conclusion="FAILURE")])))
        status = self.repo(clock=clock, sleep=clock.sleep).checks(7, wait=600.0)
        self.assertFalse(status.green)
        self.assertEqual(clock.slept, [], "it waited on a failure")

    def test_checks_that_never_appear_cost_the_whole_wait_and_still_refuse(self) -> None:
        clock = FakeClock()
        self.runner.on("pr view", json.dumps(payload(statusCheckRollup=[])))
        status = self.repo(clock=clock, sleep=clock.sleep).checks(
            7, wait=60.0, poll=20.0)
        self.assertFalse(status.green)
        self.assertEqual(clock.slept, [20.0, 20.0, 20.0])

    def test_a_number_can_be_worked_out_from_the_branch(self) -> None:
        self.runner.on("pr list --head", json.dumps(
            [{"number": 7, "url": PR_URL, "title": "t", "isDraft": False,
              "headRefName": BRANCH, "baseRefName": "main"}]))
        self.assertEqual(self.repo().pr_number(), 7)

    def test_no_open_pull_request_is_an_error_and_not_a_guess(self) -> None:
        with self.assertRaises(vcs.VcsError):
            self.repo().pr_number()

    # -- merging ----------------------------------------------------------

    def test_a_green_pull_request_is_merged_with_the_configured_method(self) -> None:
        result = self.repo().merge(7)
        self.assertTrue(result.merged)
        self.assertEqual(result.method, "squash")
        merged = self.runner.ran("pr merge 7")
        self.assertEqual(merged[0][-2:], ["--squash", "--delete-branch"])
        self.assertEqual(self.audited("vcs.merged")[0]["number"], 7)

    def test_the_merge_method_and_branch_deletion_follow_the_config(self) -> None:
        self.settings.set("vcs.merge_method", "rebase")
        self.settings.set("vcs.delete_branch", False)
        self.repo().merge(7)
        self.assertEqual(self.runner.ran("pr merge 7")[0][-1], "--rebase")

    def test_an_unknown_merge_method_never_reaches_gh(self) -> None:
        with self.assertRaises(vcs.VcsError):
            self.repo().merge(7, method="ff-only")
        self.assertEqual(self.runner.ran("pr merge"), [])

    def refuses(self, **over: Any) -> vcs.NotGreen:
        self.runner.on("pr view", json.dumps(payload(**over)))
        with self.assertRaises(vcs.NotGreen) as caught:
            self.repo().merge(7)
        self.assertEqual(self.runner.ran("pr merge"), [],
                         "it merged something that was not green")
        return caught.exception

    def test_a_repository_with_no_ci_is_never_merged(self) -> None:
        exc = self.refuses(statusCheckRollup=[])
        self.assertIn("no checks", "; ".join(exc.status.refusals))

    def test_a_failing_check_is_never_merged(self) -> None:
        self.refuses(statusCheckRollup=[check("suite", conclusion="FAILURE")])

    def test_a_pending_check_is_never_merged(self) -> None:
        self.refuses(statusCheckRollup=[
            check("suite", status="IN_PROGRESS", conclusion="")])

    def test_a_draft_is_never_merged(self) -> None:
        self.refuses(isDraft=True)

    def test_a_conflicting_pull_request_is_never_merged(self) -> None:
        self.refuses(mergeable="CONFLICTING")

    def test_requested_changes_are_never_merged_over(self) -> None:
        self.refuses(reviewDecision="CHANGES_REQUESTED")

    def test_an_all_skipped_rollup_is_never_merged(self) -> None:
        self.refuses(statusCheckRollup=[check("a", conclusion="SKIPPED")])

    def test_a_refusal_leaves_the_pull_request_open_and_says_why(self) -> None:
        exc = self.refuses(statusCheckRollup=[check("s", conclusion="FAILURE")])
        self.assertEqual(self.runner.ran("pr close"), [])
        entry = self.audited("vcs.merge.refused")[0]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["number"], 7)
        self.assertTrue(entry["refusals"])
        self.assertIn("not green", str(exc))

    def test_the_green_gate_runs_before_the_question(self) -> None:
        """A prompt whose answer cannot matter trains the user to click through.

        `git_merge = ask` with checks that are already red must refuse without
        ever raising a confirmation.
        """
        self.settings.set("confirm.git_merge", "ask")
        asked: list[Any] = []
        broker = confirm_mod.ConfirmBroker(
            settings=self.settings, audit=self.audit,
            asker=lambda pending, channel: asked.append(pending))
        repo = vcs.Repo(self.root, run=self.runner, audit=self.audit,
                        settings=self.settings, confirm=broker)
        self.runner.on("pr view", json.dumps(payload(
            statusCheckRollup=[check("s", conclusion="FAILURE")])))
        with self.assertRaises(vcs.NotGreen):
            repo.merge(7)
        self.assertEqual(asked, [])

    def test_a_denied_git_merge_policy_stops_a_green_merge(self) -> None:
        self.settings.set("confirm.git_merge", "deny")
        with self.assertRaises(confirm_mod.ConfirmDenied):
            self.repo().merge(7)
        self.assertEqual(self.runner.ran("pr merge"), [])

    def test_the_default_policy_merges_a_green_one_without_asking(self) -> None:
        # The autonomy level the user chose, asserted against the shipped
        # default rather than against a value this case set.
        self.settings.write({"confirm": {}})
        self.assertEqual(self.settings.get("confirm.git_merge"), "never")
        self.assertTrue(self.repo().merge(7).merged)

    # -- notification -----------------------------------------------------

    def test_a_refused_merge_reaches_the_notifier_and_is_swallowed(self) -> None:
        """Nothing is stubbed: the spawn is genuinely attempted.

        `[vcs] notify_on_refusal` defaults to on, so this runs on every refusal
        in production. It has to fail on the disarmed notifier, be swallowed,
        and still leave the refusal intact.
        """
        repo = self.repo()
        self.assertTrue(self.settings.get("vcs.notify_on_refusal"))
        status = vcs.evaluate(payload(statusCheckRollup=[]))
        self.assertFalse(repo.notify_refusal(status))

    def test_the_toast_can_be_turned_off(self) -> None:
        self.settings.set("vcs.notify_on_refusal", False)
        self.assertFalse(self.repo().notify_refusal(vcs.evaluate(payload())))

    # -- issues -----------------------------------------------------------

    def test_an_issue_is_opened_and_audited(self) -> None:
        issue = self.repo().open_issue("CI is flaky on 3.13",
                                       "It fails about one run in five.",
                                       labels=["bug"])
        self.assertEqual(issue.number, 12)
        self.assertEqual(self.audited("vcs.issue.opened")[0]["number"], 12)
        self.assertIn("--label", self.runner.ran("issue create")[0])

    def test_a_comment_goes_to_the_right_thing(self) -> None:
        self.runner.on("pr comment", PR_URL + "#issuecomment-1\n")
        self.repo().comment(7, "Rerunning the suite.", on="pr")
        self.assertEqual(len(self.runner.ran("pr comment 7")), 1)
        with self.assertRaises(vcs.VcsError):
            self.repo().comment(7, "", on="pr")

    # -- the whole workflow ----------------------------------------------

    def test_ship_goes_branch_commit_push_pr_merge(self) -> None:
        report = self.repo().ship(
            topic="a thing", message="wire the merge gate",
            paths=["lunad/vcs.py"],
            body="What changed, and why it changed, in enough words to be a "
                 "description rather than a restatement of the diff.")
        self.assertTrue(report["merged"])
        order = [self.runner.line(c).split()[0] for c in self.runner.calls]
        self.assertLess(order.index("switch"), order.index("add"))
        self.assertLess(order.index("add"), order.index("commit"))
        self.assertLess(order.index("commit"), order.index("push"))
        self.assertEqual(len(self.runner.ran("pr create")), 1)
        self.assertEqual(len(self.runner.ran("pr merge")), 1)

    def test_ship_that_cannot_merge_still_leaves_the_pull_request(self) -> None:
        """A refused merge is not a failed `ship`.

        Everything up to the pull request succeeded, and discarding that with
        an exception would throw away the branch, the commit and the review
        thread over a state the caller simply needs to be told about.
        """
        self.runner.on("pr view", json.dumps(payload(statusCheckRollup=[])))
        # `self.repo()` here carries the real clock: an empty rollup is
        # "still settling" (see `_still_settling`), so without `wait=0.0`
        # this would cost the whole real `vcs.check_wait_seconds` patience —
        # sleeping in wall-clock time, not simulated time — before refusing.
        # That behaviour, with a controllable clock, is covered by
        # `test_checks_that_never_appear_cost_the_whole_wait_and_still_refuse`;
        # this test only cares about the shape of the report.
        report = self.repo().ship(
            topic="a thing", message="wire the merge gate",
            paths=["lunad/vcs.py"], wait=0.0,
            body="What changed, and why it changed, in enough words to count "
                 "as a description of the work.")
        self.assertFalse(report["merged"])
        self.assertEqual(report["pull_request"]["number"], 7)
        self.assertTrue(report["refusals"])
        self.assertIn("no checks", report["note"])
        self.assertEqual(self.runner.ran("pr merge"), [])

    def test_ship_does_not_merge_a_draft(self) -> None:
        report = self.repo().ship(
            topic="a thing", message="wip", paths=["lunad/vcs.py"],
            draft=True,
            body="Still being written; opened early so the work is visible "
                 "rather than sitting on one laptop.")
        self.assertFalse(report["merged"])
        self.assertEqual(self.runner.ran("pr merge"), [])
        self.assertIn("draft", report["note"])

    def test_auto_merge_off_opens_the_pull_request_and_stops(self) -> None:
        self.settings.set("vcs.auto_merge", False)
        report = self.repo().ship(
            topic="a thing", message="wire the merge gate",
            paths=["lunad/vcs.py"],
            body="What changed and why, at enough length to be a description "
                 "somebody could review against.")
        self.assertFalse(report["merged"])
        self.assertEqual(self.runner.ran("pr merge"), [])
        self.assertIn("auto_merge", report["note"])

    def test_a_generated_description_says_what_and_why_not_the_diff(self) -> None:
        report = self.repo().ship(topic="the merge gate",
                                  message="wire the merge gate",
                                  paths=["lunad/vcs.py"])
        body = self.runner.ran("pr create")[0]
        self.assertIn("--body", body)
        text = body[body.index("--body") + 1]
        self.assertIn("What this is", text)
        self.assertIn("Why", text)
        self.assertTrue(report["merged"])


class NeverGhCase(RepoCase):
    """The claim at the top of this file, checked rather than asserted."""

    def test_no_case_in_this_module_can_reach_the_real_gh(self) -> None:
        self.assertEqual(config.GH_BIN, FORBIDDEN_GH)
        self.assertIsNone(shutil.which(config.GH_BIN))
        # And a Repo that was handed no runner cannot get past the sentinel.
        with self.assertRaises(vcs.VcsUnavailable):
            vcs.run_process([config.GH_BIN, "pr", "merge", "1"], self.root, 5.0)

    def test_every_invocation_this_module_makes_names_a_disarmed_binary(self) -> None:
        self.repo().ship(topic="a thing", message="m", paths=["lunad/vcs.py"],
                         body="A description long enough to pass the check on "
                              "descriptions, saying what and why.")
        self.assertTrue(self.runner.calls)
        for call in self.runner.calls:
            self.assertIn(call[0], (config.GIT_BIN, config.GH_BIN))


# =========================================================================
# Against the real git — no remote, no gh, no network
# =========================================================================


@unittest.skipUnless(GIT, "git is not installed")
class RealGitCase(TempMemoryCase):
    """The staging rules, against real `git status` output.

    A parser proved only against fixtures written by the same hand that wrote
    the parser proves nothing. There is no remote configured here, so there is
    nowhere for a push to go even if one were attempted, and `gh` is never
    reached: every case stops at the local half of the workflow.
    """

    def setUp(self) -> None:
        super().setUp()
        self.work = self.root / "checkout"
        self.work.mkdir()
        self.at("init", "--initial-branch=main")
        self.at("config", "user.email", "tests@example.invalid")
        self.at("config", "user.name", "luna tests")
        self.at("config", "commit.gpgsign", "false")
        (self.work / "seed.txt").write_text("seed\n")
        self.at("add", "seed.txt")
        self.at("commit", "-m", "seed")

    def at(self, *args: str) -> str:
        proc = subprocess.run([GIT, "-C", str(self.work), *args],
                              capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def repo(self) -> vcs.Repo:
        self.settings.set("confirm.git_push", "never")
        return vcs.Repo(self.work, git_bin=GIT, audit=self.audit,
                        settings=self.settings,
                        confirm=confirm_mod.ConfirmBroker(
                            settings=self.settings, audit=self.audit))

    def test_the_default_branch_is_found_without_a_remote(self) -> None:
        self.assertEqual(self.repo().default_branch(), "main")

    def test_dirty_lists_modifications_and_untracked_files(self) -> None:
        (self.work / "seed.txt").write_text("changed\n")
        (self.work / "new.txt").write_text("new\n")
        self.assertEqual(self.repo().dirty(), ("new.txt", "seed.txt"))

    def test_she_will_not_commit_on_main(self) -> None:
        (self.work / "seed.txt").write_text("changed\n")
        with self.assertRaises(vcs.ProtectedBranch):
            self.repo().commit("straight to main", ["seed.txt"])
        self.assertEqual(self.at("rev-list", "--count", "HEAD"), "1")

    def test_someone_elses_dirty_file_does_not_get_committed(self) -> None:
        """The rule that matters, proved against a real working tree."""
        repo = self.repo()
        repo.branch("her work")
        (self.work / "hers.txt").write_text("hers\n")
        (self.work / "theirs.txt").write_text("half-finished, not hers\n")
        commit = repo.commit("add her file", ["hers.txt"])
        self.assertEqual(commit.files, ("hers.txt",))
        self.assertEqual(commit.left_alone, ("theirs.txt",))
        listed = self.at("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(listed, ["hers.txt"])
        self.assertEqual(repo.dirty(), ("theirs.txt",))

    def test_a_branch_is_created_then_adopted_on_the_second_run(self) -> None:
        first = self.repo().branch("her work")
        self.assertTrue(first.created)
        self.assertEqual(self.at("rev-parse", "--abbrev-ref", "HEAD"),
                         first.name)
        second = self.repo().branch("her work")
        self.assertFalse(second.created)
        self.assertEqual(second.name, first.name)

    def test_a_commit_with_no_paths_is_refused_against_a_real_tree(self) -> None:
        repo = self.repo()
        repo.branch("her work")
        (self.work / "theirs.txt").write_text("not hers\n")
        with self.assertRaises(vcs.VcsError) as caught:
            repo.commit("everything", [])
        self.assertIn("theirs.txt", str(caught.exception))
        self.assertEqual(self.at("rev-list", "--count", "HEAD"), "1")


if __name__ == "__main__":
    unittest.main()
