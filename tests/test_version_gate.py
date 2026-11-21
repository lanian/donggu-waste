"""배포 기준과 비교할 때 PWA 변경에 새 캐시 버전을 요구하는지 검사한다."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VersionGateTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="dg-version-test-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / "web").mkdir()
        (self.root / "pc/dgpc").mkdir(parents=True)
        self.git("init", "-q")
        self.versions("0.14.33")
        (self.root / "web/module.js").write_text("export const old = true;", encoding="utf8")
        (self.root / "web/unused.css").touch()
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

    def versions(self, version):
        (self.root / "web/app.js").write_text(f"export const APP_VERSION = '{version}';", encoding="utf8")
        (self.root / "web/sw.js").write_text(f"const VERSION = '{version}';\nconst FILES = ['module.js'];", encoding="utf8")
        (self.root / "web/index.html").write_text("", encoding="utf8")
        (self.root / "pc/dgpc/__init__.py").write_text(f'__version__ = "{version}"', encoding="utf8")

    def check(self):
        env = {key: value for key, value in os.environ.items() if key not in ("GITHUB_REF_TYPE", "GITHUB_REF_NAME")}
        env["PYTHONUTF8"] = "1"
        return subprocess.run([sys.executable, str(ROOT / "tools/check_version.py"), "--root", str(self.root), "--base", self.base],
                              capture_output=True, encoding="utf-8", env=env)

    def test_old_mobile_version_rejects_modified_and_removed_assets(self):
        file = self.root / "web/module.js"
        file.write_text("export const current = true;", encoding="utf8")
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("서비스 워커 버전", result.stdout)
        self.git("restore", "web/module.js")
        (self.root / "web/unused.css").unlink()
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("서비스 워커 버전", result.stdout)

    def test_version_bump_accepts_changed_mobile_assets(self):
        (self.root / "web/module.js").write_text("export const current = true;", encoding="utf8")
        self.versions("0.14.34")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pc_only_change_does_not_require_a_mobile_bump(self):
        (self.root / "pc/dgpc/__init__.py").write_text('__version__ = "0.14.33"\n# PC change', encoding="utf8")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
