"""Session reuse — the cost fix.

The rule being tested is narrow and easy to get wrong in the direction that
costs money silently: resume the same conversation while the cacheable prefix
is unchanged, and start a clean one the moment it is not.
"""

from __future__ import annotations

import threading
import time
import unittest

from lunad.session import SessionBusy, SessionManager, fingerprint


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


class ConcurrentAcquireTests(unittest.TestCase):
    """The race: a detached voice-router ask arriving while a CLI ask on the
    same conversation is still on its first turn.

    Before the fix, two overlapping ``acquire()`` calls for one key were both
    handed the same unstarted ``Session`` while it was still ``started =
    False``, so both computed "this is turn one" and would have handed the
    agent the identical ``--session-id``. These tests exercise the actual
    race with threads and barriers, not just the sequential happy path.
    """

    def setUp(self) -> None:
        self.prefix = fingerprint("persona", "tier1")

    def test_two_concurrent_first_turns_do_not_race(self):
        mgr = SessionManager(idle_s=100.0, max_turns=10, pending_wait_s=5.0)
        first_acquired = threading.Event()
        let_first_finish = threading.Event()
        second_done = threading.Event()
        second_args: dict = {}

        def first():
            sess = mgr.acquire("default", self.prefix)
            first_acquired.set()
            # Stand-in for "the agent call is still running": this thread
            # holds its unstarted session for a while before finishing.
            let_first_finish.wait(3.0)
            mgr.succeeded(sess, cost_usd=0.01)

        def second():
            first_acquired.wait(3.0)
            sess = mgr.acquire("default", self.prefix)   # must wait, not race
            second_args["args"] = mgr.args_for(sess)
            second_args["session_id"] = sess.session_id
            second_done.set()

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        try:
            self.assertTrue(first_acquired.wait(2.0))
            # Give the second thread a fair chance to have raced ahead if the
            # bug were present; it must still be waiting.
            time.sleep(0.2)
            self.assertFalse(second_done.is_set(),
                             "a concurrent second caller must wait for the "
                             "first turn, not be handed the same session id")
            let_first_finish.set()
            self.assertTrue(second_done.wait(3.0),
                            "the second caller never unblocked once the "
                            "first turn finished")
        finally:
            t1.join(3.0)
            t2.join(3.0)

        # Only one first turn ever happened: the second caller correctly
        # resumed the session the first one just established, rather than
        # colliding on a second `--session-id`.
        self.assertEqual(mgr.counters["new"], 1)
        self.assertIsNone(second_args["args"]["session_id"])
        self.assertIsNotNone(second_args["args"]["resume"])
        self.assertEqual(second_args["session_id"], second_args["args"]["resume"])

    def test_a_hung_first_turn_rejects_rather_than_deadlocks_the_second(self):
        # A short bound so the test itself stays fast; real usage bounds it
        # by config.AGENT_TIMEOUT_S, the same ceiling a single agent call is
        # allowed to take.
        mgr = SessionManager(idle_s=100.0, max_turns=10, pending_wait_s=0.15)
        started = threading.Event()

        def hung_first_turn():
            mgr.acquire("default", self.prefix)   # never calls succeeded()
            started.set()

        t1 = threading.Thread(target=hung_first_turn, daemon=True)
        t1.start()
        self.assertTrue(started.wait(2.0))

        before = time.monotonic()
        with self.assertRaises(SessionBusy):
            mgr.acquire("default", self.prefix)
        elapsed = time.monotonic() - before

        # Bounded, not a deadlock: an ask can be long, so a second caller
        # must give up cleanly rather than wait forever behind a hung turn.
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertEqual(mgr.counters["rejected"], 1)
        # Still exactly one session in the table: the rejection did not
        # fabricate a second one.
        self.assertEqual(len(mgr), 1)

    def test_a_retry_from_the_same_thread_is_not_treated_as_a_race(self):
        """The sequential retry-after-failure case this must not break.

        A caller whose own first attempt already returned (e.g. a synchronous
        retry, or server.py's resume-refused-so-start-fresh path) re-acquires
        on its own thread. That is not a concurrent second caller and must be
        served immediately, exactly as before this existed.
        """
        mgr = SessionManager(idle_s=100.0, max_turns=10, pending_wait_s=0.2)
        first = mgr.acquire("default", self.prefix)
        before = time.monotonic()
        again = mgr.acquire("default", self.prefix)
        elapsed = time.monotonic() - before
        self.assertLess(elapsed, 0.05, "a same-thread retry must not wait")
        self.assertIs(first, again)
        self.assertIsNone(mgr.args_for(again)["resume"])


if __name__ == "__main__":
    unittest.main()
