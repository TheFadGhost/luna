"""Write-through to voxtype's config: the projection, the file, and the daemon.

Three things are being defended here, in the order they would hurt:

  * voxtype's file survives. Comments, key order, inline comments and tables
    Jarvis has never heard of all come back byte for byte, and the gotcha
    comment above `model` — the one that says the endpoint is sent `model` and
    not `remote_model` — is in the fixture precisely so a writer that eats it
    fails a test rather than a morning.
  * a recording is never interrupted. While voxtype says `recording` or
    `transcribing`, nothing is written at all.
  * a change that could not be put into force says so. voxtype reads its
    config once, at start-up, so "written" and "in effect" are different
    claims and the outcome distinguishes them.

Nothing here touches the real `~/.config/voxtype/config.toml` and nothing here
restarts the real daemon: `_support` replaces both names process-wide.
"""

import os
import pathlib
import stat
import sys
import tempfile
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import _support  # noqa: E402,F401  (imported for the guard)

from jarvis import voxtype  # noqa: E402

# A cut-down copy of the real file, keeping the parts that make writing to it
# risky: a comment block carrying a gotcha, an inline comment with a `#` in the
# value's own neighbourhood, a table Jarvis knows nothing about, and a dotted
# table after the one being edited.
SAMPLE = '''# Voxtype Configuration
#
# Location: ~/.config/voxtype/config.toml
state_file = "auto"

[audio]
device = "default"
sample_rate = 16000

[whisper]
# Model to use for transcription
# GOTCHA (voxtype 0.7.5): in mode="remote" the daemon sends THIS key to the
# remote endpoint, NOT `remote_model` — despite remote_model being a real field
# in its config struct.
model = "fish-audio/transcribe-1"

# Language for transcription
language = "en"
translate = false

mode = "remote"
remote_endpoint = "https://openrouter.ai/api"  # NOT /v1 - voxtype appends it
remote_model = "fish-audio/transcribe-1"
remote_timeout_secs = 30

[output]
mode = "type"

[profiles.luna]
post_process_command = "/home/ghost/Work/luna/bin/luna-voice-router"
output_mode = "clipboard"
'''

REMOTE = {"listen.provider": "openrouter",
          "listen.model": "fish-audio/transcribe-1",
          "listen.language": "en"}


class Case(unittest.TestCase):
    """A throwaway copy of SAMPLE at `self.path`, and a daemon that is down."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis-voxtype-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.path = self.dir / "config.toml"
        self.path.write_text(SAMPLE, encoding="utf-8")
        os.chmod(self.path, 0o644)

    def swap(self, name, value):
        old = getattr(voxtype, name)
        setattr(voxtype, name, value)
        self.addCleanup(setattr, voxtype, name, old)

    def state(self, text):
        p = self.dir / "state"
        p.write_text(text, encoding="utf-8")
        self.swap("STATE_PATH", p)

    def values(self, **over):
        v = dict(REMOTE)
        v.update({f"listen.{k}": w for k, w in over.items()})
        return v

    def parsed(self):
        return tomllib.loads(self.path.read_text(encoding="utf-8"))


class GuardTest(unittest.TestCase):
    """The suite must not be able to reach the real file or the real daemon."""

    def test_the_config_path_is_the_sentinel(self):
        self.assertEqual(voxtype.CONFIG_PATH, _support.FORBIDDEN_CONFIG)
        self.assertFalse(voxtype.CONFIG_PATH.exists())
        self.assertNotIn(".config/voxtype", str(voxtype.CONFIG_PATH))

    def test_the_restart_command_cannot_resolve(self):
        self.assertEqual(voxtype.RESTART_COMMAND, _support.FORBIDDEN_RESTART)
        ok, detail = voxtype.restart()
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_the_daemon_looks_absent_whatever_it_is_doing(self):
        self.assertEqual(voxtype.activity(), "unknown")
        self.assertIsNone(voxtype.running_pid())
        self.assertIsNone(voxtype.stale())


class ProjectionTest(Case):
    def test_remote_writes_the_model_twice_because_of_the_gotcha(self):
        # voxtype sends `model` to the endpoint in remote mode. `remote_model`
        # is kept in step so the file has one answer, not two.
        got = voxtype.projection(self.values())
        self.assertEqual(got["whisper.mode"], "remote")
        self.assertEqual(got["whisper.model"], "fish-audio/transcribe-1")
        self.assertEqual(got["whisper.remote_model"], "fish-audio/transcribe-1")

    def test_local_leaves_remote_model_alone(self):
        got = voxtype.projection(self.values(provider="local", model="base.en"))
        self.assertEqual(got["whisper.mode"], "local")
        self.assertEqual(got["whisper.model"], "base.en")
        self.assertNotIn("whisper.remote_model", got)

    def test_language_lands_on_whispers_own_key(self):
        got = voxtype.projection(self.values(language="fr"))
        self.assertEqual(got["whisper.language"], "fr")

    def test_an_openrouter_id_is_refused_in_local_mode(self):
        with self.assertRaises(voxtype.VoxtypeError) as caught:
            voxtype.projection(self.values(provider="local"))
        self.assertIn("base.en", str(caught.exception))

    def test_an_absolute_path_is_a_legitimate_local_model(self):
        got = voxtype.projection(
            self.values(provider="local", model="/opt/models/custom.bin"))
        self.assertEqual(got["whisper.model"], "/opt/models/custom.bin")

    def test_an_empty_model_is_refused(self):
        with self.assertRaises(voxtype.VoxtypeError):
            voxtype.projection(self.values(model="  "))

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(voxtype.VoxtypeError):
            voxtype.projection(self.values(provider="whisper.cpp"))

    def test_a_refused_projection_never_reaches_the_file(self):
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(voxtype.VoxtypeError):
            voxtype.apply(self.values(provider="local"), self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


class MirrorTest(Case):
    def test_the_file_reads_back_as_listen_values(self):
        _text, vox = voxtype.read(self.path)
        self.assertEqual(voxtype.mirrored(vox), REMOTE)

    def test_local_mode_reads_back_as_the_local_provider(self):
        self.assertEqual(
            voxtype.mirrored({"whisper.mode": "local",
                              "whisper.model": "base.en",
                              "whisper.language": "en"})["listen.provider"],
            "local")

    def test_no_mode_key_at_all_means_local(self):
        # voxtype's own default. A file that has never been switched to remote
        # simply has no `mode`, and reading that as "openrouter" would invent a
        # disagreement that is not there.
        self.assertEqual(voxtype.mirrored({})["listen.provider"], "local")


class DriftTest(Case):
    def test_a_matching_pair_has_no_drift(self):
        self.assertEqual(voxtype.drift(self.values(), self.path), [])

    def test_a_hand_edit_to_voxtypes_file_shows_both_sides(self):
        self.path.write_text(
            SAMPLE.replace('language = "en"', 'language = "de"'),
            encoding="utf-8")
        rows = voxtype.drift(self.values(), self.path)
        self.assertEqual([r["key"] for r in rows], ["listen.language"])
        self.assertEqual(rows[0]["jarvis"], "en")
        self.assertEqual(rows[0]["voxtype"], "de")

    def test_a_key_voxtype_does_not_have_is_reported_as_missing(self):
        self.path.write_text(
            SAMPLE.replace('language = "en"\n', ""), encoding="utf-8")
        rows = voxtype.drift(self.values(), self.path)
        self.assertIsNone(rows[0]["voxtype"])

    def test_drift_reports_and_does_not_resolve(self):
        edited = SAMPLE.replace('language = "en"', 'language = "de"')
        self.path.write_text(edited, encoding="utf-8")
        voxtype.drift(self.values(), self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), edited)

    def test_a_missing_file_is_named_not_swallowed(self):
        with self.assertRaises(voxtype.VoxtypeError) as caught:
            voxtype.drift(self.values(), self.dir / "nowhere.toml")
        self.assertIn("nowhere.toml", str(caught.exception))

    def test_an_unparseable_file_is_refused_before_any_write(self):
        broken = "[whisper\nmodel = "
        self.path.write_text(broken, encoding="utf-8")
        with self.assertRaises(voxtype.VoxtypeError) as caught:
            voxtype.apply(self.values(language="fr"), self.path)
        self.assertIn("does not parse", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)


class WriteTest(Case):
    def test_the_file_survives_the_edit(self):
        self.state("idle")
        voxtype.apply(self.values(language="fr"), self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("GOTCHA (voxtype 0.7.5)", text)
        self.assertIn("# NOT /v1 - voxtype appends it", text)
        self.assertIn("[profiles.luna]", text)
        self.assertIn('post_process_command = "/home/ghost/Work/luna/bin/'
                      'luna-voice-router"', text)
        self.assertEqual(self.parsed()["whisper"]["language"], "fr")

    def test_only_the_keys_that_differ_are_touched(self):
        self.state("idle")
        out = voxtype.apply(self.values(language="fr"), self.path)
        self.assertEqual(out.written, ("whisper.language",))

    def test_switching_to_local_writes_the_mode_and_the_model_together(self):
        self.state("idle")
        out = voxtype.apply(self.values(provider="local", model="base.en"),
                            self.path)
        self.assertEqual(sorted(out.written),
                         ["whisper.mode", "whisper.model"])
        whisper = self.parsed()["whisper"]
        self.assertEqual(whisper["mode"], "local")
        self.assertEqual(whisper["model"], "base.en")
        # Left as it was, so switching back to remote does not have to guess
        # which model the user was using.
        self.assertEqual(whisper["remote_model"], "fish-audio/transcribe-1")

    def test_nothing_to_do_writes_nothing(self):
        self.state("idle")
        before = self.path.read_text(encoding="utf-8")
        out = voxtype.apply(self.values(), self.path)
        self.assertTrue(out.ok)
        self.assertEqual(out.written, ())
        self.assertFalse(out.restarted)
        self.assertIn("already matches", out.detail)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_the_mode_of_voxtypes_file_is_not_tightened(self):
        self.state("idle")
        voxtype.apply(self.values(language="fr"), self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)


class BackupTest(Case):
    def backup(self):
        return self.path.with_name(self.path.name + voxtype.BACKUP_SUFFIX)

    def test_the_first_write_snapshots_the_original(self):
        self.state("idle")
        voxtype.apply(self.values(language="fr"), self.path)
        self.assertEqual(self.backup().read_text(encoding="utf-8"), SAMPLE)

    def test_the_second_write_leaves_the_snapshot_alone(self):
        # Otherwise the backup ends up being a copy of what Jarvis wrote, which
        # answers a different question from the one it exists to answer.
        self.state("idle")
        voxtype.apply(self.values(language="fr"), self.path)
        voxtype.apply(self.values(language="de"), self.path)
        self.assertEqual(self.backup().read_text(encoding="utf-8"), SAMPLE)

    def test_no_write_means_no_backup(self):
        self.state("idle")
        voxtype.apply(self.values(), self.path)
        self.assertFalse(self.backup().exists())


class RecordingTest(Case):
    def test_a_recording_refuses_the_whole_save(self):
        self.state("recording")
        before = self.path.read_text(encoding="utf-8")
        out = voxtype.apply(self.values(language="fr"), self.path)
        self.assertFalse(out.ok)
        self.assertEqual(out.written, ())
        self.assertIn("recording", out.detail)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_transcribing_counts_as_busy(self):
        self.state("transcribing")
        out = voxtype.apply(self.values(language="fr"), self.path)
        self.assertFalse(out.ok)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE)

    def test_an_unreadable_state_file_does_not_block_the_save(self):
        # `unknown` means the daemon is down or has published nothing. Refusing
        # every save on a machine where voxtype is not running would make the
        # setting unusable, and there is no recording to lose.
        out = voxtype.apply(self.values(language="fr"), self.path)
        self.assertTrue(out.ok)
        self.assertEqual(self.parsed()["whisper"]["language"], "fr")


class RestartTest(Case):
    def test_a_daemon_that_is_not_running_is_not_a_failure(self):
        self.state("idle")
        out = voxtype.apply(self.values(language="fr"), self.path)
        self.assertTrue(out.ok)
        self.assertFalse(out.restarted)
        self.assertIn("not running", out.detail)

    def test_a_failed_restart_is_reported_as_a_failure(self):
        # The file is right and the daemon is wrong, which is the one outcome
        # that looks like success and is not.
        self.state("idle")
        self.swap("running_pid", lambda: 4242)
        out = voxtype.apply(self.values(language="fr"), self.path)
        self.assertFalse(out.ok)
        self.assertEqual(out.written, ("whisper.language",))
        self.assertFalse(out.restarted)
        self.assertIn("old settings", out.detail)
        self.assertEqual(self.parsed()["whisper"]["language"], "fr")

    def test_a_successful_restart_is_reported_as_one(self):
        self.state("idle")
        self.swap("running_pid", lambda: 4242)
        self.swap("restart", lambda: (True, "systemctl --user restart voxtype"))
        out = voxtype.apply(self.values(language="fr"), self.path)
        self.assertTrue(out.ok)
        self.assertTrue(out.restarted)


class StalenessTest(Case):
    def test_a_config_newer_than_the_daemon_is_stale(self):
        self.swap("running_pid", lambda: os.getpid())
        os.utime(self.path, (2 ** 31, 2 ** 31))     # far in the future
        self.assertTrue(voxtype.stale(self.path))

    def test_a_config_older_than_the_daemon_is_not(self):
        self.swap("running_pid", lambda: os.getpid())
        os.utime(self.path, (0, 0))
        self.assertFalse(voxtype.stale(self.path))

    def test_no_daemon_means_the_question_cannot_be_answered(self):
        self.assertIsNone(voxtype.stale(self.path))

    def test_a_pid_file_pointing_at_something_else_is_not_voxtype(self):
        # Linux recycles pids, and a stale pid file left by a daemon that died
        # points at whatever inherited the number.
        pid = self.dir / "pid"
        pid.write_text(str(os.getpid()), encoding="utf-8")
        self.swap("PID_PATH", pid)
        self.assertIsNone(voxtype.running_pid())


class ContractTest(unittest.TestCase):
    def test_enabled_and_keybind_are_not_written_through(self):
        # `enabled` is honoured by bin/luna-voice-router and `keybind` by
        # Hyprland. Projecting either on to voxtype would be inventing a
        # mapping, which is the thing this whole block was documented as
        # avoiding.
        self.assertEqual(sorted(voxtype.WRITE_THROUGH_KEYS),
                         ["listen.language", "listen.model", "listen.provider"])


if __name__ == "__main__":
    unittest.main()
