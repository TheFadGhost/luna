"""The session firewall. ARCHITECTURE.md section 7.

Luna runs with full autonomy on a machine where other agent sessions are
working at the same time. The thing that must never happen is Luna signalling
one of them. So there is exactly one gate, :func:`may_signal`, and every code
path in ``lunad`` that could deliver a signal goes through it.

The rule is narrow on purpose:

    A pid may be signalled only if Luna spawned it **and** the process now
    holding that pid is still the one she spawned.

The second half is not paranoia. Linux recycles pids, and on this machine
``kernel.pid_max`` is small enough that a long-lived daemon can outlive a full
wrap. A record that says "pid 1234 is mine" is worthless on its own; the record
also stores field 22 of ``/proc/<pid>/stat`` — the process start time in clock
ticks since boot — which is stable for the life of a process and different for
whatever inherits the pid next. If the two disagree, the answer is no.

Everything Luna spawns is registered in ``~/.local/share/luna/spawned.json``.
The file is the durable copy of the allowlist so that a dispatched job stays
signallable across a daemon restart; the in-memory dict is the one consulted on
the hot path.

**fsync policy.** Records are written on every spawn, but only *durable*
records (dispatched jobs, which outlive the daemon) are fsync'd. A TTS player
or a headless agent child dies with the daemon, so its record is worthless
after a crash and does not justify an fsync in the path that decides how fast
Luna starts speaking. Measured here: the whole `spawn` call costs 0.66 ms
median, of which the ledger write is 0.41 ms; an fsync on the same file ranged
0.4-4 ms depending on cache state, which is the variance that made it not worth
paying on every utterance.

Nothing here ever matches a process by name. ``pkill -f`` and its relatives are
banned from this codebase: a previous agent on this machine killed its own
shell with one, and pattern matching cannot distinguish Luna's agent from the
user's.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config

log = logging.getLogger("lunad.safety")


class SignalRefused(PermissionError):
    """Luna declined to signal a pid. Never downgraded to a warning.

    Raised rather than returned so that a caller cannot ignore it by accident.
    A refusal is the firewall working, and it is always audited.
    """

    def __init__(self, pid: int, reason: str) -> None:
        super().__init__(
            f"refusing to signal pid {pid}: {reason}. Luna only signals "
            "processes she spawned herself."
        )
        self.pid = pid
        self.reason = reason


# =========================================================================
# /proc introspection
# =========================================================================


def read_starttime(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``: start time in clock ticks since boot.

    ``None`` means there is no such process. The comm field (2) is wrapped in
    parentheses and may itself contain spaces and parentheses, so the split
    starts after the *last* ``)``; from there field 3 is index 0 and field 22
    is index 19.
    """
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    except OSError:
        return None
    _, sep, rest = raw.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def is_alive(pid: int) -> bool:
    return read_starttime(pid) is not None


def process_cmdline(pid: int) -> str:
    """Best-effort ``/proc/<pid>/cmdline``, for audit detail only."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


# =========================================================================
# The allowlist
# =========================================================================


@dataclass
class SpawnRecord:
    pid: int
    starttime: int
    cmd: list[str]
    started: float
    kind: str = "child"
    job_id: str | None = None
    durable: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started))
        d["alive"] = read_starttime(self.pid) == self.starttime
        return d


class SpawnLedger:
    """Every pid Luna has spawned, and the proof that it is still that pid."""

    def __init__(self, path: Path = config.SPAWNED_PATH,
                 max_records: int = config.SPAWN_LEDGER_MAX) -> None:
        self.path = Path(path)
        self._dir_existed = self.path.parent.is_dir()
        self.max_records = max_records
        self._lock = threading.RLock()
        self._records: dict[int, SpawnRecord] = {}
        self._since_prune = 0
        self.refusals = 0
        self.load()

    # -- persistence -----------------------------------------------------

    def load(self) -> int:
        """Read the durable copy. A corrupt file is logged and started over:
        an unreadable allowlist must fail closed, not take the daemon down."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except OSError as exc:
            log.warning("could not read the spawn ledger",
                        extra={"path": str(self.path), "detail": str(exc)})
            return 0
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            log.warning("spawn ledger is corrupt; starting a new one",
                        extra={"path": str(self.path), "detail": str(exc)})
            return 0
        if not isinstance(data, list):
            return 0
        loaded = 0
        with self._lock:
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    rec = SpawnRecord(
                        pid=int(item["pid"]),
                        starttime=int(item["starttime"]),
                        cmd=[str(c) for c in item.get("cmd", [])],
                        started=float(item.get("started", 0.0)),
                        kind=str(item.get("kind", "child")),
                        job_id=item.get("job_id"),
                        durable=bool(item.get("durable", True)),
                        note=str(item.get("note", "")),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                self._records[rec.pid] = rec
                loaded += 1
        return loaded

    def _save(self, fsync: bool) -> None:
        with self._lock:
            payload = [asdict(r) for r in self._records.values()]
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            config.ensure_parent(self.path, existed=self._dir_existed)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                if fsync:
                    os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not write the spawn ledger",
                        extra={"path": str(self.path), "detail": str(exc)})

    # -- membership ------------------------------------------------------

    def record(self, pid: int, cmd: Iterable[str], *, kind: str = "child",
               job_id: str | None = None, durable: bool = True,
               note: str = "") -> SpawnRecord | None:
        """Add a pid to the allowlist.

        The start time is read *now*, immediately after the fork, which is the
        only moment at which "this pid is the process I just created" is
        certain. If the child has already exited there is nothing to remember
        and nothing that could ever need signalling, so the record is dropped.
        """
        starttime = read_starttime(int(pid))
        if starttime is None:
            return None
        rec = SpawnRecord(pid=int(pid), starttime=starttime,
                          cmd=[str(c) for c in cmd], started=time.time(),
                          kind=kind, job_id=job_id, durable=durable, note=note)
        with self._lock:
            self._records[rec.pid] = rec
            # Pruning stats every live record, so it is amortised rather than
            # paid on a spawn that sits in the speech latency path.
            self._since_prune += 1
            if (self._since_prune >= 32
                    or len(self._records) > self.max_records):
                self._since_prune = 0
                self._prune_locked()
        self._save(fsync=durable)
        return rec

    def forget(self, pid: int) -> bool:
        with self._lock:
            rec = self._records.pop(int(pid), None)
        if rec is None:
            return False
        self._save(fsync=False)
        return True

    def get(self, pid: int) -> SpawnRecord | None:
        with self._lock:
            return self._records.get(int(pid))

    def may_signal(self, pid: int) -> bool:
        """The gate. True only for a pid Luna spawned that is still that pid.

        Everything else — a pid she never spawned, a pid that has exited, a pid
        that has been recycled into somebody else's process — is False.
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 1 or pid == os.getpid():
            # pid 1 and the daemon itself are never signalled through this
            # path; see signal_self for the deliberate, separate exception.
            return False
        rec = self.get(pid)
        if rec is None:
            return False
        live = read_starttime(pid)
        if live is None:
            return False
        if live != rec.starttime:
            # Somebody else is holding the pid now. Drop the stale record so a
            # later spawn cannot inherit its permission.
            log.warning("pid reuse detected; refusing",
                        extra={"pid": pid, "recorded_starttime": rec.starttime,
                               "live_starttime": live})
            self.forget(pid)
            return False
        return True

    def refuse(self, pid: int, reason: str) -> SignalRefused:
        with self._lock:
            self.refusals += 1
        log.warning("signal refused", extra={"pid": pid, "reason": reason,
                                             "cmdline": process_cmdline(pid)[:200]})
        return SignalRefused(pid, reason)

    def why_not(self, pid: int) -> str:
        """Human-readable reason ``may_signal`` said no. For audit and tests."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return "not a pid"
        if pid <= 1:
            return "pid 1 or lower is never a Luna child"
        if pid == os.getpid():
            return "that is the daemon itself"
        rec = self.get(pid)
        if rec is None:
            return "Luna did not spawn it"
        live = read_starttime(pid)
        if live is None:
            return "the process has already exited"
        if live != rec.starttime:
            return (f"pid reuse: recorded start time {rec.starttime}, "
                    f"live start time {live}")
        return ""

    def entries(self, include_dead: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())
        out = [r.to_dict() for r in records]
        if not include_dead:
            out = [r for r in out if r["alive"]]
        out.sort(key=lambda r: r["started"], reverse=True)
        return out

    def prune(self) -> int:
        with self._lock:
            n = self._prune_locked()
        if n:
            self._save(fsync=False)
        return n

    def _prune_locked(self) -> int:
        """Drop dead records, then cap the file. Called with the lock held."""
        dead = [pid for pid, rec in self._records.items()
                if read_starttime(pid) != rec.starttime]
        for pid in dead:
            self._records.pop(pid, None)
        overflow = len(self._records) - self.max_records
        if overflow > 0:
            oldest = sorted(self._records.values(), key=lambda r: r.started)
            for rec in oldest[:overflow]:
                self._records.pop(rec.pid, None)
            return len(dead) + overflow
        return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


# =========================================================================
# Module-level gate — this is what the rest of lunad imports
# =========================================================================

_ledger_lock = threading.Lock()
_LEDGER: SpawnLedger | None = None
_AUDIT_HOOK: Any = None


def ledger() -> SpawnLedger:
    global _LEDGER
    with _ledger_lock:
        if _LEDGER is None:
            _LEDGER = SpawnLedger()
        return _LEDGER


def use_ledger(new: SpawnLedger | None) -> SpawnLedger | None:
    """Swap the process-wide ledger. Tests use this; nothing else should."""
    global _LEDGER
    with _ledger_lock:
        old, _LEDGER = _LEDGER, new
    return old


def set_audit_hook(hook: Any) -> None:
    """Give the firewall somewhere to record signals and refusals.

    A callable ``hook(action, **fields)``. Kept as a hook rather than an import
    so ``safety`` stays at the bottom of the dependency graph: audit imports
    nothing from here, and here imports nothing from audit.
    """
    global _AUDIT_HOOK
    _AUDIT_HOOK = hook


def _audit(action: str, **fields: Any) -> None:
    hook = _AUDIT_HOOK
    if hook is None:
        return
    try:
        hook(action, **fields)
    except Exception:  # noqa: BLE001 - auditing must never break the caller
        log.exception("audit hook failed")


def may_signal(pid: int) -> bool:
    """The single choke point. See :meth:`SpawnLedger.may_signal`."""
    return ledger().may_signal(pid)


def signal_pid(pid: int, sig: int = signal.SIGTERM, *, reason: str = "") -> bool:
    """Signal one pid, or raise :class:`SignalRefused`.

    Returns False (without raising) when the process has already gone: a race
    with a child that exited on its own is not a firewall event.
    """
    lg = ledger()
    if not lg.may_signal(pid):
        why = lg.why_not(pid)
        _audit("signal.refused", pid=pid, signal=_signame(sig), why=reason,
               reason=why, ok=False)
        raise lg.refuse(pid, why)
    try:
        os.kill(int(pid), sig)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        _audit("signal.failed", pid=pid, signal=_signame(sig), why=reason,
               reason=str(exc), ok=False)
        raise
    _audit("signal.sent", pid=pid, signal=_signame(sig), why=reason, ok=True)
    return True


def signal_group(pid: int, sig: int = signal.SIGTERM, *,
                 reason: str = "") -> bool:
    """Signal the process *group* led by ``pid``.

    Only ever used on children spawned with ``start_new_session=True``, whose
    pgid is their own pid. If the pgid is anything else the group belongs to
    somebody else — quite possibly the user's shell — and the call is refused.
    That is the exact mistake that killed a previous agent's own terminal.
    """
    lg = ledger()
    if not lg.may_signal(pid):
        why = lg.why_not(pid)
        _audit("signal.refused", pid=pid, signal=_signame(sig), group=True,
               why=reason, reason=why, ok=False)
        raise lg.refuse(pid, why)
    try:
        pgid = os.getpgid(int(pid))
    except ProcessLookupError:
        return False
    if pgid != int(pid):
        why = (f"pid {pid} is not its own process group leader (pgid {pgid}); "
               "its group contains processes Luna did not spawn")
        _audit("signal.refused", pid=pid, signal=_signame(sig), group=True,
               why=reason, reason=why, ok=False)
        raise lg.refuse(pid, why)
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        _audit("signal.failed", pid=pid, signal=_signame(sig), group=True,
               why=reason, reason=str(exc), ok=False)
        raise
    _audit("signal.sent", pid=pid, signal=_signame(sig), group=True,
           why=reason, ok=True)
    return True


def signal_self(sig: int = signal.SIGTERM) -> None:
    """The one deliberate exception: the daemon stopping itself.

    A process may always signal itself, and ``may_signal`` refuses ``getpid()``
    precisely so that self-termination cannot be reached by accident through
    the pid path. Kept here so that every ``os.kill`` in the package lives in
    this file and a grep proves it.
    """
    _audit("signal.self", pid=os.getpid(), signal=_signame(sig), ok=True,
           why="shutdown requested")
    os.kill(os.getpid(), sig)


def terminate(proc: subprocess.Popen, *, grace: float = 5.0,
              reason: str = "") -> bool:
    """Stop a child Luna spawned: SIGTERM, wait, then SIGKILL.

    Signals the child's process group when it leads one (everything Luna
    spawns does, via ``start_new_session=True``) so that the agent's own
    children go with it. Falls back to the single pid otherwise.
    """
    if proc.poll() is not None:
        return False
    pid = proc.pid
    lg = ledger()
    if not lg.may_signal(pid):
        why = lg.why_not(pid)
        _audit("signal.refused", pid=pid, signal="SIGTERM", why=reason,
               reason=why, ok=False)
        raise lg.refuse(pid, why)

    _kill_best_effort(pid, signal.SIGTERM, reason)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    if proc.poll() is not None:
        return True
    _kill_best_effort(pid, signal.SIGKILL, reason + " (escalated)")
    return True


def _kill_best_effort(pid: int, sig: int, reason: str) -> None:
    """Group first, single pid second.

    A child that exited between the gate check and here is a race, not a
    firewall event, so a vanished pid returns quietly. A pid that is alive but
    not Luna's still raises: that would mean the ledger changed underneath us,
    and silence there is exactly the bug this module exists to prevent.
    """
    if not is_alive(pid):
        return
    try:
        if os.getpgid(pid) == pid:
            signal_group(pid, sig, reason=reason)
            return
    except ProcessLookupError:
        return
    try:
        signal_pid(pid, sig, reason=reason)
    except ProcessLookupError:
        return


def _signame(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except ValueError:
        return str(sig)


# =========================================================================
# The spawn side of the same gate
# =========================================================================


def spawn(argv: list[str], *, kind: str = "child", job_id: str | None = None,
          durable: bool = True, note: str = "",
          **popen_kw: Any) -> subprocess.Popen:
    """``Popen`` plus registration, as one operation.

    Every child gets its own session (``start_new_session=True``) so that a
    later termination can take its whole group without ever reaching a process
    group Luna does not own. Registration happens immediately after the fork,
    which is the only instant at which the pid unambiguously belongs to this
    child.
    """
    popen_kw.setdefault("start_new_session", True)
    proc = subprocess.Popen(argv, **popen_kw)
    rec = ledger().record(proc.pid, argv, kind=kind, job_id=job_id,
                          durable=durable, note=note)
    _audit("process.spawned", pid=proc.pid, kind=kind, job_id=job_id,
           cmd=argv[:12], ok=True,
           why=note or f"{kind} started by lunad",
           starttime=(rec.starttime if rec else None))
    return proc


def reap(proc: subprocess.Popen | None) -> None:
    """Drop a finished child from the allowlist. Safe to call twice."""
    if proc is None:
        return
    ledger().forget(proc.pid)
