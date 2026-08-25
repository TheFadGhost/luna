"""Workspace dispatch.

The compositor is faked (see ``_support.FakeHyprland``) but the *process* half
is real: these tests spawn actual children through ``safety.spawn``, wait on
them, and read the exit codes and output back off disk. That is the half that
can silently rot, and it is the half the firewall depends on.

The terminal is stubbed out with ``/bin/bash`` in place of ``foot``, so the
runner script itself is executed for real — pipefail, the exit file, the
linger — without needing a Wayland session.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from lunad import config, dispatch, persona, safety

from ._support import FakeHyprland, TempMemoryCase


class DispatcherCase(TempMemoryCase):
    """A dispatcher whose terminal is bash and whose agent is a shell script."""

    def setUp(self) -> None:
        super().setUp()
        safety.set_audit_hook(self.audit.hook)
        self.hypr = FakeHyprland()
        self.fake_agent = self.root / "fake-agent"
        self.fake_agent.write_text(
            "#!/bin/bash\n"
            "# Stands in for `claude -p`: echoes the prompt it was given on\n"
            "# stdin, then exits with whatever LUNA_TEST_RC says.\n"
            "cat\n"
            "exit ${LUNA_TEST_RC:-0}\n", encoding="utf-8")
        self.fake_agent.chmod(0o755)

    def dispatcher(self, **kw) -> dispatch.Dispatcher:
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs", hypr=self.hypr,
                                audit=self.audit, terminal="/bin/bash",
                                agent_bin=str(self.fake_agent),
                                sol_memory_dir=self.root / "memory" / "sol",
                                **kw)
        # `foot --app-id X --title Y -- /bin/bash run.sh` becomes
        # `bash -c 'exec "$5"' _ ... run.sh` in the tests: same script, no
        # terminal. Only the argv shape is faked, never the process handling.
        real_spawn = d.spawn

        def spawn_without_a_terminal(argv, **popen_kw):
            script = argv[-1]
            return real_spawn(["/bin/bash", script], **popen_kw)

        d.spawn = spawn_without_a_terminal
        self.addCleanup(d.close)
        return d

    def run_job(self, dispatcher: dispatch.Dispatcher, task: str,
                to: str = "worker", timeout: float = 30.0) -> dispatch.Job:
        job = dispatcher.dispatch(task, to, timeout=timeout, linger=0)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and job.state == "running":
            time.sleep(0.05)
        self.assertNotEqual(job.state, "running", "the job never finished")
        return job


class HappyPathTests(DispatcherCase):
    def test_a_job_runs_and_reports_success(self):
        d = self.dispatcher()
        job = self.run_job(d, "write the proof file")
        self.assertEqual(job.state, "finished")
        self.assertEqual(job.exit_code, 0)
        self.assertIn("write the proof file", job.read_output())

    def test_the_pid_is_tracked_while_the_job_runs(self):
        d = self.dispatcher()
        job = d.dispatch("sleep a little", timeout=30, linger=1)
        self.addCleanup(_stop_quietly, d, job)
        self.assertIsNotNone(job.pid)
        self.assertTrue(safety.may_signal(job.pid),
                        "a job Luna spawned must be signallable")
        rec = self.ledger.get(job.pid)
        self.assertEqual(rec.job_id, job.id)
        self.assertEqual(rec.kind, "dispatch")
        self.assertTrue(rec.durable,
                        "a dispatched job outlives the daemon, so its record "
                        "must be fsync'd")

    def test_a_failing_job_is_reported_as_failed_with_its_exit_code(self):
        import os
        os.environ["LUNA_TEST_RC"] = "3"
        self.addCleanup(os.environ.pop, "LUNA_TEST_RC", None)
        d = self.dispatcher()
        job = self.run_job(d, "fail please")
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.exit_code, 3)

    def test_the_exit_code_is_written_to_disk_as_well_as_returned(self):
        d = self.dispatcher()
        job = self.run_job(d, "anything")
        self.assertEqual((job.dir / "exit").read_text().strip(), "0")

    def test_the_job_directory_holds_the_task_and_the_system_prompt(self):
        d = self.dispatcher()
        job = self.run_job(d, "a distinctive task string")
        self.assertIn("a distinctive task string",
                      (job.dir / "task.txt").read_text())
        self.assertTrue((job.dir / "system.txt").read_text().strip())

    def test_the_workspace_rule_is_installed_once_per_compositor(self):
        d = self.dispatcher()
        self.run_job(d, "one")
        self.run_job(d, "two")
        self.assertEqual(self.hypr.rules, 2, "checked each time")
        # The idempotence lives in the compositor's Lua global, which the fake
        # models by answering "added" once and "present" afterwards.

    def test_an_empty_task_is_refused_before_anything_is_spawned(self):
        d = self.dispatcher()
        with self.assertRaises(dispatch.DispatchError):
            d.dispatch("   ")
        self.assertEqual(len(self.ledger), 0)

    def test_an_unknown_delegate_is_refused(self):
        d = self.dispatcher()
        with self.assertRaises(dispatch.DispatchError) as caught:
            d.dispatch("something", to="mallory")
        self.assertIn("sol", str(caught.exception))


class JobListingTests(DispatcherCase):
    def test_jobs_are_listed_newest_first(self):
        d = self.dispatcher()
        first = self.run_job(d, "first task")
        second = self.run_job(d, "second task")
        ids = [j["id"] for j in d.jobs()]
        self.assertEqual(ids[:2], [second.id, first.id])

    def test_jobs_survive_a_dispatcher_restart(self):
        d = self.dispatcher()
        job = self.run_job(d, "persisted task")
        fresh = self.dispatcher()
        listed = {j["id"]: j for j in fresh.jobs()}
        self.assertIn(job.id, listed)
        self.assertEqual(listed[job.id]["exit_code"], 0)
        self.assertEqual(listed[job.id]["task"], "persisted task")

    def test_a_job_whose_daemon_died_is_reported_as_orphaned(self):
        """Not "running". A dead job that still claims to run is a lie."""
        jobs_dir = self.root / "jobs" / "deadbeef"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "job.json").write_text(json.dumps({
            "id": "deadbeef", "task": "abandoned", "to": "worker",
            "state": "running", "pid": 4_194_303, "started": time.time(),
            "iso": "x", "elapsed_s": 1.0, "exit_code": None,
            "dir": str(jobs_dir)}), encoding="utf-8")
        listed = {j["id"]: j for j in self.dispatcher().jobs()}
        self.assertEqual(listed["deadbeef"]["state"], "orphaned")

    def test_output_is_included_only_when_asked_for(self):
        d = self.dispatcher()
        self.run_job(d, "echo me")
        self.assertNotIn("output", d.jobs()[0])
        self.assertIn("echo me", d.jobs(with_output=True)[0]["output"])

    def test_output_is_capped(self):
        job = dispatch.Job(id="x", task="t", dir=self.root)
        (self.root / "output.txt").write_text("y" * 5000, encoding="utf-8")
        text = job.read_output(limit=100)
        self.assertLess(len(text), 300)
        self.assertIn("more characters", text)


class CancelTests(DispatcherCase):
    def test_cancelling_a_running_job_stops_it(self):
        d = self.dispatcher()
        job = d.dispatch("long one", timeout=30, linger=20)
        self.addCleanup(_stop_quietly, d, job)
        time.sleep(0.4)
        self.assertTrue(d.cancel(job.id))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and safety.is_alive(job.pid):
            time.sleep(0.05)
        self.assertFalse(safety.is_alive(job.pid))

    def test_cancelling_an_unknown_job_is_not_an_error(self):
        self.assertFalse(self.dispatcher().cancel("no-such-job"))


class PeekTests(DispatcherCase):
    def test_peek_toggles_and_reports_the_new_state(self):
        d = self.dispatcher()
        self.assertTrue(d.peek()["visible"])
        self.assertFalse(d.peek()["visible"])

    def test_peek_is_audited_with_its_own_inverse(self):
        d = self.dispatcher()
        d.peek()
        [entry] = self.audit.read(action="workspace.peek")
        self.assertEqual(entry["undo"]["cmd"], ["luna", "peek"])


class AuditTrailTests(DispatcherCase):
    def test_a_dispatch_writes_spawn_and_finish_entries(self):
        d = self.dispatcher()
        job = self.run_job(d, "audited task")
        actions = [e["action"] for e in self.audit.read()]
        self.assertIn("dispatch.spawn", actions)
        self.assertIn("dispatch.finish", actions)
        self.assertIn("process.spawned", actions)

    def test_the_spawn_entry_carries_the_pid_and_the_reason(self):
        d = self.dispatcher()
        job = self.run_job(d, "a task with a reason")
        [entry] = self.audit.read(action="dispatch.spawn")
        self.assertEqual(entry["pid"], job.pid)
        self.assertEqual(entry["job_id"], job.id)
        self.assertIn("a task with a reason", entry["why"])

    def test_the_finish_entry_carries_the_exit_status(self):
        d = self.dispatcher()
        job = self.run_job(d, "a task")
        [entry] = self.audit.read(action="dispatch.finish")
        self.assertEqual(entry["exit_code"], 0)
        self.assertTrue(entry["ok"])
        self.assertIn("elapsed_s", entry)

    def test_the_spawn_entry_records_stopping_the_job_as_its_inverse(self):
        d = self.dispatcher()
        job = self.run_job(d, "a task")
        [entry] = self.audit.read(action="dispatch.spawn")
        self.assertEqual(entry["undo"]["cmd"][:3], ["luna", "jobs", "--cancel"])


class AnnouncementTests(DispatcherCase):
    def test_sol_is_announced_by_name(self):
        d = self.dispatcher()
        job = dispatch.Job(id="ab12cd34", task="t", to="sol")
        line = d.announce(job)
        self.assertIn("Sol", line)
        self.assertIn("ab12cd34", line)
        self.assertEqual(len(line.splitlines()), 1,
                         "the persona spec says one line")

    def test_a_worker_is_not_announced_as_sol(self):
        d = self.dispatcher()
        line = d.announce(dispatch.Job(id="x", task="t", to="worker"))
        self.assertNotIn("Sol", line)


class LuaSyntaxTests(unittest.TestCase):
    """The incantation, pinned.

    Omarchy's Hyprland takes a Lua config and ``hyprctl`` evaluates its
    arguments as Lua, so the familiar ``[workspace special:x] cmd`` bracket
    syntax is a Lua syntax error. These assert the shapes that were verified by
    hand against the live compositor, so a future edit that "tidies" them fails
    here rather than at dispatch time.
    """

    def test_the_window_rule_is_a_lua_table_call(self):
        lua = _rule_lua()
        self.assertIn("hl.window_rule({", lua.replace(" ", "")[:200]
                      .replace("hl.window_rule({", "hl.window_rule({"))
        self.assertIn("match", lua)
        self.assertIn("workspace = \"special:luna silent\"", lua)

    def test_the_app_id_is_regex_anchored_and_lua_escaped(self):
        lua = _rule_lua()
        # `.` in org.omarchy.luna must be escaped for the regex, and the
        # backslash then escaped again for the Lua string literal.
        self.assertIn("org\\\\.omarchy\\\\.luna", lua)
        self.assertIn("^", lua)
        self.assertIn("$", lua)

    def test_the_rule_is_guarded_by_a_lua_global(self):
        lua = _rule_lua()
        self.assertIn("_G.luna_workspace_rule", lua)

    def test_the_app_id_is_not_the_one_the_users_own_terminals_use(self):
        """`org.omarchy.agent` belongs to the user's agent terminals.

        A workspace rule matching it would move live sessions into Luna's
        hidden workspace. This is the single most dangerous thing this module
        could have got wrong.
        """
        self.assertNotEqual(config.LUNA_APP_ID, "org.omarchy.agent")
        self.assertTrue(config.LUNA_APP_ID.startswith("org.omarchy."))

    def test_the_special_workspace_is_not_the_users_scratchpad(self):
        self.assertNotEqual(config.LUNA_WORKSPACE, "scratchpad")
        self.assertEqual(config.LUNA_WORKSPACE, "luna")


def _rule_lua() -> str:
    """Reproduce the Lua that ``ensure_workspace_rule`` sends."""
    captured: list[str] = []

    class Recorder(dispatch.Hyprland):
        def _run(self, args, timeout=5.0):
            captured.append(args[-1])
            return 0, "added"

    Recorder().ensure_workspace_rule()
    return captured[0]


class RunnerScriptTests(DispatcherCase):
    def test_the_script_sets_pipefail(self):
        """Without it the pipeline reports tee's status, so every job passes."""
        d = self.dispatcher()
        job = dispatch.Job(id="x", task="t", to="worker", dir=self.root)
        script = d._runner_script(job, timeout=60, linger=0)
        self.assertIn("set -o pipefail", script)

    def test_the_script_is_bash_because_pipefail_is_not_posix(self):
        d = self.dispatcher()
        job = dispatch.Job(id="x", task="t", to="worker", dir=self.root)
        self.assertTrue(d._runner_script(job, 60, 0).startswith("#!/bin/bash"))

    def test_only_sol_gets_sols_memory_directory(self):
        d = self.dispatcher()
        sol = d._runner_script(dispatch.Job(id="x", task="t", to="sol",
                                            dir=self.root), 60, 0)
        worker = d._runner_script(dispatch.Job(id="x", task="t", to="worker",
                                               dir=self.root), 60, 0)
        self.assertIn(str(self.root / "memory" / "sol"), sol)
        self.assertNotIn(str(self.root / "memory" / "sol"), worker)

    def test_the_agent_runs_with_tools_and_without_customisations(self):
        d = self.dispatcher()
        script = d._runner_script(dispatch.Job(id="x", task="t", dir=self.root),
                                  60, 0)
        self.assertIn("--tools default", script)
        self.assertIn("--safe-mode", script)
        self.assertIn("--permission-mode bypassPermissions", script)

    def test_the_agent_is_wrapped_in_a_timeout(self):
        d = self.dispatcher()
        script = d._runner_script(dispatch.Job(id="x", task="t", dir=self.root),
                                  120, 0)
        self.assertIn("timeout --signal=TERM --kill-after=20 120", script)


class BoundaryPromptTests(unittest.TestCase):
    """What a dispatched session is told it must not do."""

    def prompt(self, to: str = "worker") -> str:
        return persona.build_dispatch_system_prompt(to=to, job_dir="/tmp/j")

    def test_every_dispatched_session_is_told_not_to_pkill(self):
        for to in ("worker", "sol"):
            with self.subTest(to=to):
                self.assertIn("pkill", self.prompt(to))
                self.assertIn("Signal no process you did not", self.prompt(to))

    def test_every_dispatched_session_is_told_not_to_restart_the_shell(self):
        self.assertIn("omarchy-shell", self.prompt())
        self.assertIn("voxtype", self.prompt())

    def test_every_dispatched_session_is_told_usr_share_is_overwritten(self):
        self.assertIn("/usr/share/omarchy/", self.prompt())
        self.assertIn("omarchy update", self.prompt())

    def test_sudo_is_ruled_out(self):
        self.assertIn("sudo", self.prompt())

    def test_a_worker_is_not_given_sols_persona(self):
        self.assertNotIn("Sol", self.prompt("worker"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


def _stop_quietly(d: dispatch.Dispatcher, job: dispatch.Job) -> None:
    try:
        d.cancel(job.id)
    except Exception:  # noqa: BLE001 - cleanup, and the test already failed
        pass
