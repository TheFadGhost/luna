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
AUDIT_PATH = STATE_DIR / "audit.jsonl"             # append-only, never rotated
SPAWNED_PATH = STATE_DIR / "spawned.json"          # the signal allowlist
JOBS_DIR = STATE_DIR / "jobs"                      # one directory per dispatch
AGENT_CWD = STATE_DIR / "agent-cwd"                # neutral cwd for agent runs

LUNA_MD = MEMORY_DIR / "LUNA.md"
USER_MD = MEMORY_DIR / "USER.md"
EPISODES_DB = MEMORY_DIR / "episodes.db"
PROFILE_JSON = MEMORY_DIR / "profile.json"         # Tier 3, derived from tier 2

# Sol's namespace. Separate directory, separate files, separate episode store:
# Sol reports to Luna and must never write into LUNA.md or USER.md.
SOL_MEMORY_DIR = MEMORY_DIR / "sol"
SOL_MD = SOL_MEMORY_DIR / "SOL.md"
SOL_EPISODES_DB = SOL_MEMORY_DIR / "episodes.db"
SOL_PERSONA_PATH = DATA_DIR / "sol-persona.md"

RUNTIME_DIR = _xdg("XDG_RUNTIME_DIR", Path(f"/run/user/{os.getuid()}")) / "luna"
SOCKET_PATH = RUNTIME_DIR / "luna.sock"

OMARCHY_DEFAULT_AGENT = HOME / ".config" / "omarchy" / "defaults" / "agent"

# --- Jarvis: the app's own configuration home ------------------------------
#
# The *app* is Jarvis; the assistant's name is a setting inside it. The config
# directory is therefore ~/.config/jarvis and not ~/.config/luna, and it is the
# one directory Jarvis owns outside its state tree. Secrets never live in the
# TOML: they live beside it in an env file that systemd reads, so a config file
# the settings GUI rewrites can never carry a key into a backup or a commit.

CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config") / "jarvis"
CONFIG_PATH = CONFIG_DIR / "config.toml"
SECRETS_PATH = CONFIG_DIR / "secrets.env"

# The key Luna already has on this machine. voxtype owns that file; Jarvis
# reads it when it has no key of its own and never writes to it.
VOXTYPE_SECRETS_PATH = _xdg("XDG_CONFIG_HOME", HOME / ".config") / "voxtype" / "secrets.env"

CONFIG_DIR_MODE = 0o700
CONFIG_FILE_MODE = 0o600

# Hot reload. A stat every two seconds costs nothing measurable and needs no
# inotify dependency; two seconds is also faster than a user can change a
# setting in the GUI and then speak.
CONFIG_POLL_S = 2.0

# --- OpenRouter TTS -------------------------------------------------------

OPENROUTER_SPEECH_URL = "https://openrouter.ai/api/v1/audio/speech"
OPENROUTER_TIMEOUT_S = 20.0          # one sentence, not a whole reply
OPENROUTER_KEY_ENVS = ("OPENROUTER_API_KEY", "VOXTYPE_WHISPER_API_KEY")

# --- Confirmation ---------------------------------------------------------

NOTIFY_BIN = "omarchy-notification-send"
CONFIRM_POLL_S = 0.25                # how often a waiting thread checks

# --- Memory caps (ARCHITECTURE.md section 4, tier 1) ----------------------
#
# Fallback defaults. `[memory] luna_cap_chars` and `[memory] user_cap_chars`
# override these at read time, so a cap changed in the GUI applies to the very
# next write. SOL_MD_CAP has no key of its own: Sol's namespace is not part of
# the user-facing contract, so it stays a constant on purpose.

LUNA_MD_CAP = 3000
USER_MD_CAP = 2000
SOL_MD_CAP = 3000
ENTRY_DELIMITER = "§"                         # section sign, from Hermes

# --- Salience / decay -----------------------------------------------------
#
# Fallback default for `[memory] decay_half_life_days`.
#
# 30, not the 14 this constant carried before the config contract was wired.
# Nothing user-facing ever said 14: CONFIG-SCHEMA.md, the Jarvis GUI and the
# user's own config.toml all said 30, and 14 only won because the setting was
# never read. Half-life is a *ranking* lift on recall, not a delete, and the
# salience score already carries a recency term of its own; a fortnight on top
# of that sinks a month-old episode below anything said this week, which is
# how a tier-2 store stops being worth searching.

SALIENCE_HALF_LIFE_DAYS = 30.0
CORRECTION_SALIENCE = 1.0                          # exempt from decay

# --- Tier 3: the derived profile ------------------------------------------
#
# All local, all stdlib, no model call. A rebuild reads at most PROFILE_WINDOW
# episodes and takes single-digit milliseconds, which is what lets it run on a
# turn counter without anyone budgeting for it.
#
# The window is a bound on *truth* as much as on cost: a profile built from
# every exchange since installation describes a person who has since changed
# their mind, and habits from six months ago are not evidence about today.

PROFILE_WINDOW = 2000                 # episodes read per rebuild, newest first
PROFILE_TOP_N = 6                     # facts kept per slot
PROFILE_EVIDENCE = 3                  # verbatim examples kept per signal

# --- Consolidation (ARCHITECTURE.md section 4) ----------------------------
#
# Fallback default for `[memory] consolidate_every_turns`. 0 there means never.
#
# The rest are not settings and are deliberately not offered as any: they are
# the cost envelope of a background pass that spends the user's own money, and
# a config file that lets someone set the episode limit to 5000 is a config
# file that lets someone set fire to their account overnight. The pass is
# bounded above by roughly 3k input tokens whatever the settings say.

CONSOLIDATE_EVERY_TURNS = 12
CONSOLIDATE_MIN_INTERVAL_S = 300.0    # floor between passes, whatever the count
CONSOLIDATE_EPISODE_LIMIT = 24        # episodes offered to one pass
CONSOLIDATE_EPISODE_CHARS = 240       # per side of an exchange, then clipped
CONSOLIDATE_ENTRY_CHARS = 300         # longest tier-1 entry a pass may propose
CONSOLIDATE_MAX_ADDITIONS = 4         # per file, per pass
CONSOLIDATE_TIMEOUT_S = 90.0          # a librarian's job, not a conversation

# --- Voice / TTS (ARCHITECTURE.md section 5) ------------------------------
#
# piper lives in a project venv, not system-wide: sudo needs a password on this
# machine, so an unattended AUR build is impossible and the venv is revertible
# with a single `rm -rf`.

VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
PIPER_WORKER = PKG_DIR / "piper_worker.py"

VOICES_DIR = STATE_DIR / "voices"
VOICE_NAME = "en_GB-jenny_dioco-medium"                 # `[voice] piper_voice`
VOICE_ONNX = VOICES_DIR / f"{VOICE_NAME}.onnx"
VOICE_CONFIG = VOICES_DIR / f"{VOICE_NAME}.onnx.json"   # sample rate read here


def voice_paths(name: str) -> tuple[Path, Path]:
    """The model and its config, for one piper voice name.

    Derived rather than stored so `[voice] piper_voice` can name any voice
    downloaded into VOICES_DIR without a second setting for each path.
    """
    return (VOICES_DIR / f"{name}.onnx", VOICES_DIR / f"{name}.onnx.json")


APLAY_BIN = "aplay"

# 331 MB resident but only 1.12 s to cold-load, against 3-4 GB of headroom:
# holding the model permanently is the wrong trade. Unload when idle.
SPEECH_IDLE_UNLOAD_S = 300.0
SPEECH_START_TIMEOUT_S = 60.0        # cold import of onnxruntime, then the model
# Fallback default for `[voice] max_spoken_chars`. 400, matching
# ARCHITECTURE.md section 5, CONFIG-SCHEMA.md and the GUI — the 700 this
# carried before was the odd one out and only ever surfaced when the config
# file was missing entirely.
SPEECH_MAX_CHARS = 400               # spoken replies are short; detail goes to screen
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

# --- Codex adapter --------------------------------------------------------
#
# codex has no `--tools ""`, so the sandbox *is* the tool policy. A
# conversational ask has no business writing to the disk, so it runs read-only;
# a dispatched job is real work and gets what Omarchy itself gives codex.
#
# Values: "read-only", "workspace-write", "danger-full-access", or "bypass"
# (which means --dangerously-bypass-approvals-and-sandbox, not a sandbox mode).

CODEX_ASK_SANDBOX = "read-only"
CODEX_DISPATCH_SANDBOX = "bypass"

# codex's analogue of claude's --safe-mode: do not load the user's own
# ~/.codex/config.toml or their execpolicy .rules into Luna's turns. Luna must
# not inherit the user's Codex setup, and must not change it either.
CODEX_IGNORE_USER_CONFIG = True

# Which config key carries Luna's persona into codex. codex 0.149.1 has no
# --append-system-prompt, so this is a `-c <key>=<persona>` override.
#
#   "developer_instructions" — layers a developer message on top of codex's
#       own base instructions. The default, and the measured better of the two:
#       same persona capture, 4,874 prompt tokens against 5,422, and a
#       dispatched session keeps codex's tool and patch guidance.
#   "instructions" — also captures the persona, but *replaces* codex's base
#       instructions. Costs more and strips the tool guidance. Switch here if
#       a harder identity override is ever wanted.
#
# Both are accepted under `--strict-config`; `base_instructions`,
# `system_prompt`, `persona` and `experimental_instructions_file` are not.
CODEX_PERSONA_KEY = "developer_instructions"

# Where the user's codex state lives. Read only to check that auth exists —
# Luna never writes here.
CODEX_HOME = _xdg("CODEX_HOME", HOME / ".codex")
CODEX_AUTH = CODEX_HOME / "auth.json"

# --- Dispatch (ARCHITECTURE.md section 6) ---------------------------------
#
# Luna's own special workspace. `scratchpad` is already bound to SUPER+S and
# belongs to the user; taking it would break a keybind they use.
# Fallback default for `[dispatch] workspace`.
LUNA_WORKSPACE = "luna"

# NOT `org.omarchy.agent`. That app-id is what `omarchy-launch-tui` gives the
# user's own agent terminals — there are several open on this machine right
# now — and a workspace rule matching it would sweep the user's live sessions
# into Luna's hidden workspace. Luna's terminals get an app-id of their own.
# Fallback default for `[dispatch] app_id`.
LUNA_APP_ID = "org.omarchy.luna"

TERMINAL_BIN = "foot"                              # Omarchy's terminal
HYPRCTL_BIN = "hyprctl"

DISPATCH_TIMEOUT_S = 3600.0                        # a real job, not an ask
DISPATCH_LINGER_S = 8.0                            # window stays up after exit
JOB_OUTPUT_MAX_CHARS = 20_000                      # what `luna jobs` will show
JOB_LIST_LIMIT = 20
SPAWN_LEDGER_MAX = 200                             # records kept in spawned.json

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
    for d in (STATE_DIR, MEMORY_DIR, SOL_MEMORY_DIR, JOBS_DIR, AGENT_CWD,
              RUNTIME_DIR, VOICES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.chmod(0o700)
    # 0700, always: the config directory sits next to a secrets file, and a
    # directory that is only private on the day it was created is not private.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(CONFIG_DIR_MODE)
