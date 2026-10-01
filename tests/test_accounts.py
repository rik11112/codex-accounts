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
if sys.argv[1:] == ["logout"]:
    with (home / "daemon-call-order").open("a") as log:
        log.write("logout\\n")
    (home / "logout-events").write_text((home / "auth.json").read_text())
    if os.environ.get("FAKE_LOGOUT_FAILURE"):
        print("private-logout-output", file=sys.stderr)
        sys.exit(1)
    (home / "auth.json").unlink()
    print("Successfully logged out")
    sys.exit(0)
assert sys.argv[1:3] == ["app-server", "daemon"]
assert sys.argv[3] in {"stop", "start"}
with (home / "daemon-call-order").open("a") as log:
    log.write(sys.argv[3] + "\\n")
if sys.argv[3] == "start":
    (home / "daemon-start-events").write_text((home / "auth.json").read_text())
    if os.environ.get("FAKE_START_FAILURE"):
        print("private-start-output", file=sys.stderr)
        sys.exit(1)
    print(os.environ.get("FAKE_START_RESULT", '{"status":"started"}'))
    sys.exit(0)
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
        self.assertIn("disconnect while the daemon restarts", result.stderr)
        self.assertEqual(json.loads((self.home / "daemon-start-events").read_text()), self.after)
        self.assertEqual((self.home / "daemon-call-order").read_text().splitlines(), ["stop", "start"])
        self.assertEqual((self.home / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_shutdown_failure_keeps_current_and_saved_credentials(self):
        result = self.run_cli("switch", "after", "--yes", FAKE_FAILURE="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)
        self.assertNotIn("private-cli-output", result.stderr)
        self.assertFalse((self.home / "daemon-start-events").exists())

    def test_startup_failure_keeps_selected_credentials_and_explains_recovery(self):
        result = self.run_cli("switch", "after", "--yes", FAKE_START_FAILURE="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.after)
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)
        self.assertIn("Credentials switched to 'after'", result.stderr)
        self.assertIn("codex app-server daemon start", result.stderr)
        self.assertNotIn("private-start-output", result.stderr)

    def test_unconfirmed_startup_reports_selected_credentials(self):
        for output in ('{"status":"stopped"}', 'not-json', '[]', '{"status":[]}'):
            with self.subTest(output=output):
                result = self.run_cli("switch", "after", "--yes", FAKE_START_RESULT=output)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.after)
                self.assertIn("Credentials switched to 'after'", result.stderr)

    def test_unknown_or_malformed_shutdown_status_aborts(self):
        for output in ('{"status":"running"}', 'not-json', '[]', '{}', '{"status":[]}'):
            with self.subTest(output=output):
                result = self.run_cli("sw", "after", "-y", FAKE_RESULT=output)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_stopped_daemon_is_not_required_to_switch(self):
        result = self.run_cli("sw", "after", "-y", FAKE_RESULT='{"status":"notRunning"}')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_switch_force_alias_and_third_output_line(self):
        for flag in ("-y", "-f", "--yes", "--force"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    ["python3", str(SCRIPT), "sw", "after", flag], env=self.env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                lines = result.stdout.splitlines()
                self.assertEqual(len(lines), 3)
                self.assertEqual(lines[2], "Recommended: restart any running Codex sessions.")

    def test_logout_stops_then_clears_credentials_before_codex_logout(self):
        result = self.run_cli("logout", "-y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "daemon-call-order").read_text().splitlines(), ["stop", "logout"])
        self.assertEqual(json.loads((self.home / "daemon-events").read_text()), self.before)
        self.assertEqual(json.loads((self.home / "logout-events").read_text()), {})
        self.assertFalse((self.home / "auth.json").exists())
        self.assertFalse((self.home / "daemon-start-events").exists())
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)
        self.assertEqual(json.loads((self.home / "accounts/after.json").read_text()), self.after)

    def test_logout_preserves_refreshed_saved_credentials(self):
        result = self.run_cli("logout", "-f", FAKE_REFRESH="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads((self.home / "accounts/before.json").read_text())
        self.assertEqual(saved["tokens"]["access_token"], "refreshed-during-stop")
        self.assertEqual(json.loads((self.home / "logout-events").read_text()), {})

    def test_logout_shutdown_failure_leaves_credentials_unchanged(self):
        result = self.run_cli("logout", "-y", FAKE_FAILURE="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)
        self.assertFalse((self.home / "logout-events").exists())

    def test_logout_failure_preserves_saved_account_and_cleared_current_auth(self):
        result = self.run_cli("logout", "-y", FAKE_LOGOUT_FAILURE="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), {})
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)
        self.assertIn("Saved accounts are preserved", result.stderr)
        self.assertNotIn("private-logout-output", result.stderr)
        self.assertEqual(self.run_cli("logout", "-y").returncode, 0)

    def test_logout_without_confirmation_does_not_stop_or_clear_auth(self):
        result = self.run_cli("logout")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "daemon-events").exists())
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_logout_cancel_has_no_effect(self):
        args = mock.Mock(yes=False)
        with mock.patch.object(accounts.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(accounts, "stop_daemon") as stop:
            accounts.logout(self.home, args)
        stop.assert_not_called()
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_logout_requires_file_storage(self):
        (self.home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        result = self.run_cli("logout", "-y")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "daemon-events").exists())

    def test_logout_with_no_current_auth_leaves_saved_accounts_untouched(self):
        (self.home / "auth.json").unlink()
        result = self.run_cli("logout", "-y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.home / "logout-events").read_text()), {})
        self.assertEqual(json.loads((self.home / "accounts/before.json").read_text()), self.before)

    def test_logout_does_not_run_if_credential_clear_fails(self):
        args = mock.Mock(yes=True)
        with mock.patch.object(accounts, "stop_daemon"), \
                mock.patch.object(accounts, "write_auth", side_effect=PermissionError()), \
                mock.patch.object(accounts.subprocess, "run") as command:
            with self.assertRaises(PermissionError):
                accounts.logout(self.home, args)
        command.assert_not_called()
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_noninteractive_switch_requires_explicit_yes(self):
        result = self.run_cli("switch", "after")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--yes", result.stderr)
        self.assertFalse((self.home / "daemon-events").exists())
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_settings_default_to_false_without_creating_file(self):
        result = self.run_cli("config", "no-switch-confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "no-switch-confirm: false")
        self.assertFalse((self.home / "codex-accounts.json").exists())
        self.assertFalse((self.home / "daemon-events").exists())

    def test_enabled_setting_switches_without_flags_and_keeps_messages(self):
        configured = self.run_cli("config", "no-switch-confirm", "true")
        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertEqual(json.loads((self.home / "codex-accounts.json").read_text()), {"no-switch-confirm": True})
        result = subprocess.run(
            ["python3", str(SCRIPT), "sw", "after"], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.after)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("Warning:", lines[0])
        self.assertEqual(lines[2], "Recommended: restart any running Codex sessions.")

    def test_disabling_setting_restores_confirmation(self):
        self.assertEqual(self.run_cli("config", "no-switch-confirm", "true").returncode, 0)
        result = self.run_cli("config", "no-switch-confirm", "false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_cli("config").stdout.strip(), "no-switch-confirm: false")
        result = self.run_cli("sw", "after")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "daemon-events").exists())
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)
        self.assertEqual(self.run_cli("sw", "after", "-f").returncode, 0)

    def test_missing_setting_defaults_to_false_and_other_settings_are_preserved(self):
        path = self.home / "codex-accounts.json"
        self.write(path, {"other-setting": "preserved"})
        self.assertEqual(self.run_cli("config").stdout.strip(), "no-switch-confirm: false")
        self.assertEqual(self.run_cli("config", "no-switch-confirm", "true").returncode, 0)
        self.assertEqual(json.loads(path.read_text()), {"no-switch-confirm": True, "other-setting": "preserved"})

    def test_switch_setting_does_not_bypass_logout_confirmation(self):
        self.assertEqual(self.run_cli("config", "no-switch-confirm", "true").returncode, 0)
        result = self.run_cli("logout")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "daemon-events").exists())
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_invalid_settings_do_not_silently_bypass_confirmation(self):
        path = self.home / "codex-accounts.json"
        for raw in ('invalid-json', '[]', '{"no-switch-confirm":"false"}', '{"no-switch-confirm":1}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                result = self.run_cli("sw", "after")
                self.assertEqual(result.returncode, 1)
                self.assertFalse((self.home / "daemon-events").exists())
                self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.before)

    def test_invalid_setting_name_or_value_does_not_write_config(self):
        for arguments in (("unknown", "true"), ("no-switch-confirm", "yes")):
            with self.subTest(arguments=arguments):
                result = self.run_cli("config", *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.home / "codex-accounts.json").exists())

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

    def test_startup_timeout_reports_switched_credentials(self):
        args = mock.Mock(yes=True)
        args.name = "after"
        with mock.patch.object(accounts, "stop_daemon"), \
                mock.patch.object(accounts.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 60)):
            with self.assertRaisesRegex(accounts.AccountError, "Credentials switched to 'after'.*startup timed out"):
                accounts.switch(self.home, args)
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.after)

    def test_startup_execution_failure_reports_switched_credentials(self):
        args = mock.Mock(yes=True)
        args.name = "after"
        for failure in (FileNotFoundError(), PermissionError()):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(accounts, "stop_daemon"), \
                        mock.patch.object(accounts.subprocess, "run", side_effect=failure):
                    with self.assertRaisesRegex(accounts.AccountError, "Credentials switched to 'after'"):
                        accounts.switch(self.home, args)
                self.assertEqual(json.loads((self.home / "auth.json").read_text()), self.after)


if __name__ == "__main__":
    unittest.main()
