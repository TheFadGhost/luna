"""Session reuse — the cost fix.

The rule being tested is narrow and easy to get wrong in the direction that
costs money silently: resume the same conversation while the cacheable prefix
is unchanged, and start a clean one the moment it is not.
"""

from __future__ import annotations

import time
import unittest

from lunad.session import SessionManager, fingerprint


class FingerprintTests(unittest.TestCase):
    def test_same_input_same_fingerprint(self):
        self.assertEqual(fingerprint("persona", "tier1"),
                         fingerprint("persona", "tier1"))

    def test_any_change_moves_it(self):
        self.assertNotEqual(fingerprint("persona", "tier1"),
                            fingerprint("persona", "tier1 "))

    def test_field_boundaries_are_not_ambiguous(self):
        # Without a separator "ab"+"c" and "a"+"bc" would hash identically, and
        # a memory edit that only moved a boundary would go unnoticed.
        self.assertNotEqual(fingerprint("ab", "c"), fingerprint("a", "bc"))


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = SessionManager(idle_s=100.0, max_turns=3)
        self.prefix = fingerprint("persona", "tier1")

    def turn(self, key: str = "default", prefix: str | None = None,
             now: float | None = None):
        sess = self.mgr.acquire(key, prefix or self.prefix, now=now)
        args = self.mgr.args_for(sess)
        self.mgr.succeeded(sess, cost_usd=0.01)
        return sess, args

    def test_the_first_turn_creates_a_session_id(self):
        _, args = self.turn()
        self.assertIsNotNone(args["session_id"])
        self.assertIsNone(args["resume"])
        self.assertEqual(self.mgr.counters["new"], 1)

    def test_the_second_turn_resumes_the_same_id(self):
        first, first_args = self.turn()
        _, second_args = self.turn()
        self.assertIsNone(second_args["session_id"])
        self.assertEqual(second_args["resume"], first_args["session_id"])
        self.assertEqual(self.mgr.counters["resumed"], 1)
        self.assertEqual(first.turns, 2)

    def test_a_session_id_is_a_uuid(self):
        import uuid
        _, args = self.turn()
        uuid.UUID(args["session_id"])          # raises if it is not one

    def test_different_conversations_do_not_share_a_session(self):
        _, voice = self.turn("voice")
        _, cli = self.turn("cli")
        self.assertNotEqual(voice["session_id"], cli["session_id"])
        self.assertEqual(len(self.mgr), 2)

    def test_a_tier1_change_starts_a_clean_session(self):
        _, first = self.turn()
        _, after = self.turn(prefix=fingerprint("persona", "tier1 + new fact"))
        self.assertIsNotNone(after["session_id"])
        self.assertNotEqual(after["session_id"], first["session_id"])
        self.assertEqual(self.mgr.counters["retired"], 1)

    def test_an_idle_session_is_retired(self):
        now = time.time()
        _, first = self.turn(now=now)
        sess = self.mgr.acquire("default", self.prefix, now=now + 1000)
        self.assertNotEqual(sess.session_id, first["session_id"])
        self.assertEqual(self.mgr.counters["retired"], 1)

    def test_a_session_is_retired_after_max_turns(self):
        ids = []
        for _ in range(5):
            sess, args = self.turn()
            ids.append(sess.session_id)
        self.assertGreater(len(set(ids)), 1)
        self.assertGreaterEqual(self.mgr.counters["retired"], 1)

    def test_the_agents_own_session_id_wins(self):
        sess = self.mgr.acquire("default", self.prefix)
        self.mgr.succeeded(sess, 0.01, reported_id="a-different-id")
        self.assertEqual(sess.session_id, "a-different-id")
        self.assertEqual(self.mgr.args_for(sess)["resume"], "a-different-id")

    def test_an_unstarted_session_is_never_resumed_into(self):
        # A failed first turn leaves an id the agent has never seen. Resuming
        # it would fail every time until the idle timeout.
        sess = self.mgr.acquire("default", self.prefix)
        again = self.mgr.acquire("default", self.prefix)
        self.assertIs(sess, again)
        self.assertIsNone(self.mgr.args_for(again)["resume"])

    def test_cost_accumulates_per_conversation(self):
        self.turn()
        self.turn()
        self.assertAlmostEqual(self.mgr.snapshot()[0]["cost_usd"], 0.02)

    def test_drop_and_clear(self):
        self.turn("a")
        self.turn("b")
        self.assertTrue(self.mgr.drop("a"))
        self.assertFalse(self.mgr.drop("a"))
        self.assertEqual(self.mgr.clear(), 1)
        self.assertEqual(len(self.mgr), 0)

    def test_a_blank_key_falls_back_to_the_default_conversation(self):
        from lunad import config
        sess = self.mgr.acquire("", self.prefix)
        self.assertEqual(sess.key, config.DEFAULT_CONVERSATION)


if __name__ == "__main__":
    unittest.main()
