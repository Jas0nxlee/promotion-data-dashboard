# Docker 部署说明

当前迁移分支已完成六个平台代表账号的真实后台验证，累计9／14账号通过；剩余账号运行时授权、目标环境会话验证和七天观察尚未完成，不直接替换生产服务。工作树测试使用 [独立开发说明](docs/LOCAL_DEVELOPMENT.md) 和单独的 `docker-compose.local.yml`。

## 架构

- `frontend`：BusyBox `httpd`，只提供静态页面，默认监听宿主机 `8080`。
- `scheduler`：Python + Playwright/Chromium，负责数据采集、评论检查和 SMTP 邮件发送。
- `data/`、`web/data/`、`web/articles/data/` 使用宿主机目录持久化；升级镜像不会丢失快照、评论游标和待发邮件。

调度时间固定按北京时间执行：

- 每天 `08:00`：依次采集视频数据、图文数据、校验快照，然后执行当小时评论检查和邮件发送。
- 每小时整点：评论检查，随后发送待发邮件；SMTP 临时失败时队列保留到下一小时重试。

评论正文接口当前覆盖抖音、B站、小红书和视频号：评论任务默认每2小时读取各账号最新一页作品，发现每日快照后新发布的内容。默认保留全部历史作品，按内容年龄分层轮询：7天内每2小时、30天内每6小时、更早内容每24小时；完整分页一级评论，并按跟踪评论的回复增长和补查条件读取二级回复。容器首次启动会立即执行评论任务并建立持久化 `monitor_started_at` 基线，不发送此前的历史评论；容器重启继续使用原基线。基线完成后发现的新内容，只提醒有可靠时间戳且产生于启动基线之后的评论；无可靠时间戳的存量先纳入基线，后续新评论 ID 正常提醒。游标异常、重复或超过 `--max-pages` 时，该内容本轮失败且不推进状态。CSDN、电子发烧友、百家号、知乎、公众号、今日头条和搜狐只对比大屏快照中的评论数；由于大屏每天 08:00 刷新，这些平台的评论变化只能在每日刷新后发现。

邮件采用至少一次投递：提醒先原子写入持久化队列，再推进评论状态；SMTP 成功后才逐封移出。进程在 SMTP 已接收邮件、但队列尚未来得及落盘的极端窗口中可能导致重复邮件，但不会主动删除未确认成功的提醒。

全量范围通过分层轮询、请求预算和分页上限控制访问量。上线前仍需实测目标环境的会话稳定性、请求频率和扫描耗时；超过预算或分页上限时明确报告未完成，不作为空数据处理。

## 配置

复制环境变量示例并填写真实值：

```bash
cp .env.example .env
chmod 600 .env
```

必填：

- `PROMOTION_PROVIDER_CONFIG`：已核验的账号 provider 配置。
- `PROMOTION_SESSION_DIR`：独立且不入库的登录会话目录。
- 官方公众号数据源另需配置该账号授权令牌环境变量。
- `SMTP_HOST`、`SMTP_FROM`：邮件服务器和发件地址。
- SMTP 需要认证时，同时填写 `SMTP_USERNAME`、`SMTP_PASSWORD`。
- 587/STARTTLS 使用 `SMTP_SSL=false`、`SMTP_STARTTLS=true`；465/SSL 使用 `SMTP_SSL=true`、`SMTP_STARTTLS=false`。

收件人可全部通过 `.env` 配置，变量格式为：

```env
COMMENT_RECIPIENT_BILIBILI=receiver@example.com
COMMENT_OWNER_BILIBILI=负责人姓名
COMMENT_RECIPIENT_DOUYIN=receiver@example.com
COMMENT_RECIPIENT_TOUTIAO=receiver@example.com
COMMENT_RECIPIENT_SOHU=receiver@example.com
```

变量名后缀使用平台英文标识的大写形式。环境变量覆盖 `config/platform_recipients.json`；值设为 `disabled` 可停用对应平台。也可使用一个 JSON 批量覆盖：

```env
COMMENT_RECIPIENTS_JSON={"bilibili":{"email":"a@example.com","owner":"负责人"},"douyin":"b@example.com"}
```

迁移分支默认参数：

```env
VIDEO_FETCH_ARGS=--no-enrich-bili
ARTICLE_FETCH_ARGS=--wechat-pages 200
COMMENT_MONITOR_ARGS=--limit 0 --max-age-days 0 --max-pages 200
COMMENT_EMAIL_MAX_EVENTS=100
```

已有 `.env` 会覆盖这些默认值，升级时必须检查旧的条数/时间范围和 `--no-replies` 参数。公众号浏览器每页10组发表记录，200页是安全上限；不把未完成分页当成全量。镜像设置 `PROMOTION_BROWSER_CHANNEL=chromium` 以使用镜像内浏览器，本机 Chrome 会话需先导出为独立账号的 portable 文件，目标环境仍须只读验证。

## 启动与检查

```bash
docker compose build
docker compose run --rm scheduler python pipeline/send_comment_alerts.py --check-config
docker compose up -d
docker compose ps
docker compose logs -f scheduler
```

若精简安装的 Docker CLI 没有 buildx，可先分别构建，再启动 Compose：

```bash
docker build -f Dockerfile.frontend -t promotion-dashboard-frontend .
docker build -f Dockerfile.collector -t promotion-dashboard-scheduler .
docker compose up -d --no-build
```

访问：

- 视频大屏：`http://服务器IP:8080/`
- 图文大屏：`http://服务器IP:8080/articles/`

如需修改外部端口，在 `.env` 设置 `DASHBOARD_PORT=端口号`。

## 上线前手动验证

以下命令会真实调用采集接口并更新持久化快照：

```bash
docker compose run --rm scheduler python pipeline/scheduler.py --run data
docker compose run --rm scheduler python pipeline/comment_monitor.py --dry-run --limit 1
```

确认采集结果后再启动常驻调度器。评论监控第一次正式运行会建立基线，不发送历史评论。

## 运维命令

```bash
# 立即执行完整数据采集
docker compose exec scheduler python pipeline/scheduler.py --run data

# 立即执行评论检查和待发邮件发送
docker compose exec scheduler python pipeline/scheduler.py --run comments

# 查看调度状态和最近返回码
docker compose exec scheduler cat data/scheduler_state.json

# 查看待发邮件队列
docker compose exec scheduler python -m json.tool data/comment_alert.json
```

不要同时运行多个数据采集实例。08:00 的数据任务和评论任务由同一个调度进程串行执行，避免互相覆盖快照。
