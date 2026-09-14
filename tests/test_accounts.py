import asyncio
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_accounts.native import read_identity, read_rate_limits
from codex_accounts.state import AccountError

SOURCE = Path(__file__).resolve().parents[1]

FAKE_CODEX = r"""
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
home = Path(os.environ["CODEX_HOME"])
event = {
    "args": args, "home": str(home), "cwd": os.getcwd(), "pid": os.getpid(),
    "sqlite_home": os.environ.get("CODEX_SQLITE_HOME"),
    "auth_overrides": [name for name in
        ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN") if name in os.environ],
}
def log(value):
    with open(os.environ["TEST_CODEX_LOG"], "a") as handle:
        handle.write(json.dumps(value) + "\n")
log(event)
credential = home / "auth.json"
if args == ["app-server"]:
    for line in sys.stdin:
        request = json.loads(line)
        log({"rpc": request})
        if request["method"] == "initialize":
            print(json.dumps({"id": request["id"], "result": {"userAgent": "fixture"}}), flush=True)
        elif request["method"] == "account/read":
            if os.environ.get("TEST_METADATA_HANG"):
                time.sleep(30)
            if os.environ.get("TEST_METADATA_ERROR"):
                print(json.dumps({"id": request["id"], "error": {"message": "secret-token-do-not-print"}}), flush=True)
                continue
            account = None
            if credential.exists():
                account = {"type": "chatgpt", **json.loads(credential.read_text())}
                account["accessToken"] = "secret-token-do-not-print"
            print(json.dumps({"method": "account/updated", "params": {}}), flush=True)
            print(json.dumps({"id": request["id"], "result": {"account": account, "requiresOpenaiAuth": True}}), flush=True)
        elif request["method"] == "account/rateLimits/read":
            if os.environ.get("TEST_QUOTA_HANG"):
                time.sleep(30)
            if (home / "quota-error").exists():
                print(json.dumps({"id": request["id"], "error": {"message": "secret-token-do-not-print"}}), flush=True)
                continue
            quota = home / "quota.json"
            result = json.loads(quota.read_text()) if quota.exists() else {"rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 25, "windowDurationMins": 300},
                "secondary": {"usedPercent": 58, "windowDurationMins": 10080},
            }}
            print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
            print(json.dumps({"id": request["id"], "result": result}), flush=True)
    sys.exit(0)
if args and args[0] == "login":
    if os.environ.get("TEST_LOGIN_FAIL"):
        sys.exit(7)
    if delay := os.environ.get("TEST_LOGIN_DELAY"):
        time.sleep(float(delay))
    credential.write_text(json.dumps({
        "email": os.environ.get("TEST_EMAIL", "alice@example.com"),
        "planType": os.environ.get("TEST_PLAN", "pro"),
    }))
elif args == ["logout"]:
    credential.unlink(missing_ok=True)
elif args == ["--refresh-token"]:
    (home / "refreshed").touch()
elif args == ["--echo-stdin"]:
    print(sys.stdin.read(), end="")
elif args == ["--exit-37"]:
    sys.exit(37)
elif args == ["--quota"]:
    print("You have hit your usage limit.", file=sys.stderr)
    sys.exit(1)
elif args == ["--wait-and-report"]:
    Path(os.environ["TEST_READY"]).touch()
    while not Path(os.environ["TEST_RELEASE"]).exists():
        time.sleep(0.01)
    print(str(home))
else:
    print(json.dumps(event))
"""


@unittest.skipUnless(
    os.name == "posix", "Native executable fixture uses a POSIX shebang"
)
class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / "manager"
        self.original = self.root / "original"
        self.original.mkdir()
        (self.original / "auth.json").write_text(
            json.dumps({"email": "original@example.com", "planType": "plus"})
        )
        self.log = self.root / "events.jsonl"
        self.binary = self.root / "codex fixture"
        self.binary.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
        self.binary.chmod(0o700)
        self.env = {
            **os.environ,
            "PYTHONPATH": str(SOURCE),
            "CODEX_ACCOUNTS_HOME": str(self.registry),
            "CODEX_ACCOUNTS_HISTORY_HOME": str(self.original),
            "CODEX_ACCOUNTS_CODEX_BIN": str(self.binary),
            "CODEX_HOME": str(self.original),
            "TEST_CODEX_LOG": str(self.log),
            "OPENAI_API_KEY": "test-env-key",
            "CODEX_API_KEY": "test-env-key",
            "CODEX_ACCESS_TOKEN": "test-env-token",
        }

    def invoke(self, *arguments, code=0, env=None, stdin=None):
        result = subprocess.run(
            [sys.executable, "-m", "codex_accounts", *arguments],
            env=self.env if env is None else env,
            input=stdin,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        return result

    def state(self):
        return json.loads((self.registry / "accounts.json").read_text())

    def events(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def add(self, email="alice@example.com", *flags):
        result = self.invoke(
            "add", "account", *flags, env={**self.env, "TEST_EMAIL": email}
        )
        self.assertIn(email, result.stdout)
        return result

    def record(self, email):
        return next(
            (key, record)
            for key, record in self.state()["accounts"].items()
            if record["email"] == email
        )

    def test_repeated_add_discovers_emails_without_labels(self):
        for email in ("alice@example.com", "bob@example.com", "carol@example.com"):
            self.add(email)
        result = self.invoke("list", "accounts")
        for email in ("alice@example.com", "bob@example.com", "carol@example.com"):
            self.assertIn(email, result.stdout)
        self.assertNotIn("personal", result.stdout)
        self.assertNotIn("home", result.stdout)
        self.assertIsNone(self.state()["selected"])
        self.assertEqual(len(self.state()["accounts"]), 3)
        for key, record in self.state()["accounts"].items():
            self.assertEqual(set(record), {"home", "email", "plan", "kind"})
            self.assertEqual(Path(record["home"]).name, key)
        rpc = [event["rpc"] for event in self.events() if "rpc" in event]
        self.assertTrue(
            all(
                request["params"] == {"refreshToken": False}
                for request in rpc
                if request["method"] == "account/read"
            )
        )
        self.assertNotIn("secret-token", (self.registry / "accounts.json").read_text())

    def test_device_login_uses_official_cli(self):
        self.add("alice@example.com", "--device-auth")
        self.assertIn(
            ["login", "--device-auth"], [event.get("args") for event in self.events()]
        )

    def test_email_selection_reuses_saved_login_without_logout(self):
        self.add()
        self.add("bob@example.com")
        for email in ("alice@example.com", "bob@example.com", "alice@example.com"):
            self.invoke("select", "account", email)
            self.invoke("--refresh-token")
        self.assertEqual(self.state()["selected"], self.record("alice@example.com")[0])
        self.assertTrue(
            (Path(self.record("alice@example.com")[1]["home"]) / "refreshed").exists()
        )
        arguments = [event.get("args") for event in self.events()]
        self.assertEqual(arguments.count(["login"]), 2)
        self.assertNotIn(["logout"], arguments)

    def test_list_is_cached_and_json_is_structured(self):
        self.add()
        count = len(self.events())
        self.invoke("list", "accounts")
        result = json.loads(self.invoke("list", "accounts", "--json").stdout)
        self.assertEqual(result["accounts"][0]["email"], "alice@example.com")
        self.assertEqual(len(self.events()), count)

    def test_numbered_picker_selects_account_and_reprompts_invalid_input(self):
        self.add()
        self.add("bob@example.com")
        result = self.invoke("select", "account", stdin="9\n2\n")
        self.assertIn("Enter one of the numbers", result.stdout)
        self.assertEqual(self.state()["selected"], self.record("bob@example.com")[0])

    def test_picker_reads_fresh_quotas_for_each_saved_home(self):
        self.add()
        self.add("bob@example.com")
        home = Path(self.record("bob@example.com")[1]["home"])
        quota = home / "quota.json"
        quota.write_text(
            json.dumps(
                {
                    "rateLimits": {
                        "primary": {"usedPercent": 100, "windowDurationMins": 300},
                        "secondary": {"usedPercent": 82, "windowDurationMins": 10080},
                    }
                }
            )
        )
        before = self.state()

        result = self.invoke("select", "account", stdin="q\n", code=130)
        self.assertIn(
            "alice@example.com  (pro)  5h: 75% left | weekly: 42% left", result.stdout
        )
        self.assertIn(
            "bob@example.com  (pro)  5h: 0% left | weekly: 18% left", result.stdout
        )
        quota.write_text(
            json.dumps(
                {
                    "rateLimits": {
                        "primary": {"usedPercent": 10, "windowDurationMins": 300},
                        "secondary": {"usedPercent": 20, "windowDurationMins": 10080},
                    }
                }
            )
        )
        result = self.invoke("select", "account", stdin="q\n", code=130)
        self.assertIn(
            "bob@example.com  (pro)  5h: 90% left | weekly: 80% left", result.stdout
        )
        self.assertEqual(self.state(), before)
        methods = [event["rpc"]["method"] for event in self.events() if "rpc" in event]
        self.assertEqual(methods.count("account/rateLimits/read"), 4)
        self.assertLessEqual(
            set(methods),
            {"initialize", "initialized", "account/read", "account/rateLimits/read"},
        )

    def test_picker_keeps_failed_quota_account_selectable(self):
        self.add()
        self.add("bob@example.com")
        key, record = self.record("alice@example.com")
        (Path(record["home"]) / "quota-error").touch()

        result = self.invoke("select", "account", stdin="1\n")

        self.assertIn(
            "alice@example.com  (pro)  5h: unavailable | weekly: unavailable",
            result.stdout,
        )
        self.assertIn(
            "bob@example.com  (pro)  5h: 75% left | weekly: 42% left", result.stdout
        )
        self.assertNotIn("secret-token", result.stdout + result.stderr)
        self.assertEqual(self.state()["selected"], key)

    def test_direct_selection_and_removal_skip_quota_lookups(self):
        self.add()
        self.invoke("select", "account", "alice@example.com")
        self.invoke("select", "account", "1")
        self.invoke("remove", "account", stdin="q\n", code=130)
        methods = [event["rpc"]["method"] for event in self.events() if "rpc" in event]
        self.assertNotIn("account/rateLimits/read", methods)

    def test_cancel_or_eof_does_not_change_selection(self):
        self.add()
        self.invoke("select", "account", "alice@example.com")
        before = self.state()
        self.invoke("select", "account", stdin="q\n", code=130)
        self.invoke("select", "account", stdin="", code=130)
        self.assertEqual(self.state(), before)

    def test_empty_registry_does_not_launch_original_login(self):
        result = self.invoke("list", "accounts")
        self.assertIn("No saved accounts", result.stdout)
        self.invoke(code=2)
        self.invoke("select", "account", stdin="1\n", code=2)
        self.assertFalse(self.log.exists())

    def test_account_selection_required_for_noninteractive_launch(self):
        self.add()
        self.invoke("exec", "a task", code=2)
        self.assertIsNone(self.state()["selected"])

    def test_override_preserves_selection_and_forwards_exact_arguments_and_stdin(self):
        self.add()
        self.add("bob@example.com")
        self.invoke("select", "account", "alice@example.com")
        arguments = ["exec", "a b; $(touch unwanted)", "-c", 'model="test"']
        result = self.invoke("--account", "bob@example.com", *arguments)
        event = json.loads(result.stdout)
        self.assertEqual(event["args"], arguments)
        self.assertEqual(event["home"], self.record("bob@example.com")[1]["home"])
        self.assertEqual(event["cwd"], str(self.root))
        self.assertEqual(event["auth_overrides"], [])
        self.assertFalse((self.root / "unwanted").exists())
        self.assertEqual(
            self.invoke(
                "--account=bob@example.com", "--echo-stdin", stdin="input\n"
            ).stdout,
            "input\n",
        )
        self.invoke("--account=bob@example.com", "--exit-37", code=37)
        self.assertEqual(self.state()["selected"], self.record("alice@example.com")[0])

    def test_select_and_run_starts_the_selected_cli(self):
        self.add()
        result = self.invoke("select", "account", "alice@example.com", "--run")
        event = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(event["home"], self.record("alice@example.com")[1]["home"])

    def test_accounts_keep_logins_and_share_existing_history(self):
        self.add()
        self.add("bob@example.com")
        sessions = self.original / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / "old-session.jsonl").write_text("original history\n")
        for email in ("alice@example.com", "bob@example.com"):
            self.invoke("select", "account", email)
            event = json.loads(self.invoke("resume", "old-session").stdout)
            account_home = Path(self.record(email)[1]["home"])
            self.assertEqual(event["home"], str(account_home))
            self.assertEqual(event["sqlite_home"], str(self.original))
            self.assertEqual(
                json.loads((account_home / "auth.json").read_text())["email"],
                email,
            )
            self.assertEqual(
                (account_home / "sessions" / "old-session.jsonl").read_text(),
                "original history\n",
            )
        self.invoke("list", "accounts", "--refresh")
        metadata_calls = [
            event for event in self.events() if event.get("args") == ["app-server"]
        ]
        self.assertEqual(metadata_calls[-1]["sqlite_home"], str(self.original))
        self.assertEqual(
            json.loads((self.original / "auth.json").read_text())["email"],
            "original@example.com",
        )

    def test_new_logins_already_have_shared_skills_and_settings(self):
        skills = self.original / "skills" / "custom"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("existing custom skill")
        (self.original / "config.toml").write_text('model="shared"\n')
        self.add()
        self.add("bob@example.com")
        for record in self.state()["accounts"].values():
            home = Path(record["home"])
            self.assertTrue(
                (home / "config.toml").samefile(self.original / "config.toml")
            )
            self.assertEqual(
                (home / "skills/custom/SKILL.md").read_text(), "existing custom skill"
            )
            self.assertEqual(
                json.loads((home / "auth.json").read_text())["email"], record["email"]
            )

    def test_share_data_migrates_without_launching_codex_or_changing_selection(self):
        self.add()
        self.add("bob@example.com")
        self.invoke("select", "account", "alice@example.com")
        first = Path(self.record("alice@example.com")[1]["home"])
        second = Path(self.record("bob@example.com")[1]["home"])
        (second / "AGENTS.md").write_text("recovered instructions")
        before, events = self.state(), self.events()
        result = self.invoke("share", "data")
        self.assertIn("Credentials remain separate", result.stdout)
        self.assertEqual(self.events(), events)
        self.assertEqual(self.state(), before)
        self.assertEqual((first / "AGENTS.md").read_text(), "recovered instructions")

    def test_duplicate_emails_are_separate_and_require_picker(self):
        self.add()
        self.add()
        self.assertEqual(len(self.state()["accounts"]), 2)
        self.invoke("select", "account", "alice@example.com", code=2)
        self.invoke("select", "account", stdin="2\n")
        self.assertIsNotNone(self.state()["selected"])

    def test_new_api_rejects_old_names(self):
        self.invoke("add", "account", "personal", code=2)
        self.invoke("account", "add", "work", code=2)
        self.assertFalse(self.log.exists())

    def test_login_failure_preserves_existing_selection(self):
        self.add()
        self.invoke("select", "account", "alice@example.com")
        before = self.state()
        self.invoke("add", "account", env={**self.env, "TEST_LOGIN_FAIL": "1"}, code=7)
        self.assertEqual(self.state(), before)

    def test_metadata_failure_preserves_login_and_retry_recovers_email(self):
        result = self.invoke(
            "add", "account", env={**self.env, "TEST_METADATA_ERROR": "1"}
        )
        self.assertNotIn("secret-token", result.stdout + result.stderr)
        self.assertEqual(len(self.state()["accounts"]), 1)
        self.invoke("list", "accounts", "--refresh")
        self.assertEqual(self.record("alice@example.com")[1]["kind"], "chatgpt")
        self.assertEqual(
            [event.get("args") for event in self.events()].count(["login"]), 1
        )

    def test_invalid_email_cannot_inject_terminal_controls(self):
        result = self.invoke(
            "add", "account", env={**self.env, "TEST_EMAIL": "a\x1b[2J@example.com"}
        )
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        record = next(iter(self.state()["accounts"].values()))
        self.assertIsNone(record["email"])

    def test_existing_home_discovers_email_without_copy_or_login(self):
        before = (self.original / "auth.json").read_bytes()
        self.invoke("add", "account", "--home", str(self.original))
        self.assertEqual(
            self.record("original@example.com")[1]["home"], str(self.original)
        )
        self.assertNotIn(["login"], [event.get("args") for event in self.events()])
        self.assertEqual((self.original / "auth.json").read_bytes(), before)
        self.invoke("add", "account", "--home", str(self.original), code=2)

    def test_v1_migration_discards_labels_and_keeps_home_and_selection(self):
        self.registry.mkdir()
        old = {
            "version": 1,
            "selected": "old-label",
            "accounts": {"old-label": str(self.original)},
        }
        (self.registry / "accounts.json").write_text(json.dumps(old))
        output = self.invoke("list", "accounts").stdout
        self.assertNotIn("old-label", output)
        self.assertIn("original@example.com", output)
        self.assertEqual(self.state()["version"], 2)
        self.assertEqual(
            self.state()["selected"], self.record("original@example.com")[0]
        )
        self.assertEqual(
            json.loads((self.registry / "accounts.v1.backup.json").read_text()), old
        )

    def test_remove_unregisters_without_revoking_or_deleting_home(self):
        self.add()
        self.invoke("select", "account", "alice@example.com")
        home = Path(self.record("alice@example.com")[1]["home"])
        self.invoke("remove", "account", "alice@example.com")
        self.assertEqual(self.state(), {"version": 2, "selected": None, "accounts": {}})
        self.assertTrue((home / "auth.json").exists())
        self.assertNotIn(["logout"], [event.get("args") for event in self.events()])

    def test_current_email_and_reauthentication_refresh_identity(self):
        self.add()
        self.invoke("select", "account", "alice@example.com")
        self.assertEqual(
            self.invoke("current", "account").stdout.strip(), "alice@example.com"
        )
        self.invoke("login", env={**self.env, "TEST_EMAIL": "changed@example.com"})
        self.assertEqual(
            self.invoke("current", "account").stdout.strip(), "changed@example.com"
        )
        self.invoke("logout")
        self.assertIn("sign-in required", self.invoke("list", "accounts").stdout)

    def test_missing_home_and_corrupt_registry_never_fall_back(self):
        self.add()
        self.invoke("select", "account", "alice@example.com")
        shutil.rmtree(self.record("alice@example.com")[1]["home"])
        before = self.events()
        self.invoke("exec", "task", code=2)
        (self.registry / "accounts.json").write_text("{broken")
        self.invoke("exec", "task", code=2)
        self.assertEqual(self.events(), before)

    def test_private_permissions(self):
        self.add()
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.registry / "accounts.json").stat().st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE(
                Path(self.record("alice@example.com")[1]["home"]).stat().st_mode
            ),
            0o700,
        )

    def test_usage_limit_does_not_switch_or_retry(self):
        self.add()
        self.add("bob@example.com")
        self.invoke("select", "account", "alice@example.com")
        before, count = self.state(), len(self.events())
        self.invoke("--quota", code=1)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.events()), count + 1)

    def test_shell_integration_supports_requested_command_spelling(self):
        self.add()
        init = self.invoke("shell-init", "bash").stdout
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", init + "\ncodex list accounts"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alice@example.com", result.stdout)
        self.assertIn("codex add account", self.invoke("--help").stdout)

    def test_recursive_backend_is_rejected(self):
        (self.root / "codex-accounts").symlink_to(self.binary)
        self.invoke(
            "--version",
            env={**self.env, "PATH": str(self.root) + os.pathsep + self.env["PATH"]},
            code=2,
        )
        self.assertFalse(self.log.exists())

    def test_metadata_timeout_reaps_process(self):
        with patch.dict(os.environ, {**self.env, "TEST_METADATA_HANG": "1"}):
            with self.assertRaises(AccountError):
                asyncio.run(read_identity(str(self.binary), self.original, timeout=0.2))
        pid = next(
            event["pid"]
            for event in self.events()
            if event.get("args") == ["app-server"]
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_quota_timeout_reaps_process(self):
        with patch.dict(os.environ, {**self.env, "TEST_QUOTA_HANG": "1"}):
            with self.assertRaises(AccountError):
                asyncio.run(
                    read_rate_limits(str(self.binary), self.original, timeout=0.2)
                )
        pid = next(
            event["pid"]
            for event in self.events()
            if event.get("args") == ["app-server"]
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_invalid_quota_response_is_safely_rejected(self):
        (self.original / "quota.json").write_text('["secret-token-do-not-print"]')
        with patch.dict(os.environ, self.env):
            with self.assertRaises(AccountError) as error:
                asyncio.run(read_rate_limits(str(self.binary), self.original))
        self.assertNotIn("secret-token", str(error.exception))

    def test_parallel_selection_keeps_all_accounts(self):
        self.add()
        self.add("bob@example.com")
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "codex_accounts", "select", "account", email],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for email in ("alice@example.com", "bob@example.com") * 4
        ]
        for child in processes:
            output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, (output, error))
        self.assertEqual(len(self.state()["accounts"]), 2)
        self.assertIn(self.state()["selected"], self.state()["accounts"])

    def test_running_session_keeps_original_selected_account(self):
        self.add()
        self.add("bob@example.com")
        self.invoke("select", "account", "alice@example.com")
        ready, release = self.root / "ready", self.root / "release"
        with subprocess.Popen(
            [sys.executable, "-m", "codex_accounts", "--wait-and-report"],
            env={**self.env, "TEST_READY": str(ready), "TEST_RELEASE": str(release)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                self.invoke("select", "account", "bob@example.com")
            finally:
                release.touch()
            output, error = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, error)
        self.assertEqual(output.strip(), self.record("alice@example.com")[1]["home"])

    def tty_picker(self, keys=None, interrupt=False):
        import termios

        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        original = termios.tcgetattr(slave)
        child = subprocess.Popen(
            [sys.executable, "-m", "codex_accounts", "select", "account"],
            env={**self.env, "TERM": "xterm-256color", "COLUMNS": "80", "LINES": "24"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        try:
            output = b""
            deadline = time.monotonic() + 10
            while b"Esc cancel" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    output += os.read(master, 8192)
            self.assertIn(b"Esc cancel", output)
            if interrupt:
                child.send_signal(signal.SIGINT)
            else:
                os.write(master, keys)
            child.wait(timeout=5)
            while select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 8192)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(termios.tcgetattr(slave), original)
        self.assertIn(b"\x1b[?25h", output)
        return child.returncode, output.decode()

    def test_arrow_picker_selects_email_and_restores_terminal(self):
        self.add()
        self.add("bob@example.com")
        code, output = self.tty_picker(b"\x1b[B\r")
        self.assertEqual(code, 0, output)
        self.assertIn("5h: 75% left | weekly: 42% left", output)
        self.assertEqual(self.state()["selected"], self.record("bob@example.com")[0])

    def test_picker_wraps_long_emails_without_hiding_weekly_quota(self):
        email = "a-long-account-address-that-needs-extra-space@example.com"
        self.add(email)
        code, output = self.tty_picker(b"\r")
        self.assertEqual(code, 0, output)
        self.assertIn(email, output)
        self.assertIn("weekly: 42% left", output)
        self.assertEqual(self.state()["selected"], self.record(email)[0])

    def test_escape_and_ctrl_c_restore_terminal_and_leave_selection_unchanged(self):
        self.add()
        self.invoke("select", "account", "alice@example.com")
        before = self.state()
        for kwargs in ({"keys": b"\x1b"}, {"interrupt": True}):
            code, output = self.tty_picker(**kwargs)
            self.assertEqual(code, 130, output)
            self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
