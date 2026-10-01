import base64
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex-accounts"
loader = importlib.machinery.SourceFileLoader("accounts", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
accounts = importlib.util.module_from_spec(spec)
loader.exec_module(accounts)


def credentials(email, access="initial", account_id="workspace-1"):
    payload = {"email": email, "sub": email, "https://api.openai.com/auth": {"chatgpt_plan_type": "team"}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return {"tokens": {"id_token": f"header.{encoded}.signature", "access_token": access,
                       "refresh_token": "fake-refresh", "account_id": account_id}}


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "codex"
        self.home.mkdir()
        (self.home / "accounts").mkdir()
        self.before = credentials("before@example.test")
        self.after = credentials("after@example.test")
        self.write(self.home / "auth.json", self.before)
        self.write(self.home / "accounts/before.json", self.before)
        self.write(self.home / "accounts/after.json", self.after)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        daemon = self.bin / "codex"
        daemon.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ["CODEX_HOME"])
assert sys.argv[1:] == ["app-server", "daemon", "stop"]
with (home / "daemon-events").open("a") as log:
    log.write((home / "auth.json").read_text() if (home / "auth.json").exists() else "absent")
if os.environ.get("FAKE_REFRESH"):
    auth = json.loads((home / "auth.json").read_text())
    auth["tokens"]["access_token"] = "refreshed-during-stop"
    (home / "auth.json").write_text(json.dumps(auth))
if os.environ.get("FAKE_FAILURE"):
    print("private-cli-output", file=sys.stderr)
    sys.exit(1)
print(os.environ.get("FAKE_RESULT", '{"status":"stopped"}'))
''')
        daemon.chmod(0o755)
        self.env = dict(os.environ, CODEX_HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def write(self, path, auth):
        path.write_text(json.dumps(auth))

    def run_cli(self, *args, **env):
        return subprocess.run(["python3", str(SCRIPT), *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=10)

    def test_stop_precedes_switch_and_accounts_are_preserved(self):
        result = self.run_cli("switch", "after", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.home / "daemon-events").read_text()), self.before)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.after)
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)
        self.assertIn("background tasks may be interrupted", result.stderr)
        self.assertEqual((self.home / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_shutdown_failure_keeps_current_and_saved_credentials(self):
        result = self.run_cli("switch", "after", "--yes", FAKE_FAILURE="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)
        self.assertNotIn("private-cli-output", result.stderr)

    def test_unknown_or_malformed_shutdown_status_aborts(self):
        for output in ('{"status":"running"}', 'not-json', '[]', '{}', '{"status":[]}'):
            with self.subTest(output=output):
                result = self.run_cli("sw", "after", "-y", FAKE_RESULT=output)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_stopped_daemon_is_not_required_to_switch(self):
        result = self.run_cli("sw", "after", "-y", FAKE_RESULT='{"status":"notRunning"}')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_noninteractive_switch_requires_explicit_yes(self):
        result = self.run_cli("switch", "after")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--yes", result.stderr)
        self.assertFalse((self.home / "daemon-events").exists())
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_interactive_cancel_has_no_effect(self):
        args = mock.Mock(yes=False)
        args.name = "after"
        with mock.patch.object(accounts.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(accounts, "stop_daemon") as stop:
            accounts.switch(self.home, args)
        stop.assert_not_called()
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_empty_or_eof_confirmation_cancels(self):
        with mock.patch.object(accounts.sys.stdin, "isatty", return_value=True):
            with mock.patch("builtins.input", return_value=""):
                self.assertFalse(accounts.confirm_switch(False))
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertFalse(accounts.confirm_switch(False))

    def test_invalid_target_is_rejected_before_shutdown(self):
        (self.home / "accounts/after.json").write_text('{"secret": "do-not-print",')
        result = self.run_cli("switch", "after", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("do-not-print", result.stderr)
        self.assertFalse((self.home / "daemon-events").exists())

    def test_missing_target_is_rejected_before_shutdown(self):
        result = self.run_cli("switch", "missing", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "daemon-events").exists())

    def test_names_cannot_escape_accounts_directory(self):
        for command in ("add", "switch", "remove"):
            for name in ("../auth", "/absolute", "..", "a\\b", "a\nb"):
                with self.subTest(command=command, name=name):
                    result = self.run_cli(command, name)
                    self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "daemon-events").exists())

    def test_refreshed_tokens_are_saved_after_shutdown(self):
        result = self.run_cli("switch", "after", "--yes", FAKE_REFRESH="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads((self.home / "accounts/before.json").read_text())
        self.assertEqual(saved["tokens"]["access_token"], "refreshed-during-stop")

    def test_switching_to_current_account_uses_refreshed_tokens(self):
        result = self.run_cli("switch", "before", "--yes", FAKE_REFRESH="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        current = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(current["tokens"]["access_token"], "refreshed-during-stop")

    def test_current_matches_identity_after_refresh(self):
        self.write(self.home / "auth.json", credentials("before@example.test", access="new"))
        result = self.run_cli("whoami")
        self.assertEqual(result.returncode, 0)
        self.assertIn("before  before@example.test", result.stdout)
        self.assertNotIn("unsaved", result.stdout)

    def test_explicit_current_label_uses_fresh_tokens_even_with_duplicates(self):
        self.write(self.home / "accounts/duplicate.json", self.before)
        result = self.run_cli("switch", "before", "--yes", FAKE_REFRESH="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        current = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(current["tokens"]["access_token"], "refreshed-during-stop")
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), current)
        self.assertEqual(json.loads((self.home / "accounts/duplicate.json").read_text()), self.before)

    def test_same_user_in_different_workspaces_is_distinguished(self):
        self.write(self.home / "accounts/other-workspace.json",
                   credentials("before@example.test", account_id="workspace-2"))
        result = self.run_cli("current")
        self.assertEqual(result.returncode, 0)
        self.assertIn("before  before@example.test", result.stdout)

    def test_ambiguous_identity_does_not_overwrite_saved_snapshots(self):
        self.write(self.home / "accounts/duplicate.json", self.before)
        result = self.run_cli("switch", "after", "--yes", FAKE_REFRESH="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)

    def test_add_default_name_force_and_private_permissions(self):
        result = self.run_cli("add")
        self.assertEqual(result.returncode, 1)
        result = self.run_cli("add", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "accounts").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / "accounts/before.json").stat().st_mode & 0o777, 0o600)

    def test_list_remove_aliases_and_corrupt_snapshot(self):
        (self.home / "accounts/broken.json").write_text("broken-secret")
        result = self.run_cli("ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<- active", result.stdout)
        self.assertNotIn("broken-secret", result.stderr)
        self.assertIn("Warning:", result.stderr)
        self.assertEqual(self.run_cli("rm", "after").returncode, 0)
        self.assertFalse((self.home / "accounts/after.json").exists())

    def test_keyring_storage_aborts_before_shutdown(self):
        (self.home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        result = self.run_cli("switch", "after", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn('cli_auth_credentials_store = "file"', result.stderr)
        self.assertFalse((self.home / "daemon-events").exists())

    def test_nested_storage_setting_does_not_override_top_level(self):
        (self.home / "config.toml").write_text('cli_auth_credentials_store = "file"\n[profiles.test]\ncli_auth_credentials_store = "keyring"\n')
        result = self.run_cli("switch", "after", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_current_login_can_be_replaced_from_saved_account(self):
        (self.home / "auth.json").unlink()
        result = self.run_cli("switch", "after", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.after)

    def test_api_key_accounts_do_not_print_keys(self):
        self.write(self.home / "auth.json", {"OPENAI_API_KEY": "fake-private-key"})
        result = self.run_cli("add", "api")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("list", "current"):
            result = self.run_cli(command)
            self.assertNotIn("fake-private-key", result.stdout + result.stderr)
        result = self.run_cli("switch", "api", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fake-private-key", result.stdout + result.stderr)

    def test_concurrent_switch_is_rejected(self):
        with (self.home / ".codex-accounts.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_cli("switch", "after", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Another codex-accounts command", result.stderr)
        self.assertFalse((self.home / "daemon-events").exists())

    def test_atomic_write_failure_leaves_old_file_and_cleans_staging(self):
        path = self.home / "auth.json"
        with mock.patch.object(accounts.os, "replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                accounts.write_auth(path, json.dumps(self.after).encode())
        self.assertEqual(json.loads(path.read_text()), self.before)
        self.assertEqual(list(self.home.glob(".codex-accounts-*")), [])

    def test_missing_cli_and_shutdown_timeout_are_clear(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("codex", 60)):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(accounts.subprocess, "run", side_effect=failure):
                    with self.assertRaisesRegex(accounts.AccountError, "Credentials have not been switched"):
                        accounts.stop_daemon(self.home)


if __name__ == "__main__":
    unittest.main()
