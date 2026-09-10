"""How `luna` looks in a terminal — and how it looks in a pipe.

The desktop this belongs to is monochrome on purpose, and the settings GUI
beside it is typeset rather than boxed: hierarchy comes from weight, from
whitespace and from one hairline rule, never from chrome. The command line is
the other half of the same program and reads as the same system, so the rules
here are short and they are rules:

**Alignment is the design.** Every block is a label column and a value column,
the label column sized to its widest label rather than to a number somebody
typed. Two lines that mean the same kind of thing start in the same place. That
is the whole of the layout: there are no boxes, no borders and no rules of
`+---+`, because a box is chrome and the alignment already did the work.

**Weight, not colour, carries hierarchy.** Bold is the one identifying token on
a line — a name, an id, a filename. Dim is provenance: units, paths,
timestamps, the thing you read second. Everything else is plain.

**Colour is reserved for state that matters**, which on this machine means
*bad*: an agent that is not there, a job that failed, an audit entry that
records a refusal, a missing API key. Nothing is coloured for decoration, and
nothing is coloured merely because it is a heading. There is exactly one colour
and it is red.

**A pipe gets bytes, not escapes.** `Style.detect` says no to colour when
stdout is not a tty, when `NO_COLOR` is set, and when `TERM` is `dumb`. The
same object is what the JSON path uses, so `luna --json` cannot leak an escape
even by accident: with colour off every helper here is the identity function.

**Width is read, never assumed.** `Style.width` comes from the real terminal
(or `COLUMNS`), and every free-text field is fitted to what is left after its
label. At 80 columns and below the gaps tighten and the meters shrink; nothing
wraps into a ragged second column and nothing is silently cut without an
ellipsis to say so.

**The alphabet degrades.** The hairline glyphs are U+2500 and U+2501, which is
fine everywhere on this machine and nowhere guaranteed. `Style.detect` asks the
stream's own encoding whether it can carry them and falls back to `-` and `=`
if it cannot, so a C-locale terminal gets a plainer meter rather than a
`UnicodeEncodeError` in the middle of `luna status`.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from typing import Any, Iterable, Sequence

#: What separates parts of one value. A middle dot, because a comma reads as a
#: list of things and these are facets of one thing.
SEP = " · "

_BOLD = "\033[1m"
_DIM = "\033[2m"
_ALERT = "\033[31m"
_RESET = "\033[0m"

#: Narrower than this and the two-column layout stops paying for itself.
MIN_WIDTH = 40
#: The label column never grows past this, however long a label gets.
MAX_LABEL = 12


def supports_color(stream: Any = None, env: dict[str, str] | None = None) -> bool:
    """Three ways to say no, and only one way to say yes.

    `NO_COLOR` is honoured as the convention states it — present and non-empty
    disables colour whatever else is true — and it is checked first, because a
    user who sets it has already decided.
    """
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return False
    if (env.get("TERM") or "").lower() == "dumb":
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def terminal_width(stream: Any = None, env: dict[str, str] | None = None) -> int:
    """The real width, or 80 when there is nobody to ask.

    `COLUMNS` wins, because that is what a user resizing a pane exports and
    what a test sets; `shutil.get_terminal_size` already consults it, but only
    for the process's own stdout, and this has to answer for whichever stream
    it was handed.
    """
    env = os.environ if env is None else env
    raw = env.get("COLUMNS")
    if raw and raw.strip().isdigit():
        return max(MIN_WIDTH, int(raw.strip()))
    try:
        fd = (sys.stdout if stream is None else stream).fileno()
        return max(MIN_WIDTH, os.get_terminal_size(fd).columns)
    except (AttributeError, ValueError, OSError):
        pass
    return max(MIN_WIDTH, shutil.get_terminal_size((80, 24)).columns)


def _encodable(stream: Any, text: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


class Style:
    """Escapes, width and alphabet, decided once and passed down.

    Every renderer takes one of these rather than reading `sys.stdout` for
    itself. That is what makes the no-colour path testable: a case builds a
    `Style(color=False)` and asserts the output has no `\\033` in it, instead
    of hoping the suite is piped.
    """

    __slots__ = ("color", "width", "unicode")

    def __init__(self, color: bool = False, width: int = 80,
                 unicode: bool = True) -> None:
        self.color = bool(color)
        self.width = max(MIN_WIDTH, int(width))
        self.unicode = bool(unicode)

    @classmethod
    def detect(cls, stream: Any = None, env: dict[str, str] | None = None) -> "Style":
        stream = sys.stdout if stream is None else stream
        return cls(color=supports_color(stream, env),
                   width=terminal_width(stream, env),
                   unicode=_encodable(stream, "─━…·"))

    @property
    def narrow(self) -> bool:
        """80 columns or less: the width a person actually has."""
        return self.width <= 80

    # -- the three weights ------------------------------------------------

    def bold(self, text: Any) -> str:
        return f"{_BOLD}{text}{_RESET}" if self.color else str(text)

    def dim(self, text: Any) -> str:
        return f"{_DIM}{text}{_RESET}" if self.color else str(text)

    def alert(self, text: Any) -> str:
        return f"{_ALERT}{text}{_RESET}" if self.color else str(text)

    def plain(self, text: Any) -> str:
        return str(text)

    # -- the alphabet -----------------------------------------------------

    @property
    def sep(self) -> str:
        return SEP if self.unicode else " | "

    @property
    def ellipsis(self) -> str:
        return "…" if self.unicode else "..."

    def rule(self, width: int | None = None) -> str:
        """The single hairline. There is one per screen at most."""
        glyph = "─" if self.unicode else "-"
        return self.dim(glyph * min(self.width, width or self.width))


def visible_len(text: str) -> int:
    """Length as the terminal counts it: escapes are zero-width."""
    out, i, n = 0, 0, len(text)
    while i < n:
        if text[i] == "\033":
            end = text.find("m", i)
            if end == -1:
                return out + (n - i)
            i = end + 1
            continue
        out += 1
        i += 1
    return out


def fit(text: str, width: int, style: Style | None = None) -> str:
    """Cut to width, and say so. Never a silent truncation."""
    text = " ".join(str(text).split())
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    mark = style.ellipsis if style else "…"
    if width <= len(mark):
        return text[:width]
    return text[:width - len(mark)] + mark


def meter(pct: float, width: int = 18, style: Style | None = None) -> str:
    """Occupancy as a hairline: heavy for used, light for free.

    Drawn here and nowhere else, so a file at 48% looks the same in
    `luna status` as it does in the consolidation report. Two bars from the
    same number by two pieces of code is a difference waiting to happen.
    """
    unicode = style.unicode if style else True
    full, empty = ("━", "─") if unicode else ("=", "-")
    width = max(4, int(width))
    try:
        value = float(pct)
    except (TypeError, ValueError):
        value = 0.0
    filled = max(0, min(width, round(width * value / 100.0)))
    # A file with anything in it shows something: rounding 0.4% to an empty
    # bar reads as an empty file, which is a different fact.
    if value > 0 and filled == 0:
        filled = 1
    return full * filled + empty * (width - filled)


# =========================================================================
# Numbers a person can read
# =========================================================================


def human_bytes(n: Any) -> str:
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if value >= scale:
            return f"{value / scale:.1f} {unit}".replace(".0 ", " ")
    return f"{int(value)} B"


def human_duration(seconds: Any) -> str:
    """`48824s` is a number; `13h34m` is an answer."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if value < 0:
        return "?"
    if value < 60:
        return f"{value:.0f}s"
    minutes, secs = divmod(int(value), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def count(n: Any, singular: str, plural: str | None = None) -> str:
    """`1 fact`, `3 facts` — never `1 fact(s)`."""
    try:
        value = int(n)
    except (TypeError, ValueError):
        value = 0
    word = singular if value == 1 else (plural or singular + "s")
    return f"{value} {word}"


def money(amount: Any, places: int = 4) -> str:
    try:
        return f"${float(amount):.{places}f}"
    except (TypeError, ValueError):
        return "$?"


# =========================================================================
# The two-column block, which is the whole of the layout
# =========================================================================


class Block:
    """Labelled lines that agree on where the value column starts.

    Labels are collected first and measured together, so the column is as wide
    as it needs to be and no wider — and so adding a line called `consolidate`
    moves every other line rather than leaving one out of true.
    """

    def __init__(self, style: Style, indent: int = 2, gap: int = 2) -> None:
        self.style = style
        self.indent = indent
        self.gap = 1 if style.narrow else gap
        self._rows: list[tuple[str | None, str, bool]] = []

    def add(self, label: str, *parts: Any, alert: bool = False) -> "Block":
        """One labelled line; the parts are joined with the separator."""
        value = self.style.sep.join(str(p) for p in parts if p not in (None, ""))
        self._rows.append((label, value, alert))
        return self

    def cont(self, *parts: Any) -> "Block":
        """A continuation, aligned under the value column, not the label."""
        value = self.style.sep.join(str(p) for p in parts if p not in (None, ""))
        self._rows.append((None, value, False))
        return self

    def blank(self) -> "Block":
        self._rows.append((None, "", False))
        return self

    def raw(self, text: str) -> "Block":
        """A line that is already laid out — a sub-table, say."""
        self._rows.append((None, text, False))
        return self

    @property
    def label_width(self) -> int:
        widths = [len(lab) for lab, _, _ in self._rows if lab]
        return min(MAX_LABEL, max(widths)) if widths else 0

    def lines(self) -> list[str]:
        pad = self.label_width
        left = " " * self.indent
        out: list[str] = []
        for label, value, alert in self._rows:
            if label is None and not value:
                out.append("")
                continue
            if label is None:
                out.append(f"{left}{' ' * (pad + self.gap)}{value}")
                continue
            shown = self.style.alert(label) if alert else label
            shown += " " * max(0, pad - len(label))
            out.append(f"{left}{shown}{' ' * self.gap}{value}".rstrip())
        return out

    def value_column(self) -> int:
        return self.indent + self.label_width + self.gap

    def render(self, write: Any = None) -> str:
        text = "\n".join(self.lines())
        if write is not None:
            write(text + "\n" if text else "\n")
        return text


def columns(rows: Sequence[Sequence[str]], style: Style, gap: int = 2,
            align: str = "") -> list[str]:
    """A table with no table: columns sized to their contents, no borders.

    `align` is one character per column, `r` for right and anything else for
    left. Cells may already carry escapes, so widths are measured with
    `visible_len` rather than `len`.
    """
    if not rows:
        return []
    n = max(len(r) for r in rows)
    widths = [0] * n
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], visible_len(str(cell)))
    out = []
    for row in rows:
        parts = []
        for i, cell in enumerate(row):
            cell = str(cell)
            padding = " " * max(0, widths[i] - visible_len(cell))
            if i == len(row) - 1 and (align[i:i + 1] or "l") != "r":
                parts.append(cell)                      # no trailing padding
            elif (align[i:i + 1] or "l") == "r":
                parts.append(padding + cell)
            else:
                parts.append(cell + padding)
        out.append((" " * gap).join(parts).rstrip())
    return out


def wrapped(text: str, style: Style, indent: int, first: str = "") -> list[str]:
    """Long free text, wrapped to the terminal with a hanging indent.

    Tier-1 entries are paragraphs, not fields. Left alone they wrap wherever
    the terminal happens to run out and the second line starts at column zero,
    which loses the list. Wrapping here keeps the block rectangular.
    """
    body = " ".join(str(text).split())
    pad = " " * indent
    if not body:
        return [(first or pad).rstrip()]
    return textwrap.wrap(body, width=max(32, style.width),
                         initial_indent=first or pad, subsequent_indent=pad,
                         break_long_words=False, break_on_hyphens=False)


def join(style: Style, parts: Iterable[Any]) -> str:
    return style.sep.join(str(p) for p in parts if p not in (None, ""))
