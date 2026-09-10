"""The skills farm — what codex can see, and how it got there.

codex 0.151.0 discovers skills by itself. `skill_search` is a stable feature,
`skip_host_skill_discovery` is off, and the mechanism is progressive
disclosure: every `SKILL.md` under `~/.codex/skills/` contributes its YAML
frontmatter to the prompt, and the *body* is not loaded until the model
decides to invoke that skill. There is no loader here, no router, and no
prompt injection, and this module must never grow one. Everything lunad owns
about skills is the question of which directories are in that farm — which is
a question about symlinks and a `SKILL.md` that parses.

Three consequences shape the whole file:

**The description is the entire retrieval surface.** A body can be five
hundred lines of excellent prose and never be read, because the only thing the
model saw when it chose was one `description:` line. So `doctor` treats a
short description as a defect rather than a style note: below
`config.SKILL_MIN_DESCRIPTION` there is no room for the concrete trigger words
that make a skill findable, and a skill that is never chosen is a skill that
does not exist.

**A dangling symlink is silent.** codex does not announce a skill it could not
read; it simply has one fewer. That is the worst failure mode this module has
to catch, because from the outside it is indistinguishable from a model that
did not feel like using the skill — so `list` and `doctor` both report a
broken link loudly rather than dropping the row.

**Nothing is ever copied into the farm.** Every entry is a link back to a
directory somebody owns: this repo, the Omarchy package under `/usr/share`, or
a clone under `config.SKILLS_STORE_DIR`. A copy drifts from its source without
saying so; a link either resolves or breaks. It also means `remove` has a
sharp rule it can actually enforce — unlink, never delete — so the worst a
mistaken removal can do is cost a `luna skills add` to undo.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

__all__ = ["Skill", "SkillError", "SkillFarm", "parse_frontmatter"]


class SkillError(Exception):
    """A refusal with a reason a person can act on.

    Every failure in this module is one of these, and the message is expected
    to be printed verbatim to the terminal — so it says what was wrong *and*
    what to do, never just "invalid skill".
    """


#: Recognised sources, in the order `list` sorts them. "repo" is Luna's own,
#: "omarchy" is packaged and clobbered by `omarchy update`, "system" is codex's
#: own `.system/` tree which is not ours to touch, and "external" is everything
#: else — a clone, or a link somebody made by hand.
SOURCES = ("repo", "omarchy", "external", "system")

_OMARCHY_PREFIX = Path("/usr/share/omarchy")

#: The frontmatter fence. codex wants the *first* line to be `---`; a file
#: with a leading blank line or a BOM has no frontmatter at all as far as the
#: discovery pass is concerned, which is a real way to ship a skill that is
#: never seen, so the check here is exactly as strict.
_FENCE = "---"

_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")

#: A YAML block scalar indicator, with its optional indentation and chomping
#: suffixes: `>`, `>-`, `|`, `|+`, `|2-`.
_BLOCK = re.compile(r"^[>|][0-9]*[+-]?$")


def parse_frontmatter(text: str) -> dict[str, str]:
    """The YAML frontmatter of a `SKILL.md`, as far as codex reads it.

    Deliberately not a YAML parser. The frontmatter codex acts on is a flat
    map of scalars — in practice `name` and `description`, occasionally a
    `license` or a version — and pulling in a parser to read two keys would
    add a dependency this project does not have for no behaviour it needs.

    What it does handle is the shape that actually appears: quoted or bare
    scalars, and a value continued on following indented lines (the way a long
    description is wrapped), which is folded back into one line with single
    spaces. Anything it cannot make sense of is *dropped*, not guessed at,
    because a wrong value here would be reported as a working skill.

    Returns an empty mapping when there is no frontmatter — which is itself a
    finding, and every caller treats it as one.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}
    body: list[str] = []
    for line in lines[1:]:
        if line.strip() == _FENCE:
            break
        body.append(line)
    else:                                   # unterminated: no frontmatter
        return {}

    out: dict[str, str] = {}
    key: str | None = None
    for line in body:
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and key:
            out[key] = (out[key] + " " + line.strip()).strip()
            continue
        match = _KEY.match(line)
        if not match:
            key = None
            continue
        key, value = match.group(1), match.group(2).strip()
        # A block scalar (`>`, `>-`, `|`, `|2-` …) has its text on the
        # following indented lines, and the indicator is not part of it. Both
        # packaged Omarchy skills are written this way, and reading the marker
        # as the value made every one of their descriptions start with "> " —
        # cosmetic in a listing, not cosmetic at all in a length check.
        out[key] = "" if _BLOCK.match(value) else _unquote(value)
    return {k: v.strip() for k, v in out.items()}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


@dataclass
class Skill:
    """One entry in the farm, resolved as far as it can be.

    A `Skill` is produced even when the thing is broken — a dangling link
    still gets a row, with `resolves` false and the reason in `problems`.
    Dropping it would hide the one failure that has no other symptom.
    """

    name: str                       # the entry's name in the farm
    link: Path                      # ~/.codex/skills/<name>
    target: Path                    # where it points (itself, if not a link)
    is_link: bool
    resolves: bool
    source: str
    declared_name: str = ""         # the frontmatter `name:`
    description: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def summary(self) -> str:
        """The description's first line — what the model sees first."""
        return self.description.split("\n")[0].strip()

    @property
    def removable(self) -> bool:
        """Whether `remove` will touch it. See `SkillFarm.remove`."""
        return (self.is_link and self.source != "system"
                and not _under(self.target, _OMARCHY_PREFIX)
                and not _under(self.target, Path("/usr/share")))


def _under(path: Path, prefix: Path) -> bool:
    try:
        path.resolve().relative_to(prefix.resolve())
    except (ValueError, OSError):
        return False
    return True


def _run(argv: list[str], timeout: float, cwd: Path | None = None) -> str:
    """Spawn, or raise a `SkillError` saying which program was missing.

    The only thing this module ever runs is `git clone`, and the binary name
    reaches it from `config.GIT_BIN` read *late* — see `SkillFarm.__init__`.
    `shutil.which` first so that a disarmed name fails as a refusal naming the
    binary rather than as a bare `FileNotFoundError`.
    """
    if not shutil.which(argv[0]):
        raise SkillError(f"{argv[0]} is not on PATH, so there is no way to "
                         f"clone anything")
    try:
        done = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        raise SkillError(f"{argv[0]} did not finish within {timeout:.0f}s")
    except OSError as exc:
        raise SkillError(f"could not run {argv[0]}: {exc}")
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise SkillError(f"{argv[0]} failed: "
                         f"{detail[-1] if detail else f'exit {done.returncode}'}")
    return done.stdout


class SkillFarm:
    """`~/.codex/skills/`, and the four things anyone needs to do to it.

    Every path is a constructor argument defaulting to `None` and read from
    `config` in the body. That is the guard contract this repo enforces in
    `tests/test_guards.py`, and it is not a formality here: the farm is a
    directory this class *creates links in and removes links from*, so a test
    that could not redirect it would edit the live set of skills Luna reasons
    with, and the symptom would be a skill quietly missing weeks later.
    """

    def __init__(self, root: Path | None = None, store: Path | None = None,
                 git_bin: str | None = None,
                 extra_roots: tuple[Path, ...] | None = None,
                 clone_timeout: float = 120.0) -> None:
        self.root = Path(root) if root else config.CODEX_SKILLS_DIR
        self.store = Path(store) if store else config.SKILLS_STORE_DIR
        self.git_bin = git_bin or config.GIT_BIN
        self.extra_roots = tuple(
            Path(p) for p in (config.SKILLS_EXTRA_ROOTS
                              if extra_roots is None else extra_roots))
        self.clone_timeout = clone_timeout

    # -- reading -----------------------------------------------------------

    def source_of(self, target: Path, *, system: bool = False) -> str:
        if system:
            return "system"
        if _under(target, config.SKILLS_DIR):
            return "repo"
        if _under(target, _OMARCHY_PREFIX):
            return "omarchy"
        return "external"

    def _inspect(self, link: Path, *, system: bool = False) -> Skill:
        is_link = link.is_symlink()
        try:
            target = link.resolve()
        except OSError:
            target = link
        resolves = target.is_dir()
        problems: list[str] = []

        if is_link and not resolves:
            # The silent failure this module exists for. `readlink` still
            # works on a dangling link, so the message can name the path that
            # went away, which is almost always enough to fix it.
            problems.append(f"dangling symlink → {os.readlink(link)} "
                            f"(codex sees no skill here at all)")
        elif not resolves:
            problems.append("not a directory")

        skill = Skill(name=link.name, link=link, target=target,
                      is_link=is_link, resolves=resolves,
                      source=self.source_of(target, system=system),
                      problems=problems)
        if not resolves:
            return skill

        md = target / "SKILL.md"
        if not md.is_file():
            skill.problems.append("no SKILL.md — codex will not see this")
            return skill
        try:
            front = parse_frontmatter(md.read_text(encoding="utf-8",
                                                   errors="replace"))
        except OSError as exc:
            skill.problems.append(f"SKILL.md is unreadable: {exc}")
            return skill

        skill.declared_name = front.get("name", "")
        skill.description = front.get("description", "")
        if not front:
            skill.problems.append(
                "SKILL.md has no YAML frontmatter (it must open with a `---` "
                "line); with none, nothing about this skill is in the prompt")
            return skill
        if not skill.declared_name:
            skill.problems.append("frontmatter has no `name:`")
        if not skill.description:
            skill.problems.append(
                "frontmatter has no `description:` — the description is the "
                "whole retrieval surface, so this skill can never be chosen")
        return skill

    def list(self) -> list[Skill]:
        """Every skill codex can see, broken ones included.

        Sorted by source and then by name, so the repo's own skills — the ones
        a person is most likely to be editing — come first and `.system/`
        comes last.
        """
        found: list[Skill] = []
        if self.root.is_dir():
            for entry in sorted(self.root.iterdir()):
                if entry.name == config.SKILLS_SYSTEM_DIR:
                    continue
                if entry.name.startswith(".") or not _looks_like_skill(entry):
                    continue
                found.append(self._inspect(entry))
        system_dir = self.root / config.SKILLS_SYSTEM_DIR
        if system_dir.is_dir():
            for entry in sorted(system_dir.iterdir()):
                if entry.name.startswith(".") or not _looks_like_skill(entry):
                    continue
                found.append(self._inspect(entry, system=True))
        order = {name: i for i, name in enumerate(SOURCES)}
        found.sort(key=lambda s: (order.get(s.source, 9), s.name))
        return found

    def get(self, name: str) -> Skill | None:
        for skill in self.list():
            if skill.name == name:
                return skill
        return None

    # -- validating --------------------------------------------------------

    def validate(self, path: Path) -> tuple[str, str]:
        """Check a candidate directory *before* anything is linked.

        Returns `(name, description)` or raises. The order matters: a bad
        skill must be refused while the farm is still untouched, because the
        alternative — link first, discover the problem in `doctor` later — is
        exactly the dangling-link failure this module is here to prevent, only
        self-inflicted.
        """
        path = Path(path)
        if not path.is_dir():
            raise SkillError(f"{path} is not a directory")
        md = path / "SKILL.md"
        if not md.is_file():
            raise SkillError(
                f"{path} has no SKILL.md, so codex would never see it.\n"
                f"  a skill is a directory with a SKILL.md in it, and that "
                f"file opens with `---`, `name:` and `description:`")
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SkillError(f"{md} cannot be read: {exc}")
        front = parse_frontmatter(text)
        if not front:
            raise SkillError(
                f"{md} has no YAML frontmatter.\n"
                f"  the very first line must be `---`, and the block must "
                f"close with another `---`")
        name = front.get("name", "").strip()
        description = front.get("description", "").strip()
        if not name:
            raise SkillError(f"{md} has no `name:` in its frontmatter")
        if not description:
            raise SkillError(
                f"{md} has no `description:` in its frontmatter.\n"
                f"  the description is the entire retrieval surface — the "
                f"body is not loaded until the skill fires — so a skill "
                f"without one can never be chosen")
        return name, description

    def doctor(self) -> tuple[list[Skill], list[str]]:
        """Validate the whole farm. Returns the skills and any farm-wide notes.

        Per-skill defects land in `Skill.problems`; the second return value is
        for things no single skill owns — duplicate names across two entries,
        and a farm large enough that retrieval accuracy is the constraint
        rather than tokens.
        """
        skills = self.list()
        notes: list[str] = []

        for skill in skills:
            if skill.resolves and skill.description:
                if len(skill.description) < config.SKILL_MIN_DESCRIPTION:
                    skill.problems.append(
                        f"description is {len(skill.description)} characters; "
                        f"under {config.SKILL_MIN_DESCRIPTION} there is no "
                        f"room for the trigger words that get it chosen")
                elif not any(ch.isalpha() for ch in skill.description):
                    skill.problems.append("description has no words in it")
            if (skill.declared_name and skill.source != "system"
                    and skill.declared_name != skill.name):
                skill.problems.append(
                    f"frontmatter name is `{skill.declared_name}` but the "
                    f"directory is `{skill.name}`; make them match, or the "
                    f"skill is referred to by two different names")

        seen: dict[str, str] = {}
        for skill in skills:
            key = (skill.declared_name or skill.name).lower()
            if key in seen:
                notes.append(
                    f"two skills answer to `{key}`: {seen[key]} and "
                    f"{skill.link} — one of them will be shadowed")
            else:
                seen[key] = str(skill.link)

        notes.extend(self._other_roots())

        live = [s for s in skills if s.resolves]
        if len(live) > config.SKILL_FARM_SOFT_LIMIT:
            notes.append(
                f"{len(live)} skills installed; past about "
                f"{config.SKILL_FARM_SOFT_LIMIT} the descriptions compete "
                f"with each other and the right skill stops being chosen — "
                f"remove the ones that are not earning their place")
        return skills, notes

    def _other_roots(self) -> list[str]:
        """Dangling links in a root this class does not manage.

        `~/.agents/skills/` is the cross-agent convention and codex reads it
        too — a live turn was observed reaching for it *before* the codex-own
        farm. Luna neither installs into it nor removes from it; that tree
        belongs to the machine, not to this repo. What she can do is notice
        that something in it points at nothing, because that is the same
        invisible failure as a dangling link in her own farm and nothing else
        on this machine looks for it.

        Only broken links are reported. A skill that lives in one root and not
        the other is an ordinary arrangement, not a defect, and a doctor that
        complained about it would be noise the third time it ran.
        """
        notes: list[str] = []
        try:
            mine = self.root.resolve()
        except OSError:
            mine = self.root
        for extra in self.extra_roots:
            if not extra.is_dir():
                continue
            try:
                if extra.resolve() == mine:
                    continue
            except OSError:
                continue
            for entry in sorted(extra.iterdir()):
                if entry.name.startswith(".") or not _looks_like_skill(entry):
                    continue
                if entry.is_symlink() and not entry.resolve().is_dir():
                    notes.append(
                        f"{entry} is a dangling symlink → "
                        f"{os.readlink(entry)}; that root is not Luna's to "
                        f"repair, but nothing reading it can see the skill")
        return notes

    # -- changing ----------------------------------------------------------

    def add(self, source: str, name: str | None = None) -> Skill:
        """Install a skill by linking it into the farm.

        `source` is a local path or a git URL. A URL is cloned into
        `self.store` first and the link is made from *there*: cloning straight
        into the farm would put a `.git`, a README and whatever else the
        repository holds inside the directory codex walks, and would make
        `remove` a `rm -rf` instead of an `unlink`.
        """
        if _is_git_url(source):
            path = self._clone(source)
        else:
            path = Path(source).expanduser().resolve()
        path = self._locate(path)
        declared, _ = self.validate(path)
        name = name or declared or path.name

        self.root.mkdir(parents=True, exist_ok=True)
        link = self.root / name
        if link.exists() or link.is_symlink():
            where = os.readlink(link) if link.is_symlink() else str(link)
            raise SkillError(
                f"a skill called `{name}` is already installed, from "
                f"{where}.\n"
                f"  remove it first (`luna skills remove {name}`) or install "
                f"this one under another name")
        link.symlink_to(path, target_is_directory=True)
        return self._inspect(link)

    def remove(self, name: str) -> Skill:
        """Unlink a skill. Never delete a directory.

        Three refusals, and they are the point of the method rather than
        edge cases:

        * `.system/` is codex's own — `skill-creator` and `skill-installer`
          live there and removing one breaks codex, not Luna.
        * anything under `/usr/share` is pacman's. Even if it *were* a real
          directory this could remove, the next `omarchy update` would put it
          back, so the honest answer is that this is not the tool for it.
        * a real directory is never removed. If somebody has put an actual
          skill directory in the farm rather than a link, this refuses and
          says so; deleting it would destroy the only copy.
        """
        link = self.root / name
        if not link.exists() and not link.is_symlink():
            raise SkillError(f"no skill called `{name}` is installed")
        if name == config.SKILLS_SYSTEM_DIR or name.startswith("."):
            raise SkillError(
                f"`{name}` is codex's own — `.system/` holds skill-creator "
                f"and skill-installer, and they are not Luna's to remove")
        skill = self._inspect(link)
        if not skill.is_link:
            raise SkillError(
                f"`{name}` is a real directory, not a symlink, so this will "
                f"not remove it — that would delete the only copy.\n"
                f"  if it really is disposable: rm -r {link}")
        if _under(skill.target, Path("/usr/share")):
            raise SkillError(
                f"`{name}` points into {skill.target}, which is owned by a "
                f"package.\n"
                f"  removing the link here is possible but pointless — it is "
                f"part of the system install and the next update restores it")
        link.unlink()
        return skill

    # -- internals ---------------------------------------------------------

    def _clone(self, url: str) -> Path:
        """`git clone --depth 1` into the user's own store, and nothing else.

        The only network path in this module, and deliberately the only one.
        No `curl | sh`, no `npx`: an installer script for a skills registry
        runs arbitrary code from a third party with the user's own
        credentials on the machine, to obtain what is ultimately a directory
        with a markdown file in it. A clone gets the same result and can be
        read afterwards.
        """
        name = _repo_name(url)
        if not name:
            raise SkillError(f"cannot work out a directory name from {url}")
        self.store.mkdir(parents=True, exist_ok=True)
        dest = self.store / name
        if dest.exists():
            raise SkillError(
                f"{dest} already exists.\n"
                f"  install from it directly (`luna skills add {dest}`), or "
                f"remove it and clone again")
        _run([self.git_bin, "clone", "--depth", "1", url, str(dest)],
             self.clone_timeout)
        return dest

    def _locate(self, path: Path) -> Path:
        """The skill inside `path`, which may be a repository of several.

        `github.com/openai/skills` is a directory of skills, not one skill.
        Guessing which one was meant would be worse than asking, so: a
        `SKILL.md` at the root means the whole thing is the skill, exactly one
        subdirectory with a `SKILL.md` means that one, and several means a
        refusal that lists them by name so the next command can be typed
        straight from the message.
        """
        if (path / "SKILL.md").is_file():
            return path
        if not path.is_dir():
            raise SkillError(f"{path} is not a directory")
        found = sorted(child for child in path.iterdir()
                       if child.is_dir() and (child / "SKILL.md").is_file())
        if len(found) == 1:
            return found[0]
        if not found:
            raise SkillError(
                f"no SKILL.md anywhere in {path}, at the top level or one "
                f"directory down — this is not a skill")
        listed = "\n".join(f"    luna skills add {child}" for child in found)
        raise SkillError(
            f"{path} holds {len(found)} skills, not one. Install the ones you "
            f"want, by name:\n{listed}\n"
            f"  three or four skills that are actually used beat twenty that "
            f"dilute every search")


def _looks_like_skill(entry: Path) -> bool:
    """A directory, or a link to one. Loose files in the farm are not skills."""
    return entry.is_dir() or entry.is_symlink()


def _is_git_url(source: str) -> bool:
    return (source.startswith(("http://", "https://", "git://", "ssh://",
                               "git@"))
            or source.endswith(".git"))


def _repo_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    tail = tail.split(":")[-1]
    return "".join(ch for ch in tail if ch.isalnum() or ch in "-_.").strip(".")
