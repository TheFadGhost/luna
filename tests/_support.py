"""Shared test scaffolding.

Tests must never touch ~/.local/share/luna. Everything here builds memory
objects rooted in a temporary directory.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lunad.memory import EpisodeStore, Memory, Tier1File  # noqa: E402


class TempMemoryCase(unittest.TestCase):
    """A TestCase with a throwaway memory tree at ``self.root``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-test-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def tier1(self, name: str = "LUNA.md", cap: int = 200) -> Tier1File:
        return Tier1File(self.root / name, cap, name)

    def episodes(self) -> EpisodeStore:
        store = EpisodeStore(self.root / "episodes.db")
        self.addCleanup(store.close)
        return store

    def memory(self) -> Memory:
        mem = Memory(self.root / "LUNA.md", self.root / "USER.md",
                     self.root / "episodes.db")
        self.addCleanup(mem.close)
        return mem
