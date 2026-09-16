# 平台直采开发与隔离测试

本分支为迁移开发版。采集公共层、入口改造和隔离测试已实现；真实账号后台工作流需要在授权登录后逐项校准。不要直接用本分支替换生产调度器。

## 已实现与待接入

| 能力 | 当前实现状态 | 真实平台验收 |
| --- | --- | --- |
| 视频、图文、评论及小时级发现的统一 provider | 已接入，运行代码不再含 TikHub 请求路径或 Key 依赖 | 已验证缺少配置时保留历史并明确失败 |
| 浏览器会话、身份校验、响应监听、自动翻页、字段映射 | 已实现；真实 Chromium 本地响应夹具测试通过 | B站2账号、抖音2账号、视频号2账号、小红书安芯日记、知乎安芯工程师及公众号国科安芯已通过 |
| B站公开视频详情 | 原生 HTTP 适配器，校验 BV 与作者 ID | 望获OS已通过全量后台稿件、代表作品评论及回复、CLI与会话恢复验证 |
| 公众号官方发布列表 | 已实现分页、多图文展开、历史 ID 映射与权限错误处理 | 稳定令牌自动续期已实现；未提供实际账号凭证，尚未实测 |
| 公众号新每日阅读统计 | 提供单独查询方法，保存阅读人数与延迟标记 | 未接入常规调度；不混入累计阅读次数 |
| 小红书创作后台 | `xiaohongshu_creator` 内置页面配方、容器滚动、首屏总数与置顶去重 | 安芯日记62篇及五项指标、31条评论回复通过；创作后台和主站需分别登录 |
| 知乎创作后台 | `zhihu_creator`，文章与回答分开，身份前后核验和严格分页 | 安芯工程师32篇文章通过；土星云待正确账号登录 |
| 公众号后台 | `wechat_browser`，原始ID核验、发表记录分页、多图文展开、人数指标单列 | 国科安芯28篇文章通过，旧26篇全部覆盖；图片消息仅完成兼容测试，待运行时实测 |
| 抖音作品及主站评论 | `douyin_creator`，独立会话、完整作品游标、数字评论ID与回复分页 | 望获OS98条作品、55条评论回复；安芯Max14条作品、3条一级评论，样本无二级回复 |
| 评论迁移保护 | 新旧 ID 未确认时阻止扫描推进与提醒 | 需真实新旧评论对账后配置兼容标记；尚未实现不同 ID 体系的自动映射迁移 |
| 自动下载平台报表、官方令牌自动续期 | 通用导出与微信稳定令牌续期已实现并本地测试 | 真实导出按钮与账号凭证仍待接入 |
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

`config/providers.example.json` 提供已核验的 B站原生后台配置结构，使用 `provider=bilibili_creator`；仍需对应账号的登录会话。其他平台的通用 browser 配置须登录后校准。实际配置保存到 `.runtime/providers.json` 并设置文件权限 600，避免把浏览器环境和账号绑定写入仓库。

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
      "bound_platform_uid": "gh_your_verified_id",
      "app_id_env": "WECHAT_OWN_ACCOUNT_APP_ID",
      "app_secret_env": "WECHAT_OWN_ACCOUNT_APP_SECRET",
      "expected_app_id": "wx_your_verified_app_id",
      "history_scope_verified": false
    }
  }
}
```

先通过实际账号核验 AppID 与公众号原始ID的对应关系，再填写 `bound_account_key`、`bound_platform_uid` 和 `expected_app_id`。密钥通过进程环境提供，稳定令牌采用文件锁和私有缓存自动续期，不强制其他令牌失效。静态令牌目前不能独立证明所属账号，因此不会用于采集；未经身份核验的完整或部分记录均不进入快照。认证、权限与发布清单范围仍由微信实际返回决定。完成与后台历史和图片消息的对账前，不将 `history_scope_verified` 设为 true。

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

本分支已集成并回归主干的官方回复时间线。所有变更仍仅在独立分支，尚未反向合并到主干。


## 7. 本地接入面板和健康检查

```bash
PROMOTION_RUNTIME_DIR="$PWD/.runtime/local" PROMOTION_TEST_MODE=1 \
  .venv/bin/python pipeline/control_panel.py --port 18761
```

在本机打开 `http://127.0.0.1:18761/`。可逐账号打开登录窗口、查看配置和提交只读验证。面板不启动正式调度器，不发送邮件，不对外提供凭证。管理页面必须由控制面板服务提供，静态前端上的同名 HTML 不具备管理 API。

```bash
.venv/bin/python pipeline/provider_setup.py probe-comments \
  --account 'bilibili:望获OS' --content-id BV1YsY76REso --max-pages 20 \
  --output .runtime/probes/bilibili-comments.json
.venv/bin/python pipeline/healthcheck.py --require-ready --min-observation-days 7
```

健康检查按配置版本保留评论流程验证证据，账号身份和内容采集结果须在24小时内；正式采集会更新结果。观察期记录来自实际调度运行，没有足够跨度或存在账号未接入时返回非零状态，不能用修改日期或演示数据冒充完成。

调度器的数据和评论使用独立执行通道；失败15分钟后可重试，错过08:00的每日任务可在当天补跑。同一运行目录不允许重复启动调度器。预算单位为直接 HTTP 请求或一页浏览器采集，不等同于网页内部全部网络请求，也不代表第三方计费。保留的旧公开采集器不纳入这份 provider 预算。

## 8. 会话移植与部署

```bash
.venv/bin/python pipeline/provider_setup.py export-session --account 'bilibili:望获OS'
```

程序先验证身份，再仅导出该平台的 Cookies 和浏览器本地状态，文件权限为600。生产配置可设置 `session_mode=portable`、`channel=chromium`，并移除本机调试用的 `cdp_url`。镜像默认设置 `PROMOTION_BROWSER_CHANNEL=chromium`，覆盖本机账号保存的 Chrome 通道以匹配镜像已安装浏览器；本机未设置该变量时仍沿用账号配置。Compose 通过 `PROMOTION_PROVIDER_FILE` 选择配置文件，同时挂载私有会话目录，不将它们写入镜像。可移植会话已在本机的新浏览器上下文验证；跨机器、跨IP仍可能触发重新授权，必须先做目标环境只读测试。

微信稳定令牌规则已按[微信官方文档](https://developers.weixin.qq.com/doc/subscription/api/base/api_getstableaccesstoken.html)核对。实际开通权限、IP白名单和管理员确认仍需该账号完成。


## 9. 视频号原生适配

`provider=wechat_channels_creator` 使用真实后台读取接口，首次绑定要求首页短号与项目配置严格一致，包含大小写。完成登录后可执行 `bind-channels`，程序在会话捕获前后再次核验短号，只保存稳定标识，不保存临时请求认证字段。

当前已验证视频号望获OS。全量目录、回复分页、命名空间切换及旧缓存隔离的细节见 [视频号适配记录](WECHAT_CHANNELS_ADAPTER.md)。本地接入面板可为其他视频号自动进行该绑定，前提是运营人员已经登录对应账号。

设置 `session_mode=portable` 后，日常采集直接使用独立后台浏览器，不会占用交互登录窗口；`export-session` 和绑定命令会显式使用交互窗口获取新会话。更新凭证后，面板的只读验证会先刷新私有会话文件。

## 暂缓登录的账号

视频号“安芯很安心”和知乎“土星云”按用户要求留到实际运行时登录。同平台适配器复用，未授权账号不作为当前代码开发阻塞项，也不标为已验证。本地不会将其标为已验证，已有数据保持缓存；运行时使用该账号独立登录入口，再依次执行 `bind-channels`、`export-session`、全量 `probe` 和 `probe-comments`。在完成这些步骤前，不具备该账号的生产验收证据。


知乎土星云运行时必须登录正确账号 `zhifxi5atvi`，选择 `zhihu_creator`，按实际核验结果绑定内部UID；此前误登录安芯工程师的会话不能作为土星云会话使用。同平台复用不等于账号权限完全一致，首次仍需完整性与指标检查。授权会话可持续复用，失效或平台要求验证时再人工处理。

公众号国科安芯已完成后台代表账号实测，其余公众号按约定留到运行时授权。图片消息、其它账号历史范围及权限仍须在首次运行核验，见 [公众号适配记录](WECHAT_BROWSER_ADAPTER.md)。


## 运行时登录配置兼容

接入面板在明确点击登录或验证时，会把已支持平台的纯浏览器占位配置升级到对应内置适配器，保留会话与身份绑定。存在自定义流程、字段映射、导出配置或内容/评论别名时，即使配置为空，也不会自动替换。公众号服务号与订阅号的纯占位配置也会升级为 `wechat_browser`；显式选择官方 API 的配置保持原样。

公众号历史对账依据见 [历史数据基线](WECHAT_HISTORY_BASELINE.md)。


## 全部图文平台纳入验收

原接入范围14账号只是TikHub替换子集。`healthcheck.py`、账号接入面板和 `provider_setup.py status` 现在展示完整24账号（视频7、图文17），不会遗漏原有公开图文账号。只读验证先检查原始采集结果中的身份、完整性和阅读/评论覆盖率，再写独立验证证据；不会用缓存补回值通过健康检查。

百家号现使用 `baijiahao_creator` 授权后台，独立登录并按app_id核验；其余四类公开采集账号的登录/配置入口明确禁用；已实现的公开流程可从面板点击“只读验证”，或使用：

```bash
PROMOTION_RUNTIME_DIR="$PWD/.runtime/local" PROMOTION_TEST_MODE=1 \
  .venv/bin/python pipeline/provider_setup.py probe \
  --account 'csdn:国科安芯' --max-pages 200 \
  --output .runtime/probes/csdn-guoke.json
```

只读验证不合并或覆盖大屏快照，也不触发邮件。来源、指标与尚未验证的账号清单见 [图文验收清单](ARTICLE_PLATFORM_STATUS.md)。

图文常规采集、公众号与头条的默认分页上限现均为200，避免百家号超过原80页或头条超过原20页后每天只得到部分目录。超过200页仍明确失败或部分采集，不伪称全量；已有 `.env` 覆盖值需要部署时同步检查。
