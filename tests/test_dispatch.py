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
import os
import threading
import time
import unittest
from pathlib import Path

from lunad import config, dispatch, persona, safety
from lunad import settings as settings_mod

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


class QueueTests(DispatcherCase):
    """`[dispatch] max_parallel`: the admission gate and the pending queue.

    Every case here holds the first job open with a long ``linger`` so the
    second one's fate is decided by the gate and not by a race. The fake agent
    exits immediately; ``linger`` is the only thing keeping the terminal alive,
    which makes "is there a slot" a question the test controls.
    """

    def blocker(self, d: dispatch.Dispatcher) -> dispatch.Job:
        """A job that stays running until the test cancels it."""
        job = d.dispatch("hold the only slot", timeout=30, linger=20)
        self.addCleanup(_stop_quietly, d, job)
        self.assertEqual(job.state, "running")
        return job

    def wait_for(self, job: dispatch.Job, *states: str,
                 timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job.state in states:
                return job.state
            time.sleep(0.05)
        self.fail(f"job {job.id} stayed {job.state!r}, never reached {states}")

    def test_a_job_over_the_limit_queues_instead_of_spawning(self):
        d = self.dispatcher()
        self.blocker(d)
        second = d.dispatch("wait your turn", timeout=30, linger=0)
        self.assertEqual(second.state, "queued")
        self.assertIsNone(second.pid, "a queued job has no process yet")
        self.assertEqual(len(self.ledger), 1,
                         "the second job must not have forked anything")

    def test_simultaneous_dispatches_cannot_both_take_the_last_slot(self):
        """The daemon answers each connection on its own thread.

        The slot is reserved at the admission *decision*, not when the ``Popen``
        appears, or two callers arriving together would both look at an empty
        process map and both be let through a limit of one.
        """
        import threading

        d = self.dispatcher()
        self.settings.set("dispatch.max_parallel", 2)
        ready = threading.Barrier(4)
        jobs: list[dispatch.Job] = []
        lock = threading.Lock()

        def go(n: int) -> None:
            ready.wait(timeout=10)
            job = d.dispatch(f"racer {n}", timeout=30, linger=20)
            with lock:
                jobs.append(job)

        threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        for job in jobs:
            self.addCleanup(_stop_quietly, d, job)
        self.assertEqual(len(jobs), 4)
        running = [j for j in jobs if j.state == "running"]
        self.assertEqual(len(running), 2,
                         "the gate let more than max_parallel through")
        self.assertEqual(len([j for j in jobs if j.state == "queued"]), 2)

    def test_a_queued_job_is_a_first_class_job_in_the_listing(self):
        d = self.dispatcher()
        self.blocker(d)
        second = d.dispatch("wait your turn", timeout=30, linger=0)
        listed = {j["id"]: j for j in d.jobs()}
        self.assertEqual(listed[second.id]["state"], "queued")
        self.assertEqual(listed[second.id]["elapsed_s"], 0.0,
                         "a job that has not run has not run for any time")
        self.assertTrue((second.dir / "job.json").exists(),
                        "a queued job has its directory and its prompt already")
        self.assertTrue((second.dir / "system.txt").read_text().strip())

    def test_the_queue_drains_when_a_slot_frees(self):
        d = self.dispatcher()
        first = self.blocker(d)
        second = d.dispatch("wait your turn", timeout=30, linger=0)
        d.cancel(first.id)
        self.wait_for(second, "running", "finished")
        self.assertIsNotNone(second.pid)
        self.assertEqual(self.wait_for(second, "finished", "failed"),
                         "finished")

    def test_the_queue_is_first_in_first_out(self):
        d = self.dispatcher()
        first = self.blocker(d)
        # The second job lingers too, so the slot it inherits stays taken and
        # the third's state is the gate's answer rather than a race with it.
        second = d.dispatch("second", timeout=30, linger=20)
        self.addCleanup(_stop_quietly, d, second)
        third = d.dispatch("third", timeout=30, linger=0)
        self.assertEqual([j.id for j in d.queued()], [second.id, third.id])
        d.cancel(first.id)
        self.wait_for(second, "running")
        self.assertEqual(third.state, "queued",
                         "one slot means one job admitted at a time")

    def test_a_free_slot_does_not_let_a_newcomer_overtake_the_queue(self):
        # Admission is FIFO even when there is room: a job that arrives while
        # the queue is draining must not jump the two already waiting.
        d = self.dispatcher()
        first = self.blocker(d)
        second = d.dispatch("second", timeout=30, linger=0)
        self.settings.set("dispatch.max_parallel", 2)
        third = d.dispatch("third", timeout=30, linger=0)
        self.assertEqual(third.state, "queued")
        self.assertEqual([j.id for j in d.queued()], [second.id, third.id])
        # Drain, and wait for it: a case that returns with jobs mid-flight
        # leaves watchers writing into a temporary tree teardown is deleting.
        d.cancel(first.id)
        self.wait_for(second, "finished", "failed")
        self.wait_for(third, "finished", "failed")

    def test_a_queued_job_can_be_cancelled_before_it_ever_starts(self):
        d = self.dispatcher()
        first = self.blocker(d)
        second = d.dispatch("never mind", timeout=30, linger=0)
        self.assertTrue(d.cancel(second.id))
        self.assertEqual(second.state, "cancelled")
        self.assertIsNone(second.pid)
        self.assertEqual(d.queued(), [])
        # And it does not come back to life when the slot frees.
        d.cancel(first.id)
        time.sleep(0.5)
        self.assertEqual(second.state, "cancelled")
        self.assertEqual([e for e in self.audit.read(action="dispatch.spawn")
                          if e["job_id"] == second.id], [],
                         "a cancelled job was spawned by the drain")

    def test_cancelling_a_queued_job_signals_nothing(self):
        """The firewall is never involved: there is no process to refuse."""
        d = self.dispatcher()
        self.blocker(d)
        second = d.dispatch("never mind", timeout=30, linger=0)
        d.cancel(second.id)
        [entry] = [e for e in self.audit.read(action="dispatch.cancel")
                   if e["job_id"] == second.id]
        self.assertEqual(entry["was"], "queued")
        self.assertEqual([e for e in self.audit.read(action="signal.")], [])

    def test_raising_the_limit_admits_waiting_work(self):
        d = self.dispatcher()
        self.blocker(d)
        second = d.dispatch("wait your turn", timeout=30, linger=0)
        self.settings.set("dispatch.max_parallel", 2)
        self.assertEqual(d.admit_ready(), [second.id])
        self.wait_for(second, "running", "finished")

    def test_lowering_the_limit_kills_nothing_already_running(self):
        d = self.dispatcher()
        self.settings.set("dispatch.max_parallel", 2)
        first = self.blocker(d)
        second = d.dispatch("also running", timeout=30, linger=20)
        self.addCleanup(_stop_quietly, d, second)
        self.assertEqual(second.state, "running")
        self.settings.set("dispatch.max_parallel", 1)
        self.assertEqual(d.admit_ready(), [], "nothing new is admitted")
        self.assertTrue(safety.is_alive(first.pid))
        self.assertTrue(safety.is_alive(second.pid),
                        "lowering the limit must not reach into running work")
        third = d.dispatch("waits for the count to drain", timeout=30, linger=0)
        self.assertEqual(third.state, "queued")

    def test_a_queued_job_is_dropped_on_shutdown_rather_than_left_promised(self):
        d = self.dispatcher()
        first = self.blocker(d)
        # Held so the job can be stopped *after* close(), which has already
        # let go of the process map on purpose: a running job outlives the
        # daemon by design, and only a queued one is dropped.
        proc = d._procs[first.id]
        second = d.dispatch("never going to run", timeout=30, linger=0)
        d.close(join_timeout=0.5)
        self.assertEqual(second.state, "cancelled")
        self.assertIn("daemon stopped", second.note)
        on_disk = json.loads((second.dir / "job.json").read_text())
        self.assertEqual(on_disk["state"], "cancelled")
        self.assertEqual(d.queued(), [])
        self.assertTrue(safety.is_alive(first.pid),
                        "close() must not have touched the running job")
        # And now stop it, and wait for its watcher to finish writing, so
        # nothing is still holding the temporary tree teardown is about to
        # delete. That stray-directory bug is on the record already.
        safety.terminate(proc, reason="test teardown")
        self.wait_for(first, "finished", "failed", "cancelled")

    def test_a_queued_job_left_by_a_dead_daemon_reads_as_orphaned(self):
        """A queue lives in one process. Nothing else can still be waiting."""
        job_dir = self.root / "jobs" / "cafebabe"
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps({
            "id": "cafebabe", "task": "accepted, never started", "to": "worker",
            "state": "queued", "pid": None, "started": time.time(),
            "iso": "x", "elapsed_s": 0.0, "exit_code": None,
            "dir": str(job_dir)}), encoding="utf-8")
        listed = {j["id"]: j for j in self.dispatcher().jobs()}
        self.assertEqual(listed["cafebabe"]["state"], "orphaned")
        self.assertIn("never started", listed["cafebabe"]["note"])

    def test_the_queue_entry_is_audited_with_the_cancel_that_undoes_it(self):
        d = self.dispatcher()
        self.blocker(d)
        second = d.dispatch("wait your turn", timeout=30, linger=0)
        [entry] = self.audit.read(action="dispatch.queued")
        self.assertEqual(entry["job_id"], second.id)
        self.assertEqual(entry["position"], 1)
        self.assertEqual(entry["max_parallel"], 1)
        self.assertEqual(entry["undo"]["cmd"][:3], ["luna", "jobs", "--cancel"])

    def test_the_spawn_entry_records_how_long_the_job_waited(self):
        d = self.dispatcher()
        first = self.blocker(d)
        second = d.dispatch("wait your turn", timeout=30, linger=0)
        time.sleep(0.3)
        d.cancel(first.id)
        self.wait_for(second, "running", "finished")
        [entry] = [e for e in self.audit.read(action="dispatch.spawn")
                   if e["job_id"] == second.id]
        self.assertGreater(entry["queued_s"], 0.0)
        # And the job that never waited does not carry a zero on the line
        # somebody is reading for the pid.
        [straight] = [e for e in self.audit.read(action="dispatch.spawn")
                      if e["job_id"] == first.id]
        self.assertNotIn("queued_s", straight)


class AdmissionLeakTests(DispatcherCase):
    """`_start` used to leak `_admitting` on anything but `OSError`.

    Anything past `spawn` raising something other than `OSError` left the job
    id in `_admitting` forever, shrinking `max_parallel` by one until a
    restart — and, when the leak happened inside `_watch`'s own call to
    `_admit_next`, aborted the rest of `_watch` so the job that had actually
    just finished never got its `notify_finished` either.
    """

    def wait_for(self, job: dispatch.Job, *states: str,
                 timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job.state in states:
                return job.state
            time.sleep(0.02)
        self.fail(f"job {job.id} stayed {job.state!r}, never reached {states}")

    def test_the_admitting_slot_is_released_on_an_unexpected_spawn_error(self):
        d = self.dispatcher()

        def boom(argv, **kw):
            raise RuntimeError("not an OSError — the kind that used to leak")

        d.spawn = boom
        with self.assertRaises(RuntimeError):
            d.dispatch("boom", timeout=5, linger=0)

        # The reservation must be gone: nothing is running and nothing is
        # still "admitting" on this job's behalf.
        self.assertEqual(d._taken(), 0)

        # And the limit must not actually be shrunk: a normal dispatch right
        # after has to be admitted, not queued forever behind a phantom slot.
        d.spawn = lambda argv, **kw: safety.spawn(
            ["/bin/bash", argv[-1]], **kw)
        job = self.run_job(d, "fine now")
        self.assertEqual(job.state, "finished")

    def test_watch_finishes_its_own_bookkeeping_despite_a_bad_admission(self):
        d = self.dispatcher()
        self.settings.set("dispatch.max_parallel", 1)

        job_a = d.dispatch("first, finishes on its own", timeout=30, linger=0.3)
        self.assertEqual(job_a.state, "running")
        job_b = d.dispatch("second, queued behind the only slot",
                           timeout=30, linger=0)
        self.assertEqual(job_b.state, "queued")
        self.addCleanup(_stop_quietly, d, job_a)
        self.addCleanup(_stop_quietly, d, job_b)

        real_spawn = d.spawn

        def boom_once(argv, **kw):
            d.spawn = real_spawn      # sabotage exactly one admission
            raise RuntimeError("boom admitting the next job")

        d.spawn = boom_once

        notified: list[str] = []
        orig_notify = d.notify_finished

        def spy_notify(job: dispatch.Job) -> bool:
            notified.append(job.id)
            return orig_notify(job)

        d.notify_finished = spy_notify

        # Job A finishes on its own (its own spawn already happened before
        # the sabotage above was installed); its `_watch` thread then tries
        # to admit job B, hits the injected error, and must still finish its
        # own bookkeeping regardless.
        self.wait_for(job_a, "finished", "failed")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and job_a.id not in notified:
            time.sleep(0.02)
        self.assertIn(job_a.id, notified,
                     "job A's own notify_finished must fire even though "
                     "admitting job B blew up")

        # Job B's reservation is not leaked either, even though it never
        # actually started.
        self.assertEqual(d._taken(), 0)


class CollectTests(DispatcherCase):
    """`[dispatch] job_retention_days`: the GC pass, and what it refuses.

    Job directories are written by hand here rather than dispatched: the pass
    reads `job.json` and the filesystem, and building the state directly is the
    only way to have a job that finished three weeks ago.
    """

    OLD = 40 * 86_400.0
    RECENT = 2 * 86_400.0

    def job_dir(self, jid: str, *, state: str = "finished",
                finished: float | None = None, started: float | None = None,
                pid: int | None = 4_194_303, manifest: bool = True) -> Path:
        now = time.time()
        path = self.root / "jobs" / jid
        path.mkdir(parents=True, exist_ok=True)
        (path / "output.txt").write_text("what the agent wrote\n",
                                         encoding="utf-8")
        if manifest:
            (path / "job.json").write_text(json.dumps({
                "id": jid, "task": f"task {jid}", "to": "worker",
                "state": state, "pid": pid,
                "started": now - (self.OLD if started is None else started),
                "iso": "x", "elapsed_s": 1.0, "exit_code": 0,
                "dir": str(path)}
                | ({} if finished is None else {"finished": now - finished})),
                encoding="utf-8")
        return path

    def test_a_finished_job_past_the_window_is_collected(self):
        d = self.dispatcher()
        old = self.job_dir("00000001", finished=self.OLD)
        report = d.collect()
        self.assertFalse(old.exists())
        self.assertEqual(report["collected"], 1)
        self.assertGreater(report["freed_bytes"], 0)

    def test_a_job_inside_the_window_is_kept(self):
        d = self.dispatcher()
        recent = self.job_dir("00000002", finished=self.RECENT)
        self.assertEqual(d.collect()["collected"], 0)
        self.assertTrue(recent.exists())

    def test_zero_days_means_never_delete_not_delete_everything(self):
        """The footgun. Read as a duration, 0 would mean "keep nothing"."""
        d = self.dispatcher()
        old = self.job_dir("00000003", finished=self.OLD)
        self.settings.set("dispatch.job_retention_days", 0)
        report = d.collect()
        self.assertEqual(report["collected"], 0)
        self.assertIn("never", report["note"])
        self.assertTrue(old.exists(), "0 must not have deleted the archive")

    def test_a_negative_retention_is_refused_and_would_also_mean_never(self):
        # The schema stops one reaching the daemon at all; `collect` is written
        # so that if one ever did -- a hand-edited file, a future default --
        # it still deletes nothing.
        with self.assertRaises(settings_mod.SettingsError):
            self.settings.set("dispatch.job_retention_days", -5)

        class _Negative(dispatch.Dispatcher):
            @property
            def retention_days(self) -> int:
                return -5

        d = _Negative(jobs_dir=self.root / "jobs", hypr=self.hypr,
                      audit=self.audit, terminal="/bin/bash",
                      agent_bin=str(self.fake_agent))
        self.addCleanup(d.close)
        old = self.job_dir("00000004", finished=self.OLD)
        self.assertEqual(d.collect()["collected"], 0)
        self.assertTrue(old.exists())

    def test_a_running_job_is_never_collected_however_old(self):
        d = self.dispatcher()
        # `pid` is this very process, so the liveness check says it is running.
        running = self.job_dir("00000005", state="running", pid=os.getpid())
        self.assertEqual(d.collect()["collected"], 0)
        self.assertTrue(running.exists())

    def test_a_queued_job_is_never_collected_however_old(self):
        d = self.dispatcher()
        self.blocker = d.dispatch("hold the slot", timeout=30, linger=20)
        self.addCleanup(_stop_quietly, d, self.blocker)
        queued = d.dispatch("waiting", timeout=30, linger=0)
        self.assertEqual(queued.state, "queued")
        # Backdate it past any conceivable window: state, not age, decides.
        data = json.loads((queued.dir / "job.json").read_text())
        data["started"] = time.time() - self.OLD
        (queued.dir / "job.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(d.collect()["collected"], 0)
        self.assertTrue(queued.dir.exists())

    def test_an_orphaned_job_is_collected_once_past_the_window(self):
        """It says "running" and never will be. One crash must not pin a
        directory for the life of the machine."""
        d = self.dispatcher()
        orphan = self.job_dir("00000006", state="running", pid=4_194_303)
        self.assertEqual(d.collect()["collected"], 1)
        self.assertFalse(orphan.exists())

    def test_the_age_is_taken_from_when_the_job_stopped(self):
        d = self.dispatcher()
        # Started long ago, finished yesterday: the record is a day old.
        long_runner = self.job_dir("00000007", started=self.OLD,
                                   finished=self.RECENT)
        self.assertEqual(d.collect()["collected"], 0)
        self.assertTrue(long_runner.exists())

    def test_a_directory_with_no_manifest_ages_from_itself(self):
        d = self.dispatcher()
        stump = self.job_dir("00000008", manifest=False)
        self.assertEqual(d.collect()["collected"], 0,
                         "just created, so inside any window")
        os.utime(stump, (0, 0))
        self.assertEqual(d.collect()["collected"], 1)
        self.assertFalse(stump.exists())

    def test_every_deletion_is_audited_and_claims_no_undo(self):
        d = self.dispatcher()
        self.job_dir("00000009", finished=self.OLD)
        d.collect()
        [entry] = self.audit.read(action="job.collected")
        self.assertEqual(entry["job_id"], "00000009")
        self.assertEqual(entry["state"], "finished")
        self.assertGreater(entry["age_days"], 14)
        self.assertNotIn("undo", entry,
                         "there is no inverse for a deleted directory")

    def test_the_collector_runs_on_its_own_thread_and_stops_with_close(self):
        d = self.dispatcher()
        old = self.job_dir("0000000a", finished=self.OLD)
        d.start_gc(interval=3600.0)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and old.exists():
            time.sleep(0.05)
        self.assertFalse(old.exists(), "the pass on the way up never ran")
        d.close(join_timeout=10.0)
        self.assertFalse(d._gc.is_alive(), "the collector outlived close()")


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
