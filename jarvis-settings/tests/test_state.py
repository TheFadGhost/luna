"""Reading lunad's state, and the one rule that matters: the key is never
read, never returned, never rendered."""

import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import state, theme, voices  # noqa: E402

SECRET = "sk-or-v1-THIS-MUST-NEVER-APPEAR-ANYWHERE"


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = state.MEMORY_DIR
        state.MEMORY_DIR = pathlib.Path(self.tmp.name)
        self.addCleanup(lambda: setattr(state, "MEMORY_DIR", self.saved))
        self.addCleanup(self.tmp.cleanup)

    def test_entries_split_on_the_section_sign(self):
        (state.MEMORY_DIR / "LUNA.md").write_text(
            "§ one thing\n§ another thing\n§ a third\n")
        entries, err = state.read_entries("LUNA.md")
        self.assertEqual(err, "")
        self.assertEqual(len(entries), 3)
        self.assertTrue(entries[0].startswith("one"))

    def test_a_file_with_no_delimiter_is_one_entry(self):
        (state.MEMORY_DIR / "USER.md").write_text("plain hand-written note\n")
        entries, _ = state.read_entries("USER.md")
        self.assertEqual(len(entries), 1)

    def test_missing_file_is_reported_not_raised(self):
        entries, err = state.read_entries("LUNA.md")
        self.assertEqual(entries, [])
        self.assertIn("does not exist", err)

    def test_usage_is_capped_at_one(self):
        (state.MEMORY_DIR / "LUNA.md").write_text("x" * 9000)
        usage = {u["file"]: u for u in state.tier1_usage(
            {"memory.luna_cap_chars": 3000, "memory.user_cap_chars": 2000})}
        self.assertEqual(usage["LUNA.md"]["chars"], 9000)
        self.assertEqual(usage["LUNA.md"]["pct"], 1.0)
        self.assertFalse(usage["USER.md"]["exists"])


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = state.JOBS_DIR
        state.JOBS_DIR = pathlib.Path(self.tmp.name)
        self.addCleanup(lambda: setattr(state, "JOBS_DIR", self.saved))
        self.addCleanup(self.tmp.cleanup)

    def test_newest_first_and_survives_a_broken_job_json(self):
        for name, started in (("aaa", 100.0), ("bbb", 300.0)):
            d = state.JOBS_DIR / name
            d.mkdir()
            (d / "job.json").write_text(json.dumps(
                {"id": name, "task": "t", "started": started, "state": "done"}))
        broken = state.JOBS_DIR / "ccc"
        broken.mkdir()
        (broken / "job.json").write_text("{ not json")
        (broken / "exit").write_text("3\n")
        # No usable job.json, so recent_jobs falls back to the directory's
        # mtime for ordering. Age it so the assertion below is about the
        # ordering rule and not about which test ran first.
        os.utime(broken, (50.0, 50.0))
        rows = state.recent_jobs()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["id"], "bbb")
        self.assertEqual(rows[-1]["id"], "ccc")
        ccc = [r for r in rows if r["id"] == "ccc"][0]
        self.assertEqual(ccc["exit_code"], 3)

    def test_absent_dir_is_empty_not_an_error(self):
        state.JOBS_DIR = pathlib.Path("/nonexistent/jobs")
        self.assertEqual(state.recent_jobs(), [])


class KeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "secrets.env"
        self.saved = state.KEY_FILES
        state.KEY_FILES = (self.path,)
        self.addCleanup(lambda: setattr(state, "KEY_FILES", self.saved))
        self.addCleanup(self.tmp.cleanup)

    def test_present_but_never_the_value(self):
        self.path.write_text(f"OPENROUTER_API_KEY={SECRET}\n")
        present, where = state.key_present()
        self.assertTrue(present)
        self.assertNotIn(SECRET, where)
        self.assertNotIn(SECRET[:12], where)

    def test_absent(self):
        present, where = state.key_present()
        self.assertFalse(present)
        self.assertNotIn(SECRET, where)

    def test_empty_file_is_not_a_key(self):
        self.path.write_text("")
        self.assertFalse(state.key_present()[0])


class ThemeTest(unittest.TestCase):
    def test_palette_keys_land_in_the_css(self):
        colors = theme.read_theme()
        for key in theme.PALETTE_KEYS:
            self.assertIn(key, colors, f"{key} missing from the palette")
        css = theme.css_for(colors)
        for key in theme.PALETTE_KEYS:
            self.assertIn(colors[key], css,
                          f"{key}={colors[key]} never used in the stylesheet")

    def test_no_stray_hex_literals_outside_the_palette(self):
        import re
        colors = theme.read_theme()
        css = theme.css_for(colors)
        allowed = {v.lower() for v in colors.values() if isinstance(v, str)}
        for hexlit in re.findall(r"#[0-9a-fA-F]{6}", css):
            self.assertIn(hexlit.lower(), allowed,
                          f"{hexlit} is not a palette colour")

    def test_hyprland_border_spec_is_translated(self):
        self.assertEqual(
            theme.border_from_hypr("rgba(dfe3e6cc) rgba(6b7276cc) 45deg"),
            "alpha(#dfe3e6, 0.80)")
        self.assertIsNone(theme.border_from_hypr("nonsense"))

    def test_fallback_when_the_theme_dir_is_gone_mid_theme_set(self):
        colors = theme.read_theme("/nonexistent/theme")
        self.assertEqual(colors["background"], theme.FALLBACK["background"])


class VoicesTest(unittest.TestCase):
    def test_samples_are_discovered_and_defaults_come_first(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name)
        for v in ("flux-zzz-en", "flux-donovan-en", "flux-alexis-en"):
            (d / f"deepgram_{v}.wav").write_text("")
        got = voices.available(d)
        self.assertEqual(got[:2], ["flux-alexis-en", "flux-donovan-en"])
        self.assertIn("flux-zzz-en", got)

    def test_the_two_contract_voices_are_annotated(self):
        self.assertIn("female", voices.label_for("flux-alexis-en"))
        self.assertIn("male", voices.label_for("flux-donovan-en"))
        self.assertEqual(voices.label_for("flux-kai-en"), "flux-kai-en")

    def test_missing_sample_reports_rather_than_playing_the_wrong_voice(self):
        p = voices.Player()
        ok, detail = p.play_file(pathlib.Path("/nonexistent/x.wav"))
        self.assertFalse(ok)
        self.assertIn("no sample", detail)


if __name__ == "__main__":
    unittest.main()
