"""The config contract, proved key by key.

docs/CONFIG-SCHEMA.md calls itself the contract, and the Jarvis GUI writes the
whole of it. For a long stretch most of it was fiction: the file was accepted,
stored, round-tripped and displayed while the daemon went on using hard-coded
constants. A settings app that lies is worse than one with fewer settings.

So every key wired here gets a case that changes the *setting* and asserts the
*behaviour* moved with it — never that the value round-trips, which was already
true when none of it worked. :class:`DriftCase` at the end is the guard for the
other half of the problem: a schema default and its fallback constant that
disagree, which is exactly how `max_spoken_chars` came to mean 400 in three
documents and 700 in the code.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from ._support import FakeHyprland, TempMemoryCase

from lunad import audit as audit_mod
from lunad import config, consolidate, dispatch, memory, speech
from lunad import settings as settings_mod


def _stop_quietly(d: dispatch.Dispatcher, job: dispatch.Job) -> None:
    """Cleanup for a job deliberately left running by a case."""
    try:
        d.cancel(job.id)
    except Exception:  # noqa: BLE001 - cleanup, and the test already failed
        pass


# =========================================================================
# [voice]
# =========================================================================


class PiperVoiceCase(TempMemoryCase):
    """`[voice] piper_voice` picks the local model, not a constant."""

    def obj(self) -> speech.Speech:
        s = speech.Speech(settings=self.settings)
        self.addCleanup(s.close)
        return s

    def test_the_model_paths_follow_the_setting(self) -> None:
        s = self.obj()
        self.assertEqual(s.piper_voice(), config.VOICE_NAME)
        self.assertEqual(s.model, config.VOICES_DIR / f"{config.VOICE_NAME}.onnx")

        self.settings.set("voice.piper_voice", "en_US-amy-low")
        self.assertEqual(s.piper_voice(), "en_US-amy-low")
        self.assertEqual(s.model, config.VOICES_DIR / "en_US-amy-low.onnx")
        self.assertEqual(s.voice_config,
                         config.VOICES_DIR / "en_US-amy-low.onnx.json")

    def test_status_reports_the_configured_voice_not_the_constant(self) -> None:
        s = self.obj()
        self.settings.set("voice.piper_voice", "en_US-amy-low")
        self.settings.set("voice.provider", "piper")
        status = s.status()
        self.assertEqual(status["piper_voice"], "en_US-amy-low")
        self.assertEqual(status["voice"], "en_US-amy-low")

    def test_an_explicit_path_still_wins_over_the_setting(self) -> None:
        # Tests and any caller with a voice outside VOICES_DIR pass paths in.
        # A setting must not reach past an explicit argument.
        pinned = self.root / "pinned.onnx"
        s = speech.Speech(model=pinned, voice_config=self.root / "p.onnx.json",
                          settings=self.settings)
        self.addCleanup(s.close)
        self.settings.set("voice.piper_voice", "en_US-amy-low")
        self.assertEqual(s.model, pinned)

    def test_changing_the_voice_unloads_a_worker_holding_the_old_one(self) -> None:
        unloaded: list[str] = []

        class _Live:
            def poll(self) -> None:
                return None

        s = self.obj()
        s._proc = _Live()                       # type: ignore[assignment]
        s._loaded_voice = config.VOICE_NAME
        def fake_unload(reason: str) -> None:
            unloaded.append(reason)
            s._proc, s._loaded_voice = None, None

        s._unload = fake_unload                 # type: ignore[method-assign]

        # Same voice: the loaded worker is reused, nothing is unloaded.
        proc, _ = s._ensure_worker()
        self.assertIs(proc, s._proc)
        self.assertEqual(unloaded, [])

        # Different voice: one ONNX per worker, so it has to go.
        self.settings.set("voice.piper_voice", "en_US-amy-low")
        with self.assertRaises(speech.SpeechUnavailable):
            # Having dropped the old worker, the reload runs on and fails on
            # the missing model -- which is the point: it got as far as trying
            # to start a *new* one rather than reusing the wrong voice.
            s._ensure_worker()
        self.assertEqual(len(unloaded), 1)
        self.assertIn("en_US-amy-low", unloaded[0])


class SpeedCase(TempMemoryCase):
    """`[voice] speed` reaches both providers, in each one's own units."""

    def test_length_scale_is_the_reciprocal(self) -> None:
        self.assertEqual(speech.length_scale(1.0), 1.0)
        self.assertEqual(speech.length_scale(2.0), 0.5)
        self.assertEqual(speech.length_scale(0.5), 2.0)

    def test_a_nonsense_speed_never_raises(self) -> None:
        for bad in (None, "fast", 0, -3, float("nan")):
            self.assertEqual(speech._clamp_speed(bad), 1.0)
        self.assertEqual(speech._clamp_speed(99.0), speech.SPEED_MAX)
        self.assertEqual(speech._clamp_speed(0.01), speech.SPEED_MIN)

    def test_the_setting_reaches_the_piper_frame(self) -> None:
        sent: list[dict] = []

        class _Speech(speech.Speech):
            def _ensure_worker(self):  # noqa: ANN202
                return object(), object()

            def _send(self, proc, payload):  # noqa: ANN001, ANN202
                sent.append(payload)
                return False            # stop before any real playback

        s = _Speech(model=self.root / "v.onnx",
                    voice_config=self.root / "v.onnx.json",
                    settings=self.settings)
        self.addCleanup(s.close)
        s._sample_rate = 22_050

        self.settings.set("voice.speed", 1.5)
        job = speech._Job(["One."], "One.",
                          cfg=dict(self.settings.section("voice")))
        with self.assertRaises(speech.SpeechUnavailable):
            s._play_piper(job, ["One."])
        self.assertEqual(sent[-1]["length_scale"], speech.length_scale(1.5))

    def test_the_setting_reaches_the_openrouter_request(self) -> None:
        seen: list[dict] = []

        class _Response:
            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, *a):  # noqa: ANN002, ANN204
                return False

            def read(self) -> bytes:
                return _wav()

        def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
            import json
            seen.append(json.loads(request.data.decode()))
            return _Response()

        old = speech.urllib.request.urlopen
        speech.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
        self.addCleanup(setattr, speech.urllib.request, "urlopen", old)

        speech.synthesise("hi", model="m", voice="v", api_key="k", speed=1.75)
        self.assertEqual(seen[-1]["speed"], 1.75)

        # The default is left out entirely: not every model behind OpenRouter
        # implements `speed`, and a 400 on an unknown field for a value nobody
        # set would break speech for everyone.
        speech.synthesise("hi", model="m", voice="v", api_key="k", speed=1.0)
        self.assertNotIn("speed", seen[-1])


class SpokenLengthCase(TempMemoryCase):
    """`[voice] max_spoken_chars` decides how much is actually said."""

    def test_lowering_the_cap_shortens_the_spoken_form(self) -> None:
        s = speech.Speech(settings=self.settings)
        self.addCleanup(s.close)
        self.settings.set("voice.provider", "piper")
        text = " ".join(f"Sentence number {n} runs on a while." for n in range(40))

        self.settings.set("voice.max_spoken_chars", 60)
        short = speech.strip_for_speech(
            text, max_chars=s._voice_settings()["max_spoken_chars"])
        self.settings.set("voice.max_spoken_chars", 900)
        long = speech.strip_for_speech(
            text, max_chars=s._voice_settings()["max_spoken_chars"])

        self.assertLess(len(short), len(long))
        self.assertLessEqual(len(short), 60)


# =========================================================================
# [memory]
# =========================================================================


class MemoryCapCase(TempMemoryCase):
    """`[memory] luna_cap_chars` / `user_cap_chars` are the real caps."""

    def test_the_cap_follows_the_setting_without_a_restart(self) -> None:
        mem = self.memory()
        self.assertEqual(mem.luna.cap, config.LUNA_MD_CAP)
        self.settings.set("memory.luna_cap_chars", 250)
        self.assertEqual(mem.luna.cap, 250)
        self.assertEqual(mem.luna.usage()["cap"], 250)
        # And the user's file has its own key, not Luna's.
        self.settings.set("memory.user_cap_chars", 400)
        self.assertEqual(mem.user.cap, 400)
        self.assertEqual(mem.luna.cap, 250)

    def test_a_lowered_cap_rejects_a_write_that_used_to_fit(self) -> None:
        mem = self.memory()
        self.settings.set("memory.luna_cap_chars", 3000)
        mem.luna.append("x" * 500)
        self.settings.set("memory.luna_cap_chars", 300)
        with self.assertRaises(memory.MemoryCapExceeded) as caught:
            mem.luna.append("y" * 100)
        self.assertEqual(caught.exception.cap, 300)

    def test_sols_file_has_no_key_and_keeps_its_constant(self) -> None:
        # Sol's namespace is deliberately outside the user-facing contract.
        sol = self.sol_memory()
        self.settings.set("memory.luna_cap_chars", 250)
        self.assertEqual(sol.sol.cap, config.SOL_MD_CAP)


class DecayCase(TempMemoryCase):
    """`[memory] decay_half_life_days` is the half-life recall actually uses."""

    def test_the_half_life_follows_the_setting(self) -> None:
        self.settings.set("memory.decay_half_life_days", 10)
        self.assertEqual(memory.half_life_days_setting(), 10.0)
        self.assertAlmostEqual(
            memory.decayed_salience(0.8, 10 * 86400.0), 0.4, places=4)

        self.settings.set("memory.decay_half_life_days", 40)
        self.assertAlmostEqual(
            memory.decayed_salience(0.8, 10 * 86400.0), 0.8 * 2 ** -0.25,
            places=4)

    def test_recall_ranking_moves_with_it(self) -> None:
        store = self.episodes()
        store.record("the note about kettles", "mm",
                     ts=time.time() - 20 * 86400.0, salience=0.8)

        self.settings.set("memory.decay_half_life_days", 20)
        [row] = store.recent()
        self.assertAlmostEqual(row.effective_salience, 0.4, places=3)

        self.settings.set("memory.decay_half_life_days", 40)
        [row] = store.recent()
        self.assertAlmostEqual(row.effective_salience, 0.8 * 2 ** -0.5,
                               places=3)

    def test_an_explicit_argument_still_wins(self) -> None:
        self.settings.set("memory.decay_half_life_days", 10)
        self.assertAlmostEqual(
            memory.decayed_salience(0.8, 30 * 86400.0, half_life_days=30),
            0.4, places=4)


class ConsolidateCase(TempMemoryCase):
    """`[memory] consolidate_every_turns` is the counter a pass runs on.

    This key spent a release in §Not wired — in the file, validated, displayed
    and honoured by nothing — because there was no pass to schedule. There is
    now, so the case here is the same shape as every other in this module:
    change the setting, watch the behaviour move.
    """

    def consolidator(self) -> consolidate.Consolidator:
        self.mem = self.memory()
        con = consolidate.Consolidator(
            self.mem, adapter=lambda: None, settings=self.settings,
            min_interval_s=0.0)
        self.fired: list[str] = []
        con.run_once = lambda why="": self.fired.append(why)  # type: ignore[assignment]
        self.addCleanup(con.close)
        return con

    def test_the_counter_follows_the_setting(self) -> None:
        con = self.consolidator()
        self.assertEqual(con.every, config.CONSOLIDATE_EVERY_TURNS)
        self.settings.set("memory.consolidate_every_turns", 3)
        self.assertEqual([con.turn() for _ in range(3)],
                         [False, False, True])
        self.assertEqual(len(self.fired), 1)

    def test_zero_turns_it_off_entirely(self) -> None:
        con = self.consolidator()
        self.settings.set("memory.consolidate_every_turns", 0)
        for _ in range(100):
            self.assertFalse(con.turn())
        self.assertEqual(self.fired, [])

    def test_zero_is_a_value_the_config_file_accepts(self) -> None:
        # It was `minimum=1` while the key was inert, which would have made
        # "never" the one thing a user could not ask for.
        self.settings.set("memory.consolidate_every_turns", 0)
        self.assertEqual(self.settings.get("memory.consolidate_every_turns"), 0)
        again = settings_mod.Settings(self.settings.path)
        self.addCleanup(again.stop_watching)
        self.assertEqual(again.get("memory.consolidate_every_turns"), 0)
        self.assertEqual(again.problems, [])


# =========================================================================
# [dispatch]
# =========================================================================


class WorkspaceCase(TempMemoryCase):
    """`[dispatch] workspace` / `app_id` place the window."""

    def hypr(self) -> dispatch.Hyprland:
        h = dispatch.Hyprland()
        self.ran: list[list[str]] = []

        def run(args, timeout=5.0):  # noqa: ANN001, ANN202
            self.ran.append(list(args))
            return 0, "added"

        h._run = run                            # type: ignore[method-assign]
        return h

    def test_both_follow_the_settings(self) -> None:
        h = self.hypr()
        self.assertEqual(h.workspace, config.LUNA_WORKSPACE)
        self.assertEqual(h.app_id, config.LUNA_APP_ID)
        self.settings.set("dispatch.workspace", "atelier")
        self.settings.set("dispatch.app_id", "org.omarchy.jarvis")
        self.assertEqual(h.workspace, "atelier")
        self.assertEqual(h.app_id, "org.omarchy.jarvis")

    def test_the_rule_carries_the_configured_pair(self) -> None:
        h = self.hypr()
        self.settings.set("dispatch.workspace", "atelier")
        self.settings.set("dispatch.app_id", "org.omarchy.jarvis")
        h.ensure_workspace_rule()
        # The class is a Lua-escaped regex, so the dots arrive backslashed.
        lua = self.ran[-1][1]
        self.assertIn("omarchy", lua)
        self.assertIn("jarvis", lua)
        self.assertNotIn("org.omarchy.luna", lua.replace("\\", ""))
        self.assertIn("special:atelier silent", lua)

    def test_changing_either_half_renames_the_lua_guard(self) -> None:
        # The guard is what stops a second rule stacking. Keyed on a fixed
        # name it would also stop the *new* rule installing after a change,
        # and every job would then open on the active workspace.
        h = self.hypr()
        first = h.rule_global
        self.settings.set("dispatch.app_id", "org.omarchy.jarvis")
        second = h.rule_global
        self.settings.set("dispatch.workspace", "atelier")
        third = h.rule_global
        self.assertEqual(len({first, second, third}), 3)

    def test_an_explicit_argument_still_wins(self) -> None:
        h = dispatch.Hyprland(workspace="pinned", app_id="org.pinned")
        self.settings.set("dispatch.workspace", "atelier")
        self.settings.set("dispatch.app_id", "org.omarchy.jarvis")
        self.assertEqual(h.workspace, "pinned")
        self.assertEqual(h.app_id, "org.pinned")

    def test_the_dispatcher_launches_with_the_compositors_app_id(self) -> None:
        # One source of truth. Two copies used to exist, and a mismatch means
        # the rule matches one class while foot is launched with another.
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(app_id="org.fake"),
                                audit=self.audit, agent_bin="/bin/true")
        self.assertEqual(d.app_id, "org.fake")


class MaxParallelCase(TempMemoryCase):
    """`[dispatch] max_parallel` is the admission gate, read live.

    The mechanics of the queue live in test_dispatch; what is asserted here is
    the contract itself — that changing the number changes what the daemon
    does, in both directions, without a restart.
    """

    def dispatcher(self) -> dispatch.Dispatcher:
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                terminal="/bin/bash", agent_bin="/bin/true")
        real_spawn = d.spawn

        def spawn_without_a_terminal(argv, **kw):  # noqa: ANN001, ANN202
            return real_spawn(["/bin/bash", argv[-1]], **kw)

        d.spawn = spawn_without_a_terminal      # type: ignore[method-assign]
        self.addCleanup(d.close)
        return d

    def hold(self, d: dispatch.Dispatcher) -> dispatch.Job:
        job = d.dispatch("hold a slot", timeout=30, linger=20)
        self.addCleanup(_stop_quietly, d, job)
        return job

    def test_the_limit_is_read_at_every_admission_not_captured(self) -> None:
        d = self.dispatcher()
        self.assertEqual(d.max_parallel, 1)
        self.settings.set("dispatch.max_parallel", 3)
        self.assertEqual(d.max_parallel, 3)

    def test_one_means_the_second_job_waits(self) -> None:
        d = self.dispatcher()
        self.hold(d)
        self.assertEqual(d.dispatch("second", timeout=30, linger=0).state,
                         "queued")

    def test_two_means_it_does_not(self) -> None:
        d = self.dispatcher()
        self.settings.set("dispatch.max_parallel", 2)
        self.hold(d)
        second = d.dispatch("second", timeout=30, linger=20)
        self.addCleanup(_stop_quietly, d, second)
        self.assertEqual(second.state, "running")

    def test_the_snapshot_reports_the_limit_and_who_is_waiting(self) -> None:
        d = self.dispatcher()
        self.hold(d)
        waiting = d.dispatch("second", timeout=30, linger=0)
        snap = d.snapshot()
        self.assertEqual(snap["max_parallel"], 1)
        self.assertEqual([j["id"] for j in snap["queued"]], [waiting.id])


class JobRetentionCase(TempMemoryCase):
    """`[dispatch] job_retention_days` decides what `collect` may delete."""

    def dispatcher(self) -> dispatch.Dispatcher:
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                terminal="/bin/bash", agent_bin="/bin/true")
        self.addCleanup(d.close)
        return d

    def aged_job(self, jid: str, days: float) -> Path:
        path = self.root / "jobs" / jid
        path.mkdir(parents=True, exist_ok=True)
        (path / "job.json").write_text(json.dumps({
            "id": jid, "task": "an old job", "to": "worker",
            "state": "finished", "pid": 4_194_303,
            "started": time.time() - days * 86_400.0,
            "finished": time.time() - days * 86_400.0,
            "iso": "x", "elapsed_s": 1.0, "exit_code": 0,
            "dir": str(path)}), encoding="utf-8")
        return path

    def test_the_window_follows_the_setting(self) -> None:
        d = self.dispatcher()
        job = self.aged_job("aaaaaaaa", days=20)
        self.settings.set("dispatch.job_retention_days", 30)
        self.assertEqual(d.collect()["collected"], 0)
        self.assertTrue(job.exists())

        self.settings.set("dispatch.job_retention_days", 10)
        self.assertEqual(d.collect()["collected"], 1)
        self.assertFalse(job.exists())

    def test_zero_is_off_and_not_the_shortest_window(self) -> None:
        d = self.dispatcher()
        job = self.aged_job("bbbbbbbb", days=9_000)
        self.settings.set("dispatch.job_retention_days", 0)
        self.assertEqual(d.collect()["collected"], 0)
        self.assertTrue(job.exists())


class AuditRotationCase(TempMemoryCase):
    """`[audit] max_mb` and `keep` bound the record without shortening it."""

    def fill(self, log, entries: int) -> None:  # noqa: ANN001
        for i in range(entries):
            log.append("test", index=i, filler="x" * 300_000)

    def test_the_ceiling_follows_the_setting(self) -> None:
        log = audit_mod.AuditLog(self.root / "audit.jsonl")
        self.assertEqual(log._rotate_at(), config.AUDIT_MAX_MB * 1_048_576)
        self.settings.set("audit.max_mb", 2)
        self.assertEqual(log._rotate_at(), 2 * 1_048_576)
        self.settings.set("audit.max_mb", 0)
        self.assertEqual(log._rotate_at(), 0, "0 is the off switch")

    def test_how_many_siblings_survive_follows_the_setting(self) -> None:
        self.settings.set("audit.max_mb", 1)
        self.settings.set("audit.keep", 2)
        log = audit_mod.AuditLog(self.root / "audit.jsonl")
        self.fill(log, 16)
        self.assertGreaterEqual(log.rotations, 3)
        self.assertTrue(log.sibling(2).exists())
        self.assertFalse(log.sibling(3).exists())


class NotifyOnFinishCase(TempMemoryCase):
    """`[ui] notify_on_finish` decides whether a finished job says so."""

    def dispatcher(self) -> dispatch.Dispatcher:
        d = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                hypr=FakeHyprland(), audit=self.audit,
                                agent_bin="/bin/true",
                                notify_bin="jarvis-tests-must-not-notify")
        self.spawned: list[list[str]] = []

        class _Done:
            def wait(self, timeout=None):  # noqa: ANN001, ANN202
                return 0

            def poll(self) -> int:
                return 0

        def spawn(argv, **kw):  # noqa: ANN001, ANN202
            self.spawned.append(list(argv))
            return _Done()

        # Stubbed, not merely pointed at a bad binary: a real spawn here would
        # put a toast on the user's desktop from the test suite.
        old = dispatch.safety.spawn
        dispatch.safety.spawn = spawn           # type: ignore[assignment]
        self.addCleanup(setattr, dispatch.safety, "spawn", old)
        return d

    def job(self, exit_code: int = 0) -> dispatch.Job:
        return dispatch.Job(id="abc123", task="tidy the log rotation",
                            to="worker", dir=self.root / "jobs" / "abc123",
                            exit_code=exit_code, state="finished")

    def test_off_means_nothing_is_sent(self) -> None:
        d = self.dispatcher()
        self.settings.set("ui.notify_on_finish", False)
        self.assertFalse(d.notify_finished(self.job()))
        self.assertEqual(self.spawned, [])

    def test_on_means_one_toast_naming_the_job(self) -> None:
        d = self.dispatcher()
        self.settings.set("ui.notify_on_finish", True)
        self.assertTrue(d.notify_finished(self.job()))
        argv = self.spawned[-1]
        self.assertEqual(argv[0], "jarvis-tests-must-not-notify")
        self.assertIn("abc123", " ".join(argv))
        self.assertIn("tidy the log rotation", " ".join(argv))

    def test_the_toast_uses_her_configured_name(self) -> None:
        d = self.dispatcher()
        self.settings.set("assistant.name", "Jarvis")
        self.assertTrue(d.notify_finished(self.job()))
        self.assertIn("Jarvis", " ".join(self.spawned[-1]))

    def test_a_failed_job_is_urgent_and_says_so(self) -> None:
        d = self.dispatcher()
        d.notify_finished(self.job(exit_code=2))
        argv = self.spawned[-1]
        self.assertIn("critical", argv)
        self.assertIn("exit 2", " ".join(argv))

    def test_a_missing_notifier_never_fails_the_job(self) -> None:
        d = self.dispatcher()

        def boom(argv, **kw):  # noqa: ANN001, ANN202
            raise FileNotFoundError("no such binary")

        dispatch.safety.spawn = boom            # type: ignore[assignment]
        self.assertFalse(d.notify_finished(self.job()))


# =========================================================================
# Drift
# =========================================================================


class DriftCase(unittest.TestCase):
    """A schema default and its fallback constant must agree.

    Both exist for a reason -- the constant is what a daemon with no config
    file uses -- but when they disagree the file silently means one thing and
    the code another. That is precisely how `max_spoken_chars` read 400 in
    CONFIG-SCHEMA.md, 400 in the GUI, 400 in the user's own config.toml, and
    700 in `lunad/config.py`, and how `decay_half_life_days` read 30
    everywhere a human could see and 14 where it was actually used.
    """

    PAIRS = (
        ("voice.max_spoken_chars", "SPEECH_MAX_CHARS"),
        ("voice.piper_voice", "VOICE_NAME"),
        ("memory.decay_half_life_days", "SALIENCE_HALF_LIFE_DAYS"),
        ("memory.consolidate_every_turns", "CONSOLIDATE_EVERY_TURNS"),
        ("memory.luna_cap_chars", "LUNA_MD_CAP"),
        ("memory.user_cap_chars", "USER_MD_CAP"),
        ("dispatch.workspace", "LUNA_WORKSPACE"),
        ("dispatch.app_id", "LUNA_APP_ID"),
        ("dispatch.max_parallel", "DISPATCH_MAX_PARALLEL"),
        ("dispatch.job_retention_days", "JOB_RETENTION_DAYS"),
        ("audit.max_mb", "AUDIT_MAX_MB"),
        ("audit.keep", "AUDIT_KEEP"),
        ("ambient.poll_seconds", "AMBIENT_POLL_S"),
        ("ambient.battery_low_pct", "AMBIENT_BATTERY_LOW_PCT"),
        ("ambient.battery_critical_pct", "AMBIENT_BATTERY_CRITICAL_PCT"),
    )

    def test_every_default_matches_its_fallback_constant(self) -> None:
        for dotted, constant in self.PAIRS:
            with self.subTest(key=dotted):
                _section, key = settings_mod.find(dotted)
                self.assertEqual(
                    float(key.default) if isinstance(key.default, (int, float))
                    else key.default,
                    float(getattr(config, constant))
                    if isinstance(getattr(config, constant), (int, float))
                    else getattr(config, constant),
                    f"[{dotted}] defaults to {key.default!r} but "
                    f"config.{constant} is {getattr(config, constant)!r}")

    def test_the_two_resolved_mismatches_stay_resolved(self) -> None:
        # Named explicitly so a future edit to either number has to come here
        # and say why, rather than quietly reintroducing the drift.
        self.assertEqual(config.SPEECH_MAX_CHARS, 400)
        self.assertEqual(config.SALIENCE_HALF_LIFE_DAYS, 30.0)


def _wav() -> bytes:
    """The smallest RIFF the parser will accept."""
    import struct
    pcm = b"\x00\x00" * 64
    fmt = struct.pack("<HHIIHH", 1, 1, 24_000, 48_000, 2, 16)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
    return b"RIFF" + struct.pack("<I", len(body)) + body


if __name__ == "__main__":
    unittest.main()
