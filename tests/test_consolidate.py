"""The consolidation pass: the counter, the bounds, and the cap contract.

Every model call here is a scripted stub. That is not only about money — the
interesting cases in this file are the ones where the model answers badly
(unparseable JSON, an index that does not exist, a proposal too big for the
cap), and those are precisely the answers a real model gives too rarely to
test against and too often to leave unhandled.

The bounds get more attention than the happy path, deliberately. A background
job that spends the user's money on a timer has exactly one unforgivable
failure mode, and it is not "did not consolidate".
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

from lunad import agent, config, consolidate
from lunad.memory import (CONSOLIDATED_THROUGH, EpisodeStore,
                          MemoryCapExceeded, Tier1File)

from ._support import TempMemoryCase


class ScriptedAdapter(agent.BaseAdapter):
    """Answers from a script and records what it was asked.

    ``replies`` is consumed one per call; the last one repeats, so a test that
    runs two passes does not have to say the same thing twice.
    """

    name = "scripted"

    def __init__(self, *replies: str, raises: Exception | None = None,
                 cost_usd: float | None = 0.0004) -> None:
        self.replies = list(replies) or ["{}"]
        self.raises = raises
        self.cost_usd = cost_usd
        self.calls: list[dict[str, Any]] = []

    def available(self) -> tuple[bool, str]:
        return True, "scripted adapter"

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> agent.AgentReply:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt,
                           "model": kw.get("model"),
                           "session_id": kw.get("session_id"),
                           "resume": kw.get("resume"),
                           "timeout": kw.get("timeout")})
        if self.raises:
            raise self.raises
        text = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        return agent.AgentReply(text=text, agent=self.name, model="scripted-1",
                                cost_usd=self.cost_usd, wall_ms=1,
                                usage={"input_tokens": 900, "output_tokens": 40})


def proposal(luna_add: list[str] | None = None,
             luna_remove: list[int] | None = None,
             user_add: list[str] | None = None,
             note: str = "kept the two standing instructions") -> str:
    return json.dumps({"LUNA.md": {"add": luna_add or [],
                                   "remove": luna_remove or []},
                       "USER.md": {"add": user_add or [], "remove": []},
                       "note": note})


_CLI: Any = None


def _cli() -> Any:
    """``bin/luna`` loaded as a module, once, for the report it prints.

    It is a script rather than a package, so there is no import for it — and
    the report is the whole of this feature from the user's side, which is a
    contract with nothing under it unless something loads the file. Importing
    it runs only the module body: it connects to no socket and reads no
    config. The escapes are blanked afterwards because they are chosen at
    import from ``sys.stdout.isatty()``, and assertions that pass only when
    the suite is piped are worse than none.
    """
    global _CLI
    if _CLI is None:
        path = Path(__file__).resolve().parent.parent / "bin" / "luna"
        # Named loader rather than `spec_from_file_location` alone: the file
        # has no `.py` on it, so there is no extension for importlib to pick a
        # loader from and it returns None without saying why.
        loader = importlib.machinery.SourceFileLoader("luna_cli", str(path))
        spec = importlib.util.spec_from_loader("luna_cli", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        module.BOLD = module.DIM = module.RESET = ""
        _CLI = module
    return _CLI


class ConsolidatorCase(TempMemoryCase):
    def build(self, *replies: str, adapter: agent.BaseAdapter | None = None,
              **kw: Any) -> consolidate.Consolidator:
        self.memory_obj = self.memory()
        self.adapter = adapter or ScriptedAdapter(*replies)
        kw.setdefault("min_interval_s", 0.0)
        con = consolidate.Consolidator(
            self.memory_obj, adapter=lambda: self.adapter, audit=self.audit,
            settings=self.settings, **kw)
        self.addCleanup(con.close)
        return con

    def seed(self, count: int = 3) -> None:
        for n in range(count):
            self.memory_obj.episodes.record(
                f"remember that thing number {n}", "Noted.",
                ts=1000.0 + n, salience=0.5)


# =========================================================================
# The counter — including the one value that must mean "never"
# =========================================================================


class TurnCounterTests(ConsolidatorCase):
    def counted(self, con: consolidate.Consolidator) -> list[str]:
        """Replace the pass with a recorder, so no thread races the assertion."""
        fired: list[str] = []
        con.run_once = lambda why="": fired.append(why)  # type: ignore[assignment]
        return fired

    def test_zero_means_never(self) -> None:
        # The setting a user reaches for when a background pass has surprised
        # them on their bill. It must stop everything, not slow it down.
        con = self.build()
        fired = self.counted(con)
        self.settings.set("memory.consolidate_every_turns", 0)
        for _ in range(200):
            self.assertFalse(con.turn())
        self.assertEqual(fired, [])
        self.assertEqual(con.turns, 200)
        self.assertFalse(con.snapshot()["enabled"])

    def test_zero_reaches_the_model_never_even_with_episodes_waiting(self) -> None:
        con = self.build(proposal(luna_add=["a fact"]))
        self.seed()
        self.settings.set("memory.consolidate_every_turns", 0)
        for _ in range(50):
            con.turn()
        con.close()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.memory_obj.luna.entries(), [])

    def test_a_pass_starts_on_the_nth_turn_and_not_before(self) -> None:
        con = self.build()
        fired = self.counted(con)
        self.settings.set("memory.consolidate_every_turns", 4)
        self.assertEqual([con.turn() for _ in range(4)],
                         [False, False, False, True])
        self.assertEqual(len(fired), 1)
        self.assertEqual(con.turns, 0)

    def test_the_count_follows_the_setting_without_a_restart(self) -> None:
        con = self.build()
        self.counted(con)
        self.settings.set("memory.consolidate_every_turns", 10)
        self.assertEqual(con.every, 10)
        self.settings.set("memory.consolidate_every_turns", 2)
        self.assertEqual(con.every, 2)
        self.assertEqual([con.turn() for _ in range(2)], [False, True])

    def test_only_one_pass_runs_at_a_time(self) -> None:
        # A slow agent must not be able to stack passes: the turns accumulate
        # and the next one runs on a larger batch instead.
        release = threading.Event()
        con = self.build()
        self.settings.set("memory.consolidate_every_turns", 1)
        started = threading.Event()

        def slow(why: str = "") -> None:
            started.set()
            release.wait(5.0)

        con.run_once = slow                      # type: ignore[assignment]
        self.assertTrue(con.turn())
        self.assertTrue(started.wait(5.0))
        for _ in range(5):
            self.assertFalse(con.turn())
        release.set()
        con.close()
        self.assertEqual(con.snapshot()["skipped"], 5)

    def test_the_interval_floor_holds_even_at_every_one_turn(self) -> None:
        con = self.build(min_interval_s=60.0)
        self.counted(con)
        self.settings.set("memory.consolidate_every_turns", 1)
        self.assertTrue(con.turn())
        for _ in range(5):
            self.assertFalse(con.turn())

    def test_a_broken_counter_never_breaks_an_answered_ask(self) -> None:
        con = self.build()

        class _Boom:
            def get(self, *a: Any, **kw: Any) -> Any:
                raise RuntimeError("settings exploded")

        con.settings = _Boom()                   # type: ignore[assignment]
        self.assertFalse(con.turn())


# =========================================================================
# One pass, end to end
# =========================================================================


class PassTests(ConsolidatorCase):
    def test_nothing_new_means_no_model_call_at_all(self) -> None:
        # The bound that matters most: a daemon left idle with
        # `consolidate_every_turns = 1` must spend nothing whatsoever.
        con = self.build(proposal(luna_add=["should never be written"]))
        result = con.run_once()
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "no new episodes")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.memory_obj.luna.entries(), [])

    def test_a_proposal_is_applied_to_both_files(self) -> None:
        con = self.build(proposal(luna_add=["The bar is omarchy-shell."],
                                  user_add=["Writes in British English."]))
        self.seed()
        result = con.run_once()
        self.assertTrue(result["ran"])
        self.assertEqual(result["added"], 2)
        self.assertEqual(self.memory_obj.luna.entries(),
                         ["The bar is omarchy-shell."])
        self.assertEqual(self.memory_obj.user.entries(),
                         ["Writes in British English."])

    def test_removals_are_by_the_index_the_model_was_shown(self) -> None:
        con = self.build(proposal(luna_add=["Hyprland 0.56.2, Lua config."],
                                  luna_remove=[1]))
        self.memory_obj.luna.replace(["keep this", "this one is wrong",
                                      "keep this too"])
        self.seed()
        con.run_once()
        self.assertEqual(self.memory_obj.luna.entries(),
                         ["keep this", "keep this too",
                          "Hyprland 0.56.2, Lua config."])

    def test_an_empty_proposal_is_a_normal_answer(self) -> None:
        con = self.build(proposal(note="nothing here is worth keeping"))
        self.seed()
        result = con.run_once()
        self.assertTrue(result["ran"])
        self.assertEqual(result["added"], 0)
        self.assertEqual(self.memory_obj.luna.entries(), [])
        self.assertEqual(con.snapshot()["note"],
                         "nothing here is worth keeping")

    def test_the_pass_records_no_episode_of_its_own(self) -> None:
        # Otherwise it would feed itself: every pass would create the input
        # for the next one and the batch would never empty.
        con = self.build(proposal(luna_add=["a fact"]))
        self.seed(3)
        con.run_once()
        self.assertEqual(self.memory_obj.episodes.stats()["episodes"], 3)

    def test_the_prompt_is_a_librarians_and_not_lunas_persona(self) -> None:
        # ~8k tokens of specification about how to talk to a human, on a task
        # that is not a conversation, would be most of what a pass costs.
        con = self.build(proposal())
        self.seed()
        con.run_once()
        system = self.adapter.calls[0]["system_prompt"]
        self.assertNotIn("persona specification", system)
        self.assertLess(len(system), 2500)
        self.assertIn("memory keeper", system)

    def test_the_pass_uses_no_conversation_session(self) -> None:
        # A librarian's turn injected into the user's warm session would break
        # the cacheable prefix it shares with every ask.
        con = self.build(proposal())
        self.seed()
        con.run_once()
        self.assertIsNone(self.adapter.calls[0]["session_id"])
        self.assertIsNone(self.adapter.calls[0]["resume"])

    def test_the_input_is_bounded_however_much_has_happened(self) -> None:
        con = self.build(proposal(), episode_limit=5)
        self.seed(40)
        result = con.run_once()
        self.assertEqual(result["episodes"], 5)
        self.assertEqual(result["through_id"], 5)

    def test_the_message_carries_the_caps_and_the_profile(self) -> None:
        con = self.build(proposal())
        self.memory_obj.episodes.record("I use quickshell.", "ok", ts=1.0,
                                        salience=0.4)
        self.memory_obj.episodes.record("I use quickshell.", "ok", ts=2.0,
                                        salience=0.4)
        con.run_once()
        message = self.adapter.calls[0]["prompt"]
        self.assertIn("LUNA.md — 0/3000 chars used", message)
        self.assertIn("USER.md", message)
        self.assertIn("quickshell (x2)", message)

    def test_the_cost_is_reported_and_handed_to_the_spender(self) -> None:
        spent: list[float | None] = []
        con = self.build(proposal(), on_spend=spent.append)
        self.seed()
        result = con.run_once()
        self.assertEqual(spent, [0.0004])
        self.assertAlmostEqual(result["cost_usd"], 0.0004)
        self.assertAlmostEqual(con.snapshot()["cost_usd"], 0.0004)


# =========================================================================
# The watermark — the half that makes an interrupted pass safe
# =========================================================================


class WatermarkTests(ConsolidatorCase):
    def through(self) -> str:
        return self.memory_obj.episodes.get_meta(CONSOLIDATED_THROUGH)

    def test_a_completed_pass_moves_it_and_the_next_pass_sees_nothing(self) -> None:
        con = self.build(proposal())
        self.seed(3)
        self.assertEqual(self.through(), "")
        con.run_once()
        self.assertEqual(self.through(), "3")
        self.assertFalse(con.run_once()["ran"])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_new_episodes_after_a_pass_are_the_next_batch(self) -> None:
        con = self.build(proposal())
        self.seed(2)
        con.run_once()
        self.memory_obj.episodes.record("something later", "ok", ts=2000.0)
        result = con.run_once()
        self.assertEqual(result["episodes"], 1)
        self.assertIn("something later", self.adapter.calls[-1]["prompt"])

    def test_an_agent_failure_leaves_it_where_it_was(self) -> None:
        # No tokens were spent, so the same batch is offered again next time.
        con = self.build(adapter=ScriptedAdapter(
            raises=agent.AgentUnavailable("no claude on this machine")))
        self.seed(2)
        result = con.run_once()
        self.assertFalse(result["ran"])
        self.assertEqual(self.through(), "")
        self.assertEqual(con.snapshot()["failures"], 1)
        self.assertEqual(self.memory_obj.luna.entries(), [])

    def test_an_unusable_reply_retries_before_giving_up_the_batch(self) -> None:
        # The old contract moved the watermark on the very first unparseable
        # reply, unconditionally, in a `finally` -- defended as runaway-cost
        # control against per-token billing. That defence is gone: the brain
        # runs on Codex against a flat ChatGPT subscription, so a retry costs
        # nothing, and a batch of the user's episodes should not be erased
        # forever by one malformed reply. It now gets
        # `max_unparseable_retries` tries against the *same* batch (same
        # watermark, even as new episodes may extend it) before the pass
        # gives up, moves the watermark anyway, and says so loudly.
        con = self.build("I had a look and there's nothing worth keeping!",
                         max_unparseable_retries=3)
        self.seed(2)

        first = con.run_once()
        self.assertTrue(first["ran"])            # it ran, and it was paid for
        self.assertFalse(first["parsed"])
        self.assertTrue(first["retried"])
        self.assertFalse(first["gave_up"])
        self.assertEqual(first["retry_count"], 1)
        self.assertEqual(self.through(), "")     # watermark did NOT move

        second = con.run_once()
        self.assertTrue(second["retried"])
        self.assertEqual(second["retry_count"], 2)
        self.assertEqual(self.through(), "")

        third = con.run_once()
        self.assertFalse(third["retried"])
        self.assertTrue(third["gave_up"])
        self.assertEqual(third["retry_count"], 3)
        self.assertEqual(self.through(), "2")    # now it moves

        self.assertEqual(con.snapshot()["failures"], 3)
        self.assertEqual(self.memory_obj.luna.entries(), [])

        # Giving up resets the retry count, so the next batch starts clean.
        self.memory_obj.episodes.record("something new", "ok", ts=3000.0)
        fourth = con.run_once()
        self.assertEqual(fourth["retry_count"], 1)

    def test_a_reply_that_parses_on_a_retry_saves_the_whole_batch(self) -> None:
        con = self.build("not json", "not json either", proposal(),
                         max_unparseable_retries=3)
        self.seed(2)
        self.assertFalse(con.run_once()["parsed"])
        self.assertFalse(con.run_once()["parsed"])
        third = con.run_once()
        self.assertTrue(third["parsed"])
        self.assertFalse(third["retried"])
        self.assertFalse(third["gave_up"])
        self.assertEqual(self.through(), "2")


# =========================================================================
# The cap contract, which the pass does not get to bend
# =========================================================================


class CapTests(ConsolidatorCase):
    def test_a_proposal_that_does_not_fit_leaves_the_file_untouched(self) -> None:
        self.settings.set("memory.luna_cap_chars", 200)
        con = self.build(proposal(luna_add=["y" * 400]))
        self.memory_obj.luna.append("the one entry that was already here")
        before = self.memory_obj.luna.text()
        self.seed()
        result = con.run_once()

        self.assertTrue(result["ran"])           # a normal outcome, not a fault
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["over_cap"], ["LUNA.md"])
        self.assertEqual(self.memory_obj.luna.text(), before)
        self.assertNotIn("yyyy", self.memory_obj.luna.text())

    def test_the_overflow_is_recorded_with_what_would_have_to_go(self) -> None:
        self.settings.set("memory.luna_cap_chars", 200)
        con = self.build(proposal(luna_add=["y" * 400]))
        self.seed()
        report = con.run_once()["files"]["LUNA.md"]["over_cap"]
        self.assertEqual(report["error"], "MemoryCapExceeded")
        self.assertEqual(report["cap"], 200)
        self.assertGreater(report["overflow"], 0)

    def test_one_file_overflowing_does_not_block_the_other(self) -> None:
        self.settings.set("memory.luna_cap_chars", 200)
        con = self.build(proposal(luna_add=["y" * 400],
                                  user_add=["Prefers short answers."]))
        self.seed()
        con.run_once()
        self.assertEqual(self.memory_obj.luna.entries(), [])
        self.assertEqual(self.memory_obj.user.entries(),
                         ["Prefers short answers."])

    def test_an_overflow_still_lets_the_next_pass_try(self) -> None:
        self.settings.set("memory.luna_cap_chars", 200)
        con = self.build(proposal(luna_add=["y" * 400]),
                         adapter=ScriptedAdapter(
                             proposal(luna_add=["y" * 400]),
                             proposal(luna_add=["short enough"])))
        self.seed(2)
        con.run_once()
        self.memory_obj.episodes.record("more to think about", "ok", ts=3000.0)
        con.run_once()
        self.assertEqual(self.memory_obj.luna.entries(), ["short enough"])

    def test_the_ordinary_cap_error_is_unchanged(self) -> None:
        # The pass is subject to the same rule as any other write, and the
        # rule is still "rejected whole, nothing truncated".
        self.settings.set("memory.luna_cap_chars", 200)
        mem = self.memory()
        with self.assertRaises(MemoryCapExceeded):
            mem.luna.append("z" * 400)


# =========================================================================
# Parsing, and the bounds on what a model may do to a curated file
# =========================================================================


class ProposalParsingTests(TempMemoryCase):
    def test_a_fenced_object_is_still_read(self) -> None:
        # Models fence JSON about one time in ten however plainly they are
        # told not to, and the call has already been paid for.
        text = '```json\n{"note": "fenced"}\n```'
        self.assertEqual(consolidate.parse_proposal(text)["note"], "fenced")

    def test_prose_around_the_object_is_tolerated(self) -> None:
        text = 'Here you go:\n{"note": "ok"}\nHope that helps.'
        self.assertEqual(consolidate.parse_proposal(text)["note"], "ok")

    def test_no_object_at_all_raises(self) -> None:
        with self.assertRaises(ValueError):
            consolidate.parse_proposal("nothing worth keeping this time")

    def test_a_json_array_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            consolidate.parse_proposal("[1, 2, 3]")

    def test_additions_are_capped_in_number_and_length(self) -> None:
        adds, _ = consolidate.clean_edit(
            {"add": ["x" * 900] + [f"entry {n}" for n in range(20)]}, 0)
        self.assertEqual(len(adds), config.CONSOLIDATE_MAX_ADDITIONS)
        self.assertEqual(len(adds[0]), config.CONSOLIDATE_ENTRY_CHARS)

    def test_an_invented_index_is_dropped_not_applied(self) -> None:
        # Applying it would shift the meaning of every removal after it.
        _, removes = consolidate.clean_edit({"remove": [0, 7, -1, 2]}, 3)
        self.assertEqual(removes, [0, 2])

    def test_nonsense_shapes_are_read_as_no_edit(self) -> None:
        # `{"add": "one entry"}` is the interesting one: a string is iterable
        # and slices into characters, which without a type check writes one
        # tier-1 entry per letter.
        for bad in (None, [], "add everything", {"add": "not a list"},
                    {"remove": [True, "0"]}, {"remove": "0"}):
            self.assertEqual(consolidate.clean_edit(bad, 3), ([], []))


# =========================================================================
# Running one by hand
# =========================================================================


class ManualPassTests(ConsolidatorCase):
    """`luna memory consolidate`. Which guards a person may override, and
    which of them still say no when it is a person asking."""

    def test_a_pass_asked_for_by_hand_runs_now_and_applies_its_proposal(self) -> None:
        con = self.build(proposal(luna_add=["The bar is omarchy-shell."]))
        self.seed()
        result = con.run_manual()
        self.assertTrue(result["ran"])
        self.assertTrue(result["manual"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(self.memory_obj.luna.entries(),
                         ["The bar is omarchy-shell."])

    def test_the_interval_floor_does_not_hold_against_a_person(self) -> None:
        # The floor exists to stop a *timer* running away. There is no timer
        # here, only someone who typed the command and can see the bill.
        con = self.build(proposal(luna_add=["a fact"]), min_interval_s=600.0)
        self.seed()
        con.run_once()
        self.memory_obj.episodes.record("something later", "ok", ts=2000.0)
        result = con.run_manual()
        self.assertTrue(result["ran"])
        self.assertGreater(result["floor_left_s"], 0.0)

    def test_the_turn_counter_is_neither_read_nor_reset(self) -> None:
        # A manual pass that zeroed the counter would silently postpone the
        # automatic one, and a user who ran one out of curiosity would have
        # changed a schedule they were only looking at.
        con = self.build(proposal(luna_add=["a fact"]))
        self.settings.set("memory.consolidate_every_turns", 4)
        self.seed()
        self.assertEqual([con.turn() for _ in range(2)], [False, False])
        con.run_manual()
        self.assertEqual(con.turns, 2)
        self.assertEqual(con.snapshot()["turns_since"], 2)

    def test_the_off_switch_is_not_overridable_by_hand(self) -> None:
        # `0` promises that nothing is counted and nothing is spent. A command
        # that spent money anyway would make the promise a lie.
        con = self.build(proposal(luna_add=["should never be written"]))
        self.seed()
        self.settings.set("memory.consolidate_every_turns", 0)
        for dry in (False, True):
            result = con.run_manual(dry_run=dry)
            self.assertFalse(result["ran"])
            self.assertEqual(result["reason"], "disabled")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.memory_obj.luna.entries(), [])

    def test_nothing_new_is_a_reason_and_not_a_model_call(self) -> None:
        con = self.build(proposal(luna_add=["should never be written"]))
        result = con.run_manual()
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "no new episodes")
        self.assertEqual(self.adapter.calls, [])

    def test_one_asked_for_while_a_pass_is_in_flight_refuses(self) -> None:
        release = threading.Event()
        started = threading.Event()
        con = self.build(proposal(luna_add=["a fact"]))
        self.settings.set("memory.consolidate_every_turns", 1)

        def slow(why: str = "", manual: bool = False) -> None:
            started.set()
            release.wait(5.0)

        con.run_once = slow                      # type: ignore[assignment]
        self.assertTrue(con.turn())
        self.assertTrue(started.wait(5.0))
        result = con.run_manual()
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "already running")
        release.set()
        con.close()

    def test_a_turn_landing_mid_pass_does_not_start_a_second_one(self) -> None:
        # The same guard from the other side. The daemon answers each
        # connection on its own thread, so an ask can genuinely complete while
        # a manual pass is still waiting on the model.
        con = self.build(proposal())
        self.seed()
        self.settings.set("memory.consolidate_every_turns", 1)
        scripted, turned = self.adapter, []

        class _Reentrant(agent.BaseAdapter):
            name = "reentrant"

            def available(self) -> tuple[bool, str]:
                return True, "reentrant adapter"

            def ask(self, *a: Any, **kw: Any) -> agent.AgentReply:
                turned.append(con.turn())
                return scripted.ask(*a, **kw)

        self.adapter = _Reentrant()
        con.run_manual()
        self.assertEqual(turned, [False])
        self.assertEqual(con.snapshot()["skipped"], 1)
        self.assertEqual(len(scripted.calls), 1)

    def test_the_cost_goes_through_the_same_spender(self) -> None:
        spent: list[float | None] = []
        con = self.build(proposal(), on_spend=spent.append)
        self.seed()
        con.run_manual()
        self.assertEqual(spent, [0.0004])

    def test_the_audit_entry_says_a_person_asked_for_it(self) -> None:
        con = self.build(proposal(luna_add=["a fact"]))
        self.seed()
        con.run_manual()
        entry = self.audit.read(action="memory.consolidate")[0]
        self.assertTrue(entry["manual"])
        self.assertNotIn("dry_run", entry)

    def test_an_automatic_pass_still_says_it_was_not_asked_for(self) -> None:
        con = self.build(proposal(luna_add=["a fact"]))
        self.seed()
        con.run_once()
        self.assertFalse(self.audit.read(action="memory.consolidate")[0]["manual"])


# =========================================================================
# The dry run, which is the one that has to be provably unable to write
# =========================================================================


class DryRunTests(ConsolidatorCase):
    def through(self) -> str:
        return self.memory_obj.episodes.get_meta(CONSOLIDATED_THROUGH)

    def test_the_whole_proposal_is_reported_and_none_of_it_applied(self) -> None:
        con = self.build(proposal(luna_add=["The bar is omarchy-shell."],
                                  user_add=["Writes in British English."]))
        self.seed()
        result = con.run_manual(dry_run=True)
        self.assertTrue(result["ran"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["files"]["LUNA.md"]["added"],
                         ["The bar is omarchy-shell."])
        self.assertEqual(self.memory_obj.luna.entries(), [])
        self.assertEqual(self.memory_obj.user.entries(), [])

    def test_it_does_not_reach_a_write_even_when_a_write_would_fail_loudly(self) -> None:
        # The claim is structural, so it is tested structurally: the two calls
        # that change state — the tier-1 write and the watermark — are
        # replaced with something that raises, and the dry run does not
        # notice. This is the test that fails the day somebody implements a
        # preview as "apply it and put it back".
        con = self.build(proposal(luna_add=["a fact"], luna_remove=[0]))
        self.memory_obj.luna.replace(["the entry it wants to drop"])
        self.seed()

        def boom(*a: Any, **kw: Any) -> Any:
            raise AssertionError("a dry run reached a write")

        write, watermark = Tier1File.replace, EpisodeStore.set_meta
        Tier1File.replace = boom                 # type: ignore[assignment]
        EpisodeStore.set_meta = boom             # type: ignore[assignment]
        try:
            result = con.run_manual(dry_run=True)
        finally:
            Tier1File.replace = write            # type: ignore[assignment]
            EpisodeStore.set_meta = watermark    # type: ignore[assignment]
        self.assertTrue(result["ran"])
        self.assertEqual(result["files"]["LUNA.md"]["added"], ["a fact"])
        self.assertEqual(self.memory_obj.luna.entries(),
                         ["the entry it wants to drop"])

    def test_the_watermark_stays_put_so_the_next_pass_sees_the_same_batch(self) -> None:
        # Moved by a preview, the episodes you looked at would be skipped by
        # the pass you were deciding whether to allow.
        con = self.build(proposal(luna_add=["a fact"]))
        self.seed(3)
        con.run_manual(dry_run=True)
        self.assertEqual(self.through(), "")
        result = con.run_manual()
        self.assertEqual(result["episodes"], 3)
        self.assertEqual(self.through(), "3")

    def test_tier_three_is_in_the_prompt_and_not_on_the_disk(self) -> None:
        con = self.build(proposal())
        self.memory_obj.episodes.record("I use quickshell.", "ok", ts=1.0,
                                        salience=0.4)
        self.memory_obj.episodes.record("I use quickshell.", "ok", ts=2.0,
                                        salience=0.4)
        con.run_manual(dry_run=True)
        self.assertIn("quickshell (x2)", self.adapter.calls[0]["prompt"])
        self.assertFalse(self.memory_obj.profile.status()["exists"])

    def test_the_prompt_is_the_one_the_real_pass_would_have_sent(self) -> None:
        # A preview built from a different prompt is a preview of something
        # else, and the difference would not show in the output.
        con = self.build(proposal())
        self.seed(2)
        con.run_manual(dry_run=True)
        con.run_manual()
        previewed, applied = self.adapter.calls
        self.assertEqual(previewed["prompt"], applied["prompt"])
        self.assertEqual(previewed["system_prompt"], applied["system_prompt"])

    def test_a_removal_is_shown_with_its_index_and_its_text(self) -> None:
        con = self.build(proposal(luna_remove=[1]))
        self.memory_obj.luna.replace(["keep this", "this one is wrong",
                                      "keep this too"])
        self.seed()
        record = con.run_manual(dry_run=True)["files"]["LUNA.md"]
        self.assertEqual(record["removed"], ["this one is wrong"])
        self.assertEqual(record["removed_index"], [1])
        self.assertEqual(self.memory_obj.luna.entries(),
                         ["keep this", "this one is wrong", "keep this too"])

    def test_a_proposal_that_would_not_fit_is_previewed_as_refused(self) -> None:
        self.settings.set("memory.luna_cap_chars", 200)
        con = self.build(proposal(luna_add=["y" * 400]))
        self.seed()
        record = con.run_manual(dry_run=True)["files"]["LUNA.md"]
        self.assertEqual(record["added"], [])
        self.assertEqual(record["over_cap"]["cap"], 200)
        self.assertGreater(record["over_cap"]["overflow"], 0)
        self.assertEqual(record["refused"]["added"],
                         ["y" * config.CONSOLIDATE_ENTRY_CHARS])

    def test_it_costs_what_the_pass_it_previews_costs(self) -> None:
        spent: list[float | None] = []
        con = self.build(proposal(), on_spend=spent.append)
        self.seed()
        con.run_manual(dry_run=True)
        self.assertEqual(spent, [0.0004])
        snapshot = con.snapshot()
        self.assertEqual(snapshot["previews"], 1)
        self.assertEqual(snapshot["passes"], 0)
        self.assertAlmostEqual(snapshot["cost_usd"], 0.0004)

    def test_an_unreadable_reply_is_a_failure_and_still_writes_nothing(self) -> None:
        con = self.build("I had a look and there's nothing worth keeping!")
        self.seed(2)
        result = con.run_manual(dry_run=True)
        self.assertTrue(result["ran"])
        self.assertFalse(result["parsed"])
        self.assertEqual(self.through(), "")
        self.assertEqual(con.snapshot()["failures"], 1)

    def test_an_agent_that_cannot_be_reached_spends_nothing(self) -> None:
        con = self.build(adapter=ScriptedAdapter(
            raises=agent.AgentUnavailable("no claude on this machine")))
        self.seed(2)
        result = con.run_manual(dry_run=True)
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "agent unavailable")
        self.assertEqual(self.through(), "")

    def test_the_audit_entry_cannot_be_mistaken_for_a_write(self) -> None:
        # Somebody grepping the log for what was removed from LUNA.md must not
        # find this entry and believe it.
        con = self.build(proposal(luna_add=["a fact"], luna_remove=[0]))
        self.memory_obj.luna.replace(["an entry"])
        self.seed()
        con.run_manual(dry_run=True)
        entry = self.audit.read(action="memory.consolidate")[0]
        self.assertTrue(entry["dry_run"])
        self.assertTrue(entry["manual"])
        self.assertEqual(entry["would_add"], 1)
        self.assertEqual(entry["would_remove"], 1)
        self.assertNotIn("added", entry)
        self.assertNotIn("removed", entry)


class PreviewEditTests(TempMemoryCase):
    """The pure half of the dry run: lists and an integer, no file anywhere."""

    def test_it_reports_the_file_the_pass_would_have_left(self) -> None:
        record = consolidate.preview_edit(
            "LUNA.md", ["keep this", "drop this"], 3000, ["and add this"], [1])
        self.assertEqual(record["added"], ["and add this"])
        self.assertEqual(record["removed"], ["drop this"])
        self.assertEqual(record["removed_index"], [1])
        self.assertEqual(record["usage"]["entries"], 2)
        self.assertIsNone(record["over_cap"])

    def test_the_cap_arithmetic_is_the_one_the_write_would_have_done(self) -> None:
        record = consolidate.preview_edit("LUNA.md", ["a"], 20, ["y" * 100], [])
        self.assertEqual(record["added"], [])
        self.assertEqual(record["over_cap"]["error"], "MemoryCapExceeded")
        self.assertEqual(record["over_cap"]["cap"], 20)
        self.assertEqual(record["refused"]["added"], ["y" * 100])


# =========================================================================
# Through the daemon
# =========================================================================


class DaemonWiringTests(TempMemoryCase):
    def daemon(self, reply: str = ""):
        from lunad import dispatch
        from lunad.server import Daemon
        from ._support import FakeHyprland

        dispatcher = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                         hypr=FakeHyprland(), audit=self.audit,
                                         sol_memory_dir=self.root / "sol",
                                         terminal="/bin/bash",
                                         agent_bin="/bin/true")
        daemon = Daemon(agent_name="claude", memory=self.memory(),
                        sol_memory=self.sol_memory(), audit=self.audit,
                        dispatcher=dispatcher)
        daemon.adapter = ScriptedAdapter(reply or proposal())
        daemon.speech.close()
        self.addCleanup(daemon.close)
        return daemon

    def test_status_reports_the_consolidation_state(self) -> None:
        resp = self.daemon().dispatch({"op": "status"})
        self.assertTrue(resp["ok"])
        block = resp["consolidation"]
        self.assertTrue(block["enabled"])
        self.assertEqual(block["every_turns"], 12)
        self.assertEqual(block["passes"], 0)

    def test_memory_read_carries_tier_three(self) -> None:
        resp = self.daemon().dispatch({"op": "memory.read"})
        self.assertTrue(resp["tier3"]["implemented"])
        self.assertFalse(resp["tier3"]["exists"])

    def test_the_profile_op_reads_and_rebuilds(self) -> None:
        d = self.daemon()
        d.memory.episodes.record("I use quickshell.", "ok", ts=1.0, salience=0.3)
        d.memory.episodes.record("I use quickshell.", "ok", ts=2.0, salience=0.3)
        self.assertEqual(d.dispatch({"op": "memory.profile"})["profile"], {})
        resp = d.dispatch({"op": "memory.profile", "rebuild": True})
        self.assertEqual(resp["profile"]["episodes"], 2)
        self.assertIn("quickshell (x2)", resp["block"])
        # And it is readable afterwards without rebuilding again.
        self.assertEqual(d.dispatch({"op": "memory.profile"})["profile"],
                         resp["profile"])

    def test_an_ask_counts_a_turn_without_waiting_for_a_pass(self) -> None:
        d = self.daemon()
        started = time.monotonic()
        resp = d.dispatch({"op": "ask", "prompt": "what is the bar", "id": "x"})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["consolidating"])
        self.assertEqual(d.consolidator.turns, 1)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_the_adapter_is_read_late_so_an_agent_switch_is_honoured(self) -> None:
        # The daemon rebinds `self.adapter` on a settings change, and a
        # background thread holding the old object would go on shelling out to
        # the CLI the user just switched away from.
        d = self.daemon()
        swapped = ScriptedAdapter(proposal(luna_add=["from the new adapter"]))
        d.adapter = swapped
        d.memory.episodes.record("something to think about", "ok", ts=1.0)
        d.consolidator.min_interval_s = 0.0
        d.consolidator.run_once()
        self.assertEqual(len(swapped.calls), 1)
        self.assertEqual(d.memory.luna.entries(), ["from the new adapter"])

    def test_a_pass_is_one_line_in_the_audit_log(self) -> None:
        d = self.daemon(proposal(luna_add=["The bar is omarchy-shell."]))
        d.memory.episodes.record("the bar is omarchy-shell", "noted", ts=1.0)
        d.consolidator.min_interval_s = 0.0
        d.consolidator.run_once()
        entries = self.audit.read(action="memory.consolidate")
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["ok"])
        self.assertEqual(entries[0]["added"], 1)
        self.assertNotIn("undo", entries[0])

    def test_the_consolidate_op_runs_one_on_the_spot(self) -> None:
        d = self.daemon(proposal(luna_add=["The bar is omarchy-shell."]))
        d.memory.episodes.record("the bar is omarchy-shell", "noted", ts=1.0)
        resp = d.dispatch({"op": "memory.consolidate"})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["ran"])
        self.assertTrue(resp["manual"])
        self.assertFalse(resp["dry_run"])
        self.assertEqual(d.memory.luna.entries(), ["The bar is omarchy-shell."])
        # Through the daemon's own money counter, like every other spend.
        self.assertAlmostEqual(d.cost_usd, 0.0004)

    def test_the_consolidate_op_dry_runs_without_touching_anything(self) -> None:
        d = self.daemon(proposal(luna_add=["The bar is omarchy-shell."]))
        d.memory.episodes.record("the bar is omarchy-shell", "noted", ts=1.0)
        resp = d.dispatch({"op": "memory.consolidate", "dry_run": True})
        self.assertTrue(resp["dry_run"])
        self.assertEqual(resp["files"]["LUNA.md"]["added"],
                         ["The bar is omarchy-shell."])
        self.assertEqual(d.memory.luna.entries(), [])
        self.assertEqual(d.memory.episodes.get_meta(CONSOLIDATED_THROUGH), "")

    def test_status_counts_a_dry_run_apart_from_a_pass(self) -> None:
        d = self.daemon()
        d.memory.episodes.record("something to think about", "ok", ts=1.0)
        d.dispatch({"op": "memory.consolidate", "dry_run": True})
        block = d.dispatch({"op": "status"})["consolidation"]
        self.assertEqual(block["previews"], 1)
        self.assertEqual(block["passes"], 0)
        self.assertFalse(block["running"])


# =========================================================================
# What it prints, which is the whole of the feature from the outside
# =========================================================================


class ReportTests(TempMemoryCase):
    """`bin/luna`'s consolidation report.

    The daemon can be right and the command still useless: a pass that did
    nothing and said nothing about why is the one answer a person cannot act
    on. So each outcome is asserted to name its own cause.
    """

    def printed(self, resp: dict[str, Any]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = _cli()._print_consolidation(resp)
        return code, out.getvalue() + err.getvalue()

    def test_every_way_of_doing_nothing_says_which_guard_said_no(self) -> None:
        for reason, expected in (
                ("no new episodes", "no exchanges recorded since"),
                ("already running", "a pass is already running"),
                ("disabled", "consolidate_every_turns = 0"),
        ):
            code, text = self.printed({"ran": False, "reason": reason,
                                       "after_id": 4})
            self.assertEqual(code, 0, reason)
            self.assertIn(expected, text)

    def test_the_off_switch_is_printed_with_the_way_to_turn_it_on(self) -> None:
        _, text = self.printed({"ran": False, "reason": "disabled"})
        self.assertIn("luna settings set memory.consolidate_every_turns 12",
                      text)

    def test_an_unreachable_agent_is_a_failure_and_says_nothing_was_spent(self) -> None:
        code, text = self.printed({"ran": False, "reason": "agent unavailable",
                                   "detail": "no claude on this machine"})
        self.assertEqual(code, 1)
        self.assertIn("nothing was spent", text)
        self.assertIn("no claude on this machine", text)

    def test_a_dry_run_leads_with_the_fact_that_it_wrote_nothing(self) -> None:
        code, text = self.printed(self.report(dry_run=True))
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("dry run — nothing was written."))
        self.assertIn("would consolidate 3 episode(s)", text)
        self.assertIn("+ The bar is omarchy-shell.", text)
        self.assertIn("- [2] Uses waybar.", text)
        self.assertIn("the watermark did not move", text)
        self.assertIn("luna memory consolidate", text)

    def test_an_applied_pass_prints_what_went_in_and_what_came_out(self) -> None:
        code, text = self.printed(self.report())
        self.assertEqual(code, 0)
        self.assertIn("consolidated 3 episode(s)", text)
        self.assertNotIn("would consolidate", text)
        self.assertIn("+ The bar is omarchy-shell.", text)
        self.assertIn("$0.0004", text)
        self.assertIn("note: promoted one standing fact", text)
        # The file the pass had nothing to do to is named as untouched rather
        # than measured, because this pass is not why it is the size it is.
        self.assertIn("USER.md unchanged", text)

    def test_an_overridden_floor_is_reported_rather_than_hidden(self) -> None:
        _, text = self.printed({**self.report(), "floor_left_s": 212.0})
        self.assertIn("212s of the interval floor", text)

    def test_a_reply_that_could_not_be_read_is_a_failure(self) -> None:
        code, text = self.printed({**self.report(), "parsed": False,
                                   "files": {}, "note": ""})
        self.assertEqual(code, 1)
        self.assertIn("could not be read as a proposal", text)

    def test_an_overflow_names_the_entry_that_would_not_fit(self) -> None:
        report = self.report()
        report["files"]["LUNA.md"] = {
            "added": [], "removed": [], "removed_index": [],
            "over_cap": {"cap": 3000, "overflow": 42, "proposed_chars": 3042},
            "refused": {"added": ["The bar is omarchy-shell."], "removed": []},
            "usage": {"file": "LUNA.md", "chars": 3042, "cap": 3000,
                      "pct": 101.4, "entries": 12},
        }
        _, text = self.printed(report)
        self.assertIn("rejected whole", text)
        self.assertIn("over cap by 42 chars", text)
        self.assertIn("would not have fitted: The bar is omarchy-shell.", text)

    def report(self, dry_run: bool = False) -> dict[str, Any]:
        """One pass as the daemon reports it, applied or previewed."""
        return {
            "ok": True, "ran": True, "manual": True, "dry_run": dry_run,
            "parsed": True, "episodes": 3, "through_id": 7,
            "cost_usd": 0.0004, "wall_ms": 1200,
            "note": "promoted one standing fact", "floor_left_s": 0.0,
            "files": {
                "LUNA.md": {"added": ["The bar is omarchy-shell."],
                            "removed": ["Uses waybar."], "removed_index": [2],
                            "over_cap": None,
                            "usage": {"file": "LUNA.md", "chars": 120,
                                      "cap": 3000, "pct": 4.0, "entries": 3}},
                "USER.md": {"added": [], "removed": [], "over_cap": None},
            },
        }
