"""Workspace dispatch. ARCHITECTURE.md section 6.

Luna hands a task to a real agent session running in a terminal in her own
Hyprland special workspace, tracks the pid she created, captures what it wrote,
and reports back. The workspace is hidden, so the job does not interrupt what
the user is doing; ``luna peek`` brings it into view.

Four decisions worth the words:

**Luna spawns the terminal, Hyprland does not.** The obvious route is
``hyprctl dispatch exec``, which lets the compositor place the window with an
exec rule. It also means the compositor owns the pid: Luna could not honestly
claim the process, could not wait on it, and could not read its exit status.
The firewall in :mod:`lunad.safety` is only worth anything if the ledger is a
record of forks Luna actually performed, so ``foot`` is started with
``subprocess.Popen`` and placed with a *window* rule instead.

**The window rule is keyed on an app-id of Luna's own.** Omarchy's
``omarchy-launch-tui`` gives agent terminals the app-id ``org.omarchy.agent``,
and there are several of the user's own sessions carrying it on this machine
right now. A rule matching that class would have swept live sessions into
Luna's hidden workspace. Luna's terminals use ``org.omarchy.luna``.

**The rule is installed at runtime, not written into the config.** It is added
through ``hyprctl repl`` behind a Lua global so repeated dispatches do not
stack duplicates, and it disappears on the next Hyprland config reload — at
which point the next dispatch adds it again. Nothing under ``~/.config/hypr``
is edited, so there is nothing to undo and nothing to survive an
``omarchy update``.

**A job that cannot start yet is still a job.** ``[dispatch] max_parallel``
bounds how many agent sessions run at once, and the alternative to a queue was
refusing the dispatch — which reads as "Luna cannot do that" when the truth is
"not yet". So a job over the limit gets its directory, its prompt and its id
immediately, goes into ``jobs/`` in the ``queued`` state, appears in
``luna jobs`` and can be cancelled there; it is simply not spawned until a slot
frees. The only thing it does not have is a pid. Admission is re-decided every
time a slot frees and every time the setting changes, and the limit is read at
that moment rather than captured, so lowering it below the number of running
jobs stops *admitting* without killing anything: the count drains on its own.

The exact Hyprland incantation is documented in :class:`Hyprland`; it was not
the obvious one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (agent as agent_mod, audit as audit_mod, config,
               confirm as confirm_mod, persona, safety,
               settings as settings_mod)

log = logging.getLogger("lunad.dispatch")


class DispatchError(Exception):
    kind = "DispatchError"

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": str(self)}


class DispatchUnavailable(DispatchError):
    kind = "DispatchUnavailable"


# =========================================================================
# The compositor
# =========================================================================


class Hyprland:
    """The three compositor calls dispatch needs, and the syntax that works.

    Hyprland 0.56 on this machine takes a **Lua** config, and ``hyprctl`` wraps
    its arguments in ``return hl.dispatch(<args>)`` before evaluating them. The
    consequence caught a previous attempt out:

        hyprctl dispatch exec "[float] foo"
          -> error: ']' expected near ';'    (the Lua parser, not Hyprland)

    Neither ``--`` nor quoting rescues it, because the text never reaches a
    shell — it reaches a Lua parser. The form that works is the dispatcher's
    own Lua function, with the window rules still inside the string:

        hyprctl dispatch 'hl.dsp.exec_cmd("[workspace special:luna silent] foo")'

    Dispatch does not use that form (see the module docstring: Luna needs to
    own the pid), but ``toggle_special`` and the runtime window rule are the
    same idea:

        hl.dsp.workspace.toggle_special("luna")
        hl.window_rule({ match = { class = "^org\\.omarchy\\.luna$" },
                         workspace = "special:luna silent" })

    ``hl.window_rule``'s table shape was taken from Omarchy's own
    ``/usr/share/omarchy/default/hypr/helpers.lua``, not guessed: it accepts
    almost any table without complaint, so an experiment could not have told
    the right key names from the wrong ones.
    """

    def __init__(self, hyprctl: str | None = None,
                 workspace: str | None = None,
                 app_id: str | None = None) -> None:
        # Late read, like the terminal and the notifier. `hyprctl repl` installs
        # window rules and `hyprctl dispatch` moves workspaces: a test that
        # built one of these unstubbed would rearrange the user's live desktop,
        # and a signature default cannot be patched to stop it.
        self.hyprctl = hyprctl or config.HYPRCTL_BIN
        # Kept as overrides. With neither given, both come from `[dispatch]`
        # on every read, so a workspace or app-id changed in the GUI applies
        # to the next dispatch without a restart. Windows already open keep
        # the app-id they were born with — an app-id is set at map time and
        # cannot be changed afterwards — so a change moves new jobs only.
        self._workspace_override = workspace
        self._app_id_override = app_id

    @property
    def workspace(self) -> str:
        if self._workspace_override is not None:
            return self._workspace_override
        return (str(settings_mod.get("dispatch.workspace") or "").strip()
                or config.LUNA_WORKSPACE)

    @property
    def app_id(self) -> str:
        if self._app_id_override is not None:
            return self._app_id_override
        return (str(settings_mod.get("dispatch.app_id") or "").strip()
                or config.LUNA_APP_ID)

    @property
    def rule_global(self) -> str:
        """The Lua guard's name, keyed on what the rule actually says.

        A single fixed global was right while the app-id was a constant. Now
        that both halves are settings, a fixed name would let the guard from
        the *old* rule suppress installing the new one, and every job after a
        change would open on the active workspace instead — visibly, in the
        user's face, and only until the next Hyprland config reload, which is
        the hardest kind of bug to catch. Keying the guard on a digest of the
        pair means changing either one installs a rule again.
        """
        digest = hashlib.sha256(
            f"{self.app_id}\0{self.workspace}".encode()).hexdigest()[:12]
        return f"luna_workspace_rule_{digest}"

    # -- plumbing --------------------------------------------------------

    def _run(self, args: list[str], timeout: float = 5.0) -> tuple[int, str]:
        try:
            proc = subprocess.run([self.hyprctl, *args], capture_output=True,
                                  text=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise DispatchUnavailable(
                f"{self.hyprctl} not found; is this a Hyprland session?"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DispatchUnavailable(
                f"{self.hyprctl} {' '.join(args[:1])} timed out after {timeout}s"
            ) from exc
        return proc.returncode, (proc.stdout or proc.stderr or "").strip()

    def available(self) -> tuple[bool, str]:
        if not shutil.which(self.hyprctl):
            return False, f"{self.hyprctl} is not on PATH"
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return False, ("HYPRLAND_INSTANCE_SIGNATURE is unset; lunad cannot "
                           "reach the compositor. systemd user units inherit it "
                           "only if the session imported it.")
        try:
            rc, out = self._run(["version"])
        except DispatchUnavailable as exc:
            return False, str(exc)
        if rc != 0:
            return False, f"hyprctl version exited {rc}: {out[:200]}"
        return True, out.splitlines()[0][:120] if out else "hyprland"

    def _json(self, what: str) -> Any:
        rc, out = self._run([what, "-j"])
        if rc != 0:
            raise DispatchUnavailable(f"hyprctl {what} exited {rc}: {out[:200]}")
        try:
            return json.loads(out or "null")
        except json.JSONDecodeError as exc:
            raise DispatchUnavailable(
                f"hyprctl {what} -j did not return JSON: {out[:200]}"
            ) from exc

    # -- what dispatch actually needs -------------------------------------

    def ensure_workspace_rule(self) -> str:
        """Install the window rule that places Luna's terminals, once.

        Guarded by a Lua global rather than by a flag in Python: the guard has
        to survive a lunad restart, and it has to *stop* guarding when Hyprland
        reloads its config and forgets the rule. A global in the compositor's
        own Lua state does both for free.
        """
        klass = "^" + re.escape(self.app_id) + "$"
        guard = self.rule_global
        lua = (
            f"if not _G.{guard} then "
            f'hl.window_rule({{ match = {{ class = "{_lua_escape(klass)}" }}, '
            f'workspace = "special:{self.workspace} silent" }}); '
            f"_G.{guard} = true; return \"added\" end "
            'return "present"'
        )
        rc, out = self._run(["repl", lua])
        if rc != 0 or out.startswith("error:"):
            raise DispatchUnavailable(
                f"could not install the workspace rule: {out[:300]}"
            )
        return out or "present"

    def toggle_special(self) -> bool:
        """Show or hide the workspace. Returns whether it is now visible."""
        rc, out = self._run(
            ["dispatch", f'hl.dsp.workspace.toggle_special("{self.workspace}")'])
        if rc != 0 or out.startswith("error:"):
            raise DispatchUnavailable(f"could not toggle the workspace: {out[:300]}")
        # Hyprland answers "ok" before the animation finishes; give it a beat
        # so the reported state is the one the user will actually see.
        time.sleep(0.25)
        return self.special_visible()

    def special_visible(self) -> bool:
        want = f"special:{self.workspace}"
        for monitor in self._json("monitors") or []:
            if (monitor.get("specialWorkspace") or {}).get("name") == want:
                return True
        return False

    def workspace_exists(self) -> bool:
        want = f"special:{self.workspace}"
        return any(ws.get("name") == want for ws in self._json("workspaces") or [])

    def windows(self) -> list[dict[str, Any]]:
        want = f"special:{self.workspace}"
        return [c for c in self._json("clients") or []
                if (c.get("workspace") or {}).get("name") == want]

    def state(self) -> dict[str, Any]:
        ok, detail = self.available()
        if not ok:
            return {"available": False, "detail": detail,
                    "workspace": f"special:{self.workspace}",
                    "app_id": self.app_id}
        try:
            return {"available": True, "detail": detail,
                    "workspace": f"special:{self.workspace}",
                    "app_id": self.app_id,
                    "exists": self.workspace_exists(),
                    "visible": self.special_visible(),
                    "windows": len(self.windows())}
        except DispatchUnavailable as exc:
            return {"available": False, "detail": str(exc),
                    "workspace": f"special:{self.workspace}",
                    "app_id": self.app_id}


def _lua_escape(text: str) -> str:
    """Escape for a Lua double-quoted string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# =========================================================================
# A job
# =========================================================================

_STATES = ("queued", "running", "finished", "failed", "cancelled")


@dataclass
class Job:
    """One dispatched task.

    ``started`` is when the job was *accepted* — the moment the directory and
    the prompt were written — and ``admitted`` is when it was actually spawned.
    They are the same instant for a job that was not held behind
    ``max_parallel``, and the gap between them is the wait. Keeping both means
    ``luna jobs`` can sort by when the user asked without reporting an hour of
    queueing as an hour of work.
    """

    id: str
    task: str
    to: str = "worker"
    state: str = "running"
    pid: int | None = None
    started: float = field(default_factory=time.time)
    admitted: float | None = None
    finished: float | None = None
    exit_code: int | None = None
    note: str = ""
    dir: Path | None = None

    def to_dict(self, output: str | None = None) -> dict[str, Any]:
        d = {
            "id": self.id,
            "task": self.task,
            "to": self.to,
            "state": self.state,
            "pid": self.pid,
            "started": round(self.started, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started)),
            "admitted": round(self.admitted, 3) if self.admitted else None,
            "queued_s": round((self.admitted or time.time()) - self.started, 1),
            "finished": round(self.finished, 3) if self.finished else None,
            # Time the *agent* has had, not time the request has existed. A job
            # still in the queue, or cancelled before it ever ran, has had
            # none: reporting the wait here would make `luna jobs` claim work
            # that never happened.
            "elapsed_s": (0.0 if self.admitted is None else
                          round((self.finished or time.time()) - self.admitted, 1)),
            "exit_code": self.exit_code,
            "note": self.note,
            "dir": str(self.dir) if self.dir else None,
        }
        if output is not None:
            d["output"] = output
        return d

    def read_output(self, limit: int = config.JOB_OUTPUT_MAX_CHARS) -> str:
        if self.dir is None:
            return ""
        text = _read(self.dir / "output.txt", limit)
        if not text.strip():
            text = _read(self.dir / "stderr.txt", limit)
        return text


def _reap_notify(proc: subprocess.Popen) -> None:
    """Wait on the toast so it never becomes a zombie, then release the pid.

    ``reap_after`` keeps waiting in the background past its own 15s bound
    rather than giving up: a ``notify-send`` that never exits must not leak a
    permanent zombie for the rest of the daemon's life.
    """
    try:
        safety.reap_after(proc, timeout=15)
    except Exception:  # noqa: BLE001 - reaping a toast must not raise
        pass


def _read(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > limit:
        return text[:limit] + f"\n[... {len(text) - limit} more characters in "\
                              f"{path} ...]"
    return text


# =========================================================================
# The dispatcher
# =========================================================================


@dataclass
class _Pending:
    """A job accepted but not yet spawned, and what spawning it will need.

    The confirmation decisions travel with it because they were made when the
    user asked, not when the slot freed: an entry claiming a job was confirmed
    at admission time would misdate the one thing in the log that says a human
    agreed to it.
    """

    job: Job
    timeout: float
    confirmed: list[str] | None = None


class Dispatcher:
    """Spawns jobs, owns their pids, and answers ``jobs`` / ``peek``."""

    def __init__(self, *, jobs_dir: Path | None = None,
                 hypr: Hyprland | None = None,
                 audit: audit_mod.AuditLog | None = None,
                 terminal: str | None = None,
                 app_id: str | None = None,
                 agent_bin: str | None = None,
                 agent_name: str | None = None,
                 sol_memory_dir: Path = config.SOL_MEMORY_DIR,
                 confirm: confirm_mod.ConfirmBroker | None = None,
                 notify_bin: str | None = None,
                 spawn: Any = None) -> None:
        # Late read, and for a sharper reason than the terminal's: since
        # `collect()` exists, this directory is one this object *deletes* from.
        # A signature default is bound at import and cannot be patched, so a
        # test that forgot to pass one would have had the collector walking the
        # user's real jobs tree. See tests/_support.py.
        self.jobs_dir = Path(jobs_dir) if jobs_dir is not None else config.JOBS_DIR
        try:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DispatchUnavailable(
                f"cannot use {self.jobs_dir} as the jobs directory: {exc}"
            ) from exc
        self.hypr = hypr if hypr is not None else Hyprland(app_id=app_id)
        self.audit = audit if audit is not None else audit_mod.audit()
        # Resolved here, not in the signature default: a default is
        # bound at import time, so `config.TERMINAL_BIN = ...` would
        # have no effect on it. The test suite patches exactly that
        # name to keep a stray Dispatcher from opening a real window
        # on the user's desktop, and needs the late read to do it.
        self.terminal = terminal or config.TERMINAL_BIN
        # Resolved here and not in the signature, for exactly the reason the
        # terminal is: a default is bound at import, so `config.NOTIFY_BIN =
        # ...` would have no effect on it. The suite patches that name to keep
        # a finished job from putting a real toast on the user's desktop --
        # and it did, ten of them, the first time this was wired without the
        # late read.
        self.notify_bin = notify_bin or config.NOTIFY_BIN
        self._agent_bin = agent_bin
        # Which CLI's flags the runner script is written in. Read from
        # Omarchy's default rather than hard-coded, because `claude -p` and
        # `codex exec -` share not one flag between them.
        self.agent_name = (agent_name or agent_mod.read_default_agent()).lower()
        self.adapter = agent_mod.get_adapter(self.agent_name)
        self.sol_memory_dir = Path(sol_memory_dir)
        # The one place in the daemon where a confirmation is genuinely
        # enforced rather than requested: nothing is spawned until the task
        # text has been through the classifier.
        self.confirm = (confirm if confirm is not None
                        else confirm_mod.ConfirmBroker(audit=self.audit))
        self.spawn = spawn or safety.spawn
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        # Job ids admitted but not yet holding a Popen. The slot has to be
        # reserved at the *decision*, not at the spawn: the daemon answers each
        # connection on its own thread, so two simultaneous dispatches would
        # otherwise both look at an empty `_procs` -- the first one's process
        # does not exist until `_start` returns -- and both be admitted past a
        # limit of one.
        self._admitting: set[str] = set()
        self._queue: list[_Pending] = []
        self._watchers: list[threading.Thread] = []
        self._lock = threading.Lock()
        # Set by close(). Admission checks it, so a watcher that finishes
        # during shutdown cannot start a fresh job — and a fresh job means a
        # fresh watcher, which close() has already snapshotted the list of and
        # would never join. That is exactly the leak the bounded drain exists
        # to prevent.
        self._closing = threading.Event()
        self._gc: threading.Thread | None = None

    # -- helpers ---------------------------------------------------------

    @property
    def max_parallel(self) -> int:
        """`[dispatch] max_parallel`, read at every admission decision.

        Never captured: the whole point is that raising it lets waiting work
        through and lowering it stops admitting, both without a restart, and a
        value read once at construction would do neither.
        """
        try:
            wanted = int(settings_mod.get("dispatch.max_parallel",
                                          config.DISPATCH_MAX_PARALLEL))
        except (TypeError, ValueError):
            wanted = config.DISPATCH_MAX_PARALLEL
        return max(1, wanted)

    @property
    def retention_days(self) -> int:
        """`[dispatch] job_retention_days`. Zero or less means never collect."""
        try:
            return int(settings_mod.get("dispatch.job_retention_days",
                                        config.JOB_RETENTION_DAYS))
        except (TypeError, ValueError):
            return config.JOB_RETENTION_DAYS

    def agent_bin(self) -> str:
        if self._agent_bin:
            return self._agent_bin
        return self.adapter.binary()

    def available(self) -> tuple[bool, str]:
        if not shutil.which(self.terminal):
            return False, f"{self.terminal} is not on PATH"
        try:
            self.agent_bin()
        except agent_mod.AgentUnavailable as exc:
            return False, str(exc)
        return self.hypr.available()

    @property
    def app_id(self) -> str:
        """One source of truth, and it is the compositor handle's.

        Two copies of this used to exist — one here, one on ``Hyprland`` — and
        an injected fake compositor made them disagree: the window rule matched
        one class while ``foot`` was launched with the other, which places
        every job on the active workspace. Ask the object that writes the rule.
        """
        return self.hypr.app_id

    def announce(self, job: Job) -> str:
        """One line, as the persona spec requires: who, and why.

        Composed here rather than asked of a model. An announcement that costs
        an API call and 4 seconds would not get made.
        """
        who = (settings_mod.specialist_name() if job.to == "sol"
               else "a worker")
        why = ("depth-first technical work, reporting back to me"
               if job.to == "sol" else "grunt work I would only be in the way of")
        return (f"Enrolled {who} on job {job.id} — {why}. "
                f"`luna peek` to watch, `luna jobs` for the result.")

    # -- dispatch --------------------------------------------------------

    def dispatch(self, task: str, to: str = "worker", *,
                 timeout: float = config.DISPATCH_TIMEOUT_S,
                 linger: float = config.DISPATCH_LINGER_S,
                 sol_memory_block: str = "",
                 estimate_seconds: float | None = None,
                 estimate_usd: float | None = None) -> Job:
        task = (task or "").strip()
        if not task:
            raise DispatchError("dispatch needs a non-empty task")
        to = (to or "worker").strip().lower()
        if to not in ("worker", "sol"):
            raise DispatchError(
                f"unknown delegate {to!r}; Luna dispatches to 'sol' or 'worker'")

        ok, detail = self.available()
        if not ok:
            raise DispatchUnavailable(
                f"cannot dispatch: {detail}. Nothing was spawned.")

        # Before anything forks. A refusal here raises ConfirmDenied and the
        # terminal is never started, which is the only part of the
        # confirmation system that is a real boundary rather than an
        # instruction to a well-behaved agent.
        # `timeout` is a ceiling, not an estimate — every dispatch carries the
        # same one-hour default, so gating `long_job` on it would ask about
        # every job ever dispatched and train the user to click through. The
        # class only fires on an estimate a caller actually made.
        decisions = self.confirm.gate(
            task, why=f"dispatch to {to}", actor="luna",
            seconds=estimate_seconds, usd=estimate_usd)

        job_id = uuid.uuid4().hex[:8]
        job_dir = self.jobs_dir / job_id
        # `parents=True` would rebuild the whole tree, and there is one caller
        # that must never be allowed to: a watcher that won the race in
        # `_admit_next` and reached here after `close()` gave up joining it.
        # Under test that resurrects the temporary tree teardown has already
        # deleted -- the stray `/tmp/luna-test-*` this suite has a CI check
        # for, and the one that failed twice on the 3.13 runner and nowhere
        # else. In production the state directory always exists, so this
        # cannot fire; when it does, `_admit_next` already treats
        # `DispatchUnavailable` as "this job could not start" and moves on.
        if not self.jobs_dir.parent.is_dir():
            raise DispatchUnavailable(
                f"{self.jobs_dir.parent} has gone; refusing to recreate it")
        job_dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, task=task, to=to, dir=job_dir)

        ask_classes = [name for name in confirm_mod.CLASSES
                       if self.confirm.policy(name) == confirm_mod.ASK]
        system_prompt = persona.build_dispatch_system_prompt(
            to=to, memory_block=sol_memory_block,
            memory_dir=str(self.sol_memory_dir) if to == "sol" else "",
            job_dir=str(job_dir),
            confirm_block=persona.build_confirm_block(
                ask_classes, cli=str(config.PROJECT_DIR / "bin" / "luna")))

        (job_dir / "task.txt").write_text(task + "\n", encoding="utf-8")
        (job_dir / "system.txt").write_text(system_prompt, encoding="utf-8")
        runner = job_dir / "run.sh"
        runner.write_text(self._runner_script(job, timeout, linger),
                          encoding="utf-8")
        runner.chmod(0o700)

        # Everything above happens whether or not there is a slot: a queued job
        # is a real job with a real directory, and the work of composing its
        # prompt is done once, now, while the caller is still here to be told
        # about a failure.
        pending = _Pending(job=job, timeout=timeout,
                           confirmed=[d.action for d in decisions] or None)
        limit = self.max_parallel
        with self._lock:
            self._jobs[job_id] = job
            running, waiting = self._taken(), len(self._queue)
            # FIFO even when a slot is free: admitting a newcomer past jobs
            # that are already waiting would turn a queue into a lottery.
            if running >= limit or waiting:
                job.state = "queued"
                self._queue.append(pending)
            else:
                self._admitting.add(job_id)
        if job.state == "queued":
            job.note = (f"queued: {running} of {limit} running, "
                        f"{waiting} already waiting")
            self._write_job(job)
            self.audit.append(
                "dispatch.queued", ok=True, job_id=job_id, to=to,
                why=task[:500], job_dir=str(job_dir), position=waiting + 1,
                running=running, max_parallel=limit,
                confirmed=pending.confirmed,
                undo={"what": "drop the job before it starts",
                      "cmd": ["luna", "jobs", "--cancel", job_id],
                      "valid_while": "the job has not been admitted yet"})
            log.info("queued", extra={"job_id": job_id, "to": to,
                                      "running": running, "waiting": waiting,
                                      "max_parallel": limit})
            return job
        return self._start(pending)

    def _taken(self) -> int:
        """Slots in use. Call with the lock held."""
        return len(self._procs) + len(self._admitting)

    def _start(self, pending: _Pending) -> Job:
        """Spawn an accepted job. Called at dispatch time, or when a slot frees.

        The caller has already reserved the slot in ``_admitting``; this
        releases the reservation either way, into ``_procs`` on success and
        into nothing on failure.

        Raises :class:`DispatchUnavailable` if the terminal will not start —
        the job is marked ``failed`` and written to disk first, because from
        the queue there is no caller left to raise at and the record is the
        only thing that will be read afterwards.

        The ``_admitting`` reservation is released in a ``finally`` around the
        whole body: it used to be released only on the ``OSError`` branch and
        the success path, so anything else raised by ``spawn`` (or by the
        bookkeeping after it) left the job id in ``_admitting`` forever,
        shrinking ``max_parallel`` by one until the daemon restarted.
        """
        job = pending.job
        task, job_id, to = job.task, job.id, job.to
        job_dir = job.dir
        try:
            # Best effort: without the rule the job still runs, it just opens
            # the window where the user is looking. Say so rather than failing.
            placed = True
            try:
                rule = self.hypr.ensure_workspace_rule()
            except DispatchUnavailable as exc:
                placed = False
                rule = f"unavailable: {exc}"
                log.warning("workspace rule not installed; the window will "
                            "open on the active workspace",
                            extra={"detail": str(exc)})

            argv = [self.terminal, "--app-id", self.app_id,
                    "--title", f"luna job {job_id} → {to}",
                    "--", "/bin/bash", str(job_dir / "run.sh")]
            try:
                proc = self.spawn(argv, kind="dispatch", job_id=job_id,
                                  durable=True,
                                  note=f"dispatch to {to}: {task[:80]}",
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  cwd=str(job_dir))
            except OSError as exc:
                job.state = "failed"
                job.note = f"could not start {self.terminal}: {exc}"
                job.finished = time.time()
                self._write_job(job)
                self.audit.append("dispatch.failed", ok=False, job_id=job_id,
                                  why=task[:200], reason=str(exc))
                raise DispatchUnavailable(job.note) from exc

            job.pid = proc.pid
            job.admitted = time.time()
            job.state = "running"
            job.note = ("in special workspace" if placed
                        else "workspace rule unavailable; window is on the "
                             "active workspace")
            with self._lock:
                self._jobs[job_id] = job
                self._procs[job_id] = proc
            self._write_job(job)

            waited = round(job.admitted - job.started, 1)
            self.audit.append(
                "dispatch.spawn", ok=True, job_id=job_id, to=to, pid=proc.pid,
                why=task[:500], cmd=argv, job_dir=str(job_dir),
                workspace=f"special:{self.hypr.workspace}", rule=rule,
                # Only when it actually waited. A `queued_s: 0.0` on every job
                # would be noise on the line a reader is scanning for the pid.
                queued_s=waited if waited >= 0.1 else None,
                confirmed=pending.confirmed,
                undo={"what": "stop the dispatched job",
                      "cmd": ["luna", "jobs", "--cancel", job_id],
                      "valid_while": "the job is still running"})
            log.info("dispatched", extra={"job_id": job_id, "to": to,
                                          "pid": proc.pid, "placed": placed,
                                          "queued_s": waited})

            watcher = threading.Thread(
                target=self._watch, args=(job, proc, pending.timeout),
                daemon=True, name=f"luna-job-{job_id}")
            with self._lock:
                self._watchers = [t for t in self._watchers if t.is_alive()]
                self._watchers.append(watcher)
            watcher.start()
            return job
        finally:
            with self._lock:
                self._admitting.discard(job_id)

    # -- the admission gate, `[dispatch] max_parallel` --------------------

    def _admit_next(self) -> Job | None:
        """Start the oldest waiting job, if there is now room for it.

        The limit is re-read here rather than passed in, so a job that finished
        while the user was lowering ``max_parallel`` does not hand its slot on.
        A job that cannot be spawned at all does not block the queue: it is
        recorded as failed and the next one is tried.
        """
        while not self._closing.is_set():
            limit = self.max_parallel
            with self._lock:
                # `_closing` is re-checked here, under the lock close() also
                # takes, so a shutdown that begins mid-loop wins the tie.
                if (self._closing.is_set() or not self._queue
                        or self._taken() >= limit):
                    return None
                pending = self._queue.pop(0)
                self._admitting.add(pending.job.id)
            try:
                return self._start(pending)
            except DispatchUnavailable as exc:
                log.warning("a queued job could not be started",
                            extra={"job_id": pending.job.id,
                                   "detail": str(exc)})
        return None

    def admit_ready(self) -> list[str]:
        """Start everything the current limit now has room for.

        Called when a slot frees and when ``max_parallel`` changes on disk —
        raising the limit has to release waiting work immediately, or the
        setting only appears to take effect when the next job happens to end.
        """
        started: list[str] = []
        while True:
            job = self._admit_next()
            if job is None:
                return started
            started.append(job.id)

    def queued(self) -> list[Job]:
        with self._lock:
            return [p.job for p in self._queue]

    def _runner_script(self, job: Job, timeout: float, linger: float) -> str:
        """The script the terminal runs.

        ``pipefail`` matters: the agent's status is what the job's status is,
        and without it the pipeline reports ``tee``'s. The exit code is written
        to a file as well as returned, because ``luna jobs`` has to answer
        after a daemon restart, when there is no ``Popen`` left to ask.
        """
        job_dir = shlex.quote(str(job.dir))
        # Sol gets his own memory namespace on the allowed-directory list, and
        # nothing else does: an anonymous worker has no business there.
        add_dirs = ((shlex.quote(str(self.sol_memory_dir)),)
                    if job.to == "sol" else ())
        # The agent's own adapter writes its own command line. dispatch knows
        # about job directories, timeouts and pipefail; it does not know, and
        # must not know, which flag carries a system prompt this week.
        agent_cmd = " \\\n    ".join(agent_mod.shell_lines(self.adapter.dispatch_argv(
            job_dir='"$JOB"',
            system_file='"$JOB/system.txt"',
            add_dirs=add_dirs,
            binary=shlex.quote(self.agent_bin()),
        )))
        return f"""#!/bin/bash
# Generated by lunad for job {job.id}. Safe to delete with the job directory.
set -u
set -o pipefail
JOB={job_dir}
cd "$JOB" || exit 78

printf '\\033[1m── luna job {job.id} → {job.to} ──\\033[0m\\n'
cat "$JOB/task.txt"
printf '\\033[2m%s\\033[0m\\n' '────────────────────────────────────────────'

start=$(date +%s)
timeout --signal=TERM --kill-after=20 {int(timeout)} \\
  {agent_cmd} \\
    < "$JOB/task.txt" \\
    2> "$JOB/stderr.txt" \\
  | tee "$JOB/output.txt"
rc=$?
printf '%s' "$rc" > "$JOB/exit"
printf '%s' "$(( $(date +%s) - start ))" > "$JOB/elapsed"

if [ -s "$JOB/stderr.txt" ]; then
  printf '\\033[2m%s\\033[0m\\n' '--- stderr ---'
  cat "$JOB/stderr.txt"
fi
printf '\\033[2m%s\\033[0m\\n' '────────────────────────────────────────────'
printf 'exit %s — this window closes in {int(linger)}s\\n' "$rc"
sleep {int(linger)}
exit "$rc"
"""

    # -- lifecycle -------------------------------------------------------

    def _watch(self, job: Job, proc: subprocess.Popen, timeout: float) -> None:
        """Wait for the terminal, record the outcome, release the pid.

        The wait is generous: the script has its own ``timeout`` around the
        agent, so this one only catches a terminal that will not exit at all.
        """
        try:
            proc.wait(timeout=timeout + config.DISPATCH_LINGER_S + 120)
        except subprocess.TimeoutExpired:
            job.note = "terminal outlived the job timeout and was terminated"
            try:
                safety.terminate(proc, reason=f"job {job.id} overran")
            except safety.SignalRefused:
                log.exception("refused to terminate an overrunning job")
        finally:
            # `proc.wait()` above already reaped it on the ordinary path. On
            # the timeout path `terminate()` may or may not have confirmed
            # death (a wedged terminal can outlive even SIGKILL's bounded
            # wait), so this has to keep trying rather than just forgetting a
            # pid that might still be a zombie — `reap_after` does, in the
            # background, for as long as it takes.
            safety.reap_after(proc, timeout=5.0)

        job.finished = time.time()
        job.exit_code = self._exit_code(job, proc)
        job.state = "finished" if job.exit_code == 0 else "failed"
        with self._lock:
            self._procs.pop(job.id, None)
        self._write_job(job)
        output = job.read_output(limit=2000)
        self.audit.append(
            "dispatch.finish", ok=(job.exit_code == 0), job_id=job.id,
            to=job.to, pid=job.pid, exit_code=job.exit_code,
            elapsed_s=round(job.finished - (job.admitted or job.started), 1),
            why=job.task[:200], output_chars=len(job.read_output()),
            output_head=output[:400])
        log.info("job finished", extra={"job_id": job.id,
                                        "exit_code": job.exit_code,
                                        "state": job.state})
        # The slot is free before the toast is sent: notifying is best-effort
        # and can take a moment, and the next job should not wait on a desktop
        # nicety. Nothing is admitted once close() has begun.
        #
        # Guarded: admitting the next job can itself raise (a queued job's own
        # `_start` failing in a way `_admit_next` does not already catch), and
        # that must not abort the bookkeeping for *this* job — the finish
        # audit entry is already written above, but the toast below is still
        # this job's, not the next one's.
        try:
            self._admit_next()
        except Exception:  # noqa: BLE001 - this job's own finish must still fire
            log.exception("failed to admit the next queued job",
                          extra={"job_id": job.id})
        self.notify_finished(job)

    # -- `[ui] notify_on_finish` -----------------------------------------

    def notify_finished(self, job: Job) -> bool:
        """Tell the user a dispatched job ended. Returns whether it notified.

        The window is on a hidden workspace by design, so without this the
        only way to learn a job finished is to go and look. Gated on
        `[ui] notify_on_finish`, read here rather than at construction so the
        toggle takes effect on the very next job.

        Failure to notify is logged and swallowed. A missing
        ``omarchy-notification-send`` is a desktop that cannot show a toast,
        not a job that did not finish, and it must never turn a completed job
        into a failed one.
        """
        if not bool(settings_mod.get("ui.notify_on_finish", True)):
            return False
        who = settings_mod.assistant_name()
        ok = job.exit_code == 0
        headline = f"{who}: job {job.id} {'finished' if ok else job.state}"
        body = job.task.strip().replace("\n", " ")[:160] or "(no task text)"
        if not ok and job.exit_code is not None:
            body = f"exit {job.exit_code} — {body}"
        argv = [self.notify_bin, "--app-name", "Jarvis",
                "-u", "normal" if ok else "critical",
                "-g", "󰄬" if ok else "󰀦",
                headline, body]
        try:
            proc = safety.spawn(
                argv, kind="job-notify", durable=False,
                note=f"job {job.id} finished",
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except (OSError, safety.SignalRefused) as exc:
            log.warning("could not send the job-finished notification",
                        extra={"detail": str(exc), "bin": self.notify_bin,
                               "job_id": job.id})
            return False
        threading.Thread(target=_reap_notify, args=(proc,), daemon=True,
                         name=f"luna-job-notify-{job.id}").start()
        return True

    def _exit_code(self, job: Job, proc: subprocess.Popen | None) -> int | None:
        """The script's own record wins over the terminal's status.

        ``foot`` does propagate its child's exit code here (verified), but the
        file is the thing that survives a daemon restart, so it is read first.
        """
        if job.dir is not None:
            raw = _read(job.dir / "exit", 32).strip()
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    pass
        return proc.returncode if proc is not None else None

    def cancel(self, job_id: str) -> bool:
        """Stop a job. A queued one is dropped; a running one is signalled.

        Cancelling from the queue is the easy half and the important half: it
        is the only way to take back a dispatch that has not started, and it
        signals nothing at all, so the firewall is never involved.
        """
        with self._lock:
            proc = self._procs.get(job_id)
            job = self._jobs.get(job_id)
            pending = next((p for p in self._queue if p.job.id == job_id), None)
            if pending is not None:
                self._queue.remove(pending)
        if job is None:
            return False
        if pending is not None and proc is None:
            job.state = "cancelled"
            job.finished = time.time()
            job.note = "cancelled before it started; nothing was spawned"
            self._write_job(job)
            self.audit.append("dispatch.cancel", ok=True, job_id=job_id,
                              why="cancel requested", was="queued",
                              queued_s=round(job.finished - job.started, 1),
                              note="never admitted; no process existed")
            log.info("dropped a queued job", extra={"job_id": job_id})
            return True
        if proc is None:
            return False
        try:
            stopped = safety.terminate(proc, reason=f"job {job_id} cancelled")
        except safety.SignalRefused as exc:
            self.audit.append("dispatch.cancel", ok=False, job_id=job_id,
                              reason=str(exc), why="cancel requested")
            raise
        job.state = "cancelled"
        job.note = "cancelled by the user"
        self._write_job(job)
        self.audit.append("dispatch.cancel", ok=True, job_id=job_id,
                          pid=job.pid, why="cancel requested")
        return stopped

    def peek(self) -> dict[str, Any]:
        visible = self.hypr.toggle_special()
        self.audit.append("workspace.peek", ok=True,
                          why="user asked to see the workspace",
                          workspace=f"special:{self.hypr.workspace}",
                          visible=visible,
                          undo={"what": "hide it again",
                                "cmd": ["luna", "peek"],
                                "valid_while": "always — peek is a toggle"})
        return {"workspace": f"special:{self.hypr.workspace}",
                "visible": visible,
                "windows": len(self.hypr.windows()) if visible else None}

    # -- listing ---------------------------------------------------------

    def _write_job(self, job: Job) -> None:
        if job.dir is None:
            return
        try:
            (job.dir / "job.json").write_text(
                json.dumps(job.to_dict(), ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError as exc:
            log.warning("could not write job.json",
                        extra={"job_id": job.id, "detail": str(exc)})

    def jobs(self, limit: int = config.JOB_LIST_LIMIT,
             with_output: bool = False) -> list[dict[str, Any]]:
        """Newest first, from disk, so the list survives a daemon restart.

        A job whose daemon died mid-run is reported as ``orphaned`` rather than
        left claiming to be running: the pid check is the same start-time check
        the firewall uses, so a recycled pid cannot make a dead job look live.
        """
        found: dict[str, dict[str, Any]] = {}
        for entry in sorted(self.jobs_dir.glob("*/job.json")):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not data.get("id"):
                continue
            found[str(data["id"])] = data
        with self._lock:
            live = {jid: job for jid, job in self._jobs.items()}
        for jid, job in live.items():
            found[jid] = job.to_dict()
        out = sorted(found.values(), key=lambda d: d.get("started") or 0,
                     reverse=True)[:max(1, limit)]
        for data in out:
            if data.get("state") == "running" and jid_is_dead(data, live):
                data["state"] = "orphaned"
                data["note"] = ("the daemon that started this job is gone; "
                                "its outcome is whatever is in the job directory")
            elif data.get("state") == "queued" and jid_is_dead(data, live):
                # A queue lives in one daemon's memory and nowhere else, so a
                # `queued` directory belonging to no live dispatcher is not
                # waiting for anything. Saying "queued" would be a promise
                # nothing is going to keep.
                data["state"] = "orphaned"
                data["note"] = ("the daemon that accepted this job is gone; "
                                "it was never started and nothing ran")
            if with_output and data.get("dir"):
                job = Job(id=str(data["id"]), task=str(data.get("task", "")),
                          dir=Path(str(data["dir"])))
                data["output"] = job.read_output()
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = [j.to_dict() for j in self._jobs.values()
                       if j.state == "running"]
            waiting = [p.job.to_dict() for p in self._queue]
        return {"running": running, "queued": waiting,
                "max_parallel": self.max_parallel,
                "retention_days": self.retention_days,
                "jobs_dir": str(self.jobs_dir),
                "workspace": self.hypr.state()}

    # -- `[dispatch] job_retention_days` ---------------------------------

    def collect(self, now: float | None = None) -> dict[str, Any]:
        """Delete job directories that are past the retention window.

        These directories are the only record of what a dispatched agent did,
        so the policy is narrow and stated here rather than inferred from the
        code:

        **A job is aged from when it stopped, not from when it started.** The
        stamp is ``finished`` out of ``job.json``, falling back to ``started``
        for a directory that has no finish recorded, and to the file's mtime if
        the JSON is unreadable. Retention answers "how long is the *record*
        kept", and a job that ran for six hours has a record that begins when
        it ends.

        **Nothing running or queued is ever collected, at any age.** Neither is
        anything this dispatcher still holds. A job whose daemon died —
        ``orphaned`` in ``luna jobs`` — *is* collectable once past the window:
        it will never finish, and keeping it forever would mean one crash
        pinning a directory for the life of the machine.

        **Zero means never.** ``job_retention_days = 0`` collects nothing.
        That is the footgun this method exists to disarm: read as a duration,
        zero days would mean "delete everything", which is the one thing a user
        typing the smallest allowed number cannot possibly want.

        Each deletion gets its own audit entry and none of them carries an
        ``undo``, because there is not one.
        """
        now = time.time() if now is None else now
        days = self.retention_days
        if days <= 0:
            return {"collected": 0, "kept": 0, "freed_bytes": 0,
                    "retention_days": days,
                    "note": "retention is 0 or less; directories are never collected"}
        cutoff = now - days * 86_400.0
        with self._lock:
            live = {jid: job for jid, job in self._jobs.items()}
        collected, kept, freed = 0, 0, 0
        for entry in sorted(self.jobs_dir.glob("*")):
            if not entry.is_dir():
                continue
            verdict, stamp, state = _collectable(entry, live, cutoff)
            if not verdict:
                kept += 1
                continue
            size = _dir_bytes(entry)
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                kept += 1
                log.warning("could not collect a job directory",
                            extra={"job_dir": str(entry), "detail": str(exc)})
                continue
            collected += 1
            freed += size
            with self._lock:
                self._jobs.pop(entry.name, None)
            self.audit.append(
                "job.collected", ok=True, job_id=entry.name,
                why=f"older than [dispatch] job_retention_days ({days}d)",
                job_dir=str(entry), state=state, bytes=size,
                age_days=round((now - stamp) / 86_400.0, 1))
        if collected:
            log.info("collected job directories",
                     extra={"collected": collected, "kept": kept,
                            "freed_bytes": freed, "retention_days": days})
        return {"collected": collected, "kept": kept, "freed_bytes": freed,
                "retention_days": days}

    def start_gc(self, interval: float = config.JOB_GC_INTERVAL_S) -> None:
        """Run :meth:`collect` on a timer, off the request path. Idempotent.

        A thread rather than a hook on dispatch: the pass walks a directory and
        must never be something a user waits for, and hanging it off dispatch
        would mean the collection only happens on a machine that is already
        busy. One pass on the way up, because a laptop that is suspended and
        resumed daily would otherwise never reach the first tick.
        """
        if self._gc is not None and self._gc.is_alive():
            return

        def loop() -> None:
            while True:
                try:
                    self.collect()
                except Exception:  # noqa: BLE001 - the collector must not die
                    log.exception("the job collector failed")
                if self._closing.wait(interval):
                    return

        self._gc = threading.Thread(target=loop, daemon=True,
                                    name="luna-job-gc")
        self._gc.start()

    def close(self, join_timeout: float = 5.0) -> None:
        """Daemon shutdown. Dispatched jobs are deliberately left running.

        Killing them would be the wrong default: a job is somebody's work in
        progress, the terminal is visible to the user, and the ledger entry is
        durable so a restarted daemon can still signal it if asked.

        A *queued* job is the opposite case and gets the opposite treatment.
        Nothing was spawned, the queue only ever existed in this process's
        memory, and a `queued` directory left behind by a dead daemon is a job
        that will never start however long anyone waits for it. So the queue is
        emptied and each job is recorded as cancelled, with the reason. That is
        also what stops the queue leaking: no waiting job, no watcher, nothing
        holding a slot that will never be released.

        The *watchers* are a different matter. ``_watch`` wakes when the
        terminal exits and only then writes ``dispatch.finish``, so a watcher
        that outlives its dispatcher goes on appending to an audit log its
        owner has finished with -- under test, to one inside a temporary tree
        that teardown is deleting, which recreates the tree and leaves a stray
        directory behind. So close waits for them, but only for a bounded
        while: a job that is still genuinely running has a watcher that will
        not return for as long as the job lasts, and shutdown must not hang on
        somebody's hour-long task.
        """
        # First, so a watcher that finishes during the drain below cannot
        # admit a new job and start a watcher this call will never join.
        self._closing.set()
        with self._lock:
            self._procs.clear()
            self._admitting.clear()
            dropped = list(self._queue)
            self._queue.clear()
            gc_thread = self._gc
        for pending in dropped:
            job = pending.job
            job.state = "cancelled"
            job.finished = time.time()
            job.note = "the daemon stopped before this job was started"
            self._write_job(job)
            self.audit.append("dispatch.cancel", ok=True, job_id=job.id,
                              why="lunad shut down with the job still queued",
                              was="queued",
                              queued_s=round(job.finished - job.started, 1),
                              note="never admitted; no process existed")
        # Re-read the list each pass rather than joining one snapshot. A
        # watcher that was mid-`_admit_next` when `_closing` went up wins that
        # race and starts one more job -- and one more watcher, appended after
        # the snapshot was taken, which the snapshot loop would then never
        # join. Under test that is a `dispatch.finish` written into a
        # temporary tree teardown has already deleted, which recreates the
        # tree: the stray `/tmp/luna-test-*` this suite has a CI check for.
        deadline = time.monotonic() + join_timeout
        while True:
            with self._lock:
                self._watchers = [t for t in self._watchers if t.is_alive()]
                pending = list(self._watchers)
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                break
            pending[0].join(remaining)
        if gc_thread is not None:
            gc_thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._watchers = [t for t in self._watchers if t.is_alive()]


def jid_is_dead(data: dict[str, Any], live: dict[str, Any]) -> bool:
    """Is a job that claims to be running actually gone?"""
    if data.get("id") in live:
        return False
    pid = data.get("pid")
    if not isinstance(pid, int):
        return True
    return not safety.is_alive(pid)


def _collectable(entry: Path, live: dict[str, Job],
                 cutoff: float) -> tuple[bool, float, str]:
    """May this job directory be deleted? Returns the verdict, stamp and state.

    Read defensively on purpose: this is the one function in the daemon that
    deletes something the user cannot get back, so every branch that cannot
    establish the age of a directory keeps it.
    """
    job = live.get(entry.name)
    if job is not None and job.state in ("running", "queued"):
        return False, 0.0, job.state
    manifest = entry / "job.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("job.json is not an object")
    except (OSError, ValueError):
        # No readable manifest: this is a dispatch that died between mkdir and
        # the first write, not a record of anything. It ages from the directory
        # itself, and is still only collected past the window.
        stamp = _mtime(entry)
        return stamp < cutoff, stamp, "unknown"
    state = str(data.get("state") or "unknown")
    if state in ("running", "queued") and not jid_is_dead(data, live):
        return False, 0.0, state
    stamp = _float(data.get("finished")) or _float(data.get("started")) \
        or _mtime(manifest)
    return stamp < cutoff, stamp, state


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        # Unreadable is not old. Returning "now" keeps the directory.
        return time.time()


def _dir_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total
