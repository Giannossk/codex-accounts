"""Share local history while keeping each login in its original Codex home."""

import filecmp
import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import ExitStack, closing
from pathlib import Path

from .state import AccountError, Store, file_lock, private_directory

MARKER = ".codex-accounts-history"
DIRECTORIES = ("sessions", "archived_sessions", "thread-writer-locks")
FILES = ("history.jsonl", "session_index.jsonl")
DATABASE_PREFIXES = ("thread_history_", "goals_", "queue_", "memories_", "state_")


class HistoryBusy(AccountError):
    pass


def configured_history(account_home: Path) -> Path | None:
    try:
        value = json.loads((account_home / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as error:
        raise AccountError("Invalid shared history marker.") from error
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise AccountError("Invalid shared history location.")
    return Path(value)


def history_home(store: Store) -> Path:
    """Pin the existing default store; never derive history from the selected login."""
    path = store.root / "history.json"
    requested = os.environ.get("CODEX_ACCOUNTS_HISTORY_HOME")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        target = (
            Path(requested).expanduser() if requested else Path.home() / ".codex"
        ).resolve()
        private_directory(target)
        _write_json(path, {"version": 1, "home": str(target)})
        return target
    except (ValueError, UnicodeError) as error:
        raise AccountError(f"Invalid shared history settings: {path}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("home"), str)
        or not Path(value["home"]).is_absolute()
    ):
        raise AccountError(f"Invalid shared history settings: {path}")
    target = Path(value["home"])
    if requested and Path(requested).expanduser().resolve() != target:
        raise AccountError(f"Shared history is already configured at {target}.")
    if not target.is_dir():
        raise AccountError(f"The shared history directory is missing: {target}")
    return target


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            if os.name == "posix":
                temporary.chmod(0o600)
            json.dump(value, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def is_junction(path: Path) -> bool:
    if os.name != "nt":
        return False
    if hasattr(path, "is_junction"):
        try:
            return path.is_junction()
        except OSError:
            return False
    if hasattr(os.path, "isjunction"):
        try:
            return os.path.isjunction(str(path))
        except OSError:
            return False
    return False


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    return is_junction(path)


def create_link(source: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        source.symlink_to(target, target_is_directory=target_is_directory)
        return
    except OSError:
        if os.name != "nt":
            raise
    if target_is_directory or target.is_dir():
        import _winapi

        _winapi.CreateJunction(str(target.resolve()), str(source))
    else:
        try:
            os.link(str(target.resolve()), str(source))
        except OSError:
            shutil.copy2(target, source)


def _linked(source: Path, target: Path) -> bool:
    if not source.exists() and not is_link(source):
        return False
    if not target.exists():
        return False
    try:
        if is_link(source):
            return source.resolve() == target.resolve()
        if os.name == "nt" and source.is_file() and target.is_file():
            return source.samefile(target)
        return source.resolve() == target.resolve()
    except OSError:
        return False


def _idle(account_home: Path, stack: ExitStack) -> None:
    directory = account_home / "thread-writer-locks"
    private_directory(directory)
    try:
        stack.enter_context(file_lock(directory / ".coordination.lock", blocking=False))
        for path in sorted(directory.glob("*.lock")):
            if path.name != ".coordination.lock":
                stack.enter_context(file_lock(path, blocking=False))
    except AccountError as error:
        raise HistoryBusy(
            f"Close running Codex sessions in {account_home} before joining shared history."
        ) from error


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _read_database(path: Path) -> sqlite3.Connection:
    # mode=ro includes committed WAL records. Never copy live database files with cp.
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)


def _tables(db: sqlite3.Connection) -> dict[str, str]:
    return dict(
        db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )


def _schema(db: sqlite3.Connection, table: str) -> list:
    return list(db.execute(f"PRAGMA table_info({_quote(table)})"))


def _create_database(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    for statement in _tables(source).values():
        target.execute(statement)
    for (statement,) in source.execute(
        "SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL"
    ):
        target.execute(statement)
    if "_sqlx_migrations" in _tables(source):
        _insert_rows(
            target,
            "_sqlx_migrations",
            list(source.execute('SELECT * FROM "_sqlx_migrations"')),
        )


def _insert_rows(db: sqlite3.Connection, table: str, rows: list) -> None:
    if not rows:
        return
    schema = _schema(db, table)
    keys = [column[1] for column in sorted(schema, key=lambda col: col[5]) if column[5]]
    if not keys:
        raise AccountError(f"Cannot safely import history table without a key: {table}")
    placeholders = ",".join("?" for _ in schema)
    # Ignore only identical primary-key duplicates left by an interrupted import.
    for row in rows:
        key_values = [
            row[next(i for i, col in enumerate(schema) if col[1] == key)]
            for key in keys
        ]
        existing = db.execute(
            f"SELECT * FROM {_quote(table)} WHERE "
            + " AND ".join(f"{_quote(key)}=?" for key in keys),
            key_values,
        ).fetchone()
        if existing is not None:
            if existing != row:
                raise AccountError(
                    f"Conflicting history records in {table}; originals retained."
                )
            continue
        db.execute(f"INSERT INTO {_quote(table)} VALUES ({placeholders})", row)


def _import_databases(account_home: Path, target_home: Path, backup: Path) -> None:
    # Import only threads absent from the shared store. Its existing threads win.
    states = sorted(account_home.glob("state_*.sqlite"))
    if len(states) > 1:
        raise AccountError(
            f"Multiple state database versions in {account_home}; migration needs review."
        )
    if not states:
        return
    state_path = states[0]
    with closing(_read_database(state_path)) as source:
        if "threads" not in _tables(source):
            return
        new_ids = {row[0] for row in source.execute("SELECT id FROM threads")}
    target_state = target_home / state_path.name
    if target_state.exists():
        with closing(_read_database(target_state)) as target:
            new_ids.difference_update(
                row[0] for row in target.execute("SELECT id FROM threads")
            )
    if not new_ids:
        return

    # Publish the threads table last. Retrying an interrupted import checks all
    # already-copied rows before making those threads visible in the resume picker.
    for prefix in DATABASE_PREFIXES:
        for source_path in sorted(account_home.glob(prefix + "*.sqlite")):
            destination = target_home / source_path.name
            with closing(_read_database(source_path)) as source:
                tables = _tables(source)
                selected = {}
                for table in tables:
                    columns = _schema(source, table)
                    names = [column[1] for column in columns]
                    links = [
                        name
                        for name in names
                        if name == "thread_id" or name.endswith("_thread_id")
                    ]
                    if table == "threads":
                        links = ["id"]
                    if not links:
                        continue
                    positions = [names.index(name) for name in links]
                    rows = [
                        row
                        for row in source.execute(f"SELECT * FROM {_quote(table)}")
                        if any(row[i] in new_ids for i in positions)
                    ]
                    if rows:
                        selected[table] = rows
                if not selected:
                    continue
                private_directory(backup)
                if destination.exists():
                    with (
                        closing(_read_database(destination)) as current,
                        closing(sqlite3.connect(backup / destination.name)) as saved,
                    ):
                        current.backup(saved)
                with (
                    closing(sqlite3.connect(destination, timeout=10)) as target,
                    target,
                ):
                    target.execute("PRAGMA foreign_keys=ON")
                    target.execute("BEGIN IMMEDIATE")
                    target.execute("PRAGMA defer_foreign_keys=ON")
                    if not _tables(target):
                        _create_database(source, target)
                    if "threads" in selected and "projects" in tables:
                        names = [column[1] for column in _schema(source, "threads")]
                        if "project_id" in names:
                            project_ids = {
                                row[names.index("project_id")]
                                for row in selected["threads"]
                            } - {None}
                            if "projects" in _tables(target):
                                project_ids.difference_update(
                                    row[0]
                                    for row in target.execute("SELECT id FROM projects")
                                )
                            for table in (
                                "projects",
                                "project_roots",
                                "project_idempotency_keys",
                            ):
                                if table not in tables:
                                    continue
                                names = [column[1] for column in _schema(source, table)]
                                position = names.index(
                                    "id" if table == "projects" else "project_id"
                                )
                                rows = [
                                    row
                                    for row in source.execute(
                                        f"SELECT * FROM {_quote(table)}"
                                    )
                                    if row[position] in project_ids
                                ]
                                if rows:
                                    selected[table] = rows
                    for table in selected:
                        if _schema(source, table) != _schema(target, table):
                            raise AccountError(
                                f"History database formats differ for {table}; originals retained. Update both stores with the same Codex CLI first."
                            )
                    for table, rows in selected.items():
                        _insert_rows(target, table, rows)
                if os.name == "posix":
                    destination.chmod(0o600)


def _copy_tree(source: Path, target: Path) -> None:
    private_directory(target)
    for entry in source.iterdir():
        destination = target / entry.name
        if entry.is_symlink():
            raise AccountError(f"Unexpected link in account history: {entry}")
        if entry.is_dir():
            _copy_tree(entry, destination)
        elif destination.exists():
            if not filecmp.cmp(entry, destination, shallow=False):
                raise AccountError(
                    f"History files conflict: {destination}; originals retained."
                )
        else:
            shutil.copy2(entry, destination)


def _merge_lines(source: Path, target: Path) -> None:
    # Codex uses file locking for prompt-history appends as well.
    # msvcrt needs a byte to lock; use a valid blank JSONL line instead of the
    # NUL byte the registry-lock helper uses for an empty lock file.
    if os.name == "nt" and target.stat().st_size == 0:
        with target.open("ab") as handle:
            handle.write(b"\n")
    with file_lock(target):
        existing = target.read_bytes()
        seen = set(existing.splitlines())
        with target.open("ab") as handle:
            if existing and not existing.endswith(b"\n"):
                handle.write(b"\n")
            for line in source.read_bytes().splitlines():
                if line and line not in seen:
                    handle.write(line + b"\n")
                    seen.add(line)
            handle.flush()
            os.fsync(handle.fileno())


def remove_link(path: Path) -> None:
    if is_junction(path):
        try:
            path.unlink()
        except OSError:
            os.rmdir(path)
    else:
        path.unlink(missing_ok=True)


def _join(account_home: Path, target: Path, store: Store) -> None:
    if account_home.resolve() == target.resolve():
        return
    names = DIRECTORIES + FILES
    if configured_history(account_home) == target and all(
        _linked(account_home / name, target / name) for name in names
    ):
        return
    # Probe to ensure linking works before moving originals.
    probe = account_home / f".history-link-test-{uuid.uuid4().hex}"
    try:
        create_link(probe, target, target_is_directory=True)
    except OSError as error:
        raise AccountError(
            "Shared history requires symbolic links or NTFS junctions."
        ) from error
    finally:
        remove_link(probe)

    with ExitStack() as stack:
        # If only a legacy index link was replaced by Codex, directory/lock
        # sharing is already complete and no database migration is necessary.
        first_join = configured_history(account_home) is None
        _idle(account_home, stack)
        backup = account_home / ".history-backups" / uuid.uuid4().hex
        for name in names:
            source, destination = account_home / name, target / name
            if _linked(source, destination):
                continue
            if is_link(source):
                raise AccountError(f"History already links elsewhere: {source}")
            if name in DIRECTORIES:
                private_directory(destination)
                if source.exists() and name != "thread-writer-locks":
                    _copy_tree(source, destination)
            else:
                if not destination.exists():
                    destination.touch(mode=0o600)
                if source.exists():
                    _merge_lines(source, destination)
        if first_join:
            _import_databases(
                account_home, target, store.root / "history-backups" / uuid.uuid4().hex
            )
        stack.close()
        for name in names:
            source, destination = account_home / name, target / name
            if _linked(source, destination):
                continue
            if source.exists():
                private_directory(backup)
                source.rename(backup / name)
            create_link(source, destination, target_is_directory=name in DIRECTORIES)
        _write_json(account_home / MARKER, str(target))


def prepare_history(store: Store, account_home: Path | None = None) -> Path:
    private_directory(store.root)
    with file_lock(store.root / "history.lock"):
        target = history_home(store)
        candidates = {Path(row["home"]) for row in store.read()["accounts"].values()}
        if account_home is not None:
            candidates.add(account_home)
        for candidate in sorted(candidates):
            if not candidate.is_dir():
                continue
            try:
                _join(candidate, target, store)
            except HistoryBusy:
                if candidate == account_home:
                    raise
                print(
                    f"History migration deferred until sessions in {candidate} close.",
                    file=sys.stderr,
                )
            except sqlite3.Error as error:
                raise AccountError(
                    f"Could not merge history from {candidate}; originals and backups retained: {error}"
                ) from error
        return target
