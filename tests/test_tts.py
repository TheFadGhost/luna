"""OpenRouter TTS: the WAV parser, the request, and the fall back to piper.

Nothing here touches the network or the sound card. The synthesiser is
injected, the player is faked, and the piper path is stubbed — so every branch
that matters (all good, first sentence fails, a later sentence fails, no
fallback configured) runs in milliseconds and costs nothing.
"""

from __future__ import annotations

import io
import json
import struct
import threading
import time
import unittest
import unittest.mock
import urllib.error

from ._support import TempMemoryCase

from lunad import config, speech


def make_wav(samples: bytes = b"\x01\x02" * 64, rate: int = 24_000,
             channels: int = 1, bits: int = 16, extra_chunk: bool = False) -> bytes:
    """A minimal but honest RIFF file, optionally with a chunk before `data`."""
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, channels, rate,
                      rate * channels * bits // 8, channels * bits // 8, bits)
    body = fmt
    if extra_chunk:
        info = b"LISTINFOthis is not audio"
        body += struct.pack("<4sI", b"LIST", len(info)) + info
        if len(info) % 2:
            body += b"\0"
    body += struct.pack("<4sI", b"data", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


class _Player:
    """Something that quacks like the ``aplay`` Popen, without any aplay."""

    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = 0
        self.pid = 424242

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _Speech(speech.Speech):
    """The real class with only the two things that touch the system replaced."""

    def __init__(self, synth, settings) -> None:  # noqa: ANN001
        super().__init__(synth=synth, settings=settings)
        self.players: list[_Player] = []
        self.piper_calls: list[list[str]] = []

    def _start_player(self, rate, channels=1, fmt="S16_LE"):  # noqa: ANN001
        player = _Player()
        player.args = (rate, channels, fmt)  # type: ignore[attr-defined]
        self.players.append(player)
        return player

    def _finish_player(self, player, job) -> None:  # noqa: ANN001
        pass

    def _play_piper(self, job, sentences) -> None:  # noqa: ANN001
        self.piper_calls.append(list(sentences))


class WavCase(unittest.TestCase):
    def test_a_plain_wav_parses(self) -> None:
        wav = speech.parse_wav(make_wav())
        self.assertEqual(wav.rate, 24_000)
        self.assertEqual(wav.channels, 1)
        self.assertEqual(wav.bits, 16)
        self.assertEqual(len(wav.pcm), 128)
        self.assertEqual(wav.format(), "S16_LE")

    def test_a_chunk_before_data_is_not_played_as_audio(self) -> None:
        wav = speech.parse_wav(make_wav(extra_chunk=True))
        self.assertEqual(len(wav.pcm), 128)
        self.assertNotIn(b"not audio", wav.pcm)

    def test_a_json_error_page_is_not_mistaken_for_audio(self) -> None:
        with self.assertRaises(speech.RemoteSpeechFailed):
            speech.parse_wav(b'{"error":{"message":"upstream 502"}}')

    def test_an_empty_body_is_rejected(self) -> None:
        with self.assertRaises(speech.RemoteSpeechFailed):
            speech.parse_wav(b"")

    def test_a_riff_with_no_data_chunk_is_rejected(self) -> None:
        with self.assertRaises(speech.RemoteSpeechFailed):
            speech.parse_wav(make_wav(samples=b""))

    def test_formats_that_differ_do_not_match(self) -> None:
        a = speech.parse_wav(make_wav(rate=24_000))
        b = speech.parse_wav(make_wav(rate=22_050))
        self.assertFalse(a.matches(b))
        self.assertTrue(a.matches(speech.parse_wav(make_wav(rate=24_000))))


def padded(lead_ms: int, body_ms: int, trail_ms: int,
           rate: int = 24_000) -> speech.Wav:
    """A chunk shaped like the provider's: padding, speech, decay."""
    def ms(n: int, sample: bytes) -> bytes:
        return sample * (rate * n // 1000)
    pcm = (ms(lead_ms, b"\x00\x00") + ms(body_ms, b"\x00\x40")
           + ms(trail_ms, b"\x00\x00"))
    return speech.parse_wav(make_wav(samples=pcm, rate=rate))


class SeamTrimCase(unittest.TestCase):
    """`trim_seam`: the padding between the opening sentence's two halves.

    The provider pads every request, which in the middle of a sentence is
    heard as a hole — measured at 20 to 620 ms across nine renderings. This
    caps it without ever cutting into anything that is not silence.
    """

    def ms(self, wav_pcm: bytes, rate: int = 24_000) -> int:
        return len(wav_pcm) * 1000 // (rate * 2)

    def test_a_long_trailing_pad_is_cut_back_to_the_keep(self) -> None:
        wav = padded(0, 500, 400)
        out = speech.trim_seam(wav, trailing=True)
        self.assertEqual(self.ms(out), 500 + speech.SEAM_KEEP_MS)

    def test_a_long_leading_pad_is_cut_back_to_the_keep(self) -> None:
        wav = padded(400, 500, 0)
        out = speech.trim_seam(wav, trailing=False)
        self.assertEqual(self.ms(out), speech.SEAM_KEEP_MS + 500)

    def test_only_the_edge_asked_for_is_touched(self) -> None:
        wav = padded(400, 500, 400)
        self.assertEqual(self.ms(speech.trim_seam(wav, trailing=True)),
                         400 + 500 + speech.SEAM_KEEP_MS)
        self.assertEqual(self.ms(speech.trim_seam(wav, trailing=False)),
                         speech.SEAM_KEEP_MS + 500 + 400)

    def test_a_pad_shorter_than_the_keep_is_left_alone(self) -> None:
        wav = padded(0, 500, 40)
        self.assertEqual(speech.trim_seam(wav, trailing=True), wav.pcm)

    def test_a_chunk_with_no_silence_comes_back_identical(self) -> None:
        wav = padded(0, 500, 0)
        self.assertEqual(speech.trim_seam(wav, trailing=True), wav.pcm)
        self.assertEqual(speech.trim_seam(wav, trailing=False), wav.pcm)

    def test_no_sample_above_the_floor_is_ever_removed(self) -> None:
        """The one thing that must not happen is clipping a word."""
        wav = padded(300, 200, 300)
        for trailing in (True, False):
            out = speech.trim_seam(wav, trailing=trailing)
            self.assertIn(b"\x00\x40", out)
            self.assertEqual(out.count(b"\x00\x40"),
                             wav.pcm.count(b"\x00\x40"),
                             "every loud sample survives the trim")

    def test_the_trim_is_capped_however_long_the_silence_is(self) -> None:
        wav = padded(0, 100, 2_000)
        out = speech.trim_seam(wav, trailing=True)
        self.assertEqual(self.ms(wav.pcm) - self.ms(out), 400)

    def test_a_quiet_utterance_is_not_mistaken_for_a_silent_one(self) -> None:
        """The floor is a fraction of the chunk's own peak, not an absolute."""
        rate = 24_000
        quiet = b"\x00\x01" * (rate * 500 // 1000)      # 1/64th of `padded`
        pcm = quiet + b"\x00\x00" * (rate * 300 // 1000)
        wav = speech.parse_wav(make_wav(samples=pcm, rate=rate))
        out = speech.trim_seam(wav, trailing=True)
        self.assertEqual(self.ms(out), 500 + speech.SEAM_KEEP_MS)

    def test_a_format_it_cannot_read_is_returned_untouched(self) -> None:
        wav = speech.Wav(b"\x01\x02\x03", 24_000, 1, 24)
        self.assertEqual(speech.trim_seam(wav, trailing=True), wav.pcm)
        self.assertEqual(speech.trim_seam(speech.Wav(b"", 24_000, 1, 16),
                                          trailing=True), b"")


class SynthesiseCase(TempMemoryCase):
    def test_no_key_fails_fast_without_a_request(self) -> None:
        with self.assertRaises(speech.RemoteSpeechFailed) as caught:
            speech.synthesise("hello", model="m", voice="v", api_key="")
        self.assertIn("API key", str(caught.exception))

    def test_the_request_body_is_model_input_voice(self) -> None:
        captured: dict = {}

        class _Response:
            def read(self) -> bytes:
                return make_wav()

            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, *exc: object) -> None:
                pass

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data)
            return _Response()

        old = speech.urllib.request.urlopen
        speech.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
        self.addCleanup(setattr, speech.urllib.request, "urlopen", old)

        wav = speech.synthesise("Good morning.", model="deepgram/flux-tts:free",
                                voice="flux-alexis-en", api_key="sk-test")
        self.assertEqual(wav.rate, 24_000)
        self.assertEqual(captured["url"], config.OPENROUTER_SPEECH_URL)
        self.assertEqual(captured["body"], {"model": "deepgram/flux-tts:free",
                                            "input": "Good morning.",
                                            "voice": "flux-alexis-en"})
        self.assertIn("Bearer sk-test", str(captured["headers"]))

    def test_a_502_becomes_a_remote_failure(self) -> None:
        def boom(request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError(
                config.OPENROUTER_SPEECH_URL, 502, "Bad Gateway", {},
                io.BytesIO(b'{"error":"upstream"}'))

        old = speech.urllib.request.urlopen
        speech.urllib.request.urlopen = boom  # type: ignore[assignment]
        self.addCleanup(setattr, speech.urllib.request, "urlopen", old)
        with self.assertRaises(speech.RemoteSpeechFailed) as caught:
            speech.synthesise("x", model="m", voice="v", api_key="k")
        self.assertIn("502", str(caught.exception))

    def test_a_timeout_becomes_a_remote_failure(self) -> None:
        def slow(request, timeout=None):  # noqa: ANN001
            raise TimeoutError("timed out")

        old = speech.urllib.request.urlopen
        speech.urllib.request.urlopen = slow  # type: ignore[assignment]
        self.addCleanup(setattr, speech.urllib.request, "urlopen", old)
        with self.assertRaises(speech.RemoteSpeechFailed):
            speech.synthesise("x", model="m", voice="v", api_key="k")


class FallbackCase(TempMemoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []
        self.speeds: list[float] = []

    def speech_with(self, outcomes) -> _Speech:  # noqa: ANN001
        """`outcomes` is one entry per sentence: bytes, or an exception."""
        queue = list(outcomes)

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            self.calls.append(text)
            self.speeds.append(speed)
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return speech.parse_wav(outcome)

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        return obj

    def job(self, sentences: list[str],
            lead_pair: bool = False) -> speech._Job:
        cfg = dict(self.settings.section("voice"))
        return speech._Job(sentences, " ".join(sentences), cfg=cfg,
                           lead_pair=lead_pair)

    def test_the_happy_path_never_reaches_piper(self) -> None:
        obj = self.speech_with([make_wav(), make_wav()])
        job = self.job(["One.", "Two."])
        obj._play(job)
        self.assertEqual(obj.piper_calls, [])
        self.assertEqual(self.calls, ["One.", "Two."])
        self.assertEqual(len(obj.players), 1, "one aplay per utterance")
        self.assertEqual(obj.players[0].args, (24_000, 1, "S16_LE"))
        self.assertEqual(job.played_bytes, 256)
        self.assertEqual(obj.counters["remote"], 1)
        self.assertIsNotNone(job.first_audio)

    def test_a_failure_on_sentence_one_hands_the_whole_thing_to_piper(self) -> None:
        obj = self.speech_with([speech.RemoteSpeechFailed("HTTP 502")])
        job = self.job(["One.", "Two.", "Three."])
        obj._play(job)
        self.assertEqual(obj.piper_calls, [["One.", "Two.", "Three."]])
        self.assertIn("502", job.fell_back)
        self.assertEqual(obj.counters["fallbacks"], 1)

    def test_a_failure_midway_hands_only_the_rest_to_piper(self) -> None:
        obj = self.speech_with([make_wav(),
                                speech.RemoteSpeechFailed("HTTP 502")])
        job = self.job(["One.", "Two.", "Three."])
        obj._play(job)
        self.assertEqual(obj.piper_calls, [["Two.", "Three."]])
        self.assertEqual(len(obj.players), 1)

    def test_an_unexpected_exception_also_falls_back(self) -> None:
        obj = self.speech_with([ValueError("something else entirely")])
        job = self.job(["One."])
        obj._play(job)
        self.assertEqual(obj.piper_calls, [["One."]])
        self.assertIn("ValueError", job.fell_back)

    def test_fallback_none_raises_instead_of_silently_using_piper(self) -> None:
        self.settings.set("voice.fallback", "none")
        obj = self.speech_with([speech.RemoteSpeechFailed("HTTP 502")])
        with self.assertRaises(speech.SpeechUnavailable):
            obj._play(self.job(["One."]))
        self.assertEqual(obj.piper_calls, [])

    def test_provider_piper_never_calls_the_network(self) -> None:
        self.settings.set("voice.provider", "piper")
        obj = self.speech_with([])
        job = self.job(["One.", "Two."])
        obj._play(job)
        self.assertEqual(self.calls, [])
        self.assertEqual(obj.piper_calls, [["One.", "Two."]])

    def test_a_format_change_starts_a_second_player(self) -> None:
        obj = self.speech_with([make_wav(rate=24_000), make_wav(rate=22_050)])
        obj._play(self.job(["One.", "Two."]))
        self.assertEqual(len(obj.players), 2)
        self.assertEqual(obj.players[1].args, (22_050, 1, "S16_LE"))

    def test_a_stalled_sentence_stops_billing_the_rest(self) -> None:
        """The consumer giving up must stop the producer, not just fall back.

        Without this, `produce()` keeps requesting (and OpenRouter TTS is the
        one paid service in this project) every sentence piper is about to
        speak instead — audio nobody will ever hear, paid for anyway.
        """
        order: list[str] = []
        release = threading.Event()

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            order.append(text)
            if text == "One.":
                # Simulate "never arrives": still in flight when the consumer
                # gives up on it.
                release.wait(5.0)
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        job = self.job(["One.", "Two.", "Three."])

        # A real 30s wait for `_play_remote`'s own timeout would make this
        # test slow without proving anything more; a fake clock trips the
        # same deadline check almost immediately instead, after two real
        # `ready.wait(0.05)` cycles give the producer thread a fair chance to
        # actually start and call synth("One.").
        base = time.monotonic()
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            if calls["n"] <= 3:
                return base + calls["n"] * 0.001
            return base + 10_000.0

        with unittest.mock.patch.object(speech.time, "monotonic",
                                        fake_monotonic):
            remaining = obj._play_remote(job)

        self.assertEqual(remaining, ["One.", "Two.", "Three."])
        self.assertTrue(job.abandon_remote)
        self.assertFalse(job.cancelled, "abandon_remote must not look like a "
                                        "user barge-in to _play_piper")

        release.set()          # let the stuck synth("One.") call finish
        # Give the producer thread a moment to notice `abandon_remote` and
        # stop, if it was ever going to call synth again — a generous, fixed
        # wait rather than racing it, since the assertion below is exact.
        time.sleep(0.3)

        self.assertEqual(order, ["One."],
                         "the producer must not have requested Two. or "
                         "Three. after the consumer abandoned the utterance")

    def test_the_configured_voice_reaches_the_request(self) -> None:
        seen: list[str] = []

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            seen.append(voice)
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        obj._play(self.job(["One."]))
        self.assertEqual(seen, ["flux-alexis-en"])

        self.settings.set("voice.voice", "flux-donovan-en")
        obj._play(self.job(["Two."]))
        self.assertEqual(seen[-1], "flux-donovan-en")


class PrewarmCase(TempMemoryCase):
    """`prewarm()` overlaps piper's cold load with the model's thinking time.

    Measured on this machine: the piper worker costs 1.47-1.61 s to load and
    212 ms to produce its first audio frame once loaded. Started when the agent
    subprocess is spawned, the load finishes inside the 2.4-3.6 s the model
    spends answering and the user never hears it.

    What matters as much as the saving is the narrowness — the remote provider
    falls back to piper on roughly one ask in forty here, and pre-loading
    331 MB of ONNX for that would be a worse trade than the wait. So these
    mostly assert that it stays out of the way.
    """

    def obj(self) -> speech.Speech:
        s = speech.Speech(settings=self.settings)
        self.addCleanup(s.close)
        return s

    def test_the_remote_provider_does_not_pre_load_piper(self) -> None:
        """piper is only the *fallback* here. 331 MB is too much to spend on
        a path taken about one ask in forty."""
        self.settings.set("voice.provider", "openrouter")
        self.assertFalse(self.obj().prewarm())

    def test_piper_as_the_provider_does_pre_load(self) -> None:
        self.settings.set("voice.provider", "piper")
        obj = self.obj()
        loads: list[int] = []
        obj._ensure_worker = lambda: (loads.append(1), (None, None))[1]  # type: ignore[assignment]
        self.assertTrue(obj.prewarm())
        for _ in range(200):
            if loads:
                break
            time.sleep(0.01)
        self.assertEqual(loads, [1])

    def test_voice_switched_off_pre_loads_nothing(self) -> None:
        self.settings.set("voice.provider", "piper")
        self.settings.set("voice.enabled", False)
        self.assertFalse(self.obj().prewarm())

    def test_a_closed_speech_pre_loads_nothing(self) -> None:
        self.settings.set("voice.provider", "piper")
        obj = speech.Speech(settings=self.settings)
        obj.close()
        self.assertFalse(obj.prewarm())

    def test_a_warm_worker_is_not_loaded_twice(self) -> None:
        self.settings.set("voice.provider", "piper")
        obj = self.obj()
        obj._proc = _AliveProc()                       # type: ignore[assignment]
        self.addCleanup(setattr, obj, "_proc", None)
        self.assertFalse(obj.prewarm())

    def test_a_load_that_fails_is_swallowed_not_raised(self) -> None:
        """A warm-up is an optimisation. It may never turn into a failed ask."""
        self.settings.set("voice.provider", "piper")
        obj = self.obj()

        def boom() -> None:
            raise speech.SpeechUnavailable("no model on this machine")

        obj._ensure_worker = boom                      # type: ignore[assignment]
        self.assertTrue(obj.prewarm())
        time.sleep(0.1)
        self.assertIsNone(obj._proc)


class _AliveProc:
    """The shape `prewarm` checks for: a worker that is already running."""

    def poll(self) -> None:
        return None


class LeadPairCase(TempMemoryCase):
    """The opening sentence's two halves, requested at the same time.

    This is the only place in the pipeline where two requests are ever in
    flight at once, and the two things that must stay true about it are that
    the concurrency does not outlive the opening sentence and that a barge-in
    still throws away everything, including whatever arrives late.
    """

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []

    def job(self, sentences: list[str],
            lead_pair: bool = True) -> speech._Job:
        cfg = dict(self.settings.section("voice"))
        return speech._Job(sentences, " ".join(sentences), cfg=cfg,
                           lead_pair=lead_pair)

    def test_both_halves_are_in_flight_together(self) -> None:
        """A barrier of two: serial requests could never both reach it.

        This is the whole point of the change. If the tail were only requested
        once the head came back, it would arrive about a second after the
        head had finished playing — a gap inside a sentence.
        """
        together = threading.Barrier(2)

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            self.calls.append(text)
            together.wait(10.0)      # BrokenBarrierError if they are serial
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        job = self.job(["The build is green,", "and the tests all pass."])
        obj._play(job)

        self.assertEqual(obj.piper_calls, [], "neither request failed")
        self.assertEqual(sorted(self.calls),
                         ["The build is green,", "and the tests all pass."])
        self.assertEqual(len(obj.players), 1, "still one aplay per utterance")

    def test_the_concurrency_does_not_outlive_the_opening_sentence(self) -> None:
        """Sentence two waits for the tail: one-ahead, exactly as before."""
        tail_in_flight = threading.Event()
        release = threading.Event()

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            self.calls.append(text)
            if text == "and the tests all pass.":
                tail_in_flight.set()
                release.wait(5.0)
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        job = self.job(["The build is green,", "and the tests all pass.",
                        "Nothing else to report."])
        runner = threading.Thread(target=obj._play, args=(job,), daemon=True)
        runner.start()
        self.addCleanup(runner.join, 5.0)
        self.addCleanup(release.set)

        self.assertTrue(tail_in_flight.wait(5.0))
        # A fixed, generous wait rather than a race: the assertion is exact,
        # so a third request would have to appear inside it to be caught, and
        # it cannot appear later than this without the head having finished.
        time.sleep(0.3)
        self.assertNotIn("Nothing else to report.", self.calls,
                         "sentence two must not be requested while the "
                         "opening sentence's tail is still out")

        release.set()
        runner.join(5.0)
        self.assertEqual(self.calls,
                         ["The build is green,", "and the tests all pass.",
                          "Nothing else to report."])

    def test_a_barge_in_plays_nothing_that_arrives_afterwards(self) -> None:
        """Both halves are cancelled, and neither is played once it lands."""
        both_out = threading.Barrier(3)
        release = threading.Event()

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            self.calls.append(text)
            both_out.wait(10.0)
            release.wait(5.0)
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        job = self.job(["The build is green,", "and the tests all pass."])
        remaining: list[list[str]] = []
        runner = threading.Thread(
            target=lambda: remaining.append(obj._play_remote(job)),
            daemon=True)
        runner.start()
        self.addCleanup(runner.join, 5.0)

        both_out.wait(10.0)          # both requests are now genuinely in flight
        job.cancelled = True         # what `cancel()` sets on a barge-in
        release.set()                # and now both of them come back
        runner.join(5.0)

        self.assertEqual(remaining, [[]], "a cancel is not a fallback")
        self.assertEqual(obj.players, [], "nothing may be played after a cancel")
        self.assertEqual(job.played_bytes, 0)
        self.assertEqual(obj.piper_calls, [])

    def test_the_seam_is_trimmed_but_only_across_the_pair(self) -> None:
        """Sentence two keeps its own padding: it follows a full stop."""
        wavs = {"The build is green,": padded(0, 300, 400),
                "and the tests all pass.": padded(400, 300, 0),
                "Nothing else to report.": padded(400, 300, 400)}

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            return wavs[text]

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        job = self.job(list(wavs))
        obj._play(job)

        keep = speech.SEAM_KEEP_MS
        expected = sum(len(w.pcm) for w in wavs.values())
        expected -= 24 * 2 * (400 - keep) * 2      # both facing edges, 24 kHz
        self.assertEqual(job.played_bytes, expected)

    def test_a_failed_head_hands_both_halves_to_piper_intact(self) -> None:
        """The seam is invisible to the fallback: piper speaks two units."""
        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            self.calls.append(text)
            if text == "The build is green,":
                raise speech.RemoteSpeechFailed("HTTP 502")
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        job = self.job(["The build is green,", "and the tests all pass."])
        obj._play(job)
        self.assertEqual(obj.piper_calls,
                         [["The build is green,", "and the tests all pass."]])
        self.assertEqual(obj.players, [], "no half was spoken twice")

    def test_without_the_flag_the_producer_is_the_old_serial_one(self) -> None:
        """A single thread, so a barrier of two can never be reached."""
        together = threading.Barrier(2)
        broke: list[bool] = []

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            self.calls.append(text)
            try:
                together.wait(0.4)
            except threading.BrokenBarrierError:
                broke.append(True)
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        obj._play(self.job(["One.", "Two."], lead_pair=False))
        self.assertTrue(broke, "requests must still be serial without the flag")


class LeadSplitInSayCase(TempMemoryCase):
    """`say()` decides the split, and only for the remote provider."""

    TEXT = ("I checked the logs, and the service came back up about ten "
            "minutes ago. It is fine now.")

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []

    def obj(self) -> _Speech:
        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            self.calls.append(text)
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        return obj

    def test_the_opening_sentence_is_cut_before_the_first_request(self) -> None:
        result = self.obj().say(self.TEXT, wait=True, timeout=10.0)
        self.assertTrue(result["lead_split"])
        self.assertEqual(result["sentences"], 3)
        self.assertEqual(self.calls[0], "I checked the logs,")

    def test_piper_is_left_whole_because_it_is_already_fast(self) -> None:
        """~212 ms pre-warmed: a seam there would be bought for nothing."""
        self.settings.set("voice.provider", "piper")
        obj = self.obj()
        result = obj.say(self.TEXT, wait=True, timeout=10.0)
        self.assertNotIn("lead_split", result)
        self.assertEqual(result["sentences"], 2)
        self.assertEqual(self.calls, [], "the network is not touched")
        self.assertEqual(obj.piper_calls[0][0],
                         "I checked the logs, and the service came back up "
                         "about ten minutes ago.")

    def test_a_reply_with_no_usable_boundary_is_not_split(self) -> None:
        result = self.obj().say("It is already running.", wait=True,
                                timeout=10.0)
        self.assertNotIn("lead_split", result)
        self.assertEqual(result["sentences"], 1)


class VoiceSettingsCase(TempMemoryCase):
    def obj(self) -> speech.Speech:
        s = speech.Speech(settings=self.settings)
        self.addCleanup(s.close)
        return s

    def test_settings_are_read_per_utterance_not_at_construction(self) -> None:
        obj = self.obj()
        self.assertEqual(obj._voice_settings()["voice"], "flux-alexis-en")
        self.settings.set("voice.voice", "flux-donovan-en")
        self.assertEqual(obj._voice_settings()["voice"], "flux-donovan-en")

    def test_voice_disabled_says_nothing_and_does_not_raise(self) -> None:
        self.settings.set("voice.enabled", False)
        result = self.obj().say("Hello there.")
        self.assertEqual(result["sentences"], 0)
        self.assertIn("switched off", result["note"])

    def test_max_spoken_chars_caps_at_a_sentence_boundary(self) -> None:
        self.settings.set("voice.max_spoken_chars", 60)
        long = ("One sentence here. Two sentences here. Three sentences here. "
                "Four sentences here. Five sentences here.")
        spoken = speech.strip_for_speech(long, max_chars=60)
        self.assertLess(len(spoken), len(long))
        self.assertTrue(spoken.endswith("The rest is on screen."))

    def test_status_reports_the_live_provider_and_voice(self) -> None:
        status = self.obj().status()
        self.assertEqual(status["provider"], "openrouter")
        self.assertEqual(status["voice"], "flux-alexis-en")
        self.assertEqual(status["piper_voice"], config.VOICE_NAME)
        self.assertEqual(status["fallback"], "piper")

    def test_status_reports_piper_voice_when_piper_is_the_provider(self) -> None:
        self.settings.set("voice.provider", "piper")
        self.assertEqual(self.obj().status()["voice"], config.VOICE_NAME)


if __name__ == "__main__":
    unittest.main()
