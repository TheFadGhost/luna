"""The binding layer: validate, apply live, persist, report.

Every control in the GUI calls `Editor.set(dotted, value)` and nothing else.
What happens next:

  1. **Validate.** `config.coerce` raises on anything out of contract. An
     invalid value never reaches disk and never reaches the daemon; the row
     marks itself and the status line says why.
  2. **Coalesce.** Changes are queued and flushed on a short debounce, so
     dragging a slider is one write, not forty.
  3. **Apply live if we can.** `settings.set` over the socket, so the change
     takes effect without waiting for a file watch. If the daemon is down, or
     has not implemented the op, this step is skipped — it is an optimisation,
     never the system of record.
  4. **Persist.** The file is always written. lunad hot-reloads it either way,
     so the file is the contract and the socket is the fast path.

The flush runs on a worker thread (a socket call may block for seconds) and
comes back to GTK with GLib.idle_add.
"""

from __future__ import annotations

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import client, config
from .tomledit import TomlEditError

DEBOUNCE_MS = 350
SELF_WRITE_GRACE_S = 1.5    # ignore our own file events for this long


class Editor:
    def __init__(self, on_status=None, on_external_reload=None):
        self.values, self.unknown, self.warnings = config.load()
        self.settings = client.Settings()
        self.on_status = on_status or (lambda text, kind: None)
        self.on_external_reload = on_external_reload or (lambda keys: None)
        self._pending: dict = {}
        self._timer = 0
        self._last_self_write = 0.0
        self._monitor = None
        self._reload_timer = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reading
    def get(self, dotted):
        return self.values.get(dotted)

    # ------------------------------------------------------------- writing
    def set(self, dotted, value) -> bool:
        """Queue a change. Returns False (and reports) if it is invalid."""
        try:
            clean = config.coerce(dotted, value)
        except config.ValidationError as exc:
            self.on_status(f"{dotted}: {exc.message}", "error")
            return False
        if self.values.get(dotted) == clean and dotted not in self._pending:
            return True
        with self._lock:
            self._pending[dotted] = clean
        self.values[dotted] = clean
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(DEBOUNCE_MS, self._flush)
        return True

    def flush_now(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._flush()

    def _flush(self):
        self._timer = 0
        with self._lock:
            batch, self._pending = dict(self._pending), {}
        if not batch:
            return False
        self._last_self_write = time.time()
        threading.Thread(target=self._worker, args=(batch,),
                         daemon=True).start()
        return False

    def _worker(self, batch):
        live = self.settings.set(batch)
        try:
            written = config.save(batch, all_values=self.values)
            err = None
        except (config.ValidationError, TomlEditError, OSError) as exc:
            written, err = [], exc
        self._last_self_write = time.time()
        GLib.idle_add(self._done, batch, live, written, err)

    def _done(self, batch, live, written, err):
        if err is not None:
            self.on_status(f"NOT saved — {err}", "error")
            # The in-memory value is now a lie; put the file's truth back.
            self.values, self.unknown, self.warnings = config.load()
            self.on_external_reload(sorted(batch))
            return False
        names = ", ".join(k.rpartition(".")[2] for k in written)
        route = ("applied live via the socket and written to config.toml"
                 if live else "written to config.toml (daemon reloads it)")
        self.on_status(f"Saved {names} — {route}", "ok")
        return False

    # ------------------------------------------------------------- watching
    def watch(self):
        """Reload if something else edits config.toml (a hand edit, or the
        daemon). Our own writes are ignored for a grace period so the app does
        not fight itself."""
        try:
            gfile = Gio.File.new_for_path(str(config.CONFIG_PATH))
            self._monitor = gfile.monitor_file(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.set_rate_limit(200)
            self._monitor.connect("changed", self._on_file_event)
        except GLib.Error:
            self._monitor = None

    def _on_file_event(self, *_a):
        if time.time() - self._last_self_write < SELF_WRITE_GRACE_S:
            return
        if self._reload_timer:
            return
        self._reload_timer = GLib.timeout_add(150, self._reload)

    def _reload(self):
        self._reload_timer = 0
        old = self.values
        self.values, self.unknown, self.warnings = config.load()
        changed = config.diff(old, self.values)
        if changed:
            self.on_status("Reloaded — config.toml changed on disk", "ok")
            self.on_external_reload(changed)
        return False
