"""Luna delegating for herself: the loop from her shell and back to her memory.

The user's requirement, in their words: "if I say do X, it's going to give a
Luna agent its own task, and it will go do that, and then it will report back
to me whenever everything is done, and then update memory accordingly."

The mechanism is the cheapest one that could work. She has a shell now, and
`luna dispatch` already exists, is already audited and already returns
immediately — so she invokes it, like anybody else would. Nothing new was built
to carry it. What had to be built was the three things that make it real:

1. she has to be *told* it exists, with the exact syntax (persona.py);
2. the call has to work from inside a live ask, which means the daemon must be
   able to answer a request that a request it is already serving provoked; and
3. the result has to come back — a toast, which existed, and an episode, which
   did not.

(3) is the one that makes delegation compound rather than evaporate: without
it a finding sat in `jobs/<id>/output.txt` where Luna would never read it, and
the same question a week later got the same job dispatched again.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from lunad import agent, config, dispatch, persona, settings as settings_mod
from lunad.server import Daemon, LunaServer, ReportingDispatcher

from ._support import FakeHyprland, TempMemoryCase


class OperatingNotesCase(unittest.TestCase):
    """What her prompt now says she can do — and what it must not lose.

    The persona spec has said "she does small things herself; anything with
    real depth she hands to Sol" since draft 1, and it was aspiration: the
    prompt underneath it said she had no tools and never named a command. These
    assertions are the difference between a rule and a mechanism.
    """

    def notes(self) -> str:
        return persona.build_system_prompt("tier one", spec=persona.load_spec(),
                                           name="Luna", specialist="Sol")

    def test_the_lie_about_having_no_tools_is_gone(self) -> None:
        prompt = self.notes()
        self.assertNotIn("no tools", prompt)
        self.assertNotIn("You cannot read files", prompt)

    def test_she_is_told_she_has_a_shell_files_and_the_web(self) -> None:
        # Matched on fragments that do not straddle a line break — the block is
        # hand-wrapped prose, and a test that asserts on a wrapped phrase fails
        # the next time somebody reflows a paragraph.
        prompt = self.notes().lower()
        for claim in ("shell on this machine", "write files",
                      "run commands", "reach the web"):
            with self.subTest(claim=claim):
                self.assertIn(claim, prompt)

    def test_the_dispatch_syntax_is_exact_and_absolute(self) -> None:
        """Not `luna dispatch`: the absolute path.

        Her shell is lunad's, and lunad is a systemd --user service whose PATH
        need not contain ~/.local/bin. A bare `luna` would work when tested
        from the user's terminal and fail from hers.
        """
        prompt = self.notes()
        self.assertIn(f'{config.LUNA_CLI} dispatch --to sol "', prompt)
        self.assertTrue(Path(config.LUNA_CLI).is_file())

    def test_the_look_syntax_is_there_too(self) -> None:
        self.assertIn(f'{config.LUNA_CLI} look "', self.notes())

    def test_she_is_told_when_to_delegate_and_when_not_to(self) -> None:
        prompt = self.notes().lower()
        self.assertIn("depth", prompt)
        self.assertIn("yourself", prompt)

    def test_she_is_told_not_to_sit_and_poll(self) -> None:
        # She is notified and the result lands in memory, so waiting on a job
        # would burn a whole conversational turn doing nothing.
        self.assertIn("poll", self.notes().lower())

    def test_every_persona_rule_survived(self) -> None:
        """The spec is the user's document and none of it was softened."""
        prompt = self.notes()
        for rule in ("Never open with praise",
                     "at most TWO objections",
                     "does NOT re-litigate",
                     "Spoken replies are SHORT",
                     "British, dry, unhurried",
                     "interrogate before executing".title().replace(
                         "Interrogate Before Executing",
                         "Core stance: interrogate before executing")):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)

    def test_the_specialist_is_named_from_settings(self) -> None:
        prompt = persona.build_system_prompt("t", spec="SPEC", name="Luna",
                                             specialist="Hermes")
        self.assertIn("Hermes", prompt)

    def test_a_toolless_agent_gets_the_honest_notes_instead(self) -> None:
        """The other half of not lying.

        claude's ask path still passes `--tools ""`. Telling that model it has
        a shell would recreate the original bug pointing the other way: an
        assistant who says she will go and check, and then guesses.
        """
        prompt = persona.build_system_prompt("t", spec="SPEC", name="Luna",
                                             tools=False)
        self.assertIn("no tools", prompt)
        self.assertNotIn("dispatch --to sol", prompt)

    def test_which_notes_are_used_is_the_adapters_call(self) -> None:
        self.assertTrue(agent.CodexAdapter().ask_has_tools)
        self.assertFalse(agent.ClaudeAdapter().ask_has_tools)


class JobEpisodeCase(TempMemoryCase):
    """A finished job comes back: a toast, and a line in tier 2."""

    def dispatcher(self, **kw: Any) -> ReportingDispatcher:
        self.recorded: list[dispatch.Job] = []
        return ReportingDispatcher(jobs_dir=self.root / "jobs",
                                   hypr=FakeHyprland(), audit=self.audit,
                                   agent_bin="/bin/true", terminal="/bin/bash",
                                   **kw)

    def finished_job(self, output: str = "The bar widget leaks a texture.",
                     state: str = "finished",
                     exit_code: int = 0) -> dispatch.Job:
        job_dir = self.root / "jobs" / "abc12345"
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "output.txt").write_text(output, encoding="utf-8")
        return dispatch.Job(id="abc12345", task="find out why the bar leaks",
                            to="sol", dir=job_dir, state=state,
                            exit_code=exit_code)

    def test_the_result_is_written_into_tier_two(self) -> None:
        memory = self.memory()
        seen: list[dispatch.Job] = []
        d = self.dispatcher(on_finished=seen.append)
        self.addCleanup(d.close)
        d.notify_finished(self.finished_job())
        self.assertEqual([j.id for j in seen], ["abc12345"])

    def test_the_daemon_records_it_as_an_exchange(self) -> None:
        daemon = self.daemon()
        daemon._job_finished(self.finished_job())
        episode = daemon.memory.episodes.recent(1)[0]
        self.assertIn("find out why the bar leaks", episode.user_text)
        self.assertIn("abc12345", episode.user_text)
        self.assertIn("Sol", episode.user_text)
        self.assertEqual(episode.luna_text, "The bar widget leaks a texture.")
        self.assertEqual(episode.surface, "dispatch")

    def test_the_result_is_findable_by_ordinary_recall(self) -> None:
        """The whole point. Delegation has to compound.

        Otherwise the finding lives in jobs/<id>/output.txt, Luna never reads
        it, and the same question next week dispatches the same job again.
        """
        daemon = self.daemon()
        daemon._job_finished(self.finished_job())
        self.assertIn("texture",
                      daemon.memory.recall_block("why does the bar leak"))

    def test_a_failed_job_is_remembered_as_failed(self) -> None:
        daemon = self.daemon()
        daemon._job_finished(self.finished_job(output="could not build",
                                               state="failed", exit_code=2))
        text = daemon.memory.episodes.recent(1)[0].luna_text
        self.assertIn("failed", text)
        self.assertIn("exit 2", text)

    def test_a_job_that_said_nothing_still_lands(self) -> None:
        daemon = self.daemon()
        daemon._job_finished(self.finished_job(output=""))
        self.assertTrue(daemon.memory.episodes.recent(1)[0].luna_text)

    def test_a_memory_fault_cannot_turn_a_finished_job_into_a_failed_one(self) -> None:
        def explode(_job: dispatch.Job) -> None:
            raise RuntimeError("the episode store is on fire")

        d = self.dispatcher(on_finished=explode)
        self.addCleanup(d.close)
        # False because the notifier is the process-wide sentinel and cannot
        # resolve — but it *returned*, which is the assertion: the recording
        # blew up and the job is still finished.
        self.assertFalse(d.notify_finished(self.finished_job()))

    def test_the_toast_is_sent_before_anything_can_go_wrong_recording(self) -> None:
        order: list[str] = []

        class Watching(ReportingDispatcher):
            def notify_finished(inner, job):  # noqa: N805
                order.append("notify")
                return super().notify_finished(job)

        d = Watching(jobs_dir=self.root / "jobs", hypr=FakeHyprland(),
                     audit=self.audit, agent_bin="/bin/true",
                     terminal="/bin/bash",
                     on_finished=lambda _j: order.append("record"))
        self.addCleanup(d.close)
        d.notify_finished(self.finished_job())
        self.assertEqual(order, ["notify", "record"])

    def daemon(self) -> Daemon:
        dispatcher = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                         hypr=FakeHyprland(), audit=self.audit,
                                         sol_memory_dir=self.root / "sol",
                                         terminal="/bin/bash",
                                         agent_bin="/bin/true")
        d = Daemon(agent_name="codex", memory=self.memory(),
                   sol_memory=self.sol_memory(), audit=self.audit,
                   dispatcher=dispatcher)
        d.speech.close()
        d.speech = _Mute()
        self.addCleanup(d.close)
        return d


class DispatcherAgentCase(TempMemoryCase):
    """The dispatcher runs the brain Luna runs, not the desktop's default."""

    def test_the_daemon_hands_its_agent_to_the_dispatcher(self) -> None:
        """Without this every delegated job would have been written in the
        wrong CLI's flags.

        `Dispatcher` falls back to `read_default_agent()` — the *desktop's*
        ~/.config/omarchy/defaults/agent, which says "claude" on this machine —
        while Luna herself now runs codex. Nothing would have crashed. The jobs
        would simply all have failed, in a hidden workspace.
        """
        d = Daemon(agent_name="codex", memory=self.memory(),
                   sol_memory=self.sol_memory(), audit=self.audit)
        self.addCleanup(d.close)
        self.assertEqual(d.dispatcher.agent_name, "codex")
        self.assertIsInstance(d.dispatcher.adapter, agent.CodexAdapter)
        self.assertIsInstance(d.dispatcher, ReportingDispatcher)

    def test_a_dispatched_job_runs_sols_model(self) -> None:
        d = ReportingDispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                agent_name="codex", agent_bin="/fake/codex",
                                terminal="/bin/bash")
        self.addCleanup(d.close)
        job = dispatch.Job(id="deadbeef", task="dig", to="sol",
                           dir=self.root / "jobs" / "deadbeef")
        script = d._runner_script(job, timeout=60.0, linger=1.0)
        self.assertIn(f"-m {config.CODEX_DISPATCH_MODEL}", script)
        self.assertIn("gpt-5.6-sol", script)
        self.assertNotIn("gpt-5.6-luna", script)


class ReentrancyCase(TempMemoryCase):
    """The check the brief asked for rather than assumed.

    Luna calls `luna dispatch` from her own shell *while the daemon is still
    inside her ask*. If the daemon serialised requests — a single accept loop,
    or a lock around `Daemon.dispatch` — that call would block on a thread that
    is itself blocked waiting for the agent to finish, and the whole thing
    would wedge until the ask timed out.

    It does not, and this is what proves it: a real Unix socket, a real second
    connection opened from inside the adapter, and the second reply read back
    before the first one is produced. Verified rather than argued from
    `ThreadingUnixStreamServer`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.sock_path = self.root / "luna.sock"
        dispatcher = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                         hypr=FakeHyprland(), audit=self.audit,
                                         sol_memory_dir=self.root / "sol",
                                         terminal="/bin/bash",
                                         agent_bin="/bin/true")
        self.daemon = Daemon(agent_name="codex", memory=self.memory(),
                             sol_memory=self.sol_memory(), audit=self.audit,
                             dispatcher=dispatcher)
        self.daemon.speech.close()
        self.daemon.speech = _Mute()
        self.daemon.adapter = _CallsBack(self)
        self.server = LunaServer(self.sock_path, self.daemon)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.daemon.close()

    def call(self, timeout: float = 10.0, **request: Any) -> dict:
        request.setdefault("id", uuid.uuid4().hex[:8])
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(self.sock_path))
        try:
            fh = s.makefile("rwb")
            fh.write((json.dumps(request) + "\n").encode())
            fh.flush()
            return json.loads(fh.readline())
        finally:
            s.close()

    def test_an_ask_can_call_back_into_the_daemon_without_deadlocking(self) -> None:
        started = time.monotonic()
        resp = self.call(op="ask", prompt="look into the bar leak", timeout=8.0)
        elapsed = time.monotonic() - started
        self.assertTrue(resp["ok"], resp)
        # The inner call was answered while the outer one was still open. If
        # requests were serialised this would have hung until a socket timeout
        # rather than returning in milliseconds.
        self.assertLess(elapsed, 8.0)
        self.assertIsNotNone(self.daemon.adapter.inner)
        self.assertTrue(self.daemon.adapter.inner["ok"],
                        self.daemon.adapter.inner)

    def test_the_inner_call_really_was_a_dispatch(self) -> None:
        self.call(op="ask", prompt="look into the bar leak", timeout=8.0)
        inner = self.daemon.adapter.inner
        self.assertIn("id", inner)
        # The terminal is /bin/bash rather than foot, so the job genuinely
        # started and genuinely has a job id to report back with.
        self.assertIn("announce", inner)
        self.assertIn(settings_mod.specialist_name(), inner["announce"])


class _CallsBack(agent.BaseAdapter):
    """An "agent" that does what Luna is now told to do: shells back in.

    Standing in for `codex` running
    `~/Work/luna/bin/luna dispatch --to sol "..."`, minus the subprocess. The
    socket round trip is real, which is the part under test.
    """

    name = "calls-back"
    ask_has_tools = True

    def __init__(self, case: ReentrancyCase) -> None:
        self.case = case
        self.inner: dict[str, Any] | None = None

    def available(self) -> tuple[bool, str]:
        return True, "fake"

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> agent.AgentReply:
        self.inner = self.case.call(op="dispatch", to="sol",
                                    task="find out why the bar leaks",
                                    timeout=5.0)
        return agent.AgentReply(text="Enrolled Sol on it.", agent=self.name,
                                wall_ms=1)


class _Mute:
    speaking = False

    def say(self, text: str, wait: bool = False, timeout: float = 0.0):
        return {"spoken": text, "sentences": 1, "id": "x", "cancelled": False}

    def cancel(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"loaded": False, "speaking": False, "counters": {}}

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
