"""Shared test scaffolding.

Tests must never touch ~/.local/share/luna. Everything here builds memory
objects rooted in a temporary directory — and, since Phase 2, redirects the
two process-wide singletons (the spawn ledger and the audit log) as well.
Those are global by design, because the firewall has to be the same object for
every caller in the daemon; the price is that a test which forgets to redirect
them would append to the user's real audit log, so redirecting them is done
once, here, for every case.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lunad import audit as audit_mod, safety  # noqa: E402
from lunad.memory import (EpisodeStore, Memory, SolMemory,  # noqa: E402
                          Tier1File)


class FakeHyprland:
    """A compositor that answers without a compositor.

    Every method that would shell out to ``hyprctl`` is replaced. Tests that
    need the real thing are the manual verification steps, not the suite: a
    unit test that depends on a live Wayland session is a test that fails on
    someone else's machine for reasons that have nothing to do with the code.
    """

    def __init__(self, workspace: str = "luna",
                 app_id: str = "org.omarchy.luna") -> None:
        self.workspace = workspace
        self.app_id = app_id
        self.visible = False
        self.rules = 0
        self.toggles = 0

    def available(self) -> tuple[bool, str]:
        return True, "fake hyprland"

    def ensure_workspace_rule(self) -> str:
        self.rules += 1
        return "added" if self.rules == 1 else "present"

    def toggle_special(self) -> bool:
        self.toggles += 1
        self.visible = not self.visible
        return self.visible

    def special_visible(self) -> bool:
        return self.visible

    def workspace_exists(self) -> bool:
        return True

    def windows(self) -> list[dict[str, Any]]:
        return []

    def state(self) -> dict[str, Any]:
        return {"available": True, "detail": "fake hyprland",
                "workspace": f"special:{self.workspace}", "app_id": self.app_id,
                "exists": True, "visible": self.visible, "windows": 0}


class TempMemoryCase(unittest.TestCase):
    """A TestCase with a throwaway memory tree at ``self.root``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-test-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Redirect the globals before anything can touch the real ones.
        self.ledger = safety.SpawnLedger(self.root / "spawned.json")
        self.audit = audit_mod.AuditLog(self.root / "audit.jsonl")
        old_ledger = safety.use_ledger(self.ledger)
        old_audit = audit_mod.use_audit(self.audit)
        self.addCleanup(safety.use_ledger, old_ledger)
        self.addCleanup(audit_mod.use_audit, old_audit)
        self.addCleanup(safety.set_audit_hook, None)

    def sol_memory(self) -> SolMemory:
        mem = SolMemory(self.root / "memory" / "sol")
        self.addCleanup(mem.close)
        return mem

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
