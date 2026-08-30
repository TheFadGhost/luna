"""Tier 3: the derived profile, and the two promises it makes.

The promises are that it is **derived** — regenerable from tier 2, so deleting
it costs nothing and it can never be the only copy of anything — and that it is
**measured**, not written. Most of what follows is one of those two: a case
that deletes the file and rebuilds it identically, or a case that checks a
number came out of the episodes rather than out of a heuristic's opinion.

Nothing here calls a model. Tier 3 is stdlib end to end, which is the whole
reason it is cheap enough to rebuild on a turn counter.
"""

from __future__ import annotations

import json
import time

from lunad import config
from lunad.memory import (Episode, Profile, accumulate_persona, extract_facts)

from ._support import TempMemoryCase


def _episode(user: str, luna: str = "", ts: float = 0.0, salience: float = 0.3,
             surface: str = "cli", ident: int = 1) -> Episode:
    return Episode(id=ident, ts=ts or time.time(), surface=surface,
                   user_text=user, luna_text=luna, salience=salience)


class FactExtractionTests(TempMemoryCase):
    """The factual half — schema extraction from the user's own words."""

    def facts(self, *texts: str) -> dict[str, list[dict]]:
        return extract_facts([_episode(t, ident=i)
                              for i, t in enumerate(texts, 1)])

    def test_the_five_slots_pick_up_a_plain_statement(self) -> None:
        found = self.facts(
            "my name is Ghost",
            "I'm working on the bar widget.",
            "I use quickshell for everything.",
            "I prefer British spelling.",
            "never restart the shell.",
        )
        self.assertEqual([f["value"] for f in found["name"]], ["Ghost"])
        self.assertEqual([f["value"] for f in found["works_on"]], ["bar widget"])
        self.assertEqual([f["value"] for f in found["uses"]],
                         ["quickshell for everything"])
        self.assertEqual([f["value"] for f in found["prefers"]],
                         ["British spelling"])
        self.assertEqual([f["value"] for f in found["avoids"]],
                         ["restart the shell"])

    def test_a_home_path_counts_as_something_worked_on(self) -> None:
        found = self.facts("the fix is in ~/Work/luna/lunad and nowhere else")
        self.assertEqual([f["value"] for f in found["works_on"]],
                         ["~/Work/luna/lunad"])

    def test_support_counts_repetitions_and_keeps_the_first_spelling(self) -> None:
        found = self.facts("I use Quickshell.", "I use quickshell.",
                           "I use QUICKSHELL.")
        [fact] = found["uses"]
        self.assertEqual(fact["value"], "Quickshell")
        self.assertEqual(fact["support"], 3)

    def test_lunas_own_words_are_never_read(self) -> None:
        # She paraphrases what she was told, so counting her text would
        # manufacture support for anything she repeated -- including a
        # mistake.
        found = extract_facts([_episode("what am I called?",
                                        luna="my name is Luna and I use piper")])
        self.assertEqual(found, {})

    def test_an_empty_history_produces_no_facts(self) -> None:
        self.assertEqual(extract_facts([]), {})

    def test_only_the_top_n_survive_per_slot(self) -> None:
        found = self.facts(*[f"I use tool{n}." for n in range(20)])
        self.assertEqual(len(found["uses"]), config.PROFILE_TOP_N)


class PersonaAccumulatorTests(TempMemoryCase):
    """The persona half — counters and evidence, never prose."""

    def test_corrections_come_from_the_stored_score_not_a_second_detector(self):
        # score_salience already ran at write time and 1.0 is its sentinel. A
        # detector here would be a copy that can disagree with the first.
        persona = accumulate_persona([
            _episode("no, that's wrong", salience=config.CORRECTION_SALIENCE),
            _episode("what is the weather", salience=0.3),
        ])
        self.assertEqual(persona["corrections"]["count"], 1)
        self.assertEqual(persona["corrections"]["recent"], ["no, that's wrong"])

    def test_approval_and_friction_are_counted_separately(self) -> None:
        persona = accumulate_persona([
            _episode("perfect, thanks"),
            _episode("that's too long"),
            _episode("just tell me the number"),
        ])
        self.assertEqual(persona["approval"]["count"], 1)
        self.assertEqual(persona["friction"]["count"], 2)

    def test_reply_length_is_paired_with_the_reply_being_reacted_to(self) -> None:
        # The reaction is about the *previous* reply, which is the only reply
        # the user had seen when they typed it.
        short, long = "word " * 10, "word " * 100
        persona = accumulate_persona([
            _episode("explain it", luna=short, ts=1.0),
            _episode("perfect", luna="ok", ts=2.0),
            _episode("and the other one", luna=long, ts=3.0),
            _episode("that's too long", luna="ok", ts=4.0),
        ])
        self.assertEqual(persona["length"]["praised_reply_words"], 10)
        self.assertEqual(persona["length"]["criticised_reply_words"], 100)

    def test_no_reaction_reports_none_rather_than_zero(self) -> None:
        # "no reply has ever drawn a complaint" and "the replies that draw
        # complaints are zero words long" are different statements.
        persona = accumulate_persona([_episode("hello", luna="hi")])
        self.assertIsNone(persona["length"]["criticised_reply_words"])
        self.assertIsNone(persona["length"]["praised_reply_words"])
        self.assertEqual(persona["length"]["user_median_words"], 1)

    def test_vocabulary_needs_repetition_and_ignores_short_words(self) -> None:
        persona = accumulate_persona(
            [_episode("hyprland and the bar")] * 3
            + [_episode("quickshell once")])
        words = dict(tuple(pair) for pair in persona["vocabulary"])
        self.assertEqual(words.get("hyprland"), 3)
        self.assertNotIn("bar", words)          # under VOCAB_MIN_CHARS
        self.assertNotIn("quickshell", words)   # under VOCAB_MIN_COUNT

    def test_hours_and_surfaces_are_histograms_of_the_real_rows(self) -> None:
        at_nine = time.mktime((2026, 8, 30, 9, 30, 0, 0, 0, -1))
        persona = accumulate_persona([
            _episode("one", ts=at_nine, surface="voice"),
            _episode("two", ts=at_nine, surface="cli"),
        ])
        self.assertEqual(persona["hours"], {"09": 2})
        self.assertEqual(persona["surfaces"], {"cli": 1, "voice": 1})


class ProfileFileTests(TempMemoryCase):
    """The file itself: where it lives, how it is written, and that it is free."""

    def seeded(self):
        store = self.episodes()
        store.record("my name is Ghost", "Noted.", salience=0.4)
        store.record("I use quickshell.", "Right.", salience=0.4)
        store.record("no, the bar is omarchy-shell", "Corrected.",
                     salience=config.CORRECTION_SALIENCE)
        return store

    def test_it_lives_beside_the_episode_store_it_is_derived_from(self) -> None:
        # Not at a fixed path: a caller who redirects tier 2 must get tier 3
        # redirected with it, or a test rebuilds over the user's real profile.
        mem = self.memory()
        self.assertEqual(mem.profile.path,
                         self.root / config.PROFILE_JSON.name)

    def test_a_rebuild_writes_a_readable_profile(self) -> None:
        profile = Profile(self.root / "profile.json")
        payload = profile.rebuild(self.seeded())
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["episodes"], 3)
        self.assertEqual(payload["through_id"], 3)
        self.assertEqual(payload["persona"]["corrections"]["count"], 1)
        self.assertEqual(json.loads(profile.path.read_text()), payload)

    def test_it_is_regenerable_so_deleting_it_costs_nothing(self) -> None:
        store = self.seeded()
        profile = Profile(self.root / "profile.json")
        first = profile.rebuild(store, now=1000.0)
        profile.clear()
        self.assertFalse(profile.path.exists())
        self.assertEqual(profile.load(), {})
        again = profile.rebuild(store, now=1000.0)
        self.assertEqual(first, again)

    def test_the_write_is_atomic_and_leaves_no_temp_file(self) -> None:
        profile = Profile(self.root / "profile.json")
        profile.rebuild(self.seeded())
        self.assertEqual([p.name for p in self.root.glob("*.tmp")], [])

    def test_a_corrupt_profile_reads_as_absent_rather_than_raising(self) -> None:
        # Tier 3 is derived, so the cure for damage is a rebuild. A malformed
        # profile must never be the reason an ask fails.
        profile = Profile(self.root / "profile.json")
        profile.path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(profile.load(), {})
        self.assertEqual(profile.block(), "")
        profile.rebuild(self.seeded())
        self.assertTrue(profile.load())

    def test_the_window_bounds_what_is_read(self) -> None:
        store = self.episodes()
        for n in range(10):
            store.record(f"message {n}", "ok", ts=1000.0 + n, salience=0.2)
        payload = Profile(self.root / "profile.json").rebuild(store, limit=4)
        self.assertEqual(payload["episodes"], 4)
        self.assertEqual(payload["window"], 4)
        self.assertEqual(payload["through_id"], 10)

    def test_status_says_implemented_and_derived(self) -> None:
        profile = Profile(self.root / "profile.json")
        before = profile.status()
        self.assertTrue(before["implemented"])
        self.assertTrue(before["derived"])
        self.assertFalse(before["exists"])
        profile.rebuild(self.seeded())
        after = profile.status()
        self.assertTrue(after["exists"])
        self.assertEqual(after["episodes"], 3)
        self.assertEqual(after["corrections"], 1)
        self.assertGreater(after["size_bytes"], 0)


class ProfileBlockTests(TempMemoryCase):
    """What the consolidation pass actually reads."""

    def test_an_empty_profile_renders_to_nothing(self) -> None:
        self.assertEqual(Profile(self.root / "profile.json").block(), "")

    def test_facts_carry_their_support_so_a_guess_looks_like_one(self) -> None:
        store = self.episodes()
        store.record("I use quickshell.", "ok", salience=0.2)
        store.record("I use quickshell.", "ok", salience=0.2)
        store.record("my name is Ghost", "ok", salience=0.2)
        profile = Profile(self.root / "profile.json")
        block = profile.block(profile.rebuild(store))
        self.assertIn("quickshell (x2)", block)
        self.assertIn("Ghost (x1)", block)
        self.assertIn("treat it as a guess", block)

    def test_the_evidence_is_verbatim_and_clipped(self) -> None:
        store = self.episodes()
        store.record("no, " + "x" * 500, "sorry",
                     salience=config.CORRECTION_SALIENCE)
        profile = Profile(self.root / "profile.json")
        block = profile.block(profile.rebuild(store))
        self.assertIn("Most recent corrections:", block)
        self.assertIn("...", block)
        self.assertNotIn("x" * 300, block)
