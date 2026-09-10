"""The GUI's schema against the daemon's, key for key.

There was no test on this axis, and it showed. `tests/test_schema.py` checks
the GUI against `docs/CONFIG-SCHEMA.md`, which documents *defaults* — so
defaults could not drift. `tests/test_contract.py::DriftCase` in the root
suite checks lunad's schema against `lunad/config.py`'s fallback constants.
Nothing compared the two schemas to each other, so **ranges** drifted freely,
and fourteen of them had:

    voice.speed                    GUI 0.5-2.0     daemon 0.25-4.0
    memory.luna_cap_chars          GUI 500-20000   daemon 200-100000
    dispatch.max_parallel          GUI 1-8         daemon 1-16
    ... eleven more

A narrower GUI range is not a harmless conservatism, because
`jarvis/config.py::_accept` is a *read* path: a value the daemon accepts but
the GUI's range rejects is not shown as-is with a warning, it is replaced by
the GUI's default and written back on the next save. So `luna settings set
voice.speed 3.0` worked, spoke faster, and was silently reset to 1.0 the next
time anybody opened this window and changed anything at all.

Two schemas describing one file have to agree about what the file may contain.
This asserts that they do.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from jarvis import schema  # noqa: E402

try:
    from lunad import settings as daemon  # noqa: E402
except Exception:                                    # pragma: no cover
    daemon = None

#: The three policy values. `lunad`'s schema records these keys as
#: `kind="policy"` with an empty `choices` tuple — the values are validated by
#: the kind rather than listed — so they are compared against the constant
#: instead of against an empty tuple.
POLICY = ("never", "ask", "deny")


def daemon_keys() -> dict:
    return {f"{sec.name}.{key.name}": key
            for sec in daemon.SCHEMA for key in sec.keys}


def gui_fields() -> dict:
    return {f"{sec.key}.{fld.key}": fld
            for sec in schema.SPEC for fld in sec.fields}


@unittest.skipUnless(daemon is not None,
                     "lunad is not importable from here")
class DaemonContractTest(unittest.TestCase):
    def setUp(self):
        self.daemon = daemon_keys()
        self.gui = gui_fields()

    def test_the_two_schemas_describe_the_same_keys(self):
        self.assertEqual(sorted(self.gui), sorted(self.daemon))

    def test_every_default_agrees(self):
        for dotted, fld in self.gui.items():
            with self.subTest(key=dotted):
                want = self.daemon[dotted].default
                if isinstance(want, float) or isinstance(fld.default, float):
                    self.assertAlmostEqual(float(fld.default), float(want),
                                           places=6)
                else:
                    self.assertEqual(fld.default, want)

    def test_every_range_is_the_daemon_s_range(self):
        """The one that was drifting.

        Not "within" — equal. A GUI that offers less than the daemon accepts
        overwrites legal values; a GUI that offers more writes values the
        daemon will reject on load. Both are the same bug from opposite ends.
        """
        for dotted, fld in self.gui.items():
            if not hasattr(fld, "min"):
                continue
            key = self.daemon[dotted]
            with self.subTest(key=dotted):
                self.assertIsNotNone(
                    key.minimum,
                    f"{dotted} has a range in the GUI and none in the daemon")
                self.assertAlmostEqual(float(fld.min), float(key.minimum),
                                       places=6)
                self.assertAlmostEqual(float(fld.max), float(key.maximum),
                                       places=6)

    def test_a_daemon_range_is_never_left_unoffered(self):
        """The mirror: a bounded key the GUI presents as unbounded."""
        for dotted, key in self.daemon.items():
            if key.minimum is None and key.maximum is None:
                continue
            with self.subTest(key=dotted):
                self.assertTrue(hasattr(self.gui[dotted], "min"),
                                f"{dotted} is bounded in the daemon and the "
                                f"GUI renders it as an unbounded field")

    def test_every_choice_list_agrees(self):
        for dotted, fld in self.gui.items():
            options = tuple(getattr(fld, "options", ()) or ())
            if not options:
                continue
            key = self.daemon[dotted]
            with self.subTest(key=dotted):
                want = (POLICY if key.kind == "policy"
                        else tuple(key.choices))
                self.assertEqual(options, want)

    def test_the_daemon_accepts_every_option_the_gui_offers(self):
        """Belt as well as braces: the lists match *and* each value coerces."""
        for dotted, fld in self.gui.items():
            for option in tuple(getattr(fld, "options", ()) or ()):
                with self.subTest(key=dotted, option=option):
                    self.assertEqual(self.daemon[dotted].coerce(option),
                                     option)

    def test_the_daemon_accepts_both_ends_of_every_range(self):
        for dotted, fld in self.gui.items():
            if not hasattr(fld, "min"):
                continue
            key = self.daemon[dotted]
            for edge in (fld.min, fld.max):
                with self.subTest(key=dotted, edge=edge):
                    self.assertAlmostEqual(float(key.coerce(edge)),
                                           float(edge), places=6)

    def test_every_kind_lines_up(self):
        """A GUI float over a daemon int writes 1.0 where 1 belongs, and TOML
        keeps the difference."""
        for dotted, fld in self.gui.items():
            kind = self.daemon[dotted].kind
            with self.subTest(key=dotted, kind=kind):
                if isinstance(fld, schema.Toggle):
                    self.assertEqual(kind, "bool")
                elif isinstance(fld, schema.Tri):
                    self.assertEqual(kind, "policy")
                elif isinstance(fld, schema.Real):
                    self.assertEqual(kind, "float")
                elif isinstance(fld, schema.Number):
                    self.assertEqual(kind, "int")
                else:
                    self.assertEqual(kind, "str")


if __name__ == "__main__":
    unittest.main()
