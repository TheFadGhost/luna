"""The skills farm: what gets linked, what gets refused, and what breaks.

Every case here builds its own farm in a temporary directory. None of them
touches `~/.codex/skills/`, none of them clones anything, and the last class
in this file proves both rather than promising them — the guard sentinels in
``tests/_support.py`` are what make that true, and a test suite that installs
or unlinks a real skill has no visible symptom at all, because codex says
nothing about a skill that is not there.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from ._support import (FORBIDDEN_CODEX_SKILLS_DIR,  # noqa: F401
                       FORBIDDEN_GIT,
                       FORBIDDEN_SKILLS_EXTRA_ROOTS,
                       FORBIDDEN_SKILLS_STORE_DIR,
                       TempMemoryCase)

from lunad import config, skills
from lunad.skills import _under

GOOD_DESCRIPTION = ("Do the specific thing this skill is for, with enough "
                    "concrete trigger words in the line that it actually "
                    "gets chosen. Triggers - widget, popup, panel.")


def write_skill(directory: Path, name: str = "example",
                description: str = GOOD_DESCRIPTION,
                body: str = "# Example\n\nHow to do the thing.\n",
                frontmatter: str | None = None) -> Path:
    """A skill on disk. `frontmatter=` replaces the whole block verbatim."""
    # A hard stop, added because it was needed: an early draft of this file
    # passed `config.SKILLS_DIR / "luna-machine"` in and overwrote the repo's
    # own shipped skill with fixture text. Nothing failed loudly — the working
    # tree was simply wrong afterwards. A test helper that can write anywhere
    # will eventually write somewhere real.
    if _under(directory, config.SKILLS_DIR) or _under(directory, Path.home() / ".codex"):
        raise AssertionError(
            f"a test tried to write a fixture skill into {directory}; "
            f"fixtures go in the case's own temporary tree")
    directory.mkdir(parents=True, exist_ok=True)
    if frontmatter is None:
        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n"
    (directory / "SKILL.md").write_text(frontmatter + "\n" + body)
    return directory


class FarmCase(TempMemoryCase):
    """A throwaway farm, a throwaway store, and somewhere to keep sources."""

    def setUp(self) -> None:
        super().setUp()
        self.farm_root = self.root / "codex-skills"
        self.store = self.root / "store"
        self.sources = self.root / "sources"
        self.sources.mkdir(parents=True, exist_ok=True)
        self.agents_root = self.root / "agents-skills"
        self.farm = skills.SkillFarm(root=self.farm_root, store=self.store,
                                     git_bin="/nonexistent/luna-tests-git",
                                     extra_roots=(self.agents_root,))

    def source(self, name: str = "example", **kw: object) -> Path:
        return write_skill(self.sources / name, name=name, **kw)  # type: ignore[arg-type]


class FrontmatterCase(unittest.TestCase):
    """The two keys codex acts on, out of the shapes people actually write."""

    def test_a_plain_scalar_is_read(self) -> None:
        front = skills.parse_frontmatter(
            "---\nname: thing\ndescription: does a thing\n---\nbody\n")
        self.assertEqual(front["name"], "thing")
        self.assertEqual(front["description"], "does a thing")

    def test_quotes_are_stripped(self) -> None:
        front = skills.parse_frontmatter(
            "---\nname: 'thing'\ndescription: \"does a thing\"\n---\n")
        self.assertEqual(front["name"], "thing")
        self.assertEqual(front["description"], "does a thing")

    def test_a_wrapped_value_is_folded_back_into_one_line(self) -> None:
        front = skills.parse_frontmatter(
            "---\nname: thing\ndescription: one\n  two\n  three\n---\n")
        self.assertEqual(front["description"], "one two three")

    def test_a_block_scalar_indicator_is_not_part_of_the_value(self) -> None:
        """Both packaged Omarchy skills are written this way.

        Read naively, `description: >` makes the description the string ">",
        which is neither empty nor long — so it passes an "is it present"
        check and fails a length check for a reason that is not the real one.
        """
        for marker in (">", ">-", "|", "|-", "|2-"):
            with self.subTest(marker=marker):
                front = skills.parse_frontmatter(
                    f"---\nname: thing\ndescription: {marker}\n"
                    f"  REQUIRED for the thing.\n  Use when editing X.\n---\n")
                self.assertEqual(front["description"],
                                 "REQUIRED for the thing. Use when editing X.")

    def test_no_frontmatter_at_all_is_an_empty_mapping(self) -> None:
        # A leading blank line is enough: codex wants `---` on line one, so
        # this is a real way to ship a skill that is never seen.
        self.assertEqual(skills.parse_frontmatter("\n---\nname: x\n---\n"), {})
        self.assertEqual(skills.parse_frontmatter("# just a heading\n"), {})

    def test_an_unterminated_block_is_not_frontmatter(self) -> None:
        self.assertEqual(skills.parse_frontmatter("---\nname: x\nbody\n"), {})

    def test_the_real_shipped_skill_parses(self) -> None:
        """The repo's own `luna-machine` is the house-style reference."""
        md = config.SKILLS_DIR / "luna-machine" / "SKILL.md"
        front = skills.parse_frontmatter(md.read_text())
        self.assertEqual(front["name"], "luna-machine")
        self.assertGreater(len(front["description"]),
                           config.SKILL_MIN_DESCRIPTION)


class ValidateCase(FarmCase):
    """Nothing is linked until the candidate has been read."""

    def test_a_good_skill_gives_back_its_name_and_description(self) -> None:
        name, description = self.farm.validate(self.source("example"))
        self.assertEqual(name, "example")
        self.assertEqual(description, GOOD_DESCRIPTION)

    def test_a_directory_with_no_skill_md_is_refused(self) -> None:
        bare = self.sources / "bare"
        bare.mkdir()
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.validate(bare)
        self.assertIn("no SKILL.md", str(caught.exception))

    def test_no_frontmatter_is_refused(self) -> None:
        path = write_skill(self.sources / "nofront", frontmatter="")
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.validate(path)
        self.assertIn("frontmatter", str(caught.exception))

    def test_a_missing_name_is_refused(self) -> None:
        path = write_skill(self.sources / "noname",
                           frontmatter=f"---\ndescription: {GOOD_DESCRIPTION}\n---\n")
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.validate(path)
        self.assertIn("`name:`", str(caught.exception))

    def test_an_empty_description_is_refused_and_says_why(self) -> None:
        path = write_skill(self.sources / "nodesc",
                           frontmatter="---\nname: nodesc\ndescription:\n---\n")
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.validate(path)
        self.assertIn("retrieval surface", str(caught.exception))

    def test_a_refused_skill_leaves_the_farm_untouched(self) -> None:
        """The whole point of validating first."""
        bare = self.sources / "bare"
        bare.mkdir()
        with self.assertRaises(skills.SkillError):
            self.farm.add(str(bare))
        self.assertFalse((self.farm_root / "bare").exists())
        self.assertFalse((self.farm_root / "bare").is_symlink())


class AddCase(FarmCase):
    def test_adding_links_rather_than_copies(self) -> None:
        path = self.source("example")
        skill = self.farm.add(str(path))
        link = self.farm_root / "example"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), path.resolve())
        self.assertEqual(skill.name, "example")
        self.assertTrue(skill.ok)

    def test_the_frontmatter_name_wins_over_the_directory_name(self) -> None:
        path = write_skill(self.sources / "some-checkout", name="real-name")
        skill = self.farm.add(str(path))
        self.assertEqual(skill.name, "real-name")
        self.assertTrue((self.farm_root / "real-name").is_symlink())

    def test_an_explicit_name_wins_over_both(self) -> None:
        path = self.source("example")
        skill = self.farm.add(str(path), name="renamed")
        self.assertEqual(skill.name, "renamed")

    def test_a_collision_is_refused_and_names_the_incumbent(self) -> None:
        first = self.source("example")
        self.farm.add(str(first))
        other = write_skill(self.sources / "elsewhere", name="example")
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.add(str(other))
        message = str(caught.exception)
        self.assertIn("already installed", message)
        self.assertIn(str(first), message)
        # And the incumbent is still the one that is linked.
        self.assertEqual((self.farm_root / "example").resolve(),
                         first.resolve())

    def test_a_repository_of_one_skill_is_found_one_level_down(self) -> None:
        repo = self.sources / "repo"
        write_skill(repo / "only-one", name="only-one")
        skill = self.farm.add(str(repo))
        self.assertEqual(skill.name, "only-one")
        self.assertEqual(skill.target, (repo / "only-one").resolve())

    def test_a_repository_of_several_refuses_and_lists_them(self) -> None:
        """`github.com/openai/skills` is a directory of skills, not one skill.

        Picking one for the user would be a guess; the refusal is written so
        the next command can be typed straight out of it.
        """
        repo = self.sources / "many"
        write_skill(repo / "alpha", name="alpha")
        write_skill(repo / "beta", name="beta")
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.add(str(repo))
        message = str(caught.exception)
        self.assertIn("2 skills", message)
        self.assertIn(f"luna skills add {repo / 'alpha'}", message)
        self.assertIn(f"luna skills add {repo / 'beta'}", message)
        self.assertFalse(self.farm_root.exists())

    def test_a_directory_with_no_skill_anywhere_is_refused(self) -> None:
        empty = self.sources / "nothing"
        (empty / "docs").mkdir(parents=True)
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.add(str(empty))
        self.assertIn("not a skill", str(caught.exception))


class RemoveCase(FarmCase):
    def test_removing_unlinks_and_leaves_the_target_alone(self) -> None:
        path = self.source("example")
        self.farm.add(str(path))
        skill = self.farm.remove("example")
        self.assertFalse((self.farm_root / "example").is_symlink())
        self.assertTrue((path / "SKILL.md").is_file(),
                        "remove deleted the source directory")
        self.assertEqual(skill.name, "example")

    def test_a_real_directory_is_never_deleted(self) -> None:
        """Somebody put an actual skill in the farm. It is the only copy."""
        real = write_skill(self.farm_root / "inplace", name="inplace")
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.remove("inplace")
        self.assertIn("not a symlink", str(caught.exception))
        self.assertTrue((real / "SKILL.md").is_file())

    def test_a_link_into_usr_share_is_refused(self) -> None:
        """Packaged skills come back on the next update. Say so instead."""
        self.farm_root.mkdir(parents=True, exist_ok=True)
        (self.farm_root / "omarchy").symlink_to(
            "/usr/share/omarchy/default/agents/skills/omarchy",
            target_is_directory=True)
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.remove("omarchy")
        self.assertIn("owned by a package", str(caught.exception))
        self.assertTrue((self.farm_root / "omarchy").is_symlink())

    def test_codex_own_system_tree_is_refused(self) -> None:
        (self.farm_root / config.SKILLS_SYSTEM_DIR).mkdir(parents=True)
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.remove(config.SKILLS_SYSTEM_DIR)
        self.assertIn("codex's own", str(caught.exception))
        self.assertTrue((self.farm_root / config.SKILLS_SYSTEM_DIR).is_dir())

    def test_removing_something_absent_says_so(self) -> None:
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.remove("never-installed")
        self.assertIn("no skill called", str(caught.exception))


class ListCase(FarmCase):
    def test_an_empty_farm_lists_nothing_without_raising(self) -> None:
        self.assertEqual(self.farm.list(), [])

    def test_a_dangling_link_is_reported_rather_than_dropped(self) -> None:
        """The failure with no other symptom.

        codex does not announce a skill it could not read; it simply has one
        fewer, which from the outside looks exactly like a model that chose
        not to use it. So the row has to survive into the listing.
        """
        path = self.source("example")
        self.farm.add(str(path))
        for child in path.iterdir():
            child.unlink()
        path.rmdir()

        found = self.farm.list()
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].resolves)
        self.assertFalse(found[0].ok)
        self.assertIn("dangling", found[0].problems[0])
        self.assertIn(str(path), found[0].problems[0],
                      "the message must name the path that went away")

    def test_a_link_to_a_directory_with_no_skill_md_is_flagged(self) -> None:
        target = self.sources / "hollow"
        target.mkdir()
        self.farm_root.mkdir(parents=True)
        (self.farm_root / "hollow").symlink_to(target,
                                               target_is_directory=True)
        found = self.farm.list()
        self.assertIn("no SKILL.md", found[0].problems[0])

    def test_sources_are_classified_and_sorted(self) -> None:
        self.farm_root.mkdir(parents=True)
        (self.farm_root / "luna-machine").symlink_to(
            config.SKILLS_DIR / "luna-machine", target_is_directory=True)
        (self.farm_root / "omarchy").symlink_to(
            "/usr/share/omarchy/default/agents/skills/omarchy",
            target_is_directory=True)
        (self.farm_root / "mine").symlink_to(self.source("mine"),
                                             target_is_directory=True)
        system = self.farm_root / config.SKILLS_SYSTEM_DIR
        write_skill(system / "skill-creator", name="skill-creator")

        found = {s.name: s for s in self.farm.list()}
        self.assertEqual(found["luna-machine"].source, "repo")
        self.assertEqual(found["omarchy"].source, "omarchy")
        self.assertEqual(found["mine"].source, "external")
        self.assertEqual(found["skill-creator"].source, "system")
        self.assertEqual([s.source for s in self.farm.list()],
                         list(skills.SOURCES))

    def test_only_a_link_outside_usr_share_is_removable(self) -> None:
        self.farm_root.mkdir(parents=True)
        (self.farm_root / "mine").symlink_to(self.source("mine"),
                                             target_is_directory=True)
        (self.farm_root / "omarchy").symlink_to(
            "/usr/share/omarchy/default/agents/skills/omarchy",
            target_is_directory=True)
        write_skill(self.farm_root / "inplace", name="inplace")
        found = {s.name: s for s in self.farm.list()}
        self.assertTrue(found["mine"].removable)
        self.assertFalse(found["omarchy"].removable)
        self.assertFalse(found["inplace"].removable)

    def test_loose_files_in_the_farm_are_not_skills(self) -> None:
        self.farm_root.mkdir(parents=True)
        (self.farm_root / "README.md").write_text("not a skill\n")
        self.assertEqual(self.farm.list(), [])


class DoctorCase(FarmCase):
    def test_a_healthy_farm_has_nothing_to_say(self) -> None:
        self.farm.add(str(self.source("example")))
        found, notes = self.farm.doctor()
        self.assertEqual(notes, [])
        self.assertTrue(all(s.ok for s in found))

    def test_a_description_too_short_to_match_is_a_defect(self) -> None:
        """Not a style note. It is a skill that never gets chosen."""
        path = write_skill(self.sources / "terse", name="terse",
                           description="Does stuff.")
        self.farm.add(str(path))
        found, _ = self.farm.doctor()
        self.assertFalse(found[0].ok)
        self.assertIn("trigger words", found[0].problems[0])

    def test_a_frontmatter_name_that_disagrees_with_the_directory(self) -> None:
        self.farm.add(str(self.source("example")), name="other")
        found, _ = self.farm.doctor()
        self.assertIn("two different names", found[0].problems[0])

    def test_two_skills_answering_to_one_name_is_a_farm_wide_note(self) -> None:
        self.farm.add(str(self.source("example")))
        second = write_skill(self.sources / "second", name="example")
        self.farm_root.joinpath("example-two").symlink_to(
            second, target_is_directory=True)
        _, notes = self.farm.doctor()
        self.assertTrue(any("shadowed" in note for note in notes), notes)

    def test_a_dangling_link_survives_into_doctor(self) -> None:
        path = self.source("example")
        self.farm.add(str(path))
        (path / "SKILL.md").unlink()
        path.rmdir()
        found, _ = self.farm.doctor()
        self.assertFalse(found[0].ok)

    def test_a_farm_past_the_soft_limit_is_a_note_not_an_error(self) -> None:
        """Retrieval accuracy goes before token cost does."""
        for i in range(config.SKILL_FARM_SOFT_LIMIT + 1):
            self.farm.add(str(self.source(f"skill-{i:03d}")))
        found, notes = self.farm.doctor()
        self.assertTrue(all(s.ok for s in found))
        self.assertTrue(any("compete with each other" in n for n in notes),
                        notes)


class OtherRootsCase(FarmCase):
    """`~/.agents/skills/` is read by codex too, and is not Luna's to repair.

    Found by watching a real turn: codex reached for `~/.agents/skills/` — the
    cross-agent convention — *before* the codex-own farm. A doctor that
    reports a clean farm while a second live root is broken is a doctor that
    passes for the wrong reason.
    """

    def test_a_dangling_link_in_the_other_root_is_reported(self) -> None:
        self.agents_root.mkdir(parents=True)
        gone = self.sources / "gone"
        write_skill(gone, name="gone")
        (self.agents_root / "gone").symlink_to(gone, target_is_directory=True)
        (gone / "SKILL.md").unlink()
        gone.rmdir()
        _, notes = self.farm.doctor()
        self.assertTrue(any("dangling" in n and "gone" in n for n in notes),
                        notes)

    def test_a_healthy_other_root_says_nothing(self) -> None:
        self.agents_root.mkdir(parents=True)
        (self.agents_root / "fine").symlink_to(self.source("fine"),
                                               target_is_directory=True)
        self.assertEqual(self.farm.doctor()[1], [])

    def test_a_skill_in_one_root_but_not_the_other_is_not_a_defect(self) -> None:
        """An ordinary arrangement. Complaining about it would be noise."""
        self.agents_root.mkdir(parents=True)
        (self.agents_root / "elsewhere").symlink_to(
            self.source("elsewhere"), target_is_directory=True)
        self.farm.add(str(self.source("here")))
        self.assertEqual(self.farm.doctor()[1], [])

    def test_a_missing_other_root_is_not_an_error(self) -> None:
        self.assertFalse(self.agents_root.exists())
        self.assertEqual(self.farm.doctor()[1], [])

    def test_the_other_root_is_never_written_to(self) -> None:
        self.agents_root.mkdir(parents=True)
        self.farm.add(str(self.source("here")))
        self.assertEqual(list(self.agents_root.iterdir()), [],
                         "add() reached into a root it does not manage")
        self.farm.remove("here")
        self.assertEqual(list(self.agents_root.iterdir()), [])


class CloneCase(FarmCase):
    """The one network path, and the two things it must not be.

    No `curl | sh` and no `npx`: an installer script for a skills registry
    runs third-party code as the user, on this machine, to obtain a directory
    with a markdown file in it. A clone gets the same result and can be read
    first.
    """

    def test_a_url_is_recognised_as_one(self) -> None:
        for url in ("https://github.com/openai/skills",
                    "https://github.com/openai/skills.git",
                    "git@github.com:openai/skills.git",
                    "ssh://git@example.com/x/y.git"):
            with self.subTest(url=url):
                self.assertTrue(skills._is_git_url(url))
        for path in ("/home/ghost/Work/luna/skills/luna-machine",
                     "./skills", "skills/luna-machine"):
            with self.subTest(path=path):
                self.assertFalse(skills._is_git_url(path))

    def test_the_clone_directory_name_comes_from_the_url(self) -> None:
        self.assertEqual(skills._repo_name(
            "https://github.com/openai/skills.git"), "skills")
        self.assertEqual(skills._repo_name(
            "git@github.com:openai/skills.git"), "skills")
        self.assertEqual(skills._repo_name(
            "https://github.com/openai/skills/"), "skills")

    def test_a_clone_with_no_git_fails_as_a_refusal_naming_git(self) -> None:
        """The sentinel is the only thing between this and a real clone."""
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.add("https://github.com/openai/skills.git")
        self.assertIn("/nonexistent/luna-tests-git", str(caught.exception))
        self.assertIn("not on PATH", str(caught.exception))

    def test_an_existing_clone_is_never_silently_overwritten(self) -> None:
        (self.store / "skills").mkdir(parents=True)
        with self.assertRaises(skills.SkillError) as caught:
            self.farm.add("https://github.com/openai/skills.git")
        self.assertIn("already exists", str(caught.exception))

    def test_nothing_is_ever_cloned_into_the_farm_itself(self) -> None:
        """A clone in the farm would put a .git in the tree codex walks.

        It would also turn `remove` from an unlink into an `rm -rf`, which is
        the one thing this module refuses to be.
        """
        self.assertNotEqual(self.farm.store, self.farm.root)
        try:
            self.farm._clone("https://github.com/openai/skills.git")
        except skills.SkillError:
            pass
        self.assertFalse(self.farm_root.exists())


class RepoSkillsCase(unittest.TestCase):
    """The skills this repository ships must pass its own doctor.

    A skill that fails `luna skills doctor` after being committed is a skill
    somebody wrote, linked, and never got any use out of — and the failure is
    silent, so nothing else would ever catch it.
    """

    def farm(self) -> skills.SkillFarm:
        return skills.SkillFarm(root=config.SKILLS_DIR, extra_roots=())

    def test_every_shipped_skill_is_complete_and_matchable(self) -> None:
        found = self.farm().doctor()[0]
        self.assertTrue(found, "the repo ships no skills at all")
        for skill in found:
            with self.subTest(skill=skill.name):
                self.assertEqual(skill.problems, [],
                                 f"skills/{skill.name} would not be usable")

    def test_no_two_shipped_skills_share_a_name(self) -> None:
        self.assertEqual(self.farm().doctor()[1], [])

    def test_the_descriptions_carry_more_than_one_sentence(self) -> None:
        """Retrieval is the description. One clause is not enough of one."""
        for skill in self.farm().list():
            with self.subTest(skill=skill.name):
                self.assertGreaterEqual(
                    len(skill.description), 120,
                    f"skills/{skill.name}: the description is the entire "
                    f"retrieval surface and this one is too thin to match on")


class NotTheRealFarmCase(unittest.TestCase):
    """Proof, not a promise: this suite cannot reach the user's own skills.

    `tests/_support.py` redirects both paths process-wide. This asserts the
    redirect held, and that nothing in this file wrote into the real farm —
    because the damage has no symptom. An unlinked skill does not error, does
    not warn, and does not appear in a log; Luna is simply less capable than
    she was, from a change nobody can see.
    """

    def test_the_farm_and_the_store_are_redirected(self) -> None:
        self.assertEqual(config.CODEX_SKILLS_DIR, FORBIDDEN_CODEX_SKILLS_DIR)
        self.assertEqual(config.SKILLS_STORE_DIR, FORBIDDEN_SKILLS_STORE_DIR)
        for path in (config.CODEX_SKILLS_DIR, config.SKILLS_STORE_DIR):
            self.assertFalse(str(path).startswith(str(Path.home())), str(path))

    def test_the_users_real_farm_still_holds_what_it_held(self) -> None:
        """Read-only, and only to prove this run did not edit it.

        Skipped where there is no real farm — a CI runner has none, and a
        machine that does not run codex is not the machine this protects.
        """
        real = Path(os.path.expanduser("~/.codex/skills"))
        if not real.is_dir():
            self.skipTest("no real skills farm on this machine")
        for entry in real.iterdir():
            with self.subTest(entry=entry.name):
                self.assertTrue(entry.exists(),
                                f"{entry} is dangling — did a test unlink "
                                f"its target?")

    def test_git_is_disarmed_for_anything_that_would_clone(self) -> None:
        self.assertEqual(config.GIT_BIN, FORBIDDEN_GIT)

    def test_the_cross_agent_root_is_redirected_too(self) -> None:
        """Read-only, but reading the real one makes the suite machine-specific."""
        self.assertEqual(config.SKILLS_EXTRA_ROOTS,
                         FORBIDDEN_SKILLS_EXTRA_ROOTS)
        for path in config.SKILLS_EXTRA_ROOTS:
            self.assertNotEqual(path, Path.home() / ".agents" / "skills")
