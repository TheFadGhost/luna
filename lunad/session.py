"""Conversation sessions, so the prompt cache is paid for once.

The Phase 0 daemon spawned a fresh ``claude`` for every ask. Each of those
started a new conversation, so the ~8k-token system prompt (persona + tier 1)
was *cache-created* every single time — the expensive direction, at 1.25x
input price — and never read back. Measured at roughly $0.05 an ask.

The fix is not a smaller prompt. It is to stop throwing the conversation away:
``--session-id <uuid>`` on the first turn, ``--resume <uuid>`` on every turn
after, so the identical prefix is a cache *read* at 0.1x instead.

Three things follow from that, and they are the whole reason this module
exists rather than a single variable in the server:

1. **The prefix must be byte-identical between turns**, or the cache breaks at
   the first differing token and the saving evaporates. So per-request tier-2
   recall may no longer live in the system prompt; the server moves it into the
   user message instead. What stays in the prefix is persona + tier 1, both of
   which change rarely.
2. **A tier-1 write invalidates the session.** The frozen block is baked into
   the resumed conversation's history; carrying on would mean Luna answering
   from memory she no longer has. Fingerprint it, and start clean when it moves.
3. **A session cannot be resumed forever.** Its history grows with every turn,
   and the cache has a short TTL anyway, so an idle or over-long session is
   retired rather than resumed into.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import config

log = logging.getLogger("lunad.session")


def fingerprint(*parts: str) -> str:
    """A stable id for the cacheable prefix. Any change to it retires sessions."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


class SessionBusy(RuntimeError):
    """A second caller hit a conversation whose first turn is still in flight.

    Raised rather than silently racing: two concurrent callers on the same
    conversation key must never both compute "this is turn one" and hand the
    identical ``--session-id`` to the agent. See :meth:`SessionManager.acquire`.
    """

    def __init__(self, key: str) -> None:
        super().__init__(
            f"conversation {key!r} already has a first turn in flight; "
            "try again once it finishes")
        self.key = key


@dataclass
class Session:
    key: str
    session_id: str
    prefix: str                       # fingerprint of persona + tier 1
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turns: int = 0
    started: bool = False             # has the id been handed to the agent yet
    cost_usd: float = 0.0
    # The thread currently holding this session unstarted, or None if nobody
    # is (a fresh session, or one whose turn already concluded). Distinct from
    # `started`: a caller that re-acquires *on its own thread* after its own
    # first attempt returned (e.g. to retry, or the resume-refused-so-start-
    # fresh path in server.py) must get the same treatment as always — this
    # field only ever blocks a genuinely different, concurrent thread.
    pending_owner: int | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "session_id": self.session_id,
            "turns": self.turns,
            "age_s": round(time.time() - self.created, 1),
            "idle_s": round(time.time() - self.last_used, 1),
            "cost_usd": round(self.cost_usd, 6),
            "resumable": self.started,
        }


class SessionManager:
    """One live conversation per key, retired on staleness or memory change."""

    def __init__(self, idle_s: float | None = None,
                 max_turns: int | None = None,
                 pending_wait_s: float | None = None) -> None:
        self.idle_s = config.SESSION_IDLE_S if idle_s is None else idle_s
        self.max_turns = (config.SESSION_MAX_TURNS if max_turns is None
                          else max_turns)
        # How long a second, concurrent caller on the same conversation waits
        # for the first turn to finish establishing the session before giving
        # up with SessionBusy. Bounded by the same ceiling a single agent call
        # is allowed to take — waiting any longer could never pay off, since
        # the daemon itself would have given up on the first turn by then.
        self.pending_wait_s = (config.AGENT_TIMEOUT_S if pending_wait_s is None
                               else pending_wait_s)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.counters = {"new": 0, "resumed": 0, "retired": 0, "rejected": 0}

    # -- lifecycle -------------------------------------------------------

    def acquire(self, key: str, prefix: str, now: float | None = None) -> Session:
        """The session to use for this turn, creating or rotating as needed.

        Two concurrent callers on the same conversation must never both be
        handed the same *unstarted* session: both would compute "this is turn
        one" in :meth:`args_for` and race on ``--session-id``. So an unstarted
        session remembers which thread is currently carrying it
        (``pending_owner``); a second, different thread that shows up while it
        is still unstarted waits here — bounded by ``pending_wait_s`` — for the
        first turn to either finish (``succeeded``) or the session to be
        dropped/retired, rather than being handed the same session to race on.

        A retry from the *same* thread (the caller's own first attempt already
        returned to it, e.g. to try again, or server.py's resume-refused
        fallback) is not a race and is served immediately, same as before this
        existed — that is what ``pending_owner in (None, owner)`` allows.

        If nothing changes before the wait times out, :class:`SessionBusy` is
        raised rather than the caller blocking forever: an ask can be long, and
        a hung first turn must not wedge every later caller on this
        conversation for good.
        """
        auto_now = now is None
        now = time.time() if auto_now else now
        key = key or config.DEFAULT_CONVERSATION
        owner = threading.get_ident()
        deadline = time.monotonic() + self.pending_wait_s
        with self._cv:
            while True:
                if auto_now:
                    now = time.time()
                existing = self._sessions.get(key)
                reason = self._retire_reason(existing, prefix, now)
                if reason:
                    log.info("retiring session", extra={"conversation": key,
                                                        "reason": reason,
                                                        "turns": existing.turns})
                    self._sessions.pop(key, None)
                    self.counters["retired"] += 1
                    existing = None
                    self._cv.notify_all()
                if existing is None:
                    existing = Session(key=key, session_id=str(uuid.uuid4()),
                                       prefix=prefix, created=now, last_used=now,
                                       pending_owner=owner)
                    self._sessions[key] = existing
                    self.counters["new"] += 1
                    return existing
                if existing.started or existing.pending_owner in (None, owner):
                    if not existing.started:
                        existing.pending_owner = owner
                    self.counters["resumed"] += 1
                    return existing
                # A different thread's first turn is genuinely in flight for
                # this key. Wait for it to conclude rather than racing it.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.counters["rejected"] += 1
                    log.warning("rejecting a concurrent ask: a first turn is "
                               "already in flight", extra={"conversation": key})
                    raise SessionBusy(key)
                self._cv.wait(timeout=remaining)

    def _retire_reason(self, session: Session | None, prefix: str,
                       now: float) -> str | None:
        if session is None:
            return None
        if session.prefix != prefix:
            return "tier-1 memory changed"
        if now - session.last_used > self.idle_s:
            return f"idle for more than {self.idle_s:.0f}s"
        if session.turns >= self.max_turns:
            return f"reached {self.max_turns} turns"
        return None

    def args_for(self, session: Session) -> dict[str, str | None]:
        """``--session-id`` on turn one, ``--resume`` thereafter."""
        if session.started:
            return {"session_id": None, "resume": session.session_id}
        return {"session_id": session.session_id, "resume": None}

    def succeeded(self, session: Session, cost_usd: float | None = None,
                  reported_id: str | None = None) -> None:
        with self._cv:
            session.started = True
            session.pending_owner = None
            session.turns += 1
            session.last_used = time.time()
            if cost_usd:
                session.cost_usd += cost_usd
            # The agent is the authority on its own session id; if it renamed
            # the conversation, follow it rather than resuming into nothing.
            if reported_id and reported_id != session.session_id:
                log.info("agent reported a different session id",
                         extra={"asked": session.session_id,
                                "got": reported_id})
                session.session_id = reported_id
            # Wake anyone waiting in acquire() for this first turn to land.
            self._cv.notify_all()

    def drop(self, key: str) -> bool:
        """Forget a conversation. Used when a resume is refused by the agent."""
        with self._cv:
            gone = self._sessions.pop(key, None) is not None
            if gone:
                self._cv.notify_all()
        if gone:
            self.counters["retired"] += 1
        return gone

    def clear(self) -> int:
        with self._cv:
            n = len(self._sessions)
            self._sessions.clear()
            if n:
                self._cv.notify_all()
        self.counters["retired"] += n
        return n

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._sessions.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
