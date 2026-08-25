"""Luna's memory. ARCHITECTURE.md section 4, tiers 1 and 2.

Tier 1 — curated identity, always in the prompt, hard character caps.
Tier 2 — episodic, SQLite + FTS5, searched on demand, salience-scored.
Tier 3 — derived profile. Stubbed (see ``ProfileStub`` at the bottom).

The one behaviour inherited wholesale from Hermes Agent (Nous Research, MIT)
is the cap: a write that would overflow a tier-1 file is REJECTED with a
report of current usage, never silently truncated. Overflow is a signal to
consolidate, and swallowing it is how a curated file rots into a log.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

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
    cap: int
    name: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.path.name

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(self.path)


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


def decayed_salience(
    salience: float,
    age_seconds: float,
    half_life_days: float = config.SALIENCE_HALF_LIFE_DAYS,
) -> float:
    """Apply exponential time decay to a stored salience.

    Applied at READ time. Rows are never mutated by decay: a stored score is a
    fact about the moment it was written, and rewriting history every time the
    clock ticks would make the database its own worst-behaved client.

    Corrections (``salience >= CORRECTION_SALIENCE``) do not decay, per
    ARCHITECTURE.md: "Corrections from the user score 1.0 and never decay."
    """
    if salience >= config.CORRECTION_SALIENCE:
        return salience
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
"""

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_STOPWORDS = frozenset(
    "a an the and or of to in on for with what when did we i you is are was "
    "were be been it that this about how why".split()
)


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


def build_fts_query(raw: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression.

    User queries contain apostrophes, hyphens and question marks, all of which
    are FTS5 syntax. Every token is quoted and stopwords are dropped; returns
    ``None`` when nothing usable is left.
    """
    tokens = [t for t in _FTS_TOKEN_RE.findall(raw.lower()) if t not in _STOPWORDS]
    if not tokens:
        tokens = _FTS_TOKEN_RE.findall(raw.lower())
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


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

    def search(self, query: str, limit: int = 10) -> list[Episode]:
        """Keyword recall, ranked by BM25 lifted by decayed salience.

        Decay is applied here, at read time. A stale trivial episode sinks;
        a correction from six months ago still surfaces.
        """
        match = build_fts_query(query)
        if not match:
            return []
        now = time.time()
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT e.*, bm25(episodes_fts) AS bm "
                    "FROM episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid "
                    "WHERE episodes_fts MATCH ? "
                    "ORDER BY bm LIMIT ?",
                    (match, limit * 4),
                ).fetchall()
        except sqlite3.Error as exc:
            raise MemoryError(f"episode search failed for {query!r}: {exc}") from exc

        episodes = [self._hydrate(r, now, rank=float(r["bm"])) for r in rows]
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
# Tier 3 — derived profile.  STUB. Not implemented in Phase 0.
# =========================================================================


class ProfileStub:
    """PLACEHOLDER for tier 3 (``profile.json``). Deliberately not implemented.

    Tier 3 is regenerated periodically from tier 2 by a background pass on a
    cheap model: working style, recurring frustrations, vocabulary, stated
    intent vs actual behaviour. It is never hand-written, which is exactly why
    it cannot be built before tier 2 has any history in it.

    Phase 0 exposes only its status so ``luna status`` can say "not yet".
    """

    path = config.PROFILE_JSON
    implemented = False

    @classmethod
    def status(cls) -> dict[str, Any]:
        return {
            "tier": 3,
            "implemented": False,
            "path": str(cls.path),
            "note": "profile.json is a Phase 3 deliverable; not written in Phase 0",
        }


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
    ) -> None:
        self.luna = Tier1File(luna_md, config.LUNA_MD_CAP, "LUNA.md")
        self.user = Tier1File(user_md, config.USER_MD_CAP, "USER.md")
        self.episodes = EpisodeStore(episodes_db)

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
        """Relevant tier-2 episodes, rendered for the prompt. May be empty."""
        hits = self.episodes.search(query, limit=limit)
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
            "tier3": ProfileStub.status(),
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
