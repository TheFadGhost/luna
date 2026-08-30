"""Append-only audit log. ARCHITECTURE.md section 7.

Luna has full autonomy and asks no permission. The thing that makes that
tolerable is not a prompt, it is a record: every dispatched task, every process
spawned, every signal delivered or refused, every memory write, with what it
was for and how it ended.

Four properties, in the order they matter:

**Append-only.** The file is opened ``"a"`` and never truncated, never
rewritten, never edited in place. A log Luna can rewrite is not evidence.

**Durable.** Each line is flushed and ``fsync``'d before the call returns. An
action that is in the log but did not happen is recoverable confusion; an
action that happened but is not in the log is not.

**Bounded without losing anything silently.** This log used to grow forever,
on purpose, and the purpose was sound: a rotation that quietly drops the week
you are asking about is worse than a large file. So rotation here *moves*
bytes rather than deleting them. Past ``[audit] max_mb`` the live file becomes
``audit.jsonl.1``, each sibling shifts up one, and only the oldest of
``[audit] keep`` is ever removed — and the removal is itself an entry,
``audit.rotated``, written as the first line of the new file, naming what was
renamed and what was dropped. So the chain can be walked backwards from the
live file and any gap in it has a line explaining itself. :meth:`read` reads
the siblings too, stopping at the first file that cannot contain anything the
caller asked for, so ``luna audit --since 30m`` does not pay for months of
history.

The rename happens between two whole lines, while the lock is held and after
the previous line's ``fsync`` — never mid-write. A reader that opened the file
before the rename goes on reading the renamed inode, which still holds every
byte it held before.

**Honest about undo.** ``undo`` is recorded only where an inverse genuinely
exists and is known at the time — removing the tier-1 entry that was just
appended, stopping the job that was just started. Nothing invents an inverse
for something irreversible: a fabricated undo command is worse than the
absence of one, because it invites a user to run it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config, settings as settings_mod

log = logging.getLogger("lunad.audit")

_ISO = "%Y-%m-%dT%H:%M:%S"
_MB = 1_048_576


class AuditLog:
    """One JSON object per line, newest at the bottom."""

    def __init__(self, path: Path = config.AUDIT_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.written = 0
        self.rotations = 0

    # -- writing ---------------------------------------------------------

    def append(self, action: str, *, actor: str = "luna", ok: bool | None = None,
               why: str = "", undo: dict[str, Any] | None = None,
               **fields: Any) -> dict[str, Any]:
        """Record one action. Returns the entry as written.

        ``why`` is the intent, not the mechanics: "the user asked for a proof
        file" rather than "ran touch". The mechanics are in the other fields.
        A failure to write is logged and swallowed — the audit log must never
        be the reason an action fails — but it is loud in the daemon log.
        """
        entry = self._entry(action, actor=actor, ok=ok, why=why, undo=undo,
                            **fields)
        with self._lock:
            size = self._emit(entry)
            if size:
                self.written += 1
            ceiling = self._rotate_at()
            # Rotate *after* the write, on the position the file reached, so
            # the decision costs no extra syscall and can never land between a
            # line and its fsync. The consequence, stated because it will show
            # up in a file listing: a rotated sibling is one line past the
            # ceiling, never short of it.
            if ceiling and size >= ceiling:
                self._rotate(size, ceiling)
        return entry

    def _entry(self, action: str, *, actor: str = "luna",
               ok: bool | None = None, why: str = "",
               undo: dict[str, Any] | None = None,
               **fields: Any) -> dict[str, Any]:
        now = time.time()
        entry: dict[str, Any] = {
            "ts": round(now, 3),
            "iso": time.strftime(_ISO, time.localtime(now)),
            "actor": actor,
            "action": action,
            # The *writer*, not the subject. `pid` is left free for the
            # process an entry is about — a spawned job, a refused signal —
            # because that is the pid a reader is looking for.
            "by_pid": os.getpid(),
        }
        if why:
            entry["why"] = why
        if ok is not None:
            entry["ok"] = bool(ok)
        for key, value in fields.items():
            if value is not None:
                entry[key] = value
        if undo:
            entry["undo"] = undo
        return entry

    def _emit(self, entry: dict[str, Any]) -> int:
        """Write one entry. Returns the file's size after it, or 0 on failure.

        The caller holds the lock. The size comes from ``tell()`` on the handle
        that just wrote, which is free — a ``stat`` per line would be a syscall
        spent asking a question the write already answered.
        """
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
                return fh.tell()
        except OSError as exc:
            log.error("could not append to the audit log",
                      extra={"path": str(self.path), "detail": str(exc),
                             "audit_action": str(entry.get("action"))})
            return 0

    def hook(self, action: str, **fields: Any) -> None:
        """Adapter for :func:`lunad.safety.set_audit_hook`."""
        self.append(action, **fields)

    # -- rotation --------------------------------------------------------

    def _rotate_at(self) -> int:
        """The size past which the live file is rotated. 0 means never."""
        return max(0, _int_setting("audit.max_mb", config.AUDIT_MAX_MB)) * _MB

    def _keep(self) -> int:
        """How many numbered siblings survive. At least one."""
        return max(1, _int_setting("audit.keep", config.AUDIT_KEEP))

    def sibling(self, n: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{n}")

    def _rotate(self, size: int, ceiling: int) -> None:
        """Shift the chain up one and start a fresh live file.

        The caller holds the lock and the previous line is already on disk, so
        there is no half-written record anywhere in here. The oldest sibling is
        the only file that is *deleted*, and the entry that opens the new live
        file names it: a gap in the record has to be able to explain itself.

        A failure is logged and swallowed, like a failed append. A log that
        cannot rotate is a large log; a log that raises out of ``append`` is a
        daemon that cannot dispatch.
        """
        keep = self._keep()
        oldest = self.sibling(keep)
        dropped = str(oldest) if oldest.exists() else None
        try:
            oldest.unlink(missing_ok=True)
            for n in range(keep - 1, 0, -1):
                older = self.sibling(n)
                if older.exists():
                    os.replace(older, self.sibling(n + 1))
            os.replace(self.path, self.sibling(1))
            _sync_dir(self.path.parent)
        except OSError as exc:
            log.error("could not rotate the audit log; it keeps growing",
                      extra={"path": str(self.path), "detail": str(exc)})
            return
        self.rotations += 1
        log.info("rotated the audit log",
                 extra={"path": str(self.path), "bytes": size,
                        "keep": keep, "dropped": dropped})
        # First line of the new file, so the chain reads forwards from here.
        if self._emit(self._entry(
                "audit.rotated", ok=True,
                why="the audit log passed its size ceiling",
                bytes=size, ceiling=ceiling, keep=keep,
                rotated_to=str(self.sibling(1)), dropped=dropped)):
            self.written += 1

    def chain(self) -> list[Path]:
        """Every file this log has bytes in, newest first.

        Found on disk rather than counted up to ``keep``, and deliberately: a
        user who lowers ``[audit] keep`` from 8 to 5 leaves ``.6`` and ``.7``
        sitting there, and rotation will never touch them again because it only
        ever unlinks the oldest file *inside* the window. Reading them anyway
        is the difference between "history you stopped rotating" and "history
        that silently stopped existing". They can be deleted by hand; nothing
        here will do it for you.
        """
        numbered: list[tuple[int, Path]] = []
        try:
            for path in self.path.parent.glob(self.path.name + ".*"):
                suffix = path.name[len(self.path.name) + 1:]
                if suffix.isdigit():
                    numbered.append((int(suffix), path))
        except OSError:
            return [self.path] if self.path.exists() else []
        return ([self.path] if self.path.exists() else []) \
            + [p for _n, p in sorted(numbered)]

    # -- reading ---------------------------------------------------------

    def read(self, since: float | None = None, limit: int | None = None,
             action: str | None = None,
             newest_first: bool = True) -> list[dict[str, Any]]:
        """Read entries back, across the rotated siblings as well.

        Unparseable lines are surfaced, not hidden. Files are opened newest
        first and the walk stops at the first one that cannot contain anything
        else the caller asked for — an older file than the ``since`` bound, or
        one line past a satisfied ``limit``. Without that, every
        ``luna audit -n 40`` would read the whole retained history.
        """
        entries: list[dict[str, Any]] = []
        for path in self.chain():
            batch, oldest_ts = self._read_file(path, since, action)
            # Older file, so its entries belong in front of what we have.
            entries[:0] = batch
            if since is not None and oldest_ts is not None and oldest_ts < since:
                break
            if (since is None and newest_first
                    and limit is not None and limit > 0
                    and len(entries) >= limit):
                break
        if newest_first:
            entries.reverse()
        if limit is not None and limit > 0:
            entries = entries[:limit]
        return entries

    def _read_file(self, path: Path, since: float | None,
                   action: str | None) -> tuple[list[dict[str, Any]], float | None]:
        """One file's matching entries, oldest first, and its oldest ``ts``.

        The oldest timestamp is reported *unfiltered*: it is what tells the
        caller whether an even older file could still hold something.
        """
        entries: list[dict[str, Any]] = []
        oldest_ts: float | None = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, raw in enumerate(fh, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        obj = {"ts": 0.0, "iso": "", "action": "unparseable",
                               "line": lineno, "raw": raw[:200]}
                    if not isinstance(obj, dict):
                        continue
                    if oldest_ts is None:
                        oldest_ts = float(obj.get("ts") or 0)
                    if since is not None and float(obj.get("ts") or 0) < since:
                        continue
                    if action and not str(obj.get("action", "")).startswith(action):
                        continue
                    entries.append(obj)
        except FileNotFoundError:
            return [], None
        except OSError as exc:
            log.warning("could not read the audit log",
                        extra={"path": str(path), "detail": str(exc)})
            return [], None
        return entries, oldest_ts

    def stats(self) -> dict[str, Any]:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        rotated = self.chain()[1:]
        return {"path": str(self.path), "size_bytes": size,
                "written_this_session": self.written,
                "rotated_this_session": self.rotations,
                "rotates_at_bytes": self._rotate_at(),
                "siblings": len(rotated),
                "retained_bytes": size + sum(_size_of(p) for p in rotated)}


def _int_setting(dotted: str, fallback: int) -> int:
    """A whole number from the config, or the fallback constant.

    Settings are validated on the way in, so this only catches the case the
    validator cannot: a daemon reading a key before anything wrote a file.
    """
    try:
        return int(settings_mod.get(dotted, fallback))
    except (TypeError, ValueError):
        return fallback


def _sync_dir(path: Path) -> None:
    """fsync a directory, so a rename survives a power cut.

    Every line in this file is already fsync'd; a rotation that renamed the
    evidence and then lost the rename would leave two files claiming to be the
    same one. Once per 8 MB, this costs nothing worth measuring.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# =========================================================================
# `--since` parsing, shared by the CLI, the daemon and the tests
# =========================================================================

_REL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


def parse_since(value: str | float | None, now: float | None = None) -> float | None:
    """Turn ``30m`` / ``2d`` / ``2026-08-25`` / an epoch into an epoch.

    Returns ``None`` for "no lower bound". A value that cannot be understood
    raises, rather than silently reading the whole log: an audit query that
    quietly ignores its filter is a way to miss the thing you were looking for.
    """
    if value is None or value == "":
        return None
    now = time.time() if now is None else now
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = _REL_RE.match(text)
    if match:
        return now - float(match.group(1)) * _UNITS[match.group(2).lower()]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"cannot read {value!r} as a time. Use 30m, 6h, 2d, 1w, "
            "YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, or a unix timestamp."
        ) from exc


# =========================================================================
# Process-wide instance
# =========================================================================

_lock = threading.Lock()
_LOG: AuditLog | None = None


def audit() -> AuditLog:
    global _LOG
    with _lock:
        if _LOG is None:
            _LOG = AuditLog()
        return _LOG


def use_audit(new: AuditLog | None) -> AuditLog | None:
    """Swap the process-wide log. Tests use this; nothing else should."""
    global _LOG
    with _lock:
        old, _LOG = _LOG, new
    return old


def record(action: str, **fields: Any) -> dict[str, Any]:
    return audit().append(action, **fields)


def undo_for_memory_append(file: str, index: int) -> dict[str, Any]:
    """The one memory inverse that genuinely exists.

    Appending entry *n* to a tier-1 file is undone by removing index *n* —
    provided nothing has been appended since, which the entry's timestamp lets
    a reader check. Stated as a command the user can actually run.
    """
    return {"what": f"remove the entry appended to {file}",
            "cmd": ["luna", "memory", "rm", str(index), "--file", file],
            "valid_while": "no further entry has been appended to that file"}


def summarise(entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("action", "?"))
        counts[key] = counts.get(key, 0) + 1
    return counts
