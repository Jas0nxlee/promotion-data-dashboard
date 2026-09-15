"""Read page-generated JSON with account-scoped persistent browser sessions.

Recipes describe observed read-only workflows, never executable JavaScript.
Unknown paging/identity is an error, not an empty successful collection.
"""
import hashlib
import json
import os
import time
from contextlib import contextmanager
from urllib.parse import urlparse, quote
import fcntl

from runtime import SESSIONS
from .base import ProviderError, Pages, pick, number


HOSTS = {
    "bilibili": ("bilibili.com",), "douyin": ("douyin.com",),
    "xiaohongshu": ("xiaohongshu.com",), "zhihu": ("zhihu.com",),
    "wechat_channels": ("channels.weixin.qq.com",),
    "wechat_service": ("mp.weixin.qq.com",),
    "wechat_subscription": ("mp.weixin.qq.com",),
}


def session_key(account_key):
    return hashlib.sha256(account_key.encode()).hexdigest()[:24]


def allowed(url, platform):
    p = urlparse(url)
    return p.scheme == "https" and any(p.hostname == h or (p.hostname or "").endswith("." + h)
                                       for h in HOSTS[platform])


def expand(template, values):
    try:
        if template in ("{content_url}", "{profile_url}"):
            # A complete observed URL is validated by allowed(), not embedded as a query value.
            return str(values[template[1:-1]])
        return template.format_map({k: quote(str(v), safe="") for k, v in values.items()})
    except KeyError as exc:
        raise ProviderError("setup_required", f"采集配置缺少变量 {exc.args[0]}") from None


@contextmanager
def account_lock(key):
    SESSIONS.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = SESSIONS / (session_key(key) + ".lock")
    with path.open("a") as f:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProviderError("session_busy", "该账号会话正在执行其他任务") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class BrowserSource:
    def __init__(self, account, settings):
        self.account = account
        self.settings = settings
        self.key = f"{account['platform']}:{account['account_name']}"
        self.call_count = 0
        self.context = None

    @contextmanager
    def session(self):
        with account_lock(self.key):
            from playwright.sync_api import sync_playwright
            driver = sync_playwright().start()
            context = None
            connected = False
            try:
                cdp = self.settings.get("cdp_url")
                if cdp:
                    parsed = urlparse(cdp)
                    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
                        raise ProviderError("invalid_session", "调试浏览器只允许本机回环连接")
                    browser = driver.chromium.connect_over_cdp(cdp, timeout=15000)
                    if not browser.contexts:
                        raise ProviderError("session_expired", "测试浏览器没有可用会话")
                    context, connected = browser.contexts[0], True
                else:
                    profile = SESSIONS / session_key(self.key)
                    if not profile.is_dir():
                        raise ProviderError("session_expired", "尚未建立独立登录会话，请运行 provider_setup login")
                    context = driver.chromium.launch_persistent_context(
                        str(profile), headless=not self.settings.get("headed", False),
                        channel=self.settings.get("channel", "chromium"),
                        locale="zh-CN", timezone_id="Asia/Shanghai")
                self.context = context
                yield self
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError("browser_error", f"浏览器操作失败（{type(exc).__name__}），请检查会话及采集配置") from None
            finally:
                self.context = None
                if context is not None and not connected:
                    context.close()
                driver.stop()

    def pages(self, name, values=None, max_pages=200, allow_partial=False):
        recipe = self.settings.get("workflows", {}).get(name)
        if not recipe:
            raise ProviderError("setup_required", f"尚未核验并配置 {name} 采集流程")
        values = {**self.account, **self.settings.get("variables", {}), **(values or {})}
        url = expand(recipe["url"], values)
        if not allowed(url, self.account["platform"]):
            raise ProviderError("invalid_host", "页面 URL 不属于当前账号的平台")
        pattern = expand(recipe.get("response_path", ""), values)
        if not pattern.startswith("/"):
            raise ProviderError("setup_required", "必须配置核验过的响应路径")
        page = self.context.new_page()
        pending = []
        failures = []
        timeout = min(60000, max(1000, int(recipe.get("timeout_ms", 20000))))

        def receive(response):
            if not allowed(response.url, self.account["platform"]):
                return
            if urlparse(response.url).path != pattern:
                return
            self.call_count += 1
            if response.status != 200:
                failures.append("平台响应未成功")
                return
            try:
                # Python JSON retains arbitrarily large integer IDs.
                pending.append(json.loads(response.body()))
            except Exception:
                failures.append("匹配的响应不是有效 JSON")

        page.on("response", receive)
        rows, envelopes, fingerprints = [], [], set()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            for selector in recipe.get("open_selectors", []):
                page.locator(selector).click(timeout=timeout)
            for index in range(max(1, max_pages)):
                until = time.monotonic() + timeout / 1000
                while not pending and not failures and time.monotonic() < until:
                    if "login" in urlparse(page.url).path.lower() or "passport" in (urlparse(page.url).hostname or ""):
                        raise ProviderError("session_expired", "页面已进入登录流程")
                    page.wait_for_timeout(100)
                if failures:
                    raise ProviderError("page_error", failures[0])
                if not pending:
                    raise ProviderError("schema_changed", "未观察到配置的页面数据响应")
                payload = pending.pop(0)
                code = pick(payload, recipe.get("code_path", "code"))
                if code is not None and code not in recipe.get("success_codes", [0, 200, "0", "200"]):
                    raise ProviderError("platform_error", "平台响应状态不成功")
                page_rows = pick(payload, recipe.get("rows_path", "data"))
                if recipe.get("single") and isinstance(page_rows, dict):
                    page_rows = [page_rows]
                if not isinstance(page_rows, list) or any(not isinstance(r, dict) for r in page_rows):
                    raise ProviderError("schema_changed", "记录字段缺失或不再是对象数组")
                fp = hashlib.sha256(json.dumps(page_rows, sort_keys=True).encode()).hexdigest()
                if fp in fingerprints:
                    raise ProviderError("incomplete_pagination", "返回重复页面，本轮不推进状态")
                fingerprints.add(fp)
                rows.extend(page_rows)
                envelopes.append(payload)
                end = False
                if recipe.get("single"):
                    end = True
                elif recipe.get("has_more_path"):
                    more = pick(payload, recipe["has_more_path"])
                    if more not in (True, False, 0, 1, "0", "1"):
                        raise ProviderError("schema_changed", "分页结束标记缺失或无效")
                    end = more in (False, 0, "0")
                elif recipe.get("total_path"):
                    total = number(pick(payload, recipe["total_path"]))
                    if total is None:
                        raise ProviderError("schema_changed", "总数缺失")
                    id_path = recipe.get("row_id_path")
                    if not id_path:
                        raise ProviderError("setup_required", "按总数判断结束必须配置 row_id_path")
                    ids = [pick(r, id_path) for r in rows]
                    if any(v in (None, "") for v in ids):
                        raise ProviderError("schema_changed", "分页记录缺少 ID")
                    end = len(set(str(v) for v in ids)) >= total
                else:
                    raise ProviderError("setup_required", "缺少可靠的分页终止条件")
                if end:
                    return Pages(rows, len(envelopes), True, envelopes=envelopes)
                if not page_rows:
                    raise ProviderError("incomplete_pagination", "空页面仍报告有后续内容")
                if index + 1 >= max_pages:
                    if allow_partial:
                        return Pages(rows, len(envelopes), False, "达到分页上限，保留历史并标记部分覆盖", envelopes)
                    raise ProviderError("incomplete_pagination", "达到分页上限，本轮不推进评论状态")
                if recipe.get("next_selector"):
                    page.locator(recipe["next_selector"]).click(timeout=timeout)
                elif recipe.get("scroll"):
                    page.mouse.wheel(0, 1600)
                else:
                    raise ProviderError("setup_required", "存在下一页但未配置翻页操作")
            raise ProviderError("incomplete_pagination", "分页未完成")
        finally:
            page.close()
