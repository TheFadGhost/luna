"""Sol — the specialist, and the wall between his memory and Luna's.

Sol reports to Luna, not to the user, and he keeps his notes in his own
namespace. The failure this file guards against is drift: two agents writing
to one model of the world until neither is right. So the separation is tested
where it is enforced — in the memory API — rather than assumed from the prompt.
"""

from __future__ import annotations

import unittest

from lunad import config, persona
from lunad.memory import MemoryError as LunaMemoryError, SolMemory

from ._support import TempMemoryCase


class NamespaceTests(TempMemoryCase):
    def test_sols_files_live_under_memory_sol(self):
        sol = self.sol_memory()
        self.assertEqual(sol.sol.path.parent.name, "sol")
        self.assertEqual(sol.sol.path.name, "SOL.md")

    def test_the_real_namespace_is_a_child_of_lunas_memory_dir(self):
        self.assertEqual(config.SOL_MD.parent, config.SOL_MEMORY_DIR)
        self.assertEqual(config.SOL_MEMORY_DIR.parent, config.MEMORY_DIR)
        self.assertNotEqual(config.SOL_MD, config.LUNA_MD)

    def test_sol_cannot_open_lunas_files(self):
        sol = self.sol_memory()
        for name in ("LUNA.md", "USER.md", "luna", "user.MD"):
            with self.subTest(name=name):
                with self.assertRaises(LunaMemoryError) as caught:
                    sol.file(name)
                self.assertIn("SOL.md", str(caught.exception))

    def test_the_refusal_says_why_not_just_no(self):
        sol = self.sol_memory()
        with self.assertRaises(LunaMemoryError) as caught:
            sol.file("LUNA.md")
        self.assertIn("belongs to Luna", str(caught.exception))
        self.assertIn("report", str(caught.exception))

    def test_luna_cannot_open_sols_file_either(self):
        """The wall is not one-way. Luna does not edit Sol's notes for him."""
        mem = self.memory()
        with self.assertRaises(LunaMemoryError):
            mem.file("SOL.md")

    def test_writing_sols_memory_leaves_lunas_untouched(self):
        mem = self.memory()
        mem.luna.append("Luna's own fact")
        before = mem.luna.text()
        sol = self.sol_memory()
        sol.file("SOL.md").append("Sol's finding about a flag")
        self.assertEqual(mem.luna.text(), before)
        self.assertNotIn("Sol's finding", mem.luna.text())
        self.assertNotIn("Sol's finding", mem.user.text())
        self.assertIn("Sol's finding", sol.sol.text())

    def test_sols_episodes_are_a_separate_store(self):
        mem = self.memory()
        sol = self.sol_memory()
        sol.episodes.record("a sol question", "a sol answer", surface="dispatch")
        self.assertEqual(mem.episodes.stats()["episodes"], 0)
        self.assertEqual(sol.episodes.stats()["episodes"], 1)

    def test_sols_notes_never_surface_as_lunas_recall(self):
        mem = self.memory()
        sol = self.sol_memory()
        sol.episodes.record("the piper sample rate mystery",
                            "it is in the onnx.json", surface="dispatch")
        self.assertEqual(mem.recall_block("piper sample rate"), "")

    def test_sol_has_his_own_cap(self):
        sol = self.sol_memory()
        self.assertEqual(sol.sol.cap, config.SOL_MD_CAP)

    def test_the_cap_rejects_rather_than_truncates_for_sol_too(self):
        sol = SolMemory(self.root / "capped")
        self.addCleanup(sol.close)
        sol.sol.cap_default = 80
        sol.sol.append("short one")
        with self.assertRaises(LunaMemoryError):
            sol.sol.append("x" * 200)
        self.assertIn("short one", sol.sol.text())
        self.assertNotIn("x" * 200, sol.sol.text())

    def test_usage_reports_the_namespace_path(self):
        sol = self.sol_memory()
        self.assertIn("sol", sol.usage()["namespace"])
        self.assertIn("SOL.md", sol.usage())

    def test_the_block_is_empty_until_something_is_written(self):
        sol = self.sol_memory()
        self.assertEqual(sol.block(), "")
        sol.sol.append("a fact worth keeping")
        self.assertIn("a fact worth keeping", sol.block())


class PersonaTests(unittest.TestCase):
    def test_sols_spec_ships_with_the_package(self):
        self.assertTrue(config.SOL_PERSONA_PATH.exists(),
                        f"{config.SOL_PERSONA_PATH} is missing")
        self.assertTrue(persona.load_sol_spec().strip())

    def test_a_missing_spec_is_not_fatal(self):
        """Luna can still dispatch workers without Sol's spec on disk."""
        self.assertEqual(persona.load_sol_spec(config.DATA_DIR / "nope.md"), "")

    def test_sols_prompt_says_he_reports_to_luna_not_the_user(self):
        prompt = persona.build_dispatch_system_prompt(to="sol")
        self.assertIn("report to Luna", prompt)
        self.assertIn("not your audience", prompt)

    def test_sols_prompt_names_his_namespace_and_forbids_lunas(self):
        prompt = persona.build_dispatch_system_prompt(
            to="sol", memory_dir="/home/x/.local/share/luna/memory/sol")
        self.assertIn("/home/x/.local/share/luna/memory/sol", prompt)
        self.assertIn("LUNA.md", prompt)
        self.assertIn("do not write to", prompt)

    def test_sols_memory_block_reaches_his_prompt(self):
        prompt = persona.build_dispatch_system_prompt(
            to="sol", memory_block="- foot propagates its child's exit code")
        self.assertIn("foot propagates", prompt)

    def test_a_worker_gets_neither_the_spec_nor_the_namespace(self):
        prompt = persona.build_dispatch_system_prompt(
            to="worker", memory_block="- a sol note",
            memory_dir="/somewhere/sol")
        self.assertNotIn("a sol note", prompt)
        self.assertNotIn("/somewhere/sol", prompt)

    def test_the_spec_states_the_report_shape(self):
        spec = persona.load_sol_spec()
        for heading in ("Finding", "Evidence", "did not check"):
            with self.subTest(heading=heading):
                self.assertIn(heading, spec)

    def test_the_spec_forbids_writing_lunas_memory(self):
        spec = persona.load_sol_spec()
        self.assertIn("LUNA.md", spec)
        self.assertIn("USER.md", spec)


class ServerNamespaceTests(TempMemoryCase):
    """The wall holds over the socket, too, not only in the objects."""

    def daemon(self):
        from lunad import dispatch
        from lunad.server import Daemon
        from ._support import FakeHyprland
        d = Daemon(agent_name="claude", memory=self.memory(),
                   sol_memory=self.sol_memory(), audit=self.audit,
                   dispatcher=dispatch.Dispatcher(
                       jobs_dir=self.root / "jobs", hypr=FakeHyprland(),
                       audit=self.audit, terminal="/bin/bash",
                       agent_bin="/bin/true"))
        self.addCleanup(d.close)
        return d

    def test_a_write_to_sols_namespace_lands_in_sols_file(self):
        d = self.daemon()
        resp = d.dispatch({"op": "memory.write", "namespace": "sol",
                           "entry": "a finding about foot"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["file"], "SOL.md")
        self.assertIn("a finding about foot", d.sol_memory.sol.text())
        self.assertNotIn("a finding about foot", d.memory.luna.text())

    def test_asking_sols_namespace_for_lunas_file_is_refused(self):
        d = self.daemon()
        resp = d.dispatch({"op": "memory.write", "namespace": "sol",
                           "file": "LUNA.md", "entry": "sneaky"})
        self.assertFalse(resp["ok"])
        self.assertIn("belongs to Luna", resp["message"])
        self.assertEqual(d.memory.luna.entries(), [])

    def test_an_unknown_namespace_is_refused(self):
        d = self.daemon()
        resp = d.dispatch({"op": "memory.read", "namespace": "mallory"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "ProtocolError")

    def test_reading_sols_namespace_shows_only_sols_file(self):
        d = self.daemon()
        d.memory.luna.append("a luna fact")
        d.sol_memory.sol.append("a sol fact")
        resp = d.dispatch({"op": "memory.read", "namespace": "sol"})
        self.assertTrue(resp["ok"])
        self.assertEqual(list(resp["tier1"]), ["SOL.md"])
        self.assertIn("a sol fact", resp["tier1"]["SOL.md"]["entries"][0])

    def test_the_default_namespace_is_still_lunas(self):
        d = self.daemon()
        resp = d.dispatch({"op": "memory.read"})
        self.assertEqual(resp["namespace"], "luna")
        self.assertEqual(sorted(resp["tier1"]), ["LUNA.md", "USER.md"])

    def test_memory_writes_are_audited_with_the_namespace_as_the_actor(self):
        d = self.daemon()
        d.dispatch({"op": "memory.write", "namespace": "sol",
                    "entry": "audited finding"})
        [entry] = self.audit.read(action="memory.write")
        self.assertEqual(entry["actor"], "sol")
        self.assertEqual(entry["file"], "SOL.md")
        self.assertIn("undo", entry)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
