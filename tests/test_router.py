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
import unittest
from pathlib import Path

ROUTER = Path(__file__).resolve().parent.parent / "bin" / "luna-voice-router"


def run_router(payload: bytes, runtime_dir: Path | None = None,
               timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
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
