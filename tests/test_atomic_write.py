"""atomic_write: durability (fsync) layered on top of the atomic rename.

Every file memory owns -- LUNA.md, USER.md, SOL.md, profile.json -- goes
through lunad.memory.atomic_write. The temp-file-then-rename shape already
made it atomic against a process kill: a reader sees either the whole old
file or the whole new one, never a half-written mix. It was not durable
against power loss, because a rename (and the write before it) can sit in
the page cache and vanish with the machine. These tests are for the fsync
calls that close that gap, and for the one thing that must not change: a
filesystem or platform that refuses fsync must not turn a memory write into
an unhandled exception on the answer path.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lunad.memory import atomic_write


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-atomic-write-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_content_round_trips(self) -> None:
        target = self.root / "LUNA.md"
        atomic_write(target, "hello world")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

    def test_no_tmp_file_is_left_behind(self) -> None:
        target = self.root / "LUNA.md"
        atomic_write(target, "hello world")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_creates_parent_directories(self) -> None:
        target = self.root / "nested" / "dir" / "LUNA.md"
        atomic_write(target, "hello")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")

    def test_fsyncs_the_temp_file_and_the_directory(self) -> None:
        # One fsync for the new content before the rename, one for the
        # directory entry the rename changes -- see the docstring on
        # atomic_write for why both are needed for durability, not just the
        # rename's atomicity.
        target = self.root / "LUNA.md"
        real_fsync = os.fsync
        synced_fds: list[int] = []

        def spy(fd: int) -> None:
            synced_fds.append(fd)
            real_fsync(fd)

        with mock.patch("lunad.memory.os.fsync", side_effect=spy) as spied:
            atomic_write(target, "hello world")

        # Not asserting the fds differ: fd numbers are small integers the OS
        # is free to reuse once the file's fd is closed, so two fsync calls
        # can legitimately see the same number for two different files.
        self.assertEqual(spied.call_count, 2)
        self.assertEqual(len(synced_fds), 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

    def test_a_refused_file_fsync_does_not_raise_or_lose_the_write(self) -> None:
        target = self.root / "LUNA.md"
        with mock.patch("lunad.memory.os.fsync", side_effect=OSError("nope")):
            atomic_write(target, "hello world")  # must not raise
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

    def test_a_refused_directory_open_does_not_raise_or_lose_the_write(self) -> None:
        target = self.root / "LUNA.md"
        with mock.patch("lunad.memory.os.open", side_effect=OSError("nope")):
            atomic_write(target, "hello world")  # must not raise
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

    def test_overwrite_still_round_trips(self) -> None:
        target = self.root / "LUNA.md"
        atomic_write(target, "first")
        atomic_write(target, "second")
        self.assertEqual(target.read_text(encoding="utf-8"), "second")


if __name__ == "__main__":
    unittest.main()
