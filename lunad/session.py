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

And one thing that does *not* follow from cache economics at all, but shares
this module because this is where a conversation ends:

4. **Retiring a session is an act of forgetting, and it has to be paid for.**
   Everything above retires a conversation mid-flight — a tier-1 write, a turn
   count, a token budget — and the turn after a retirement starts a thread that
   has never heard of the turn before it. Tier 1 and tier 2 both survive
   (they are rebuilt into every prompt), so what is lost is narrow but sharp:
   the *deixis*. "The second one." "Do that again." "Why?" A retirement the
   user has to notice is a failed retirement.

   So a session created to replace a live one is born **carrying** the reason
   it exists (:attr:`Session.carry`), and the caller is expected to bridge the
   gap on that first turn — see ``Memory.bridge_block`` and ``Daemon._ask``.
   An *idle* retirement deliberately carries nothing: half an hour of silence
   is a new conversation, and dragging the last one into it would be worse
   than starting clean.

   The token budget in :data:`config.SESSION_TOKEN_BUDGET` is the fourth
   retirement reason and the newest. Measured on this machine (see
   ``docs/_context-notes.md``): `gpt-5.6-luna` advertises a 272,000-token
   context window, a resumed thread was driven to **283,135 input tokens and
   answered normally**, and `codex exec` exposes none of the `notes` /
   `new_context` / `get_context_remaining` tools that let the model manage its
   own window elsewhere. So the wall is real but generous, nothing below it
   degrades, and nothing above it is handled for us. A conversational turn
   grows the history by ~155 tokens, which puts the wall ~1,600 turns away and
   far behind the turn cap; a *tool-using* turn can grow it by thousands, which
   does not. The budget costs one integer comparison on a number codex already
   reports, and turns a hard failure nobody handles into an ordinary rollover.
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
    #: Input tokens the agent reported for the most recent turn on this
    #: session — the *whole* request, history included, which is exactly the
    #: number the context window is measured against. 0 until a turn has
    #: landed, and 0 forever for an adapter that reports no usage, which is
    #: why the budget check below is written to be inert at 0 rather than
    #: guessing.
    input_tokens: int = 0
    #: Why this session exists, when it exists because a live one was retired
    #: out from under the conversation. "" for a genuinely fresh start — no
    #: predecessor, or a predecessor nobody should be reminded of. The caller
    #: reads this once, bridges the gap on its first turn, and
    #: :meth:`succeeded` clears it.
    carry: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "session_id": self.session_id,
            "turns": self.turns,
            "age_s": round(time.time() - self.created, 1),
            "idle_s": round(time.time() - self.last_used, 1),
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
            "resumable": self.started,
            "carrying": self.carry,
        }


class SessionManager:
    """One live conversation per key, retired on staleness or memory change."""

    def __init__(self, idle_s: float | None = None,
                 max_turns: int | None = None,
                 pending_wait_s: float | None = None,
                 token_budget: int | None = None) -> None:
        self.idle_s = config.SESSION_IDLE_S if idle_s is None else idle_s
        self.max_turns = (config.SESSION_MAX_TURNS if max_turns is None
                          else max_turns)
        # The history ceiling, in input tokens as the agent reports them.
        # 0 disables the check entirely; so does an adapter that reports no
        # usage, because an unknown size must never be treated as a large one.
        self.token_budget = (config.SESSION_TOKEN_BUDGET if token_budget is None
                             else token_budget)
        # How long a second, concurrent caller on the same conversation waits
        # for the first turn to finish establishing the session before giving
        # up with SessionBusy. Bounded by the same ceiling a single agent call
        # is allowed to take — waiting any longer could never pay off, since
        # the daemon itself would have given up on the first turn by then.
        self.pending_wait_s = (config.AGENT_TIMEOUT_S if pending_wait_s is None
                               else pending_wait_s)
        self._sessions: dict[str, Session] = {}
        # Reasons owed to conversations whose session has gone but whose next
        # session has not been created yet — `drop()` is called from one place
        # and `acquire()` from another, with an agent call in between.
        self._owed: dict[str, str] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.counters = {"new": 0, "resumed": 0, "retired": 0, "rejected": 0,
                         "carried": 0, "over_budget": 0}

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
                retire = self._retire_reason(existing, prefix, now)
                if retire is not None:
                    carries, reason = retire
                    log.info("retiring session", extra={"conversation": key,
                                                        "reason": reason,
                                                        "turns": existing.turns,
                                                        "input_tokens":
                                                            existing.input_tokens})
                    self._sessions.pop(key, None)
                    self.counters["retired"] += 1
                    # A conversation that was still live is owed a bridge; an
                    # idle one is not, and must not overwrite an older debt
                    # with silence either — hence the guard rather than a
                    # plain assignment.
                    if carries and existing.started:
                        self._owed[key] = reason
                    existing = None
                    self._cv.notify_all()
                if existing is None:
                    existing = Session(key=key, session_id=str(uuid.uuid4()),
                                       prefix=prefix, created=now, last_used=now,
                                       pending_owner=owner,
                                       carry=self._owed.pop(key, ""))
                    self._sessions[key] = existing
                    self.counters["new"] += 1
                    if existing.carry:
                        self.counters["carried"] += 1
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
                       now: float) -> tuple[bool, str] | None:
        """``(carries, why)`` if this session must go, else ``None``.

        ``carries`` says whether the replacement is owed a bridge back to
        what was just being talked about. Every reason here except idleness
        interrupts a conversation that was still going; idleness *is* the
        conversation ending, and bridging across it would drag half an hour
        of stale context into a fresh question.
        """
        if session is None:
            return None
        if session.prefix != prefix:
            return True, "tier-1 memory changed"
        if now - session.last_used > self.idle_s:
            return False, f"idle for more than {self.idle_s:.0f}s"
        if session.turns >= self.max_turns:
            return True, f"reached {self.max_turns} turns"
        if self.token_budget and session.input_tokens >= self.token_budget:
            self.counters["over_budget"] += 1
            return True, (f"history reached {session.input_tokens} input "
                          f"tokens, over the {self.token_budget} budget")
        return None

    def args_for(self, session: Session) -> dict[str, str | None]:
        """``--session-id`` on turn one, ``--resume`` thereafter."""
        if session.started:
            return {"session_id": None, "resume": session.session_id}
        return {"session_id": session.session_id, "resume": None}

    def succeeded(self, session: Session, cost_usd: float | None = None,
                  reported_id: str | None = None,
                  usage: dict[str, Any] | None = None) -> None:
        """Record a landed turn. ``usage`` is the agent's own token report.

        Only ``input_tokens`` is taken, and only when it reads as a positive
        integer: it is the size of the whole request — system prompt, history
        and this turn's message — which is the one number the context window
        is actually measured against. Anything else (missing, zero, a string,
        an adapter that reports nothing) leaves the count alone, so the budget
        check stays inert rather than acting on a guess.
        """
        with self._cv:
            session.started = True
            session.pending_owner = None
            session.turns += 1
            session.last_used = time.time()
            # The bridge, if one was owed, has now been spoken over. Clearing
            # it here rather than at read time means a turn that *failed*
            # keeps the debt and the retry still bridges.
            session.carry = ""
            if isinstance(usage, dict):
                seen = usage.get("input_tokens")
                if isinstance(seen, int) and not isinstance(seen, bool) and seen > 0:
                    session.input_tokens = seen
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

    def drop(self, key: str, carry: str = "") -> bool:
        """Forget a conversation. Used when a resume is refused by the agent.

        ``carry`` is the reason to hand the *next* session for this key, for
        the same purpose as a retirement's: a refused resume is the one
        failure mode that arrives with no warning at all — the agent's own
        session store was cleaned up under us, or its context finally gave
        out — and the turn that heals it is the turn most in need of a bridge.
        Held on the manager rather than the session because the session is
        precisely what has just been thrown away.
        """
        with self._cv:
            gone = self._sessions.pop(key, None) is not None
            if gone:
                if carry:
                    self._owed[key] = carry
                self._cv.notify_all()
        if gone:
            self.counters["retired"] += 1
        return gone

    def clear(self) -> int:
        with self._cv:
            n = len(self._sessions)
            self._sessions.clear()
            # An explicit `luna session clear` is the user asking to start
            # again, not an interruption to paper over. Any debt goes with it.
            self._owed.clear()
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
