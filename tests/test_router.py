"""The voice router must never, ever fail loudly.

voxtype falls back to delivering the raw transcript on a non-zero exit, a
timeout, or empty output with ``fallback_on_empty``. If the router crashes, the
user's spoken question is typed into whatever window has focus. So the contract
under test is blunt and absolute:

    exit 0, and nothing on stdout. Whatever happens.

These tests run the real script as a real subprocess, because the failure mode
lives in process exit codes and file descriptors, not in Python objects.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROUTER = Path(__file__).resolve().parent.parent / "bin" / "luna-voice-router"


def run_router(payload: bytes, runtime_dir: Path | None = None,
               timeout: float = 30.0) -> subprocess.CompletedProcess:
    # `XDG_DATA_HOME` and `XDG_CONFIG_HOME` are redirected even though these
    # cases only care about the socket. The router keeps a breadcrumb log at
    # `config.ROUTER_LOG`, which is under the data dir, and it writes one on
    # every path including the failures this file exists to exercise. Left
    # inheriting the real environment, a suite run appended ten lines of test
    # noise to the user's own `~/.local/share/luna/voice-router.log` -- the
    # exact class of leak `_support.py`'s outward guard exists to stop, just
    # one the guard cannot see because it happens in a subprocess.
    with tempfile.TemporaryDirectory(prefix="luna-router-") as tmp:
        env = dict(os.environ)
        env["XDG_DATA_HOME"] = str(Path(tmp) / "data")
        env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        if runtime_dir is not None:
            # Points config.SOCKET_PATH somewhere with no daemon listening.
            env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        return subprocess.run(
            [sys.executable, str(ROUTER)], input=payload, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            check=False)


def run_isolated(payload: bytes, home: Path, config_body: str | None
                 ) -> tuple[subprocess.CompletedProcess, str]:
    """Run the router against a config, a log and a socket dir of our own.

    All three XDG variables are redirected, not just the runtime one: the
    router reads ``[listen] enabled`` out of ``$XDG_CONFIG_HOME/jarvis`` and
    writes its breadcrumb under ``$XDG_DATA_HOME``, and a test that read the
    user's own config would pass or fail depending on whether they happened to
    have listening switched on.
    """
    conf, data, runtime = home / "config", home / "data", home / "runtime"
    (conf / "jarvis").mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    if config_body is not None:
        (conf / "jarvis" / "config.toml").write_text(config_body,
                                                     encoding="utf-8")
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(conf)
    env["XDG_DATA_HOME"] = str(data)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    proc = subprocess.run(
        [sys.executable, str(ROUTER)], input=payload, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        check=False)
    log = data / "luna" / "voice-router.log"
    try:
        return proc, log.read_text(encoding="utf-8")
    except OSError:
        return proc, ""


class VoiceTurnMarkerTests(unittest.TestCase):
    """The other half of "is this dictation Luna's?".

    `bin/luna-voice-turn` stamps `$XDG_RUNTIME_DIR/luna/voice-turn` before it
    starts recording, because voxtype 0.7.5 publishes `recording` without ever
    saying which profile is recording -- so the bar widget and the orb lit up
    for plain F9 dictation too. This hook is what takes the marker down again,
    and it is the only place that can: it is the one thing voxtype runs for the
    luna profile, and it runs for *every* transcript that profile produces.

    That "every" is the whole argument for putting the clear here rather than
    in the daemon, so it is what these cases assert: the ordinary path, the
    empty transcript, and the transcript refused by `[listen] enabled` all
    leave the marker gone. A marker left set means F9 never stops lighting her
    up, which is worse than the bug it was added to fix.
    """

    def marker(self, home: Path) -> Path:
        return home / "runtime" / "luna" / "voice-turn"

    def arm(self, home: Path) -> Path:
        path = self.marker(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1789000000000", encoding="utf-8")
        return path

    def test_an_ordinary_transcript_takes_the_marker_down(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luna-router-") as tmp:
            home = Path(tmp)
            marker = self.arm(home)
            proc, log = run_isolated(b"what is the time", home, None)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, b"")
            self.assertFalse(marker.exists())

    def test_an_empty_transcript_takes_it_down_too(self) -> None:
        """The daemon never hears about this turn at all.

        Nothing is dispatched for an empty transcript, so a clear that lived
        in `lunad` would never run and the marker would sit there until it
        went stale. This is the first of the three cases that decided where
        the clear belongs.
        """
        with tempfile.TemporaryDirectory(prefix="luna-router-") as tmp:
            home = Path(tmp)
            marker = self.arm(home)
            proc, log = run_isolated(b"   ", home, None)
            self.assertEqual(proc.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn("empty transcript", log)

    def test_listening_switched_off_takes_it_down_as_well(self) -> None:
        """And the second: `[listen] enabled = false` sends nothing either."""
        with tempfile.TemporaryDirectory(prefix="luna-router-") as tmp:
            home = Path(tmp)
            marker = self.arm(home)
            proc, log = run_isolated(b"she is not listening", home,
                                     "[listen]\nenabled = false\n")
            self.assertEqual(proc.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn("listening is off", log)

    def test_no_marker_at_all_is_not_an_error(self) -> None:
        """The third: a turn that never went through the wrapper.

        `luna ask` from a terminal, or the router run by hand. There is
        nothing to clear and that must be silent -- an exception here would
        become a non-zero exit, and a non-zero exit is the user's speech typed
        into whatever window has focus.
        """
        with tempfile.TemporaryDirectory(prefix="luna-router-") as tmp:
            home = Path(tmp)
            proc, _log = run_isolated(b"hello", home, None)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, b"")
            self.assertFalse(self.marker(home).exists())

    def test_a_marker_that_cannot_be_removed_is_still_exit_zero(self) -> None:
        """A directory where the file should be: `unlink` raises IsADirectory.

        Chosen over a permission trick because it fails the same way for root,
        which is how this suite gets run in CI.
        """
        with tempfile.TemporaryDirectory(prefix="luna-router-") as tmp:
            home = Path(tmp)
            stuck = self.marker(home)
            stuck.mkdir(parents=True)
            proc, _log = run_isolated(b"hello", home, None)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, b"")
            self.assertTrue(stuck.is_dir())


class VoiceTurnWrapperTests(unittest.TestCase):
    """`bin/luna-voice-turn`: the marker is on disk *before* voxtype runs.

    The ordering is the entire mechanism. voxtype's `record` is a client; the
    daemon behind it is what publishes `recording`, and the widget latches
    "this one is Luna's" from the marker at the instant it sees that word. If
    the marker could land second there would be a window in which an F10 turn
    was indistinguishable from an F9 one -- which is the bug.

    An earlier note rejected a wrapper partly because it "still races
    voxtype's own state write". It does not: the race is decided by program
    order in a single script, and the exec that starts voxtype is the last
    thing in it. This case is the proof, and it is a real one -- a `voxtype`
    stub on PATH that reports what it found when it was reached.
    """

    WRAPPER = Path(__file__).resolve().parent.parent / "bin" / "luna-voice-turn"

    def run_wrapper(self, tmp: Path) -> tuple[subprocess.CompletedProcess, Path]:
        binhome, runtime = tmp / "bin", tmp / "runtime"
        binhome.mkdir(parents=True, exist_ok=True)
        runtime.mkdir(parents=True, exist_ok=True)
        report = tmp / "voxtype-saw"
        stub = binhome / "voxtype"
        # Reports the marker as voxtype found it, plus the arguments it was
        # given, and nothing else. Deliberately not a Python stub: the thing
        # under test is a POSIX shell script and the exec has to be real.
        stub.write_text(
            "#!/bin/sh\n"
            f'm="{runtime}/luna/voice-turn"\n'
            f'{{ if [ -f "$m" ]; then printf "present %s\\n" "$(cat "$m")";'
            ' else printf "absent\\n"; fi; printf "args %s\\n" "$*"; } > '
            f'"{report}"\n',
            encoding="utf-8")
        stub.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{binhome}:{env.get('PATH', '')}"
        env["XDG_RUNTIME_DIR"] = str(runtime)
        proc = subprocess.run(["/bin/sh", str(self.WRAPPER)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30, check=False)
        return proc, report

    def test_voxtype_finds_the_marker_already_written(self) -> None:
        with tempfile.TemporaryDirectory(prefix="luna-turn-") as tmp:
            proc, report = self.run_wrapper(Path(tmp))
            self.assertEqual(proc.returncode, 0)
            saw = report.read_text(encoding="utf-8").splitlines()
            self.assertTrue(saw[0].startswith("present "),
                            f"voxtype ran before the marker: {saw}")
            stamp = saw[0].split(" ", 1)[1]
            # Epoch milliseconds, ASCII digits, no trailing newline -- the same
            # one-bare-token shape as the `state` file beside it.
            self.assertTrue(stamp.isdigit(), stamp)
            self.assertGreaterEqual(len(stamp), 13)
            self.assertEqual(saw[1], "args record toggle --profile luna")

    def test_the_stamp_is_the_current_time_in_milliseconds(self) -> None:
        """Because the reader's staleness rule is the only backstop.

        A marker the wrapper wrote and nothing ever cleared -- voxtype killed
        mid-recording, a cancelled turn -- must stop being believed on its
        own. That only works if what is written is genuinely *now*.
        """
        with tempfile.TemporaryDirectory(prefix="luna-turn-") as tmp:
            before = time.time() * 1000.0
            proc, report = self.run_wrapper(Path(tmp))
            after = time.time() * 1000.0
            self.assertEqual(proc.returncode, 0)
            stamp = int(report.read_text(encoding="utf-8").splitlines()[0]
                        .split(" ", 1)[1])
            self.assertGreaterEqual(stamp, int(before) - 1000)
            self.assertLessEqual(stamp, int(after) + 1000)

    def test_recording_still_starts_when_the_marker_cannot_be_written(self) -> None:
        """One-directional failure: a cosmetic file may not cost the mic.

        The marker's directory is made unusable, so the write cannot happen.
        voxtype must still be reached -- a broken tmpfs that took the
        microphone with it would be a far worse fault than an orb that does
        not light up.
        """
        with tempfile.TemporaryDirectory(prefix="luna-turn-") as tmp:
            root = Path(tmp)
            # A *file* named `luna` where the directory belongs: `mkdir -p`
            # fails, and so would every write under it.
            (root / "runtime").mkdir()
            (root / "runtime" / "luna").write_text("not a directory")
            proc, report = self.run_wrapper(root)
            self.assertEqual(proc.returncode, 0)
            saw = report.read_text(encoding="utf-8").splitlines()
            self.assertEqual(saw[0], "absent")
            self.assertEqual(saw[1], "args record toggle --profile luna")

    def test_nothing_is_left_behind_on_stdout_or_stderr(self) -> None:
        """It sits in front of a keybind, not in a terminal."""
        with tempfile.TemporaryDirectory(prefix="luna-turn-") as tmp:
            proc, _report = self.run_wrapper(Path(tmp))
            self.assertEqual(proc.stdout, b"")
            self.assertEqual(proc.stderr, b"")


class ListeningSwitchTests(unittest.TestCase):
    """``[listen] enabled`` — the one key in that block that is not voxtype's.

    voxtype owns the microphone and knows nothing about Luna, so "listening
    off" cannot mean "do not record": the keybind still records. It means the
    transcript is not handed to her, and voxtype's own ``fallback_on_empty``
    then delivers it through the profile's ``output_mode = "clipboard"``. The
    router is the only place that boundary exists, so it is the only place the
    key could be honoured.

    The evidence is the breadcrumb log, because the observable difference is
    that a socket connection is *not* attempted, and "did not happen" has no
    other trace.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-listen-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_off_means_the_transcript_is_not_sent(self):
        proc, log = run_isolated(b"what is the battery at\n", self.home,
                                 "[listen]\nenabled = false\n")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("listening is off", log)

    def test_on_means_it_is(self):
        proc, log = run_isolated(b"what is the battery at\n", self.home,
                                 "[listen]\nenabled = true\n")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertNotIn("listening is off", log)

    def test_a_config_that_does_not_parse_fails_open(self):
        # Every other failure path here ends with the transcript on the
        # clipboard, which is recoverable. This one decides whether she is
        # spoken to at all, and an unreadable file is not a reason to stop
        # answering.
        proc, log = run_isolated(b"hello", self.home, "[listen\nenabled = ")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertNotIn("listening is off", log)

    def test_no_config_file_at_all_fails_open(self):
        proc, log = run_isolated(b"hello", self.home, None)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertNotIn("listening is off", log)

    def test_switching_it_off_still_says_nothing_on_stderr(self):
        # voxtype captures our stderr into its own journal. Reading the config
        # brings lunad's settings logger into the process, and a warning from
        # it would land there on every single utterance.
        proc, _log = run_isolated(b"hello", self.home,
                                  "[listen]\nenabled = false\n")
        self.assertEqual(proc.stderr, b"")


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-router-")
        self.addCleanup(self._tmp.cleanup)
        # Every test runs against a runtime dir with no lunad in it, so no test
        # can accidentally spend money on the real daemon.
        self.dead = Path(self._tmp.name)

    def assert_silent_success(self, proc: subprocess.CompletedProcess,
                              what: str) -> None:
        self.assertEqual(proc.returncode, 0, f"{what}: exited {proc.returncode}")
        self.assertEqual(proc.stdout, b"", f"{what}: wrote {proc.stdout!r}")

    def test_the_script_is_executable(self):
        self.assertTrue(os.access(ROUTER, os.X_OK), f"{ROUTER} is not +x")

    def test_a_dead_socket_still_exits_zero_and_silent(self):
        self.assert_silent_success(
            run_router(b"what is the battery at\n", self.dead), "dead socket")

    def test_empty_input(self):
        self.assert_silent_success(run_router(b"", self.dead), "empty")

    def test_whitespace_only_input(self):
        self.assert_silent_success(run_router(b"   \n\t \n", self.dead),
                                   "whitespace")

    def test_binary_garbage(self):
        payload = bytes(range(256)) * 40
        self.assert_silent_success(run_router(payload, self.dead), "garbage")

    def test_embedded_nulls_are_not_passed_on(self):
        self.assert_silent_success(
            run_router(b"hello\x00\x01world", self.dead), "nulls")

    def test_invalid_utf8(self):
        self.assert_silent_success(run_router(b"\xff\xfe\xfd bad", self.dead),
                                   "invalid utf-8")

    def test_a_huge_transcript(self):
        self.assert_silent_success(
            run_router(b"word " * 500_000, self.dead), "huge")

    def test_text_that_looks_like_a_protocol_injection(self):
        # A transcript is data. It must not be able to become a second request.
        payload = b'hi"}\n{"op":"shutdown"}\n'
        self.assert_silent_success(run_router(payload, self.dead), "injection")

    def test_a_socket_path_that_is_a_directory(self):
        bad = Path(self._tmp.name) / "as-dir"
        (bad / "luna" / "luna.sock").mkdir(parents=True)
        self.assert_silent_success(run_router(b"hello", bad), "socket is a dir")

    def test_an_unwritable_state_dir_does_not_break_the_exit_code(self):
        # The router logs to XDG_DATA_HOME; if that is unwritable it must still
        # exit 0 rather than dying inside its own logger.
        env_dir = Path(self._tmp.name) / "ro"
        env_dir.mkdir()
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = str(self.dead)
        env["XDG_DATA_HOME"] = "/proc/nonexistent/definitely-not-writable"
        proc = subprocess.run([sys.executable, str(ROUTER)], input=b"hello",
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30, check=False)
        self.assert_silent_success(proc, "unwritable state dir")

    def test_it_returns_promptly(self):
        # post_process_timeout_ms is 2000 in the profile. A router that takes
        # longer than that is a router voxtype has already given up on.
        import time
        started = time.monotonic()
        run_router(b"a normal length spoken question", self.dead)
        self.assertLess(time.monotonic() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
