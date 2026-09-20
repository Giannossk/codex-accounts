"""Share Codex's persistent files without moving any login's credential home."""

import filecmp
import json
import os
import shutil
import sys
import tomllib
import uuid
from datetime import date, datetime, time
from pathlib import Path

from .history import (
    DIRECTORIES as HISTORY_DIRECTORIES,
    FILES as HISTORY_FILES,
    _linked,
    _write_json,
    create_link,
    is_link,
    prepare_history,
    remove_link,
)
from .state import AccountError, Store, file_lock, private_directory

MARKER = ".codex-accounts-shared"
# Prelink extensible directories even before the first skill/rule/etc. is added.
DIRECTORIES = (
    "skills",
    "plugins",
    "rules",
    "prompts",
    "agents",
    "memories",
    "automations",
    "hooks",
    "cache",
    "log",
    "shell_snapshots",
)


def shareable(name: str) -> bool:
    """Credentials, recovery copies, and process-local files stay in place."""
    plain = name.lstrip(".").casefold()
    if any(
        plain == word or plain.startswith((word + ".", word + "-", word + "_"))
        for word in ("auth", "credentials", "secrets", "tokens", "keyring")
    ):
        return False
    return not (
        name in HISTORY_DIRECTORIES + HISTORY_FILES
        or plain in ("tmp", "sandbox", "sandbox-secrets", "env")
        or plain.startswith(("codex-accounts-", "history-", "shared-", "backup"))
        or ".backup" in plain
        or ".sqlite" in plain
        or plain.endswith((".lock", ".sock", ".pid"))
    )


def _manifest(home: Path, target: Path) -> set[str]:
    try:
        value = json.loads((home / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (ValueError, UnicodeError) as error:
        raise AccountError(f"Invalid shared home marker: {home / MARKER}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("home") != str(target)
        or not isinstance(value.get("names"), list)
        or not all(isinstance(name, str) for name in value["names"])
    ):
        raise AccountError(f"Invalid shared home marker: {home / MARKER}")
    return set(value["names"])


def _merge_missing(target: dict, source: dict) -> bool:
    changed = False
    for key, value in source.items():
        if key not in target:
            target[key] = value
            changed = True
        elif isinstance(target[key], dict) and isinstance(value, dict):
            changed = _merge_missing(target[key], value) or changed
        elif isinstance(target[key], list) and isinstance(value, list):
            for item in value:
                if item in target[key]:
                    continue
                match = None
                if isinstance(item, dict):
                    identity = next(
                        (name for name in ("path", "name", "id") if name in item), None
                    )
                    if identity:
                        match = next(
                            (
                                entry
                                for entry in target[key]
                                if isinstance(entry, dict)
                                and entry.get(identity) == item[identity]
                            ),
                            None,
                        )
                if match is not None:
                    changed = _merge_missing(match, item) or changed
                else:
                    target[key].append(item)
                    changed = True
    return changed


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{_toml_value(key)} = {_toml_value(item)}"
                for key, item in value.items()
            )
            + " }"
        )
    raise AccountError("Unsupported value in Codex configuration; originals retained.")


def _toml_document(value: dict, prefix: tuple[str, ...] = ()) -> str:
    lines = []
    if prefix:
        lines.append("[" + ".".join(_toml_value(key) for key in prefix) + "]")
    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(f"{_toml_value(key)} = {_toml_value(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append("\n" + _toml_document(item, (*prefix, key)))
    return "\n".join(lines) + "\n"


def _save_original(source: Path, backup: Path) -> None:
    if backup.exists() or backup.is_symlink():
        return
    private_directory(backup.parent)
    shutil.copy2(source, backup)


def _replace_file(source: Path, target: Path, backup: Path) -> None:
    # Resolve user-owned links too: atomic writes must not detach their links.
    destination = target.resolve()
    _save_original(destination, backup)
    temporary = destination.with_name(f".shared-{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_config(source: Path, target: Path, backup: Path) -> None:
    try:
        source_value = tomllib.loads(source.read_text(encoding="utf-8"))
        target_value = tomllib.loads(target.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise AccountError(
            f"Invalid configuration at {source} or {target}; originals retained."
        ) from error
    if not _merge_missing(target_value, source_value):
        return
    contents = _toml_document(target_value)
    # Validate the complete serialization before replacing any user file.
    tomllib.loads(contents)
    destination = target.resolve()
    _save_original(destination, backup)
    temporary = destination.with_name(f".shared-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            if os.name == "posix":
                temporary.chmod(0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _merge(
    source: Path,
    target: Path,
    backup: Path,
    *,
    repair: bool = False,
    ancestors: frozenset[Path] = frozenset(),
) -> None:
    if not source.exists() and not is_link(source):
        return
    if is_link(target) and target.resolve().is_relative_to(source.absolute()):
        raise AccountError(
            f"Shared data links back into an account home: {target}; originals retained."
        )
    if source.resolve() == target.resolve():
        return
    if source.is_dir():
        resolved = source.resolve()
        if resolved in ancestors or target.resolve().is_relative_to(resolved):
            raise AccountError(
                f"Recursive directory link in {source}; originals retained."
            )
        ancestors = ancestors | {resolved}
    if not target.exists() and not is_link(target):
        if is_link(source):
            # Absolute links keep externally installed skills working after relocation.
            create_link(target, source.resolve(), target_is_directory=source.is_dir())
        elif source.is_dir():
            private_directory(target)
            for child in sorted(source.iterdir()):
                _merge(
                    child, target / child.name, backup / child.name, ancestors=ancestors
                )
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            raise AccountError(
                f"Cannot share special file: {source}; originals retained."
            )
        return
    if source.is_dir() and target.is_dir():
        for child in sorted(source.iterdir()):
            _merge(
                child,
                target / child.name,
                backup / child.name,
                repair=repair,
                ancestors=ancestors,
            )
    elif source.is_file() and target.is_file():
        if filecmp.cmp(source, target, shallow=False):
            return
        if repair and source.stat().st_mtime_ns > target.stat().st_mtime_ns:
            _replace_file(source, target, backup)
        elif source.name == "config.toml" or source.name.endswith(".config.toml"):
            _merge_config(source, target, backup)
    # Conflicts keep the existing shared version; the entire source is backed up.


def prepare_home(store: Store, account_home: Path | None = None) -> Path:
    """Migrate all registered homes, then link all noncredential persistent files."""
    target = prepare_history(store, account_home)
    with file_lock(store.root / "shared.lock"):
        candidates = {
            Path(row["home"]).resolve() for row in store.read()["accounts"].values()
        }
        if account_home is not None:
            candidates.add(account_home.resolve())
        candidates = {home for home in candidates if home.is_dir() and home != target}
        for home in candidates:
            if home.is_relative_to(target) or target.is_relative_to(home):
                raise AccountError(
                    "Shared and account homes cannot contain one another."
                )
        names = set(DIRECTORIES) | {"config.toml"}
        for home in [target, *sorted(candidates)]:
            names.update(path.name for path in home.iterdir() if shareable(path.name))
        # Check links are supported in every home before migrating settings.
        manifests = {}
        for home in sorted(candidates):
            manifests[home] = _manifest(home, target)
            probe = home / f".shared-link-test-{uuid.uuid4().hex}"
            try:
                create_link(probe, target, target_is_directory=True)
            except OSError as error:
                raise AccountError(
                    "Shared Codex data requires symbolic links or NTFS junctions."
                ) from error
            finally:
                remove_link(probe)
        for name in DIRECTORIES:
            private_directory(target / name)
        if not (target / "config.toml").exists():
            (target / "config.toml").touch(mode=0o600)
        # Import all homes before linking, so new files reach every account at once.
        migrations = []
        for home in sorted(candidates):
            pending = [
                name
                for name in sorted(names)
                if not _linked(home / name, target / name)
            ]
            if not pending and manifests[home] == names:
                continue
            backup = home / ".shared-backups" / uuid.uuid4().hex
            for name in pending:
                _merge(
                    home / name,
                    target / name,
                    backup / "shared" / name,
                    repair=name in manifests[home],
                )
            migrations.append((home, pending, backup))
        for home, pending, backup in migrations:
            for name in pending:
                source, destination = home / name, target / name
                if source.exists() or is_link(source):
                    private_directory(backup / "account")
                    source.rename(backup / "account" / name)
                try:
                    create_link(
                        source, destination, target_is_directory=destination.is_dir()
                    )
                except OSError:
                    original = backup / "account" / name
                    if original.exists() or is_link(original):
                        original.rename(source)
                    raise
            _write_json(
                home / MARKER,
                {"version": 1, "home": str(target), "names": sorted(names)},
            )
            if backup.exists():
                print(
                    f"Shared Codex data in {target}. Originals retained at {backup}.",
                    file=sys.stderr,
                )
        return target
