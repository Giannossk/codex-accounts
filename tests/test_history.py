import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_accounts.history import MARKER, HistoryBusy, prepare_history
from codex_accounts.state import AccountError, Store, file_lock


@unittest.skipUnless(os.name == "posix", "Migration requires symbolic links")
class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shared = self.root / "original"
        self.shared.mkdir()
        self.account = self.root / "account"
        self.account.mkdir()
        self.store = Store(self.root / "manager")
        with self.store.edit() as state:
            state["accounts"]["a"] = {
                "home": str(self.account),
                "email": "alice@example.com",
                "plan": "plus",
                "kind": "chatgpt",
            }
            state["selected"] = "a"
        self.addCleanup(patch.stopall)
        patch.dict(
            os.environ, {"CODEX_ACCOUNTS_HISTORY_HOME": str(self.shared)}
        ).start()
        self.databases = []
        self.addCleanup(lambda: [db.close() for db in self.databases])

    def database(self, directory, name, schema, rows=()):
        db = sqlite3.connect(directory / name)
        self.databases.append(db)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(schema)
        for statement, values in rows:
            db.execute(statement, values)
        db.commit()
        return db

    def state_database(self, directory, thread):
        return self.database(
            directory,
            "state_5.sqlite",
            "CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, title TEXT);",
            [
                (
                    "INSERT INTO threads VALUES (?,?,?)",
                    (thread, str(directory / "sessions" / f"{thread}.jsonl"), thread),
                )
            ],
        )

    def rollout(self, directory, name, contents):
        (directory / "sessions").mkdir(exist_ok=True)
        (directory / "sessions" / name).write_text(contents)

    def test_existing_store_and_credentials_stay_and_wal_history_is_imported(self):
        self.state_database(self.shared, "old")
        source = self.state_database(self.account, "new")
        self.rollout(self.shared, "old.jsonl", "old history\n")
        self.rollout(self.account, "new.jsonl", "new history\n")
        (self.account / "auth.json").write_text("account credentials")
        (self.shared / "auth.json").write_text("original credentials")
        (self.account / "config.toml").write_text('model="test"\n')
        for directory, text in ((self.shared, "old"), (self.account, "new")):
            for name in ("history.jsonl", "session_index.jsonl"):
                (directory / name).write_text(json.dumps({"text": text}) + "\n")
        self.database(
            self.account,
            "thread_history_1.sqlite",
            "CREATE TABLE items(thread_id TEXT, ordinal INTEGER, content TEXT, PRIMARY KEY(thread_id,ordinal));",
            [("INSERT INTO items VALUES (?,?,?)", ("new", 1, "message from WAL"))],
        )
        self.database(
            self.account,
            "goals_1.sqlite",
            "CREATE TABLE goals(thread_id TEXT PRIMARY KEY, objective TEXT);",
            [("INSERT INTO goals VALUES (?,?)", ("new", "complete the plan"))],
        )
        self.database(
            self.account,
            "queue_1.sqlite",
            "CREATE TABLE queue(thread_id TEXT PRIMARY KEY, message TEXT);",
            [("INSERT INTO queue VALUES (?,?)", ("new", "next task"))],
        )

        self.assertEqual(prepare_history(self.store, self.account), self.shared)
        self.assertEqual(
            (self.account / "auth.json").read_text(), "account credentials"
        )
        self.assertEqual(
            (self.shared / "auth.json").read_text(), "original credentials"
        )
        self.assertEqual((self.account / "config.toml").read_text(), 'model="test"\n')
        with sqlite3.connect(self.shared / "state_5.sqlite") as db:
            self.assertEqual(
                set(db.execute("SELECT id FROM threads")), {("old",), ("new",)}
            )
            imported = db.execute(
                "SELECT rollout_path FROM threads WHERE id='new'"
            ).fetchone()[0]
            self.assertEqual(Path(imported).read_text(), "new history\n")
        self.assertEqual(list(source.execute("SELECT id FROM threads")), [("new",)])
        for name, table, expected in (
            ("thread_history_1.sqlite", "items", ("new", 1, "message from WAL")),
            ("goals_1.sqlite", "goals", ("new", "complete the plan")),
            ("queue_1.sqlite", "queue", ("new", "next task")),
        ):
            with sqlite3.connect(self.shared / name) as db:
                self.assertEqual(
                    db.execute(f"SELECT * FROM {table}").fetchone(), expected
                )
        self.assertTrue(
            list(self.account.glob(".history-backups/*/sessions/new.jsonl"))
        )
        snapshots = list(self.store.root.glob("history-backups/*/state_5.sqlite"))
        self.assertEqual(len(snapshots), 1)
        with sqlite3.connect(snapshots[0]) as db:
            self.assertEqual(list(db.execute("SELECT id FROM threads")), [("old",)])
        before = (self.shared / "history.jsonl").read_bytes()
        prepare_history(self.store, self.account)
        self.assertEqual((self.shared / "history.jsonl").read_bytes(), before)

    def test_future_files_and_writer_locks_are_shared_between_accounts(self):
        prepare_history(self.store, self.account)
        second = self.root / "second"
        second.mkdir()
        prepare_history(self.store, second)
        (second / "sessions" / "future.jsonl").write_text("future chat")
        self.assertEqual(
            (self.account / "sessions" / "future.jsonl").read_text(), "future chat"
        )
        lock = second / "thread-writer-locks" / "thread.lock"
        with file_lock(lock):
            with self.assertRaises(AccountError):
                with file_lock(
                    self.account / "thread-writer-locks" / "thread.lock", blocking=False
                ):
                    self.fail("the same thread was writable under both accounts")
            # Already configured launches work while other sessions are active.
            self.assertEqual(prepare_history(self.store, self.account), self.shared)

    def test_active_account_is_not_migrated_until_it_closes(self):
        self.rollout(self.account, "active.jsonl", "still writing")
        lock_dir = self.account / "thread-writer-locks"
        lock_dir.mkdir()
        with file_lock(lock_dir / "active.lock"):
            with self.assertRaises(HistoryBusy):
                prepare_history(self.store, self.account)
            self.assertFalse((self.account / "sessions").is_symlink())
            self.assertFalse((self.account / MARKER).exists())
        prepare_history(self.store, self.account)
        self.assertTrue((self.account / "sessions").is_symlink())

    def test_conflicting_files_never_overwrite_either_copy(self):
        self.rollout(self.shared, "same.jsonl", "shared version")
        self.rollout(self.account, "same.jsonl", "account version")
        with self.assertRaisesRegex(AccountError, "conflict"):
            prepare_history(self.store, self.account)
        self.assertEqual(
            (self.shared / "sessions" / "same.jsonl").read_text(), "shared version"
        )
        self.assertEqual(
            (self.account / "sessions" / "same.jsonl").read_text(), "account version"
        )

    def test_unknown_database_format_preserves_originals(self):
        self.state_database(self.shared, "old")
        self.database(
            self.account,
            "state_5.sqlite",
            "CREATE TABLE threads(id TEXT PRIMARY KEY, unsupported_column TEXT);",
            [("INSERT INTO threads VALUES (?,?)", ("new", "unsupported"))],
        )
        with self.assertRaisesRegex(AccountError, "formats differ"):
            prepare_history(self.store, self.account)
        self.assertFalse((self.account / MARKER).exists())
        with sqlite3.connect(self.shared / "state_5.sqlite") as db:
            self.assertEqual(list(db.execute("SELECT id FROM threads")), [("old",)])

    def test_shared_threads_take_precedence_over_obsolete_account_database(self):
        target = self.state_database(self.shared, "same")
        source = self.state_database(self.account, "same")
        source.execute("UPDATE threads SET title='obsolete'")
        source.commit()
        prepare_history(self.store, self.account)
        self.assertEqual(
            target.execute("SELECT title FROM threads").fetchone()[0], "same"
        )

    def test_custom_history_location_cannot_silently_change_after_setup(self):
        prepare_history(self.store, self.account)
        with patch.dict(
            os.environ, {"CODEX_ACCOUNTS_HISTORY_HOME": str(self.root / "another")}
        ):
            with self.assertRaisesRegex(AccountError, "already configured"):
                prepare_history(self.store, self.account)

    def test_project_and_tool_references_survive_database_import(self):
        schema = """
            CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE project_roots(project_id TEXT REFERENCES projects(id), position INTEGER, path TEXT, PRIMARY KEY(project_id,position));
            CREATE TABLE threads(id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id));
            CREATE TABLE thread_dynamic_tools(thread_id TEXT REFERENCES threads(id), position INTEGER, name TEXT, PRIMARY KEY(thread_id,position));
        """
        self.database(self.shared, "state_5.sqlite", schema)
        self.database(
            self.account,
            "state_5.sqlite",
            schema,
            [
                ("INSERT INTO projects VALUES (?,?)", ("project", "IASPIS")),
                (
                    "INSERT INTO project_roots VALUES (?,?,?)",
                    ("project", 0, "/project"),
                ),
                ("INSERT INTO threads VALUES (?,?)", ("new", "project")),
                ("INSERT INTO thread_dynamic_tools VALUES (?,?,?)", ("new", 0, "tool")),
            ],
        )
        prepare_history(self.store, self.account)
        with sqlite3.connect(self.shared / "state_5.sqlite") as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                db.execute("SELECT name FROM projects").fetchone()[0], "IASPIS"
            )
            self.assertEqual(
                db.execute("SELECT path FROM project_roots").fetchone()[0], "/project"
            )
            self.assertEqual(
                db.execute("SELECT name FROM thread_dynamic_tools").fetchone()[0],
                "tool",
            )

    def test_interrupted_import_retries_without_duplicates_or_partial_thread(self):
        from codex_accounts.history import _insert_rows

        self.state_database(self.shared, "old")
        self.state_database(self.account, "new")
        self.database(
            self.account,
            "goals_1.sqlite",
            "CREATE TABLE goals(thread_id TEXT PRIMARY KEY, objective TEXT);",
            [("INSERT INTO goals VALUES (?,?)", ("new", "goal"))],
        )

        def fail_before_publication(db, table, rows):
            if table == "threads":
                raise AccountError("simulated interrupted import")
            return _insert_rows(db, table, rows)

        with patch(
            "codex_accounts.history._insert_rows", side_effect=fail_before_publication
        ):
            with self.assertRaisesRegex(AccountError, "simulated"):
                prepare_history(self.store, self.account)
        with sqlite3.connect(self.shared / "state_5.sqlite") as db:
            self.assertEqual(list(db.execute("SELECT id FROM threads")), [("old",)])
        prepare_history(self.store, self.account)
        with sqlite3.connect(self.shared / "goals_1.sqlite") as db:
            self.assertEqual(list(db.execute("SELECT * FROM goals")), [("new", "goal")])
        with sqlite3.connect(self.shared / "state_5.sqlite") as db:
            self.assertEqual(
                set(db.execute("SELECT id FROM threads")), {("old",), ("new",)}
            )

    def test_other_active_account_can_join_later_without_blocking_launches(self):
        lock_dir = self.account / "thread-writer-locks"
        lock_dir.mkdir()
        second = self.root / "second"
        second.mkdir()
        with file_lock(lock_dir / "active.lock"), patch("sys.stderr"):
            prepare_history(self.store, second)
            self.assertFalse((self.account / MARKER).exists())
            self.assertTrue((second / MARKER).exists())
        prepare_history(self.store, second)
        self.assertTrue((self.account / MARKER).exists())

    def test_existing_history_store_is_default_independent_of_selected_home(self):
        with (
            patch.dict(
                os.environ,
                {"CODEX_ACCOUNTS_HISTORY_HOME": "", "CODEX_HOME": str(self.account)},
            ),
            patch("pathlib.Path.home", return_value=self.root),
        ):
            self.assertEqual(
                prepare_history(self.store, self.account), self.root / ".codex"
            )


if __name__ == "__main__":
    unittest.main()
