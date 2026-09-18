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

from runtime import SESSIONS, DATA
from .base import ProviderError, Pages, pick, number
from .authorization import AuthorizationStore, AUTHENTICATION_ERRORS
from api_budget import ApiBudget, ApiBudgetExceeded


HOSTS = {
    "bilibili": ("bilibili.com",), "douyin": ("douyin.com",),
    "xiaohongshu": ("xiaohongshu.com",), "zhihu": ("zhihu.com",),
    "baijiahao": ("baidu.com",),
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
        self.budget = ApiBudget(default_task="browser_action")
        self.last_request = 0.0

    @contextmanager
    def session(self):
        authorization = AuthorizationStore(SESSIONS / "authorization")
        bypass_authorization = os.environ.get("PROMOTION_AUTHORIZATION_OPERATION") == "1"
        if not bypass_authorization:
            authorization.guard(self.key)
        with account_lock(self.key):
            if not bypass_authorization:
                authorization.guard(self.key)
            from playwright.sync_api import sync_playwright
            driver = sync_playwright().start()
            context = None
            connected = False
            launched_browser = None
            try:
                # Portable collectors run without taking focus from a user's login
                # window. Onboarding/export explicitly selects interactive mode.
                cdp = self.settings.get("cdp_url") if self.settings.get("session_mode") != "portable" else None
                if cdp:
                    parsed = urlparse(cdp)
                    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
                        raise ProviderError("invalid_session", "调试浏览器只允许本机回环连接")
                    try:
                        browser = driver.chromium.connect_over_cdp(cdp, timeout=3000)
                    except Exception:
                        browser = None
                    if browser is not None:
                        if not browser.contexts:
                            raise ProviderError("session_expired", "测试浏览器没有可用会话")
                        context, connected = browser.contexts[0], True
                if context is None:
                    # Deployment images can select their installed browser while
                    # keeping portable account settings compatible with local Chrome.
                    launch_channel = (os.environ.get("PROMOTION_BROWSER_CHANNEL", "").strip()
                                      or self.settings.get("channel", "chromium"))
                    portable = SESSIONS / (session_key(self.key) + ".storage.json")
                    profile = SESSIONS / session_key(self.key)
                    if self.settings.get("session_mode") == "portable":
                        if not portable.is_file():
                            raise ProviderError("session_expired", "缺少导出的可移植会话，请先运行 export-session")
                        launched_browser = driver.chromium.launch(headless=not self.settings.get("headed", False), channel=launch_channel)
                        context = launched_browser.new_context(storage_state=str(portable), locale="zh-CN", timezone_id="Asia/Shanghai")
                    elif not profile.is_dir():
                        raise ProviderError("session_expired", "尚未建立独立登录会话，请运行 provider_setup login")
                    else:
                        context = driver.chromium.launch_persistent_context(
                            str(profile), headless=not self.settings.get("headed", False),
                            channel=launch_channel,
                            ignore_default_args=["--password-store=basic", "--use-mock-keychain"],
                            locale="zh-CN", timezone_id="Asia/Shanghai")
                self.context = context
                yield self
                if self.settings.get("session_mode") == "portable":
                    self.export_session()
            except ProviderError as exc:
                if (exc.reason in AUTHENTICATION_ERRORS
                        and getattr(exc, "authorization_guard", False) is not True
                        and getattr(exc, "authorization_error_recorded", False) is not True):
                    state = authorization.require_reauthorization(self.key, exc.reason, str(exc))
                    # This transition happens while the account lock is held.
                    # Downstream health writers must not repeat it after a newer
                    # authorization operation has replaced the failed session.
                    exc.authorization_error_recorded = True
                    exc.authorization_operation_id = state.get("operation_id")
                raise
            except ApiBudgetExceeded:
                raise
            except Exception as exc:
                raise ProviderError("browser_error", f"浏览器操作失败（{type(exc).__name__}），请检查会话及采集配置") from None
            finally:
                self.context = None
                if context is not None and not connected:
                    context.close()
                if launched_browser is not None:
                    launched_browser.close()
                driver.stop()

    def export_session(self):
        """Export only this platform's state, after the caller verified identity."""
        from .credentials import private_json
        state = self.context.storage_state(indexed_db=True)
        state["cookies"] = [cookie for cookie in state.get("cookies", [])
                            if allowed("https://" + cookie.get("domain", "").lstrip("."), self.account["platform"])]
        state["origins"] = [origin for origin in state.get("origins", []) if allowed(origin.get("origin", ""), self.account["platform"])]
        path = SESSIONS / (session_key(self.key) + ".storage.json")
        private_json(path, state)
        return path

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
            if recipe.get("request_frame_name"):
                try:
                    if response.request.frame.name != recipe["request_frame_name"]:
                        return
                except Exception:
                    return
            if recipe.get("request_match"):
                try:
                    body = response.request.post_data_json
                except Exception:
                    return
                if any(pick(body, key) != value for key, value in recipe["request_match"].items()):
                    return
            if recipe.get("request_identity_path"):
                try:
                    body = response.request.post_data_json
                    actual = pick(body, recipe["request_identity_path"])
                except Exception:
                    actual = None
                if actual != recipe.get("request_identity_value"):
                    failures.append("请求中的账号身份与绑定不一致")
                    return
            self.call_count += 1
            if response.status not in recipe.get("success_http_statuses", [200]):
                failures.append(f"平台响应未成功 (HTTP {response.status})")
                return
            try:
                # Python JSON retains arbitrarily large integer IDs.
                pending.append(json.loads(response.body()))
            except Exception:
                failures.append("匹配的响应不是有效 JSON")

        page.on("response", receive)
        rows, envelopes, fingerprints = [], [], set()
        try:
            self.budget.consume(self.account["platform"] + ":" + name)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if recipe.get("foreground"):
                page.bring_to_front()
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
                    if os.environ.get("PROMOTION_CAPTURE_FAILURE") == "1":
                        folder = DATA / "debug"
                        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                        target = folder / (session_key(self.key) + "-page-failure.png")
                        try:
                            page.screenshot(path=str(target))
                            os.chmod(target, 0o600)
                        except Exception:
                            pass
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
                elif recipe.get("is_end_path"):
                    value = pick(payload, recipe["is_end_path"])
                    if value not in (True, False, 0, 1, "0", "1"):
                        raise ProviderError("schema_changed", "分页结束标记缺失或无效")
                    end = value in (True, 1, "1")
                elif recipe.get("has_more_path"):
                    more = pick(payload, recipe["has_more_path"])
                    if more not in (True, False, 0, 1, "0", "1"):
                        raise ProviderError("schema_changed", "分页结束标记缺失或无效")
                    end = more in (False, 0, "0")
                elif recipe.get("total_path"):
                    total = number(pick(payload, recipe["total_path"]))
                    if total is None and recipe.get("total_first_page") and envelopes:
                        total = number(pick(envelopes[0], recipe["total_path"]))
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
                    self.budget.consume(self.account["platform"] + ":" + name)
                    page.locator(recipe["next_selector"]).click(timeout=timeout)
                elif recipe.get("scroll"):
                    self.budget.consume(self.account["platform"] + ":" + name)
                    if recipe.get("scroll_container"):
                        page.locator(recipe["scroll_container"]).evaluate("el => { el.scrollTop = el.scrollHeight; }")
                    else:
                        page.mouse.wheel(0, 1600)
                else:
                    raise ProviderError("setup_required", "存在下一页但未配置翻页操作")
            raise ProviderError("incomplete_pagination", "分页未完成")
        finally:
            page.close()

    def export(self, name="export", values=None):
        from .exports import read_export
        import uuid
        recipe = self.settings.get("workflows", {}).get(name)
        if not recipe or not recipe.get("download_selector"):
            raise ProviderError("setup_required", "未配置后台导出步骤")
        url = expand(recipe["url"], {**self.account, **self.settings.get("variables", {}), **(values or {})})
        if not allowed(url, self.account["platform"]):
            raise ProviderError("invalid_host", "导出入口不属于当前平台")
        page = self.context.new_page()
        try:
            self.budget.consume(self.account["platform"] + ":export")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for selector in recipe.get("open_selectors", []):
                page.locator(selector).click(timeout=15000)
            expected = None
            if recipe.get("total_selector"):
                expected = number(page.locator(recipe["total_selector"]).inner_text())
            with page.expect_download(timeout=60000) as received:
                page.locator(recipe["download_selector"]).click(timeout=15000)
            download = received.value
            if download.failure():
                raise ProviderError("export_failed", "后台导出下载失败")
            from pathlib import Path
            suffix = Path(download.suggested_filename).suffix.lower()
            if suffix not in {".csv", ".xlsx"}:
                raise ProviderError("export_format", "后台未导出 CSV 或 XLSX")
            folder = DATA / "exports" / session_key(self.key)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = folder / (uuid.uuid4().hex + suffix)
            download.save_as(str(path))
            os.chmod(path, 0o600)
            rows = read_export(path, sheet=recipe.get("sheet"))
            complete = recipe.get("coverage") == "all_published" and expected is not None and len(rows) == expected
            self.call_count += 1
            return Pages(rows, 1, complete, "后台报表导出" if complete else "导出范围尚未证明覆盖全部作品，合并历史缓存")
        finally:
            page.close()

    def get_json(self, url, params=None, *, min_interval=0.2):
        """Read a verified native GET endpoint using this account's browser cookies."""
        if not allowed(url, self.account["platform"]):
            raise ProviderError("invalid_host", "接口不属于当前平台")
        self.budget.consume(self.account["platform"] + ":" + urlparse(url).path, task="platform_http")
        interval = max(0.2, float(self.settings.get("request_interval", 0.6)), min_interval)
        time.sleep(max(0, interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.call_count += 1
        response = self.context.request.get(url, params=params, timeout=25000, max_redirects=0)
        try:
            if response.status in (401, 403):
                raise ProviderError("session_expired", "平台会话已失效或无权访问")
            if response.status in (412, 429):
                raise ProviderError("rate_limited", "平台要求访问校验，停止本账号采集")
            if response.status != 200:
                raise ProviderError("http_error", f"平台返回 HTTP {response.status}")
            try:
                return json.loads(response.body())
            except ValueError:
                raise ProviderError("schema_changed", "原生接口未返回 JSON") from None
        finally:
            response.dispose()

    def post_channels_readonly(self, path, payload):
        allowed_paths = {
            "/micro/interaction/cgi-bin/mmfinderassistant-bin/comment/comment_list",
            "/micro/content/cgi-bin/mmfinderassistant-bin/post/post_list",
        }
        if self.account["platform"] != "wechat_channels" or path not in allowed_paths:
            raise ProviderError("invalid_operation", "未核验为只读的视频号接口，拒绝请求")
        self.budget.consume("wechat_channels:" + path, task="platform_http")
        interval = max(0.2, float(self.settings.get("request_interval", 0.6)))
        time.sleep(max(0, interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.call_count += 1
        response = self.context.request.post("https://channels.weixin.qq.com" + path, data=payload,
            headers={"Origin": "https://channels.weixin.qq.com", "Referer": "https://channels.weixin.qq.com/platform/interaction/comment"},
            timeout=25000, max_redirects=0)
        try:
            if response.status not in (200, 201):
                raise ProviderError("http_error", f"视频号读取返回 HTTP {response.status}")
            try:
                return json.loads(response.body())
            except ValueError:
                raise ProviderError("schema_changed", "视频号读取响应不是 JSON") from None
        finally:
            response.dispose()
