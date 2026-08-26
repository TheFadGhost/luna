"""The codex adapter — argv, event parsing, sessions, and the persona problem.

codex 0.149.1 has no ``--append-system-prompt`` and no ``--system-prompt``.
Everything unusual in :class:`lunad.agent.CodexAdapter` follows from that one
fact, so most of what is asserted here is really one question asked in several
ways: does the persona still get in, and can we tell when it did not?

The end-to-end tests spawn a real fake-codex script through the real
``safety.spawn``, following the convention in ``test_dispatch.py``: the process
handling is never mocked, only the program on the other end of it.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from lunad import agent, config, dispatch

from ._support import FakeHyprland, TempMemoryCase

PERSONA = "You are Luna.\nBe brief.\n\"Quoted\" and `backticked` and \\ backslashed."


def events(text: str = "Fine.", thread: str = "01a0-thread",
           usage: dict | None = None) -> str:
    lines = [
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "item_0", "type": "agent_message", "text": text}},
        {"type": "turn.completed",
         "usage": usage or {"input_tokens": 15862, "cached_input_tokens": 11008,
                            "cache_write_input_tokens": 0, "output_tokens": 159,
                            "reasoning_output_tokens": 99}},
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


class ArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = agent.CodexAdapter()
        self.a.binary = lambda: "/fake/codex"          # type: ignore[method-assign]

    def test_the_subcommand_is_exec_not_dash_p(self):
        """`-p` on codex means --profile. Reaching for claude's flag here
        would silently select a config profile instead of print mode."""
        argv = self.a.build_argv(PERSONA)
        self.assertEqual(argv[:3], ["/fake/codex", "exec", "-"])

    def test_there_is_no_system_prompt_flag_so_persona_rides_a_config_override(self):
        argv = self.a.build_argv(PERSONA)
        for absent in ("--append-system-prompt", "--system-prompt"):
            self.assertNotIn(absent, argv)
        override = argv[argv.index("-c") + 1]
        self.assertTrue(override.startswith(config.CODEX_PERSONA_KEY + "="))
        self.assertIn("Be brief.", override)

    def test_the_override_keeps_the_persona_byte_for_byte(self):
        """Quotes, backticks and backslashes must survive: codex only falls
        back to a raw literal when the value fails to parse as TOML."""
        override = self.a._instructions_override(PERSONA)
        self.assertEqual(override.split("=", 1)[1], "\n" + PERSONA)

    def test_the_override_cannot_be_mistaken_for_toml(self):
        """The leading newline is load-bearing: it pins the literal path open
        for any persona, including one that happened to look like TOML."""
        for hostile in ('"looks like a toml string"', "key = value", "123"):
            value = self.a._instructions_override(hostile).split("=", 1)[1]
            with self.assertRaises(tomllib.TOMLDecodeError):
                tomllib.loads(f"x = {value}")

    def test_the_prompt_is_not_in_argv(self):
        """It goes on stdin via `-`: a recall block makes it unbounded, and a
        prompt starting with `-` would be read as a flag."""
        argv = self.a.build_argv(PERSONA)
        self.assertNotIn("What is 2+2?", argv)
        self.assertIn("-", argv)

    def test_ask_is_read_only(self):
        argv = self.a.build_argv(PERSONA, mode="ask")
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_dispatch_bypasses_the_sandbox(self):
        argv = self.a.build_argv(PERSONA, mode="dispatch")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("-s", argv)

    def test_the_sandbox_is_configurable(self):
        argv = self.a.build_argv(PERSONA, sandbox="workspace-write")
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")

    def test_luna_does_not_inherit_the_users_codex_setup(self):
        argv = self.a.build_argv(PERSONA)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)

    def test_resume_uses_the_subcommand_and_the_long_way_round_to_a_sandbox(self):
        """`codex exec resume` accepts neither -s nor -C. Passing them anyway
        would abort the turn, so the sandbox goes through -c instead."""
        argv = self.a.build_argv(PERSONA, resume="01a0-thread")
        self.assertEqual(argv[1:4], ["exec", "resume", "01a0-thread"])
        self.assertNotIn("-s", argv)
        self.assertNotIn("-C", argv)
        self.assertIn("sandbox_mode=read-only", argv)

    def test_a_fresh_turn_names_a_working_root(self):
        argv = self.a.build_argv(PERSONA)
        self.assertEqual(argv[argv.index("-C") + 1], str(config.AGENT_CWD))

    def test_the_model_is_passed_when_asked_for(self):
        self.assertNotIn("-m", self.a.build_argv(PERSONA))
        argv = self.a.build_argv(PERSONA, model="gpt-5")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5")

    def test_dispatch_argv_gives_the_job_its_directory(self):
        argv = self.a.dispatch_argv('"$JOB"', '"$JOB/system.txt"',
                                    add_dirs=("/sol",), binary="/fake/codex")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("/sol", argv)
        self.assertTrue(any("system.txt" in tok for tok in argv))


class ShellLineTests(unittest.TestCase):
    def test_a_flag_adopts_its_value_but_not_another_flag(self):
        lines = agent.shell_lines(["/bin/x", "exec", "-", "--json",
                                   "-s", "read-only", "-c", "k=v"])
        self.assertEqual(lines, ["/bin/x", "exec", "-", "--json",
                                 "-s read-only", "-c k=v"])


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = agent.CodexAdapter()

    def test_a_good_stream_yields_text_thread_and_tokens(self):
        reply = self.a.parse_output(events("Fine."), "", 0, 1200)
        self.assertEqual(reply.text, "Fine.")
        self.assertEqual(reply.session_id, "01a0-thread")
        self.assertEqual(reply.usage["output_tokens"], 159)
        self.assertEqual(reply.num_turns, 1)
        self.assertEqual(reply.agent, "codex")

    def test_a_subscription_call_reports_no_dollars(self):
        """codex here is on a ChatGPT plan. A fabricated price would corrupt
        the daemon's running total, which only ever adds a truthy cost."""
        reply = self.a.parse_output(events(), "", 0, 1)
        self.assertIsNone(reply.cost_usd)
        self.assertEqual(reply.billing, "subscription")
        self.assertIn("input_tokens", reply.to_dict()["usage"])

    def test_human_progress_lines_are_tolerated(self):
        noisy = "Reading additional input from stdin...\n" + events("Fine.")
        self.assertEqual(self.a.parse_output(noisy, "", 0, 1).text, "Fine.")

    def test_the_last_message_of_a_turn_wins(self):
        stream = events("first") + json.dumps(
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "second"}}) + "\n"
        self.assertEqual(self.a.parse_output(stream, "", 0, 1).text, "second")

    def test_silence_on_a_clean_exit_is_malformed_not_empty(self):
        with self.assertRaises(agent.AgentMalformedOutput):
            self.a.parse_output("", "", 0, 1)

    def test_unparseable_output_is_malformed(self):
        with self.assertRaises(agent.AgentMalformedOutput) as caught:
            self.a.parse_output("<html>504 Gateway Timeout</html>", "", 0, 1)
        self.assertIn("504", caught.exception.to_dict()["sample"])

    def test_events_without_a_message_are_malformed(self):
        stream = json.dumps({"type": "thread.started", "thread_id": "x"}) + "\n"
        with self.assertRaises(agent.AgentMalformedOutput):
            self.a.parse_output(stream, "", 0, 1)

    def test_a_turn_that_failed_is_a_failure(self):
        stream = (json.dumps({"type": "thread.started", "thread_id": "x"}) + "\n"
                  + json.dumps({"type": "turn.failed",
                                "error": {"message": "model not supported"}}) + "\n")
        with self.assertRaises(agent.AgentFailed) as caught:
            self.a.parse_output(stream, "", 1, 1)
        self.assertIn("model not supported", str(caught.exception))

    def test_a_refused_resume_is_a_failure_the_daemon_can_retry(self):
        """Exit 1, empty stdout, reason only on stderr — the shape codex uses
        when a thread id has aged out. AgentFailed is what makes the server
        drop the session and start a clean one."""
        with self.assertRaises(agent.AgentFailed) as caught:
            self.a.parse_output("", "Error: no rollout found for thread id", 1, 1)
        self.assertIn("no rollout found", str(caught.exception))

    def test_output_last_message_is_the_second_witness(self):
        """If the stream loses the message but -o caught it, use it."""
        stream = (json.dumps({"type": "thread.started", "thread_id": "x"}) + "\n"
                  + json.dumps({"type": "turn.completed", "usage": {}}) + "\n")
        reply = self.a.parse_output(stream, "", 0, 1, last_message="Recovered.\n")
        self.assertEqual(reply.text, "Recovered.")


class ProfileTests(unittest.TestCase):
    """The `luna` codex profile, for the user's own `codex -p luna`."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="luna-codex-home-"))
        self.a = agent.CodexAdapter()

    def test_a_profile_is_its_own_file_not_a_table_in_the_users_config(self):
        """In 0.149.1 `-p x` layers $CODEX_HOME/x.config.toml. That is what
        makes writing it safe: ~/.codex/config.toml is never opened."""
        path = self.a.profile_path(self.home)
        self.assertEqual(path.name, "luna.config.toml")
        self.a.write_profile(PERSONA, self.home)
        self.assertFalse((self.home / "config.toml").exists())

    def test_the_profile_is_valid_toml_and_round_trips_the_persona(self):
        """Unlike the -c path, this one really is parsed as TOML."""
        path = self.a.write_profile(PERSONA, self.home)
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(parsed[config.CODEX_PERSONA_KEY].strip(), PERSONA.strip())

    def test_an_existing_profile_is_backed_up_before_it_is_replaced(self):
        path = self.a.profile_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# the user's own\n", encoding="utf-8")
        self.a.write_profile(PERSONA, self.home)
        backup = path.with_suffix(path.suffix + ".luna-backup")
        self.assertEqual(backup.read_text(encoding="utf-8"), "# the user's own\n")


class AvailabilityTests(unittest.TestCase):
    def test_the_adapter_is_registered_and_no_longer_a_stub(self):
        self.assertIsInstance(agent.get_adapter("codex"), agent.CodexAdapter)

    def test_a_missing_login_is_reported_before_the_first_call(self):
        a = agent.CodexAdapter()
        a.binary = lambda: "/fake/codex"                # type: ignore[method-assign]
        old = config.CODEX_AUTH
        config.CODEX_AUTH = Path("/nonexistent/auth.json")
        try:
            ok, detail = a.available()
        finally:
            config.CODEX_AUTH = old
        self.assertFalse(ok)
        self.assertIn("not logged in", detail)


class FakeCodexTests(TempMemoryCase):
    """A real subprocess, through the real safety.spawn, running a fake codex."""

    def setUp(self) -> None:
        super().setUp()
        self.cwd = self.root / "agent-cwd"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._old_cwd = config.AGENT_CWD
        config.AGENT_CWD = self.cwd
        self.addCleanup(setattr, config, "AGENT_CWD", self._old_cwd)
        self.fake = self.root / "fake-codex"

    def write_fake(self, body: str) -> agent.CodexAdapter:
        self.fake.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        self.fake.chmod(0o755)
        a = agent.CodexAdapter()
        a.binary = lambda: str(self.fake)               # type: ignore[method-assign]
        return a

    def test_a_whole_turn_end_to_end(self):
        stream_file = self.root / "stream.jsonl"
        stream_file.write_text(events("Fine."), encoding="utf-8")
        a = self.write_fake(
            "cat > /dev/null\n"
            # -o is the last argument we care about; find it the way codex does
            "while [ $# -gt 0 ]; do [ \"$1\" = -o ] && out=$2; shift; done\n"
            f"cat {stream_file}\n"
            "printf 'Fine.' > \"$out\"\n")
        reply = a.ask("hello", PERSONA, timeout=30)
        self.assertEqual(reply.text, "Fine.")
        self.assertEqual(reply.billing, "subscription")
        self.assertGreaterEqual(reply.wall_ms, 0)

    def test_the_prompt_really_does_arrive_on_stdin(self):
        a = self.write_fake(
            "got=$(cat)\n"
            "while [ $# -gt 0 ]; do [ \"$1\" = -o ] && out=$2; shift; done\n"
            "printf '{\"type\":\"item.completed\",\"item\":"
            "{\"type\":\"agent_message\",\"text\":\"%s\"}}\\n' \"$got\"\n"
            "printf 'x' > \"$out\"\n")
        self.assertEqual(a.ask("ping-on-stdin", PERSONA, timeout=30).text,
                         "ping-on-stdin")

    def test_a_nonzero_exit_with_no_events_becomes_agentfailed(self):
        a = self.write_fake("cat > /dev/null\n"
                            "echo 'Error: no rollout found' >&2\nexit 1\n")
        with self.assertRaises(agent.AgentFailed):
            a.ask("hi", PERSONA, timeout=30)

    def test_a_clean_exit_with_junk_becomes_malformed(self):
        a = self.write_fake("cat > /dev/null\necho 'not json at all'\n")
        with self.assertRaises(agent.AgentMalformedOutput):
            a.ask("hi", PERSONA, timeout=30)

    def test_the_scratch_file_is_not_left_behind(self):
        a = self.write_fake("cat > /dev/null\necho 'not json'\n")
        with self.assertRaises(agent.AgentMalformedOutput):
            a.ask("hi", PERSONA, timeout=30)
        self.assertEqual(list(self.cwd.glob("codex-last-*")), [])


class CodexDispatchTests(TempMemoryCase):
    """A dispatched job written in codex's flags rather than claude's."""

    def script(self) -> str:
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                terminal="/bin/bash", agent_name="codex",
                                agent_bin="/fake/codex",
                                sol_memory_dir=self.root / "sol")
        job = dispatch.Job(id="x", task="t", to="sol", dir=self.root)
        return d._runner_script(job, timeout=60, linger=0)

    def test_a_dispatched_codex_gets_its_tools(self):
        script = self.script()
        self.assertIn("exec", script)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", script)
        self.assertNotIn("--append-system-prompt", script)

    def test_the_persona_is_read_from_the_job_directory(self):
        self.assertIn(f'{config.CODEX_PERSONA_KEY}=$(cat "$JOB/system.txt")',
                      self.script())

    def test_sol_still_gets_his_own_memory_directory(self):
        self.assertIn(str(self.root / "sol"), self.script())


if __name__ == "__main__":
    unittest.main()
