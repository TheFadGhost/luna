"""Small composition helpers, so every pane is built from the same parts.

The Omarchy design-system rule (CUSTOMISATIONS.md §6d) is "build from the
shared components, never hand-roll a lookalike". Its components are QML and
cannot be instantiated in a GTK process, so this module is the GTK dialect of
the same idea: one `section_header`, one `separator`, one `row`, one button
style — defined once here, styled once in theme.py from the live palette, and
reused by every pane. No pane sets a colour or a pixel size of its own.

The app is typeset rather than boxed. There is no `card()`: a group of
settings is a heading and a rule, not a rectangle inside another rectangle.
Hierarchy is size, weight, colour value and whitespace; the only geometry
left is the window's own chrome and the controls themselves.

Every row is the same three columns — label, control, trailing note — so the
eye can run down one x for any of the three across all seven panes.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .theme import COLUMN, RHYTHM, SPACE


def label(text, css=("rowlabel",), wrap=False, xalign=0.0, selectable=False,
          measure=None):
    """One label. `measure` is a line length in characters, not pixels —
    typographic measure is counted in characters, and stating it here keeps
    every wrapping string in the app on one of the COLUMN measures."""
    lb = Gtk.Label(label=text, xalign=xalign)
    lb.set_wrap(wrap)
    if wrap:
        lb.set_wrap_mode(2)          # Pango.WrapMode.WORD_CHAR
        lb.set_max_width_chars(COLUMN["doc"] if measure is None else measure)
    lb.set_selectable(selectable)
    for c in css:
        lb.add_css_class(c)
    return lb


def section_header(text):
    """A real heading, in the UI font at a real size.

    Not letterspaced micro-caps: uppercase tracking was standing in for a
    type scale, and the app has one now.
    """
    return label(text, css=("sectionhead",))


def separator():
    sep = Gtk.Box()
    sep.add_css_class("sep")
    return sep


def column(spacing=None):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                   spacing=RHYTHM["row"] if spacing is None else spacing)


def rowbox(spacing=None):
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                   spacing=SPACE["lg"] if spacing is None else spacing)


def head(title, desc=""):
    box = column(SPACE["md"])
    box.append(label(title, css=("panehead",)))
    if desc:
        box.append(label(desc, css=("panedesc",), wrap=True,
                         measure=COLUMN["lede"]))
    return box


def clear(box):
    """Empty a container, breaking the widget/closure cycles as it goes.

    `run_dispose()` after `remove()` is the language-binding-sanctioned cycle
    breaker (CUSTOMISATIONS.md §6e): a GTK4 widget removed inside a Python
    process leaks if any closure still holds it, because Python's GC cannot
    traverse the C edges. Safe for every list this app rebuilds — none of
    them holds a shared GdkTexture, which is the one case that must not be
    disposed while another owner may still drop the last reference.
    """
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child.run_dispose()
        child = nxt


def group(title, *children, note=None, action=None):
    """A section: the rule that marks the break, a real heading, then rows.

    The heading gets more room above it than below, so the whitespace reads
    as belonging to the break rather than floating the heading off its own
    content.
    """
    box = column(0)
    box.set_margin_top(RHYTHM["section"])
    gap = 0
    if title:
        box.append(separator())
        h = section_header(title)
        h.set_margin_top(RHYTHM["ruleGap"])
        if action is None:
            box.append(h)
        else:
            line = rowbox(SPACE["lg"])
            line.set_margin_top(RHYTHM["ruleGap"])
            h.set_margin_top(0)
            h.set_hexpand(True)
            h.set_valign(Gtk.Align.CENTER)
            line.append(h)
            line.append(action)
            box.append(line)
        gap = RHYTHM["afterHeading"]
    if note:
        n = label(note, css=("rowdoc",), wrap=True, measure=COLUMN["note"])
        n.set_margin_top(gap)
        box.append(n)
        gap = RHYTHM["afterHeading"]
    for i, child in enumerate(children):
        child.set_margin_top(RHYTHM["row"] if i else gap)
        box.append(child)
    return box


def row(title, doc, *controls, trail=None):
    """One line of the settings grid.

    Three columns and no separator between rows: the label sits four pixels
    off its own helper text and fourteen off the next row, which is all the
    grouping a reader needs. The control column and the trailing column have
    fixed widths, so a control's left edge is at the same x on every row of
    every pane, and so is whatever note sits beyond it.
    """
    box = rowbox(SPACE["panelPadding"])
    left = column(SPACE["labelGap"] // 2)
    left.set_hexpand(True)
    left.append(label(title))
    if doc:
        left.append(label(doc, css=("rowdoc",), wrap=True))
    box.append(left)

    holder = rowbox(SPACE["md"])
    holder.set_valign(Gtk.Align.CENTER)
    holder.set_size_request(COLUMN["control"], -1)
    holder.set_halign(Gtk.Align.END)
    for c in controls:
        holder.append(c)
    box.append(holder)

    tail = rowbox(SPACE["md"])
    tail.set_valign(Gtk.Align.CENTER)
    tail.set_size_request(COLUMN["trail"], -1)
    tail.set_halign(Gtk.Align.END)
    if trail is not None:
        tail.append(trail)
    box.append(tail)
    box.jarvis_trail = tail
    return box


def state_note(text, risk=False):
    """What a setting is currently doing, in the trailing column.

    `risk` is not decoration: it marks the answers that change what the
    machine will do without being asked again, and it is the only thing in
    that column carrying full contrast.
    """
    css = ("statenote", "risk") if risk else ("statenote",)
    lb = label(text, css=css, wrap=True, measure=COLUMN["state"])
    lb.set_valign(Gtk.Align.CENTER)
    return lb


def act(text, primary=False):
    b = Gtk.Button(label=text)
    b.add_css_class("act")
    if primary:
        b.add_css_class("primary")
    b.set_valign(Gtk.Align.CENTER)
    return b


def banner(text, kind="ok"):
    """A strip across the window, not a box floating inside it."""
    box = rowbox(SPACE["lg"])
    box.add_css_class("banner")
    if kind:
        box.add_css_class(kind)
    lb = label(text, css=("value",), wrap=True, measure=COLUMN["note"])
    lb.set_hexpand(True)
    box.append(lb)
    return box, lb


def scroller(child):
    sc = Gtk.ScrolledWindow()
    sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sc.set_vexpand(True)
    sc.set_child(child)
    return sc


class TriToggle(Gtk.Box):
    """Allow / Ask first / Refuse — one linked group of three.

    A radio group, not a dropdown: the point of the confirmations pane is that
    all three answers are visible at once for every action class, so the shape
    of the safety model can be read off the screen in one look.

    The three are drawn as words with a rule under the one in force, not as
    three identical chips. Chips made a permissive answer and a restrictive
    one the same object with different text inside it, which on a safety
    surface is the failure mode: the reader compares boxes, not words.
    """

    def __init__(self, options, labels, value, on_change):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=SPACE["labelGap"])
        self._buttons = {}
        self._on_change = on_change
        self._watchers = []
        self._muted = False
        first = None
        for opt in options:
            b = Gtk.ToggleButton(label=labels.get(opt, opt))
            b.add_css_class("seg")
            b.add_css_class(f"seg-{opt}")
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self._toggled, opt)
            self._buttons[opt] = b
            self.append(b)
        self.set_value(value)

    def watch(self, fn):
        """Call `fn(value)` whenever the answer changes, from either side.

        The Binder keeps one refresh callback per config key, so a pane that
        also wants to know — the confirmations pane, which annotates the row
        in words — subscribes here instead of fighting for that slot.
        """
        self._watchers.append(fn)
        fn(self.value())

    def _announce(self, value):
        for fn in self._watchers:
            fn(value)

    def set_value(self, value):
        b = self._buttons.get(value)
        if b is None:
            return
        self._muted = True
        b.set_active(True)
        self._muted = False
        self._announce(value)

    def value(self):
        for opt, b in self._buttons.items():
            if b.get_active():
                return opt
        return None

    def _toggled(self, button, opt):
        if not button.get_active():
            return
        if self._muted:
            return
        self._announce(opt)
        self._on_change(opt)


def locked_entry(name, reason):
    """A hard deny. Rendered as TEXT — there is no widget here to enable.

    This is deliberate: a disabled Switch is one `set_sensitive(True)` away
    from being editable, and these four are not settings at all. They are not
    read from config.toml and there is no code path in Jarvis that writes them.

    It is built from the same `row` as every editable setting so the reader
    can run down the trailing column and see, in words, that these four say
    "not editable" where the others say what they will do.
    """
    return row(name, reason, trail=state_note("not editable"))
