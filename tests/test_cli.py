"""`bin/luna` — what it prints, what it refuses to print, and how it fails.

Three things are under test here and they are different in kind.

**The look.** Every screen a person reads is rendered by a pure function of the
daemon's reply, so the layout can be asserted without a daemon and without a
terminal. That is the only way to test the rule that matters most: *a pipe gets
bytes, not escapes*. A tool that leaks colour into `luna --json` is worse than
an ugly one, and "it looked fine when I ran it" is not evidence, because the
suite itself is sometimes a tty and sometimes not.

**The four bugs.** Each has a case named after the thing that used to happen.

**The wiring.** `luna embed` and `[memory] semantic_recall`, which existed as
working code nothing could reach.

Nothing here opens a socket to lunad. The one case that needs a real socket
makes its own with `socketpair`, which is a pair of file descriptors and not a
daemon.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import socket
import unittest
from pathlib import Path
from typing import Any

from lunad import embed, render
from lunad import settings as settings_mod

from ._support import TempMemoryCase

_CLI: Any = None


def cli() -> Any:
    """``bin/luna`` as a module. It has no ``.py``, so importlib needs a nudge.

    Importing runs only the module body: no socket, no config read. The style
    is then pinned to no colour at a fixed width, because the one chosen at
    import comes from the real stdout and would make every assertion below
    depend on whether the suite was piped.
    """
    global _CLI
    if _CLI is None:
        path = Path(__file__).resolve().parent.parent / "bin" / "luna"
        loader = importlib.machinery.SourceFileLoader("luna_cli", str(path))
        spec = importlib.util.spec_from_loader("luna_cli", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _CLI = module
    _CLI.S = render.Style(color=False, width=100)
    return _CLI


PLAIN = render.Style(color=False, width=100)
COLOURED = render.Style(color=True, width=100)
NARROW = render.Style(color=False, width=64)

ESC = "\033"


# =========================================================================
# The rules in lunad/render.py
# =========================================================================


class FakeStream:
    """Something with the three attributes `Style.detect` asks about."""

    def __init__(self, tty: bool = True, encoding: str = "utf-8") -> None:
        self._tty = tty
        self.encoding = encoding

    def isatty(self) -> bool:
        return self._tty


class StyleTests(unittest.TestCase):
    def test_a_pipe_gets_no_escapes_however_the_environment_is_set(self) -> None:
        style = render.Style.detect(FakeStream(tty=False), {"TERM": "xterm"})
        self.assertFalse(style.color)
        for text in (style.bold("x"), style.dim("x"), style.alert("x"),
                     style.rule(10)):
            self.assertNotIn(ESC, text)

    def test_no_color_wins_over_a_real_terminal(self) -> None:
        env = {"NO_COLOR": "1", "TERM": "xterm-256color"}
        style = render.Style.detect(FakeStream(tty=True), env)
        self.assertFalse(style.color)
        self.assertEqual(style.bold("lunad"), "lunad")

    def test_no_color_set_empty_is_not_no_color(self) -> None:
        # The convention is "present and non-empty". An empty NO_COLOR is what
        # `NO_COLOR= luna status` leaves behind when someone unsets it the
        # wrong way, and reading it as a request for monochrome would make the
        # variable impossible to turn off.
        style = render.Style.detect(FakeStream(tty=True),
                                    {"NO_COLOR": "", "TERM": "xterm"})
        self.assertTrue(style.color)

    def test_a_dumb_terminal_gets_no_escapes(self) -> None:
        style = render.Style.detect(FakeStream(tty=True), {"TERM": "dumb"})
        self.assertFalse(style.color)

    def test_a_tty_with_a_normal_term_does_get_colour(self) -> None:
        style = render.Style.detect(FakeStream(tty=True), {"TERM": "foot"})
        self.assertTrue(style.color)
        self.assertIn(ESC, style.bold("lunad"))

    def test_the_width_comes_from_the_environment_when_it_is_set(self) -> None:
        style = render.Style.detect(FakeStream(), {"COLUMNS": "133"})
        self.assertEqual(style.width, 133)

    def test_an_absurd_width_is_floored_rather_than_obeyed(self) -> None:
        self.assertEqual(render.Style.detect(FakeStream(),
                                             {"COLUMNS": "3"}).width, 40)

    def test_a_terminal_that_cannot_encode_the_glyphs_gets_ascii(self) -> None:
        style = render.Style.detect(FakeStream(encoding="ascii"), {"TERM": "foot"})
        self.assertFalse(style.unicode)
        self.assertEqual(render.meter(50, 4, style), "==--")
        self.assertNotIn("─", style.rule(5))
        self.assertEqual(style.ellipsis, "...")


class MeterTests(unittest.TestCase):
    def test_the_ends_are_exact(self) -> None:
        self.assertEqual(render.meter(0, 6, PLAIN), "──────")
        self.assertEqual(render.meter(100, 6, PLAIN), "━━━━━━")
        self.assertEqual(render.meter(150, 6, PLAIN), "━━━━━━")

    def test_a_file_with_something_in_it_never_reads_as_empty(self) -> None:
        # 0.4% of 3000 chars is twelve characters, and an empty bar would say
        # the file is empty, which is a different fact from "nearly empty".
        self.assertEqual(render.meter(0.4, 10, PLAIN).count("━"), 1)

    def test_a_meter_is_always_its_full_width(self) -> None:
        for pct in (0, 1, 33.3, 47.9, 99.9, 100):
            self.assertEqual(len(render.meter(pct, 18, PLAIN)), 18)


class NumberTests(unittest.TestCase):
    def test_bytes_are_read_as_sizes(self) -> None:
        self.assertEqual(render.human_bytes(512), "512 B")
        self.assertEqual(render.human_bytes(102400), "100 kB")
        self.assertEqual(render.human_bytes(90 << 20), "90 MB")

    def test_a_number_of_seconds_becomes_an_answer(self) -> None:
        self.assertEqual(render.human_duration(48), "48s")
        self.assertEqual(render.human_duration(48824), "13h33m")
        self.assertEqual(render.human_duration(300), "5m00s")
        self.assertEqual(render.human_duration(200000), "2d 7h")

    def test_nothing_says_one_entry_s(self) -> None:
        self.assertEqual(render.count(1, "fact"), "1 fact")
        self.assertEqual(render.count(0, "fact"), "0 facts")
        self.assertEqual(render.count(2, "entry", "entries"), "2 entries")


class LayoutTests(unittest.TestCase):
    def test_escapes_are_zero_width_when_columns_are_measured(self) -> None:
        self.assertEqual(render.visible_len("\033[1mlunad\033[0m"), 5)

    def test_a_coloured_cell_does_not_widen_its_column(self) -> None:
        rows = [[COLOURED.bold("aa"), "x"], ["bbbb", "y"]]
        lines = render.columns(rows, COLOURED)
        self.assertEqual([render.visible_len(line) for line in lines], [7, 7])

    def test_the_label_column_is_measured_not_guessed(self) -> None:
        b = render.Block(PLAIN)
        b.add("agent", "claude")
        b.add("consolidate", "off")
        first, second = b.lines()
        self.assertEqual(first.index("claude"), second.index("off"))

    def test_a_continuation_lines_up_under_the_value_not_the_label(self) -> None:
        b = render.Block(PLAIN)
        b.add("memory", "LUNA.md")
        b.cont("USER.md")
        first, second = b.lines()
        self.assertEqual(first.index("LUNA.md"), second.index("USER.md"))

    def test_truncation_says_it_truncated(self) -> None:
        self.assertEqual(render.fit("abcdefghij", 6, PLAIN), "abcde…")
        self.assertEqual(render.fit("abc", 6, PLAIN), "abc")

    def test_a_paragraph_keeps_its_hanging_indent(self) -> None:
        lines = render.wrapped("word " * 40, render.Style(False, 50), 6,
                               first="  [0] ")
        self.assertTrue(lines[0].startswith("  [0] "))
        for line in lines[1:]:
            self.assertTrue(line.startswith("      "))
            self.assertLessEqual(len(line), 50)


# =========================================================================
# The screens
# =========================================================================


def status_reply(**over: Any) -> dict:
    reply = {
        "ok": True,
        "daemon": {"version": "0.1.0", "pid": 1852972, "uptime_s": 48824.0,
                   "threads": 5, "socket": "/run/user/1000/luna/luna.sock"},
        "agent": {"name": "claude", "available": True, "detail": "/bin/claude",
                  "model": "", "tools": True, "sandbox": ""},
        "vision": {"available": True, "detail": "grim"},
        "memory": {
            "tier1": {"LUNA.md": {"chars": 1436, "cap": 3000, "pct": 47.9,
                                  "entries": 7},
                      "USER.md": {"chars": 506, "cap": 2000, "pct": 25.3,
                                  "entries": 3}},
            "tier2": {"episodes": 19, "size_bytes": 102400,
                      "mean_salience": 0.461},
            "tier3": {"exists": True, "facts": 1, "slots": ["tools"],
                      "corrections": 0, "episodes": 14,
                      "iso": "2026-08-30 20:26"},
        },
        "activity": {"counters": {"ask": 4, "errors": 0},
                     "in_flight": [], "session_cost_usd": 0.0342},
        "consolidation": {"enabled": True, "every_turns": 12, "turns_since": 4,
                          "passes": 0, "added": 0, "removed": 0,
                          "cost_usd": 0.0, "previews": 0, "running": False},
        "speech": {"voice": "flux-alexis-en", "speaking": False,
                   "loaded": False, "idle_unload_s": 300.0},
        "dispatch": {"workspace": {"available": True, "workspace": "special:luna",
                                   "visible": False, "windows": 0},
                     "running": [], "queued": [], "max_parallel": 1},
        "spawned": {"tracked": 41, "refusals": 0},
        "audit": {"size_bytes": 143457},
        "sessions": {"live": [{"key": "default", "turns": 4, "idle_s": 45563.0,
                               "cost_usd": 0.0342}],
                     "counters": {"new": 1, "resumed": 3, "retired": 0}},
    }
    reply.update(over)
    return reply


class StatusTests(unittest.TestCase):
    def text(self, style: render.Style = PLAIN, **over: Any) -> str:
        return cli()._status_text(status_reply(**over), style)

    def test_nothing_but_bytes_when_there_is_nobody_to_colour_for(self) -> None:
        self.assertNotIn(ESC, self.text())

    def test_the_labels_line_up_down_the_whole_screen(self) -> None:
        body = [line for line in self.text().splitlines()[2:]
                if line.strip() and not line.startswith("   ")]
        starts = {line.split(" ")[0]: line for line in body}
        columns = {len(line) - len(line.lstrip()) for line in body}
        self.assertEqual(columns, {2}, starts)
        # ...and every value starts in the same place too.
        values = {line.index(line.strip().split(" ")[1] if " " in line.strip()
                              else line.strip())
                  for line in body if len(line.strip().split()) > 1}
        self.assertEqual(len(values), 1, body)

    def test_it_answers_what_she_is_doing_and_what_it_costs(self) -> None:
        text = self.text()
        self.assertIn("up 13h33m", text)                  # not "48824s"
        self.assertIn("$0.0342 this session", text)
        self.assertIn("4 asks", text)
        self.assertIn("0 in flight", text)
        self.assertIn("every 12 turns, 4 since", text)

    def test_a_missing_tool_is_the_one_thing_that_takes_colour(self) -> None:
        text = cli()._status_text(
            status_reply(agent={"name": "claude", "available": True,
                                "detail": "/bin/claude", "model": "",
                                "tools": False, "sandbox": ""}), COLOURED)
        line = [n for n in text.splitlines() if "no tools" in n][0]
        self.assertIn("\033[31mno tools\033[0m", line)
        # ...and nothing else on the screen is red for decoration.
        self.assertEqual(text.count("\033[31m"), 1)

    def test_an_absent_agent_is_named_in_the_one_colour(self) -> None:
        text = cli()._status_text(
            status_reply(agent={"name": "claude", "available": False,
                                "detail": "not on PATH", "model": "",
                                "tools": False, "sandbox": ""}), COLOURED)
        self.assertIn("\033[31mUNAVAILABLE\033[0m", text)

    def test_the_meter_shrinks_rather_than_overflowing_a_narrow_terminal(self) -> None:
        wide = [n for n in self.text().splitlines() if "LUNA.md" in n][0]
        narrow = [n for n in self.text(NARROW).splitlines() if "LUNA.md" in n][0]
        self.assertEqual(wide.count("━") + wide.count("─"), 18)
        self.assertEqual(narrow.count("━") + narrow.count("─"), 10)
        self.assertLessEqual(len(narrow), NARROW.width)

    def test_tier_two_says_how_much_of_it_is_reachable_by_meaning(self) -> None:
        # Additive: a daemon that cannot count vectors says nothing rather
        # than reporting zero, which would be a lie about tier 2.
        self.assertNotIn("embedded", self.text())
        m = status_reply()["memory"]
        m["tier2"].update({"vectors": 12, "vectors_pending": 7})
        text = cli()._status_text(status_reply(memory=m), PLAIN)
        self.assertIn("12 embedded", text)
        self.assertIn("7 still keyword-only", text)
        self.assertIn("luna embed status", text)

    def test_a_running_job_is_named_under_the_workspace(self) -> None:
        dsp = status_reply()["dispatch"]
        dsp["running"] = [{"id": "abc123", "to": "worker", "elapsed_s": 12.0,
                           "task": "run the suite"}]
        text = cli()._status_text(status_reply(dispatch=dsp), PLAIN)
        self.assertIn("abc123 → worker 12.0s  run the suite", text)


class JobsTests(unittest.TestCase):
    def reply(self, state: str = "finished") -> dict:
        return {"ok": True,
                "workspace": {"workspace": "special:luna", "visible": False,
                              "windows": 0},
                "jobs": [{"id": "0065b9d6", "state": state, "to": "worker",
                          "elapsed_s": 10.9, "exit_code": 0,
                          "iso": "2026-08-30T20:42:20",
                          "task": "Print the word BETA and nothing else."}]}

    def test_a_pipe_gets_no_escapes(self) -> None:
        self.assertNotIn(ESC, cli()._jobs_text(self.reply(), PLAIN))

    def test_a_failure_is_the_only_row_that_takes_colour(self) -> None:
        ok = cli()._jobs_text(self.reply(), COLOURED)
        bad = cli()._jobs_text(self.reply("failed"), COLOURED)
        self.assertNotIn("\033[31m", ok)
        self.assertIn("\033[31mFAIL\033[0m", bad)

    def test_the_task_gets_a_line_of_its_own_rather_than_a_cut_column(self) -> None:
        text = cli()._jobs_text(self.reply(), PLAIN)
        head, task = [n for n in text.splitlines() if n.strip()][-2:]
        self.assertIn("0065b9d6", head)
        self.assertIn("Print the word BETA and nothing else.", task)
        # It hangs under the state column, clear of the id, so the ids read
        # down the left edge as a column of their own.
        self.assertEqual(task.index("Print"), head.index("ok"))
        self.assertNotIn("…", task)

    def test_an_empty_list_says_so_instead_of_printing_a_header(self) -> None:
        text = cli()._jobs_text({"ok": True, "workspace": {}, "jobs": []}, PLAIN)
        self.assertIn("no jobs dispatched yet", text)


class AuditTests(unittest.TestCase):
    reply = {
        "ok": True, "count": 3, "size_bytes": 143457, "path": "/tmp/audit.jsonl",
        "entries": [
            {"iso": "2026-08-30T22:19:45", "action": "process.spawned",
             "actor": "luna", "ok": True, "why": "claude ask", "pid": 2151215},
            {"iso": "2026-08-30T22:16:27", "action": "settings.set",
             "actor": "cli", "ok": False, "why": "set from the CLI"},
            {"iso": "2026-08-29T09:02:01", "action": "job.collected",
             "actor": "luna", "ok": True},
        ]}

    def test_the_day_is_said_once_and_the_times_hang_under_it(self) -> None:
        text = cli()._audit_text(self.reply, PLAIN)
        self.assertEqual(text.count("2026-08-30"), 1)
        self.assertEqual(text.count("2026-08-29"), 1)
        self.assertIn("22:19:45", text)
        self.assertNotIn("2026-08-30T22:19:45", text)

    def test_a_failed_entry_is_the_only_one_that_takes_colour(self) -> None:
        text = cli()._audit_text(self.reply, COLOURED)
        self.assertEqual(text.count("\033[31m"), 1)
        self.assertIn("\033[31mFAILED\033[0m", text)

    def test_a_pipe_gets_no_escapes(self) -> None:
        self.assertNotIn(ESC, cli()._audit_text(self.reply, PLAIN))


class SettingsRenderTests(unittest.TestCase):
    reply = {
        "ok": True, "path": "/tmp/config.toml", "mode": "0644", "reloads": 0,
        "watching": True,
        "settings": {"memory": {"luna_cap_chars": 3000, "user_cap_chars": 999}},
        "defaults": {"memory": {"luna_cap_chars": 3000, "user_cap_chars": 2000}},
        "schema": [{"section": "memory",
                    "keys": [{"name": "luna_cap_chars", "comment": ""},
                             {"name": "user_cap_chars", "comment": "how big"}]}],
        "secrets": {"present": True, "source": "secrets.env"},
    }

    def test_what_you_changed_is_the_thing_that_stands_out(self) -> None:
        text = cli()._settings_text(self.reply, COLOURED)
        changed = [n for n in text.splitlines() if "user_cap_chars" in n][0]
        default = [n for n in text.splitlines() if "luna_cap_chars" in n][0]
        self.assertIn("\033[1m999\033[0m", changed)
        self.assertNotIn("\033[1m3000", default)

    def test_a_missing_api_key_is_the_one_alarming_thing_in_a_config_dump(self) -> None:
        broken = dict(self.reply, secrets={"present": False, "hint": "put it in"})
        self.assertIn("\033[31mMISSING\033[0m",
                      cli()._settings_text(broken, COLOURED))

    def test_a_comment_is_marked_as_one_with_the_colour_off(self) -> None:
        self.assertIn("# how big", cli()._settings_text(self.reply, PLAIN))

    def test_a_pipe_gets_no_escapes(self) -> None:
        self.assertNotIn(ESC, cli()._settings_text(self.reply, PLAIN))


# =========================================================================
# Bug 1 — a daemon that dies mid-sentence is an outcome, not a traceback
# =========================================================================


class CallTests(unittest.TestCase):
    """`Client.call` against a real socket pair and a hostile far end.

    A socketpair is two file descriptors, not a daemon: nothing here can reach
    lunad, and every reply is one this test wrote by hand.
    """

    def pair(self) -> tuple[Any, socket.socket]:
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        client = cli().Client(path=Path("/nonexistent"), timeout=5.0)
        client.sock = ours
        client.fh = ours.makefile("rwb")
        # Through `__exit__`, which is where an unguarded close would turn the
        # clean exit back into a traceback.
        self.addCleanup(client.__exit__)
        return client, theirs

    def call(self, client: Any) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                client.call("status")
        return int(caught.exception.code), err.getvalue()

    def test_a_whole_reply_is_returned_as_a_dict(self) -> None:
        client, far = self.pair()
        far.sendall(b'{"ok": true, "pong": 1}\n')
        self.assertEqual(client.call("ping"), {"ok": True, "pong": 1})

    def test_a_reply_cut_off_mid_write_is_a_message_not_a_traceback(self) -> None:
        # The bug, exactly: `systemctl restart lunad` while a request is in
        # flight leaves a non-empty line with no newline on it. The EOF guard
        # does not fire and `json.loads` used to raise JSONDecodeError out of
        # the top of the program.
        client, far = self.pair()
        far.sendall(b'{"ok": true, "daemon": {"vers')
        far.shutdown(socket.SHUT_WR)
        code, err = self.call(client)
        self.assertEqual(code, 70)
        self.assertIn("stopped writing partway through its status reply", err)
        self.assertIn("luna log", err)
        self.assertNotIn("Traceback", err)

    def test_a_connection_closed_without_an_answer_says_so(self) -> None:
        client, far = self.pair()
        far.shutdown(socket.SHUT_WR)
        code, err = self.call(client)
        self.assertEqual(code, 70)
        self.assertIn("closed the connection during status", err)

    def test_a_line_that_is_not_json_is_reported_with_what_it_said(self) -> None:
        client, far = self.pair()
        far.sendall(b"Traceback (most recent call last):\n")
        code, err = self.call(client)
        self.assertEqual(code, 70)
        self.assertIn("could not be read as JSON", err)
        self.assertIn("Traceback (most recent call last):", err)

    def test_a_reply_that_is_not_an_object_is_refused(self) -> None:
        client, far = self.pair()
        far.sendall(b"[1, 2, 3]\n")
        code, err = self.call(client)
        self.assertEqual(code, 70)
        self.assertIn("was list, not an object", err)

    def test_undecodable_bytes_are_a_message_too(self) -> None:
        client, far = self.pair()
        far.sendall(b"\xff\xfe not utf-8 at all\n")
        code, err = self.call(client)
        self.assertEqual(code, 70)
        self.assertIn("could not be read as JSON", err)

    def test_a_socket_that_died_before_the_request_went_out(self) -> None:
        client, far = self.pair()
        client.sock.shutdown(socket.SHUT_WR)
        code, err = self.call(client)
        self.assertEqual(code, 70)
        self.assertIn("went away while the status request was being sent", err)


# =========================================================================
# Bug 2 — a global flag that only works in one position
# =========================================================================


class JsonFlagTests(unittest.TestCase):
    def parse(self, argv: list[str]) -> argparse.Namespace:
        args = cli().build_parser().parse_args(argv)
        if not hasattr(args, "json"):
            args.json = False
        return args

    def test_json_is_accepted_after_the_subcommand(self) -> None:
        # `luna ask hello --json` used to be "unrecognized arguments: --json".
        self.assertTrue(self.parse(["ask", "hello", "--json"]).json)

    def test_json_still_works_in_front_of_it(self) -> None:
        self.assertTrue(self.parse(["--json", "ask", "hello"]).json)

    def test_a_subcommand_default_does_not_overwrite_the_global(self) -> None:
        # The argparse trap: a subparser's own `--json` writes its default
        # False over the True the main parser already set, so accepting the
        # flag in both places naively *breaks* the position that used to work.
        for argv in (["--json", "status"], ["--json", "memory", "search", "x"],
                     ["--json", "settings", "set", "a.b", "c"],
                     ["--json", "jobs"], ["--json", "embed", "status"]):
            self.assertTrue(self.parse(argv).json, argv)

    def test_it_is_accepted_after_a_nested_subcommand_too(self) -> None:
        for argv in (["memory", "search", "x", "--json"],
                     ["memory", "--json", "search", "x"],
                     ["settings", "show", "--json"],
                     ["embed", "status", "--json"],
                     ["confirm", "list", "--json"]):
            self.assertTrue(self.parse(argv).json, argv)

    def test_not_asking_for_it_still_means_no(self) -> None:
        for argv in (["status"], ["ask", "hello"], ["memory", "show"]):
            self.assertFalse(self.parse(argv).json, argv)


# =========================================================================
# Bug 3 — a value coerced against the text instead of against the type
# =========================================================================


class ParseValueTests(unittest.TestCase):
    def parse(self, key: str, raw: str, literal: bool = False) -> object:
        return cli()._parse_value(key, raw, literal=literal)

    def test_a_numeric_looking_name_stays_a_string(self) -> None:
        # The bug: this became the integer 42 before anything looked at the
        # declared type, and the daemon then complained about a value the user
        # had not typed.
        self.assertEqual(self.parse("assistant.name", "42"), "42")
        self.assertEqual(self.parse("assistant.name", "true"), "true")

    def test_a_number_key_still_gets_a_number(self) -> None:
        self.assertEqual(self.parse("memory.luna_cap_chars", "3000"), 3000)
        self.assertIsInstance(self.parse("voice.speed", "1.2"), float)

    def test_a_bool_key_takes_the_words_a_person_types(self) -> None:
        for yes in ("true", "TRUE", "yes", "on", "1"):
            self.assertIs(self.parse("ambient.enabled", yes), True)
        for no in ("false", "no", "off", "0"):
            self.assertIs(self.parse("ambient.enabled", no), False)

    def test_string_is_the_escape_for_a_value_that_looks_like_something_else(self) -> None:
        self.assertEqual(self.parse("assistant.name", "42", literal=True), "42")
        self.assertEqual(self.parse("memory.luna_cap_chars", "3000",
                                    literal=True), "3000")

    def test_a_wrong_type_is_refused_here_with_the_key_named(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIs(self.parse("memory.luna_cap_chars", "lots"),
                          cli()._BAD)
            self.assertIs(self.parse("ambient.enabled", "maybe"), cli()._BAD)
        self.assertIn("memory.luna_cap_chars wants a whole number", err.getvalue())
        self.assertIn("ambient.enabled is a true/false setting", err.getvalue())

    def test_a_key_this_client_has_never_heard_of_falls_back_to_guessing(self) -> None:
        # A daemon newer than the client knows keys the local schema does not,
        # and refusing them here would make the client the thing that has to be
        # upgraded first.
        self.assertEqual(self.parse("future.thing", "7"), 7)
        self.assertIs(self.parse("future.thing", "true"), True)
        self.assertEqual(self.parse("future.thing", "hello"), "hello")


# =========================================================================
# Bug 4 — a command that waits on a keyboard nobody is typing at
# =========================================================================


class FakeStdin:
    def __init__(self, tty: bool, text: str = "") -> None:
        self._tty = tty
        self._text = text
        self.reads = 0

    def isatty(self) -> bool:
        return self._tty

    def read(self) -> str:
        self.reads += 1
        return self._text


class NothingToAskTests(unittest.TestCase):
    @contextlib.contextmanager
    def stdin(self, fake: FakeStdin):
        module = cli()
        old = module.sys.stdin
        module.sys.stdin = fake
        try:
            yield
        finally:
            module.sys.stdin = old

    def run_cmd(self, name: str, args: argparse.Namespace) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = getattr(cli(), name)(args)
        return code, err.getvalue()

    def test_ask_with_nothing_at_all_fails_at_once_instead_of_blocking(self) -> None:
        # It used to sit on `stdin.read()` waiting for a terminal that was not
        # sending anything, which is indistinguishable from a hung daemon. If
        # this test hangs, the bug is back; if it reached the socket at all the
        # Client would raise, and it does not, because it never gets there.
        fake = FakeStdin(tty=True)
        with self.stdin(fake):
            code, err = self.run_cmd("cmd_ask", argparse.Namespace(prompt=[]))
        self.assertEqual(code, 2)
        self.assertEqual(fake.reads, 0)
        self.assertIn("nothing to ask", err)
        self.assertIn('luna ask "what is the bar called"', err)

    def test_say_says_how_to_say_something(self) -> None:
        with self.stdin(FakeStdin(tty=True)):
            code, err = self.run_cmd("cmd_say", argparse.Namespace(text=[]))
        self.assertEqual(code, 2)
        self.assertIn("nothing to say", err)
        self.assertIn("| luna say", err)

    def test_dispatch_too(self) -> None:
        with self.stdin(FakeStdin(tty=True)):
            code, _ = self.run_cmd("cmd_dispatch", argparse.Namespace(task=[]))
        self.assertEqual(code, 2)

    def test_a_pipe_is_still_read_because_a_pipe_is_an_answer(self) -> None:
        fake = FakeStdin(tty=False, text="what is the bar called\n")
        with self.stdin(fake):
            self.assertEqual(cli()._from_stdin([], "ask", "eg"),
                             "what is the bar called")
        self.assertEqual(fake.reads, 1)

    def test_an_empty_pipe_is_reported_rather_than_sent(self) -> None:
        err = io.StringIO()
        with self.stdin(FakeStdin(tty=False, text="   \n")):
            with contextlib.redirect_stderr(err):
                self.assertIsNone(cli()._from_stdin([], "ask", "eg"))
        self.assertIn("the pipe was empty", err.getvalue())


# =========================================================================
# --json is a contract with a program, not with a person
# =========================================================================


class FakeClient:
    """A `Client` that answers from a dict. Opens nothing."""

    replies: dict[str, dict] = {}
    seen: list[tuple[str, dict]] = []

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def call(self, op: str, **payload: Any) -> dict:
        FakeClient.seen.append((op, payload))
        return FakeClient.replies[op]


class JsonOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = cli()
        # Colour ON, deliberately: the point is that `--json` is clean even
        # when the program has every reason to believe it is talking to a
        # terminal, because that is the case where a leak would happen.
        self.module.S = COLOURED
        self.addCleanup(setattr, self.module, "S", PLAIN)
        self.old = self.module.Client
        self.module.Client = FakeClient
        self.addCleanup(setattr, self.module, "Client", self.old)
        FakeClient.seen = []

    def run_cmd(self, name: str, **args: Any) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            getattr(self.module, name)(argparse.Namespace(json=True, **args))
        return out.getvalue()

    def test_status_json_is_json_and_nothing_else(self) -> None:
        FakeClient.replies = {"status": status_reply()}
        text = self.run_cmd("cmd_status")
        self.assertNotIn(ESC, text)
        self.assertEqual(json.loads(text)["daemon"]["pid"], 1852972)

    def test_jobs_json_keeps_its_shape(self) -> None:
        FakeClient.replies = {"jobs": {"ok": True, "jobs": [], "workspace": {}}}
        text = self.run_cmd("cmd_jobs", limit=5, output=False, cancel=None)
        self.assertNotIn(ESC, text)
        self.assertEqual(json.loads(text), FakeClient.replies["jobs"])

    def test_settings_json_keeps_its_shape(self) -> None:
        FakeClient.replies = {"settings.get": {"ok": True, "settings": {},
                                               "path": "/tmp/x"}}
        text = self.run_cmd("cmd_settings", set_cmd="show", key=None)
        self.assertNotIn(ESC, text)
        self.assertEqual(json.loads(text)["path"], "/tmp/x")

    def test_a_typed_value_reaches_the_daemon_as_the_declared_type(self) -> None:
        FakeClient.replies = {"settings.set": {"ok": True, "applied": {},
                                               "path": "/tmp/x"}}
        self.run_cmd("cmd_settings", set_cmd="set", key="assistant.name",
                     value="42", string=False)
        op, payload = FakeClient.seen[-1]
        self.assertEqual(op, "settings.set")
        self.assertEqual(payload["value"], "42")

    def test_a_value_refused_by_the_client_never_reaches_the_daemon(self) -> None:
        FakeClient.replies = {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.module.cmd_settings(argparse.Namespace(
                json=False, set_cmd="set", key="memory.luna_cap_chars",
                value="lots", string=False))
        self.assertEqual(code, 2)
        self.assertEqual(FakeClient.seen, [])


# =========================================================================
# The wiring: `luna embed`, and the setting that was a documented no-op
# =========================================================================


class EmbedCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = cli()

    def test_status_is_what_bare_luna_embed_means(self) -> None:
        args = self.module.build_parser().parse_args(["embed"])
        self.assertEqual(args.embed_cmd, "status")
        self.assertIs(args.func, self.module.cmd_embed)

    def test_fetch_and_backfill_are_handed_to_the_module_that_owns_them(self) -> None:
        calls: list[list[str]] = []
        old = embed.cli
        embed.cli = lambda argv: (calls.append(list(argv)), 0)[1]
        self.addCleanup(setattr, embed, "cli", old)
        for cmd, force, expected in (
                ("fetch", False, ["embed", "fetch"]),
                ("fetch", True, ["embed", "fetch", "--force"]),
                ("backfill", True, ["embed", "backfill", "--force"])):
            code = self.module.cmd_embed(argparse.Namespace(
                json=False, embed_cmd=cmd, force=force))
            self.assertEqual(code, 0)
            self.assertEqual(calls[-1], expected)

    def test_force_is_only_offered_where_it_means_something(self) -> None:
        parse = self.module.build_parser().parse_args
        self.assertTrue(parse(["embed", "fetch", "--force"]).force)
        self.assertFalse(parse(["embed", "status"]).force)

    def absent(self) -> dict:
        return {"model": "all-MiniLM-L6-v2", "licence": "Apache-2.0",
                "dim": 384, "dir": "/tmp/models", "python": "/tmp/python",
                "present": False, "available": False, "enabled": False,
                "ready": False, "broken": False, "power": True}

    def test_a_missing_model_is_a_state_with_a_remedy_not_an_error(self) -> None:
        text = self.module._embed_status_text(self.absent(), PLAIN)
        self.assertIn("not fetched", text)
        self.assertIn("keyword-only", text)
        self.assertIn("luna embed fetch", text)
        self.assertNotIn(ESC, text)

    def test_a_model_that_is_there_but_switched_off_names_the_switch(self) -> None:
        st = dict(self.absent(), present=True, available=True, enabled=False)
        text = self.module._embed_status_text(st, PLAIN)
        self.assertIn("luna settings set memory.semantic_recall true", text)

    def test_a_model_that_is_on_offers_the_backfill(self) -> None:
        st = dict(self.absent(), present=True, available=True, enabled=True)
        text = self.module._embed_status_text(st, PLAIN)
        self.assertIn("luna embed backfill", text)

    def test_absence_is_the_one_thing_on_the_screen_that_takes_colour(self) -> None:
        text = self.module._embed_status_text(self.absent(), COLOURED)
        self.assertIn("\033[31mnot fetched\033[0m", text)


class SemanticRecallKeyTests(TempMemoryCase):
    """`[memory] semantic_recall` — a switch that was not connected to a wire.

    `Embedder.enabled()` has always read it and treated `None` as on. Unknown
    keys return `None` from `Settings.get`, so before it was registered the
    setting could not be turned *off*: writing `false` into the file was
    dropped by the loader and `settings.set` refused the key outright.
    """

    def test_the_schema_knows_the_key(self) -> None:
        _, key = settings_mod.find("memory.semantic_recall")
        self.assertEqual(key.kind, "bool")
        self.assertIs(key.default, True)
        self.assertIn("meaning", key.comment)

    def test_it_reads_back_as_true_rather_than_none(self) -> None:
        # None was the bug: indistinguishable from "no such setting", which is
        # what made it a documented no-op.
        self.assertIs(self.settings.get("memory.semantic_recall"), True)

    def test_turning_it_off_now_survives_the_write(self) -> None:
        self.settings.set("memory.semantic_recall", False)
        self.assertIs(self.settings.get("memory.semantic_recall"), False)
        reloaded = settings_mod.Settings(self.settings.path)
        self.addCleanup(reloaded.stop_watching)
        self.assertIs(reloaded.get("memory.semantic_recall"), False)

    def test_the_embedder_honours_it(self) -> None:
        emb = embed.Embedder()
        emb.available = lambda: True          # type: ignore[assignment]
        self.assertTrue(emb.enabled())
        self.settings.set("memory.semantic_recall", False)
        self.assertFalse(emb.enabled())

    def test_a_typed_false_from_the_cli_is_a_bool_and_not_the_word(self) -> None:
        self.assertIs(cli()._parse_value("memory.semantic_recall", "false"),
                      False)


if __name__ == "__main__":
    unittest.main()
