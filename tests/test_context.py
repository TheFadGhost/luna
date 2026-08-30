"""The focused-window line: what it says, where it rides, and how it fails.

Two properties matter here and they pull in opposite directions. The line has
to be *there* — it is always-on by the user's explicit request, and an
assistant who has to ask "which window?" about the window in front of them is
the thing this replaced. And it has to be *free to lose*: it rides on every
single ask, so a compositor that is missing, slow or lying must cost the answer
nothing at all.

So most of this file is failure. The success case is one function.

Nothing here reaches a compositor. ``config.HYPRCTL_BIN`` is a sentinel for the
whole test process (tests/_support.py), which is itself asserted in
test_guards.py; the cases that need a *particular* answer from Hyprland stub
``context._hyprctl`` and get one.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from lunad import config, context, persona

from ._support import TempMemoryCase

#: A real `hyprctl -j activewindow` from this machine, trimmed to the fields
#: that are read. Copied from the live compositor rather than invented, because
#: the shape of `at`/`size`/`workspace` is the whole reason `_geometry` exists.
LIVE = {
    "address": "0x55e02423c340",
    "mapped": True,
    "at": [10, 54],
    "size": [1900, 1016],
    "workspace": {"id": 3, "name": "3"},
    "floating": False,
    "monitor": 0,
    "class": "org.omarchy.agent",
    "title": "✳ Parsec virtual monitor setup",
    "initialClass": "org.omarchy.agent",
    "initialTitle": "foot",
    "pid": 2121792,
}


def answering(payload: object) -> object:
    """Stub `_hyprctl` with a compositor that says exactly this."""
    return mock.patch.object(context, "_hyprctl",
                             lambda *a, **k: json.dumps(payload))


class FocusedWindowCase(unittest.TestCase):
    def test_a_live_window_is_read(self) -> None:
        with answering(LIVE):
            window = context.focused_window()
        self.assertIsNotNone(window)
        self.assertEqual(window["class"], "org.omarchy.agent")  # type: ignore[index]

    def test_nothing_focused_is_not_an_error(self) -> None:
        """Hyprland answers `{}` when no window has focus.

        That is a fact about the desktop, not a fault, and it has to arrive as
        "no context line" rather than as an exception on the ask path.
        """
        with answering({}):
            self.assertIsNone(context.focused_window())
        self.assertEqual(context.describe(None), "")

    def test_output_that_is_not_json_is_survived(self) -> None:
        with mock.patch.object(context, "_hyprctl", lambda *a, **k: "not json"):
            self.assertIsNone(context.focused_window())

    def test_a_compositor_that_is_not_there_is_survived(self) -> None:
        with mock.patch.object(context, "_hyprctl", lambda *a, **k: None):
            self.assertIsNone(context.focused_window())
            self.assertEqual(context.context_line(), "")


class SlowCompositorCase(TempMemoryCase):
    """The one that would actually hurt: hyprctl that answers eventually.

    A missing binary fails in microseconds and nobody notices. A compositor
    under load that takes eight seconds to answer would add eight seconds to
    every question the user asks, for a line worth twenty tokens. The timeout
    is the whole design, so it is tested against a program that really does
    hang rather than against a mock that pretends to.
    """

    def _sleeper(self) -> str:
        path = self.root / "slow-hyprctl"
        path.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def test_a_hanging_hyprctl_is_abandoned_and_costs_the_ask_nothing(self) -> None:
        old = config.HYPRCTL_BIN
        config.HYPRCTL_BIN = self._sleeper()
        try:
            self.assertIsNone(context.focused_window(timeout=0.3))
            self.assertEqual(context.context_line(timeout=0.3), "")
        finally:
            config.HYPRCTL_BIN = old

    def test_a_hyprctl_that_fails_is_survived(self) -> None:
        path = self.root / "angry-hyprctl"
        path.write_text("#!/bin/sh\necho boom >&2\nexit 1\n", encoding="utf-8")
        path.chmod(0o700)
        old = config.HYPRCTL_BIN
        config.HYPRCTL_BIN = str(path)
        try:
            self.assertIsNone(context.focused_window())
        finally:
            config.HYPRCTL_BIN = old


class DescribeCase(unittest.TestCase):
    def test_the_line_names_the_app_id_title_and_workspace(self) -> None:
        line = context.describe(LIVE)
        self.assertIn("org.omarchy.agent", line)
        self.assertIn("Parsec virtual monitor setup", line)
        self.assertIn("workspace 3", line)

    def test_the_initial_class_is_only_worth_it_when_it_differs(self) -> None:
        self.assertNotIn("initially", context.describe(LIVE))
        renamed = {**LIVE, "initialClass": "foot"}
        self.assertIn("initially foot", context.describe(renamed))

    def test_a_long_title_is_clipped(self) -> None:
        line = context.describe({**LIVE, "title": "x" * 500})
        self.assertLess(len(line), 200)
        self.assertIn("…", line)

    def test_a_newline_in_a_title_cannot_break_the_block(self) -> None:
        line = context.describe({**LIVE, "title": "one\ntwo"})
        self.assertNotIn("\n", line)

    def test_a_window_with_no_class_describes_nothing(self) -> None:
        self.assertEqual(context.describe({"title": "orphan"}), "")

    def test_the_line_does_not_claim_she_can_read_the_window(self) -> None:
        """Knowing which window has focus is not seeing what is in it.

        Without this the title is the only evidence in the prompt and the model
        will happily narrate a screen it has never been shown, which is worse
        than having no context line at all.
        """
        with answering(LIVE):
            line = context.context_line()
        self.assertIn("not its contents", line)
        self.assertIn("look", line)


class UserMessageCase(unittest.TestCase):
    """Where the line rides. This is the load-bearing one.

    ARCHITECTURE.md §4: the system prompt is the cacheable prefix and must stay
    byte-identical between turns. The focused window changes every time the
    user alt-tabs, so in the prefix it would invalidate the cache on every
    single ask — the exact mistake tier-2 recall made, measured at $0.0513/ask
    against $0.0096 once it moved. It goes in the user message or nowhere.
    """

    def test_the_context_line_is_in_the_message(self) -> None:
        message = persona.build_user_message("what is this",
                                             context_line="Focused: foot")
        self.assertIn("Focused: foot", message)
        self.assertTrue(message.endswith("what is this"))

    def test_the_context_line_is_not_in_the_system_prompt(self) -> None:
        prompt = persona.build_system_prompt("tier one", spec="SPEC",
                                             name="Luna")
        self.assertNotIn("Focused", prompt)

    def test_no_context_adds_nothing_at_all(self) -> None:
        self.assertEqual(persona.build_user_message("bare"), "bare")

    def test_recall_comes_before_the_desktop_and_both_before_the_question(self) -> None:
        message = persona.build_user_message("the question", "RECALLED",
                                             context_line="DESKTOP")
        self.assertLess(message.index("RECALLED"), message.index("DESKTOP"))
        self.assertLess(message.index("DESKTOP"), message.index("the question"))


class GeometryCase(unittest.TestCase):
    """`grim -g` wants "<x>,<y> <w>x<h>"; Hyprland gives two lists."""

    def test_a_live_window_converts(self) -> None:
        self.assertEqual(context._geometry(LIVE), "10,54 1900x1016")

    def test_a_window_that_did_not_say_falls_back_to_none(self) -> None:
        for bad in ({}, {"at": [1, 2]}, {"at": [1, 2], "size": [0, 0]},
                    {"at": "10,54", "size": [1, 1]},
                    {"at": [1, 2], "size": ["a", "b"]}):
            with self.subTest(window=bad):
                self.assertIsNone(context._geometry(bad))


class CaptureCase(TempMemoryCase):
    """The screenshot: taken on request only, and never left behind."""

    def fake_grim(self, script: str = 'printf PNG > "$LAST"') -> str:
        """A grim that writes to the file it was given, and records its argv.

        ``$LAST`` because the output path is grim's trailing positional and its
        index moves with ``-g``; hard-coding ``$2`` would make the window case
        and the screen case need different scripts for no reason.
        """
        path = self.root / "fake-grim"
        self.argv_log = self.root / "grim-argv"
        path.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {self.argv_log}\n'
            'for LAST; do :; done\n'
            f"{script}\n",
            encoding="utf-8")
        path.chmod(0o700)
        old = config.GRIM_BIN
        config.GRIM_BIN = str(path)
        self.addCleanup(setattr, config, "GRIM_BIN", old)
        return str(path)

    def test_a_window_look_asks_for_the_window_geometry(self) -> None:
        self.fake_grim()
        with answering(LIVE):
            with context.look("window") as shot:
                self.assertTrue(shot.is_file())
        self.assertIn("-g 10,54 1900x1016", self.argv_log.read_text())

    def test_a_screen_look_asks_for_no_geometry(self) -> None:
        self.fake_grim()
        with context.look("screen") as shot:
            self.assertTrue(shot.is_file())
        self.assertNotIn("-g", self.argv_log.read_text())

    def test_a_window_with_no_geometry_falls_back_to_the_whole_screen(self) -> None:
        # A picture of everything answers the question; no picture does not.
        self.fake_grim()
        with answering({"class": "app", "title": "t"}):
            with context.look("window") as shot:
                self.assertTrue(shot.is_file())
        self.assertNotIn("-g", self.argv_log.read_text())

    def test_the_file_is_deleted_when_the_block_ends(self) -> None:
        self.fake_grim()
        with context.look("screen") as shot:
            path = Path(shot)
            self.assertTrue(path.is_file())
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_the_file_is_deleted_even_when_the_call_raises(self) -> None:
        """The whole reason this is a context manager.

        An agent call that blew up mid-turn would otherwise leave a photograph
        of the user's screen sitting in /tmp with nobody left to remove it.
        """
        self.fake_grim()
        seen: list[Path] = []
        with self.assertRaises(ZeroDivisionError):
            with context.look("screen") as shot:
                seen.append(Path(shot))
                raise ZeroDivisionError("the agent fell over")
        self.assertFalse(seen[0].exists())
        self.assertFalse(seen[0].parent.exists())

    def test_a_grim_that_fails_says_so(self) -> None:
        self.fake_grim("echo 'no wayland display' >&2; exit 1")
        with self.assertRaises(context.LookUnavailable) as caught:
            with context.look("screen"):
                self.fail("should not have got a picture")
        self.assertIn("no wayland display", str(caught.exception))

    def test_a_grim_that_writes_nothing_is_not_a_screenshot(self) -> None:
        # exit 0 and an empty file. Passing that to `codex exec -i` would waste
        # a turn and get an answer about nothing.
        self.fake_grim(': > "$LAST"')
        with self.assertRaises(context.LookUnavailable):
            with context.look("screen"):
                self.fail("should not have got a picture")

    def test_an_unknown_scope_is_refused_before_anything_is_captured(self) -> None:
        self.fake_grim()
        with self.assertRaises(context.LookUnavailable):
            with context.look("everything"):
                self.fail("should not have got a picture")
        self.assertFalse(self.argv_log.exists())

    def test_a_missing_grim_names_the_fix(self) -> None:
        # The process-wide sentinel, i.e. what every other test in the suite
        # sees. It has to fail, and it has to say what to install.
        with self.assertRaises(context.LookUnavailable) as caught:
            with context.look("screen"):
                self.fail("should not have got a picture")
        self.assertIn("grim", str(caught.exception).lower())

    def test_the_screenshot_never_lands_in_a_directory_luna_keeps(self) -> None:
        """A throwaway directory, not the state tree.

        Anything under ~/.local/share/luna is backed up, collected and read by
        other parts of the daemon; a photograph of the user's screen has no
        business in any of them, and one written there survives until something
        else decides to delete it.
        """
        self.fake_grim()
        with context.look("screen") as shot:
            self.assertFalse(str(shot).startswith(str(config.STATE_DIR)))
            self.assertFalse(str(shot).startswith(str(config.CONFIG_DIR)))
            self.assertTrue(Path(shot).parent.name.startswith("luna-look-"))


if __name__ == "__main__":
    unittest.main()
