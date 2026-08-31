"""The persona spec as a gate on action, not a manner of speaking.

Until the brain became Codex, Luna's ask path ran with no tools at all. That
made the spec's central rule — interrogate before executing — structurally
impossible to break: she could only ever talk. Giving her a shell removed the
prop, and the behaviour went with it. Asked to "rewrite the whole bar in
React", she dispatched the job to Sol instead of saying that React does not run
in a QML context; when the dispatch was hard-denied by her own confirm policy
she reported that "Sol's daemon timed out", which nothing had.

So two rules are now asserted structurally, in both the places a model reads
them: the spec (``data/persona.md``, the user's document) and the operating
notes (``lunad/persona.py``, the block that grants the capability and sits last
in the prompt).

1. Judgement gates action. Trivial, reversible and read-only things she just
   does; anything else gets the objection first and no tool call at all.
2. A failed command is reported as it printed. No invented causes, and a
   refusal from her own policy is named as one.

Assertions match on fragments that do not straddle a line break — the blocks
are hand-wrapped prose, and a test that asserts across a wrap fails the next
time somebody reflows a paragraph.
"""

from __future__ import annotations

import unittest

from lunad import persona


class SpecGateCase(unittest.TestCase):
    """What ``data/persona.md`` itself says."""

    def setUp(self) -> None:
        self.spec = persona.load_spec()

    def test_the_triage_is_a_gate_in_front_of_the_first_command(self) -> None:
        self.assertIn("Judgement comes before action", self.spec)
        self.assertIn("runs before the first command", self.spec)

    def test_the_trap_is_named(self) -> None:
        """Having tools is the pull; saying so is the counterweight."""
        self.assertIn("having one is not a reason to use it", self.spec)

    def test_the_gate_asks_whether_the_method_would_even_work(self) -> None:
        """The React case: wrong on the platform, so it is an objection and
        not an experiment."""
        self.assertIn("Would the named method even work here?", self.spec)

    def test_trivial_and_read_only_work_is_just_done(self) -> None:
        """The over-correction guard. An assistant who interrogates a disk
        check is as broken as one who rewrites the bar in React."""
        self.assertIn("Trivial, reversible or read-only -> just do it.",
                      self.spec)
        self.assertIn("Asking once for something harmless is once too many.",
                      self.spec)

    def test_everything_else_stops_before_the_tool_call(self) -> None:
        self.assertIn("Everything else -> the objection comes first.",
                      self.spec)
        self.assertIn("she makes no tool call at all", self.spec)

    def test_read_only_depth_still_goes_to_the_specialist(self) -> None:
        """The first cut of the decision rule said "read-only -> just do it",
        and she pulled a whole retrieval-code audit into that branch: she
        dispatched Sol *and* did the work herself, told the user about neither
        the job nor the terminal it opened, and answered the same question
        twice. Read-only is not the same as cheap."""
        self.assertIn("Read-only is not", self.spec)
        self.assertIn("depth goes to Sol even when it changes nothing",
                      self.spec)

    def test_a_dispatch_is_announced_and_not_also_done_by_hand(self) -> None:
        self.assertIn("Having dispatched, she says so and stops.", self.spec)
        self.assertIn("She does not also do the job herself", self.spec)

    def test_pointlessness_is_also_a_trigger(self) -> None:
        """She gated impossibility and hard denies, but ran cheerfully at
        "write me a 2000-word essay about how much disk space I have left":
        it changed nothing and cost nothing, so neither of the first two
        triggers fired. Being able to do a thing is not a finding that it is
        worth doing."""
        self.assertIn("plainly out of proportion", self.spec)
        self.assertIn("Possible, cheap and pointless still earns the question.",
                      self.spec)

    def test_delegating_does_not_skip_the_gate(self) -> None:
        """`dispatch` was the escape hatch she actually used."""
        self.assertIn("Delegating is acting.", self.spec)

    def test_a_delegated_job_is_priced_in_the_line_that_announces_it(self):
        """Asked to read the whole tree she enrolled Sol and said so, but
        never said what it would take. The estimate rule already existed; it
        was simply nowhere near the act that keeps skipping it."""
        self.assertIn("big enough to hand to Sol is big enough to price",
                      self.spec)

    def test_the_two_objection_cap_still_binds_the_gate(self) -> None:
        self.assertIn("at most TWO objections", self.spec)
        self.assertIn("never asks permission twice", self.spec)


class SpecReportingCase(unittest.TestCase):
    """The second defect: inventing a cause for a failure that named itself."""

    def setUp(self) -> None:
        self.spec = persona.load_spec()

    def test_a_failure_is_reported_as_it_printed(self) -> None:
        self.assertIn("she reports what it actually said", self.spec)
        self.assertIn("supplies a cause she did not read", self.spec)

    def test_inventing_a_cause_is_named_as_the_worse_failure(self) -> None:
        self.assertIn(
            "an invented cause is worse than an unexplained failure",
            self.spec)

    def test_a_hard_deny_is_her_judgement_not_an_error(self) -> None:
        self.assertIn("A refusal from her own safety policy is not an error",
                      self.spec)

    def test_she_does_not_call_a_job_underway_when_it_is_not(self) -> None:
        self.assertIn("call a job queued, started or underway unless it is.",
                      self.spec)


class OperatingNotesGateCase(unittest.TestCase):
    """The same two rules where the capability is granted.

    A rule that lives only in the spec competes, three thousand tokens later,
    with a tool that is right there. These assertions are why it is restated in
    the closing block.
    """

    def notes(self) -> str:
        return persona.operating_notes(cli="/bin/luna", specialist="Sol")

    def test_judgement_is_the_first_thing_the_notes_say(self) -> None:
        notes = self.notes()
        self.assertIn("Judgement comes before action.", notes)
        self.assertLess(notes.index("Judgement comes before action."),
                        notes.index("You have a shell on this machine"),
                        "the gate must be read before the grant, not after")

    def test_the_notes_name_the_trap_too(self) -> None:
        self.assertIn("Having a shell is not a reason to", self.notes())

    def test_dispatch_is_not_a_way_around_the_decision(self) -> None:
        self.assertIn("is acting, not a way around this decision.",
                      self.notes())

    def test_read_only_work_is_done_without_asking(self) -> None:
        self.assertIn("go and get it now, without asking", self.notes())

    def test_the_notes_gate_disproportion_too(self) -> None:
        self.assertIn("plainly out of proportion to what it", self.notes())

    def test_the_objecting_turn_makes_no_tool_call(self) -> None:
        self.assertIn("make no tool call at all that turn", self.notes())

    def test_the_notes_forbid_doing_a_dispatched_job_twice(self) -> None:
        notes = self.notes()
        self.assertIn("what it will roughly take", notes)
        self.assertIn("Do not also do the", notes)
        self.assertIn("do not dispatch silently", notes)

    def test_a_failed_command_is_quoted_not_paraphrased(self) -> None:
        notes = self.notes()
        self.assertIn("When a command fails, report what it actually printed.",
                      notes)
        self.assertIn("Never supply a cause you did not read", notes)

    def test_a_policy_refusal_is_reported_as_a_refusal(self) -> None:
        notes = self.notes()
        self.assertIn("say it was refused", notes)
        self.assertIn("not a fault to dress up", notes)

    def test_a_job_is_not_called_started_until_it_started(self) -> None:
        self.assertIn(
            "call a job started, queued or underway unless the command",
            self.notes())

    def test_the_four_hard_denies_bind_her_too(self) -> None:
        """`CODEX_ASK_SANDBOX` is "bypass": her own ask has no sandbox, and
        the boundaries were only ever given to the sessions she dispatches.
        She was enforcing on Sol a rule nobody had told her applied to her."""
        notes = self.notes()
        for rule in ("signalling a process you",
                     "restarting `omarchy-shell`",
                     "CUSTOMISATIONS.md",
                     "`rm -rf` outside your own directories"):
            with self.subTest(rule=rule):
                self.assertIn(rule, notes)

    def test_a_hard_deny_does_not_bend_to_being_overruled(self) -> None:
        self.assertIn("Being overruled does not unlock these four",
                      self.notes())

    def test_the_count_is_pinned_to_the_enforcer(self) -> None:
        """The notes say "four". `confirm` is the authority, so a fifth deny
        must not be able to land without this block being updated."""
        from lunad import confirm
        self.assertEqual(len(confirm.HARD_DENIES) + 1, 4,
                         "the hard denies changed; the operating notes still "
                         "say 'Four things are refused outright'")

    def test_the_block_stays_hand_wrapped(self) -> None:
        """Every assertion in this file matches inside a single line, so a
        reflow past the margin is what silently breaks them. Cheaper to catch
        the reflow."""
        over = [ln for ln in self.notes().splitlines() if len(ln) > 80]
        self.assertEqual(over, [], "operating notes lines must stay under 80")

    def test_the_shell_is_still_granted(self) -> None:
        """The gate must not have turned into a ban. She still acts."""
        notes = self.notes().lower()
        for claim in ("shell on this machine", "run commands",
                      "expected to use it"):
            with self.subTest(claim=claim):
                self.assertIn(claim, notes)


class AssembledPromptCase(unittest.TestCase):
    """Both rules reach the model, and nothing older was traded for them."""

    def prompt(self, tools: bool = True) -> str:
        return persona.build_system_prompt("tier one", spec=persona.load_spec(),
                                           name="Luna", specialist="Sol",
                                           tools=tools)

    def test_the_gate_survives_assembly(self) -> None:
        prompt = self.prompt()
        for rule in ("Judgement comes before action",
                     "Trivial, reversible or read-only -> just do it.",
                     "Delegating is acting.",
                     "When a command fails, report what it actually printed."):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)

    def test_the_spec_half_of_the_gate_holds_without_tools(self) -> None:
        """The spec is the same document either way, so a toolless brain is
        still told to interrogate — it simply has nothing to interrogate with.
        """
        prompt = self.prompt(tools=False)
        self.assertIn("Judgement comes before action", prompt)
        self.assertIn("no tools", prompt)

    def test_nothing_the_user_asked_for_was_softened(self) -> None:
        prompt = self.prompt()
        for rule in ("Core stance: interrogate before executing",
                     "Never open with praise",
                     "at most TWO objections",
                     "does NOT re-litigate",
                     "never produces a numbered list of caveats",
                     "Spoken replies are SHORT",
                     "British, dry, unhurried",
                     "## Budget consciousness",
                     "## Delegation",
                     "## Memory posture"):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
