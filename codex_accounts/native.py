"""Run Codex and read its public account metadata without handling credentials."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .history import configured_history, prepare_history
from .state import AccountError, Store, default_root, safe_text, valid_email


def codex_binary() -> str:
    candidate = os.environ.get("CODEX_ACCOUNTS_CODEX_BIN") or "codex"
    binary = shutil.which(candidate)
    if binary is None:
        raise AccountError(
            "Install Codex CLI or set CODEX_ACCOUNTS_CODEX_BIN to its executable."
        )
    resolved = Path(binary).resolve()
    wrappers = {
        Path(sys.argv[0]).resolve(),
        (Path(sys.executable).parent / "codex-accounts").resolve(),
        (Path(sys.executable).parent / "codex-accounts.exe").resolve(),
    }
    if wrapper := shutil.which("codex-accounts"):
        wrappers.add(Path(wrapper).resolve())
    if resolved in wrappers:
        raise AccountError(
            "The Codex executable points back to this wrapper. Set CODEX_ACCOUNTS_CODEX_BIN to the official CLI."
        )
    return str(resolved)


def account_environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    if shared_history := configured_history(home):
        env["CODEX_SQLITE_HOME"] = str(shared_history)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(name, None)
    return env


async def read_identity(binary: str, home: Path, *, timeout: float = 12) -> dict:
    """Read account/read via stdio, with a deadline and no thread/model requests."""
    process = await asyncio.create_subprocess_exec(
        binary,
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=account_environment(home),
        start_new_session=os.name == "posix",
        limit=1024 * 1024,
    )

    async def send(message: dict) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def response(request_id: int) -> dict:
        while line := await process.stdout.readline():
            message = json.loads(line)
            if not isinstance(message, dict):
                raise AccountError("Codex returned invalid account metadata.")
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise AccountError("Codex could not read the account metadata.")
                return message["result"]
            if "method" in message and "id" in message:
                await send(
                    {
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Unsupported request"},
                    }
                )
        raise AccountError("Codex exited before returning account metadata.")

    async def exchange() -> dict:
        await send(
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex_accounts",
                        "title": "Codex Accounts",
                        "version": "0.2.0",
                    }
                },
            }
        )
        await response(0)
        await send({"method": "initialized", "params": {}})
        await send(
            {"id": 1, "method": "account/read", "params": {"refreshToken": False}}
        )
        result = await response(1)
        if "account" not in result:
            raise AccountError("Codex returned invalid account metadata.")
        account = result["account"]
        if account is None:
            return {"email": None, "plan": None, "kind": "signedOut"}
        if not isinstance(account, dict) or not safe_text(
            account.get("type"), limit=64
        ):
            raise AccountError("Codex returned an unsupported account identity.")
        email, plan = account.get("email"), account.get("planType")
        if email is not None and not valid_email(email):
            raise AccountError("Codex did not return a valid email address.")
        if plan is not None and not safe_text(plan, limit=64):
            raise AccountError("Codex returned invalid account plan metadata.")
        return {"email": email, "plan": plan, "kind": account["type"]}

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except (TimeoutError, ValueError, BrokenPipeError, ConnectionError) as error:
        raise AccountError(
            "Could not read the account email. Try codex list accounts --refresh."
        ) from error
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                await process.wait()


def launch(home: Path, arguments: list[str]) -> int:
    binary = codex_binary()
    shared_history = prepare_history(Store(default_root()), home)
    env = account_environment(home)
    env["CODEX_SQLITE_HOME"] = str(shared_history)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "posix":
        os.execve(binary, [binary, *arguments], env)
    return subprocess.call([binary, *arguments], env=env)
