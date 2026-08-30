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

from . import (__version__, agent, audit as audit_mod, config, confirm,
               consolidate, dispatch, log as luna_log, persona,
               presence as presence_mod, protocol, safety,
               session as sessions, settings as settings_mod, speech)
from .memory import (Memory, MemoryCapExceeded, MemoryError as LunaMemoryError,
                     SolMemory)

log = logging.getLogger("lunad.server")


# =========================================================================
# Daemon state
# =========================================================================


class Daemon:
    """Everything the handlers need, built once."""

    def __init__(self, agent_name: str | None = None,
                 memory: Memory | None = None,
                 sol_memory: SolMemory | None = None,
                 audit: audit_mod.AuditLog | None = None,
                 dispatcher: dispatch.Dispatcher | None = None,
                 settings: settings_mod.Settings | None = None) -> None:
        config.ensure_dirs()
        self.started = time.time()
        # Settings come up before anything that reads one. A missing file is
        # created here, populated with the schema's defaults, rather than left
        # for the GUI to write: lunad must be configurable from the moment it
        # starts, not from the moment somebody opens a settings window.
        self.settings = (settings if settings is not None
                         else settings_mod.settings())
        settings_mod.ensure_secrets_file()
        # `memory` is injectable so tests can run against a temp directory
        # without touching the user's real memory files.
        self.memory = memory if memory is not None else Memory()
        self.sol_memory = sol_memory if sol_memory is not None else SolMemory()
        self.audit = audit if audit is not None else audit_mod.audit()
        # Every signal and every spawn lands in the audit log from here on.
        # The firewall holds the hook rather than importing audit itself, so
        # `safety` stays at the bottom of the dependency graph.
        safety.set_audit_hook(self.audit.hook)
        self.confirm = confirm.ConfirmBroker(settings=self.settings,
                                             audit=self.audit)
        self.dispatcher = (dispatcher if dispatcher is not None
                           else dispatch.Dispatcher(audit=self.audit,
                                                    confirm=self.confirm))
        # Exactly one broker in the process, whoever built the dispatcher. Two
        # would mean a question raised by a dispatch could not be answered
        # through `luna confirm`, because the pending map it looks in would be
        # the other object's.
        self.dispatcher.confirm = self.confirm
        # `[dispatch] job_retention_days`, on a timer in its own thread. Not on
        # the request path: it walks a directory tree, and nobody should wait
        # for it to answer a question about the weather.
        self.dispatcher.start_gc()
        self.runs = agent.RunRegistry()
        # `--agent` beats the config, the config beats Omarchy's default. The
        # CLI flag is an operator override for one run and must not be silently
        # replaced by a file the operator did not look at.
        self.agent_name = (agent_name
                           or str(self.settings.get("assistant.agent") or "")
                           or agent.read_default_agent()).lower()
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
        #
        # Presence comes first because Speech reports its own transitions into
        # it: it publishes `idle`/`thinking`/`speaking` to the file the bar
        # widget watches.
        self.presence = presence_mod.Presence()
        self.speech = speech.Speech(settings=self.settings,
                                    on_activity=self._publish_state)
        # The consolidation pass. `adapter` is a callable and not the adapter
        # itself, because `_settings_changed` rebinds `self.adapter` when the
        # user switches agent, and a background thread holding the old object
        # would go on shelling out to the CLI they just switched away from.
        self.consolidator = consolidate.Consolidator(
            self.memory, adapter=lambda: self.adapter, audit=self.audit,
            settings=self.settings, on_spend=self._spend)
        self.settings.on_change(self._settings_changed)
        self.settings.start_watching()
        log.info(
            "daemon initialised",
            extra={"agent": self.agent_name, "version": __version__},
        )
        self._publish_state()
        self.audit.append("daemon.started", ok=True, agent=self.agent_name,
                          version=__version__,
                          why="lunad came up",
                          assistant=settings_mod.assistant_name(),
                          config=str(self.settings.path),
                          tracked_pids=len(safety.ledger()))

    # -- presence --------------------------------------------------------

    def _publish_state(self) -> None:
        """Recompute the one word the desktop reads, and publish it.

        Derived rather than assigned, so the callers do not have to agree on
        an order: speech finishing while an ask is still running leaves
        `thinking`, and an ask finishing while she is mid-sentence leaves
        `speaking`. Speaking wins over thinking because it is the state you
        can act on — it is what `luna hush` interrupts.
        """
        if self.speech.speaking:
            state = presence_mod.SPEAKING
        elif len(self.runs):
            state = presence_mod.THINKING
        else:
            state = presence_mod.IDLE
        self.presence.set(state)

    # -- hot reload ------------------------------------------------------

    def _settings_changed(self, changes: list[dict[str, Any]]) -> None:
        """React to a config change without a restart.

        Most settings need nothing done: they are read at the point of use, so
        the next request already sees them. Three do. Her *name* is part of the
        cacheable system prompt, so a rename must retire the live sessions or
        the next turn resumes a conversation with the old identity frozen into
        its prefix. The agent is a different binary entirely, so switching it
        needs a new adapter. And `max_parallel` is read at every admission
        decision, but admission only *happens* when a job ends or a new one
        arrives — so raising the limit has to poke the queue here, or waiting
        work sits there until something else moves and the setting looks inert.
        """
        keys = {c["key"] for c in changes}
        self.audit.append("settings.reloaded", ok=True,
                          why="config.toml changed on disk",
                          changed=[f"{c['key']}: {c['from']!r} -> {c['to']!r}"
                                   for c in changes])
        if "assistant.name" in keys:
            dropped = self.sessions.clear()
            # NOT a bare extra={"name": ...}: "name" is a reserved LogRecord
            # field and logging raises KeyError on the collision, which killed
            # the whole settings-listener chain. safe_extra() renames clashes.
            log.info("assistant renamed; retired the live sessions",
                     extra=luna_log.safe_extra(
                         {"name": settings_mod.assistant_name(),
                          "dropped": dropped}))
        if "assistant.agent" in keys:
            wanted = str(self.settings.get("assistant.agent") or "").lower()
            try:
                self.adapter = agent.get_adapter(wanted)
                self.agent_name = wanted
                log.info("agent switched", extra={"agent": wanted})
            except agent.AgentError as exc:
                log.warning("cannot switch agent; keeping the current one",
                            extra={"wanted": wanted, "detail": str(exc)})
        if "dispatch.max_parallel" in keys:
            # Lowering it admits nothing and kills nothing, which is the point:
            # the running count drains on its own.
            started = self.dispatcher.admit_ready()
            if started:
                log.info("the raised parallel limit admitted waiting jobs",
                         extra={"started": started,
                                "max_parallel": self.dispatcher.max_parallel})

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
                "state_file": str(config.STATE_FILE),
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
            consolidation=self.consolidator.snapshot(),
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
            settings={**self.settings.status(),
                      "assistant": settings_mod.assistant_name(),
                      "specialist": settings_mod.specialist_name(),
                      "secrets": settings_mod.secrets_status()},
            confirm=self.confirm.snapshot(),
            dispatch=self.dispatcher.snapshot(),
            audit=self.audit.stats(),
            spawned={"tracked": len(safety.ledger()),
                     "refusals": safety.ledger().refusals,
                     "path": str(config.SPAWNED_PATH)},
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
        who = settings_mod.assistant_name()
        system_prompt = persona.build_system_prompt(tier1, self.persona_spec,
                                                    name=who)
        message = persona.build_user_message(prompt, recall, surface)
        # Her name is in the prefix, so it is in the fingerprint: renaming her
        # must retire the warm sessions rather than resume one whose cached
        # prefix still introduces her by the old name.
        prefix = sessions.fingerprint(self.persona_spec, tier1, who)

        explicit = req.get("session_id") or req.get("resume")
        sess = None if explicit else self.sessions.acquire(conversation, prefix)
        args = ({"session_id": req.get("session_id"), "resume": req.get("resume")}
                if explicit else self.sessions.args_for(sess))

        run = self.runs.new(prompt, surface, request_id)
        self._publish_state()
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
            self._publish_state()

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
        self._spend(reply.cost_usd)

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
        # Last, and on a thread of its own. The reply is already built; a
        # consolidation pass must never be something the user waits behind,
        # and `turn` is written so that it cannot raise into this path.
        payload["consolidating"] = self.consolidator.turn()
        return protocol.ok(request_id, **payload)

    def _spend(self, cost_usd: float | None) -> None:
        """Add a metered cost to the session total, from any thread.

        Only a truthy figure moves the counter, which is what keeps a
        subscription reply (``cost_usd`` is ``None`` on codex) from pretending
        money was spent. The consolidation pass spends through here too: it is
        the user's account either way, and a background cost that did not show
        up in `luna status` would be the least forgivable kind of surprise.
        """
        if not cost_usd:
            return
        with self._lock:
            self.cost_usd += cost_usd

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
        # "" in the config means "the agent's own default", which is also what
        # `model=None` means to every adapter, so an empty string must not be
        # forwarded as a model name.
        model = req.get("model") or str(self.settings.get("assistant.model")
                                        or "") or None
        try:
            return self.adapter.ask(message, system_prompt,
                                    model=model,
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
                                    model=model,
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

    def _namespace(self, req: dict[str, Any]) -> tuple[str, Any]:
        """Pick the memory namespace for a request.

        Two namespaces, never one with a prefix: Luna's tier-1 files and Sol's.
        `SolMemory.file` refuses LUNA.md and USER.md by name, so a request that
        names Sol's namespace cannot reach Luna's memory even by asking for it
        directly.
        """
        name = str(req.get("namespace") or "luna").strip().lower()
        if name == "luna":
            return "luna", self.memory
        if name == "sol":
            return "sol", self.sol_memory
        raise protocol.ProtocolError(
            f"unknown memory namespace {name!r}; expected 'luna' or 'sol'")

    def op_codex_profile(self, req: dict[str, Any]) -> dict[str, Any]:
        """Write ``$CODEX_HOME/luna.config.toml`` so `codex -p luna` is Luna.

        This is the one thing Luna writes outside her own state directory, and
        it is deliberately the *least* invasive shape that satisfies the ask.
        In codex 0.149.1 a profile is a separate file layered on top of the
        user's config, not a table inside it, so the user's own
        ``~/.codex/config.toml`` is never opened, never mind edited — and
        plain ``codex`` keeps behaving exactly as it did. Deleting the file
        undoes this completely.

        It is generated on request rather than before every ask, because the
        persona embeds tier-1 memory and rewriting a file in the user's codex
        home on every turn would be a background process quietly churning
        their dotfiles.
        """
        adapter = agent.CodexAdapter()
        tier1 = self.memory.tier1_block()
        system_prompt = persona.build_system_prompt(
            tier1, self.persona_spec, name=settings_mod.assistant_name())
        path = adapter.write_profile(system_prompt)
        # Recorded with its undo, because this is the one file Luna writes
        # into the user's own dotfiles and it must be reversible from the log.
        self.audit.append("codex.profile", ok=True, path=str(path),
                          bytes=len(system_prompt),
                          undo={"cmd": ["rm", "-f", str(path)],
                                "note": "removes the luna codex profile; "
                                        "plain `codex` is unaffected either way"})
        log.info("wrote the codex luna profile", extra={"path": str(path)})
        return protocol.ok(req.get("id"), path=str(path),
                           usage=f"codex -p {adapter.PROFILE_NAME}")

    def op_memory_read(self, req: dict[str, Any]) -> dict[str, Any]:
        namespace, store = self._namespace(req)
        name = req.get("file")
        if name:
            handle = store.file(str(name))
            return protocol.ok(req.get("id"), file=handle.name,
                               namespace=namespace,
                               entries=handle.entries(), usage=handle.usage(),
                               text=handle.text())
        if namespace == "sol":
            return protocol.ok(
                req.get("id"), namespace="sol",
                tier1={"SOL.md": {"entries": store.sol.entries(),
                                  "usage": store.sol.usage()}},
                recent=[e.to_dict() for e in
                        store.episodes.recent(int(req.get("limit") or 10))],
                tier2=store.episodes.stats(),
                dir=str(store.root),
            )
        return protocol.ok(
            req.get("id"),
            namespace="luna",
            tier1={
                "LUNA.md": {"entries": self.memory.luna.entries(),
                            "usage": self.memory.luna.usage()},
                "USER.md": {"entries": self.memory.user.entries(),
                            "usage": self.memory.user.usage()},
            },
            recent=[e.to_dict() for e in
                    self.memory.episodes.recent(int(req.get("limit") or 10))],
            tier2=self.memory.episodes.stats(),
            tier3=self.memory.profile.status(),
        )

    def op_memory_profile(self, req: dict[str, Any]) -> dict[str, Any]:
        """Read tier 3, or regenerate it from tier 2 first.

        `rebuild` exists because tier 3 is otherwise only refreshed by the
        consolidation pass, and `[memory] consolidate_every_turns = 0` — a
        perfectly reasonable setting for someone who does not want background
        spend — would otherwise strand the profile at whatever it last said.
        A rebuild is local, free and takes milliseconds, so there is no reason
        to make anyone wait for a paid pass to get one.

        Sol has no profile. His namespace is a working set for one job, not a
        model of a person, and deriving a persona from a specialist's job
        chatter would describe nobody.
        """
        if req.get("rebuild"):
            payload = self.memory.profile.rebuild(self.memory.episodes)
            log.info("profile rebuilt",
                     extra={"episodes": payload["episodes"],
                            "through_id": payload["through_id"]})
        else:
            payload = self.memory.profile.load()
        return protocol.ok(req.get("id"), namespace="luna",
                           profile=payload,
                           block=self.memory.profile.block(payload),
                           status=self.memory.profile.status())

    def op_memory_consolidate(self, req: dict[str, Any]) -> dict[str, Any]:
        """Run one consolidation pass now, or show what one would do.

        Synchronous, which is the whole difference from the automatic pass.
        That one runs on its own thread because a user waiting for a reply
        must never wait for a librarian; this one runs on the request thread
        because the user *is* waiting for the librarian, and there is nothing
        to report to a client that has already been answered.

        The guards are the consolidator's, not this method's — `run_manual`
        owns the decision about which of them a person may override, because
        the same decision has to hold for any other caller that ever reaches
        it. All that is decided here is that it is the luna namespace: Sol has
        no tier-1 pass to run, for the same reason he has no tier-3 profile.
        """
        dry_run = bool(req.get("dry_run"))
        result = self.consolidator.run_manual(dry_run=dry_run)
        log.info("consolidation asked for by hand",
                 extra={"dry_run": dry_run, "ran": result.get("ran"),
                        "reason": result.get("reason")})
        return protocol.ok(req.get("id"), namespace="luna", **result)

    def op_memory_write(self, req: dict[str, Any]) -> dict[str, Any]:
        namespace, store = self._namespace(req)
        default = "SOL.md" if namespace == "sol" else "LUNA.md"
        handle = store.file(str(req.get("file") or default))
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
        # The only tier-1 write with a genuine inverse is an append: the entry
        # that was just added is the last index, and removing it restores the
        # file exactly. `replace` and `remove` discard text that is not kept
        # anywhere, so no undo is claimed for them.
        undo = (audit_mod.undo_for_memory_append(handle.name,
                                                 len(handle.entries()) - 1)
                if mode == "append" else None)
        self.audit.append("memory.write", ok=True, actor=namespace,
                          file=handle.name, mode=mode,
                          chars=usage["chars"], pct=usage["pct"],
                          why=str(req.get("why") or "curated memory updated"),
                          undo=undo)
        return protocol.ok(req.get("id"), file=handle.name, mode=mode,
                           namespace=namespace, usage=usage,
                           entries=handle.entries())

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

    # -- Phase 2: delegation, the workspace, and the record -------------

    def op_dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        """Hand a task to a real agent session in the `luna` workspace."""
        task = req.get("task")
        if not isinstance(task, str) or not task.strip():
            raise protocol.ProtocolError("dispatch requires a non-empty 'task'")
        to = str(req.get("to") or "worker").lower()
        block = self.sol_memory.block() if to == "sol" else ""
        job = self.dispatcher.dispatch(
            task, to,
            timeout=float(req.get("timeout") or config.DISPATCH_TIMEOUT_S),
            sol_memory_block=block,
            estimate_seconds=_number(req.get("estimate_seconds")),
            estimate_usd=_number(req.get("estimate_usd")))
        payload = job.to_dict()
        payload["announce"] = self.dispatcher.announce(job)
        if req.get("wait"):
            # Blocking is opt-in. The default is to hand back the job id
            # immediately, because a dispatched job runs for minutes and the
            # socket client should not be holding a thread for all of it.
            payload = self._wait_for_job(job, float(req.get("wait_timeout") or
                                                    config.DISPATCH_TIMEOUT_S))
            payload["announce"] = self.dispatcher.announce(job)
        return protocol.ok(req.get("id"), **payload)

    def _wait_for_job(self, job: dispatch.Job, timeout: float) -> dict[str, Any]:
        # `queued` counts as in progress: a caller that asked to wait wants the
        # outcome, and returning the instant the job was accepted would report
        # a job that has not started as though it had nothing to say.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job.state not in ("running", "queued"):
                break
            time.sleep(0.5)
        payload = job.to_dict(output=job.read_output())
        if job.state == "running":
            payload["note"] = (f"still running after {timeout:.0f}s; "
                               f"`luna jobs` for the outcome")
        elif job.state == "queued":
            payload["note"] = (f"still queued after {timeout:.0f}s behind "
                               f"[dispatch] max_parallel = "
                               f"{self.dispatcher.max_parallel}; "
                               f"`luna jobs` for the outcome")
        return payload

    def op_jobs(self, req: dict[str, Any]) -> dict[str, Any]:
        target = req.get("cancel")
        if target:
            stopped = self.dispatcher.cancel(str(target))
            return protocol.ok(req.get("id"), cancelled=1 if stopped else 0,
                               target=str(target),
                               note="" if stopped else
                                    "no running or queued job with that id "
                                    "in this daemon")
        jobs = self.dispatcher.jobs(limit=int(req.get("limit")
                                              or config.JOB_LIST_LIMIT),
                                    with_output=bool(req.get("output")))
        return protocol.ok(req.get("id"), jobs=jobs, count=len(jobs),
                           workspace=self.dispatcher.hypr.state())

    def op_peek(self, req: dict[str, Any]) -> dict[str, Any]:
        return protocol.ok(req.get("id"), **self.dispatcher.peek())

    def op_audit(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            since = audit_mod.parse_since(req.get("since"))
        except ValueError as exc:
            raise protocol.ProtocolError(str(exc)) from exc
        entries = self.audit.read(since=since,
                                  limit=int(req.get("limit") or 40),
                                  action=req.get("action"))
        return protocol.ok(req.get("id"), entries=entries, count=len(entries),
                           summary=audit_mod.summarise(entries),
                           **self.audit.stats())

    def op_spawned(self, req: dict[str, Any]) -> dict[str, Any]:
        """The signal allowlist, and the gate's answer for a given pid.

        Exposed so the firewall can be inspected from outside the process —
        `luna spawned --check <pid>` is how the refusal is demonstrated on a
        pid Luna did not spawn.
        """
        lg = safety.ledger()
        payload: dict[str, Any] = {
            "path": str(config.SPAWNED_PATH),
            "tracked": len(lg),
            "refusals": lg.refusals,
            "entries": lg.entries(include_dead=bool(req.get("all"))),
        }
        pid = req.get("check")
        if pid is not None:
            allowed = lg.may_signal(pid)
            payload["check"] = {
                "pid": pid,
                "may_signal": allowed,
                "reason": "" if allowed else lg.why_not(pid),
                "cmdline": safety.process_cmdline(int(pid))[:200]
                if str(pid).isdigit() else "",
            }
        return protocol.ok(req.get("id"), **payload)

    # -- Jarvis: settings and confirmation over the socket ---------------

    def op_settings_get(self, req: dict[str, Any]) -> dict[str, Any]:
        """Read the config — one key, or all of it.

        The GUI reads through the daemon rather than off the disk so that what
        it shows is what lunad is actually using, including any value that fell
        back to its default because the file said something invalid.
        """
        key = req.get("key")
        if key:
            section, spec = settings_mod.find(str(key))
            return protocol.ok(
                req.get("id"), key=str(key),
                value=self.settings.get(str(key)),
                default=spec.default, kind=spec.kind,
                choices=list(spec.choices) or None,
                comment=spec.comment or None, section=section.name)
        return protocol.ok(
            req.get("id"), settings=self.settings.data,
            defaults=settings_mod.defaults(),
            schema=[{"section": sec.name,
                     "keys": [{"name": k.name, "default": k.default,
                               "kind": k.kind, "choices": list(k.choices),
                               "minimum": k.minimum, "maximum": k.maximum,
                               "comment": k.comment}
                              for k in sec.keys]}
                    for sec in settings_mod.SCHEMA],
            secrets=settings_mod.secrets_status(),
            **self.settings.status())

    def op_settings_set(self, req: dict[str, Any]) -> dict[str, Any]:
        """Write one key, or several. Validated; an invalid value is refused.

        Refusing rather than falling back is the opposite of what the *file*
        loader does, and deliberately so: a file is a thing a human typed and
        must degrade gracefully, while a `settings.set` is a program asserting
        a value and deserves to be told it is wrong.
        """
        updates = req.get("updates")
        if updates is None:
            key, value = req.get("key"), req.get("value")
            if not isinstance(key, str) or not key:
                raise protocol.ProtocolError(
                    "settings.set requires 'key' and 'value', or an "
                    "'updates' object")
            updates = {key: value}
        if not isinstance(updates, dict):
            raise protocol.ProtocolError("'updates' must be an object")
        why = str(req.get("why") or "changed through the socket")
        applied: dict[str, Any] = {}
        for key, value in updates.items():
            applied[str(key)] = self.settings.set(str(key), value, why=why)
        self.audit.append("settings.set", ok=True, why=why,
                          actor=str(req.get("actor") or "gui"),
                          changed=list(applied),
                          values={k: str(v) for k, v in applied.items()})
        return protocol.ok(req.get("id"), applied=applied,
                           settings=self.settings.data,
                           path=str(self.settings.path))

    def op_confirm(self, req: dict[str, Any]) -> dict[str, Any]:
        """List, answer, or raise a confirmation.

        ``action`` is one of ``list``, ``yes``, ``no`` or ``ask``. ``ask`` is
        the tool-side gate: a dispatched agent calls it before doing something
        in a class set to ``ask``, and blocks on the answer.
        """
        what = str(req.get("action") or "list").lower()
        if what == "list":
            return protocol.ok(req.get("id"), **self.confirm.snapshot())
        if what in ("yes", "no"):
            token = str(req.get("token") or "")
            if not token:
                raise protocol.ProtocolError(
                    f"confirm {what} requires a 'token'")
            hit = self.confirm.answer(token, what == "yes",
                                      by=str(req.get("by") or "user"))
            return protocol.ok(req.get("id"), token=token, answered=hit,
                               allow=what == "yes",
                               note="" if hit else
                                    "no pending question with that token; "
                                    "it may have already timed out")
        if what == "ask":
            klass = str(req.get("class") or req.get("confirm_action") or "")
            if not klass:
                raise protocol.ProtocolError(
                    "confirm ask requires a 'class'; known: "
                    + ", ".join(confirm.CLASSES))
            detail = str(req.get("detail") or "")
            for rule in confirm.hard_denials(detail or klass):
                decision = confirm.Decision(rule.name, confirm.DENY, False,
                                            "hard", detail, rule=rule.why)
                return protocol.ok(req.get("id"), **decision.to_dict())
            decision = self.confirm.check(
                klass, detail, why=str(req.get("why") or "agent asked"),
                actor=str(req.get("actor") or "agent"))
            return protocol.ok(req.get("id"), **decision.to_dict())
        raise protocol.ProtocolError(
            f"unknown confirm action {what!r}; expected list, yes, no or ask")

    def op_shutdown(self, req: dict[str, Any]) -> dict[str, Any]:
        # Present so a supervisor or the CLI can stop the daemon cleanly; the
        # actual stop is scheduled after the response is flushed.
        # safety.signal_self, not os.kill: the daemon signalling itself is the
        # one deliberate exception to the firewall, and it is written down in
        # exactly one place so a grep for os.kill lands there.
        threading.Timer(0.1, lambda: safety.signal_self(signal.SIGTERM)).start()
        return protocol.ok(req.get("id"), stopping=True)

    def dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        table: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "ping": self.op_ping,
            "status": self.op_status,
            "ask": self.op_ask,
            "say": self.op_say,
            "speak.cancel": self.op_speak_cancel,
            "session.reset": self.op_session_reset,
            "codex.profile": self.op_codex_profile,
            "memory.read": self.op_memory_read,
            "memory.write": self.op_memory_write,
            "memory.search": self.op_memory_search,
            "memory.profile": self.op_memory_profile,
            "memory.consolidate": self.op_memory_consolidate,
            "dispatch": self.op_dispatch,
            "jobs": self.op_jobs,
            "peek": self.op_peek,
            "audit": self.op_audit,
            "spawned": self.op_spawned,
            "settings.get": self.op_settings_get,
            "settings.set": self.op_settings_set,
            "confirm": self.op_confirm,
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
        except dispatch.DispatchError as exc:
            with self._lock:
                self.counters["errors"] += 1
            log.warning("dispatch error", extra={"op": op, "detail": str(exc)})
            return protocol.err(req.get("id"), **_strip(exc.to_dict()))
        except confirm.ConfirmDenied as exc:
            # Not an error in the daemon: it is the answer. Reported with the
            # decision attached so a caller can say *why* it did not happen.
            log.info("action not confirmed",
                     extra={"op": op,
                            "confirm_action": exc.decision.action,
                            "outcome": exc.decision.outcome})
            return protocol.err(req.get("id"), **_strip(exc.to_dict()))
        except settings_mod.SettingsError as exc:
            return protocol.err(req.get("id"), **_strip(exc.to_dict()))
        except safety.SignalRefused as exc:
            # The firewall said no. That is not an internal error and it is not
            # something to retry; it is the answer.
            log.warning("signal refused", extra={"op": op, "pid": exc.pid,
                                                 "reason": exc.reason})
            return protocol.err(req.get("id"), "SignalRefused", str(exc),
                                pid=exc.pid, reason=exc.reason)
        except protocol.ProtocolError as exc:
            return protocol.err(req.get("id"), "ProtocolError", str(exc))
        except Exception as exc:  # noqa: BLE001 - the daemon must survive anything
            with self._lock:
                self.counters["errors"] += 1
            log.exception("unhandled error in %s", op)
            return protocol.err(req.get("id"), "InternalError",
                                f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        # First, so the bar stops claiming she is here while the rest of the
        # shutdown (cancelling runs, draining speech) takes its time.
        self.presence.clear()
        self.settings.stop_watching()
        # Before the memory it writes into is closed, and bounded so a wedged
        # agent cannot hold the daemon's shutdown open.
        self.consolidator.close()
        self.runs.cancel_all()
        self.speech.close()
        self.dispatcher.close()
        self.memory.close()
        self.sol_memory.close()
        self.audit.append("daemon.stopped", ok=True,
                          why="lunad shutting down",
                          uptime_s=round(time.time() - self.started, 1))


def _number(value: Any) -> float | None:
    """A caller's estimate, or None. Never a guess of our own."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    # Before anything is built, not inside LunaServer where it used to happen
    # alone. A second lunad now finds the live one and gives up while it is
    # still nothing but a function call -- so it cannot append a
    # `daemon.started` line the machine never saw, and, since presence became
    # a file, cannot overwrite the state the running daemon is publishing and
    # leave the bar describing the wrong process. LunaServer still calls this
    # for callers that build one directly; on this path it finds nothing to do.
    _clear_stale_socket(config.SOCKET_PATH)
    daemon = Daemon(agent_name)
    try:
        server = LunaServer(config.SOCKET_PATH, daemon)
    except BaseException:
        # Constructing the daemon already published `idle` to the desktop and
        # opened everything else it owns. If the socket cannot be had -- a
        # runtime path too long for AF_UNIX, a permission fault -- none of that
        # may be left behind, or the bar spends the rest of the session showing
        # an assistant that exited during startup.
        daemon.close()
        raise
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
