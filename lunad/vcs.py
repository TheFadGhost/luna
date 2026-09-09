"""The GitHub workflow, as code rather than as good intentions.

Luna works unattended. Work that only exists on this disk is work nobody can
review, revert, or find again next month, so every piece of it takes the same
route: **branch, commit with a written message, push, open a pull request,
wait for CI, and merge only when the checks are green.** She never pushes to
the default branch, and she never merges a pull request whose checks have not
reported.

Three things in here are load-bearing and are worth reading before changing
anything.

**1. "Green" is a function, not a feeling.** :func:`evaluate` takes the JSON
``gh pr view`` returns and produces a :class:`CheckStatus` carrying every
reason the pull request may *not* be merged. The list is the whole gate:
:meth:`Repo.merge` refuses unless it is empty. Crucially, **no checks at all is
not green.** A repository with no CI configured reports an empty check rollup,
and the tempting reading of that — "nothing failed" — is the reading that
merges unreviewed work into ``main`` on every repository that has not got round
to CI yet. So an empty rollup is a refusal with its own message, and so is a
rollup in which every entry was skipped: something has to have actually passed.

**2. She never sweeps up work that is not hers.** :meth:`Repo.commit` takes an
explicit list of paths and stages exactly those. There is no ``git add -A``
anywhere in this module and no "commit everything that is dirty" mode, because
an agent that woke up in somebody's working tree cannot tell its own edits from
a half-finished change a human left there an hour ago. The committed set is
named by the caller; :attr:`Commit.left_alone` reports what was dirty and was
deliberately not touched, so the report says so out loud.

**3. It shells out.** ``git`` and ``gh`` are the API. There is no HTTP client
here, no token handling, and no dependency: ``gh`` already holds the user's
credentials and already knows how to talk to GitHub, and a hand-rolled second
copy of that would be one more thing to keep in step with GitHub's API and one
more place a token could leak. The two binary names are read **late**, in the
constructor body, so the test suite can replace them with names that cannot
resolve — see ``tests/_support.py``. A test that actually reached ``gh`` would
open a pull request on the user's real account.

The confirmation broker is the other boundary. Pushing, opening a pull request
and merging all go through it (``[confirm] git_push`` and
``[confirm] git_merge``), so a user who wants to be asked can be, and a user
who wants the whole thing refused can have that too. The green gate is *not*
one of those settings: it is enforced here, in code, whatever the config says.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import (audit as audit_mod, config, confirm as confirm_mod, safety,
               settings as settings_mod)

log = logging.getLogger("lunad.vcs")


# =========================================================================
# Errors
# =========================================================================


class VcsError(Exception):
    """Something in the workflow could not be done. Reported, never fatal."""

    kind = "VcsError"

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.fields = fields

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": str(self), **self.fields}


class VcsUnavailable(VcsError):
    """``git`` or ``gh`` is missing, unauthenticated, or not a repository."""

    kind = "VcsUnavailable"


class NotARepository(VcsUnavailable):
    kind = "NotARepository"


class ProtectedBranch(VcsError):
    """The default branch is not hers to commit to, push, or force.

    In code and not in the config, for the same reason the four hard denies in
    :mod:`lunad.confirm` are: "she may push straight to main if the file says
    so" is not a setting anybody should be able to turn on by accident at
    three in the morning.
    """

    kind = "ProtectedBranch"


class NothingToCommit(VcsError):
    kind = "NothingToCommit"


class NotGreen(VcsError):
    """The merge was refused because the checks are not green.

    Carries the whole :class:`CheckStatus`, because "not green" on its own is
    not an answer anybody can act on: the caller needs to know whether it is
    waiting for CI, looking at a failure, or looking at a repository that has
    no CI at all.
    """

    kind = "NotGreen"

    def __init__(self, status: "CheckStatus") -> None:
        super().__init__(status.summary(), **status.to_dict())
        self.status = status


# =========================================================================
# Running git and gh
# =========================================================================


@dataclass(frozen=True)
class Ran:
    """One finished ``git`` or ``gh`` invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    @property
    def message(self) -> str:
        """Whatever the command had to say, for an error the user will read."""
        return (self.stderr.strip() or self.stdout.strip())[:400]


#: A runner takes the whole argv and gives back what happened. Injected so the
#: suite can drive every branch of this module without a repository, a network
#: or a GitHub account.
Runner = Callable[[Sequence[str], Path, float], Ran]


#: Environment forced on every child. An unattended agent must never be left
#: sitting on a credential prompt nobody is there to answer, and colour codes
#: in captured output turn a comparison into a puzzle.
CHILD_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_PAGER": "cat",
    "CLICOLOR": "0",
    "NO_COLOR": "1",
}


def run_process(argv: Sequence[str], cwd: Path, timeout: float) -> Ran:
    """The default runner: ``subprocess.run``, captured, bounded.

    Not :func:`lunad.safety.spawn`, and the difference is deliberate. That
    function exists so a *long-lived* child stays signallable across a daemon
    restart; these are synchronous captures of a program that either answers in
    a second or is wedged, and ``subprocess.run`` already kills its own child on
    the timeout. Registering each of them in the spawn ledger would churn a
    file that exists to answer one question — "may Luna signal this pid" — for
    processes that are gone before anybody could ask it.
    """
    env = dict(os.environ)
    env.update(CHILD_ENV)
    try:
        proc = subprocess.run(list(argv), cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout, check=False, env=env)
    except FileNotFoundError as exc:
        raise VcsUnavailable(f"{argv[0]} is not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VcsError(
            f"{' '.join(str(a) for a in argv[:3])} timed out after {timeout:g}s"
        ) from exc
    return Ran(tuple(str(a) for a in argv), proc.returncode,
               proc.stdout or "", proc.stderr or "")


# =========================================================================
# Branch names
# =========================================================================

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Refs git will not accept, and one it accepts but nobody should generate.
_BAD_REF_BITS = ("..", "@{", "//", "\\", "~", "^", ":", "?", "*", "[")


def slugify(text: str, *, limit: int = 40) -> str:
    """A branch-safe fragment of ``text``. Never empty, never a bad ref."""
    slug = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-.")
    for bad in _BAD_REF_BITS:
        slug = slug.replace(bad, "-")
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    if len(slug) > limit:
        slug = slug[:limit].rstrip("-.")
    return slug or "work"


def branch_name(topic: str, *, prefix: str | None = None,
                when: date | None = None) -> str:
    """``luna/<topic>-<yymmdd>``.

    Dated rather than random on purpose. A random suffix makes every re-run a
    new branch and leaves a repository full of near-identical abandoned ones;
    a date means a job re-run on the same day lands on the branch it was
    already using, which is the resume case and is the one worth optimising
    for. Two genuinely different jobs on one topic on one day collide, and
    :meth:`Repo.branch` adopts rather than forking — see its docstring for why
    that is the safer of the two wrong answers.
    """
    who = prefix if prefix is not None else str(
        settings_mod.get("vcs.branch_prefix", config.VCS_BRANCH_PREFIX))
    who = slugify(who, limit=20) or config.VCS_BRANCH_PREFIX
    stamp = (when or date.today()).strftime("%y%m%d")
    return f"{who}/{slugify(topic)}-{stamp}"


# =========================================================================
# What "green" means
# =========================================================================

#: Concluded and not a failure. ``SKIPPED`` and ``NEUTRAL`` are in here because
#: path-filtered workflows skip constantly and a repository whose matrix skips
#: two of six jobs is not a repository that should never be able to merge. They
#: do *not* count towards the "something actually passed" rule below, which is
#: what stops an all-skipped rollup being read as success.
PASSED = ("SUCCESS",)
TOLERATED = ("SKIPPED", "NEUTRAL")

#: Concluded and a failure. ``STALE`` is included: a check whose run was
#: superseded has not reported on this head.
FAILED = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
          "STARTUP_FAILURE", "STALE")

#: Not concluded. Any one of these is a refusal — "no check pending" is half of
#: what the user asked "green" to mean.
RUNNING = ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED",
           "EXPECTED")

#: The fields ``gh pr view`` is asked for. Kept in one tuple so the request and
#: the parser cannot drift apart.
PR_FIELDS = ("number", "title", "url", "state", "isDraft", "mergeable",
             "mergeStateStatus", "reviewDecision", "statusCheckRollup",
             "headRefName", "baseRefName", "mergedAt")

MERGE_METHODS = ("squash", "merge", "rebase")


@dataclass(frozen=True)
class Check:
    """One entry from the status check rollup."""

    name: str
    status: str = ""            # CheckRun: QUEUED | IN_PROGRESS | COMPLETED
    conclusion: str = ""        # CheckRun conclusion, or StatusContext state
    url: str = ""

    @property
    def verdict(self) -> str:
        """``passing`` | ``tolerated`` | ``failing`` | ``pending`` | ``unknown``."""
        if self.conclusion in FAILED:
            return "failing"
        if self.status and self.status != "COMPLETED":
            return "pending"
        if self.conclusion in RUNNING:
            return "pending"
        if self.conclusion in PASSED:
            return "passing"
        if self.conclusion in TOLERATED:
            return "tolerated"
        # A completed check with a conclusion nobody here recognises. Refused
        # rather than guessed: an unrecognised state is exactly the case where
        # assuming success is how a broken build gets merged.
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status,
                "conclusion": self.conclusion, "verdict": self.verdict,
                "url": self.url}


def _check_from(entry: Any) -> Check:
    """One rollup entry, whichever of the two shapes GitHub sent.

    ``gh`` returns a flat list mixing ``CheckRun`` (name/status/conclusion) and
    ``StatusContext`` (context/state) objects, so both spellings are read and
    neither is required.
    """
    if not isinstance(entry, dict):
        return Check(name=str(entry)[:80], conclusion="")
    name = str(entry.get("name") or entry.get("context")
               or entry.get("workflowName") or "(unnamed check)")
    return Check(
        name=name,
        status=str(entry.get("status") or "").upper(),
        conclusion=str(entry.get("conclusion") or entry.get("state")
                       or "").upper(),
        url=str(entry.get("detailsUrl") or entry.get("targetUrl") or ""),
    )


@dataclass(frozen=True)
class CheckStatus:
    """Everything the merge gate looked at, and every reason it said no."""

    number: int
    state: str = "OPEN"
    draft: bool = False
    mergeable: str = ""
    merge_state: str = ""
    review_decision: str = ""
    checks: tuple[Check, ...] = ()
    refusals: tuple[str, ...] = ()
    title: str = ""
    url: str = ""
    head: str = ""
    base: str = ""

    @property
    def green(self) -> bool:
        return not self.refusals

    def counts(self) -> dict[str, int]:
        out = {"passing": 0, "tolerated": 0, "failing": 0, "pending": 0,
               "unknown": 0}
        for check in self.checks:
            out[check.verdict] += 1
        return out

    def named(self, verdict: str) -> list[str]:
        return [c.name for c in self.checks if c.verdict == verdict]

    def summary(self) -> str:
        if self.green:
            counts = self.counts()
            return (f"pull request #{self.number} is green: "
                    f"{counts['passing']} passing"
                    + (f", {counts['tolerated']} skipped"
                       if counts["tolerated"] else ""))
        return (f"pull request #{self.number} is not green — "
                + "; ".join(self.refusals))

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "green": self.green,
                "refusals": list(self.refusals), "state": self.state,
                "draft": self.draft, "mergeable": self.mergeable,
                "merge_state": self.merge_state,
                "review_decision": self.review_decision,
                "checks": [c.to_dict() for c in self.checks],
                "counts": self.counts(), "title": self.title,
                "url": self.url, "head": self.head, "base": self.base,
                "summary": self.summary()}


def evaluate(payload: dict[str, Any], *, number: int = 0) -> CheckStatus:
    """Turn ``gh pr view --json ...`` into a verdict. Pure; no I/O.

    Every refusal is collected rather than short-circuited, so a caller is told
    everything that is wrong in one go instead of finding out one round trip at
    a time. The order is fixed and goes from "this is not a mergeable object at
    all" down to the individual checks.
    """
    payload = payload if isinstance(payload, dict) else {}
    number = int(payload.get("number") or number or 0)
    state = str(payload.get("state") or "").upper()
    draft = bool(payload.get("isDraft"))
    mergeable = str(payload.get("mergeable") or "").upper()
    merge_state = str(payload.get("mergeStateStatus") or "").upper()
    review = str(payload.get("reviewDecision") or "").upper()
    rollup = payload.get("statusCheckRollup")
    checks = tuple(_check_from(e) for e in rollup) if isinstance(rollup, list) \
        else ()

    refusals: list[str] = []

    if state and state != "OPEN":
        refusals.append(f"the pull request is {state.lower()}, not open")
    if draft:
        refusals.append("it is still a draft")

    if mergeable == "CONFLICTING":
        refusals.append("it conflicts with its base branch")
    elif mergeable != "MERGEABLE":
        # UNKNOWN means GitHub has not finished computing the merge; absent
        # means the field was never asked for or never came back. Neither is
        # evidence that merging is safe, and waiting costs one poll.
        refusals.append(
            "GitHub has not confirmed it can be merged "
            f"(mergeable={mergeable or 'missing'})")

    if merge_state and merge_state != "CLEAN":
        refusals.append(
            f"GitHub reports the merge state as {merge_state}, not CLEAN")

    if review == "CHANGES_REQUESTED":
        refusals.append("a reviewer has asked for changes")
    elif review == "REVIEW_REQUIRED":
        refusals.append("a required review has not happened yet")

    if not checks:
        refusals.append(
            "no checks reported on this pull request at all — a repository "
            "with no CI is never merged automatically")
    else:
        counts = {"passing": 0, "tolerated": 0, "failing": 0, "pending": 0,
                  "unknown": 0}
        for check in checks:
            counts[check.verdict] += 1
        if counts["failing"]:
            names = ", ".join(c.name for c in checks
                              if c.verdict == "failing")[:200]
            refusals.append(f"{counts['failing']} check(s) failed: {names}")
        if counts["pending"]:
            names = ", ".join(c.name for c in checks
                              if c.verdict == "pending")[:200]
            refusals.append(
                f"{counts['pending']} check(s) have not finished: {names}")
        if counts["unknown"]:
            names = ", ".join(f"{c.name}={c.conclusion or c.status or '?'}"
                              for c in checks if c.verdict == "unknown")[:200]
            refusals.append(
                f"{counts['unknown']} check(s) reported a state this gate "
                f"does not recognise: {names}")
        if not counts["passing"]:
            # Every check skipped is the empty rollup wearing a hat.
            refusals.append(
                "no check actually passed; every one was skipped or neutral")

    return CheckStatus(
        number=number, state=state or "OPEN", draft=draft,
        mergeable=mergeable, merge_state=merge_state, review_decision=review,
        checks=checks, refusals=tuple(refusals),
        title=str(payload.get("title") or ""),
        url=str(payload.get("url") or ""),
        head=str(payload.get("headRefName") or ""),
        base=str(payload.get("baseRefName") or ""))


# =========================================================================
# Results
# =========================================================================


def _still_settling(status: CheckStatus) -> bool:
    """Whether waiting longer could plausibly change the verdict.

    Three things are worth waiting on: a check that has not finished, a merge
    state GitHub is still computing, and a rollup that is empty *so far* —
    workflows take a few seconds to register after a push, and refusing on the
    first poll would make every fast `ship` fail for a repository that does
    have CI. Nothing else improves with time: a failed check does not un-fail,
    and a draft does not undraft itself.
    """
    if any(c.verdict == "pending" for c in status.checks):
        return True
    if not status.checks:
        return True
    return status.mergeable in ("", "UNKNOWN")


@dataclass(frozen=True)
class BranchResult:
    name: str
    created: bool
    previous: str = ""
    base: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"branch": self.name, "created": self.created,
                "previous": self.previous, "base": self.base}


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    branch: str
    files: tuple[str, ...] = ()
    left_alone: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"sha": self.sha, "subject": self.subject,
                "branch": self.branch, "files": list(self.files),
                "left_alone": list(self.left_alone)}


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    title: str = ""
    head: str = ""
    base: str = ""
    draft: bool = False
    created: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "url": self.url, "title": self.title,
                "head": self.head, "base": self.base, "draft": self.draft,
                "created": self.created}


@dataclass(frozen=True)
class MergeResult:
    number: int
    merged: bool
    method: str = ""
    url: str = ""
    branch_deleted: bool = False
    status: CheckStatus | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "merged": self.merged,
                "method": self.method, "url": self.url,
                "branch_deleted": self.branch_deleted,
                "checks": self.status.to_dict() if self.status else None}


@dataclass(frozen=True)
class Issue:
    number: int
    url: str
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "url": self.url, "title": self.title}


_NUMBER_RE = re.compile(r"/(?:pull|issues)/(\d+)\b")


def _number_from(url: str) -> int:
    hit = _NUMBER_RE.search(url or "")
    return int(hit.group(1)) if hit else 0


def _url_in(text: str) -> str:
    for word in (text or "").split():
        if word.startswith("https://"):
            return word.rstrip(".,")
    return ""


# =========================================================================
# The repository
# =========================================================================


class Repo:
    """One checkout, and the whole workflow over it.

    Built per operation rather than held: a daemon that cached one of these
    would be caching the branch the user was on ten minutes ago.
    """

    def __init__(self, path: str | Path | None = None, *,
                 git_bin: str | None = None, gh_bin: str | None = None,
                 notify_bin: str | None = None,
                 timeout: float | None = None,
                 audit: audit_mod.AuditLog | None = None,
                 confirm: confirm_mod.ConfirmBroker | None = None,
                 settings: settings_mod.Settings | None = None,
                 run: Runner | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.path = Path(path) if path is not None else Path.cwd()
        # Late reads, never signature defaults. `tests/test_guards.py`
        # asserts this by `inspect.signature`, and it is not a formality: a
        # `gh` bound at import is a `gh` the suite cannot disarm, and the thing
        # it would then do is open a pull request on the user's own account.
        self.git_bin = git_bin or config.GIT_BIN
        self.gh_bin = gh_bin or config.GH_BIN
        self.notify_bin = notify_bin or config.NOTIFY_BIN
        self.timeout = float(timeout if timeout is not None
                             else config.VCS_TIMEOUT_S)
        self.runner: Runner = run if run is not None else run_process
        self.clock = clock
        self.sleep = sleep
        self.audit = audit if audit is not None else audit_mod.audit()
        self._confirm = confirm
        self._settings = settings
        self._root: Path | None = None
        self._slug: str | None = None
        self._default: str | None = None

    # -- plumbing --------------------------------------------------------

    @property
    def settings(self) -> settings_mod.Settings:
        return (self._settings if self._settings is not None
                else settings_mod.settings())

    @property
    def confirm(self) -> confirm_mod.ConfirmBroker:
        if self._confirm is None:
            self._confirm = confirm_mod.ConfirmBroker(audit=self.audit)
        return self._confirm

    def setting(self, dotted: str, fallback: Any) -> Any:
        value = self.settings.get(dotted, None)
        return fallback if value is None else value

    def _run(self, argv: Sequence[str], *, cwd: Path | None = None,
             timeout: float | None = None) -> Ran:
        return self.runner(list(argv), cwd or self.path,
                           float(timeout if timeout is not None
                                 else self.timeout))

    def git(self, *args: str, check: bool = True,
            timeout: float | None = None) -> Ran:
        ran = self._run([self.git_bin, "-C", str(self.root), *args],
                        timeout=timeout)
        if check and not ran.ok:
            raise VcsError(f"git {' '.join(args[:3])} exited "
                           f"{ran.returncode}: {ran.message}",
                           returncode=ran.returncode, stderr=ran.message)
        return ran

    def gh(self, *args: str, check: bool = True,
           timeout: float | None = None) -> Ran:
        ran = self._run([self.gh_bin, *args], cwd=self.root, timeout=timeout)
        if check and not ran.ok:
            raise VcsError(f"gh {' '.join(args[:3])} exited "
                           f"{ran.returncode}: {ran.message}",
                           returncode=ran.returncode, stderr=ran.message)
        return ran

    def gh_json(self, *args: str) -> Any:
        ran = self.gh(*args)
        try:
            return json.loads(ran.out or "null")
        except json.JSONDecodeError as exc:
            raise VcsError(
                f"gh {' '.join(args[:3])} did not return JSON: {ran.out[:200]}"
            ) from exc

    # -- where we are ----------------------------------------------------

    @property
    def root(self) -> Path:
        """The top of the checkout. Resolved once, then remembered."""
        if self._root is None:
            ran = self._run([self.git_bin, "-C", str(self.path), "rev-parse",
                             "--show-toplevel"], cwd=self.path)
            if not ran.ok or not ran.out:
                raise NotARepository(
                    f"{self.path} is not inside a git repository "
                    f"({ran.message or 'no toplevel'}). Luna commits work into "
                    "a repository or not at all.", path=str(self.path))
            self._root = Path(ran.out)
        return self._root

    def is_repo(self) -> bool:
        try:
            return self.root.is_dir()
        except VcsError:
            return False

    def available(self) -> tuple[bool, str]:
        """Whether the whole workflow can run here, and what is missing."""
        if not shutil.which(self.git_bin):
            return False, f"{self.git_bin} is not on PATH"
        if not shutil.which(self.gh_bin):
            return False, (f"{self.gh_bin} is not on PATH; the GitHub half of "
                           "the workflow needs the gh CLI")
        try:
            root = self.root
        except VcsError as exc:
            return False, str(exc)
        ran = self.gh("auth", "status", check=False, timeout=20.0)
        if not ran.ok:
            return False, f"gh is not authenticated: {ran.message}"
        return True, f"{root} with an authenticated gh"

    def slug(self) -> str:
        """``owner/name``, from ``gh``. Empty when there is no GitHub remote."""
        if self._slug is None:
            ran = self.gh("repo", "view", "--json", "nameWithOwner",
                          check=False)
            name = ""
            if ran.ok:
                try:
                    name = str(json.loads(ran.out or "{}").get(
                        "nameWithOwner") or "")
                except json.JSONDecodeError:
                    name = ""
            self._slug = name
        return self._slug

    def default_branch(self) -> str:
        """The branch nothing of hers is ever pushed to.

        Asked of git first and ``gh`` second, in that order on purpose: the
        local answer needs no network and is right on every clone, and falling
        back to a network call for something this important would make the
        protection depend on connectivity. ``gh`` missing entirely — no
        remote configured, no network, or simply not installed — is exactly
        the case the final guess exists for, so it is caught here rather than
        left to blow up a caller that only wanted a branch name.
        """
        if self._default is not None:
            return self._default
        ran = self.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD",
                       check=False)
        if ran.ok and ran.out:
            self._default = ran.out.split("/", 1)[-1]
            return self._default
        try:
            ran = self.gh("repo", "view", "--json", "defaultBranchRef",
                          check=False)
        except VcsUnavailable:
            ran = None
        if ran is not None and ran.ok:
            try:
                ref = json.loads(ran.out or "{}").get("defaultBranchRef") or {}
                if ref.get("name"):
                    self._default = str(ref["name"])
                    return self._default
            except json.JSONDecodeError:
                pass
        for guess in ("main", "master", "trunk"):
            if self.git("rev-parse", "--verify", "--quiet",
                        f"refs/heads/{guess}", check=False).ok:
                self._default = guess
                return self._default
        self._default = "main"
        return self._default

    def current_branch(self) -> str:
        out = self.git("rev-parse", "--abbrev-ref", "HEAD").out
        return "" if out == "HEAD" else out          # "" means detached

    def branch_exists(self, name: str) -> bool:
        return self.git("rev-parse", "--verify", "--quiet",
                        f"refs/heads/{name}", check=False).ok

    def dirty(self) -> tuple[str, ...]:
        """Every path with an uncommitted change, tracked or not."""
        ran = self.git("status", "--porcelain")
        paths: list[str] = []
        for line in ran.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:                        # a rename
                path = path.split(" -> ", 1)[1]
            paths.append(path.strip('"'))
        return tuple(sorted(set(paths)))

    def head_sha(self, short: bool = True) -> str:
        args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
        ran = self.git(*args, check=False)
        return ran.out if ran.ok else ""

    def status(self) -> dict[str, Any]:
        """One call that answers "where am I and what is open"."""
        branch = self.current_branch()
        default = self.default_branch()
        pr = self.existing_pr(branch) if branch else None
        return {"root": str(self.root), "slug": self.slug(),
                "branch": branch, "default_branch": default,
                "on_default": bool(branch) and branch == default,
                "head": self.head_sha(),
                "dirty": list(self.dirty()),
                "pull_request": pr.to_dict() if pr else None}

    # -- the workflow ----------------------------------------------------

    def branch(self, topic: str, *, base: str | None = None,
               exact: bool = False, adopt: bool = True) -> BranchResult:
        """Get onto a branch of her own, creating it if it is not there.

        ``topic`` is run through :func:`branch_name` unless it already carries
        the configured prefix or ``exact`` is set, so ``branch("github
        workflow")`` and ``branch("luna/github-workflow-260909")`` both land on
        the same branch — which is what makes a re-run resume rather than fork.

        **An existing branch is adopted, not forked.** The alternative is
        suffixing (``-2``, ``-3``), and that trades one rare wrong answer for a
        common one: adopting is wrong only if two unrelated jobs picked the
        same topic on the same day, while suffixing is wrong every single time
        a job is re-run, and litters the repository with abandoned branches
        nobody will ever delete. Pass ``adopt=False`` to make a collision an
        error instead.

        The new branch is cut from **where she already is**, not from the
        default branch, unless ``base`` says otherwise. Silently re-basing onto
        ``origin/main`` would throw away the checkout state she was handed.
        """
        prefix = str(self.setting("vcs.branch_prefix",
                                  config.VCS_BRANCH_PREFIX))
        topic = (topic or "").strip()
        if not topic:
            raise VcsError("a branch needs a topic to be named after")
        name = topic if (exact or topic.startswith(prefix + "/")) \
            else branch_name(topic, prefix=prefix)
        default = self.default_branch()
        if name == default:
            raise ProtectedBranch(
                f"{name} is this repository's default branch. Luna works on a "
                "branch of her own and opens a pull request; she does not "
                "commit here.", branch=name)

        previous = self.current_branch()
        if self.branch_exists(name):
            if not adopt:
                raise VcsError(f"branch {name} already exists",
                               branch=name)
            if previous != name:
                self.git("switch", name)
            result = BranchResult(name, created=False, previous=previous,
                                  base=base or previous)
        else:
            args = ["switch", "--create", name]
            if base:
                args.append(base)
            self.git(*args)
            result = BranchResult(name, created=True, previous=previous,
                                  base=base or previous)
        self.audit.append(
            "vcs.branch", ok=True, actor="luna",
            why=f"work on {topic}", branch=name, created=result.created,
            previous=previous, repo=str(self.root),
            undo=({"what": "delete the branch",
                   "cmd": f"git -C {self.root} branch -D {name}"}
                  if result.created else None))
        return result

    def commit(self, message: str, paths: Iterable[str], *,
               body: str = "") -> Commit:
        """Stage exactly ``paths`` and commit them with a written message.

        ``paths`` is required and there is no "everything that is dirty" mode.
        That is the whole point: an agent working in somebody's checkout cannot
        tell its own edits from a half-finished change a human left there, and
        ``git add -A`` is precisely how the second kind ends up in her pull
        request. Whatever was dirty and was not named comes back in
        :attr:`Commit.left_alone`, so the report can say what she did not touch.

        Passing ``["."]`` is the deliberate escape hatch for "all of this is
        mine" — it is explicit, it is one path like any other, and it appears
        verbatim in the audit entry.
        """
        message = (message or "").strip()
        if not message:
            raise VcsError(
                "a commit needs a message. Not 'wip', not 'fix': one line "
                "saying what changed, and why if it is not obvious.")
        wanted = [str(p) for p in paths if str(p).strip()]
        if not wanted:
            raise VcsError(
                "commit needs the paths to stage. Luna never runs "
                "`git add -A`: she may be standing in somebody else's dirty "
                "working tree, and sweeping it into her own commit is how "
                "another person's half-finished change gets pushed. "
                f"Uncommitted here right now: {', '.join(self.dirty()) or 'nothing'}")

        branch = self.current_branch()
        default = self.default_branch()
        if branch == default:
            raise ProtectedBranch(
                f"refusing to commit on {default}. Branch first: every piece "
                "of her work goes through a pull request.", branch=branch)
        if not branch:
            raise VcsError("HEAD is detached; there is no branch to commit to")

        before = set(self.dirty())
        self.git("add", "--", *wanted)
        staged = tuple(sorted(
            p for p in self.git("diff", "--cached", "--name-only").stdout.splitlines()
            if p.strip()))
        if not staged:
            raise NothingToCommit(
                f"nothing to commit from {', '.join(wanted)} — those paths are "
                "unchanged. Nothing was committed and nothing was reset.",
                paths=wanted)

        args = ["commit", "-m", message]
        if body.strip():
            args += ["-m", body.strip()]
        self.git(*args)
        sha = self.head_sha()
        left = tuple(sorted(before - set(staged)))
        self.audit.append(
            "vcs.commit", ok=True, actor="luna", why=message[:400],
            sha=sha, branch=branch, files=list(staged[:50]),
            file_count=len(staged), left_alone=list(left[:50]),
            repo=str(self.root),
            undo={"what": "undo the commit, keeping the changes staged",
                  "cmd": f"git -C {self.root} reset --soft HEAD~1"})
        if left:
            log.info("left uncommitted work alone",
                     extra={"branch": branch, "left_alone": len(left)})
        return Commit(sha=sha, subject=message.splitlines()[0], branch=branch,
                      files=staged, left_alone=left)

    def push(self, branch: str | None = None, *, remote: str = "origin",
             actor: str = "luna", why: str = "") -> str:
        """Push a branch, never the default one.

        Gated on ``[confirm] git_push``, which is the existing policy class and
        already matches ``git push`` in the classifier — this calls the broker
        directly rather than going through the text classifier, because a
        structured action should not have to be recognised by a regular
        expression when the caller knows exactly what it is doing.
        """
        branch = branch or self.current_branch()
        if not branch:
            raise VcsError("HEAD is detached; there is no branch to push")
        default = self.default_branch()
        if branch == default:
            raise ProtectedBranch(
                f"refusing to push {branch}: it is the default branch. Her "
                "work reaches main through a reviewed pull request or not at "
                "all.", branch=branch)

        detail = f"git push -u {remote} {branch}"
        decision = self.confirm.check(
            "git_push", detail, actor=actor,
            why=why or f"push {branch} to {remote}")
        if not decision.allowed:
            raise confirm_mod.ConfirmDenied(decision)

        self.git("push", "--set-upstream", remote, branch, timeout=self.timeout)
        self.audit.append("vcs.push", ok=True, actor=actor,
                          why=why or f"push {branch}", branch=branch,
                          remote=remote, repo=str(self.root),
                          sha=self.head_sha(),
                          undo={"what": "delete the remote branch",
                                "cmd": f"git -C {self.root} push {remote} "
                                       f"--delete {branch}"})
        return branch

    # -- pull requests ---------------------------------------------------

    def existing_pr(self, branch: str) -> PullRequest | None:
        """The open pull request for ``branch``, if there already is one."""
        if not branch:
            return None
        ran = self.gh("pr", "list", "--head", branch, "--state", "open",
                      "--limit", "1", "--json",
                      "number,url,title,isDraft,headRefName,baseRefName",
                      check=False)
        if not ran.ok:
            return None
        try:
            rows = json.loads(ran.out or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        return PullRequest(number=int(row.get("number") or 0),
                           url=str(row.get("url") or ""),
                           title=str(row.get("title") or ""),
                           head=str(row.get("headRefName") or branch),
                           base=str(row.get("baseRefName") or ""),
                           draft=bool(row.get("isDraft")),
                           created=False)

    def pull_request(self, title: str, body: str, *, base: str | None = None,
                     head: str | None = None, draft: bool = False,
                     actor: str = "luna", why: str = "") -> PullRequest:
        """Open a pull request, or return the one that is already open.

        **A second pull request is never opened for the same branch.** GitHub
        will happily let you, and the result is two review threads over one
        change that a human then has to reconcile; returning the existing one
        with ``created=False`` is the answer a re-run wants anyway. Its
        description is left exactly as it is, because by then it may have been
        edited by the person reviewing it.

        The description is required to be a real one. A pull request body that
        restates the diff tells a reviewer nothing they could not get from the
        diff; what it has to carry is what changed and why.
        """
        head = head or self.current_branch()
        if not head:
            raise VcsError("HEAD is detached; there is no branch to propose")
        base = base or self.default_branch()
        if head == base:
            raise ProtectedBranch(
                f"cannot open a pull request from {head} into itself",
                branch=head)

        title = (title or "").strip()
        body = (body or "").strip()
        if not title:
            raise VcsError("a pull request needs a title")
        if len(body) < 40:
            raise VcsError(
                "a pull request needs a description saying what changed and "
                "why — at least a couple of sentences. A body that restates "
                "the diff is not one.", body_chars=len(body))

        existing = self.existing_pr(head)
        if existing is not None:
            self.audit.append("vcs.pr.exists", ok=True, actor=actor,
                              why=why or "a pull request was already open",
                              number=existing.number, url=existing.url,
                              branch=head, repo=str(self.root))
            return existing

        detail = f"gh pr create --head {head} --base {base}: {title}"
        decision = self.confirm.check("git_push", detail, actor=actor,
                                      why=why or f"open a pull request for {head}")
        if not decision.allowed:
            raise confirm_mod.ConfirmDenied(decision)

        args = ["pr", "create", "--title", title, "--body", body,
                "--base", base, "--head", head]
        if draft:
            args.append("--draft")
        ran = self.gh(*args)
        url = _url_in(ran.stdout) or _url_in(ran.stderr)
        number = _number_from(url)
        if not number:
            # gh printed something we could not read. Ask rather than guess:
            # a wrong number here would later be a merge of the wrong PR.
            found = self.existing_pr(head)
            if found is None:
                raise VcsError(
                    "gh pr create returned no pull request URL and none is "
                    f"open for {head}: {ran.message or ran.out[:200]}")
            number, url = found.number, found.url
        pr = PullRequest(number=number, url=url, title=title, head=head,
                         base=base, draft=draft, created=True)
        self.audit.append("vcs.pr.opened", ok=True, actor=actor,
                          why=why or title[:400], number=number, url=url,
                          branch=head, base=base, draft=draft,
                          repo=str(self.root),
                          undo={"what": "close the pull request",
                                "cmd": f"gh pr close {number}"})
        return pr

    def pr_number(self, number: int | None = None) -> int:
        """Resolve a pull request number, defaulting to the current branch's."""
        if number:
            return int(number)
        branch = self.current_branch()
        found = self.existing_pr(branch)
        if found is None:
            raise VcsError(
                f"no open pull request for {branch or 'this detached HEAD'}; "
                "say which one, or open one first.", branch=branch)
        return found.number

    def checks(self, number: int | None = None, *, wait: float = 0.0,
               poll: float | None = None) -> CheckStatus:
        """Read the check status, optionally waiting for it to settle.

        Waiting stops as soon as the verdict can no longer change: green, or
        refused for a reason that is not "still running". A pull request whose
        checks never start therefore costs the whole wait and then refuses,
        which is correct — an unattended merge that gave up waiting and merged
        anyway is the exact failure this module exists to prevent.
        """
        number = self.pr_number(number)
        interval = float(poll if poll is not None else config.VCS_CHECK_POLL_S)
        deadline = self.clock() + max(0.0, float(wait))
        polls = 0
        while True:
            payload = self.gh_json("pr", "view", str(number), "--json",
                                   ",".join(PR_FIELDS))
            status = evaluate(payload if isinstance(payload, dict) else {},
                              number=number)
            polls += 1
            if status.green or not _still_settling(status):
                break
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.sleep(min(interval, remaining))
        self.audit.append("vcs.checks", ok=status.green, actor="luna",
                          why=f"check status for #{number}",
                          number=number, green=status.green,
                          refusals=list(status.refusals) or None,
                          counts=status.counts(), polls=polls,
                          repo=str(self.root))
        return status

    def merge(self, number: int | None = None, *, method: str | None = None,
              delete_branch: bool | None = None, wait: float = 0.0,
              actor: str = "luna", why: str = "") -> MergeResult:
        """Merge — and only if the checks are green.

        The green gate runs **before** the confirmation gate, deliberately: if
        the answer is already no there is nothing worth putting a question on
        the user's screen about, and asking would train them to click through a
        prompt whose answer does not matter.

        Refusal leaves the pull request open, records why, and (by default)
        says so on the desktop. It never closes anything and never retries.
        """
        number = self.pr_number(number)
        status = self.checks(number, wait=wait)
        if not status.green:
            self.audit.append(
                "vcs.merge.refused", ok=False, actor=actor,
                why=why or f"auto-merge refused for #{number}",
                number=number, refusals=list(status.refusals),
                counts=status.counts(), url=status.url, repo=str(self.root),
                undo=None)
            log.warning("refusing to merge a pull request that is not green",
                        extra={"number": number,
                               "refusals": "; ".join(status.refusals)[:400]})
            self.notify_refusal(status)
            raise NotGreen(status)

        method = str(method or self.setting("vcs.merge_method",
                                            config.VCS_MERGE_METHOD)).lower()
        if method not in MERGE_METHODS:
            raise VcsError(f"unknown merge method {method!r}; expected one of "
                           + ", ".join(MERGE_METHODS))
        delete = bool(self.setting("vcs.delete_branch",
                                   config.VCS_DELETE_BRANCH)
                      if delete_branch is None else delete_branch)

        detail = f"gh pr merge {number} --{method}"
        decision = self.confirm.check(
            "git_merge", detail, actor=actor,
            why=why or f"merge #{number}, checks green")
        if not decision.allowed:
            raise confirm_mod.ConfirmDenied(decision)

        args = ["pr", "merge", str(number), f"--{method}"]
        if delete:
            args.append("--delete-branch")
        self.gh(*args)
        self.audit.append("vcs.merged", ok=True, actor=actor,
                          why=why or f"#{number} merged, checks green",
                          number=number, method=method,
                          branch_deleted=delete, url=status.url,
                          checks=status.counts(), repo=str(self.root),
                          undo={"what": "revert the merge commit on the "
                                        "default branch",
                                "cmd": f"gh pr view {number} --json mergeCommit"})
        return MergeResult(number=number, merged=True, method=method,
                           url=status.url, branch_deleted=delete,
                           status=status)

    # -- issues ----------------------------------------------------------

    def open_issue(self, title: str, body: str = "", *,
                   labels: Sequence[str] = (), actor: str = "luna") -> Issue:
        """Open an issue. Not gated: an issue changes no code and no history."""
        title = (title or "").strip()
        if not title:
            raise VcsError("an issue needs a title")
        args = ["issue", "create", "--title", title,
                "--body", body.strip() or title]
        for label in labels:
            args += ["--label", str(label)]
        ran = self.gh(*args)
        url = _url_in(ran.stdout) or _url_in(ran.stderr)
        issue = Issue(number=_number_from(url), url=url, title=title)
        self.audit.append("vcs.issue.opened", ok=True, actor=actor,
                          why=title[:400], number=issue.number, url=url,
                          labels=list(labels) or None, repo=str(self.root),
                          undo={"what": "close the issue",
                                "cmd": f"gh issue close {issue.number}"})
        return issue

    def comment(self, number: int, body: str, *, on: str = "issue",
                actor: str = "luna") -> str:
        """Comment on an issue or a pull request. Returns the comment URL."""
        body = (body or "").strip()
        if not body:
            raise VcsError("a comment needs something to say")
        if on not in ("issue", "pr"):
            raise VcsError(f"unknown comment target {on!r}; expected issue or pr")
        ran = self.gh(on, "comment", str(int(number)), "--body", body)
        url = _url_in(ran.stdout) or _url_in(ran.stderr)
        self.audit.append("vcs.comment", ok=True, actor=actor,
                          why=body[:400], number=int(number), on=on, url=url,
                          repo=str(self.root), undo=None)
        return url

    # -- the whole thing -------------------------------------------------

    def ship(self, *, topic: str, message: str, paths: Iterable[str],
             title: str | None = None, body: str = "",
             base: str | None = None, draft: bool = False,
             wait: float | None = None, merge: bool | None = None,
             actor: str = "luna", why: str = "") -> dict[str, Any]:
        """Branch, commit, push, open a pull request, and merge if it is green.

        Returns a report rather than raising when the merge is refused: the
        pull request is open, which is the *successful* outcome of everything
        up to that point, and the refusal is a state the caller has to be told
        about rather than an exception that discards the work.
        """
        auto = bool(self.setting("vcs.auto_merge", config.VCS_AUTO_MERGE)
                    if merge is None else merge)
        patience = float(wait if wait is not None
                         else self.setting("vcs.check_wait_seconds",
                                           config.VCS_CHECK_WAIT_S))
        branch = self.branch(topic, base=base)
        commit = self.commit(message, paths, body=body)
        self.push(branch.name, actor=actor, why=why or topic)
        pr = self.pull_request(
            title or message.splitlines()[0], body or _default_body(topic, commit),
            base=base, head=branch.name, draft=draft, actor=actor,
            why=why or topic)

        report: dict[str, Any] = {"branch": branch.to_dict(),
                                  "commit": commit.to_dict(),
                                  "pull_request": pr.to_dict(),
                                  "merged": False, "refusals": [],
                                  "checks": None}
        if draft or not auto:
            report["note"] = ("left open on purpose: "
                              + ("it is a draft" if draft
                                 else "[vcs] auto_merge is off"))
            return report
        try:
            result = self.merge(pr.number, wait=patience, actor=actor,
                                why=why or f"merge #{pr.number}")
        except NotGreen as exc:
            report["refusals"] = list(exc.status.refusals)
            report["checks"] = exc.status.to_dict()
            report["note"] = ("the pull request is open and was not merged: "
                              + "; ".join(exc.status.refusals))
            return report
        report["merged"] = True
        report["checks"] = result.status.to_dict() if result.status else None
        report["merge"] = result.to_dict()
        return report

    # -- telling the user ------------------------------------------------

    def notify_refusal(self, status: CheckStatus) -> bool:
        """Toast a refused auto-merge. Returns whether it notified.

        On by default, and the reasoning is the same one that put
        ``[ui] notify_on_finish`` there: a job whose window is hidden and whose
        pull request quietly stayed open is a job the user finds out about days
        later. A refusal is also the only outcome here that needs a person —
        a green merge needs nobody — so it is the one worth the interruption.
        Failure to notify is logged and swallowed; a desktop that cannot show a
        toast is not a merge that should have happened.
        """
        if not bool(self.setting("vcs.notify_on_refusal",
                                 config.VCS_NOTIFY_ON_REFUSAL)):
            return False
        who = settings_mod.assistant_name()
        headline = f"{who}: #{status.number} not merged"
        body = ("; ".join(status.refusals) or "checks are not green")[:160]
        argv = [self.notify_bin, "--app-name", "Jarvis", "-u", "critical",
                "-g", "󰀦", headline, body]
        try:
            proc = safety.spawn(argv, kind="vcs-notify", durable=False,
                                note=f"merge refused for #{status.number}",
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        except (OSError, safety.SignalRefused) as exc:
            log.warning("could not send the merge-refused notification",
                        extra={"detail": str(exc), "bin": self.notify_bin,
                               "number": status.number})
            return False
        threading.Thread(target=_reap_after, args=(proc,), daemon=True,
                         name=f"luna-vcs-notify-{status.number}").start()
        return True


def _default_body(topic: str, commit: Commit) -> str:
    """A last-resort description. Deliberately dull, and deliberately not a diff.

    Used only when a caller gave none. It says what the change is for and what
    it touched; it does not attempt to summarise the diff, because a bad
    summary of a diff is worse for a reviewer than no summary at all.
    """
    files = "\n".join(f"- `{p}`" for p in commit.files[:20])
    more = (f"\n- …and {len(commit.files) - 20} more"
            if len(commit.files) > 20 else "")
    return (f"## What this is\n\n{topic.strip()}\n\n"
            f"## Why\n\n{commit.subject}\n\n"
            f"## Files touched\n\n{files}{more}\n\n"
            f"Opened by {settings_mod.assistant_name()} from "
            f"`{commit.branch}`. Review before merging.")


def _reap_after(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    safety.reap(proc)
