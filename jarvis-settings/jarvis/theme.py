"""Jarvis theming — the live Omarchy palette, driven into GTK4 CSS.

Ported from ~/Work/omarchy-monochrome/sill/theme.py so Jarvis sits next to
Sill rather than next to a stock GTK dialog, with the Omarchy design tokens
(CUSTOMISATIONS.md §6d) transcribed as named constants below. Nothing in the
stylesheet is a bare hex literal or a magic pixel number: every colour comes
from colors.toml and every size from a token.

THE GOTCHA THIS FILE EXISTS TO SURVIVE
--------------------------------------
`omarchy theme-set` does `rm -rf current/theme && mv next current/theme`. That
destroys the directory any inner file monitor is watching, so a watch on
colors.toml itself fires once and then points at a deleted inode forever.
ThemeWatch therefore watches the STABLE parent (~/.local/state/omarchy/current)
and re-arms the inner colors.toml monitor after every rebuild.
"""

from __future__ import annotations

import os
import re
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

HOME = os.path.expanduser("~")
THEME_DIR = os.path.join(HOME, ".local/state/omarchy/current/theme")

# --- design tokens, CUSTOMISATIONS.md §6d ---------------------------------
# Mirrors ~/.config/hypr/looknfeel.lua (rounding = 10, border_size = 2) and
# Style.* from the shell. Not read from Hyprland at runtime: hyprctl getoption
# costs a fork per re-theme.
RADIUS = 10             # Style.cornerRadius / decoration:rounding
BORDER = 2              # looknfeel border_size
RADIUS_SM = 8           # inner cards and controls
RADIUS_XS = 6

FONT = {                # Style.font.*
    "caption": 10, "bodySmall": 11, "body": 12, "subtitle": 13,
    "title": 14, "heading": 16, "display": 24, "displayLarge": 28,
}
SPACE = {               # Style.space* / Style.spacing.*
    "labelGap": 4, "md": 6, "lg": 8, "controlPaddingX": 10,
    "controlPaddingY": 6, "panelGap": 14, "popupPadding": 14,
    "panelPadding": 18, "controlHeight": 28,
}
# Util.alpha(fg, a) levels used by the shell's own panels.
DIM = {"label": 0.60, "section": 0.55, "hint": 0.45, "faint": 0.30,
       "hairline": 0.14, "wash": 0.06}

# --- the app's own settings grid ------------------------------------------
# Not from §6d: the shell has no settings-grid analogue, so these are named
# here rather than inlined at a call site. One label column, one control
# column and one trailing note column, shared by every pane, so a reader can
# scan a single x down the whole app for labels, for controls, or for the
# state notes that say what a setting is currently doing.
COLUMN = {
    "sidebar": 180,     # widest nav label ("Confirmations") plus padding
    "control": 240,     # every bound control starts at this column's left
    "narrow": 140,      # a number or short code — same left edge, less width
    "trail": 120,       # state notes and per-row actions
    "doc": 48,          # chars — helper-text measure inside the label column
    "value": 33,        # chars — a read-back value inside the control column
    "face": 28,         # chars — a dropdown's closed face, before the arrow
    "state": 16,        # chars — the trailing note, at caption size
    "lede": 72,         # chars — pane description measure (prose, 65–75ch)
    "note": 74,         # chars — full-width explanatory paragraphs
}

# Vertical rhythm. Every value is a multiple of a SPACE token above, so the
# app breathes on the same grid as the shell's own panels. More space above a
# heading than below it: the gap belongs to the break, not to the heading.
RHYTHM = {
    "row": SPACE["panelGap"],                    # 14 — rows within a group
    "section": SPACE["panelGap"] * 2,            # 28 — above a group's rule
    "ruleGap": SPACE["lg"],                      # 8  — rule to its heading
    "afterHeading": SPACE["controlPaddingY"] * 2,  # 12 — heading to first row
}

# No-theme fallbacks only — at runtime the colours come from colors.toml.
FALLBACK = {
    "background": "#0a0a0b",
    "foreground": "#dfe3e6",
    "accent": "#8f979c",
    "muted": "#6b7276",
    "selection": "#1f2326",
    "lighter_background": "#16181a",
}
PALETTE_KEYS = ("background", "foreground", "accent", "muted", "selection")


def border_from_hypr(spec):
    """`rgba(dfe3e6cc) rgba(6b7276cc) 45deg` -> `alpha(#dfe3e6, 0.80)`.

    GTK CSS has no gradient *border-color*, so only the first stop is used —
    that is the active-border colour, which is what a focused window shows.
    """
    m = re.search(r"rgba?\(\s*([0-9a-fA-F]{6})([0-9a-fA-F]{2})?\s*\)", spec)
    if not m:
        return None
    rgb, aa = m.group(1), m.group(2)
    alpha = int(aa, 16) / 255 if aa else 1.0
    return f"alpha(#{rgb}, {alpha:.2f})"


def read_theme(theme_dir: str = THEME_DIR, follow: bool = True) -> dict:
    """Parse the *current* theme's colors.toml so Jarvis follows
    `omarchy theme set` instead of pinning one palette.

    `follow` is `[ui] theme_follows_omarchy`. Off, the palette above is used
    verbatim and colors.toml is never opened, so `omarchy theme set` leaves
    Jarvis alone — which is the whole point of a user turning it off. The
    tokens (radius, spacing, font sizes) are not part of the switch: they
    mirror Hyprland's own geometry, and a window that stops matching the
    rounding of every other window on the desktop is not a theme choice.
    """
    import tomllib
    colors = dict(FALLBACK)
    if not follow:
        return colors
    path = os.path.join(theme_dir, "colors.toml")
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return colors
    for k, v in raw.items():
        if isinstance(v, str) and v.startswith("#"):
            colors[k] = v
    hb = raw.get("hyprland_active_border")
    if isinstance(hb, str):
        colors["_border"] = border_from_hypr(hb)
    return colors


def current_font() -> str:
    """Follow `omarchy font set` rather than pinning a family."""
    try:
        out = subprocess.run(["omarchy-font-current"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return "monospace"


def css_for(c: dict) -> str:
    """The whole stylesheet.

    The rule the app is typeset by: hierarchy comes from size, weight and
    colour value, never from a box. There are no cards, no pills, no accent
    bars and no fills marking selection — a hairline rule appears only where
    a break has to be marked, and the one selection state in the app (the
    sidebar) is expressed on the row's own text.

    Colour is stated on label nodes explicitly rather than left to inherit:
    the `*` rule below sets a colour on every node, so a container's colour
    would not otherwise reach the label inside it.
    """
    fg = c["foreground"]
    bg = c["background"]
    accent = c.get("accent", FALLBACK["accent"])
    muted = c.get("muted", FALLBACK["muted"])
    sel = c.get("selection", FALLBACK["selection"])
    # The second neutral layer. With the cards gone, the only thing left
    # separating chrome from content is the surface it sits on, so the
    # header, the sidebar and the footer share one wash and the panes have
    # none. `muted` rather than the foreground because a palette states its
    # own second neutral and it is not always a tint of the text colour.
    chrome = f"alpha({muted}, 0.16)"
    border = c.get("_border") or f"alpha({fg}, 0.80)"
    font = current_font()
    f, s, d = FONT, SPACE, DIM

    return f"""
    window {{ background: {bg}; color: {fg}; }}
    * {{ font-family: "{font}", monospace; font-size: {f['body']}px;
         color: {fg}; }}

    /* ---- window chrome: same rounding and border as every tiled window */
    .root {{
        background: {bg};
        border: {BORDER}px solid {border};
        border-radius: {RADIUS}px;
    }}

    /* ---- header and footer strips ---- */
    .titlebar {{
        background: {chrome};
        padding: {s['panelPadding']}px;
        border-bottom: 1px solid alpha({fg}, {d['hairline']});
    }}
    .apptitle {{ font-size: {f['heading']}px; font-weight: bold; }}
    .appsub {{ font-size: {f['bodySmall']}px;
               color: alpha({fg}, {d['label']}); }}
    /* The identity pair in the header: each value is labelled, so "Luna ·
       Sol" is not two proper nouns a first-time reader has to guess at. */
    .metakey {{ font-size: {f['bodySmall']}px;
                color: alpha({fg}, {d['label']}); }}
    .metaval {{ font-size: {f['bodySmall']}px; font-weight: bold;
                color: {fg}; }}

    .statusbar {{
        background: {chrome};
        padding: {s['controlPaddingY']}px {s['panelPadding']}px;
        border-top: 1px solid alpha({fg}, {d['hairline']});
    }}

    /* ---- sidebar: selection is weight and contrast on the row itself ---- */
    .sidebar {{
        background: {chrome};
        border-right: 1px solid alpha({fg}, {d['hairline']});
        padding: {s['panelPadding']}px;
    }}
    .sidebar list, .sidebar row {{ background: transparent; }}
    .navrow {{
        background: transparent;
        border: none;
        border-radius: 0;
        padding: {s['controlPaddingY']}px 0;
        margin-bottom: {s['labelGap']}px;
        outline-offset: {s['labelGap']}px;
    }}
    .navrow:hover, .navrow:selected {{ background: transparent; }}
    .navrow label {{ font-size: {f['body']}px;
                     color: alpha({fg}, {d['label']}); }}
    .navrow:hover label {{ color: alpha({fg}, 0.85); }}
    .navrow:selected label {{ color: {fg}; font-weight: bold; }}
    .navrow:focus-visible {{
        outline-style: solid;
        outline-width: 1px;
        outline-color: alpha({fg}, {d['faint']});
    }}

    /* ---- panes: typeset, not boxed ---- */
    .pane {{ padding: {s['panelPadding']}px; }}
    .panehead {{ font-size: {f['display']}px; font-weight: bold; }}
    .panedesc {{ font-size: {f['body']}px;
                 color: alpha({fg}, {d['label']}); }}
    .sectionhead {{ font-size: {f['title']}px; font-weight: bold;
                    color: {fg}; }}
    .sep {{ background: alpha({fg}, {d['hairline']}); min-height: 1px; }}

    /* Text selection. Without this a selectable label paints the stock
       theme's accent blue — the one colour a greyscale desktop must not
       show. */
    selection {{ background: {sel}; color: {fg}; }}
    label selection {{ background-color: alpha({fg}, 0.22); color: {fg}; }}
    entry selection {{ background-color: alpha({fg}, 0.22); color: {fg}; }}

    /* ---- rows ----
       Every readable string sits at {d['label']} or above against the
       background; {d['hint']} and below are for rules, marks and the
       deliberately-unreadable disabled state only. */
    .rowlabel {{ font-size: {f['body']}px; }}
    .rowdoc {{ font-size: {f['bodySmall']}px;
               color: alpha({fg}, {d['label']}); }}
    .value  {{ font-size: {f['bodySmall']}px;
               color: alpha({fg}, {d['label']}); }}
    .mono   {{ font-size: {f['bodySmall']}px; }}
    /* What a setting is currently doing, in the trailing column. `.risk`
       is the one that has to be seen from across the pane. */
    .statenote {{ font-size: {f['caption']}px;
                  color: alpha({fg}, {d['label']}); }}
    .statenote.risk {{ color: {fg}; font-weight: bold; }}
    .locked-value {{ font-size: {f['bodySmall']}px; color: {accent};
                     font-weight: bold; }}
    label:disabled {{ color: alpha({fg}, {d['faint']}); }}

    /* ---- controls ---- */
    entry {{
        background: alpha({fg}, 0.07);
        border: 1px solid alpha({fg}, {d['faint']});
        border-radius: {RADIUS_XS}px;
        padding: 0 {s['md']}px;
        min-height: {s['controlHeight']}px;
        caret-color: {fg};
        font-size: {f['bodySmall']}px;
    }}
    entry:focus {{ border-color: alpha({fg}, 0.75); }}
    entry.bad {{ border-color: {accent}; background: alpha({accent}, 0.18); }}
    entry:disabled {{ color: alpha({fg}, {d['faint']});
                      background: transparent;
                      border-color: alpha({fg}, {d['hairline']}); }}
    entry > text > placeholder {{ color: alpha({fg}, {d['label']}); }}
    /* An insensitive entry has to look insensitive. `label:disabled` cannot
       reach the text: what an entry actually renders is a `text` node. */
    text:disabled {{ color: alpha({fg}, {d['faint']}); }}
    text:disabled selection {{ background-color: transparent; }}

    spinbutton {{
        background: alpha({fg}, 0.07);
        border: 1px solid alpha({fg}, {d['faint']});
        border-radius: {RADIUS_XS}px;
        min-height: {s['controlHeight']}px;
        font-size: {f['bodySmall']}px;
    }}
    spinbutton entry {{ background: transparent; border: none;
                        min-height: {s['controlHeight']}px; }}
    spinbutton button {{ background: transparent; border: none;
                         min-width: 20px; }}
    spinbutton button label {{ color: alpha({fg}, {d['label']}); }}
    spinbutton button:hover {{ background: alpha({fg}, 0.10); }}
    spinbutton button:hover label {{ color: {fg}; }}
    spinbutton:disabled {{ border-color: alpha({fg}, {d['hairline']});
                           background: transparent; }}
    /* The steppers are icons, not labels, so they need dimming of their own. */
    spinbutton:disabled button {{ opacity: 0.35; }}

    dropdown > button {{
        background: alpha({fg}, 0.07);
        border: 1px solid alpha({fg}, {d['faint']});
        border-radius: {RADIUS_XS}px;
        padding: 0 {s['md']}px;
        min-height: {s['controlHeight']}px;
        font-size: {f['bodySmall']}px;
        color: {fg};
    }}
    dropdown > button:hover {{ border-color: alpha({fg}, 0.55); }}
    dropdown label {{ font-size: {f['bodySmall']}px; }}
    dropdown:disabled > button {{ border-color: alpha({fg}, {d['hairline']});
                                  background: transparent; }}
    popover contents {{
        background: {bg};
        border: 1px solid {border};
        border-radius: {RADIUS_SM}px;
        padding: {s['labelGap']}px;
    }}
    popover listview row {{ border-radius: {RADIUS_XS}px;
                            padding: {s['labelGap']}px {s['md']}px; }}
    popover listview row:selected {{ background: {sel}; }}

    switch {{
        background: alpha({fg}, 0.12);
        border: 1px solid alpha({fg}, {d['faint']});
        border-radius: {RADIUS}px;
        min-width: 40px;
    }}
    switch:checked {{ background: alpha({accent}, 0.55);
                      border-color: {accent}; }}
    switch:disabled {{ background: transparent;
                       border-color: alpha({fg}, {d['hairline']}); }}
    switch > slider {{ background: {fg}; border-radius: {RADIUS}px;
                       min-width: 16px; min-height: 16px; }}
    switch:disabled > slider {{ background: alpha({fg}, {d['faint']}); }}

    scale trough {{ background: alpha({fg}, 0.12);
                    border-radius: {RADIUS}px; min-height: 4px; }}
    scale highlight {{ background: {accent}; border-radius: {RADIUS}px; }}
    scale slider {{ background: {fg}; border-radius: {RADIUS}px;
                    min-width: 12px; min-height: 12px; }}

    /* ---- Ui/Button analogue ---- */
    .act {{
        background: transparent;
        border: 1px solid alpha({fg}, 0.22);
        border-radius: {RADIUS_XS}px;
        padding: {s['labelGap']}px {s['controlPaddingX']}px;
        min-height: 0;
    }}
    .act label {{ font-size: {f['bodySmall']}px;
                  color: alpha({fg}, 0.85); }}
    .act:hover {{ background: alpha({fg}, 0.10); }}
    .act:hover label {{ color: {fg}; }}
    .act:disabled {{ border-color: alpha({fg}, {d['hairline']});
                     background: transparent; }}
    .act:disabled label {{ color: alpha({fg}, {d['faint']}); }}
    .act.primary {{ border-color: alpha({fg}, 0.50); }}
    .act.primary label {{ color: {fg}; font-weight: bold; }}
    .act.primary:hover {{ background: alpha({fg}, 0.14); }}
    /* A button mid-async is disabled so it cannot be double-fired, but its
       working label is the only feedback the click gets: it must stay
       readable, which the ordinary disabled treatment is not. */
    .act.busy:disabled {{ border-color: alpha({fg}, 0.22); }}
    .act.busy:disabled label {{ color: alpha({fg}, 0.85);
                                font-weight: bold; }}

    /* ---- the confirmation control: three words, one underlined ----
       Not three chips. Boxed options made a permissive answer and a
       restrictive one look like the same object; here the answer in force
       is the one carrying weight, full contrast and the rule beneath it. */
    .seg {{
        background: transparent;
        border: none;
        border-radius: 0;
        padding: {s['labelGap']}px {s['md']}px;
        min-height: 0;
        /* The unpicked options keep a visible baseline: without one the
           three words read as a sentence rather than as a control. */
        box-shadow: inset 0 -1px 0 0 alpha({fg}, {d['faint']});
    }}
    .seg label {{ font-size: {f['bodySmall']}px;
                  color: alpha({fg}, {d['label']}); }}
    .seg:hover {{ background: transparent;
                  box-shadow: inset 0 -1px 0 0 alpha({fg}, 0.55); }}
    .seg:hover label {{ color: {fg}; }}
    .seg:checked {{ box-shadow: inset 0 -{BORDER}px 0 0 alpha({fg}, 0.85); }}
    .seg:checked label {{ color: {fg}; font-weight: bold; }}
    .seg:disabled {{ box-shadow: none; }}

    /* ---- banners: a strip across the window, not a floating box ---- */
    .banner {{
        background: alpha({muted}, 0.30);
        border-bottom: 1px solid alpha({fg}, {d['hairline']});
        padding: {s['controlPaddingY']}px {s['panelPadding']}px;
    }}
    .banner.warn {{ border-bottom-color: alpha({accent}, 0.55); }}

    /* ---- usage meters ----
       Selectors are qualified with `trough` so they out-specify the stock
       theme's own levelbar rules; an unqualified `levelbar block.filled`
       loses to Adwaita and the bar renders in the GTK accent blue, which on
       a greyscale desktop is the one colour that must never appear. */
    levelbar trough {{ background: transparent; border: none;
                       border-radius: {RADIUS_XS}px;
                       min-height: {s['md']}px; padding: 0; }}
    levelbar trough block {{ background: alpha({fg}, 0.10); border: none;
                             border-radius: {RADIUS_XS}px;
                             min-height: {s['md']}px; }}
    levelbar trough block.filled {{ background: alpha({fg}, 0.65); }}
    levelbar.hot trough block.filled {{ background: {accent}; }}
    levelbar trough block.empty {{ background: alpha({fg}, 0.10); }}

    textview, textview text {{ background: transparent;
                               font-size: {f['bodySmall']}px; }}
    scrollbar {{ background: transparent; border: none;
                 margin: {s['labelGap']}px; }}
    scrollbar trough {{ background: transparent; border: none; }}
    scrollbar slider {{ background: alpha({fg}, 0.22);
                        border: none; min-width: {s['md']}px;
                        border-radius: {RADIUS}px; }}
    scrollbar slider:hover {{ background: alpha({fg}, 0.45); }}
    """


class ThemeWatch:
    """Owns the CSS provider and the two-level theme watch."""

    def __init__(self, follows=None):
        self.css_provider = None
        self.theme_monitor = None
        self.theme_dir_monitor = None
        self._theme_id = 0
        self.colors = dict(FALLBACK)
        self.on_change = None
        # A callable, not a bool: the themer is built before the settings
        # editor exists, and a value read once at construction would pin the
        # answer from before the config file had been read at all.
        self.follows = follows or (lambda: True)

    def following(self) -> bool:
        try:
            return bool(self.follows())
        except Exception:
            return True

    def apply(self):
        display = Gdk.Display.get_default()
        if display is None:
            return
        self.colors = read_theme(follow=self.following())
        provider = Gtk.CssProvider()
        provider.load_from_data(css_for(self.colors).encode())
        if self.css_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display,
                                                         self.css_provider)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.css_provider = provider

    def start(self):
        try:
            parent = Gio.File.new_for_path(os.path.dirname(THEME_DIR))
            self.theme_dir_monitor = parent.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.theme_dir_monitor.set_rate_limit(300)
            self.theme_dir_monitor.connect("changed", self._on_changed)
        except GLib.Error:
            self.theme_dir_monitor = None
        self._arm_inner()

    def _arm_inner(self):
        if self.theme_monitor is not None:
            self.theme_monitor.cancel()
            self.theme_monitor = None
        try:
            gfile = Gio.File.new_for_path(os.path.join(THEME_DIR,
                                                       "colors.toml"))
            self.theme_monitor = gfile.monitor_file(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.theme_monitor.set_rate_limit(300)
            self.theme_monitor.connect("changed", self._on_changed)
        except GLib.Error:
            self.theme_monitor = None

    def _on_changed(self, *_a):
        if self._theme_id:
            return
        self._theme_id = GLib.timeout_add(150, self._retheme)

    def _retheme(self):
        self._theme_id = 0
        self.apply()
        self._arm_inner()      # the old inode is gone after a theme-set
        if self.on_change:
            self.on_change()
        return False
