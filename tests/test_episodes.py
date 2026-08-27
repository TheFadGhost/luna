"""Tier 2: SQLite + FTS5 episodic memory."""

from __future__ import annotations

import sqlite3
import time
import unittest

from lunad import config
from lunad.memory import (
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

    def test_all_stopwords_falls_back_to_the_raw_tokens(self):
        self.assertEqual(build_fts_query("what is it"), '"what" OR "is" OR "it"')

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


if __name__ == "__main__":
    unittest.main()
