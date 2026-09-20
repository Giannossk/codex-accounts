"""Email discovery, account registration, and manual selection."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .native import account_environment, codex_binary, read_identity, read_rate_limits
from .picker import pick
from .shared import prepare_home
from .state import AccountError, Store, file_lock, private_directory


def describe(record: dict) -> str:
    email = record.get("email") or "Email unavailable"
    detail = record.get("plan") or record.get("kind") or "metadata pending"
    if record.get("kind") == "signedOut":
        detail = "sign-in required"
    return f"{email}  ({detail})"


def describe_quota(result: dict | None) -> str:
    remaining = {300: None, 10080: None}
    if isinstance(result, dict):
        limits = result.get("rateLimits")
        by_id = result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and "codex" in by_id:
            limits = by_id["codex"]
        if isinstance(limits, dict) and limits.get("limitId") in (None, "codex"):
            for name in ("primary", "secondary"):
                window = limits.get(name)
                if not isinstance(window, dict):
                    continue
                duration = window.get("windowDurationMins")
                used = window.get("usedPercent")
                if (
                    type(duration) is int
                    and duration in remaining
                    and type(used) is int
                ):
                    remaining[duration] = max(0, min(100, 100 - used))
    return " | ".join(
        f"{label}: {remaining[duration]}% left"
        if remaining[duration] is not None
        else f"{label}: unavailable"
        for label, duration in (("5h", 300), ("weekly", 10080))
    )


def quota_labels(accounts: dict[str, dict]) -> dict[str, str]:
    """Refresh picker quotas in parallel; unavailable accounts stay selectable."""
    try:
        binary = codex_binary()
    except AccountError:
        return {key: describe_quota(None) for key in accounts}

    async def collect():
        semaphore = asyncio.Semaphore(4)

        async def read(key, record):
            result = None
            home = Path(record["home"])
            if record.get("kind") in (None, "chatgpt") and home.is_dir():
                async with semaphore:
                    try:
                        result = await read_rate_limits(binary, home)
                    except (AccountError, OSError):
                        pass
            return key, describe_quota(result)

        return dict(await asyncio.gather(*(read(k, r) for k, r in accounts.items())))

    return asyncio.run(collect())


def refresh(store: Store, *, force: bool = False, only: str | None = None) -> dict:
    state = store.read()
    pending = {
        key: record
        for key, record in state["accounts"].items()
        if (only is None or key == only) and (force or record.get("kind") is None)
    }
    if not pending:
        return state
    binary = codex_binary()

    async def collect():
        semaphore = asyncio.Semaphore(4)

        async def read(key, record):
            async with semaphore:
                try:
                    home = Path(record["home"])
                    if not home.is_dir():
                        raise AccountError("Saved home is missing.")
                    return key, await read_identity(binary, home)
                except (AccountError, OSError):
                    return key, None

        return await asyncio.gather(
            *(read(key, record) for key, record in pending.items())
        )

    results = asyncio.run(collect())
    failed = 0
    with store.edit() as current:
        for key, identity in results:
            if (
                key not in current["accounts"]
                or current["accounts"][key]["home"] != pending[key]["home"]
            ):
                continue
            if identity is None:
                failed += 1
                continue
            if identity["kind"] == "signedOut":
                identity["email"] = current["accounts"][key].get("email")
            current["accounts"][key].update(identity)
    if failed:
        print(
            f"Could not refresh {failed} saved account(s). Try codex list accounts --refresh.",
            file=sys.stderr,
        )
    return store.read()


def add_account(
    store: Store, *, device_auth: bool = False, existing_home: str | None = None
) -> int:
    if device_auth and existing_home:
        raise AccountError("--home uses an existing login; omit --device-auth.")
    binary = codex_binary()
    private_directory(store.root)
    with file_lock(store.root / "signin.lock", blocking=False):
        state = store.read()
        account_id = uuid.uuid4().hex
        if existing_home:
            home = Path(existing_home).expanduser().resolve()
            if not home.is_dir():
                raise AccountError("The specified Codex home does not exist.")
            if any(
                Path(record["home"]).resolve() == home
                for record in state["accounts"].values()
            ):
                raise AccountError("That saved login is already in the account list.")
        else:
            homes = store.root / "homes"
            if homes.is_symlink():
                raise AccountError(
                    "The managed homes directory cannot be a symbolic link."
                )
            private_directory(homes)
            home = homes / account_id
            home.mkdir(mode=0o700)
            prepare_home(store, home)
            print("Sign into the account you want to add.", flush=True)
            arguments = [binary, "login"]
            if device_auth:
                arguments.append("--device-auth")
            cmd = arguments
            if os.name == "nt" and cmd[0].lower().endswith((".cmd", ".bat")):
                cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
            result = subprocess.run(
                cmd, env=account_environment(home), check=False
            )
            if result.returncode:
                print(
                    "Sign-in did not complete. Your account selection is unchanged.",
                    file=sys.stderr,
                )
                return (
                    result.returncode
                    if result.returncode > 0
                    else 128 - result.returncode
                )

        metadata_failed = False
        try:
            identity = asyncio.run(read_identity(binary, home))
        except (AccountError, OSError):
            if existing_home:
                raise AccountError(
                    "Could not verify the email for that saved login. Try again."
                ) from None
            # A completed browser login must remain selectable if metadata lookup fails.
            identity = {"email": None, "plan": None, "kind": None}
            metadata_failed = True
        if existing_home and identity["kind"] == "signedOut":
            raise AccountError("There is no saved login in that Codex home.")

        with store.edit() as current:
            if any(
                Path(record["home"]).resolve() == home
                for record in current["accounts"].values()
            ):
                raise AccountError(
                    "That saved login was already added by another terminal."
                )
            current["accounts"][account_id] = {"home": str(home), **identity}
        if existing_home:
            prepare_home(store, home)
        if identity["email"]:
            print(f"Added {identity['email']}.")
        else:
            print("Login saved. Its email is not available yet.")
        if metadata_failed:
            print(
                "Run codex list accounts --refresh to retry email discovery.",
                file=sys.stderr,
            )
        print(
            "Run codex add account to add another, or codex select account to choose one."
        )
        return 0


def list_accounts(store: Store, *, as_json: bool = False, force: bool = False) -> None:
    state = refresh(store, force=force)
    rows = store.rows(state)
    if as_json:
        print(
            json.dumps(
                {
                    "selected": state["selected"],
                    "accounts": [
                        {"id": key, "selected": key == state["selected"], **record}
                        for key, record in rows
                    ],
                },
                indent=2,
            )
        )
        return
    if not rows:
        print("No saved accounts. Run codex add account.")
        return
    print("Saved accounts\n")
    for number, (key, record) in enumerate(rows, 1):
        marker = "*" if key == state["selected"] else " "
        print(f" {marker} {number}. {describe(record)}")
    print(
        "\n* Selected for new Codex sessions"
        if state["selected"]
        else "\nChoose one with codex select account."
    )


def choose(
    store: Store, selector: str | None = None, *, remove: bool = False
) -> str | None:
    state = refresh(store)
    if not state["accounts"]:
        raise AccountError("No saved accounts. Run codex add account.")
    if selector is not None:
        return store.resolve(selector, state)
    quotas = {}
    if not remove:
        print("Checking account quotas...", flush=True)
        quotas = quota_labels(state["accounts"])
    title = "Choose an account to remove:" if remove else "Choose the account to use:"
    print(title)
    return pick(
        [
            (key, describe(record) + (f"  {quotas[key]}" if key in quotas else ""))
            for key, record in store.rows(state)
        ],
        selected=state["selected"],
    )


def select_account(store: Store, selector: str | None = None) -> str | None:
    key = choose(store, selector)
    if key is None:
        return None
    prepare_home(store, store.home(key))
    with store.edit() as state:
        store.home(key, state)
        state["selected"] = key
        record = state["accounts"][key]
    print(f"Selected {record.get('email') or 'the saved login'}.", flush=True)
    return key


def remove_account(store: Store, selector: str | None = None) -> int:
    key = choose(store, selector, remove=True)
    if key is None:
        return 130
    with store.edit() as state:
        record = state["accounts"].pop(key, None)
        if record is None:
            raise AccountError("That account was already removed.")
        if state["selected"] == key:
            state["selected"] = None
    print(
        f"Removed {record.get('email') or 'the account'} from the list. Its saved home is retained."
    )
    return 0
