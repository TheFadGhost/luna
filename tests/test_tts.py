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
import unittest
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
                                voice="flux-sienna-en", api_key="sk-test")
        self.assertEqual(wav.rate, 24_000)
        self.assertEqual(captured["url"], config.OPENROUTER_SPEECH_URL)
        self.assertEqual(captured["body"], {"model": "deepgram/flux-tts:free",
                                            "input": "Good morning.",
                                            "voice": "flux-sienna-en"})
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

    def job(self, sentences: list[str]) -> speech._Job:
        cfg = dict(self.settings.section("voice"))
        return speech._Job(sentences, " ".join(sentences), cfg=cfg)

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

    def test_the_configured_voice_reaches_the_request(self) -> None:
        seen: list[str] = []

        def synth(text, *, model, voice, api_key, speed=1.0,  # noqa: ANN001
                  timeout=None):
            seen.append(voice)
            return speech.parse_wav(make_wav())

        obj = _Speech(synth=synth, settings=self.settings)
        self.addCleanup(obj.close)
        obj._play(self.job(["One."]))
        self.assertEqual(seen, ["flux-sienna-en"])

        self.settings.set("voice.voice", "flux-donovan-en")
        obj._play(self.job(["Two."]))
        self.assertEqual(seen[-1], "flux-donovan-en")


class VoiceSettingsCase(TempMemoryCase):
    def obj(self) -> speech.Speech:
        s = speech.Speech(settings=self.settings)
        self.addCleanup(s.close)
        return s

    def test_settings_are_read_per_utterance_not_at_construction(self) -> None:
        obj = self.obj()
        self.assertEqual(obj._voice_settings()["voice"], "flux-sienna-en")
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
        self.assertEqual(status["voice"], "flux-sienna-en")
        self.assertEqual(status["piper_voice"], config.VOICE_NAME)
        self.assertEqual(status["fallback"], "piper")

    def test_status_reports_piper_voice_when_piper_is_the_provider(self) -> None:
        self.settings.set("voice.provider", "piper")
        self.assertEqual(self.obj().status()["voice"], config.VOICE_NAME)


if __name__ == "__main__":
    unittest.main()
