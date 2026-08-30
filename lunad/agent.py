"""Agent invocation. ARCHITECTURE.md section 6.

lunad never talks to a model API directly. It shells out to whichever headless
agent CLI it is configured for. The choice is `[assistant] agent` in Luna's own
config, falling back to ``~/.config/omarchy/defaults/agent`` when that key is
empty. The fallback is the fallback and not the source of truth: that file is
the *desktop's* default agent and several other things read it, so Luna picking
her own brain must not mean editing it out from under them.

Two adapters are real: ``claude`` (Claude Code 2.1.241) and ``codex``
(codex-cli 0.149.1). Both were verified against the binaries actually installed
on this machine before a single flag was written down here; neither is guessed.

They are not symmetrical, and the asymmetry is the interesting part:

* ``claude`` takes a system prompt on the command line
  (``--append-system-prompt``). ``codex`` has no such flag at all — no
  ``--system-prompt``, no ``--append-system-prompt``. Its persona arrives as a
  config override, ``-c developer_instructions=<text>``.
* ``claude`` bills per token and reports dollars. ``codex`` here is signed in
  with a ChatGPT subscription and reports no price at all, so its cost field is
  deliberately ``None`` and its ``billing`` is ``"subscription"``. Inventing a
  dollar figure for it would corrupt the daemon's running total.
* ``claude`` accepts a session id chosen by the caller. ``codex`` assigns its
  own thread id and only tells you afterwards, so turn one is always "new" and
  the id is adopted from the reply.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import config, safety

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
    usage: dict[str, int] | None = None
    billing: str = "metered"
    """How this reply was paid for.

    ``"metered"`` — ``cost_usd`` is a real price and belongs in the daemon's
    running total. ``"subscription"`` — the call was covered by a flat-rate
    plan, ``cost_usd`` is ``None``, and the only honest unit of account is
    tokens. The daemon only ever adds a truthy ``cost_usd``, so a subscription
    reply contributes nothing to the money counter, which is correct: no money
    was spent on it.
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.text,
            "agent": self.agent,
            "model": self.model,
            "session_id": self.session_id,
            "cost_usd": self.cost_usd,
            "billing": self.billing,
            "usage": self.usage,
            "duration_ms": self.duration_ms,
            "num_turns": self.num_turns,
            "wall_ms": self.wall_ms,
        }


# =========================================================================
# Adapters
# =========================================================================


class BaseAdapter:
    name = "base"

    #: Whether a conversational ask through this adapter can actually run
    #: commands. Read by the server so Luna's operating notes describe the
    #: machine she is on rather than the machine she used to be on: a prompt
    #: that promises a shell to an agent invoked with the tools switched off
    #: produces an assistant who says she will check and then guesses, and a
    #: prompt that denies a shell to an agent that has one produces an
    #: assistant who refuses work she could have done in a single command.
    #: Both were observed. See persona.operating_notes.
    ask_has_tools = False

    def binary(self) -> str:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        try:
            return True, self.binary()
        except AgentUnavailable as exc:
            return False, str(exc)

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> AgentReply:
        raise NotImplementedError

    def dispatch_argv(self, job_dir: str, system_file: str,
                      add_dirs: tuple[str, ...] = (),
                      binary: str | None = None) -> list[str]:
        """Argv for one *dispatched* session: tools on, prompt on stdin.

        Returned rather than run, because a dispatched job is executed by a
        bash script inside a terminal (see :mod:`lunad.dispatch`) and not by
        this process. Every element is a **shell-ready** token — the caller
        passes shell expressions such as ``"$JOB"`` and gets back fragments
        that can be joined into a script — because the flag that carries the
        system prompt differs per agent and only the adapter knows whether it
        wants ``--append-system-prompt "$(cat …)"`` or ``-c key=$(cat …)``.
        """
        raise NotImplementedError

    # -- shared process plumbing ------------------------------------------

    def _spawn_and_wait(
        self,
        argv: list[str],
        *,
        timeout: float,
        run: "AgentRun | None" = None,
        stdin_data: str | None = None,
        note: str = "",
    ) -> tuple[str, str, int, int]:
        """Run an agent CLI to completion. Returns (stdout, stderr, rc, wall_ms).

        Every adapter goes through here so that there is one spawn path, one
        timeout path and one place where the pid enters and leaves the signal
        ledger. Interpreting the output is the adapter's business; owning the
        process is not.
        """
        started = time.monotonic()

        env = dict(os.environ)
        env.setdefault("HOME", str(Path.home()))
        # Keep the agent out of any project: a neutral cwd stops it picking up
        # a repo's CLAUDE.md / AGENTS.md, git status, or settings as Luna's
        # context.
        config.AGENT_CWD.mkdir(parents=True, exist_ok=True)

        try:
            # safety.spawn, not Popen: the pid is registered in the signal
            # ledger in the same operation that creates it, so there is no
            # window in which Luna owns a process she cannot prove is hers.
            # `durable=False` — this child dies with the daemon, so its record
            # is worthless after a crash and does not earn an fsync.
            proc = safety.spawn(
                argv,
                kind="agent",
                durable=False,
                note=note or f"{self.name} ask",
                stdin=subprocess.PIPE if stdin_data is not None
                else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(config.AGENT_CWD),
                env=env,
                text=True,
            )
        except OSError as exc:
            raise AgentUnavailable(f"could not start {argv[0]}: {exc}") from exc

        if run is not None:
            run.attach(proc)

        try:
            stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            stdout, stderr = proc.communicate()
            safety.reap(proc)
            raise AgentTimeout(
                f"{self.name} did not answer within {timeout:.0f}s and was "
                f"terminated. Partial stderr: {(stderr or '').strip()[-500:]}"
            )

        wall_ms = int((time.monotonic() - started) * 1000)
        safety.reap(proc)

        if run is not None and run.cancelled:
            raise AgentCancelled(f"request {run.request_id} was cancelled")

        return stdout or "", stderr or "", proc.returncode, wall_ms

    @staticmethod
    def _terminate(proc: subprocess.Popen, reason: str = "agent run ended") -> bool:
        """Stop an agent child, through the firewall and nowhere else.

        There is no signalling code here any more. Every check — is this pid
        one Luna spawned, is it still the same process, does it lead its own
        group — lives in :mod:`lunad.safety`, so there is exactly one place to
        read and exactly one place to get it wrong. A refusal propagates: a
        request to kill something Luna does not own is a bug worth seeing, not
        a warning worth swallowing.
        """
        return safety.terminate(proc, grace=5.0, reason=reason)


def shell_lines(argv: list[str]) -> list[str]:
    """Group a shell-ready argv into one line per flag, for a readable script.

    A dispatched job's script is the one part of Luna the user actually reads
    when something has gone wrong at 2am, so ``--permission-mode`` and
    ``bypassPermissions`` belong on the same line. The rule is simply that a
    flag adopts the token after it unless that token is itself a flag.
    """
    lines: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if (token.startswith("-") and token != "-"
                and i + 1 < len(argv) and not argv[i + 1].startswith("-")):
            lines.append(f"{token} {argv[i + 1]}")
            i += 2
        else:
            lines.append(token)
            i += 1
    return lines


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

    # `--tools ""` above. Unchanged, and the reason Luna's default brain is no
    # longer this adapter: a resident assistant that cannot look at the machine
    # she lives on is a chatbot with a persona file. Switching
    # `[assistant] agent` back to claude is still supported and still works —
    # her operating notes simply tell the truth about what she can do.
    ask_has_tools = False

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

    def dispatch_argv(self, job_dir: str, system_file: str,
                      add_dirs: tuple[str, ...] = (),
                      binary: str | None = None) -> list[str]:
        """A dispatched claude: tools on, permissions bypassed, prompt on stdin."""
        argv = [
            binary or self.binary(), "-p",
            "--append-system-prompt", f'"$(cat {system_file})"',
            "--permission-mode", "bypassPermissions",
            "--tools", "default",
            "--safe-mode",
            "--add-dir", job_dir,
        ]
        for extra in add_dirs:
            argv += ["--add-dir", extra]
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
        stdout, stderr, rc, wall_ms = self._spawn_and_wait(
            argv, timeout=timeout, run=run)

        if rc != 0:
            raise AgentFailed(
                f"{self.name} exited {rc}: "
                f"{(stderr or stdout or '').strip()[-1000:] or '(no output)'}",
                returncode=rc,
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
            billing="metered",
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
            duration_ms=data.get("duration_ms"),
            num_turns=data.get("num_turns"),
            wall_ms=wall_ms,
        )


class CodexAdapter(BaseAdapter):
    """Headless ``codex exec`` (verified live against codex-cli 0.149.1).

    Flags used, all confirmed present in this version by running them:

    ``exec -``                   non-interactive; ``-`` reads the prompt from
                                 stdin. Not ``-p``: that is claude's flag, and
                                 on codex ``-p`` means ``--profile``.
    ``--json``                   JSONL events on stdout (schema below)
    ``-o FILE``                  the final assistant message, written verbatim
    ``-c developer_instructions=`` persona + tier-1 memory (see below)
    ``-s read-only`` etc.        sandbox policy; the tool policy, in effect
    ``--skip-git-repo-check``    Luna's cwd is a neutral directory, not a repo
    ``--ignore-user-config``     codex's analogue of claude's ``--safe-mode``
    ``--ignore-rules``           and the same for the user's execpolicy rules
    ``-C DIR``                   working root (``exec`` only — see gotchas)
    ``exec resume <id>``         conversation continuity

    **Persona injection — the central problem, and why this answer.**

    codex 0.149.1 has no ``--system-prompt`` and no ``--append-system-prompt``.
    Three mechanisms were tested on this machine, not guessed:

    * ``-c developer_instructions=<text>`` — *chosen*. It layers a developer
      message on top of codex's own base instructions. Verified end to end:
      with it, "what are you?" answers "I'm Luna, your resident desktop
      assistant"; without it, "I'm Codex, an AI coding agent". It survives
      ``--strict-config``, and it survives ``codex exec resume``.
    * ``-c instructions=<text>`` — also works and also captures the identity,
      but it *replaces* codex's base instructions rather than adding to them.
      That throws away the tool-use and patch-application guidance a
      dispatched session needs, and measured 5,422 prompt tokens against
      4,874 for the same persona as a developer message. Rejected: strictly
      more expensive and strictly more destructive.
    * an ``AGENTS.md`` in the working directory — rejected without testing it
      as a *mechanism*, because the persona has to change with tier-1 memory
      and that would mean rewriting a file on disk before every ask.

    The value is passed on the command line, where codex parses it as TOML and
    falls back to "raw string literal" when that fails. Luna's persona begins
    ``You are Luna.`` and can never parse as TOML, so it always takes the
    literal path — which is what preserves its newlines, quotes and backticks
    exactly. :meth:`_instructions_override` states that dependency out loud
    rather than leaving it to luck.

    **The ``--json`` event schema**, observed rather than documented::

        {"type":"thread.started","thread_id":"01a0…"}
        {"type":"turn.started"}
        {"type":"item.completed","item":{"id":"item_0",
                                         "type":"agent_message","text":"…"}}
        {"type":"turn.completed","usage":{"input_tokens":15862,
            "cached_input_tokens":11008,"cache_write_input_tokens":0,
            "output_tokens":159,"reasoning_output_tokens":99}}

    On failure the stream carries ``{"type":"error","message":…}`` and
    ``{"type":"turn.failed","error":{"message":…}}`` and the process exits 1.
    A refused resume is worse than that: it exits 1 having written *nothing*
    to stdout, with the reason only on stderr.

    The reply text is taken from the JSONL and ``-o`` is kept as a fallback.
    The stream is preferred because it is the same read that yields the thread
    id, the token usage and the errors, so one parse either succeeds or fails
    as a whole; the ``-o`` file is a second, independent witness for the one
    case the stream cannot cover — output truncated mid-write.

    **Cost.** codex here is signed in with a ChatGPT subscription. There is no
    price in the events and there is no per-call dollar figure to be had, so
    ``cost_usd`` stays ``None`` and ``billing`` says ``"subscription"``.
    Tokens are reported because tokens are what was actually spent.
    """

    name = "codex"

    _CANDIDATES = [
        Path.home() / ".local/share/mise/installs/codex/latest/bin/codex",
        Path.home() / ".local/share/mise/shims/codex",
        "/usr/bin/codex",
    ]

    #: Name of the profile file Luna will write for the user's *own* codex
    #: sessions. A profile-v2 is `$CODEX_HOME/<name>.config.toml`, a separate
    #: file — not a `[profiles.x]` table inside the user's config.toml, which
    #: Luna never touches.
    PROFILE_NAME = "luna"

    def __init__(self, model: str | None = None) -> None:
        # Read late from `config`, not bound as a signature default: a default
        # is fixed at import, and `CODEX_ASK_MODEL` is exactly the sort of name
        # a test wants to move. `""` means the same as None here — the config
        # contract says an empty `[assistant] model` is "the agent's own
        # default", and for codex-as-Luna that default is a real slug rather
        # than whatever codex would have picked.
        self.model = model or config.CODEX_ASK_MODEL

    @property
    def ask_has_tools(self) -> bool:
        """codex has no `--tools ""`; the sandbox *is* the tool policy.

        So this is not a constant, it is a reading of `CODEX_ASK_SANDBOX`:
        under "read-only" she genuinely cannot run anything that changes the
        machine, and her prompt must not claim otherwise.
        """
        return config.CODEX_ASK_SANDBOX != "read-only"

    def binary(self) -> str:
        # LUNA_CODEX_BIN, not LUNA_AGENT_BIN: dispatch may run codex while the
        # conversational agent is claude, so the two overrides cannot share a
        # name without one of them silently pointing at the wrong binary.
        override = os.environ.get("LUNA_CODEX_BIN")
        if override:
            if not (Path(override).is_file() and os.access(override, os.X_OK)):
                raise AgentUnavailable(
                    f"LUNA_CODEX_BIN={override} is not an executable file"
                )
            return override
        found = _first_existing(self._CANDIDATES)
        if found:
            return found
        which = shutil.which("codex")
        if which:
            return which
        raise AgentUnavailable(
            "codex CLI not found. Looked at: "
            + ", ".join(str(c) for c in self._CANDIDATES)
            + " and $PATH. Set LUNA_CODEX_BIN to its absolute path."
        )

    def available(self) -> tuple[bool, str]:
        """A binary is not enough: codex without auth fails at the first call.

        Checking for the credentials file here turns a mid-conversation
        failure into a startup answer the user can act on.
        """
        try:
            found = self.binary()
        except AgentUnavailable as exc:
            return False, str(exc)
        if not config.CODEX_AUTH.is_file():
            return False, (
                f"{found} is installed but {config.CODEX_AUTH} is missing — "
                "codex is not logged in. Run `codex login`."
            )
        return True, found

    # -- argv --------------------------------------------------------------

    @staticmethod
    def _instructions_override(system_prompt: str) -> str:
        """``developer_instructions=<persona>``, as codex's ``-c`` wants it.

        codex parses the value as TOML and only falls back to a raw literal
        when that fails. A persona that happened to begin and end with a quote
        would therefore be *parsed*, losing its interior escapes; a persona
        that happened to look like ``key = value`` would be worse. Neither can
        happen with real prose, but "cannot happen" is how silent corruption
        gets in, so the value is prefixed with a newline: a leading newline is
        not valid TOML for any value, which pins the literal path open.
        """
        return f"{config.CODEX_PERSONA_KEY}=\n" + system_prompt

    def build_argv(
        self,
        system_prompt: str,
        model: str | None = None,
        resume: str | None = None,
        output_file: str | None = None,
        sandbox: str | None = None,
        mode: str = "ask",
        images: Sequence[str] = (),
    ) -> list[str]:
        """The argv for one turn. The prompt is *not* here — it goes on stdin.

        Putting the prompt on stdin (``exec -``) rather than in argv is a
        deliberate difference from :class:`ClaudeAdapter`. A Luna prompt
        carries a tier-2 recall block, so it is unbounded in a way claude's
        never was, and a prompt that happens to start with ``-`` would
        otherwise be read as a flag.
        """
        argv = [self.binary(), "exec"]
        if resume:
            # `codex exec resume <SESSION_ID> [PROMPT]`. Note the asymmetry
            # with plain `exec`: resume takes no -s/--sandbox and no -C/--cd.
            argv += ["resume", resume]
        argv += ["-"]
        argv += ["--json", "--skip-git-repo-check"]
        if output_file:
            argv += ["-o", output_file]
        if config.CODEX_IGNORE_USER_CONFIG:
            argv += ["--ignore-user-config", "--ignore-rules"]

        chosen_sandbox = sandbox or (
            config.CODEX_DISPATCH_SANDBOX if mode == "dispatch"
            else config.CODEX_ASK_SANDBOX
        )
        if chosen_sandbox == "bypass":
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        elif resume:
            # `exec resume` has no -s, so the same policy has to be set the
            # long way round. Verified: -c sandbox_mode=read-only is accepted.
            argv += ["-c", f"sandbox_mode={chosen_sandbox}"]
        else:
            argv += ["-s", chosen_sandbox]

        if not resume:
            argv += ["-C", str(config.AGENT_CWD)]

        argv += ["-c", self._instructions_override(system_prompt)]
        chosen_model = model or self.model
        if chosen_model:
            argv += ["-m", chosen_model]
        # Last, and deliberately last. `-i/--image <FILE>...` is variadic: clap
        # keeps eating tokens until it meets one that looks like a flag, so an
        # image list in the middle of the command line would swallow whatever
        # followed it. At the end there is nothing left to swallow. One `-i`
        # per file rather than one `-i` with many, for the same reason.
        #
        # gpt-5.6-luna has vision natively and `codex exec -i` attaches the
        # image to the turn. There is no second model here and no HTTP call:
        # OpenRouter is for text-to-speech and nothing else.
        for image in images:
            argv += ["-i", str(image)]
        return argv

    def dispatch_argv(self, job_dir: str, system_file: str,
                      add_dirs: tuple[str, ...] = (),
                      binary: str | None = None) -> list[str]:
        """A dispatched codex: sandbox bypassed, prompt on stdin.

        ``--add-dir`` is passed even under the bypass flag, which makes no
        difference to what the session *can* do, because it is what tells the
        session where its work is meant to be.
        """
        argv = [binary or self.binary(), "exec", "-", "--skip-git-repo-check"]
        if config.CODEX_IGNORE_USER_CONFIG:
            argv += ["--ignore-user-config", "--ignore-rules"]
        if config.CODEX_DISPATCH_SANDBOX == "bypass":
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            argv += ["-s", config.CODEX_DISPATCH_SANDBOX]
        argv += ["-C", job_dir, "--add-dir", job_dir]
        for extra in add_dirs:
            argv += ["--add-dir", extra]
        # Sol's model, not Luna's, and not the caller's. `self.model` is the
        # conversational slug and belongs to the ask path; a dispatched session
        # is a coding agent doing coding-agent work, and the two are different
        # models on purpose. Read from `config` at script-generation time so a
        # change to the constant reaches the next job rather than the next
        # daemon restart.
        if config.CODEX_DISPATCH_MODEL:
            argv += ["-m", config.CODEX_DISPATCH_MODEL]
        argv += ["-c", f'"{config.CODEX_PERSONA_KEY}=$(cat {system_file})"']
        return argv

    # -- the call ----------------------------------------------------------

    def ask(
        self,
        prompt: str,
        system_prompt: str,
        model: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        timeout: float = config.AGENT_TIMEOUT_S,
        run: "AgentRun | None" = None,
        images: Sequence[str] = (),
        **_: Any,
    ) -> AgentReply:
        # `session_id` is accepted and ignored, on purpose. claude lets the
        # caller name a conversation before it exists; codex assigns its own
        # thread id and only reports it in `thread.started`. SessionManager
        # already adopts an id the agent reports back, so turn one simply has
        # no id and turn two resumes the one codex chose.
        config.AGENT_CWD.mkdir(parents=True, exist_ok=True)
        fd, out_path = tempfile.mkstemp(prefix="codex-last-", suffix=".txt",
                                        dir=str(config.AGENT_CWD))
        os.close(fd)
        try:
            argv = self.build_argv(system_prompt, model=model, resume=resume,
                                   output_file=out_path, images=images)
            stdout, stderr, rc, wall_ms = self._spawn_and_wait(
                argv, timeout=timeout, run=run, stdin_data=prompt)
            try:
                last_message = Path(out_path).read_text(encoding="utf-8")
            except OSError:
                last_message = ""
            return self.parse_output(stdout, stderr, rc, wall_ms,
                                     last_message=last_message,
                                     model=model or self.model)
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def parse_output(self, stdout: str, stderr: str, rc: int, wall_ms: int,
                     last_message: str = "",
                     model: str | None = None) -> AgentReply:
        """Parse the ``--json`` event stream. A shrug here would hide faults.

        Unparseable *lines* are tolerated — this is JSONL from a program that
        also writes human progress text — but a stream with no parseable event
        at all is malformed, and so is one that ends without a message.
        """
        events: list[dict[str, Any]] = []
        junk = 0
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                junk += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                junk += 1

        if not events:
            if rc != 0:
                # The refused-resume shape: exit 1, empty stdout, reason on
                # stderr. AgentFailed is right — the daemon retries a failed
                # resume on a fresh session, and that is the correct cure.
                raise AgentFailed(
                    f"{self.name} exited {rc} without emitting any events: "
                    f"{(stderr or '').strip()[-1000:] or '(no output)'}",
                    returncode=rc, stderr=stderr or "")
            raise AgentMalformedOutput(
                f"{self.name} exited 0 but emitted no JSON events on stdout "
                f"({junk} unparseable line(s)). "
                f"stderr: {(stderr or '').strip()[-500:] or '(empty)'}",
                sample=(stdout or "")[:2000])

        thread_id: str | None = None
        text: str | None = None
        usage: dict[str, int] | None = None
        turns = 0
        failure: str | None = None

        for event in events:
            kind = event.get("type")
            if kind == "thread.started":
                thread_id = event.get("thread_id") or thread_id
            elif kind == "turn.completed":
                turns += 1
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
            elif kind == "turn.failed":
                err = event.get("error")
                failure = (err.get("message") if isinstance(err, dict)
                           else str(err))
            elif kind == "error":
                failure = failure or str(event.get("message") or event)
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    # Last one wins: a turn can complete more than one message.
                    candidate = item.get("text")
                    if isinstance(candidate, str) and candidate.strip():
                        text = candidate

        if failure:
            raise AgentFailed(f"{self.name} reported an error: {failure[:1000]}",
                              returncode=rc, stderr=stderr or "")

        if text is None and last_message.strip():
            # The event stream lost the message but -o caught it. Take it, and
            # say so, because a stream that loses messages is worth knowing.
            log.warning("codex reply recovered from --output-last-message",
                        extra={"events": len(events), "junk_lines": junk})
            text = last_message

        if text is None or not text.strip():
            raise AgentMalformedOutput(
                f"{self.name} emitted {len(events)} event(s) but no "
                "agent_message, and --output-last-message was empty",
                sample=(stdout or "")[:2000])

        if rc != 0:
            raise AgentFailed(
                f"{self.name} exited {rc} after answering: "
                f"{(stderr or '').strip()[-500:] or '(no stderr)'}",
                returncode=rc, stderr=stderr or "")

        return AgentReply(
            text=text.strip(),
            agent=self.name,
            model=model,
            session_id=thread_id,
            # Subscription, not metered. See the class docstring: there is no
            # per-call price to report and one will not be invented.
            cost_usd=None,
            billing="subscription",
            usage=usage,
            duration_ms=None,
            num_turns=turns or None,
            wall_ms=wall_ms,
        )

    # -- the `luna` codex profile ------------------------------------------

    def profile_path(self, codex_home: Path | None = None) -> Path:
        """``$CODEX_HOME/luna.config.toml``.

        In codex 0.149.1 ``-p/--profile`` layers *a separate file* named after
        the profile on top of the user's config — it is not a ``[profiles.x]``
        table inside ``config.toml``. That is what makes writing this profile
        safe: it is a new file that does nothing at all unless someone types
        ``codex -p luna``, so the user's own codex setup is untouched.
        """
        return Path(codex_home or config.CODEX_HOME) / f"{self.PROFILE_NAME}.config.toml"

    def render_profile(self, system_prompt: str) -> str:
        """The profile file's contents, as valid TOML.

        Unlike the ``-c`` path this really is parsed as TOML, so the persona
        is emitted as a multi-line basic string with the two sequences that
        could end it early escaped.
        """
        body = system_prompt.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        return (
            "# Generated by lunad. `codex -p luna` boots Codex as Luna.\n"
            "# Regenerate with `luna codex-profile`; delete this file to undo.\n"
            "# Luna never edits ~/.codex/config.toml — this is a separate\n"
            "# profile-v2 file and is inert unless -p luna is passed.\n"
            f'{config.CODEX_PERSONA_KEY} = """\n{body}"""\n'
        )

    def write_profile(self, system_prompt: str,
                      codex_home: Path | None = None) -> Path:
        """Write the profile, keeping one backup of anything already there."""
        path = self.profile_path(codex_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".luna-backup")
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
        path.write_text(self.render_profile(system_prompt), encoding="utf-8")
        return path


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
    return cls(**kw)


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
