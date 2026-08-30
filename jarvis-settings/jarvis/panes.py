"""The seven panes.

Every editable control is produced by `Binder.control_for(dotted)`, which
dispatches on the schema field kind. That is the whole point: a setting added
to schema.SPEC gets a correctly typed, correctly validated, correctly bound
widget without a pane having to know anything about it.
"""

from __future__ import annotations

import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import async_util, client, config, models, schema, state, voices, voxtype
from .theme import SPACE
from .widgets import (TriToggle, act, banner, card, column, label,
                      locked_entry, row, rowbox, scroller, section_header,
                      separator)


class Binder:
    """Builds bound controls and keeps them in step with the Editor."""

    def __init__(self, editor):
        self.editor = editor
        self._refresh = {}          # dotted -> fn(value)

    # ------------------------------------------------------------ dispatch
    def control_for(self, dotted, width=200):
        found = schema.field_for(dotted)
        if found is None:
            return label(f"unknown setting {dotted}", css=("rowdoc",))
        _sec, fld = found
        value = self.editor.get(dotted)
        if isinstance(fld, schema.Toggle):
            return self._switch(dotted, value)
        if isinstance(fld, schema.Tri):
            return self._tri(dotted, fld, value)
        if isinstance(fld, (schema.Choice,)):
            return self._dropdown(dotted, fld.options, value, width)
        if isinstance(fld, schema.VoicePick):
            opts = voices.available() or [value]
            if value not in opts:
                opts = [value] + opts
            return self._dropdown(dotted, opts, value, width,
                                  labeller=voices.label_for)
        if isinstance(fld, schema.Number):
            return self._spin(dotted, fld.min, fld.max, fld.step, 0, value,
                              width)
        if isinstance(fld, schema.Real):
            return self._spin(dotted, fld.min, fld.max, fld.step, fld.digits,
                              value, width)
        if isinstance(fld, schema.ModelPick):
            return self._model_entry(dotted, fld, value, width)
        if isinstance(fld, schema.Text):
            return self._entry(dotted, fld, value, width)
        return label(str(value), css=("value",))

    def bound_row(self, dotted, width=200, extra=(), ctl=None):
        found = schema.field_for(dotted)
        if found is None:
            return label(f"missing {dotted}", css=("rowdoc",))
        _sec, fld = found
        if ctl is None:
            ctl = self.control_for(dotted, width)
        unit = getattr(fld, "unit", "")
        parts = [ctl]
        if unit and not isinstance(fld, schema.Real):
            parts.append(label(unit, css=("rowdoc",)))
        parts.extend(extra)
        return row(fld.label, fld.doc, *parts, control_width=width + 40)

    def refresh(self, keys=None):
        for dotted, fn in self._refresh.items():
            if keys is None or dotted in keys:
                fn(self.editor.get(dotted))

    # ------------------------------------------------------------ builders
    def _switch(self, dotted, value):
        sw = Gtk.Switch()
        sw.set_active(bool(value))
        sw.set_halign(Gtk.Align.END)
        sw.set_valign(Gtk.Align.CENTER)
        guard = {"muted": False}

        def toggled(w, _p):
            if not guard["muted"]:
                self.editor.set(dotted, w.get_active())

        sw.connect("notify::active", toggled)

        def put(v):
            guard["muted"] = True
            sw.set_active(bool(v))
            guard["muted"] = False

        self._refresh[dotted] = put
        return sw

    def _tri(self, dotted, fld, value):
        t = TriToggle(fld.options, schema.TRI_LABELS, value,
                      lambda v: self.editor.set(dotted, v))
        self._refresh[dotted] = t.set_value
        return t

    def _dropdown(self, dotted, options, value, width, labeller=str):
        options = list(options)
        dd = Gtk.DropDown.new_from_strings([labeller(o) for o in options])
        dd.set_size_request(width, -1)
        try:
            dd.set_selected(options.index(value))
        except ValueError:
            dd.set_selected(0)
        guard = {"muted": False}

        def changed(w, _p):
            if guard["muted"]:
                return
            i = w.get_selected()
            if 0 <= i < len(options):
                self.editor.set(dotted, options[i])

        dd.connect("notify::selected", changed)

        def put(v):
            guard["muted"] = True
            try:
                dd.set_selected(options.index(v))
            except ValueError:
                pass
            guard["muted"] = False

        self._refresh[dotted] = put
        dd.jarvis_options = options
        return dd

    def _spin(self, dotted, lo, hi, step, digits, value, width):
        sb = Gtk.SpinButton.new_with_range(float(lo), float(hi), float(step))
        sb.set_digits(digits)
        sb.set_value(float(value or 0))
        sb.set_size_request(width, -1)
        sb.set_numeric(True)
        guard = {"muted": False}

        def changed(w):
            if guard["muted"]:
                return
            v = w.get_value()
            self.editor.set(dotted, v if digits else int(round(v)))

        sb.connect("value-changed", changed)

        def put(v):
            guard["muted"] = True
            sb.set_value(float(v or 0))
            guard["muted"] = False

        self._refresh[dotted] = put
        return sb

    def _entry(self, dotted, fld, value, width):
        e = Gtk.Entry()
        e.set_text(str(value or ""))
        e.set_size_request(width, -1)
        if getattr(fld, "placeholder", ""):
            e.set_placeholder_text(fld.placeholder)
        if fld.readonly:
            e.set_editable(False)
            e.set_can_focus(False)
            e.set_sensitive(False)
            return e
        guard = {"muted": False}

        def changed(w):
            if guard["muted"]:
                return
            try:
                config.coerce(dotted, w.get_text())
            except config.ValidationError:
                w.add_css_class("bad")     # no status spam mid-typing
                return
            w.remove_css_class("bad")
            self.editor.set(dotted, w.get_text())

        def left(_ctl):
            if "bad" in e.get_css_classes():
                guard["muted"] = True
                e.set_text(str(self.editor.get(dotted) or ""))
                e.remove_css_class("bad")
                guard["muted"] = False

        e.connect("changed", changed)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", left)
        e.add_controller(focus)

        def put(v):
            guard["muted"] = True
            e.set_text(str(v or ""))
            e.remove_css_class("bad")
            guard["muted"] = False

        self._refresh[dotted] = put
        return e

    def _model_entry(self, dotted, fld, value, width):
        """`assistant.model`: free text (any slug is accepted — see
        schema.ModelPick) with per-agent suggestions and a non-blocking
        "not a known slug" hint below the field. The current agent is
        pushed in from outside via the box's `jarvis_set_agent(agent)`,
        wired up in assistant_pane once both controls exist.

        Presentation is deliberately plain (existing "rowdoc" hint style,
        no new CSS) — a design pass owns how this looks; this is the logic
        that pass will hang its presentation off.
        """
        outer = column(SPACE["labelGap"] // 2)
        e = Gtk.Entry()
        e.set_text(str(value or ""))
        e.set_size_request(width, -1)
        completion = Gtk.EntryCompletion()
        store = Gtk.ListStore(str)
        completion.set_model(store)
        completion.set_text_column(0)
        completion.set_inline_completion(False)
        completion.set_popup_completion(True)
        e.set_completion(completion)
        hint = label("", css=("rowdoc",), wrap=True)
        outer.append(e)
        outer.append(hint)

        agent_state = {"agent": ""}

        def refresh_hint(entry):
            text = entry.get_text().strip()
            opts = models.suggestions_for(agent_state["agent"])
            if not text or text in opts:
                hint.set_text("")
                return
            agent = agent_state["agent"] or "this agent"
            hint.set_text(f"not a known {agent} slug — saved as typed")

        def set_agent(agent):
            agent_state["agent"] = agent
            store.clear()
            opts = models.suggestions_for(agent)
            for s in opts:
                store.append([s])
            e.set_placeholder_text(
                f"agent default, e.g. {opts[0]}" if opts else "agent default")
            refresh_hint(e)

        set_agent(agent_state["agent"])

        guard = {"muted": False}

        def changed(w):
            if guard["muted"]:
                return
            try:
                config.coerce(dotted, w.get_text())
            except config.ValidationError:
                w.add_css_class("bad")
                return
            w.remove_css_class("bad")
            self.editor.set(dotted, w.get_text())
            refresh_hint(w)

        e.connect("changed", changed)
        focus = Gtk.EventControllerFocus()

        def left(ctl):
            w = ctl.get_widget()
            if "bad" in w.get_css_classes():
                guard["muted"] = True
                w.set_text(str(self.editor.get(dotted) or ""))
                w.remove_css_class("bad")
                guard["muted"] = False
                refresh_hint(w)

        focus.connect("leave", left)
        e.add_controller(focus)

        def put(v):
            guard["muted"] = True
            e.set_text(str(v or ""))
            e.remove_css_class("bad")
            guard["muted"] = False
            refresh_hint(e)

        self._refresh[dotted] = put
        outer.jarvis_set_agent = set_agent
        return outer


# ------------------------------------------------------------------ helpers

def head(title, desc=""):
    box = column(SPACE["labelGap"])
    box.append(label(title, css=("panehead",)))
    if desc:
        box.append(label(desc, css=("panedesc",), wrap=True))
    return box


def group(title, *rows, flat=False):
    c = card(flat=flat)
    if title:
        c.append(section_header(title))
    for i, r in enumerate(rows):
        if i or title:
            c.append(separator())
        c.append(r)
    return c


def pane(*children):
    body = column()
    body.add_css_class("pane")
    for ch in children:
        body.append(ch)
    return scroller(body)


def open_path(path):
    """Hand a path to the desktop. Our own child, launched detached."""
    try:
        Gio.AppInfo.launch_default_for_uri(
            GLib.filename_to_uri(str(path), None), None)
        return True
    except GLib.Error:
        try:
            subprocess.Popen(["xdg-open", str(path)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            return False


# ------------------------------------------------------------------- panes

def assistant_pane(b):
    keys = ("assistant.name", "assistant.specialist", "assistant.agent",
            "assistant.model")
    agent_ctl = b.control_for(keys[2])
    model_ctl = b.control_for(keys[3], width=240)

    def on_agent_changed(w, _p):
        opts = getattr(w, "jarvis_options", None) or ()
        i = w.get_selected()
        set_agent = getattr(model_ctl, "jarvis_set_agent", None)
        if set_agent is not None and 0 <= i < len(opts):
            set_agent(opts[i])

    agent_ctl.connect("notify::selected", on_agent_changed)
    # Prime the model hint/suggestions from whatever the agent dropdown
    # already shows, rather than waiting for the first change.
    on_agent_changed(agent_ctl, None)

    return pane(
        head("Assistant",
             "Who she is. The name is a setting, so every prompt, greeting "
             "and log label follows it."),
        group("Identity", b.bound_row(keys[0]), b.bound_row(keys[1])),
        group("Brain",
              b.bound_row(keys[2], ctl=agent_ctl),
              b.bound_row(keys[3], width=240, ctl=model_ctl)),
    )


def _busy_click(btn, working_label, work, on_done):
    """Disable `btn` and swap its label for the duration of `work` (run off
    the GTK thread), so it cannot be clicked twice while a preview/say/
    restart is in flight, and so it visibly shows it is doing something.
    `on_done(result, error)` runs back on the GTK thread with the button
    already restored to normal."""
    if not btn.get_sensitive():
        return                      # already working; a second click is a no-op
    orig = btn.get_label()
    btn.set_sensitive(False)
    btn.set_label(working_label)

    def done(result, error):
        btn.set_label(orig)
        btn.set_sensitive(True)
        on_done(result, error)
        return False

    async_util.run_async(work, done)


def voice_pane(b, player, on_status):
    def preview(dotted):
        def clicked(btn):
            def work():
                return player.preview(b.editor.get(dotted))

            def done(result, error):
                if error is not None:
                    on_status(f"Preview failed: {error}", "error")
                    return
                ok, path = result
                on_status(("Preview: " if ok else "Preview failed: ") + path,
                          "ok" if ok else "error")

            _busy_click(btn, "Playing…", work, done)
        return clicked

    prev_a = act("▶ Preview")
    prev_a.connect("clicked", preview("voice.voice"))
    prev_b = act("▶ Preview")
    prev_b.connect("clicked", preview("voice.voice_male"))

    live = act("Test the live pipeline")

    def test(btn):
        def work():
            return player.speak_via_daemon()

        def done(result, error):
            if error is not None:
                on_status(f"Could not speak: {error}", "error")
                return
            ok, detail = result
            on_status(("Spoke via " if ok else "Could not speak: ") + detail,
                      "ok" if ok else "error")

        _busy_click(btn, "Testing…", work, done)

    live.connect("clicked", test)

    n = len(voices.available())
    return pane(
        head("Voice out",
             "How she sounds. Preview plays the local sample for the picked "
             "voice; the live test speaks through the daemon with whatever "
             "is currently saved."),
        group("Output",
              b.bound_row("voice.enabled"),
              b.bound_row("voice.provider"),
              b.bound_row("voice.model", width=240)),
        group(f"Voice — {n} samples in ~/Music/luna-voices",
              b.bound_row("voice.voice", width=220, extra=(prev_a,)),
              b.bound_row("voice.voice_male", width=220, extra=(prev_b,)),
              row("Live check",
                  "Speaks one sentence through lunad's say op.", live,
                  control_width=260)),
        group("Fallback and delivery",
              b.bound_row("voice.fallback"),
              b.bound_row("voice.piper_voice", width=240),
              b.bound_row("voice.speed", width=140),
              b.bound_row("voice.max_spoken_chars", width=140)),
    )


def _voxtype_report(values):
    """Lines for the Listening pane: where the two files disagree, and whether
    the running daemon has read either of them.

    Both questions have to be asked, because they fail independently. A save
    that could not restart voxtype leaves the files in step and the *daemon*
    out of step, which is the §8a.2 gotcha and looks like success from every
    angle except the microphone.
    """
    try:
        rows = voxtype.drift(values)
    except voxtype.VoxtypeError as exc:
        return [(str(exc), ("locked-value",))]
    out = []
    if rows:
        out.append(("Jarvis and voxtype disagree. Neither file is changed to "
                    "match the other until you save a listening setting here.",
                    ("locked-value",)))
        for r in rows:
            out.append((f"{r['key'].rpartition('.')[2]} · Jarvis has "
                        f"{r['jarvis']!r}, voxtype's file has {r['voxtype']!r}",
                        ("rowdoc",)))
    else:
        out.append((f"In step with {voxtype.CONFIG_PATH}.", ("rowdoc",)))
    reading = voxtype.stale()
    if reading is True:
        out.append(("voxtype started before that file was last changed, so it "
                    "is still listening with what it loaded then. It only "
                    "reads its config at start-up — restart it.",
                    ("locked-value",)))
    elif reading is None:
        out.append(("voxtype is not running. It reads that file when it "
                    "starts, so the settings are already waiting for it.",
                    ("rowdoc",)))
    return out


def listen_pane(b, on_status):
    kb = b.editor.get("listen.keybind")
    note = label(
        f"{kb} runs `voxtype record toggle --profile luna`. The binding lives "
        "in ~/.config/hypr/bindings.lua and is edited there — changing it here "
        "would put Jarvis and Hyprland out of step.",
        css=("rowdoc",), wrap=True)
    mapping = label(
        "Provider, model and language are written through to "
        f"{voxtype.CONFIG_PATH} — provider becomes [whisper] mode, model "
        "becomes [whisper] model (and remote_model with it, in remote mode), "
        "language becomes [whisper] language — and voxtype is restarted, "
        "because it reads its config only at start-up. A save is refused "
        "outright while a recording is in flight. Listening on/off is Luna's "
        "own and needs no restart: with it off, F10 still records and the "
        "transcript goes to the clipboard instead of to her.",
        css=("rowdoc",), wrap=True)

    lines = column(SPACE["labelGap"])
    restart = act("Restart voxtype")

    def rebuild():
        child = lines.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            lines.remove(child)
            child = nxt
        for text, css in _voxtype_report(b.editor.values):
            lines.append(label(text, css=css, wrap=True))
        restart.set_sensitive(voxtype.activity() not in voxtype.BUSY)

    def do_restart(btn):
        state = voxtype.activity()          # a small local file; not a block
        if state in voxtype.BUSY:
            on_status(f"voxtype is {state} — not restarting mid-recording",
                      "error")
            return

        def done(result, error):
            if error is not None:
                on_status(f"Could not restart voxtype · {error}", "error")
            else:
                ok, detail = result
                on_status(("Restarted voxtype · " if ok else
                           "Could not restart voxtype · ") + detail,
                          "ok" if ok else "error")
            rebuild()          # the authority on restart's sensitivity, last

        _busy_click(btn, "Restarting…", voxtype.restart, done)

    restart.connect("clicked", do_restart)
    rebuild()

    p = pane(
        head("Listening", "How she hears you. Transcription runs through "
                          "voxtype's post-process hook."),
        group("Input",
              b.bound_row("listen.enabled"),
              b.bound_row("listen.provider"),
              b.bound_row("listen.model", width=240),
              b.bound_row("listen.language", width=120)),
        group("Push-to-talk", b.bound_row("listen.keybind", width=200), note),
        group("voxtype", mapping, lines,
              row("voxtype's own config",
                  "It reads the file once, at start-up.", restart,
                  control_width=200)),
    )
    p.jarvis_refresh = rebuild
    return p


def confirm_pane(b):
    tri_rows, other_rows = [], []
    for s in schema.sections_for("confirm"):
        for fld in s.fields:
            tri = isinstance(fld, schema.Tri)
            r = b.bound_row(f"{s.key}.{fld.key}", width=300 if tri else 150)
            (tri_rows if tri else other_rows).append(r)

    locked = card()
    locked.add_css_class("lockedcard")
    locked.append(section_header("Always denied · not editable"))
    lockdoc = label(
        "Not settings: no key in config.toml, no widget, no code path that "
        "writes them. They protect other running sessions and the machine's "
        "own record of itself.", css=("rowdoc",), wrap=True)
    lockdoc.set_max_width_chars(110)
    locked.append(lockdoc)
    for name, why in schema.HARD_DENIES:
        locked.append(separator())
        locked.append(locked_entry(name, why))

    return pane(
        head("Confirmations",
             "The safety model. These are not hard blocks — she asks first, "
             "then proceeds."),
        locked,
        group("Action classes", *tri_rows),
        group("Thresholds and prompting", *other_rows),
    )


def memory_pane(b, window):
    bars = column(SPACE["lg"])
    rows_holder = {}

    def rebuild():
        child = bars.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            bars.remove(child)
            child = nxt
        for u in state.tier1_usage(b.editor.values):
            item = column(SPACE["labelGap"] // 2)
            top = rowbox()
            name = label(f"{u['title']} · {u['file']}")
            name.set_hexpand(True)
            top.append(name)
            top.append(label(
                f"{u['chars']} / {u['cap']} chars · {u['entries']} entries",
                css=("value",)))
            item.append(top)
            lb = Gtk.LevelBar.new_for_interval(0.0, 1.0)
            lb.set_value(u["pct"])
            # A class, not an offset value: GtkLevelBar offsets pull in the
            # stock theme's colours, and this desktop has none.
            if u["pct"] >= 0.9:
                lb.add_css_class("hot")
            item.append(lb)
            if not u["exists"]:
                item.append(label("not written yet", css=("rowdoc",)))
            bars.append(item)
        ep = state.episodes_info()
        n = ep["count"]
        bars.append(label(
            "Episodes: " + (f"{n} rows" if n is not None else "unreadable")
            + f" · {state.human_size(ep['size'])} · {ep['path']}",
            css=("rowdoc",), wrap=True))
    rows_holder["rebuild"] = rebuild
    rebuild()

    refresh = act("Refresh")
    refresh.connect("clicked", lambda _b: rebuild())
    view = act("View memories", primary=True)
    view.connect("clicked", lambda _b: _memory_window(window))

    usage = card()
    usage.append(section_header("Live usage"))
    usage.append(bars)
    usage.append(separator())
    controls = rowbox()
    controls.set_halign(Gtk.Align.END)
    controls.append(refresh)
    controls.append(view)
    usage.append(controls)

    caps = group("Caps and decay",
                 *[b.bound_row(f"memory.{f.key}", width=140)
                   for f in schema.sections_for("memory")[0].fields])

    p = pane(
        head("Memory",
             "Tier 1 is curated identity and is always in the prompt. A write "
             "past the cap is rejected, not truncated — overflow forces "
             "consolidation instead of letting the file rot into a log."),
        usage, caps)
    p.jarvis_refresh = rebuild
    return p


def _memory_window(parent):
    win = Gtk.Window(title="Jarvis — memories")
    win.set_transient_for(parent)
    win.set_modal(True)
    win.set_default_size(720, 560)
    esc = Gtk.EventControllerKey()

    def on_key(_c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            win.close()
            return True
        return False

    esc.connect("key-pressed", on_key)
    win.add_controller(esc)
    outer = column()
    outer.add_css_class("root")
    body = column()
    body.add_css_class("pane")
    body.append(label("Memories", css=("panehead",)))
    body.append(label(
        "§-delimited tier-1 entries, exactly as lunad wrote them. Read-only: "
        "these files have consistency rules inside the daemon and are not "
        "edited from here.", css=("panedesc",), wrap=True))
    for fname, _cap, title in state.TIER1:
        entries, err = state.read_entries(fname)
        c = card()
        c.append(section_header(f"{title} · {fname}"))
        if err:
            c.append(label(err, css=("rowdoc",), wrap=True))
        elif not entries:
            c.append(label("empty", css=("rowdoc",)))
        for i, entry in enumerate(entries):
            if i:
                c.append(separator())
            r = rowbox(SPACE["md"])
            r.append(label(f"§{i + 1}", css=("rowdoc",)))
            t = label(entry, css=("mono",), wrap=True, selectable=True)
            t.set_hexpand(True)
            r.append(t)
            c.append(r)
        body.append(c)
    close = act("Close")
    close.connect("clicked", lambda _b: win.close())
    close.set_halign(Gtk.Align.END)
    body.append(close)
    outer.append(scroller(body))
    win.set_child(outer)
    win.present()
    # Otherwise focus lands on the first selectable label, which selects its
    # whole paragraph on focus-in and the window opens pre-highlighted.
    # Deferred to idle: GTK assigns the initial focus after present().
    GLib.idle_add(win.set_focus, close)


def jobs_pane(b):
    listing = column(SPACE["md"])

    def rebuild():
        child = listing.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listing.remove(child)
            child = nxt
        rows = state.recent_jobs()
        if not rows:
            listing.append(label(f"No job directories under {state.JOBS_DIR}",
                                 css=("rowdoc",), wrap=True))
            return
        for m in rows:
            r = rowbox(SPACE["lg"])
            left = column(SPACE["labelGap"] // 2)
            left.set_hexpand(True)
            task = (m.get("task") or "(no task recorded)").strip()
            if len(task) > 96:
                task = task[:93] + "..."
            left.append(label(task))
            meta = " · ".join(str(x) for x in (
                m.get("id"), m.get("to") or "worker",
                state.job_age(m),
                f"{m.get('elapsed_s')}s" if m.get("elapsed_s") else None,
            ) if x)
            left.append(label(meta, css=("rowdoc",)))
            r.append(left)
            code = m.get("exit_code")
            statetxt = m.get("state") or ("finished" if code is not None
                                          else "unknown")
            if code not in (None, 0):
                statetxt = f"{statetxt} · exit {code}"
            r.append(label(statetxt, css=("value",)))
            open_btn = act("Open")
            open_btn.connect("clicked",
                             lambda _b, d=m.get("dir"): open_path(d))
            r.append(open_btn)
            listing.append(r)
            listing.append(separator())

    rebuild()
    refresh = act("Refresh")
    refresh.connect("clicked", lambda _b: rebuild())

    recent = card()
    top = rowbox()
    h = section_header("Recent jobs")
    h.set_hexpand(True)
    top.append(h)
    top.append(refresh)
    recent.append(top)
    recent.append(separator())
    recent.append(listing)

    p = pane(
        head("Jobs",
             "Dispatched work runs in a Hyprland special workspace, one "
             "directory per job, so the list survives a daemon restart."),
        group("Dispatch",
              *[b.bound_row(f"dispatch.{f.key}",
                            width=240 if isinstance(f, schema.Text) else 140)
                for f in schema.section_for("dispatch").fields]),
        recent)
    p.jarvis_refresh = rebuild
    return p


def about_pane(b, on_start):
    lines = column(SPACE["md"])
    fetching = {"busy": False}

    def kv(k, v, css=("value",)):
        r = rowbox()
        lk = label(k)
        lk.set_hexpand(True)
        r.append(lk)
        r.append(label(v, css=css, wrap=True))
        return r

    def render(st, up, unit, error):
        child = lines.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            lines.remove(child)
            child = nxt
        d = (st or {}).get("daemon") or {}
        lines.append(kv("Daemon", "running" if up else "not running"))
        lines.append(kv("systemd unit", f"lunad · {unit}"))
        if error is not None:
            lines.append(kv("Status check", f"failed — {error}",
                            css=("locked-value",)))
        if up:
            lines.append(kv("Version", str(d.get("version") or "?")))
            lines.append(kv("Protocol", str(d.get("protocol") or "?")))
            lines.append(kv("PID", str(d.get("pid") or "?")))
            lines.append(kv("Uptime", f"{d.get('uptime_s', 0):.0f} s"))
            ag = st.get("agent") or {}
            lines.append(kv("Agent", f"{ag.get('name')} · "
                                     f"{'available' if ag.get('available') else 'missing'}"))
        lines.append(kv("Socket", str(client.SOCKET_PATH)))
        sup = b.editor.settings.supported
        lines.append(kv("settings.get / settings.set",
                        "supported — changes apply live" if sup
                        else ("not implemented by this daemon; Jarvis writes "
                              "config.toml and lunad hot-reloads it")
                        if sup is False else "not probed yet"))
        present, where = state.key_present()
        lines.append(kv("API key", ("present in " + where) if present
                        else "not found — " + where))
        lines.append(label(
            "The key itself is never read, never shown and never written to "
            "config.toml.", css=("rowdoc",), wrap=True))

    def rebuild():
        if fetching["busy"]:
            return
        fetching["busy"] = True
        orig = refresh.get_label()
        refresh.set_sensitive(False)
        refresh.set_label("Checking…")
        child = lines.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            lines.remove(child)
            child = nxt
        lines.append(label("Checking daemon status…", css=("rowdoc",)))

        def work():
            try:
                st = client.status(timeout=2.0)
                up = True
            except (client.DaemonDown, client.OpFailed):
                st, up = {}, False
            unit = client.unit_state()
            return st, up, unit

        def done(result, error):
            fetching["busy"] = False
            refresh.set_label(orig)
            refresh.set_sensitive(True)
            if error is not None:
                render(None, False, "unknown", str(error))
            else:
                st, up, unit = result
                render(st, up, unit, None)
            return False

        async_util.run_async(work, done)

    audit = rowbox()
    al = label(str(state.AUDIT_PATH))
    al.set_hexpand(True)
    audit.append(al)
    ab = act("Open audit log")
    ab.connect("clicked", lambda _b: open_path(state.AUDIT_PATH))
    audit.append(ab)
    lb = act("Open daemon log")
    lb.connect("clicked", lambda _b: open_path(state.LOG_PATH))
    audit.append(lb)

    cfg = rowbox()
    cl = label(str(config.CONFIG_PATH))
    cl.set_hexpand(True)
    cfg.append(cl)
    cb = act("Open config.toml")
    cb.connect("clicked", lambda _b: open_path(config.CONFIG_PATH))
    cfg.append(cb)

    refresh = act("Refresh")
    refresh.connect("clicked", lambda _b: rebuild())
    startb = act("Start daemon", primary=True)
    startb.connect("clicked", lambda _b: on_start())
    btns = rowbox()
    btns.set_halign(Gtk.Align.END)
    btns.append(startb)
    btns.append(refresh)

    statuscard = card()
    statuscard.append(section_header("Status"))
    statuscard.append(lines)
    statuscard.append(separator())
    statuscard.append(btns)

    unknown = card()
    unknown.append(section_header("Keys Jarvis does not understand"))
    if b.editor.unknown:
        unknown.append(label(
            "Preserved verbatim in config.toml and never rewritten:",
            css=("rowdoc",), wrap=True))
        for k, v in sorted(b.editor.unknown.items()):
            unknown.append(label(f"{k} = {v!r}", css=("mono",), wrap=True))
    else:
        unknown.append(label("None — every key in the file is in the schema.",
                             css=("rowdoc",)))

    p = pane(
        head("About and status", "What is running, and where everything is."),
        statuscard,
        group("Interface",
              *[b.bound_row(f"ui.{f.key}")
                for f in schema.section_for("ui").fields]),
        group("Audit log",
              *[b.bound_row(f"audit.{f.key}", width=140)
                for f in schema.section_for("audit").fields]),
        group("Files", audit, cfg),
        unknown)
    p.jarvis_refresh = rebuild
    rebuild()          # first fetch, now that `refresh` exists to be labelled
    return p
