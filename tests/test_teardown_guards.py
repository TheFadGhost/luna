"""Neither settings nor dispatch may rebuild a tree that has been removed.

CI fails a run that leaves a `/tmp/luna-test-*` behind, and it caught one that
reproduces on the 3.13 runner and almost nowhere else: a thread outliving the
case that started it, writing into a temporary tree teardown had already
deleted, and rebuilding the tree on the way in.

The two paths that can rebuild it are `Settings.write`, which creates the
config directory, and `Dispatcher.dispatch`, which creates a job directory with
`parents=True`. Both now refuse. These cases pin that refusal down, because it
is invisible in ordinary use: in production neither directory ever vanishes, so
nothing else would ever exercise it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from ._support import TempMemoryCase  # noqa: F401  (installs the outward guard)
from .test_dispatch import DispatcherCase

from lunad import audit as audit_mod
from lunad import config
from lunad import dispatch as dispatch_mod
from lunad import safety
from lunad.settings import Settings, SettingsError


class SettingsRefusesToRebuild(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-teardown-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_first_run_still_creates_its_directory(self):
        # The directory not existing at construction is the normal first-run
        # case, and creating it is exactly what `write` is for.
        path = self.tmp / "never-existed" / "config.toml"
        cfg = Settings(path)
        self.addCleanup(cfg.stop_watching)
        self.assertTrue(path.exists())

    def test_a_directory_that_vanishes_is_not_rebuilt(self):
        path = self.tmp / "here" / "config.toml"
        path.parent.mkdir(parents=True)
        cfg = Settings(path)
        self.addCleanup(cfg.stop_watching)
        shutil.rmtree(path.parent)

        with self.assertRaises(SettingsError) as caught:
            cfg.write(why="a thread that outlived its case")
        self.assertIn("refusing to recreate", str(caught.exception))
        self.assertFalse(path.parent.exists(),
                         "the tree was rebuilt despite the refusal")


class DispatchRefusesToRebuild(DispatcherCase):
    def test_a_state_dir_that_vanishes_is_not_rebuilt(self):
        d = self.dispatcher()
        # Teardown, as a thread that outlived its case would find it: the whole
        # temporary tree is gone, jobs directory and all.
        shutil.rmtree(self.root)

        with self.assertRaises(dispatch_mod.DispatchUnavailable) as caught:
            d.dispatch("a task nobody asked for", to="worker")
        self.assertIn("refusing to recreate", str(caught.exception))
        self.assertFalse(self.root.exists(),
                         "the tree was rebuilt despite the refusal")
        # Put it back, so the case's own teardown has something to remove and
        # the failure cannot masquerade as the leak this file is about.
        self.root.mkdir(parents=True, exist_ok=True)


class SinksRefuseToRebuild(unittest.TestCase):
    """The append-only sinks that write straight into the state directory.

    Both already wrap their write in `except OSError` and log, so a refusal
    costs the entry and nothing else — which is the right trade for a daemon
    that must not fall over because a log line could not land.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="luna-teardown-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_audit_log_does_not_rebuild_a_deleted_state_dir(self):
        state = self.tmp / "state"
        state.mkdir()
        log = audit_mod.AuditLog(state / "audit.jsonl")
        log.append("a.first.entry", ok=True)
        self.assertTrue((state / "audit.jsonl").exists())

        shutil.rmtree(state)
        log.append("an.entry.from.a.thread.that.outlived.its.case", ok=True)
        self.assertFalse(state.exists(), "the audit log rebuilt the tree")

    def test_the_spawn_ledger_does_not_rebuild_a_deleted_state_dir(self):
        state = self.tmp / "state"
        state.mkdir()
        ledger = safety.SpawnLedger(state / "spawned.json")
        # Our own pid, because the ledger reads a start time out of /proc to
        # pin the record to this exact process, and a pid it cannot read is
        # one it refuses to add.
        ledger.record(os.getpid(), ["/bin/true"])
        self.assertTrue((state / "spawned.json").exists())

        shutil.rmtree(state)
        ledger.record(os.getppid(), ["/bin/true"])
        self.assertFalse(state.exists(), "the spawn ledger rebuilt the tree")

    def test_a_sink_whose_directory_never_existed_still_creates_it(self):
        # The first-run case, which must keep working: nothing to resurrect,
        # so `ensure_parent` creates the directory as it always did.
        path = self.tmp / "brand-new" / "audit.jsonl"
        audit_mod.AuditLog(path).append("first.run", ok=True)
        self.assertTrue(path.exists())

    def test_ensure_parent_names_what_vanished(self):
        gone = self.tmp / "gone" / "file"
        gone.parent.mkdir()
        shutil.rmtree(gone.parent)
        with self.assertRaises(config.VanishedDirectory) as caught:
            config.ensure_parent(gone, existed=True)
        self.assertIn("refusing to recreate", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
