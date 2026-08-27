"""Text-to-speech. ARCHITECTURE.md section 5, "Out".

Two halves, deliberately separable:

* pure text preparation — :func:`strip_for_speech` and :func:`split_sentences`.
  No subprocesses, no piper, no audio. These are the parts that decide what
  Luna is *allowed* to say aloud, and they are unit-tested on their own.
* :class:`Speech` — the worker: a lazy piper process, sentence streaming into a
  single ``aplay``, barge-in, and a five-minute idle unload.

Design notes worth keeping:

**Nothing that cannot be spoken is spoken.** A file path read aloud is noise,
a URL read aloud is worse, and a code block is unlistenable. They are replaced
with a short placeholder and the detail stays on screen. This is a persona
requirement, not a nicety.

**One ``aplay`` per utterance, not per sentence.** Sentence-per-process gives
an audible gap at every full stop. One playback process fed sentence by
sentence streams continuously, and back-pressure from its pipe throttles
synthesis for free.

**Only our own PIDs are ever signalled.** Cancellation kills the ``aplay`` this
module started and asks the worker to stop; it never goes looking for piper or
audio processes by name. That is the session firewall in section 7.

**Two providers, one pipeline.** Since the Jarvis pass there is a second voice:
OpenRouter's ``deepgram/flux-tts:free``, requested one sentence at a time so
that speech starts on sentence one exactly as it does with piper. It answers
with a RIFF/WAV body, whose header is parsed off so the samples can be fed to
the *same* single ``aplay`` — one player per utterance, no gap at the full
stop. piper stays installed and stays the fallback: providers 502 intermittently
and a failed request must never be the reason she goes quiet. Which provider is
in use, which voice, and whether there is a fallback are all settings, read at
the start of every utterance so a change takes effect on the next thing she
says rather than on the next restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from . import config, safety, settings as settings_mod

log = logging.getLogger("lunad.speech")


# =========================================================================
# What must never be spoken
# =========================================================================

# Order matters: fenced code first (it may contain paths and URLs), then URLs
# (they contain slashes that look like paths), then paths, then bare numbers.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_FENCE_UNCLOSED_RE = re.compile(r"```.*\Z", re.DOTALL)
_INDENTED_CODE_RE = re.compile(r"(?m)^(?: {4,}|\t)\S.*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_URL_RE = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# A path is a slashed run with no spaces: /usr/share/x, ~/Work/luna, ./bin/foo,
# lunad/speech.py. A lone "and/or" is not, hence the requirement for a root
# marker or a file extension.
_PATH_RE = re.compile(
    r"(?<![\w/])(?:~|\.{1,2})?/[\w.@+-]+(?:/[\w.@+-]+)*/?(?<![.,;:!?])"
    r"|(?<![\w/])[\w.-]+/[\w./@+-]+\.\w{1,6}\b"
)
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")
_DIGITS_RE = re.compile(r"\b\d[\d,._]{5,}\b")
_MD_MARKS_RE = re.compile(r"(?m)^\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?)")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|~~)")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")

# A "wordlike" inline-code span (`true`, `lunad`, `F9`) is fine to speak; a
# slashed or dotted one is a path or a call and is not.
_WORDLIKE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,24}$")


def _placeholder() -> str:
    return config.SPEECH_PLACEHOLDER


def strip_for_speech(text: str, max_chars: int | None = None) -> str:
    """Reduce a written reply to something worth hearing.

    Code, paths, URLs, e-mail addresses, hashes and long digit runs collapse
    into one short placeholder each; runs of placeholders collapse into one.
    The result is then capped at a sentence boundary — a spoken answer that
    runs past a paragraph has already failed.
    """
    if not text:
        return ""
    cap = config.SPEECH_MAX_CHARS if max_chars is None else max_chars
    ph = _placeholder()

    out = text
    out = _FENCE_RE.sub(f" {ph} ", out)
    out = _FENCE_UNCLOSED_RE.sub(f" {ph} ", out)   # a truncated reply, mid-fence
    out = _MD_LINK_RE.sub(r"\1", out)              # keep the label, drop the target
    out = _URL_RE.sub(f" {ph} ", out)
    out = _EMAIL_RE.sub(f" {ph} ", out)
    out = _INDENTED_CODE_RE.sub(f" {ph} ", out)
    out = _WIN_PATH_RE.sub(f" {ph} ", out)
    out = _PATH_RE.sub(f" {ph} ", out)
    out = _HEX_RE.sub(f" {ph} ", out)
    out = _DIGITS_RE.sub(f" {ph} ", out)
    out = _INLINE_CODE_RE.sub(
        lambda m: m.group(1) if _WORDLIKE_RE.match(m.group(1).strip())
        else f" {ph} ", out)
    out = _MD_MARKS_RE.sub("", out)
    out = _EMPHASIS_RE.sub("", out)

    out = _collapse_placeholders(out, ph)
    out = re.sub(r"[ \t]+", " ", out)
    # A substitution leaves " ." and " ," behind; piper reads those as a pause
    # in the wrong place, so tidy the punctuation back up against the word.
    out = re.sub(r"\s+([,.;:!?…])", r"\1", out)
    out = re.sub(r"([,.;:!?…])\1+", r"\1", out)
    out = re.sub(r"\n{2,}", "\n", out)
    out = "\n".join(line.strip() for line in out.splitlines())
    out = out.strip()
    return _truncate_at_sentence(out, cap)


def _collapse_placeholders(text: str, ph: str) -> str:
    """Several placeholders in a row are one thought, not several.

    "Check it's on screen and it's on screen and it's on screen" is what three
    file paths in one sentence turns into, and it is worse than saying nothing.
    Connectives count as part of the run, not as content between two runs.
    """
    escaped = re.escape(ph)
    joiner = r"(?:[\s,;:.\-]*(?:and|or|then|plus|,)?[\s,;:.\-]*)"
    pattern = re.compile(rf"(?:{escaped})(?:{joiner}(?:{escaped}))+")
    return pattern.sub(ph, text)


def _truncate_at_sentence(text: str, cap: int) -> str:
    if cap <= 0 or len(text) <= cap:
        return text
    head = text[:cap]
    cut = max(head.rfind("."), head.rfind("!"), head.rfind("?"))
    if cut < cap // 3:                       # no usable boundary: fall back to a word
        cut = head.rfind(" ")
    if cut <= 0:
        cut = cap
    return head[:cut + 1].strip() + " The rest is on screen."


# =========================================================================
# Sentence splitting
# =========================================================================

# Abbreviations whose full stop does not end a sentence. Short and English-only
# on purpose: a big list buys accuracy we cannot hear.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g",
    "i.e", "approx", "fig", "no", "vol", "al", "inc", "ltd", "co", "eg", "ie",
})
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:])\s+")


def split_sentences(text: str,
                    max_chars: int | None = None) -> list[str]:
    """Split into speakable units, first sentence first.

    The point is latency: piper starts on unit one while the rest is still
    text, so speech begins in about a fifth of a second rather than after the
    whole reply has been synthesised.
    """
    if not text or not text.strip():
        return []
    limit = config.SPEECH_MAX_SENTENCE_CHARS if max_chars is None else max_chars

    units: list[str] = []
    for para in text.splitlines():
        para = para.strip()
        if not para:
            continue
        units.extend(_split_paragraph(para))

    out: list[str] = []
    for unit in units:
        out.extend(_cap_length(unit, limit))
    return [u for u in (u.strip() for u in out) if u]


def _split_paragraph(para: str) -> list[str]:
    pieces = _SENTENCE_END_RE.split(para)
    merged: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if merged and _ends_in_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged


def _ends_in_abbreviation(chunk: str) -> bool:
    if not chunk.endswith("."):
        return False
    tail = chunk[:-1].split()[-1] if chunk[:-1].split() else ""
    tail = tail.strip("([\"'").lower()
    if tail in _ABBREVIATIONS:
        return True
    # A single initial ("J." in "J. Smith") is never a sentence end either.
    return len(tail) == 1 and tail.isalpha()


def _cap_length(unit: str, limit: int) -> list[str]:
    """Break an over-long sentence on clause boundaries, then on words."""
    if limit <= 0 or len(unit) <= limit:
        return [unit]
    out: list[str] = []
    buf = ""
    for clause in _CLAUSE_SPLIT_RE.split(unit):
        candidate = f"{buf} {clause}".strip() if buf else clause
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            out.append(buf)
        buf = clause if len(clause) <= limit else ""
        if not buf:
            out.extend(_hard_wrap(clause, limit))
    if buf:
        out.append(buf)
    return out or [unit[:limit]]


def _hard_wrap(text: str, limit: int) -> list[str]:
    out, buf = [], ""
    for word in text.split():
        candidate = f"{buf} {word}".strip()
        if len(candidate) > limit and buf:
            out.append(buf)
            buf = word
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


# =========================================================================
# Voice configuration
# =========================================================================


class SpeechUnavailable(RuntimeError):
    """TTS cannot run. Reported to the client; never fatal to the daemon."""


#: `[voice] speed`'s range, and the range the OpenAI-shaped /audio/speech
#: endpoint documents. The schema already validates against it; clamping again
#: here is for the fallback path, where a value can arrive from a raw dict.
SPEED_MIN, SPEED_MAX = 0.25, 4.0


def _clamp_speed(value: Any) -> float:
    """`[voice] speed` as a usable multiplier. Never raises."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return 1.0
    if speed != speed or speed <= 0:            # NaN, zero, negative
        return 1.0
    return min(SPEED_MAX, max(SPEED_MIN, speed))


def length_scale(speed: float) -> float:
    """piper's knob, from ours.

    piper measures duration, we measure rate, so they are reciprocals: speed
    2.0 is length_scale 0.5. Naming it here rather than inlining ``1/speed``
    because getting the direction backwards is silent — it still speaks, just
    wrongly, and nothing in the pipeline would flag it.
    """
    return round(1.0 / _clamp_speed(speed), 6)


def read_sample_rate(config_path: Path | None = None) -> int:
    """Read the voice's own sample rate from its ``.onnx.json``.

    Hard-coding 22050 works for this voice and silently pitch-shifts the next
    one. The rate is in the config file; read it.
    """
    path = config_path or config.VOICE_CONFIG
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpeechUnavailable(
            f"voice config missing at {path}. Download the voice with: "
            f"{config.VENV_PYTHON} -m piper.download_voices {config.VOICE_NAME} "
            f"--download-dir {config.VOICES_DIR}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SpeechUnavailable(f"voice config at {path} is unreadable: {exc}") from exc
    rate = (data.get("audio") or {}).get("sample_rate")
    if not isinstance(rate, int) or rate <= 0:
        raise SpeechUnavailable(
            f"voice config at {path} has no usable audio.sample_rate")
    return rate


# =========================================================================
# OpenRouter TTS
# =========================================================================


class RemoteSpeechFailed(RuntimeError):
    """One sentence did not come back. Recoverable: piper is still there."""


class Wav:
    """The three numbers ``aplay`` needs, and the samples themselves."""

    __slots__ = ("pcm", "rate", "channels", "bits")

    def __init__(self, pcm: bytes, rate: int, channels: int, bits: int) -> None:
        self.pcm, self.rate, self.channels, self.bits = pcm, rate, channels, bits

    def format(self) -> str:
        return {8: "U8", 16: "S16_LE", 24: "S24_3LE", 32: "S32_LE"}.get(
            self.bits, "S16_LE")

    def matches(self, other: "Wav") -> bool:
        return (self.rate, self.channels, self.bits) == (
            other.rate, other.channels, other.bits)


def parse_wav(data: bytes) -> Wav:
    """Pull the samples out of a RIFF container.

    Walking the chunk list rather than assuming a 44-byte header: a WAV with a
    ``LIST``/``INFO`` chunk before ``data`` is perfectly legal, and slicing at
    a fixed offset would feed that metadata to ``aplay`` as audio — which is
    audible, as a click.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RemoteSpeechFailed(
            f"the provider did not return WAV audio (got {len(data)} bytes "
            f"starting {data[:16]!r})")
    rate = channels = bits = 0
    pos = 12
    pcm = b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            _, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", body, 0)
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)          # chunks are word-aligned
    if not pcm or not rate or not channels:
        raise RemoteSpeechFailed("the WAV body had no usable data chunk")
    return Wav(pcm, rate, channels, bits or 16)


def synthesise(text: str, *, model: str, voice: str, api_key: str,
               speed: float = 1.0,
               url: str = config.OPENROUTER_SPEECH_URL,
               timeout: float = config.OPENROUTER_TIMEOUT_S) -> Wav:
    """One sentence, one request. Raises :class:`RemoteSpeechFailed`.

    Every failure mode the provider actually exhibits is folded into one
    exception on purpose: a 502, a timeout, an empty body and a JSON error page
    all mean the same thing to the caller, which is "use piper for this one".
    """
    if not api_key:
        raise RemoteSpeechFailed(
            "no OpenRouter API key; set OPENROUTER_API_KEY in "
            f"{config.SECRETS_PATH}")
    body_json: dict[str, Any] = {"model": model, "input": text, "voice": voice}
    # Sent only when it is not 1.0. `speed` is in the OpenAI /audio/speech
    # shape this endpoint copies, but not every model behind OpenRouter
    # implements it, and a request that 400s on an unknown field for the
    # default value would break speech for everyone who never touched the
    # setting. Off the default, a rejection is a fallback to piper — which
    # honours the speed anyway — and it is reported, not swallowed.
    if _clamp_speed(speed) != 1.0:
        body_json["speed"] = _clamp_speed(speed)
    payload = json.dumps(body_json).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "Accept": "audio/wav"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - the status is the useful part
            pass
        raise RemoteSpeechFailed(
            f"{url} returned HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RemoteSpeechFailed(f"could not reach {url}: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RemoteSpeechFailed(f"{url} timed out or failed: {exc}") from exc
    if not body:
        raise RemoteSpeechFailed(f"{url} returned an empty body")
    return parse_wav(body)


# =========================================================================
# The worker
# =========================================================================


class _Job:
    __slots__ = ("id", "sentences", "text", "cancelled", "done", "started",
                 "error", "played_bytes", "first_audio", "provider",
                 "fell_back", "voice", "sample_rate", "cfg", "speed")

    def __init__(self, sentences: list[str], text: str,
                 cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or {}
        self.id = uuid.uuid4().hex[:12]
        self.sentences = sentences
        self.text = text
        self.cancelled = False
        self.error: str | None = None
        self.started = time.monotonic()
        self.done = threading.Event()
        self.played_bytes = 0
        self.first_audio: float | None = None   # proves streaming, not batching
        self.provider = str(self.cfg.get("provider", "piper"))
        self.voice = str(self.cfg.get("voice", ""))
        self.speed = _clamp_speed(self.cfg.get("speed", 1.0))
        self.sample_rate: int | None = None
        # Set when a remote request failed and piper finished the utterance.
        # Reported rather than swallowed: silent degradation is how a broken
        # voice goes unnoticed for a week.
        self.fell_back: str = ""


class Speech:
    """Two voices, one pipeline: OpenRouter first, piper underneath.

    Lazy piper worker with sentence streaming, barge-in and idle unload; an
    OpenRouter request per sentence when the config asks for it, feeding the
    same single ``aplay``.
    """

    def __init__(
        self,
        model: Path | None = None,
        voice_config: Path | None = None,
        python: Path | None = None,
        idle_unload_s: float | None = None,
        aplay: str | None = None,
        settings: Any = None,
        synth: Any = None,
    ) -> None:
        # Kept as *overrides*, not as resolved values: with neither given,
        # `model` and `voice_config` are derived from `[voice] piper_voice`
        # on every read, so changing the voice in the GUI swaps the model
        # without a restart. Tests pass explicit paths and keep them.
        self._model_override = Path(model) if model is not None else None
        self._voice_config_override = (Path(voice_config)
                                       if voice_config is not None else None)
        self.python = python or config.VENV_PYTHON
        self.idle_unload_s = (config.SPEECH_IDLE_UNLOAD_S
                              if idle_unload_s is None else idle_unload_s)
        self.aplay = aplay or config.APLAY_BIN
        # Injected in tests so that "the provider 502s" is a branch the suite
        # can take without a network, and so that a test can never spend money.
        self._settings = settings
        self.synth = synth if synth is not None else synthesise

        self._proc: subprocess.Popen | None = None
        self._stdout: Any = None
        self._sample_rate: int | None = None
        # Which piper voice the running worker actually holds. A worker is one
        # loaded ONNX model and cannot be re-pointed, so a change to
        # `[voice] piper_voice` has to unload it rather than be ignored.
        self._loaded_voice: str | None = None
        self._lock = threading.RLock()          # guards worker + job slot
        self._speak_lock = threading.Lock()     # serialises playback threads
        self._job: _Job | None = None
        self._player: subprocess.Popen | None = None
        self._last_used = time.monotonic()
        self._closed = False
        self._load_ms: int | None = None
        self.counters = {"said": 0, "cancelled": 0, "errors": 0,
                         "loads": 0, "unloads": 0, "remote": 0,
                         "fallbacks": 0}

        self._reaper = threading.Thread(target=self._idle_reaper, daemon=True,
                                        name="luna-speech-idle")
        self._reaper.start()

    # -- public API ------------------------------------------------------

    @property
    def settings(self) -> Any:
        return (self._settings if self._settings is not None
                else settings_mod.settings())

    def piper_voice(self) -> str:
        """`[voice] piper_voice`, or the fallback default."""
        if self._model_override is not None:
            return self._model_override.name.removesuffix(".onnx")
        name = str(self._voice_settings().get("piper_voice") or "").strip()
        return name or config.VOICE_NAME

    @property
    def model(self) -> Path:
        if self._model_override is not None:
            return self._model_override
        return config.voice_paths(self.piper_voice())[0]

    @property
    def voice_config(self) -> Path:
        if self._voice_config_override is not None:
            return self._voice_config_override
        return config.voice_paths(self.piper_voice())[1]

    def available(self) -> tuple[bool, str]:
        missing = [str(p) for p in (self.python, self.model, self.voice_config)
                   if not p.exists()]
        if missing:
            return False, "missing: " + ", ".join(missing)
        return True, f"{self.piper_voice()} via {self.python}"

    def _reported_rate(self) -> int | None:
        """Sample rate for status/report payloads.

        ``self._sample_rate`` is only populated once the worker has sent its
        first metadata frame, so a detached ``say`` reports ``None`` even though
        playback itself resolves the rate from the voice config. Fall back to
        the config so the reported value matches what is actually played.
        """
        if self._sample_rate:
            return self._sample_rate
        # Only guess from the piper voice config when piper is the provider
        # that will actually speak. Guessing piper's 22050 while OpenRouter is
        # configured reports the WRONG rate on every detached say, which reads
        # as "it fell back to piper" when it did not. Unknown is honest.
        try:
            if str(self._voice_settings().get("provider", "")) != "openrouter":
                return read_sample_rate(self.voice_config)
        except Exception:
            pass
        return None

    # -- settings, read fresh on every utterance --------------------------

    def _voice_settings(self) -> dict[str, Any]:
        """The voice half of the config, as of right now.

        Read here and not cached on the instance: hot reload is only real if
        the value that reaches the request is the value in the file at the time
        of the request. A voice captured in ``__init__`` would need a restart,
        which is the thing this is meant to remove.
        """
        cfg = self.settings.section("voice") if self.settings else {}
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "provider": str(cfg.get("provider", "openrouter")),
            "model": str(cfg.get("model", "deepgram/flux-tts:free")),
            "voice": str(cfg.get("voice", "flux-sienna-en")),
            "fallback": str(cfg.get("fallback", "piper")),
            "max_spoken_chars": int(cfg.get("max_spoken_chars",
                                            config.SPEECH_MAX_CHARS)),
            "piper_voice": str(cfg.get("piper_voice") or config.VOICE_NAME),
            "speed": _clamp_speed(cfg.get("speed", 1.0)),
        }

    def say(self, text: str, wait: bool = False,
            timeout: float = 120.0) -> dict[str, Any]:
        """Speak ``text``. Cancels anything already speaking (barge-in).

        Returns immediately unless ``wait``; the spoken form is returned either
        way so a caller can show what was actually said.
        """
        voice_cfg = self._voice_settings()
        if not voice_cfg["enabled"]:
            return {"spoken": "", "sentences": 0, "id": None,
                    "note": "voice is switched off in [voice] enabled"}
        # `max_spoken_chars` is the cap the schema exposes: anything past it is
        # trimmed at a sentence boundary and the full text stays on screen.
        spoken = strip_for_speech(text, max_chars=voice_cfg["max_spoken_chars"])
        sentences = split_sentences(spoken)
        if not sentences:
            return {"spoken": "", "sentences": 0, "id": None,
                    "note": "nothing speakable in that text"}

        self.cancel()                       # barge-in: previous utterance stops
        job = _Job(sentences, spoken, cfg=voice_cfg)
        with self._lock:
            if self._closed:
                raise SpeechUnavailable("speech worker is shut down")
            self._job = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True,
                         name=f"luna-speak-{job.id}").start()
        if wait:
            job.done.wait(timeout)
            if job.error:
                raise SpeechUnavailable(job.error)
        payload: dict[str, Any] = {
            "spoken": spoken, "sentences": len(sentences), "id": job.id,
            "sample_rate": job.sample_rate or self._reported_rate(),
            "waited": wait, "cancelled": job.cancelled,
            "provider": job.provider, "voice": job.voice or self.piper_voice(),
        }
        if job.fell_back:
            payload["fell_back_to"] = "piper"
            payload["fallback_reason"] = job.fell_back[:300]
        if job.first_audio is not None:
            payload["first_audio_ms"] = int(
                (job.first_audio - job.started) * 1000)
        if wait:
            payload["total_ms"] = int((time.monotonic() - job.started) * 1000)
        return payload

    def cancel(self) -> bool:
        """Stop speaking now. Safe to call when nothing is speaking."""
        with self._lock:
            job, player, proc = self._job, self._player, self._proc
            # "Was anything actually speaking" is decided before we interrupt,
            # so the counter measures barge-ins and not the no-op cancel that
            # every say() issues on its way in.
            speaking = (job is not None and not job.done.is_set()) or (
                player is not None and player.poll() is None)
            if job is not None:
                job.cancelled = True
        if proc is not None and proc.poll() is None:
            self._send(proc, {"op": "cancel"})
        if player is not None and player.poll() is None:
            self._kill(player)
        if speaking:
            self.counters["cancelled"] += 1
        return speaking

    def status(self) -> dict[str, Any]:
        cfg = self._voice_settings()
        with self._lock:
            proc, job = self._proc, self._job
            speaking = job is not None and not job.done.is_set()
            return {
                "loaded": proc is not None and proc.poll() is None,
                "pid": proc.pid if proc and proc.poll() is None else None,
                "speaking": speaking,
                "provider": cfg["provider"],
                "voice": (cfg["voice"] if cfg["provider"] == "openrouter"
                          else self.piper_voice()),
                "piper_voice": self.piper_voice(),
                "speed": cfg["speed"],
                "model": cfg["model"],
                "fallback": cfg["fallback"],
                "enabled": cfg["enabled"],
                "max_spoken_chars": cfg["max_spoken_chars"],
                "key": settings_mod.secrets_status().get("present", False),
                "sample_rate": (job.sample_rate if job and job.sample_rate
                                else self._reported_rate()),
                "load_ms": self._load_ms,
                "idle_s": round(time.monotonic() - self._last_used, 1),
                "idle_unload_s": self.idle_unload_s,
                "counters": dict(self.counters),
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel()
        self._unload("shutdown")

    # -- playback --------------------------------------------------------

    def _run_job(self, job: _Job) -> None:
        # Serialised: a cancelled predecessor still has to drain its END frame
        # before the next utterance may read from the same stdout.
        with self._speak_lock:
            try:
                self._play(job)
            except SpeechUnavailable as exc:
                job.error = str(exc)
                self.counters["errors"] += 1
                log.warning("speech unavailable", extra={"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001 - a TTS fault is not fatal
                job.error = f"{type(exc).__name__}: {exc}"
                self.counters["errors"] += 1
                log.exception("speech failed")
            finally:
                with self._lock:
                    if self._job is job:
                        self._player = None
                self._last_used = time.monotonic()
                job.done.set()

    def _play(self, job: _Job) -> None:
        """Route the utterance to a provider, and catch it if it falls.

        The remote path returns the sentences it did not manage to speak. That
        is the whole fallback contract: an empty list means she said it all, a
        non-empty one means piper finishes the job. Falling back mid-utterance
        rather than only at sentence one matters because the failure the
        provider actually produces is an intermittent 502, which is as likely
        on sentence three as on sentence one.
        """
        remaining = job.sentences
        if job.provider == "openrouter":
            remaining = self._play_remote(job)
            if remaining and job.cfg.get("fallback") != "piper":
                raise SpeechUnavailable(
                    job.fell_back or "the speech provider failed and "
                    "[voice] fallback is not piper")
            if remaining:
                log.warning("falling back to piper",
                            extra={"job": job.id, "left": len(remaining),
                                   "detail": job.fell_back[:300]})
                self.counters["fallbacks"] += 1
        if remaining:
            self._play_piper(job, remaining)

    # -- OpenRouter ------------------------------------------------------

    def _play_remote(self, job: _Job) -> list[str]:
        """Sentence-at-a-time synthesis into one ``aplay``.

        A second thread runs one sentence ahead, so the request for sentence
        two is already in flight while sentence one is playing. One ahead and
        not all of them: a barge-in two words in should not have paid for the
        whole reply.
        """
        cfg = job.cfg
        key = settings_mod.api_key()
        model, voice = str(cfg.get("model", "")), str(cfg.get("voice", ""))
        results: dict[int, Any] = {}
        playing = [0]                     # the index the consumer is on
        ready = threading.Condition()

        def produce() -> None:
            for index, sentence in enumerate(job.sentences):
                with ready:
                    # Stay at most one sentence ahead of playback.
                    while not job.cancelled and index - playing[0] > 1:
                        ready.wait(0.05)
                if job.cancelled:
                    break
                try:
                    outcome: Any = self.synth(
                        sentence, model=model, voice=voice, api_key=key,
                        speed=job.speed,
                        timeout=config.OPENROUTER_TIMEOUT_S)
                except RemoteSpeechFailed as exc:
                    outcome = exc
                except Exception as exc:  # noqa: BLE001 - any fault is a fallback
                    outcome = RemoteSpeechFailed(f"{type(exc).__name__}: {exc}")
                with ready:
                    results[index] = outcome
                    ready.notify_all()
                if isinstance(outcome, RemoteSpeechFailed):
                    break

        worker = threading.Thread(target=produce, daemon=True,
                                  name=f"luna-tts-{job.id}")
        worker.start()

        player: subprocess.Popen | None = None
        current: Wav | None = None
        index = 0
        try:
            while index < len(job.sentences):
                with ready:
                    deadline = time.monotonic() + config.OPENROUTER_TIMEOUT_S + 10
                    while index not in results and not job.cancelled:
                        if time.monotonic() > deadline:
                            results[index] = RemoteSpeechFailed(
                                "the synthesis thread produced nothing in time")
                            break
                        ready.wait(0.05)
                    outcome = results.get(index)
                if job.cancelled:
                    return []
                if isinstance(outcome, RemoteSpeechFailed) or outcome is None:
                    job.fell_back = str(outcome or "no audio")
                    return job.sentences[index:]
                wav: Wav = outcome
                if player is not None and current is not None and not wav.matches(current):
                    # A format change cannot be spliced into a running aplay.
                    self._finish_player(player, job)
                    player, current = None, None
                if player is None:
                    player = self._start_player(wav.rate, channels=wav.channels,
                                                fmt=wav.format())
                    current = wav
                    job.first_audio = job.first_audio or time.monotonic()
                    job.sample_rate = wav.rate
                if not _feed(player, wav.pcm):
                    job.cancelled = True
                    return []
                job.played_bytes += len(wav.pcm)
                index += 1
                with ready:
                    playing[0] = index
                    ready.notify_all()
        finally:
            if player is not None:
                self._finish_player(player, job)
        if not job.cancelled and not job.error:
            self.counters["said"] += 1
            self.counters["remote"] += 1
        return []

    # -- piper -----------------------------------------------------------

    def _play_piper(self, job: _Job, sentences: list[str]) -> None:
        proc, stdout = self._ensure_worker()
        rate = self._sample_rate or read_sample_rate(self.voice_config)
        job.sample_rate = job.sample_rate or rate

        if job.cancelled:
            return
        if not self._send(proc, {"op": "say", "id": job.id,
                                 "sentences": sentences,
                                 "length_scale": length_scale(job.speed)}):
            raise SpeechUnavailable("the piper worker vanished mid-request")

        player: subprocess.Popen | None = None
        try:
            while True:
                header = _read_header(stdout)
                if header is None:
                    raise SpeechUnavailable(
                        "the piper worker closed its output unexpectedly")
                kind, _, rest = header.partition(" ")
                if kind == "AUDIO":
                    payload = _read_exactly(stdout, int(rest.strip()))
                    if job.cancelled:
                        continue            # keep draining; frames must balance
                    if player is None:
                        player = self._start_player(rate)
                        job.first_audio = time.monotonic()
                    if not _feed(player, payload):
                        job.cancelled = True
                    else:
                        job.played_bytes += len(payload)
                elif kind == "BEGIN":
                    continue
                elif kind == "END":
                    bits = rest.split(" ", 2)
                    status = bits[1] if len(bits) > 1 else "ok"
                    if status == "error":
                        job.error = bits[2] if len(bits) > 2 else "synthesis failed"
                    break
                else:                        # a stray line is a bug, not audio
                    log.warning("unexpected frame from the piper worker",
                                extra={"frame": header[:120]})
        finally:
            if player is not None:
                self._finish_player(player, job)
        if not job.cancelled and not job.error:
            self.counters["said"] += 1

    def _start_player(self, rate: int, channels: int = 1,
                      fmt: str = "S16_LE") -> subprocess.Popen:
        argv = [self.aplay, "-q", "-r", str(rate), "-f", fmt,
                "-c", str(channels), "-t", "raw", "-"]
        try:
            # The barge-in kills this pid, so this pid must be in the ledger
            # before it can start making noise. `durable=False` keeps the fsync
            # out of the path that decides how fast Luna speaks; what is left
            # is a 0.4 ms ledger write, and first audio still measures 40-45 ms
            # warm, the same as Phase 1.
            player = safety.spawn(
                argv, kind="tts-play", durable=False, note="aplay",
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
        except OSError as exc:
            raise SpeechUnavailable(f"could not start {self.aplay}: {exc}") from exc
        with self._lock:
            self._player = player
        return player

    def _finish_player(self, player: subprocess.Popen, job: _Job) -> None:
        if job.cancelled:
            self._kill(player)
            return
        try:
            if player.stdin:
                player.stdin.close()
        except OSError:
            pass
        try:
            player.wait(timeout=180)
        except subprocess.TimeoutExpired:
            self._kill(player)
            return
        if player.returncode not in (0, None) and not job.cancelled:
            err = ""
            if player.stderr:
                try:
                    err = player.stderr.read().decode("utf-8", "replace")[-300:]
                except OSError:
                    pass
            job.error = f"aplay exited {player.returncode}: {err.strip()}"
            self.counters["errors"] += 1

    # -- worker lifecycle ------------------------------------------------

    def _ensure_worker(self) -> tuple[subprocess.Popen, Any]:
        wanted = self.piper_voice()
        # Outside the lock: _unload takes it, and a worker holding the wrong
        # model has to go before the branch below can decide it is reusable.
        with self._lock:
            stale = (self._proc is not None and self._proc.poll() is None
                     and self._loaded_voice not in (None, wanted))
        if stale:
            log.info("piper voice changed; reloading",
                     extra={"was": self._loaded_voice, "now": wanted})
            self._sample_rate = None
            self._unload(f"voice changed to {wanted}")
        with self._lock:
            if self._closed:
                raise SpeechUnavailable("speech worker is shut down")
            if self._proc is not None and self._proc.poll() is None:
                self._last_used = time.monotonic()
                return self._proc, self._stdout
            self._proc = None

            ok, detail = self.available()
            if not ok:
                raise SpeechUnavailable(f"cannot start piper — {detail}")

            argv = [str(self.python), str(config.PIPER_WORKER),
                    str(self.model), str(self.voice_config)]
            started = time.monotonic()
            try:
                # Registered in the signal ledger by the same call that forks
                # it, so the idle unload and the barge-in have a pid they can
                # prove is theirs. Not durable: piper dies with the daemon.
                proc = safety.spawn(
                    argv, kind="tts-worker", durable=False, note="piper worker",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=str(config.PROJECT_DIR))
            except OSError as exc:
                raise SpeechUnavailable(f"could not start piper: {exc}") from exc

            threading.Thread(target=_drain_stderr, args=(proc,), daemon=True,
                             name="luna-piper-stderr").start()
            ready = _read_header_timeout(proc.stdout,
                                         config.SPEECH_START_TIMEOUT_S)
            if ready is None or not ready.startswith("READY"):
                self._kill(proc)
                raise SpeechUnavailable(
                    "the piper worker did not report READY "
                    f"(got {ready!r}); check `luna log`")
            try:
                meta = json.loads(ready[len("READY "):] or "{}")
            except json.JSONDecodeError:
                meta = {}
            self._sample_rate = (meta.get("sample_rate")
                                 or read_sample_rate(self.voice_config))
            self._load_ms = int((time.monotonic() - started) * 1000)
            self._loaded_voice = wanted
            self._proc, self._stdout = proc, proc.stdout
            self._last_used = time.monotonic()
            self.counters["loads"] += 1
            log.info("piper loaded", extra={"pid": proc.pid,
                                            "load_ms": self._load_ms,
                                            "sample_rate": self._sample_rate})
            return proc, proc.stdout

    def _idle_reaper(self) -> None:
        while True:
            time.sleep(5.0)
            with self._lock:
                if self._closed:
                    return
                proc = self._proc
                idle = time.monotonic() - self._last_used
                busy = self._job is not None and not self._job.done.is_set()
            if proc is None or busy or idle < self.idle_unload_s:
                continue
            self._unload(f"idle {idle:.0f}s")

    def _unload(self, reason: str) -> None:
        with self._lock:
            proc, self._proc, self._stdout = self._proc, None, None
            self._loaded_voice = None
        if proc is None:
            return
        self.counters["unloads"] += 1
        log.info("unloading piper", extra={"reason": reason, "pid": proc.pid})
        self._send(proc, {"op": "quit"})
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill(proc)

    # -- process handling. Only PIDs this module spawned, ever. ----------

    @staticmethod
    def _send(proc: subprocess.Popen, obj: dict[str, Any]) -> bool:
        if proc.stdin is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    @staticmethod
    def _kill(proc: subprocess.Popen, reason: str = "speech stopped") -> None:
        """Stop a child this module spawned — through the firewall, always.

        A barge-in is the most latency-sensitive kill in the daemon and it was
        the most tempting place to keep a direct ``killpg``. It does not have
        one: the ledger lookup is a dict hit and one ``/proc`` read, and the
        alternative is a second copy of the rule that must never be wrong.

        Grace is 1.5 s rather than 5: this is ``aplay`` and a piper worker
        being told to stop, and a barge-in that waits five seconds to go quiet
        is not a barge-in.
        """
        try:
            safety.terminate(proc, grace=1.5, reason=reason)
        except safety.SignalRefused:
            # Not reachable through any current path — every proc handed here
            # came from safety.spawn. If it ever happens, it is a real bug and
            # the log has to say so rather than the daemon dying mid-sentence.
            log.exception("speech tried to signal a process Luna does not own")
        finally:
            safety.reap(proc)


# =========================================================================
# Frame reading
# =========================================================================


def _read_header(stream: Any) -> str | None:
    """Read one ASCII header line. ``None`` means the worker is gone."""
    if stream is None:
        return None
    line = stream.readline()
    if not line:
        return None
    return line.decode("utf-8", "replace").rstrip("\n")


def _read_header_timeout(stream: Any, timeout: float) -> str | None:
    """``_read_header`` with a wall clock.

    readline() on a blocking pipe cannot be interrupted, so the read happens on
    a throwaway thread. Without this a piper that wedges during model load
    would wedge the daemon thread that asked it to speak — the one failure mode
    a lazy loader must not have.
    """
    box: list[str | None] = []
    reader = threading.Thread(target=lambda: box.append(_read_header(stream)),
                              daemon=True, name="luna-piper-ready")
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        return None
    return box[0] if box else None


def _read_exactly(stream: Any, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = stream.read(count - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _feed(player: subprocess.Popen, payload: bytes) -> bool:
    if player.stdin is None:
        return False
    try:
        player.stdin.write(payload)
        player.stdin.flush()
        return True
    except (BrokenPipeError, ValueError, OSError):
        return False   # aplay was killed: that is a cancel, not a failure


def _drain_stderr(proc: subprocess.Popen) -> None:
    if proc.stderr is None:
        return
    for line in proc.stderr:
        text = line.decode("utf-8", "replace").rstrip()
        if text:
            log.warning("piper: %s", text[:400])
