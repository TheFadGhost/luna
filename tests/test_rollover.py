"""Surviving the end of a conversation without the user noticing.

A resumed codex thread is not immortal. It is retired when tier-1 memory
moves, when it reaches the turn cap, when its history reaches the token
budget, and it dies outright when the agent refuses to resume it. All four
were already handled in the sense that the *next* ask still got answered —
by a brand new thread that had never heard of the one before it.

What that costs is narrow and sharp. Tier 1 and tier 2 are rebuilt into every
prompt, so the facts survive; what does not survive is everything a
conversation keeps in the air rather than in a store. "The second one." "Do
that again." "Why?" Recall cannot fetch it, because a pronoun matches nothing
— the antecedent is in the previous turn, not in the query.

So these are the tests for the two halves of the fix:

* the bridge — the last few exchanges verbatim, on exactly the turn that
  lost them, with an instruction not to remark on it; and
* tier-1 eviction — the consolidation pass making room instead of giving up,
  because the alternative is that a full LUNA.md means Luna silently stops
  learning for good.

Nothing here reaches a real agent. The rollover is driven through the
``SessionManager`` and the ``Memory`` facade directly, which is where the
decisions are actually made.
"""

from __future__ import annotations

import unittest

from lunad import agent, config, consolidate, persona
from lunad.memory import MemoryCapExceeded
from lunad.session import SessionManager, fingerprint

from ._support import TempMemoryCase
from .test_server import DaemonCase, FakeAdapter, MuteSpeech


class BridgeBlockTests(TempMemoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.mem = self.memory()

    def test_nothing_to_bridge_to_is_an_empty_block(self):
        self.assertEqual(self.mem.bridge_block(), "")

    def test_it_carries_the_most_recent_exchanges(self):
        for i in range(6):
            self.mem.episodes.record(f"question {i}", f"answer {i}")
        block = self.mem.bridge_block(limit=2)
        self.assertIn("question 5", block)
        self.assertIn("answer 5", block)
        self.assertIn("question 4", block)
        self.assertNotIn("question 3", block)

    def test_it_reads_oldest_first(self):
        for i in range(3):
            self.mem.episodes.record(f"question {i}", f"answer {i}")
        block = self.mem.bridge_block(limit=3)
        self.assertLess(block.index("question 0"), block.index("question 2"))

    def test_recency_beats_relevance(self):
        # The whole point: `recall_block` is a search and would never return
        # this, because "mm-hm" matches nothing. The bridge returns it because
        # it is what was just said.
        self.mem.episodes.record("is the printer on", "yes, it is")
        self.mem.episodes.record("mm-hm", "quite")
        self.assertEqual(self.mem.recall_block("the second one"), "")
        self.assertIn("mm-hm", self.mem.bridge_block())

    def test_it_can_skip_what_recall_already_showed(self):
        first = self.mem.episodes.record("about the printer", "it is on")
        self.mem.episodes.record("and the scanner", "also on")
        block = self.mem.bridge_block(limit=2, exclude=[first.id])
        self.assertNotIn("about the printer", block)
        self.assertIn("and the scanner", block)

    def test_a_zero_limit_disables_it(self):
        self.mem.episodes.record("something", "else")
        self.assertEqual(self.mem.bridge_block(limit=0), "")

    def test_a_broken_store_rolls_over_without_a_bridge(self):
        # A rollover with no bridge is what the daemon did before this
        # existed. It must never become a failed ask.
        self.mem.episodes.record("something", "else")
        self.mem.episodes.close()
        self.assertEqual(self.mem.bridge_block(), "")


class BridgeSectionTests(unittest.TestCase):
    def test_an_empty_block_renders_nothing(self):
        self.assertEqual(persona.bridge_section("   "), "")

    def test_it_tells_her_not_to_mention_the_seam(self):
        section = persona.bridge_section("- user: hello\n  luna: hello")
        self.assertIn("hello", section)
        self.assertIn("do not", section.lower())
        self.assertIn("mention", section.lower())

    def test_the_message_puts_the_bridge_after_recall_and_before_the_question(self):
        message = persona.build_user_message(
            "and the second one?", recall_block="### old thing",
            context_line="Focused window right now: foot.",
            bridge_block="- user: name two rivers\n  luna: the Thames and the Po",
        )
        self.assertLess(message.index("old thing"), message.index("the Po"))
        self.assertLess(message.index("the Po"), message.index("and the second one?"))

    def test_no_bridge_leaves_the_message_exactly_as_it_was(self):
        without = persona.build_user_message("hello", recall_block="### r")
        with_empty = persona.build_user_message("hello", recall_block="### r",
                                                bridge_block="")
        self.assertEqual(without, with_empty)


class CarryTests(unittest.TestCase):
    """Which retirements owe the next session a bridge, and which do not."""

    def setUp(self) -> None:
        self.mgr = SessionManager(idle_s=100.0, max_turns=3, token_budget=1000)
        self.prefix = fingerprint("persona", "tier1")

    def take(self, prefix: str | None = None, now: float | None = None,
             usage: dict | None = None):
        sess = self.mgr.acquire("default", prefix or self.prefix, now=now)
        self.mgr.succeeded(sess, cost_usd=0.01, usage=usage)
        return sess

    def test_a_first_session_carries_nothing(self):
        sess = self.mgr.acquire("default", self.prefix)
        self.assertEqual(sess.carry, "")

    def test_the_turn_cap_carries(self):
        for _ in range(3):
            self.take()
        after = self.mgr.acquire("default", self.prefix)
        self.assertIn("3 turns", after.carry)

    def test_a_tier1_write_carries(self):
        self.take()
        after = self.mgr.acquire("default", fingerprint("persona", "tier1 v2"))
        self.assertIn("tier-1", after.carry)

    def test_idleness_deliberately_carries_nothing(self):
        # Half an hour of silence is a new conversation, not an interruption.
        self.take(now=0.0)
        after = self.mgr.acquire("default", self.prefix, now=10_000.0)
        self.assertEqual(after.carry, "")

    def test_a_refused_resume_carries(self):
        self.take()
        self.mgr.drop("default", carry="the agent refused to resume the thread")
        after = self.mgr.acquire("default", self.prefix)
        self.assertIn("refused", after.carry)

    def test_a_drop_with_no_reason_carries_nothing(self):
        self.take()
        self.mgr.drop("default")
        self.assertEqual(self.mgr.acquire("default", self.prefix).carry, "")

    def test_a_session_that_never_started_owes_nothing(self):
        # Acquired, never handed to the agent, then the prefix moved. There is
        # no conversation behind it to bridge back to.
        self.mgr.acquire("default", self.prefix)
        after = self.mgr.acquire("default", fingerprint("persona", "other"))
        self.assertEqual(after.carry, "")

    def test_the_debt_is_cleared_by_the_turn_that_pays_it(self):
        for _ in range(3):
            self.take()
        after = self.mgr.acquire("default", self.prefix)
        self.assertTrue(after.carry)
        self.mgr.succeeded(after)
        self.assertEqual(after.carry, "")

    def test_a_failed_turn_keeps_the_debt_for_the_retry(self):
        for _ in range(3):
            self.take()
        after = self.mgr.acquire("default", self.prefix)
        self.assertTrue(after.carry)
        # No `succeeded` — the turn blew up. Re-acquiring on the same thread
        # must still know a bridge is owed.
        again = self.mgr.acquire("default", self.prefix)
        self.assertIs(again, after)
        self.assertTrue(again.carry)

    def test_clearing_the_manager_forgets_the_debt_too(self):
        self.take()
        self.mgr.drop("default", carry="something")
        self.mgr.clear()
        self.assertEqual(self.mgr.acquire("default", self.prefix).carry, "")

    def test_carrying_is_counted(self):
        for _ in range(3):
            self.take()
        self.mgr.acquire("default", self.prefix)
        self.assertEqual(self.mgr.counters["carried"], 1)


class TokenBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = SessionManager(idle_s=100.0, max_turns=1000,
                                  token_budget=1000)
        self.prefix = fingerprint("persona", "tier1")

    def turn(self, usage=None):
        sess = self.mgr.acquire("default", self.prefix)
        self.mgr.succeeded(sess, cost_usd=0.0, usage=usage)
        return sess

    def test_a_history_under_budget_is_resumed(self):
        first = self.turn({"input_tokens": 900})
        self.assertIs(self.mgr.acquire("default", self.prefix), first)

    def test_a_history_at_budget_is_retired(self):
        first = self.turn({"input_tokens": 1000})
        after = self.mgr.acquire("default", self.prefix)
        self.assertIsNot(after, first)
        self.assertIn("input tokens", after.carry)
        self.assertEqual(self.mgr.counters["over_budget"], 1)

    def test_the_replacement_starts_the_count_again(self):
        self.turn({"input_tokens": 5000})
        after = self.mgr.acquire("default", self.prefix)
        self.assertEqual(after.input_tokens, 0)
        self.assertIsNone(self.mgr.args_for(after)["resume"])

    def test_an_agent_that_reports_nothing_is_never_retired_for_it(self):
        # An unknown size must never be treated as a large one.
        first = self.turn(None)
        self.assertEqual(first.input_tokens, 0)
        self.assertIs(self.mgr.acquire("default", self.prefix), first)

    def test_nonsense_usage_is_ignored_rather_than_believed(self):
        first = self.turn({"input_tokens": "lots"})
        self.assertEqual(first.input_tokens, 0)
        self.mgr.succeeded(first, usage={"input_tokens": True})
        self.assertEqual(first.input_tokens, 0)
        self.mgr.succeeded(first, usage={"input_tokens": -5})
        self.assertEqual(first.input_tokens, 0)
        self.assertIs(self.mgr.acquire("default", self.prefix), first)

    def test_a_zero_budget_disables_the_check(self):
        mgr = SessionManager(idle_s=100.0, max_turns=1000, token_budget=0)
        sess = mgr.acquire("default", self.prefix)
        mgr.succeeded(sess, usage={"input_tokens": 10_000_000})
        self.assertIs(mgr.acquire("default", self.prefix), sess)

    def test_the_count_is_visible_in_the_snapshot(self):
        self.turn({"input_tokens": 700})
        self.assertEqual(self.mgr.snapshot()[0]["input_tokens"], 700)

    def test_the_shipped_budget_sits_under_the_measured_window(self):
        # 272,000 is what gpt-5.6-luna advertises; docs/_context-notes.md
        # records a resumed thread answering normally at 283,135. The budget
        # is a floor under both, not a guess at either.
        self.assertGreater(config.SESSION_TOKEN_BUDGET, 0)
        self.assertLess(config.SESSION_TOKEN_BUDGET, 272_000)


class Tier1EvictionTests(TempMemoryCase):
    """A full tier 1 must not mean she quietly stops learning."""

    #: Long enough that three fit under a 200-character cap and four do not,
    #: and each carries a word nothing else does so a tier-2 search for it is
    #: unambiguous.
    WORDS = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")

    def note(self, word: str) -> str:
        return f"the {word} note, padded out to about sixty characters here"

    def setUp(self) -> None:
        super().setUp()
        # 200 is the smallest the setting allows, which is the smallest
        # fixture that can exercise a real cap rather than a stubbed one.
        self.settings.set("memory.luna_cap_chars", 200)
        self.mem = self.memory()
        self.assertEqual(self.mem.luna.cap, 200)

    def test_a_write_that_fits_evicts_nothing(self):
        result = self.mem.fit_tier1(self.mem.luna, ["one", "two"])
        self.assertEqual(result["evicted"], [])
        self.assertEqual(self.mem.luna.entries(), ["one", "two"])

    def test_the_oldest_entries_are_evicted_to_make_room(self):
        old = [self.note(w) for w in self.WORDS[:3]]
        self.mem.luna.replace(old)
        result = self.mem.fit_tier1(self.mem.luna, old + ["a brand new fact"],
                                    protect=1)
        self.assertIn("a brand new fact", self.mem.luna.entries())
        self.assertEqual(result["evicted"], [old[0]])
        self.assertNotIn(old[0], self.mem.luna.entries())
        self.assertIn(old[1], self.mem.luna.entries())

    def test_an_evicted_entry_lands_in_tier_2(self):
        old = [self.note(w) for w in self.WORDS[:3]]
        self.mem.fit_tier1(self.mem.luna, old + ["a brand new fact"], protect=1)
        found = self.mem.episodes.search("alpha")
        self.assertTrue(found, "the evicted entry was not searchable")
        self.assertIn("alpha", found[0].luna_text)
        self.assertEqual(found[0].surface, "tier1-eviction")

    def test_an_evicted_entry_keeps_full_salience(self):
        old = [self.note(w) for w in self.WORDS[:3]]
        self.mem.fit_tier1(self.mem.luna, old + ["a brand new fact"], protect=1)
        self.assertEqual(self.mem.episodes.search("alpha")[0].salience, 1.0)

    def test_the_new_entries_are_never_the_ones_evicted(self):
        adds = ["a brand new fact", "and a second brand new fact"]
        self.mem.fit_tier1(self.mem.luna,
                           [self.note(w) for w in self.WORDS] + adds,
                           protect=len(adds))
        self.assertEqual(self.mem.luna.entries()[-len(adds):], adds)

    def test_something_larger_than_the_whole_cap_is_still_refused(self):
        with self.assertRaises(MemoryCapExceeded):
            self.mem.fit_tier1(self.mem.luna, ["x" * 500], protect=1)

    def test_a_refusal_leaves_the_file_untouched(self):
        self.mem.luna.replace(["a note worth keeping"])
        with self.assertRaises(MemoryCapExceeded):
            self.mem.fit_tier1(self.mem.luna,
                               ["a note worth keeping", "y" * 500], protect=1)
        self.assertEqual(self.mem.luna.entries(), ["a note worth keeping"])

    def test_plain_replace_still_refuses_rather_than_evicting(self):
        # The RPC and CLI path is unchanged: somebody asked for something that
        # does not fit and is told so, rather than something else being thrown
        # away on their behalf.
        self.mem.luna.replace([self.note(w) for w in self.WORDS[:3]])
        with self.assertRaises(MemoryCapExceeded):
            self.mem.luna.append("a brand new fact that will not fit at all")
        self.assertEqual(len(self.mem.luna.entries()), 3)



class ConsolidationMakesRoomTests(TempMemoryCase):
    """The pass that nobody watches must not be the pass that gives up.

    Driven through ``Consolidator._apply`` rather than a whole scripted pass:
    what is being tested is the decision taken when the cap refuses a write,
    and everything upstream of it already has its own tests in
    ``tests/test_consolidate.py``.
    """

    def setUp(self) -> None:
        super().setUp()
        self.settings.set("memory.luna_cap_chars", 200)
        self.mem = self.memory()
        self.con = consolidate.Consolidator(self.mem, lambda: None,
                                            settings=self.settings)

    def full(self) -> list[str]:
        return [f"the {w} note, padded out to about sixty characters here"
                for w in ("alpha", "beta", "gamma")]

    def test_a_full_tier_1_no_longer_swallows_what_it_learned(self):
        self.mem.luna.replace(self.full())
        out = self.con._apply({"LUNA.md": {"add": ["the printer is upstairs"]}})
        record = out["LUNA.md"]
        self.assertIsNone(record["over_cap"])
        self.assertEqual(record["added"], ["the printer is upstairs"])
        self.assertIn("the printer is upstairs", self.mem.luna.entries())
        self.assertEqual(len(record["evicted"]), 1)

    def test_what_it_made_room_by_is_still_findable(self):
        self.mem.luna.replace(self.full())
        self.con._apply({"LUNA.md": {"add": ["the printer is upstairs"]}})
        self.assertTrue(self.mem.episodes.search("alpha"))

    def test_the_eviction_is_in_the_record_for_the_pass_to_count(self):
        # `_apply` does not aggregate; `run_once` does, and it reads this key.
        # An eviction that nobody could see would be its own quiet failure.
        self.mem.luna.replace(self.full())
        out = self.con._apply({"LUNA.md": {"add": ["the printer is upstairs"]}})
        self.assertEqual(out["LUNA.md"]["evicted"], [self.full()[0]])
        self.assertNotIn("lost", out["LUNA.md"])

    def test_an_entry_bigger_than_the_cap_is_still_refused_outright(self):
        # The old contract, unchanged: this is not a lack of room, and
        # emptying the file would not help.
        self.mem.luna.replace(["a note worth keeping"])
        out = self.con._apply({"LUNA.md": {"add": ["y" * 400]}})
        self.assertIsNotNone(out["LUNA.md"]["over_cap"])
        self.assertEqual(out["LUNA.md"]["added"], [])
        self.assertEqual(self.mem.luna.entries(), ["a note worth keeping"])


class CountingAdapter(FakeAdapter):
    """A FakeAdapter that also reports how big its context got."""

    def __init__(self, input_tokens: int = 0, **kw) -> None:
        super().__init__(**kw)
        self.input_tokens = input_tokens

    def ask(self, prompt: str, system_prompt: str, **kw):
        reply = super().ask(prompt, system_prompt, **kw)
        if self.input_tokens:
            reply.usage = {"input_tokens": self.input_tokens}
        return reply


class DaemonRolloverTests(DaemonCase):
    """End to end: the seam does not show."""

    def daemon(self, adapter=None, **session_kw):
        d = self.build_daemon()
        d.adapter = adapter or FakeAdapter()
        d.speech.close()
        d.speech = MuteSpeech()
        d.sessions = SessionManager(idle_s=1000.0, **session_kw)
        self.addCleanup(d.close)
        return d

    def test_an_ordinary_turn_carries_no_bridge(self):
        d = self.daemon(max_turns=60)
        reply = d.op_ask({"prompt": "name two rivers"})
        self.assertFalse(reply["bridged"])
        self.assertNotIn("Where this conversation had got to",
                         d.adapter.last_prompt)

    def test_the_turn_after_a_rollover_is_told_what_was_being_said(self):
        d = self.daemon(max_turns=1)
        d.op_ask({"prompt": "name two rivers"})
        reply = d.op_ask({"prompt": "and the second one?"})
        self.assertTrue(reply["bridged"])
        self.assertIn("Where this conversation had got to", d.adapter.last_prompt)
        self.assertIn("name two rivers", d.adapter.last_prompt)
        # And it really was a new thread, not a resumed one.
        self.assertIsNone(d.adapter.calls[-1]["resume"])

    def test_she_is_told_not_to_mention_it(self):
        d = self.daemon(max_turns=1)
        d.op_ask({"prompt": "name two rivers"})
        d.op_ask({"prompt": "and the second one?"})
        self.assertIn("do not mention", d.adapter.last_prompt.lower())

    def test_the_bridge_is_paid_once_and_not_again(self):
        d = self.daemon(max_turns=2)
        d.op_ask({"prompt": "one"})
        d.op_ask({"prompt": "two"})
        third = d.op_ask({"prompt": "three"})     # rolled over
        self.assertTrue(third["bridged"])
        fourth = d.op_ask({"prompt": "four"})     # resumed, no debt left
        self.assertFalse(fourth["bridged"])

    def test_an_idle_gap_is_a_new_conversation_and_is_not_bridged(self):
        d = self.daemon(max_turns=60)
        d.sessions.idle_s = 0.0
        d.op_ask({"prompt": "name two rivers"})
        reply = d.op_ask({"prompt": "and the second one?"})
        self.assertFalse(reply["bridged"])

    def test_a_history_over_budget_rolls_over_and_bridges(self):
        d = self.daemon(adapter=CountingAdapter(input_tokens=50_000),
                        max_turns=60, token_budget=10_000)
        d.op_ask({"prompt": "name two rivers"})
        reply = d.op_ask({"prompt": "and the second one?"})
        self.assertTrue(reply["bridged"])
        self.assertIsNone(d.adapter.calls[-1]["resume"])

    def test_a_history_under_budget_keeps_resuming(self):
        d = self.daemon(adapter=CountingAdapter(input_tokens=500),
                        max_turns=60, token_budget=10_000)
        d.op_ask({"prompt": "name two rivers"})
        reply = d.op_ask({"prompt": "and the second one?"})
        self.assertFalse(reply["bridged"])
        self.assertIsNotNone(d.adapter.calls[-1]["resume"])

    def test_the_user_is_told_nothing_about_any_of_it(self):
        # The reply is the model's, verbatim. A rollover the user has to
        # notice is a failed rollover, and the daemon adds no apology.
        d = self.daemon(max_turns=1)
        d.op_ask({"prompt": "one"})
        reply = d.op_ask({"prompt": "two"})
        self.assertEqual(reply["reply"], "Fine.")
        self.assertEqual(d.speech.said, [])

    def test_a_refused_resume_heals_with_a_bridge_rather_than_amnesia(self):
        class RefusesOnce(FakeAdapter):
            def ask(self, prompt, system_prompt, **kw):
                if kw.get("resume"):
                    raise agent.AgentFailed("codex exec resume: no such session")
                return super().ask(prompt, system_prompt, **kw)

        d = self.daemon(adapter=RefusesOnce(), max_turns=60)
        d.op_ask({"prompt": "name two rivers"})
        d.op_ask({"prompt": "and the second one?"})
        self.assertIn("Where this conversation had got to", d.adapter.last_prompt)
        self.assertIn("name two rivers", d.adapter.last_prompt)

    def test_a_rollover_that_then_fails_does_not_bridge_twice(self):
        class RefusesResume(FakeAdapter):
            def ask(self, prompt, system_prompt, **kw):
                if kw.get("resume"):
                    raise agent.AgentFailed("no such session")
                return super().ask(prompt, system_prompt, **kw)

        d = self.daemon(adapter=RefusesResume(), max_turns=1)
        d.op_ask({"prompt": "one"})
        d.op_ask({"prompt": "two"})
        self.assertEqual(
            d.adapter.last_prompt.count("Where this conversation had got to"), 1)

    def test_the_budget_is_reported_in_status(self):
        d = self.daemon(max_turns=60, token_budget=12_345)
        status = d.dispatch({"op": "status"})
        self.assertEqual(status["sessions"]["token_budget"], 12_345)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
