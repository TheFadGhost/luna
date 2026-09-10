"""Model-slug suggestions for `assistant.model`: codex's own cache when it
can be read, the built-in fallback otherwise, and never a hard allowlist."""

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import models  # noqa: E402


class CodexModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "models_cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, obj):
        self.path.write_text(json.dumps(obj))

    def test_missing_file_falls_back(self):
        self.assertEqual(models.codex_models(self.path),
                         models.FALLBACK_CODEX_MODELS)

    def test_malformed_json_falls_back(self):
        self.path.write_text("{not json")
        self.assertEqual(models.codex_models(self.path),
                         models.FALLBACK_CODEX_MODELS)

    def test_unexpected_shape_falls_back(self):
        self._write({"models": "not-a-list"})
        self.assertEqual(models.codex_models(self.path),
                         models.FALLBACK_CODEX_MODELS)

    def test_only_list_visibility_slugs_are_offered(self):
        self._write({"models": [
            {"slug": "gpt-5.6-luna", "visibility": "list"},
            {"slug": "gpt-5.6-sol", "visibility": "list"},
            {"slug": "codex-auto-review", "visibility": "hide"},
            {"slug": "gpt-reserve", "visibility": "hide"},
        ]})
        got = models.codex_models(self.path)
        self.assertEqual(got, ("gpt-5.6-luna", "gpt-5.6-sol"))

    def test_a_cache_with_no_list_visibility_entries_falls_back(self):
        self._write({"models": [{"slug": "codex-auto-review",
                                 "visibility": "hide"}]})
        self.assertEqual(models.codex_models(self.path),
                         models.FALLBACK_CODEX_MODELS)


class SuggestionsForTest(unittest.TestCase):
    def test_codex_reads_the_live_cache_by_default(self):
        # No path override: reads the real ~/.codex/models_cache.json if
        # present, else the fallback — either way, a non-empty tuple.
        got = models.suggestions_for("codex")
        self.assertTrue(len(got) > 0)

    def test_claude_is_the_built_in_list(self):
        self.assertEqual(models.suggestions_for("claude"),
                         models.CLAUDE_MODELS)

    def test_unknown_agent_offers_nothing(self):
        self.assertEqual(models.suggestions_for("gemini"), ())


if __name__ == "__main__":
    unittest.main()
