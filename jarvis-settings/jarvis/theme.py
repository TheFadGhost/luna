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


def read_theme(theme_dir: str = THEME_DIR) -> dict:
    """Parse the *current* theme's colors.toml so Jarvis follows
    `omarchy theme set` instead of pinning one palette."""
    import tomllib
    colors = dict(FALLBACK)
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
    fg = c["foreground"]
    bg = c["background"]
    accent = c.get("accent", FALLBACK["accent"])
    muted = c.get("muted", FALLBACK["muted"])
    sel = c.get("selection", FALLBACK["selection"])
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

    /* ---- title bar ---- */
    .titlebar {{
        padding: {s['controlPaddingY']}px {s['panelPadding']}px;
        border-bottom: 1px solid alpha({fg}, {d['hairline']});
    }}
    .apptitle {{ font-size: {f['title']}px; font-weight: bold;
                 letter-spacing: 2px; }}
    .appsub {{ font-size: {f['caption']}px; color: alpha({fg}, {d['hint']});
               letter-spacing: 1.2px; }}

    /* ---- sidebar ---- */
    .sidebar {{
        background: alpha({fg}, {d['wash']});
        border-right: 1px solid alpha({fg}, {d['hairline']});
        padding: {s['lg']}px {s['md']}px;
    }}
    .sidebar list, .sidebar row {{ background: transparent; }}
    .navrow {{
        border-radius: {RADIUS_SM}px;
        padding: {s['controlPaddingY']}px {s['controlPaddingX']}px;
        margin-bottom: {s['labelGap']}px;
        color: alpha({fg}, {d['label']});
    }}
    .navrow:hover {{ background: alpha({fg}, 0.08); color: {fg}; }}
    .navrow:selected {{
        background: {sel};
        color: {fg};
        font-weight: bold;
        box-shadow: inset {BORDER}px 0 0 0 {accent};
    }}
    .navrow label {{ font-size: {f['body']}px; }}
    .navnum {{ font-size: {f['caption']}px; color: alpha({fg}, {d['faint']}); }}

    /* ---- panes ---- */
    .pane {{ padding: {s['panelPadding']}px; }}
    .panehead {{ font-size: {f['heading']}px; font-weight: bold; }}
    .panedesc {{ font-size: {f['bodySmall']}px;
                 color: alpha({fg}, {d['label']}); }}

    /* ---- BorderSurface analogue: the card every group of rows sits in */
    .card {{
        background: alpha({fg}, {d['wash']});
        border: 1px solid alpha({fg}, {d['hairline']});
        border-radius: {RADIUS_SM}px;
        padding: {s['popupPadding']}px;
    }}
    .card.flat {{ background: transparent; }}

    /* ---- PanelSectionHeader analogue ---- */
    .sectionhead {{
        font-size: {f['caption']}px;
        font-weight: bold;
        letter-spacing: 1.2px;
        color: alpha({fg}, {d['section']});
    }}
    .sep {{ background: alpha({fg}, {d['hairline']}); min-height: 1px; }}

    /* Text selection. Without this a selectable label paints the stock
       theme's accent blue — the one colour a greyscale desktop must not
       show. */
    selection {{ background: {sel}; color: {fg}; }}
    label selection {{ background-color: alpha({fg}, 0.22); color: {fg}; }}
    entry selection {{ background-color: alpha({fg}, 0.22); color: {fg}; }}

    /* ---- rows ---- */
    .rowlabel {{ font-size: {f['body']}px; }}
    .rowdoc {{ font-size: {f['caption']}px; color: alpha({fg}, {d['hint']}); }}
    .value  {{ font-size: {f['bodySmall']}px;
               color: alpha({fg}, {d['label']}); }}
    .mono   {{ font-size: {f['bodySmall']}px; }}
    .locked-value {{ font-size: {f['bodySmall']}px; color: {accent};
                     font-weight: bold; letter-spacing: 1.2px; }}

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
    entry:focus {{ border-color: {accent}; }}
    entry.bad {{ border-color: {accent}; background: alpha({accent}, 0.18); }}
    entry:disabled {{ color: alpha({fg}, {d['faint']});
                      background: transparent; }}

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
                         color: alpha({fg}, {d['label']}); min-width: 20px; }}
    spinbutton button:hover {{ color: {fg}; background: alpha({fg}, 0.10); }}

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
    switch > slider {{ background: {fg}; border-radius: {RADIUS}px;
                       min-width: 16px; min-height: 16px; }}

    scale trough {{ background: alpha({fg}, 0.12);
                    border-radius: {RADIUS}px; min-height: 4px; }}
    scale highlight {{ background: {accent}; border-radius: {RADIUS}px; }}
    scale slider {{ background: {fg}; border-radius: {RADIUS}px;
                    min-width: 12px; min-height: 12px; }}

    /* ---- Ui/Button analogue ---- */
    .act {{
        background: transparent;
        color: alpha({fg}, 0.85);
        border: 1px solid alpha({fg}, 0.22);
        border-radius: {RADIUS_SM}px;
        padding: {s['labelGap']}px {s['controlPaddingX']}px;
        font-size: {f['bodySmall']}px;
        min-height: 0;
    }}
    .act:hover {{ background: alpha({fg}, 0.10); color: {fg}; }}
    .act:disabled {{ color: alpha({fg}, {d['faint']});
                     border-color: alpha({fg}, {d['hairline']}); }}
    .act.primary {{ border-color: {accent}; color: {fg};
                    background: alpha({accent}, 0.18); }}
    .act.primary:hover {{ background: alpha({accent}, 0.32); }}

    /* ---- three-way confirmation control ---- */
    .seg {{
        background: transparent;
        color: alpha({fg}, {d['label']});
        border: 1px solid alpha({fg}, 0.20);
        border-radius: {RADIUS_XS}px;
        padding: {s['labelGap']}px {s['md']}px;
        font-size: {f['caption']}px;
        letter-spacing: 0.6px;
        min-height: 0;
    }}
    .seg:hover {{ color: {fg}; background: alpha({fg}, 0.08); }}
    .seg:checked {{ color: {fg}; font-weight: bold;
                    background: {sel}; border-color: {accent}; }}

    /* ---- the four immovable denies: text, not widgets ---- */
    .lockedcard {{
        border: 1px dashed alpha({accent}, 0.65);
        border-radius: {RADIUS_SM}px;
        padding: {s['popupPadding']}px;
        background: alpha({muted}, 0.10);
    }}
    .lockedname {{ font-size: {f['body']}px; font-weight: bold; }}
    .lockedwhy  {{ font-size: {f['caption']}px;
                   color: alpha({fg}, {d['label']}); }}
    .lockmark {{ font-size: {f['bodySmall']}px; color: {accent}; }}

    /* ---- banners ---- */
    .banner {{
        border: 1px solid alpha({fg}, 0.35);
        border-radius: {RADIUS_SM}px;
        padding: {s['controlPaddingY']}px {s['controlPaddingX']}px;
        font-size: {f['bodySmall']}px;
    }}
    .banner.warn {{ border-color: {accent};
                    background: alpha({accent}, 0.14); }}
    .banner.ok {{ border-color: alpha({fg}, {d['faint']}); }}

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

    def __init__(self):
        self.css_provider = None
        self.theme_monitor = None
        self.theme_dir_monitor = None
        self._theme_id = 0
        self.colors = dict(FALLBACK)
        self.on_change = None

    def apply(self):
        display = Gdk.Display.get_default()
        if display is None:
            return
        self.colors = read_theme()
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
