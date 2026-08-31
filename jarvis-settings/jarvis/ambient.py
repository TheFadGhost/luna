"""What the desktop already watches — asked of lunad's own watchers.

The Ambient pane has to say *why* two of its three hooks ship off, and the
honest answer is a fact about this machine rather than an opinion: Omarchy
ships a crash watcher and a battery service that already do those jobs, and
one of them is running right now. lunad knows the answer and exposes it —
``CrashWatcher.desktop_already_watching()`` is a static method built for
exactly this question, two ``stat()``s and no fork — so this module asks it
rather than re-deriving the rule in the GUI. A rule copied into a second
place is a rule that will disagree with itself after the next change.

Everything here is guarded and nothing here raises. lunad is a sibling
package in the same checkout, not a dependency Jarvis installs, and the
settings window must open and be usable on a machine where it cannot be
imported at all. A fact Jarvis cannot establish comes back as ``None`` and
the pane says it could not ask — it never guesses, because the whole point
of the crash group is that the reader can trust what it says about the
service competing with it.

Everything is a read. Nothing here constructs an `Ambient`, starts a thread,
or calls a watcher's `check()`: `check()` is the method that fires
notifications, and a settings window that toasted you for opening a pane
would be its own bug report.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# lunad sits beside jarvis-settings in the same checkout. Appended rather than
# inserted: an installed lunad, if there ever is one, must win over the tree
# this file happens to be sitting in.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The three hook names, which are also the `[ambient]` key names.
HOOKS = ("update", "crash", "battery")

_MOD = None          # lunad.ambient once imported, False once it has failed


def _lunad():
    """Import `lunad.ambient` once, or remember that we cannot.

    The broad except is deliberate. An ImportError is the expected miss, but
    this imports a whole daemon package out of a checkout that is edited
    independently of the GUI, and a settings window that will not open
    because the daemon has a half-finished edit in it is a worse failure than
    a pane that says one fact is unknown.
    """
    global _MOD
    if _MOD is None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.append(str(REPO_ROOT))
        try:
            from lunad import ambient as mod
            _MOD = mod
        except Exception:
            _MOD = False
    return _MOD or None


def _watchers(mod):
    """The daemon's three watcher classes, in the pane's own order."""
    return tuple(cls for cls in (getattr(mod, "UpdateWatcher", None),
                                 getattr(mod, "CrashWatcher", None),
                                 getattr(mod, "BatteryWatcher", None))
                 if cls is not None)


def duplicated_hooks(values: dict) -> tuple[str, ...]:
    """Hooks that are on *on screen* and behind something the desktop runs.

    `Ambient.duplicated_hooks()` asks this of the watchers the daemon booted
    with. The pane has to ask it of the values in the editor instead, so that
    a hook the user has just switched on says so before the file has even
    been written. Only the source of the answer changes: each watcher class
    is still asked its own `desktop_already_watching()`, exactly as the
    daemon's method does, so only lunad decides what counts as a duplicate.

    The master switch is included here where the daemon's version has no
    need of it: `tick()` skips every watcher when `ambient.enabled` is off,
    so with it off nothing is running and nothing can be duplicating.
    """
    mod = _lunad()
    if mod is None or not values.get("ambient.enabled"):
        return ()
    out = []
    for cls in _watchers(mod):
        duplicate = getattr(cls, "desktop_already_watching", None)
        try:
            if values.get(f"ambient.{cls.name}") and callable(duplicate) \
                    and duplicate():
                out.append(cls.name)
        except Exception:
            continue
    return tuple(out)


def facts() -> dict:
    """Live ground for the pane: what she would actually be reading.

    `available` is False when lunad could not be imported at all, and every
    other value is None when that particular question could not be answered.
    The pane must distinguish "no" from "could not ask" — they are different
    sentences.
    """
    out = {
        "available": False,
        "crash_desktop": None,       # omarchy-crash-watch.service would run
        "crash_unit": "omarchy-crash-watch.service",
        "battery_device": None,      # the path she would read, e.g. .../BAT1
        "battery_pct": None,
        "battery_status": None,
        "version_file": None,        # /usr/share/omarchy/version
        "version": None,             # what it says now
        "version_written": None,     # and when it was last written
    }
    mod = _lunad()
    if mod is None:
        return out
    out["available"] = True

    try:
        out["crash_unit"] = mod.config.OMARCHY_CRASH_WATCH_UNIT.name
    except Exception:
        pass
    try:
        # A static method: no instance, no state file, no fork.
        out["crash_desktop"] = bool(
            mod.CrashWatcher.desktop_already_watching())
    except Exception:
        pass

    try:
        # An empty state dict is all a watcher needs to be constructed; it is
        # what it has already seen, and this one is asked nothing about the
        # past. `battery()` and `reading()` are pure reads of sysfs.
        watcher = mod.BatteryWatcher({})
        device = watcher.battery()
        out["battery_device"] = str(device) if device else None
        if device is not None:
            pct, status = watcher.reading()
            out["battery_pct"] = pct
            out["battery_status"] = status
    except Exception:
        pass

    try:
        # snapshot() names the file the update hook watches; its contents are
        # then read here rather than through check(), which would notify.
        path = mod.UpdateWatcher({}).snapshot().get("version_file")
        if path:
            out["version_file"] = str(path)
            stat = Path(path).stat()
            out["version"] = Path(path).read_text(
                encoding="utf-8", errors="replace")[:64].strip()
            out["version_written"] = time.strftime(
                "%d %b %Y", time.localtime(stat.st_mtime))
    except Exception:
        pass
    return out
