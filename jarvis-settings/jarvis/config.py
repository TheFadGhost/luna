"""Load, validate and save ~/.config/jarvis/config.toml.

Rules this module exists to enforce:

  * **Never write an invalid config.** Every value is validated against
    schema.SPEC before it is formatted, the whole file is re-parsed after the
    edit, and the values are read back and compared. Any failure raises and
    nothing is written.
  * **Never destroy a key you don't understand.** Writes are surgical
    (tomledit); unknown tables and keys are never rewritten and never dropped.
  * **The file is 0600 inside a 0700 directory.** The mode is set on the temp
    file *before* os.replace, so the config is never briefly world-readable.
  * Secrets never live here. `save` refuses any key whose name looks like a
    credential, whatever the caller thinks it is doing.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from . import schema
from .tomledit import TomlEditError, emit_default, set_values

CONFIG_DIR = Path(os.path.expanduser("~/.config/jarvis"))
CONFIG_PATH = CONFIG_DIR / "config.toml"
DIR_MODE = 0o700
FILE_MODE = 0o600

# Keys refused outright. CONFIG-SCHEMA.md: "Secrets NEVER live here."
# Matched on whole underscore/hyphen-separated tokens, not as substrings —
# `keybind` contains "key" and is a perfectly ordinary setting.
SECRET_TOKENS = frozenset(
    ("key", "keys", "apikey", "token", "secret", "password", "passwd",
     "pass", "credential", "credentials", "auth"))


def looks_secret(name: str) -> bool:
    return bool(SECRET_TOKENS & set(re.split(r"[_\-]+", name.lower())))

HEADER = """
Jarvis — configuration. Written by the Jarvis settings app; hand-editing is
fine, lunad watches this file and hot-reloads. Secrets NEVER live here:
API keys belong in ~/.config/jarvis/secrets.env, chmod 600.
Unknown keys are preserved verbatim and ignored.
"""


class ValidationError(ValueError):
    """A value the GUI must not write. Carries the dotted key."""

    def __init__(self, dotted: str, message: str):
        super().__init__(f"{dotted}: {message}")
        self.dotted = dotted
        self.message = message


# ---------------------------------------------------------------- validation

def coerce(dotted: str, v):
    """Return `v` coerced to the field's type, or raise ValidationError.

    Coercion, not clamping, on the write path: a value the user typed that is
    out of range is a mistake to report, not to silently alter.
    """
    found = schema.field_for(dotted)
    if found is None:
        raise ValidationError(dotted, "not a known setting")
    _sec, fld = found
    if fld.readonly:
        raise ValidationError(dotted, "is read-only")
    if looks_secret(dotted.rpartition(".")[2]):
        raise ValidationError(dotted, "looks like a secret; refusing to store")

    if isinstance(fld, schema.Toggle):
        if not isinstance(v, bool):
            raise ValidationError(dotted, "must be true or false")
        return v
    if isinstance(fld, schema.Number):
        if isinstance(v, bool):
            raise ValidationError(dotted, "must be a whole number")
        if isinstance(v, float) and not v.is_integer():
            raise ValidationError(dotted, "must be a whole number")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValidationError(dotted, "must be a whole number") from None
        if not fld.min <= n <= fld.max:
            raise ValidationError(dotted, f"must be {fld.min}–{fld.max}")
        return n
    if isinstance(fld, schema.Real):
        if isinstance(v, bool):
            raise ValidationError(dotted, "must be a number")
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ValidationError(dotted, "must be a number") from None
        if x != x or x in (float("inf"), float("-inf")):
            raise ValidationError(dotted, "must be a finite number")
        if not fld.min <= x <= fld.max:
            raise ValidationError(dotted, f"must be {fld.min}–{fld.max}")
        return round(x, fld.digits)
    if isinstance(fld, (schema.Choice, schema.Tri)):
        if v not in fld.options:
            raise ValidationError(dotted, "must be " + " | ".join(fld.options))
        return v
    if isinstance(fld, schema.VoicePick):
        if not isinstance(v, str) or not v.strip():
            raise ValidationError(dotted, "pick a voice")
        return v.strip()
    if isinstance(fld, schema.Text):
        if not isinstance(v, str):
            raise ValidationError(dotted, "must be text")
        s = v.strip()
        if not s and not fld.allow_empty:
            raise ValidationError(dotted, "cannot be empty")
        if "\n" in s or "\r" in s:
            raise ValidationError(dotted, "must be a single line")
        return s
    raise ValidationError(dotted, "unhandled field kind")


def _accept(dotted: str, v):
    """Read path: keep a good value, fall back to the default on a bad one."""
    try:
        return coerce(dotted, v), None
    except ValidationError as exc:
        found = schema.field_for(dotted)
        default = found[1].default if found else None
        return default, f"{dotted}: {v!r} ignored ({exc.message}); using {default!r}"


# ---------------------------------------------------------------- loading

def _dig(raw: dict, table: str, key: str, default):
    node = raw
    for part in table.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    if not isinstance(node, dict):
        return default
    return node.get(key, default)


def unknown_keys(raw: dict) -> dict:
    """Every leaf in the file that SPEC has never heard of. These are shown in
    the About pane so the user can see nothing was silently dropped."""
    known = schema.known_keys()
    out: dict[str, object] = {}

    def walk(node, prefix):
        for k, v in node.items():
            dotted = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(v, dotted)
            elif dotted not in known:
                out[dotted] = v

    walk(raw, "")
    return out


def load(path: Path = CONFIG_PATH) -> tuple[dict, dict, list[str]]:
    """-> (values, unknown, warnings). Never raises.

    A parse failure falls back to config.toml.bak, then to schema defaults,
    and says so in `warnings` — a broken file must not stop the app that
    exists to fix it.
    """
    warnings: list[str] = []
    raw: dict = {}
    for candidate in (path, path.with_suffix(".toml.bak")):
        try:
            with open(candidate, "rb") as fh:
                raw = tomllib.load(fh)
            break
        except FileNotFoundError:
            raw = {}
            if candidate == path:
                break                 # no file at all is not an error
        except (OSError, tomllib.TOMLDecodeError) as exc:
            warnings.append(f"{candidate.name}: {exc}")
            raw = {}

    values: dict = {}
    sentinel = object()
    for sec in schema.SPEC:
        for fld in sec.fields:
            dotted = f"{sec.key}.{fld.key}"
            v = _dig(raw, sec.key, fld.key, sentinel)
            if v is sentinel:
                values[dotted] = fld.default
                continue
            if fld.readonly:
                values[dotted] = v if isinstance(v, str) else fld.default
                continue
            good, warn = _accept(dotted, v)
            values[dotted] = good
            if warn:
                warnings.append(warn)
    return values, unknown_keys(raw), warnings


def diff(old: dict, new: dict) -> list[str]:
    return sorted(k for k in new if new[k] != old.get(k))


# ---------------------------------------------------------------- saving

def ensure_dir(path: Path = CONFIG_PATH) -> None:
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        if (d.stat().st_mode & 0o777) != DIR_MODE:
            d.chmod(DIR_MODE)         # tighten a directory made by something else
    except OSError:
        pass


def save(changes: dict, path: Path = CONFIG_PATH, all_values: dict | None = None) -> list[str]:
    """Apply `changes` (dotted key -> raw value) to the file on disk.

    Returns the list of keys actually written. Raises ValidationError or
    TomlEditError without touching the file if anything is wrong.
    """
    clean = {k: coerce(k, v) for k, v in changes.items()}
    if not clean:
        return []
    ensure_dir(path)

    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise TomlEditError(
                f"{path.name} does not parse ({exc}); fix or move it aside "
                "before saving") from exc
        new_text = set_values(text, clean)
        backup = path.with_suffix(".toml.bak")
        try:
            shutil.copy2(path, backup)
            backup.chmod(FILE_MODE)
        except OSError:
            pass
    else:
        base = dict(schema.defaults())
        if all_values:
            base.update({k: v for k, v in all_values.items() if k in base})
        base.update(clean)
        new_text = emit_default(schema.SPEC, base, HEADER)

    _atomic_write(path, new_text)
    return sorted(clean)


def write_default(path: Path = CONFIG_PATH, values: dict | None = None) -> None:
    """Create the commented default file. No-op if one already exists."""
    if path.exists():
        return
    ensure_dir(path)
    base = dict(schema.defaults())
    if values:
        base.update({k: v for k, v in values.items() if k in base})
    _atomic_write(path, emit_default(schema.SPEC, base, HEADER))


def _atomic_write(path: Path, text: str) -> None:
    tomllib.loads(text)               # last line of defence, cheap
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-")
    try:
        os.fchmod(fd, FILE_MODE)      # before the rename, never after
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)             # one inotify event, always a whole file
