"""``$XDG_RUNTIME_DIR/luna/hud.json`` — the orb overlay's settings, published.

The overlay is QML, drawn by the Quickshell plugin
``~/.config/omarchy/plugins/ghost.lunaorb``. It has to know six things —
whether to draw at all, which corner, how big, whether to sit there while she
is idle, whether to draw the caption, and which sprite — and every one of them
is a user setting that already lives in ``~/.config/jarvis/config.toml``.

**It cannot read that file, and it must not learn how.** Three reasons, in the
order they matter:

1. Quickshell has no TOML parser. Adding one — in QML, by hand, for one table
   — is a parser nobody will maintain in a language that makes it painful.
2. ``config.toml`` is 0600 inside a 0700 directory, next to ``secrets.env``.
   Widening either to let a shell plugin read it is a permission change made
   for a cosmetic feature, in a directory whose whole point is that it is
   private.
3. A second reader of the schema is a second thing that drifts from it. The
   config file is a contract between the GUI and the daemon; the overlay is
   not a party to it and should not be able to misread it.

So lunad *projects* the ``[hud]`` table into a file shaped for the reader:
plain JSON, six keys, no comments, no other tables, in the same tmpfs
directory the overlay already watches for ``state`` and ``message``. The
projection is one-way. Nothing here ever reads ``hud.json`` back, and an
overlay that writes to it is writing to a file the next reload overwrites.

## The three properties this file has to have

**Atomic.** Written to ``hud.json.tmp`` and ``os.replace``d over, exactly as
``presence.py`` writes ``state`` and for the same reason: the reader is an
inotify watch, and a reader that can see a half-written file is a reader that
will eventually parse one. ``os.replace`` on the same filesystem is a rename,
so there is no instant in which the path holds a partial object.

**Deduplicated.** The daemon republishes on every settings reload, and almost
no settings reload touches ``[hud]``. Writing an identical object would move
the mtime, wake the overlay's ``FileView``, and cost a QML re-parse for
nothing — thirty times an hour on a daemon whose config file is being watched.
So the last payload is remembered and an unchanged one writes nothing at all.

**Incapable of failing anything.** Nothing in this module raises. It is called
from daemon start-up and from the settings-listener chain, and a full tmpfs or
a vanished runtime directory is not a reason to refuse to start or to break
every other listener behind it. A write that fails is logged once and then
silently — a broken desktop must not flood the log from the middle of every
reload.

## What is deliberately not here

**The message file.** ``$XDG_RUNTIME_DIR/luna/message`` — the caption itself —
is written by :class:`lunad.ambient.HudWriter`, and stays there. It carries a
monotonically increasing ``id`` that is what makes a message *new* to the
reader, so there must be exactly one writer and one counter in the process;
splitting it across two modules is how two sentences come to share an id and
one of them is never shown. :class:`Caption` below is the speech path's *user*
of that single writer, not a second one.

**A ttl setting.** How long a caption stays up is the pane's contract
(``HANDOFF-hud.md``), and the countdown does not start while
``state`` reads ``speaking``. A number in ``[hud]`` would not mean what it
looked like it meant.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from . import config
from . import settings as settings_mod

log = logging.getLogger("lunad.hud")

#: The six keys, in the order the contract states them. The overlay does not
#: care about order, but a file a human opens in `cat` should read the way the
#: table it came from reads.
FIELDS = ("enabled", "corner", "scale", "idle_visible", "caption", "sprite")


def payload(values: dict[str, Any] | None = None) -> dict[str, Any]:
    """The six settings, normalised into what the overlay is promised.

    ``values`` is a raw ``[hud]`` table; omitted, the live settings are read.

    Everything here is normalised a second time even though
    :meth:`settings.Key.coerce` already refused anything out of range. That is
    not redundancy for its own sake: ``validate()`` falls back to the default
    for a bad value in the *file*, but this function is also handed dicts by
    tests and by anything that grows a second caller later, and the overlay has
    no error state to show. A sprite it does not recognise has to become an
    orb somewhere, and the honest place is the last hop before it is written
    rather than the first hop after it is read.
    """
    if values is None:
        values = settings_mod.settings().section("hud")
    values = values or {}

    def flag(name: str, fallback: bool) -> bool:
        raw = values.get(name, fallback)
        return bool(raw) if isinstance(raw, bool) else fallback

    corner = values.get("corner", config.HUD_CORNER)
    if corner not in config.HUD_CORNERS:
        corner = config.HUD_CORNER

    sprite = values.get("sprite", config.HUD_SPRITE)
    if sprite not in config.HUD_SPRITES:
        sprite = config.HUD_SPRITE

    scale = values.get("scale", config.HUD_SCALE)
    # `bool` first: it is an `int` subclass, so `scale = true` would otherwise
    # sail through as 1.0 and look like a value somebody meant.
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        scale = config.HUD_SCALE
    scale = float(min(max(float(scale), config.HUD_SCALE_MIN),
                      config.HUD_SCALE_MAX))

    return {"enabled": flag("enabled", config.HUD_ENABLED),
            "corner": corner,
            "scale": scale,
            "idle_visible": flag("idle_visible", config.HUD_IDLE_VISIBLE),
            "caption": flag("caption", config.HUD_CAPTION),
            "sprite": sprite}


class HudSettings:
    """Publishes ``hud.json``. Best-effort, always, like :mod:`presence`."""

    def __init__(self, path: Path | str | None = None,
                 settings: Any = None) -> None:
        # Read late, never as a signature default. `tests/_support.py` points
        # `config.HUD_SETTINGS_FILE` at a temporary directory, and a default
        # bound at import would sail past the redirect and rewrite the live
        # desktop's overlay settings from the middle of a test run --
        # `tests/test_guards.py` asserts this shape by `inspect.signature`.
        self.path = (Path(path) if path is not None
                     else config.HUD_SETTINGS_FILE)
        self._tmp = self.path.with_name(self.path.name + ".tmp")
        self._settings = settings
        self._lock = threading.Lock()
        #: The payload believed to be on disk. `None` means "nothing, or we do
        #: not know", which forces the next publish to actually write.
        self._current: dict[str, Any] | None = None
        # Final, exactly as `Presence._closed` is final and for the same
        # measured reason: shutdown runs listeners on its way out, and a clear
        # that could be undone leaves the file behind on every clean stop with
        # the overlay then reading settings for a daemon that has exited.
        self._closed = False
        self._complained = False

    @property
    def settings(self) -> Any:
        return (self._settings if self._settings is not None
                else settings_mod.settings())

    def payload(self) -> dict[str, Any]:
        """What would be written right now."""
        return payload(self.settings.section("hud"))

    def publish(self) -> bool:
        """Write the file if anything changed. Returns whether it was written.

        ``False`` means "nothing to do" far more often than it means "it
        failed", which is why the failure path logs and this return value is
        not treated as one by any caller.
        """
        try:
            want = self.payload()
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            # Reading the settings cannot realistically raise -- `section()`
            # is a dict copy under a lock -- but this is called from the
            # settings-listener chain, and a listener that raises is a
            # listener that stops the ones queued behind it. "Nothing in this
            # module raises" is only true if it is true here too.
            self._complain("could not read the HUD settings", exc)
            return False
        with self._lock:
            if self._closed or want == self._current:
                return False
            self._current = want
            return self._write(want)

    def clear(self) -> bool:
        """Remove the file: the daemon is going away and is not coming back.

        The overlay reads an absent ``hud.json`` as "every default", which is
        deliberately the same thing it reads before lunad has ever run. It does
        not read it as "the daemon has stopped" -- that is what the absence of
        ``state`` says, and it says it for both files.
        """
        with self._lock:
            self._closed = True
            self._current = None
            removed = False
            try:
                self.path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._complain("could not remove the HUD settings", exc)
            try:
                self._tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            return removed

    # -- internals -------------------------------------------------------

    def _write(self, want: dict[str, Any]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._tmp, "w", encoding="utf-8") as fh:
                json.dump(want, fh, ensure_ascii=False)
            os.replace(self._tmp, self.path)
        except OSError as exc:
            self._complain("could not publish the HUD settings", exc)
            # Forget what we think is on disk, so the next reload tries again
            # rather than being deduplicated away against a write that never
            # landed. Same correction as `Presence._write`.
            self._current = None
            return False
        return True

    def _complain(self, what: str, exc: BaseException) -> None:
        if self._complained:
            return
        self._complained = True
        log.warning("%s (further failures will be silent)", what,
                    extra={"path": str(self.path),
                           "detail": f"{type(exc).__name__}: {exc}"})


# =========================================================================
# The caption — the speech path's half of the message file
# =========================================================================


class Caption:
    """What Luna just said, put on the HUD beside the orb.

    Until this existed the message file only ever carried an *ambient* event:
    a coredump, a flat battery, an update that landed. So the one surface built
    to show her words showed everything except her words, and the pane sat
    empty through an entire spoken answer.

    Three constraints, and the third outranks the other two.

    **It shares one writer.** :func:`lunad.ambient.hud` is the process-wide
    :class:`~lunad.ambient.HudWriter`, and its ``id`` counter is what makes a
    message new to the reader. A second writer would start a second sequence,
    hand the pane an id it had already shown, and lose a message with no
    symptom anywhere. So this holds the shared one and tags its writes, which
    is also what lets ambient's shutdown retract its own toast without wiping
    a caption, and lets a ``luna hush`` clear a caption without wiping a
    crash notice.

    **It is a glance surface, not a transcript.** What goes here is the
    *spoken* form — already capped by ``[voice] max_spoken_chars`` and cut at a
    sentence boundary — and one message per utterance, not one per sentence.
    Nothing else in the daemon may write here; log lines and progress belong
    in the log.

    **It may never fail a reply.** Every method swallows everything. A caption
    is decoration on a spoken answer, and the answer is the part that matters:
    a full tmpfs, a vanished runtime directory or a bug in this file must cost
    a missing caption and one log line, never a sentence that was not said.
    The one log line is once per process, not once per sentence, for the same
    reason ``Presence`` complains once — the desktop that broke this write is
    the desktop that will break the next thousand.
    """

    def __init__(self, writer: Any = None, settings: Any = None) -> None:
        self._writer = writer
        self._settings = settings
        self._complained = False

    @property
    def settings(self) -> Any:
        return (self._settings if self._settings is not None
                else settings_mod.settings())

    @property
    def writer(self) -> Any:
        if self._writer is not None:
            return self._writer
        # Imported here, not at module scope. `ambient` is the larger module
        # and importing it lazily keeps `lunad.hud` cheap for the CLI, which
        # imports settings and nothing else; it also keeps the dependency
        # pointing one way at import time, which matters in a package where
        # the *other* direction -- ambient reaching speech -- is forbidden by
        # design and enforced by a test.
        from . import ambient
        return ambient.hud()

    def wanted(self) -> bool:
        """Whether the user has asked for captions at all.

        Both keys, because ``[hud] enabled = false`` means the overlay draws
        nothing: writing a caption for a surface that is switched off would
        leave a file on disk that only matters if the overlay is later
        switched on, at which point it would pop up something she said before.
        """
        try:
            cfg = self.settings.section("hud")
        except Exception:  # noqa: BLE001 - a caption may not fail a reply
            return False
        return (bool(cfg.get("enabled", config.HUD_ENABLED))
                and bool(cfg.get("caption", config.HUD_CAPTION)))

    def said(self, text: str) -> bool:
        """Caption one utterance. Returns whether anything reached disk."""
        if not text or not text.strip():
            return False
        if not self.wanted():
            return False
        return self._guarded(
            lambda: self.writer.write(text, kind="say",
                                      ttl=config.SPEECH_HUD_TTL_S,
                                      owner=config.HUD_OWNER_SPEECH),
            "could not write the spoken caption")

    def clear(self) -> bool:
        """Retract a caption this path put up. Leaves an ambient one alone.

        Called on barge-in, on ``luna hush`` and on shutdown, and unconditional
        on purpose: the settings are not consulted here. Somebody who turns
        captions off mid-sentence wants the one on screen gone, and a clear
        that checked ``wanted()`` first would leave it there forever.
        """
        return self._guarded(
            lambda: self.writer.clear(only=config.HUD_OWNER_SPEECH),
            "could not clear the spoken caption")

    def _guarded(self, action: Any, what: str) -> bool:
        try:
            return bool(action())
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            if not self._complained:
                self._complained = True
                log.warning("%s (further failures will be silent)", what,
                            extra={"detail": f"{type(exc).__name__}: {exc}"})
            return False
