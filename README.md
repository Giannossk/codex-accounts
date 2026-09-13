# Codex Accounts

A complete terminal account selector for the official Codex CLI. Sign into each
account once, list the discovered email addresses, and manually choose which
account Codex uses. Skills, plugins, settings, instructions, and history are
shared across accounts; each login keeps its own credentials.

## Everyday commands

```bash
codex add account
codex list accounts
codex select account
codex
```

Each `codex add account` starts the official browser sign-in flow in a new,
private account home. Choose the account to add in the browser. After sign-in,
its email and subscription plan are discovered automatically. No account names
or labels are requested.

`codex list accounts` displays every saved email, its plan, and a star beside
the selected account. For example:

```text
Saved accounts

 * 1. alice@example.com  (pro)
   2. bob@example.com  (plus)

* Selected for new Codex sessions
```

`codex select account` opens the account picker. Use **Up/Down** or **j/k**,
then **Enter** to select. **Esc**, **q**, or **Ctrl+C** cancels without changing the
selection. A numbered-input menu is used when arrow-key terminal support is
unavailable. Long lists scroll in the terminal.

Selection is saved for future launches. Run `codex` to start, or choose and
launch together:

```bash
codex select account --run
```

A running Codex session keeps its account. Exit it normally and start another
session after selecting a different account; logging out is unnecessary.

## More controls

| Command | Behavior |
| --- | --- |
| `codex select account alice@example.com` | Select an email directly. |
| `codex select account 2` | Select a number from the displayed list. |
| `codex current account` | Show the selected email. |
| `codex --account alice@example.com exec "Review this repository"` | Use an account for one launch without changing the saved selection. |
| `codex add account --device-auth` | Sign in using the official device-code flow. |
| `codex list accounts --refresh` | Refresh cached email and plan metadata. |
| `codex list accounts --json` | Read structured account metadata, including IDs and home paths. |
| `codex remove account` | Pick an account to remove from the list. |
| `codex remove account alice@example.com` | Remove an email directly. |
| `codex share data` | Migrate and share existing local data across every saved account now. |

Multiple saved logins can have the same email, including accounts with different
workspace contexts. They remain separate; use the numbered picker when an email
matches more than one row.

Removing an account unregisters it and clears the selection if it was active.
Its home, session history, and credentials are retained. To explicitly sign out,
run `codex --account EMAIL logout` before removing it. If Codex requests
reauthentication later, `codex --account EMAIL login` updates that saved login
and refreshes its displayed email.

To recover an already signed-in home, `codex add account --home /path/to/home`
discovers its email without copying credentials or starting another sign-in.

## Installation

This is a companion to the official Codex CLI. It does not include the Codex
runtime and it does not replace the official executable. Install Python 3.11+
and the official Codex CLI first, then install this checkout with `uv`.

### 1. Install the official Codex CLI

On macOS or Linux, the official standalone installer is:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

On any platform with Node.js, the npm installation is:

```bash
npm install -g @openai/codex
```

Confirm that the official command works before installing this package:

```text
codex --version
```

See the [official Codex CLI setup guide](https://learn.chatgpt.com/docs/codex/cli)
for the current supported installation options.

### 2. Install this checkout

If `uv` is not installed, install it first.

Linux and macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

Open a new terminal after installing `uv`, then change to this repository and
install the package as an editable command-line tool:

```bash
cd /path/to/codex-accounts
uv tool install --editable .
```

PowerShell uses the same installation command:

```powershell
Set-Location C:\path\to\codex-accounts
uv tool install --editable .
```

If `codex-accounts` is not found after installation, run `uv tool update-shell`,
open a new terminal, and try again.

### 3. Enable the `codex` wrapper

On Bash, enable it in the current terminal with:

```bash
eval "$(codex-accounts shell-init bash)"
```

To enable it in future Bash terminals, add that same `eval` line once to
`~/.bashrc`, then run `source ~/.bashrc`. On Zsh, use the matching commands:

```zsh
eval "$(codex-accounts shell-init zsh)"
```

Add the Zsh line to `~/.zshrc` for persistence. The generated function keeps
the official Codex executable as the backend and routes account-management
commands through this package.

PowerShell does not use the Bash/Zsh `shell-init` output. Add the following to
`$PROFILE` (open it with `notepad $PROFILE`), then reload it with `. $PROFILE`:

```powershell
$script:CodexOfficial = @(
    (Get-Command codex.cmd -CommandType Application -ErrorAction SilentlyContinue).Source
    (Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue).Source
    (Get-Command codex -CommandType Application -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ } | Select-Object -First 1

if (-not $script:CodexOfficial) {
    throw "Install the official Codex CLI before enabling the account wrapper."
}

function codex {
    $previous = $env:CODEX_ACCOUNTS_CODEX_BIN
    $env:CODEX_ACCOUNTS_CODEX_BIN = $script:CodexOfficial
    try {
        & codex-accounts @args
        $exitCode = $LASTEXITCODE
    }
    finally {
        if ($null -eq $previous) {
            Remove-Item Env:CODEX_ACCOUNTS_CODEX_BIN -ErrorAction SilentlyContinue
        } else {
            $env:CODEX_ACCOUNTS_CODEX_BIN = $previous
        }
    }
    if ($null -ne $exitCode) {
        $global:LASTEXITCODE = $exitCode
    }
}
```

If you do not want a PowerShell wrapper, the executable is always available as
`codex-accounts`; for example, use `codex-accounts list accounts`.

### 4. Add and select accounts

After the wrapper is enabled, the commands are the same on all platforms:

```text
codex add account
codex add account
codex list accounts
codex select account
codex
```

Each `add account` login is saved in its own Codex home. `select account`
changes the account used by the next session; it never logs out another saved
account. On Windows, the picker uses numbered input. On POSIX terminals,
arrow keys and `j`/`k` are also available.

### Updating or uninstalling

From the checkout, refresh the editable installation with:

```text
uv tool install --editable . --force
```

Remove this package with:

```text
uv tool uninstall codex-accounts
```

The official Codex CLI is a separate installation and must be updated or
removed separately.

## Shared data and separate credentials

Each saved login has a stable, separate `CODEX_HOME` for credentials. All accounts
share the existing data at `~/.codex`: settings (`config.toml`), skills, plugins,
instructions (`AGENTS.md`), rules, prompts, agents, automations, memories, caches,
and history. Switching accounts changes the login while keeping your setup.
Selecting an account chooses the login used for both new and resumed chats:

```sh
codex select account alice@example.com
codex resume
codex resume SESSION_ID
```

No environment override is required. The wrapper sets `CODEX_SQLITE_HOME` to the
shared store and links each account's session directories, history indexes, and
thread writer locks to it, along with its other persistent files and directories.
The existing `~/.codex` store stays in place. Its
credentials are used only when that login is explicitly selected.

Sharing is prepared when adding a new login, selecting an account, or launching
Codex. To apply it to all existing accounts immediately, without starting a chat:

```sh
codex share data
```

Existing custom skills and plugins from all registered homes are combined.
Existing shared files take precedence when the same path has different content.
Missing configuration keys, project settings, profiles, and skill entries are
merged into the shared configuration; its existing values take precedence.
Configuration is rewritten only when missing settings must be imported; its
original formatting and comments are kept in the backup.

Original account files are retained under
`<account-home>/.shared-backups/<migration-id>/account/`. Shared configuration
files changed by the merge are backed up alongside them under `shared/`.
The migration prints each backup location. No credential files are copied.
Directory links make later skill installations and edits visible to all
accounts immediately. Additional top-level files introduced by Codex are
discovered on subsequent launches or with `codex share data`. If an external
editor replaces a file link, the next preparation repairs it, keeping the
newer file and retaining the other copy in the backup.

Credential files (`auth.json`, `.credentials.json`), the `secrets` directory,
credential backups, and OS keyring entries remain specific to each login.
Temporary process files, locks, and recovery copies also stay local; old SQLite
files are retained for recovery while live databases use `CODEX_SQLITE_HOME`.
Sharing local plugin files does not transfer a remote account's authorizations.
Restart existing Codex sessions to reload the shared skills and configuration.
Codex's [official credential storage documentation](https://learn.chatgpt.com/docs/auth#credential-storage)
describes its file and keyring backends.

Account-specific history also joins the shared store. Original history files
are retained in `.history-backups` inside the account home; original account
databases remain in place. Shared databases modified by an import are backed up
under the account manager's `history-backups` directory using SQLite's backup
API, including committed WAL data. Existing shared threads take precedence.
Threads, paginated history, goals, queued messages, and thread memory records
are imported; account-specific enrollments and old logs stay in their original
stores. Future SQLite data, including logs, goals, and memories, is shared.

Finish and exit running chats before the first launch with this version. A busy
account's migration is deferred until its chats close; launching that account
before migration is complete reports an actionable error. Normal launches after
migration do not require other chats to close. The original account databases
and file backups are retained for recovery and should not be used for parallel
sessions after migration.

The shared location is recorded in `history.json` beside the account registry
and is reused for settings and skills as well.
For a custom location, set `CODEX_ACCOUNTS_HISTORY_HOME` before the first launch;
later launches keep the recorded location. Symbolic links are required; Windows
users must enable Developer Mode or run with permission to create them.

The manager does not import the original default login automatically. If no
account is selected, an interactive Codex launch opens the picker; scripts must
select an account or supply `--account EMAIL`. Empty registries ask you to add
an account. Quota errors never cause automatic switching or retries.

Email discovery uses Codex's local stdio `account/read` API with
`refreshToken: false`. It reads metadata, starts no conversations, and submits
no model requests. Codex's credential backend handles both file and keyring
storage. The manager never reads, copies, or parses raw tokens. The
[official account API documentation](https://learn.chatgpt.com/docs/app-server)
describes this interface and its nullable email field.

Only home paths, emails, plan metadata, and the chosen account ID enter the
registry. On Linux it lives in `~/.local/share/codex-accounts/accounts.json`,
respecting `XDG_DATA_HOME`. New homes are under `homes/<internal-id>/`.
On macOS the default root is `~/Library/Application Support/codex-accounts`;
on Windows it is `%LOCALAPPDATA%/codex-accounts`.

Registry changes are locked and written atomically. Created directories use mode
`0700` and registry files `0600` on POSIX. A successful login remains saved
if email lookup temporarily fails; use `list accounts --refresh` to retry.
Accounts whose provider supplies no email are shown as “Email unavailable”.

Named environment credentials (`OPENAI_API_KEY`, `CODEX_API_KEY`, and
`CODEX_ACCESS_TOKEN`) are omitted from account launches so they cannot override
the chosen saved login. Each home's configuration and explicit provider flags
still apply. A remote Codex server manages its own authentication.

| Setting | Purpose |
| --- | --- |
| `CODEX_ACCOUNTS_HOME` | Override the registry and managed-home root. |
| `CODEX_ACCOUNTS_CODEX_BIN` | Set the official Codex executable; otherwise use `codex` on `PATH`. |

Old registries migrate to the email-based schema when updated. A private backup
is saved before migration, and existing home paths remain stable. Nicknames are
discarded.

## Development

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
```

The integration suite uses a temporary fake Codex process with the actual
initialize/account-read protocol shape. It exercises repeated sign-ins, email
discovery, cached lists, token-refresh isolation, registry migration, concurrent
selection, failure recovery, shell forwarding, and manual handling of quota
errors. Real PTY tests cover arrow keys, cancellation, and terminal restoration.
The native metadata protocol is also smoke-tested with the installed Codex CLI.

The tests never sign into real accounts. Browser sign-in remains an interactive
action for the account owner. Native PTY integration tests run on POSIX, while
portable account-state, parser, and numbered-picker tests run on every platform.

GitHub Actions runs the test, parser, lint, formatting, and wheel checks on
Ubuntu, macOS, and Windows.

Verified with Codex CLI `0.154.0` and upstream source commit
`1715e55076737158ba61d43158ede504de6d4ce1`.

Implementation references:
[account protocol](https://github.com/openai/codex/blob/1715e55076737158ba61d43158ede504de6d4ce1/codex-rs/app-server-protocol/src/protocol/v2/account.rs),
[login flow](https://github.com/openai/codex/blob/1715e55076737158ba61d43158ede504de6d4ce1/codex-rs/cli/src/login.rs),
[credential storage](https://github.com/openai/codex/blob/1715e55076737158ba61d43158ede504de6d4ce1/codex-rs/login/src/auth/storage.rs).
