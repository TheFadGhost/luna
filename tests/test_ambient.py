"""Ambient awareness: the three hooks, and the rule that outranks them.

The rule is the user's own, and it is the reason this file leads with
:class:`NeverSpeaksCase` rather than with the hooks:

    "Yes, she may do that, but I'd like a notification so it doesn't actually
    disturb me... I prefer notify only, unless I spoke to it first and it was
    coming back with an answer to a task that I gave beforehand."

So an ambient event must not be able to reach the speech path, and the point of
the first case in this file is that it **fails if somebody later wires one
up** — not that it documents that they should not. It checks all three layers
of the enforcement in `lunad/ambient.py` and then walks the live object graph
of a real `Daemon`'s ambient subsystem looking for anything that can be told to
talk.

Nothing here touches the machine. `tests/_support.py` redirects the coredump
directory, the power-supply directory, both omarchy paths, the ambient state
file and the HUD message file into throwaway trees, and `config.NOTIFY_BIN`
was already disarmed — so a case that fires all three hooks at once produces no
toast, no caption and no reading of anybody's real battery.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unittest
from pathlib import Path
from typing import Any

from ._support import FakeHyprland, TempMemoryCase

from lunad import ambient, config, dispatch, safety
from lunad import settings as settings_mod


AMBIENT_SRC = Path(ambient.__file__)


class Recorder(ambient.Notifier):
    """A Notifier that records instead of reaching the desktop.

    A *subclass*, not a stand-in, because that is the contract `Ambient`
    enforces: delivery happens through a `Notifier` or it does not happen.
    Overriding the two surfaces rather than `send` keeps `send`'s own logic --
    which is what production runs -- under test.
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[ambient.Event] = []
        self.panes: list[str] = []
        self.cleared = 0
        self.fail = False

    def toast(self, event: ambient.Event) -> bool:
        self.events.append(event)
        return not self.fail

    def pane(self, event: ambient.Event) -> bool:
        self.panes.append(event.headline)
        return not self.fail

    def clear_pane(self) -> bool:
        self.cleared += 1
        return True


class Talker:
    """Something with a `.say()`. The thing ambient must refuse to hold."""

    def say(self, text: str) -> None:  # pragma: no cover - never called
        raise AssertionError("ambient spoke")


class AmbientCase(TempMemoryCase):
    """A case with an `Ambient` wired to throwaway paths and a Recorder."""

    def setUp(self) -> None:
        super().setUp()
        self.dumps = self.root / "coredump"
        self.dumps.mkdir()
        self.power = self.root / "power_supply"
        self.power.mkdir()
        self.version = self.root / "omarchy-version"
        self.updatelog = self.root / "omarchy-update.log"
        self.notifier = Recorder()

    def build(self, **kw: Any) -> ambient.Ambient:
        amb = ambient.Ambient(notifier=self.notifier, audit=self.audit,
                              settings=self.settings,
                              state_path=self.root / "ambient.json", **kw)
        amb.watchers = (
            ambient.CrashWatcher(amb.state.setdefault("crash", {}),
                                 directory=self.dumps, uid=1000),
            ambient.BatteryWatcher(amb.state.setdefault("battery", {}),
                                   directory=self.power),
            ambient.UpdateWatcher(amb.state.setdefault("update", {}),
                                  version=self.version,
                                  log_path=self.updatelog),
        )
        self.addCleanup(amb.close)
        return amb

    # -- fixtures --------------------------------------------------------

    def dump(self, comm: str, pid: int, usec: int, uid: int = 1000) -> str:
        name = f"core.{comm}.{uid}.abcdef0123456789.{pid}.{usec}.zst"
        (self.dumps / name).write_bytes(b"not really a core")
        return name

    def battery(self, pct: int, status: str = "Discharging",
                name: str = "BAT1") -> Path:
        # Named BAT1, not BAT0, because that is what this laptop actually has
        # and a watcher that guessed the name would pass against BAT0 and find
        # nothing on the real machine.
        device = self.power / name
        device.mkdir(exist_ok=True)
        (device / "type").write_text("Battery\n")
        (device / "capacity").write_text(f"{pct}\n")
        (device / "status").write_text(f"{status}\n")
        return device

    def mains(self, name: str = "ACAD") -> Path:
        device = self.power / name
        device.mkdir(exist_ok=True)
        (device / "type").write_text("Mains\n")
        (device / "online").write_text("1\n")
        return device

    def entries(self, action: str) -> list[dict[str, Any]]:
        try:
            lines = self.audit.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [e for e in (json.loads(line) for line in lines if line.strip())
                if e.get("action") == action]


# =========================================================================
# The rule
# =========================================================================


class NeverSpeaksCase(AmbientCase):
    """An ambient event notifies. It must not be able to speak.

    Four checks, one per layer of the enforcement plus a reachability walk.
    Any one of them failing means somebody has opened a path from a hook to
    the synthesiser, which is the exact thing the user asked not to happen.
    """

    def test_the_module_cannot_reach_the_speech_path_at_all(self) -> None:
        # Layer 1: read the shipped source. `speech` is not imported and no
        # call to `.say(`/`.speak(` exists, so there is no name in the file
        # that could reach a synthesiser even by accident.
        src = AMBIENT_SRC.read_text(encoding="utf-8")
        code = "\n".join(_without_docstrings(src))
        self.assertNotIn("speech", code,
                         "lunad/ambient.py names `speech`; ambient events "
                         "notify, they never speak")
        for call in (".say(", ".speak(", "Speech("):
            self.assertNotIn(call, code, f"ambient calls {call}")

    def test_a_plain_callable_is_refused_as_a_channel(self) -> None:
        # Layer 2. "Just pass a function that says it out loud" is the shape
        # this would take, so a bare callable has to be rejected outright
        # rather than duck-typed into working.
        spoken: list[str] = []
        with self.assertRaises(ambient.AmbientChannelError) as caught:
            ambient.Ambient(notifier=lambda e: spoken.append(e.headline),
                            audit=self.audit, settings=self.settings,
                            state_path=self.root / "a.json")
        self.assertIn("never", str(caught.exception).lower())
        self.assertEqual(spoken, [])

    def test_a_collaborator_that_can_talk_is_refused_at_construction(self) -> None:
        # Layer 3. This is the one that catches `Ambient(..., speech=...)`,
        # and it fails at construction -- in the test run of whoever wired it
        # -- rather than the first time the machine crashes at 3am.
        amb = self.build()
        with self.assertRaises(ambient.AmbientChannelError):
            ambient._assert_mute(_WithSpeaker(amb))

    def test_no_speaker_is_reachable_from_a_real_daemon_s_ambient(self) -> None:
        """The walk. This is the test that fails if someone wires one up.

        It does not read the source and it does not check a type: it takes the
        `Ambient` a real `Daemon` built and walks everything reachable from it,
        looking for any object that has a `say` or `speak` method. A future
        edit that stores the daemon, the `Speech` object, or a callback that
        closes over either, fails here.
        """
        daemon = _daemon(self)
        found = _reachable_speakers(daemon.ambient)
        self.assertEqual(found, [],
                         "a speaker is reachable from the ambient subsystem: "
                         + ", ".join(found))
        # And the same walk finds one when there genuinely is one, so a green
        # result above means the walk works rather than that it looked nowhere.
        self.assertTrue(_reachable_speakers(_WithSpeaker(daemon.ambient)))

    def test_firing_every_hook_never_reaches_the_speech_object(self) -> None:
        """End to end: three real events, a real Daemon, a counting Speech."""
        daemon = _daemon(self)
        calls: list[str] = []
        original = daemon.speech.say
        daemon.speech.say = lambda *a, **k: calls.append(str(a[:1]))  # type: ignore[method-assign]
        self.addCleanup(setattr, daemon.speech, "say", original)

        recorder = Recorder()
        amb = ambient.Ambient(notifier=recorder, audit=self.audit,
                              settings=self.settings,
                              state_path=self.root / "live.json")
        amb.watchers = self.build().watchers
        self.addCleanup(amb.close)
        self.settings.set("ambient.battery", True)
        self.version.write_text("4.0.0.alpha\n")
        amb.tick(now=0.0)                                    # seeds, says nothing
        self.dump("foot", 4242, 1_787_717_390_000_000)
        self.battery(3)
        self.version.write_text("4.0.0\n")
        amb.tick(now=10_000.0)

        self.assertEqual(len(recorder.events), 3)
        self.assertEqual(calls, [], "an ambient event reached speech.say")
        self.assertFalse(amb.snapshot()["speaks"])


class _WithSpeaker:
    """A stand-in for the wiring mistake, so the guards can be shown to bite."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.speech = Talker()


def _without_docstrings(src: str) -> list[str]:
    """Executable lines only: no docstrings, no comments.

    Both have to go. The module docstring must be able to *say* "they never
    speak" without failing the check that enforces it, and so must the comment
    on `_SPEAKER_METHODS`, which exists precisely to explain what it is looking
    for. What is left is the part that could actually call something.
    """
    out, inside = [], False
    for line in src.splitlines():
        marks = line.count('"""')
        if inside:
            inside = marks % 2 == 0
            continue
        if marks:
            inside = marks % 2 == 1
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return out


def _reachable_speakers(root: Any, depth: int = 4) -> list[str]:
    """Every object reachable from ``root`` that has a `say` or `speak`.

    Bounded, and it does not follow modules, classes or plain containers of
    strings — the interesting edges are instance attributes and the closures
    of bound methods, which is exactly how a `Speech` would arrive.
    """
    seen: set[int] = set()
    found: list[str] = []

    def walk(obj: Any, path: str, level: int) -> None:
        if level > depth or id(obj) in seen:
            return
        seen.add(id(obj))
        for method in ambient._SPEAKER_METHODS:
            if callable(getattr(obj, method, None)) and not isinstance(obj, type):
                found.append(f"{path}.{method}()")
                return
        for name, value in list(getattr(obj, "__dict__", {}).items()):
            if value is None or isinstance(value, (str, bytes, int, float, bool)):
                continue
            walk(value, f"{path}.{name}", level + 1)
        for name in ("__self__", "__func__"):
            bound = getattr(obj, name, None)
            if bound is not None:
                walk(bound, f"{path}.{name}", level + 1)

    walk(root, "ambient", 0)
    return found


def _daemon(case: TempMemoryCase) -> Any:
    """A real `Daemon` on the case's throwaway tree. Imported late on purpose.

    `server` pulls in most of the package; importing it at module scope would
    make this file's collection cost the whole daemon even for the cases that
    only parse a filename.
    """
    from lunad import server

    d = server.Daemon(
        agent_name="claude",
        memory=case.memory(), sol_memory=case.sol_memory(), audit=case.audit,
        settings=case.settings,
        dispatcher=dispatch.Dispatcher(
            audit=case.audit, jobs_dir=case.root / "jobs",
            terminal="/bin/bash", hypr=FakeHyprland(),
            sol_memory_dir=case.root / "sol"))
    case.addCleanup(d.close)
    return d


# =========================================================================
# Crash
# =========================================================================


class DumpNameCase(unittest.TestCase):
    """The filename is the whole data source, so parsing it is the hook."""

    def test_a_real_name_from_this_machine(self) -> None:
        d = ambient.parse_dump(
            "core.foot.1000.ee093d49aa794259b16113845dbd7983.3080863."
            "1787717390000000.zst")
        assert d is not None
        self.assertEqual((d.comm, d.uid, d.pid, d.usec),
                         ("foot", 1000, 3080863, 1787717390000000))

    def test_a_comm_containing_a_dot(self) -> None:
        # `python3.14` is a real comm and the reason the parse works from the
        # right rather than splitting on every dot.
        d = ambient.parse_dump("core.python3.14.1000.abc.99.1787717390000000.zst")
        assert d is not None
        self.assertEqual(d.comm, "python3.14")
        self.assertEqual(d.pid, 99)

    def test_an_uncompressed_dump(self) -> None:
        d = ambient.parse_dump("core.gdb.1000.abc.7.1787717390000000")
        assert d is not None
        self.assertEqual(d.comm, "gdb")

    def test_a_future_compression_suffix_still_parses(self) -> None:
        # The suffix is dropped because it is not a number, not because it is
        # on a list -- so systemd moving off zstd does not silently stop the
        # hook noticing anything.
        d = ambient.parse_dump("core.foot.1000.abc.7.1787717390000000.br")
        assert d is not None
        self.assertEqual(d.comm, "foot")

    def test_rubbish_is_not_a_dump(self) -> None:
        for name in ("README", "core.foot", "core.foot.x.y.z.w",
                     "notacore.foot.1000.abc.7.1787717390000000.zst"):
            self.assertIsNone(ambient.parse_dump(name), name)


class CrashCase(AmbientCase):
    def test_the_first_tick_seeds_and_announces_nothing(self) -> None:
        # tmpfiles keeps dumps for two weeks. A daemon that started after a
        # bad afternoon must not open with fourteen critical toasts.
        for pid in range(1, 9):
            self.dump("foot", 3080000 + pid, 1_787_717_390_000_000 + pid)
        amb = self.build()
        self.assertEqual(amb.tick(now=0.0), 0)
        self.assertEqual(self.notifier.events, [])
        self.assertTrue(amb.state["crash"]["seeded"])

    def test_a_new_dump_notifies_once_with_the_diagnosis_one_click_away(self) -> None:
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("quickshell", 152141, 1_787_717_400_000_000)
        self.assertEqual(amb.tick(now=1000.0), 1)
        event = self.notifier.events[0]
        self.assertEqual(event.source, "crash")
        self.assertIn("quickshell", event.headline)
        self.assertEqual(event.urgency, "critical")
        self.assertEqual(event.action[-3:], ["152141", "--exe", "quickshell"])
        self.assertEqual(event.action[1:3], ["ambient", "diagnose"])
        # And not again on the next tick.
        self.assertEqual(amb.tick(now=2000.0), 0)

    def test_dumps_from_another_user_are_not_this_user_s_problem(self) -> None:
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("sshd", 7, 1_787_717_400_000_000, uid=0)
        self.assertEqual(amb.tick(now=1000.0), 0)

    def test_a_burst_coalesces_into_one_toast(self) -> None:
        """Eight `foot` cores in a minute is one incident, not eight alarms."""
        amb = self.build()
        amb.tick(now=0.0)
        for n in range(8):
            self.dump("foot", 3080000 + n, 1_787_717_400_000_000 + n)
        self.assertEqual(amb.tick(now=1000.0), 1)
        event = self.notifier.events[0]
        self.assertIn("8 processes crashed", event.headline)
        self.assertTrue(event.detail["coalesced"])
        # The click still diagnoses something: the newest of them.
        self.assertIn("3080007", event.action)

    def test_two_dumps_in_the_same_microsecond_are_both_new(self) -> None:
        # This machine really does write three cores stamped with the same
        # microsecond, so `usec >` alone would drop two of them.
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("foot", 11, 1_787_717_400_000_000)
        amb.tick(now=1000.0)
        self.notifier.events.clear()
        self.dump("foot", 12, 1_787_717_400_000_000)
        self.assertEqual(amb.tick(now=2000.0), 1)

    def test_an_unchanged_directory_is_never_scanned(self) -> None:
        """The gate is a stat(). A tick that finds nothing must not scandir."""
        amb = self.build()
        amb.tick(now=0.0)
        watcher = amb.watchers[0]
        scans = 0
        real = ambient.os.scandir

        def counting(path):  # noqa: ANN001, ANN202
            nonlocal scans
            scans += 1
            return real(path)

        ambient.os.scandir = counting
        self.addCleanup(setattr, ambient.os, "scandir", real)
        for n in range(5):
            watcher.check()
        self.assertEqual(scans, 0)

    def test_the_click_action_can_be_switched_off_on_its_own(self) -> None:
        self.settings.set("ambient.crash_diagnose", False)
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("foot", 99, 1_787_717_400_000_000)
        amb.tick(now=1000.0)
        self.assertIsNone(self.notifier.events[0].action)

    def test_a_missing_coredump_directory_is_not_an_error(self) -> None:
        amb = self.build()
        amb.watchers = (ambient.CrashWatcher({}, directory=self.root / "nope"),)
        self.assertEqual(amb.tick(now=0.0), 0)


# =========================================================================
# Battery
# =========================================================================


class BatteryCase(AmbientCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings.set("ambient.battery", True)

    def test_it_is_off_by_default_because_omarchy_already_warns(self) -> None:
        # The one hook that duplicates the desktop, and therefore the one that
        # must not be on unless somebody asked for it. Omarchy's own battery
        # service toasts at 10% and UPower hibernates at 2%.
        self.assertIs(settings_mod.defaults()["ambient"]["battery"], False)
        fresh = settings_mod.Settings(self.root / "fresh.toml")
        self.addCleanup(fresh.stop_watching)
        old = settings_mod.use_settings(fresh)
        self.addCleanup(settings_mod.use_settings, old)
        # The watcher's own `enabled()` overrides the base class's default of
        # True, so a config that has never heard of `[ambient]` still reads as
        # off rather than on.
        self.assertFalse(ambient.BatteryWatcher({}).enabled())

    def test_the_battery_is_found_by_type_not_by_name(self) -> None:
        # BAT1, not BAT0 -- and with mains and two USB-C sources beside it,
        # which is what /sys/class/power_supply actually looks like here.
        self.mains()
        (self.power / "ucsi-source-psy-USBC000:001").mkdir()
        (self.power / "ucsi-source-psy-USBC000:001" / "type").write_text("USB\n")
        self.battery(50)
        watcher = ambient.BatteryWatcher({}, directory=self.power)
        found = watcher.battery()
        assert found is not None
        self.assertEqual(found.name, "BAT1")

    def test_low_then_critical_each_fire_once(self) -> None:
        amb = self.build()
        self.battery(18)
        self.assertEqual(amb.tick(now=0.0), 1)
        self.assertIn("low", self.notifier.events[-1].headline.lower())
        self.assertEqual(amb.tick(now=1000.0), 0, "the low warning repeated")

        self.battery(4)
        self.assertEqual(amb.tick(now=2000.0), 1)
        self.assertIn("critical", self.notifier.events[-1].headline.lower())
        self.assertEqual(amb.tick(now=3000.0), 0)

    def test_a_charging_battery_says_nothing_and_re_arms_the_latch(self) -> None:
        amb = self.build()
        self.battery(8)
        self.assertEqual(amb.tick(now=0.0), 1)
        self.battery(8, status="Charging")
        self.assertEqual(amb.tick(now=1000.0), 0)
        self.battery(8, status="Discharging")
        self.assertEqual(amb.tick(now=2000.0), 1,
                         "unplugging again should be allowed to warn again")

    def test_a_full_battery_is_silent(self) -> None:
        amb = self.build()
        self.battery(96)
        self.assertEqual(amb.tick(now=0.0), 0)

    def test_a_critical_set_above_the_low_is_clamped_not_obeyed(self) -> None:
        # Both are independently settable, and a critical above the low would
        # make the low unreachable. A misconfigured threshold must still warn.
        self.settings.set("ambient.battery_low_pct", 10)
        self.settings.set("ambient.battery_critical_pct", 40)
        watcher = ambient.BatteryWatcher({}, directory=self.power)
        self.assertEqual(watcher.thresholds(), (10, 10))

    def test_no_battery_at_all_is_not_an_error(self) -> None:
        amb = self.build()
        self.mains()
        self.assertEqual(amb.tick(now=0.0), 0)

    def test_a_capacity_that_is_not_a_number_is_ignored(self) -> None:
        device = self.battery(20)
        (device / "capacity").write_text("unknown\n")
        watcher = ambient.BatteryWatcher({}, directory=self.power)
        self.assertIsNone(watcher.reading())


# =========================================================================
# omarchy update
# =========================================================================


class UpdateCase(AmbientCase):
    def test_the_first_tick_seeds_and_says_nothing(self) -> None:
        self.version.write_text("4.0.0.alpha\n")
        amb = self.build()
        self.assertEqual(amb.tick(now=0.0), 0)
        self.assertEqual(amb.state["update"]["version"], "4.0.0.alpha")

    def test_a_new_version_names_what_it_clobbered(self) -> None:
        self.version.write_text("4.0.0.alpha\n")
        amb = self.build()
        amb.tick(now=0.0)
        self.version.write_text("4.1.0\n")
        self.assertEqual(amb.tick(now=1000.0), 1)
        event = self.notifier.events[0]
        self.assertIn("4.0.0.alpha", event.headline)
        self.assertIn("4.1.0", event.headline)
        # The whole point of the hook: say what the update took with it.
        self.assertIn("/usr/share/omarchy was rewritten", event.body)
        self.assertIn("CUSTOMISATIONS.md", event.body)

    def test_a_same_version_reinstall_is_still_reported(self) -> None:
        """`pacman -Syu --overwrite` rewrites the tree whether or not the
        version moved, so the mtime is checked as well as the string."""
        self.version.write_text("4.0.0.alpha\n")
        amb = self.build()
        amb.tick(now=0.0)
        stamp = self.version.stat().st_mtime_ns
        self.version.write_text("4.0.0.alpha\n")
        ambient.os.utime(self.version, ns=(stamp + 10**9, stamp + 10**9))
        self.assertEqual(amb.tick(now=1000.0), 1)
        self.assertIn("reinstalled", self.notifier.events[0].headline)

    def test_an_update_run_that_moved_no_package_is_a_quiet_line(self) -> None:
        self.version.write_text("4.0.0.alpha\n")
        amb = self.build()
        amb.tick(now=0.0)
        self.updatelog.write_text("Script started\n")
        self.assertEqual(amb.tick(now=1000.0), 1)
        event = self.notifier.events[0]
        self.assertEqual(event.urgency, "low")
        self.assertFalse(event.detail["package_changed"])

    def test_a_machine_that_is_not_omarchy_is_silent(self) -> None:
        amb = self.build()
        for n in range(3):
            self.assertEqual(amb.tick(now=n * 1000.0), 0)

    def test_availability_is_deliberately_not_checked(self) -> None:
        # `omarchy-update-available` costs a network sync and Omarchy's own bar
        # widget already polls it every six hours. Asserted so that adding one
        # here is a decision somebody has to make in this file.
        self.version.write_text("4.0.0.alpha\n")
        amb = self.build()
        amb.tick(now=0.0)
        self.assertFalse(amb.snapshot()["watchers"]["update"]["available_checked"])
        code = "\n".join(_without_docstrings(
            AMBIENT_SRC.read_text(encoding="utf-8")))
        self.assertNotIn("update-available", code)
        self.assertNotIn("checkupdates", code)


# =========================================================================
# Delivery
# =========================================================================


class DeliveryCase(AmbientCase):
    """The one function every event leaves through."""

    def test_the_toast_is_spawned_through_the_firewall_and_reaped(self) -> None:
        """Everything lunad forks lands in the allowlist, and nothing leaks.

        The audit found hung `notify-send` children leaking zombies. This uses
        `safety.reap_after`, which polls once inline and otherwise hands the
        wait to a thread that never gives up -- so a toast that never exits
        costs a thread, not a permanent zombie.
        """
        spawned: list[list[str]] = []
        reaped: list[Any] = []
        real_spawn, real_reap = safety.spawn, safety.reap_after

        def fake_spawn(argv, **kw):  # noqa: ANN001, ANN202
            spawned.append(argv)
            self.assertEqual(kw["kind"], "ambient-notify")
            self.assertFalse(kw["durable"])
            self.assertEqual(kw["stdout"], subprocess.DEVNULL)
            return _FakeProc()

        safety.spawn = fake_spawn
        safety.reap_after = lambda proc, **kw: reaped.append(proc) or True
        self.addCleanup(setattr, safety, "spawn", real_spawn)
        self.addCleanup(setattr, safety, "reap_after", real_reap)

        ambient.Notifier().toast(ambient.Event(
            source="crash", headline="foot crashed", body="pid 7",
            urgency="critical", icon="X", action=["/bin/luna", "ambient"]))
        self.assertEqual(len(spawned), 1)
        self.assertEqual(len(reaped), 1, "the notifier child was not reaped")
        argv = spawned[0]
        self.assertEqual(argv[0], config.NOTIFY_BIN)
        self.assertIn("--exec", argv)
        self.assertEqual(argv[-2:], ["foot crashed", "pid 7"])

    def test_a_notifier_that_cannot_reach_the_desktop_does_not_raise(self) -> None:
        # `config.NOTIFY_BIN` is a name that cannot resolve for the whole test
        # process, so this is the real production path against a machine with
        # no notification daemon.
        sent = ambient.Notifier().toast(
            ambient.Event(source="update", headline="hi", body="there"))
        self.assertFalse(sent)

    def test_the_hud_message_matches_the_handoff_contract(self) -> None:
        writer = ambient.HudWriter(self.root / "message")
        self.assertTrue(writer.write("Something happened.", kind="alert",
                                     ttl=12))
        payload = json.loads((self.root / "message").read_text())
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["text"], "Something happened.")
        self.assertEqual(payload["kind"], "alert")
        self.assertEqual(payload["ttl"], 12)
        # `ts` is not optional in practice: without it the pane treats a file
        # left behind by a dead daemon as never stale.
        self.assertAlmostEqual(payload["ts"], time.time(), delta=30)
        # The id is what makes a message new, so it has to move.
        writer.write("And another.")
        self.assertEqual(json.loads((self.root / "message").read_text())["id"], 2)
        self.assertFalse((self.root / "message.tmp").exists())

    def test_the_hud_truncates_rather_than_handing_the_pane_a_transcript(self) -> None:
        writer = ambient.HudWriter(self.root / "message")
        writer.write("x" * 900)
        self.assertEqual(
            len(json.loads((self.root / "message").read_text())["text"]), 500)

    def test_removing_the_message_dismisses_it_but_only_if_it_was_ours(self) -> None:
        writer = ambient.HudWriter(self.root / "message")
        self.assertFalse(writer.clear(only_mine=True))
        writer.write("mine")
        self.assertTrue(writer.clear(only_mine=True))
        self.assertFalse((self.root / "message").exists())

    def test_an_unwritable_hud_path_costs_a_caption_not_the_event(self) -> None:
        writer = ambient.HudWriter(self.root / "no" / "such" / "dir" / "m")
        (self.root / "no").write_text("a file, not a directory")
        self.assertFalse(writer.write("hello"))

    def test_delivery_sends_to_both_surfaces(self) -> None:
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("foot", 5, 1_787_717_400_000_000)
        amb.tick(now=1000.0)
        self.assertEqual(len(self.notifier.events), 1)
        self.assertEqual(len(self.notifier.panes), 1)


class _FakeProc:
    pid = 424242

    def poll(self) -> int:
        return 0


# =========================================================================
# Audit, state and the loop
# =========================================================================


class AuditCase(AmbientCase):
    def test_every_event_is_recorded_with_its_why_and_its_outcome(self) -> None:
        self.settings.set("ambient.battery", True)
        self.version.write_text("4.0.0.alpha\n")
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("foot", 5, 1_787_717_400_000_000)
        self.battery(3)
        self.version.write_text("4.1.0\n")
        amb.tick(now=1000.0)
        for action in ("ambient.crash", "ambient.battery", "ambient.update"):
            entries = self.entries(action)
            self.assertEqual(len(entries), 1, action)
            entry = entries[0]
            self.assertTrue(entry["why"], f"{action} has no why")
            self.assertTrue(entry["ok"])
            self.assertTrue(entry["delivered"])

    def test_an_event_nobody_could_be_shown_is_still_recorded(self) -> None:
        """"She noticed and could not tell you" and "she never noticed" are
        different facts, and the log has to keep them apart."""
        self.notifier.fail = True
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("foot", 5, 1_787_717_400_000_000)
        amb.tick(now=1000.0)
        entry = self.entries("ambient.crash")[0]
        self.assertFalse(entry["ok"])
        self.assertFalse(entry["delivered"])

    def test_a_watcher_that_raises_is_audited_and_does_not_stop_the_others(self) -> None:
        self.version.write_text("4.0.0.alpha\n")
        amb = self.build()
        amb.tick(now=0.0)

        class Broken(ambient.Watcher):
            name = "crash"

            def check(self):  # noqa: ANN202
                raise RuntimeError("sysfs on fire")

        self.version.write_text("4.1.0\n")
        amb.watchers = (Broken({}), amb.watchers[2])
        amb.tick(now=1000.0)
        self.assertEqual(len(self.entries("ambient.failed")), 1)
        self.assertEqual(len(self.notifier.events), 1,
                         "one broken watcher cost the user the other hook")


class StateCase(AmbientCase):
    def test_a_restart_does_not_re_announce_what_was_already_seen(self) -> None:
        self.dump("foot", 5, 1_787_717_400_000_000)
        first = self.build()
        first.tick(now=0.0)
        first.close()
        self.dump("foot", 6, 1_787_717_500_000_000)

        self.notifier = Recorder()
        second = self.build()
        self.assertEqual(second.tick(now=0.0), 1)
        self.assertIn("6", str(second.state["crash"]["recent"]))
        self.assertEqual(len(self.notifier.events), 1)

    def test_a_corrupt_state_file_seeds_fresh_rather_than_guessing(self) -> None:
        (self.root / "ambient.json").write_text("{not json at all")
        self.dump("foot", 5, 1_787_717_400_000_000)
        amb = self.build()
        self.assertEqual(amb.tick(now=0.0), 0, "a corrupt file must not toast history")

    def test_an_unwritable_state_path_does_not_break_a_tick(self) -> None:
        amb = ambient.Ambient(notifier=self.notifier, audit=self.audit,
                              settings=self.settings,
                              state_path=self.root / "nope" / "x" / "s.json")
        self.addCleanup(amb.close)
        amb.watchers = self.build().watchers
        (self.root / "nope").write_text("a file, not a directory")
        self.assertEqual(amb.tick(now=0.0), 0)

    def test_the_recent_list_is_capped(self) -> None:
        for n in range(config.AMBIENT_RECENT_DUMPS + 20):
            self.dump("foot", 1000 + n, 1_787_717_400_000_000 + n)
        amb = self.build()
        amb.tick(now=0.0)
        self.assertEqual(len(amb.state["crash"]["recent"]),
                         config.AMBIENT_RECENT_DUMPS)


class TickCase(AmbientCase):
    def test_the_master_switch_skips_every_watcher(self) -> None:
        self.settings.set("ambient.enabled", False)
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("foot", 5, 1_787_717_400_000_000)
        self.assertEqual(amb.tick(now=1000.0), 0)
        self.assertEqual(self.notifier.events, [])

    def test_each_hook_switches_off_on_its_own(self) -> None:
        for hook in ambient.SOURCES:
            with self.subTest(hook=hook):
                self.assertIn(f"ambient.{hook}",
                              [f"{s.name}.{k.name}"
                               for s in settings_mod.SCHEMA for k in s.keys])
        self.settings.set("ambient.crash", False)
        amb = self.build()
        amb.tick(now=0.0)
        self.dump("foot", 5, 1_787_717_400_000_000)
        self.assertEqual(amb.tick(now=1000.0), 0)

    def test_the_update_hook_runs_on_its_own_slower_cadence(self) -> None:
        amb = self.build()
        self.version.write_text("4.0.0\n")
        amb.tick(now=0.0)
        self.version.write_text("4.1.0\n")
        # Inside the update watcher's 300 s window: the crash hook is due
        # again, the update hook is not.
        self.assertEqual(amb.tick(now=100.0), 0)
        self.assertEqual(amb.tick(now=400.0), 1)

    def test_the_thread_starts_once_and_stops_cleanly(self) -> None:
        amb = self.build()
        self.assertTrue(amb.start())
        self.assertFalse(amb.start())
        self.assertTrue(amb.snapshot()["running"])
        amb.close()
        self.assertFalse(amb.snapshot()["running"])
        self.assertEqual(self.notifier.cleared, 1)

    def test_the_poll_interval_has_a_floor(self) -> None:
        amb = self.build()
        self.settings.set("ambient.poll_seconds", 5)
        self.assertEqual(amb.interval(), 5.0)


# =========================================================================
# The daemon's side
# =========================================================================


class DaemonCase(AmbientCase):
    def test_status_reports_the_hooks_and_that_she_does_not_speak(self) -> None:
        daemon = _daemon(self)
        status = daemon.op_status({"op": "status"})
        self.assertIn("ambient", status)
        self.assertEqual(set(status["ambient"]["watchers"]),
                         set(ambient.SOURCES))
        self.assertFalse(status["ambient"]["speaks"])

    def test_the_ambient_op_answers(self) -> None:
        daemon = _daemon(self)
        resp = daemon.dispatch({"op": "ambient", "id": "x"})
        self.assertTrue(resp["ok"])
        self.assertIn("crash", resp["ambient"]["watchers"])

    def test_diagnose_dispatches_a_job_that_names_the_skill(self) -> None:
        daemon = _daemon(self)
        tasks: list[str] = []
        daemon.dispatcher.dispatch = lambda task, to, **kw: (  # type: ignore[method-assign]
            tasks.append(task) or _FakeJob())
        daemon.dispatcher.announce = lambda job: "on it"  # type: ignore[method-assign]
        resp = daemon.dispatch({"op": "ambient.diagnose", "id": "x",
                                "pid": 152141, "exe": "quickshell"})
        self.assertTrue(resp["ok"])
        self.assertIn("diagnose-crash", tasks[0])
        self.assertIn("152141", tasks[0])
        self.assertIn("quickshell", tasks[0])
        # Read-only work: the skill diagnoses, it does not repair.
        self.assertIn("do not fix", tasks[0])
        self.assertEqual(len(self.entries("ambient.diagnose")), 1)

    def test_diagnose_refuses_a_pid_that_is_not_one(self) -> None:
        daemon = _daemon(self)
        for bad in (None, "not a pid", 0, -3):
            with self.subTest(pid=bad):
                resp = daemon.dispatch({"op": "ambient.diagnose", "id": "x",
                                        "pid": bad})
                self.assertFalse(resp["ok"])
                self.assertEqual(resp["error"], "ProtocolError")

    def test_closing_the_daemon_stops_the_ambient_thread(self) -> None:
        daemon = _daemon(self)
        self.assertTrue(daemon.ambient.snapshot()["running"])
        daemon.close()
        self.assertFalse(daemon.ambient.snapshot()["running"])


class _FakeJob:
    id = "job-test"
    state = "running"

    def to_dict(self, **kw: Any) -> dict[str, Any]:
        return {"id": self.id, "state": self.state}


# =========================================================================
# The document and the schema
# =========================================================================


class ContractCase(unittest.TestCase):
    def test_the_defaults_are_the_ones_that_were_argued_for(self) -> None:
        """Named here so changing one has to come through this file.

        `battery` is the interesting one: it is the only hook that duplicates
        something the desktop already does, so it is the only one that is off.
        """
        amb = settings_mod.defaults()["ambient"]
        self.assertIs(amb["enabled"], True)
        self.assertIs(amb["crash"], True)
        self.assertIs(amb["crash_diagnose"], True)
        self.assertIs(amb["battery"], False)
        self.assertIs(amb["update"], True)
        self.assertEqual(amb["poll_seconds"], 60)
        # Either side of Omarchy's own 10% toast, and above UPower's 2%
        # hibernate, so the three do not land on top of each other.
        self.assertGreater(amb["battery_low_pct"], 10)
        self.assertLess(amb["battery_critical_pct"], 10)
        self.assertGreater(amb["battery_critical_pct"], 2)

    def test_the_document_records_that_ambient_never_speaks(self) -> None:
        doc = (Path(__file__).resolve().parent.parent / "docs"
               / "CONFIG-SCHEMA.md").read_text(encoding="utf-8")
        self.assertIn("an ambient event notifies, it never speaks", doc.lower())


if __name__ == "__main__":
    unittest.main()
