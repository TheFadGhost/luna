"""Presence: the one word the desktop reads.

The bar widget is not in this repository and cannot be tested from here, so
what is asserted is the *contract* it depends on — the exact file body, that a
write is atomic, that an unchanged state costs nothing, and above all that
nothing in here can raise into the middle of an answer.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lunad import presence

from ._support import FORBIDDEN_STATE_FILE


class ContractCase(unittest.TestCase):
    """What the widget is entitled to assume."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="luna-presence-"))
        self.path = self.root / "state"
        self.p = presence.Presence(self.path)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_file_is_one_bare_word(self) -> None:
        self.p.set(presence.THINKING)
        # No newline, no JSON, no whitespace: a watcher comparing the whole
        # body must not have to strip anything.
        self.assertEqual(self.path.read_bytes(), b"thinking")

    def test_every_published_state_round_trips(self) -> None:
        for state in presence.STATES:
            with self.subTest(state=state):
                self.p.set(state)
                self.assertEqual(self.path.read_text(), state)

    def test_an_unknown_state_degrades_to_idle(self) -> None:
        # The widget treats anything it does not recognise as idle; the daemon
        # must not be the one that puts an unrecognised word there.
        self.p.set("dreaming")
        self.assertEqual(self.path.read_text(), presence.IDLE)

    def test_clear_removes_the_file(self) -> None:
        self.p.set(presence.SPEAKING)
        self.p.clear()
        self.assertFalse(self.path.exists())
        # Absence is the daemon-not-running signal, so it has to be absence
        # and not an empty file.
        self.assertEqual(list(self.root.iterdir()), [])

    def test_clear_is_final(self) -> None:
        # The regression this exists for: `Daemon.close()` clears, then
        # cancels speech, and cancelling speech fires the callback that
        # publishes. Without the latch the file came straight back and the
        # bar showed a daemon that had already stopped.
        self.p.set(presence.SPEAKING)
        self.p.clear()
        self.p.set(presence.IDLE)
        self.p.set(presence.THINKING)
        self.assertFalse(self.path.exists())

    def test_clearing_twice_is_not_an_error(self) -> None:
        self.p.clear()
        self.p.clear()
        self.assertFalse(self.path.exists())

    def test_the_write_is_atomic(self) -> None:
        # The temp file must be a sibling, or `os.replace` is a cross-device
        # copy and stops being atomic. Verified by where it lands, because
        # that is the property that actually breaks.
        self.p.set(presence.SPEAKING)
        self.assertEqual(self.p._tmp.parent, self.path.parent)
        self.assertFalse(self.p._tmp.exists())


class CheapCase(unittest.TestCase):
    """It runs inside a turn, so it must cost nothing when nothing changed."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="luna-presence-"))
        self.path = self.root / "state"
        self.p = presence.Presence(self.path)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_repeating_a_state_does_not_touch_the_file(self) -> None:
        self.p.set(presence.THINKING)
        first = self.path.stat().st_mtime_ns
        for _ in range(50):
            self.p.set(presence.THINKING)
        self.assertEqual(self.path.stat().st_mtime_ns, first)

    def test_a_change_does_touch_the_file(self) -> None:
        self.p.set(presence.THINKING)
        self.p.set(presence.SPEAKING)
        self.assertEqual(self.path.read_text(), presence.SPEAKING)


class NeverRaisesCase(unittest.TestCase):
    """A broken desktop must not fail an answer that was otherwise fine."""

    def test_an_unwritable_directory_is_swallowed(self) -> None:
        # /proc is real, present on every machine this runs on, and not
        # writable by anyone -- a truer failure than a mocked one.
        p = presence.Presence(Path("/proc/luna-tests-must-not-exist/state"))
        p.set(presence.SPEAKING)
        p.set(presence.IDLE)
        p.clear()

    def test_a_failed_write_is_retried_rather_than_deduplicated(self) -> None:
        # The dedupe is an optimisation; it must never latch a state that
        # never reached the disk, or the first successful write after a
        # transient failure would be skipped and the bar would stay wrong.
        root = Path(tempfile.mkdtemp(prefix="luna-presence-"))
        p = presence.Presence(root / "gone" / "state")
        (root).chmod(0o500)
        try:
            p.set(presence.SPEAKING)
            self.assertIsNone(p._current)
        finally:
            root.chmod(0o700)
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class SecondDaemonCase(unittest.TestCase):
    """A second lunad must not disturb the one that is already running.

    Since presence became a file, starting lunad twice was capable of real
    harm: the newcomer built a whole Daemon -- publishing `idle` over whatever
    the live one had published, and appending a `daemon.started` line for a
    daemon that never started -- before discovering the socket was taken.
    `serve()` now checks first.
    """

    def test_the_running_daemons_state_survives(self) -> None:
        import shutil
        import socket as socket_mod

        from lunad import config, server

        root = Path(tempfile.mkdtemp(prefix="luna-second-"))
        self.addCleanup(shutil.rmtree, root, True)

        # A live listener standing in for the daemon that got there first.
        sock_path = root / "luna.sock"
        live = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        live.bind(str(sock_path))
        live.listen(1)
        self.addCleanup(live.close)

        state = root / "state"
        state.write_text(presence.SPEAKING)

        old_sock, old_state = config.SOCKET_PATH, config.STATE_FILE
        config.SOCKET_PATH, config.STATE_FILE = sock_path, state
        try:
            with self.assertRaises(server.AlreadyRunning):
                server.serve()
        finally:
            config.SOCKET_PATH, config.STATE_FILE = old_sock, old_state

        self.assertTrue(sock_path.exists(), "the live socket was removed")
        self.assertEqual(state.read_text(), presence.SPEAKING)


class DefaultPathCase(unittest.TestCase):
    """Built with nothing specified, it holds the redirected test path."""

    def test_the_default_is_the_disarmed_one(self) -> None:
        self.assertEqual(presence.Presence().path, FORBIDDEN_STATE_FILE)


if __name__ == "__main__":
    unittest.main()
