"""Tier 2: SQLite + FTS5 episodic memory."""

from __future__ import annotations

import sqlite3
import time
import unittest

from lunad import config
from lunad.memory import (
    EPISODE_TEXT_CHARS,
    SIMILARITY_QUERY_CHARS,
    FTS5Unavailable,
    MemoryError as LunaMemoryError,
    assert_fts5,
    build_fts_query,
)

from ._support import TempMemoryCase

DAY = 86400.0


class FTS5AvailabilityTests(unittest.TestCase):
    def test_this_python_has_fts5(self):
        # If this fails, lunad refuses to start. That is the intended
        # behaviour, and this test is the early warning.
        assert_fts5()

    def test_probe_raises_the_documented_error_when_fts5_is_missing(self):
        class NoFTS:
            def execute(self, *_a, **_k):
                raise sqlite3.OperationalError("no such module: fts5")

        with self.assertRaises(FTS5Unavailable) as ctx:
            assert_fts5(NoFTS())
        self.assertIn("FTS5", str(ctx.exception))
        self.assertIn("will not start", str(ctx.exception))


class QueryBuildingTests(unittest.TestCase):
    def test_punctuation_that_is_fts5_syntax_is_neutralised(self):
        for raw in ["what about the bar-widget?", "don't \"quote\" me",
                    "NEAR(a b)", "a AND OR NOT b", "*", "foo:bar", "50%"]:
            with self.subTest(raw=raw):
                built = build_fts_query(raw)
                if built is not None:
                    self.assertNotIn("*", built)

    def test_stopwords_are_dropped(self):
        self.assertEqual(build_fts_query("what did we decide about the widget"),
                         '"decide" OR "widget"')

    def test_all_stopwords_returns_none_rather_than_matching_on_filler(self):
        # The old behaviour fell back to querying on the raw stopword tokens
        # themselves when filtering emptied the list -- which is backwards:
        # a query that is entirely filler is exactly the case that should
        # retrieve nothing. Confirmed against the real database: this is the
        # shape of query that matched 10 of 15 unrelated episodes.
        self.assertIsNone(build_fts_query("what is it"))

    def test_short_non_stopword_leftovers_do_not_clear_the_rarity_gate(self):
        # "bit" and "way" are real, non-stopword words, but three-letter
        # leftovers with nothing longer are still not a signal worth
        # building a query around.
        self.assertIsNone(build_fts_query("get a bit of a way on"))

    def test_and_mode_requires_every_token_in_the_same_row(self):
        self.assertEqual(build_fts_query("decide about the widget", mode="and"),
                         '"decide" AND "widget"')

    def test_no_usable_tokens_returns_none(self):
        self.assertIsNone(build_fts_query("!!! ??? ***"))


class EpisodeStoreTests(TempMemoryCase):
    def test_schema_and_round_trip(self):
        store = self.episodes()
        ep = store.record("where does the socket live",
                          "under XDG_RUNTIME_DIR/luna", surface="cli")
        self.assertGreater(ep.id, 0)
        rows = store.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_text, "where does the socket live")
        self.assertEqual(rows[0].luna_text, "under XDG_RUNTIME_DIR/luna")
        self.assertEqual(rows[0].surface, "cli")

    def test_fts_search_finds_by_keyword(self):
        store = self.episodes()
        store.record("we decided the bar widget should be monochrome", "noted")
        store.record("the kettle is broken", "unfortunate")
        hits = store.search("bar widget")
        self.assertEqual(len(hits), 1)
        self.assertIn("monochrome", hits[0].user_text)

    def test_fts_search_matches_luna_side_too(self):
        store = self.episodes()
        store.record("what did you do", "I rebuilt the piper voice cache")
        self.assertTrue(store.search("piper"))

    def test_fts_stemming_works(self):
        store = self.episodes()
        store.record("I am rebuilding the voice index", "done")
        self.assertTrue(store.search("rebuild"))

    def test_search_with_hostile_punctuation_does_not_raise(self):
        store = self.episodes()
        store.record("check the bar-widget", "ok")
        for query in ['"', "NEAR(", "a AND", "*", "don't", "bar-widget?"]:
            with self.subTest(query=query):
                store.search(query)  # must not raise

    def test_search_returns_nothing_for_an_unusable_query(self):
        self.assertEqual(self.episodes().search("!!!"), [])

    def test_deletes_are_mirrored_into_the_index(self):
        store = self.episodes()
        ep = store.record("ephemeral thought", "mm")
        store._conn.execute("DELETE FROM episodes WHERE id = ?", (ep.id,))
        store._conn.commit()
        self.assertEqual(store.search("ephemeral"), [])

    def test_salience_is_scored_on_record(self):
        store = self.episodes()
        bland = store.record("what time is it", "half four")
        correction = store.record("no, that's wrong", "understood")
        self.assertEqual(correction.salience, config.CORRECTION_SALIENCE)
        self.assertLess(bland.salience, correction.salience)

    def test_explicit_salience_overrides_the_heuristic(self):
        ep = self.episodes().record("anything", "", salience=0.123)
        self.assertEqual(ep.salience, 0.123)

    def test_repetition_lifts_salience_of_a_repeated_topic(self):
        store = self.episodes()
        first = store.record("please fix the flickering bar", "looking")
        for _ in range(4):
            store.record("please fix the flickering bar", "still looking")
        last = store.record("please fix the flickering bar", "fixed")
        self.assertGreater(last.salience, first.salience)

    def test_count_similar_caps_the_query_to_a_prefix_of_a_long_message(self):
        # count_similar used to build_fts_query() the entire, uncapped
        # message on every write -- a long dictated transcript made that
        # OR-query (proportional to token count) expensive every turn. The
        # same word, same message, is seen when it falls inside the cap and
        # invisible past it: that difference is the cap, and nothing else
        # ("xylophone" never appears in the stored episode at all).
        store = self.episodes()
        store.record("please remember the special unicorn codeword", "ok")
        padding = "xylophone " * ((SIMILARITY_QUERY_CHARS // 10) + 5)
        self.assertGreater(len(padding), SIMILARITY_QUERY_CHARS)
        self.assertEqual(store.count_similar(padding + "unicorn"), 0)
        self.assertEqual(store.count_similar("unicorn " + padding), 1)

    def test_record_clips_pathologically_long_text(self):
        store = self.episodes()
        ep = store.record("a" * (EPISODE_TEXT_CHARS + 5000),
                          "b" * (EPISODE_TEXT_CHARS + 5000))
        self.assertEqual(len(ep.user_text), EPISODE_TEXT_CHARS)
        self.assertEqual(len(ep.luna_text), EPISODE_TEXT_CHARS)
        [stored] = store.recent()
        self.assertEqual(len(stored.user_text), EPISODE_TEXT_CHARS)
        self.assertEqual(len(stored.luna_text), EPISODE_TEXT_CHARS)

    def test_ordinary_length_text_is_unaffected_by_the_cap(self):
        store = self.episodes()
        ep = store.record("a normal message", "a normal reply")
        self.assertEqual(ep.user_text, "a normal message")
        self.assertEqual(ep.luna_text, "a normal reply")

    def test_decay_is_applied_at_read_time_and_rows_are_untouched(self):
        store = self.episodes()
        # The setting, not the constant: decay reads `[memory]
        # decay_half_life_days`, and the constant is only its fallback.
        hl = self.settings.get("memory.decay_half_life_days")
        old_ts = time.time() - hl * DAY
        ep = store.record("the old note about kettles", "mm",
                          ts=old_ts, salience=0.8)
        [row] = store.recent()
        self.assertAlmostEqual(row.effective_salience, 0.4, places=3)
        # Stored value is unchanged: decay never mutates history.
        stored = store._conn.execute(
            "SELECT salience FROM episodes WHERE id = ?", (ep.id,)).fetchone()
        self.assertAlmostEqual(stored["salience"], 0.8, places=6)

    def test_an_old_correction_still_outranks_fresh_trivia(self):
        store = self.episodes()
        year_ago = time.time() - 365 * DAY
        store.record("no, the terminal is foot", "understood", ts=year_ago)
        store.record("the terminal window is quite wide today", "it is")
        hits = store.search("terminal")
        self.assertEqual(len(hits), 2)
        self.assertIn("foot", hits[0].user_text)

    def test_filler_query_retrieves_nothing(self):
        # Reproduces the audit finding against a stand-in for the real
        # database: a battery question, a bar-widget rewrite, a Quickshell
        # version check, a greeting. None of it is about the filler query,
        # and the old OR-only build_fts_query matched most of it anyway.
        store = self.episodes()
        store.record("Hello, what is the battery level right now?",
                     "About 62 percent.")
        store.record("What's eating my battery?", "Mostly the browser.")
        store.record("I want to rewrite my whole bar widget in React.",
                     "That is a lot of scope for tonight.")
        store.record("look up the current Quickshell version",
                     "You are on the latest.")
        store.record("hello", "hi")
        self.assertEqual(
            store.search("so anyway do you think I should do something "
                         "about this"),
            [])

    def test_and_then_widens_to_or_with_partial_coverage(self):
        store = self.episodes()
        store.record("the bar widget needs a rewrite", "noted")
        store.record("the kettle finally boiled", "good")
        # No episode has both "widget" and "kettle" -- the AND pass finds
        # nothing -- so this must widen to OR and report partial coverage
        # rather than come back empty.
        hits = store.search("widget kettle")
        self.assertEqual({h.user_text for h in hits},
                         {"the bar widget needs a rewrite",
                          "the kettle finally boiled"})
        for h in hits:
            self.assertAlmostEqual(h.coverage, 0.5)

    def test_and_pass_hits_have_full_coverage(self):
        store = self.episodes()
        store.record("we decided the bar widget should be monochrome", "ok")
        [hit] = store.search("bar widget")
        self.assertEqual(hit.coverage, 1.0)

    def test_stats(self):
        store = self.episodes()
        store.record("a", "b")
        stats = store.stats()
        self.assertEqual(stats["episodes"], 1)
        self.assertGreater(stats["size_bytes"], 0)

    def test_recall_block_renders_hits(self):
        mem = self.memory()
        mem.episodes.record("we agreed on jenny_dioco for the voice", "yes")
        block = mem.recall_block("voice")
        self.assertIn("jenny_dioco", block)
        self.assertIn("salience", block)

    def test_recall_block_is_empty_when_nothing_matches(self):
        self.assertEqual(self.memory().recall_block("nothing here"), "")

    def test_recall_block_refuses_weak_partial_matches(self):
        # "widget kettle printer" shares only one of its three content
        # tokens with either episode (coverage 1/3). raw search() still
        # returns both -- it stays permissive for callers who want to judge
        # weak hits themselves -- but recall_block must inject neither: a
        # one-in-three match presented as "possibly relevant" reads as
        # confirmed context to whatever reads the prompt, and that is worse
        # than nothing. (A two-of-two-tokens-down-to-one case, e.g. "what
        # voice did we choose" against an episode about choosing a voice, is
        # coverage 0.5 and clears the floor -- see
        # test_recall_block_keeps_a_strong_single_token_partial_match.)
        mem = self.memory()
        mem.episodes.record("the bar widget needs a rewrite", "noted")
        mem.episodes.record("the kettle finally boiled", "good")
        self.assertTrue(mem.episodes.search("widget kettle printer"))
        self.assertEqual(mem.recall_block("widget kettle printer"), "")

    def test_recall_block_keeps_a_strong_single_token_partial_match(self):
        # An irregular verb defeats the porter stemmer ("choose" vs.
        # "chose"), so the AND pass fails and this widens to OR on "voice"
        # alone -- coverage 0.5 on a two-token query. That still clears the
        # floor: one already rarity-gated, specific shared word is a real
        # signal, not noise.
        mem = self.memory()
        mem.episodes.record("we chose jenny_dioco for the voice", "agreed")
        block = mem.recall_block("what voice did we choose")
        self.assertIn("jenny_dioco", block)

    def test_recall_block_keeps_full_coverage_hits(self):
        mem = self.memory()
        mem.episodes.record("we decided the bar widget should be monochrome",
                            "noted")
        block = mem.recall_block("bar widget")
        self.assertIn("monochrome", block)


if __name__ == "__main__":
    unittest.main()
