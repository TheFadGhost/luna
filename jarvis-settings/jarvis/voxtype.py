"""Write-through from `[listen]` to voxtype's own config file.

`[listen]` was the last block in the contract that was a lie. Listening is not
lunad's — it belongs to voxtype, a separate Rust daemon reading
`~/.config/voxtype/config.toml`, so for a long time these keys were a *mirror*
kept for display and editing one changed nothing. This module is what makes
three of them real: a save in Jarvis is projected onto voxtype's keys, written
into voxtype's file in place, and voxtype is restarted so it actually reads it.

Four rules, in the order they will bite:

**voxtype's file belongs to voxtype.** It is hand-written and its comments
carry things that cost someone a morning to find out — most of all the one
above `model`, which says that in `mode = "remote"` the daemon sends `model`
to the endpoint and *not* `remote_model`, despite `remote_model` being a real
field in its config struct. A writer that reformatted that file, or dropped
that comment, would be worse than no writer at all. So the edit goes through
`tomledit`, which rewrites the value token of an existing key and nothing else,
and the first write takes a `config.toml.pre-jarvis` snapshot next to the
`.pre-luna`, `.pre-openrouter` and `.pre-osd` backups already there.

**voxtype does not re-read its config.** This is a recorded gotcha
(`~/.config/omarchy/CUSTOMISATIONS.md` §8a.2): the symptom is that a change is
accepted, the file on disk is right, and the running daemon carries on with the
settings it loaded at start-up — the journal says `Profile 'luna' not found in
config, using default settings` and the transcript is typed into the focused
window. So a write that does not restart voxtype has changed *nothing* while
looking like it worked, and this module restarts it. Where it cannot,
:func:`stale` says so out loud rather than leaving the user to discover it.

**A recording in flight outranks a settings save.** Restarting voxtype
mid-recording loses whatever was being dictated, and no settings change is
worth someone's sentence. :func:`activity` reads voxtype's own state file and
:func:`apply` refuses outright while it says `recording` or `transcribing` —
refuses, rather than writing and skipping the restart, because a half-applied
change is exactly the state this module exists to avoid.

**Drift is reported, never resolved silently.** Someone editing voxtype's file
directly is not doing anything wrong, and Jarvis has no business deciding which
of the two files wins. :func:`drift` compares them and the Listening pane shows
the disagreement, key by key, with the value each side holds.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .tomledit import TomlEditError, set_values

CONFIG_PATH = Path(os.path.expanduser("~/.config/voxtype/config.toml"))

#: Taken once, before Jarvis has ever written to the file, and never
#: overwritten afterwards. The convention is voxtype's own: `.pre-luna`,
#: `.pre-openrouter` and `.pre-osd` are already sitting next to it, each one a
#: snapshot from before a change rather than from before the last save.
BACKUP_SUFFIX = ".pre-jarvis"

UNIT = "voxtype"
RESTART_COMMAND = ("systemctl", "--user", "restart", UNIT)
RESTART_TIMEOUT_S = 20.0

#: What voxtype publishes while it is busy. `state_file = "auto"` puts it in
#: the runtime directory; the same file is what `voxtype status` reads and what
#: the bar's listening indicator watches.
BUSY = ("recording", "transcribing")

#: The `[listen]` keys that reach voxtype. `enabled` is not among them — it is
#: honoured by `bin/luna-voice-router` on this side of the boundary — and
#: `keybind` is Hyprland's.
WRITE_THROUGH_KEYS = ("listen.provider", "listen.model", "listen.language")

#: The whisper models voxtype can load locally, from the comment above `model`
#: in its own config. Anything else in `mode = "local"` has to be an absolute
#: path to a `.bin`, and an OpenRouter model id is neither.
LOCAL_MODELS = ("tiny", "tiny.en", "base", "base.en", "small", "small.en",
                "medium", "medium.en", "large-v3", "large-v3-turbo")


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


STATE_PATH = _runtime_dir() / "voxtype" / "state"
PID_PATH = _runtime_dir() / "voxtype" / "pid"


class VoxtypeError(Exception):
    """A write-through that must not happen. Nothing has been changed."""


@dataclass(frozen=True)
class Applied:
    """What one write-through actually did.

    `ok` is about the whole errand, not about the file: a config written and a
    daemon left running the old settings is not a success, because the user
    asked for listening to behave differently and it does not.
    """

    ok: bool
    written: tuple[str, ...] = ()
    restarted: bool = False
    detail: str = ""


# ---------------------------------------------------------------- projection

def projection(values: dict) -> dict:
    """The voxtype keys `[listen]` implies, as dotted paths.

    Computed from the whole block rather than from the key that changed,
    because `model` cannot be projected without knowing `provider`: the same
    string means an OpenRouter model id in remote mode and a whisper model name
    in local mode, and voxtype reads it out of the same key either way.

    In remote mode `remote_model` is written alongside `model` even though
    voxtype ignores it — the gotcha in its own file says the endpoint is sent
    `model`. Leaving the two disagreeing would leave a reader of that file with
    two answers to "which model is this using", and the wrong one looks
    authoritative because of its name.
    """
    provider = values.get("listen.provider")
    model = str(values.get("listen.model") or "").strip()
    language = str(values.get("listen.language") or "").strip()
    if provider not in ("openrouter", "local"):
        raise VoxtypeError(f"listen.provider is {provider!r}, "
                           "which is neither openrouter nor local")
    if not model:
        raise VoxtypeError("listen.model is empty; voxtype needs a model name")
    if not language:
        raise VoxtypeError("listen.language is empty")

    remote = provider == "openrouter"
    if not remote and model not in LOCAL_MODELS and not model.startswith("/"):
        # The failure this refusal prevents is quiet and slow: voxtype in local
        # mode treats an unknown name as a path, fails to load it, and
        # dictation stops working with the reason only in the journal.
        raise VoxtypeError(
            f"{model!r} is not a local whisper model. With provider = local, "
            "voxtype needs one of " + ", ".join(LOCAL_MODELS)
            + ", or an absolute path to a .bin file.")

    out = {"whisper.mode": "remote" if remote else "local",
           "whisper.model": model,
           "whisper.language": language}
    if remote:
        out["whisper.remote_model"] = model
    return out


def mirrored(vox: dict) -> dict:
    """voxtype's file, read back as the `[listen]` values it stands for.

    The inverse of :func:`projection` for the three keys that round-trip.
    `remote_model` has no inverse of its own and deliberately gets none: it is
    a companion of `model`, not a second setting.
    """
    mode = vox.get("whisper.mode", "local")
    return {"listen.provider": "openrouter" if mode == "remote" else "local",
            "listen.model": vox.get("whisper.model"),
            "listen.language": vox.get("whisper.language")}


# ---------------------------------------------------------------- the file

def read(path: Path | None = None) -> tuple[str, dict]:
    """(text, flattened values). Raises VoxtypeError; never a partial read."""
    target = CONFIG_PATH if path is None else path
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise VoxtypeError(f"{target} does not exist — voxtype is not "
                           "configured on this machine") from None
    except OSError as exc:
        raise VoxtypeError(f"{target}: {exc}") from exc
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise VoxtypeError(
            f"{target.name} does not parse ({exc}); fix it before Jarvis "
            "writes to it") from exc
    return text, _flatten(parsed)


def _flatten(node: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in node.items():
        dotted = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, dotted))
        else:
            out[dotted] = v
    return out


def drift(values: dict, path: Path | None = None) -> list[dict]:
    """Where Jarvis's mirror and voxtype's file disagree.

    Returns one row per key, `{"key", "jarvis", "voxtype"}`, in schema order.
    A missing key on voxtype's side is reported with `None` rather than filled
    in from a default: "voxtype's file does not say" and "voxtype's file says
    something else" are different problems and the pane names both.
    """
    _text, vox = read(path)
    theirs = mirrored(vox)
    out: list[dict] = []
    for key in ("listen.provider", "listen.model", "listen.language"):
        ours = values.get(key)
        them = theirs.get(key)
        if ours != them:
            out.append({"key": key, "jarvis": ours, "voxtype": them})
    return out


# ---------------------------------------------------------------- the daemon

def activity() -> str:
    """`idle`, `recording`, `transcribing`, or `unknown`.

    voxtype writes this file on every transition. `unknown` covers both "the
    daemon is not running" and "`state_file` is disabled", which are not worth
    telling apart here: in either case Jarvis cannot prove nothing is being
    recorded, and that is the only question being asked.
    """
    try:
        return STATE_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def running_pid() -> int | None:
    """The live voxtype daemon's pid, or None.

    Read from voxtype's own pid file and then *checked against `/proc`*, for
    the reason the hard denies give: Linux recycles pids, and a stale pid file
    left by a daemon that died points at whatever process inherited the number.
    """
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pid if comm == UNIT else None


def stale(path: Path | None = None) -> bool | None:
    """Has voxtype's config changed since the daemon last read it?

    True means the file on disk and the running daemon disagree, which is the
    §8a.2 gotcha exactly: the setting is saved, it is correct, and it is not in
    force. None means unknowable — no daemon, or no `/proc` entry to date it
    from — and the pane says "cannot tell" rather than guessing either way.

    The daemon's start time comes from the mtime of its `/proc` directory,
    which the kernel sets when the process is created. `systemctl show` would
    answer the same question with a locale-formatted timestamp to parse.
    """
    pid = running_pid()
    if pid is None:
        return None
    target = CONFIG_PATH if path is None else path
    try:
        started = Path(f"/proc/{pid}").stat().st_mtime
        changed = target.stat().st_mtime
    except OSError:
        return None
    return changed > started


def restart() -> tuple[bool, str]:
    """`systemctl --user restart voxtype`.

    The second process Jarvis is allowed to touch, and the only one it is
    allowed to *stop*. It is here because the alternative is worse: without it
    every `[listen]` write is a change the user made, saw accepted, and does
    not have. Callers must have checked :func:`activity` first — this function
    does not, so that the Listening pane's explicit "restart it now" button and
    the automatic path share one implementation.
    """
    try:
        r = subprocess.run(list(RESTART_COMMAND), capture_output=True,
                           text=True, timeout=RESTART_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if r.returncode == 0:
        return True, " ".join(RESTART_COMMAND)
    return False, (r.stderr or r.stdout or f"exit {r.returncode}").strip()


# ---------------------------------------------------------------- the write

def apply(values: dict, path: Path | None = None) -> Applied:
    """Project `[listen]` onto voxtype's file, write it, and restart voxtype.

    Nothing is written when nothing differs, so saving the same value twice
    does not cost a restart — and does not interrupt a dictation that started
    between the two saves.

    Raises ``VoxtypeError`` for the two conditions that make the errand
    impossible before it begins: a `[listen]` block that cannot be projected,
    and a voxtype config that is missing or does not parse. Everything that
    gets as far as being *attempted* comes back as an :class:`Applied`, because
    from there on the interesting answer is not "did it throw" but "how far did
    it get".
    """
    target = CONFIG_PATH if path is None else path
    want = projection(values)                 # raises before anything is read
    text, have = read(target)
    changes = {k: v for k, v in want.items() if have.get(k) != v}
    if not changes:
        return Applied(ok=True, detail=f"{target.name} already matches")

    state = activity()
    if state in BUSY:
        # Deliberately before the write, not between the write and the
        # restart: a written file that voxtype has not read is the state this
        # whole module exists to prevent, and leaving one behind to "finish
        # later" would be that state with a promise attached.
        return Applied(
            ok=False,
            detail=f"voxtype is {state} — nothing was written. Save again "
                   "when the recording has finished.")

    try:
        new_text = set_values(text, changes)
    except TomlEditError as exc:
        return Applied(ok=False, detail=f"{target.name}: {exc}")

    _backup(target)
    try:
        _write(target, new_text)
    except OSError as exc:
        return Applied(ok=False, detail=f"could not write {target}: {exc}")

    written = tuple(sorted(changes))
    names = ", ".join(k.rpartition(".")[2] for k in written)
    if running_pid() is None:
        # Nothing to restart. Not a failure: voxtype reads the file at
        # start-up, so the change is in force the moment it next starts.
        return Applied(ok=True, written=written,
                       detail=f"wrote {names} to {target.name}; voxtype is "
                              "not running, so it will read them when it "
                              "starts")
    ok, detail = restart()
    if not ok:
        return Applied(
            ok=False, written=written,
            detail=f"wrote {names} to {target.name} but could not restart "
                   f"voxtype ({detail}) — it is still listening with the old "
                   "settings")
    return Applied(ok=True, written=written, restarted=True,
                   detail=f"wrote {names} to {target.name} and restarted "
                          "voxtype")


def _backup(path: Path) -> None:
    """Snapshot the file the first time Jarvis writes to it, and only then.

    A backup taken on every save would, after two saves, be a copy of the file
    Jarvis wrote — which is no use at all when the question is what the file
    looked like before Jarvis existed.
    """
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():
        return
    try:
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, path.stat().st_mode & 0o777)
    except OSError:
        # Best effort. A machine that cannot take the backup can still take
        # the setting, and refusing the write here would be the tail wagging
        # the dog — but the backup is tried first so that the one write that
        # matters is the one that has it.
        pass


def _write(path: Path, text: str) -> None:
    """Atomically, keeping the mode voxtype's file already had.

    Not 0600: this is another application's file, it holds no secret (the
    OpenRouter key lives in `secrets.env` next to it, and the comment above
    `mode` says so), and quietly tightening another program's permissions is
    not Jarvis's call to make.
    """
    tomllib.loads(text)                       # last line of defence, cheap
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".voxtype-")
    try:
        os.fchmod(fd, mode)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
