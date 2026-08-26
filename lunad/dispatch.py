"""Workspace dispatch. ARCHITECTURE.md section 6.

Luna hands a task to a real agent session running in a terminal in her own
Hyprland special workspace, tracks the pid she created, captures what it wrote,
and reports back. The workspace is hidden, so the job does not interrupt what
the user is doing; ``luna peek`` brings it into view.

Three decisions worth the words:

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

The exact Hyprland incantation is documented in :class:`Hyprland`; it was not
the obvious one.
"""

from __future__ import annotations

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

from . import agent as agent_mod, audit as audit_mod, config, persona, safety

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

    _RULE_GLOBAL = "luna_workspace_rule"

    def __init__(self, hyprctl: str = config.HYPRCTL_BIN,
                 workspace: str = config.LUNA_WORKSPACE,
                 app_id: str = config.LUNA_APP_ID) -> None:
        self.hyprctl = hyprctl
        self.workspace = workspace
        self.app_id = app_id

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
        lua = (
            f"if not _G.{self._RULE_GLOBAL} then "
            f'hl.window_rule({{ match = {{ class = "{_lua_escape(klass)}" }}, '
            f'workspace = "special:{self.workspace} silent" }}); '
            f"_G.{self._RULE_GLOBAL} = true; return \"added\" end "
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

_STATES = ("running", "finished", "failed", "cancelled")


@dataclass
class Job:
    id: str
    task: str
    to: str = "worker"
    state: str = "running"
    pid: int | None = None
    started: float = field(default_factory=time.time)
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
            "finished": round(self.finished, 3) if self.finished else None,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
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


class Dispatcher:
    """Spawns jobs, owns their pids, and answers ``jobs`` / ``peek``."""

    def __init__(self, *, jobs_dir: Path = config.JOBS_DIR,
                 hypr: Hyprland | None = None,
                 audit: audit_mod.AuditLog | None = None,
                 terminal: str = config.TERMINAL_BIN,
                 app_id: str = config.LUNA_APP_ID,
                 agent_bin: str | None = None,
                 agent_name: str | None = None,
                 sol_memory_dir: Path = config.SOL_MEMORY_DIR,
                 spawn: Any = None) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.hypr = hypr if hypr is not None else Hyprland(app_id=app_id)
        self.audit = audit if audit is not None else audit_mod.audit()
        self.terminal = terminal
        self.app_id = app_id
        self._agent_bin = agent_bin
        # Which CLI's flags the runner script is written in. Read from
        # Omarchy's default rather than hard-coded, because `claude -p` and
        # `codex exec -` share not one flag between them.
        self.agent_name = (agent_name or agent_mod.read_default_agent()).lower()
        self.adapter = agent_mod.get_adapter(self.agent_name)
        self.sol_memory_dir = Path(sol_memory_dir)
        self.spawn = spawn or safety.spawn
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------

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

    def announce(self, job: Job) -> str:
        """One line, as the persona spec requires: who, and why.

        Composed here rather than asked of a model. An announcement that costs
        an API call and 4 seconds would not get made.
        """
        who = "Sol" if job.to == "sol" else "a worker"
        why = ("depth-first technical work, reporting back to me"
               if job.to == "sol" else "grunt work I would only be in the way of")
        return (f"Enrolled {who} on job {job.id} — {why}. "
                f"`luna peek` to watch, `luna jobs` for the result.")

    # -- dispatch --------------------------------------------------------

    def dispatch(self, task: str, to: str = "worker", *,
                 timeout: float = config.DISPATCH_TIMEOUT_S,
                 linger: float = config.DISPATCH_LINGER_S,
                 sol_memory_block: str = "") -> Job:
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

        job_id = uuid.uuid4().hex[:8]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, task=task, to=to, dir=job_dir)

        system_prompt = persona.build_dispatch_system_prompt(
            to=to, memory_block=sol_memory_block,
            memory_dir=str(self.sol_memory_dir) if to == "sol" else "",
            job_dir=str(job_dir))

        (job_dir / "task.txt").write_text(task + "\n", encoding="utf-8")
        (job_dir / "system.txt").write_text(system_prompt, encoding="utf-8")
        runner = job_dir / "run.sh"
        runner.write_text(self._runner_script(job, timeout, linger),
                          encoding="utf-8")
        runner.chmod(0o700)

        # Best effort: without the rule the job still runs, it just opens the
        # window where the user is looking. Say so rather than failing.
        placed = True
        try:
            rule = self.hypr.ensure_workspace_rule()
        except DispatchUnavailable as exc:
            placed = False
            rule = f"unavailable: {exc}"
            log.warning("workspace rule not installed; the window will open on "
                        "the active workspace", extra={"detail": str(exc)})

        argv = [self.terminal, "--app-id", self.app_id,
                "--title", f"luna job {job_id} → {to}",
                "--", "/bin/bash", str(runner)]
        try:
            proc = self.spawn(argv, kind="dispatch", job_id=job_id, durable=True,
                              note=f"dispatch to {to}: {task[:80]}",
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              cwd=str(job_dir))
        except OSError as exc:
            job.state = "failed"
            job.note = f"could not start {self.terminal}: {exc}"
            self._write_job(job)
            self.audit.append("dispatch.failed", ok=False, job_id=job_id,
                              why=task[:200], reason=str(exc))
            raise DispatchUnavailable(job.note) from exc

        job.pid = proc.pid
        job.note = ("in special workspace" if placed
                    else "workspace rule unavailable; window is on the active "
                         "workspace")
        with self._lock:
            self._jobs[job_id] = job
            self._procs[job_id] = proc
        self._write_job(job)

        self.audit.append(
            "dispatch.spawn", ok=True, job_id=job_id, to=to, pid=proc.pid,
            why=task[:500], cmd=argv, job_dir=str(job_dir),
            workspace=f"special:{self.hypr.workspace}", rule=rule,
            undo={"what": "stop the dispatched job",
                  "cmd": ["luna", "jobs", "--cancel", job_id],
                  "valid_while": "the job is still running"})
        log.info("dispatched", extra={"job_id": job_id, "to": to,
                                      "pid": proc.pid, "placed": placed})

        threading.Thread(target=self._watch, args=(job, proc, timeout),
                         daemon=True, name=f"luna-job-{job_id}").start()
        return job

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
            safety.reap(proc)

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
            elapsed_s=round(job.finished - job.started, 1),
            why=job.task[:200], output_chars=len(job.read_output()),
            output_head=output[:400])
        log.info("job finished", extra={"job_id": job.id,
                                        "exit_code": job.exit_code,
                                        "state": job.state})

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
        with self._lock:
            proc = self._procs.get(job_id)
            job = self._jobs.get(job_id)
        if proc is None or job is None:
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
            if with_output and data.get("dir"):
                job = Job(id=str(data["id"]), task=str(data.get("task", "")),
                          dir=Path(str(data["dir"])))
                data["output"] = job.read_output()
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = [j.to_dict() for j in self._jobs.values()
                       if j.state == "running"]
        return {"running": running, "jobs_dir": str(self.jobs_dir),
                "workspace": self.hypr.state()}

    def close(self) -> None:
        """Daemon shutdown. Dispatched jobs are deliberately left running.

        Killing them would be the wrong default: a job is somebody's work in
        progress, the terminal is visible to the user, and the ledger entry is
        durable so a restarted daemon can still signal it if asked.
        """
        with self._lock:
            self._procs.clear()


def jid_is_dead(data: dict[str, Any], live: dict[str, Any]) -> bool:
    """Is a job that claims to be running actually gone?"""
    if data.get("id") in live:
        return False
    pid = data.get("pid")
    if not isinstance(pid, int):
        return True
    return not safety.is_alive(pid)
