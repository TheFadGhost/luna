"""Luna's coarse state, published as one word in a file.

ARCHITECTURE.md section 3 originally reserved a `subscribe` op for this: the
bar widget would hold a socket open and read NDJSON state frames. That is the
wrong shape and it has been dropped.

Three reasons, in order of how much they matter:

1. **A reader must never be able to slow the reply path.** A subscriber is a
   socket the daemon has to write to, and a socket has a buffer that a stalled
   reader fills. The moment `speaking` is published down a pipe that nobody is
   draining, the publishing thread is the thread that was about to speak. A
   file has no reader-side backpressure at all: `os.replace` succeeds whether
   the bar is running, wedged, or was never started.

2. **The widget is a bar module, not a network client.** Quickshell's
   `FileView` already does inotify-backed file watching with no polling — Sill
   uses exactly this for its pending-screenshot badge. Making a QML module
   speak newline-delimited JSON over a Unix socket, and reconnect after every
   daemon restart, is a large amount of machinery to reinvent what the toolkit
   hands over for free.

3. **It is the established pattern on this machine.** voxtype writes
   `$XDG_RUNTIME_DIR/voxtype/state` on every transition for exactly this
   purpose (`state_file` in its config), and the desktop already reads it.
   Luna publishing the same way means one idiom, not two.

The contract, which the bar module depends on:

* Path: ``$XDG_RUNTIME_DIR/luna/state`` — tmpfs, so a reboot clears it.
* Content: a single ASCII word, no trailing newline, one of ``idle``,
  ``thinking``, ``speaking``. Anything else must be treated as ``idle``.
* Absent file means the daemon is not running. lunad removes it on a clean
  shutdown, and `lunad.service` has an `ExecStopPost` that removes it after a
  crash too, so absence is trustworthy in both directions.
* Writes are atomic (temp file plus `os.replace` in the same directory), so a
  watcher never reads a half-written word.
* `listening` is deliberately NOT published here. Luna does not own the
  microphone — voxtype does, and the widget reads voxtype's own state file for
  that. The daemon does not learn about a voice turn until the transcript
  arrives, which is after the listening is over.

Nothing in this module may raise. It is called from the middle of answering a
question, and a full disk or a vanished runtime directory is not a reason to
fail an answer that was otherwise fine.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from . import config

log = logging.getLogger("lunad.presence")

IDLE = "idle"
THINKING = "thinking"
SPEAKING = "speaking"

STATES = (IDLE, THINKING, SPEAKING)


class Presence:
    """Publishes one word to one file. Best-effort, always."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.STATE_FILE
        self._tmp = self.path.with_name(self.path.name + ".tmp")
        self._lock = threading.Lock()
        self._current: str | None = None
        # `clear()` is final, and it has to be. Shutdown publishes on its way
        # out whether anyone meant it to or not: `Daemon.close()` cancels
        # speech, cancelling speech fires the activity callback, and the
        # callback republishes `idle` -- so a clear that could be undone left
        # the file behind on every clean stop, with the bar then showing an
        # assistant that had already exited. Measured against a real daemon,
        # not reasoned about.
        self._closed = False
        # A write that failed once is usually a write that will fail every
        # time (no runtime directory, read-only filesystem). Log the first
        # one and then stay quiet, so a broken desktop cannot flood the log
        # from the middle of every turn.
        self._complained = False

    def set(self, state: str) -> None:
        """Publish ``state``. Unchanged state writes nothing at all.

        A no-op once `clear()` has been called: after that the daemon is on
        its way out and there is no state it could truthfully claim.
        """
        if state not in STATES:
            state = IDLE
        with self._lock:
            if self._closed or state == self._current:
                return
            self._current = state
            self._write(state)

    def clear(self) -> None:
        """Remove the file: the daemon is going away, and is not coming back.

        Final by design -- see `_closed`. Nothing may re-publish afterwards.
        """
        with self._lock:
            self._closed = True
            self._current = None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._complain("could not remove the state file", exc)
            try:
                self._tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    # -- internals -------------------------------------------------------

    def _write(self, state: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # No newline: the contract is one bare word, and a watcher that
            # compares the whole file body should not have to strip anything.
            with open(self._tmp, "w", encoding="ascii") as fh:
                fh.write(state)
            os.replace(self._tmp, self.path)
        except OSError as exc:
            self._complain("could not publish state", exc)
            # Forget what we think is on disk, so the next transition tries
            # again rather than being deduplicated away against a write that
            # never landed.
            self._current = None

    def _complain(self, what: str, exc: BaseException) -> None:
        if self._complained:
            return
        self._complained = True
        log.warning("%s (further failures will be silent)", what,
                    extra={"path": str(self.path),
                           "detail": f"{type(exc).__name__}: {exc}"})
