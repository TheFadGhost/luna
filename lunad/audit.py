"""Append-only audit log. ARCHITECTURE.md section 7.

Luna has full autonomy and asks no permission. The thing that makes that
tolerable is not a prompt, it is a record: every dispatched task, every process
spawned, every signal delivered or refused, every memory write, with what it
was for and how it ended.

Three properties, in the order they matter:

**Append-only.** The file is opened ``"a"`` and never truncated, never
rewritten, never rotated. A log Luna can edit is not evidence. It grows
without bound on purpose; one line is a few hundred bytes and the machine has
disk.

**Durable.** Each line is flushed and ``fsync``'d before the call returns. An
action that is in the log but did not happen is recoverable confusion; an
action that happened but is not in the log is not.

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

from . import config

log = logging.getLogger("lunad.audit")

_ISO = "%Y-%m-%dT%H:%M:%S"


class AuditLog:
    """One JSON object per line, newest at the bottom."""

    def __init__(self, path: Path = config.AUDIT_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.written = 0

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

        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                self.written += 1
        except OSError as exc:
            log.error("could not append to the audit log",
                      extra={"path": str(self.path), "detail": str(exc),
                             "audit_action": action})
        return entry

    def hook(self, action: str, **fields: Any) -> None:
        """Adapter for :func:`lunad.safety.set_audit_hook`."""
        self.append(action, **fields)

    # -- reading ---------------------------------------------------------

    def read(self, since: float | None = None, limit: int | None = None,
             action: str | None = None,
             newest_first: bool = True) -> list[dict[str, Any]]:
        """Read entries back. Unparseable lines are surfaced, not hidden."""
        entries: list[dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
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
                    if since is not None and float(obj.get("ts") or 0) < since:
                        continue
                    if action and not str(obj.get("action", "")).startswith(action):
                        continue
                    entries.append(obj)
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("could not read the audit log",
                        extra={"path": str(self.path), "detail": str(exc)})
            return []
        if newest_first:
            entries.reverse()
        if limit is not None and limit > 0:
            entries = entries[:limit]
        return entries

    def stats(self) -> dict[str, Any]:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {"path": str(self.path), "size_bytes": size,
                "written_this_session": self.written}


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
