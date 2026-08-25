"""The daemon: socket, dispatch, lifecycle.

ARCHITECTURE.md section 3 — a single Unix socket, every surface is a client,
and the daemon is the only thing that touches memory or spawns agents.

Threaded rather than async on purpose: the only slow operation is a blocking
subprocess, threads express that directly, and there is no third-party async
runtime available to lean on. One thread per connection, an agent call blocks
only its own thread, and the accept loop never stalls.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import socket
import socketserver
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import (__version__, agent, config, log as luna_log, persona,
               protocol, session as sessions, speech)
from .memory import Memory, MemoryCapExceeded, MemoryError as LunaMemoryError

log = logging.getLogger("lunad.server")


# =========================================================================
# Daemon state
# =========================================================================


class Daemon:
    """Everything the handlers need, built once."""

    def __init__(self, agent_name: str | None = None,
                 memory: Memory | None = None) -> None:
        config.ensure_dirs()
        self.started = time.time()
        # `memory` is injectable so tests can run against a temp directory
        # without touching the user's real memory files.
        self.memory = memory if memory is not None else Memory()
        self.runs = agent.RunRegistry()
        self.agent_name = (agent_name or agent.read_default_agent()).lower()
        self.adapter = agent.get_adapter(self.agent_name)
        self.persona_spec = persona.load_spec()
        self.counters: dict[str, int] = {"ask": 0, "errors": 0, "cancelled": 0,
                                         "said": 0}
        self.cost_usd = 0.0
        self._lock = threading.Lock()
        # Tier-1 is frozen for the life of the daemon's *session view* but must
        # reflect a memory.write immediately; it is cheap to rebuild, so it is
        # rebuilt per request and cached only within one request.
        self.sessions = sessions.SessionManager()
        # Speech is constructed eagerly but loads nothing: the piper process
        # only starts on the first `say`, and dies again after five idle
        # minutes. Constructing it here means `status` can report why TTS is
        # unavailable without anyone having tried to speak first.
        self.speech = speech.Speech()
        log.info(
            "daemon initialised",
            extra={"agent": self.agent_name, "version": __version__},
        )

    # -- operations ------------------------------------------------------

    def op_ping(self, req: dict[str, Any]) -> dict[str, Any]:
        return protocol.ok(req.get("id"), pong=True, ts=time.time())

    def op_status(self, req: dict[str, Any]) -> dict[str, Any]:
        available, detail = self.adapter.available()
        with self._lock:
            counters = dict(self.counters)
            cost = round(self.cost_usd, 6)
        return protocol.ok(
            req.get("id"),
            daemon={
                "version": __version__,
                "protocol": protocol.PROTOCOL_VERSION,
                "pid": os.getpid(),
                "uptime_s": round(time.time() - self.started, 1),
                "socket": str(config.SOCKET_PATH),
                "state_dir": str(config.STATE_DIR),
                "log": str(config.LOG_PATH),
                "threads": threading.active_count(),
            },
            agent={
                "name": self.agent_name,
                "available": available,
                "detail": detail,
                "timeout_s": config.AGENT_TIMEOUT_S,
            },
            memory=self.memory.usage(),
            activity={
                "in_flight": self.runs.snapshot(),
                "counters": counters,
                "session_cost_usd": cost,
            },
            persona={
                "path": str(config.PERSONA_PATH),
                "chars": len(self.persona_spec),
            },
            speech=self.speech.status(),
            sessions={
                "live": self.sessions.snapshot(),
                "counters": dict(self.sessions.counters),
                "idle_s": self.sessions.idle_s,
                "max_turns": self.sessions.max_turns,
            },
        )

    def op_ask(self, req: dict[str, Any]) -> dict[str, Any]:
        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise protocol.ProtocolError("ask requires a non-empty 'prompt' string")
        request_id = str(req.get("id") or uuid.uuid4().hex[:12])

        if req.get("detach"):
            # For the voice router, which runs inside voxtype's post_process
            # timeout and must not wait for an answer it is not going to print.
            # The reply is spoken, logged and remembered on this thread instead.
            threading.Thread(target=self._detached_ask, args=(dict(req),),
                             daemon=True,
                             name=f"luna-ask-{request_id}").start()
            return protocol.ok(request_id, queued=True,
                               surface=str(req.get("surface") or "cli"))
        return self._ask(req, request_id)

    def _detached_ask(self, req: dict[str, Any]) -> None:
        request_id = str(req.get("id") or uuid.uuid4().hex[:12])
        try:
            self._ask(req, request_id)
        except Exception as exc:  # noqa: BLE001 - nobody is listening; log it
            with self._lock:
                self.counters["errors"] += 1
            log.warning("detached ask failed",
                        extra={"req_id": request_id,
                               "detail": f"{type(exc).__name__}: {exc}"})
            if str(req.get("surface")) == "voice":
                # Silence after speaking to her is indistinguishable from a
                # daemon that died. Say so, briefly.
                self._speak_safely("Something went wrong with that one. "
                                   "The detail is on screen.")

    def _ask(self, req: dict[str, Any], request_id: str) -> dict[str, Any]:
        prompt = str(req["prompt"]).strip()
        surface = str(req.get("surface") or "cli")
        remember = bool(req.get("remember", True))
        speak = bool(req.get("speak", surface == "voice"))
        conversation = str(req.get("conversation")
                           or config.DEFAULT_CONVERSATION)

        tier1 = self.memory.tier1_block()
        try:
            recall = self.memory.recall_block(prompt)
        except LunaMemoryError as exc:
            log.warning("recall failed, continuing without it",
                        extra={"req_id": request_id, "detail": str(exc)})
            recall = ""

        # The prefix is persona + tier 1 and nothing else, so it survives
        # unchanged from turn to turn and the prompt cache is read rather than
        # rewritten. Recall belongs to this turn, so it goes in the message.
        system_prompt = persona.build_system_prompt(tier1, self.persona_spec)
        message = persona.build_user_message(prompt, recall, surface)
        prefix = sessions.fingerprint(self.persona_spec, tier1)

        explicit = req.get("session_id") or req.get("resume")
        sess = None if explicit else self.sessions.acquire(conversation, prefix)
        args = ({"session_id": req.get("session_id"), "resume": req.get("resume")}
                if explicit else self.sessions.args_for(sess))

        run = self.runs.new(prompt, surface, request_id)
        log.info("ask", extra={"req_id": request_id, "surface": surface,
                               "prompt_chars": len(prompt),
                               "system_chars": len(system_prompt),
                               "recalled": bool(recall),
                               "conversation": conversation,
                               "resuming": bool(args.get("resume"))})
        try:
            reply = self._ask_agent(req, message, system_prompt, args, run,
                                    sess, conversation, prefix, request_id)
        finally:
            self.runs.done(run)

        if sess is not None:
            self.sessions.succeeded(sess, reply.cost_usd, reply.session_id)

        episode = None
        if remember:
            try:
                episode = self.memory.episodes.record(
                    prompt, reply.text, surface=surface
                )
            except Exception:  # noqa: BLE001 - a memory fault must not eat a reply
                log.exception("failed to record episode",
                              extra={"req_id": request_id})

        with self._lock:
            self.counters["ask"] += 1
            if reply.cost_usd:
                self.cost_usd += reply.cost_usd

        spoken = None
        if speak:
            spoken = self._speak_safely(reply.text)

        log.info("reply", extra={"req_id": request_id, "wall_ms": reply.wall_ms,
                                 "cost_usd": reply.cost_usd,
                                 "reply_chars": len(reply.text),
                                 "spoke": bool(spoken)})
        payload = reply.to_dict()
        payload["recalled"] = bool(recall)
        payload["conversation"] = conversation
        payload["resumed"] = bool(args.get("resume"))
        if spoken is not None:
            payload["spoken"] = spoken
        if episode is not None:
            payload["episode"] = {"id": episode.id, "salience": episode.salience}
        return protocol.ok(request_id, **payload)

    def _ask_agent(self, req: dict[str, Any], message: str, system_prompt: str,
                   args: dict[str, Any], run: agent.AgentRun,
                   sess: sessions.Session | None, conversation: str,
                   prefix: str, request_id: str) -> agent.AgentReply:
        """One agent call, with a single retry if a resume is refused.

        A resumable session id can go stale under the daemon — the agent's own
        session store is cleaned up independently of ours. Treating that as a
        hard failure would break the first ask after every such cleanup, so a
        refused resume costs one uncached call and then heals itself.
        """
        timeout = float(req.get("timeout") or config.AGENT_TIMEOUT_S)
        try:
            return self.adapter.ask(message, system_prompt,
                                    model=req.get("model"),
                                    session_id=args.get("session_id"),
                                    resume=args.get("resume"),
                                    timeout=timeout, run=run)
        except agent.AgentFailed as exc:
            if not args.get("resume") or sess is None:
                raise
            log.warning("resume refused, starting a fresh session",
                        extra={"req_id": request_id,
                               "conversation": conversation,
                               "detail": str(exc)[:300]})
            self.sessions.drop(conversation)
            fresh = self.sessions.acquire(conversation, prefix)
            return self.adapter.ask(message, system_prompt,
                                    model=req.get("model"),
                                    session_id=fresh.session_id, resume=None,
                                    timeout=timeout, run=run)

    def _speak_safely(self, text: str) -> str | None:
        """Speak, but never let a mute speaker turn into a failed ask."""
        try:
            result = self.speech.say(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not speak the reply",
                        extra={"detail": f"{type(exc).__name__}: {exc}"})
            return None
        with self._lock:
            self.counters["said"] += 1
        return result.get("spoken") or None

    def op_say(self, req: dict[str, Any]) -> dict[str, Any]:
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            raise protocol.ProtocolError("say requires a non-empty 'text' string")
        try:
            result = self.speech.say(
                text, wait=bool(req.get("wait")),
                timeout=float(req.get("timeout") or 120.0))
        except speech.SpeechUnavailable as exc:
            return protocol.err(req.get("id"), "SpeechUnavailable", str(exc))
        with self._lock:
            self.counters["said"] += 1
        log.info("say", extra={"chars": len(text),
                               "sentences": result.get("sentences")})
        return protocol.ok(req.get("id"), **result)

    def op_speak_cancel(self, req: dict[str, Any]) -> dict[str, Any]:
        hit = self.speech.cancel()
        return protocol.ok(req.get("id"), cancelled=1 if hit else 0,
                           note="" if hit else "nothing was speaking")

    def op_session_reset(self, req: dict[str, Any]) -> dict[str, Any]:
        target = req.get("conversation")
        if target:
            return protocol.ok(req.get("id"), conversation=target,
                               dropped=1 if self.sessions.drop(str(target)) else 0)
        return protocol.ok(req.get("id"), dropped=self.sessions.clear())

    def op_memory_read(self, req: dict[str, Any]) -> dict[str, Any]:
        name = req.get("file")
        if name:
            handle = self.memory.file(str(name))
            return protocol.ok(req.get("id"), file=handle.name,
                               entries=handle.entries(), usage=handle.usage(),
                               text=handle.text())
        return protocol.ok(
            req.get("id"),
            tier1={
                "LUNA.md": {"entries": self.memory.luna.entries(),
                            "usage": self.memory.luna.usage()},
                "USER.md": {"entries": self.memory.user.entries(),
                            "usage": self.memory.user.usage()},
            },
            recent=[e.to_dict() for e in
                    self.memory.episodes.recent(int(req.get("limit") or 10))],
            tier2=self.memory.episodes.stats(),
        )

    def op_memory_write(self, req: dict[str, Any]) -> dict[str, Any]:
        handle = self.memory.file(str(req.get("file") or "LUNA.md"))
        mode = str(req.get("mode") or "append").lower()
        if mode == "append":
            entry = req.get("entry")
            if not isinstance(entry, str) or not entry.strip():
                raise protocol.ProtocolError(
                    "memory.write mode=append requires a non-empty 'entry'")
            usage = handle.append(entry)
        elif mode == "replace":
            entries = req.get("entries")
            if not isinstance(entries, list):
                raise protocol.ProtocolError(
                    "memory.write mode=replace requires an 'entries' list")
            usage = handle.replace([str(e) for e in entries])
        elif mode == "remove":
            index = req.get("index")
            if not isinstance(index, int):
                raise protocol.ProtocolError(
                    "memory.write mode=remove requires an integer 'index'")
            usage = handle.remove(index)
        else:
            raise protocol.ProtocolError(
                f"unknown memory.write mode {mode!r}; "
                "expected append, replace or remove")
        log.info("memory.write", extra={"file": handle.name, "mode": mode,
                                        "pct": usage["pct"]})
        return protocol.ok(req.get("id"), file=handle.name, mode=mode,
                           usage=usage, entries=handle.entries())

    def op_memory_search(self, req: dict[str, Any]) -> dict[str, Any]:
        query = req.get("query")
        if not isinstance(query, str) or not query.strip():
            raise protocol.ProtocolError("memory.search requires a 'query' string")
        limit = int(req.get("limit") or 10)
        hits = self.memory.episodes.search(query, limit=limit)
        return protocol.ok(req.get("id"), query=query,
                           results=[h.to_dict() for h in hits], count=len(hits))

    def op_cancel(self, req: dict[str, Any]) -> dict[str, Any]:
        if req.get("all"):
            n = self.runs.cancel_all()
            with self._lock:
                self.counters["cancelled"] += n
            return protocol.ok(req.get("id"), cancelled=n)
        target = req.get("target")
        if not isinstance(target, str) or not target:
            raise protocol.ProtocolError(
                "cancel requires a 'target' request id, or all=true")
        hit = self.runs.cancel(target)
        if hit:
            with self._lock:
                self.counters["cancelled"] += 1
        return protocol.ok(req.get("id"), target=target, cancelled=1 if hit else 0,
                           note="" if hit else "no in-flight request with that id")

    def op_shutdown(self, req: dict[str, Any]) -> dict[str, Any]:
        # Present so a supervisor or the CLI can stop the daemon cleanly; the
        # actual stop is scheduled after the response is flushed.
        threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return protocol.ok(req.get("id"), stopping=True)

    def dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        table: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "ping": self.op_ping,
            "status": self.op_status,
            "ask": self.op_ask,
            "say": self.op_say,
            "speak.cancel": self.op_speak_cancel,
            "session.reset": self.op_session_reset,
            "memory.read": self.op_memory_read,
            "memory.write": self.op_memory_write,
            "memory.search": self.op_memory_search,
            "cancel": self.op_cancel,
            "shutdown": self.op_shutdown,
        }
        op = req["op"]
        handler = table.get(op)
        if handler is None:
            return protocol.err(
                req.get("id"), "UnknownOp",
                f"unknown op {op!r}; known: {', '.join(sorted(table))}")
        try:
            return handler(req)
        except MemoryCapExceeded as exc:
            with self._lock:
                self.counters["errors"] += 1
            log.warning("memory cap exceeded", extra=luna_log.safe_extra(exc.to_dict()))
            return protocol.err(req.get("id"), **_strip(exc.to_dict()))
        except LunaMemoryError as exc:
            with self._lock:
                self.counters["errors"] += 1
            return protocol.err(req.get("id"), **_strip(exc.to_dict()))
        except agent.AgentError as exc:
            with self._lock:
                self.counters["errors"] += 1
            log.warning("agent error", extra={"op": op, "detail": str(exc)})
            return protocol.err(req.get("id"), **_strip(exc.to_dict()))
        except protocol.ProtocolError as exc:
            return protocol.err(req.get("id"), "ProtocolError", str(exc))
        except Exception as exc:  # noqa: BLE001 - the daemon must survive anything
            with self._lock:
                self.counters["errors"] += 1
            log.exception("unhandled error in %s", op)
            return protocol.err(req.get("id"), "InternalError",
                                f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        self.runs.cancel_all()
        self.speech.close()
        self.memory.close()


def _strip(d: dict[str, Any]) -> dict[str, Any]:
    """Adapt an exception dict to protocol.err's keyword signature."""
    out = dict(d)
    return {"error": out.pop("error", "Error"),
            "message": out.pop("message", ""), **out}


# =========================================================================
# Socket plumbing
# =========================================================================


class _Handler(socketserver.StreamRequestHandler):
    # A slow agent call must not be killed by a socket timeout, but a wedged
    # client must not hold a thread forever either.
    timeout = config.AGENT_TIMEOUT_S + 60

    def handle(self) -> None:
        daemon: Daemon = self.server.daemon  # type: ignore[attr-defined]
        peer = "unix"
        while True:
            try:
                line = self.rfile.readline()
            except (TimeoutError, socket.timeout):
                log.info("client idle timeout", extra={"peer": peer})
                return
            except OSError as exc:
                if exc.errno not in (errno.ECONNRESET, errno.EPIPE):
                    log.warning("read error", extra={"detail": str(exc)})
                return
            if not line:
                return
            if not line.strip():
                continue
            try:
                req = protocol.decode(line)
            except protocol.ProtocolError as exc:
                response = protocol.err(None, "ProtocolError", str(exc))
            else:
                response = daemon.dispatch(req)
            try:
                self.wfile.write(protocol.encode(response))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log.info("client vanished before the response was written")
                return


class LunaServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(self, path: Path, daemon: Daemon) -> None:
        self.socket_path = path
        self.daemon = daemon
        _clear_stale_socket(path)
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        log.exception("handler crashed")

    def server_close(self) -> None:
        super().server_close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove socket",
                        extra={"path": str(self.socket_path), "detail": str(exc)})


class AlreadyRunning(RuntimeError):
    pass


def _clear_stale_socket(path: Path) -> None:
    """Remove a leftover socket, but never one a live daemon is listening on."""
    if not path.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink(missing_ok=True)
        log.info("removed stale socket", extra={"path": str(path)})
    except OSError:
        path.unlink(missing_ok=True)
    else:
        raise AlreadyRunning(
            f"another lunad is already listening on {path}. "
            "Stop it with: systemctl --user stop lunad"
        )
    finally:
        probe.close()


def serve(agent_name: str | None = None) -> int:
    """Run until SIGTERM/SIGINT. Returns a process exit code."""
    daemon = Daemon(agent_name)
    server = LunaServer(config.SOCKET_PATH, daemon)
    stopping = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("shutting down", extra={"signal": signal.Signals(signum).name})
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("listening", extra={"socket": str(config.SOCKET_PATH),
                                 "agent": daemon.agent_name,
                                 "pid": os.getpid()})
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        daemon.close()
        log.info("stopped")
    return 0
