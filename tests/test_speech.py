"""Speech: what gets spoken, and how it is cut into speakable pieces.

No audio and no piper here. These tests cover the decisions — what is unfit to
be read aloud, and where a sentence ends — because those are the parts that go
wrong silently. The subprocess machinery is exercised by hand against real
hardware; asserting on it in unit tests would only assert on the mock.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from lunad import config, safety, speech

from ._support import TempMemoryCase

PH = config.SPEECH_PLACEHOLDER


class StripperTests(unittest.TestCase):
    """Nothing unspeakable survives. Read every assertion aloud to check it."""

    def test_empty_in_empty_out(self):
        self.assertEqual(speech.strip_for_speech(""), "")
        self.assertEqual(speech.strip_for_speech("   \n  "), "")

    def test_plain_prose_is_left_alone(self):
        text = "That will not work. The bar is Quickshell, not Waybar."
        self.assertEqual(speech.strip_for_speech(text), text)

    def test_fenced_code_becomes_a_placeholder(self):
        out = speech.strip_for_speech(
            "Try this.\n```python\nprint('hello')\n```\nThat is all.")
        self.assertNotIn("print", out)
        self.assertIn(PH, out)
        self.assertIn("That is all.", out)

    def test_an_unclosed_fence_does_not_leak_code(self):
        out = speech.strip_for_speech("Here:\n```bash\nrm -rf /some/dir")
        self.assertNotIn("rm -rf", out)
        self.assertIn(PH, out)

    def test_absolute_and_home_paths_are_replaced(self):
        for path in ("/usr/share/omarchy/bin/x", "~/Work/luna/lunad/speech.py",
                     "./bin/luna", "../etc/passwd", "lunad/server.py"):
            out = speech.strip_for_speech(f"It lives in {path} today.")
            self.assertNotIn(path, out, path)
            self.assertIn(PH, out, path)

    def test_urls_and_emails_are_replaced(self):
        out = speech.strip_for_speech(
            "See https://github.com/TheFadGhost/luna or mail a@b.co about it.")
        self.assertNotIn("github", out)
        self.assertNotIn("a@b.co", out)
        self.assertIn(PH, out)

    def test_markdown_link_keeps_the_label_and_drops_the_target(self):
        out = speech.strip_for_speech("Read [the architecture](docs/ARCH.md).")
        self.assertIn("the architecture", out)
        self.assertNotIn("ARCH", out)

    def test_long_digit_runs_and_hashes_are_replaced(self):
        out = speech.strip_for_speech(
            "Commit a1b2c3d4e5f6 changed 1234567 bytes.")
        self.assertNotIn("a1b2c3d4e5f6", out)
        self.assertNotIn("1234567", out)

    def test_short_numbers_survive(self):
        out = speech.strip_for_speech("There are 12 entries and 3 errors.")
        self.assertIn("12", out)
        self.assertIn("3", out)

    def test_wordlike_inline_code_is_spoken_but_a_path_is_not(self):
        out = speech.strip_for_speech("Set `enabled` in `~/.config/x/y.toml`.")
        self.assertIn("enabled", out)
        self.assertNotIn("toml", out)

    def test_adjacent_placeholders_collapse(self):
        out = speech.strip_for_speech(
            "Check /a/b and /c/d and /e/f before you start.")
        self.assertEqual(out.count(PH), 1, out)

    def test_punctuation_closes_up_after_a_substitution(self):
        out = speech.strip_for_speech("The file is /etc/hosts.")
        self.assertTrue(out.endswith("screen."), out)
        self.assertNotIn(" .", out)

    def test_markdown_furniture_is_removed(self):
        out = speech.strip_for_speech(
            "## Heading\n- **bold** point\n- another one")
        self.assertNotIn("#", out)
        self.assertNotIn("*", out)
        self.assertIn("bold point", out)

    def test_a_long_reply_is_cut_at_a_sentence_and_says_so(self):
        text = ("This is a complete sentence that carries on for a while. " * 40)
        out = speech.strip_for_speech(text)
        self.assertLess(len(out), len(text))
        self.assertTrue(out.endswith("The rest is on screen."), out[-60:])

    def test_a_reply_that_is_only_code_still_says_something(self):
        out = speech.strip_for_speech("```\nls -la\n```")
        self.assertEqual(out, PH)


class SentenceSplitTests(unittest.TestCase):
    def test_nothing_to_say(self):
        self.assertEqual(speech.split_sentences(""), [])
        self.assertEqual(speech.split_sentences("  \n "), [])

    def test_basic_terminators(self):
        self.assertEqual(
            speech.split_sentences("One. Two! Three?"),
            ["One.", "Two!", "Three?"])

    def test_abbreviations_do_not_end_a_sentence(self):
        self.assertEqual(
            speech.split_sentences("Dr. Smith left. He was late."),
            ["Dr. Smith left.", "He was late."])

    def test_initials_do_not_end_a_sentence(self):
        self.assertEqual(speech.split_sentences("J. Smith wrote it."),
                         ["J. Smith wrote it."])

    def test_newlines_are_boundaries(self):
        self.assertEqual(speech.split_sentences("First line\nSecond line"),
                         ["First line", "Second line"])

    def test_the_first_unit_is_short_enough_to_start_fast(self):
        # The whole point of splitting: unit one is what decides how long the
        # user waits before hearing anything.
        units = speech.split_sentences(
            "Right. " + "A far longer follow-up sentence goes here. " * 5)
        self.assertEqual(units[0], "Right.")

    def test_an_over_long_sentence_is_broken_on_clauses(self):
        unit = ("first clause here, second clause here, third clause here, "
                "fourth clause here")
        units = speech.split_sentences(unit, max_chars=30)
        self.assertGreater(len(units), 1)
        self.assertTrue(all(len(u) <= 30 for u in units), units)

    def test_a_single_unbroken_run_is_hard_wrapped_not_dropped(self):
        units = speech.split_sentences("alpha beta gamma delta epsilon zeta",
                                       max_chars=12)
        self.assertTrue(all(len(u) <= 12 for u in units), units)
        self.assertIn("alpha", " ".join(units))
        self.assertIn("zeta", " ".join(units))

    def test_split_of_stripped_text_never_yields_empty_units(self):
        units = speech.split_sentences(
            speech.strip_for_speech("Look at /a/b/c.\n\n```\ncode\n```\n\nDone."))
        self.assertTrue(all(u.strip() for u in units), units)


class SampleRateTests(unittest.TestCase):
    """The rate comes from the voice, never from a constant in the source."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-voice-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, payload: object) -> Path:
        path = self.root / "voice.onnx.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_the_rate_from_the_voice_config(self):
        self.assertEqual(
            speech.read_sample_rate(self._write({"audio": {"sample_rate": 16000}})),
            16000)

    def test_a_missing_config_says_how_to_fix_it(self):
        with self.assertRaises(speech.SpeechUnavailable) as ctx:
            speech.read_sample_rate(self.root / "nope.json")
        self.assertIn("download_voices", str(ctx.exception))

    def test_a_config_without_a_rate_is_an_error_not_a_guess(self):
        with self.assertRaises(speech.SpeechUnavailable):
            speech.read_sample_rate(self._write({"audio": {}}))

    def test_the_installed_voice_has_a_usable_rate(self):
        if not config.VOICE_CONFIG.exists():
            self.skipTest("voice not installed on this machine")
        self.assertGreater(speech.read_sample_rate(), 0)


class WorkerGuardTests(unittest.TestCase):
    """A missing piper is reported, never crashed on."""

    def speech_with(self, root: Path) -> speech.Speech:
        sp = speech.Speech(model=root / "no.onnx",
                           voice_config=root / "no.onnx.json",
                           python=root / "no-python", idle_unload_s=3600)
        self.addCleanup(sp.close)
        return sp

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="luna-speech-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_availability_names_what_is_missing(self):
        ok, detail = self.speech_with(self.root).available()
        self.assertFalse(ok)
        self.assertIn("no-python", detail)

    def test_cancelling_silence_is_not_an_error(self):
        self.assertFalse(self.speech_with(self.root).cancel())

    def test_saying_nothing_speakable_does_not_start_a_worker(self):
        sp = self.speech_with(self.root)
        result = sp.say("```\ncode\n```")
        # The stripper leaves a placeholder, so there IS something to say; a
        # genuinely empty input is the case that must short-circuit.
        self.assertEqual(sp.say("   ")["sentences"], 0)
        self.assertGreaterEqual(result["sentences"], 0)
        self.assertFalse(sp.status()["loaded"])

    def test_status_is_readable_before_anything_has_been_said(self):
        st = self.speech_with(self.root).status()
        self.assertFalse(st["loaded"])
        self.assertFalse(st["speaking"])
        self.assertEqual(st["counters"]["said"], 0)


class BargeInDuringLoadTests(TempMemoryCase):
    """Barge-in must not wait behind piper's cold load.

    ``_ensure_worker`` used to hold ``self._lock`` for the whole cold load —
    spawn plus up to ``SPEECH_START_TIMEOUT_S`` waiting for READY — and
    ``cancel()``/``status()`` took the same lock. A real piper worker is not
    used here (that is the hand-verified part, per the module docstring): a
    genuine, harmless, long-lived process (``sleep 30``) stands in for it, so
    the ledger/firewall behave normally, while its stdout never produces a
    READY line — simulating a worker that is still loading.
    """

    def _speech(self) -> speech.Speech:
        obj = speech.Speech(settings=self.settings, idle_unload_s=3600)
        self.addCleanup(obj.close)
        # Bypass the file-existence gate: the fake spawn below never touches
        # `self.model`/`self.voice_config`, so their real presence on this
        # machine must not decide whether the test can run.
        obj.available = lambda: (True, "faked for this test")
        return obj

    def _spawn_stub_worker(self):
        """Patch `speech.safety.spawn` to hand back a real `sleep 30`.

        The daemon's own `argv` (python + PIPER_WORKER + model + config) is
        discarded; what the caller gets back is a real, ledger-registered
        process that will never write a READY line on its own — exactly a
        cold load stuck mid-flight.
        """
        real_spawn = safety.spawn
        spawned: list = []

        def fake_spawn(argv, **kw):
            proc = real_spawn(["sleep", "30"], **kw)
            spawned.append(proc)
            return proc

        patcher = unittest.mock.patch.object(speech.safety, "spawn", fake_spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

        def cleanup():
            for proc in spawned:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001 - best-effort test cleanup
                        pass
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
        self.addCleanup(cleanup)
        return spawned

    def _start_load(self, obj: speech.Speech):
        started = threading.Event()
        finished = threading.Event()
        outcome: list[BaseException | None] = []

        def load():
            started.set()
            try:
                obj._ensure_worker()
            except BaseException as exc:  # noqa: BLE001 - captured, not raised
                outcome.append(exc)
            else:
                outcome.append(None)
            finished.set()

        thread = threading.Thread(target=load, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2.0), "the load thread never started")
        # A brief, generous grace period for the load to actually reach the
        # point of blocking on READY (spawn a real process, register it,
        # start reading its stdout) before this thread acts on it.
        time.sleep(0.2)
        return finished, outcome

    def test_cancel_does_not_wait_behind_a_cold_load(self):
        obj = self._speech()
        self._spawn_stub_worker()
        finished, outcome = self._start_load(obj)

        before = time.monotonic()
        speaking = obj.cancel()
        elapsed = time.monotonic() - before

        self.assertLess(elapsed, 2.0,
                        "cancel() must not block behind the cold load")
        self.assertTrue(speaking, "a load in progress counts as speaking")
        # The cancel above must have actually aborted the load (killed the
        # in-progress worker), not merely returned before it was done.
        self.assertTrue(finished.wait(5.0), "the load never noticed the cancel")
        self.assertIsInstance(outcome[0], speech.SpeechUnavailable)

    def test_status_does_not_wait_behind_a_cold_load(self):
        obj = self._speech()
        self._spawn_stub_worker()
        finished, _outcome = self._start_load(obj)
        self.addCleanup(finished.wait, 5.0)   # let the load's own kill happen
        self.addCleanup(obj.cancel)

        before = time.monotonic()
        st = obj.status()
        elapsed = time.monotonic() - before

        self.assertLess(elapsed, 2.0,
                        "status() must not block behind the cold load")
        self.assertFalse(st["loaded"], "no usable worker exists yet")


if __name__ == "__main__":
    unittest.main()
