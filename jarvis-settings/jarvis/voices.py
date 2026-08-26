"""Voice sample discovery and preview playback.

WHY PREVIEW PREFERS THE SAMPLE FILE
-----------------------------------
`~/Music/luna-voices/deepgram_<voice>.wav` is a rendering of *that exact
voice*, by the same provider the daemon would use. The daemon's `say` op
speaks through whatever voice is **currently saved**, so driving it to preview
a voice the user has picked but not yet applied plays the wrong voice and
quietly lies about the choice being made. The picker's Preview button
therefore plays the sample, and a separate "Test the live pipeline" button
exercises `say` — both paths are implemented, and each is labelled with what
it actually does.

Order for `preview()` when no sample exists for a voice (a voice the daemon
knows about but which has no local WAV): fall through to the daemon's `say`,
then give up with a reason. Nothing here ever kills a process it did not
spawn — only the aplay child from a previous preview.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import client

SAMPLE_DIR = Path(os.path.expanduser("~/Music/luna-voices"))
SAMPLE_PREFIX = "deepgram_"
PIPER_VOICE_DIR = Path(os.path.expanduser("~/.local/share/luna/voices"))
APLAY = "aplay"

SAMPLE_LINE = ("Jarvis here. This is how I sound when I read something back "
               "to you.")

# From CONFIG-SCHEMA.md. Only the two the contract names are annotated; the
# rest are listed by id, because inventing a gender for 34 voices from their
# filenames would be a guess presented as fact.
ANNOTATED = {
    "flux-sienna-en": "default · female",
    "flux-donovan-en": "alternate · male",
}


def sample_path(voice: str) -> Path:
    return SAMPLE_DIR / f"{SAMPLE_PREFIX}{voice}.wav"


def available(sample_dir: Path | None = None) -> list[str]:
    """Voice ids that have a local sample, schema defaults first."""
    d = sample_dir or SAMPLE_DIR
    try:
        found = sorted(p.name[len(SAMPLE_PREFIX):-4] for p in d.glob(
            f"{SAMPLE_PREFIX}*.wav"))
    except OSError:
        found = []
    head = [v for v in ("flux-sienna-en", "flux-donovan-en") if v in found]
    return head + [v for v in found if v not in head]


def label_for(voice: str) -> str:
    note = ANNOTATED.get(voice)
    return f"{voice}  ({note})" if note else voice


def piper_voices() -> list[str]:
    try:
        return sorted({p.name[:-5] for p in PIPER_VOICE_DIR.glob("*.onnx")})
    except OSError:
        return []


class Player:
    """Owns at most one aplay child. Jarvis started it, so Jarvis may stop
    it; nothing else on this machine is ever signalled."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def stop(self) -> None:
        p, self._proc = self._proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()          # our own child, spawned above
            except OSError:
                pass

    def busy(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def play_file(self, path: Path) -> tuple[bool, str]:
        if shutil.which(APLAY) is None:
            return False, f"{APLAY} is not installed"
        if not path.exists():
            return False, f"no sample at {path}"
        self.stop()
        argv = [APLAY, "-q", str(path)]
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            return False, f"{APLAY} failed: {exc}"
        return True, " ".join(argv)

    # ---------------------------------------------------------------- api

    def preview(self, voice: str) -> tuple[bool, str]:
        """Play the picked voice. -> (ok, the command path actually taken)."""
        path = sample_path(voice)
        if path.exists():
            ok, detail = self.play_file(path)
            return ok, (f"aplay sample: {detail}" if ok else detail)
        ok, detail = self.speak_via_daemon(
            f"This is {voice}. " + SAMPLE_LINE)
        if ok:
            return True, f"no local sample; {detail}"
        return False, f"no sample at {path}, and {detail}"

    def speak_via_daemon(self, text: str = SAMPLE_LINE) -> tuple[bool, str]:
        """Exercise the live pipeline: lunad `say` over the socket."""
        try:
            client.say(text, timeout=8.0)
        except client.OpFailed as exc:
            return False, f"daemon say refused: {exc}"
        except client.DaemonDown as exc:
            return False, f"daemon down: {exc}"
        return True, f'socket say -> {client.SOCKET_PATH}'
