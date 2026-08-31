"""Tier1File: reads share the same lock as writes.

`text()`, `entries()` and `usage()` used to read the file straight off disk
with no lock at all, while every write (`replace`, `append`, `remove`,
`clear`) went through `self._lock`. That was safe only by accident of
`atomic_write`'s `os.replace` being atomic at the filesystem level -- a
reader could never observe a half-written file even without the lock -- but
it broke the invariant the rest of the class is written against: "the lock
guards every access to this file." `_check_cap` reads `entries()` from
*inside* a held lock and only works because `threading.RLock` is reentrant;
a read path that quietly opted out of the lock was one future refactor away
from being wrong in a way nobody would see in a diff.

These tests prove the reads now genuinely contend for `self._lock`, not just
that they return the right bytes (a torn read was never observable here
either way, given `os.replace`'s atomicity -- so a bytes-only test would
pass before this fix as easily as after it).
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from lunad.memory import Tier1File


class Tier1LockingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-tier1-lock-test-")
        self.addCleanup(self._tmp.cleanup)
        self.handle = Tier1File(Path(self._tmp.name) / "LUNA.md",
                                cap_default=3000, name="LUNA.md")
        self.handle.append("first entry")

    def _hold_lock_in_a_thread(self, release: threading.Event) -> threading.Event:
        """Start a thread holding ``self.handle._lock``; return once it has it."""
        acquired = threading.Event()

        def hold() -> None:
            with self.handle._lock:
                acquired.set()
                release.wait(timeout=5)

        threading.Thread(target=hold, daemon=True).start()
        self.assertTrue(acquired.wait(timeout=2), "lock was never acquired")
        return acquired

    def test_text_blocks_while_the_lock_is_held_elsewhere(self) -> None:
        release = threading.Event()
        self._hold_lock_in_a_thread(release)

        result: dict[str, str] = {}
        reader = threading.Thread(target=lambda: result.update(
            text=self.handle.text()))
        reader.start()
        time.sleep(0.1)
        still_blocked = reader.is_alive()
        release.set()
        reader.join(timeout=2)

        self.assertTrue(still_blocked,
                        "text() returned without contending for the lock")
        self.assertIn("first entry", result["text"])

    def test_entries_blocks_while_the_lock_is_held_elsewhere(self) -> None:
        release = threading.Event()
        self._hold_lock_in_a_thread(release)

        result: dict[str, list[str]] = {}
        reader = threading.Thread(target=lambda: result.update(
            entries=self.handle.entries()))
        reader.start()
        time.sleep(0.1)
        still_blocked = reader.is_alive()
        release.set()
        reader.join(timeout=2)

        self.assertTrue(still_blocked)
        self.assertEqual(result["entries"], ["first entry"])

    def test_usage_blocks_while_the_lock_is_held_elsewhere(self) -> None:
        release = threading.Event()
        self._hold_lock_in_a_thread(release)

        result: dict[str, dict] = {}
        reader = threading.Thread(target=lambda: result.update(
            usage=self.handle.usage()))
        reader.start()
        time.sleep(0.1)
        still_blocked = reader.is_alive()
        release.set()
        reader.join(timeout=2)

        self.assertTrue(still_blocked)
        self.assertEqual(result["usage"]["entries"], 1)

    def test_reads_still_return_correct_content_without_contention(self) -> None:
        # The lock must not change the answer, only when it is given.
        self.assertIn("first entry", self.handle.text())
        self.assertEqual(self.handle.entries(), ["first entry"])
        self.assertEqual(self.handle.usage()["entries"], 1)


if __name__ == "__main__":
    unittest.main()
