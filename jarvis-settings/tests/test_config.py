"""Writing: comments survive, unknown keys survive, invalid never lands,
and the file is 0600 inside a 0700 directory."""

import os
import pathlib
import stat
import sys
import tempfile
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import config, schema  # noqa: E402
from jarvis.tomledit import TomlEditError, set_values  # noqa: E402

SAMPLE = '''# Jarvis config, hand-written.
# A leading comment block that must survive.

[assistant]
name         = "Luna"        # display name + how she refers to herself
specialist   = "Sol"         # the delegate persona
agent        = "claude"      # claude | codex
model        = ""            # "" = agent default

[voice]
enabled      = true
speed        = 1.0
max_spoken_chars = 400       # longer replies are summarised

[wildly_unknown]
some_key = "keep me"
nested_number = 7
'''


class EditTest(unittest.TestCase):
    def test_comments_and_order_survive(self):
        out = set_values(SAMPLE, {"assistant.name": "Ada"})
        self.assertIn("# display name + how she refers to herself", out)
        self.assertIn('name         = "Ada"', out)
        # order preserved
        self.assertLess(out.index("[assistant]"), out.index("[voice]"))
        self.assertLess(out.index("name"), out.index("specialist"))

    def test_unknown_keys_are_untouched(self):
        out = set_values(SAMPLE, {"voice.speed": 1.25})
        self.assertIn("[wildly_unknown]", out)
        self.assertIn('some_key = "keep me"', out)
        self.assertIn("nested_number = 7", out)

    def test_float_stays_a_float(self):
        out = set_values(SAMPLE, {"voice.speed": 1.0})
        self.assertEqual(tomllib.loads(out)["voice"]["speed"], 1.0)
        self.assertIsInstance(tomllib.loads(out)["voice"]["speed"], float)

    def test_missing_key_is_appended_to_its_table(self):
        out = set_values(SAMPLE, {"voice.provider": "piper"})
        got = tomllib.loads(out)
        self.assertEqual(got["voice"]["provider"], "piper")
        self.assertLess(out.index("provider"), out.index("[wildly_unknown]"))

    def test_missing_table_is_appended(self):
        out = set_values(SAMPLE, {"confirm.prompt.channel": "both"})
        self.assertEqual(tomllib.loads(out)["confirm"]["prompt"]["channel"],
                         "both")

    def test_comment_column_is_preserved_when_it_fits(self):
        out = set_values(SAMPLE, {"assistant.agent": "codex"})
        line = [ln for ln in out.splitlines() if ln.startswith("agent")][0]
        self.assertEqual(line.index("#"),
                         [ln for ln in SAMPLE.splitlines()
                          if ln.startswith("agent")][0].index("#"))

    def test_a_multiline_value_is_refused_not_mangled(self):
        text = 'a = 1\n[voice]\nmodel = """\nx\n"""\n'
        with self.assertRaises(TomlEditError):
            set_values(text, {"voice.model": "y"})


class ValidationTest(unittest.TestCase):
    def test_out_of_range_is_refused(self):
        with self.assertRaises(config.ValidationError):
            config.coerce("voice.speed", 9.0)
        with self.assertRaises(config.ValidationError):
            config.coerce("memory.luna_cap_chars", 0)

    def test_bad_choice_is_refused(self):
        with self.assertRaises(config.ValidationError):
            config.coerce("assistant.agent", "gemini")
        with self.assertRaises(config.ValidationError):
            config.coerce("confirm.git_push", "maybe")

    def test_required_text_cannot_be_emptied(self):
        with self.assertRaises(config.ValidationError):
            config.coerce("assistant.name", "   ")
        self.assertEqual(config.coerce("assistant.model", ""), "")

    def test_readonly_is_refused(self):
        with self.assertRaises(config.ValidationError):
            config.coerce("listen.keybind", "SUPER + K")

    def test_secrets_are_refused_by_name(self):
        self.assertTrue(config.looks_secret("api_key"))
        self.assertTrue(config.looks_secret("OPENROUTER_TOKEN"))
        self.assertFalse(config.looks_secret("keybind"))

    def test_tri_accepts_all_three(self):
        for v in ("never", "ask", "deny"):
            self.assertEqual(config.coerce("confirm.delete_files", v), v)


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "cfg" / "config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_permissions(self):
        config.write_default(self.path)
        d = self.path.parent.stat().st_mode & 0o777
        f = self.path.stat().st_mode & 0o777
        self.assertEqual(oct(d), oct(0o700))
        self.assertEqual(oct(f), oct(0o600))

    def test_default_file_round_trips(self):
        config.write_default(self.path)
        values, unknown, warnings = config.load(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(unknown, {})
        for k, v in schema.defaults().items():
            with self.subTest(k):
                self.assertEqual(values[k], v)

    def test_save_preserves_a_hand_written_file(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.write_text(SAMPLE)
        self.path.chmod(0o600)
        config.save({"assistant.name": "Ada", "voice.speed": 1.15},
                    path=self.path)
        text = self.path.read_text()
        self.assertIn("# A leading comment block that must survive.", text)
        self.assertIn('some_key = "keep me"', text)
        got = tomllib.loads(text)
        self.assertEqual(got["assistant"]["name"], "Ada")
        self.assertEqual(got["voice"]["speed"], 1.15)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), oct(0o600))

    def test_invalid_never_reaches_disk(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.write_text(SAMPLE)
        before = self.path.read_text()
        with self.assertRaises(config.ValidationError):
            config.save({"assistant.name": "Ada", "voice.speed": 99.0},
                        path=self.path)
        self.assertEqual(self.path.read_text(), before)

    def test_a_broken_file_is_not_overwritten(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.write_text("this is [not toml\n")
        with self.assertRaises(TomlEditError):
            config.save({"assistant.name": "Ada"}, path=self.path)
        self.assertEqual(self.path.read_text(), "this is [not toml\n")

    def test_load_of_a_broken_file_falls_back_and_warns(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.write_text("nope = [\n")
        values, _unknown, warnings = config.load(self.path)
        self.assertTrue(warnings)
        self.assertEqual(values["assistant.name"], "Luna")

    def test_out_of_range_on_read_is_reported_not_written(self):
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.write_text("[voice]\nspeed = 12.0\n")
        values, _u, warnings = config.load(self.path)
        self.assertEqual(values["voice.speed"], 1.0)
        self.assertTrue(any("voice.speed" in w for w in warnings))
        # the file itself is left exactly as the user wrote it
        self.assertIn("12.0", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
