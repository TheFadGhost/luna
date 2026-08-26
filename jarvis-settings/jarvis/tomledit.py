"""Surgical TOML value replacement.

The brief: *preserve comments and key order where possible; never destroy a
key you don't understand.* A regenerate-from-schema emitter (the ghost-settings
approach) preserves the schema's comments but throws away anything the user
typed into the file by hand. So Jarvis edits the file in place instead:

  * a key that already exists has only its **value token** rewritten — the
    indentation, the inline comment and the comment's column all survive;
  * a key that is missing is appended to the end of its own table, so key
    order is preserved and the new line lands where a reader expects it;
  * a table that is missing is appended at the end of the file;
  * every other byte of the file, including keys this app has never heard of,
    is passed through untouched.

The only whole-file emitter here is `emit_default`, used exactly once: when
there is no config.toml at all and one has to be created from nothing.
"""

from __future__ import annotations

import re
import tomllib

_TABLE_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(?:#.*)?$")
_ARRAY_TABLE_RE = re.compile(r"^\s*\[\[")
# key = value  [# comment]
_PAIR_RE = re.compile(
    r"^(?P<pre>\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*)"
    r"(?P<val>.*?)"
    r"(?P<gap>\s*)(?P<comment>#.*)?$"
)


class TomlEditError(Exception):
    pass


# ---------------------------------------------------------------- formatting

def format_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # repr keeps 1.0 as "1.0"; a bare "1" would load back as an int and
        # a float-typed key would silently change type on every save.
        return repr(float(v))
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(format_value(x) for x in v) + "]"
    raise TomlEditError(f"cannot emit {type(v).__name__} as TOML")


def _scalar(tok: str) -> bool:
    """True if `tok` is a complete single-line TOML scalar. Guards against
    rewriting the first line of a multi-line array or string, which would
    corrupt everything after it."""
    tok = tok.strip()
    if not tok:
        return False
    if tok[0] in "[{" or tok.startswith('"""') or tok.startswith("'''"):
        return False
    try:
        tomllib.loads(f"x = {tok}")
    except tomllib.TOMLDecodeError:
        return False
    return True


def _split(dotted: str) -> tuple[str, str]:
    table, _, key = dotted.rpartition(".")
    if not table or not key:
        raise TomlEditError(f"not a table.key path: {dotted!r}")
    return table, key


# ---------------------------------------------------------------- the editor

def set_values(text: str, changes: dict) -> str:
    """Return `text` with each dotted key in `changes` set to its value.

    Raises TomlEditError if the result would not parse, or would not read
    back the values asked for — Jarvis never writes a config it cannot
    prove correct.
    """
    if not changes:
        return text

    lines = text.splitlines(keepends=True)
    pending = dict(changes)
    # table name -> index just past its last content line
    tail_of: dict[str, int] = {}
    current = ""

    for i, line in enumerate(lines):
        if _ARRAY_TABLE_RE.match(line):
            current = "\x00array"           # never a target; skip its keys
            continue
        m = _TABLE_RE.match(line)
        if m:
            current = m.group(1).strip().strip('"').strip("'")
            tail_of.setdefault(current, i + 1)
            continue
        if line.strip():
            tail_of[current] = i + 1
        pm = _PAIR_RE.match(line)
        if not pm:
            continue
        dotted = f"{current}.{pm.group('key')}" if current else pm.group("key")
        if dotted not in pending:
            continue
        if not _scalar(pm.group("val")):
            # A multi-line or unrecognised value. Leave it alone rather than
            # guess; the key stays at its old value and the caller is told.
            raise TomlEditError(
                f"{dotted} has a value this editor will not rewrite in place")
        new = format_value(pending.pop(dotted))
        comment = pm.group("comment") or ""
        gap = pm.group("gap") or ""
        if comment:
            # Keep the comment column if the new value still fits under it.
            column = len(pm.group("pre")) + len(pm.group("val")) + len(gap)
            pad = column - (len(pm.group("pre")) + len(new))
            gap = " " * pad if pad >= 1 else " "
        eol = "\n" if line.endswith("\n") else ""
        lines[i] = f"{pm.group('pre')}{new}{gap}{comment}{eol}"

    # Whatever is left needs inserting. Group by table so a new table is
    # written once, and insert from the bottom up so earlier indices stay valid.
    by_table: dict[str, list[tuple[str, object]]] = {}
    for dotted, v in pending.items():
        table, key = _split(dotted)
        by_table.setdefault(table, []).append((key, v))

    inserts: list[tuple[int, list[str]]] = []
    appends: list[str] = []
    for table in sorted(by_table):
        block = [f"{k} = {format_value(v)}\n" for k, v in by_table[table]]
        if table in tail_of:
            inserts.append((tail_of[table], block))
        else:
            if appends or (lines and lines[-1].strip()):
                appends.append("\n")
            appends.append(f"[{table}]\n")
            appends.extend(block)

    for at, block in sorted(inserts, reverse=True):
        lines[at:at] = block
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.extend(appends)

    out = "".join(lines)
    _verify(out, changes)
    return out


def _verify(text: str, changes: dict) -> None:
    try:
        got = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"edit produced invalid TOML: {exc}") from exc
    for dotted, want in changes.items():
        node = got
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                raise TomlEditError(f"{dotted} missing after edit")
        have = node.get(parts[-1]) if isinstance(node, dict) else None
        if isinstance(want, float):
            ok = isinstance(have, float) and abs(have - want) < 1e-9
        else:
            ok = have == want and type(have) is type(want)
        if not ok:
            raise TomlEditError(
                f"{dotted} read back as {have!r}, expected {want!r}")


# ---------------------------------------------------------------- first run

def emit_default(spec, values: dict, header: str) -> str:
    """The fully-commented file written when none exists. The file is also
    the documentation, so first run should leave something worth hand-editing.
    """
    out = [f"# {ln}" if ln else "#" for ln in header.strip().splitlines()]
    for sec in spec:
        out.append("")
        for ln in _wrap(sec.doc, 72) if sec.doc else []:
            out.append(f"# {ln}")
        out.append(f"[{sec.key}]")
        rows = []
        for fld in sec.fields:
            v = values.get(f"{sec.key}.{fld.key}", fld.default)
            rows.append((f"{fld.key} = {format_value(v)}", _note(fld)))
        width = max((len(r[0]) for r in rows), default=0)
        for body, note in rows:
            out.append(f"{body:<{width}}  # {note}" if note else body)
    return "\n".join(out) + "\n"


def _note(fld) -> str:
    from . import schema
    bits = []
    doc = (fld.doc or "").split("\n")[0].split(". ")[0]
    if doc:
        bits.append(doc.rstrip("."))
    if isinstance(fld, (schema.Choice, schema.Tri)):
        bits.append(" | ".join(fld.options))
    elif isinstance(fld, (schema.Number, schema.Real)):
        unit = f" {fld.unit}" if fld.unit else ""
        bits.append(f"{fld.min}-{fld.max}{unit}")
    return " · ".join(bits)


def _wrap(s: str, width: int) -> list[str]:
    words, line, out = s.split(), "", []
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}" if line else w
    if line:
        out.append(line)
    return out
