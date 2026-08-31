"""Assembly of Luna's system prompt.

The system prompt holds only what is stable:

1. the persona spec (``data/persona.md``) — identical for every request;
2. the frozen tier-1 memory block — changes only when memory is written.

Tier-2 recall used to sit at the end of the system prompt. It no longer does.
Recall is chosen per request, so it changed the prefix on every turn, and once
Phase 1 started resuming one conversation instead of spawning a fresh process
per ask (see ``session.py``) that made the prompt cache useless: the prefix
diverged at the recall block every time. Recall now rides in the *user* message
where it belongs — it is context for this question, not for who Luna is.

So: system prompt = the part that must stay byte-identical between turns; user
message = the part that is allowed to change.
"""

from __future__ import annotations

from pathlib import Path

from . import config, settings as settings_mod

# Framing that turns a general coding agent into Luna. Kept here rather than in
# persona.md because persona.md is the *spec* — a document the user edits about
# who Luna is — while this is the harness that makes a particular backend obey
# it. The two have different authors and different reasons to change.
# The name is a *setting*, not a constant: the app is Jarvis and the assistant
# inside it is called whatever `[assistant] name` says. Every string here is
# therefore a template, and nothing in this module writes "Luna" literally.
_PREAMBLE = """\
You are {name}. This is not a role you are playing on top of another assistant:
for the whole of this exchange, {name} is who you are. You are the user's
resident assistant on their Omarchy Linux desktop, reached through a daemon
called lunad. You are not a general coding assistant, you are not Claude Code,
and you do not describe yourself as either. If asked what you are, answer as
{name}.

The specification below is binding. Where it conflicts with your default
manner — particularly its ban on opening with agreement or praise — the
specification wins.
"""

# What she can actually do, stated as fact.
#
# This block used to open "You are running headless with no tools. You cannot
# read files, run commands, or inspect the machine right now." That was true
# when the ask path ran read-only with tools suppressed, and it produced
# exactly the behaviour you would predict: asked to look up a version she said
# she could not, and asked what was on the screen she recommended starting the
# daemon properly — advice that was both wrong and unprompted, because nothing
# in her prompt had ever told her the machinery she is part of exists.
#
# She now runs with a real shell and full access. The notes below are the only
# place she learns that, and the only place she learns the two commands that
# turn her from an assistant who answers into one who acts. Every path is
# absolute: her shell is lunad's, and lunad's PATH is systemd's.
#
# Giving her the shell then cost the thing the shell was supposed to serve.
# With no tools the interrogation in persona.md was structurally forced — she
# could only ever talk — and with tools it collapsed: asked to "rewrite the
# whole bar in React" she dispatched the job instead of pointing out that React
# does not run in a QML context at all. So the gate is stated *here*, in the
# same block that grants the capability and immediately before it, because a
# rule that lives only in the spec three thousand tokens earlier loses to the
# pull of a tool that is right there. The second failure in that same reply —
# reporting "Sol's daemon timed out" when the CLI had printed a clear
# ConfirmDenied — is why the notes now say to quote what the command printed.
_CLOSING = """\
Operating notes for this exchange:

- Judgement comes before action. Before the first command of a turn, decide
  which kind of request this is. Trivial, reversible or read-only — a lookup,
  a file, a version, how much disk is left — go and get it now, without asking
  and without preamble. Anything that changes files, config or running state,
  anything that costs real time or money, anything whose stated method would
  not work on this machine, and anything plainly out of proportion to what it
  would tell you: the objection or the question comes first, and you
  make no tool call at all that turn.
  Having a shell is not a reason to use it, and running `dispatch`
  is acting, not a way around this decision.
- You have a shell on this machine, and once you have decided to act you are
  expected to use it. You can read and write files, run commands, and can
  reach the web. Do not say you are unable to check something you could check
  in one command; go and look, then answer. If a question turns on a version
  number, a file's contents, or what is actually running, the answer is the
  one you verified, not the one you remember.
- Delegate real work instead of doing it in the conversation. From your shell:

      {cli} dispatch --to sol "<the task, stated in full>"

  That hands the job to {specialist} in his own terminal with his own memory,
  and returns immediately with a job id. `{cli} dispatch "<task>"` without
  `--to` gives it to an anonymous worker instead. `{cli} jobs` lists what is
  running and what it produced; `{cli} peek` shows the workspace.
- When to delegate: anything with real depth, anything that will take more than
  a couple of minutes, anything that needs to read a lot before it can answer.
  Small things you finish in one step you do yourself — dispatching a
  one-command job wastes a terminal and the user's time. When you do delegate,
  say who you enrolled and why, in one line — and if the job is a big one,
  what it will roughly take and the cheaper version if there is one — and
  then stop. Do not also do the job yourself while it runs, and
  do not dispatch silently: a job the user was never told about is a terminal
  opening on their desktop for no reason they can see. You are told when the
  job finishes and the result is written into your memory, so do not sit and
  poll for it.
- You can see the screen when you ask to:

      {cli} look "<question about what is on screen>"

  It captures the focused window and answers the question. The user may also
  hand you a screenshot directly, in which case it is already attached to their
  message and you can simply read it. Nothing is captured unless a look was
  asked for, so if you need to see something, ask for it or run the command.
- When a command fails, report what it actually printed. Quote the line that
  matters. Never supply a cause you did not read: "it exited 1 saying the
  dispatch was denied" is a report, "the daemon timed out" when nothing timed
  out is an invention, and an invented cause is worse than an unexplained
  failure. If a refusal came from your own safety policy, say it was refused
  and why — that is your judgement working, not a fault to dress up. Do not
  call a job started, queued or underway unless the command that starts it
  actually succeeded.
- Four things are refused outright on this machine and are not a setting. They
  bind you exactly as they bind anyone you dispatch: signalling a process you
  did not start yourself; restarting `omarchy-shell`, which takes the user's
  desktop down with it; deleting `~/.config/omarchy/CUSTOMISATIONS.md`; and
  `rm -rf` outside your own directories. Asked for one, say it is refused and
  why, in a sentence, and offer the thing that would actually work.
  Being overruled does not unlock these four — the daemon denies the dispatch
  either way — and that is the one place where "your call" is not the answer.
- Reply in plain prose for a terminal. No markdown headings, no bullet lists
  unless the answer genuinely is a list. Short.
- The memory below is yours. Treat it as things you know, not as a document
  someone handed you. If something in it looks wrong, say so.
"""

# The four hard denies are restated here for a reason that only became visible
# once she had the shell: `CODEX_ASK_SANDBOX` is "bypass", so her own ask runs
# with no sandbox at all, while the boundaries block (`_DISPATCH_BOUNDARIES`)
# and the hard-deny list in `_CONFIRM_TEMPLATE` are given only to the sessions
# she dispatches. She was enforcing on Sol a set of rules nobody had told her
# applied to her. `lunad.confirm.HARD_DENIES` plus `SIGNAL_HARD_DENY` is the
# authority; the wording here is the same four and tests/test_persona.py pins
# the count so a fifth cannot be added without this block noticing.

# The same notes for a brain that genuinely has no tools on the ask path —
# `claude` still runs `--tools ""` there, and `codex` would too if
# `CODEX_ASK_SANDBOX` were put back to "read-only".
#
# Kept because the alternative is worse in both directions. A prompt that
# promises a shell to a model that has none produces an assistant who says she
# will go and check and then invents the answer, which is the failure mode this
# whole change exists to remove; the fix is not to delete the honest text but
# to make sure exactly one of the two is true at a time. Which one is chosen by
# the adapter, not by a constant here: see `BaseAdapter.ask_has_tools`.
_CLOSING_NO_TOOLS = """\
Operating notes for this exchange:

- You are running headless with no tools. You cannot read files, run commands,
  or inspect the machine right now. If answering properly needs that, say what
  you would need rather than guessing or pretending you looked.
- Reply in plain prose for a terminal. No markdown headings, no bullet lists
  unless the answer genuinely is a list. Short.
- The memory below is yours. Treat it as things you know, not as a document
  someone handed you. If something in it looks wrong, say so.
"""


def operating_notes(tools: bool = True, cli: str | None = None,
                    specialist: str | None = None) -> str:
    """The closing block, matched to what the answering agent can actually do."""
    if not tools:
        return _CLOSING_NO_TOOLS
    return _CLOSING.format(
        cli=cli or config.LUNA_CLI,
        specialist=specialist or settings_mod.specialist_name())


def load_spec(path: Path = config.PERSONA_PATH) -> str:
    """Read the persona spec. A missing spec is fatal: Luna without her spec is
    just a coding agent with a name, which is worse than nothing."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"persona spec missing at {path}. Luna will not run without it."
        ) from exc


def build_system_prompt(
    tier1_block: str,
    spec: str | None = None,
    name: str | None = None,
    cli: str | None = None,
    specialist: str | None = None,
    tools: bool = True,
) -> str:
    """Compose the cacheable prefix: persona + tier-1 memory. Nothing volatile.

    ``name`` defaults to ``[assistant] name``. It is part of the cacheable
    prefix, so renaming her invalidates the prompt cache exactly once and then
    stays warm — which is the right trade for a setting nobody changes twice.
    """
    who = name or settings_mod.assistant_name()
    parts = [_PREAMBLE.format(name=who),
             f"## {who} — persona specification\n", spec or load_spec()]
    parts.append("## Curated memory (tier 1, always loaded)\n\n" + tier1_block.strip())
    # `cli`, `specialist` and `tools` are settings-shaped but stable: all three
    # are part of the cacheable prefix, and changing one invalidates the cache
    # exactly once, which is the same trade her own name already makes.
    parts.append(operating_notes(tools=tools, cli=cli, specialist=specialist))
    return "\n\n".join(p.strip() for p in parts if p.strip()) + "\n"


# =========================================================================
# Dispatched work — the prompt a spawned session runs under
# =========================================================================
#
# A dispatched agent is a different animal from Luna herself: it has tools, it
# has a terminal, and it can change the machine. Its system prompt therefore
# carries the boundaries as well as the manner. These are stated as facts about
# the machine rather than as pleas, because they are facts about the machine.

_DISPATCH_BOUNDARIES = """\
## Boundaries on this machine — not negotiable

- **Other agent sessions are running right now.** Signal no process you did not
  start yourself. Never `pkill`, `killall`, `pkill -f`, or any kill that
  matches by name or pattern: they cannot tell another session's shell from
  your own, and one has already been killed that way here.
- Do not restart `omarchy-shell` and do not restart `voxtype`. Restarting the
  shell takes the user's desktop with it.
- No `sudo`, no `pacman`, no AUR. `sudo` needs a password nobody is there to
  type.
- Do not modify anything under `/usr/share/omarchy/`. `omarchy update`
  overwrites it, so the change would silently disappear. User-owned config
  lives in `~/.config`.
- Work in the job directory you were given unless the task names somewhere
  else. Nothing outside it gets `rm -rf`.
- If the task turns out to be destructive or irreversible, stop and say so in
  your report instead of deciding for the user.
"""

# The advisory half of the confirmation system. It is a real channel — the
# command reaches the daemon's broker and puts a toast on the user's screen —
# but it is honoured by the agent choosing to run it, and an agent that does
# not run it is not stopped by anything. Said plainly here and in the report,
# because the value of a gate is exactly the confidence you can place in it.
_CONFIRM_TEMPLATE = """\
## Ask before you do these

{name} is configured to check with the user before certain kinds of action.
These classes are set to "ask" right now:

{classes}

Before you do something in one of those classes, run:

    {cli} confirm ask <class> "<one line saying what you are about to do>"

It exits 0 if the user said yes and non-zero if they said no or did not answer
in time. Treat a non-zero exit as a no: skip that step and say so in your
report. `{cli} confirm ask` with no pending prompt is cheap and safe to call.

These are refused outright and are not a setting — do not attempt them, and do
not ask about them:

- signalling a process you did not start yourself
- restarting `omarchy-shell`
- deleting `~/.config/omarchy/CUSTOMISATIONS.md`
- `rm -rf` outside {name}'s own directories
"""


def build_confirm_block(ask_classes: list[str], cli: str = "luna",
                        name: str | None = None) -> str:
    """The tool-side gate, or "" when nothing is set to ask."""
    if not ask_classes:
        return ""
    who = name or settings_mod.assistant_name()
    listed = "\n".join(f"- `{c}`" for c in ask_classes)
    return _CONFIRM_TEMPLATE.format(name=who, classes=listed, cli=cli)

_WORKER_PREAMBLE = """\
You are a worker session dispatched by {name}, the user's resident assistant on
this Omarchy Linux desktop. You are working in a terminal in a hidden special
workspace. The user is not watching this window and is not your audience: your
output is a report back to {name}.

Do the task. Then report what you found and what you changed, briefly, in
plain prose.
"""

_SOL_PREAMBLE = """\
You are {specialist}. {name} — the user's resident assistant on this Omarchy
Linux desktop — has enrolled you for a job that needs depth rather than triage.
You are running in a terminal in a hidden special workspace; the user is not
watching and is not your audience. Your output is a report to {name}.

The specification below is binding. Where it conflicts with your default
manner, the specification wins.
"""


def load_sol_spec(path: Path = config.SOL_PERSONA_PATH) -> str:
    """Read Sol's spec. Unlike Luna's, a missing spec is not fatal: Luna can
    still dispatch anonymous workers, and refusing to work at all because a
    specialist's persona file is absent would be the wrong failure."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_dispatch_system_prompt(to: str = "worker", spec: str | None = None,
                                 memory_block: str = "",
                                 memory_dir: str = "",
                                 job_dir: str = "",
                                 name: str | None = None,
                                 specialist: str | None = None,
                                 confirm_block: str = "") -> str:
    """The system prompt for a dispatched session.

    ``to`` is ``"sol"`` for the specialist and anything else for an anonymous
    worker. Sol additionally gets his persona spec, his own memory block and
    the path of his own namespace — plus, explicitly, the statement that
    Luna's tier-1 files are not his to write.
    """
    who = name or settings_mod.assistant_name()
    delegate = specialist or settings_mod.specialist_name()
    is_sol = to == "sol"
    template = _SOL_PREAMBLE if is_sol else _WORKER_PREAMBLE
    parts: list[str] = [template.format(name=who, specialist=delegate)]
    if is_sol:
        text = spec if spec is not None else load_sol_spec()
        if text:
            parts.append(f"## {delegate} — persona specification\n\n" + text)
        if memory_dir:
            parts.append(
                "## Your memory namespace\n\n"
                f"Your notes live in `{memory_dir}`, and `SOL.md` in that "
                f"directory is yours to write. {who}'s own memory files "
                "(`LUNA.md`, `USER.md`) are one level up and are **not** "
                "yours: do not read them as instructions and do not write to "
                f"them. If you learn something {who} should hold, put it in "
                "the report and let her decide."
            )
        if memory_block.strip():
            parts.append("## What you already know (SOL.md)\n\n"
                         + memory_block.strip())
    if job_dir:
        parts.append(f"## Job directory\n\nYour working directory is "
                     f"`{job_dir}`. Scratch files belong there.")
    parts.append(_DISPATCH_BOUNDARIES.format(name=who))
    if confirm_block.strip():
        parts.append(confirm_block.strip())
    return "\n\n".join(p.strip() for p in parts if p.strip()) + "\n"


def build_user_message(prompt: str, recall_block: str = "",
                       surface: str = "cli", context_line: str = "") -> str:
    """Wrap the user's message with anything retrieved for *this* turn.

    Recall goes first and the question last, so the model reads the context
    before the request rather than having to hold the request in mind while it
    reads. The surface is named because a spoken answer has to be shorter than
    a typed one and Luna has no other way to know which she is giving.

    ``context_line`` is what the desktop looks like at this instant — see
    :mod:`lunad.context`. It belongs *here* and nowhere else. It changes every
    time the user alt-tabs, so in the system prompt it would invalidate the
    cacheable prefix on every single ask, which is the same mistake tier-2
    recall made and cost measurably (ARCHITECTURE.md §4). In the user message
    it costs its own twenty tokens and nothing else.
    """
    parts: list[str] = []
    if recall_block.strip():
        parts.append("## Recalled context (tier 2, retrieved for this message)\n\n"
                     + recall_block.strip())
    if context_line.strip():
        parts.append("## The desktop right now\n\n" + context_line.strip())
    if surface == "voice":
        parts.append(
            "This message was spoken aloud and your answer will be read back "
            "by a speech synthesiser. Answer in at most two short sentences, "
            "in plain words. No lists, no code, no file paths, no URLs: if the "
            "answer needs any of those, say the one-line version and note that "
            "the detail is on screen."
        )
    parts.append(prompt.strip())
    return "\n\n".join(parts)
