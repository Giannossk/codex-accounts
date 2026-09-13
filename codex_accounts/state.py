"""Private registry of email identities and stable Codex homes."""

import json
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


class AccountError(Exception):
    """An actionable error safe to display without authentication payloads."""


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def default_root() -> Path:
    if custom := os.environ.get("CODEX_ACCOUNTS_HOME"):
        return Path(custom).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        if not base.is_absolute():
            base = Path.home() / ".local/share"
    return (base / "codex-accounts").resolve()


def safe_text(value: object, *, limit: int = 320) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()


def valid_email(value: object) -> bool:
    return (
        safe_text(value)
        and value.count("@") == 1
        and all(value.split("@"))
        and not any(c.isspace() for c in value)
    )


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise AccountError(
                    "Another account operation is in progress."
                ) from error
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise AccountError(
                    "Another account operation is in progress."
                ) from error
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "accounts.json"

    def read(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 2, "selected": None, "accounts": {}}
        except (ValueError, UnicodeError) as error:
            raise AccountError(f"Invalid account registry: {self.path}") from error
        if (
            not isinstance(state, dict)
            or type(state.get("version")) is not int
            or state["version"] not in (1, 2)
        ):
            raise AccountError(f"Unsupported account registry: {self.path}")
        accounts = state.get("accounts")
        if not isinstance(accounts, dict):
            raise AccountError(f"Invalid account registry: {self.path}")
        selected = state.get("selected")
        if selected is not None and (
            not isinstance(selected, str) or selected not in accounts
        ):
            raise AccountError("The selected account is missing from the registry.")
        if state["version"] == 1:
            # Preserve locations and selection, discarding the old labels.
            migrated, ids = {}, {}
            for old_name, home in accounts.items():
                if not isinstance(home, str) or not Path(home).is_absolute():
                    raise AccountError("Invalid home in the old account registry.")
                account_id = uuid.uuid5(uuid.NAMESPACE_URL, home).hex
                ids[old_name] = account_id
                migrated[account_id] = {
                    "home": home,
                    "email": None,
                    "plan": None,
                    "kind": None,
                }
            state = {"version": 2, "selected": ids.get(selected), "accounts": migrated}
        for account_id, record in state["accounts"].items():
            if not re.fullmatch(r"[a-z0-9_-]{1,64}", account_id) or not isinstance(
                record, dict
            ):
                raise AccountError("Invalid account record.")
            home = record.get("home")
            if not isinstance(home, str) or not Path(home).is_absolute():
                raise AccountError("Invalid saved account home.")
            email = record.get("email")
            if email is not None and not valid_email(email):
                raise AccountError("Invalid saved account email.")
            for field in ("plan", "kind"):
                if record.get(field) is not None and not safe_text(
                    record[field], limit=64
                ):
                    raise AccountError(f"Invalid account {field}.")
        return state

    @contextmanager
    def edit(self):
        private_directory(self.root)
        with file_lock(self.root / "registry.lock"):
            state = self.read()
            yield state
            if (
                self.path.exists()
                and json.loads(self.path.read_text(encoding="utf-8")).get("version")
                == 1
            ):
                backup = self.root / "accounts.v1.backup.json"
                try:
                    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(self.path.read_bytes())
            fd, filename = tempfile.mkstemp(prefix=".accounts-", dir=self.root)
            temporary = Path(filename)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(state, handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def rows(self, state: dict | None = None) -> list[tuple[str, dict]]:
        state = self.read() if state is None else state
        return sorted(
            state["accounts"].items(),
            key=lambda item: ((item[1].get("email") or "~").casefold(), item[0]),
        )

    def resolve(self, selector: str, state: dict | None = None) -> str:
        state = self.read() if state is None else state
        if selector in state["accounts"]:
            return selector
        matches = [
            key
            for key, record in state["accounts"].items()
            if (record.get("email") or "").casefold() == selector.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AccountError(
                "That email has multiple saved logins. Choose one with codex select account."
            )
        if (
            selector.isascii()
            and selector.isdigit()
            and 0 < int(selector) <= len(state["accounts"])
        ):
            return self.rows(state)[int(selector) - 1][0]
        raise AccountError("Account not found. Run codex list accounts.")

    def home(self, account_id: str, state: dict | None = None) -> Path:
        state = self.read() if state is None else state
        record = state["accounts"].get(account_id)
        if record is None:
            raise AccountError(
                "That account was removed. Run codex select account again."
            )
        home = Path(record["home"])
        if not home.is_dir():
            raise AccountError("The selected account's saved home is missing.")
        return home
