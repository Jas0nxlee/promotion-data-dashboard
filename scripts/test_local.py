#!/usr/bin/env python3
"""Run isolated local tests with HTTP/SMTP transports denied by default."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def tracked_hashes():
    import subprocess
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
            for f in files if f and (ROOT / f).is_file()}


def main():
    before = tracked_hashes()
    with tempfile.TemporaryDirectory(prefix="promotion-tests-") as tmp:
        os.environ.update({"PROMOTION_RUNTIME_DIR": tmp, "PROMOTION_TEST_MODE": "1",
                           "PROMOTION_SESSION_DIR": str(Path(tmp) / "sessions"),
                           "PROMOTION_PROVIDER_CONFIG": str(Path(tmp) / "providers.json")})
        # Block HTTP and real email even if a test accidentally enters an old path.
        with patch("requests.sessions.Session.request", side_effect=AssertionError("测试禁止外部 HTTP")), \
             patch("smtplib.SMTP", side_effect=AssertionError("测试禁止真实 SMTP")), \
             patch("smtplib.SMTP_SSL", side_effect=AssertionError("测试禁止真实 SMTP SSL")):
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    after = tracked_hashes()
    changed = [f for f, h in before.items() if after.get(f) != h]
    if changed:
        print("测试修改了受版本管理的文件：", changed, file=sys.stderr)
    print("隔离验证：受版本管理文件未变化" if not changed else "隔离验证失败")
    raise SystemExit(0 if result.wasSuccessful() and not changed else 1)


if __name__ == "__main__":
    main()
