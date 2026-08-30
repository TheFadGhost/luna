"""The daemon's Jarvis surface: settings over the socket, and the gate.

These are the parts a settings GUI and a dispatched agent talk to, so the
assertions are about the *protocol* — the shape of what comes back — rather
than about internals a caller cannot see.
"""

from __future__ import annotations

import unittest
from typing import Any

from lunad import agent, confirm as confirm_mod, dispatch, settings as settings_mod
from lunad.server import Daemon

from ._support import FakeHyprland, TempMemoryCase


class MuteSpeech:
    def say(self, text: str, wait: bool = False, timeout: float = 0.0):
        return {"spoken": text, "sentences": 1, "id": "fake", "cancelled": False}

    def cancel(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {"loaded": False, "speaking": False, "counters": {}}

    def close(self) -> None:
        pass


class JarvisDaemonCase(TempMemoryCase):
    def daemon(self, **kw: Any) -> Daemon:
        self.hypr = FakeHyprland()
        dispatcher = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                         hypr=self.hypr, audit=self.audit,
                                         sol_memory_dir=self.root / "sol",
                                         terminal="/bin/bash",
                                         agent_bin="/bin/true")
        d = Daemon(agent_name="claude", memory=self.memory(),
                   sol_memory=self.sol_memory(), audit=self.audit,
                   dispatcher=dispatcher, settings=self.settings, **kw)
        d.speech.close()
        d.speech = MuteSpeech()          # type: ignore[assignment]
        self.addCleanup(d.close)
        return d


class SettingsOpCase(JarvisDaemonCase):
    def test_settings_get_returns_the_whole_config_and_the_schema(self) -> None:
        resp = self.daemon().dispatch({"op": "settings.get", "id": "1"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["settings"]["voice"]["voice"], "flux-alexis-en")
        self.assertEqual(resp["defaults"]["assistant"]["name"], "Luna")
        sections = {s["section"] for s in resp["schema"]}
        self.assertIn("confirm.prompt", sections)
        self.assertIn("path", resp)

    def test_settings_get_one_key_carries_its_type_and_choices(self) -> None:
        resp = self.daemon().dispatch({"op": "settings.get",
                                       "key": "voice.provider"})
        self.assertEqual(resp["value"], "openrouter")
        self.assertEqual(resp["choices"], ["openrouter", "piper"])
        self.assertEqual(resp["kind"], "str")

    def test_settings_get_never_returns_a_key(self) -> None:
        resp = self.daemon().dispatch({"op": "settings.get"})
        self.assertNotIn("key", resp.get("secrets", {}))
        self.assertIsInstance(resp["secrets"]["present"], bool)

    def test_settings_set_writes_the_file_and_takes_effect(self) -> None:
        d = self.daemon()
        resp = d.dispatch({"op": "settings.set", "key": "voice.voice",
                           "value": "flux-donovan-en"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["applied"]["voice.voice"], "flux-donovan-en")
        self.assertIn("flux-donovan-en",
                      self.settings.path.read_text(encoding="utf-8"))
        self.assertEqual(d.speech._voice_settings()["voice"]  # type: ignore[attr-defined]
                         if hasattr(d.speech, "_voice_settings")
                         else self.settings.get("voice.voice"),
                         "flux-donovan-en")

    def test_settings_set_many_at_once(self) -> None:
        resp = self.daemon().dispatch({
            "op": "settings.set",
            "updates": {"assistant.name": "Jarvis",
                        "confirm.git_push": "never"}})
        self.assertTrue(resp["ok"])
        self.assertEqual(self.settings.get("assistant.name"), "Jarvis")
        self.assertEqual(self.settings.get("confirm.git_push"), "never")

    def test_settings_set_rejects_a_bad_value_with_a_useful_error(self) -> None:
        resp = self.daemon().dispatch({"op": "settings.set",
                                       "key": "voice.provider",
                                       "value": "carrier pigeon"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "SettingsError")
        self.assertIn("openrouter", resp["message"])

    def test_settings_set_rejects_an_unknown_key(self) -> None:
        resp = self.daemon().dispatch({"op": "settings.set",
                                       "key": "voice.volume", "value": 3})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "SettingsError")

    def test_settings_set_without_a_key_is_a_protocol_error(self) -> None:
        resp = self.daemon().dispatch({"op": "settings.set"})
        self.assertEqual(resp["error"], "ProtocolError")

    def test_a_set_is_audited(self) -> None:
        self.daemon().dispatch({"op": "settings.set", "key": "assistant.name",
                                "value": "Jarvis"})
        actions = [e["action"] for e in self.audit.read()]
        self.assertIn("settings.set", actions)


class HotReloadInDaemonCase(JarvisDaemonCase):
    def test_a_file_edit_reaches_the_next_request(self) -> None:
        d = self.daemon()
        self.settings.path.write_text('[voice]\nvoice = "flux-donovan-en"\n',
                                      encoding="utf-8")
        self.settings.poll()
        resp = d.dispatch({"op": "settings.get", "key": "voice.voice"})
        self.assertEqual(resp["value"], "flux-donovan-en")

    def test_renaming_her_retires_the_warm_sessions(self) -> None:
        d = self.daemon()
        d.sessions.acquire("default", "prefix-abc")
        self.assertEqual(len(d.sessions.snapshot()), 1)
        self.settings.set("assistant.name", "Jarvis")
        self.assertEqual(d.sessions.snapshot(), [])

    def test_a_reload_is_audited_with_the_diff(self) -> None:
        d = self.daemon()
        self.settings.set("confirm.git_push", "never")
        entries = [e for e in self.audit.read()
                   if e["action"] == "settings.reloaded"]
        self.assertTrue(entries)
        self.assertTrue(any("confirm.git_push" in c
                            for c in entries[0]["changed"]))

    def test_switching_to_a_bad_agent_keeps_the_working_one(self) -> None:
        d = self.daemon()
        before = d.agent_name
        d._settings_changed([{"key": "assistant.agent", "from": "claude",
                              "to": "nonsense"}])
        self.assertEqual(d.agent_name, before)


class ConfirmOpCase(JarvisDaemonCase):
    def test_confirm_list_reports_the_policy(self) -> None:
        resp = self.daemon().dispatch({"op": "confirm", "action": "list"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["policies"]["delete_files"], "ask")
        self.assertIn("signal_unspawned", resp["hard_denies"])
        self.assertEqual(resp["pending"], [])

    def test_confirm_ask_with_never_returns_allowed(self) -> None:
        self.settings.set("confirm.delete_files", "never")
        resp = self.daemon().dispatch({"op": "confirm", "action": "ask",
                                       "class": "delete_files",
                                       "detail": "rm a.txt"})
        self.assertTrue(resp["allowed"])
        self.assertEqual(resp["outcome"], "auto")

    def test_confirm_ask_with_deny_returns_refused(self) -> None:
        self.settings.set("confirm.delete_files", "deny")
        resp = self.daemon().dispatch({"op": "confirm", "action": "ask",
                                       "class": "delete_files"})
        self.assertFalse(resp["allowed"])
        self.assertEqual(resp["outcome"], "denied")

    def test_confirm_ask_refuses_a_hard_deny_without_prompting(self) -> None:
        self.settings.set("confirm.system_config", "never")
        resp = self.daemon().dispatch({
            "op": "confirm", "action": "ask", "class": "system_config",
            "detail": "systemctl --user restart omarchy-shell"})
        self.assertFalse(resp["allowed"])
        self.assertEqual(resp["outcome"], "hard")

    def test_confirm_yes_on_an_unknown_token_says_so(self) -> None:
        resp = self.daemon().dispatch({"op": "confirm", "action": "yes",
                                       "token": "nope"})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["answered"])

    def test_confirm_ask_without_a_class_is_a_protocol_error(self) -> None:
        resp = self.daemon().dispatch({"op": "confirm", "action": "ask"})
        self.assertEqual(resp["error"], "ProtocolError")

    def test_an_unknown_confirm_action_is_a_protocol_error(self) -> None:
        resp = self.daemon().dispatch({"op": "confirm", "action": "shrug"})
        self.assertEqual(resp["error"], "ProtocolError")

    def test_status_carries_the_confirm_policy_and_the_settings(self) -> None:
        resp = self.daemon().dispatch({"op": "status"})
        self.assertEqual(resp["confirm"]["policies"]["git_push"], "ask")
        self.assertEqual(resp["settings"]["assistant"], "Luna")
        self.assertIn("path", resp["settings"])


class DispatchGateCase(JarvisDaemonCase):
    def test_a_denied_class_stops_the_dispatch_before_anything_spawns(self) -> None:
        self.settings.set("confirm.delete_files", "deny")
        d = self.daemon()
        resp = d.dispatch({"op": "dispatch", "task": "rm -f ~/notes.txt"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "ConfirmDenied")
        self.assertEqual(resp["decision"]["action"], "delete_files")
        self.assertEqual(d.dispatcher.jobs(), [])

    def test_a_hard_deny_stops_the_dispatch(self) -> None:
        d = self.daemon()
        resp = d.dispatch({"op": "dispatch",
                           "task": "restart omarchy-shell for me"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "ConfirmDenied")
        self.assertEqual(resp["decision"]["outcome"], "hard")
        self.assertIn("not a setting", resp["message"])

    def test_never_lets_the_dispatch_through(self) -> None:
        for name in confirm_mod.CLASSES:
            self.settings.set(f"confirm.{name}", "never")
        d = self.daemon()
        resp = d.dispatch({"op": "dispatch", "task": "rm -f /tmp/jarvis-test"})
        self.assertTrue(resp["ok"], resp.get("message"))

    def test_the_long_job_class_does_not_fire_on_the_default_timeout(self) -> None:
        """A one-hour ceiling is not a one-hour estimate."""
        d = self.daemon()
        d.confirm.asker = _refuse
        resp = d.dispatch({"op": "dispatch", "task": "read the README"})
        self.assertTrue(resp["ok"], resp.get("message"))

    def test_an_explicit_estimate_does_fire_it(self) -> None:
        d = self.daemon()
        d.confirm.asker = _refuse
        resp = d.dispatch({"op": "dispatch", "task": "read the README",
                           "estimate_seconds": 3000})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["decision"]["action"], "long_job")

    def test_the_dispatch_prompt_carries_the_tool_side_gate(self) -> None:
        d = self.daemon()
        d.confirm.asker = _approve
        resp = d.dispatch({"op": "dispatch", "task": "rm -f /tmp/jarvis-test"})
        self.assertTrue(resp["ok"], resp.get("message"))
        system = (self.root / "jobs" / resp["id"] / "system.txt").read_text()
        self.assertIn("confirm ask <class>", system)
        self.assertIn("`delete_files`", system)
        self.assertIn("restarting `omarchy-shell`", system)


def _approve(pending, channel) -> None:  # noqa: ANN001
    pending.answer = True
    pending.event.set()


def _refuse(pending, channel) -> None:  # noqa: ANN001
    pending.answer = False
    pending.event.set()


class BrandingCase(JarvisDaemonCase):
    def test_the_system_prompt_uses_the_configured_name(self) -> None:
        from lunad import persona
        self.settings.set("assistant.name", "Jarvis")
        prompt = persona.build_system_prompt("nothing yet", spec="a spec")
        self.assertIn("You are Jarvis.", prompt)
        self.assertIn("## Jarvis — persona specification", prompt)
        self.assertNotIn("You are Luna.", prompt)

    def test_the_worker_prompt_uses_the_configured_name(self) -> None:
        from lunad import persona
        self.settings.set("assistant.name", "Jarvis")
        prompt = persona.build_dispatch_system_prompt(to="worker")
        self.assertIn("dispatched by Jarvis", prompt)
        self.assertNotIn("dispatched by Luna", prompt)

    def test_the_specialist_prompt_uses_both_names(self) -> None:
        from lunad import persona
        self.settings.set("assistant.name", "Jarvis")
        self.settings.set("assistant.specialist", "Atlas")
        prompt = persona.build_dispatch_system_prompt(to="sol", spec="Sol spec")
        self.assertIn("You are Atlas.", prompt)
        self.assertIn("report to Jarvis", prompt)
        self.assertIn("## Atlas — persona specification", prompt)

    def test_the_announcement_uses_the_specialist_name(self) -> None:
        self.settings.set("assistant.specialist", "Atlas")
        d = self.daemon()
        job = dispatch.Job(id="x", task="t", to="sol")
        self.assertIn("Atlas", d.dispatcher.announce(job))

    def test_no_confirm_block_when_nothing_is_set_to_ask(self) -> None:
        from lunad import persona
        self.assertEqual(persona.build_confirm_block([]), "")


if __name__ == "__main__":
    unittest.main()
