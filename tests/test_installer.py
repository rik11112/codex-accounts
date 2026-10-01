import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home with spaces"
        self.home.mkdir()
        (self.home / "accounts").mkdir()
        (self.home / "accounts/work.json").write_text("saved-credentials")
        (self.home / "auth.json").write_text("current-credentials")
        (self.home / "codex-accounts.json").write_text('{"no-switch-confirm":true}')
        (self.home / "codex-accounts").write_text("old executable")
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, sys
pathlib.Path(os.environ["CURL_LOG"]).write_text("\\n".join(sys.argv[1:]))
if os.environ.get("FAIL_DOWNLOAD"):
    pathlib.Path(sys.argv[-1]).write_text("partial download")
    sys.exit(22)
if os.environ.get("BAD_DOWNLOAD"):
    pathlib.Path(sys.argv[-1]).write_text("<html>not a script</html>")
else:
    shutil.copyfile(os.environ["DOWNLOAD_SOURCE"], sys.argv[-1])
''')
        curl.chmod(0o755)
        self.env = dict(os.environ, CODEX_HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        DOWNLOAD_SOURCE=str(ROOT / "codex-accounts"),
                        CURL_LOG=str(Path(self.tmp.name) / "curl-log"))

    def install(self, *args, **env):
        return subprocess.run(["bash", str(ROOT / "install.sh"), *args],
                              env=dict(self.env, **env), text=True, capture_output=True, timeout=10)

    def assert_accounts_untouched(self):
        self.assertEqual((self.home / "auth.json").read_text(), "current-credentials")
        self.assertEqual((self.home / "accounts/work.json").read_text(), "saved-credentials")
        self.assertEqual((self.home / "codex-accounts.json").read_text(), '{"no-switch-confirm":true}')
        self.assertEqual(list(self.home.glob(".codex-accounts-install.*")), [])

    def test_local_install_replaces_only_executable_without_backup(self):
        result = self.install("--local")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "codex-accounts").read_bytes(), (ROOT / "codex-accounts").read_bytes())
        self.assertEqual((self.home / "codex-accounts").stat().st_mode & 0o777, 0o755)
        self.assertEqual(set(p.name for p in self.home.iterdir()), {"auth.json", "accounts", "codex-accounts", "codex-accounts.json"})
        self.assert_accounts_untouched()

    def test_remote_install_downloads_from_pinned_release(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/v1.0.0/codex-accounts", Path(self.env["CURL_LOG"]).read_text())
        self.assert_accounts_untouched()

    def test_explicit_ref_can_install_main(self):
        result = self.install(CODEX_ACCOUNTS_REF="main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/main/codex-accounts", Path(self.env["CURL_LOG"]).read_text())

    def test_failed_and_invalid_downloads_keep_old_executable(self):
        for flag in ("FAIL_DOWNLOAD", "BAD_DOWNLOAD"):
            with self.subTest(flag=flag):
                result = self.install(**{flag: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.home / "codex-accounts").read_text(), "old executable")
                self.assert_accounts_untouched()


if __name__ == "__main__":
    unittest.main()
