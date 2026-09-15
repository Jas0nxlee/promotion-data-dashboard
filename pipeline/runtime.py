"""Separate mutable runtime files from source and from other worktrees."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ.get("PROMOTION_RUNTIME_DIR", str(ROOT))).expanduser().resolve()
DATA = RUNTIME / "data"
WEB = RUNTIME / "web"
SESSIONS = Path(os.environ.get("PROMOTION_SESSION_DIR", str(ROOT / ".runtime" / "sessions"))).expanduser().resolve()
PROVIDER_CONFIG = Path(os.environ.get("PROMOTION_PROVIDER_CONFIG", str(ROOT / ".runtime" / "providers.json"))).expanduser().resolve()

if os.environ.get("PROMOTION_TEST_MODE") == "1" and RUNTIME == ROOT:
    raise RuntimeError("测试模式必须设置独立 PROMOTION_RUNTIME_DIR，禁止写入工作树快照")


def is_test():
    return os.environ.get("PROMOTION_TEST_MODE") == "1"


def load_env():
    # Test processes must never inherit credentials from a checkout .env file.
    if is_test():
        if RUNTIME == ROOT:
            raise RuntimeError("测试模式必须设置独立 PROMOTION_RUNTIME_DIR")
        return
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))
