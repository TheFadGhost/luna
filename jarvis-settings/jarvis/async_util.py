"""One place for "block on a worker thread, come back on the GTK main loop"
— the shape every socket/subprocess call in this app must take, per
client.py's own rule: nothing that can block may run on the GTK thread.

Used by every click handler and poll timer that ends up calling client.py,
voxtype.py or systemctl: the daemon-status probe in the About pane, the
daemon-poll timer in app.py, "Restart voxtype", and the voice preview /
live-pipeline buttons. One helper here instead of four ad-hoc
threading.Thread + GLib.idle_add pairs, so the marshalling is right in one
place rather than four.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402


def run_async(work, done):
    """Run `work()` (no arguments) on a daemon worker thread.

    `work` must never touch a GTK object — only client.py, voxtype.py,
    subprocess, or the filesystem. Its return value (or, on an exception,
    the exception itself) is marshalled back to the GTK main loop via
    `GLib.idle_add`, which calls `done(result, error)` there — exactly one
    of the two is `None`. `done` is where GTK objects are touched again.

    Nothing here waits for the thread: the call returns immediately, which
    is the whole point — the caller is expected to have already put the
    calling control into a "working" state before calling this.
    """

    def runner():
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - marshalled back, not swallowed
            GLib.idle_add(done, None, exc)
        else:
            GLib.idle_add(done, result, None)

    threading.Thread(target=runner, daemon=True).start()
