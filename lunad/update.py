"""`luna update` — she brings herself up to date, or says what is in the way.

She runs from a git checkout (`~/Work/luna`), so "update yourself" is a fetch
and a fast-forward. That sentence hides every interesting decision, so they are
all written down here.

**Nothing is ever discarded.** The only write this module makes to the live
checkout is `git merge --ff-only`, which moves a ref and cannot lose a commit.
There is no `stash`, no `reset`, no `--force`, no `clean`, and no code path
that adds one. A dirty tree, a detached HEAD, a branch with no upstream and a
branch that has diverged are all *refusals*, each naming the thing in the way,
because the alternative — being clever about someone's half-finished work at
three in the morning — is how an assistant destroys an afternoon.

**The suite runs before the update lands, not after.** An assistant that
updates itself into a broken state is worse than one that never updates at
all, so the gate is: green suite or no restart. The obvious implementation —
fast-forward, test, roll back if red — means the rollback is a `reset --hard`,
and a `reset --hard` in the self-update path is exactly the loaded gun this
module refuses to own. So the suite runs in a **throwaway git worktree**
checked out at the target commit instead. The live checkout is not touched
until the tests are green; if they are red, a temporary directory is deleted
and nothing else happened at all.

**It goes through the confirmation gate.** She runs with a bypass sandbox and
no permission prompts, which makes a self-update path the single most
dangerous thing in the program: it is the one that changes what she *is*. It
is classified `system_config` — the same class as any other `systemctl --user
restart` — checked against `lunad/confirm.py`'s hard denials first and its
policy second, and every outcome, refusal included, is written to the audit
log with a `why`. There is no argument, environment variable or flag that
skips it.

**Skills are reported, not updated.** See `skill_drift` at the bottom.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import audit as audit_mod
from . import config
from . import confirm as confirm_mod
from . import vcs

#: The confirmation class a self-update is asked under. Not a new class: a
#: `systemctl --user restart` is what it is whichever unit it names, and
#: inventing `self_update = "ask"` would put a second, quieter switch next to
#: the one the user already set.
CONFIRM_CLASS = "system_config"

#: Field separator inside `git log --pretty`. A subject may contain anything a
#: person can type; a unit separator is the one byte it cannot.
_SEP = "\x1f"


class UpdateError(Exception):
    """Something went wrong that is not a refusal."""

    kind = "UpdateError"

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.message = message
        self.fields = fields

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": self.message, **self.fields}


class UpdateUnavailable(UpdateError):
    """No git, or not a checkout."""

    kind = "UpdateUnavailable"


# --------------------------------------------------------------- the survey


@dataclass(frozen=True)
class Change:
    """One commit that would arrive."""

    sha: str
    subject: str

    def to_dict(self) -> dict[str, str]:
        return {"sha": self.sha, "subject": self.subject}


@dataclass(frozen=True)
class Survey:
    """What a fetch found, and what would stop it being applied.

    `refusals` is the whole safety story in one field: it is empty exactly
    when applying the update is allowed to proceed. `available` is deliberately
    *not* "there are commits" — a dirty tree with commits waiting is not an
    available update, it is a blocked one, and the two must not read the same.
    """

    root: str = ""
    branch: str = ""
    upstream: str = ""
    remote: str = ""
    head: str = ""
    target: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: tuple[str, ...] = ()
    changes: tuple[Change, ...] = ()
    refusals: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.refusals)

    @property
    def available(self) -> bool:
        return self.behind > 0 and not self.refusals

    def summary(self) -> str:
        if self.refusals:
            return self.refusals[0]
        if not self.behind:
            return "already up to date"
        return (f"{self.behind} commit{'' if self.behind == 1 else 's'} "
                f"behind {self.upstream}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root, "branch": self.branch,
            "upstream": self.upstream, "remote": self.remote,
            "head": self.head, "target": self.target,
            "ahead": self.ahead, "behind": self.behind,
            "dirty": list(self.dirty),
            "changes": [c.to_dict() for c in self.changes],
            "refusals": list(self.refusals),
            "available": self.available, "blocked": self.blocked,
            "summary": self.summary(),
        }


@dataclass
class Outcome:
    """The result of a real update. Never raises to say "refused"."""

    ok: bool = False
    updated: bool = False
    survey: Survey | None = None
    from_sha: str = ""
    to_sha: str = ""
    changes: tuple[Change, ...] = ()
    tested: bool = False
    tests_ok: bool = False
    tests_note: str = ""
    restarted: bool = False
    ready: bool = False
    ready_note: str = ""
    refusals: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    decision: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "updated": self.updated,
            "survey": self.survey.to_dict() if self.survey else None,
            "from": self.from_sha, "to": self.to_sha,
            "changes": [c.to_dict() for c in self.changes],
            "tested": self.tested, "tests_ok": self.tests_ok,
            "tests_note": self.tests_note,
            "restarted": self.restarted, "ready": self.ready,
            "ready_note": self.ready_note,
            "refusals": list(self.refusals), "notes": list(self.notes),
            "decision": self.decision,
        }


# -------------------------------------------------------------- the updater


class Updater:
    """Fetch, judge, test in a scratch worktree, fast-forward, restart.

    Every binary name is read late, in this constructor, for the reason
    `lunad/config.py` records next to them: a name bound as a signature default
    is fixed at import and cannot be patched, and a test suite that reached the
    real `systemctl` would restart the user's daemon out from under them.
    `tests/test_guards.py` asserts the shape as well as the values.
    """

    def __init__(self, root: str | Path | None = None, *,
                 repo: vcs.Repo | None = None,
                 systemctl_bin: str | None = None,
                 unit: str | None = None,
                 python_bin: str | Path | None = None,
                 socket_path: Path | None = None,
                 state_file: Path | None = None,
                 skills_dir: Path | None = None,
                 store_dir: Path | None = None,
                 audit: audit_mod.AuditLog | None = None,
                 confirm: Any | None = None,
                 settings: Any | None = None,
                 run: vcs.Runner | None = None,
                 probe: Callable[[Path, float], bool] | None = None,
                 timeout: float | None = None,
                 test_timeout: float | None = None,
                 ready_timeout: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.systemctl_bin = (systemctl_bin if systemctl_bin is not None
                              else config.SYSTEMCTL_BIN)
        self.unit = unit if unit is not None else config.LUNAD_UNIT
        self.python_bin = Path(python_bin if python_bin is not None
                               else config.UPDATE_PYTHON)
        self.socket_path = Path(socket_path if socket_path is not None
                                else config.SOCKET_PATH)
        self.state_file = Path(state_file if state_file is not None
                               else config.STATE_FILE)
        self.skills_dir = Path(skills_dir if skills_dir is not None
                               else config.SKILLS_DIR)
        self.store_dir = Path(store_dir if store_dir is not None
                              else config.SKILLS_STORE_DIR)
        self.timeout = (float(timeout) if timeout is not None
                        else config.UPDATE_GIT_TIMEOUT_S)
        self.test_timeout = (float(test_timeout) if test_timeout is not None
                             else config.UPDATE_TEST_TIMEOUT_S)
        self.ready_timeout = (float(ready_timeout) if ready_timeout is not None
                              else config.UPDATE_READY_S)
        self.run: vcs.Runner = run if run is not None else vcs.run_process
        self.probe = probe if probe is not None else _ping
        self.clock = clock
        self.sleep = sleep
        self._audit = audit
        self._confirm = confirm
        self._settings = settings
        # One git wrapper, not two: `vcs.Repo` already owns the child
        # environment, the timeout handling, the `-C <root>` prefix and the
        # late-read binary names, and a second one would drift from it.
        self.repo = repo if repo is not None else vcs.Repo(
            root, run=self.run, audit=audit, settings=settings,
            timeout=self.timeout)

    # -- plumbing ----------------------------------------------------------

    @property
    def audit(self) -> audit_mod.AuditLog:
        if self._audit is None:
            self._audit = audit_mod.audit()
        return self._audit

    @property
    def confirm(self) -> Any:
        """The gate. Defaults to a real broker rather than to nothing.

        A caller that wants the *daemon's* broker — the one `luna confirm yes`
        can answer — passes it in. A caller that passes nothing still gets a
        gate; it never gets an ungated update.
        """
        if self._confirm is None:
            self._confirm = confirm_mod.ConfirmBroker(
                settings=self._settings, audit=self.audit)
        return self._confirm

    def _git(self, *args: str, check: bool = False,
             timeout: float | None = None) -> vcs.Ran:
        return self.repo.git(*args, check=check, timeout=timeout)

    def _run(self, argv: Sequence[str], *, cwd: Path,
             timeout: float) -> vcs.Ran:
        return self.run(list(argv), Path(cwd), float(timeout))

    # -- looking -----------------------------------------------------------

    def survey(self, *, fetch: bool = True) -> Survey:
        """Fetch, then report where this checkout stands. Writes nothing.

        A fetch is the one network call, and it touches only `.git/refs/remotes`
        — it cannot move HEAD and it cannot touch a file the user is editing.
        `--check` runs exactly this and stops.
        """
        # Deliberately not `Repo.available()`: that asks for an authenticated
        # `gh` as well, because the workflow it guards ends in a pull request.
        # A fast-forward needs `git` and a checkout and nothing else, and an
        # update that refused because GitHub was logged out would be lying
        # about what stopped it.
        try:
            root = str(self.repo.root)
            branch = self.repo.current_branch()
        except vcs.VcsError as exc:
            return Survey(refusals=(str(exc),))
        if not branch:
            return Survey(root=root, refusals=(
                "HEAD is detached — check out a branch before updating; "
                "nothing here will move it for you",))

        ran = self._git("rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{upstream}")
        upstream = ran.out if ran.ok else ""
        if not upstream:
            return Survey(root=root, branch=branch, refusals=(
                f"`{branch}` tracks nothing, so there is no 'up to date' to "
                f"be — set an upstream with `git branch --set-upstream-to="
                f"{config.UPDATE_REMOTE}/{branch}`",))
        remote = upstream.split("/", 1)[0] if "/" in upstream else ""

        if fetch:
            got = self._git("fetch", "--quiet", "--prune",
                            remote or config.UPDATE_REMOTE)
            if not got.ok:
                return Survey(root=root, branch=branch, upstream=upstream,
                              remote=remote, refusals=(
                                  f"could not reach {remote or 'the remote'}: "
                                  f"{got.message or 'fetch failed'}",))

        try:
            head = self.repo.head_sha()
            target = self._git("rev-parse", "--short", upstream).out
            ahead, behind = self._counts(upstream)
            dirty = self.repo.dirty()
        except vcs.VcsError as exc:
            return Survey(root=root, branch=branch, upstream=upstream,
                          remote=remote, refusals=(str(exc),))
        changes = self._changes(upstream) if behind else ()

        refusals: list[str] = []
        if dirty:
            shown = ", ".join(dirty[:6])
            more = f" (and {len(dirty) - 6} more)" if len(dirty) > 6 else ""
            refusals.append(
                f"{len(dirty)} path(s) have uncommitted changes: {shown}"
                f"{more} — commit or stash them yourself; this will not touch "
                f"unfinished work")
        if ahead and behind:
            refusals.append(
                f"`{branch}` and {upstream} have both moved on — "
                f"{ahead} local commit(s) here, {behind} there. That is a "
                f"merge or a rebase and it is yours to make, not hers")

        return Survey(root=root, branch=branch, upstream=upstream,
                      remote=remote, head=head, target=target, ahead=ahead,
                      behind=behind, dirty=dirty, changes=changes,
                      refusals=tuple(refusals))

    def _counts(self, upstream: str) -> tuple[int, int]:
        ran = self._git("rev-list", "--left-right", "--count",
                        f"HEAD...{upstream}")
        parts = ran.out.replace("\t", " ").split()
        if not ran.ok or len(parts) < 2:
            return (0, 0)
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return (0, 0)

    def _changes(self, upstream: str, *, limit: int = 200) -> tuple[Change, ...]:
        ran = self._git("log", "--no-color", f"--max-count={limit}",
                        f"--pretty=format:%h{_SEP}%s", f"HEAD..{upstream}")
        if not ran.ok:
            return ()
        out: list[Change] = []
        for line in ran.stdout.splitlines():
            if not line.strip():
                continue
            sha, _, subject = line.partition(_SEP)
            out.append(Change(sha.strip(), subject.strip()))
        return tuple(out)

    # -- testing -----------------------------------------------------------

    def test_target(self, upstream: str) -> tuple[bool, str]:
        """Run the suite against the commit that has not landed yet.

        In a scratch worktree, so that a red suite costs a `rm -rf` of a
        temporary directory and nothing else. The live checkout is not
        involved and does not move.
        """
        if not self.python_bin.exists():
            return (False, f"no interpreter at {self.python_bin} — the suite "
                           f"cannot be run, so the update cannot be trusted")
        scratch = Path(tempfile.mkdtemp(prefix="luna-update-"))
        tree = scratch / "tree"
        try:
            made = self._git("worktree", "add", "--detach",
                             str(tree), upstream)
            if not made.ok:
                return (False, f"could not check the update out for testing: "
                               f"{made.message or 'worktree add failed'}")
            ran = self._run(
                [str(self.python_bin), "-m", "unittest", "discover",
                 "-s", "tests", "-t", "."],
                cwd=tree, timeout=self.test_timeout)
            if ran.ok:
                return (True, _tail(ran.stderr or ran.stdout) or "suite passed")
            return (False, _tail(ran.stderr or ran.stdout)
                    or f"the suite exited {ran.returncode}")
        except vcs.VcsError as exc:
            return (False, str(exc))
        finally:
            # `rm -rf` the scratch directory and let `prune` forget it, rather
            # than `worktree remove --force`. The two are equivalent here and
            # the difference is that this way there is no `--force` anywhere
            # in the self-update path at all — which is a thing a reader, and
            # `tests/test_update.py::NothingRanCase`, can check in one grep.
            shutil.rmtree(scratch, ignore_errors=True)
            self._git("worktree", "prune")

    # -- landing -----------------------------------------------------------

    def fast_forward(self, upstream: str) -> tuple[bool, str]:
        """`--ff-only`, and nothing else, ever.

        `--ff-only` is chosen over `pull` because it fails rather than
        inventing a merge commit, and over `reset --hard` because it cannot
        lose a commit even if every check above this were wrong.
        """
        ran = self._git("merge", "--ff-only", upstream)
        if ran.ok:
            return (True, ran.out)
        return (False, ran.message or "fast-forward refused")

    def restart(self) -> tuple[bool, str]:
        ran = self._run([self.systemctl_bin, "--user", "restart", self.unit],
                        cwd=self.repo.root,
                        timeout=config.UPDATE_RESTART_TIMEOUT_S)
        if ran.ok:
            return (True, f"{self.systemctl_bin} --user restart {self.unit}")
        return (False, ran.message or f"exit {ran.returncode}")

    def wait_ready(self, *, since: float,
                   timeout: float | None = None) -> tuple[bool, str]:
        """Did she actually come back?

        Two questions, not one. The socket answering says a process is
        listening; `state` being republished *after the restart began* says it
        is the new one and that it got far enough to tell the desktop it
        exists. A restart that leaves a listener but never republishes is the
        interesting failure, and it is the one a single check would miss.
        """
        deadline = self.clock() + float(
            timeout if timeout is not None else self.ready_timeout)
        answered = False
        while True:
            if not answered:
                answered = self.probe(self.socket_path,
                                      config.UPDATE_READY_POLL_S * 4)
            if answered and _republished(self.state_file, since):
                return (True, "socket answering, state republished")
            if self.clock() >= deadline:
                break
            self.sleep(config.UPDATE_READY_POLL_S)
        if not answered:
            return (False, f"nothing is listening on {self.socket_path}; "
                           f"`systemctl --user status {self.unit}` will say "
                           f"why")
        return (False, f"she is answering but has not republished "
                       f"{self.state_file} — she came up part-way")

    # -- the whole thing ---------------------------------------------------

    def update(self, *, why: str = "", actor: str = "user",
               run_tests: bool = True, restart: bool = True) -> Outcome:
        """Survey, ask, test, fast-forward, restart, verify. In that order.

        Returns an `Outcome` rather than raising for a refusal: a refused
        update is an answer, not a crash, and the caller turns it into an exit
        code. Every branch through here writes exactly one audit record with
        an `ok` and a `why`.
        """
        why = why or "bringing the checkout up to date"
        survey = self.survey()
        out = Outcome(survey=survey, from_sha=survey.head,
                      to_sha=survey.head, changes=survey.changes)

        if survey.refusals:
            out.refusals = survey.refusals
            self._record("update.refused", ok=False, why=why, actor=actor,
                         refusals=list(survey.refusals),
                         branch=survey.branch, head=survey.head)
            return out

        if not survey.behind:
            out.ok = True
            out.notes = ("already up to date",)
            self._record("update.checked", ok=True, why=why, actor=actor,
                         branch=survey.branch, head=survey.head,
                         behind=0, updated=False)
            return out

        detail = (f"fast-forward {survey.root} from {survey.head} to "
                  f"{survey.target} ({survey.behind} commit"
                  f"{'' if survey.behind == 1 else 's'} from "
                  f"{survey.upstream}) and {self.systemctl_bin} --user "
                  f"restart {self.unit}")

        # Hard denials first, exactly as `server.op_confirm` does it: they are
        # not policy and no broker, local or remote, may soften them.
        for rule in confirm_mod.hard_denials(detail):
            out.refusals = (f"{rule.name}: {rule.why}",)
            out.decision = {"action": rule.name, "policy": confirm_mod.DENY,
                            "allowed": False, "outcome": "hard"}
            self._record("update.refused", ok=False, why=why, actor=actor,
                         outcome="hard", rule=rule.name, detail=detail)
            return out

        decision = self.confirm.check(CONFIRM_CLASS, detail, why=why,
                                      actor=actor)
        out.decision = (decision.to_dict() if hasattr(decision, "to_dict")
                        else dict(decision))
        if not getattr(decision, "allowed", False):
            message = (decision.message() if hasattr(decision, "message")
                       else str(decision))
            out.refusals = (message,)
            self._record("update.refused", ok=False, why=why, actor=actor,
                         outcome=getattr(decision, "outcome", "denied"),
                         detail=detail)
            return out

        if run_tests:
            out.tested = True
            out.tests_ok, out.tests_note = self.test_target(survey.upstream)
            if not out.tests_ok:
                out.refusals = (
                    f"the suite is red on {survey.target}, so nothing was "
                    f"applied and she was not restarted — this checkout is "
                    f"exactly as you left it",)
                self._record("update.refused", ok=False, why=why, actor=actor,
                             outcome="red", target=survey.target,
                             tests_note=out.tests_note)
                return out

        moved, note = self.fast_forward(survey.upstream)
        if not moved:
            out.refusals = (f"the fast-forward was refused: {note}",)
            self._record("update.refused", ok=False, why=why, actor=actor,
                         outcome="ff-refused", detail=note)
            return out
        out.updated = True
        out.to_sha = self.repo.head_sha()
        out.notes = tuple(list(out.notes) + list(self.skill_drift()))

        if restart:
            since = time.time()
            out.restarted, restart_note = self.restart()
            if not out.restarted:
                out.refusals = (f"the code is updated but she did not "
                                f"restart: {restart_note}",)
                self._record("update.applied", ok=False, why=why, actor=actor,
                             **{"from": out.from_sha}, to=out.to_sha,
                             restarted=False, detail=restart_note)
                return out
            out.ready, out.ready_note = self.wait_ready(since=since)
            if not out.ready:
                out.refusals = (out.ready_note,)
                self._record("update.applied", ok=False, why=why, actor=actor,
                             **{"from": out.from_sha}, to=out.to_sha,
                             restarted=True, ready=False,
                             detail=out.ready_note)
                return out

        out.ok = True
        self._record("update.applied", ok=True, why=why, actor=actor,
                     **{"from": out.from_sha}, to=out.to_sha,
                     commits=len(out.changes), branch=survey.branch,
                     tested=out.tested, restarted=out.restarted,
                     ready=out.ready)
        return out

    def _record(self, action: str, *, ok: bool, why: str, actor: str,
                **fields: Any) -> None:
        try:
            self.audit.append(action, actor=actor, ok=ok, why=why, **fields)
        except OSError:
            # An unwritable audit log must not be the thing that stops her
            # telling the user what happened. It is already the loudest
            # failure this program has; it is not this function's to raise.
            pass

    # -- skills ------------------------------------------------------------

    def skill_drift(self) -> tuple[str, ...]:
        """What the pull did *not* bring up to date. Reports; never writes.

        Three kinds of skill, three different answers, and the reasoning is
        the point:

        * The repo's own skills are **symlinks** from `~/.codex/skills/` into
          this checkout. A fast-forward updates their contents by definition —
          there is nothing to do and nothing to say.
        * A skill this update *ships for the first time* has no link yet, so
          it is invisible until someone makes one. That is silent breakage, so
          it is named here. It is not linked automatically: `~/.codex/skills/`
          is the farm's, `luna skills` owns it, and one command owning one
          directory is worth more than the two seconds it saves.
        * A skill cloned from somewhere else lives under the store and is
          somebody else's repository. Pulling it would run unreviewed
          third-party code under a bypass sandbox on the strength of *this*
          repository's green suite, which says nothing whatever about it. It
          is counted and named so that "up to date" never quietly means
          "except for those", and updated on purpose or not at all.
        """
        notes: list[str] = []
        try:
            shipped = sorted(p.name for p in self.skills_dir.iterdir()
                             if p.is_dir() and not p.name.startswith("."))
        except OSError:
            shipped = []
        linked = _farm_names()
        missing = [n for n in shipped if n not in linked]
        if missing:
            notes.append(
                f"{len(missing)} skill(s) in this checkout are not in the "
                f"farm: {', '.join(missing)} — `luna skills add "
                f"{self.skills_dir / missing[0]}`")
        try:
            clones = sorted(p.name for p in self.store_dir.iterdir()
                            if (p / ".git").exists())
        except OSError:
            clones = []
        if clones:
            notes.append(
                f"{len(clones)} cloned skill(s) were not updated — they are "
                f"other people's repositories: {', '.join(clones)}")
        return tuple(notes)


# ------------------------------------------------------------------ helpers


def _farm_names() -> frozenset[str]:
    """Names already linked into the codex farm, or an empty set.

    Imported here rather than at module scope: `lunad.skills` walks the farm
    on construction, and `luna update --check` should not pay for that.
    """
    try:
        from . import skills as skills_mod  # noqa: PLC0415
        return frozenset(s.name for s in skills_mod.SkillFarm().list())
    except Exception:
        return frozenset()


def _tail(text: str, *, lines: int = 12) -> str:
    kept = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(kept[-lines:])


def _republished(path: Path, since: float) -> bool:
    try:
        return path.stat().st_mtime >= since - 1.0
    except OSError:
        return False


def _ping(path: Path, timeout: float) -> bool:
    """One `ping` op over the socket. True only for a real `pong`."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        sock.settimeout(max(0.1, float(timeout)))
        sock.connect(str(path))
        request = json.dumps({"op": "ping", "id": uuid.uuid4().hex[:12]})
        sock.sendall((request + "\n").encode())
        with sock.makefile("rb") as fh:
            line = fh.readline(65536)
        return bool(json.loads(line.decode("utf-8")).get("pong"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
