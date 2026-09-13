import json
import os
import tempfile
import tomllib
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest.mock import patch

from codex_accounts.history import prepare_history
from codex_accounts.shared import DIRECTORIES, MARKER, _toml_document, prepare_home
from codex_accounts.state import AccountError, Store


class SharedHomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.shared = self.root / "original"
        self.shared.mkdir()
        self.homes = [self.root / "alice", self.root / "bob"]
        self.store = Store(self.root / "manager")
        with self.store.edit() as state:
            for home in self.homes:
                home.mkdir()
                state["accounts"][home.name] = {"home": str(home)}
        probe = self.root / "probe"
        try:
            probe.symlink_to(self.shared, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic links are unavailable")
        probe.unlink()
        environment = patch.dict(
            os.environ, {"CODEX_ACCOUNTS_HISTORY_HOME": str(self.shared)}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, home, name, value):
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_recovers_skills_plugins_instructions_and_merges_config_for_every_login(
        self,
    ):
        original = '# keep this original in backup\nmodel = "original"\n[projects.old]\ntrust_level = "trusted"\n'
        self.write(self.shared, "config.toml", original)
        self.write(self.shared, "skills/old/SKILL.md", "original skill")
        self.write(self.homes[0], "skills/new/SKILL.md", "new skill")
        self.write(self.homes[1], "plugins/custom/manifest.json", '{"name": "custom"}')
        self.write(self.homes[1], "AGENTS.md", "my instructions")
        self.write(
            self.homes[0],
            "config.toml",
            'model = "account"\n[projects.new]\ntrust_level = "trusted"\n',
        )
        for home in [self.shared, *self.homes]:
            self.write(home, "auth.json", home.name)
        self.assertEqual(prepare_home(self.store), self.shared)
        for home in self.homes:
            self.assertEqual(
                (home / "skills/old/SKILL.md").read_text(), "original skill"
            )
            self.assertEqual((home / "skills/new/SKILL.md").read_text(), "new skill")
            self.assertEqual((home / "AGENTS.md").read_text(), "my instructions")
            self.assertTrue((home / "plugins/custom/manifest.json").exists())
            self.assertEqual((home / "auth.json").read_text(), home.name)
            self.assertFalse((home / "auth.json").is_symlink())
            settings = tomllib.loads((home / "config.toml").read_text())
            self.assertEqual(settings["model"], "original")
            self.assertEqual(set(settings["projects"]), {"old", "new"})
        self.assertEqual((self.shared / "auth.json").read_text(), "original")
        backup = next(self.homes[0].glob(".shared-backups/*/shared/config.toml"))
        self.assertEqual(backup.read_text(), original)

    def test_future_changes_and_atomic_shared_config_writes_reach_other_accounts(self):
        prepare_home(self.store)
        first, second = self.homes
        for name in DIRECTORIES:
            self.assertTrue((first / name).samefile(second / name))
        self.write(first, "skills/later/SKILL.md", "installed after migration")
        self.assertEqual(
            (second / "skills/later/SKILL.md").read_text(), "installed after migration"
        )
        self.write(first, "config.toml", 'model="changed"\n')
        self.assertEqual(
            tomllib.loads((second / "config.toml").read_text())["model"], "changed"
        )
        temporary = self.write(self.root, "new-config", 'model="atomic"\n')
        temporary.replace((first / "config.toml").resolve())
        self.assertEqual(
            tomllib.loads((second / "config.toml").read_text())["model"], "atomic"
        )
        self.assertTrue((first / "config.toml").is_symlink())

    def test_new_top_level_data_is_discovered_from_all_homes_on_next_launch(self):
        prepare_home(self.store)
        self.write(self.homes[1], "future-feature/note.txt", "future data")
        self.write(self.shared, "AGENTS.md", "shared instructions")
        prepare_home(self.store, self.homes[0])
        for home in self.homes:
            self.assertEqual(
                (home / "future-feature/note.txt").read_text(), "future data"
            )
            self.assertEqual((home / "AGENTS.md").read_text(), "shared instructions")
            self.assertTrue((home / "future-feature").is_symlink())

    def test_credentials_secrets_runtime_and_old_database_copies_stay_local(self):
        local_names = (
            "auth.json",
            "auth.json.backup",
            ".credentials.json",
            "secrets/codex_auth.age",
            "secrets/mcp_oauth.age",
            "tokens.json",
            ".env",
            "backups/auth.json",
            "tmp/process/file",
            ".tmp/process/file",
            "logs_2.sqlite",
            "logs_2.sqlite-wal",
        )
        for home in [self.shared, *self.homes]:
            for name in local_names:
                self.write(home, name, home.name)
        prepare_home(self.store)
        for home in [self.shared, *self.homes]:
            for name in local_names:
                self.assertEqual((home / name).read_text(), home.name)
                self.assertFalse((home / Path(name).parts[0]).is_symlink())
        self.assertFalse(list(self.homes[0].glob(".shared-backups/**/auth.json")))

    def test_conflicting_files_are_recoverable_and_repeated_launch_is_idempotent(self):
        self.write(self.shared, "skills/same/SKILL.md", "shared")
        self.write(self.homes[0], "skills/same/SKILL.md", "account")
        prepare_home(self.store)
        saved = next(
            self.homes[0].glob(".shared-backups/*/account/skills/same/SKILL.md")
        )
        self.assertEqual(saved.read_text(), "account")
        self.assertEqual((self.homes[0] / "skills/same/SKILL.md").read_text(), "shared")
        before = list(self.homes[0].glob(".shared-backups/*"))
        prepare_home(self.store)
        self.assertEqual(list(self.homes[0].glob(".shared-backups/*")), before)

    def test_external_skill_symlinks_keep_working(self):
        installed = self.root / "external-skill"
        self.write(installed, "SKILL.md", "linked skill")
        skills = self.homes[0] / "skills"
        skills.mkdir()
        (skills / "external").symlink_to(
            "../../external-skill", target_is_directory=True
        )
        prepare_home(self.store)
        for home in self.homes:
            self.assertTrue((home / "skills/external").is_symlink())
            self.assertEqual(
                (home / "skills/external/SKILL.md").read_text(), "linked skill"
            )

    def test_merges_skills_when_the_whole_skills_directory_is_an_external_link(self):
        installed = self.root / "external-skills"
        self.write(installed, "custom/SKILL.md", "external skill")
        (self.homes[0] / "skills").symlink_to(installed, target_is_directory=True)
        self.write(self.shared, "skills/original/SKILL.md", "original skill")
        prepare_home(self.store)
        for home in self.homes:
            self.assertEqual(
                (home / "skills/custom/SKILL.md").read_text(), "external skill"
            )
            self.assertEqual(
                (home / "skills/original/SKILL.md").read_text(), "original skill"
            )
        self.assertEqual((installed / "custom/SKILL.md").read_text(), "external skill")

    def test_detached_file_with_newer_edits_is_published_and_relinked(self):
        prepare_home(self.store)
        self.write(self.shared, "config.toml", 'model="before"\n')
        temporary = self.write(self.root, "detached", 'model="after"\n')
        temporary.replace(self.homes[0] / "config.toml")
        timestamp = (self.shared / "config.toml").stat().st_mtime_ns + 1_000_000_000
        os.utime(self.homes[0] / "config.toml", ns=(timestamp, timestamp))
        prepare_home(self.store)
        self.assertTrue((self.homes[0] / "config.toml").is_symlink())
        self.assertEqual((self.homes[1] / "config.toml").read_text(), 'model="after"\n')
        backup = next(self.homes[0].glob(".shared-backups/*/shared/config.toml"))
        self.assertEqual(backup.read_text(), 'model="before"\n')

    def test_merge_keeps_other_skills_and_settings_arrays_and_toml_value_types(self):
        self.write(
            self.shared,
            "config.toml",
            '[[skills.config]]\npath="same"\nenabled=false\n',
        )
        self.write(
            self.homes[0],
            "config.toml",
            '[[skills.config]]\npath="same"\nenabled=true\n[[skills.config]]\npath="unique"\nenabled=true\n',
        )
        prepare_home(self.store)
        settings = tomllib.loads((self.shared / "config.toml").read_text())
        self.assertEqual(
            settings["skills"]["config"],
            [{"path": "same", "enabled": False}, {"path": "unique", "enabled": True}],
        )
        values = {
            "text": 'line one\n"line two" 😀',
            "bool": True,
            "int": 10,
            "float": 2.5,
            "date": date(2026, 9, 13),
            "time": time(12, 13),
            "datetime": datetime(2026, 9, 13, tzinfo=timezone.utc),
            "nested": {"quoted.key": [{"key": "value", "empty": {}}]},
        }
        self.assertEqual(tomllib.loads(_toml_document(values)), values)

    def test_bad_config_preserves_originals_and_retry_succeeds(self):
        self.write(self.shared, "config.toml", 'model="original"\n')
        source = self.write(self.homes[0], "config.toml", "broken TOML\n")
        with self.assertRaises(AccountError):
            prepare_home(self.store)
        self.assertEqual(source.read_text(), "broken TOML\n")
        self.assertFalse(source.is_symlink())
        self.assertFalse((self.homes[0] / MARKER).exists())
        source.write_text('model="other"\n')
        prepare_home(self.store)
        self.assertTrue(source.is_symlink())

    def test_symlink_failure_restores_original_and_retry_succeeds(self):
        prepare_history(self.store)
        source = self.write(self.homes[0], "config.toml", 'model="original"\n')
        original = Path.symlink_to

        def fail_config(path, *args, **kwargs):
            if path == source:
                raise OSError("simulated link failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "symlink_to", fail_config):
            with self.assertRaises(OSError):
                prepare_home(self.store)
        self.assertEqual(source.read_text(), 'model="original"\n')
        self.assertFalse(source.is_symlink())
        prepare_home(self.store)
        self.assertTrue(source.is_symlink())

    def test_cannot_retarget_shared_home_using_a_stale_marker(self):
        self.write(
            self.homes[0],
            MARKER,
            json.dumps({"version": 1, "home": str(self.root), "names": []}),
        )
        with self.assertRaises(AccountError):
            prepare_home(self.store)
        self.assertFalse((self.homes[0] / "config.toml").exists())


if __name__ == "__main__":
    unittest.main()
