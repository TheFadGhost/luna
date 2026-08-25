"""Agent invocation. ARCHITECTURE.md section 6.

lunad never talks to a model API directly. It shells out to whichever headless
agent CLI the desktop is configured for, reading the choice from
``~/.config/omarchy/defaults/agent`` so Luna follows the same default as the
rest of Omarchy rather than inventing her own.

Phase 0 implements the ``claude`` adapter only. The ``codex`` adapter is a
declared stub: its flags have not been verified on this machine and guessing
them would produce a component that looks finished and is not.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("lunad.agent")


# =========================================================================
# Errors — every one of these is surfaced to the client, never swallowed
# =========================================================================


class AgentError(Exception):
    kind = "AgentError"

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": str(self)}


class AgentUnavailable(AgentError):
    """The configured agent cannot be run at all (missing binary, or a stub)."""
    kind = "AgentUnavailable"


class AgentTimeout(AgentError):
    kind = "AgentTimeout"


class AgentFailed(AgentError):
    """Non-zero exit, or the agent reported an error in its own output."""
    kind = "AgentFailed"

    def __init__(self, message: str, returncode: int | None = None,
                 stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["returncode"] = self.returncode
        if self.stderr:
            d["stderr"] = self.stderr[-2000:]
        return d


class AgentMalformedOutput(AgentError):
    """The agent exited cleanly but did not produce the JSON we asked for."""
    kind = "AgentMalformedOutput"

    def __init__(self, message: str, sample: str = "") -> None:
        super().__init__(message)
        self.sample = sample

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        if self.sample:
            d["sample"] = self.sample[:2000]
        return d


class AgentCancelled(AgentError):
    kind = "AgentCancelled"


# =========================================================================
# Result
# =========================================================================


@dataclass
class AgentReply:
    text: str
    agent: str
    model: str | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    wall_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.text,
            "agent": self.agent,
            "model": self.model,
            "session_id": self.session_id,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "num_turns": self.num_turns,
            "wall_ms": self.wall_ms,
        }


# =========================================================================
# Adapters
# =========================================================================


class BaseAdapter:
    name = "base"

    def binary(self) -> str:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        try:
            return True, self.binary()
        except AgentUnavailable as exc:
            return False, str(exc)

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> AgentReply:
        raise NotImplementedError


def _first_existing(candidates: list[Path | str]) -> str | None:
    for cand in candidates:
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


class ClaudeAdapter(BaseAdapter):
    """Headless ``claude`` (verified against Claude Code 2.1.241).

    Flags used, all present in this version:

    ``-p``                       print mode, non-interactive
    ``--output-format json``     one JSON object on stdout, parsed properly
    ``--append-system-prompt``   persona + memory
    ``--model``                  optional override
    ``--permission-mode``        set explicitly; Phase 0 needs no permissions
    ``--session-id`` / ``--resume``  conversation continuity
    ``--safe-mode``              disables CLAUDE.md discovery, skills, plugins,
                                 hooks and MCP. Without it the user's own
                                 ~/.claude/CLAUDE.md is loaded into every Luna
                                 turn: ~22k extra cached tokens per call and,
                                 worse, a second set of standing instructions
                                 competing with the persona spec.
    ``--tools ""``               Phase 0 is text in, text out. No tools at all.
    """

    name = "claude"

    _CANDIDATES = [
        Path.home() / ".local/share/mise/installs/claude/latest/claude",
        Path.home() / ".local/share/mise/shims/claude",
        "/usr/bin/claude",
    ]

    def __init__(self, model: str | None = config.DEFAULT_MODEL) -> None:
        self.model = model

    def binary(self) -> str:
        # LUNA_AGENT_BIN wins so the unit file can pin a version if mise moves.
        override = os.environ.get("LUNA_AGENT_BIN")
        if override:
            if not (Path(override).is_file() and os.access(override, os.X_OK)):
                raise AgentUnavailable(
                    f"LUNA_AGENT_BIN={override} is not an executable file"
                )
            return override
        found = _first_existing(self._CANDIDATES)
        if found:
            return found
        which = shutil.which("claude")
        if which:
            return which
        raise AgentUnavailable(
            "claude CLI not found. Looked at: "
            + ", ".join(str(c) for c in self._CANDIDATES)
            + " and $PATH. Set LUNA_AGENT_BIN to its absolute path."
        )

    def build_argv(
        self,
        prompt: str,
        system_prompt: str,
        model: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        permission_mode: str = "dontAsk",
    ) -> list[str]:
        argv = [
            self.binary(),
            "-p", prompt,
            "--output-format", "json",
            "--append-system-prompt", system_prompt,
            "--permission-mode", permission_mode,
            "--safe-mode",
            "--tools", "",
        ]
        chosen = model or self.model
        if chosen:
            argv += ["--model", chosen]
        if resume:
            argv += ["--resume", resume]
        elif session_id:
            argv += ["--session-id", session_id]
        return argv

    def ask(
        self,
        prompt: str,
        system_prompt: str,
        model: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        timeout: float = config.AGENT_TIMEOUT_S,
        run: "AgentRun | None" = None,
        **_: Any,
    ) -> AgentReply:
        argv = self.build_argv(prompt, system_prompt, model, session_id, resume)
        started = time.monotonic()

        env = dict(os.environ)
        env.setdefault("HOME", str(Path.home()))
        # Keep the agent out of any project: a neutral cwd stops it picking up
        # a repo's CLAUDE.md, git status, or settings as Luna's context.
        config.AGENT_CWD.mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(config.AGENT_CWD),
                env=env,
                text=True,
                # Own process group: cancellation signals only what we spawned.
                start_new_session=True,
            )
        except OSError as exc:
            raise AgentUnavailable(f"could not start {argv[0]}: {exc}") from exc

        if run is not None:
            run.attach(proc)

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            stdout, stderr = proc.communicate()
            raise AgentTimeout(
                f"{self.name} did not answer within {timeout:.0f}s and was "
                f"terminated. Partial stderr: {(stderr or '').strip()[-500:]}"
            )

        wall_ms = int((time.monotonic() - started) * 1000)

        if run is not None and run.cancelled:
            raise AgentCancelled(f"request {run.request_id} was cancelled")

        if proc.returncode != 0:
            raise AgentFailed(
                f"{self.name} exited {proc.returncode}: "
                f"{(stderr or stdout or '').strip()[-1000:] or '(no output)'}",
                returncode=proc.returncode,
                stderr=stderr or "",
            )

        return self.parse_output(stdout, stderr, wall_ms)

    def parse_output(self, stdout: str, stderr: str, wall_ms: int) -> AgentReply:
        """Parse ``--output-format json``. Malformed output is an error, not
        a shrug: returning raw stdout as a "reply" would hide real faults."""
        raw = (stdout or "").strip()
        if not raw:
            raise AgentMalformedOutput(
                f"{self.name} exited 0 but wrote nothing to stdout. "
                f"stderr: {(stderr or '').strip()[-500:] or '(empty)'}"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentMalformedOutput(
                f"{self.name} stdout was not valid JSON ({exc}). "
                "Expected a single object from --output-format json.",
                sample=raw,
            ) from exc
        if not isinstance(data, dict):
            raise AgentMalformedOutput(
                f"{self.name} returned {type(data).__name__}, expected an object",
                sample=raw,
            )
        if data.get("is_error"):
            raise AgentFailed(
                f"{self.name} reported an error: "
                f"{data.get('result') or data.get('api_error_status') or raw[:500]}"
            )
        text = data.get("result")
        if not isinstance(text, str) or not text.strip():
            raise AgentMalformedOutput(
                f"{self.name} JSON had no usable 'result' field "
                f"(subtype={data.get('subtype')!r})",
                sample=raw,
            )

        # modelUsage can hold several models (a cheap one for side tasks as
        # well as the answering model). The one that produced the reply is the
        # one that emitted the most output tokens; first-key order is not it.
        usage = data.get("modelUsage")
        model = None
        if isinstance(usage, dict) and usage:
            model = max(
                usage,
                key=lambda k: (usage[k] or {}).get("outputTokens", 0)
                if isinstance(usage.get(k), dict) else 0,
            )
        return AgentReply(
            text=text.strip(),
            agent=self.name,
            model=model,
            session_id=data.get("session_id"),
            cost_usd=data.get("total_cost_usd"),
            duration_ms=data.get("duration_ms"),
            num_turns=data.get("num_turns"),
            wall_ms=wall_ms,
        )

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Kill only the process group we created. Never signals anything else.

        This is the session firewall from ARCHITECTURE.md section 7 in its
        smallest form: Luna signals what she spawned and nothing more.
        """
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(50):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class CodexAdapter(BaseAdapter):
    """STUB — not implemented in Phase 0. Do not call.

    codex 0.149.1 is installed on this machine via mise, but its headless
    invocation (exec/non-interactive flags, output shape, sandbox arguments)
    has not been verified here. A guessed flag set would fail at the worst
    possible time, in a daemon, in front of the user. So this adapter refuses
    loudly instead.

    Phase 1+ checklist to make it real:
      * confirm the non-interactive subcommand and its flags from --help
      * confirm the machine-readable output format and its schema
      * confirm how a system prompt is supplied
      * confirm sandbox/approval flags, since Luna runs with full autonomy
    """

    name = "codex"

    _CANDIDATES = [
        Path.home() / ".local/share/mise/installs/codex/latest/codex",
        Path.home() / ".local/share/mise/shims/codex",
    ]

    def binary(self) -> str:
        found = _first_existing(self._CANDIDATES) or shutil.which("codex")
        if not found:
            raise AgentUnavailable("codex CLI not found")
        return found

    def available(self) -> tuple[bool, str]:
        return False, "codex adapter is a Phase 0 stub; flags unverified"

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> AgentReply:
        raise AgentUnavailable(
            "the codex adapter is a declared stub and was not implemented in "
            "Phase 0: its headless flags have not been verified on this "
            "machine. Set ~/.config/omarchy/defaults/agent to 'claude', or "
            "implement lunad/agent.py:CodexAdapter."
        )


ADAPTERS: dict[str, type[BaseAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
}


def read_default_agent(path: Path = config.OMARCHY_DEFAULT_AGENT) -> str:
    """Read Omarchy's configured default agent. Falls back to ``claude``."""
    try:
        name = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "claude"
    return name or "claude"


def get_adapter(name: str | None = None, **kw: Any) -> BaseAdapter:
    chosen = (name or read_default_agent()).lower()
    cls = ADAPTERS.get(chosen)
    if cls is None:
        raise AgentUnavailable(
            f"no adapter for agent {chosen!r}; known: {', '.join(sorted(ADAPTERS))}"
        )
    if cls is ClaudeAdapter:
        return cls(**kw)
    return cls()


# =========================================================================
# In-flight run tracking (so `cancel` has something to cancel)
# =========================================================================


@dataclass
class AgentRun:
    """A single in-flight agent invocation.

    The socket handler thread owns it; the registry lets another thread cancel
    it by request id without either of them touching the other's sockets.
    """

    request_id: str
    prompt: str
    surface: str = "cli"
    started: float = field(default_factory=time.time)
    cancelled: bool = False
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
            if self.cancelled:
                ClaudeAdapter._terminate(proc)

    def cancel(self) -> bool:
        with self._lock:
            self.cancelled = True
            if self._proc is None:
                return False
            ClaudeAdapter._terminate(self._proc)
            return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.request_id,
            "surface": self.surface,
            "age_s": round(time.time() - self.started, 2),
            "prompt": self.prompt[:120],
            "pid": self._proc.pid if self._proc else None,
            "cancelled": self.cancelled,
        }


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}
        self._lock = threading.Lock()

    def new(self, prompt: str, surface: str, request_id: str | None = None) -> AgentRun:
        run = AgentRun(request_id or uuid.uuid4().hex[:12], prompt, surface)
        with self._lock:
            self._runs[run.request_id] = run
        return run

    def done(self, run: AgentRun) -> None:
        with self._lock:
            self._runs.pop(run.request_id, None)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            run = self._runs.get(request_id)
        return run.cancel() if run else False

    def cancel_all(self) -> int:
        with self._lock:
            runs = list(self._runs.values())
        return sum(1 for r in runs if r.cancel())

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._runs.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._runs)
