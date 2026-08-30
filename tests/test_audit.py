"""The audit log: append-only, durable, and honest about undo."""

from __future__ import annotations

import json
import os
import time
import unittest

from lunad import audit as audit_mod

from ._support import TempMemoryCase


class AppendTests(TempMemoryCase):
    def log(self) -> audit_mod.AuditLog:
        return audit_mod.AuditLog(self.root / "audit.jsonl")

    def test_an_entry_round_trips(self):
        log = self.log()
        written = log.append("dispatch.spawn", ok=True, job_id="abc",
                             why="the user asked for a proof file", pid=1234)
        [read] = log.read()
        self.assertEqual(read["action"], "dispatch.spawn")
        self.assertEqual(read["job_id"], "abc")
        self.assertEqual(read["pid"], 1234)
        self.assertTrue(read["ok"])
        self.assertEqual(read["ts"], written["ts"])

    def test_every_entry_carries_a_timestamp_in_both_forms(self):
        log = self.log()
        before = time.time()
        log.append("test")
        [read] = log.read()
        # `ts` is rounded to milliseconds for readability, so it can land a
        # fraction of a millisecond *below* the reading taken just before it.
        # Comparing exactly made this test fail roughly one run in fifty.
        self.assertGreaterEqual(read["ts"], before - 0.001)
        self.assertRegex(read["iso"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_writes_append_and_never_rewrite(self):
        log = self.log()
        for i in range(5):
            log.append("test", index=i)
        raw = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(raw.strip().splitlines()), 5)
        # Every earlier line is still there, byte for byte, in order.
        indices = [json.loads(line)["index"] for line in raw.strip().splitlines()]
        self.assertEqual(indices, [0, 1, 2, 3, 4])

    def test_a_second_log_object_appends_rather_than_truncating(self):
        self.log().append("first")
        self.log().append("second")
        actions = [e["action"] for e in self.log().read(newest_first=False)]
        self.assertEqual(actions, ["first", "second"])

    def test_newest_first_is_the_default(self):
        log = self.log()
        log.append("older")
        log.append("newer")
        self.assertEqual([e["action"] for e in log.read()], ["newer", "older"])

    def test_none_valued_fields_are_dropped_not_written_as_null(self):
        log = self.log()
        log.append("test", present="yes", absent=None)
        [read] = log.read()
        self.assertIn("present", read)
        self.assertNotIn("absent", read)

    def test_the_writing_process_is_recorded_without_shadowing_the_subject(self):
        """`pid` belongs to whatever the entry is about, not to the writer."""
        log = self.log()
        log.append("signal.refused", pid=4321)
        [read] = log.read()
        self.assertEqual(read["by_pid"], os.getpid())
        self.assertEqual(read["pid"], 4321)

    def test_an_unwritable_path_does_not_raise(self):
        """A broken audit log must not be able to fail an action.

        It is loud in the daemon log instead. The alternative — an exception
        propagating out of `append` — would mean a full disk could stop Luna
        speaking.
        """
        log = audit_mod.AuditLog(self.root / "not-a-dir" / "x" / "audit.jsonl")
        (self.root / "not-a-dir").write_text("I am a file", encoding="utf-8")
        entry = log.append("test", ok=True)
        self.assertEqual(entry["action"], "test")
        self.assertEqual(log.read(), [])


class ReadFilterTests(TempMemoryCase):
    def log(self) -> audit_mod.AuditLog:
        log = audit_mod.AuditLog(self.root / "audit.jsonl")
        now = time.time()
        for age_s, action in ((7200, "old.thing"), (60, "dispatch.spawn"),
                              (30, "dispatch.finish"), (10, "signal.refused")):
            entry = {"ts": now - age_s, "iso": "x", "action": action,
                     "actor": "luna"}
            with open(log.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        return log

    def test_since_filters_by_time(self):
        entries = self.log().read(since=time.time() - 300)
        self.assertEqual([e["action"] for e in entries],
                         ["signal.refused", "dispatch.finish", "dispatch.spawn"])

    def test_action_prefix_filters(self):
        entries = self.log().read(action="dispatch.")
        self.assertEqual(len(entries), 2)

    def test_limit_applies_after_ordering(self):
        entries = self.log().read(limit=1)
        self.assertEqual(entries[0]["action"], "signal.refused")

    def test_an_unparseable_line_is_surfaced_not_hidden(self):
        log = self.log()
        with open(log.path, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        actions = [e["action"] for e in log.read()]
        self.assertIn("unparseable", actions)

    def test_a_missing_log_reads_as_empty(self):
        log = audit_mod.AuditLog(self.root / "never-written.jsonl")
        self.assertEqual(log.read(), [])

    def test_summarise_counts_actions(self):
        counts = audit_mod.summarise(self.log().read())
        self.assertEqual(counts["dispatch.spawn"], 1)
        self.assertEqual(counts["old.thing"], 1)


class SinceParsingTests(unittest.TestCase):
    def test_relative_units(self):
        now = 1_000_000.0
        for text, seconds in (("30s", 30), ("5m", 300), ("6h", 21600),
                              ("2d", 172800), ("1w", 604800)):
            with self.subTest(text=text):
                self.assertAlmostEqual(
                    audit_mod.parse_since(text, now=now), now - seconds)

    def test_case_and_spacing_are_forgiving(self):
        now = 1_000_000.0
        self.assertAlmostEqual(audit_mod.parse_since(" 2 H ", now=now),
                               now - 7200)

    def test_absolute_dates(self):
        parsed = audit_mod.parse_since("2026-08-25")
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(parsed)),
                         "2026-08-25")

    def test_epoch_passes_through(self):
        self.assertEqual(audit_mod.parse_since(1_700_000_000), 1_700_000_000.0)
        self.assertEqual(audit_mod.parse_since("1700000000"), 1_700_000_000.0)

    def test_none_and_empty_mean_no_lower_bound(self):
        self.assertIsNone(audit_mod.parse_since(None))
        self.assertIsNone(audit_mod.parse_since(""))

    def test_nonsense_raises_rather_than_silently_reading_everything(self):
        with self.assertRaises(ValueError) as caught:
            audit_mod.parse_since("last tuesday")
        self.assertIn("30m", str(caught.exception))


class RotationTests(TempMemoryCase):
    """`[audit] max_mb` / `keep`: bounded, and never quietly shorter.

    The ceiling is in whole megabytes because that is the only unit a person
    setting it thinks in, so the entries here carry a fat payload rather than
    the suite writing four thousand ordinary lines and four thousand fsyncs to
    cross one. What is being tested is the rotation, not the arithmetic.
    """

    FAT = 300_000       # a third of a megabyte per entry

    def log(self) -> audit_mod.AuditLog:
        self.settings.set("audit.max_mb", 1)
        return audit_mod.AuditLog(self.root / "audit.jsonl")

    def fill(self, log: audit_mod.AuditLog, entries: int,
             start: int = 0) -> None:
        for i in range(start, start + entries):
            log.append("test", index=i, filler="x" * self.FAT)

    def test_the_live_log_rotates_and_the_old_bytes_move_to_a_sibling(self):
        log = self.log()
        self.fill(log, 4)
        self.assertEqual(log.rotations, 1)
        self.assertTrue(log.sibling(1).exists())
        self.assertGreater(log.sibling(1).stat().st_size, 1_048_576)
        self.assertLess(log.path.stat().st_size, 1_048_576)

    def test_the_new_file_opens_with_the_entry_that_explains_the_gap(self):
        log = self.log()
        self.fill(log, 4)
        first = json.loads(log.path.read_text(encoding="utf-8")
                           .splitlines()[0])
        self.assertEqual(first["action"], "audit.rotated")
        self.assertEqual(first["rotated_to"], str(log.sibling(1)))
        self.assertEqual(first["keep"], 5)

    def test_no_entry_is_lost_across_a_rotation(self):
        log = self.log()
        self.fill(log, 8)
        self.assertGreaterEqual(log.rotations, 2)
        seen = [e["index"] for e in log.read(newest_first=False)
                if e["action"] == "test"]
        self.assertEqual(seen, list(range(8)))

    def test_a_rotation_never_splits_a_line(self):
        log = self.log()
        self.fill(log, 8)
        for path in log.chain():
            for lineno, raw in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                with self.subTest(path=path.name, line=lineno):
                    json.loads(raw)      # must parse; a torn write would not

    def test_the_oldest_sibling_is_dropped_and_the_drop_is_recorded(self):
        self.settings.set("audit.keep", 1)
        log = self.log()
        self.fill(log, 4)
        dropped_none = json.loads(log.path.read_text(
            encoding="utf-8").splitlines()[0])
        self.assertNotIn("dropped", dropped_none,
                         "nothing had to be deleted on the first rotation")
        self.fill(log, 4, start=4)
        self.assertFalse(log.sibling(2).exists(), "keep = 1")
        entry = json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["dropped"], str(log.sibling(1)))

    def test_zero_means_never_rotate(self):
        self.settings.set("audit.max_mb", 0)
        log = audit_mod.AuditLog(self.root / "audit.jsonl")
        self.fill(log, 6)
        self.assertEqual(log.rotations, 0)
        self.assertFalse(log.sibling(1).exists())
        self.assertGreater(log.path.stat().st_size, 1_048_576)

    def test_reading_stops_at_the_first_file_that_cannot_help(self):
        """The whole retained history is up to `keep` x `max_mb`. A
        `luna audit -n 40` must not read all of it to answer."""
        opened: list[str] = []

        class _Counting(audit_mod.AuditLog):
            def _read_file(self, path, since, action):  # noqa: ANN001, ANN202
                opened.append(path.name)
                return super()._read_file(path, since, action)

        self.settings.set("audit.max_mb", 1)
        log = _Counting(self.root / "audit.jsonl")
        self.fill(log, 8)
        opened.clear()
        log.read(limit=1)
        self.assertEqual(opened, ["audit.jsonl"])
        opened.clear()
        # A `since` older than everything has to walk the whole chain.
        log.read(since=0.0)
        self.assertEqual(len(opened), len(log.chain()))

    def test_a_sibling_beyond_a_lowered_keep_is_still_read(self):
        # History you stopped rotating is not history that stopped existing.
        log = self.log()
        self.fill(log, 4)
        self.settings.set("audit.keep", 1)
        self.assertIn(log.sibling(1), log.chain())
        seen = [e["index"] for e in log.read(newest_first=False)
                if e["action"] == "test"]
        self.assertEqual(seen, [0, 1, 2, 3])

    def test_stats_report_the_whole_retained_chain(self):
        log = self.log()
        self.fill(log, 4)
        stats = log.stats()
        self.assertEqual(stats["siblings"], 1)
        self.assertEqual(stats["rotated_this_session"], 1)
        self.assertEqual(stats["rotates_at_bytes"], 1_048_576)
        self.assertGreater(stats["retained_bytes"], stats["size_bytes"])


class UndoTests(TempMemoryCase):
    def test_an_append_records_the_inverse_that_actually_exists(self):
        undo = audit_mod.undo_for_memory_append("LUNA.md", 3)
        self.assertEqual(undo["cmd"],
                         ["luna", "memory", "rm", "3", "--file", "LUNA.md"])
        self.assertIn("no further entry", undo["valid_while"])

    def test_undo_is_absent_when_there_is_no_inverse(self):
        """The honest half. Nothing invents an undo command."""
        log = audit_mod.AuditLog(self.root / "audit.jsonl")
        log.append("dispatch.finish", ok=True, exit_code=0)
        [read] = log.read()
        self.assertNotIn("undo", read)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
