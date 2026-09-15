import json
from runtime import PROVIDER_CONFIG
from .base import ProviderError
from .mapped import MappedBrowserProvider
from .bilibili import BilibiliProvider
from .wechat_mp import WeChatOfficialProvider
from .bilibili_creator import BilibiliCreatorProvider
from .wechat_channels import WeChatChannelsProvider
from .xiaohongshu import XiaohongshuProvider
from api_budget import ApiBudget

PLATFORMS = {"bilibili", "douyin", "wechat_channels", "xiaohongshu", "zhihu", "wechat_service", "wechat_subscription"}


class ProviderRegistry:
    def __init__(self, config=None):
        if config is None:
            try:
                config = json.loads(PROVIDER_CONFIG.read_text())
            except FileNotFoundError:
                config = {"accounts": {}}
            except (ValueError, OSError):
                raise ProviderError("invalid_config", "无法读取 provider 配置") from None
        if not isinstance(config, dict) or not isinstance(config.get("accounts", {}), dict):
            raise ProviderError("invalid_config", "provider 配置必须为含 accounts 映射的对象")
        self.config = config
        self.providers = {}

    @property
    def call_count(self):
        return sum(p.call_count for p in self.providers.values())

    def get(self, account):
        platform = account["platform"]
        if platform not in PLATFORMS:
            raise ProviderError("unsupported", "此平台继续使用原公开采集器")
        key = f"{platform}:{account['account_name']}"
        if key not in self.providers:
            settings = self.config.get("accounts", {}).get(key)
            if not settings:
                raise ProviderError("setup_required", "账号尚未绑定已核验的后台采集配置，请运行 provider_setup status")
            kind = settings.get("provider", "browser")
            if kind == "xiaohongshu_creator" and platform == "xiaohongshu":
                self.providers[key] = XiaohongshuProvider(account, settings)
                return self.providers[key]
            if kind == "wechat_channels_creator" and platform == "wechat_channels":
                self.providers[key] = WeChatChannelsProvider(account, settings)
                return self.providers[key]
            if kind == "bilibili_creator" and platform == "bilibili":
                self.providers[key] = BilibiliCreatorProvider(account, settings)
                return self.providers[key]
            if kind == "wechat_official" and platform in {"wechat_service", "wechat_subscription"}:
                self.providers[key] = WeChatOfficialProvider(account, settings)
                return self.providers[key]
            if kind != "browser":
                raise ProviderError("unsupported", "未知数据提供方式")
            provider = BilibiliProvider if platform == "bilibili" else MappedBrowserProvider
            self.providers[key] = provider(account, settings)
        return self.providers[key]

    def fetch_comments(self, platform, item, max_pages=200, include_replies=True):
        return self.get(item).comments(item, max_pages, include_replies)

    def discover(self, account):
        return self.get(account).collect(discovery=True)

    def fetch_roots(self, item, max_pages=200):
        comments, stats = self.get(item).comments(item, max_pages, False)
        return comments, stats["root_pages"]

    def fetch_replies(self, item, root_id, max_pages=200):
        return self.get(item).replies(item, root_id, max_pages)

    def usage_snapshot(self):
        return {**ApiBudget().snapshot(), "source": "platform_direct", "scope": "day",
                "unit": "direct_http_or_browser_action"}
