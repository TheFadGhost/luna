"""Luna's memory. ARCHITECTURE.md section 4, all three tiers.

Tier 1 — curated identity, always in the prompt, hard character caps.
Tier 2 — episodic, SQLite + FTS5, searched on demand, salience-scored.
Tier 3 — derived profile, rebuilt from tier 2, never hand-written.

The one behaviour inherited wholesale from Hermes Agent (Nous Research, MIT)
is the cap: a write that would overflow a tier-1 file is REJECTED with a
report of current usage, never silently truncated. Overflow is a signal to
consolidate, and swallowing it is how a curated file rots into a log.

The tiers are separated by lifetime, not by subject. Tier 1 is small and
expensive because it is in every prompt; tier 2 is large and cheap because it
is only read when something matches; tier 3 is small, free and disposable
because it is a *measurement* of tier 2 rather than a copy of it. Deleting
profile.json costs nothing — the next rebuild reproduces it. Deleting
episodes.db loses history for good, and deleting LUNA.md loses text nobody
else has. That ordering is the whole reason the tiers are three files and not
one.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config
from . import settings as settings_mod

# =========================================================================
# Errors
# =========================================================================


class MemoryError(Exception):
    """Base class for memory faults that a client should see."""

    def to_dict(self) -> dict[str, Any]:
        return {"error": type(self).__name__, "message": str(self)}


class MemoryCapExceeded(MemoryError):
    """A tier-1 write would push the file past its hard character cap.

    Carries everything the caller needs to decide what to consolidate, so the
    daemon never has to guess and the user never gets a bare "too long".
    """

    def __init__(
        self,
        name: str,
        cap: int,
        current_chars: int,
        proposed_chars: int,
        entry_count: int,
    ) -> None:
        self.name = name
        self.cap = cap
        self.current_chars = current_chars
        self.proposed_chars = proposed_chars
        self.entry_count = entry_count
        self.overflow = proposed_chars - cap
        self.usage_pct = round(100.0 * current_chars / cap, 1) if cap else 0.0
        self.proposed_pct = round(100.0 * proposed_chars / cap, 1) if cap else 0.0
        super().__init__(
            f"{name} write rejected: would be {proposed_chars}/{cap} chars "
            f"({self.proposed_pct}%), over cap by {self.overflow}. "
            f"Currently {current_chars}/{cap} ({self.usage_pct}%) across "
            f"{entry_count} entries. Consolidate before writing; "
            f"nothing was truncated."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "MemoryCapExceeded",
            "message": str(self),
            "file": self.name,
            "cap": self.cap,
            "current_chars": self.current_chars,
            "proposed_chars": self.proposed_chars,
            "overflow": self.overflow,
            "usage_pct": self.usage_pct,
            "proposed_pct": self.proposed_pct,
            "entry_count": self.entry_count,
        }


class FTS5Unavailable(MemoryError):
    """This interpreter's sqlite3 was built without FTS5."""


# =========================================================================
# Tier 1 — curated identity files
# =========================================================================

_DELIM = config.ENTRY_DELIMITER
_ENTRY_RE = re.compile(rf"^{re.escape(_DELIM)}[ \t]?", re.MULTILINE)


def _fsync_best_effort(fd: int) -> None:
    """fsync, but never let a filesystem/platform that refuses it raise here.

    Durability is an upgrade over plain atomicity, not a replacement for it:
    the rename in :func:`atomic_write` is what every reader's correctness
    depends on, and that already works without this. A `tmpfs` scratch dir,
    a sandboxed test tree, or a platform where `fsync` is refused on a
    directory descriptor must not turn a memory write into an unhandled
    exception on the answer path -- so this is best-effort and silent.
    """
    try:
        os.fsync(fd)
    except OSError:
        pass


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` so that no reader ever sees a half-written file.

    Temp file beside the target, ``fsync``ed, then ``Path.replace`` (which is
    ``os.replace``, atomic within one filesystem), then the containing
    directory is ``fsync``ed too. Every file memory owns goes through here —
    the tier-1 files and the tier-3 profile alike — because the failure being
    prevented is identical for both: a daemon killed mid-write leaving a
    truncated ``LUNA.md``, which is worse than no file at all because it still
    parses. One mechanism that is known to work beats two that look similar.

    The rename alone is atomic against a killed process: a reader sees either
    the old file or the new one, never a mix. It is not durable against power
    loss, because a rename can sit in the page cache and vanish with it --
    the old file, the new file, or (transiently, mid-rename, on some
    filesystems) neither may be what is on disk after the machine comes back.
    The two ``fsync`` calls close that gap: one flushes the new content to
    disk before the rename is attempted, the other flushes the directory
    entry the rename changed. Both are best-effort (see
    :func:`_fsync_best_effort`) — they harden this against a lost write, they
    do not gate it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        _fsync_best_effort(f.fileno())
    tmp.replace(path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        _fsync_best_effort(dir_fd)
    finally:
        os.close(dir_fd)


def parse_entries(text: str) -> list[str]:
    """Split a tier-1 file into its ``§``-delimited entries.

    An entry starts at a line beginning with ``§`` and runs to the next such
    line. Text before the first delimiter is not discarded: it is returned as
    a leading entry, so a hand-edited file survives a read/write round trip
    instead of losing whatever was at the top.
    """
    if not text.strip():
        return []
    parts = _ENTRY_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def render_entries(entries: Sequence[str]) -> str:
    """Inverse of :func:`parse_entries`. ``parse(render(x)) == x``."""
    cleaned = [e.strip() for e in entries if e and e.strip()]
    if not cleaned:
        return ""
    return "\n\n".join(f"{_DELIM} {e}" for e in cleaned) + "\n"


@dataclass
class Tier1File:
    """A capped, ``§``-delimited curated memory file.

    All writes are atomic (temp file + ``os.replace``) and all writes are
    cap-checked first. There is deliberately no ``force`` parameter.
    """

    path: Path
    cap_default: int
    name: str = ""
    cap_key: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.path.name

    @property
    def cap(self) -> int:
        """The cap in force *right now*.

        Resolved per call rather than frozen at construction: `[memory]
        luna_cap_chars` hot-reloads, and a cap captured when the daemon came
        up would keep rejecting writes at the old size until a restart —
        which is the failure hot reload exists to prevent. ``cap_key`` is
        empty for files with no key of their own (SOL.md), and those keep
        ``cap_default`` forever.
        """
        if self.cap_key:
            value = settings_mod.get(self.cap_key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return self.cap_default

    # -- reading ---------------------------------------------------------

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def entries(self) -> list[str]:
        return parse_entries(self.text())

    def usage(self) -> dict[str, Any]:
        """Current occupancy. ``pct`` is the number to show a human."""
        entries = self.entries()
        chars = len(render_entries(entries))
        return {
            "file": self.name,
            "path": str(self.path),
            "chars": chars,
            "cap": self.cap,
            "pct": round(100.0 * chars / self.cap, 1) if self.cap else 0.0,
            "remaining": self.cap - chars,
            "entries": len(entries),
        }

    # -- writing ---------------------------------------------------------

    def replace(self, entries: Iterable[str]) -> dict[str, Any]:
        """Replace the whole file. Raises :class:`MemoryCapExceeded` if over."""
        with self._lock:
            new = list(entries)
            rendered = render_entries(new)
            self._check_cap(rendered, len(new))
            self._atomic_write(rendered)
            return self.usage()

    def append(self, entry: str) -> dict[str, Any]:
        """Add one entry. Raises :class:`MemoryCapExceeded` if it would not fit.

        Nothing is written and nothing is truncated when it does not fit.
        """
        entry = entry.strip()
        if not entry:
            raise MemoryError(f"{self.name}: refusing to write an empty entry")
        with self._lock:
            return self.replace(self.entries() + [entry])

    def remove(self, index: int) -> dict[str, Any]:
        with self._lock:
            entries = self.entries()
            if not -len(entries) <= index < len(entries):
                raise MemoryError(
                    f"{self.name}: no entry at index {index} "
                    f"({len(entries)} entries)"
                )
            del entries[index]
            return self.replace(entries)

    def clear(self) -> dict[str, Any]:
        with self._lock:
            return self.replace([])

    # -- internals -------------------------------------------------------

    def _check_cap(self, rendered: str, entry_count: int) -> None:
        proposed = len(rendered)
        if proposed <= self.cap:
            return
        current_entries = self.entries()
        raise MemoryCapExceeded(
            name=self.name,
            cap=self.cap,
            current_chars=len(render_entries(current_entries)),
            proposed_chars=proposed,
            entry_count=len(current_entries),
        )

    def _atomic_write(self, rendered: str) -> None:
        atomic_write(self.path, rendered)


# =========================================================================
# Salience — ARCHITECTURE.md section 4, "Salience and decay"
# =========================================================================

# An explicit correction from the user. Kept tight on purpose: a false
# positive here pins a memory at 1.0 forever, which is expensive to undo.
_CORRECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\s*no[,.\s]",
        r"\bthat'?s (wrong|not right|incorrect|not what)\b",
        r"\bactually\b",
        r"\bi (said|meant|told you)\b",
        r"\bnot what i\b",
        r"\b(wrong|incorrect)\b",
        r"\bcorrection\b",
        r"\binstead of\b",
        r"\bstop (doing|saying|calling)\b",
        r"\bdon'?t (do|say|call|use) that\b",
    )
)

# Markers that an exchange has lasting consequence: a standing instruction, a
# preference, or something irreversible that happened.
_CONSEQUENCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfrom now on\b",
        r"\balways\b",
        r"\bnever\b",
        r"\bremember (that|this)?\b",
        r"\bi prefer\b",
        r"\bmy (rule|preference|convention)\b",
        r"\bmust (not )?\b",
        r"\bdeleted\b",
        r"\b(broke|broken|crashed|failed)\b",
        r"\birreversible\b",
        r"\bdefault(s)? to\b",
    )
)

# Component weights. They sum to 1.0 so the result is a genuine 0-1 score.
W_REPETITION = 0.30
W_CONSEQUENCE = 0.40
W_RECENCY = 0.30

REPETITION_SATURATES_AT = 4
CONSEQUENCE_SATURATES_AT = 3


def score_salience(
    user_text: str,
    luna_text: str = "",
    repetitions: int = 0,
) -> float:
    """Score one exchange 0-1 for how much it deserves to be remembered.

    Pure function: no clock, no I/O, no database. Given the same arguments it
    always returns the same number, which is what makes it testable.

    The four factors from ARCHITECTURE.md:

    * **explicit correction** — the user telling Luna she got something wrong.
      This short-circuits to ``1.0``. A 1.0 score is the sentinel for "never
      decays" (see :func:`decayed_salience`); corrections are permanent
      because re-learning a correction is the most expensive kind of mistake.
    * **repetition** (weight 0.30) — how many prior episodes look like this
      one. Saturates at 4; the fifth mention adds nothing.
    * **consequence** (weight 0.40) — standing instructions, stated
      preferences, and irreversible events, counted across both sides of the
      exchange and saturating at 3 distinct markers. The heaviest factor
      because consequence is what makes a memory worth carrying.
    * **recency** (weight 0.30) — a freshly scored episode is by definition
      maximally recent, so this contributes its full weight at write time and
      is then eroded at read time by :func:`decayed_salience`. It is a
      baseline, not a constant fudge: it is exactly the part of the score
      that time is allowed to take back.

    ``repetitions`` is supplied by the caller (see
    ``EpisodeStore.count_similar``) rather than computed here, to keep the
    function pure.
    """
    haystack = f"{user_text}\n{luna_text}"

    if any(p.search(user_text) for p in _CORRECTION_PATTERNS):
        return config.CORRECTION_SALIENCE

    rep = min(max(repetitions, 0), REPETITION_SATURATES_AT) / REPETITION_SATURATES_AT

    hits = sum(1 for p in _CONSEQUENCE_PATTERNS if p.search(haystack))
    con = min(hits, CONSEQUENCE_SATURATES_AT) / CONSEQUENCE_SATURATES_AT

    score = W_REPETITION * rep + W_CONSEQUENCE * con + W_RECENCY * 1.0
    return round(min(1.0, max(0.0, score)), 4)


def half_life_days_setting() -> float:
    """`[memory] decay_half_life_days`, or the fallback default.

    A function and not a module constant: decay is applied at read time, so a
    half-life changed in the GUI has to reach the very next recall without a
    restart. A module-level default argument would be bound at import.
    """
    value = settings_mod.get("memory.decay_half_life_days")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return float(config.SALIENCE_HALF_LIFE_DAYS)


def decayed_salience(
    salience: float,
    age_seconds: float,
    half_life_days: float | None = None,
) -> float:
    """Apply exponential time decay to a stored salience.

    ``half_life_days`` defaults to `[memory] decay_half_life_days`, read on
    every call. Pass a number to override it — the tests do.

    Applied at READ time. Rows are never mutated by decay: a stored score is a
    fact about the moment it was written, and rewriting history every time the
    clock ticks would make the database its own worst-behaved client.

    Corrections (``salience >= CORRECTION_SALIENCE``) do not decay, per
    ARCHITECTURE.md: "Corrections from the user score 1.0 and never decay."
    """
    if salience >= config.CORRECTION_SALIENCE:
        return salience
    if half_life_days is None:
        half_life_days = half_life_days_setting()
    if half_life_days <= 0:
        return salience
    age_days = max(0.0, age_seconds) / 86400.0
    return round(salience * math.pow(0.5, age_days / half_life_days), 6)


# =========================================================================
# Tier 2 — episodic store
# =========================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    surface   TEXT    NOT NULL,
    user_text TEXT    NOT NULL,
    luna_text TEXT    NOT NULL DEFAULT '',
    salience  REAL    NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    user_text,
    luna_text,
    content='episodes',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, user_text, luna_text)
    VALUES (new.id, new.user_text, new.luna_text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, user_text, luna_text)
    VALUES ('delete', old.id, old.user_text, old.luna_text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, user_text, luna_text)
    VALUES ('delete', old.id, old.user_text, old.luna_text);
    INSERT INTO episodes_fts(rowid, user_text, luna_text)
    VALUES (new.id, new.user_text, new.luna_text);
END;

-- A tiny key/value side table, and the only mutable state the store carries
-- that is not an episode. The consolidation pass keeps its watermark here --
-- how far through the episodes it has read -- rather than in a file of its
-- own, because a watermark is a fact *about these rows* and belongs under the
-- same commit as they do. It is also what makes the pass safe to interrupt:
-- see `CONSOLIDATED_THROUGH` below for the ordering that follows from it.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Meta key: the id of the last episode the consolidation pass has considered.
#:
#: The pass writes tier 1 first and moves this second, and never the other way
#: round. Killed between the two, it reconsiders episodes it has already seen,
#: which costs one repeated model call and produces no duplicate entries
#: because the proposal is made against the *current* tier-1 contents. Killed
#: in the other order it would skip them silently and forever, and a memory
#: system that loses things quietly is the failure worth designing against.
CONSOLIDATED_THROUGH = "consolidated_through_id"

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

# Deliberately large. The old ~30-word list let filler survive: "so anyway do
# you think I should do something about this" kept "do", "so", "think" and
# "something", which OR-joined into a query that matched 10 of 15 episodes in
# the real database on pure noise. Every word below is either closed-class
# (article, preposition, conjunction, pronoun, auxiliary/modal verb) or a
# generic verb/quantifier so common it carries no discriminating power on its
# own ("think", "get", "something"). Domain words never belong here — the
# risk of dropping something meaningful is in the rare-token gate below, not
# in this list, so this list can be generous.
_STOPWORDS = frozenset("""
    a an the and or of to in on for with what when did we i you is are was
    were be been it that this about how why who which whom whose where
    do does did doing done am have has had having will would shall should
    can could may might must
    i you we he she it they me him her us them my your his its our their
    mine yours ours theirs myself yourself himself herself itself ourselves
    yourselves themselves
    think thinks thought thinking know knows knew knowing knowledge
    want wants wanted wanting need needs needed needing
    like likes liked liking get gets got getting gotten
    go goes going went gone make makes made making
    say says said saying tell tells told telling ask asks asked asking
    come comes came coming look looks looked looking see sees saw seeing
    let lets letting
    so anyway anyways well just really very quite actually basically
    literally honestly probably maybe perhaps kind sort okay ok um uh
    yeah yep nope hey hi hello
    something anything nothing everything someone anyone everyone nobody
    some any all much many more most less least few several little lot lots
    bit thing things stuff way ways
    also too still even ever never always right one please thanks thank
    now then today tomorrow yesterday here there
""".split())

#: Minimum length for a survivor token to count as "reasonably rare" and
#: therefore worth building a query around at all. Below this, a query is
#: treated as content-free even if a handful of short non-stopword tokens
#: happen to survive — three or four two-and-three-letter leftovers are not a
#: signal.
_RARE_TOKEN_MIN_LEN = 4

#: :meth:`Memory.recall_block` refuses to inject an episode whose
#: ``coverage`` (fraction of the search query's content tokens actually
#: present — see ``EpisodeStore.search``) is below this. Deliberately not a
#: threshold on the raw BM25 score: BM25's IDF term collapses toward zero on
#: a tiny corpus (a single-episode test database scores a perfect one-word
#: match at bm25 ≈ 0, indistinguishable from noise), so it does not compare
#: across corpus sizes. Coverage does — it is a token-count ratio, the same
#: meaning whether the store holds 2 episodes or 20,000. Inclusive: an
#: OR-widened hit needs at least half the query's content tokens present, so
#: a two-token query sharing its one specific, already rarity-gated word
#: ("what voice did we choose" against an episode about choosing a voice)
#: still surfaces, while a three-token query sharing only one incidental
#: word out of three does not. Every AND-pass hit is coverage 1.0 by
#: construction and always clears it.
RECALL_COVERAGE_FLOOR = 0.5


def assert_fts5(conn: sqlite3.Connection | None = None) -> None:
    """Fail loudly, and early, if this Python's sqlite3 lacks FTS5.

    Tier-2 recall is not optional and there is no graceful degradation worth
    having, so this is checked at daemon start rather than on first search.
    """
    own = conn is None
    conn = conn or sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts5_probe")
    except sqlite3.Error as exc:
        raise FTS5Unavailable(
            "This Python's sqlite3 has no FTS5 module, so Luna cannot index "
            f"episodic memory (sqlite {sqlite3.sqlite_version}). "
            f"Underlying error: {exc}. "
            "Fix: install a python-sqlite/sqlite build with -DSQLITE_ENABLE_FTS5. "
            "Luna will not start without it."
        ) from exc
    finally:
        if own:
            conn.close()


def _content_tokens(raw: str) -> list[str]:
    """The lexical half of recall: tokenize, drop stopwords, gate on rarity.

    Returns ``[]`` — not the stopword-only tokens — when nothing survives
    that is worth searching on. The old behaviour fell back to the *raw*
    tokens (stopwords included) whenever filtering emptied the list, which
    is backwards: a query that is entirely filler ("what is it") is exactly
    the case that should retrieve nothing, not the case that should retrieve
    on the filler words verbatim. A query also has to clear
    :data:`_RARE_TOKEN_MIN_LEN` on at least one surviving token — a handful
    of short leftovers ("do", "so", "get" would already be gone, but e.g.
    "bit", "way") is still not a signal worth building a query around.

    This is the lexical (FTS5/BM25) half of recall only. The paraphrase half
    — "how much charge is left" never matching an episode that says
    "battery" — needs a semantic/embedding index, which is out of scope here
    (no new dependency, no downloaded model). The seam for it is
    :meth:`EpisodeStore.search`: run this function's FTS candidates and a
    future ANN lookup over an embeddings table side by side, union the
    episode ids, and let each candidate's relevance score (BM25 here, cosine
    there) feed the same coverage/floor gate ``recall_block`` already
    enforces. Nothing here needs to change shape to add that later — it is
    an additional candidate source into the same funnel.
    """
    tokens = [t for t in _FTS_TOKEN_RE.findall(raw.lower()) if t not in _STOPWORDS]
    if not any(len(t) >= _RARE_TOKEN_MIN_LEN or t.isdigit() for t in tokens):
        return []
    return tokens


def build_fts_query(raw: str, mode: str = "or") -> str | None:
    """Turn free text into a safe FTS5 MATCH expression.

    User queries contain apostrophes, hyphens and question marks, all of which
    are FTS5 syntax. Every token is quoted; returns ``None`` when
    :func:`_content_tokens` finds nothing usable.

    ``mode="or"`` (the default, and what every existing caller gets) joins
    permissively. ``mode="and"`` requires every surviving token to appear in
    the same row — :meth:`EpisodeStore.search` tries that first and only
    widens to ``"or"`` when it comes back empty, so a multi-word query cannot
    match on a single incidental word the way a pure-OR query can.
    """
    tokens = _content_tokens(raw)
    if not tokens:
        return None
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(f'"{t}"' for t in tokens)


@dataclass
class Episode:
    id: int
    ts: float
    surface: str
    user_text: str
    luna_text: str
    salience: float
    effective_salience: float = 0.0
    rank: float = 0.0
    #: Fraction of the search query's content tokens actually found in this
    #: episode's text. 1.0 for every hit from the AND pass (all tokens
    #: present, by construction) and for anything not fetched via
    #: :meth:`EpisodeStore.search` at all (``recent``/``since``, where
    #: coverage does not apply). Only the OR-widen fallback produces values
    #: below 1.0 — see ``search`` and ``Memory.recall_block``'s relevance
    #: floor.
    coverage: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.ts)),
            "surface": self.surface,
            "user_text": self.user_text,
            "luna_text": self.luna_text,
            "salience": self.salience,
            "effective_salience": self.effective_salience,
        }


class EpisodeStore:
    """SQLite-backed tier-2 memory.

    One connection guarded by a lock. The daemon is threaded but episodic
    writes are tiny and infrequent (one per exchange), so a lock costs nothing
    and removes a whole class of cross-thread sqlite bugs.
    """

    def __init__(self, path: Path = config.EPISODES_DB) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        assert_fts5(self._conn)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writing ---------------------------------------------------------

    def count_similar(self, user_text: str) -> int:
        """How many prior episodes look like this one (for the repetition term)."""
        query = build_fts_query(user_text)
        if not query:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT count(*) AS n FROM episodes_fts WHERE episodes_fts MATCH ?",
                    (query,),
                ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error:
            # A malformed MATCH must never lose an episode. Score without it.
            return 0

    def record(
        self,
        user_text: str,
        luna_text: str = "",
        surface: str = "cli",
        ts: float | None = None,
        salience: float | None = None,
    ) -> Episode:
        ts = time.time() if ts is None else ts
        if salience is None:
            salience = score_salience(
                user_text, luna_text, repetitions=self.count_similar(user_text)
            )
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO episodes (ts, surface, user_text, luna_text, salience) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, surface, user_text, luna_text, salience),
            )
            self._conn.commit()
            episode_id = int(cur.lastrowid or 0)
        return Episode(episode_id, ts, surface, user_text, luna_text, salience,
                       effective_salience=salience)

    # -- reading ---------------------------------------------------------

    def _hydrate(self, row: sqlite3.Row, now: float, rank: float = 0.0) -> Episode:
        ep = Episode(
            id=int(row["id"]),
            ts=float(row["ts"]),
            surface=row["surface"],
            user_text=row["user_text"],
            luna_text=row["luna_text"],
            salience=float(row["salience"]),
            rank=rank,
        )
        ep.effective_salience = decayed_salience(ep.salience, now - ep.ts)
        return ep

    def recent(self, limit: int = 10) -> list[Episode]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM episodes ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._hydrate(r, now) for r in rows]

    def since(self, after_id: int = 0, limit: int = 50) -> list[Episode]:
        """Episodes recorded after ``after_id``, **oldest first**.

        By id and not by timestamp, because the consolidation pass needs a
        watermark it can store and compare exactly. Timestamps come from
        ``time.time()`` at the caller's discretion — the tests write episodes
        dated weeks ago — so "everything since 14:32" is not a question this
        table can answer without ambiguity, and "everything after row 118" is.

        Oldest first because the pass reads them as a narrative: a preference
        stated on Tuesday and reversed on Thursday must arrive in that order,
        or the model consolidates the reversal and then the preference.
        """
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM episodes WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(after_id), limit),
            ).fetchall()
        return [self._hydrate(r, now) for r in rows]

    # -- the meta side table ---------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def max_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT max(id) AS m FROM episodes").fetchone()
        return int(row["m"] or 0)

    def _run_match(self, match: str, limit: int) -> list[sqlite3.Row]:
        try:
            with self._lock:
                return self._conn.execute(
                    "SELECT e.*, bm25(episodes_fts) AS bm "
                    "FROM episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid "
                    "WHERE episodes_fts MATCH ? "
                    "ORDER BY bm LIMIT ?",
                    (match, limit * 4),
                ).fetchall()
        except sqlite3.Error as exc:
            raise MemoryError(f"episode search failed for {match!r}: {exc}") from exc

    def _token_hit_ids(self, token: str) -> set[int]:
        """Every episode id the FTS index itself says contains ``token``.

        Used to score coverage on the OR-widen fallback. Deliberately routed
        back through the index rather than a plain-text ``token in text``
        check: the index applies the porter stemmer, and a naive substring
        check disagrees with it on irregular forms ("chose" is the matched
        stem for a query token "choose" — MATCH finds it, `"choose" in text`
        does not) and would under-count coverage for hits the index itself
        considers legitimate.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH ?",
                    (f'"{token}"',),
                ).fetchall()
            return {int(r["rowid"]) for r in rows}
        except sqlite3.Error:
            return set()

    def search(self, query: str, limit: int = 10) -> list[Episode]:
        """Keyword recall, ranked by BM25 lifted by decayed salience.

        AND-then-widen: tries every surviving content token required in the
        same row first, and only falls back to OR (any token) when that
        comes back empty. A pure-OR query is what let "so anyway do you
        think I should do something about this" match 10 of 15 episodes in
        the real database — six of the ten shared exactly one incidental
        word with the query and nothing else. Requiring AND first means a
        multi-word query only matches loosely when nothing matches it
        fully, and the fallback hits are marked with their true
        ``coverage`` (fraction of query tokens actually present) so
        :meth:`Memory.recall_block` can refuse the weak ones rather than
        inject them as if they were as good as an AND match.

        Decay is applied here, at read time. A stale trivial episode sinks;
        a correction from six months ago still surfaces.
        """
        tokens = _content_tokens(query)
        if not tokens:
            return []
        now = time.time()
        and_match = " AND ".join(f'"{t}"' for t in tokens)
        rows = self._run_match(and_match, limit)
        full_coverage = True
        token_hits: dict[str, set[int]] | None = None
        if not rows and len(tokens) > 1:
            or_match = " OR ".join(f'"{t}"' for t in tokens)
            rows = self._run_match(or_match, limit)
            full_coverage = False
            token_hits = {t: self._token_hit_ids(t) for t in tokens}

        episodes = []
        for row in rows:
            ep = self._hydrate(row, now, rank=float(row["bm"]))
            if full_coverage:
                ep.coverage = 1.0
            else:
                rowid = int(row["id"])
                found = sum(1 for t in tokens if rowid in token_hits[t])
                ep.coverage = found / len(tokens)
            episodes.append(ep)
        # bm25 is negative and lower is better; salience in [0,1] lifts a hit.
        episodes.sort(key=lambda e: e.rank - 2.0 * e.effective_salience)
        return episodes[:limit]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) AS n, min(ts) AS first, max(ts) AS last, "
                "avg(salience) AS avg_sal FROM episodes"
            ).fetchone()
        return {
            "path": str(self.path),
            "episodes": int(row["n"] or 0),
            "first_ts": row["first"],
            "last_ts": row["last"],
            "mean_salience": round(float(row["avg_sal"]), 4) if row["avg_sal"] else 0.0,
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }


# =========================================================================
# Tier 3 — the derived profile
# =========================================================================
#
# `profile.json`: what tier 2 says about the user, measured rather than
# remembered. Rebuilt from scratch on every pass, never appended to, and safe
# to delete — the next rebuild reproduces it exactly. Nothing may live only
# here.
#
# **The design is stolen, the implementation is not.** VoiceMem
# (xzf-thu/VoiceMem, Apache-2.0) was read and rejected as a dependency: ~3.27
# GB of models on top of torch, transformers, funasr and sherpa-onnx, against
# 3-4 GB of free RAM and no GPU on this machine. It cannot run here and there
# is no hosted API to fall back to. What was worth taking is its **dual-brain
# split**, and only that:
#
# * a *factual* half — schema and entity extraction. Who the user is, what
#   they work on, what they use, what they have asked for and ruled out.
# * a *persona* half — an accumulator for the relationship. How they like to
#   be talked to, what they react badly to, what earns a "perfect".
#
# The two are separated because they age differently and are wrong in
# different ways. A fact is either right or stale; a persona signal is a
# tendency and is never more than a tendency. Merging them would produce a
# single blob in which "you work on Luna" and "you dislike long answers" carry
# the same weight, and the second is a guess.
#
# Everything below is stdlib: regex, `statistics`, `json`. No embeddings, no
# model call, no new dependency. That is a constraint (`lunad` is stdlib-only
# and stays that way) but it is also the point — tier 3 has to be cheap enough
# to rebuild on a turn counter without anyone thinking about it.
#
# **What this cannot do, stated plainly.** Pattern extraction has false
# negatives everywhere: a preference expressed as a story, a fact stated
# obliquely, sarcasm of any kind. It reads only the user's own words, never
# Luna's, so anything she inferred and said back is invisible to it. The
# support count is published beside every fact for exactly this reason — a
# fact seen once is a guess, and the consolidation pass is told so in those
# words rather than being handed a tidy list that hides it.

PROFILE_VERSION = 1

#: The factual half: slot -> patterns, each with one capture group.
#:
#: Deliberately narrow, on the same reasoning as `_CORRECTION_PATTERNS` above:
#: a false positive here becomes a "fact" the consolidation pass may promote
#: into LUNA.md, and an invented fact costs far more to remove than a missed
#: one costs to re-learn. Every pattern requires the user to have said the
#: thing in the first person, in a form that has one plain reading.
_FACT_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("name", (
        re.compile(r"\bmy name(?:'s| is)\s+([A-Za-z][\w'-]{1,30})", re.IGNORECASE),
        re.compile(r"\bcall me\s+([A-Za-z][\w'-]{1,30})", re.IGNORECASE),
        re.compile(r"\bi go by\s+([A-Za-z][\w'-]{1,30})", re.IGNORECASE),
    )),
    ("works_on", (
        re.compile(r"\bi'?m working on (?:the )?(.{3,60}?)(?:[.,;!?\n]|$)",
                   re.IGNORECASE),
        re.compile(r"\bmy (?:project|repo|repository|app|widget|site) "
                   r"(?:is )?(?:called )?(.{2,40}?)(?:[.,;!?\n]|$)",
                   re.IGNORECASE),
        # A path under the home directory is the least ambiguous statement of
        # what somebody works on that this file will ever see.
        re.compile(r"(~/[\w.@+-]+(?:/[\w.@+-]+)*)"),
    )),
    ("uses", (
        re.compile(r"\bi use\s+(.{2,40}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
        re.compile(r"\bi'?m on\s+(.{2,40}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
        re.compile(r"\bi run\s+(.{2,40}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
    )),
    ("prefers", (
        re.compile(r"\bi (?:prefer|like it)\s+(.{3,60}?)(?:[.,;!?\n]|$)",
                   re.IGNORECASE),
        re.compile(r"\bfrom now on,?\s+(.{3,60}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
        re.compile(r"\balways\s+(.{3,60}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
    )),
    ("avoids", (
        re.compile(r"\bnever\s+(.{3,60}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
        re.compile(r"\bdon'?t\s+(.{3,60}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
        re.compile(r"\bstop\s+(.{3,60}?)(?:[.,;!?\n]|$)", re.IGNORECASE),
    )),
)

FACT_SLOTS: tuple[str, ...] = tuple(slot for slot, _ in _FACT_PATTERNS)

#: The persona half. Corrections are NOT here: they were already detected at
#: write time and stored as a salience of 1.0, and a second detector would be
#: a copy that can disagree with the first. These two are the softer signals
#: either side of one — the user pleased, and the user irritated without
#: bothering to correct anything.
_APPROVAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(perfect|exactly|spot on|nailed it|lovely|brilliant)\b",
        r"\bthat'?s (it|right|the one)\b",
        r"\bthank(s| you)\b",
        r"\byes[,.!]",
        r"\bgood (call|answer|point)\b",
    )
)

_FRICTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\btoo (long|much|verbose|wordy)\b",
        r"\b(shorter|be brief|briefly|just answer|just tell me)\b",
        r"\bi already (said|told|asked)\b",
        r"\byou keep\b",
        r"\bwhy (are|did) you\b",
        r"\bstop explaining\b",
        r"\bnot what i asked\b",
    )
)

#: Words too common to say anything about a person's vocabulary. The tier-2
#: search stopwords, plus the handful that only a chat log produces.
_VOCAB_STOPWORDS = _STOPWORDS | frozenset(
    "can just like get make know think want need there their they them then "
    "than have has had will would could should from your yours mine ours "
    "luna does doing done into more most some also very much".split()
)

VOCAB_MIN_CHARS = 4
VOCAB_MIN_COUNT = 3


def _normalise_fact(raw: str) -> str:
    """Tidy one extracted value. Case is preserved; only edges are trimmed.

    Lower-casing would turn a name into a word and a path into a different
    path, so the only normalisation is whitespace and the punctuation a
    sentence leaves clinging to a capture group.
    """
    return " ".join(raw.split()).strip(" \t\"'`.,;:!?-")


def _words(text: str) -> list[str]:
    return _FTS_TOKEN_RE.findall(text.lower())


def _matches(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _median_int(values: Sequence[int]) -> int | None:
    """Median as a whole number, or ``None`` when there is nothing to average.

    ``None`` rather than 0: "no reply has ever drawn a complaint" and "replies
    that draw complaints are zero words long" are different statements, and
    the second one is false.
    """
    return int(round(statistics.median(values))) if values else None


def extract_facts(episodes: Sequence[Episode]) -> dict[str, list[dict[str, Any]]]:
    """The factual half. Reads the user's words only, never Luna's.

    Luna's replies are excluded on purpose. She paraphrases what the user told
    her, so scanning her text would count every fact twice and manufacture
    support for anything she happened to repeat — including anything she got
    wrong.
    """
    found: dict[str, dict[str, dict[str, Any]]] = {s: {} for s in FACT_SLOTS}
    for episode in episodes:
        for slot, patterns in _FACT_PATTERNS:
            for pattern in patterns:
                for match in pattern.finditer(episode.user_text):
                    value = _normalise_fact(match.group(1))
                    if len(value) < 2 or value.lower() in _VOCAB_STOPWORDS:
                        continue
                    bucket = found[slot]
                    key = value.casefold()
                    entry = bucket.get(key)
                    if entry is None:
                        # First spelling seen wins: "Omarchy" beats "omarchy"
                        # only because it came first, which is at least a rule.
                        entry = {"value": value, "support": 0,
                                 "first_seen": episode.ts, "last_seen": episode.ts}
                        bucket[key] = entry
                    entry["support"] += 1
                    entry["last_seen"] = max(entry["last_seen"], episode.ts)
                    entry["first_seen"] = min(entry["first_seen"], episode.ts)
    out: dict[str, list[dict[str, Any]]] = {}
    for slot, bucket in found.items():
        ranked = sorted(bucket.values(),
                        key=lambda e: (-e["support"], -e["last_seen"]))
        if ranked:
            out[slot] = ranked[:config.PROFILE_TOP_N]
    return out


def accumulate_persona(episodes: Sequence[Episode]) -> dict[str, Any]:
    """The persona half. Counters and evidence — never prose.

    This function deliberately produces no sentences. Turning "seven
    corrections, four of them about answer length" into "the user finds you
    long-winded" is a judgement, and judgement is what the consolidation pass
    pays a model for. Writing it here would mean a heuristic quietly
    editorialising about the user in a file that then looks authoritative.

    ``episodes`` must be in chronological order: the reply-length figures pair
    each reaction with the reply *before* it, which is the reply being reacted
    to.
    """
    corrections: list[str] = []
    friction: list[str] = []
    approval: list[str] = []
    praised: list[int] = []
    criticised: list[int] = []
    user_words: list[int] = []
    luna_words: list[int] = []
    vocab: dict[str, int] = {}
    hours: dict[str, int] = {}
    surfaces: dict[str, int] = {}

    for index, episode in enumerate(episodes):
        text = episode.user_text.strip()
        user_words.append(len(_words(text)))
        luna_words.append(len(_words(episode.luna_text)))
        surfaces[episode.surface] = surfaces.get(episode.surface, 0) + 1
        hour = f"{time.localtime(episode.ts).tm_hour:02d}"
        hours[hour] = hours.get(hour, 0) + 1

        for word in _words(text):
            if len(word) >= VOCAB_MIN_CHARS and word not in _VOCAB_STOPWORDS:
                vocab[word] = vocab.get(word, 0) + 1

        # The stored score, not a second detector: score_salience already
        # decided this at write time and 1.0 is its sentinel for a correction.
        if episode.salience >= config.CORRECTION_SALIENCE:
            corrections.append(text)
        previous = len(_words(episodes[index - 1].luna_text)) if index else None
        if _matches(_APPROVAL_PATTERNS, text):
            approval.append(text)
            if previous:
                praised.append(previous)
        if _matches(_FRICTION_PATTERNS, text):
            friction.append(text)
            if previous:
                criticised.append(previous)

    top_vocab = sorted(((w, n) for w, n in vocab.items() if n >= VOCAB_MIN_COUNT),
                       key=lambda pair: (-pair[1], pair[0]))
    return {
        "corrections": {"count": len(corrections),
                        "recent": corrections[-config.PROFILE_EVIDENCE:]},
        "friction": {"count": len(friction),
                     "recent": friction[-config.PROFILE_EVIDENCE:]},
        "approval": {"count": len(approval),
                     "recent": approval[-config.PROFILE_EVIDENCE:]},
        "length": {
            "user_median_words": _median_int(user_words),
            "luna_median_words": _median_int(luna_words),
            "praised_reply_words": _median_int(praised),
            "criticised_reply_words": _median_int(criticised),
        },
        "vocabulary": [list(pair) for pair in top_vocab[:config.PROFILE_TOP_N * 2]],
        "hours": dict(sorted(hours.items())),
        "surfaces": dict(sorted(surfaces.items())),
    }


class Profile:
    """Tier 3. Derived from tier 2, rebuilt whole, never edited by hand.

    There is no ``append``, no ``add_fact`` and no way to write one field: the
    only mutation is :meth:`rebuild`, which reads tier 2 and replaces the file.
    That is not minimalism, it is the guarantee — a profile that could be
    edited would eventually hold something tier 2 does not, and then deleting
    it would lose data. As built, ``rm profile.json`` is free.
    """

    def __init__(self, path: Path = config.PROFILE_JSON) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    # -- reading ---------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """The profile as written, or ``{}``.

        A corrupt file reads as absent rather than raising. Tier 3 is derived,
        so the cure for damage is a rebuild and not an error message — and a
        malformed profile must never be the reason an ask fails.
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def status(self) -> dict[str, Any]:
        """What ``luna status`` shows for tier 3."""
        data = self.load()
        persona = data.get("persona") or {}
        facts = data.get("facts") or {}
        return {
            "tier": 3,
            "implemented": True,
            "derived": True,
            "path": str(self.path),
            "exists": self.path.exists(),
            "version": data.get("version"),
            "generated": data.get("generated"),
            "iso": data.get("generated_iso", ""),
            "episodes": data.get("episodes", 0),
            "through_id": data.get("through_id", 0),
            "facts": sum(len(v) for v in facts.values() if isinstance(v, list)),
            "slots": sorted(facts),
            "corrections": (persona.get("corrections") or {}).get("count", 0),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def block(self, data: dict[str, Any] | None = None) -> str:
        """The profile rendered for a prompt. ``""`` when there is nothing.

        Compact on purpose. This is read by the consolidation pass on every
        run and every character of it is paid for, so it is a digest and not a
        dump: counts, the strongest facts, and the most recent evidence.

        Support counts are shown because they are the part that matters. A
        fact seen once is a guess and must arrive looking like one.
        """
        data = self.load() if data is None else data
        if not data:
            return ""
        lines: list[str] = []
        facts = data.get("facts") or {}
        for slot in FACT_SLOTS:
            items = facts.get(slot) or []
            if not items:
                continue
            rendered = ", ".join(
                f"{i.get('value')} (x{i.get('support', 0)})" for i in items)
            lines.append(f"- {slot}: {rendered}")
        if lines:
            lines.insert(0, "Facts extracted from the user's own words "
                            "(x1 means said once — treat it as a guess):")

        persona = data.get("persona") or {}
        counts = [f"{(persona.get(k) or {}).get('count', 0)} {k}"
                  for k in ("corrections", "friction", "approval")]
        lines.append("")
        lines.append("Relationship signals over "
                     f"{data.get('episodes', 0)} exchanges: " + ", ".join(counts) + ".")
        length = persona.get("length") or {}
        praised, criticised = (length.get("praised_reply_words"),
                               length.get("criticised_reply_words"))
        if praised is not None or criticised is not None:
            lines.append(
                "Replies that drew approval ran "
                f"{praised if praised is not None else '?'} words; replies that "
                f"drew friction ran {criticised if criticised is not None else '?'}.")
        if length.get("user_median_words"):
            lines.append(f"The user writes about {length['user_median_words']} "
                         "words per message.")
        for label in ("corrections", "friction"):
            recent = (persona.get(label) or {}).get("recent") or []
            if recent:
                lines.append(f"Most recent {label}:")
                lines += [f"- {_clip(r, 160)}" for r in recent]
        vocab = persona.get("vocabulary") or []
        if vocab:
            lines.append("Recurring words: "
                         + ", ".join(str(pair[0]) for pair in vocab))
        return "\n".join(lines).strip()

    # -- writing ---------------------------------------------------------

    def rebuild(self, store: EpisodeStore, limit: int | None = None,
                now: float | None = None,
                persist: bool = True) -> dict[str, Any]:
        """Regenerate the whole profile from tier 2 and write it atomically.

        Bounded by ``[PROFILE_WINDOW]`` episodes, newest kept, for two
        reasons. Cost — this runs on a turn counter and must stay a few
        milliseconds. And truth — a profile built from every exchange since
        the daemon was installed describes a person who no longer exists, and
        the user's habits from six months ago are not evidence about today.

        ``persist=False`` returns the same payload and writes nothing. It has
        exactly one caller — the consolidation dry run, which has to build its
        prompt from the same tier 3 a real pass would read, and which claims
        to leave no trace on disk. Without it the claim would be false by one
        file: ``profile.json`` is derived and reproducible, but a dry run that
        rewrites it is still a dry run that wrote something.
        """
        window = config.PROFILE_WINDOW if limit is None else limit
        stamp = time.time() if now is None else now
        # `recent` is newest first; everything downstream reads a narrative.
        episodes = sorted(store.recent(window), key=lambda e: (e.ts, e.id))
        payload = {
            "version": PROFILE_VERSION,
            "generated": stamp,
            "generated_iso": time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(stamp)),
            "episodes": len(episodes),
            "window": window,
            "through_id": max((e.id for e in episodes), default=0),
            "first_ts": episodes[0].ts if episodes else None,
            "last_ts": episodes[-1].ts if episodes else None,
            "facts": extract_facts(episodes),
            "persona": accumulate_persona(episodes),
        }
        if persist:
            self.write(payload)
        return payload

    def write(self, payload: dict[str, Any]) -> Path:
        with self._lock:
            atomic_write(self.path,
                         json.dumps(payload, indent=2, ensure_ascii=False,
                                    default=str) + "\n")
        return self.path

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


# =========================================================================
# Facade
# =========================================================================


class Memory:
    """Everything the daemon needs from memory, in one object."""

    def __init__(
        self,
        luna_md: Path = config.LUNA_MD,
        user_md: Path = config.USER_MD,
        episodes_db: Path = config.EPISODES_DB,
        profile_json: Path | None = None,
    ) -> None:
        self.luna = Tier1File(luna_md, config.LUNA_MD_CAP, "LUNA.md",
                              cap_key="memory.luna_cap_chars")
        self.user = Tier1File(user_md, config.USER_MD_CAP, "USER.md",
                              cap_key="memory.user_cap_chars")
        self.episodes = EpisodeStore(episodes_db)
        # The profile defaults to sitting *beside the episode store it is
        # derived from*, not to a fixed path. That is the honest relationship
        # — tier 3 is a function of tier 2 and belongs with its input — and it
        # has a useful consequence: a caller who redirects the episode store
        # (every test does) gets the profile redirected with it, so nothing
        # can accidentally rebuild over the user's real profile.json.
        self.profile = Profile(
            profile_json if profile_json is not None
            else Path(episodes_db).parent / config.PROFILE_JSON.name)

    def file(self, name: str) -> Tier1File:
        key = name.strip().upper().removesuffix(".MD")
        if key == "LUNA":
            return self.luna
        if key == "USER":
            return self.user
        raise MemoryError(f"unknown tier-1 file {name!r}; expected LUNA.md or USER.md")

    def tier1_block(self) -> str:
        """The frozen tier-1 prompt block.

        Built once per agent invocation and injected verbatim. Stable text
        keeps the provider's KV-cache prefix valid for the session.
        """
        chunks: list[str] = []
        for handle, heading in ((self.luna, "Environment and conventions"),
                                (self.user, "What Luna knows about the user")):
            entries = handle.entries()
            if not entries:
                continue
            body = "\n".join(f"- {e}" for e in entries)
            chunks.append(f"### {heading} ({handle.name})\n{body}")
        if not chunks:
            return "### Curated memory\n(empty — nothing has been written yet.)"
        return "\n\n".join(chunks)

    def recall_block(self, query: str, limit: int = config.RECALL_LIMIT) -> str:
        """Relevant tier-2 episodes, rendered for the prompt. May be empty.

        Filters out anything below :data:`RECALL_COVERAGE_FLOOR` first.
        Injecting a weak match into the prompt is worse than injecting
        nothing — it reads as confirmed context and actively misleads,
        rather than just failing to help — so this is the one place in the
        recall path that would rather come back empty than come back wrong.
        ``EpisodeStore.search`` itself stays permissive, because other
        callers (e.g. the raw ``mem search`` surface) want to see weak hits
        to judge for themselves.
        """
        hits = [ep for ep in self.episodes.search(query, limit=limit)
                if ep.coverage >= RECALL_COVERAGE_FLOOR]
        if not hits:
            return ""
        lines = []
        for ep in hits:
            when = time.strftime("%Y-%m-%d", time.localtime(ep.ts))
            luna = ep.luna_text.strip().replace("\n", " ")
            if len(luna) > 240:
                luna = luna[:237] + "..."
            user = ep.user_text.strip().replace("\n", " ")
            if len(user) > 240:
                user = user[:237] + "..."
            lines.append(
                f"- [{when}, salience {ep.effective_salience:.2f}] "
                f"user: {user}\n  luna: {luna}"
            )
        return "### Possibly relevant past exchanges\n" + "\n".join(lines)

    def usage(self) -> dict[str, Any]:
        return {
            "tier1": {"LUNA.md": self.luna.usage(), "USER.md": self.user.usage()},
            "tier2": self.episodes.stats(),
            "tier3": self.profile.status(),
        }

    def close(self) -> None:
        self.episodes.close()


class SolMemory:
    """Sol's namespace — deliberately not a :class:`Memory`.

    Sol is a specialist who reports to Luna, and the failure mode worth
    designing against is not malice but drift: two agents editing one model of
    the world until neither is right and nobody can say which write was wrong.
    So the separation is structural rather than advisory.

    * different directory (``memory/sol/``), so a path mistake lands somewhere
      harmless rather than in ``LUNA.md``;
    * different episode store, so Sol's job chatter never surfaces as recall in
      Luna's conversation;
    * :meth:`file` knows exactly one name. Asking it for ``LUNA.md`` raises,
      with the reason, rather than returning a handle.

    The class enforces this for every path that goes through ``lunad``. A
    dispatched session holding real tools could in principle write anywhere on
    the disk; that is bounded by its system prompt and by the audit log, not by
    this object, and the docs say so rather than implying otherwise.
    """

    NAME = "SOL.md"

    def __init__(self, root: Path = config.SOL_MEMORY_DIR) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sol = Tier1File(self.root / self.NAME, config.SOL_MD_CAP, self.NAME)
        self.episodes = EpisodeStore(self.root / "episodes.db")

    def file(self, name: str) -> Tier1File:
        key = name.strip().upper().removesuffix(".MD")
        if key == "SOL":
            return self.sol
        if key in ("LUNA", "USER"):
            raise MemoryError(
                f"{name} belongs to Luna. Sol's namespace holds SOL.md only; "
                "a specialist does not edit his supervisor's memory. Put it in "
                "the report instead."
            )
        raise MemoryError(
            f"unknown file {name!r} in Sol's namespace; expected SOL.md"
        )

    def block(self) -> str:
        """Sol's tier-1 block, for his system prompt. May be empty."""
        entries = self.sol.entries()
        if not entries:
            return ""
        return "\n".join(f"- {e}" for e in entries)

    def usage(self) -> dict[str, Any]:
        return {"namespace": str(self.root),
                "SOL.md": self.sol.usage(),
                "episodes": self.episodes.stats()}

    def close(self) -> None:
        self.episodes.close()
