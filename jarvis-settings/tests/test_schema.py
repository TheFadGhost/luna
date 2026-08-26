"""The GUI must be able to edit every setting in the contract.

docs/CONFIG-SCHEMA.md is the contract, and its TOML block is machine-readable,
so this test reads the document itself rather than a copy of it. If the daemon
agent adds a key to the schema doc and Jarvis has no widget for it, this fails.
"""

import pathlib
import sys
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import schema  # noqa: E402

DOC = ROOT.parent / "docs" / "CONFIG-SCHEMA.md"


def doc_block() -> dict:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("```toml") + len("```toml")
    # The doc's fence is not always closed (the block runs to EOF), so a
    # missing terminator is normal, not a parse failure.
    end = text.find("```", start)
    return tomllib.loads(text[start:] if end < 0 else text[start:end])


def flatten(node, prefix=""):
    out = {}
    for k, v in node.items():
        dotted = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, dotted))
        else:
            out[dotted] = v
    return out


@unittest.skipUnless(DOC.exists(), "CONFIG-SCHEMA.md not present")
class ContractTest(unittest.TestCase):
    def setUp(self):
        self.contract = flatten(doc_block())

    def test_every_contract_key_is_editable(self):
        missing = sorted(set(self.contract) - set(schema.known_keys()))
        self.assertEqual(missing, [], f"no GUI control for: {missing}")

    def test_no_invented_keys(self):
        extra = sorted(set(schema.known_keys()) - set(self.contract))
        self.assertEqual(extra, [], f"not in the contract: {extra}")

    def test_defaults_match_the_contract(self):
        for dotted, want in self.contract.items():
            with self.subTest(dotted):
                got = schema.field_for(dotted)[1].default
                if isinstance(want, float) or isinstance(got, float):
                    self.assertAlmostEqual(float(got), float(want))
                else:
                    self.assertEqual(got, want)

    def test_contract_values_all_validate(self):
        from jarvis import config
        for dotted, want in self.contract.items():
            fld = schema.field_for(dotted)[1]
            if fld.readonly:
                continue
            with self.subTest(dotted):
                config.coerce(dotted, want)   # must not raise


class HardDenyTest(unittest.TestCase):
    def test_four_denies_exist_and_are_not_settings(self):
        self.assertEqual(len(schema.HARD_DENIES), 4)
        for name, reason in schema.HARD_DENIES:
            self.assertTrue(reason.strip(), f"{name} has no stated reason")
        # They must not be config keys under any spelling.
        keys = {k.rpartition(".")[2] for k in schema.known_keys()}
        for bad in ("signal", "restart_shell", "delete_customisations",
                    "rm_rf"):
            self.assertNotIn(bad, keys)

    def test_hard_denies_are_never_written(self):
        """No code path may emit them: they are absent from SPEC, so
        config.save could not name them even if asked."""
        from jarvis import config
        with self.assertRaises(config.ValidationError):
            config.coerce("confirm.restart_omarchy_shell", "never")


class TriTest(unittest.TestCase):
    def test_three_way(self):
        self.assertEqual(schema.TRI, ("never", "ask", "deny"))
        for opt in schema.TRI:
            self.assertIn(opt, schema.TRI_LABELS)

    def test_every_action_class_is_three_way(self):
        tri = [f.key for s in schema.SPEC if s.key == "confirm"
               for f in s.fields if isinstance(f, schema.Tri)]
        self.assertEqual(sorted(tri), sorted([
            "delete_files", "git_push", "install_packages", "long_job",
            "network_send", "spend", "system_config", "write_outside_home"]))


if __name__ == "__main__":
    unittest.main()
