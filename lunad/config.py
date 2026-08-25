"""Paths and tunable constants for Luna.

Every path Luna owns is derived here so that nothing else in the package
hard-codes a location. State lives under XDG data; the socket lives under
XDG runtime (tmpfs, cleared on reboot, which is what we want for a socket).
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Filesystem layout ----------------------------------------------------

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent                       # ~/Work/luna
DATA_DIR = PROJECT_DIR / "data"                    # shipped, read-only assets
PERSONA_PATH = DATA_DIR / "persona.md"


def _xdg(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else default


HOME = Path.home()
STATE_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / "luna"
MEMORY_DIR = STATE_DIR / "memory"
LOG_PATH = STATE_DIR / "luna.log"
AUDIT_PATH = STATE_DIR / "audit.jsonl"             # Phase 2; created lazily
AGENT_CWD = STATE_DIR / "agent-cwd"                # neutral cwd for agent runs

LUNA_MD = MEMORY_DIR / "LUNA.md"
USER_MD = MEMORY_DIR / "USER.md"
EPISODES_DB = MEMORY_DIR / "episodes.db"
PROFILE_JSON = MEMORY_DIR / "profile.json"         # Tier 3, stubbed

RUNTIME_DIR = _xdg("XDG_RUNTIME_DIR", Path(f"/run/user/{os.getuid()}")) / "luna"
SOCKET_PATH = RUNTIME_DIR / "luna.sock"

OMARCHY_DEFAULT_AGENT = HOME / ".config" / "omarchy" / "defaults" / "agent"

# --- Memory caps (ARCHITECTURE.md section 4, tier 1) ----------------------

LUNA_MD_CAP = 3000
USER_MD_CAP = 2000
ENTRY_DELIMITER = "§"                         # section sign, from Hermes

# --- Salience / decay -----------------------------------------------------

SALIENCE_HALF_LIFE_DAYS = 14.0
CORRECTION_SALIENCE = 1.0                          # exempt from decay

# --- Agent invocation -----------------------------------------------------

AGENT_TIMEOUT_S = 180
DEFAULT_MODEL = None                               # None -> agent's own default

# --- Logging --------------------------------------------------------------

LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5

# --- Recall ---------------------------------------------------------------

RECALL_LIMIT = 6                                   # tier-2 episodes per prompt


def ensure_dirs() -> None:
    """Create every directory Luna writes into. Idempotent."""
    for d in (STATE_DIR, MEMORY_DIR, AGENT_CWD, RUNTIME_DIR):
        d.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.chmod(0o700)
