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
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config, safety

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
# The worker
# =========================================================================


class _Job:
    __slots__ = ("id", "sentences", "text", "cancelled", "done", "started",
                 "error", "played_bytes", "first_audio")

    def __init__(self, sentences: list[str], text: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.sentences = sentences
        self.text = text
        self.cancelled = False
        self.error: str | None = None
        self.started = time.monotonic()
        self.done = threading.Event()
        self.played_bytes = 0
        self.first_audio: float | None = None   # proves streaming, not batching


class Speech:
    """Lazy piper worker with sentence streaming, barge-in and idle unload."""

    def __init__(
        self,
        model: Path | None = None,
        voice_config: Path | None = None,
        python: Path | None = None,
        idle_unload_s: float | None = None,
        aplay: str | None = None,
    ) -> None:
        self.model = model or config.VOICE_ONNX
        self.voice_config = voice_config or config.VOICE_CONFIG
        self.python = python or config.VENV_PYTHON
        self.idle_unload_s = (config.SPEECH_IDLE_UNLOAD_S
                              if idle_unload_s is None else idle_unload_s)
        self.aplay = aplay or config.APLAY_BIN

        self._proc: subprocess.Popen | None = None
        self._stdout: Any = None
        self._sample_rate: int | None = None
        self._lock = threading.RLock()          # guards worker + job slot
        self._speak_lock = threading.Lock()     # serialises playback threads
        self._job: _Job | None = None
        self._player: subprocess.Popen | None = None
        self._last_used = time.monotonic()
        self._closed = False
        self._load_ms: int | None = None
        self.counters = {"said": 0, "cancelled": 0, "errors": 0,
                         "loads": 0, "unloads": 0}

        self._reaper = threading.Thread(target=self._idle_reaper, daemon=True,
                                        name="luna-speech-idle")
        self._reaper.start()

    # -- public API ------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        missing = [str(p) for p in (self.python, self.model, self.voice_config)
                   if not p.exists()]
        if missing:
            return False, "missing: " + ", ".join(missing)
        return True, f"{config.VOICE_NAME} via {self.python}"

    def _reported_rate(self) -> int | None:
        """Sample rate for status/report payloads.

        ``self._sample_rate`` is only populated once the worker has sent its
        first metadata frame, so a detached ``say`` reports ``None`` even though
        playback itself resolves the rate from the voice config. Fall back to
        the config so the reported value matches what is actually played.
        """
        if self._sample_rate:
            return self._sample_rate
        try:
            return read_sample_rate(self.voice_config)
        except Exception:
            return None

    def say(self, text: str, wait: bool = False,
            timeout: float = 120.0) -> dict[str, Any]:
        """Speak ``text``. Cancels anything already speaking (barge-in).

        Returns immediately unless ``wait``; the spoken form is returned either
        way so a caller can show what was actually said.
        """
        spoken = strip_for_speech(text)
        sentences = split_sentences(spoken)
        if not sentences:
            return {"spoken": "", "sentences": 0, "id": None,
                    "note": "nothing speakable in that text"}

        self.cancel()                       # barge-in: previous utterance stops
        job = _Job(sentences, spoken)
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
            "sample_rate": self._reported_rate(), "waited": wait,
            "cancelled": job.cancelled,
        }
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
        with self._lock:
            proc, job = self._proc, self._job
            speaking = job is not None and not job.done.is_set()
            return {
                "loaded": proc is not None and proc.poll() is None,
                "pid": proc.pid if proc and proc.poll() is None else None,
                "speaking": speaking,
                "voice": config.VOICE_NAME,
                "sample_rate": self._reported_rate(),
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
        proc, stdout = self._ensure_worker()
        rate = self._sample_rate or read_sample_rate(self.voice_config)

        if job.cancelled:
            return
        if not self._send(proc, {"op": "say", "id": job.id,
                                 "sentences": job.sentences}):
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

    def _start_player(self, rate: int) -> subprocess.Popen:
        argv = [self.aplay, "-q", "-r", str(rate), "-f", "S16_LE", "-c", "1",
                "-t", "raw", "-"]
        try:
            # The barge-in kills this pid, so this pid must be in the ledger
            # before it can start making noise. `durable=False` keeps the
            # ~4 ms fsync out of the path that decides how fast Luna speaks.
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
