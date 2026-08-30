"""Jarvis settings schema — a literal transcription of docs/CONFIG-SCHEMA.md.

SPEC is the single source of truth for the GUI, the validator and the TOML
emitter. Adding a setting is one line here and it appears in the app.

Every key in CONFIG-SCHEMA.md must exist here, with the same default, or the
GUI silently stops being able to edit part of the contract. tests/test_schema.py
parses the schema doc and asserts exactly that, so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- field kinds


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    doc: str = ""
    readonly: bool = False      # displayed, never written by the GUI


@dataclass(frozen=True)
class Toggle(Field):
    default: bool = False


@dataclass(frozen=True)
class Number(Field):
    """Integer with an inclusive range."""
    default: int = 0
    min: int = 0
    max: int = 100
    step: int = 1
    unit: str = ""


@dataclass(frozen=True)
class Real(Field):
    """Float with an inclusive range. Separate from Number because TOML
    distinguishes 1 from 1.0 and lunad's config reader will too."""
    default: float = 0.0
    min: float = 0.0
    max: float = 1.0
    step: float = 0.1
    digits: int = 2
    unit: str = ""


@dataclass(frozen=True)
class Choice(Field):
    default: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Text(Field):
    default: str = ""
    placeholder: str = ""
    allow_empty: bool = True


@dataclass(frozen=True)
class VoicePick(Field):
    """A Choice whose options are discovered at runtime from the sample WAVs
    in ~/Music/luna-voices/. Validation therefore accepts any non-empty
    string — a voice the daemon knows about but which has no local sample
    must not be rejected by the GUI."""
    default: str = ""


# "never" (just do it) | "ask" (confirm first) | "deny" (refuse outright)
TRI = ("never", "ask", "deny")
TRI_LABELS = {"never": "Never ask", "ask": "Ask first", "deny": "Never allow"}


@dataclass(frozen=True)
class Tri(Field):
    default: str = "ask"
    options: tuple[str, ...] = TRI


@dataclass(frozen=True)
class Section:
    key: str                     # TOML table name, may be dotted
    title: str
    pane: str                    # which sidebar pane renders it
    fields: tuple[Field, ...] = ()
    doc: str = ""


# ---------------------------------------------------------------- the schema

SPEC: tuple[Section, ...] = (
    Section(
        key="assistant", title="Assistant", pane="assistant",
        doc="Who she is. The name is a setting so every prompt, greeting and "
            "log label follows it.",
        fields=(
            Text("name", "Name", "Display name, and how she refers to herself.",
                 default="Luna", placeholder="Luna", allow_empty=False),
            Text("specialist", "Specialist name",
                 "The delegate persona enrolled for deep technical work.",
                 default="Sol", placeholder="Sol", allow_empty=False),
            Choice("agent", "Agent",
                   "Headless CLI that does the thinking. Falls back to "
                   "~/.config/omarchy/defaults/agent.",
                   default="claude", options=("claude", "codex")),
            Text("model", "Model", "Empty means the agent's own default.",
                 default="", placeholder="agent default"),
        ),
    ),
    Section(
        key="voice", title="Voice out", pane="voice",
        fields=(
            Toggle("enabled", "Speak replies aloud", default=True),
            Choice("provider", "Provider", "Remote TTS, or local piper.",
                   default="openrouter", options=("openrouter", "piper")),
            Text("model", "TTS model", default="deepgram/flux-tts:free",
                 placeholder="deepgram/flux-tts:free"),
            VoicePick("voice", "Primary voice",
                      "The default voice (flux-alexis-en, female).",
                      default="flux-alexis-en"),
            VoicePick("voice_male", "Alternate voice",
                      "The selectable alternate (flux-donovan-en, male).",
                      default="flux-donovan-en"),
            Choice("fallback", "On provider failure",
                   "Used when the network or the provider fails.",
                   default="piper", options=("piper", "none")),
            Text("piper_voice", "Piper voice",
                 "Local ONNX voice used by the piper fallback.",
                 default="en_GB-jenny_dioco-medium", allow_empty=False),
            Real("speed", "Speed", default=1.0, min=0.5, max=2.0, step=0.05,
                 digits=2, unit="x"),
            Number("max_spoken_chars", "Max spoken characters",
                   "Longer replies are summarised for speech; the full text "
                   "still goes to screen.",
                   default=400, min=40, max=4000, step=20, unit="chars"),
        ),
    ),
    Section(
        key="listen", title="Listening", pane="listen",
        fields=(
            Toggle("enabled", "Listen for speech", default=True),
            Choice("provider", "Provider", default="openrouter",
                   options=("openrouter", "local")),
            Text("model", "STT model", default="fish-audio/transcribe-1",
                 placeholder="fish-audio/transcribe-1"),
            Text("language", "Language", "ISO code passed to the transcriber.",
                 default="en", placeholder="en", allow_empty=False),
            Text("keybind", "Talk-to-Luna keybind",
                 "Tap to start listening, tap again to send. Owned by "
                 "~/.config/hypr/bindings.lua. Shown here, edited there.",
                 default="F10", readonly=True),
        ),
    ),
    Section(
        key="confirm", title="Confirmations", pane="confirm",
        doc="The safety model. NOT hard blocks — Jarvis asks first, then "
            "proceeds.",
        fields=(
            Tri("install_packages", "Install packages",
                "pacman, yay, pip, npm — anything that adds software."),
            Tri("delete_files", "Delete files",
                "Removing anything from disk."),
            Tri("write_outside_home", "Write outside $HOME",
                "Any write with a destination that is not under ~."),
            Tri("system_config", "System configuration",
                "/etc, systemd units, Hyprland config."),
            Tri("network_send", "Send data off the machine",
                "Posting anything outward."),
            Tri("git_push", "git push", "Publishing commits to a remote."),
            Tri("long_job", "Long-running jobs",
                "Anything estimated over the threshold below."),
            Number("long_job_seconds", "Long job threshold",
                   default=300, min=10, max=7200, step=10, unit="s"),
            Tri("spend", "Metered spend",
                "Anything with a metered cost over the threshold below."),
            Real("spend_threshold", "Spend threshold", default=0.25, min=0.0,
                 max=100.0, step=0.05, digits=2, unit="$"),
        ),
    ),
    Section(
        key="confirm.prompt", title="How she asks", pane="confirm",
        fields=(
            Number("timeout_seconds", "Answer timeout",
                   "No answer within this is treated as the default below.",
                   default=60, min=5, max=600, step=5, unit="s"),
            Choice("default_on_timeout", "On timeout, assume",
                   default="no", options=("no", "yes")),
            Choice("channel", "Ask via", default="notification",
                   options=("notification", "terminal", "both")),
        ),
    ),
    Section(
        key="memory", title="Memory", pane="memory",
        fields=(
            Number("luna_cap_chars", "Assistant memory cap",
                   "LUNA.md. A write past the cap is rejected, not truncated "
                   "— overflow forces consolidation.",
                   default=3000, min=500, max=20000, step=100, unit="chars"),
            Number("user_cap_chars", "User memory cap",
                   "USER.md, the user model.",
                   default=2000, min=500, max=20000, step=100, unit="chars"),
            Number("consolidate_every_turns", "Consolidate every",
                   "Turns between background consolidation passes. Each one "
                   "is a model call on your account — set it to 0 to turn "
                   "them off entirely.",
                   default=12, min=0, max=200, step=1, unit="turns"),
            Number("decay_half_life_days", "Salience half-life",
                   "Trivia ages out on its own. Corrections never decay.",
                   default=30, min=1, max=365, step=1, unit="days"),
        ),
    ),
    Section(
        key="dispatch", title="Jobs", pane="jobs",
        fields=(
            Text("workspace", "Hyprland special workspace",
                 default="luna", allow_empty=False),
            Text("app_id", "Terminal app-id",
                 "The window rule matches this. Never org.omarchy.agent — "
                 "that would sweep live agent sessions into the hidden "
                 "workspace.",
                 default="org.omarchy.luna", allow_empty=False),
            Number("max_parallel", "Max parallel jobs",
                   default=1, min=1, max=8, step=1),
            Number("job_retention_days", "Keep job directories for",
                   default=14, min=1, max=365, step=1, unit="days"),
        ),
    ),
    Section(
        key="ui", title="Interface", pane="about",
        fields=(
            Toggle("theme_follows_omarchy", "Follow the Omarchy theme",
                   default=True),
            Toggle("notify_on_finish", "Notify when a job finishes",
                   default=True),
        ),
    ),
)


# ------------------------------------------------- the four immovable denies
#
# From CONFIG-SCHEMA.md: "These are ALWAYS 'deny' and are not user-editable —
# they exist to protect other running sessions and the machine's own record of
# itself." They are NOT config keys. They are never read from or written to
# config.toml, and the GUI renders them as text, not as disabled widgets, so
# there is nothing on screen that could be made editable by a stray
# set_sensitive(True).

HARD_DENIES: tuple[tuple[str, str], ...] = (
    ("Signal a process Jarvis did not spawn",
     "Other sessions run here, and Linux recycles pids."),
    ("Restart omarchy-shell",
     "The bar and every popup share one engine; a restart mid-reload "
     "segfaults the desktop."),
    ("Delete ~/.config/omarchy/CUSTOMISATIONS.md",
     "That file is the machine's record of itself; without it a change "
     "cannot be undone."),
    ("rm -rf outside Jarvis's own directories",
     "A recursive delete has no inverse. The audit log cannot bring the "
     "bytes back."),
)


# ---------------------------------------------------------------- accessors

_BY_KEY = {f"{s.key}.{f.key}": (s, f) for s in SPEC for f in s.fields}


def sections_for(pane: str) -> tuple[Section, ...]:
    return tuple(s for s in SPEC if s.pane == pane)


def field_for(dotted: str) -> tuple[Section, Field] | None:
    return _BY_KEY.get(dotted)


def known_keys() -> frozenset[str]:
    return frozenset(_BY_KEY)


def writable_keys() -> frozenset[str]:
    return frozenset(k for k, (_s, f) in _BY_KEY.items() if not f.readonly)


def defaults() -> dict:
    return {k: f.default for k, (_s, f) in _BY_KEY.items()}
