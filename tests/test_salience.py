"""Salience scoring and read-time decay."""

from __future__ import annotations

import unittest

from ._support import TempMemoryCase

from lunad import config
from lunad.memory import (
    CONSEQUENCE_SATURATES_AT,
    REPETITION_SATURATES_AT,
    W_CONSEQUENCE,
    W_RECENCY,
    W_REPETITION,
    decayed_salience,
    score_salience,
)

DAY = 86400.0


class WeightTests(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(W_REPETITION + W_CONSEQUENCE + W_RECENCY, 1.0)


class ScoreTests(unittest.TestCase):
    def test_is_pure_and_deterministic(self):
        args = ("always use foot, not alacritty", "noted", 2)
        self.assertEqual(score_salience(*args), score_salience(*args))

    def test_bounded_zero_to_one(self):
        cases = [
            ("", "", 0),
            ("hello", "", 0),
            ("always never remember from now on i prefer must", "broke crashed", 99),
            ("a" * 5000, "b" * 5000, 1000),
        ]
        for user, luna, reps in cases:
            with self.subTest(user=user[:20]):
                score = score_salience(user, luna, reps)
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 1.0)

    def test_bland_exchange_scores_the_recency_baseline(self):
        # Nothing repeated, nothing consequential: only the recency weight.
        self.assertAlmostEqual(score_salience("what time is it", "half four"),
                               W_RECENCY, places=4)

    def test_explicit_correction_pins_to_one(self):
        for text in [
            "no, that's wrong",
            "No. the socket is under XDG_RUNTIME_DIR",
            "actually it's foot, not alacritty",
            "I said use the medium voice",
            "that's incorrect",
            "instead of waybar, use omarchy-shell",
            "correction: the cap is 3000 not 2000",
        ]:
            with self.subTest(text=text):
                self.assertEqual(score_salience(text), config.CORRECTION_SALIENCE)

    def test_correction_beats_every_other_signal(self):
        plain = score_salience("always use foot", "ok", REPETITION_SATURATES_AT)
        self.assertLess(plain, 1.0)
        self.assertEqual(score_salience("no, always use foot"), 1.0)

    def test_bare_actually_and_wrong_are_not_corrections(self):
        # The two false positives an audit found against the real database:
        # bare `\bactually\b` and `\b(wrong|incorrect)\b` pinned any memory
        # containing either word at salience 1.0 forever, with no decay,
        # whether or not the sentence had anything to do with Luna being
        # wrong about something. Neither of these is aimed at her at all.
        for text in [
            "actually I like this",
            "my code is wrong",
            "actually, I think we should ship this on Friday",
            "actually, that reminds me, I need more coffee",
            "I think my approach here is wrong somewhere but I can't find it",
        ]:
            with self.subTest(text=text):
                self.assertLess(score_salience(text), config.CORRECTION_SALIENCE)

    def test_second_person_actually_and_wrong_still_pin_to_one(self):
        # Tightening the patterns must not lose the real corrections they
        # were written to catch -- just the ones aimed at nothing.
        for text in [
            "actually you're wrong about that",
            "you got that wrong",
            "your answer was incorrect",
            "no actually, you misread the config",
        ]:
            with self.subTest(text=text):
                self.assertEqual(score_salience(text), config.CORRECTION_SALIENCE)

    def test_correction_is_detected_on_the_user_side_only(self):
        # Luna saying "that's wrong" about her own output is not a correction
        # *from the user*, and must not pin the memory forever.
        self.assertLess(score_salience("run the tests", "that's wrong of me"), 1.0)

    def test_repetition_raises_the_score_and_saturates(self):
        scores = [score_salience("check the battery", "", n) for n in range(0, 7)]
        self.assertTrue(all(b >= a for a, b in zip(scores, scores[1:])),
                        f"not monotonic: {scores}")
        self.assertLess(scores[0], scores[REPETITION_SATURATES_AT])
        self.assertEqual(scores[REPETITION_SATURATES_AT], scores[6])
        self.assertAlmostEqual(
            scores[REPETITION_SATURATES_AT] - scores[0], W_REPETITION, places=4)

    def test_negative_repetitions_are_clamped(self):
        self.assertEqual(score_salience("x", "", -5), score_salience("x", "", 0))

    def test_consequence_markers_raise_the_score_and_saturate(self):
        base = score_salience("move the file", "done")
        one = score_salience("always move the file", "done")
        many = score_salience(
            "always move the file, from now on, never ask, i prefer it", "done")
        self.assertLess(base, one)
        self.assertLess(one, many)
        self.assertAlmostEqual(many - base, W_CONSEQUENCE, places=4)
        self.assertGreaterEqual(CONSEQUENCE_SATURATES_AT, 1)

    def test_consequence_is_read_from_both_sides(self):
        self.assertGreater(score_salience("tidy up", "I deleted the stale worktree"),
                           score_salience("tidy up", "I had a look"))


class DecayTests(TempMemoryCase):
    """Decay now reads `[memory] decay_half_life_days` when no half-life is
    passed, so these run against a redirected settings file. A plain TestCase
    here would read -- and, on a machine that had never opened Jarvis, create
    -- the user's real ~/.config/jarvis/config.toml, and would pass or fail on
    whatever they last changed in the GUI."""

    def test_no_decay_at_zero_age(self):
        self.assertAlmostEqual(decayed_salience(0.8, 0.0), 0.8, places=6)

    def test_halves_over_one_half_life(self):
        hl = self.settings.get("memory.decay_half_life_days")
        self.assertAlmostEqual(decayed_salience(0.8, hl * DAY), 0.4, places=4)
        self.assertAlmostEqual(decayed_salience(0.8, 2 * hl * DAY), 0.2, places=4)

    def test_monotonically_decreasing(self):
        vals = [decayed_salience(0.9, d * DAY) for d in range(0, 90, 5)]
        self.assertTrue(all(b <= a for a, b in zip(vals, vals[1:])))

    def test_corrections_never_decay(self):
        for age_days in (0, 30, 365, 3650):
            with self.subTest(age_days=age_days):
                self.assertEqual(
                    decayed_salience(config.CORRECTION_SALIENCE, age_days * DAY),
                    config.CORRECTION_SALIENCE,
                )

    def test_negative_age_does_not_amplify(self):
        self.assertAlmostEqual(decayed_salience(0.5, -99 * DAY), 0.5, places=6)

    def test_zero_half_life_disables_decay_rather_than_dividing_by_zero(self):
        self.assertEqual(decayed_salience(0.5, 10 * DAY, half_life_days=0), 0.5)

    def test_decay_does_not_mutate_its_input(self):
        salience = 0.6
        decayed_salience(salience, 100 * DAY)
        self.assertEqual(salience, 0.6)
