# 视频与图文推广数据大屏

面向多平台推广账号的数据采集、质量检查、可视化和评论提醒项目。

项目包含两套静态大屏：

- 视频推广数据大屏：B站、抖音、视频号。
- 图文推广数据大屏：CSDN、电子发烧友、百家号、知乎、微信公众号、今日头条、搜狐、小红书。

后端不是常驻 Web API，而是一组按计划运行的 Python 采集任务。采集结果写入 JSON 快照，前端由原生 JavaScript 和 ECharts 直接读取。

## 功能概览

- 多平台账号、作品、文章和公开互动指标采集。
- 视频与图文大屏切换、筛选、趋势、排行、账号对比和明细查看。
- 数据去重、指标覆盖率、采集状态和新鲜度展示。
- 单账号失败保留最近成功快照，防止线上数据被临时空响应覆盖。
- B站、抖音、小红书、视频号评论及二级回复完整分页监测。
- 其他平台评论数量增长提醒。
- SMTP 邮件队列、失败重试和按平台配置收件人。
- Docker 部署及北京时间定时调度。

## 运行架构

```text
TikHub / 公开网页 / 今日头条浏览器采集 / 人工导入
                         │
                         ▼
       fetch_data.py / fetch_article_data.py
                         │
                         ▼
        清洗、去重、质量标记、原子写入
                         │
            ┌────────────┴────────────┐
            ▼                         ▼
 data/*.json                 web/**/data/*.json
            │                         │
            ▼                         ▼
   评论状态与邮件队列          BusyBox httpd 静态大屏
```

Docker Compose 包含两个服务：

- `frontend`：BusyBox `httpd`，只读提供静态页面，默认端口 `8080`。
- `scheduler`：Python 采集、评论监测、SMTP 发送和定时调度。

## 目录结构

```text
.
├── config/                         # 视频、图文账号和收件人默认配置
├── data/                           # 数据快照及运行时状态
├── pipeline/
│   ├── fetch_data.py               # 视频数据采集
│   ├── fetch_article_data.py       # 图文数据采集
│   ├── comment_monitor.py          # 评论发现、分页、增量判断和入队
│   ├── send_comment_alerts.py      # SMTP 待发队列发送
│   ├── scheduler.py                # 北京时间调度器
│   ├── snapshot_utils.py           # 快照合并、质量摘要和原子写入
│   └── validate_snapshots.py       # 快照校验
├── tests/                          # 分页、基线、邮件和调度测试
├── web/                            # 静态视频大屏
│   └── articles/                   # 静态图文大屏
├── docker-compose.yml
├── Dockerfile.frontend
├── Dockerfile.collector
├── DEPLOYMENT.md                   # 完整部署与运维说明
└── DASHBOARD_AUDIT.md              # 指标口径和质量边界
```

## Docker 快速启动

### 1. 配置环境变量

```bash
cp .env.example .env
chmod 600 .env
```

至少需要配置 TikHub 和 SMTP：

```env
TIKHUB_API_KEY=你的令牌
TIKHUB_BASE_URL=https://api.tikhub.io

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=发件账号
SMTP_PASSWORD=应用专用密码
SMTP_FROM=sender@example.com
SMTP_SSL=false
SMTP_STARTTLS=true
```

`.env` 含敏感信息，已加入 `.gitignore` 和 `.dockerignore`，不要提交或写入镜像。

### 2. 配置评论收件人

环境变量优先于 `config/platform_recipients.json`：

```env
COMMENT_RECIPIENT_BILIBILI=bili@example.com
COMMENT_OWNER_BILIBILI=B站负责人

COMMENT_RECIPIENT_DOUYIN=douyin@example.com
COMMENT_OWNER_DOUYIN=抖音负责人

COMMENT_RECIPIENT_WECHAT_CHANNELS=channels@example.com
COMMENT_RECIPIENT_XIAOHONGSHU=xhs@example.com
COMMENT_RECIPIENT_TOUTIAO=toutiao@example.com
COMMENT_RECIPIENT_SOHU=sohu@example.com
```

值设为 `disabled` 可以关闭该平台的自动分发：

```env
COMMENT_RECIPIENT_CSDN=disabled
```

也可以使用一个 JSON 变量批量覆盖：

```env
COMMENT_RECIPIENTS_JSON={"bilibili":{"email":"a@example.com","owner":"负责人"},"douyin":"b@example.com"}
```

### 3. 构建并启动

```bash
docker compose build
docker compose run --rm scheduler \
  python pipeline/send_comment_alerts.py --check-config
docker compose up -d
docker compose ps
```

访问地址：

- 视频大屏：`http://服务器IP:8080/`
- 图文大屏：`http://服务器IP:8080/articles/`

修改端口：

```env
DASHBOARD_PORT=8080
```

如果 Docker CLI 没有 buildx：

```bash
docker build -f Dockerfile.frontend -t promotion-dashboard-frontend .
docker build -f Dockerfile.collector -t promotion-dashboard-scheduler .
docker compose up -d --no-build
```

更多部署、检查和运维命令见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 定时任务

调度器固定使用北京时间：

- 容器第一次启动：立即执行评论任务并建立启动基线。
- 每天 `08:00`：视频数据采集 → 图文数据采集 → 快照校验 → 评论监测 → 邮件发送。
- 每小时：发现最新内容 → 评论监测 → 发送待发邮件。

调度状态保存在 `data/scheduler_state.json`。同一小时内重启容器不会重复执行整轮任务。

## 评论监测语义

### 明细评论平台

B站、抖音、小红书和视频号执行以下流程：

1. 每小时读取账号最新一页内容，发现当天新增作品。
2. 默认检查清单中的全部内容。
3. 完整分页一级评论。
4. 对存在回复的一级评论继续完整分页二级回复。
5. 使用评论 ID 去重，只把新增事件写入邮件队列。

默认参数：

```env
COMMENT_MONITOR_ARGS=--limit 0 --max-pages 200
COMMENT_EMAIL_MAX_EVENTS=100
```

- `--limit 0` 表示检查全部内容。
- `--max-pages 200` 是单条内容或回复线程的安全上限。
- 游标异常、游标重复或超过上限时，该内容本轮不推进状态，下小时重试。
- 可用 `--no-replies` 关闭二级回复采集。
- 可用 `--no-discovery` 关闭小时级新内容发现。

### 只提醒服务启动后的评论

- 第一次正式运行会建立持久化 `monitor_started_at` 基线，不发送此前的历史评论。
- 基线完成后发现的新内容，只提醒具有可靠时间戳且产生于启动基线之后的现有评论。
- 没有可靠时间戳的存量评论先纳入基线，后续出现的新评论 ID 正常提醒。
- 容器重启沿用 `data/comment_state.json`，不会重新发送历史评论。
- 删除 `data/comment_state.json` 会重新建立全新基线。

### 仅评论数量平台

CSDN、电子发烧友、百家号、知乎、微信公众号、今日头条和搜狐目前比较大屏快照中的评论数量：

- 只保证发现评论数量增长。
- 不保证取得评论正文、评论人或回复关系。
- 数据快照每天 `08:00` 更新，因此增长通常在每日采集后发现。

### 邮件投递语义

邮件采用“至少一次投递”：

1. 提醒先原子写入 `data/comment_alert.json`。
2. 成功写入队列后才推进评论状态。
3. SMTP 确认成功后才从队列移除。
4. 失败邮件保留到下一小时重试。

系统不会主动丢弃未确认成功的邮件，但如果进程在 SMTP 已接收、队列尚未来得及落盘的极短窗口中中断，可能重复发送。单封邮件默认最多包含 100 条评论事件，超出自动拆分。

> 完整分页调用量可能较大。上线前应根据内容数、评论量和接口价格评估预算。

## 数据采集

### 视频数据

```bash
python3 pipeline/fetch_data.py

# 跳过 B站逐视频详情补全，减少接口调用
python3 pipeline/fetch_data.py --no-enrich-bili

# 不访问外部接口，生成演示数据
python3 pipeline/fetch_data.py --mock
```

视频账号配置：`config/accounts.json`。

### 图文数据

```bash
python3 pipeline/fetch_article_data.py

# 只刷新指定平台，其他平台保留缓存
python3 pipeline/fetch_article_data.py --only xiaohongshu

# 控制公众号和今日头条分页
python3 pipeline/fetch_article_data.py --wechat-pages 3 --toutiao-pages 10

# 不访问外部接口，生成演示数据
python3 pipeline/fetch_article_data.py --mock
```

图文账号配置：`config/article_accounts.json`。

主要数据源及边界：

| 平台 | 主要数据源 | 重要边界 |
| --- | --- | --- |
| B站、抖音、视频号 | TikHub | 部分指标可能不公开；B站详情补全调用量较大 |
| CSDN、电子发烧友、百家号、搜狐 | 公开主页 | 页面变化、访问限制或历史范围可能导致部分覆盖 |
| 知乎、小红书、微信公众号 | TikHub | 阅读量、公众号互动等取决于接口权限和额度 |
| 今日头条 | Playwright 访问公开作者页 | 需要 Chromium；访问校验或页面变化可能导致失败 |
| 其他后台数据 | `data/article_manual_input.json` | 由人工导入内容决定覆盖范围 |

采集失败不会清空已有账号数据。存在上次成功快照时会保留旧数据并标记为缓存或过期；异常数量大幅下降时会拒绝覆盖。

## 本地开发

清理后的项目不包含本地虚拟环境，需要重新创建：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

启动静态页面：

```bash
python3 -m http.server 8081 --directory web
```

访问：

- `http://127.0.0.1:8081/`
- `http://127.0.0.1:8081/articles/`

## 手动运行评论任务

```bash
# 正式检查：会访问平台接口并更新评论状态
python3 pipeline/comment_monitor.py

# 只读检查，不调用评论明细接口，也不修改状态
python3 pipeline/comment_monitor.py --dry-run --no-api --limit 1

# 只检查指定平台
python3 pipeline/comment_monitor.py --platform bilibili

# 发送待发邮件
python3 pipeline/send_comment_alerts.py
```

首次正式运行需要完整分页建立基线，可能产生较多接口调用，但不会发送历史评论。

## 校验与测试

```bash
python3 -m py_compile pipeline/*.py
python3 -m unittest discover -s tests -v
python3 pipeline/validate_snapshots.py
node --check web/js/app.js
node --check web/articles/js/app.js
docker compose config --quiet
```

`validate_snapshots.py` 检查主键、引用、发布时间、非法指标和已存质量摘要的一致性。`attention` 表示存在指标缺失或账号覆盖提醒，不等于快照结构损坏。

## 关键数据文件

| 文件 | 用途 | 是否持久化 |
| --- | --- | --- |
| `data/dashboard_data.json` | 视频主快照 | 是 |
| `data/article_dashboard_data.json` | 图文主快照 | 是 |
| `web/data/dashboard_data.json` | 视频页面数据 | 是 |
| `web/articles/data/article_dashboard_data.json` | 图文页面数据 | 是 |
| `data/comment_state.json` | 评论ID、启动基线和数量游标 | 是 |
| `data/comment_alert.json` | SMTP待发队列 | 是 |
| `data/scheduler_state.json` | 最近调度时段及返回码 | 是 |

运行时状态文件已加入 `.gitignore` 和 `.dockerignore`。服务器升级时应备份并保留宿主机 `data/`，否则评论基线和待发邮件会重置。

## 安全与运维建议

- 不要把 `.env`、API Key、SMTP密码写入镜像或版本库。
- 对外发布时在 BusyBox 前端之外增加 HTTPS 和访问控制。
- 保证同一时间只有一个正式评论监测实例写入状态和邮件队列。
- 定期备份 `data/`，重点保护评论状态和待发邮件。
- 定期检查 `docker compose logs scheduler` 和 `data/scheduler_state.json`。
- SMTP 配置检查通过不等于真实邮件已送达；上线前应进行一次真实收件验证。
- 当前 Docker 采集镜像包含 Chromium，主要用于今日头条数据采集。若要移除浏览器，需要改用外部浏览器任务、人工导入或可提供头条用户文章列表的数据源。

## 进一步说明

- 指标定义、去重规则和质量保护见 [DASHBOARD_AUDIT.md](DASHBOARD_AUDIT.md)。
- Docker部署和故障处理见 [DEPLOYMENT.md](DEPLOYMENT.md)。
- 图文账号原始标识核对表见 [图文及文章.md](图文及文章.md)。
