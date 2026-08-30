"""Jarvis — the settings app for the Luna assistant daemon.

A sidebar and seven panes, drawn from the live Omarchy palette so it sits
next to Sill rather than next to a stock GTK dialog. The window has no
GtkHeaderBar: it draws its own title row, the same way every other surface on
this desktop does.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import async_util, client, config, panes, theme, voices
from .editor import Editor
from .theme import SPACE
from .widgets import act, banner, column, label, rowbox

APP_ID = "org.omarchy.jarvis"
WIN_TITLE = "Jarvis"
WIN_W, WIN_H = 1000, 720
SIDEBAR_W = 210

PANES = (
    ("assistant", "Assistant"),
    ("voice", "Voice"),
    ("listen", "Listening"),
    ("confirm", "Confirmations"),
    ("memory", "Memory"),
    ("jobs", "Jobs"),
    ("about", "About"),
)


USAGE = """Jarvis — settings for the Luna assistant daemon.

  jarvis-settings [--pane NAME]

  --pane NAME   open on one pane: """ + ", ".join(k for k, _ in PANES) + """
  --version     print the version and exit
"""


def first_focusable(widget):
    """Depth-first search for the first widget that can take the caret."""
    if widget is None:
        return None
    stack = [widget]
    while stack:
        w = stack.pop(0)
        if w is not widget and w.get_focusable() and w.get_sensitive() \
                and w.get_visible():
            return w
        child = w.get_first_child()
        kids = []
        while child is not None:
            kids.append(child)
            child = child.get_next_sibling()
        stack = kids + stack
    return None


class JarvisApp(Gtk.Application):
    def __init__(self):
        # HANDLES_COMMAND_LINE so `--pane` reaches an instance that is already
        # running: a second launch of a single-instance app otherwise just
        # raises the existing window and drops its arguments.
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.start_pane = None
        self.win = None
        self.themer = None
        self.editor = None
        self.binder = None
        self.player = voices.Player()
        self.stack = None
        self.status_label = None
        self._status_timer = 0
        self._daemon_banner = None
        self._daemon_label = None
        self._daemon_start = None
        self._pane_widgets = {}
        self._daemon_fetching = False

    # ------------------------------------------------------------ lifecycle
    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if "--version" in args or "-V" in args:
            from . import __version__
            cmdline.print_literal(f"jarvis-settings {__version__}\n")
            return 0
        if "--help" in args or "-h" in args:
            cmdline.print_literal(USAGE)
            return 0
        if "--pane" in args:
            i = args.index("--pane")
            if i + 1 < len(args):
                self.start_pane = args[i + 1]
        self.activate()
        if self.start_pane:
            self.show_pane(self.start_pane, focus=True)
        return 0

    def show_pane(self, key, focus=False):
        for i, (name, _title) in enumerate(PANES):
            if name == key:
                self.nav.select_row(self.nav.get_row_at_index(i))
                if focus:
                    # Asked for by name, so put the caret on the pane's first
                    # control: someone who opened straight to a pane wants to
                    # edit it, not to arrow around the sidebar. child_focus()
                    # is not enough here — the sidebar already holds focus and
                    # keeps it — so the target widget is focused explicitly.
                    target = first_focusable(self._pane_widgets.get(key))
                    if target is not None:
                        self.win.set_focus(target)
                return True
        self.set_status(f"no pane called {key!r}", "error")
        return False

    def do_activate(self):
        if self.win is not None:
            self.win.present()
            return

        self.themer = theme.ThemeWatch(follows=self.theme_follows_omarchy)
        self.themer.apply()
        self.themer.start()

        # Create the config the first time the app is opened, so lunad and the
        # GUI agree on a starting point instead of both guessing defaults.
        try:
            config.write_default()
        except (OSError, ValueError) as exc:
            print(f"jarvis: could not create config: {exc}", file=sys.stderr)

        self.editor = Editor(on_status=self.set_status,
                             on_external_reload=self.reload_controls,
                             on_applied=self.settings_applied)
        self.editor.watch()
        # The themer was built before the editor could say whether the user
        # wants the Omarchy palette at all, so it defaulted to yes. Now that
        # the file has been read, ask again.
        self.settings_applied(["ui.theme_follows_omarchy"])
        self.binder = panes.Binder(self.editor)

        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_title(WIN_TITLE)
        self.win.set_default_size(WIN_W, WIN_H)
        self.win.set_child(self._build())
        self.win.connect("close-request", self._on_close)
        self.win.present()

        for warn in self.editor.warnings:
            self.set_status(warn, "error")
        if self.start_pane:
            self.show_pane(self.start_pane, focus=True)
        self.refresh_daemon()
        GLib.timeout_add_seconds(5, self._poll_daemon)

    def _on_close(self, *_a):
        self.editor.flush_now()
        self.player.stop()          # our own aplay child, nothing else
        return False

    # ------------------------------------------------------------ chrome
    def _build(self):
        root = column(0)
        root.add_css_class("root")

        root.append(self._titlebar())

        self._daemon_banner, self._daemon_label = banner("", "warn")
        self._daemon_start = act("Start daemon", primary=True)
        self._daemon_start.connect("clicked", lambda _b: self.start_daemon())
        self._daemon_banner.append(self._daemon_start)
        wrap = rowbox()
        wrap.set_margin_start(SPACE["panelPadding"])
        wrap.set_margin_end(SPACE["panelPadding"])
        wrap.set_margin_top(SPACE["md"])
        self._daemon_banner.set_hexpand(True)
        wrap.append(self._daemon_banner)
        self._banner_wrap = wrap
        root.append(wrap)

        body = rowbox(0)
        body.set_vexpand(True)
        body.append(self._sidebar())
        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        for key, _title in PANES:
            w = self._pane(key)
            self._pane_widgets[key] = w
            self.stack.add_named(w, key)
        body.append(self.stack)
        root.append(body)
        # Selected only now: row-selected fires synchronously, and the handler
        # needs the stack to exist.
        self.nav.select_row(self.nav.get_row_at_index(0))

        root.append(self._statusbar())
        return root

    def _titlebar(self):
        bar = rowbox(SPACE["controlPaddingX"])
        bar.add_css_class("titlebar")
        left = column(0)
        left.append(label(WIN_TITLE.upper(), css=("apptitle",)))
        left.append(label("SETTINGS FOR THE ASSISTANT DAEMON",
                          css=("appsub",)))
        left.set_hexpand(True)
        bar.append(left)
        self._who = label("", css=("value",))
        bar.append(self._who)
        return bar

    def _sidebar(self):
        box = column(0)
        box.add_css_class("sidebar")
        box.set_size_request(SIDEBAR_W, -1)
        self.nav = Gtk.ListBox()
        self.nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for i, (key, title) in enumerate(PANES):
            r = Gtk.ListBoxRow()
            r.add_css_class("navrow")
            inner = rowbox(SPACE["md"])
            inner.append(label(f"{i + 1}", css=("navnum",)))
            t = label(title)
            t.set_hexpand(True)
            inner.append(t)
            r.set_child(inner)
            r.jarvis_key = key
            self.nav.append(r)
        self.nav.connect("row-selected", self._nav)
        box.append(self.nav)
        return box

    def _statusbar(self):
        bar = rowbox(SPACE["controlPaddingX"])
        bar.add_css_class("titlebar")
        self.status_label = label("", css=("rowdoc",), wrap=False)
        self.status_label.set_hexpand(True)
        self.status_label.set_ellipsize(3)          # Pango.EllipsizeMode.END
        bar.append(self.status_label)
        bar.append(label(str(config.CONFIG_PATH), css=("rowdoc",)))
        return bar

    def _pane(self, key):
        b = self.binder
        if key == "assistant":
            return panes.assistant_pane(b)
        if key == "voice":
            return panes.voice_pane(b, self.player, self.set_status)
        if key == "listen":
            return panes.listen_pane(b, self.set_status)
        if key == "confirm":
            return panes.confirm_pane(b)
        if key == "memory":
            return panes.memory_pane(b, self.win)
        if key == "jobs":
            return panes.jobs_pane(b)
        return panes.about_pane(b, self.start_daemon)

    # ------------------------------------------------------------ behaviour
    def _nav(self, _lb, row_):
        if row_ is None or self.stack is None:
            return
        key = getattr(row_, "jarvis_key", None)
        if key:
            self.stack.set_visible_child_name(key)
            w = self._pane_widgets.get(key)
            fn = getattr(w, "jarvis_refresh", None)
            if fn:
                fn()

    def set_status(self, text, kind="ok"):
        if self.status_label is None:
            print(f"jarvis: {text}", file=sys.stderr)
            return
        self.status_label.set_text(text)
        self.status_label.remove_css_class("locked-value")
        if kind == "error":
            self.status_label.add_css_class("locked-value")
        if self._status_timer:
            GLib.source_remove(self._status_timer)
        self._status_timer = GLib.timeout_add_seconds(12, self._clear_status)
        self._who.set_text(
            f"{self.editor.get('assistant.name')} · "
            f"{self.editor.get('assistant.specialist')}")

    def _clear_status(self):
        self._status_timer = 0
        self.status_label.set_text("")
        self.status_label.remove_css_class("locked-value")
        return False

    def reload_controls(self, keys):
        self.binder.refresh(keys)
        for w in self._pane_widgets.values():
            fn = getattr(w, "jarvis_refresh", None)
            if fn:
                fn()
        self.settings_applied(keys)

    # ------------------------------------------------------------ [ui] keys
    def theme_follows_omarchy(self):
        """`[ui] theme_follows_omarchy`, defaulting to on.

        Read from the editor rather than cached: the themer asks on every
        re-theme, and the editor is the only object that knows what the file
        currently says.
        """
        if self.editor is None:
            return True
        value = self.editor.get("ui.theme_follows_omarchy")
        return True if value is None else bool(value)

    def settings_applied(self, keys):
        """Re-chrome the window for settings that change the app itself.

        lunad hot-reloads its own half through the config file; nothing in
        that path can restyle a GTK window, so the one `[ui]` key that is
        about this app is applied here.
        """
        if self.themer is not None and "ui.theme_follows_omarchy" in set(keys):
            self.themer.apply()

    def start_daemon(self):
        ok, detail = client.start_daemon()
        self.set_status(("Started lunad · " if ok else "Could not start "
                         "lunad · ") + detail, "ok" if ok else "error")
        GLib.timeout_add_seconds(1, lambda: (self.refresh_daemon(), False)[1])

    def refresh_daemon(self):
        """Kick off a background daemon-liveness check; the banner and the
        "who" line update when it lands. Safe to call again while one is
        already in flight — the call is simply skipped, so a slow or
        hanging daemon does not pile up overlapping probes on the 5s timer
        (each one blocked the GTK main loop for up to ~7s before this).
        """
        if self._daemon_fetching:
            return
        self._daemon_fetching = True

        def work():
            up = client.alive(timeout=0.6)
            if up and self.editor.settings.supported is None:
                self.editor.settings.get()      # one cheap capability probe
            return up

        def done(up, error):
            self._daemon_fetching = False
            self._apply_daemon_state(bool(up) and error is None)
            return False

        async_util.run_async(work, done)

    def _apply_daemon_state(self, up):
        self._banner_wrap.set_visible(not up)
        if not up:
            self._daemon_label.set_text(
                "lunad is not running. Settings still save to config.toml and "
                "apply when it next starts.")
        self._who.set_text(
            f"{self.editor.get('assistant.name')} · "
            f"{self.editor.get('assistant.specialist')}")

    def _poll_daemon(self):
        self.refresh_daemon()
        return True


def main(argv=None):
    # Without this the secondary windows (the memories viewer) report the
    # program name as their Wayland app_id instead of APP_ID, and a window
    # rule written for org.omarchy.jarvis would miss them.
    GLib.set_prgname(APP_ID)
    app = JarvisApp()
    return app.run(argv if argv is not None else sys.argv)
