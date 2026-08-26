"""Small composition helpers, so every pane is built from the same parts.

The Omarchy design-system rule (CUSTOMISATIONS.md §6d) is "build from the
shared components, never hand-roll a lookalike". Its components are QML and
cannot be instantiated in a GTK process, so this module is the GTK dialect of
the same idea: one `section_header`, one `separator`, one `card`, one `row`,
one button style — defined once here, styled once in theme.py from the live
palette, and reused by every pane. No pane sets a colour or a pixel size of
its own.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .theme import SPACE


def label(text, css=("rowlabel",), wrap=False, xalign=0.0, selectable=False):
    lb = Gtk.Label(label=text, xalign=xalign)
    lb.set_wrap(wrap)
    if wrap:
        lb.set_wrap_mode(2)          # Pango.WrapMode.WORD_CHAR
        lb.set_max_width_chars(52)
    lb.set_selectable(selectable)
    for c in css:
        lb.add_css_class(c)
    return lb


def section_header(text):
    """PanelSectionHeader analogue: uppercase, letterspaced caption."""
    return label(text.upper(), css=("sectionhead",))


def separator():
    sep = Gtk.Box()
    sep.add_css_class("sep")
    return sep


def card(spacing=None, flat=False):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                  spacing=SPACE["lg"] if spacing is None else spacing)
    box.add_css_class("card")
    if flat:
        box.add_css_class("flat")
    return box


def column(spacing=None):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                   spacing=SPACE["panelGap"] if spacing is None else spacing)


def rowbox(spacing=None):
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                   spacing=SPACE["lg"] if spacing is None else spacing)


def row(title, doc, *controls, control_width=220):
    """Label + optional sub-label on the left, control(s) on the right."""
    box = rowbox()
    left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                   spacing=SPACE["labelGap"] // 2)
    left.set_hexpand(True)
    left.append(label(title))
    if doc:
        left.append(label(doc, css=("rowdoc",), wrap=True))
    box.append(left)
    holder = rowbox(SPACE["md"])
    holder.set_valign(Gtk.Align.CENTER)
    if control_width:
        holder.set_size_request(control_width, -1)
    holder.set_halign(Gtk.Align.END)
    for c in controls:
        holder.append(c)
    box.append(holder)
    return box


def act(text, primary=False):
    b = Gtk.Button(label=text)
    b.add_css_class("act")
    if primary:
        b.add_css_class("primary")
    b.set_valign(Gtk.Align.CENTER)
    return b


def banner(text, kind="ok"):
    box = rowbox(SPACE["lg"])
    box.add_css_class("banner")
    if kind:
        box.add_css_class(kind)
    lb = label(text, css=("value",), wrap=True)
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
    """Never ask / Ask first / Never allow — one linked group of three.

    A radio group, not a dropdown: the point of the confirmations pane is that
    all three answers are visible at once for every action class, so the shape
    of the safety model can be read off the screen in one look.
    """

    def __init__(self, options, labels, value, on_change):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=SPACE["labelGap"])
        self._buttons = {}
        self._on_change = on_change
        self._muted = False
        first = None
        for opt in options:
            b = Gtk.ToggleButton(label=labels.get(opt, opt))
            b.add_css_class("seg")
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self._toggled, opt)
            self._buttons[opt] = b
            self.append(b)
        self.set_value(value)

    def set_value(self, value):
        b = self._buttons.get(value)
        if b is None:
            return
        self._muted = True
        b.set_active(True)
        self._muted = False

    def value(self):
        for opt, b in self._buttons.items():
            if b.get_active():
                return opt
        return None

    def _toggled(self, button, opt):
        if self._muted or not button.get_active():
            return
        self._on_change(opt)


def locked_entry(name, reason):
    """A hard deny. Rendered as TEXT — there is no widget here to enable.

    This is deliberate: a disabled Switch is one `set_sensitive(True)` away
    from being editable, and these four are not settings at all. They are not
    read from config.toml and there is no code path in Jarvis that writes them.
    """
    box = rowbox(SPACE["lg"])
    left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    left.set_hexpand(True)
    head = rowbox(SPACE["md"])
    head.append(label("■", css=("lockmark",)))
    head.append(label(name, css=("lockedname",)))
    left.append(head)
    why = label(reason, css=("lockedwhy",), wrap=True)
    why.set_max_width_chars(84)
    left.append(why)
    box.append(left)
    tag = label("ALWAYS DENY", css=("locked-value",))
    tag.set_valign(Gtk.Align.CENTER)
    box.append(tag)
    return box
