"""`[ui] theme_follows_omarchy` — the one config key that is this app's own.

lunad hot-reloads its half of the contract through the config file, but
nothing in that path can restyle a GTK window, so this key is honoured here or
nowhere. It was in the schema, in the GUI and in the written contract while
being read by neither process.
"""

import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import theme  # noqa: E402

COLORS = '''
background = "#112233"
foreground = "#ddeeff"
accent = "#445566"
'''


class ReadThemeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-theme-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        with open(os.path.join(self.dir, "colors.toml"), "w") as fh:
            fh.write(COLORS)

    def test_following_reads_the_omarchy_palette(self):
        colors = theme.read_theme(self.dir, follow=True)
        self.assertEqual(colors["background"], "#112233")
        self.assertEqual(colors["foreground"], "#ddeeff")

    def test_not_following_pins_the_built_in_palette(self):
        colors = theme.read_theme(self.dir, follow=False)
        self.assertEqual(colors, dict(theme.FALLBACK))
        self.assertNotEqual(colors["background"], "#112233")

    def test_the_default_is_to_follow(self):
        # Nobody who never opens the pane should lose the desktop's theme.
        self.assertEqual(theme.read_theme(self.dir),
                         theme.read_theme(self.dir, follow=True))


class ThemeWatchCase(unittest.TestCase):
    """The switch is asked for per re-theme, not captured once."""

    def test_the_predicate_is_a_callable_read_every_time(self):
        answers = [True, False]
        watch = theme.ThemeWatch(follows=lambda: answers.pop(0))
        self.assertTrue(watch.following())
        self.assertFalse(watch.following())

    def test_a_predicate_that_raises_falls_back_to_following(self):
        # The themer runs before the settings editor exists. A window with no
        # colours at all is worse than one that followed the desktop.
        def boom():
            raise RuntimeError("no editor yet")

        self.assertTrue(theme.ThemeWatch(follows=boom).following())

    def test_the_default_watch_follows(self):
        self.assertTrue(theme.ThemeWatch().following())


if __name__ == "__main__":
    unittest.main()
