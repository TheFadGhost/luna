"""The session firewall — the tests this phase exists for.

Other agent sessions run on this machine while Luna does. The single claim
being defended here is that she cannot signal one of them, and the way to test
that claim is not with a mock: it is with the pid of a real process she did not
start, asserting both that the gate says no and that the process is still
running afterwards.

The rest of the file covers the ways the gate could be fooled: a recycled pid,
a process group that is not hers, a ledger reloaded from disk, and a codebase
that grew a second kill path behind the firewall's back.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import unittest
from pathlib import Path

from lunad import safety

from ._support import TempMemoryCase

REPO = Path(__file__).resolve().parent.parent


class StartTimeTests(TempMemoryCase):
    def test_own_starttime_is_stable(self):
        first = safety.read_starttime(os.getpid())
        self.assertIsNotNone(first)
        time.sleep(0.05)
        self.assertEqual(safety.read_starttime(os.getpid()), first)

    def test_missing_process_has_no_starttime(self):
        # 4194304 is above the default pid_max on this kernel; if something is
        # somehow there, the assertion below is still the honest one.
        self.assertIsNone(safety.read_starttime(4_194_304))

    def test_pid_zero_and_negative_are_never_processes(self):
        for pid in (0, -1, 1):
            with self.subTest(pid=pid):
                if pid == 1:
                    continue  # pid 1 exists; it is rejected by may_signal, not here
                self.assertIsNone(safety.read_starttime(pid))

    def test_comm_with_spaces_and_parens_does_not_break_the_parse(self):
        """The comm field is the classic /proc/stat parsing trap.

        A process called ``sl (ee) p`` puts spaces and a closing paren inside
        field 2. Splitting on whitespace from the left would shift every field
        after it and silently return the wrong start time — which would make
        the firewall reject processes Luna really did spawn.
        """
        script = ("import ctypes, time\n"
                  "ctypes.CDLL('libc.so.6').prctl(15, b'we (ird) name')\n"
                  "time.sleep(30)\n")
        proc = subprocess.Popen(["python3", "-c", script])
        self.addCleanup(_hard_stop, proc)
        time.sleep(0.4)
        comm = Path(f"/proc/{proc.pid}/comm").read_text().strip()
        if "(" not in comm:
            self.skipTest(f"prctl did not take; comm is {comm!r}")
        self.assertIsNotNone(safety.read_starttime(proc.pid))


class ForeignPidTests(TempMemoryCase):
    """Luna refuses to signal what she did not spawn. The point of Phase 2."""

    def test_may_signal_is_false_for_the_parent_process(self):
        parent = os.getppid()
        self.assertTrue(safety.is_alive(parent),
                        "the test harness's parent should be running")
        self.assertFalse(safety.may_signal(parent))
        self.assertIn("did not spawn", self.ledger.why_not(parent))

    def test_signalling_a_foreign_pid_raises_and_does_not_signal(self):
        """A real, unrelated, running process. Nothing is delivered to it."""
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(_hard_stop, victim)
        time.sleep(0.2)
        self.assertIsNone(victim.poll(), "the victim must be running")

        with self.assertRaises(safety.SignalRefused) as caught:
            safety.signal_pid(victim.pid, signal.SIGTERM)
        self.assertEqual(caught.exception.pid, victim.pid)

        time.sleep(0.3)
        self.assertIsNone(victim.poll(),
                          "the refused signal must not have been delivered")

    def test_terminate_refuses_a_process_luna_did_not_spawn(self):
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(_hard_stop, victim)
        time.sleep(0.2)
        with self.assertRaises(safety.SignalRefused):
            safety.terminate(victim)
        time.sleep(0.3)
        self.assertIsNone(victim.poll())

    def test_the_daemon_never_reaches_itself_through_the_pid_path(self):
        self.assertFalse(safety.may_signal(os.getpid()))
        self.assertIn("daemon itself", self.ledger.why_not(os.getpid()))

    def test_pid_one_is_refused(self):
        self.assertFalse(safety.may_signal(1))

    def test_refusals_are_counted_and_audited(self):
        safety.set_audit_hook(self.audit.hook)
        parent = os.getppid()
        before = self.ledger.refusals
        with self.assertRaises(safety.SignalRefused):
            safety.signal_pid(parent, signal.SIGTERM, reason="unit test")
        self.assertEqual(self.ledger.refusals, before + 1)
        entries = self.audit.read(action="signal.refused")
        self.assertTrue(entries)
        self.assertEqual(entries[0]["pid"], parent)
        self.assertFalse(entries[0]["ok"])


class OwnedPidTests(TempMemoryCase):
    def spawned(self, argv: list[str] | None = None) -> subprocess.Popen:
        proc = safety.spawn(argv or ["sleep", "30"], kind="test",
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        self.addCleanup(_hard_stop, proc)
        return proc

    def test_a_spawned_pid_may_be_signalled(self):
        proc = self.spawned()
        self.assertTrue(safety.may_signal(proc.pid))
        self.assertEqual(self.ledger.why_not(proc.pid), "")

    def test_spawn_registers_start_time_and_command(self):
        proc = self.spawned()
        rec = self.ledger.get(proc.pid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.starttime, safety.read_starttime(proc.pid))
        self.assertEqual(rec.cmd[0], "sleep")
        self.assertEqual(rec.kind, "test")

    def test_spawn_puts_the_child_in_its_own_process_group(self):
        proc = self.spawned()
        self.assertEqual(os.getpgid(proc.pid), proc.pid)

    def test_terminate_stops_a_child_luna_spawned(self):
        proc = self.spawned()
        self.assertTrue(safety.terminate(proc, grace=3.0, reason="unit test"))
        self.assertIsNotNone(proc.poll())

    def test_terminate_kills_the_whole_group(self):
        """A dispatched job is a terminal with an agent inside it.

        Terminating only the leader would leave the agent running and
        unaccounted for, so the group goes together.
        """
        proc = safety.spawn(
            ["/bin/sh", "-c", "sleep 30 & echo $!; wait"], kind="test",
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(_hard_stop, proc)
        self.addCleanup(proc.stdout.close)
        grandchild = int(proc.stdout.readline().strip())
        self.assertTrue(safety.is_alive(grandchild))
        safety.terminate(proc, grace=3.0, reason="unit test")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and safety.is_alive(grandchild):
            time.sleep(0.05)
        self.assertFalse(safety.is_alive(grandchild),
                         "the child inside the group should have gone too")

    def test_reap_removes_the_pid_from_the_allowlist(self):
        proc = self.spawned()
        safety.terminate(proc, grace=3.0)
        safety.reap(proc)
        self.assertIsNone(self.ledger.get(proc.pid))
        self.assertFalse(safety.may_signal(proc.pid))

    def test_a_dead_pid_is_no_longer_signallable(self):
        proc = safety.spawn(["/bin/true"], kind="test")
        proc.wait(timeout=5)
        self.assertFalse(safety.may_signal(proc.pid))
        self.assertIn("exited", self.ledger.why_not(proc.pid))


class PidReuseTests(TempMemoryCase):
    """The half of the rule that is not obvious.

    A ledger that only remembers pids is a ledger that hands out permission to
    whichever process inherits one. The recorded start time is what makes the
    record about a *process* rather than about a number.
    """

    def test_a_recycled_pid_is_refused(self):
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(_hard_stop, victim)
        time.sleep(0.2)
        real = safety.read_starttime(victim.pid)
        self.assertIsNotNone(real)

        # Stand in for "Luna spawned this pid, long ago, and it has since been
        # recycled": same pid, a start time from a different process.
        self.ledger._records[victim.pid] = safety.SpawnRecord(
            pid=victim.pid, starttime=real - 5_000, cmd=["sleep", "30"],
            started=time.time() - 3600, kind="test")

        self.assertFalse(safety.may_signal(victim.pid))
        with self.assertRaises(safety.SignalRefused):
            safety.signal_pid(victim.pid, signal.SIGTERM)
        time.sleep(0.3)
        self.assertIsNone(victim.poll())

    def test_why_not_names_both_start_times(self):
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(_hard_stop, victim)
        time.sleep(0.2)
        real = safety.read_starttime(victim.pid)
        self.ledger._records[victim.pid] = safety.SpawnRecord(
            pid=victim.pid, starttime=real - 5_000, cmd=[], started=0.0)
        why = self.ledger.why_not(victim.pid)
        self.assertIn("pid reuse", why)
        self.assertIn(str(real), why)

    def test_the_stale_record_is_dropped_on_detection(self):
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(_hard_stop, victim)
        time.sleep(0.2)
        real = safety.read_starttime(victim.pid)
        self.ledger._records[victim.pid] = safety.SpawnRecord(
            pid=victim.pid, starttime=real - 5_000, cmd=[], started=0.0)
        self.assertFalse(self.ledger.may_signal(victim.pid))
        self.assertIsNone(self.ledger.get(victim.pid),
                          "a record proven stale must not linger and be "
                          "re-checked against a future process")


class ProcessGroupTests(TempMemoryCase):
    def test_group_signal_refused_when_the_pid_does_not_lead_its_group(self):
        """The `pkill -f` mistake in its other form.

        A child that shares the daemon's process group would take the daemon —
        and anything else in that group — with it. The pgid must equal the pid.
        """
        proc = subprocess.Popen(["sleep", "30"])  # inherits our group
        self.addCleanup(_hard_stop, proc)
        time.sleep(0.2)
        # Grant membership, so the only thing left to refuse is the group check.
        self.ledger.record(proc.pid, ["sleep", "30"], kind="test")
        self.assertTrue(safety.may_signal(proc.pid))
        self.assertNotEqual(os.getpgid(proc.pid), proc.pid)
        with self.assertRaises(safety.SignalRefused) as caught:
            safety.signal_group(proc.pid, signal.SIGTERM)
        self.assertIn("process group leader", str(caught.exception))
        time.sleep(0.3)
        self.assertIsNone(proc.poll())


class LedgerRoundTripTests(TempMemoryCase):
    def test_records_survive_a_reload(self):
        proc = safety.spawn(["sleep", "30"], kind="dispatch", job_id="abc123",
                            durable=True)
        self.addCleanup(_hard_stop, proc)
        reloaded = safety.SpawnLedger(self.ledger.path)
        rec = reloaded.get(proc.pid)
        self.assertIsNotNone(rec, "a durable record must survive a restart")
        self.assertEqual(rec.job_id, "abc123")
        self.assertEqual(rec.starttime, safety.read_starttime(proc.pid))
        self.assertTrue(reloaded.may_signal(proc.pid))

    def test_a_reloaded_record_is_still_start_time_checked(self):
        ledger = safety.SpawnLedger(self.root / "reuse.json")
        victim = subprocess.Popen(["sleep", "30"])
        self.addCleanup(_hard_stop, victim)
        time.sleep(0.2)
        ledger._records[victim.pid] = safety.SpawnRecord(
            pid=victim.pid, starttime=safety.read_starttime(victim.pid) - 999,
            cmd=[], started=0.0)
        ledger._save(fsync=False)
        self.assertFalse(safety.SpawnLedger(ledger.path).may_signal(victim.pid))

    def test_a_corrupt_ledger_fails_closed(self):
        path = self.root / "corrupt.json"
        path.write_text("{not json at all", encoding="utf-8")
        ledger = safety.SpawnLedger(path)
        self.assertEqual(len(ledger), 0)
        self.assertFalse(ledger.may_signal(os.getppid()))

    def test_a_missing_ledger_is_empty_not_an_error(self):
        ledger = safety.SpawnLedger(self.root / "nope" / "spawned.json")
        self.assertEqual(len(ledger), 0)

    def test_entries_report_liveness(self):
        proc = safety.spawn(["/bin/true"], kind="test")
        proc.wait(timeout=5)
        alive = self.ledger.entries(include_dead=False)
        self.assertFalse([e for e in alive if e["pid"] == proc.pid])
        allrecs = self.ledger.entries(include_dead=True)
        self.assertTrue([e for e in allrecs if e["pid"] == proc.pid])

    def test_prune_drops_dead_records(self):
        proc = safety.spawn(["/bin/true"], kind="test")
        proc.wait(timeout=5)
        self.assertGreaterEqual(self.ledger.prune(), 1)
        self.assertIsNone(self.ledger.get(proc.pid))


class AuditedSpawnTests(TempMemoryCase):
    def test_every_spawn_is_recorded(self):
        safety.set_audit_hook(self.audit.hook)
        proc = safety.spawn(["/bin/true"], kind="test", note="a note")
        proc.wait(timeout=5)
        entries = self.audit.read(action="process.spawned")
        self.assertTrue(entries)
        self.assertEqual(entries[0]["pid"], proc.pid)
        self.assertEqual(entries[0]["kind"], "test")

    def test_every_delivered_signal_is_recorded(self):
        safety.set_audit_hook(self.audit.hook)
        proc = safety.spawn(["sleep", "30"], kind="test")
        self.addCleanup(_hard_stop, proc)
        safety.terminate(proc, grace=3.0, reason="unit test")
        entries = self.audit.read(action="signal.sent")
        self.assertTrue(entries)
        self.assertEqual(entries[0]["pid"], proc.pid)

    def test_an_exploding_audit_hook_cannot_break_a_signal(self):
        def boom(*_a, **_kw):
            raise RuntimeError("audit is on fire")
        safety.set_audit_hook(boom)
        proc = safety.spawn(["sleep", "30"], kind="test")
        self.addCleanup(_hard_stop, proc)
        self.assertTrue(safety.terminate(proc, grace=3.0))


class NoSecondKillPathTests(unittest.TestCase):
    """Structural: the firewall is only a choke point if nothing routes around it.

    These read the shipped source. They are the tests that fail when somebody
    adds a convenient ``proc.kill()`` in six months' time.
    """

    SOURCES = sorted(list((REPO / "lunad").glob("*.py"))
                     + [REPO / "bin" / "luna", REPO / "bin" / "luna-voice-router"])

    def test_pkill_and_killall_appear_nowhere(self):
        banned = re.compile(r"\b(pkill|killall)\b")
        for path in self.SOURCES:
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if banned.search(line) and "never" not in line and "ban" not in line:
                    # Prose that names the mistake is allowed; a call is not.
                    self.assertNotRegex(
                        line, r"(subprocess|os\.system|run|Popen)",
                        f"{path.name}:{lineno} looks like a call: {line.strip()}")

    def test_signal_delivery_lives_only_in_safety(self):
        calls = re.compile(r"\bos\.(kill|killpg)\s*\(")
        offenders = []
        for path in self.SOURCES:
            if path.name == "safety.py":
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if calls.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "every signal must go through lunad/safety.py")

    def test_popen_terminate_and_kill_are_not_used_on_children(self):
        calls = re.compile(r"\.(terminate|kill)\s*\(\s*\)")
        offenders = []
        for path in self.SOURCES:
            if path.name == "safety.py":
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if calls.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "Popen.terminate()/kill() bypass the ledger check")

    def test_children_are_spawned_through_safety(self):
        """A raw Popen is a pid with no record, which is a pid nothing can stop."""
        offenders = []
        for path in self.SOURCES:
            if path.name in ("safety.py", "dispatch.py"):
                continue  # safety owns Popen; dispatch shells out to hyprctl
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "subprocess.Popen(" in line:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "use safety.spawn so the pid enters the ledger")


def _hard_stop(proc: subprocess.Popen) -> None:
    """Test cleanup only. Deliberately does not use the firewall.

    A test that leaked a process must be able to clean it up even when the
    thing under test is the refusal to signal it.

    The group is only signalled when the child leads one. The first draft of
    this helper called ``killpg(getpgid(pid))`` unconditionally — and several
    of these tests deliberately spawn children that share the *runner's* group,
    so it SIGKILLed the test runner. That is the same class of mistake as
    ``pkill -f``, reproduced by accident inside the tests for it, which is a
    reasonable argument that the rule in ``signal_group`` is the right one.
    """
    if proc.poll() is not None:
        return
    try:
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
