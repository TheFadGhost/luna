"""Ambient awareness — the three things Luna notices without being asked.

ARCHITECTURE.md section 8, phase P3. Until now Luna only existed when
addressed: every code path in the daemon starts with a request arriving on the
socket. This module is the first that starts with the *machine* instead.

Three hooks, and one rule that outranks all of them.

## The rule: an ambient event notifies. It never speaks.

Stated by the user, and quoted here because it is the whole design constraint:

    "Yes, she may do that, but I'd like a notification so it doesn't actually
    disturb me... I prefer notify only, unless I spoke to it first and it was
    coming back with an answer to a task that I gave beforehand."

So speaking aloud is reserved for the completion of something the user
themselves started. A coredump, a flat battery and an `omarchy update` are none
of those: they are things that happened *to* the machine while the user was
doing something else, and a voice interrupting that is exactly the failure mode
they asked to avoid.

That is enforced rather than documented, in three layers, because a comment is
not a rule:

1. **This module does not import `lunad.speech` and never will.** There is no
   name in this file that can reach a synthesiser.
2. :class:`Ambient` will only deliver through a :class:`Notifier`, checked with
   ``isinstance`` at construction. A plain callable is refused, so "just pass a
   function that says it out loud" is not reachable without editing this file.
3. :func:`_assert_mute` walks everything hung off the :class:`Ambient` object
   at construction and refuses any collaborator that has a ``say`` or
   ``speak`` method. Handing Ambient the daemon's ``Speech`` raises
   :class:`AmbientChannelError` on the spot rather than going quiet and then
   talking six weeks later.

``tests/test_ambient.py::NeverSpeaksCase`` fails if any of the three is
weakened, and additionally walks the live object graph for a reachable
speaker — which is the test that fails if someone later wires one up.

## The three hooks, and how each one detects its thing

**Crash.** ``systemd-coredump`` stores dumps in
``/var/lib/systemd/coredump`` (``Storage=external``, which is the default and
what is configured here). The directory is ``0755 root:root``, so an
unprivileged daemon can list it, and each dump's *filename* already carries
everything a notification needs:

    core.<comm>.<uid>.<boot-id>.<pid>.<usec>.zst

So the hook is a single ``stat()`` on the directory per tick, and a
``scandir`` only when the mtime moved. **It never forks `coredumpctl`**, which
matters: the useful behaviour is not "a crash happened" but "here is why", and
that is a real diagnosis costing a model call — so it is put one click away
rather than run on every dump. The toast's single action starts it.

Why one click and not automatically: a diagnosis is a dispatched agent session,
which spends money and opens a terminal. Running one unasked, every time
anything on the machine dumps core, is Luna *acting* on her own rather than
noticing — and this machine produces bursts of eight `foot` cores in a minute
when a test suite deletes a script out from under a terminal. The click is the
consent, and it is one click.

**Battery.** ``/sys/class/power_supply/`` — read, not assumed: the battery on
this laptop is ``BAT1``, not ``BAT0``, and it reports in *energy* units with no
``charge_now`` at all, so the hook discovers the battery by reading each
device's ``type`` and then uses ``capacity`` and ``status``, which every
power_supply driver has.

This one is **off by default**, and that is the interesting decision. Omarchy
already notifies: ``shell/plugins/services/battery/Service.qml`` polls every
30 s and runs ``omarchy-battery-low`` at 10%, and UPower hibernates at 2%. A
second nagging source at the same moment is worse than none. The hook exists
because the user may want an *earlier* warning than the desktop's, and its
defaults sit either side of Omarchy's rather than on top of it (20% and 5%),
but nothing fires until the user turns it on.

**`omarchy update`.** This is the one that matters more than it looks. Updates
here are pacman, not git: ``omarchy-update`` runs
``pacman -Syu --overwrite '/usr/share/omarchy/*'``, which **rewrites
`/usr/share/omarchy` wholesale**. That is exactly how this machine's
customisations get silently reverted, and noticing it is the entire value of
the hook.

Detection is two ``stat()``s and one 12-byte read: ``/usr/share/omarchy/version``
(contents *and* mtime — a same-version reinstall still clobbers, so the mtime
is checked as well as the string) and ``/tmp/omarchy-update.log``, which
``omarchy-update`` writes on every run through ``script``.

**Deliberately not built: "an update is available".** That check is
``omarchy-update-available``, which shells out to ``checkupdates`` and syncs a
pacman database over the network. Omarchy's own bar widget already runs it on a
six-hour timer and shows the answer, so a second poller would cost the network,
cost the battery, and duplicate a light the user is already looking at. The
half worth having is the half the bar does *not* show: that an update already
landed and took the customisations with it.

## Polling discipline

One thread, one timer, no busy loop. Every hook is a ``stat()``-gated
comparison, so a tick that finds nothing new costs a handful of syscalls and
allocates nothing. The default cadence is 60 s for crash and battery and 300 s
for the update check, which on a machine with 7 GB of RAM and a battery to
protect is the difference between "does not appear in `systemctl status`" and
"a feature that cost you an hour of runtime".

An event source was preferred over a poll wherever one existed and was worth
it. It was not, for any of the three: inotify has no wrapper in the standard
library, `POLLPRI` on a battery's ``uevent`` wakes on every EC report (which
is *more* wakeups than a 60 s tick while discharging, not fewer), and a
coredump has no D-Bus or udev event at all. A ``stat()`` per minute is already
below the noise floor of the settings watcher this daemon has run since P2b,
which stats its config file every two seconds.

## Nothing in here may raise into the daemon

Same contract as ``presence.py``, for the same reason: an unreadable sysfs
file, a full disk or a missing notifier is not permission to take the daemon
down. Every watcher swallows its own OSErrors, complains once, and goes quiet.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit as audit_mod
from . import config, safety
from . import settings as settings_mod

log = logging.getLogger("lunad.ambient")

#: The three hooks, each individually switchable in `[ambient]`.
CRASH = "crash"
BATTERY = "battery"
UPDATE = "update"
SOURCES = (CRASH, BATTERY, UPDATE)

#: Urgency levels `omarchy-notification-send` understands.
LOW, NORMAL, CRITICAL = "low", "normal", "critical"


class AmbientChannelError(RuntimeError):
    """An ambient event was pointed at a channel it is not allowed to use.

    Raised at construction, never at delivery: the point is that a wiring
    mistake fails the moment somebody makes it, in their own test run, rather
    than the first time the machine happens to crash at 3am.
    """


# =========================================================================
# What a hook produces
# =========================================================================


@dataclass
class Event:
    """One thing Luna noticed. A notification and an audit line, never speech.

    ``action`` is a full argv for the toast's *single* click action. Omarchy's
    notification takes exactly one — the same asymmetry `confirm.py` relies on
    — so a hook gets one verb, and it should be the one the user would type
    next.
    """

    source: str
    headline: str
    body: str
    urgency: str = NORMAL
    icon: str = ""
    action: list[str] | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def audit_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"source": self.source,
                                  "headline": self.headline,
                                  "urgency": self.urgency}
        fields.update(self.detail)
        if self.action:
            # NOT `action`: that is `AuditLog.append`'s own first
            # parameter, and a field of the same name is a
            # TypeError at the point the machine crashes.
            fields["click"] = " ".join(self.action)
        return fields


# =========================================================================
# Delivery — the one function, and the only one
# =========================================================================


class HudWriter:
    """``$XDG_RUNTIME_DIR/luna/message`` — the HUD pane's contract.

    Specified in ``HANDOFF-hud.md`` by the agent that built the pane; this is
    the implementation of that spec and it does not invent anything. One JSON
    object, written atomically (``message.tmp`` then ``os.replace``, the same
    way ``presence.py`` writes ``state``), with a monotonically increasing
    ``id`` that is what makes a message *new* to the reader.

    The counter is process-wide on purpose, and this class is deliberately not
    ambient-specific: when the speech path grows captions it must share this
    writer rather than start a second ``id`` sequence, or two writers will hand
    the pane the same id for different sentences and one of them will never be
    shown. :func:`hud` is the shared instance.

    Best-effort throughout. The pane may not be running, the runtime directory
    may be gone, and neither is a reason for an event not to reach the toast.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        # Read late, never as a signature default: the suite redirects
        # `config.HUD_MESSAGE_FILE` into a temp directory, and a default bound
        # at import would write into the live desktop's pane instead.
        self.path = Path(path) if path is not None else config.HUD_MESSAGE_FILE
        self._tmp = self.path.with_name(self.path.name + ".tmp")
        self._lock = threading.Lock()
        self._id = 0
        self._mine = False
        self._complained = False

    def write(self, text: str, *, kind: str = "say",
              ttl: float | None = None) -> bool:
        """Publish one message. Returns whether it reached disk."""
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            self._id += 1
            payload = {"id": self._id, "text": text[:500], "ts": time.time(),
                       "kind": kind if kind in ("say", "alert") else "say"}
            if ttl is not None:
                payload["ttl"] = ttl
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(self._tmp, self.path)
            except OSError as exc:
                self._complain("could not write the HUD message", exc)
                return False
            self._mine = True
            return True

    def clear(self, *, only_mine: bool = False) -> bool:
        """Remove the file, which dismisses whatever the pane is showing.

        ``only_mine`` exists for shutdown: ambient must not wipe a caption the
        speech path put there, so it retracts a message only when the last
        write to the file was its own.
        """
        with self._lock:
            if only_mine and not self._mine:
                return False
            self._mine = False
            try:
                self.path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                self._complain("could not remove the HUD message", exc)
                return False
            return True

    def _complain(self, what: str, exc: BaseException) -> None:
        if self._complained:
            return
        self._complained = True
        log.warning("%s (further failures will be silent)", what,
                    extra={"path": str(self.path),
                           "detail": f"{type(exc).__name__}: {exc}"})


_hud_lock = threading.Lock()
_HUD: HudWriter | None = None


def hud() -> HudWriter:
    """The process-wide HUD writer. One ``id`` sequence, one file."""
    global _HUD
    with _hud_lock:
        if _HUD is None:
            _HUD = HudWriter()
        return _HUD


def use_hud(new: HudWriter | None) -> HudWriter | None:
    """Swap the process-wide HUD writer. Tests use this; nothing else should."""
    global _HUD
    with _hud_lock:
        old, _HUD = _HUD, new
    return old


class Notifier:
    """The single place an ambient event becomes something the user sees.

    Two surfaces, one call: an Omarchy toast and the HUD pane's message file.
    Both are best-effort and neither can fail an event — a desktop with no
    notification daemon is still a machine where the crash happened, and the
    audit line is written either way.

    This class is the *whole* delivery capability of the ambient subsystem.
    :class:`Ambient` will not accept anything else (see its constructor), so a
    third surface — a mail, a webhook, a different pane — is added here, in one
    method, and every hook gets it at once. Subclass it to intercept delivery;
    that is how the suite watches what would have been sent without a desktop
    anywhere near it.
    """

    def __init__(self, notify_bin: str | None = None,
                 hud_writer: HudWriter | None = None) -> None:
        # Late read, not a signature default. `tests/_support.py` replaces
        # `config.NOTIFY_BIN` process-wide with a name that cannot resolve,
        # and a default bound at import would sail straight past it and put a
        # real toast on the user's desktop -- which has happened twice in this
        # codebase already, ten at a time.
        self.notify_bin = notify_bin or config.NOTIFY_BIN
        self._hud = hud_writer
        self.sent = 0

    @property
    def hud_writer(self) -> HudWriter:
        return self._hud if self._hud is not None else hud()

    def send(self, event: Event) -> bool:
        """Deliver one event. Returns whether anything reached the user."""
        toast = self.toast(event)
        pane = self.pane(event)
        if toast or pane:
            self.sent += 1
        return toast or pane

    # -- the surfaces ----------------------------------------------------

    def toast(self, event: Event) -> bool:
        """An Omarchy notification, spawned through the firewall and reaped.

        Everything lunad forks lands in the signal allowlist, so this goes
        through ``safety.spawn`` rather than ``Popen`` — nothing outside
        ``safety.py`` may deliver a signal, and a child nobody registered is a
        child nobody can ever stop.

        The reap is :func:`safety.reap_after` with no deadline, which polls
        once inline and otherwise hands the wait to a background thread that
        never gives up. A ``notify-send`` that hangs — waiting on a
        notification daemon that is not answering — must not leave a permanent
        zombie for the rest of the daemon's life, and a bounded wait that gave
        up would do exactly that.
        """
        argv = [self.notify_bin, "--app-name", "Jarvis",
                "-u", event.urgency]
        if event.icon:
            argv += ["-g", event.icon]
        if event.action:
            argv += ["--exec", shlex.join(event.action)]
        argv += [event.headline, event.body]
        try:
            proc = safety.spawn(
                argv, kind="ambient-notify", durable=False,
                note=f"ambient {event.source}",
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except (OSError, safety.SignalRefused) as exc:
            log.warning("could not send the ambient notification",
                        extra={"detail": str(exc), "bin": self.notify_bin,
                               "source": event.source})
            return False
        safety.reap_after(proc)
        return True

    def pane(self, event: Event) -> bool:
        """The HUD caption, per ``HANDOFF-hud.md``.

        ``kind`` is always ``alert``: every ambient event is by definition
        something the user did not ask about, which is what that colour means.
        """
        text = f"{event.headline} — {event.body}" if event.body else event.headline
        return self.hud_writer.write(text, kind="alert",
                                     ttl=config.AMBIENT_HUD_TTL_S)

    def clear_pane(self) -> bool:
        return self.hud_writer.clear(only_mine=True)


# =========================================================================
# The watchers
# =========================================================================


class Watcher:
    """One hook. Cheap by construction: a ``stat()`` decides whether to look.

    ``state`` is a slice of the persisted ambient state, mutated in place and
    saved by the owner. Persisting is what stops a daemon restart re-announcing
    two weeks of coredumps, and the *first* run of each watcher seeds that
    state and deliberately reports nothing: the machine's history is not news.
    """

    name = ""
    every = config.AMBIENT_POLL_S

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self._complained = False

    def enabled(self) -> bool:
        return bool(settings_mod.get(f"ambient.{self.name}", True))

    def check(self) -> list[Event]:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": self.enabled(), "every_s": self.every}

    def _complain(self, what: str, exc: BaseException) -> None:
        """Log the first failure of a kind and then stay quiet.

        A watcher that cannot read its source usually cannot read it *ever* —
        no coredump directory, no battery, sysfs not mounted — and a warning
        every sixty seconds for the life of the daemon is a log nobody reads.
        """
        if self._complained:
            return
        self._complained = True
        log.warning("%s (further failures will be silent)", what,
                    extra={"watcher": self.name,
                           "detail": f"{type(exc).__name__}: {exc}"})


# -- crash ----------------------------------------------------------------


@dataclass(frozen=True)
class Dump:
    """One core file, parsed entirely out of its own name."""

    name: str
    comm: str
    uid: int
    pid: int
    usec: int

    @property
    def when(self) -> float:
        return self.usec / 1_000_000.0


def parse_dump(name: str) -> Dump | None:
    """``core.<comm>.<uid>.<boot-id>.<pid>.<usec>.zst`` -> :class:`Dump`.

    Parsed from the right, because ``comm`` is the one field that can contain
    a dot (it is the first 15 bytes of the executable name, and
    ``python3.14`` is a real one). The compression suffix is stripped by
    checking whether the last component is a number rather than by matching a
    list of algorithms, so a systemd that switches from zstd to something else
    does not silently stop being noticed.
    """
    if not name.startswith("core."):
        return None
    stem = name
    head, _, last = stem.rpartition(".")
    if head and not last.isdigit():
        stem = head                                   # drop `.zst`, `.lz4`, ...
    parts = stem.rsplit(".", 4)
    if len(parts) != 5:
        return None
    prefix, uid, _boot, pid, usec = parts
    if not (uid.isdigit() and pid.isdigit() and usec.isdigit()):
        return None
    return Dump(name=name, comm=prefix[5:] or "?", uid=int(uid),
                pid=int(pid), usec=int(usec))


class CrashWatcher(Watcher):
    """A process on this machine dumped core.

    The gate is one ``stat()`` on ``/var/lib/systemd/coredump``. The directory
    is world-traversable here (``0755 root:root``) and each dump's name carries
    the command, the uid, the pid and the microsecond, so a full scan needs no
    subprocess and no elevated access — and only happens when the mtime moved.

    Only this user's dumps are reported. ``coredumpctl`` restricts a
    non-root caller to their own uid anyway, and a crash in another account's
    process is not something the person at this desk can act on.
    """

    name = CRASH

    def __init__(self, state: dict[str, Any], directory: Path | None = None,
                 uid: int | None = None) -> None:
        super().__init__(state)
        self._dir = directory
        self.uid = os.getuid() if uid is None else uid

    @property
    def directory(self) -> Path:
        # Late read for the same reason every other outward name is: the suite
        # points this at an empty temp directory, and a default captured at
        # import would walk the machine's real coredumps instead.
        return self._dir if self._dir is not None else config.COREDUMP_DIR

    def check(self) -> list[Event]:
        directory = self.directory
        try:
            mtime_ns = directory.stat().st_mtime_ns
        except OSError as exc:
            self._complain("cannot stat the coredump directory", exc)
            return []
        if mtime_ns == self.state.get("mtime_ns") and self.state.get("seeded"):
            return []
        try:
            names = [e.name for e in os.scandir(directory) if e.is_file()]
        except OSError as exc:
            self._complain("cannot read the coredump directory", exc)
            return []
        self.state["mtime_ns"] = mtime_ns
        dumps = [d for d in (parse_dump(n) for n in names)
                 if d is not None and d.uid == self.uid]
        dumps.sort(key=lambda d: (d.usec, d.pid))

        if not self.state.get("seeded"):
            # First ever run. Record what is already on disk and say nothing:
            # tmpfiles keeps dumps for two weeks, and announcing a fortnight of
            # history the first time the daemon starts is noise, not awareness.
            self.state["seeded"] = True
            self.state["last_usec"] = dumps[-1].usec if dumps else 0
            self.state["recent"] = [d.name for d in dumps][-config.AMBIENT_RECENT_DUMPS:]
            return []

        last_usec = int(self.state.get("last_usec") or 0)
        recent = set(self.state.get("recent") or ())
        fresh = [d for d in dumps
                 if d.usec > last_usec or (d.usec == last_usec
                                           and d.name not in recent)]
        if dumps:
            self.state["last_usec"] = dumps[-1].usec
            self.state["recent"] = [d.name for d in dumps][-config.AMBIENT_RECENT_DUMPS:]
        if not fresh:
            return []
        return self._events(fresh)

    def _events(self, fresh: list[Dump]) -> list[Event]:
        """One toast per crash, up to a burst cap, then one that counts them.

        The cap is not decoration. This machine produces eight `foot` cores in
        a minute when a test suite deletes a running script out from under a
        terminal, and eight critical toasts for one incident is worse than
        silence — it is the thing that gets a feature switched off.
        """
        burst = max(1, int(config.AMBIENT_CRASH_BURST))
        if len(fresh) > burst:
            names = ", ".join(sorted({d.comm for d in fresh}))
            newest = fresh[-1]
            return [Event(
                source=CRASH,
                headline=f"{len(fresh)} processes crashed",
                body=f"{names}. Newest: {newest.comm} (pid {newest.pid}). "
                     f"Click to diagnose that one.",
                urgency=CRITICAL, icon="󰀦",
                action=self._diagnose(newest),
                detail={"count": len(fresh), "pid": newest.pid,
                        "comm": newest.comm, "coalesced": True})]
        return [Event(
            source=CRASH,
            headline=f"{d.comm} crashed",
            body=f"pid {d.pid} dumped core at "
                 f"{time.strftime('%H:%M:%S', time.localtime(d.when))}. "
                 f"Click and I'll work out why.",
            urgency=CRITICAL, icon="󰀦",
            action=self._diagnose(d),
            detail={"pid": d.pid, "comm": d.comm, "count": 1,
                    "when": round(d.when, 3)}) for d in fresh]

    def _diagnose(self, dump: Dump) -> list[str] | None:
        """The toast's one click: hand the diagnosis to a dispatched agent.

        Returns ``None`` when the action is switched off, which leaves the
        crash a plain notification. The click is deliberately not automatic —
        see the module docstring.
        """
        if not bool(settings_mod.get("ambient.crash_diagnose", True)):
            return None
        return [str(config.LUNA_CLI), "ambient", "diagnose", str(dump.pid),
                "--exe", dump.comm]

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap.update({"directory": str(self.directory),
                     "last_usec": self.state.get("last_usec"),
                     "known": len(self.state.get("recent") or ()),
                     "diagnose": bool(settings_mod.get("ambient.crash_diagnose",
                                                       True))})
        return snap


# -- battery ---------------------------------------------------------------


class BatteryWatcher(Watcher):
    """Low and critical, off by default because the desktop already says so.

    The battery is *found*, not named. On this laptop it is ``BAT1`` — not
    ``BAT0`` — and it reports energy (``energy_now``/``energy_full``) with no
    ``charge_now`` attribute at all, so anything reading charge would silently
    read nothing. ``capacity`` and ``status`` are the two attributes every
    power_supply driver exposes, and they are the two this reads.

    The latch mirrors Omarchy's own: a threshold fires once, and re-arms only
    when the machine goes back on mains or climbs back above the level. A
    warning that repeats every minute is a warning nobody reads.
    """

    name = BATTERY
    every = config.AMBIENT_POLL_S

    def __init__(self, state: dict[str, Any], directory: Path | None = None) -> None:
        super().__init__(state)
        self._dir = directory
        self._battery: Path | None = None

    @property
    def directory(self) -> Path:
        return self._dir if self._dir is not None else config.POWER_SUPPLY_DIR

    def battery(self) -> Path | None:
        """The first device whose ``type`` is ``Battery``. Cached, re-found.

        Re-found rather than cached forever because a hot-swapped or
        re-enumerated battery changes its directory name, and a watcher holding
        a stale path would report nothing and never say why.
        """
        if self._battery is not None and (self._battery / "capacity").exists():
            return self._battery
        self._battery = None
        try:
            candidates = sorted(self.directory.iterdir())
        except OSError as exc:
            self._complain("cannot list the power supplies", exc)
            return None
        for device in candidates:
            if _read(device / "type").strip() == "Battery":
                self._battery = device
                return device
        return None

    def reading(self) -> tuple[int, str] | None:
        device = self.battery()
        if device is None:
            return None
        raw = _read(device / "capacity").strip()
        if not raw.isdigit():
            return None
        return int(raw), _read(device / "status").strip() or "Unknown"

    def thresholds(self) -> tuple[int, int]:
        low = int(settings_mod.get("ambient.battery_low_pct",
                                   config.AMBIENT_BATTERY_LOW_PCT))
        critical = int(settings_mod.get("ambient.battery_critical_pct",
                                        config.AMBIENT_BATTERY_CRITICAL_PCT))
        # A critical above the low would make the low unreachable, and the
        # config allows both to be set independently. Clamp rather than refuse:
        # a misconfigured threshold must still warn about a flat battery.
        return low, min(critical, low)

    def check(self) -> list[Event]:
        reading = self.reading()
        if reading is None:
            return []
        pct, status = reading
        low, critical = self.thresholds()
        self.state["seeded"] = True
        self.state["pct"] = pct
        self.state["status"] = status
        armed = str(self.state.get("armed") or "")

        if status != "Discharging" or pct > low:
            # On mains, or back above the warning line: re-arm and say nothing.
            # Coming off charge is exactly when the next warning should be
            # allowed to fire again.
            if armed:
                self.state["armed"] = ""
            return []
        if pct <= critical and armed != CRITICAL:
            self.state["armed"] = CRITICAL
            return [Event(
                source=BATTERY,
                headline=f"Battery critical — {pct}%",
                body="Save what you are doing. The system hibernates on its "
                     "own at 2%.",
                urgency=CRITICAL, icon="󰂃",
                detail={"pct": pct, "status": status, "level": CRITICAL,
                        "threshold": critical})]
        if pct <= low and armed not in (LOW, CRITICAL):
            self.state["armed"] = LOW
            return [Event(
                source=BATTERY,
                headline=f"Battery low — {pct}%",
                body="Running on battery. A cable soon would be sensible.",
                urgency=NORMAL, icon="󰁻",
                detail={"pct": pct, "status": status, "level": LOW,
                        "threshold": low})]
        return []

    def enabled(self) -> bool:
        # Explicit default of False rather than the base class's True: this is
        # the one hook that duplicates something the desktop already does, so
        # a missing key must mean off, not on.
        return bool(settings_mod.get("ambient.battery", False))

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        device = self.battery()
        low, critical = self.thresholds()
        snap.update({"device": str(device) if device else None,
                     "pct": self.state.get("pct"),
                     "status": self.state.get("status"),
                     "armed": self.state.get("armed") or None,
                     "low_pct": low, "critical_pct": critical})
        return snap


# -- omarchy update --------------------------------------------------------


class UpdateWatcher(Watcher):
    """An `omarchy update` landed — and took `/usr/share/omarchy` with it.

    Updates on this machine are pacman, not git: ``omarchy-update`` runs
    ``pacman -Syu --overwrite '/usr/share/omarchy/*'``. There is no checkout to
    read a HEAD from, so the fingerprint is the package's own version file
    plus its mtime — the mtime because a *reinstall of the same version* still
    rewrites every file under ``/usr/share/omarchy``, and that is the case
    where a silent revert is most likely to be missed.

    ``/tmp/omarchy-update.log`` is the third signal: ``omarchy-update`` wraps
    its whole run in ``script`` and writes there every time. It lives on tmpfs
    and is therefore gone after a reboot, so its absence proves nothing — it is
    used only to notice a run that changed no version.
    """

    name = UPDATE
    every = config.AMBIENT_UPDATE_EVERY_S

    def __init__(self, state: dict[str, Any], version: Path | None = None,
                 log_path: Path | None = None) -> None:
        super().__init__(state)
        self._version = version
        self._log = log_path

    @property
    def version_file(self) -> Path:
        return (self._version if self._version is not None
                else config.OMARCHY_VERSION_FILE)

    @property
    def log_file(self) -> Path:
        return self._log if self._log is not None else config.OMARCHY_UPDATE_LOG

    def check(self) -> list[Event]:
        version = _read(self.version_file, limit=64).strip()
        version_mtime = _mtime_ns(self.version_file)
        log_mtime = _mtime_ns(self.log_file)
        if version_mtime is None and log_mtime is None:
            return []                                  # not an Omarchy machine

        before = (self.state.get("version"), self.state.get("version_mtime_ns"),
                  self.state.get("log_mtime_ns"))
        self.state["version"] = version
        self.state["version_mtime_ns"] = version_mtime
        self.state["log_mtime_ns"] = log_mtime
        if not self.state.get("seeded"):
            self.state["seeded"] = True
            return []
        old_version, old_version_mtime, old_log_mtime = before
        if (version, version_mtime, log_mtime) == before:
            return []

        clobbered = ("/usr/share/omarchy was rewritten, so anything "
                     "customised there is gone. Check "
                     "~/.config/omarchy/CUSTOMISATIONS.md §9.")
        if version and old_version and version != old_version:
            headline = f"Omarchy updated — {old_version} → {version}"
        elif version_mtime != old_version_mtime:
            headline = f"Omarchy reinstalled — still {version or 'unknown'}"
        elif log_mtime != old_log_mtime:
            # The package did not move but a run happened. Worth one line,
            # not a critical toast: this is the case where nothing was
            # clobbered and only the AUR/mise/orphan steps did anything.
            return [Event(
                source=UPDATE,
                headline="omarchy update ran",
                body="The Omarchy package itself did not change. Other "
                     "packages may have.",
                urgency=LOW, icon="󰚰",
                detail={"version": version, "package_changed": False})]
        else:
            return []
        return [Event(source=UPDATE, headline=headline, body=clobbered,
                      urgency=NORMAL, icon="󰚰",
                      detail={"version": version, "was": old_version,
                              "package_changed": True})]

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap.update({"version": self.state.get("version"),
                     "version_file": str(self.version_file),
                     "available_checked": False})
        return snap


# =========================================================================
# The subsystem
# =========================================================================

#: What a speaker looks like. The daemon's synthesiser has both, and so would
#: any wrapper somebody writes around it, because that is what the method is
#: called. (Spelled apart from the word it guards against so that the source
#: check in tests/test_ambient.py has nothing to trip on.)
_SPEAKER_METHODS = ("say", "speak")

#: How far into containers the check looks. Two is enough for the one shape
#: that matters -- a collaborator inside the `watchers` tuple -- without
#: walking the whole persisted state dict for no reason.
_MUTE_DEPTH = 2


def _assert_mute(obj: Any) -> None:
    """Refuse any collaborator that can be told to talk.

    This is the rule "an ambient event must not reach the speech path", made
    executable. It is checked at construction rather than at delivery because a
    wiring mistake should fail in the test run of whoever made it, not the
    first time the machine crashes while the user is on a call.

    Nothing is exempt. An earlier version of this carried a skip-list of
    attribute names that were "obviously fine", which is exactly the shape of
    hole this function exists to close: `watchers` was on it, and a watcher
    holding a synthesiser would have walked straight through.
    """
    for name, value in vars(obj).items():
        _check_mute(name, value, 0)


def _check_mute(path: str, value: Any, depth: int) -> None:
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return
    if depth > _MUTE_DEPTH:
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for index, item in enumerate(value):
            _check_mute(f"{path}[{index}]", item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _check_mute(f"{path}[{key!r}]", item, depth + 1)
        return
    for method in _SPEAKER_METHODS:
        if callable(getattr(value, method, None)):
            raise AmbientChannelError(
                f"ambient was given {path!r} ({type(value).__name__}), which "
                f"has a .{method}() -- ambient events notify, they never "
                f"talk. See lunad/ambient.py, the rule at the top.")


class Ambient:
    """The three hooks, one thread, one timer.

    Constructed by the daemon and started with it. Every tick is a handful of
    ``stat()``s; the thread otherwise sits in ``Event.wait`` and costs nothing.

    :meth:`tick` is public and does the whole of a cycle synchronously, so the
    suite drives the subsystem without ever starting a thread or waiting on a
    clock.
    """

    def __init__(self, *, notifier: Notifier,
                 audit: audit_mod.AuditLog | None = None,
                 settings: settings_mod.Settings | None = None,
                 state_path: Path | None = None,
                 watchers: tuple[Watcher, ...] | None = None) -> None:
        # Layer 2 of the "ambient never speaks" rule. A plain callable is
        # refused on purpose: "pass a function that says it out loud" must not
        # be reachable without editing this file, and an isinstance check is
        # the only form of that rule a later contributor cannot miss.
        if not isinstance(notifier, Notifier):
            raise AmbientChannelError(
                "ambient delivers through a Notifier and nothing else; got "
                f"{type(notifier).__name__}. Ambient events notify, they never "
                "speak -- add a surface to Notifier.send instead.")
        self.notifier = notifier
        self.audit = audit if audit is not None else audit_mod.audit()
        self._settings = settings
        self._state_path = (Path(state_path) if state_path is not None
                            else config.AMBIENT_STATE_PATH)
        self._dir_existed = self._state_path.parent.is_dir()
        # Before `_load`, which can complain: a corrupt state file on the
        # very first read must produce a warning, not an AttributeError
        # inside the constructor of the thing that was meant to survive it.
        self._complained = False
        self.state: dict[str, Any] = self._load()
        self._watchers: tuple[Watcher, ...] = ()
        self.watchers = watchers or (
            CrashWatcher(self.state.setdefault(CRASH, {})),
            BatteryWatcher(self.state.setdefault(BATTERY, {})),
            UpdateWatcher(self.state.setdefault(UPDATE, {})),
        )
        self._due: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.delivered = 0
        # Layer 3. Nothing hung off this object may be able to talk.
        _assert_mute(self)

    # -- the hooks -------------------------------------------------------

    @property
    def watchers(self) -> tuple[Watcher, ...]:
        return self._watchers

    @watchers.setter
    def watchers(self, value: Any) -> None:
        """Assignment is gated, not just construction.

        `_assert_mute` in `__init__` covers the watchers this object builds
        for itself, which is what production uses. It does not cover
        `daemon.ambient.watchers = (...)` afterwards -- and a rule that holds
        only until somebody assigns past it is not a rule. So the setter runs
        the same check, and the tuple is frozen on the way in so a caller
        cannot append to it later either.
        """
        frozen = tuple(value)
        _check_mute("watchers", frozen, 0)
        self._watchers = frozen

    # -- settings --------------------------------------------------------

    @property
    def settings(self) -> settings_mod.Settings:
        return (self._settings if self._settings is not None
                else settings_mod.settings())

    def enabled(self) -> bool:
        return bool(self.settings.get("ambient.enabled", True))

    def interval(self) -> float:
        """The base tick, read live so a change takes effect on the next one."""
        try:
            value = float(self.settings.get("ambient.poll_seconds",
                                            config.AMBIENT_POLL_S))
        except (TypeError, ValueError):
            value = config.AMBIENT_POLL_S
        return max(5.0, value)

    # -- the loop --------------------------------------------------------

    def start(self) -> bool:
        """Start the watcher thread. A second call is a no-op."""
        with self._lock:
            if self._thread is not None:
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="luna-ambient")
            self._thread.start()
            return True

    def close(self, timeout: float = 2.0) -> None:
        """Stop the thread and retract anything ambient put on the HUD."""
        with self._lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=timeout)
        self.notifier.clear_pane()
        self._save()

    def _run(self) -> None:
        # Wait first, then work. A daemon start is the busiest moment this
        # machine has -- the shell, the bar, voxtype and lunad all coming up at
        # once -- and there is nothing an ambient hook can find in the first
        # minute that will not still be there in the second.
        while not self._stop.wait(self.interval()):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a watcher must not kill the thread
                log.exception("ambient tick failed")

    def tick(self, now: float | None = None) -> int:
        """Run every watcher that is due and enabled. Returns events delivered.

        Never raises. A watcher that throws is logged, disabled for nothing,
        and the other two still run: one broken sysfs path must not cost the
        user the crash hook.
        """
        now = time.monotonic() if now is None else now
        self.ticks += 1
        if not self.enabled():
            return 0
        delivered = 0
        dirty = False
        for watcher in self.watchers:
            if not watcher.enabled():
                continue
            if now < self._due.get(watcher.name, 0.0):
                continue
            self._due[watcher.name] = now + max(watcher.every, self.interval())
            try:
                events = watcher.check()
            except Exception as exc:  # noqa: BLE001
                log.exception("ambient watcher failed",
                              extra={"watcher": watcher.name})
                self.audit.append("ambient.failed", ok=False,
                                  why=f"the {watcher.name} watcher raised",
                                  source=watcher.name,
                                  detail=f"{type(exc).__name__}: {exc}")
                continue
            dirty = True
            for event in events:
                delivered += 1 if self.deliver(event) else 0
        if dirty:
            self._save()
        self.delivered += delivered
        return delivered

    def deliver(self, event: Event) -> bool:
        """Notify, and record it. The only exit from this subsystem.

        The audit line is written whether or not the desktop was reachable,
        with the outcome in ``ok``: "she noticed and could not tell you" and
        "she never noticed" are different facts and the log has to keep them
        apart.
        """
        try:
            sent = self.notifier.send(event)
        except Exception as exc:  # noqa: BLE001 - delivery must never raise out
            log.exception("ambient delivery failed",
                          extra={"source": event.source})
            sent = False
            self.audit.append(f"ambient.{event.source}", ok=False,
                              why=event.headline,
                              detail=f"{type(exc).__name__}: {exc}",
                              **event.audit_fields())
            return False
        self.audit.append(f"ambient.{event.source}", ok=sent,
                          why=event.headline,
                          body=event.body,
                          delivered=sent,
                          **event.audit_fields())
        log.info("ambient", extra={"source": event.source,
                                   "headline": event.headline,
                                   "delivered": sent})
        return sent

    # -- state -----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        """Read the persisted watcher state. A missing or broken file is empty.

        Empty means every watcher seeds itself on its first check and reports
        nothing, which is the right answer for a corrupt file too: the failure
        mode of guessing is a wall of toasts about a fortnight of history.
        """
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self._complain("could not read the ambient state", exc)
            return {}
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            self._complain("ambient state is corrupt; starting fresh", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        """Best-effort, atomic. Nothing here may fail an event.

        Same contract as ``presence.py``: this is called from a background
        thread on a machine whose disk may be full, and a state file that
        cannot be written costs one repeated notification after a restart, not
        a daemon.
        """
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        try:
            config.ensure_parent(self._state_path, existed=self._dir_existed)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except OSError as exc:
            self._complain("could not write the ambient state", exc)

    def _complain(self, what: str, exc: BaseException) -> None:
        if self._complained:
            return
        self._complained = True
        log.warning("%s (further failures will be silent)", what,
                    extra={"path": str(self._state_path),
                           "detail": f"{type(exc).__name__}: {exc}"})

    # -- introspection ---------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": self.enabled(),
                "running": self._thread is not None,
                "poll_seconds": self.interval(),
                "ticks": self.ticks,
                "delivered": self.delivered,
                "state_file": str(self._state_path),
                "speaks": False,
                "watchers": {w.name: w.snapshot() for w in self.watchers}}


# =========================================================================
# The crash diagnosis, as a task for a dispatched agent
# =========================================================================


def diagnosis_task(pid: int, exe: str = "", when: str = "") -> str:
    """The prompt behind the crash toast's one click.

    It names the `diagnose-crash` skill rather than restating it. The skill is
    installed on this machine at ``~/.claude/skills/diagnose-crash/`` and
    mirrored into ``~/.codex/skills/``, so the dispatched agent — whichever CLI
    it is — can load it; repeating its ten steps here would be a second copy
    that drifts from the first. What is stated is the part the skill cannot
    know: which pid, and that this was noticed rather than asked about.
    """
    what = exe or "a process"
    lines = [f"{what} crashed on this machine and dumped core"
             f"{f' at {when}' if when else ''}. The pid was {pid}.",
             "",
             "Use the `diagnose-crash` skill — it is installed at "
             "~/.claude/skills/diagnose-crash/SKILL.md and mirrored into "
             "~/.codex/skills/. Follow it rather than improvising: "
             f"`coredumpctl info {pid}` for the backtrace and command line, "
             "`coredumpctl list` to see whether this is a one-off or a "
             "pattern, rule out OOM and resource exhaustion before blaming "
             "the program, and symbolize with gdb against Arch's debuginfod "
             "if the trace is stripped. Clean up any core you extract.",
             "",
             "This is read-only work: diagnose, do not fix and do not "
             "reconfigure anything.",
             "",
             "Report back: what crashed, the most likely mechanism — saying "
             "which parts are evidence and which are inference — whether "
             "anything was lost, and whether it is likely to happen again.",
             "",
             "Nobody asked for this out loud: it was noticed. Keep the answer "
             "short enough to read in a notification."]
    return "\n".join(lines)


# =========================================================================
# Small helpers
# =========================================================================


def _read(path: Path, limit: int = 4096) -> str:
    """Read a small file, or return ``""``. sysfs and /usr never justify a raise."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None
