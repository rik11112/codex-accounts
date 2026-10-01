First stable release of codex-accounts.

- Switch saved accounts with confirmation and automatic daemon stop/start.
- Log out while keeping saved credentials out of the Codex logout operation.
- Skip switch confirmation with `-y`/`-f` or the persistent `no-switch-confirm` setting.
- Preserve refreshed credentials and remain compatible with existing saved accounts and aliases.
- Install directly over the original tool without an executable backup.

Install or update:

```bash
curl -fsSL https://raw.githubusercontent.com/rik11112/codex-accounts/v1.0.0/install.sh | bash
```

Requires Python 3.10+, Linux/macOS/WSL, file-based credentials, and Codex daemon management (tested with 0.159.3).

Validation: 50 automated tests on Linux and macOS with Python 3.10 and 3.12.
