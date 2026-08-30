"""What Luna can see of the desktop right now.

Two reads of the same compositor, with two different rules.

**The focused-window line** rides on every ask. Luna lives on this desktop and
the single most common thing the user means by "this" is the window they are
looking at; without the line she has to ask, and asking about a window she
could have been told about is the difference between an assistant and a
prompt. It costs about twenty tokens.

It goes in the **user message**, never the system prompt. The system prompt is
the cacheable prefix and must stay byte-identical between turns — that is what
turns a cache write into a cache read, it is worth roughly 5x on a Luna turn,
and a line that changes every time the user alt-tabs would invalidate it on
every single ask. This is the same mistake tier-2 recall made and the same
cure. See ARCHITECTURE.md §4, "Prompt cost and the cacheable prefix".

**The screenshot** is taken only when a look was actually asked for. Never
ambiently, never "just in case": a resident daemon that photographs the screen
on a timer is a different piece of software from the one the user agreed to
run. The file lands in a throwaway directory and the directory is removed when
the call returns, whether it returned an answer or an exception.

Nothing in here dispatches. `hyprctl -j activewindow` is a *query*; on this
machine (Hyprland 0.56.2, Lua config) `hyprctl dispatch` evaluates its
arguments as Lua, which is a much sharper tool and lives in dispatch.py.

Every function here is failure-tolerant by design. A missing compositor, a
missing `grim`, a hyprctl that hangs — none of them may turn into an ask that
does not get answered. The context line degrades to "" and the ask goes out
without it; only an explicit `luna look`, where the picture *is* the request,
raises.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

log = logging.getLogger("lunad.context")

#: Scopes a look can have. "window" is the default because the focused window
#: is what the user is talking about; "screen" is for "what else is open".
SCOPES = ("window", "screen")


class LookUnavailable(Exception):
    """A screenshot was asked for and could not be taken.

    Raised only on the explicit path (`luna look`), where the picture is the
    request and a silent failure would produce a confident answer about
    nothing. The always-on context line never raises.
    """

    kind = "LookUnavailable"

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": str(self)}


# =========================================================================
# The compositor
# =========================================================================


def _hyprctl(args: list[str], timeout: float) -> str | None:
    """Run one hyprctl query. Returns stdout, or None for any failure at all.

    Read late from ``config`` rather than bound as a default, for the reason
    every outward binary in this package is: a signature default is fixed at
    import and the test scaffolding could not disarm it. See tests/_support.py.
    """
    binary = shutil.which(config.HYPRCTL_BIN) or config.HYPRCTL_BIN
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("hyprctl unavailable", extra={"detail": str(exc)})
        return None
    if proc.returncode != 0:
        log.debug("hyprctl refused", extra={"rc": proc.returncode,
                                            "args": " ".join(args)})
        return None
    return proc.stdout


def focused_window(timeout: float | None = None) -> dict[str, Any] | None:
    """The focused window as Hyprland describes it, or None.

    None covers every way this can go wrong and they are all the same to the
    caller: no compositor, no focused window (the field comes back as an empty
    object when nothing is focused), hyprctl missing, hyprctl slow, output that
    is not JSON.
    """
    out = _hyprctl(["-j", "activewindow"],
                   config.WINDOW_CONTEXT_TIMEOUT_S if timeout is None
                   else timeout)
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        log.debug("activewindow was not JSON")
        return None
    if not isinstance(data, dict) or not data.get("class"):
        # `{}` is what Hyprland returns when nothing has focus. That is a fact
        # about the desktop, not an error, and it is still "no context line".
        return None
    return data


def describe(window: dict[str, Any] | None) -> str:
    """One line naming the focused window, or "".

    App-id, title and class, as asked for. On Wayland Hyprland reports the
    app-id in ``class``; ``initialClass`` is what the surface asked to be
    called before any window rule renamed it, and it is only worth the tokens
    when the two disagree — which on this desktop is how a terminal running an
    agent is told apart from a terminal running a shell.
    """
    if not window:
        return ""
    app_id = str(window.get("class") or "").strip()
    if not app_id:
        return ""
    initial = str(window.get("initialClass") or "").strip()
    title = str(window.get("title") or "").strip().replace("\n", " ")
    if len(title) > 120:
        title = title[:117] + "…"
    workspace = window.get("workspace")
    bits = [app_id]
    if initial and initial != app_id:
        bits.append(f"(initially {initial})")
    if title:
        bits.append(f'— "{title}"')
    if isinstance(workspace, dict) and workspace.get("name"):
        bits.append(f"on workspace {workspace['name']}")
    return " ".join(bits)


def context_line(timeout: float | None = None) -> str:
    """The line that goes into the user message. "" when there is nothing.

    Phrased as a statement of fact rather than an instruction, and it says
    *she is not looking at it* on purpose: knowing which window has focus is
    not the same as being able to read what is in it, and an assistant that
    conflates the two will describe a screen she has never seen.
    """
    described = describe(focused_window(timeout))
    if not described:
        return ""
    return (f"Focused window right now: {described}. "
            f"(That is the window title, not its contents — "
            f"run `{config.LUNA_CLI} look \"<question>\"` to actually see it.)")


# =========================================================================
# The screenshot
# =========================================================================


def _geometry(window: dict[str, Any]) -> str | None:
    """``grim -g`` geometry for one window, or None if it did not say.

    ``grim -g`` wants ``"<x>,<y> <w>x<h>"``. Hyprland gives ``at`` and ``size``
    as two-element lists of logical pixels, which is the same coordinate space.
    """
    at, size = window.get("at"), window.get("size")
    if (not isinstance(at, list) or not isinstance(size, list)
            or len(at) < 2 or len(size) < 2):
        return None
    try:
        x, y, w, h = int(at[0]), int(at[1]), int(size[0]), int(size[1])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return f"{x},{y} {w}x{h}"


def capture(path: Path, scope: str = "window",
            timeout: float | None = None) -> Path:
    """Take one screenshot into ``path``. Raises :class:`LookUnavailable`.

    The focused window by default: it is what the user means, it is a fraction
    of the pixels of a whole screen, and it keeps whatever else is open — a
    password manager, somebody's messages — out of a frame that is about to be
    uploaded. ``scope="screen"`` is opt-in.

    A window whose geometry Hyprland will not report falls back to the whole
    screen rather than failing, because a picture of everything answers the
    question and no picture does not.
    """
    scope = (scope or "window").strip().lower()
    if scope not in SCOPES:
        raise LookUnavailable(
            f"unknown look scope {scope!r}; expected one of {', '.join(SCOPES)}")

    binary = shutil.which(config.GRIM_BIN)
    if binary is None:
        raise LookUnavailable(
            f"{config.GRIM_BIN} is not on PATH, so nothing can take a "
            "screenshot on this desktop. Install grim.")

    argv = [binary]
    if scope == "window":
        window = focused_window()
        geometry = _geometry(window) if window else None
        if geometry:
            argv += ["-g", geometry]
        else:
            log.info("no window geometry; falling back to the whole screen")
    argv.append(str(path))

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, check=False,
            timeout=config.SCREENSHOT_TIMEOUT_S if timeout is None else timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LookUnavailable(
            f"{config.GRIM_BIN} did not finish in time") from exc
    except OSError as exc:
        raise LookUnavailable(f"could not run {config.GRIM_BIN}: {exc}") from exc

    if proc.returncode != 0:
        raise LookUnavailable(
            f"{config.GRIM_BIN} exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[-300:] or '(no output)'}")
    if not path.is_file() or path.stat().st_size == 0:
        raise LookUnavailable(
            f"{config.GRIM_BIN} exited 0 but wrote no image to {path}")
    return path


@contextmanager
def look(scope: str = "window", timeout: float | None = None) -> Iterator[Path]:
    """Yield a screenshot, and delete it when the block ends — always.

    The directory is removed in a ``finally``, so an agent call that raised
    mid-flight leaves no picture of the user's screen behind on disk. That is
    the whole reason this is a context manager and not a function returning a
    path: a path returned is a path somebody forgets to unlink.
    """
    tmp = Path(tempfile.mkdtemp(prefix="luna-look-"))
    try:
        yield capture(tmp / "screen.png", scope=scope, timeout=timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def available() -> tuple[bool, str]:
    """Whether a look could be taken at all, for `luna status`."""
    binary = shutil.which(config.GRIM_BIN)
    if binary is None:
        return False, f"{config.GRIM_BIN} is not on PATH"
    return True, binary
