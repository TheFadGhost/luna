"""Shared test scaffolding.

Tests must never touch ~/.local/share/luna. Everything here builds memory
objects rooted in a temporary directory — and, since Phase 2, redirects the
two process-wide singletons (the spawn ledger and the audit log) as well.
Those are global by design, because the firewall has to be the same object for
every caller in the daemon; the price is that a test which forgets to redirect
them would append to the user's real audit log, so redirecting them is done
once, here, for every case.

Since the Jarvis pass the settings singleton is redirected too, and for a
sharper reason: a test that read the *real* config would pass or fail
depending on what the user last changed in the GUI.

Importing this module also disarms every ``config`` name that reaches the
outside world -- the terminal, the notifier, ``aplay``, ``hyprctl`` and the
piper interpreter -- for the whole test process. See ``FORBIDDEN_TERMINAL``
and the block after it: a test that forgets to stub one of these opens windows,
toasts, audio or workspaces on the user's live desktop, and that is not a
failure the suite can be trusted to notice on its own. ``tests/test_guards.py``
asserts the whole arrangement, including that none of these may go back to
being a signature default.
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lunad import (audit as audit_mod, config, safety,  # noqa: E402
                   settings as settings_mod)
from lunad.memory import (EpisodeStore, Memory, SolMemory,  # noqa: E402
                          Tier1File)

#: What ``config.TERMINAL_BIN`` is replaced with for the whole test process.
#:
#: ``Dispatcher`` shells out to Omarchy's real terminal, and three cases used
#: to build one without stubbing it: every suite run opened three ``foot``
#: windows on the live desktop, each of which then segfaulted when teardown
#: deleted the ``run.sh`` it was executing out from under it -- three core
#: dumps and three "Process crashed" notifications per run, plus the stray
#: ``/tmp/luna-test-*`` the dying job's watcher thread recreated on its way
#: out. A test must never reach the user's desktop, so the name is replaced
#: with one that cannot resolve: ``Dispatcher.available()`` reports it as not
#: on PATH and ``dispatch()`` raises ``DispatchUnavailable`` naming the fix,
#: which is a loud in-test failure rather than a window.
FORBIDDEN_TERMINAL = "luna-tests-must-pass-terminal=/bin/bash"

config.TERMINAL_BIN = FORBIDDEN_TERMINAL

#: The rest of the same class of bug, disarmed the same way.
#:
#: A binary name bound as a *signature default* is fixed at import and cannot
#: be patched afterwards, so a test can stub the object and still reach the
#: real program. That is how three ``foot`` windows a run happened, and then
#: how a wall of ten real Omarchy toasts happened the moment
#: ``[ui] notify_on_finish`` was wired: five test modules legitimately pass
#: ``terminal="/bin/bash"`` so a job runs headlessly, the job then genuinely
#: *finishes*, and the finish handler notified the user's actual desktop with
#: test fixture text ("fail please", "rm -f /tmp/jarvis-test").
#:
#: So every name here is (a) read late at construction in the module that uses
#: it, and (b) replaced process-wide with something that cannot resolve. The
#: sentinel names the fix, because the failure it produces is a caller that
#: forgot to stub, not a broken program.
#:
#: What each one would otherwise do to the machine running the suite:
#:   NOTIFY_BIN   - puts a real toast on the user's desktop
#:   APLAY_BIN    - plays audio out of the user's speakers
#:   HYPRCTL_BIN  - installs window rules and moves the user's workspaces
#:   VENV_PYTHON  - forks a real 331 MB piper worker
FORBIDDEN_NOTIFIER = "luna-tests-must-pass-notify_bin=/bin/true"
FORBIDDEN_APLAY = "luna-tests-must-pass-aplay=/bin/true"
FORBIDDEN_HYPRCTL = "luna-tests-must-pass-hypr=FakeHyprland"
FORBIDDEN_PYTHON = Path("/nonexistent/luna-tests-must-pass-python")

config.NOTIFY_BIN = FORBIDDEN_NOTIFIER
config.APLAY_BIN = FORBIDDEN_APLAY
config.HYPRCTL_BIN = FORBIDDEN_HYPRCTL
config.VENV_PYTHON = FORBIDDEN_PYTHON

#: The same class of bug, one step further out: not a binary but a *file the
#: desktop is reading*. ``config.STATE_FILE`` is what the Luna bar widget
#: watches, so a test that builds a ``Daemon`` -- or a bare ``Presence`` --
#: against the real path would overwrite the running daemon's published state
#: and leave the bar showing `thinking` forever, with nothing on screen to say
#: a test suite did it. Redirected process-wide into a temporary directory,
#: read late in ``Presence.__init__`` so the redirect actually takes.
_STATE_DIR = Path(tempfile.mkdtemp(prefix="luna-tests-state-"))
atexit.register(shutil.rmtree, _STATE_DIR, True)
FORBIDDEN_STATE_FILE = _STATE_DIR / "state"

config.STATE_FILE = FORBIDDEN_STATE_FILE


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
        # Registered first, so it runs last: every ``close`` a case registers
        # below drains its own workers before the tree they write into goes.
        self.addCleanup(self._tmp.cleanup)
        # Redirect the globals before anything can touch the real ones.
        self.ledger = safety.SpawnLedger(self.root / "spawned.json")
        self.audit = audit_mod.AuditLog(self.root / "audit.jsonl")
        self.settings = settings_mod.Settings(self.root / "config.toml")
        # A confirmation nobody answers costs 60 s by default. A test that
        # trips one by accident should fail in a second, not wedge the suite.
        self.settings.set("confirm.prompt.timeout_seconds", 1)
        old_ledger = safety.use_ledger(self.ledger)
        old_audit = audit_mod.use_audit(self.audit)
        old_settings = settings_mod.use_settings(self.settings)
        self.addCleanup(safety.use_ledger, old_ledger)
        self.addCleanup(audit_mod.use_audit, old_audit)
        self.addCleanup(settings_mod.use_settings, old_settings)
        self.addCleanup(self.settings.stop_watching)
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
