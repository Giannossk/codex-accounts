"""Run Codex and read its public account metadata without handling credentials."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .history import configured_history
from .shared import prepare_home
from .state import AccountError, Store, default_root, safe_text, valid_email


def _is_wrapper(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False

    wrappers = set()
    if sys.argv and sys.argv[0]:
        try:
            wrappers.add(Path(sys.argv[0]).resolve())
        except OSError:
            pass

    py_dir = Path(sys.executable).parent.resolve()
    for sub in ("", "Scripts", "bin"):
        folder = (py_dir / sub).resolve()
        for stem in ("codex", "codex-accounts"):
            wrappers.add((folder / stem).resolve())
            wrappers.add((folder / f"{stem}.exe").resolve())
            wrappers.add((folder / f"{stem}.cmd").resolve())
            wrappers.add((folder / f"{stem}.bat").resolve())

    if wrapper := shutil.which("codex-accounts"):
        try:
            wrappers.add(Path(wrapper).resolve())
        except OSError:
            pass

    if resolved in wrappers:
        return True
    if resolved.parent in (py_dir, (py_dir / "Scripts").resolve(), (py_dir / "bin").resolve()):
        if resolved.stem in ("codex", "codex-accounts"):
            return True

    return False


def codex_binary() -> str:
    if custom := os.environ.get("CODEX_ACCOUNTS_CODEX_BIN"):
        binary = shutil.which(custom)
        if binary is None:
            raise AccountError(
                f"CODEX_ACCOUNTS_CODEX_BIN executable not found: {custom}"
            )
        resolved = Path(binary).resolve()
        if _is_wrapper(resolved):
            raise AccountError(
                "The Codex executable points back to this wrapper. Set CODEX_ACCOUNTS_CODEX_BIN to the official CLI."
            )
        return str(resolved)

    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    extensions = [""]
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        extensions = [ext.lower() for ext in pathext if ext]
        if ".exe" not in extensions:
            extensions.insert(0, ".exe")

    for directory in path_dirs:
        if not directory:
            continue
        dir_path = Path(directory)
        for ext in extensions:
            candidate = dir_path / f"codex{ext}"
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    if not _is_wrapper(candidate):
                        return str(candidate.resolve())
            except OSError:
                continue

    standard_paths = []
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        app_data = os.environ.get("APPDATA")
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        user_profile = os.environ.get("USERPROFILE")

        if local_app_data:
            standard_paths.append(Path(local_app_data) / "Programs/OpenAI/Codex/bin/codex.exe")
            standard_paths.append(Path(local_app_data) / "npm/codex.cmd")
        if program_files:
            standard_paths.append(Path(program_files) / "OpenAI/Codex/bin/codex.exe")
        if program_files_x86:
            standard_paths.append(Path(program_files_x86) / "OpenAI/Codex/bin/codex.exe")
        if app_data:
            standard_paths.append(Path(app_data) / "npm/codex.cmd")
            standard_paths.append(Path(app_data) / "npm/codex")
        if user_profile:
            standard_paths.append(Path(user_profile) / "AppData/Local/Programs/OpenAI/Codex/bin/codex.exe")
    else:
        standard_paths.extend([
            Path("/usr/local/bin/codex"),
            Path("/opt/homebrew/bin/codex"),
            Path.home() / ".local/bin/codex",
            Path.home() / ".npm-global/bin/codex",
        ])

    for candidate in standard_paths:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                if not _is_wrapper(candidate):
                    return str(candidate.resolve())
        except OSError:
            continue

    raise AccountError(
        "Install Codex CLI or set CODEX_ACCOUNTS_CODEX_BIN to its executable."
    )


def account_environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    if shared_history := configured_history(home):
        env["CODEX_SQLITE_HOME"] = str(shared_history)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(name, None)
    return env


async def read_identity(binary: str, home: Path, *, timeout: float = 12) -> dict:
    """Read the saved identity without requesting a token refresh."""
    result = await _read_account(
        binary, home, "account/read", params={"refreshToken": False}, timeout=timeout
    )
    if "account" not in result:
        raise AccountError("Codex returned invalid account metadata.")
    account = result["account"]
    if account is None:
        return {"email": None, "plan": None, "kind": "signedOut"}
    if not isinstance(account, dict) or not safe_text(account.get("type"), limit=64):
        raise AccountError("Codex returned an unsupported account identity.")
    email, plan = account.get("email"), account.get("planType")
    if email is not None and not valid_email(email):
        raise AccountError("Codex did not return a valid email address.")
    if plan is not None and not safe_text(plan, limit=64):
        raise AccountError("Codex returned invalid account plan metadata.")
    return {"email": email, "plan": plan, "kind": account["type"]}


async def read_rate_limits(binary: str, home: Path, *, timeout: float = 5) -> dict:
    """Read live quota metadata through Codex's own credential backend."""
    return await _read_account(binary, home, "account/rateLimits/read", timeout=timeout)


async def _read_account(
    binary: str,
    home: Path,
    method: str,
    *,
    params: dict | None = None,
    timeout: float,
) -> dict:
    """Make a bounded stdio account request without starting a conversation."""
    cmd = [binary]
    if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
        cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", binary]
    process = await asyncio.create_subprocess_exec(
        *cmd,
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
        request = {"id": 1, "method": method}
        if params is not None:
            request["params"] = params
        await send(request)
        return await response(1)

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except (TimeoutError, ValueError, BrokenPipeError, ConnectionError) as error:
        raise AccountError(
            "Could not read the account metadata. Try codex list accounts --refresh."
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
    shared_history = prepare_home(Store(default_root()), home)
    env = account_environment(home)
    env["CODEX_SQLITE_HOME"] = str(shared_history)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "posix":
        os.execve(binary, [binary, *arguments], env)
    cmd = [binary, *arguments]
    if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
        cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", binary, *arguments]
    return subprocess.call(cmd, env=env)
