# Docker 可视化授权

`login` 是可选的独立服务，只运行接入面板与授权桌面，不运行调度器。浏览器窗口显示在 noVNC 中；登录会话保存在共享 sessions 目录，后续采集可继续使用导出的会话。

## 首次初始化

在项目目录的交互式终端运行。主机需要 Python 3 和 `htpasswd`（Debian/Ubuntu 包名 `apache2-utils`；macOS 通常已自带）。默认随机生成强口令，只在当前终端显示一次：

```bash
python3 scripts/init_authorization.py \
  --migrate-provider-file .runtime/providers.json
```

没有旧 provider 配置的新安装，省略 `--migrate-provider-file`。如检测到旧 `.runtime/providers.json`，工具会要求显式迁移；原文件始终保持不变，已有目标配置不会被覆盖。

迁移仅转换**新文件**：浏览器类配置统一改为 `session_mode=portable`、`channel=chromium`，删除本机 `cdp_url` 和 `headed`，保留账号 ID、别名与自定义采集流程等其它配置；`wechat_official` 官方接口配置原样保留。这样 Linux 不会尝试接管 Mac 的调试窗口或打开 Mac 的持久化 Chrome profile。已有导出的 `.storage.json` 可由共享 sessions 目录读取；未导出的账号会明确提示缺少可移植会话，需要在网页授权桌面重新登录。

初始化生成以下私有文件：

| 路径 | 用途 |
| --- | --- |
| `.runtime/authorization/admin.htpasswd` | 唯一初始管理员的 bcrypt 哈希，权限 0600 |
| `.runtime/provider-config/providers.json` | 目录挂载的账号采集配置 |
| `.runtime/sessions/` | 独立浏览器与可移植会话 |
| `.runtime/sessions/authorization/` | 授权任务状态 |

默认用户名是 `operator`，可用 `--username` 修改。`--prompt-password` 可手动输入两次口令；自动化使用 `--password-stdin`。口令不会放进命令参数、环境变量或配置文件。默认生成模式拒绝在非交互终端显示口令。

明确重置管理员口令时使用 `--replace-password`，不要再传旧配置迁移参数。provider 配置保持不变。

## 启动授权服务

需要支持命名构建上下文的 Docker Buildx 和 Compose 2.17+。登录镜像复用 collector 服务的环境，并增加 Xvfb、Openbox、x11vnc、noVNC、websockify、nginx 和中文字体。

```bash
docker compose --profile login build login
docker compose --profile login up -d --no-deps login
```

打开 `http://127.0.0.1:18762/`，输入初始化产生的管理员凭证。管理面板与 `/desktop/` 共用同一套认证；无需另设 VNC 密码。浏览器授权状态与实际账号身份仍由面板核验，打开窗口不代表授权完成。

上述命令只启动 `login`。生产调度器仍使用 `Dockerfile.collector` 原来的 `python pipeline/scheduler.py` 命令；不会因为授权服务启动而启动、重启调度器。

远程服务器建议使用 SSH 转发，保持网关只监听服务器回环地址：

```bash
ssh -L 18762:127.0.0.1:18762 operator@server
```

然后访问本机同一地址。若要经已有 HTTPS 反向代理提供访问，外层需保留浏览器原始 Host、Origin 和 WebSocket 升级头，并将 `PROMOTION_PANEL_ORIGIN` 设置为浏览器实际访问的完整 origin。不要直接发布 VNC、websockify、CDP 或内部面板端口。

## 局域网访问授权管理

将授权网关绑定到所有主机网卡，并配置浏览器实际使用的地址。例如服务器局域网 IP 为 `10.0.1.13`：

```sh
PROMOTION_LOGIN_BIND=0.0.0.0 \
PROMOTION_PANEL_ORIGIN=http://10.0.1.13:18762 \
PROMOTION_PANEL_ALLOWED_ORIGINS=http://127.0.0.1:18762,http://localhost:18762 \
  docker compose --env-file /dev/null --profile login up -d --no-deps login
```

如使用独立本地 Compose，在 `docker compose` 后增加 `-f docker-compose.local.yml`，并保留该环境使用的目录覆盖变量。持久部署可把三个变量写入本部署的环境配置。IP 变化后同步修改 `PROMOTION_PANEL_ORIGIN` 并重新创建 login 服务。

同一局域网设备访问 `http://10.0.1.13:18762/`，使用原管理员凭证。`PROMOTION_PANEL_ALLOWED_ORIGINS` 是逗号分隔的精确地址别名，不支持通配符；Host、Origin、CSRF 和 WebSocket 同源校验继续生效。本机别名可选。只有网关端口对外开放，VNC、CDP、内部面板继续仅绑定容器回环地址。

## 隔离本地开发环境

`docker-compose.local.yml` 是独立文件，不与生产 Compose 合并。指定唯一项目名后，仅启动其登录服务：

```bash
PROMOTION_DEV_ID=promotion-my-login \
  docker compose --env-file /dev/null -f docker-compose.local.yml --profile login build login
PROMOTION_DEV_ID=promotion-my-login \
  docker compose --env-file /dev/null -f docker-compose.local.yml --profile login up -d --no-deps login
```

本地模式使用 `.runtime/local/data` 和独立 login runtime，并设置测试模式。进行完全独立的授权测试时，还应覆盖下表中的 provider、sessions、管理员凭证及 login runtime 目录，避免复用现有账号会话。

| 宿主机 Compose 变量 | 默认值 |
| --- | --- |
| `PROMOTION_PROVIDER_DIR` | `./.runtime/provider-config` |
| `PROMOTION_SESSIONS_DIR` | `./.runtime/sessions` |
| `PROMOTION_AUTH_DIR` | `./.runtime/authorization` |
| `PROMOTION_LOGIN_RUNTIME_DIR` | 生产 `./.runtime/login-runtime`；本地 `./.runtime/local/login-runtime` |
| `PROMOTION_LOGIN_PORT` | `18762` |
| `PROMOTION_LOGIN_BIND` | 默认 `127.0.0.1`；局域网访问设置 `0.0.0.0` 或主机局域网 IP |
| `PROMOTION_PANEL_ORIGIN` | `http://127.0.0.1:18762` |
| `PROMOTION_PANEL_ALLOWED_ORIGINS` | 默认空；逗号分隔的其它精确访问地址 |
| `PROMOTION_AUTHORIZATION_TTL_SECONDS` | 默认 `1800`；大历史账号可临时设为 `3600`，范围300至3600秒 |

修改外部端口时，同时修改 `PROMOTION_PANEL_ORIGIN`。Origin 必须与浏览器地址完全一致，不带路径、尾部斜杠或凭证。

## 挂载与边界

- provider **目录**挂到 `/run/promotion/providers`；scheduler/collector 只读，login 可写。原子替换 `providers.json` 后两个服务都能看到新文件。旧 `PROMOTION_PROVIDER_FILE` 单文件挂载方式已移除。
- sessions 对 login 和 scheduler 均可写；授权任务状态位于其中的 `authorization` 子目录。
- login 的 `PROMOTION_RUNTIME_DIR=/app/.runtime` 单独持久化；`PROMOTION_DATA_DIR=/app/data` 与同一部署的采集器共享验证证据和预算。
- 管理员哈希目录只读挂到 `/run/promotion/authorization`。容器内 nginx worker 使用 root，以读取 0700 目录中的 0600 文件；无需公开文件权限。容器设置 `no-new-privileges`。
- Xvfb 禁止 TCP 监听；面板 `18761`、VNC `5900`、websockify `6080` 都只监听容器回环地址。Compose 只发布网关 `18762`。Chromium 的容器 root 模式由授权 broker 显式处理，不改变宿主机浏览器参数。
- `/desktop/websockify` 同时要求 BasicAuth 和精确同源 Origin；没有 Origin 或不同 Origin 都拒绝。代理不把 BasicAuth 明文凭证继续传给 Python 面板。
- 凭证文件不存在、格式不合法或权限过宽时，入口脚本在开放服务端口之前退出。任一桌面/网关/面板进程退出，容器也退出，不保留半运行的授权服务。

## 验证

单元测试位于 `tests/test_login_infrastructure.py`。容器冒烟脚本只允许 `promotion-login-smoke-*` 项目名，为测试创建独立私有目录、bcrypt 凭证、镜像标签和挂载，检查认证、WebSocket Origin、内部端口隔离及配置原子替换。脚本不会输出随机测试口令，结束时删除本次测试容器：

```bash
python3 scripts/smoke_login_container.py --project promotion-login-smoke-local
```

证据保存在 `.runtime/<project>/`；构建需要下载基础镜像和软件包。冒烟脚本不使用生产 `.env`，不启动原项目的 scheduler/frontend，也不修改已有私有凭证。

实现参考：[Docker 命名构建上下文](https://docs.docker.com/reference/compose-file/build/#additional_contexts)、[nginx BasicAuth](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)、[nginx WebSocket 代理](https://nginx.org/en/docs/http/websocket.html)、[Apache htpasswd](https://httpd.apache.org/docs/2.4/programs/htpasswd.html)。
