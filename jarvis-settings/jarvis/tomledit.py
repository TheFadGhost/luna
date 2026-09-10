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
# The `key = ` prefix of a `key = value  [# comment]` line. Only this part
# is a fixed shape; everything after it needs quote- and bracket-aware
# scanning (see _scan_value) because the value can itself contain `#`, or
# can be the first line of a value that spans several lines.
_KEY_RE = re.compile(r"^(?P<pre>\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*)")


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


def _find_triple_close(text: str, delim: str, start: int) -> int:
    """Index in `text` of the closing `delim` ('\"\"\"' or "'''"), from
    `start`, or -1. Backslash-escaping only applies inside \"\"\" — a
    literal '''...''' string has no escapes at all."""
    i, n = start, len(text)
    while i < n:
        if delim == '"""' and text[i] == "\\":
            i += 2
            continue
        if text[i:i + 3] == delim:
            return i
        i += 1
    return -1


def _find_single_close(text: str, quote: str, start: int) -> int:
    """Index of the closing quote for a one-line "..." or '...' string,
    from `start`, or -1 if it never closes on this line."""
    i, n = start, len(text)
    while i < n:
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return -1


def _scan_value(text: str, state=None) -> tuple[int, object]:
    """Scan one physical line of a TOML value, continuing from `state`.

    `state` is `None` to start a fresh value, or whatever a previous call
    to this function returned as `end_state` for the line before this one.

    Returns `(comment_index, end_state)`:

      comment_index -- where an unquoted, unbracketed `#` starts on this
                        line, or -1 if there is none (either because the
                        line has no comment, or because the value is not
                        finished by the end of the line, in which case
                        nothing after the opening token can be a comment)
      end_state      -- `None` if the value is complete by the end of this
                        line, else what continues onto the next line:
                        '\"\"\"' or "'''" mid-triple-quoted-string, or a
                        positive int (unmatched `[` depth) mid-array —
                        the only two TOML constructs that span lines.

    A `#`, `[`, `]`, `"`, or `'` inside a one-line "..." or '...' string is
    just a character of that string and never changes what this returns —
    which is exactly the bug this function exists to fix: splitting a line
    into value/comment on the first literal `#` treats `model = "gpt#4"`
    as truncated at the `#`, and treats a decoy `key = value`-shaped line
    inside someone else's multi-line string as a real assignment.
    """
    i, n = 0, len(text)
    depth = state if isinstance(state, int) else 0
    if isinstance(state, str):
        close = _find_triple_close(text, state, 0)
        if close < 0:
            return -1, state
        i = close + 3
    while i < n:
        head = text[i:i + 3]
        if head in ('"""', "'''"):
            close = _find_triple_close(text, head, i + 3)
            if close < 0:
                return -1, head
            i = close + 3
            continue
        c = text[i]
        if c in ('"', "'"):
            close = _find_single_close(text, c, i + 1)
            i = close + 1 if close >= 0 else n
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth = max(0, depth - 1)
        elif c == "#" and depth == 0:
            return i, None
        i += 1
    return -1, (depth or None)


def _match_pair(line: str) -> dict | None:
    """`{pre, key, val, gap, comment, end_state}` for one `key = ...`
    line, or `None` if `line` is not one at all. Comment-splitting and
    continuation detection both go through `_scan_value`."""
    km = _KEY_RE.match(line)
    if km is None:
        return None
    pre, key = km.group("pre"), km.group("key")
    body = line[km.end():]
    if body.endswith("\n"):
        body = body[:-1]
    ci, end_state = _scan_value(body, None)
    if ci < 0:
        val_part, comment = body, ""
    else:
        val_part, comment = body[:ci], body[ci:]
    stripped = val_part.rstrip()
    gap = val_part[len(stripped):]
    return {"pre": pre, "key": key, "val": stripped, "gap": gap,
            "comment": comment, "end_state": end_state}


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
    # None, or what a value opened on an earlier line still needs to close
    # ('\"\"\"', "'''", or an unmatched-`[` depth) — see _scan_value. While
    # this is set, every line belongs to that value, whatever it looks
    # like: a line inside someone else's multi-line string that happens to
    # read as `key = value` is not a fresh assignment.
    continuation = None

    for i, line in enumerate(lines):
        if continuation is not None:
            body = line[:-1] if line.endswith("\n") else line
            if line.strip():
                tail_of[current] = i + 1
            _ci, continuation = _scan_value(body, continuation)
            continue
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
        pm = _match_pair(line)
        if pm is None:
            continue
        if pm["end_state"] is not None:
            # This value continues past this line, whether or not it is
            # one we are writing — later lines must not be scanned as
            # fresh table headers or assignments until it closes.
            continuation = pm["end_state"]
        dotted = f"{current}.{pm['key']}" if current else pm["key"]
        if dotted not in pending:
            continue
        if not _scalar(pm["val"]):
            # A multi-line or unrecognised value. Leave it alone rather than
            # guess; the key stays at its old value and the caller is told.
            raise TomlEditError(
                f"{dotted} has a value this editor will not rewrite in place")
        new = format_value(pending.pop(dotted))
        comment = pm["comment"]
        gap = pm["gap"]
        if comment:
            # Keep the comment column if the new value still fits under it.
            column = len(pm["pre"]) + len(pm["val"]) + len(gap)
            pad = column - (len(pm["pre"]) + len(new))
            gap = " " * pad if pad >= 1 else " "
        eol = "\n" if line.endswith("\n") else ""
        lines[i] = f"{pm['pre']}{new}{gap}{comment}{eol}"

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
