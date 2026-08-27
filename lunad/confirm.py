"""Confirmation. "Not proper constraints — just double-check before doing X."

The user revised the Phase-2 position. Full autonomy stays the default shape:
Jarvis is not sandboxed, is not asked to justify herself, and does not stop to
ask about ordinary work. But a handful of action classes now get a yes/no
first, and which ones is a setting rather than a rule in the code.

Three layers, with genuinely different strengths, and it matters which is
which:

**1. Hard denies — in code, not in the config.** Four things Jarvis refuses
outright and the settings file cannot re-enable: signalling a process she did
not spawn, restarting ``omarchy-shell``, deleting the machine's own
customisation log, and ``rm -rf`` outside her own directories. The first is not
implemented here at all — it is :mod:`lunad.safety`, which every signal in the
package already goes through, and duplicating it here would create a second
copy of a rule that must never disagree with itself. The other three are text
patterns checked before dispatch.

**2. Policy classes — in the config.** ``install_packages``, ``delete_files``,
``write_outside_home``, ``system_config``, ``network_send``, ``git_push``,
``long_job``, ``spend``. Each resolves to ``never`` (just do it), ``ask``
(confirm first) or ``deny`` (refuse). Asking happens on the desktop, through
Omarchy's own notification with a click action, and/or in the terminal.

**3. The tool-side gate — advisory.** A dispatched agent runs with real tools
and bypassed permissions. Its system prompt tells it to call
``luna confirm ask <class> "<what>"`` before doing anything in a class set to
``ask``, and that command reaches this broker properly. An agent that ignores
the instruction is not stopped by anything here. That limitation is stated in
the prompt, in ARCHITECTURE.md and in the report, because a confirmation system
whose guarantee is overstated is worse than one with no guarantee at all: it
invites the user to relax about the thing it cannot actually prevent.

What *is* genuinely enforced: the daemon's own dispatch path. Nothing gets
spawned by ``lunad`` without passing the classifier first.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import audit as audit_mod, config, safety, settings as settings_mod

log = logging.getLogger("lunad.confirm")

NEVER, ASK, DENY = "never", "ask", "deny"


class ConfirmDenied(PermissionError):
    """The action was refused — by policy, by the user, or by a hard deny."""

    kind = "ConfirmDenied"

    def __init__(self, decision: "Decision") -> None:
        super().__init__(decision.message())
        self.decision = decision

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.kind, "message": str(self),
                "decision": self.decision.to_dict()}


# =========================================================================
# Action classes
# =========================================================================

# Order matters only for the summary line; every matching class is checked.
# The patterns are deliberately generous. A false positive costs one toast the
# user clicks; a false negative costs the thing the toast existed to prevent.

_CLASSIFIERS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("install_packages", re.compile(
        r"\b(?:pacman\s+-S|yay\b|paru\b|apt(?:-get)?\s+install|dnf\s+install"
        r"|flatpak\s+install|pip3?\s+install|uv\s+(?:pip\s+)?(?:install|add)"
        r"|npm\s+(?:i|install)\b|pnpm\s+add|yarn\s+add|cargo\s+install"
        r"|gem\s+install|go\s+install|makepkg)\b", re.IGNORECASE),
     "installs software"),
    ("delete_files", re.compile(
        r"(?:\brm\s+-|\brm\s+[/~.\w]|\brmdir\b|\bunlink\b|\bshred\b"
        r"|\bfind\b[^\n]*-delete\b|\bgit\s+clean\b"
        r"|\b(?:delete|remove|wipe|purge)\b[^\n]{0,40}?"
        r"\b(?:file|files|directory|directories|folder|folders)\b)",
        re.IGNORECASE),
     "deletes files"),
    ("write_outside_home", re.compile(
        r"(?:>\s*|\b(?:write|create|edit|modify|append|touch|install|copy|cp"
        r"|mv|move|tee|chmod|chown)\b[^\n]{0,40}?)"
        r"(?:/etc/|/usr/|/opt/|/srv/|/var/|/boot/|/root/)", re.IGNORECASE),
     "writes outside your home directory"),
    ("system_config", re.compile(
        r"(?:\bsystemctl\s+(?!--user\s+(?:status|is-active|show|cat|list)\b)"
        r"|\bsystemd\b[^\n]{0,30}\bunit\b|/etc/systemd/|\bjournalctl\s+--vacuum"
        r"|~?/\.config/hypr/|\bhyprland\b[^\n]{0,20}\bconfig\b"
        r"|\bsudo\b|\bmkinitcpio\b|\bgrub-mkconfig\b|/etc/)",
        re.IGNORECASE),
     "changes system configuration"),
    ("network_send", re.compile(
        r"(?:\bcurl\b[^\n]*(?:-X\s*(?:POST|PUT|PATCH)|--data|-d\s|-F\s|-T\s|--upload)"
        r"|\bwget\b[^\n]*--post|\bscp\b|\brsync\b[^\n]*\S+@|\bsftp\b"
        r"|\b(?:upload|post|publish|send)\b[^\n]{0,30}?"
        r"\b(?:to|onto)\b[^\n]{0,30}?\b(?:api|server|endpoint|s3|bucket|gist|pastebin)\b)",
        re.IGNORECASE),
     "sends data off this machine"),
    ("git_push", re.compile(
        r"(?:\bgit\s+push\b|\bgh\s+(?:pr\s+create|release\s+create)\b"
        r"|\bgit\s+\S+\s+--force\b|\bpush\b[^\n]{0,20}\b(?:to\s+)?(?:origin|remote|upstream)\b)",
        re.IGNORECASE),
     "pushes to a git remote"),
)

# Classes that no amount of reading the task text can detect: they are decided
# by a number the caller supplies, not by words.
_MEASURED = ("long_job", "spend")

CLASSES: tuple[str, ...] = tuple(name for name, _, _ in _CLASSIFIERS) + _MEASURED

_REASONS = {name: why for name, _, why in _CLASSIFIERS} | {
    "long_job": "runs for a long time",
    "spend": "costs money",
}


# =========================================================================
# Hard denies — code, not config
# =========================================================================


@dataclass(frozen=True)
class HardDeny:
    name: str
    why: str
    test: Callable[[str], bool]


def _mentions_shell_restart(text: str) -> bool:
    """Restarting omarchy-shell takes the user's whole desktop with it."""
    return bool(re.search(
        r"\b(?:restart|reload|kill|stop|systemctl\s+(?:--user\s+)?restart)\b"
        r"[^\n]{0,30}\bomarchy-shell\b"
        r"|\bomarchy-shell\b[^\n]{0,20}\b(?:restart|reload)\b"
        r"|\bomarchy\s+restart\s+shell\b",
        text, re.IGNORECASE))


def _deletes_customisations(text: str) -> bool:
    """CUSTOMISATIONS.md is the machine's memory of what was done to it."""
    if "CUSTOMISATIONS.md" not in text:
        return False
    return bool(re.search(
        r"\b(?:rm|rmdir|unlink|shred|delete|remove|truncate|overwrite|wipe)\b"
        r"[^\n]{0,60}CUSTOMISATIONS\.md"
        r"|CUSTOMISATIONS\.md[^\n]{0,40}\b(?:deleted|removed|wiped)\b"
        r"|>\s*[^\n]{0,60}CUSTOMISATIONS\.md",
        text, re.IGNORECASE))


def _own_dirs() -> tuple[str, ...]:
    """The directories a recursive delete is allowed to name.

    Jarvis's state tree and her config directory, and nothing else — not the
    project checkout, which is the user's git repository and not scratch space.
    """
    return tuple(str(p).rstrip("/") for p in
                 (config.STATE_DIR, config.JOBS_DIR, config.CONFIG_DIR))


_RM_RF_RE = re.compile(
    r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*[fF][a-zA-Z]*"
    r"|\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[fF][a-zA-Z]*[rR][a-zA-Z]*"
    r"|\brm\s+(?:--recursive|--force)\b")


def _target_of_rm(text: str, at: int) -> list[str]:
    """The path-looking words after an ``rm -rf`` occurrence."""
    tail = text[at:].splitlines()[0] if at < len(text) else ""
    words = tail.split()[1:]          # drop `rm` itself
    return [w.strip("'\"`;&|)") for w in words if not w.startswith("-")]


def _rm_rf_outside_own_dirs(text: str) -> bool:
    home = str(Path.home()).rstrip("/")
    allowed = _own_dirs()
    for match in _RM_RF_RE.finditer(text):
        targets = _target_of_rm(text, match.start())
        if not targets:
            # `rm -rf` with no visible target in the task text is not proof of
            # safety; the agent will supply one we never see. Treat as outside.
            return True
        for raw in targets:
            path = raw.replace("$HOME", home)
            if path.startswith("~"):
                path = home + path[1:]
            path = path.rstrip("/")
            if not path or path in ("/", home, ".", ".."):
                return True
            if not any(path == d or path.startswith(d + "/") for d in allowed):
                return True
    return False


HARD_DENIES: tuple[HardDeny, ...] = (
    HardDeny("restart_omarchy_shell",
             "restarting omarchy-shell takes the user's desktop down with it",
             _mentions_shell_restart),
    HardDeny("delete_customisations",
             "CUSTOMISATIONS.md is the record of every change made to this "
             "machine; without it a change cannot be undone",
             _deletes_customisations),
    HardDeny("rm_rf_outside_own_dirs",
             "a recursive delete outside Jarvis's own directories",
             _rm_rf_outside_own_dirs),
)

# The fourth hard deny lives in lunad.safety and is not re-implemented here.
SIGNAL_HARD_DENY = (
    "signalling a process Jarvis did not spawn — enforced by "
    "lunad.safety.may_signal, which every signal in the package goes through"
)


def hard_denials(text: str) -> list[HardDeny]:
    """Which hard denies this text trips. Never configurable."""
    return [rule for rule in HARD_DENIES if rule.test(text or "")]


# =========================================================================
# Classification
# =========================================================================


def classify(text: str, *, seconds: float | None = None,
             usd: float | None = None,
             long_job_seconds: float | None = None,
             spend_threshold: float | None = None) -> list[str]:
    """Which policy classes a piece of work falls into.

    Text for the six readable classes; explicit numbers for the two that are
    not readable. A task that says nothing about cost is not free — it is
    unmeasured, and unmeasured is not the same as under the threshold, so an
    absent number simply does not trigger the class rather than passing it.
    """
    text = text or ""
    hits = [name for name, pattern, _ in _CLASSIFIERS if pattern.search(text)]
    if seconds is not None:
        limit = (long_job_seconds if long_job_seconds is not None
                 else settings_mod.get("confirm.long_job_seconds", 300))
        if float(seconds) > float(limit):
            hits.append("long_job")
    if usd is not None:
        limit = (spend_threshold if spend_threshold is not None
                 else settings_mod.get("confirm.spend_threshold", 0.25))
        if float(usd) > float(limit):
            hits.append("spend")
    return hits


def describe(action: str) -> str:
    return _REASONS.get(action, action.replace("_", " "))


# =========================================================================
# A decision
# =========================================================================


@dataclass
class Decision:
    action: str
    policy: str
    allowed: bool
    outcome: str                    # auto | approved | denied | timeout | hard
    detail: str = ""
    why: str = ""
    token: str = ""
    channel: str = ""
    waited_s: float = 0.0
    rule: str = ""                  # the hard-deny rule, when outcome == hard

    def message(self) -> str:
        name = settings_mod.assistant_name()
        if self.outcome == "hard":
            return (f"{name} will not do that: {self.rule}. "
                    "This one is not a setting.")
        if self.outcome == "denied":
            return f"declined — {describe(self.action)}"
        if self.outcome == "timeout":
            return (f"no answer in time, so that is a no — "
                    f"{describe(self.action)}")
        if self.policy == DENY:
            return (f"{describe(self.action)} is set to deny in "
                    f"[confirm] {self.action}")
        return f"{describe(self.action)}: allowed"

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "policy": self.policy,
                "allowed": self.allowed, "outcome": self.outcome,
                "detail": self.detail, "why": self.why, "token": self.token,
                "channel": self.channel, "waited_s": round(self.waited_s, 2),
                "rule": self.rule, "message": self.message()}


@dataclass
class Pending:
    """A question on screen, waiting for a click or a command."""

    token: str
    action: str
    detail: str
    why: str
    asked: float = field(default_factory=time.time)
    timeout_s: float = 60.0
    answer: bool | None = None
    answered_by: str = ""
    event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict[str, Any]:
        return {"token": self.token, "action": self.action,
                "detail": self.detail, "why": self.why,
                "asked": round(self.asked, 3),
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.localtime(self.asked)),
                "age_s": round(time.time() - self.asked, 1),
                "timeout_s": self.timeout_s,
                "remaining_s": round(max(0.0, self.timeout_s
                                         - (time.time() - self.asked)), 1),
                "answered": self.answer}


# =========================================================================
# The broker
# =========================================================================


class ConfirmBroker:
    """Resolves a policy, asks if it must, and records what happened."""

    def __init__(self, *, settings: settings_mod.Settings | None = None,
                 audit: audit_mod.AuditLog | None = None,
                 notify_bin: str | None = None,
                 asker: Callable[[Pending, str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self.audit = audit if audit is not None else audit_mod.audit()
        # Late read, not a signature default: see Dispatcher.__init__. A
        # default bound at import is unpatchable, and an unpatchable notifier
        # means the test suite toasts the user.
        self.notify_bin = notify_bin or config.NOTIFY_BIN
        # Injectable so the suite can exercise every branch without a desktop.
        self.asker = asker if asker is not None else self._ask_on_desktop
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, Pending] = {}
        self.counters = {"auto": 0, "asked": 0, "approved": 0, "denied": 0,
                         "timeout": 0, "hard": 0}

    # -- settings --------------------------------------------------------

    @property
    def settings(self) -> settings_mod.Settings:
        return self._settings if self._settings is not None else settings_mod.settings()

    def policy(self, action: str) -> str:
        """The configured policy for one class. Unknown classes are asked.

        Failing to ``ask`` rather than to ``never`` is the only defensible
        default for something the classifier produced but the schema does not
        list: a class nobody has a policy for is exactly the case where the
        user should be the one deciding.
        """
        if action not in CLASSES:
            return ASK
        value = self.settings.get(f"confirm.{action}", ASK)
        return value if value in (NEVER, ASK, DENY) else ASK

    def _prompt_config(self) -> tuple[float, bool, str]:
        timeout = float(self.settings.get("confirm.prompt.timeout_seconds", 60))
        default_yes = str(
            self.settings.get("confirm.prompt.default_on_timeout", "no")) == "yes"
        channel = str(self.settings.get("confirm.prompt.channel", "notification"))
        return timeout, default_yes, channel

    # -- the gate --------------------------------------------------------

    def check(self, action: str, detail: str = "", *, why: str = "",
              actor: str = "luna") -> Decision:
        """Resolve one action class. Never raises; the caller decides."""
        policy = self.policy(action)
        if policy == NEVER:
            decision = Decision(action, policy, True, "auto", detail, why)
            self.counters["auto"] += 1
            self._record(decision, actor)
            return decision
        if policy == DENY:
            decision = Decision(action, policy, False, "denied", detail, why)
            self.counters["denied"] += 1
            self._record(decision, actor)
            return decision
        return self._ask(action, detail, why, actor)

    def gate(self, text: str, *, why: str = "", actor: str = "luna",
             seconds: float | None = None,
             usd: float | None = None) -> list[Decision]:
        """Classify a piece of work and resolve every class it falls into.

        Raises :class:`ConfirmDenied` on the first refusal. Hard denies are
        checked first and are never asked about: a question the user cannot
        answer with "yes" is not a question.
        """
        text = text or ""
        for rule in hard_denials(text):
            decision = Decision(rule.name, DENY, False, "hard", text[:200],
                                why, rule=rule.why)
            self.counters["hard"] += 1
            self._record(decision, actor)
            raise ConfirmDenied(decision)

        decisions: list[Decision] = []
        for action in classify(text, seconds=seconds, usd=usd):
            decision = self.check(action, text[:200], why=why, actor=actor)
            decisions.append(decision)
            if not decision.allowed:
                raise ConfirmDenied(decision)
        return decisions

    # -- asking ----------------------------------------------------------

    def _ask(self, action: str, detail: str, why: str, actor: str) -> Decision:
        timeout, default_yes, channel = self._prompt_config()
        pending = Pending(token=uuid.uuid4().hex[:10], action=action,
                          detail=detail, why=why, timeout_s=timeout)
        with self._lock:
            self._pending[pending.token] = pending
        self.counters["asked"] += 1
        self.audit.append("confirm.asked", actor=actor, ok=None, why=why,
                          confirm_action=action, token=pending.token,
                          channel=channel, timeout_s=timeout,
                          detail=detail[:400])
        log.info("asking for confirmation",
                 extra={"confirm_action": action, "token": pending.token,
                        "channel": channel, "timeout_s": timeout})
        try:
            self.asker(pending, channel)
        except Exception:  # noqa: BLE001 - a broken toast must not auto-approve
            log.exception("could not deliver the confirmation prompt")

        started = self.clock()
        deadline = started + timeout
        while self.clock() < deadline:
            if pending.event.wait(config.CONFIRM_POLL_S):
                break
        waited = self.clock() - started
        with self._lock:
            self._pending.pop(pending.token, None)

        if pending.answer is None:
            allowed = default_yes
            outcome = "timeout"
            self.counters["timeout"] += 1
        else:
            allowed = bool(pending.answer)
            outcome = "approved" if allowed else "denied"
            self.counters["approved" if allowed else "denied"] += 1

        decision = Decision(action, ASK, allowed, outcome, detail, why,
                            token=pending.token, channel=channel,
                            waited_s=waited)
        self._record(decision, pending.answered_by or actor)
        return decision

    def answer(self, token: str, allow: bool, *, by: str = "user") -> bool:
        """Answer a pending question. False if there is no such question."""
        with self._lock:
            pending = self._pending.get(token)
        if pending is None:
            return False
        pending.answer = bool(allow)
        pending.answered_by = by
        pending.event.set()
        log.info("confirmation answered",
                 extra={"token": token, "allow": bool(allow), "by": by})
        return True

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [p.to_dict() for p in
                    sorted(self._pending.values(), key=lambda p: p.asked)]

    # -- delivery --------------------------------------------------------

    def _ask_on_desktop(self, pending: Pending, channel: str) -> None:
        """Put the question where the user is actually looking.

        Omarchy's notification takes exactly one click action, so the toast is
        the *yes* and silence is the *no*. That asymmetry is on purpose and it
        is the safe way round: the only outcome that needs a deliberate act is
        the one that lets the action happen.
        """
        name = settings_mod.assistant_name()
        headline = f"{name} needs a yes"
        body = (f"{describe(pending.action).capitalize()}. "
                f"Click to allow — no answer in "
                f"{int(pending.timeout_s)}s is a no.")
        if channel in ("notification", "both"):
            argv = [self.notify_bin, "--app-name", "Jarvis",
                    "-u", "critical", "-g", "󰀦",
                    "--exec", self._approve_command(pending.token),
                    headline, body]
            self._run_notify(argv, pending)
        if channel in ("terminal", "both"):
            # There is no terminal attached to a daemon, so "terminal" means
            # the journal plus the pending list `luna confirm` reads. The
            # command to answer is printed in full because a prompt you cannot
            # answer is not a prompt.
            log.warning(
                "CONFIRM: %s — %s  [yes: luna confirm yes %s]  "
                "[no: luna confirm no %s]",
                describe(pending.action), pending.detail[:160],
                pending.token, pending.token,
                extra={"confirm_action": pending.action,
                       "token": pending.token})

    def _approve_command(self, token: str) -> str:
        cli = config.PROJECT_DIR / "bin" / "luna"
        return f"{shlex.quote(str(cli))} confirm yes {shlex.quote(token)}"

    def _run_notify(self, argv: list[str], pending: Pending) -> None:
        try:
            proc = safety.spawn(
                argv, kind="confirm-notify", durable=False,
                note=f"confirm {pending.action}",
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except (OSError, FileNotFoundError) as exc:
            log.warning("could not send the confirmation notification",
                        extra={"detail": str(exc), "bin": self.notify_bin})
            return
        threading.Thread(target=_reap_after, args=(proc,), daemon=True,
                         name="jarvis-confirm-notify").start()

    # -- recording -------------------------------------------------------

    def _record(self, decision: Decision, actor: str) -> None:
        action = {"auto": "confirm.auto", "approved": "confirm.approved",
                  "denied": "confirm.denied", "timeout": "confirm.timeout",
                  "hard": "confirm.hard_deny"}[decision.outcome]
        self.audit.append(action, actor=actor, ok=decision.allowed,
                          why=decision.why or describe(decision.action),
                          confirm_action=decision.action,
                          policy=decision.policy, token=decision.token or None,
                          channel=decision.channel or None,
                          waited_s=round(decision.waited_s, 2) or None,
                          rule=decision.rule or None,
                          detail=decision.detail[:400] or None)

    def snapshot(self) -> dict[str, Any]:
        policies = {name: self.policy(name) for name in CLASSES}
        timeout, default_yes, channel = self._prompt_config()
        return {"policies": policies, "channel": channel,
                "timeout_s": timeout,
                "default_on_timeout": "yes" if default_yes else "no",
                "pending": self.pending(),
                "counters": dict(self.counters),
                "hard_denies": [r.name for r in HARD_DENIES] + ["signal_unspawned"]}


def _reap_after(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    safety.reap(proc)
