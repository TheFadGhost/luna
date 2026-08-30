"""``~/.config/jarvis/config.toml`` — the one place a user changes anything.

docs/CONFIG-SCHEMA.md is the contract, and this module is its executable copy:
the schema table below carries the default, the type, the allowed values *and*
the comment for every key, so the file Jarvis writes and the document a human
reads cannot drift apart without one of them failing a test.

Four properties, in the order they matter:

**Reading is stdlib.** Python 3.14 ships ``tomllib``, so nothing is vendored.

**Writing is hand-rolled.** ``tomllib`` is read-only and there is no writer in
the standard library. A dependency for this would be absurd, and a naive
``json``-ish dump would throw away every comment in the file — which is most of
what makes a config file usable. The serialiser here emits the schema's own
comments, in the schema's own order, so a file Jarvis rewrites looks like the
file a person wrote.

**An invalid value is a warning, never a crash.** A settings GUI is being
written against this file by somebody else; a typo in it must cost one logged
warning and a fall back to the default, not a dead daemon and a silent desktop.

**Secrets are not here.** ``config.toml`` is world-readable-ish by accident all
the time — it gets copied into backups, pasted into bug reports, and rewritten
by a GUI. The API key lives in ``secrets.env`` next to it, 0600, loaded by
systemd. :func:`api_key` reads the environment, and falls back to reading the
file directly for the times lunad is started by hand.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config

log = logging.getLogger("lunad.settings")

POLICIES = ("never", "ask", "deny")


class SettingsError(Exception):
    """A request to change a setting that cannot be honoured."""

    kind = "SettingsError"

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": str(self)}


# =========================================================================
# The schema — CONFIG-SCHEMA.md, as data
# =========================================================================


@dataclass(frozen=True)
class Key:
    """One setting: its default, how to validate it, and why it exists."""

    name: str
    default: Any
    kind: str                              # str | bool | int | float | policy
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    comment: str = ""

    def coerce(self, value: Any) -> Any:
        """Return ``value`` as this key's type, or raise ``ValueError``.

        TOML distinguishes 1 from 1.0 and the GUI will not always be careful,
        so an int where a float belongs is widened rather than rejected. A
        bool where a number belongs is *not*: in Python that would silently
        succeed, and ``speed = true`` becoming ``1.0`` is a bug that never
        surfaces.
        """
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError(f"expected true or false, got {value!r}")
        if self.kind in ("str", "policy"):
            if not isinstance(value, str):
                raise ValueError(f"expected a string, got {type(value).__name__}")
            allowed = POLICIES if self.kind == "policy" else self.choices
            if allowed and value not in allowed:
                raise ValueError(
                    f"expected one of {', '.join(allowed)}, got {value!r}")
            return value
        if isinstance(value, bool):
            raise ValueError(f"expected a number, got {value!r}")
        if self.kind == "int":
            if not isinstance(value, int):
                raise ValueError(f"expected a whole number, got {value!r}")
            number: float = value
        else:
            if not isinstance(value, (int, float)):
                raise ValueError(f"expected a number, got {value!r}")
            number = float(value)
        if self.minimum is not None and number < self.minimum:
            raise ValueError(f"must be at least {self.minimum}, got {number}")
        if self.maximum is not None and number > self.maximum:
            raise ValueError(f"must be at most {self.maximum}, got {number}")
        return int(number) if self.kind == "int" else float(number)


@dataclass(frozen=True)
class Section:
    """A ``[table]`` in the file, with the prose that belongs above it."""

    name: str
    keys: tuple[Key, ...]
    header: tuple[str, ...] = ()           # comment lines above `[name]`
    footer: tuple[str, ...] = ()           # comment lines below the last key
    align: int = 0                         # key column width; 0 = one space

    def key(self, name: str) -> Key | None:
        for k in self.keys:
            if k.name == name:
                return k
        return None


SCHEMA: tuple[Section, ...] = (
    Section(
        "assistant",
        align=12,
        keys=(
            Key("name", "Luna", "str",
                comment="display name + how she refers to herself"),
            Key("specialist", "Sol", "str", comment="the delegate persona"),
            # codex, not claude. Luna's brain is `codex` running
            # `gpt-5.6-luna`: it has a shell, web access and native vision, and
            # the ask path now runs with all three switched on. This key is the
            # one that decides — deliberately here and NOT in
            # ~/.config/omarchy/defaults/agent, which is the whole desktop's
            # default agent and is read by things that are not Luna. Changing
            # that file to change her brain would have been a change to
            # everyone else's.
            Key("agent", "codex", "str", choices=("claude", "codex"),
                comment="claude | codex   (falls back to "
                        "~/.config/omarchy/defaults/agent)"),
            # Still "", and still meaning "the agent's own default" — which for
            # codex is `gpt-5.6-luna` (config.CODEX_ASK_MODEL) and for claude is
            # whatever claude picks. A model slug is not portable between
            # agents, so pinning one here would be wrong the moment `agent`
            # changed; the default belongs to the adapter, and this key is the
            # override for someone who wants a different one.
            Key("model", "", "str",
                comment='"" = agent default (codex: gpt-5.6-luna)'),
        ),
    ),
    Section(
        "voice",
        align=12,
        keys=(
            Key("enabled", True, "bool"),
            Key("provider", "openrouter", "str",
                choices=("openrouter", "piper"),
                comment="openrouter | piper"),
            Key("model", "deepgram/flux-tts:free", "str"),
            Key("voice", "flux-alexis-en", "str", comment="DEFAULT (female)"),
            Key("voice_male", "flux-donovan-en", "str",
                comment="the alternate, selectable in the GUI"),
            Key("fallback", "piper", "str", choices=("piper", "none"),
                comment="piper | none — used when the network/provider fails"),
            Key("piper_voice", "en_GB-jenny_dioco-medium", "str"),
            Key("speed", 1.0, "float", minimum=0.25, maximum=4.0),
            Key("max_spoken_chars", 400, "int", minimum=40, maximum=20_000,
                comment="longer replies are summarised for speech, full text "
                        "on screen"),
        ),
    ),
    Section(
        "listen",
        align=12,
        keys=(
            Key("enabled", True, "bool"),
            Key("provider", "openrouter", "str",
                choices=("openrouter", "local"),
                comment="openrouter | local"),
            Key("model", "fish-audio/transcribe-1", "str"),
            Key("language", "en", "str"),
            Key("keybind", "F10", "str"),
        ),
    ),
    Section(
        "confirm",
        align=18,
        header=(
            "The safety model. NOT hard blocks — Jarvis asks first, then "
            "proceeds.",
            'Each key: "never" (just do it) | "ask" (confirm first) | '
            '"deny" (refuse outright)',
        ),
        footer=(
            'These are ALWAYS "deny" and are not user-editable — they exist '
            "to protect",
            "other running sessions and the machine's own record of itself:",
            "  signalling a process Jarvis did not spawn",
            "  restarting omarchy-shell",
            "  deleting ~/.config/omarchy/CUSTOMISATIONS.md",
            "  rm -rf outside Jarvis's own directories",
        ),
        keys=(
            Key("install_packages", "ask", "policy"),
            Key("delete_files", "ask", "policy"),
            Key("write_outside_home", "ask", "policy"),
            Key("system_config", "ask", "policy",
                comment="/etc, systemd units, hyprland config"),
            Key("network_send", "ask", "policy",
                comment="posting data off the machine"),
            Key("git_push", "ask", "policy"),
            Key("long_job", "ask", "policy",
                comment="anything estimated over `long_job_seconds`"),
            Key("long_job_seconds", 300, "int", minimum=1, maximum=86_400),
            Key("spend", "ask", "policy",
                comment="anything with a metered cost over `spend_threshold`"),
            Key("spend_threshold", 0.25, "float", minimum=0.0, maximum=10_000.0,
                comment="dollars"),
        ),
    ),
    Section(
        "confirm.prompt",
        keys=(
            Key("timeout_seconds", 60, "int", minimum=1, maximum=3600,
                comment="no answer within this = treated as \"no\""),
            Key("default_on_timeout", "no", "str", choices=("no", "yes")),
            Key("channel", "notification", "str",
                choices=("notification", "terminal", "both"),
                comment="notification | terminal | both"),
        ),
    ),
    Section(
        "memory",
        keys=(
            Key("luna_cap_chars", 3000, "int", minimum=200, maximum=100_000),
            Key("user_cap_chars", 2000, "int", minimum=200, maximum=100_000),
            Key("consolidate_every_turns", 12, "int", minimum=0, maximum=1000,
                comment="0 = never; the pass costs tokens"),
            Key("decay_half_life_days", 30, "int", minimum=1, maximum=3650),
        ),
    ),
    Section(
        "dispatch",
        align=18,
        keys=(
            Key("workspace", "luna", "str",
                comment="hyprland special workspace name"),
            Key("app_id", "org.omarchy.luna", "str"),
            Key("max_parallel", 1, "int", minimum=1, maximum=16,
                comment="jobs running at once; the rest queue"),
            # Minimum 0, and 0 means *never collect*. The obvious minimum of 1
            # would make the smallest value a user can type the most
            # destructive one — "keep jobs for the shortest time I can" would
            # delete yesterday's work — and there would then be no way to say
            # "keep everything" at all. `settings.set(..., -1)` is rejected as
            # out of range; 0 is the off switch.
            Key("job_retention_days", 14, "int", minimum=0, maximum=3650,
                comment="finished job directories older than this are "
                        "collected; 0 = never"),
        ),
    ),
    Section(
        "audit",
        align=6,
        header=(
            "The append-only record. Rotation moves bytes, it never drops "
            "them:",
            "audit.jsonl -> audit.jsonl.1 -> ... -> audit.jsonl.N, oldest "
            "deleted last.",
        ),
        keys=(
            # 0 = never rotate, for the same reason job_retention_days has an
            # off switch: an audit log is evidence, and somebody keeping a
            # machine under scrutiny must be able to say "grow without bound"
            # in the file rather than by patching the daemon.
            Key("max_mb", 8, "int", minimum=0, maximum=1024,
                comment="rotate once the live log passes this; 0 = never"),
            Key("keep", 5, "int", minimum=1, maximum=100,
                comment="numbered siblings kept; the oldest is deleted"),
        ),
    ),
    Section(
        "ui",
        keys=(
            Key("theme_follows_omarchy", True, "bool"),
            Key("notify_on_finish", True, "bool"),
        ),
    ),
)

_SECTIONS = {s.name: s for s in SCHEMA}


def defaults() -> dict[str, dict[str, Any]]:
    """A fresh nested dict of every default. Never shared between callers."""
    return {s.name: {k.name: k.default for k in s.keys} for s in SCHEMA}


def find(dotted: str) -> tuple[Section, Key]:
    """Resolve ``voice.voice`` / ``confirm.prompt.channel`` to schema objects.

    Section names contain dots themselves (``confirm.prompt``), so the split
    is longest-section-first rather than on the last dot.
    """
    for section in sorted(SCHEMA, key=lambda s: -len(s.name)):
        prefix = section.name + "."
        if dotted.startswith(prefix):
            key = section.key(dotted[len(prefix):])
            if key is not None:
                return section, key
            raise SettingsError(
                f"unknown setting {dotted!r}; [{section.name}] has: "
                + ", ".join(k.name for k in section.keys))
    raise SettingsError(
        f"unknown setting {dotted!r}; known sections: "
        + ", ".join(s.name for s in SCHEMA))


# =========================================================================
# Serialising — the half tomllib does not have
# =========================================================================


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # TOML floats must keep a decimal point, or they read back as ints and
        # `speed = 1` becomes an int the next time the file is loaded.
        text = repr(value)
        return text if ("." in text or "e" in text or "E" in text) else text + ".0"
    return _quote(str(value))


def _quote(text: str) -> str:
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{out}"'


def dumps(data: dict[str, Any]) -> str:
    """Serialise settings as TOML, carrying the schema's comments with them.

    Only keys the schema knows about are written. That is deliberate: an
    unknown key in the file is either a typo or a setting from a newer build,
    and silently preserving it would let a misspelling look like it works.
    """
    lines: list[str] = [
        "# Jarvis — configuration.",
        "# Written by the settings GUI and by lunad; read by lunad, which "
        "watches this",
        "# file and hot-reloads it. See docs/CONFIG-SCHEMA.md for the "
        "contract.",
        "#",
        "# Secrets NEVER live here. The API key goes in secrets.env beside "
        "this file.",
        "",
    ]
    for section in SCHEMA:
        for comment in section.header:
            lines.append(f"# {comment}" if comment else "#")
        lines.append(f"[{section.name}]")
        values = data.get(section.name) or {}
        rendered = {k.name: _render(values.get(k.name, k.default))
                    for k in section.keys}
        # Comments line up against the widest value that has one, so a long
        # path in one row does not push every comment in the section out.
        commented = [k.name for k in section.keys if k.comment]
        width = max((len(rendered[n]) for n in commented), default=0)
        for key in section.keys:
            name = key.name.ljust(section.align) if section.align else key.name
            body = f"{name} = {rendered[key.name]}"
            if key.comment:
                body = (f"{name} = {rendered[key.name].ljust(width)}  "
                        f"# {key.comment}")
            lines.append(body)
        for comment in section.footer:
            lines.append(f"# {comment}" if comment else "#")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# =========================================================================
# Validation
# =========================================================================


def validate(raw: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Merge ``raw`` onto the defaults, dropping anything that does not fit.

    Returns the clean settings and a list of human-readable complaints. The
    complaints are logged by the caller rather than raised: a config file with
    one bad line still has every other line in it, and refusing to start over a
    typo would leave the user with no assistant and no way to fix it from the
    GUI that made the typo.
    """
    clean = defaults()
    problems: list[str] = []
    if not isinstance(raw, dict):
        return clean, ["the config file is not a table"]
    _walk("", raw, clean, problems)
    return clean, problems


def _walk(prefix: str, table: dict[str, Any], clean: dict[str, dict[str, Any]],
          problems: list[str]) -> None:
    """Validate one TOML table, recursing into sub-tables that are sections.

    ``[confirm.prompt]`` arrives from ``tomllib`` as a dict *inside*
    ``confirm``, not as a top-level key, so a flat pass over the parsed
    document would either miss it or report it as an unknown setting. Both were
    observed before this walked.
    """
    for name, value in table.items():
        dotted = f"{prefix}{name}"
        if dotted in _SECTIONS and isinstance(value, dict):
            section = _SECTIONS[dotted]
            for key_name, item in value.items():
                sub = f"{dotted}.{key_name}"
                if sub in _SECTIONS and isinstance(item, dict):
                    _walk(f"{dotted}.", {key_name: item}, clean, problems)
                    continue
                key = section.key(str(key_name))
                if key is None:
                    problems.append(f"unknown setting {sub} ignored")
                    continue
                try:
                    clean[section.name][key.name] = key.coerce(item)
                except ValueError as exc:
                    problems.append(
                        f"{sub}: {exc}; using the default "
                        f"{_render(key.default)}")
            continue
        if isinstance(value, dict):
            problems.append(f"unknown section [{dotted}] ignored")
        else:
            problems.append(f"unknown setting {dotted} ignored")


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """What changed between two settings dicts, in schema order."""
    out: list[dict[str, Any]] = []
    for section in SCHEMA:
        before = old.get(section.name) or {}
        after = new.get(section.name) or {}
        for key in section.keys:
            a, b = before.get(key.name, key.default), after.get(key.name, key.default)
            if a != b:
                out.append({"key": f"{section.name}.{key.name}",
                            "from": a, "to": b})
    return out


# =========================================================================
# The live settings object
# =========================================================================


class Settings:
    """The config file, loaded, validated and watched.

    Read with :meth:`get` on every use rather than caching values in the
    caller: a hot reload swaps the dict underneath, and a voice or model
    captured at start-up would keep the old value until a restart, which is
    exactly the bug hot reload exists to prevent.
    """

    def __init__(self, path: Path | None = None, *,
                 create: bool = True) -> None:
        self.path = Path(path) if path is not None else config.CONFIG_PATH
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = defaults()
        self._stamp: tuple[int, int] | None = None
        self.problems: list[str] = []
        self.reloads = 0
        self._listeners: list[Callable[[list[dict[str, Any]]], None]] = []
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        #: Whether the config directory was already there when this object was
        #: built. On a first run it is not, and `write` is expected to create
        #: it. Once it exists, its disappearance means something removed the
        #: tree underneath a live Settings -- see `write`.
        self._dir_existed = self.path.parent.is_dir()
        if create and not self.path.exists():
            self.write(self._data, why="no config file yet")
        self.reload(initial=True)

    # -- reading ---------------------------------------------------------

    @property
    def data(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: dict(values) for name, values in self._data.items()}

    def get(self, dotted: str, default: Any = None) -> Any:
        """One setting by dotted path. Unknown paths return ``default``."""
        try:
            section, key = find(dotted)
        except SettingsError:
            return default
        with self._lock:
            return (self._data.get(section.name) or {}).get(key.name, key.default)

    def section(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get(name) or {})

    # -- writing ---------------------------------------------------------

    def set(self, dotted: str, value: Any, *, why: str = "") -> Any:
        """Validate and persist one setting. Returns the stored value.

        Raises rather than falling back to the default: an explicit `set` with
        a bad value is a caller bug, and telling the GUI so is the whole point
        of it going through the daemon.
        """
        section, key = find(dotted)
        try:
            coerced = key.coerce(value)
        except ValueError as exc:
            raise SettingsError(f"{dotted}: {exc}") from exc
        with self._lock:
            before = self._data[section.name].get(key.name, key.default)
            self._data[section.name][key.name] = coerced
            snapshot = {n: dict(v) for n, v in self._data.items()}
        self.write(snapshot, why=why or f"set {dotted}")
        if before != coerced:
            self._announce([{"key": dotted, "from": before, "to": coerced}],
                           source="set")
        return coerced

    def write(self, data: dict[str, Any] | None = None, *,
              why: str = "") -> Path:
        """Write the file atomically, 0600, in the schema's own order."""
        payload = self.data if data is None else data
        text = dumps(payload)
        # A first run has no `~/.config/jarvis`, and creating it is this
        # method's job. Recreating one that existed when this object was built
        # is a different act entirely: it means the directory was removed
        # underneath a live Settings, and the only thing that does that is a
        # test tearing its tree down while a thread it did not join is still
        # writing. Rebuilding it there resurrects the tree, which is the stray
        # `/tmp/luna-test-*` CI fails on -- twice on the 3.13 runner and
        # nowhere else, since it turns on thread scheduling.
        #
        # Refusing is right in both worlds. In production the directory does
        # not vanish, so this cannot fire; if it ever did, silently rebuilding
        # a config directory someone had just deleted would be the wrong
        # answer anyway.
        if self._dir_existed and not self.path.parent.is_dir():
            raise SettingsError(
                f"{self.path.parent} has gone since this config was opened; "
                f"refusing to recreate it")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(config.CONFIG_DIR_MODE)
        except OSError:
            pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        # The mode is set on the temp file *before* any content reaches it, so
        # there is no instant in which the file exists and is readable.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     config.CONFIG_FILE_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)
        os.chmod(self.path, config.CONFIG_FILE_MODE)
        self._stamp = _stamp_of(self.path)
        log.info("wrote the config file",
                 extra={"path": str(self.path), "why": why,
                        "bytes": len(text)})
        return self.path

    # -- loading and hot reload -------------------------------------------

    def reload(self, *, initial: bool = False) -> list[dict[str, Any]]:
        """Re-read the file. Returns what changed."""
        raw: Any = {}
        try:
            with open(self.path, "rb") as fh:
                raw = tomllib.load(fh)
        except FileNotFoundError:
            log.warning("config file is gone; using defaults",
                        extra={"path": str(self.path)})
        except tomllib.TOMLDecodeError as exc:
            # A half-written file from a GUI mid-save reads as a parse error.
            # Keeping the settings already in memory is strictly better than
            # snapping every value back to its default for one bad save.
            log.warning("config file is not valid TOML; keeping the settings "
                        "already loaded",
                        extra={"path": str(self.path), "detail": str(exc)})
            self._stamp = _stamp_of(self.path)
            return []
        except OSError as exc:
            log.warning("could not read the config file",
                        extra={"path": str(self.path), "detail": str(exc)})
            return []

        clean, problems = validate(raw)
        with self._lock:
            before = {n: dict(v) for n, v in self._data.items()}
            self._data = clean
            self.problems = problems
            self._stamp = _stamp_of(self.path)
            if not initial:
                self.reloads += 1
        for problem in problems:
            log.warning("config: %s", problem, extra={"path": str(self.path)})
        changes = diff(before, clean)
        if changes and not initial:
            self._announce(changes, source="file")
        return changes

    def _announce(self, changes: list[dict[str, Any]], source: str) -> None:
        log.info("settings reloaded",
                 extra={"source": source, "changed": len(changes),
                        "diff": [f"{c['key']}: {c['from']!r} -> {c['to']!r}"
                                 for c in changes]})
        for listener in list(self._listeners):
            try:
                listener(changes)
            except Exception:  # noqa: BLE001 - a listener must not break reload
                log.exception("a settings listener failed")

    def on_change(self, listener: Callable[[list[dict[str, Any]]], None]) -> None:
        self._listeners.append(listener)

    def changed_on_disk(self) -> bool:
        return _stamp_of(self.path) != self._stamp

    def poll(self) -> list[dict[str, Any]]:
        """One watch tick. Reloads only if mtime or size moved."""
        if not self.changed_on_disk():
            return []
        return self.reload()

    def start_watching(self, interval: float | None = None) -> None:
        """Stat-poll in a daemon thread. Idempotent.

        Polling rather than inotify: inotify needs a third-party wrapper or a
        raw syscall wrapper of our own, and the thing being watched is one file
        that changes when a human clicks something. Two seconds of latency on
        that is invisible, and the cost is one ``stat`` per tick.
        """
        if self._watcher is not None and self._watcher.is_alive():
            return
        period = config.CONFIG_POLL_S if interval is None else interval
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(period):
                try:
                    self.poll()
                except Exception:  # noqa: BLE001 - the watcher must not die
                    log.exception("settings watch tick failed")

        self._watcher = threading.Thread(target=loop, daemon=True,
                                         name="jarvis-settings-watch")
        self._watcher.start()

    def stop_watching(self) -> None:
        self._stop.set()

    # -- reporting -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {"path": str(self.path), "exists": self.path.exists(),
                "mode": _mode_of(self.path), "reloads": self.reloads,
                "problems": list(self.problems),
                "watching": bool(self._watcher and self._watcher.is_alive())}


def _stamp_of(path: Path) -> tuple[int, int] | None:
    """mtime-ns and size. Both, because a same-second rewrite of the same
    length is exactly what a GUI toggling one boolean produces."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _mode_of(path: Path) -> str:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return "-"


# =========================================================================
# Secrets
# =========================================================================


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` env file. Never logs a value."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name.strip()] = value
    return out


def api_key() -> str:
    """The OpenRouter key, or "" if there is none.

    Environment first, because that is how systemd supplies it. The files are
    read as a fallback so that a hand-started ``python -m lunad`` in a terminal
    behaves the same as the unit — and so that the key the user already has
    under ``~/.config/voxtype`` keeps working without being copied or moved.
    voxtype's file is only ever read.
    """
    for name in config.OPENROUTER_KEY_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for path in (config.SECRETS_PATH, config.VOXTYPE_SECRETS_PATH):
        found = read_env_file(path)
        for name in config.OPENROUTER_KEY_ENVS:
            value = (found.get(name) or "").strip()
            if value:
                return value
    return ""


def secrets_status() -> dict[str, Any]:
    """Whether a key exists and where it came from. Never the key itself."""
    for name in config.OPENROUTER_KEY_ENVS:
        if os.environ.get(name, "").strip():
            return {"present": True, "source": f"${name}"}
    for path in (config.SECRETS_PATH, config.VOXTYPE_SECRETS_PATH):
        found = read_env_file(path)
        for name in config.OPENROUTER_KEY_ENVS:
            if (found.get(name) or "").strip():
                return {"present": True, "source": str(path),
                        "mode": _mode_of(path)}
    return {"present": False,
            "source": "",
            "hint": f"put OPENROUTER_API_KEY=... in {config.SECRETS_PATH} "
                    "(chmod 600) and restart lunad"}


def ensure_secrets_file() -> Path:
    """Create ``secrets.env`` 0600 if it is missing, seeded from voxtype's.

    Seeded, not moved: voxtype's file belongs to voxtype and is left exactly as
    it was. If there is nothing to seed from, a commented placeholder is
    written so the user has somewhere obvious to put the key.
    """
    path = config.SECRETS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(config.CONFIG_DIR_MODE)
    except OSError:
        pass
    if path.exists():
        try:
            os.chmod(path, config.CONFIG_FILE_MODE)
        except OSError:
            pass
        return path
    existing = read_env_file(config.VOXTYPE_SECRETS_PATH)
    key = ""
    for name in config.OPENROUTER_KEY_ENVS:
        key = (existing.get(name) or "").strip()
        if key:
            break
    body = ("# Jarvis secrets. 0600, never committed, never in config.toml.\n"
            "# systemd reads this via the lunad.service drop-in.\n")
    body += (f"OPENROUTER_API_KEY={key}\n" if key
             else "# OPENROUTER_API_KEY=sk-or-v1-...\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 config.CONFIG_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, config.CONFIG_FILE_MODE)
    log.info("created the secrets file",
             extra={"path": str(path), "seeded": bool(key)})
    return path


# =========================================================================
# Process-wide instance
# =========================================================================

_lock = threading.Lock()
_SETTINGS: Settings | None = None


def settings() -> Settings:
    global _SETTINGS
    with _lock:
        if _SETTINGS is None:
            _SETTINGS = Settings()
        return _SETTINGS


def use_settings(new: Settings | None) -> Settings | None:
    """Swap the process-wide settings. Tests use this; nothing else should."""
    global _SETTINGS
    with _lock:
        old, _SETTINGS = _SETTINGS, new
    return old


def get(dotted: str, default: Any = None) -> Any:
    return settings().get(dotted, default)


def assistant_name() -> str:
    """Her name, from settings, never hard-coded.

    Falls back to the schema default rather than to the empty string: a
    greeting from an assistant with no name is worse than a greeting from one
    with the wrong name.
    """
    name = str(get("assistant.name") or "").strip()
    return name or str(_SECTIONS["assistant"].key("name").default)  # type: ignore[union-attr]


def specialist_name() -> str:
    name = str(get("assistant.specialist") or "").strip()
    return name or str(_SECTIONS["assistant"].key("specialist").default)  # type: ignore[union-attr]
