# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities through **GitHub's private vulnerability
reporting** on this repository (Security → Report a vulnerability). Please do
not open a public issue for anything exploitable.

This is a personal project maintained by one person. There is no SLA. Expect
acknowledgement within a week or so, and be patient beyond that.

## What Luna actually is, from a security point of view

Luna is not a sandboxed application. It is a daemon that runs as your user, on
your desktop, and dispatches AI agent sessions that have real tools. You should
understand the following before running it, and doubly so before running it on
a machine that holds anything you care about.

### A dispatched job runs with permissions bypassed

There are two different agent invocation paths, and they are not equally
privileged:

- **A conversational `luna ask`** runs with `--tools ""` — no tools at all —
  and a non-bypassing permission mode. It can talk. It cannot act.
- **A dispatched job** (`luna dispatch`) runs with **`--permission-mode
  bypassPermissions`** and **`--tools default`**. Under the Codex adapter the
  equivalent is `--dangerously-bypass-approvals-and-sandbox`.

That is a deliberate design decision, not an oversight: a job dispatched to a
hidden workspace at 2am cannot stop and ask, and an agent that stops and asks
in a window nobody is looking at is an agent that has silently hung. But the
consequence is real and unhedged — **a dispatched session can read, write and
delete files, run arbitrary commands, and reach the network, as you.**

It is given `--add-dir` for its own job directory and whatever else the
dispatch specifies. That is a scoping convenience, not a jail. Nothing here
prevents a determined or badly-prompted agent from acting outside it.

If you are not comfortable with that, do not use `dispatch`. The conversational
path is genuinely toolless and is safe to use on its own.

### The mitigation is the audit log, not a prompt

Luna asks no permission. What makes that tolerable is a record rather than a
gate. `~/.local/share/luna/audit.jsonl` records every dispatched task, every
process spawned, every signal delivered or refused, and every memory write —
with what it was for and how it ended.

Its properties, and their honest limits:

- **Append-only.** Opened in append mode; never truncated and never edited in
  place.
- **Durable.** Each line is flushed and `fsync`'d before the call returns, so
  an action that happened is in the log even across a hard power loss.
- **Bounded, but never quietly shorter.** Past `[audit] max_mb` the live file
  is *renamed* to `audit.jsonl.1` and its siblings shift up; only the oldest of
  `[audit] keep` is ever deleted, and that deletion is recorded as the first
  entry of the new file, naming what was dropped. The rename happens between
  two whole lines, after the previous line's `fsync`, so no record is ever cut
  in half. `luna audit` reads the siblings as well. With the defaults that is
  five files of 8 MB; `max_mb = 0` restores the old unbounded behaviour, and if
  you are keeping this machine under scrutiny that is the setting to use.
- **Honest about undo.** An inverse is recorded only where one genuinely
  exists and is known at the time. Nothing invents an undo for something
  irreversible, because a fabricated undo command invites you to run it.

**What the audit log is not:** it is not tamper-proof. It is an ordinary file
owned by your user, in your home directory, with no signing, no chaining and no
remote replication. Anything running as you — including a dispatched agent —
can in principle rewrite it. It is *evidence of what happened* for an operator
acting in good faith and a defence against Luna's own mistakes. It is **not** a
control that survives a hostile agent, and it should not be relied on as one.

### The PID firewall

`lunad/safety.py` maintains a ledger of processes Luna spawned herself, and
refuses to signal anything not in it. Refusals raise rather than warn, and are
always audited.

Critically, **nothing matches a process by name.** `pkill -f` and its relatives
are banned from this codebase: pattern matching cannot distinguish Luna's agent
from your own shell, and an agent on the development machine killed its own
terminal exactly that way. The firewall limits what Luna will *signal*. It does
not limit what a dispatched agent can do with its own tools.

### Secrets

API keys are never stored in `config.toml` and never enter this repository.
They live in `~/.config/jarvis/secrets.env`, mode `0600`, in a directory forced
to `0700`, and are supplied to `lunad` by a systemd drop-in. `.gitignore`
blocks `secrets*`, `*.env`, `*.key` and `*.pem` as a second line of defence.

Note that a dispatched agent runs as you and can read that file. Key isolation
here protects against accidental commits and casual reads, not against the
agent itself.

### Network

`lunad` speaks to a local Unix socket. It reaches the network only when a
remote model or a remote TTS provider is configured. Local piper speech and
local models involve no outbound traffic.

## Scope

In scope: anything that lets a *third party* — a crafted input, a malicious
prompt injected through content Luna reads, a local user other than you —
escalate beyond what is described above. Injection paths that turn a
conversational `ask` into an acting agent are especially interesting.

Out of scope: the fact that a dispatched job has broad permissions. That is
documented, intended, and described above.
