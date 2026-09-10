"""Semantic recall — the vector half of tier 2.

Nothing here downloads a model, imports onnxruntime, or spawns a worker.
Three things make that true and all three are load-bearing:

* ``tests/_support`` replaces ``config.VENV_PYTHON`` process-wide with a path
  that cannot resolve, and ``Embedder.python()`` reads it *late*, so a real
  ``Embedder`` in this suite reports itself unavailable and never forks.
* Every case that needs an opinion from the model uses :class:`FakeEmbedder`,
  which is the whole of the contract ``EpisodeStore`` depends on and none of
  the machinery.
* :class:`StructuralTests` at the bottom reads the shipped source to prove the
  first point cannot be undone by accident.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from ._support import TempMemoryCase  # noqa: F401 - installs the redirects

from lunad import config, embed
from lunad.memory import RECALL_COVERAGE_FLOOR, EpisodeStore, Memory


_REAL_PAUSE = embed.BACKFILL_PAUSE_S


def setUpModule() -> None:
    """Take the sleep out of the backfill for the whole module.

    ``BACKFILL_PAUSE_S`` exists so a backfill never competes with an answer
    for the same cores. In a test it is only sleep, and the background
    indexer thread pays it on every batch — 30 seconds of otherwise idle
    suite time. Read late from the module in ``backfill_vectors``, so this
    takes.
    """
    embed.BACKFILL_PAUSE_S = 0.0


def tearDownModule() -> None:
    embed.BACKFILL_PAUSE_S = _REAL_PAUSE


def _blob(seed: float = 0.0) -> bytes:
    """A vector-shaped payload. Never read as a vector by anything in-process."""
    return struct.pack(f"<{embed.EMBED_DIM}f",
                       *[seed + i * 1e-4 for i in range(embed.EMBED_DIM)])


class FakeEmbedder:
    """Everything ``EpisodeStore`` asks of an embedder, and nothing else.

    ``scores`` maps a query string to ``{episode_id: cosine}``. Queries that
    are not in it get ``{}``, which is the same "no opinion" the real thing
    returns when it is cold, slow or broken.
    """

    def __init__(self, scores: dict[str, dict[int, float]] | None = None,
                 *, on: bool = True, ready: bool = True,
                 fail_embed: bool = False) -> None:
        self.scores = scores or {}
        self.on = on
        self._ready = ready
        self.fail_embed = fail_embed
        self.queries: list[str] = []
        self.embedded: list[str] = []
        self.loaded: dict[str, list[int]] = {}
        self.warmed = 0

    # -- the surface EpisodeStore uses -----------------------------------

    def enabled(self) -> bool:
        return self.on

    @property
    def ready(self) -> bool:
        return self._ready

    def wait_ready(self, timeout: float = 0.0) -> bool:
        self.warmed += 1
        return self._ready

    def watermark(self, space: str) -> int | None:
        if not self._ready:
            return None
        got = self.loaded.get(space) or []
        return max(got) if got else 0

    def sync(self, space: str, rows) -> int:
        rows = list(rows)
        self.loaded.setdefault(space, []).extend(int(i) for i, _ in rows)
        return len(rows)

    def search(self, space: str, query: str, k: int = 24):
        self.queries.append(query)
        if not self._ready:
            return None
        return dict(self.scores.get(query, {}))

    def embed(self, texts, timeout: float = 0.0):
        if self.fail_embed:
            return None
        self.embedded.extend(texts)
        return [_blob(float(i)) for i in range(len(texts))]


class TokenizerTests(unittest.TestCase):
    """WordPiece in stdlib, against a hand-written vocabulary.

    The real vocab.txt is 30,522 lines of downloaded model; these cases use
    twenty, so a failure names a rule rather than a word.
    """

    VOCAB = ("[PAD] [UNK] [CLS] [SEP] [MASK] the battery charge is left low "
             "run ##ning na ##ive cafe , ? ' s 嗯 wa").split()

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="luna-vocab-")
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "vocab.txt"
        path.write_text("\n".join(self.VOCAB) + "\n", encoding="utf-8")
        self.tok = embed.WordPiece.load(path)

    def ids(self, text: str) -> list[str]:
        return [self.VOCAB[i] for i in self.tok.encode(text)]

    def test_wraps_in_cls_and_sep(self):
        self.assertEqual(self.ids("the battery"),
                         ["[CLS]", "the", "battery", "[SEP]"])

    def test_lowercases_and_strips_accents(self):
        # strip_accents is null in the model's config, which BERT resolves to
        # "follow do_lower_case" -- so "naïve" must reach the vocabulary as
        # "naive" and split on its ## suffix.
        self.assertEqual(self.ids("Naïve CAFÉ"),
                         ["[CLS]", "na", "##ive", "cafe", "[SEP]"])

    def test_splits_punctuation_into_its_own_tokens(self):
        self.assertEqual(self.ids("charge, is left?"),
                         ["[CLS]", "charge", ",", "is", "left", "?", "[SEP]"])

    def test_greedy_longest_match_finds_suffixes(self):
        self.assertEqual(self.ids("running"), ["[CLS]", "run", "##ning", "[SEP]"])

    def test_unknown_words_become_unk_whole(self):
        # Not partially consumed: BERT emits one [UNK] for a word it cannot
        # cover, rather than the prefix it managed plus [UNK].
        self.assertEqual(self.ids("battery zzzzzz"),
                         ["[CLS]", "battery", "[UNK]", "[SEP]"])

    def test_cjk_characters_are_one_token_each(self):
        self.assertEqual(self.ids("嗯嗯"), ["[CLS]", "嗯", "嗯", "[SEP]"])

    def test_absurdly_long_words_do_not_blow_up(self):
        self.assertEqual(self.ids("a" * 200), ["[CLS]", "[UNK]", "[SEP]"])

    def test_truncation_leaves_room_for_both_specials(self):
        ids = self.tok.encode(" ".join(["battery"] * 500), max_len=8)
        self.assertEqual(len(ids), 8)
        self.assertEqual(ids[0], self.tok.cls_id)
        self.assertEqual(ids[-1], self.tok.sep_id)

    def test_control_characters_and_replacement_chars_are_dropped(self):
        self.assertEqual(self.ids("the\x00� battery"),
                         ["[CLS]", "the", "battery", "[SEP]"])

    def test_a_vocabulary_without_the_specials_is_refused(self):
        path = Path(self.tmp.name) / "bad.txt"
        path.write_text("the\nbattery\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            embed.WordPiece.load(path)


class CoverageMappingTests(unittest.TestCase):
    """The one number that decides whether a paraphrase is allowed in."""

    def test_the_anchors_are_exactly_where_the_constants_say(self):
        self.assertEqual(embed.coverage_from_cosine(embed.SEMANTIC_FLOOR), 0.0)
        self.assertAlmostEqual(
            embed.coverage_from_cosine(embed.SEMANTIC_HALF), 0.5)
        self.assertEqual(embed.coverage_from_cosine(embed.SEMANTIC_FULL), 1.0)

    def test_the_half_point_lands_exactly_on_the_recall_floor(self):
        # This is the whole design: a semantic hit is admitted at the same
        # coverage a lexical one is, and the floor is not moved for it.
        self.assertEqual(
            embed.coverage_from_cosine(embed.SEMANTIC_HALF),
            RECALL_COVERAGE_FLOOR)

    def test_it_is_monotonic_and_clamped(self):
        previous = -1.0
        for step in range(-20, 121):
            value = embed.coverage_from_cosine(step / 100.0)
            self.assertGreaterEqual(value, previous)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
            previous = value

    def test_the_measured_battery_pair_clears_the_floor(self):
        # Measured against the real database: "how much charge is left"
        # scores 0.42 and 0.32 on the two battery episodes, and the best
        # unrelated episode scores 0.27.
        self.assertGreaterEqual(embed.coverage_from_cosine(0.42),
                                RECALL_COVERAGE_FLOOR)
        self.assertGreaterEqual(embed.coverage_from_cosine(0.32),
                                RECALL_COVERAGE_FLOOR)
        self.assertLess(embed.coverage_from_cosine(0.27),
                        RECALL_COVERAGE_FLOOR)


class ComposeTests(unittest.TestCase):
    def test_only_the_user_turn_is_embedded(self):
        text = embed.compose_episode_text("what about the battery",
                                          "I cannot read the battery")
        self.assertEqual(text, "what about the battery")

    def test_it_is_clipped(self):
        text = embed.compose_episode_text("x" * 5000, "")
        self.assertEqual(len(text), embed.EMBED_USER_CHARS)


class SearchUnionTests(TempMemoryCase):
    """The headline case, and everything it must not break."""

    def store(self, fake: FakeEmbedder) -> EpisodeStore:
        store = EpisodeStore(self.root / "episodes.db", embedder=fake)
        self.addCleanup(store.close)
        return store

    def seed(self, store: EpisodeStore) -> dict[str, int]:
        ids = {}
        ids["battery"] = store.record(
            "what is the battery level right now?",
            "I cannot read it without tools.").id
        ids["terminal"] = store.record(
            "name the terminal on this machine", "foot.").id
        ids["kettle"] = store.record("put the kettle on", "no.").id
        return ids

    def test_a_paraphrase_with_no_shared_token_is_found(self):
        # The measured failure this whole file exists for: FTS5 retrieves
        # nothing for "how much charge is left" because "charge" and
        # "battery" share no token and no stemmer relates them.
        plain = self.store(FakeEmbedder())
        ids = self.seed(plain)
        self.assertEqual(plain.search("how much charge is left"), [])

        fake = FakeEmbedder({"how much charge is left": {ids["battery"]: 0.42}})
        store = self.store(fake)
        [hit] = store.search("how much charge is left")
        self.assertEqual(hit.id, ids["battery"])
        self.assertAlmostEqual(hit.similarity, 0.42)
        self.assertGreaterEqual(hit.coverage, RECALL_COVERAGE_FLOOR)

    def test_the_paraphrase_reaches_the_prompt(self):
        fake = FakeEmbedder()
        mem = Memory(self.root / "LUNA.md", self.root / "USER.md",
                     self.root / "episodes.db", embedder=fake)
        self.addCleanup(mem.close)
        episode = mem.episodes.record("what is the battery level right now?",
                                      "I cannot read it without tools.")
        fake.scores = {"how much charge is left": {episode.id: 0.42}}
        block = mem.recall_block("how much charge is left")
        self.assertIn("battery level", block)

    def test_a_weak_cosine_is_refused_by_the_floor(self):
        fake = FakeEmbedder()
        mem = Memory(self.root / "LUNA.md", self.root / "USER.md",
                     self.root / "episodes.db", embedder=fake)
        self.addCleanup(mem.close)
        episode = mem.episodes.record("what is the battery level right now?",
                                      "I cannot read it without tools.")
        # 0.27 is the highest false positive measured on the real database.
        fake.scores = {"how much charge is left": {episode.id: 0.27}}
        self.assertEqual(mem.recall_block("how much charge is left"), "")

    def test_a_cosine_at_or_below_the_floor_is_not_even_a_candidate(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        ids = self.seed(store)
        fake.scores = {"how much charge is left":
                       {ids["battery"]: embed.SEMANTIC_FLOOR}}
        self.assertEqual(store.search("how much charge is left"), [])

    def test_filler_queries_never_reach_the_model(self):
        """The precision win of the previous pass, kept — one step earlier.

        A content-free query is refused by ``_content_tokens`` before either
        index is consulted, so the semantic path cannot reintroduce the noise
        and no model is asked about it at all.
        """
        fake = FakeEmbedder({"so anyway do you think I should do something "
                             "about this": {1: 0.9, 2: 0.9, 3: 0.9}})
        store = self.store(fake)
        self.seed(store)
        hits = store.search("so anyway do you think I should do "
                            "something about this")
        self.assertEqual(hits, [])
        self.assertEqual(fake.queries, [])

    def test_semantic_agreement_lifts_a_weak_lexical_hit(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        ids = self.seed(store)
        # "terminal kettle" matches neither row on AND, widens to OR, and
        # both hits come back at coverage 0.5.
        before = {h.id: h.coverage for h in store.search("terminal kettle")}
        self.assertEqual(set(before.values()), {0.5})
        fake.scores = {"terminal kettle": {ids["terminal"]: 0.7}}
        after = {h.id: h.coverage for h in store.search("terminal kettle")}
        self.assertEqual(after[ids["terminal"]], 1.0)
        self.assertEqual(after[ids["kettle"]], 0.5)

    def test_a_weak_cosine_never_lowers_a_lexical_coverage(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        ids = self.seed(store)
        fake.scores = {"terminal": {ids["terminal"]: 0.16}}
        [hit] = store.search("terminal")
        self.assertEqual(hit.coverage, 1.0)

    def test_full_coverage_lexical_hits_still_outrank_paraphrases(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        ids = self.seed(store)
        fake.scores = {"terminal": {ids["battery"]: 0.9,
                                    ids["terminal"]: 0.9}}
        hits = store.search("terminal")
        self.assertEqual(hits[0].id, ids["terminal"])

    def test_no_opinion_leaves_the_lexical_results_untouched(self):
        for fake in (FakeEmbedder(on=False), FakeEmbedder(ready=False),
                     FakeEmbedder()):
            store = EpisodeStore(self.root / f"e{id(fake)}.db", embedder=fake)
            self.addCleanup(store.close)
            self.seed(store)
            [hit] = store.search("kettle")
            self.assertEqual(hit.coverage, 1.0)
            self.assertEqual(hit.similarity, 0.0)

    def test_an_embedder_that_raises_cannot_reach_the_answer_path(self):
        class Exploding(FakeEmbedder):
            def search(self, space, query, k=24):
                raise RuntimeError("the worker caught fire")

        store = self.store(Exploding())
        self.seed(store)
        [hit] = store.search("kettle")
        self.assertEqual(hit.coverage, 1.0)

    def test_corrections_are_not_bypassed_by_the_semantic_path(self):
        """Salience and decay apply to a hit from either source.

        A correction pins at 1.0 and never decays. A semantic-only hit is
        hydrated through the same path, so it must arrive with that intact
        rather than with a score the vector index made up.
        """
        fake = FakeEmbedder()
        store = self.store(fake)
        episode = store.record("no, that's wrong — the terminal is foot",
                               "noted.")
        self.assertEqual(episode.salience, config.CORRECTION_SALIENCE)
        fake.scores = {"which shell emulator": {episode.id: 0.8}}
        [hit] = store.search("which shell emulator")
        self.assertEqual(hit.effective_salience, config.CORRECTION_SALIENCE)

    def test_to_dict_reports_why_a_hit_surfaced(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        ids = self.seed(store)
        fake.scores = {"how much charge is left": {ids["battery"]: 0.42}}
        [hit] = store.search("how much charge is left")
        payload = hit.to_dict()
        self.assertAlmostEqual(payload["similarity"], 0.42)
        self.assertGreaterEqual(payload["coverage"], RECALL_COVERAGE_FLOOR)


class BackfillTests(TempMemoryCase):
    def store(self, fake: FakeEmbedder) -> EpisodeStore:
        store = EpisodeStore(self.root / "episodes.db", embedder=fake)
        self.addCleanup(store.close)
        return store

    def test_existing_episodes_start_with_no_vectors(self):
        store = self.store(FakeEmbedder())
        for i in range(5):
            store.record(f"message {i}", "reply")
        self.assertEqual(store.vector_stats(),
                         {"episodes": 5, "vectors": 0, "max_vector_id": 0,
                          "pending": 5})

    def test_backfill_embeds_everything_and_is_idempotent(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        for i in range(9):
            store.record(f"message {i}", "reply")
        done, remaining = store.backfill_vectors(force=True)
        self.assertEqual((done, remaining), (9, 0))
        self.assertEqual(store.backfill_vectors(force=True), (0, 0))
        self.assertEqual(len(fake.embedded), 9)

    def test_only_the_user_turn_is_sent_to_the_model(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        store.record("the user said this", "and luna said that")
        store.backfill_vectors(force=True)
        self.assertEqual(fake.embedded, ["the user said this"])

    def test_a_budget_stops_early_and_the_rest_survives_for_next_time(self):
        """Resumability, which is the only thing that makes a backfill safe.

        Progress is the rows already in ``episode_vectors``; each batch is
        committed before the next starts. So a run that stops early — a
        budget here, a kill in real life — loses at most one batch and the
        next run picks up exactly where it left off, with no cursor to keep
        consistent.
        """
        store = self.store(FakeEmbedder())
        for i in range(10):
            store.record(f"message {i}", "reply")
        done, remaining = store.backfill_vectors(force=True, budget=4)
        self.assertEqual((done, remaining), (4, 6))
        first = {r["id"] for r in store.pending_vector_rows(10)}
        self.assertEqual(first, {5, 6, 7, 8, 9, 10})
        done, remaining = store.backfill_vectors(force=True)
        self.assertEqual((done, remaining), (6, 0))

    def test_a_failing_worker_stops_the_backfill_without_losing_progress(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        for i in range(9):
            store.record(f"message {i}", "reply")
        store.backfill_vectors(force=True, budget=4)
        fake.fail_embed = True
        done, remaining = store.backfill_vectors(force=True)
        self.assertEqual((done, remaining), (0, 5))

    def test_new_episodes_become_pending_again(self):
        store = self.store(FakeEmbedder())
        store.record("first", "reply")
        store.backfill_vectors(force=True)
        store.record("second", "reply")
        self.assertEqual(store.vector_stats()["pending"], 1)

    def test_a_disabled_embedder_does_no_work(self):
        fake = FakeEmbedder(on=False)
        store = self.store(fake)
        store.record("first", "reply")
        self.assertEqual(store.backfill_vectors(force=True), (0, 1))
        self.assertEqual(fake.embedded, [])

    def test_a_big_first_pass_waits_for_mains_power(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        for i in range(embed.BACKFILL_BATTERY_LIMIT + 1):
            store.record(f"message {i}", "reply")
        original = embed.on_mains_power
        embed.on_mains_power = lambda: False
        self.addCleanup(setattr, embed, "on_mains_power", original)
        done, remaining = store.backfill_vectors()
        self.assertEqual(done, 0)
        self.assertEqual(fake.embedded, [])
        # ...but force overrides it, and a small catch-up never asks.
        self.assertGreater(store.backfill_vectors(force=True)[0], 0)

    def test_a_small_catch_up_runs_on_battery(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        for i in range(3):
            store.record(f"message {i}", "reply")
        original = embed.on_mains_power
        embed.on_mains_power = lambda: False
        self.addCleanup(setattr, embed, "on_mains_power", original)
        self.assertEqual(store.backfill_vectors(), (3, 0))

    def test_a_machine_with_no_battery_counts_as_mains(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        for i in range(embed.BACKFILL_BATTERY_LIMIT + 1):
            store.record(f"message {i}", "reply")
        original = embed.on_mains_power
        embed.on_mains_power = lambda: None
        self.addCleanup(setattr, embed, "on_mains_power", original)
        self.assertGreater(store.backfill_vectors()[0], 0)

    def test_the_worker_is_given_only_the_vectors_it_lacks(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        for i in range(6):
            store.record(f"message {i}", "reply")
        store.backfill_vectors(force=True)
        # backfill syncs as it goes; a second sync must find nothing new.
        self.assertEqual(sorted(fake.loaded[str(store.path)]),
                         [1, 2, 3, 4, 5, 6])
        self.assertEqual(store._sync_worker(fake), 0)

    def test_a_cold_worker_is_sent_nothing_until_it_is_ready(self):
        fake = FakeEmbedder(ready=False)
        store = self.store(fake)
        store.record("first", "reply")
        store.store_vectors([(1, _blob())])
        self.assertEqual(store._sync_worker(fake), 0)

    def test_vectors_from_another_model_are_never_handed_over(self):
        fake = FakeEmbedder()
        store = self.store(fake)
        store.record("first", "reply")
        store.store_vectors([(1, _blob())], model="some-other-model")
        self.assertEqual(store._sync_worker(fake), 0)

    def test_stats_reports_how_much_of_tier_two_is_embedded(self):
        store = self.store(FakeEmbedder())
        store.record("first", "reply")
        self.assertEqual(store.stats()["vectors_pending"], 1)
        store.backfill_vectors(force=True)
        self.assertEqual(store.stats()["vectors"], 1)
        self.assertEqual(store.stats()["vectors_pending"], 0)


class DegradationTests(TempMemoryCase):
    """A fresh clone has no model. Everything must still work."""

    def test_a_real_embedder_in_this_suite_is_unavailable(self):
        emb = embed.Embedder()
        self.assertFalse(emb.available())
        self.assertFalse(emb.enabled())
        self.assertFalse(emb.ready)

    def test_an_absent_model_directory_is_reported_not_guessed(self):
        self.assertFalse(embed.model_present(self.root / "nothing-here"))

    def test_a_store_with_no_model_behaves_exactly_as_it_did_before(self):
        store = EpisodeStore(self.root / "episodes.db")
        self.addCleanup(store.close)
        store.record("name the terminal on this machine", "foot.")
        [hit] = store.search("terminal")
        self.assertEqual(hit.coverage, 1.0)
        self.assertEqual(hit.similarity, 0.0)
        self.assertEqual(store.search("how much charge is left"), [])

    def test_warming_an_unavailable_embedder_spawns_nothing(self):
        import time
        emb = embed.Embedder()
        emb.warm()
        self.assertIsNone(emb._proc)
        self.assertIsNone(emb.search("space", "anything"))
        started = time.monotonic()
        self.assertIsNone(emb.embed(["anything"]))
        self.assertFalse(emb.wait_ready())
        # And it says so at once. Waiting out the spawn timeout on an event
        # nobody is going to set is how a machine with no model made every
        # background backfill sit still for thirty seconds.
        self.assertLess(time.monotonic() - started, 1.0)

    def test_status_is_answerable_without_a_model(self):
        status = embed.Embedder().status()
        self.assertEqual(status["model"], embed.MODEL_NAME)
        self.assertEqual(status["licence"], "Apache-2.0")
        self.assertFalse(status["available"])

    def test_the_singleton_can_be_swapped_and_restored(self):
        fake = FakeEmbedder()
        previous = embed.use_embedder(fake)
        self.addCleanup(embed.use_embedder, previous)
        self.assertIs(embed.get_embedder(), fake)


class StructuralTests(unittest.TestCase):
    """Read the shipped source. These fail when somebody undoes the isolation."""

    SOURCE = (Path(__file__).resolve().parent.parent / "lunad" / "embed.py")

    def test_the_daemon_half_imports_no_scientific_stack(self):
        """onnxruntime and numpy may only be imported inside the worker.

        lunad is stock system python and neither package is installed there.
        A module-level import would turn `import lunad.memory` into an
        ImportError on the daemon's own interpreter.
        """
        for lineno, line in enumerate(
                self.SOURCE.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import numpy", "import onnxruntime",
                                    "from numpy", "from onnxruntime")):
                self.assertTrue(
                    line.startswith("        "),
                    f"embed.py:{lineno} imports the model stack at import "
                    f"time: {stripped}")

    def test_the_model_path_is_read_late(self):
        """``models_dir`` must be a function, not a constant.

        A constant bound at import ignores a redirected ``config.STATE_DIR``,
        which is the same class of bug ``tests/_support`` documents for every
        other outward-facing name.
        """
        self.assertTrue(callable(embed.models_dir))
        self.assertTrue(callable(embed.model_dir))

    def test_nothing_downloads_outside_the_explicit_fetch_step(self):
        """No silent download on first ask — the fetch is a command.

        A fresh clone has no model and must degrade to FTS5, not quietly pull
        86 MB off the network in the middle of answering a question.
        """
        text = self.SOURCE.read_text(encoding="utf-8")
        body = text[text.index("def fetch("):text.index("def _cli_status(")]
        self.assertIn("urlopen(url", body)
        self.assertEqual(text.count("urlopen"), body.count("urlopen"),
                         "urlopen appears outside fetch()")
        for name in ("urllib", "http.client", "requests", "socket"):
            self.assertNotIn(f"\nimport {name}", text)

    def test_the_interpreter_is_not_bound_as_a_signature_default(self):
        import inspect
        signature = inspect.signature(embed.Embedder.__init__)
        self.assertIsNone(signature.parameters["python"].default)
        self.assertIsNone(signature.parameters["directory"].default)

    def test_the_venv_interpreter_is_the_one_the_suite_disarmed(self):
        self.assertEqual(embed.Embedder().python(), config.VENV_PYTHON)
        self.assertFalse(config.VENV_PYTHON.exists())


if __name__ == "__main__":
    unittest.main()
