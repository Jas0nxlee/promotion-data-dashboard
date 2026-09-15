# 平台直采开发与隔离测试

本分支为迁移开发版。采集公共层、入口改造和隔离测试已实现；真实账号后台工作流需要在授权登录后逐项校准。不要直接用本分支替换生产调度器。

## 已实现与待接入

| 能力 | 当前实现状态 | 真实平台验收 |
| --- | --- | --- |
| 视频、图文、评论及小时级发现的统一 provider | 已接入，运行代码不再含 TikHub 请求路径或 Key 依赖 | 已验证缺少配置时保留历史并明确失败 |
| 浏览器会话、身份校验、响应监听、自动翻页、字段映射 | 已实现；真实 Chromium 本地响应夹具测试通过 | 14 个账号尚未绑定已核验的完整后台流程 |
| B站公开视频详情 | 原生 HTTP 适配器，校验 BV 与作者 ID | 望获OS 一个实际视频详情读取成功 |
| 公众号官方发布列表 | 已实现分页、多图文展开、历史 ID 映射与权限错误处理 | 未提供该账号官方令牌，尚未实测 |
| 公众号新每日阅读统计 | 提供单独查询方法，保存阅读人数与延迟标记 | 未接入常规调度；不混入累计阅读次数 |
| 抖音、小红书、知乎、视频号、公众号后台 | 可使用统一浏览器执行器和显式字段映射 | 需登录确认各后台响应路径、字段、翻页操作和角色权限；当前没有可声称即插即用的已验证配方 |
| 评论迁移保护 | 新旧 ID 未确认时阻止扫描推进与提醒 | 需真实新旧评论对账后配置兼容标记；尚未实现不同 ID 体系的自动映射迁移 |
| 自动下载平台报表、官方令牌自动续期 | 未实现 | 需账号登录后确定导出入口及授权管理方式 |
| 连续运行观察 | 未开始 | 平台接入完成后再进行至少 7 天观察 |

## 1. 本地测试

在本工作树安装依赖，不修改全局 Python：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python scripts/test_local.py
```

浏览器测试使用本机 Chrome 的独立临时上下文；所有测试网页请求被夹具接管，不读取用户的默认 Chrome 登录态。测试运行器默认禁止 requests 的外部 HTTP 和真实 SMTP，并检查受版本管理文件的内容是否改变。测试中的“已发送”日志来自 FakeSmtp，不代表真实发送。

手动运行测试采集时先指定独立输出根目录：

```bash
export PROMOTION_RUNTIME_DIR="$PWD/.runtime/local"
export PROMOTION_TEST_MODE=1
export PROMOTION_SESSION_DIR="$PWD/.runtime/sessions"
export PROMOTION_PROVIDER_CONFIG="$PWD/.runtime/providers.json"
.venv/bin/python pipeline/provider_setup.py status
```

测试模式不读取 `.env`，发送器禁止建立 SMTP 连接，调度器跳过发送。测试模式本身允许明确发起的平台只读采集，不等于断网；完全离线验证使用上面的 `scripts/test_local.py`。

测试模式没有独立运行目录时会拒绝采集入口。快照、网页数据、评论状态和队列均写入 `PROMOTION_RUNTIME_DIR`；配置中的账号名单仍只读使用本工作树 `config/`。

`--out` 用于改变主 JSON 输出；如也不希望写网页快照，视频、图文入口均加 `--no-publish-web`。不要从主目录启动本分支脚本，不要复用生产状态目录。

## 2. 账号登录

```bash
.venv/bin/python pipeline/provider_setup.py login --account 'bilibili:望获OS'
```

会话按账号 key 的哈希分别保存在不入库的目录。运营人员在专用浏览器扫码完成后回车保存；保存成功不等于已核验账号身份，也不等于采集已完整接通。非 B站账号可从 `status` 的输出选择。

运行阶段每次先读取配置的 profile 工作流，并将返回 ID 与 `profile.expected_id` 比对。会话被其他账号替换时停止采集。同一账号的同时执行由文件锁拒绝。

`cdp_url` 仅供本地调试，并只接受回环地址；必须指向该账号专用的测试 Chrome，不能填写日常浏览器或主项目使用的端口。普通部署使用各自持久化 profile，不设置 `cdp_url`。

## 3. 浏览器工作流配置

`config/providers.example.json` 只是结构示例，含占位值，不能视为已验证的 B站后台适配。实际配置保存到 `.runtime/providers.json` 并设置文件权限 600，避免把浏览器环境和账号绑定写入仓库。

每个账号配置包含：

- `provider=browser`、`profile.expected_id/id_path/name_path/followers_path`。
- `workflows.profile/contents/comments/replies`：入口 URL、观察到的准确响应路径、记录字段路径、结束条件和翻页选择器。文字操作应限定在读取/翻页范围，不配置发布、回复或删除动作。
- `content_mapping.fields`：稳定 ID、标题、永久链接、发布时间、封面等源字段路径；`content_mapping.stats` 对应指标；每个非空统计指标必须同时有 `definitions`，区分累计次数、人数、日统计等口径。
- `comment_mapping/reply_mapping`：ID、作者、正文、时间、回复数量与父级字段。需要完整二级回复时，未知回复数会使扫描失败。
- `comment_identity_compatible`：只有真实旧快照/旧基线与新响应的评论 ID 核验一致后才能置为 true。否则明确返回待迁移，不偷偷重建基线或发历史提醒。

工作流可以通过 `open_selectors` 进入数据标签，`next_selector` 翻页，或 `scroll=true` 滚动触发加载。只读取页面正常产生的 JSON 响应，支持 `has_more_path` 或 `total_path + row_id_path` 两类结束条件。缺少终止标记、空页却报告更多、重复页面或超限均不会被当作完整成功。

分页得到部分作品时保留旧记录；新响应缺少已有指标或发布时间时，保留其旧值并记录 `cached_fields`、字段级缓存来源。异常及过期账号在 CLI 输出完成后返回状态码 2，供调度器识别未完整刷新。

## 4. 公众号官方接口配置

```json
{
  "accounts": {
    "wechat_service:你的账号名": {
      "provider": "wechat_official",
      "bound_account_key": "wechat_service:你的账号名",
      "access_token_env": "WECHAT_OWN_ACCOUNT_ACCESS_TOKEN",
      "history_scope_verified": false
    }
  }
}
```

令牌应来自该账号已完成绑定的授权服务，通过进程环境提供；此版本未实现令牌自动刷新。不能把别的公众号令牌填入同一个绑定。认证、权限与发布清单范围仍由微信实际返回决定。完成与后台历史和图片消息的对账前，不将 `history_scope_verified` 设为 true。

发布列表无法从 URL 解析旧文章主键时停止覆盖，避免同文换 ID 重复入库。官方素材更新时间不写成首次发布时间。每日阅读人数由 `daily_readers(day)` 单独返回，不能直接作为大屏累计阅读量。

## 5. 只读实测

```bash
.venv/bin/python pipeline/provider_setup.py probe-bilibili \
  --account 'bilibili:望获OS' --bvid BV19R8i6KEQZ \
  --output .runtime/probes/bilibili-detail.json

.venv/bin/python pipeline/provider_setup.py probe \
  --account 'bilibili:望获OS' --max-pages 2 \
  --output .runtime/probes/bilibili-browser.json
```

`probe` 只写指定诊断文件，不更新大屏、评论基线或提醒队列；两页上限可能得到 `complete=false`，不能视为全量验证通过。第一次命令只验证一个公开视频详情，不验证作品发现、评论或账号后台权限。

## 6. Docker 与主干隔离

本轮未启动或重建任何 Docker 服务。常规 `docker-compose.yml` 的固定容器名、镜像标签和端口可能与原项目冲突，不能用于工作树测试。

新增 `docker-compose.local.yml` 是独立配置文件，使用唯一开发项目名、回环测试端口、私有运行目录和一次性命令，不加载生产 `.env`，不启动调度器：

```bash
PROMOTION_DEV_ID=promotion-dev-9a6c docker compose -f docker-compose.local.yml config
```

每个工作树使用不同的 `PROMOTION_DEV_ID` 与 `PROMOTION_DEV_PORT`。本轮仅提供配置，未实际部署容器。浏览器登录 profile 跨 macOS/Linux 不保证可搬运，容器登录流程需另行验证。

当前分支从原基线独立开发；主干同期已经加入官方回复时间线。后续集成需在本分支合并或重放主干更新并重新验收回复时间线，不能直接覆盖主干新版评论模块。
