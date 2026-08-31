"""The eight panes.

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

from . import (ambient, async_util, client, config, models, schema, state,
               voices, voxtype)
from .theme import COLUMN, RHYTHM, SPACE
from .widgets import (TriToggle, act, clear, column, group, head, label,
                      locked_entry, row, rowbox, scroller, separator,
                      state_note)


def _face_factory(cap):
    """A list-item factory whose label ellipsizes at `cap` characters.

    GTK sizes a dropdown's closed face to its longest option, so one long
    voice name was reaching past the control column and dragging that row's
    control out of the line every other row keeps. The popup list gets the
    untruncated text; the closed face is a summary, and the row's own helper
    line already spells the value out.
    """
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        lb = Gtk.Label(xalign=0.0)
        if cap:
            lb.set_ellipsize(3)          # Pango.EllipsizeMode.END
            lb.set_max_width_chars(cap)
        item.set_child(lb)

    def bind(_f, item):
        item.get_child().set_text(item.get_item().get_string())

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


class Binder:
    """Builds bound controls and keeps them in step with the Editor."""

    def __init__(self, editor):
        self.editor = editor
        self._refresh = {}          # dotted -> fn(value)

    # ------------------------------------------------------------ dispatch
    def control_for(self, dotted, width=COLUMN["control"]):
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

    def bound_row(self, dotted, width=COLUMN["control"], extra=(),
                  ctl=None, trail=None):
        """One grid row for one setting.

        `extra` is a per-row action (a preview button); it goes in the
        trailing column rather than beside the control, so adding one to a
        row cannot push that row's control out of the column every other
        row shares.
        """
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
        if trail is None and extra:
            trail = extra[0] if len(extra) == 1 else rowbox(SPACE["md"])
            if len(extra) > 1:
                for e in extra:
                    trail.append(e)
        return row(fld.label, fld.doc, *parts, trail=trail)

    def refresh(self, keys=None):
        for dotted, fn in self._refresh.items():
            if keys is None or dotted in keys:
                fn(self.editor.get(dotted))

    # ------------------------------------------------------------ builders
    def _switch(self, dotted, value):
        sw = Gtk.Switch()
        sw.set_active(bool(value))
        sw.set_halign(Gtk.Align.START)
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
        dd.set_factory(_face_factory(COLUMN["face"]))
        dd.set_list_factory(_face_factory(0))
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

        def left(ctl):
            # get_widget(), never a captured `e`: a controller closure that
            # holds its own widget makes a cycle Python's GC cannot see.
            w = ctl.get_widget()
            if "bad" in w.get_css_classes():
                guard["muted"] = True
                w.set_text(str(self.editor.get(dotted) or ""))
                w.remove_css_class("bad")
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

def pane(*children):
    """A pane is one column of type on the window background.

    Spacing is zero here on purpose: each group brings its own top margin,
    so the rhythm between sections is stated once in RHYTHM rather than
    once per pane.
    """
    body = column(0)
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
    model_ctl = b.control_for(keys[3])

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
              b.bound_row(keys[3], ctl=model_ctl)),
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
    # Disabled so it cannot be fired twice, but the working label is the only
    # feedback the click gets, so it keeps full contrast instead of the
    # ordinary disabled grey.
    btn.add_css_class("busy")
    btn.set_label(working_label)

    def done(result, error):
        btn.set_label(orig)
        btn.remove_css_class("busy")
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

    prev_a = act("Play sample")
    prev_a.connect("clicked", preview("voice.voice"))
    prev_b = act("Play sample")
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

    # Everything on this pane except the switch itself only means anything
    # while speech is on. Left live it read as editable and did nothing; the
    # rows now go inert with the switch, and say so in a sentence rather than
    # leaving the reader to infer it from grey text.
    enabled_ctl = b.control_for("voice.enabled")
    off_note = label(
        "Speech is off, so nothing below is used. The settings are still "
        "saved, and take effect again the moment it is switched back on.",
        css=("rowdoc",), wrap=True, measure=COLUMN["note"])

    n = len(voices.available())
    out_rows = [b.bound_row("voice.provider"), b.bound_row("voice.model")]
    pick_rows = [b.bound_row("voice.voice", extra=(prev_a,)),
                 b.bound_row("voice.voice_male", extra=(prev_b,)),
                 row("Live check",
                     "Speaks one sentence through lunad's say op.", live)]
    deliver_rows = [b.bound_row("voice.fallback"),
                    b.bound_row("voice.piper_voice"),
                    b.bound_row("voice.speed", width=COLUMN["narrow"]),
                    b.bound_row("voice.max_spoken_chars",
                                width=COLUMN["narrow"])]
    # The rows go inert, not the groups: a dimmed section heading would say
    # the section had stopped existing, when what has stopped is the setting.
    gated = out_rows + pick_rows + deliver_rows

    def gate(sw, _p=None):
        on = sw.get_active()
        off_note.set_visible(not on)
        for w in gated:
            w.set_sensitive(on)

    output = group("Output",
                   b.bound_row("voice.enabled", ctl=enabled_ctl),
                   off_note, *out_rows)
    picks = group(f"Voice — {n} samples in ~/Music/luna-voices", *pick_rows)
    delivery = group("Fallback and delivery", *deliver_rows)
    enabled_ctl.connect("notify::active", gate)
    gate(enabled_ctl)

    return pane(
        head("Voice out",
             "How she sounds. Play sample plays the local file for the "
             "picked voice; the live check speaks through the daemon with "
             "whatever is currently saved."),
        output, picks, delivery)


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
        css=("rowdoc",), wrap=True, measure=COLUMN["note"])
    mapping = (
        "Provider, model and language are written through to "
        f"{voxtype.CONFIG_PATH} — provider becomes [whisper] mode, model "
        "becomes [whisper] model (and remote_model with it, in remote mode), "
        "language becomes [whisper] language — and voxtype is restarted, "
        "because it reads its config only at start-up. A save is refused "
        "outright while a recording is in flight. Listening on/off is Luna's "
        "own and needs no restart: with it off, F10 still records and the "
        "transcript goes to the clipboard instead of to her.")

    lines = column(SPACE["labelGap"])
    restart = act("Restart voxtype")

    def rebuild():
        clear(lines)
        for text, css in _voxtype_report(b.editor.values):
            lines.append(label(text, css=css, wrap=True,
                               measure=COLUMN["note"]))
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
              b.bound_row("listen.model"),
              b.bound_row("listen.language", width=COLUMN["narrow"])),
        group("Push-to-talk", b.bound_row("listen.keybind"), note),
        group("voxtype", lines,
              row("voxtype's own config",
                  "It reads the file once, at start-up.", restart),
              note=mapping),
    )
    p.jarvis_refresh = rebuild
    return p


def confirm_pane(b):
    """The safety surface.

    "Never ask" and "Never allow" used to sit one word apart in a row of
    three identical buttons: a permissive answer and a restrictive one drawn
    as the same object. They are three different words now — Allow, Ask
    first, Refuse — the one in force carries the weight and the rule beneath
    it, and every row states in plain language, in a column of its own, what
    the setting will actually do. The two answers that change what the
    machine does on its own say opposite things there; the safe default says
    nothing at all.
    """
    tri_rows, other_rows = [], []
    answers = {}

    # Full contrast, body size: the count of classes that run unattended is
    # the headline fact of this pane, and outranks the static legend above it.
    summary = label("", css=("rowlabel",), wrap=True, measure=COLUMN["note"])

    def restate():
        loose = sorted(k for k, v in answers.items() if v == "never")
        total = len(answers)
        if not loose:
            summary.set_text(
                f"All {total} action classes stop and ask. Nothing here runs "
                "unattended.")
        else:
            summary.set_text(
                f"{len(loose)} of {total} action classes run unattended — "
                "she does those without asking first.")

    for sec in schema.sections_for("confirm"):
        for fld in sec.fields:
            dotted = f"{sec.key}.{fld.key}"
            if not isinstance(fld, schema.Tri):
                other_rows.append(b.bound_row(dotted, width=COLUMN["narrow"]))
                continue
            ctl = b.control_for(dotted)
            note = state_note("")

            def announce(value, _note=note, _key=dotted):
                text = schema.TRI_NOTES.get(value, "")
                _note.set_text(text)
                _note.set_visible(bool(text))
                if value == "never":
                    _note.add_css_class("risk")
                else:
                    _note.remove_css_class("risk")
                answers[_key] = value
                restate()

            ctl.watch(announce)
            tri_rows.append(row(fld.label, fld.doc, ctl, trail=note))
    restate()

    return pane(
        head("Confirmations",
             "The safety model. These are not hard blocks — she asks first, "
             "then proceeds."),
        group("Action classes", summary, *tri_rows,
              note="Allow — she does it without asking. Ask first — she stops "
                   "and waits for a yes. Refuse — she will not do it at all."),
        group("Thresholds and prompting", *other_rows),
        group("Never permitted",
              *[locked_entry(name, why) for name, why in schema.HARD_DENIES],
              note="Not settings: no key in config.toml, no widget, no code "
                   "path that writes them. They protect other running "
                   "sessions and the machine's own record of itself."),
    )


def _and_list(items):
    """`a`, `a and b`, `a, b and c` — a sentence, not a comma-joined list."""
    items = list(items)
    if len(items) < 2:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def ambient_pane(b):
    """The three things she notices without being asked.

    Two of the three ship off, and this pane's job is to say *why* without
    standing in the way of turning them on. Omarchy already watches crashes
    and the battery and is better at both, so the argument sits next to the
    switch instead of in a manual, and it is sourced from lunad's own
    `desktop_already_watching()` rather than asserted here — see
    jarvis.ambient. Nothing is disabled on that account: a user who turns the
    crash hook on gets it, and gets told what it will cost them.

    Two idioms are borrowed from the Confirmations pane, because this is the
    other surface where the machine acts without being asked each time. The
    live count above the rows: the shape of what she notices on her own
    should be readable in one look, not assembled from three switches. And
    the one fact that has to carry weight — that the hook you just switched
    on duplicates a service running right now — is the only full-contrast
    thing here, the way `Allow` is the only one there.

    None of the handlers below reads the widget it is attached to; they all
    re-read the editor, which the Binder has already written by the time they
    run. That keeps every closure free of its own widget (CUSTOMISATIONS.md
    §6e) and means one function states the whole pane's dependent state,
    rather than three switches each knowing a piece of it.
    """
    ed = b.editor
    f = ambient.facts()

    # Built before the rows so the pane can listen to them as well as bind
    # them: every one of these changes something else on the pane.
    master_ctl = b.control_for("ambient.enabled")
    update_ctl = b.control_for("ambient.update")
    crash_ctl = b.control_for("ambient.crash")
    diagnose_ctl = b.control_for("ambient.crash_diagnose")
    battery_ctl = b.control_for("ambient.battery")
    low_ctl = b.control_for("ambient.battery_low_pct", COLUMN["narrow"])
    crit_ctl = b.control_for("ambient.battery_critical_pct", COLUMN["narrow"])

    # Full contrast, body size: the count of hooks that will act unprompted is
    # the headline fact of this pane, exactly as the count of unattended
    # action classes is on Confirmations.
    summary = label("", css=("rowlabel",), wrap=True, measure=COLUMN["note"])

    n_update, n_crash, n_diagnose = (state_note(""), state_note(""),
                                     state_note(""))
    n_battery, n_low, n_crit = state_note(""), state_note(""), state_note("")

    def note_line():
        return label("", css=("rowdoc",), wrap=True, measure=COLUMN["note"])

    live_update, live_crash, live_battery = (note_line(), note_line(),
                                             note_line())

    r_enabled = b.bound_row("ambient.enabled", ctl=master_ctl)
    r_poll = b.bound_row("ambient.poll_seconds", width=COLUMN["narrow"])
    r_update = b.bound_row("ambient.update", ctl=update_ctl, trail=n_update)
    r_crash = b.bound_row("ambient.crash", ctl=crash_ctl, trail=n_crash)
    r_diagnose = b.bound_row("ambient.crash_diagnose", ctl=diagnose_ctl,
                             trail=n_diagnose)
    r_battery = b.bound_row("ambient.battery", ctl=battery_ctl,
                            trail=n_battery)
    r_low = b.bound_row("ambient.battery_low_pct", width=COLUMN["narrow"],
                        ctl=low_ctl, trail=n_low)
    r_crit = b.bound_row("ambient.battery_critical_pct",
                         width=COLUMN["narrow"], ctl=crit_ctl, trail=n_crit)

    # A dependent row that is inert has to look inert *and say so*, the way
    # the Voice pane does when speech is off. Grey rows on their own read as
    # broken; a sentence reads as a consequence.
    off_all = note_line()
    off_all.set_text(
        "Noticing is off, so none of the three hooks below runs. The thread "
        "still ticks, so switching it back on takes effect on the next tick "
        "and needs no restart. Everything below is still saved.")
    off_crash = note_line()
    off_crash.set_text(
        "There is no toast to put an action on while crashes are not "
        "watched. The setting is still saved.")
    off_battery = note_line()
    off_battery.set_text(
        "Neither threshold is read while the battery is not watched. Both "
        "are still saved.")

    def mark(note, text, risk=False):
        note.set_text(text)
        note.set_visible(bool(text))
        if risk:
            note.add_css_class("risk")
        else:
            note.remove_css_class("risk")

    def restate(*_a):
        on = bool(ed.get("ambient.enabled"))
        upd = bool(ed.get("ambient.update"))
        crash = bool(ed.get("ambient.crash"))
        diag = bool(ed.get("ambient.crash_diagnose"))
        batt = bool(ed.get("ambient.battery"))
        try:
            low = int(ed.get("ambient.battery_low_pct") or 0)
            crit = int(ed.get("ambient.battery_critical_pct") or 0)
        except (TypeError, ValueError):
            low, crit = 0, 0
        duplicated = ambient.duplicated_hooks(ed.values)

        # The master gate first, then the dependents inside it: a threshold
        # under a hook that is on, under a master that is off, is still inert.
        off_all.set_visible(not on)
        for w in (r_poll, r_update, r_crash, r_diagnose, r_battery, r_low,
                  r_crit):
            w.set_sensitive(on)
        off_crash.set_visible(on and not crash)
        r_diagnose.set_sensitive(on and crash)
        off_battery.set_visible(on and not batt)
        for w in (r_low, r_crit):
            w.set_sensitive(on and batt)

        live = [name for name, flag in (("an update landing", upd),
                                        ("crashes", crash),
                                        ("the battery", batt)) if flag]
        if not on:
            summary.set_text(
                "Noticing is off. She notices nothing on her own, whatever "
                "the three hooks below say.")
        elif not live:
            summary.set_text(
                "All three hooks are off. She notices nothing on her own.")
        else:
            summary.set_text(
                f"{len(live)} of 3 hooks on — she notices {_and_list(live)}.")

        # A trailing note says what a setting is *currently doing*, so a row
        # that is inert says nothing at all: "watching" under a master switch
        # that is off would be false, and dimming it does not make it true.
        # The sentence above each inert block is what explains the silence.
        dup = "crash" in duplicated
        mark(n_update, ("watching" if upd else "off") if on else "")
        mark(n_crash, ("duplicated" if dup else
                       ("watching" if crash else "off")) if on else "",
             risk=dup)
        mark(n_diagnose, ("one click away" if diag else "no action")
             if on and crash else "")
        mark(n_battery, ("watching" if batt else "off") if on else "")
        if not (on and batt):
            mark(n_low, "")
            mark(n_crit, "")
        else:
            # Silence means the value sits where it was meant to. A note
            # appears only where a threshold has stopped doing its job.
            mark(n_low, "" if low > 10 else
                 ("with Omarchy's" if low == 10 else "after Omarchy's"))
            if crit > low:
                # The daemon clamps this rather than refusing it, so the clamp
                # is silent unless something here says it out loud.
                mark(n_crit, f"clamped to {low}%", risk=True)
            else:
                mark(n_crit, "under UPower's" if crit <= 2 else "")

        # --- what she would actually be reading, right now -----------------
        if f["version"]:
            live_update.set_text(
                f"{f['version_file']} reads {f['version']}, written "
                f"{f['version_written']}. Both the text and that date are "
                "compared, every 300 seconds.")
        elif f["available"]:
            live_update.set_text(
                "The version file could not be read, so this hook has nothing "
                "to compare — the daemon treats that as “not an Omarchy "
                "machine” rather than as an error.")
        else:
            live_update.set_text(
                "Jarvis could not ask lunad which file this watches.")

        unit = f["crash_unit"]
        if f["crash_desktop"] and on and crash:
            live_crash.set_text(
                f"{unit} is running right now, so this gives you two toasts "
                "for one crash — hers as well as the desktop's. Nothing is "
                "blocked: `omarchy-toggle-crash-capture` turns the desktop's "
                "off if you would rather have only hers.")
            live_crash.add_css_class("locked-value")
            live_crash.remove_css_class("rowdoc")
        else:
            live_crash.add_css_class("rowdoc")
            live_crash.remove_css_class("locked-value")
            if f["crash_desktop"]:
                live_crash.set_text(
                    f"{unit} is running right now, which is why this ships "
                    "off.")
            elif f["crash_desktop"] is False:
                live_crash.set_text(
                    f"{unit} is not active here, so nothing else on this "
                    "machine announces a crash.")
            else:
                live_crash.set_text(
                    f"Jarvis could not ask lunad whether {unit} is live, so "
                    "it is not saying either way.")

        if f["battery_device"]:
            reading = (f"{f['battery_pct']}%, {f['battery_status'].lower()}"
                       if f["battery_pct"] is not None else "no reading")
            live_battery.set_text(
                f"{f['battery_device']} reads {reading}. The device is found "
                "by asking each one its type, never by assuming BAT0.")
        elif f["available"]:
            live_battery.set_text(
                "No battery device was found, so this hook has nothing to "
                "read.")
        else:
            live_battery.set_text(
                "Jarvis could not ask lunad which battery this watches.")

    def rebuild():
        """Re-read the machine, then restate. Runs on every visit to the pane
        — the battery percentage is the one fact here that moves."""
        nonlocal f
        f = ambient.facts()
        restate()

    for ctl in (master_ctl, update_ctl, crash_ctl, diagnose_ctl, battery_ctl):
        ctl.connect("notify::active", restate)
    for ctl in (low_ctl, crit_ctl):
        ctl.connect("value-changed", restate)
    restate()

    p = pane(
        head("Ambient",
             "The three things she notices without being asked. Two of them "
             "are off, because the desktop already does the job — each one "
             "says so, and says whose it is."),
        group("Noticing", summary, r_enabled, off_all, r_poll,
              note="An ambient event notifies; it never speaks aloud. That is "
                   "not a setting and there is no key for it: the daemon's "
                   "ambient module never imports the speech code, refuses any "
                   "delivery channel that is not a notifier, and refuses at "
                   "construction any collaborator that has a .say() at all — "
                   "and a test walks the live object graph to prove no "
                   "speaker is reachable from it. Speaking aloud stays "
                   "reserved for a job you started."),
        group("Updates", r_update, live_update,
              note="`omarchy update` rewrites all of /usr/share/omarchy, "
                   "which is exactly how a desktop customisation gets "
                   "silently reverted — so this is the one hook nothing else "
                   "on the machine covers. She reads both the version "
                   "file's contents and its mtime, because a same-version "
                   "reinstall clobbers just as thoroughly, plus "
                   "/tmp/omarchy-update.log. Whether an update is available "
                   "is deliberately not checked: that costs a network sync, "
                   "and Omarchy's own bar widget already polls it every six "
                   "hours and shows the answer."),
        group("Crashes", r_crash, live_crash, off_crash, r_diagnose,
              note="Omarchy ships omarchy-crash-watch.service, enabled, "
                   "event-driven off the journal, and strictly better at this "
                   "job: it knows the signal name and the full executable "
                   "path, which a core file's name does not. Hers exists for "
                   "the two things that one cannot do — put the crash in her "
                   "audit log and the diagnosis in her job list, under her "
                   "confirmation policy — and for anyone who has turned the "
                   "desktop's off."),
        group("Battery", r_battery, live_battery, off_battery,
              r_low, r_crit,
              note="Omarchy's own battery service polls every 30 seconds and "
                   "fires omarchy-battery-low at 10%, and UPower hibernates "
                   "at 2%. Hers sit either side of that rather than on top of "
                   "it: one warning earlier than the desktop's, and one last "
                   "one after it, before the machine acts on its own."))
    p.jarvis_refresh = rebuild
    return p


def memory_pane(b, window):
    bars = column(SPACE["lg"])

    def rebuild():
        clear(bars)
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
            css=("rowdoc",), wrap=True, measure=COLUMN["note"]))
    rebuild()

    refresh = act("Refresh")
    refresh.connect("clicked", lambda _b: rebuild())
    view = act("View memories", primary=True)
    view.connect("clicked", lambda _b: _memory_window(window))

    controls = rowbox()
    controls.set_halign(Gtk.Align.END)
    controls.append(refresh)
    controls.append(view)

    p = pane(
        head("Memory",
             "Tier 1 is curated identity and is always in the prompt. A write "
             "past the cap is rejected, not truncated — overflow forces "
             "consolidation instead of letting the file rot into a log."),
        group("Live usage", bars, controls),
        group("Caps and decay",
              *[b.bound_row(f"memory.{f.key}", width=COLUMN["narrow"])
                for f in schema.sections_for("memory")[0].fields]),
    )
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
    body = column(0)
    body.add_css_class("pane")
    body.append(head("Memories",
                     "§-delimited tier-1 entries, exactly as lunad wrote "
                     "them. Read-only: these files have consistency rules "
                     "inside the daemon and are not edited from here."))
    for fname, _cap, title in state.TIER1:
        entries, err = state.read_entries(fname)
        items = []
        if err:
            items.append(label(err, css=("rowdoc",), wrap=True,
                               measure=COLUMN["note"]))
        elif not entries:
            items.append(label("empty", css=("rowdoc",)))
        for i, entry in enumerate(entries):
            r = rowbox(SPACE["md"])
            r.append(label(f"§{i + 1}", css=("rowdoc",)))
            t = label(entry, css=("mono",), wrap=True, selectable=True,
                      measure=COLUMN["note"])
            t.set_hexpand(True)
            r.append(t)
            items.append(r)
        body.append(group(f"{title} · {fname}", *items))
    close = act("Close")
    close.connect("clicked", lambda _b: win.close())
    close.set_halign(Gtk.Align.END)
    close.set_margin_top(RHYTHM["section"])
    body.append(close)
    outer.append(scroller(body))
    win.set_child(outer)
    win.present()
    # Otherwise focus lands on the first selectable label, which selects its
    # whole paragraph on focus-in and the window opens pre-highlighted.
    # Deferred to idle: GTK assigns the initial focus after present().
    GLib.idle_add(win.set_focus, close)


def jobs_pane(b):
    listing = column(0)

    def rebuild():
        clear(listing)
        rows = state.recent_jobs()
        if not rows:
            listing.append(label(f"No job directories under {state.JOBS_DIR}",
                                 css=("rowdoc",), wrap=True,
                                 measure=COLUMN["note"]))
            return
        for i, m in enumerate(rows):
            if i:
                sep = separator()
                sep.set_margin_top(RHYTHM["row"])
                listing.append(sep)
            left = column(SPACE["labelGap"] // 2)
            left.set_hexpand(True)
            task = (m.get("task") or "(no task recorded)").strip()
            if len(task) > 96:
                task = task[:93] + "..."
            left.append(label(task, wrap=True))
            meta = " · ".join(str(x) for x in (
                m.get("id"), m.get("to") or "worker",
                state.job_age(m),
                f"{m.get('elapsed_s')}s" if m.get("elapsed_s") else None,
            ) if x)
            left.append(label(meta, css=("rowdoc",)))
            code = m.get("exit_code")
            statetxt = m.get("state") or ("finished" if code is not None
                                          else "unknown")
            if code not in (None, 0):
                statetxt = f"{statetxt} · exit {code}"
            open_btn = act("Open")
            open_btn.connect("clicked",
                             lambda _b, d=m.get("dir"): open_path(d))
            # Built from the same three columns as a settings row, so the
            # job list scans down the same two edges as every other pane.
            r = rowbox(SPACE["panelPadding"])
            r.set_margin_top(RHYTHM["row"])
            r.append(left)
            holder = rowbox(SPACE["md"])
            holder.set_valign(Gtk.Align.CENTER)
            holder.set_size_request(COLUMN["control"], -1)
            holder.append(label(statetxt, css=("value",)))
            r.append(holder)
            tail = rowbox(SPACE["md"])
            tail.set_valign(Gtk.Align.CENTER)
            tail.set_size_request(COLUMN["trail"], -1)
            tail.append(open_btn)
            r.append(tail)
            listing.append(r)

    rebuild()
    refresh = act("Refresh")
    refresh.connect("clicked", lambda _b: rebuild())

    p = pane(
        head("Jobs",
             "Dispatched work runs in a Hyprland special workspace, one "
             "directory per job, so the list survives a daemon restart."),
        group("Dispatch",
              *[b.bound_row(f"dispatch.{f.key}",
                            width=COLUMN["control"]
                            if isinstance(f, schema.Text)
                            else COLUMN["narrow"])
                for f in schema.section_for("dispatch").fields]),
        group("Recent jobs", listing, action=refresh))
    p.jarvis_refresh = rebuild
    return p


def about_pane(b, on_start):
    lines = column(SPACE["md"])
    fetching = {"busy": False}

    def kv(k, v, css=("value",)):
        return row(k, "", label(v, css=css, wrap=True, pin=True,
                                measure=COLUMN["value"]))

    def render(st, up, unit, error):
        clear(lines)
        d = (st or {}).get("daemon") or {}
        # The one line on this pane that has to be readable from across the
        # room, and the reason the Start button below it appears at all.
        lines.append(kv("Daemon", "running" if up else "not running",
                        css=("value",) if up else ("locked-value",)))
        startb.set_visible(not up)
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
        # The value stays one word, and the location moves to the note under
        # it: a path is longer than the column every other value shares, and
        # one long read-back was pulling this row's grid out of line.
        lines.append(kv("API key", "present" if present else "not found",
                        css=("value",) if present else ("locked-value",)))
        lines.append(label(
            f"{where}. The key itself is never read, never shown and never "
            "written to config.toml.", css=("rowdoc",), wrap=True,
            measure=COLUMN["note"]))

    def rebuild():
        if fetching["busy"]:
            return
        fetching["busy"] = True
        orig = refresh.get_label()
        refresh.set_sensitive(False)
        refresh.add_css_class("busy")
        refresh.set_label("Checking…")
        clear(lines)
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
            refresh.remove_css_class("busy")
            refresh.set_sensitive(True)
            if error is not None:
                render(None, False, "unknown", str(error))
            else:
                st, up, unit = result
                render(st, up, unit, None)
            return False

        async_util.run_async(work, done)

    ab = act("Open")
    ab.connect("clicked", lambda _b: open_path(state.AUDIT_PATH))
    lb = act("Open")
    lb.connect("clicked", lambda _b: open_path(state.LOG_PATH))
    cb = act("Open")
    cb.connect("clicked", lambda _b: open_path(config.CONFIG_PATH))

    refresh = act("Refresh")
    refresh.connect("clicked", lambda _b: rebuild())
    startb = act("Start daemon", primary=True)
    startb.connect("clicked", lambda _b: on_start())
    # Hidden while lunad is up: a Start button that looks the same running or
    # stopped tells the reader nothing, and its absence is the clearest way
    # to say there is nothing to start.
    startb.set_visible(False)
    startb.set_halign(Gtk.Align.END)

    if b.editor.unknown:
        unknown_children = [label(
            "Preserved verbatim in config.toml and never rewritten:",
            css=("rowdoc",), wrap=True, measure=COLUMN["note"])]
        unknown_children += [
            label(f"{k} = {v!r}", css=("mono",), wrap=True,
                  measure=COLUMN["note"])
            for k, v in sorted(b.editor.unknown.items())]
    else:
        unknown_children = [label(
            "None — every key in the file is in the schema.", css=("rowdoc",))]

    p = pane(
        head("About and status", "What is running, and where everything is."),
        group("Status", lines, startb, action=refresh),
        group("Interface",
              *[b.bound_row(f"ui.{f.key}")
                for f in schema.section_for("ui").fields]),
        group("Audit log",
              *[b.bound_row(f"audit.{f.key}", width=COLUMN["narrow"])
                for f in schema.section_for("audit").fields]),
        group("Files",
              row("Audit log", str(state.AUDIT_PATH), ab),
              row("Daemon log", str(state.LOG_PATH), lb),
              row("Config file", str(config.CONFIG_PATH), cb)),
        group("Keys Jarvis does not understand", *unknown_children))
    p.jarvis_refresh = rebuild
    rebuild()          # first fetch, now that `refresh` exists to be labelled
    return p
