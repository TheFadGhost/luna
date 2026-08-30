"""The consolidation pass. ARCHITECTURE.md section 4, "Consolidation nudge".

Tier 1 is small and curated; tier 2 is large and raw. Something has to move
what matters from the second into the first, and until now nothing did:
`[memory] consolidate_every_turns` was in the config file, validated,
round-tripped and displayed, and honoured by nothing at all. This module is
what it now names.

**Why a model and not a rule.** Deciding that "the bar is omarchy-shell, not
waybar" belongs in LUNA.md while "what time is it" does not is a judgement
about consequence, and every attempt to write it as a heuristic ends up as
either a keyword list that misses the interesting cases or a scoring function
that promotes noise with confidence. Salience scoring already does as much as
a rule honestly can — it decides what is worth *keeping*, and it is used here
to rank the input — but what is worth *writing down as identity* is a
different question and it is the one worth paying a model for.

**Why it costs almost nothing.** The pass does not run under Luna's persona.
Her system prompt is ~8k tokens of specification about how to talk to a human,
and this is not a conversation: it is a librarian looking at a shelf. The
prompt here is a few hundred characters, the input is capped at
`CONSOLIDATE_EPISODE_LIMIT` exchanges clipped to `CONSOLIDATE_EPISODE_CHARS`
per side, and the tier-3 profile arrives as a digest rather than a dump. The
whole thing is bounded above by roughly 3k input tokens and a few hundred out,
once every `consolidate_every_turns` turns. "Cheap" here means a small prompt
and not a cheaper model: there is no second model setting in the contract and
inventing one to make this sentence true would be a worse trade than the one
sentence is worth.

**Four ways this could go wrong, and what stops each.**

* *It blocks a reply.* It runs on its own thread, started after the reply has
  been built, and nothing waits on it. A pass still running when the next turn
  lands is simply not started again.
* *It runs away.* Five separate bounds: `0` means never; one pass at a time;
  `CONSOLIDATE_MIN_INTERVAL_S` as a floor between passes whatever the turn
  count says; no episodes since the watermark means no model call at all; and
  the pass never records an episode of its own, so it cannot feed itself.
* *It corrupts tier 1.* Every write goes through `Tier1File.replace`, which is
  the same cap-checked, temp-file-then-rename path as any other write. A
  daemon killed mid-pass leaves either the old file or the new one.
* *It loses episodes, or re-reads them forever.* The watermark lives in the
  episode store's `meta` table and is moved **after** the tier-1 write, never
  before. Interrupted, the pass reconsiders exchanges it has already seen —
  harmless, because the proposal is always made against the current contents
  of the files. Moved first, it would skip them silently and for ever.

**Running one by hand, and looking before you leap.** Waiting twelve turns to
see what a pass does is the wrong shape for code that spends real money and
rewrites a file the user curates, so `luna memory consolidate` runs one now, on
the caller's thread, and prints what it did. `--dry-run` makes the same model
call and applies nothing: same episodes, same prompt, same cost, and a report
of the edit instead of the edit. It is not "apply and then undo" — `replace`
has no inverse, so there would be nothing to undo with. It is a second, shorter
path through this module that is never handed anything it could write with; see
:func:`preview_edit` and :meth:`Consolidator.preview`.

A manual run is past exactly two of the bounds below, and they are the two the
person typing the command is deliberately overriding: the turn counter and the
interval floor. Everything else holds, single-flight most of all — the daemon
answers each connection on its own thread, so two `luna memory consolidate` in
two terminals are genuinely simultaneous, and two passes proposing edits
against the same batch is the one way this feature could damage tier 1. `0`
still means never, by hand as much as on a timer: it is what a user reaches for
when a pass has surprised them on their bill, and a command that spent money
anyway would make that promise a lie.

**Overflow is a normal outcome, not an error.** The cap contract is unchanged:
a proposal that would push a file past its cap is *rejected whole*, the file is
left exactly as it was, and the pass records what did not fit. It is not
truncated, and additions are not dropped one by one until something squeezes
in — that would be the silent rot the cap exists to prevent, arriving through
the back door of the feature meant to relieve it. The model is given the cap,
the current usage and the numbered entries, and can propose removals; if it
proposes something that does not fit anyway, the next pass tries again with
one more piece of evidence.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from . import agent as agent_mod
from . import config
from . import settings as settings_mod
from .memory import (CONSOLIDATED_THROUGH, Episode, Memory, MemoryCapExceeded,
                     MemoryError as LunaMemoryError, Tier1File, render_entries)

log = logging.getLogger("lunad.consolidate")


# =========================================================================
# The prompt
# =========================================================================

# Not Luna's persona, and deliberately nothing like it. This asks for one
# judgement and one JSON object, and says the two things a caller cannot
# recover from if the model gets them wrong: no prose outside the object, and
# indices that refer to the list as printed.
_SYSTEM = """\
You are the memory keeper for {name}, a resident assistant on a Linux desktop.
You are not talking to the user and your output is never shown to them. Your
only job is to decide what, if anything, from a batch of recent exchanges
belongs in {name}'s permanent curated memory.

Two files, each with a hard character cap:

- LUNA.md — the environment and the conventions of this machine. Facts about
  the desktop, the tools, the way things are done here.
- USER.md — the user. Who they are, what they work on, how they want to be
  spoken to, what they have ruled out.

What belongs: standing instructions, stated preferences, corrections the user
had to make, and facts that will still be true next month. What does not: this
week's task, anything already written in the file, anything you inferred rather
than read, and anything you would have to guess at. An entry nobody will need
again is worse than no entry, because it costs cap space forever.

Write each entry as one short standalone sentence in the third person, as
though the file had always said it. No dates, no "the user said", no hedging.

Answer with ONE JSON object and nothing else — no prose, no code fence:

{{"LUNA.md": {{"add": ["..."], "remove": [<index>]}},
 "USER.md": {{"add": [], "remove": []}},
 "note": "one short line on what you did and why"}}

`remove` holds indices from the numbered lists below, and is for entries that
are now wrong, superseded, or duplicated by something you are adding. Removing
nothing is normal. Adding nothing is normal and is the right answer most of the
time — say so in the note and return empty lists.
"""

_FILE_BLOCK = """\
### {name} — {chars}/{cap} chars used ({pct}%), {count} entries

{entries}
"""


def render_file(handle: Tier1File) -> str:
    """One tier-1 file, numbered, with its cap. The indices are the contract."""
    entries = handle.entries()
    usage = handle.usage()
    body = ("\n".join(f"[{i}] {e}" for i, e in enumerate(entries))
            or "(empty)")
    return _FILE_BLOCK.format(name=handle.name, chars=usage["chars"],
                              cap=usage["cap"], pct=usage["pct"],
                              count=len(entries), entries=body)


def render_episodes(episodes: list[Episode], who: str = "luna") -> str:
    """The new exchanges, clipped, oldest first, with their salience.

    Salience is shown because it is the one number the model cannot work out
    for itself: a 1.00 is an explicit correction from the user, detected at
    write time, and a correction is the single strongest reason to write
    something into tier 1.
    """
    lines: list[str] = []
    for episode in episodes:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(episode.ts))
        lines.append(
            f"- [{when}, {episode.surface}, salience "
            f"{episode.salience:.2f}]\n"
            f"  user: {_clip(episode.user_text)}\n"
            f"  {who}: {_clip(episode.luna_text)}"
        )
    return "\n".join(lines)


def _clip(text: str, limit: int | None = None) -> str:
    limit = config.CONSOLIDATE_EPISODE_CHARS if limit is None else limit
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def build_message(memory: Memory, episodes: list[Episode],
                  profile_block: str = "", who: str = "luna") -> str:
    """The user message for one pass: files, profile, new exchanges."""
    parts = ["## Curated memory as it stands\n",
             render_file(memory.luna),
             render_file(memory.user)]
    if profile_block:
        parts.append("## What the derived profile measures (tier 3)\n\n"
                     + profile_block)
    parts.append(f"## {len(episodes)} new exchanges since the last pass\n\n"
                 + render_episodes(episodes, who))
    return "\n\n".join(p.strip() for p in parts if p.strip()) + "\n"


# =========================================================================
# Parsing the answer
# =========================================================================


def parse_proposal(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply. Raises ``ValueError``.

    Tolerant about what surrounds the object and strict about what is in it.
    Models wrap JSON in a code fence roughly one time in ten however plainly
    they are told not to, and treating that as a failure would throw away a
    perfectly good proposal that has already been paid for.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in the reply: {raw[:200]!r}")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"expected an object, got {type(data).__name__}")
    return data


def clean_edit(raw: Any, entry_count: int) -> tuple[list[str], list[int]]:
    """Validate one file's ``{"add": [...], "remove": [...]}``.

    Everything here is a bound on what a model is allowed to do to a file the
    user curates: how many entries it may add at once, how long each may be,
    and that an index it invented is dropped rather than shifting the meaning
    of every removal after it.
    """
    if not isinstance(raw, dict):
        return [], []
    # `isinstance(..., list)` and not a truthiness check: a model that answers
    # `"add": "the bar is omarchy-shell"` instead of a list hands back a
    # string, and a string slices into characters perfectly happily. That
    # writes one tier-1 entry per letter.
    raw_adds = raw.get("add")
    raw_removes = raw.get("remove")
    adds: list[str] = []
    for item in (raw_adds if isinstance(raw_adds, list)
                 else [])[:config.CONSOLIDATE_MAX_ADDITIONS]:
        if not isinstance(item, str):
            continue
        entry = " ".join(item.split()).strip()
        if entry:
            adds.append(entry[:config.CONSOLIDATE_ENTRY_CHARS])
    removes: list[int] = []
    for item in (raw_removes if isinstance(raw_removes, list) else []):
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if 0 <= item < entry_count and item not in removes:
            removes.append(item)
    return adds, sorted(removes)


def preview_edit(name: str, entries: list[str], cap: int,
                 adds: list[str], removes: list[int]) -> dict[str, Any]:
    """What one file would look like afterwards, worked out from data alone.

    This is the whole of the dry run's write path, and it is a function over
    two lists and an integer rather than a method on :class:`Tier1File` for
    one reason: the cheapest way to be sure a dry run cannot write is for the
    code that computes it never to be handed anything it could write with.
    Nothing here has a path, a handle or a lock.

    It therefore repeats the cap arithmetic — render, measure, compare — that
    ``Tier1File.replace`` does, instead of calling ``replace`` and catching
    the refusal, because catching the refusal means having made the call. The
    refusal it reports is a real :class:`MemoryCapExceeded` built from the
    same numbers, so the sentence a preview shows is the sentence the pass
    would have produced, word for word, rather than a second wording of it
    that could drift.

    The record has the shape ``Consolidator._apply`` returns, so one printer
    reads both, with two additions that only make sense before the fact:
    ``removed_index`` (which entry, as the file is numbered today) and, when
    the proposal does not fit, ``refused`` — because "this would have been
    rejected" without "here is what would have been rejected" is the least
    useful answer a preview could give.
    """
    dropped = set(removes)
    kept = [e for i, e in enumerate(entries) if i not in dropped]
    proposed = kept + adds
    rendered = render_entries(proposed)
    chars = len(rendered)
    record: dict[str, Any] = {
        "added": adds,
        "removed": [entries[i] for i in removes],
        "removed_index": list(removes),
        "over_cap": None,
        "usage": {"file": name, "chars": chars, "cap": cap,
                  "pct": round(100.0 * chars / cap, 1) if cap else 0.0,
                  "remaining": cap - chars, "entries": len(proposed)},
    }
    if cap and chars > cap:
        record["over_cap"] = MemoryCapExceeded(
            name=name, cap=cap, current_chars=len(render_entries(entries)),
            proposed_chars=chars, entry_count=len(entries)).to_dict()
        record["refused"] = {"added": adds, "removed": record["removed"]}
        record["added"], record["removed"] = [], []
        record["removed_index"] = []
    return record


# =========================================================================
# The pass
# =========================================================================


class Consolidator:
    """The turn counter, the background pass, and the record of what it cost.

    Built by the daemon and owned by it. ``adapter`` is a *callable* rather
    than an adapter, because the daemon swaps its adapter when
    `[assistant] agent` changes and an object captured at construction would
    go on calling the CLI the user just switched away from — and would do it
    from a background thread, where nobody would notice.
    """

    def __init__(
        self,
        memory: Memory,
        adapter: Callable[[], agent_mod.BaseAdapter],
        audit: Any = None,
        settings: settings_mod.Settings | None = None,
        on_spend: Callable[[float | None], None] | None = None,
        min_interval_s: float | None = None,
        episode_limit: int | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.memory = memory
        self.adapter = adapter
        self.audit = audit
        self.settings = settings
        self.on_spend = on_spend
        self.min_interval_s = (config.CONSOLIDATE_MIN_INTERVAL_S
                               if min_interval_s is None else min_interval_s)
        self.episode_limit = (config.CONSOLIDATE_EPISODE_LIMIT
                              if episode_limit is None else episode_limit)
        self.timeout_s = (config.CONSOLIDATE_TIMEOUT_S if timeout_s is None
                          else timeout_s)
        self.turns = 0
        self.passes = 0
        # Dry runs. Counted apart from `passes` because they cost money and
        # changed nothing, and a number that conflated the two would answer
        # neither "how often has she rewritten tier 1" nor "what did I spend
        # on looking". They do land in `cost_usd`, which is about money.
        self.previews = 0
        # Turns on which a pass was due and did not run: one already in
        # flight, the interval floor not yet elapsed, or nothing new to read.
        self.skipped = 0
        self.failures = 0
        self.added = 0
        self.removed = 0
        self.over_cap = 0
        self.cost_usd = 0.0
        self.last_run = 0.0
        self.last_note = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Single flight, held from the decision to start a pass until that
        # pass has finished, whoever started it. It is a flag and not
        # "is my thread alive", because a pass asked for from the CLI runs on
        # the caller's thread and there is no thread of ours to ask about —
        # and that pass is exactly the one that must not run beside another.
        self._busy = False

    # -- the counter ------------------------------------------------------

    @property
    def every(self) -> int:
        """`[memory] consolidate_every_turns`, read live. ``0`` means never.

        Read per call, like every other setting: a user who turns this off
        because a pass surprised them on their bill must not have to restart
        the daemon for it to stop.
        """
        value = (self.settings.get("memory.consolidate_every_turns")
                 if self.settings is not None
                 else settings_mod.get("memory.consolidate_every_turns"))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return config.CONSOLIDATE_EVERY_TURNS

    def turn(self) -> bool:
        """Count one completed exchange; start a pass if one is due.

        Returns whether a pass was started, which is what the tests assert on.
        Never raises: it is called at the end of a successful ask, and a fault
        in the memory system must not turn a reply the user already has into
        an error.
        """
        try:
            every = self.every
            with self._lock:
                self.turns += 1
                if every <= 0:
                    return False
                if self.turns < every:
                    return False
                if self._busy:
                    # Counting continues; a second pass does not start. The
                    # turns accumulate and the next one runs on a larger batch.
                    # A pass started from the CLI holds this too, so a manual
                    # run is not overtaken by the turn that lands while it is
                    # still talking to the model.
                    self.skipped += 1
                    return False
                since = time.monotonic() - self.last_run
                if self.last_run and since < self.min_interval_s:
                    self.skipped += 1
                    return False
                self.turns = 0
                # Stamped here, at the decision, and not inside the pass: the
                # floor is a gap between *starts*, and a pass that fails in
                # its first millisecond must not free the next one to go
                # immediately.
                self.last_run = time.monotonic()
                self._busy = True
                self._thread = threading.Thread(
                    target=self._run_guarded, args=("turn counter",),
                    daemon=True, name="luna-consolidate")
            self._thread.start()
            return True
        except Exception:  # noqa: BLE001 - never let this break an answered ask
            log.exception("consolidation counter failed")
            return False

    def _run_guarded(self, why: str) -> None:
        try:
            self.run_once(why=why)
        except Exception:  # noqa: BLE001 - nobody is listening on this thread
            with self._lock:
                self.failures += 1
            log.exception("consolidation pass failed")
        finally:
            # Released here and not at the end of `run_once`, because the slot
            # was taken in `turn` before this thread existed: whatever happens
            # in between, the pass is over when this function returns.
            self._release()

    # -- single flight ----------------------------------------------------

    def _acquire(self) -> bool:
        """Take the one slot, or report that something else already has it."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def _release(self) -> None:
        with self._lock:
            self._busy = False

    # -- asked for by hand -------------------------------------------------

    def run_manual(self, dry_run: bool = False) -> dict[str, Any]:
        """One pass now, on the caller's thread. `luna memory consolidate`.

        Synchronous, unlike the automatic pass, because the person who typed
        the command is waiting to read what it did; a background thread would
        have nobody to tell.

        Two guards are lifted and the rest hold. The turn counter is lifted
        because asking for a pass *is* the override — needing twelve more
        exchanges before you may see what the feature does is the problem this
        method exists to solve. The interval floor is lifted for the same
        reason: it protects against a timer running away, and there is no
        timer here, only a person who will notice the bill.

        The off switch is not lifted, and that is deliberate. `0` is what a
        user sets when a pass has surprised them on their bill, and the
        promise attached to it is that nothing is counted and no tokens are
        spent. A command that spent money anyway would make the promise a lie,
        so this refuses and says which setting refused it.

        Single flight is not lifted either, and it is the one that matters:
        the daemon answers each connection on its own thread, so two of these
        in two terminals are genuinely simultaneous, and two passes proposing
        edits against the same batch is how tier 1 would get damaged.
        """
        if self.every <= 0:
            return {"ran": False, "reason": "disabled", "manual": True,
                    "dry_run": dry_run}
        if not self._acquire():
            return {"ran": False, "reason": "already running", "manual": True,
                    "dry_run": dry_run}
        # Read before the pass moves it, and reported rather than obeyed: the
        # floor is overridden here, and a report that did not say so would
        # leave a user wondering why the same command behaves differently from
        # the automatic pass they read about in the docs.
        with self._lock:
            since = time.monotonic() - self.last_run
            floor_left = (max(0.0, self.min_interval_s - since)
                          if self.last_run else 0.0)
        try:
            result = (self.preview() if dry_run
                      else self.run_once(why="luna memory consolidate",
                                         manual=True))
        finally:
            self._release()
        return {**result, "manual": True, "dry_run": dry_run,
                "floor_left_s": round(floor_left, 1)}

    # -- the pass ---------------------------------------------------------

    def _read_batch(self, persist_profile: bool = True
                    ) -> tuple[int, list[Episode], dict[str, Any]]:
        """The free half of a pass: the watermark, the new episodes, tier 3.

        Tier 3 is rebuilt first and unconditionally: it is local, free, and
        the pass below reads it. A rebuild with nothing new in it still costs
        a couple of milliseconds and keeps the profile honest about the window
        it covers. ``persist_profile=False`` is the dry run, which needs the
        same tier 3 in the prompt and leaves no file behind.
        """
        store = self.memory.episodes
        after = _as_int(store.get_meta(CONSOLIDATED_THROUGH))
        episodes = store.since(after, limit=self.episode_limit)
        profile = self.memory.profile.rebuild(store, persist=persist_profile)
        return after, episodes, profile

    def _ask_model(self, episodes: list[Episode],
                   profile: dict[str, Any]) -> agent_mod.AgentReply:
        """The one paid step, shared by the real pass and the dry run.

        Shared deliberately rather than written twice: a preview built from a
        different prompt is a preview of something else, and the difference
        would be invisible in the output. The name is read here rather than
        captured for the same reason the adapter is — a rename between passes
        must reach the next one.
        """
        who = settings_mod.assistant_name()
        message = build_message(self.memory, episodes,
                                self.memory.profile.block(profile),
                                who.lower())
        return self.adapter().ask(message, _SYSTEM.format(name=who),
                                  model=None, session_id=None, resume=None,
                                  timeout=self.timeout_s)

    def run_once(self, why: str = "asked directly",
                 manual: bool = False) -> dict[str, Any]:
        """One complete pass. Safe to call directly; the tests do.

        The order is load-bearing and is the same order every time: rebuild
        tier 3 (free), read the new episodes, call the model (paid), write
        tier 1, and only then move the watermark.

        ``manual`` says a person asked for this one rather than the turn
        counter, and travels no further than the log and the audit entry —
        the pass itself is identical, which is the point of being able to ask
        for one. Reached from :meth:`run_manual`, never called with it here.
        """
        started = time.monotonic()
        with self._lock:
            # Stamped by a manual pass too. The floor is a gap between *paid
            # starts*, and a pass asked for by hand is a paid start: the user
            # overrides the floor for the run they asked for, not for the
            # automatic one that follows it a minute later.
            self.last_run = started
        store = self.memory.episodes
        after, episodes, profile = self._read_batch()

        if not episodes:
            # The single most important guard in this file. No new exchanges
            # means no model call, so a daemon left idle for a week with
            # `consolidate_every_turns = 1` spends nothing at all.
            with self._lock:
                self.skipped += 1
                self.last_note = "nothing new since the last pass"
            log.info("consolidation skipped",
                     extra={"why": why, "after_id": after, "episodes": 0})
            return {"ran": False, "reason": "no new episodes",
                    "after_id": after, "profile": profile.get("episodes", 0)}

        highest = max(e.id for e in episodes)

        try:
            reply = self._ask_model(episodes, profile)
        except agent_mod.AgentError as exc:
            # No tokens were spent on a spawn that failed, and none on a
            # timeout worth keeping. The watermark stays where it is so the
            # same batch is offered again after the next `every` turns.
            with self._lock:
                self.failures += 1
                self.last_note = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("consolidation could not reach the agent",
                        extra={"why": why, "episodes": len(episodes),
                               "detail": str(exc)[:300]})
            self._audit(False, why=f"consolidation failed: {exc}"[:300],
                        manual=manual, episodes=len(episodes))
            return {"ran": False, "reason": "agent unavailable",
                    "detail": str(exc)}

        # From here the call is paid for, so the watermark moves whatever
        # happens next. A reply that cannot be parsed is a reply that would
        # not parse the second time either, and paying twice for the same
        # unusable answer is the runaway this avoids.
        if self.on_spend is not None:
            self.on_spend(reply.cost_usd)
        applied: dict[str, Any] = {}
        note = ""
        parsed = False
        try:
            proposal = parse_proposal(reply.text)
            note = str(proposal.get("note") or "")[:300]
            applied = self._apply(proposal)
            parsed = True
        except ValueError as exc:                     # JSONDecodeError too
            with self._lock:
                self.failures += 1
                self.last_note = f"unparseable proposal: {exc}"[:200]
            log.warning("consolidation reply was not usable",
                        extra={"why": why, "detail": str(exc)[:300],
                               "cost_usd": reply.cost_usd})
        finally:
            store.set_meta(CONSOLIDATED_THROUGH, str(highest))

        wall_ms = int((time.monotonic() - started) * 1000)
        adds = sum(len(v["added"]) for v in applied.values())
        drops = sum(len(v["removed"]) for v in applied.values())
        over = [name for name, v in applied.items() if v.get("over_cap")]
        usage = reply.usage or {}
        with self._lock:
            self.passes += 1
            self.added += adds
            self.removed += drops
            self.over_cap += len(over)
            if reply.cost_usd:
                self.cost_usd += reply.cost_usd
            if note:
                self.last_note = note

        # The same shape as the `reply` line in server.py, so one grep of the
        # log answers "what did she spend this on" across both.
        log.info("consolidated",
                 extra={"why": why, "manual": manual, "wall_ms": wall_ms,
                        "cost_usd": reply.cost_usd, "billing": reply.billing,
                        "reply_chars": len(reply.text),
                        "episodes": len(episodes), "through_id": highest,
                        "parsed": parsed, "added": adds, "removed": drops,
                        "over_cap": over,
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "note": note})
        # `ok` is whether the answer was usable, not whether the thread got to
        # the end of itself: a pass that spent money and produced nothing
        # applicable is a failure worth seeing in `luna audit`.
        self._audit(parsed,
                    why=note or (f"consolidation pass ({why})" if parsed
                                 else "the model's answer could not be read"),
                    manual=manual, episodes=len(episodes), added=adds,
                    removed=drops, over_cap=over, cost_usd=reply.cost_usd,
                    through_id=highest, files=applied)
        return {"ran": True, "parsed": parsed, "episodes": len(episodes),
                "through_id": highest, "added": adds, "removed": drops,
                "over_cap": over, "cost_usd": reply.cost_usd,
                "wall_ms": wall_ms, "note": note, "files": applied}

    def preview(self, why: str = "luna memory consolidate --dry-run"
                ) -> dict[str, Any]:
        """The same pass, up to the moment it would write. Applies nothing.

        Not ``run_once`` with a flag threaded through it, and emphatically not
        "apply and then undo": ``replace`` has no inverse, so there would be
        nothing to undo with, and a dry run whose safety depended on a second
        write going right would be the most dangerous command in the program.

        What makes this one safe is structural and can be checked by reading
        it. The two things a pass changes are the tier-1 files, through
        ``Tier1File.replace``, and the watermark, through ``set_meta``.
        Neither name appears in this method. The proposal is turned into a
        report by :func:`preview_edit`, which is handed a list of strings and
        an integer cap and has no way to reach a file at all; tier 3 is built
        with ``persist=False``; and the watermark is read and left alone, so
        the next pass — real or dry — is offered exactly the batch that was
        previewed here. Advancing it would be the cruellest possible bug: the
        episodes you looked at would be skipped by the pass you were deciding
        whether to allow.

        What it does share with a real pass is everything before the write:
        the same episodes, the same prompt, the same model call, the same
        money. A preview that was cheaper than the thing it previews would be
        previewing something else.
        """
        started = time.monotonic()
        after, episodes, profile = self._read_batch(persist_profile=False)
        if not episodes:
            log.info("consolidation preview had nothing to read",
                     extra={"why": why, "after_id": after, "episodes": 0})
            return {"ran": False, "reason": "no new episodes",
                    "after_id": after, "profile": profile.get("episodes", 0)}

        highest = max(e.id for e in episodes)
        try:
            reply = self._ask_model(episodes, profile)
        except agent_mod.AgentError as exc:
            with self._lock:
                self.failures += 1
                self.last_note = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("consolidation preview could not reach the agent",
                        extra={"why": why, "episodes": len(episodes),
                               "detail": str(exc)[:300]})
            self._audit(False, manual=True, dry_run=True,
                        why=f"consolidation preview failed: {exc}"[:300],
                        episodes=len(episodes))
            return {"ran": False, "reason": "agent unavailable",
                    "detail": str(exc)}

        # Paid for, so it is counted and spent whatever the reply turns out to
        # say — the money left the account when the call did, not when the
        # answer proved usable.
        if self.on_spend is not None:
            self.on_spend(reply.cost_usd)
        files: dict[str, Any] = {}
        note = ""
        parsed = False
        try:
            proposal = parse_proposal(reply.text)
            note = str(proposal.get("note") or "")[:300]
            for handle in (self.memory.luna, self.memory.user):
                entries = handle.entries()
                adds, removes = clean_edit(proposal.get(handle.name),
                                           len(entries))
                files[handle.name] = preview_edit(handle.name, entries,
                                                  handle.cap, adds, removes)
            parsed = True
        except ValueError as exc:                     # JSONDecodeError too
            with self._lock:
                self.failures += 1
                self.last_note = f"unparseable proposal: {exc}"[:200]
            log.warning("consolidation preview reply was not usable",
                        extra={"why": why, "detail": str(exc)[:300],
                               "cost_usd": reply.cost_usd})

        wall_ms = int((time.monotonic() - started) * 1000)
        adds_n = sum(len(v["added"]) for v in files.values())
        drops_n = sum(len(v["removed"]) for v in files.values())
        over = [name for name, v in files.items() if v.get("over_cap")]
        usage = reply.usage or {}
        with self._lock:
            self.previews += 1
            if reply.cost_usd:
                self.cost_usd += reply.cost_usd
            if note:
                self.last_note = note

        log.info("consolidation previewed",
                 extra={"why": why, "manual": True, "wall_ms": wall_ms,
                        "cost_usd": reply.cost_usd, "billing": reply.billing,
                        "reply_chars": len(reply.text),
                        "episodes": len(episodes), "through_id": highest,
                        "parsed": parsed, "would_add": adds_n,
                        "would_remove": drops_n, "over_cap": over,
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "note": note})
        # In the log with the rest, because it spent the user's money, and
        # with `would_` field names because an audit reader scanning for what
        # was removed from LUNA.md must not find this entry and believe it.
        self._audit(parsed,
                    why=note or (f"consolidation dry run ({why})" if parsed
                                 else "the model's answer could not be read"),
                    manual=True, dry_run=True, episodes=len(episodes),
                    would_add=adds_n, would_remove=drops_n, over_cap=over,
                    cost_usd=reply.cost_usd, through_id=highest, files=files)
        return {"ran": True, "parsed": parsed, "episodes": len(episodes),
                "through_id": highest, "after_id": after, "added": adds_n,
                "removed": drops_n, "over_cap": over,
                "cost_usd": reply.cost_usd, "wall_ms": wall_ms, "note": note,
                "files": files}

    def _apply(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """Apply one proposal, file by file, under the ordinary cap rules."""
        out: dict[str, Any] = {}
        for handle in (self.memory.luna, self.memory.user):
            entries = handle.entries()
            adds, removes = clean_edit(proposal.get(handle.name), len(entries))
            record: dict[str, Any] = {"added": adds, "removed": [], "over_cap": None}
            if not adds and not removes:
                out[handle.name] = record
                continue
            dropped = set(removes)
            # The removed text is carried into the record before the write, so
            # the audit log holds it even though `replace` has no inverse. An
            # entry this pass deleted is then recoverable by reading the log,
            # which is the most that can honestly be offered for a write that
            # discards text nothing else keeps.
            record["removed"] = [entries[i] for i in removes]
            proposed = [e for i, e in enumerate(entries) if i not in dropped]
            proposed += adds
            try:
                record["usage"] = handle.replace(proposed)
            except MemoryCapExceeded as exc:
                # A normal outcome, not a fault. The file is untouched, the
                # overflow is recorded with everything needed to act on it,
                # and the next pass sees the same episodes plus whatever has
                # happened since.
                record["over_cap"] = exc.to_dict()
                record["added"], record["removed"] = [], []
                log.info("consolidation proposal did not fit",
                         extra={"file": handle.name, "overflow": exc.overflow,
                                "cap": exc.cap, "proposed": exc.proposed_chars})
            except LunaMemoryError as exc:
                record["error"] = str(exc)
                record["added"], record["removed"] = [], []
                log.warning("consolidation write refused",
                            extra={"file": handle.name, "detail": str(exc)})
            out[handle.name] = record
        return out

    def _audit(self, ok: bool, **fields: Any) -> None:
        """One line per pass. No undo is claimed — see the note in ``_apply``."""
        if self.audit is None:
            return
        try:
            self.audit.append("memory.consolidate", ok=ok, actor="luna",
                              **fields)
        except Exception:  # noqa: BLE001 - a full disk must not kill the thread
            log.exception("could not write the consolidation audit entry")

    # -- reporting and shutdown -------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            # `_busy` and not "is the thread alive": a pass asked for from the
            # CLI runs on the daemon's request thread, and `luna status` in a
            # second terminal has to be able to see it.
            return {
                "every_turns": self.every,
                "enabled": self.every > 0,
                "turns_since": self.turns,
                "running": self._busy,
                "passes": self.passes,
                "previews": self.previews,
                "skipped": self.skipped,
                "failures": self.failures,
                "added": self.added,
                "removed": self.removed,
                "over_cap": self.over_cap,
                "cost_usd": round(self.cost_usd, 6),
                "note": self.last_note,
                "through_id": _as_int(
                    self.memory.episodes.get_meta(CONSOLIDATED_THROUGH)),
            }

    def close(self, timeout: float = 5.0) -> None:
        """Wait, briefly, for a pass in flight.

        Bounded rather than open-ended: a pass is one agent call and the
        daemon is being asked to stop. Left to run, its thread would write
        into a memory tree the process is about to close — which in the test
        suite means a temporary directory that has already been deleted.
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        # A pass asked for from the CLI has no thread of ours to join: it is
        # running on the daemon's request thread, and the tree it is about to
        # write into is the one this method exists to protect. So the flag is
        # waited on too, on the same deadline — polled rather than signalled
        # because this happens once, at shutdown, and one piece of state that
        # cannot fall out of step with itself beats two that can.
        while time.monotonic() < deadline:
            with self._lock:
                if not self._busy:
                    return
            time.sleep(0.02)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
