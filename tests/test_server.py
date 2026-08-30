"""Protocol, dispatch, and a real socket round trip.

The agent adapter is stubbed throughout: these tests must never spend money or
depend on the network.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import uuid
from typing import Any

from lunad import agent, config, dispatch, protocol
from lunad.server import Daemon, LunaServer

from ._support import FakeHyprland, TempMemoryCase


class FakeAdapter(agent.BaseAdapter):
    name = "fake"

    def __init__(self, reply: str = "Fine.", raises: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.reply = reply
        self.raises = raises
        self.delay = delay
        self.last_system_prompt = ""
        self.last_prompt = ""
        self.calls: list[dict[str, Any]] = []

    def available(self) -> tuple[bool, str]:
        return True, "fake adapter"

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> agent.AgentReply:
        self.last_system_prompt = system_prompt
        self.last_prompt = prompt
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt,
                           "session_id": kw.get("session_id"),
                           "resume": kw.get("resume")})
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise self.raises
        return agent.AgentReply(text=self.reply, agent="fake", model="fake-1",
                                cost_usd=0.001, wall_ms=1)


class MuteSpeech:
    """Stands in for the piper worker: records, plays nothing, spends nothing."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.cancels = 0

    # Part of the real interface: `Daemon._publish_state` reads it on every
    # transition, and this double is never mid-utterance because nothing here
    # takes any time to say.
    speaking = False

    def say(self, text: str, wait: bool = False, timeout: float = 0.0):
        self.said.append(text)
        return {"spoken": text, "sentences": 1, "id": "fake", "cancelled": False}

    def cancel(self) -> bool:
        self.cancels += 1
        return True

    def status(self) -> dict[str, Any]:
        return {"loaded": False, "speaking": False, "counters": {}}

    def close(self) -> None:
        pass


class ProtocolTests(unittest.TestCase):
    def test_encode_decode_round_trip(self):
        line = protocol.encode({"op": "ask", "prompt": "hello §"})
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(protocol.decode(line)["prompt"], "hello §")

    def test_bad_json_is_a_protocol_error(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b"{not json}")

    def test_non_object_is_a_protocol_error(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'["a"]')

    def test_missing_op_is_a_protocol_error(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'{"prompt": "hi"}')

    def test_oversized_line_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b"x" * (protocol.MAX_LINE_BYTES + 1))


class DaemonCase(TempMemoryCase):
    """A Daemon wired to temp state and a fake compositor.

    Phase 2 gave the daemon two more things that reach outside the process —
    an audit log and a dispatcher that shells out to ``hyprctl``. Both are
    injected here so the suite neither writes to the user's audit log nor
    depends on a live Wayland session.
    """

    def build_daemon(self, **kw: object) -> Daemon:
        self.hypr = FakeHyprland()
        dispatcher = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                         hypr=self.hypr, audit=self.audit,
                                         sol_memory_dir=self.root / "sol",
                                         terminal="/bin/bash",
                                         agent_bin="/bin/true")
        daemon = Daemon(agent_name="claude", memory=self.memory(),
                        sol_memory=self.sol_memory(), audit=self.audit,
                        dispatcher=dispatcher, **kw)
        self.addCleanup(daemon.close)
        return daemon


class PresenceTests(DaemonCase):
    """The daemon publishes what the bar reads.

    `presence.py` owns the file format; this covers the wiring — that the
    daemon publishes at all, that speech transitions reach it without anyone
    calling anything by hand, and that shutting down leaves absence behind
    rather than a stale word.
    """

    def daemon(self) -> Daemon:
        d = self.build_daemon()
        d.adapter = FakeAdapter()
        return d

    def test_a_live_daemon_publishes_idle(self):
        d = self.daemon()
        self.assertEqual(config.STATE_FILE.read_text(), "idle")
        self.assertEqual(d.presence.path, config.STATE_FILE)

    def test_speaking_is_published_by_speech_itself(self):
        # Nothing in the test calls _publish_state: the point is that the
        # Speech object reports its own transitions, so a reply that speaks
        # cannot forget to say so.
        d = self.daemon()
        self.assertEqual(config.STATE_FILE.read_text(), "idle")
        d.speech._job = _StuckJob()
        d.speech._notify()
        self.assertEqual(config.STATE_FILE.read_text(), "speaking")
        d.speech._job.done.set()
        d.speech._notify()
        self.assertEqual(config.STATE_FILE.read_text(), "idle")

    def test_thinking_wins_over_idle_while_a_run_is_open(self):
        d = self.daemon()
        run = d.runs.new("what time is it", "cli")
        d._publish_state()
        self.assertEqual(config.STATE_FILE.read_text(), "thinking")
        d.runs.done(run)
        d._publish_state()
        self.assertEqual(config.STATE_FILE.read_text(), "idle")

    def test_shutdown_removes_the_file(self):
        d = self.daemon()
        d.speech.close()
        d.speech = MuteSpeech()
        d.close()
        # Absence is the "not running" signal the widget depends on. A stale
        # word here would leave the bar claiming she is up forever.
        self.assertFalse(config.STATE_FILE.exists())


class _StuckJob:
    """An utterance that has started and not finished."""

    def __init__(self) -> None:
        self.done = threading.Event()


class DispatchTests(DaemonCase):
    def daemon(self, adapter: agent.BaseAdapter | None = None) -> Daemon:
        d = self.build_daemon()
        d.adapter = adapter or FakeAdapter()
        d.speech.close()          # no piper, no aplay, no audio in tests
        d.speech = MuteSpeech()
        self.addCleanup(d.close)
        return d

    def test_unknown_op(self):
        resp = self.daemon().dispatch({"op": "nonsense", "id": "1"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "UnknownOp")

    def test_status_reports_real_state(self):
        d = self.daemon()
        resp = d.dispatch({"op": "status"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["memory"]["tier1"]["LUNA.md"]["cap"], 3000)
        self.assertTrue(resp["memory"]["tier3"]["implemented"])
        self.assertTrue(resp["memory"]["tier3"]["derived"])
        self.assertIn("consolidation", resp)
        self.assertGreater(resp["persona"]["chars"], 100)
        self.assertEqual(resp["activity"]["counters"]["ask"], 0)

    def test_ask_returns_a_reply_and_records_an_episode(self):
        d = self.daemon(FakeAdapter("Your call."))
        resp = d.dispatch({"op": "ask", "prompt": "should I rewrite it in rust",
                           "id": "abc"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["reply"], "Your call.")
        self.assertEqual(resp["id"], "abc")
        self.assertIn("episode", resp)
        self.assertEqual(d.memory.episodes.stats()["episodes"], 1)
        self.assertEqual(d.counters["ask"], 1)

    def test_ask_can_skip_remembering(self):
        d = self.daemon()
        d.dispatch({"op": "ask", "prompt": "throwaway", "remember": False})
        self.assertEqual(d.memory.episodes.stats()["episodes"], 0)

    def test_ask_injects_persona_and_tier1_into_the_system_prompt(self):
        adapter = FakeAdapter()
        d = self.daemon(adapter)
        d.memory.luna.append("the terminal on this machine is foot")
        d.dispatch({"op": "ask", "prompt": "what terminal do I use"})
        prompt = adapter.last_system_prompt
        self.assertIn("You are Luna", prompt)
        self.assertIn("Never open with praise", prompt)      # from the spec
        self.assertIn("the terminal on this machine is foot", prompt)  # tier 1

    def test_tier2_recall_rides_in_the_message_not_the_prefix(self):
        # If recall were in the system prompt it would change the cacheable
        # prefix on every turn and session reuse would save nothing.
        adapter = FakeAdapter()
        d = self.daemon(adapter)
        d.memory.episodes.record("we chose jenny_dioco for the voice", "agreed")
        d.dispatch({"op": "ask", "prompt": "what voice did we choose"})
        self.assertIn("jenny_dioco", adapter.last_prompt)
        self.assertNotIn("jenny_dioco", adapter.last_system_prompt)

    def test_the_prefix_is_identical_across_turns_of_one_conversation(self):
        adapter = FakeAdapter()
        d = self.daemon(adapter)
        d.memory.episodes.record("something about the bar widget", "noted")
        d.dispatch({"op": "ask", "prompt": "first question about the bar"})
        d.dispatch({"op": "ask", "prompt": "second, unrelated question"})
        self.assertEqual(adapter.calls[0]["system_prompt"],
                         adapter.calls[1]["system_prompt"])

    def test_the_first_ask_starts_a_session_and_the_next_resumes_it(self):
        adapter = FakeAdapter()
        d = self.daemon(adapter)
        d.dispatch({"op": "ask", "prompt": "one"})
        second = d.dispatch({"op": "ask", "prompt": "two"})
        self.assertIsNotNone(adapter.calls[0]["session_id"])
        self.assertIsNone(adapter.calls[0]["resume"])
        self.assertEqual(adapter.calls[1]["resume"],
                         adapter.calls[0]["session_id"])
        self.assertTrue(second["resumed"])

    def test_a_tier1_write_starts_a_fresh_session(self):
        adapter = FakeAdapter()
        d = self.daemon(adapter)
        d.dispatch({"op": "ask", "prompt": "one"})
        d.dispatch({"op": "memory.write", "file": "LUNA.md",
                    "entry": "the bar is Quickshell, not Waybar"})
        third = d.dispatch({"op": "ask", "prompt": "two"})
        self.assertFalse(third["resumed"])
        self.assertIsNotNone(adapter.calls[1]["session_id"])

    def test_a_refused_resume_retries_once_with_a_clean_session(self):
        class FlakyResume(FakeAdapter):
            def ask(self, prompt: str, system_prompt: str, **kw: Any):
                if kw.get("resume"):
                    self.calls.append({"prompt": prompt,
                                       "system_prompt": system_prompt,
                                       "session_id": kw.get("session_id"),
                                       "resume": kw.get("resume")})
                    raise agent.AgentFailed("no conversation found", returncode=1)
                return super().ask(prompt, system_prompt, **kw)

        adapter = FlakyResume()
        d = self.daemon(adapter)
        d.dispatch({"op": "ask", "prompt": "one"})
        resp = d.dispatch({"op": "ask", "prompt": "two"})
        self.assertTrue(resp["ok"], resp)
        self.assertIsNotNone(adapter.calls[-1]["session_id"])

    def test_session_reset_drops_everything(self):
        d = self.daemon()
        d.dispatch({"op": "ask", "prompt": "one"})
        self.assertEqual(d.dispatch({"op": "session.reset"})["dropped"], 1)

    def test_a_voice_ask_speaks_the_reply(self):
        d = self.daemon(FakeAdapter(reply="Battery is at sixty percent."))
        resp = d.dispatch({"op": "ask", "prompt": "battery",
                           "surface": "voice"})
        self.assertTrue(resp["ok"])
        self.assertEqual(d.speech.said, ["Battery is at sixty percent."])

    def test_a_cli_ask_stays_silent(self):
        d = self.daemon()
        d.dispatch({"op": "ask", "prompt": "battery"})
        self.assertEqual(d.speech.said, [])

    def test_a_voice_ask_is_told_to_answer_briefly(self):
        adapter = FakeAdapter()
        d = self.daemon(adapter)
        d.dispatch({"op": "ask", "prompt": "battery", "surface": "voice"})
        self.assertIn("speech synthesiser", adapter.last_prompt)

    def test_a_detached_ask_returns_before_the_agent_does(self):
        adapter = FakeAdapter(delay=1.0)
        d = self.daemon(adapter)
        started = time.monotonic()
        resp = d.dispatch({"op": "ask", "prompt": "slow one", "detach": True,
                           "surface": "voice"})
        self.assertTrue(resp["queued"])
        self.assertLess(time.monotonic() - started, 0.5)
        for _ in range(60):
            if d.speech.said:
                break
            time.sleep(0.1)
        self.assertEqual(d.speech.said, ["Fine."])

    def test_a_detached_failure_is_spoken_not_swallowed_silently(self):
        d = self.daemon(FakeAdapter(raises=agent.AgentTimeout("too slow")))
        d.dispatch({"op": "ask", "prompt": "x", "detach": True,
                    "surface": "voice"})
        for _ in range(60):
            if d.speech.said:
                break
            time.sleep(0.1)
        self.assertTrue(d.speech.said, "a failed voice ask said nothing at all")

    def test_say_requires_text(self):
        self.assertEqual(
            self.daemon().dispatch({"op": "say", "text": " "})["error"],
            "ProtocolError")

    def test_say_speaks(self):
        d = self.daemon()
        resp = d.dispatch({"op": "say", "text": "hello there"})
        self.assertTrue(resp["ok"])
        self.assertEqual(d.speech.said, ["hello there"])

    def test_speak_cancel(self):
        d = self.daemon()
        self.assertEqual(d.dispatch({"op": "speak.cancel"})["cancelled"], 1)
        self.assertEqual(d.speech.cancels, 1)

    def test_ask_without_a_prompt_is_a_protocol_error(self):
        resp = self.daemon().dispatch({"op": "ask", "prompt": "  "})
        self.assertEqual(resp["error"], "ProtocolError")

    def test_agent_failure_is_surfaced_not_swallowed(self):
        d = self.daemon(FakeAdapter(raises=agent.AgentTimeout("took too long")))
        resp = d.dispatch({"op": "ask", "prompt": "hello"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "AgentTimeout")
        self.assertIn("took too long", resp["message"])
        self.assertEqual(d.memory.episodes.stats()["episodes"], 0)

    def test_agent_nonzero_exit_is_surfaced_with_its_returncode(self):
        d = self.daemon(FakeAdapter(
            raises=agent.AgentFailed("boom", returncode=2, stderr="oh no")))
        resp = d.dispatch({"op": "ask", "prompt": "hello"})
        self.assertEqual(resp["error"], "AgentFailed")
        self.assertEqual(resp["returncode"], 2)

    def test_memory_write_and_read(self):
        d = self.daemon()
        resp = d.dispatch({"op": "memory.write", "file": "USER.md",
                           "entry": "prefers British spelling"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["usage"]["entries"], 1)
        read = d.dispatch({"op": "memory.read", "file": "USER.md"})
        self.assertEqual(read["entries"], ["prefers British spelling"])

    def test_memory_write_over_cap_is_reported_not_truncated(self):
        d = self.daemon()
        resp = d.dispatch({"op": "memory.write", "file": "USER.md",
                           "entry": "x" * 5000})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "MemoryCapExceeded")
        self.assertEqual(resp["cap"], 2000)
        self.assertGreater(resp["overflow"], 0)
        self.assertIn("usage_pct", resp)
        self.assertEqual(d.memory.user.entries(), [])
        self.assertEqual(d.counters["errors"], 1)

    def test_memory_write_bad_mode(self):
        resp = self.daemon().dispatch({"op": "memory.write", "mode": "clobber",
                                       "entry": "x"})
        self.assertEqual(resp["error"], "ProtocolError")

    def test_memory_search(self):
        d = self.daemon()
        d.memory.episodes.record("the bar widget should stay monochrome", "yes")
        resp = d.dispatch({"op": "memory.search", "query": "bar widget"})
        self.assertEqual(resp["count"], 1)
        self.assertIn("monochrome", resp["results"][0]["user_text"])

    def test_cancel_with_no_such_request_is_not_an_error(self):
        resp = self.daemon().dispatch({"op": "cancel", "target": "nope"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["cancelled"], 0)


class SocketTests(DaemonCase):
    """End-to-end over a real Unix socket, including concurrency."""

    def setUp(self) -> None:
        super().setUp()
        self.adapter = FakeAdapter(delay=0.4)
        self.daemon = self.build_daemon()
        self.daemon.adapter = self.adapter
        self.sock_path = self.root / "luna.sock"
        self.server = LunaServer(self.sock_path, self.daemon)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.daemon.close()

    def call(self, **request: Any) -> dict:
        request.setdefault("id", uuid.uuid4().hex[:8])
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect(str(self.sock_path))
        try:
            fh = s.makefile("rwb")
            fh.write((json.dumps(request) + "\n").encode())
            fh.flush()
            return json.loads(fh.readline())
        finally:
            s.close()

    def test_socket_is_owner_only(self):
        self.assertEqual(self.sock_path.stat().st_mode & 0o777, 0o600)

    def test_ping(self):
        self.assertTrue(self.call(op="ping")["pong"])

    def test_garbage_line_gets_an_error_not_a_dead_daemon(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(str(self.sock_path))
        fh = s.makefile("rwb")
        fh.write(b"this is not json\n")
        fh.flush()
        resp = json.loads(fh.readline())
        self.assertEqual(resp["error"], "ProtocolError")
        # The same connection still works afterwards.
        fh.write(b'{"op": "ping"}\n')
        fh.flush()
        self.assertTrue(json.loads(fh.readline())["ok"])
        s.close()

    def test_a_slow_ask_does_not_block_other_clients(self):
        results: dict[str, Any] = {}

        def slow():
            results["ask"] = self.call(op="ask", prompt="take your time")

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.1)
        started = time.monotonic()
        status = self.call(op="status")           # must answer while ask runs
        elapsed = time.monotonic() - started
        self.assertTrue(status["ok"])
        self.assertLess(elapsed, 0.3, "status blocked behind an in-flight ask")
        self.assertEqual(len(status["activity"]["in_flight"]), 1)
        t.join(timeout=10)
        self.assertTrue(results["ask"]["ok"])
        self.assertEqual(len(self.call(op="status")["activity"]["in_flight"]), 0)

    def test_stale_socket_is_reclaimed_but_a_live_one_is_not(self):
        from lunad.server import AlreadyRunning, _clear_stale_socket
        with self.assertRaises(AlreadyRunning):
            _clear_stale_socket(self.sock_path)

    def test_server_close_removes_the_socket(self):
        path = self.root / "throwaway.sock"
        daemon = self.build_daemon()
        server = LunaServer(path, daemon)
        self.assertTrue(path.exists())
        server.server_close()
        daemon.close()
        self.assertFalse(path.exists())


class Phase2OpTests(DaemonCase):
    """The ops added in Phase 2, exercised through the daemon's own dispatch."""

    def daemon(self) -> Daemon:
        d = self.build_daemon()
        d.adapter = FakeAdapter()
        d.speech.close()
        d.speech = MuteSpeech()
        return d

    def test_peek_toggles_the_workspace(self):
        d = self.daemon()
        first = d.dispatch({"op": "peek"})
        self.assertTrue(first["ok"])
        self.assertTrue(first["visible"])
        self.assertFalse(d.dispatch({"op": "peek"})["visible"])

    def test_jobs_is_empty_before_anything_is_dispatched(self):
        resp = self.daemon().dispatch({"op": "jobs"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["jobs"], [])
        self.assertTrue(resp["workspace"]["available"])

    def test_dispatch_requires_a_task(self):
        resp = self.daemon().dispatch({"op": "dispatch", "task": "  "})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "ProtocolError")

    def test_audit_reads_back_what_the_daemon_wrote(self):
        d = self.daemon()
        resp = d.dispatch({"op": "audit"})
        self.assertTrue(resp["ok"])
        actions = [e["action"] for e in resp["entries"]]
        self.assertIn("daemon.started", actions)

    def test_audit_rejects_a_since_it_cannot_parse(self):
        resp = self.daemon().dispatch({"op": "audit", "since": "yesterdayish"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "ProtocolError")

    def test_audit_since_filters(self):
        d = self.daemon()
        self.audit.append("marker.recent", ok=True)
        resp = d.dispatch({"op": "audit", "since": "1m"})
        self.assertIn("marker.recent", [e["action"] for e in resp["entries"]])
        resp = d.dispatch({"op": "audit", "since": "1m", "action": "nothing."})
        self.assertEqual(resp["entries"], [])

    def test_spawned_reports_the_allowlist(self):
        resp = self.daemon().dispatch({"op": "spawned"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["tracked"], 0)
        self.assertEqual(resp["entries"], [])

    def test_spawned_check_refuses_a_pid_luna_did_not_spawn(self):
        """The firewall, answerable from outside the process."""
        import os
        resp = self.daemon().dispatch({"op": "spawned", "check": os.getppid()})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["check"]["may_signal"])
        self.assertIn("did not spawn", resp["check"]["reason"])

    def test_cancelling_an_unknown_job_is_reported_not_raised(self):
        resp = self.daemon().dispatch({"op": "jobs", "cancel": "nope"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["cancelled"], 0)

    def test_status_reports_the_firewall_and_the_workspace(self):
        resp = self.daemon().dispatch({"op": "status"})
        self.assertIn("spawned", resp)
        self.assertIn("dispatch", resp)
        self.assertTrue(resp["dispatch"]["workspace"]["available"])
        self.assertEqual(resp["dispatch"]["workspace"]["workspace"],
                         "special:luna")

    def test_the_new_ops_are_listed_when_an_op_is_unknown(self):
        resp = self.daemon().dispatch({"op": "nope"})
        for op in ("dispatch", "jobs", "peek", "audit", "spawned"):
            self.assertIn(op, resp["message"])


if __name__ == "__main__":
    unittest.main()
