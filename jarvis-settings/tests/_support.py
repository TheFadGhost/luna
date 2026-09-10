"""Shared test scaffolding — and the guard that keeps this suite indoors.

The lunad suite has `tests/_support.py`, whose whole subject is that a name
bound to something real will eventually be reached by a test that forgot to
stub it: three `foot` windows a run, then ten Omarchy toasts a run. Jarvis had
no equivalent because it had nothing to reach with; it read the user's files
and wrote its own.

`[listen]` write-through changed that. `jarvis.voxtype` names another
application's config file and a `systemctl` command that **restarts a daemon**,
and a case that forgets to pass a temporary path would rewrite
`~/.config/voxtype/config.toml` and bounce the real voxtype — losing whatever
the user happened to be dictating at the time. So all four names are replaced
here, process-wide, at import:

  CONFIG_PATH   -> a path inside a throwaway tree that does not exist, so a
                   forgotten argument raises VoxtypeError naming the file
                   rather than editing the user's own
  RESTART_COMMAND -> a binary name that cannot resolve, so `restart()` reports
                   a failure instead of restarting voxtype
  STATE_PATH    -> absent, so `activity()` answers "unknown" whatever the real
                   daemon is doing. Without this a case would pass or fail
                   depending on whether somebody was speaking at the time.
  PID_PATH      -> absent, so `running_pid()` answers None and no test can
                   read the live daemon's `/proc` entry

`tests/test_voxtype.py::GuardTest` asserts the arrangement, including that none
of them may go back to being a real path.
"""

from __future__ import annotations

import atexit
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import voxtype  # noqa: E402

_GUARD = tempfile.TemporaryDirectory(prefix="jarvis-tests-voxtype-")
atexit.register(_GUARD.cleanup)
_DIR = pathlib.Path(_GUARD.name)

FORBIDDEN_CONFIG = _DIR / "jarvis-tests-must-pass-a-path" / "config.toml"
FORBIDDEN_RESTART = ("jarvis-tests-must-pass-restart=systemctl", "--user",
                     "restart", "voxtype")
FORBIDDEN_STATE = _DIR / "no-state-file"
FORBIDDEN_PID = _DIR / "no-pid-file"

voxtype.CONFIG_PATH = FORBIDDEN_CONFIG
voxtype.RESTART_COMMAND = FORBIDDEN_RESTART
voxtype.STATE_PATH = FORBIDDEN_STATE
voxtype.PID_PATH = FORBIDDEN_PID


# --------------------------------------------------------------------------
# And the app's *own* config file, which was never guarded at all.
#
# The reasoning above — "Jarvis had nothing to reach with; it read the user's
# files and wrote its own" — quietly assumed no test would ever build an
# `Editor`. Nothing did, because nothing built a pane. A pane test does: an
# `Editor` reads `jarvis.config.CONFIG_PATH` on construction and `Editor.set`
# writes it back through a GLib timeout, so a case that flips a switch to
# check that the row below it dims would rewrite `~/.config/jarvis/config.toml`
# — the user's live settings, read by the running daemon within two seconds.
#
# This one has to *work* rather than merely fail, since the app creates and
# rewrites it, so it is a real file in a throwaway tree, seeded with the
# defaults. `tests/test_panes.py::GuardTest` asserts it is never the real one.
from jarvis import config as _config  # noqa: E402

FORBIDDEN_JARVIS_CONFIG = _DIR / "config" / "config.toml"

_config.CONFIG_DIR = FORBIDDEN_JARVIS_CONFIG.parent
_config.CONFIG_PATH = FORBIDDEN_JARVIS_CONFIG
_config.write_default(FORBIDDEN_JARVIS_CONFIG)
