"""The config file: defaults, round-trip, validation, hot reload, secrets.

The schema in ``lunad/settings.py`` and the table in ``docs/CONFIG-SCHEMA.md``
are two copies of one contract, and a settings GUI is being written against the
document while lunad is written against the module. So one of these tests reads
the document and asserts they still agree — the drift that test catches is
exactly the drift that would show up as a GUI writing a key the daemon ignores.
"""

from __future__ import annotations

import os
import re
import time
import tomllib
import unittest
from pathlib import Path

from ._support import TempMemoryCase

from lunad import config, settings as settings_mod


DOC = Path(__file__).resolve().parent.parent / "docs" / "CONFIG-SCHEMA.md"


class DefaultsCase(TempMemoryCase):
    def test_missing_file_is_created_with_defaults(self) -> None:
        path = self.root / "fresh" / "config.toml"
        cfg = settings_mod.Settings(path)
        self.addCleanup(cfg.stop_watching)
        self.assertTrue(path.exists())
        self.assertEqual(cfg.get("assistant.name"), "Luna")
        self.assertEqual(cfg.get("voice.voice"), "flux-alexis-en")
        self.assertEqual(cfg.get("voice.voice_male"), "flux-donovan-en")
        self.assertEqual(cfg.get("voice.model"), "deepgram/flux-tts:free")
        self.assertEqual(cfg.get("confirm.delete_files"), "ask")
        self.assertEqual(cfg.get("confirm.prompt.channel"), "notification")

    def test_new_file_is_0600_in_a_0700_directory(self) -> None:
        path = self.root / "perms" / "config.toml"
        cfg = settings_mod.Settings(path)
        self.addCleanup(cfg.stop_watching)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_defaults_are_not_shared_between_callers(self) -> None:
        first = settings_mod.defaults()
        first["voice"]["voice"] = "mutated"
        self.assertEqual(settings_mod.defaults()["voice"]["voice"],
                         "flux-alexis-en")


class RoundTripCase(TempMemoryCase):
    def test_write_then_read_is_lossless(self) -> None:
        cfg = self.settings
        cfg.set("assistant.name", "Jarvis")
        cfg.set("voice.voice", "flux-donovan-en")
        cfg.set("voice.speed", 1.25)
        cfg.set("voice.max_spoken_chars", 900)
        cfg.set("confirm.spend_threshold", 2.5)
        cfg.set("ui.notify_on_finish", False)

        again = settings_mod.Settings(cfg.path)
        self.addCleanup(again.stop_watching)
        self.assertEqual(again.data, cfg.data)
        self.assertEqual(again.get("assistant.name"), "Jarvis")
        self.assertEqual(again.get("voice.speed"), 1.25)
        self.assertIs(again.get("ui.notify_on_finish"), False)
        self.assertEqual(again.problems, [])

    def test_the_written_file_is_valid_toml_and_keeps_the_comments(self) -> None:
        text = self.settings.path.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["voice"]["voice"], "flux-alexis-en")
        self.assertIn("# display name + how she refers to herself", text)
        self.assertIn("# openrouter | piper", text)
        self.assertIn("#   restarting omarchy-shell", text)
        self.assertIn("# The safety model.", text)

    def test_floats_keep_their_decimal_point(self) -> None:
        self.settings.set("voice.speed", 1)
        text = self.settings.path.read_text(encoding="utf-8")
        self.assertIn("speed        = 1.0", text)
        self.assertIsInstance(tomllib.loads(text)["voice"]["speed"], float)

    def test_a_quote_in_a_value_survives(self) -> None:
        self.settings.set("assistant.name", 'She "Who" Waits')
        again = settings_mod.Settings(self.settings.path)
        self.addCleanup(again.stop_watching)
        self.assertEqual(again.get("assistant.name"), 'She "Who" Waits')


class ValidationCase(TempMemoryCase):
    def _write(self, body: str) -> settings_mod.Settings:
        self.settings.path.write_text(body, encoding="utf-8")
        cfg = settings_mod.Settings(self.settings.path)
        self.addCleanup(cfg.stop_watching)
        return cfg

    def test_a_bad_enum_falls_back_and_warns(self) -> None:
        cfg = self._write('[voice]\nprovider = "carrier pigeon"\n')
        self.assertEqual(cfg.get("voice.provider"), "openrouter")
        self.assertTrue(any("voice.provider" in p for p in cfg.problems))

    def test_a_bad_policy_falls_back(self) -> None:
        cfg = self._write('[confirm]\ndelete_files = "maybe"\n')
        self.assertEqual(cfg.get("confirm.delete_files"), "ask")
        self.assertTrue(any("delete_files" in p for p in cfg.problems))

    def test_out_of_range_numbers_fall_back(self) -> None:
        cfg = self._write("[voice]\nspeed = 99.0\nmax_spoken_chars = 0\n")
        self.assertEqual(cfg.get("voice.speed"), 1.0)
        self.assertEqual(cfg.get("voice.max_spoken_chars"), 400)
        self.assertEqual(len(cfg.problems), 2)

    def test_a_bool_is_not_accepted_as_a_number(self) -> None:
        cfg = self._write("[voice]\nspeed = true\n")
        self.assertEqual(cfg.get("voice.speed"), 1.0)

    def test_an_int_widens_into_a_float_key(self) -> None:
        cfg = self._write("[voice]\nspeed = 2\n")
        self.assertEqual(cfg.get("voice.speed"), 2.0)
        self.assertIsInstance(cfg.get("voice.speed"), float)

    def test_unknown_keys_and_sections_are_reported_not_kept(self) -> None:
        cfg = self._write('[voice]\nvolume = 3\n\n[weather]\ncity = "Leeds"\n')
        self.assertNotIn("weather", cfg.data)
        self.assertEqual(len(cfg.problems), 2)

    def test_broken_toml_does_not_crash_and_keeps_what_was_loaded(self) -> None:
        cfg = self.settings
        cfg.set("assistant.name", "Jarvis")
        cfg.path.write_text('[voice]\nvoice = "unterminated\n', encoding="utf-8")
        cfg.reload()
        self.assertEqual(cfg.get("assistant.name"), "Jarvis")

    def test_set_refuses_an_invalid_value_instead_of_falling_back(self) -> None:
        with self.assertRaises(settings_mod.SettingsError):
            self.settings.set("voice.provider", "carrier pigeon")
        with self.assertRaises(settings_mod.SettingsError):
            self.settings.set("voice.nonexistent", 1)
        self.assertEqual(self.settings.get("voice.provider"), "openrouter")

    def test_the_confirm_prompt_subtable_is_not_an_unknown_key(self) -> None:
        cfg = self._write('[confirm]\ngit_push = "never"\n\n'
                          '[confirm.prompt]\nchannel = "both"\n')
        self.assertEqual(cfg.problems, [])
        self.assertEqual(cfg.get("confirm.git_push"), "never")
        self.assertEqual(cfg.get("confirm.prompt.channel"), "both")


class HotReloadCase(TempMemoryCase):
    def test_an_edit_on_disk_reaches_get_without_a_restart(self) -> None:
        cfg = self.settings
        self.assertEqual(cfg.get("voice.voice"), "flux-alexis-en")
        cfg.path.write_text('[voice]\nvoice = "flux-donovan-en"\n',
                            encoding="utf-8")
        changes = cfg.poll()
        self.assertEqual(cfg.get("voice.voice"), "flux-donovan-en")
        self.assertIn({"key": "voice.voice", "from": "flux-alexis-en",
                       "to": "flux-donovan-en"}, changes)

    def test_poll_is_a_no_op_when_the_file_has_not_moved(self) -> None:
        self.assertEqual(self.settings.poll(), [])
        self.assertEqual(self.settings.reloads, 0)

    def test_listeners_see_the_diff(self) -> None:
        seen: list[list[dict]] = []
        self.settings.on_change(seen.append)
        self.settings.path.write_text('[assistant]\nname = "Jarvis"\n',
                                      encoding="utf-8")
        self.settings.poll()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0]["key"], "assistant.name")
        self.assertEqual(seen[0][0]["to"], "Jarvis")

    def test_a_same_size_same_second_edit_is_still_noticed(self) -> None:
        """mtime alone is not enough; a one-character toggle proves it."""
        cfg = self.settings
        cfg.path.write_text('[voice]\nvoice = "flux-alexis-en"\n',
                            encoding="utf-8")
        cfg.reload()
        cfg.path.write_text('[voice]\nvoice = "flux-donovan-eN"\n',
                            encoding="utf-8")
        os.utime(cfg.path, (0, 0))
        self.assertTrue(cfg.changed_on_disk())

    def test_the_watcher_thread_picks_a_change_up(self) -> None:
        cfg = self.settings
        cfg.start_watching(interval=0.05)
        self.addCleanup(cfg.stop_watching)
        cfg.path.write_text('[assistant]\nname = "Watched"\n', encoding="utf-8")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cfg.get("assistant.name") == "Watched":
                break
            time.sleep(0.02)
        self.assertEqual(cfg.get("assistant.name"), "Watched")
        self.assertTrue(cfg.status()["watching"])

    def test_diff_lists_only_what_moved(self) -> None:
        before = settings_mod.defaults()
        after = settings_mod.defaults()
        after["confirm"]["git_push"] = "never"
        self.assertEqual(settings_mod.diff(before, after),
                         [{"key": "confirm.git_push", "from": "ask",
                           "to": "never"}])


class NameCase(TempMemoryCase):
    def test_assistant_name_comes_from_settings(self) -> None:
        self.assertEqual(settings_mod.assistant_name(), "Luna")
        self.settings.set("assistant.name", "Jarvis")
        self.assertEqual(settings_mod.assistant_name(), "Jarvis")

    def test_a_blank_name_falls_back_to_the_default(self) -> None:
        self.settings.set("assistant.name", "   ")
        self.assertEqual(settings_mod.assistant_name(), "Luna")

    def test_specialist_name_comes_from_settings(self) -> None:
        self.settings.set("assistant.specialist", "Atlas")
        self.assertEqual(settings_mod.specialist_name(), "Atlas")


class SecretsCase(TempMemoryCase):
    def test_env_file_parsing_handles_quotes_and_export(self) -> None:
        path = self.root / "secrets.env"
        path.write_text('# a comment\nexport A="one"\nB=two\nC=\n\nD\n',
                        encoding="utf-8")
        self.assertEqual(settings_mod.read_env_file(path),
                         {"A": "one", "B": "two", "C": ""})

    def test_a_missing_env_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(settings_mod.read_env_file(self.root / "nope"), {})

    def test_api_key_prefers_the_environment(self) -> None:
        old = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "sk-from-env"
        self.addCleanup(_restore_env, "OPENROUTER_API_KEY", old)
        self.assertEqual(settings_mod.api_key(), "sk-from-env")
        self.assertEqual(settings_mod.secrets_status()["source"],
                         "$OPENROUTER_API_KEY")

    def test_secrets_status_never_contains_the_key(self) -> None:
        old = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "sk-secret-value"
        self.addCleanup(_restore_env, "OPENROUTER_API_KEY", old)
        self.assertNotIn("sk-secret-value",
                         repr(settings_mod.secrets_status()))


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


class ContractCase(unittest.TestCase):
    """docs/CONFIG-SCHEMA.md is the contract; the module must match it."""

    def setUp(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        block = re.search(r"```toml\n(.*?)```", text, re.DOTALL)
        self.assertIsNotNone(block, "CONFIG-SCHEMA.md has no toml block")
        self.doc = tomllib.loads(block.group(1))  # type: ignore[union-attr]

    def test_every_documented_key_exists_with_the_documented_default(self) -> None:
        shipped = settings_mod.defaults()
        for section, values in self.doc.items():
            self.assertIn(section, shipped, f"[{section}] is not implemented")
            for key, value in values.items():
                if isinstance(value, dict):
                    continue                     # a sub-table, checked on its own
                self.assertIn(key, shipped[section],
                              f"{section}.{key} is not implemented")
                self.assertEqual(shipped[section][key], value,
                                 f"{section}.{key} default has drifted")

    def test_the_module_adds_no_keys_the_document_does_not_have(self) -> None:
        flat_doc = {f"{s}.{k}" for s, v in self.doc.items()
                    for k in v if not isinstance(v[k], dict)}
        flat_doc |= {f"{s}.{sub}.{k}" for s, v in self.doc.items()
                     for sub, inner in v.items() if isinstance(inner, dict)
                     for k in inner}
        flat_mod = {f"{sec.name}.{k.name}"
                    for sec in settings_mod.SCHEMA for k in sec.keys}
        self.assertEqual(flat_mod - flat_doc, set())
        self.assertEqual(flat_doc - flat_mod, set())

    def test_the_hard_denies_are_written_down_in_the_document(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for phrase in ("signalling a process Jarvis did not spawn",
                       "restarting omarchy-shell",
                       "deleting ~/.config/omarchy/CUSTOMISATIONS.md",
                       "rm -rf outside Jarvis's own directories"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
