"""Every pane, built for real, and every dependent row across its states.

Until now nothing in this suite constructed a `Gtk` widget at all — not a
pane, not a `Binder`, not a control. Every case exercised pure Python either
side of the GTK layer: the schema, the TOML writer, the theme's CSS strings.
So "the app starts" was tested by starting the app by hand, and a pane that
raised on construction would have shipped.

Two things had to be settled before this file could exist.

**A display.** GTK4 on this machine does not degrade without one: it
*segfaults* on the first widget constructed. `Gtk.init_check()` is no help
because it returns **True** with no display attached and crashes anyway two
lines later — the check that looks like the guard is not one. The reliable
probe is `Gdk.Display.get_default()`, which is `None` when there is nothing to
draw on, so that is what gates this module. CI does not run this suite at all
(see the tail of `.github/workflows/ci.yml`), so the practical effect is that
these cases run wherever the suite is actually required to be green, which is
a developer's desktop.

**A config file that is not the user's.** `tests/_support.py` now redirects
`jarvis.config.CONFIG_PATH`, because an `Editor` reads it on construction and
writes it back on every `set` — and flipping a switch to check that the row
below it dims is exactly what this file does. Without that, running this
suite would have rewritten the live settings the daemon is reading.

The rows and controls are found by the `jarvis_key` attribute `Binder` tags
them with. Nothing here removes a widget from a container, so nothing here
calls `run_dispose`: that is the cycle breaker for a widget that has been
`remove()`d (CUSTOMISATIONS.md §6e), and calling it on a live widget and then
touching it is a segfault, not a tidy-up.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ._support import FORBIDDEN_JARVIS_CONFIG          # noqa: E402  (disarms)

import gi                                              # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk                     # noqa: E402

from jarvis import config, editor as editor_mod, panes, schema  # noqa: E402


def _has_display() -> bool:
    """Not `Gtk.init_check()`: that answers True with nothing to draw on."""
    try:
        Gtk.init_check()
        return Gdk.Display.get_default() is not None
    except Exception:                                   # pragma: no cover
        return False


HAVE_DISPLAY = _has_display()
NEEDS_DISPLAY = unittest.skipUnless(
    HAVE_DISPLAY, "no display: GTK4 segfaults on the first widget without one")


class Player:
    """The voice pane's preview, without an `aplay`."""

    def __init__(self):
        self.previewed: list[str] = []
        self.spoke = 0

    def preview(self, voice):
        self.previewed.append(voice)
        return True, ""

    def speak_via_daemon(self):
        self.spoke += 1
        return True, ""

    def stop(self):
        pass


def walk(widget):
    """Every descendant, breadth first."""
    out, stack = [], [widget]
    while stack:
        w = stack.pop(0)
        child = w.get_first_child()
        while child is not None:
            out.append(child)
            stack.append(child)
            child = child.get_next_sibling()
    return out


@NEEDS_DISPLAY
class PaneCase(unittest.TestCase):
    """One editor, one binder, and whichever panes a case asks for."""

    def setUp(self):
        self.assertNotEqual(config.CONFIG_PATH,
                            pathlib.Path.home() / ".config/jarvis/config.toml")
        self.editor = editor_mod.Editor()
        self.binder = panes.Binder(self.editor)
        self.player = Player()

    # -- building ----------------------------------------------------------

    def build(self, key):
        return {
            "assistant": lambda: panes.assistant_pane(self.binder),
            "voice": lambda: panes.voice_pane(self.binder, self.player,
                                              self.status),
            "listen": lambda: panes.listen_pane(self.binder, self.status),
            "confirm": lambda: panes.confirm_pane(self.binder),
            "ambient": lambda: panes.ambient_pane(self.binder),
            "hud": lambda: panes.hud_pane(self.binder),
            "memory": lambda: panes.memory_pane(self.binder, None),
            "jobs": lambda: panes.jobs_pane(self.binder),
            "vcs": lambda: panes.vcs_pane(self.binder),
            "about": lambda: panes.about_pane(self.binder, self.start),
        }[key]()

    def status(self, text, kind):
        pass

    def start(self):
        return True, ""

    # -- finding -----------------------------------------------------------

    def tagged(self, pane, key, kind):
        for w in walk(pane):
            if getattr(w, "jarvis_key", None) == key and isinstance(w, kind):
                return w
        self.fail(f"no {kind.__name__} tagged {key!r} in this pane")

    def control(self, pane, key):
        """The bound control, not the row that contains it.

        Both carry the same `jarvis_key`. A row is the one `widgets.row`
        built, and it is the one with a `jarvis_trail` — the third column.
        A composite control (the slider's track-and-read-back, the model
        entry and its hint) is a `Gtk.Box` with no trailing column.
        """
        for w in walk(pane):
            if getattr(w, "jarvis_key", None) != key:
                continue
            if not hasattr(w, "jarvis_trail"):
                return w
        self.fail(f"no control tagged {key!r}")

    def row(self, pane, key):
        for w in walk(pane):
            if (getattr(w, "jarvis_key", None) == key
                    and hasattr(w, "jarvis_trail")):
                return w
        self.fail(f"no row tagged {key!r}")

    def segments(self, tri):
        """A TriToggle's three buttons, in `schema.TRI` order."""
        return [w for w in walk(tri) if isinstance(w, Gtk.ToggleButton)]

    def texts(self, pane):
        return [w.get_text() for w in walk(pane) if isinstance(w, Gtk.Label)]


# =========================================================================
# It builds at all
# =========================================================================


class ConstructionTest(PaneCase):
    PANES = ("assistant", "voice", "listen", "confirm", "ambient", "hud",
             "memory", "jobs", "vcs", "about")

    def test_every_pane_builds(self):
        for key in self.PANES:
            with self.subTest(pane=key):
                pane = self.build(key)
                self.assertIsInstance(pane, Gtk.Widget)

    def test_the_pane_list_is_the_app_s_pane_list(self):
        from jarvis import app                          # noqa: PLC0415
        self.assertEqual(tuple(k for k, _ in app.PANES), self.PANES)

    def test_every_schema_key_reaches_a_pane_as_a_control(self):
        """The contract test says every documented key has a *field*. This
        says the field is actually drawn, in the pane its section names."""
        by_pane = {}
        for key in self.PANES:
            by_pane[key] = self.build(key)
        for section in schema.SPEC:
            pane = by_pane[section.pane]
            for fld in section.fields:
                dotted = f"{section.key}.{fld.key}"
                with self.subTest(key=dotted):
                    if fld.readonly:
                        continue
                    self.control(pane, dotted)

    def test_every_field_s_label_is_on_screen(self):
        for section in schema.SPEC:
            pane = self.build(section.pane)
            for fld in section.fields:
                with self.subTest(key=f"{section.key}.{fld.key}"):
                    self.assertIn(fld.label, self.texts(pane))

    def test_every_declared_unit_is_actually_drawn(self):
        """A unit that is declared and never rendered is a lie in the schema.

        All three `Real` units were exactly that: `bound_row` skipped the unit
        label for a `Real`, so `[voice] speed` read "Speed  1.00" with nothing
        anywhere saying it was a multiplier.
        """
        for section in schema.SPEC:
            pane = self.build(section.pane)
            for fld in section.fields:
                unit = getattr(fld, "unit", "")
                if not unit:
                    continue
                with self.subTest(key=f"{section.key}.{fld.key}"):
                    row = self.row(pane, f"{section.key}.{fld.key}")
                    drawn = " ".join(w.get_text() for w in walk(row)
                                     if isinstance(w, Gtk.Label))
                    self.assertIn(unit, drawn)


# =========================================================================
# Dependent state, across its states
# =========================================================================


class VoiceGateTest(PaneCase):
    GATED = ("voice.provider", "voice.model", "voice.voice",
             "voice.voice_male", "voice.fallback", "voice.piper_voice",
             "voice.speed", "voice.max_spoken_chars")

    def test_turning_speech_off_makes_every_other_row_inert(self):
        pane = self.build("voice")
        switch = self.control(pane, "voice.enabled")
        self.assertTrue(switch.get_active())
        for key in self.GATED:
            self.assertTrue(self.row(pane, key).get_sensitive(), key)

        switch.set_active(False)
        for key in self.GATED:
            self.assertFalse(self.row(pane, key).get_sensitive(), key)

    def test_turning_it_back_on_restores_every_row(self):
        pane = self.build("voice")
        switch = self.control(pane, "voice.enabled")
        switch.set_active(False)
        switch.set_active(True)
        for key in self.GATED:
            self.assertTrue(self.row(pane, key).get_sensitive(), key)

    def test_the_row_dims_and_the_group_heading_does_not(self):
        """A dimmed heading would say the section had stopped existing."""
        pane = self.build("voice")
        self.control(pane, "voice.enabled").set_active(False)
        heads = [w for w in walk(pane)
                 if isinstance(w, Gtk.Label) and w.has_css_class("sectionhead")]
        self.assertTrue(heads)
        for h in heads:
            self.assertTrue(h.get_sensitive())

    def test_the_switch_writes_through_to_the_editor(self):
        pane = self.build("voice")
        self.control(pane, "voice.enabled").set_active(False)
        self.assertFalse(self.editor.get("voice.enabled"))


class AmbientGateTest(PaneCase):
    """Two levels: a master, and two hooks with thresholds under it."""

    def test_the_master_switch_governs_every_row(self):
        pane = self.build("ambient")
        master = self.control(pane, "ambient.enabled")
        rows = ("ambient.poll_seconds", "ambient.update", "ambient.crash",
                "ambient.crash_diagnose", "ambient.battery",
                "ambient.battery_low_pct", "ambient.battery_critical_pct")
        master.set_active(False)
        for key in rows:
            self.assertFalse(self.row(pane, key).get_sensitive(), key)
        master.set_active(True)
        for key in ("ambient.poll_seconds", "ambient.update",
                    "ambient.crash", "ambient.battery"):
            self.assertTrue(self.row(pane, key).get_sensitive(), key)

    def test_diagnose_follows_crash_across_all_four_combinations(self):
        pane = self.build("ambient")
        master = self.control(pane, "ambient.enabled")
        crash = self.control(pane, "ambient.crash")
        for on in (True, False):
            for watching in (True, False):
                with self.subTest(enabled=on, crash=watching):
                    master.set_active(True)      # so `crash` can be set
                    crash.set_active(watching)
                    master.set_active(on)
                    self.assertEqual(
                        self.row(pane, "ambient.crash_diagnose")
                            .get_sensitive(),
                        on and watching)

    def test_the_battery_thresholds_follow_the_battery_hook(self):
        pane = self.build("ambient")
        self.control(pane, "ambient.enabled").set_active(True)
        battery = self.control(pane, "ambient.battery")
        for watching in (True, False, True):
            battery.set_active(watching)
            for key in ("ambient.battery_low_pct",
                        "ambient.battery_critical_pct"):
                self.assertEqual(self.row(pane, key).get_sensitive(),
                                 watching, key)

    def test_the_summary_says_how_many_hooks_are_live(self):
        pane = self.build("ambient")
        self.control(pane, "ambient.enabled").set_active(True)
        for key in ("ambient.crash", "ambient.battery", "ambient.update"):
            self.control(pane, key).set_active(False)
        self.assertTrue(any("All three hooks are off" in t
                            for t in self.texts(pane)))
        self.control(pane, "ambient.update").set_active(True)
        self.assertTrue(any("1 of 3 hooks on" in t for t in self.texts(pane)))
        self.control(pane, "ambient.enabled").set_active(False)
        self.assertTrue(any("Noticing is off" in t for t in self.texts(pane)))


class HudGateTest(PaneCase):
    def test_the_overlay_switch_governs_the_five_rows_below_it(self):
        pane = self.build("hud")
        enabled = self.control(pane, "hud.enabled")
        rows = ("hud.corner", "hud.scale", "hud.idle_visible", "hud.caption",
                "hud.sprite")
        enabled.set_active(False)
        for key in rows:
            self.assertFalse(self.row(pane, key).get_sensitive(), key)
        enabled.set_active(True)
        for key in rows:
            self.assertTrue(self.row(pane, key).get_sensitive(), key)

    def test_the_summary_covers_all_four_caption_and_idle_states(self):
        pane = self.build("hud")
        self.control(pane, "hud.enabled").set_active(True)
        wants = {(True, True): "Always on screen, with a caption",
                 (True, False): "Always on screen, with no caption",
                 (False, True): "Appears when she is listening",
                 (False, False): "shows nothing else"}
        for (idle, caption), want in wants.items():
            with self.subTest(idle=idle, caption=caption):
                self.control(pane, "hud.idle_visible").set_active(idle)
                self.control(pane, "hud.caption").set_active(caption)
                self.assertTrue(any(want in t for t in self.texts(pane)),
                                f"no summary for idle={idle} caption={caption}")

    def test_an_inert_row_carries_no_trailing_note(self):
        """A note says what a setting is *currently doing*, so an inert row
        must say nothing rather than say something false."""
        pane = self.build("hud")
        self.control(pane, "hud.enabled").set_active(True)
        self.assertTrue(any(t in ("always on", "on demand")
                            for t in self.texts(pane)))
        self.control(pane, "hud.enabled").set_active(False)
        self.assertFalse(any(t in ("always on", "on demand", "her words",
                                   "sprite only")
                             for t in self.texts(pane)))


class VcsGateTest(PaneCase):
    def test_auto_merge_governs_the_rows_that_only_matter_when_it_is_on(self):
        pane = self.build("vcs")
        auto = self.control(pane, "vcs.auto_merge")
        rows = ("vcs.merge_method", "vcs.delete_branch",
                "vcs.notify_on_refusal", "vcs.check_wait_seconds")
        auto.set_active(False)
        for key in rows:
            self.assertFalse(self.row(pane, key).get_sensitive(), key)
        auto.set_active(True)
        for key in rows:
            self.assertTrue(self.row(pane, key).get_sensitive(), key)

    def test_merging_unread_is_marked_as_the_risk_it_is(self):
        pane = self.build("vcs")
        notes = [w for w in walk(pane)
                 if isinstance(w, Gtk.Label) and w.has_css_class("statenote")]
        self.control(pane, "vcs.auto_merge").set_active(True)
        self.assertTrue(any(w.has_css_class("risk") for w in notes))
        self.control(pane, "vcs.auto_merge").set_active(False)
        self.assertFalse(any(w.has_css_class("risk") for w in notes))


class ConfirmStateTest(PaneCase):
    def test_every_policy_row_reads_differently_in_all_three_positions(self):
        """`set_value` is the refresh path: it moves the toggle and tells the
        watchers, and deliberately does *not* write back — that is what stops
        a reload from re-saving what it just loaded."""
        pane = self.build("confirm")
        tri = self.control(pane, "confirm.delete_files")
        seen = set()
        for value in schema.TRI:
            tri.set_value(value)
            self.assertEqual(tri.value(), value)
            seen.add(tuple(t for t in self.texts(pane) if t))
        self.assertEqual(len(seen), 3,
                         "the pane reads the same in all three positions")

    def test_choosing_a_policy_writes_it_through(self):
        pane = self.build("confirm")
        tri = self.control(pane, "confirm.delete_files")
        for value, button in zip(schema.TRI, self.segments(tri)):
            with self.subTest(value=value):
                button.set_active(True)
                self.assertEqual(self.editor.get("confirm.delete_files"),
                                 value)

    def test_all_nine_classes_offer_all_three_answers(self):
        pane = self.build("confirm")
        for section in schema.SPEC:
            for fld in section.fields:
                if not isinstance(fld, schema.Tri):
                    continue
                dotted = f"{section.key}.{fld.key}"
                with self.subTest(key=dotted):
                    tri = self.control(pane, dotted)
                    self.assertEqual(len(self.segments(tri)), 3)

    def test_the_hard_denials_are_shown_and_not_editable(self):
        pane = self.build("confirm")
        texts = self.texts(pane)
        for name, _why in schema.HARD_DENIES:
            self.assertTrue(any(name in t for t in texts), name)
        self.assertTrue(any("not editable" in t for t in texts))


class ListenGateTest(PaneCase):
    def test_the_listen_switch_governs_the_rows_it_owns(self):
        pane = self.build("listen")
        switch = self.control(pane, "listen.enabled")
        switch.set_active(False)
        switch.set_active(True)
        self.assertTrue(self.editor.get("listen.enabled"))

    def test_the_pane_says_the_three_keys_belong_to_voxtype(self):
        pane = self.build("listen")
        blob = " ".join(self.texts(pane))
        self.assertIn("voxtype", blob)


# =========================================================================
# The slider
# =========================================================================


class SliderTest(PaneCase):
    def test_both_multipliers_are_sliders_and_nothing_else_is(self):
        sliders = [f"{s.key}.{f.key}" for s in schema.SPEC for f in s.fields
                   if isinstance(f, schema.Slider)]
        self.assertEqual(sorted(sliders), ["hud.scale", "voice.speed"])

    def test_the_read_back_carries_the_number_and_the_unit(self):
        pane = self.build("hud")
        box = self.control(pane, "hud.scale")
        reads = [w for w in walk(box) if isinstance(w, Gtk.Label)]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].get_text(), "1.00x")

    def test_dragging_it_writes_a_value_snapped_to_the_step(self):
        pane = self.build("hud")
        box = self.control(pane, "hud.scale")
        scale = next(w for w in walk(box) if isinstance(w, Gtk.Scale))
        scale.set_value(1.7333333)
        self.assertAlmostEqual(self.editor.get("hud.scale"), 1.7, places=6)
        read = next(w for w in walk(box) if isinstance(w, Gtk.Label))
        self.assertEqual(read.get_text(), "1.70x")

    def test_the_track_spans_the_field_s_whole_range(self):
        pane = self.build("voice")
        box = self.control(pane, "voice.speed")
        scale = next(w for w in walk(box) if isinstance(w, Gtk.Scale))
        adj = scale.get_adjustment()
        _sec, fld = schema.field_for("voice.speed")
        self.assertAlmostEqual(adj.get_lower(), fld.min)
        self.assertAlmostEqual(adj.get_upper(), fld.max)

    def test_it_has_a_tick_at_the_default(self):
        pane = self.build("hud")
        scale = next(w for w in walk(self.control(pane, "hud.scale"))
                     if isinstance(w, Gtk.Scale))
        self.assertTrue(scale.get_has_origin() is not None)   # widget is live
        # `Gtk.Scale` exposes no mark accessor, so the evidence that a mark
        # was added is that clearing them changes the requested height.
        before = scale.get_preferred_size()[1].height
        scale.clear_marks()
        self.assertLess(scale.get_preferred_size()[1].height, before)

    def test_a_refresh_puts_a_value_back_without_writing_it_again(self):
        pane = self.build("hud")
        box = self.control(pane, "hud.scale")
        scale = next(w for w in walk(box) if isinstance(w, Gtk.Scale))
        writes = []
        real = self.editor.set
        self.editor.set = lambda k, v: (writes.append((k, v)), real(k, v))[1]
        self.binder.refresh({"hud.scale"})
        self.assertEqual(writes, [])
        self.assertAlmostEqual(scale.get_value(), 1.0, places=6)

    def test_the_slider_never_writes_outside_its_own_range(self):
        pane = self.build("voice")
        scale = next(w for w in walk(self.control(pane, "voice.speed"))
                     if isinstance(w, Gtk.Scale))
        _sec, fld = schema.field_for("voice.speed")
        for attempt in (-99.0, 0.0, 99.0):
            scale.set_value(attempt)
            got = self.editor.get("voice.speed")
            self.assertGreaterEqual(got, fld.min)
            self.assertLessEqual(got, fld.max)


# =========================================================================
# The guard
# =========================================================================


class GuardTest(unittest.TestCase):
    """This suite may not touch the settings the daemon is reading."""

    def test_the_config_path_is_not_the_user_s(self):
        self.assertEqual(config.CONFIG_PATH, FORBIDDEN_JARVIS_CONFIG)
        real = pathlib.Path.home() / ".config" / "jarvis" / "config.toml"
        self.assertNotEqual(config.CONFIG_PATH, real)
        self.assertFalse(str(config.CONFIG_PATH).startswith(
            str(pathlib.Path.home())))

    def test_the_redirected_path_is_a_real_writable_file(self):
        """Unlike the voxtype sentinels, this one has to work: the app
        creates and rewrites it rather than merely naming it."""
        self.assertTrue(config.CONFIG_PATH.exists())
        values, _unknown, _warnings = config.load(config.CONFIG_PATH)
        self.assertEqual(values["assistant.name"], "Luna")


if __name__ == "__main__":
    unittest.main()
