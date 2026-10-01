# codex-accounts

Switch between saved Codex accounts, with confirmation before restarting the
shared background daemon to load the selected login.

This is a new implementation inspired by [YogevKr's original account-switching
Gist](https://gist.github.com/YogevKr/8a1560743b77f7c2711747ff74547042).
It keeps the original commands, aliases, and `accounts/<name>.json` format.

## Requirements

- Linux, macOS, or WSL; native Windows is not supported.
- Python 3.10 or newer and Bash/curl for installation.
- Codex on `PATH`, with `codex app-server daemon stop` support (tested with 0.159.3).
- File-based credentials. In the top-level section of your Codex `config.toml`, use:

  ```toml
  cli_auth_credentials_store = "file"
  ```

The default Codex directory is `~/.codex`. A custom `CODEX_HOME` is respected by
both the installer and the tool, including the daemon stop/start commands.
Keyring and ephemeral credentials are not managed by this tool. See the
[official authentication documentation](https://learn.chatgpt.com/docs/auth).

## Install or replace the original tool

The current build on `main` is available for testing. CLI disconnection and
reconnection after daemon shutdown have been checked. Account loading after a
switch and interrupted/background work still need checking before the first release.

```bash
curl -fsSL https://raw.githubusercontent.com/rik11112/codex-accounts/main/install.sh | CODEX_ACCOUNTS_REF=main bash
```

The installer replaces `~/.codex/codex-accounts` directly. Existing saved accounts,
`auth.json`, configuration, and shell aliases stay in place. It does not stop
Codex, change credentials, or make an executable backup. Downloads are staged
and checked for Python syntax before replacing the installed file.

If you already use an alias such as `cx`, continue using it. Otherwise, you can
add this to your shell configuration:

```bash
alias cx="$HOME/.codex/codex-accounts"
```

For a custom `CODEX_HOME`, point the alias to the executable in that directory.

To install directly from a checkout:

```bash
bash install.sh --local
```

## Usage

```bash
~/.codex/codex-accounts list
~/.codex/codex-accounts add work
~/.codex/codex-accounts switch personal
~/.codex/codex-accounts current
~/.codex/codex-accounts logout
~/.codex/codex-accounts remove old-account
~/.codex/codex-accounts --version
```

`ls`, `sw`, `whoami`, and `rm` are aliases for `list`, `switch`, `current`, and
`remove`. `logout` stops the daemon and signs out while protecting saved credentials.
`add` without a name uses the local part of your login email;
`add --force NAME` replaces an existing snapshot.

### Switching

```text
Warning: switching restarts the shared Codex daemon. Existing CLI sessions will disconnect while the daemon restarts. Active responses and background tasks may be interrupted.
Restart the daemon and switch accounts? [y/N]
```

An empty answer, `n`, or EOF cancels the switch. For deliberate noninteractive use:

```bash
~/.codex/codex-accounts switch work --yes  # -y, -f, and --force also work
```

Switching validates the saved credentials, stops the daemon using
`codex app-server daemon stop`, and checks that Codex reports `stopped` or
`notRunning`. Shutdown failure or timeout aborts the switch. It then saves any
refreshed credentials for the account being left, atomically replaces
`auth.json`, and runs `codex app-server daemon start`. Existing CLI sessions can
then reconnect without opening another CLI. The switch output also recommends
restarting any running Codex sessions to refresh their account state.

If startup fails or times out, the selected credentials remain on disk and the
command exits with an error explaining that the account was switched. Retry
`codex app-server daemon start` from a separate terminal. Credentials are not
rolled back automatically.

In a manual test with Codex 0.159.3, stopping the daemon made existing CLI sessions
show `Connection lost. Attempting to reconnect…`. Those sessions did not appear
to restart the daemon themselves. Starting a new CLI session started the daemon,
and the disconnected sessions reconnected. This confirms reconnection behavior;
it does not establish that an interrupted response or background task continues.

The tool uses the daemon management command rather than matching and killing
process names. Other Codex clients, such as an IDE extension or a session started
with `--no-daemon`, may have their own cached login; close and reopen them too.
`current` and the active marker in `list` describe the credentials on disk.

New credential files have owner-only permissions. The saved accounts directory
is restricted to its owner when adding an account. If multiple saved labels
match the account being left, automatic snapshot refresh is skipped to avoid
overwriting an ambiguous account. Explicitly save it with `add --force NAME`.

### Adding another login

Save your current account first:

```bash
~/.codex/codex-accounts add work
```

Close other Codex clients and let background tasks finish. Then, from a separate
terminal, use the protected logout command and log into the next account:

```bash
~/.codex/codex-accounts logout
codex login
~/.codex/codex-accounts add personal
```

`logout` confirms before stopping the daemon, saves refreshed credentials for the
matching saved account, replaces `auth.json` with an empty credential object, then
runs `codex logout`. This keeps the real saved credentials out of the logout
operation. The daemon stays stopped. Use `logout -y` or `logout -f` to skip
confirmation. If logout fails, saved accounts remain intact and the current
credentials remain cleared; retry `codex logout`.

Saved snapshots can still expire or be revoked. If an account stops working,
log into it again and replace its snapshot with `add --force NAME`.
Keep `auth.json` and saved snapshots private; they contain login credentials.

## Updating and rollback

Re-run the install command to replace the executable with the current build.
After the first tested release is tagged, installation can be pinned to that
version so team members install the same code. The installer accepts a
`CODEX_ACCOUNTS_REF` environment variable to select a tag or commit.

To return to the original tool, reinstall it using its Gist instructions. Your
saved accounts remain compatible. That version does not handle the shared daemon.

## Development and verification

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

Automated tests use fake credentials and a fake Codex command to check confirmation,
stop/swap/start ordering and failures, token refresh, private file permissions, account
commands, protected logout, concurrent switches, and installer replacement/failure behavior.
CI runs these checks on Linux and macOS with Python 3.10 and 3.12.
Actual daemon start/stop has also been checked with Codex 0.159.3 using an isolated
`CODEX_HOME` without real credentials. A separate manual test of live CLI sessions
confirmed the disconnection/reconnection behavior described above.

Before team rollout, verify a real account switch: check that existing CLI sessions
reconnect after the automatic daemon start and check the account they use. Also
open a fresh CLI to verify its account and check whether interrupted responses
and background tasks resume. Do not infer uninterrupted operation from the switch
command succeeding or clients reconnecting.

## License

The implementation in this repository is licensed under MIT. The original Gist
is credited as the inspiration; its source code is not bundled here.
