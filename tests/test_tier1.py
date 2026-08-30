"""Tier 1: § parsing, round-trip, and the hard cap that must never truncate."""

from __future__ import annotations

from lunad import config
from lunad.memory import (
    MemoryCapExceeded,
    MemoryError as LunaMemoryError,
    parse_entries,
    render_entries,
)

from ._support import TempMemoryCase


class ParseRoundTripTests(TempMemoryCase):
    def test_render_uses_the_section_delimiter(self):
        out = render_entries(["one", "two"])
        self.assertEqual(out, "§ one\n\n§ two\n")

    def test_round_trip_preserves_entries(self):
        entries = [
            "single line",
            "multi\nline entry\nwith three lines",
            "punctuation: §-adjacent? yes. 100% — dashes, 'quotes'",
            "unicode ✓ ünïcödé 日本語",
        ]
        self.assertEqual(parse_entries(render_entries(entries)), entries)

    def test_round_trip_through_a_file(self):
        f = self.tier1(cap=1000)
        entries = ["alpha", "beta\nstill beta", "gamma"]
        f.replace(entries)
        self.assertEqual(f.entries(), entries)
        self.assertEqual(parse_entries(f.text()), entries)

    def test_empty_file_parses_to_nothing(self):
        self.assertEqual(parse_entries(""), [])
        self.assertEqual(parse_entries("   \n\n  "), [])
        self.assertEqual(render_entries([]), "")

    def test_blank_entries_are_dropped_not_rendered(self):
        self.assertEqual(render_entries(["a", "", "   ", "b"]), "§ a\n\n§ b\n")

    def test_text_before_the_first_delimiter_is_kept(self):
        # A hand-edited file must not lose its top matter.
        raw = "notes typed by hand\n§ proper entry\n"
        self.assertEqual(parse_entries(raw), ["notes typed by hand", "proper entry"])

    def test_delimiter_without_a_trailing_space_still_parses(self):
        self.assertEqual(parse_entries("§one\n§ two\n"), ["one", "two"])

    def test_delimiter_mid_line_is_not_a_boundary(self):
        self.assertEqual(parse_entries("§ cost is 5§ per unit\n"),
                         ["cost is 5§ per unit"])


class CapTests(TempMemoryCase):
    def test_usage_reports_a_percentage(self):
        f = self.tier1(cap=100)
        f.append("x" * 48)          # renders as "§ " + 48 + "\n" = 51 chars
        usage = f.usage()
        self.assertEqual(usage["cap"], 100)
        self.assertEqual(usage["chars"], 51)
        self.assertEqual(usage["pct"], 51.0)
        self.assertEqual(usage["remaining"], 49)
        self.assertEqual(usage["entries"], 1)

    def test_append_over_cap_raises(self):
        f = self.tier1(cap=60)
        f.append("fits fine")
        with self.assertRaises(MemoryCapExceeded):
            f.append("x" * 200)

    def test_rejected_append_does_not_truncate_or_write(self):
        f = self.tier1(cap=60)
        f.append("keep me")
        before = f.text()
        with self.assertRaises(MemoryCapExceeded):
            f.append("y" * 500)
        self.assertEqual(f.text(), before, "file was modified by a rejected write")
        self.assertEqual(f.entries(), ["keep me"])
        self.assertNotIn("y", f.text())

    def test_rejected_replace_does_not_write(self):
        f = self.tier1(cap=60)
        f.replace(["original"])
        with self.assertRaises(MemoryCapExceeded):
            f.replace(["z" * 100])
        self.assertEqual(f.entries(), ["original"])

    def test_exception_reports_usage_and_overflow(self):
        f = self.tier1(cap=100)
        f.append("a" * 40)
        with self.assertRaises(MemoryCapExceeded) as ctx:
            f.append("b" * 100)
        exc = ctx.exception
        self.assertEqual(exc.name, "LUNA.md")
        self.assertEqual(exc.cap, 100)
        self.assertEqual(exc.current_chars, 43)
        self.assertEqual(exc.entry_count, 1)
        self.assertGreater(exc.proposed_chars, 100)
        self.assertEqual(exc.overflow, exc.proposed_chars - 100)
        self.assertEqual(exc.usage_pct, 43.0)
        self.assertIn("Consolidate", str(exc))
        self.assertIn("nothing was truncated", str(exc))
        payload = exc.to_dict()
        self.assertEqual(payload["error"], "MemoryCapExceeded")
        self.assertEqual(payload["overflow"], exc.overflow)

    def test_exactly_at_the_cap_is_allowed(self):
        f = self.tier1(cap=51)
        f.append("x" * 48)                      # renders to exactly 51
        self.assertEqual(f.usage()["chars"], 51)
        self.assertEqual(f.usage()["pct"], 100.0)
        with self.assertRaises(MemoryCapExceeded):
            f.append("one char over")

    def test_real_caps_match_the_architecture(self):
        self.assertEqual(config.LUNA_MD_CAP, 3000)
        self.assertEqual(config.USER_MD_CAP, 2000)

    def test_empty_entry_is_refused(self):
        f = self.tier1()
        with self.assertRaises(LunaMemoryError):
            f.append("   ")

    def test_remove_and_clear(self):
        f = self.tier1(cap=500)
        f.replace(["a", "b", "c"])
        f.remove(1)
        self.assertEqual(f.entries(), ["a", "c"])
        with self.assertRaises(LunaMemoryError):
            f.remove(9)
        f.clear()
        self.assertEqual(f.entries(), [])


class MemoryFacadeTests(TempMemoryCase):
    def test_file_lookup_is_forgiving_but_bounded(self):
        mem = self.memory()
        self.assertIs(mem.file("LUNA.md"), mem.luna)
        self.assertIs(mem.file("luna"), mem.luna)
        self.assertIs(mem.file("user.md"), mem.user)
        with self.assertRaises(LunaMemoryError):
            mem.file("SOL.md")

    def test_tier1_block_is_empty_when_memory_is_empty(self):
        mem = self.memory()
        self.assertIn("empty", mem.tier1_block())

    def test_tier1_block_contains_written_entries(self):
        mem = self.memory()
        mem.luna.append("the bar is omarchy-shell, not waybar")
        mem.user.append("prefers British spelling")
        block = mem.tier1_block()
        self.assertIn("omarchy-shell", block)
        self.assertIn("British spelling", block)
        self.assertIn("LUNA.md", block)
        self.assertIn("USER.md", block)

    def test_usage_exposes_all_three_tiers(self):
        usage = self.memory().usage()
        self.assertEqual(usage["tier1"]["LUNA.md"]["cap"], 3000)
        self.assertIn("episodes", usage["tier2"])
        self.assertTrue(usage["tier3"]["implemented"])
