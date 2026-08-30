# Handoff to whoever owns lunad/server.py

Written by the pass that fixed the concurrency/resource-leak audit items in
session.py, speech.py, dispatch.py, safety.py, protocol.py and confirm.py.
One item needs a one-line change in server.py, which was off-limits for that
pass. Everything else in the audit was fixed without touching server.py.

## Unbounded `readline()` in `_Handler.handle()` (server.py ~line 1008)

`lunad/protocol.py` now has `protocol.read_line(rfile, max_bytes=...)`, a
drop-in replacement for `rfile.readline()` that enforces `MAX_LINE_BYTES`
*while reading* instead of after the whole (potentially huge) line has
already been buffered. It raises `protocol.ProtocolError` if the line would
exceed the limit, exactly like `protocol.decode()` already does for a line
that was fully buffered.

The call site is in `_Handler.handle()`:

```python
try:
    line = self.rfile.readline()
except (TimeoutError, socket.timeout):
    ...
```

The requested change is:

```python
try:
    line = protocol.read_line(self.rfile)
except (TimeoutError, socket.timeout):
    log.info("client idle timeout", extra={"peer": peer})
    return
except OSError as exc:
    if exc.errno not in (errno.ECONNRESET, errno.EPIPE):
        log.warning("read error", extra={"detail": str(exc)})
    return
except protocol.ProtocolError as exc:
    self.wfile.write(protocol.encode(protocol.err(None, "ProtocolError", str(exc))))
    self.wfile.flush()
    return
if not line:
    return
```

i.e. add one more `except protocol.ProtocolError` arm around the existing
`readline()` try block (the socket-level `except OSError` below it must stay
last, since `ProtocolError` is a plain `Exception`, not an `OSError`, so
ordering between them doesn't matter — but `ProtocolError` has to be caught
here rather than falling through to the general `except protocol.ProtocolError`
further down in `handle()`, which only wraps `protocol.decode(line)`, not the
read itself).

One behavioural note: because `read_line` gives up mid-stream rather than
after buffering the whole (oversized) line, whatever the client sent past the
cutoff is still sitting unread in the socket buffer. The existing per-request
loop will try to read it as the *next* line on the next iteration, which most
likely fails validation again (or splits garbage across requests) rather than
resyncing cleanly. If that matters, the response to a `ProtocolError` here
should probably close the connection (`return` after writing, which the
snippet above already does) rather than looping — which is what the snippet
does. No further action needed beyond the return already shown.

This is low severity (unix socket, mode 0600, local-only), so it is fine to
pick this up whenever server.py is next touched rather than urgently.
