# Skills — working notes

Scratch notes for the `luna-skills` branch. Not `STATE-OF-PLAY.md`; fold anything durable into
that deliberately, later.

## What codex already does, and what we therefore did not build

`codex 0.151.0 features list` reports:

```
skill_search              stable             true
skip_host_skill_discovery under development  false
skill_mcp_dependency_install stable          true
```

So discovery and progressive disclosure are **codex's**, not ours. It reads every `SKILL.md` under
the skill roots itself, puts only the YAML frontmatter in the prompt, and pulls the body in when
the model invokes the skill.

Deliberately **not** built, and not to be built later:

- a skill loader or indexer in `lunad`;
- a router that picks a skill and injects it into the prompt;
- any prompt-assembly change in `persona.py` — the persona says who she is, a skill says how to do
  one specific thing, and a skill that restates the persona is dead weight in every turn;
- a daemon op for skills. `luna skills` is filesystem-only, so it runs in the client. Routing it
  through the socket would mean the farm could only be repaired while the daemon was healthy,
  which is exactly when it is least likely to be.
- anything that runs `curl | sh` or `npx`. skills.sh (Vercel Labs) installs that way and is
  Claude-Code-oriented; OpenAI's curated `github.com/openai/skills` is a plain repository. A
  `git clone` is the only network install path `luna skills add` has.

## There are two skill roots, not one

Verified live, `2026-09-10`: a real `codex exec -m gpt-5.6-luna` turn asked about the green gate,
chose `luna-github` from its description, and reached **first** for
`/home/ghost/.agents/skills/luna-github/SKILL.md` — which does not exist — before reading
`/home/ghost/.codex/skills/luna-github/SKILL.md` and answering correctly.

So `~/.agents/skills/` is a live root as well: the cross-agent convention, and the one Claude Code
on this machine reads. It currently holds `diagnose-crash`, `omarchy`, `luna-machine` (all links)
and `impeccable` (a real directory).

`luna skills` manages `~/.codex/skills/` only — it neither installs into nor removes from
`~/.agents/skills/`, and `tests/test_skills.py` asserts that. But `doctor` walks it looking for
**dangling links**, because a broken link there is the same invisible failure and nothing else on
this machine looks for it.

Open question left for later: whether the three new repo skills should be mirrored into
`~/.agents/skills/` the way `luna-machine` is, which would put them in front of Claude Code
sessions too. Not done here — it is a change to a tree this branch was not asked to touch.

## The failure mode this exists for

codex says **nothing** about a skill it could not read. A dangling symlink in the farm does not
warn, does not log, and does not error; Luna is simply less capable than she was yesterday, from a
change nobody can see. That is why:

- `luna skills list` keeps the row for a broken link instead of dropping it, marks it `broken`,
  names the path that went away, and exits 3;
- `remove` only ever unlinks — a real directory, `.system/`, and anything under `/usr/share` are
  all refused;
- `add` validates before it links, so a bad skill is refused while the farm is still untouched.

## Retrieval, not tokens, is the constraint

The description is the entire retrieval surface. `config.SKILL_MIN_DESCRIPTION = 60` is a floor
below which there is no room for trigger words; `SKILL_FARM_SOFT_LIMIT = 60` is where descriptions
start competing with each other and the right skill stops being chosen. Both are `doctor` findings.
Three or four good skills beat twenty generic ones, and the repo's own suite enforces a 120-char
floor on every skill this project ships (`RepoSkillsCase`).

## Gotcha: block scalars

Both packaged Omarchy skills write `description: >` with the text on following indented lines. Read
naively that makes the description the single character `>` — not empty, so a presence check passes,
and short, so a length check fails for a reason that is not the real one. `parse_frontmatter`
handles `>`, `>-`, `|`, `|+`, `|2-`.

## Guards added

`config.CODEX_SKILLS_DIR`, `config.SKILLS_STORE_DIR` and `config.SKILLS_EXTRA_ROOTS` are all
redirected process-wide in `tests/_support.py`, and `SkillFarm.__init__` reads them late.
`tests/test_guards.py` asserts the signature shape and that a default-constructed farm points
nowhere near `$HOME`. The first two are the `JOBS_DIR` class of hazard — directories this code
creates links in and unlinks from — and the third is the ambient class: a root Luna reads, which
left live would make the suite's result depend on what the person running it has installed.

One near miss worth recording: an early draft of `tests/test_skills.py` passed
`config.SKILLS_DIR / "luna-machine"` to its own fixture writer and **overwrote the repo's shipped
skill with fixture text**. Nothing failed loudly. `write_skill()` now refuses to write under
`config.SKILLS_DIR` or `~/.codex`.
