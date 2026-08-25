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

# --- Voice / TTS (ARCHITECTURE.md section 5) ------------------------------
#
# piper lives in a project venv, not system-wide: sudo needs a password on this
# machine, so an unattended AUR build is impossible and the venv is revertible
# with a single `rm -rf`.

VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
PIPER_WORKER = PKG_DIR / "piper_worker.py"

VOICES_DIR = STATE_DIR / "voices"
VOICE_NAME = "en_GB-jenny_dioco-medium"
VOICE_ONNX = VOICES_DIR / f"{VOICE_NAME}.onnx"
VOICE_CONFIG = VOICES_DIR / f"{VOICE_NAME}.onnx.json"   # sample rate read here

APLAY_BIN = "aplay"

# 331 MB resident but only 1.12 s to cold-load, against 3-4 GB of headroom:
# holding the model permanently is the wrong trade. Unload when idle.
SPEECH_IDLE_UNLOAD_S = 300.0
SPEECH_START_TIMEOUT_S = 60.0        # cold import of onnxruntime, then the model
SPEECH_MAX_CHARS = 700               # spoken replies are short; detail goes to screen
SPEECH_MAX_SENTENCE_CHARS = 260      # one aplay-fed unit; longer gets sub-split
SPEECH_PLACEHOLDER = "it's on screen"

# --- Voice router (voxtype post_process hook) ------------------------------

ROUTER_LOG = STATE_DIR / "voice-router.log"
ROUTER_LOG_MAX_BYTES = 262_144
ROUTER_CONNECT_TIMEOUT_S = 1.0       # < post_process_timeout_ms (2000)
ROUTER_MAX_TRANSCRIPT_CHARS = 8_000

# --- Agent invocation -----------------------------------------------------

AGENT_TIMEOUT_S = 180
DEFAULT_MODEL = None                               # None -> agent's own default

# --- Conversation sessions (prompt-cache reuse) ---------------------------
#
# A fresh `claude` process re-*creates* the ~8k-token system prompt cache on
# every call. Resuming one session id keeps the prefix warm, turning a cache
# write into a cache read. Sessions are dropped when tier-1 memory changes,
# because the frozen block in the prefix would then be stale.

SESSION_IDLE_S = 1800.0                            # 30 min without a turn
SESSION_MAX_TURNS = 60                             # then start clean
DEFAULT_CONVERSATION = "default"

# --- Logging --------------------------------------------------------------

LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5

# --- Recall ---------------------------------------------------------------

RECALL_LIMIT = 6                                   # tier-2 episodes per prompt


def ensure_dirs() -> None:
    """Create every directory Luna writes into. Idempotent."""
    for d in (STATE_DIR, MEMORY_DIR, AGENT_CWD, RUNTIME_DIR, VOICES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.chmod(0o700)
