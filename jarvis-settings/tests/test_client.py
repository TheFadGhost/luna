"""The socket client, against a real Unix socket.

The interesting case is the one that is true right now: a daemon that does
NOT implement settings.get / settings.set. Jarvis has to notice, cache it,
and stop asking — without ever mistaking it for the daemon being down.
"""

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import client  # noqa: E402


class FakeDaemon:
    """One-line-per-request NDJSON server, exactly like lunad's."""

    def __init__(self, handler):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / "luna.sock"
        self.handler = handler
        self.requests = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(self.path))
        self.srv.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.stopping = False
        self.thread.start()

    def _serve(self):
        while not self.stopping:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    continue
                req = json.loads(buf.split(b"\n", 1)[0])
                self.requests.append(req)
                conn.sendall((json.dumps(self.handler(req)) + "\n").encode())
            except Exception:
                pass
            finally:
                conn.close()

    def close(self):
        self.stopping = True
        try:
            self.srv.close()
        except OSError:
            pass
        self.dir.cleanup()


def unknown_op(req):
    return {"ok": False, "error": "UnknownOp",
            "message": f"unknown op {req['op']!r}"}


class CallTest(unittest.TestCase):
    def test_ok_reply(self):
        d = FakeDaemon(lambda r: {"ok": True, "pong": True})
        self.addCleanup(d.close)
        self.assertTrue(client.call("ping", path=d.path)["pong"])

    def test_failed_op_raises_with_the_error_name(self):
        d = FakeDaemon(unknown_op)
        self.addCleanup(d.close)
        with self.assertRaises(client.OpFailed) as cm:
            client.call("settings.get", path=d.path)
        self.assertTrue(cm.exception.unknown_op)

    def test_absent_socket_is_daemon_down(self):
        with self.assertRaises(client.DaemonDown):
            client.call("ping", path=pathlib.Path("/nonexistent/luna.sock"),
                        timeout=0.5)

    def test_alive_is_false_when_down(self):
        saved = client.SOCKET_PATH
        client.SOCKET_PATH = pathlib.Path("/nonexistent/luna.sock")
        try:
            self.assertFalse(client.alive(timeout=0.3))
        finally:
            client.SOCKET_PATH = saved


class SocketOverrideTest(unittest.TestCase):
    def test_jarvis_socket_env_wins(self):
        os.environ["JARVIS_SOCKET"] = "/tmp/somewhere/luna.sock"
        try:
            self.assertEqual(str(client._default_socket()),
                             "/tmp/somewhere/luna.sock")
        finally:
            del os.environ["JARVIS_SOCKET"]

    def test_default_is_under_xdg_runtime_dir(self):
        os.environ.pop("JARVIS_SOCKET", None)
        self.assertTrue(str(client._default_socket()).endswith(
            "/luna/luna.sock"))


class SettingsCapabilityTest(unittest.TestCase):
    def _point_at(self, daemon):
        saved = client.SOCKET_PATH
        client.SOCKET_PATH = daemon.path
        self.addCleanup(lambda: setattr(client, "SOCKET_PATH", saved))

    def test_unknown_op_is_detected_once_and_cached(self):
        d = FakeDaemon(unknown_op)
        self.addCleanup(d.close)
        self._point_at(d)
        s = client.Settings()
        self.assertIsNone(s.get())
        self.assertIs(s.supported, False)
        self.assertFalse(s.set({"assistant.name": "Ada"}))
        # `set` must not have gone back to the socket after the probe failed.
        ops = [r["op"] for r in d.requests]
        self.assertEqual(ops, ["settings.get"])

    def test_supported_daemon_applies_live(self):
        def handler(req):
            if req["op"] == "settings.get":
                return {"ok": True, "settings": {"assistant": {"name": "Ada"},
                                                 "voice": {"speed": 1.5}}}
            if req["op"] == "settings.set":
                return {"ok": True, "applied": list(req.get("changes") or {})}
            return unknown_op(req)

        d = FakeDaemon(handler)
        self.addCleanup(d.close)
        self._point_at(d)
        s = client.Settings()
        self.assertEqual(s.get(),
                         {"assistant.name": "Ada", "voice.speed": 1.5})
        self.assertTrue(s.set({"voice.speed": 1.2}))
        self.assertIs(s.supported, True)

    def test_daemon_down_is_not_mistaken_for_unsupported(self):
        saved = client.SOCKET_PATH
        client.SOCKET_PATH = pathlib.Path("/nonexistent/luna.sock")
        self.addCleanup(lambda: setattr(client, "SOCKET_PATH", saved))
        s = client.Settings()
        self.assertIsNone(s.get())
        self.assertIsNone(s.supported)      # unknown stays unknown


if __name__ == "__main__":
    unittest.main()
