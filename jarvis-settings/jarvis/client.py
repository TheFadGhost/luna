"""Client for lunad's Unix socket at $XDG_RUNTIME_DIR/luna/luna.sock.

Newline-delimited JSON: one request object per line, one reply object per line.

Two things shape this module:

  * **The daemon may not have `settings.get` / `settings.set` yet.** They are
    part of the same contract as CONFIG-SCHEMA.md and are being built in
    parallel. An unknown op comes back as a normal reply
    (`{"ok": false, "error": "UnknownOp"}`), not as a connection failure, so
    the client detects that once, caches it, and the caller falls back to
    writing config.toml directly. Jarvis works whether or not the ops land.
  * **Nothing here may block the GTK main loop.** Every call takes a short
    timeout and the app runs them on a worker thread, hopping back with
    GLib.idle_add.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

def _default_socket() -> Path:
    # JARVIS_SOCKET points Jarvis at a different daemon (or, with a path that
    # does not exist, at none) without touching the running one. It is how the
    # daemon-down banner is exercised without stopping lunad.
    override = os.environ.get("JARVIS_SOCKET")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "luna" / "luna.sock"


SOCKET_PATH = _default_socket()

UNIT = "lunad"
DEFAULT_TIMEOUT = 4.0


class DaemonDown(Exception):
    """The socket is absent, or refused, or went away mid-request."""


class OpFailed(Exception):
    """The daemon answered `ok: false`."""

    def __init__(self, error: str, message: str = ""):
        super().__init__(f"{error}: {message}" if message else error)
        self.error = error
        self.message = message

    @property
    def unknown_op(self) -> bool:
        return self.error == "UnknownOp"


def call(op: str, timeout: float = DEFAULT_TIMEOUT, path: Path | None = None,
         **params) -> dict:
    """One request, one reply. Raises DaemonDown or OpFailed."""
    sock_path = str(path or SOCKET_PATH)
    req = {"op": op, **params}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(sock_path)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        except (FileNotFoundError, ConnectionRefusedError, PermissionError,
                socket.timeout, OSError) as exc:
            raise DaemonDown(str(exc)) from exc
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = s.recv(65536)
            except (socket.timeout, OSError) as exc:
                raise DaemonDown(str(exc)) from exc
            if not chunk:
                raise DaemonDown("daemon closed the connection")
            buf += chunk
            if len(buf) > 8 << 20:
                raise DaemonDown("reply too large")
    finally:
        try:
            s.close()
        except OSError:
            pass
    line = buf.split(b"\n", 1)[0]
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonDown(f"unparsable reply: {exc}") from exc
    if not isinstance(reply, dict):
        raise DaemonDown("reply was not an object")
    if not reply.get("ok"):
        raise OpFailed(str(reply.get("error") or "Error"),
                       str(reply.get("message") or ""))
    return reply


def alive(timeout: float = 1.0) -> bool:
    try:
        return bool(call("ping", timeout=timeout).get("pong"))
    except (DaemonDown, OpFailed):
        return False


def status(timeout: float = DEFAULT_TIMEOUT) -> dict:
    return call("status", timeout=timeout)


def jobs(timeout: float = DEFAULT_TIMEOUT) -> dict:
    return call("jobs", timeout=timeout)


def say(text: str, timeout: float = DEFAULT_TIMEOUT, **params) -> dict:
    return call("say", timeout=timeout, text=text, **params)


def start_daemon() -> tuple[bool, str]:
    """`systemctl --user start lunad`. The one process Jarvis is allowed to
    start; it never stops, restarts or signals anything else."""
    try:
        r = subprocess.run(["systemctl", "--user", "start", UNIT],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if r.returncode == 0:
        return True, "systemctl --user start lunad"
    return False, (r.stderr or r.stdout or f"exit {r.returncode}").strip()


def unit_state() -> str:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", UNIT],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr).strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


# ------------------------------------------------------- settings.get / set

class Settings:
    """settings.get / settings.set with a cached capability probe.

    `supported` is None until the first attempt: unknown means unknown, and
    the About pane says so rather than guessing.
    """

    def __init__(self):
        self.supported: bool | None = None
        self.last_error: str = ""

    def get(self) -> dict | None:
        """Flat {dotted: value} from the daemon, or None if it cannot serve
        it (down, or op not implemented)."""
        try:
            reply = call("settings.get")
        except OpFailed as exc:
            if exc.unknown_op:
                self.supported = False
                self.last_error = "daemon has no settings.get op"
            else:
                self.last_error = str(exc)
            return None
        except DaemonDown as exc:
            self.last_error = str(exc)
            return None
        self.supported = True
        self.last_error = ""
        return _flatten(reply.get("settings") or reply.get("config") or {})

    def set(self, changes: dict) -> bool:
        """Push changes live. False means 'not applied by the daemon' — the
        caller then writes the file, which the daemon hot-reloads anyway."""
        if self.supported is False:
            return False
        try:
            call("settings.set", changes=changes, settings=changes)
        except OpFailed as exc:
            if exc.unknown_op:
                self.supported = False
                self.last_error = "daemon has no settings.set op"
            else:
                self.last_error = str(exc)
            return False
        except DaemonDown as exc:
            self.last_error = str(exc)
            return False
        self.supported = True
        self.last_error = ""
        return True


def _flatten(node, prefix: str = "") -> dict:
    out: dict = {}
    if not isinstance(node, dict):
        return out
    for k, v in node.items():
        dotted = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, dotted))
        else:
            out[dotted] = v
    return out
