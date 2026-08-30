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

# Luna's own CLI, by absolute path. She is told about it in her system prompt
# and runs it from her own shell, and the shell she gets is lunad's — a systemd
# --user environment whose PATH need not contain ~/.local/bin. A bare `luna`
# in the prompt would work from the user's terminal and fail from hers, which
# is the worst of the two failures because it only shows up at runtime.
LUNA_CLI = PROJECT_DIR / "bin" / "luna"


def _xdg(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else default


HOME = Path.home()
STATE_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / "luna"
MEMORY_DIR = STATE_DIR / "memory"
LOG_PATH = STATE_DIR / "luna.log"
AUDIT_PATH = STATE_DIR / "audit.jsonl"             # append-only; rotates to .1 .. .N
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

# Luna's coarse state, one ASCII word, for anything on the desktop that wants
# to show what she is doing without holding a connection open. See
# lunad/presence.py for the contract and for why it is a file and not a
# subscription. Beside the socket on purpose: same tmpfs, same lifetime.
STATE_FILE = RUNTIME_DIR / "state"

# The HUD pane's caption, a sibling of `state` in the same tmpfs directory.
# One JSON object, written atomically, with a monotonically increasing `id`.
# The contract is HANDOFF-hud.md and the writer is `ambient.HudWriter`; the
# pane (~/.config/omarchy/plugins/ghost.lunahud) reads it through inotify, so
# nothing polls and an absent file simply means there is nothing to show.
HUD_MESSAGE_FILE = RUNTIME_DIR / "message"

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
# codex has no `--tools ""`, so the sandbox *is* the tool policy.
#
# Values: "read-only", "workspace-write", "danger-full-access", or "bypass"
# (which means --dangerously-bypass-approvals-and-sandbox, not a sandbox mode).
#
# CHANGED: the ask path was "read-only", and with it Luna's system prompt said
# she had no tools at all. That was true and it was the wrong trade. Asked to
# check a version she answered that she could not; asked what was on screen she
# gave advice about starting the daemon. A resident assistant that cannot look
# at the machine she lives on is a chatbot with a persona file.
#
# "bypass" rather than "danger-full-access", for two reasons that are about the
# mechanism and not about how much access she should have (the answer to that
# is: all of it — the user chose full autonomy and the audit log is the
# backstop):
#
#   * `codex exec resume` accepts no `-s`. Under a sandbox *mode* the policy has
#     to be re-stated as `-c sandbox_mode=…` on every resumed turn, and a
#     mode-vs-flag mismatch between turn one and turn two is exactly the kind of
#     thing that is discovered in production. `--dangerously-bypass-approvals-
#     and-sandbox` is accepted identically by `exec` and by `exec resume`.
#   * It also turns approvals off. `danger-full-access` removes the sandbox but
#     leaves the approval policy in place, and an approval request in a headless
#     turn is not a prompt anybody can answer — it is a hung ask.
#
# The dispatch path has run this way since Phase 2 and is unchanged.

CODEX_ASK_SANDBOX = "bypass"
CODEX_DISPATCH_SANDBOX = "bypass"

# The two model slugs, both verified present in ~/.codex/models_cache.json.
#
# They are constants here rather than settings because they are not a
# preference: `[assistant] model` exists for someone who wants to override the
# conversational model, and leaving it "" means "whatever the agent's own
# default is", which for codex-as-Luna is this. Naming a default per *adapter*
# rather than per *assistant* is what keeps `[assistant] model = ""` correct
# when the agent is claude, where a gpt-5.6 slug would be nonsense.
#
# Luna thinks; Sol works. Sol is the coding-agent model and it is what every
# dispatched session — specialist or anonymous worker — is given.
CODEX_ASK_MODEL = "gpt-5.6-luna"
CODEX_DISPATCH_MODEL = "gpt-5.6-sol"

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

# --- What Luna can see of the desktop (lunad/context.py) -------------------
#
# Two different reads of the same compositor, with two different budgets.
#
# The focused-window line rides on *every* ask, so it is bounded hard: one
# `hyprctl -j activewindow`, a second at the outside, and any failure at all
# means the ask goes out without it. A context line is worth a few tokens; it
# is not worth a question that does not get answered.
#
# `hyprctl -j activewindow` is a *query*. `hyprctl dispatch` on this machine
# (Hyprland 0.56.2, Lua config) evaluates its arguments as Lua and is a
# different and much sharper proposition — see dispatch.py. Nothing here
# dispatches.
#
# The screenshot is only ever taken when a look was actually asked for, never
# ambiently, and the file is deleted after the call whatever happens.

GRIM_BIN = "grim"                                  # the screenshot tool here
WINDOW_CONTEXT_TIMEOUT_S = 1.0                     # per ask, hard
SCREENSHOT_TIMEOUT_S = 10.0                        # grim on a 1900x1016 window

DISPATCH_TIMEOUT_S = 3600.0                        # a real job, not an ask
DISPATCH_LINGER_S = 8.0                            # window stays up after exit
JOB_OUTPUT_MAX_CHARS = 20_000                      # what `luna jobs` will show
JOB_LIST_LIMIT = 20
SPAWN_LEDGER_MAX = 200                             # records kept in spawned.json

# Fallback default for `[dispatch] max_parallel`. One, because two agent
# sessions on this 8 GB machine is already most of the headroom, and because a
# job the user cannot see is a job they cannot notice thrashing.
DISPATCH_MAX_PARALLEL = 1

# Fallback default for `[dispatch] job_retention_days`, and how often the
# collector wakes. Six hours, not once at startup: this daemon is meant to run
# for weeks, and a pass that only runs at boot never runs at all.
JOB_RETENTION_DAYS = 14
JOB_GC_INTERVAL_S = 21_600.0

# --- Audit log rotation ---------------------------------------------------
#
# Fallback defaults for `[audit] max_mb` and `[audit] keep`. The log is
# evidence, so rotation moves bytes rather than dropping them: the live file
# becomes `audit.jsonl.1`, each sibling shifts up one, and only the oldest of
# `AUDIT_KEEP` is ever deleted. Measured on real entries, a `dispatch.spawn`
# line is 633 bytes and a bare `process.spawned` 239, so 8 MB is on the order
# of twenty thousand actions and five siblings is months of history — bounded
# at 48 MB, which is nothing, and long enough that nobody rotates away the
# evidence of the week they are asking about.

AUDIT_MAX_MB = 8
AUDIT_KEEP = 5

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

# --- Ambient awareness ----------------------------------------------------
#
# The three things Luna notices without being asked. See lunad/ambient.py for
# what each hook watches and why it is cheap; these are the outward names it
# reads, and every one of them is redirected by tests/_support.py so the suite
# can never see a real coredump, a real battery or the real /usr/share.

#: systemd-coredump's store, `Storage=external` (the default here). 0755
#: root:root, so an unprivileged daemon can list it; each filename carries the
#: comm, uid, pid and microsecond, which is why the hook never forks
#: coredumpctl.
COREDUMP_DIR = Path("/var/lib/systemd/coredump")

#: Read, not assumed: the battery on this laptop is BAT1 (not BAT0) and reports
#: energy rather than charge, so the watcher finds it by `type == "Battery"`.
POWER_SUPPLY_DIR = Path("/sys/class/power_supply")

#: `omarchy update` is pacman with `--overwrite '/usr/share/omarchy/*'`, not a
#: git pull, so there is no HEAD to fingerprint. This file's contents *and*
#: mtime are the fingerprint: a same-version reinstall still clobbers the tree.
OMARCHY_VERSION_FILE = Path("/usr/share/omarchy/version")

#: `omarchy-update` wraps its whole run in `script` and logs here, on tmpfs.
#: A run that changed no package still moves this mtime; a reboot removes it,
#: so its absence proves nothing.
OMARCHY_UPDATE_LOG = Path("/tmp/omarchy-update.log")

#: What each watcher has already seen, so a daemon restart does not re-announce
#: a fortnight of coredumps.
AMBIENT_STATE_PATH = STATE_DIR / "ambient.json"

AMBIENT_POLL_S = 60.0                 # `[ambient] poll_seconds`
AMBIENT_UPDATE_EVERY_S = 300.0        # the update hook's own, slower cadence
AMBIENT_BATTERY_LOW_PCT = 20          # above Omarchy's own 10% toast
AMBIENT_BATTERY_CRITICAL_PCT = 5      # below it, above UPower's 2% hibernate
AMBIENT_CRASH_BURST = 3               # more than this in one tick coalesces
AMBIENT_RECENT_DUMPS = 64             # dump names remembered for de-duplication
AMBIENT_HUD_TTL_S = 12.0              # seconds an ambient caption stays up
AMBIENT_DIAGNOSE_TIMEOUT_S = 900.0    # ceiling on a dispatched crash diagnosis


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


class VanishedDirectory(OSError):
    """The directory a file lives in existed once and has since been removed.

    Every sink in the daemon creates its own directory on first use, which is
    right on a first run and wrong afterwards: a directory that existed and no
    longer does was deleted underneath a live object, and rebuilding it makes
    the deletion look like it never happened.

    In production nothing deletes `~/.local/share/luna` or `~/.config/jarvis`
    out from under a running daemon, so this cannot fire. Under test it fires
    constantly in one specific shape -- a thread outliving the case that
    started it, writing into a temporary tree teardown has already removed,
    and rebuilding the tree on the way in. That is the stray `/tmp/luna-test-*`
    CI fails a run for, and it is why every caller here is already inside a
    `try: ... except OSError` that logs and carries on: a write that cannot
    land is not a reason to take the daemon down.
    """


def ensure_parent(path: Path, *, existed: bool) -> None:
    """Create ``path``'s directory, unless it existed once and has vanished."""
    parent = path.parent
    if existed and not parent.is_dir():
        raise VanishedDirectory(
            f"{parent} has gone since it was opened; refusing to recreate it")
    parent.mkdir(parents=True, exist_ok=True)
