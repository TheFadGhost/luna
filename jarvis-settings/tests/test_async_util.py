"""run_async: the caller must never block, and the result (or exception)
must be marshalled back to the GTK main loop, not swallowed."""

import pathlib
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import async_util  # noqa: E402
from gi.repository import GLib  # noqa: E402


def _pump_until(predicate, timeout=5.0):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        ctx.iteration(True)
    return predicate()


class RunAsyncTest(unittest.TestCase):
    def test_caller_returns_before_work_finishes(self):
        started = threading.Event()
        release = threading.Event()

        def work():
            started.set()
            release.wait(2.0)
            return "done"

        seen = []
        t0 = time.monotonic()
        async_util.run_async(work, lambda r, e: (seen.append((r, e)), False)[1])
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2, "run_async blocked the caller")
        started.wait(2.0)
        release.set()
        self.assertTrue(_pump_until(lambda: seen))
        self.assertEqual(seen, [("done", None)])

    def test_result_is_marshalled_back(self):
        seen = []

        def work():
            return 42

        def done(result, error):
            seen.append((result, error))
            return False

        async_util.run_async(work, done)
        self.assertTrue(_pump_until(lambda: seen))
        self.assertEqual(seen, [(42, None)])

    def test_exception_is_marshalled_back_not_swallowed(self):
        seen = []

        def work():
            raise ValueError("boom")

        def done(result, error):
            seen.append((result, error))
            return False

        async_util.run_async(work, done)
        self.assertTrue(_pump_until(lambda: seen))
        (result, error), = seen
        self.assertIsNone(result)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), "boom")

    def test_done_runs_on_the_main_thread(self):
        main_thread = threading.current_thread()
        seen = []

        def work():
            self.assertNotEqual(threading.current_thread(), main_thread)
            return None

        def done(result, error):
            seen.append(threading.current_thread())
            return False

        async_util.run_async(work, done)
        self.assertTrue(_pump_until(lambda: seen))
        self.assertEqual(seen[0], main_thread)


if __name__ == "__main__":
    unittest.main()
