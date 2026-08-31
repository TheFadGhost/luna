"""Wire protocol helpers.

One request per line, one response per line, both newline-delimited JSON on
``$XDG_RUNTIME_DIR/luna/luna.sock``. Requests carry ``op`` and an optional
``id``; responses always echo the ``id`` and always carry ``ok``.

Keeping this in its own module means the CLI, the future palette and the
future bar widget agree on the shape without importing the daemon.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1_048_576  # a prompt, not a payload; anything bigger is a bug


class ProtocolError(Exception):
    """Malformed request. Reported to the client, never fatal to the daemon."""


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def read_line(rfile: Any, max_bytes: int = MAX_LINE_BYTES) -> bytes:
    """Read one newline-terminated line, bounded *while reading*.

    ``rfile.readline()`` with no size argument buffers the whole line before
    handing it back, so a client that sends ``max_bytes`` of data with no
    newline in sight grows the daemon's memory by exactly that much before
    ``decode`` ever gets a chance to reject it on length. Each ``readline()``
    call here is itself capped, so the read is abandoned as soon as the total
    crosses ``max_bytes`` rather than after the whole (unbounded) line has
    already been buffered.

    Low severity in this daemon specifically — the socket is a 0600 unix
    socket, local-only — but it is a real amplification and it costs nothing
    to close.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        # +1 so a line landing on exactly max_bytes still reads its own
        # newline in the same call, rather than needing one more empty read
        # to discover there is nothing left.
        chunk = rfile.readline(max_bytes - total + 1)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if chunk.endswith(b"\n"):
            break
        if total > max_bytes:
            raise ProtocolError(f"request exceeds {max_bytes} bytes")
    return b"".join(chunks)


def decode(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError(f"request exceeds {MAX_LINE_BYTES} bytes")
    try:
        obj = json.loads(line.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"request is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"request is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"request must be a JSON object, got {type(obj).__name__}")
    if not isinstance(obj.get("op"), str):
        raise ProtocolError("request is missing a string 'op' field")
    return obj


def ok(request_id: str | None = None, **payload: Any) -> dict[str, Any]:
    return {"ok": True, "id": request_id, **payload}


def err(
    request_id: str | None = None,
    error: str = "Error",
    message: str = "",
    **payload: Any,
) -> dict[str, Any]:
    return {"ok": False, "id": request_id, "error": error,
            "message": message, **payload}
