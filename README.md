# grokManager

> [!NOTE]
> 本项目仅供学习与研究交流。请务必遵循 Grok 的使用条款及当地法律法规，不得用于非法用途。

<br>

`grokManager` 是面向 Grok Web 能力的一体化接入、账号与运维平台。项目在原 `grok2api` 网关能力基础上，合并了 `grok-maintainer` 浏览器注册/养号流程，并补齐 Admin、Web Chat、媒体缓存、代理防封和批量维护能力，适合自建统一的 Grok API 与账号池管理服务。

它同时覆盖两种工作模式：
- API Gateway：基于 **FastAPI** 的 Grok 网关，对外提供 OpenAI / Anthropic 兼容接口、媒体生成接口和 WebUI
- Account Maintainer：基于浏览器自动化的账号注册、token 提取、批量回写与账号池维护工具，直接并入同一代码库

核心特性：
- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos`、`/v1/videos/{video_id}`、`/v1/videos/{video_id}/content`
- Anthropic 兼容接口：`/v1/messages`、`/v1/messages/count_tokens`；`/v1/models` 在收到 `anthropic-version` 请求头时自动返回 Anthropic 格式
- 支持流式与非流式对话、显式思考输出、函数工具结构透传、Web Search 控制，以及统一的 token / usage 统计
- 支持多账号池、层级选号、失败反馈、额度同步、批量刷新、批量 NSFW 开启与自动维护
- 支持 console 429 独立计数、滑动窗口误判缓和、过期配额自动重置与健康账号自动恢复
- 支持本地缓存图片、视频、附件下载代理与本地代理链接返回
- 支持文生图、图像编辑、文生视频、图生视频
- 内置 Admin 后台管理、Web Chat、MCP 工具管理、代码预览、Masonry 生图、ChatKit 语音页面
- 内置 `app/maintainer/` 子模块，支持批量注册 Grok 账号并自动导入 token 池
- 内置代理运行时、Cloudflare clearance 刷新、防 403 Compose override 与本机防封启动脚本
- 兼容旧版 token 写入方式 `/v1/admin/tokens`，同时支持新版 `/admin/api/tokens` 与 `/admin/api/tokens/add`

<br>

## 项目模式

### 1. API Gateway

对外提供统一的 API 网关能力：
- OpenAI 兼容：适合 SDK、脚本、第三方工具直接接入
- Anthropic 兼容：适合需要 `messages` 接口的客户端
- Web 管理界面：适合维护账号池、配置、缓存和 Web Chat 页面

### 2. Account Maintainer

对内提供账号维护能力：
- 浏览器注册 Grok 账号
- 通过临时邮箱 Worker 自动收取验证码
- 提取 `sso` token 并写入本地文件
- 回写到本仓库的 Admin token 接口，形成“注册 -> 入池 -> 对外提供 API”的闭环

<br>


## 快速开始

### 推荐部署方式

- `Docker Compose 一体化部署`：推荐生产使用，同时拉起 API Gateway 和 maintainer，形成“注册 -> 入池 -> 对外 API”闭环
- `Docker Compose API-only`：仅启动网关服务，适合你已经有独立 token 池或不需要浏览器注册器的场景
- `本地 uv 部署`：适合开发、调试和单机运行 API 服务
- `Vercel / Render`：仅建议用于 API Gateway 模式，不包含内置 maintainer

### 本地 API 部署

```bash
git clone https://github.com/sleel-sun/grokManager.git
cd grokManager
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

### Docker Compose 一体化部署（推荐）

```bash
git clone https://github.com/sleel-sun/grokManager.git
cd grokManager
cp .env.example .env
docker compose up -d --build
```

这套 Compose 现在会一起拉起：
- `grokmanager`：对外 API 服务
- `maintainer`：后台注册/养号服务

如果你只想启动 API 服务，不带浏览器 maintainer：

```bash
docker compose up -d --build grokmanager
```

Camoufox Cloudflare clearance sidecar 默认不启动。只有将
`proxy.clearance.mode` 配置为 `camoufox` 时才需要启用对应 profile：

```bash
docker compose --profile camoufox up -d --build
```

可通过 `CAMOUFOX_VERSION` 覆盖默认的 `0.4.11`，但指定版本必须已发布到
PyPI。未启用该 profile 时，Camoufox 不参与主服务构建或启动。

如果需要参考 `jiujiu532/grok2api` 的防 403 / 防封部署方式，可叠加防封版 Compose：

```bash
docker compose -f docker-compose.yml -f docker-compose.antiban.yml up -d --build
```

防封版会额外拉起：
- `warp`：WARP SOCKS5 出口
- `privoxy`：把 HTTP 代理转发到 WARP SOCKS5
- `flaresolverr`：通过同一出口刷新 Cloudflare clearance

该 override 会自动给 `grokmanager` 写入：
- `proxy.egress.mode=single_proxy`
- `proxy.egress.proxy_url=http://privoxy:8118`
- `proxy.clearance.mode=flaresolverr`
- `proxy.clearance.flaresolverr_url=http://flaresolverr:8191`

### 非 Docker 一键防封部署

如果不想使用 Docker，可以使用本机一键脚本。它会写入独立的防封运行环境，尝试把官方 Cloudflare WARP 切到本地代理模式，并尝试启动本机 FlareSolverr：

```bash
./scripts/deploy-antiban-local.sh
```

默认端口：
- WARP 本地代理：`http://127.0.0.1:40000`
- FlareSolverr：`http://127.0.0.1:8191`
- grokManager：`http://127.0.0.1:8000`

可自定义：

```bash
ANTI_BAN_PROXY_URL=socks5h://127.0.0.1:1080 \
FLARESOLVERR_BIN=/path/to/flaresolverr \
./scripts/deploy-antiban-local.sh --server-port 8000
```

脚本会生成可重复使用的启动器：

```bash
./.antiban/run-grokmanager-antiban.sh
```

macOS 打包产物的 zip 根目录会额外包含 `Start Anti-Ban.command`，双击即可写入 `~/Library/Application Support/grokManager/.env` 并以防封配置启动 APP。

首次用 Compose 部署时，建议至少先在 `.env` 里设置：
- `GROK_APP_APP_KEY`
- `GROK_APP_API_KEY`
- `GROK_APP_APP_URL`
- `MAINTAINER_EMAIL_WORKER_DOMAIN`
- `MAINTAINER_EMAIL_DOMAINS`
- `MAINTAINER_EMAIL_ADMIN_PASSWORD`

如果 maintainer 相关环境变量没填完整，`maintainer` 服务会保持启动但进入等待重试，不会把整套编排打挂。

常用运维命令：

```bash
docker compose ps
docker compose logs -f grokmanager
docker compose logs -f maintainer
```

### Vercel

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/sleel-sun/grokManager&env=LOG_LEVEL,LOG_FILE_ENABLED,DATA_DIR,LOG_DIR,ACCOUNT_STORAGE,ACCOUNT_REDIS_URL,ACCOUNT_MYSQL_URL,ACCOUNT_POSTGRESQL_URL)

### Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/sleel-sun/grokManager)

以上云平台入口仅覆盖 API Gateway，不会启动内置 maintainer。需要一体化注册/回写闭环时，请优先使用 Docker Compose。

### 首次启动

1. 修改 `app.app_key`
2. 设置 `app.api_key`
3. 设置 `app.app_url`（否则图片、视频的链接会 403 无权访问）
4. 若启用 maintainer，再补齐 `MAINTAINER_EMAIL_WORKER_DOMAIN`、`MAINTAINER_EMAIL_DOMAINS`、`MAINTAINER_EMAIL_ADMIN_PASSWORD`

<br>

## Maintainer

仓库已内置 `grok-maintainer` 的浏览器注册工具，代码位于 `app/maintainer/`，用于批量注册 Grok 账号并自动回写 token 池。

```bash
cp maintainer.config.example.json maintainer.config.json
uv sync --extra maintainer
uv run grokmanager-maintainer --count 5 --workers 2  # 2 个并发 worker × 每个 5 轮 = 10 个 token
```

- 新 CLI 名称：`grokmanager-maintainer`
- 为兼容旧脚本，旧命令 `grok2api-maintainer` 仍可继续使用
- 默认输出目录：`${DATA_DIR}/maintainer/sso`
- 默认日志目录：`${LOG_DIR}/maintainer`
- 默认回写接口：`/v1/admin/tokens`，使用 `app.app_key` 作为 Bearer Token
- 兼容新后台接口：`/admin/api/tokens` 与 `/admin/api/tokens/add`

### Turnstile 验证

最终注册页如果出现 Turnstile，maintainer 会先尝试现有真人化点击逻辑，并把内置 `turnstilePatch/script.js` 通过 CDP 注入到新页面，避免依赖 Chrome 扩展加载。`web.turnstile_manual_wait_sec` 控制自动点击失败后的人工等待时间：`0` 表示自动模式（只有真实图形桌面默认等待 180 秒；Headless、Xvfb、无 `DISPLAY` 的 Linux 服务器默认不等待），大于 0 表示固定等待秒数；如需完全关闭人工等待，可设置环境变量 `MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC=off`。

容器部署默认使用 Xvfb 非 Headless Chromium，降低 Headless 浏览器直接触发入口风控的概率。若注册页显示 `Attention Required! | Cloudflare`，这是出口 IP/ASN 硬拦截而不是 Turnstile；请使用 `docker compose -f docker-compose.yml -f docker-compose.antiban.yml up -d --build` 启用同出口的 Privoxy、WARP 和 FlareSolverr。FlareSolverr 未单独设置 `MAINTAINER_FLARESOLVERR_PROXY` 时会继承 `MAINTAINER_PROXY`，避免 clearance cookie 与注册浏览器出口不一致。

### 多进程并发注册

- CLI 新增 `--workers N`（`N≥1`，**无上限**；按机器内存 / CPU / 上游配额自行控制），`N>1` 时以 `multiprocessing.spawn` 拉起 **恰好 `N`** 个子进程并行注册，每个子进程独立跑 `count` 轮迭代
- **语义：`count` 是「每个 worker 的注册轮数」**，本次任务总注册数 = `workers × count`。这保证选了并发就一定能看到 `N` 个浏览器同时起来（旧语义是「总数 / workers」平摊，在 `count < workers` 时会静默少起 worker）
- 每个 worker 的 SSO 输出文件以 `.w{idx}` 为后缀避免冲突，例如 `sso_2026-05-20T10-30-00.w0.txt`、`sso_..w1.txt`
- 每个 worker 的运行日志以 `run_w{id}_{ts}_pid{X}.log` 存放；orchestrator 日志 `run_parallel_{ts}_pid{X}.log` 集中记录「启动/结束”事件（含每个 worker 的 pid 和 exitcode），WebUI 会优先展示该 orchestrator 日志，以便直接核对真实并发数
- 不传 `--workers` 或 `--workers 1` 时退回原单进程顺序模式，行为完全保持向后兼容
- 后台 `/maintainer/run` 同样接受 `workers` 字段（默认 1）；`GET /maintainer/status` 返回的 `spawned_workers` 字段是运行时实际启动的 worker 数（以供校验、差异调试），WebUI 表单中增加了并发输入框与「实际并发 worker」状态卡

> 💡 小例：`--count 2 --workers 5` 会同时起 5 个 Chromium 子进程，每个子进程顺序注册 2 个账号 → 总计 10 个 token。资源占用随 workers 明显上升（内存 / 文件句柄 / 上游限流），调优时从 `workers=2-3` 开始逐步加。

每个 worker 子进程会获得**独立的 Chromium 用户数据目录**：`<system_tempdir>/grokmgr-chrome-w{worker_id}-{pid}/`（例如 `/tmp/grokmgr-chrome-w0-24073/`），并通过 Chromium `--user-data-dir=` 参数显式传入。这是避免「workers 看着像串行」的关键 —— Chromium 在同一个 profile 目录下会用 SingletonLock / SingletonCookie 强制序列化，多个 worker 共享 profile 就会要么报 `ProcessSingletonStartup` 失败，要么静默挂到同一个浏览器实例上跑成串行。每个 worker 的 `alive` 事件 payload 含 `user_data_dir=...`，orchestrator 日志里直接能 grep。任务结束（成功或失败）后该目录会被 best-effort 清理。

### 暂停 / 继续 / 停止

注册任务运行期间可通过 Admin API 控制：

| 接口 | 行为 |
| :-- | :-- |
| `POST /maintainer/pause` | 设置暂停信号，当前轮结束后不再启动新轮（已在跑的子进程会跑完手头那条），状态切到 `paused` |
| `POST /maintainer/resume` | 清除暂停信号，恢复 `running` 状态，继续后续轮 |
| `POST /maintainer/stop` | 设置停止信号，状态切到 `stopping`，当前轮结束后立即退出，任务终止后清理 controller |

- 单/多进程模式都受这三个端点控制：`_MaintainerController` 内部使用 `multiprocessing.Event` 在父子进程间同步暂停 / 停止状态
- WebUI 注册页（`/admin/account` 中的 Maintainer 区块）同步暴露 **并发 worker 数** 输入框 + **暂停 / 继续 / 停止** 按钮，按钮根据 `running` / `paused` / `stopping` 状态自动 disable
- `GET /maintainer/status` 返回的 `paused` / `stop_requested` 字段对应上述两个 Event 当前状态

### Compose 一体化启动

当你使用 `docker compose up -d --build` 时，maintainer 会作为独立服务一起启动：
- 自动等待 `grokmanager` 的 `/health`
- 从环境变量生成运行时 `maintainer.config.json`
- 按 `MAINTAINER_COUNT` 执行一批注册
- 按 `MAINTAINER_INTERVAL_SEC` 循环执行下一批

容器内默认回写地址是 `http://grokmanager:8000/v1/admin/tokens`。
未显式设置 `MAINTAINER_API_TOKEN` 时，会先复用 `GROK_APP_APP_KEY`，两者都为空则兼容回退到默认后台密钥 `grok2api`。

详细说明见 [app/maintainer/README.md](app/maintainer/README.md)。

<br>

## WebUI

### 页面入口

| 页面 | 路径 |
| :-- | :-- |
| Admin 登录页 | `/admin/login` |
| 账号管理 | `/admin/account` |
| 配置管理 | `/admin/config` |
| 缓存管理 | `/admin/cache` |
| WebUI 登录页 | `/webui/login` |
| Web Chat | `/webui/chat` |
| 画图工作台 | `/webui/image-studio` |
| Masonry | `/webui/masonry` |
| ChatKit | `/webui/chatkit` |

### 画图工作台

`/webui/image-studio` 提供面向图片生成与图片编辑的对话式工作台，支持历史会话、参考图上传、服务端历史同步和结果图片管理。生成结果中的图片点击后会在当前页面弹窗预览，不会默认打开图片链接；下载和引用编辑操作仍在图片卡片下方保留。

### 鉴权规则

| 范围 | 配置项 | 规则 |
| :-- | :-- | :-- |
| `/v1/*` | `app.api_key` | 为空则不额外鉴权 |
| `/admin/*` | `app.app_key` | 当前代码默认值仍为 `grok2api`，部署后建议立即修改 |
| `/webui/*` | `app.webui_enabled`, `app.webui_key` | 默认关闭；`webui_key` 为空则不额外校验 |

<br>

## 配置体系

### 配置分层

| 位置 | 用途 | 生效时机 |
| :-- | :-- | :-- |
| `.env` | 启动前配置 | 服务启动时 |
| `${DATA_DIR}/config.toml` | 运行时配置 | 保存后即时生效 |
| `config.defaults.toml` | 默认模板 | 首次初始化时 |



### 环境变量

| 变量名 | 说明 | 默认值 |
| :-- | :-- | :-- |
| `TZ` | 时区 | `Asia/Shanghai` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_FILE_ENABLED` | 写入本地文件日志 | `true` |
| `ACCOUNT_SYNC_INTERVAL` | 账号目录增量同步间隔（秒） | `30` |
| `ACCOUNT_SYNC_ACTIVE_INTERVAL` | 账号目录检测到变化后的活跃同步间隔（秒） | `3` |
| `SERVER_HOST` | 服务监听地址 | `0.0.0.0` |
| `SERVER_PORT` | 服务监听端口 | `8000` |
| `SERVER_WORKERS` | Granian worker 数量 | `1` |
| `HOST_PORT` | Docker Compose 宿主机映射端口 | `8000` |
| `CAMOUFOX_VERSION` | 可选 Camoufox sidecar 的 PyPI 版本 | `0.4.11` |
| `DATA_DIR` | 本地数据根目录（账号库、本地媒体文件、缓存索引统一位于此目录下） | `./data` |
| `LOG_DIR` | 本地日志目录 | `./logs` |
| `ACCOUNT_STORAGE` | 账号存储后端 | `local` |
| `ACCOUNT_LOCAL_PATH` | `local` 模式账号 SQLite 路径 | `${DATA_DIR}/accounts.db` |
| `ACCOUNT_REDIS_URL` | `redis` 模式 Redis DSN | `""` |
| `ACCOUNT_MYSQL_URL` | `mysql` 模式 SQLAlchemy DSN | `""` |
| `ACCOUNT_POSTGRESQL_URL` | `postgresql` 模式 SQLAlchemy DSN | `""` |
| `ACCOUNT_SQL_POOL_SIZE` | SQL 连接池核心连接数 | `5` |
| `ACCOUNT_SQL_MAX_OVERFLOW` | SQL 连接池最大溢出连接数 | `10` |
| `ACCOUNT_SQL_POOL_TIMEOUT` | 等待连接池空闲连接的超时时间（秒） | `30` |
| `ACCOUNT_SQL_POOL_RECYCLE` | 连接最大复用时间（秒），超时后自动重连 | `1800` |
| `CONFIG_LOCAL_PATH` | `local` 模式运行时配置文件路径 | `${DATA_DIR}/config.toml` |

运行时配置也支持 `GROK_` 前缀环境变量覆盖，例如 `GROK_APP_API_KEY` 会覆盖 `app.api_key`，`GROK_FEATURES_STREAM` 会覆盖 `features.stream`。

### 系统配置项

| 分组 | 关键项 |
| :-- | :-- |
| `app` | `app_key`, `app_url`, `api_key`, `webui_enabled`, `webui_key` |
| `logging` | `file_level`, `max_files` |
| `features` | `temporary`, `memory`, `stream`, `thinking`, `auto_chat_mode_fallback`, `thinking_summary`, `dynamic_statsig`, `enable_nsfw`, `show_search_sources`, `custom_instruction`, `image_format`, `video_format` |
| `proxy.egress` | `mode`, `proxy_url`, `proxy_pool`, `resource_proxy_url`, `resource_proxy_pool`, `skip_ssl_verify` |
| `proxy.clearance` | `mode`, `cf_cookies`, `user_agent`, `browser`, `flaresolverr_url`, `camoufox_url`, `timeout_sec`, `refresh_interval` |
| `retry` | `reset_session_status_codes`, `max_retries`, `on_codes` |
| `account.refresh` | `basic_interval_sec`, `super_interval_sec`, `heavy_interval_sec`, `usage_concurrency`, `on_demand_min_interval_sec` |
| `cache.local` | `image_max_mb`, `video_max_mb` |
| `chat` | `timeout` |
| `image` | `timeout`, `stream_timeout` |
| `video` | `timeout` |
| `voice` | `timeout` |
| `asset` | `upload_timeout`, `download_timeout`, `list_timeout`, `delete_timeout` |
| `nsfw` | `timeout` |
| `batch` | `nsfw_concurrency`, `refresh_concurrency`, `asset_upload_concurrency`, `asset_list_concurrency`, `asset_delete_concurrency` |

### 图片、视频格式

| 配置项 | 可选值 |
| :-- | :-- |
| `features.image_format` | `grok_url`, `local_url`, `grok_md`, `local_md`, `base64` |
| `features.video_format` | `grok_url`, `local_url`, `grok_html`, `local_html` |

<br>

## 模型支持
> 可通过 `GET /v1/models` 获取当前支持模型列表。

### Chat

| 模型名 | mode | tier |
| :-- | :-- | :-- |
| `grok-4.20-0309-non-reasoning` | `fast` | `basic` |
| `grok-4.20-0309` | `auto` | `basic` |
| `grok-4.20-0309-reasoning` | `expert` | `basic` |
| `grok-4.20-0309-non-reasoning-super` | `fast` | `super` |
| `grok-4.20-0309-super` | `auto` | `super` |
| `grok-4.20-0309-reasoning-super` | `expert` | `super` |
| `grok-4.20-0309-non-reasoning-heavy` | `fast` | `heavy` |
| `grok-4.20-0309-heavy` | `auto` | `heavy` |
| `grok-4.20-0309-reasoning-heavy` | `expert` | `heavy` |
| `grok-4.20-multi-agent-0309` | `heavy` | `heavy` |
| `grok-4.20-fast` | `fast` | `basic`，优先使用高等级账号池 |
| `grok-4.20-auto` | `auto` | `super`，优先使用 heavy 后回退 super |
| `grok-4.20-expert` | `expert` | `super`，优先使用 heavy 后回退 super |
| `grok-4.20-heavy` | `heavy` | `heavy` |
| `grok-4.5` | `build` | 独立 Grok Build CLI OAuth 上游 |
| `grok-4.3-build` | `build` | 面向 OpenClaw/Codex 等 Agent，使用 Build OAuth 号池 |
| `grok-composer-2.5-fast` | `build` | Grok Build CLI OAuth，上游账号需要 Composer 权限/额度 |
| `grok-4.3` | `console` | `basic`（走 xAI Console Responses 上游，命中 `https://console.x.ai`） |
| `grok-4.3-beta` | `grok-420-computer-use-sa` | `super` |

#### Console 免费账号模型

这些模型走 xAI Console Responses 上游，使用 `basic` 池账号；公开模型名会映射到真实上游模型，并按下表注入 `reasoning.effort`。

| 模型名 | 上游模型 | reasoning effort | 说明 |
| :-- | :-- | :-- | :-- |
| `grok-4.3-console` | `grok-4.3` | 用户传入，默认 `medium`；显式 `none` 关闭 | 免费账号 |
| `grok-4.3-low` | `grok-4.3` | 固定 `low` | 免费账号 |
| `grok-4.3-medium` | `grok-4.3` | 固定 `medium` | 免费账号 |
| `grok-4.3-high` | `grok-4.3` | 固定 `high` | 免费账号 |
| `grok-4.20-0309-console` | `grok-4.20-0309` | 默认 | 免费账号 |
| `grok-4.20-0309-reasoning-console` | `grok-4.20-0309-reasoning` | 固定 reasoning 模型 | 免费账号 |
| `grok-4.20-0309-non-reasoning-console` | `grok-4.20-0309-non-reasoning` | 无 reasoning | 免费账号 |
| `grok-4.20-multi-agent-console` | `grok-4.20-multi-agent` | 用户传入，默认 `medium` | 免费账号，多智能体，agent 数量由 effort 决定 |
| `grok-4.20-multi-agent-low` | `grok-4.20-multi-agent` | 固定 `low` | 免费账号，多智能体，4 agents |
| `grok-4.20-multi-agent-medium` | `grok-4.20-multi-agent` | 固定 `medium` | 免费账号，多智能体，4 agents |
| `grok-4.20-multi-agent-high` | `grok-4.20-multi-agent` | 固定 `high` | 免费账号，多智能体，16 agents |
| `grok-4.20-multi-agent-xhigh` | `grok-4.20-multi-agent` | 固定 `xhigh` | 免费账号，多智能体，16 agents |
| `grok-build-console` | `grok-build-0.1` | 默认 | 免费账号，Grok Build 0.1 |

#### Grok 4.5 Build OAuth

`grok-4.5`、`grok-4.5-low`、`grok-4.5-medium` 和 `grok-4.5-high`
使用 Grok Build CLI 专用上游，不使用普通 Grok SSO/Console 账号池。

将 Grok CLI 生成的 `~/.grok/auth.json` 放到项目的
`data/grok_auth.json`，然后重启服务。文件会通过 Docker 的 `/app/data`
挂载读取；access token 到期时服务会使用 `refresh_token` 自动刷新并原子写回。

```bash
install -m 600 ~/.grok/auth.json data/grok_auth.json
docker compose up -d --build grokmanager
```

### Image

| 模型名 | mode | tier |
| :-- | :-- | :-- |
| `grok-imagine-image-lite` | `fast` | `basic` |
| `grok-imagine-image` | `auto` | `super` |
| `grok-imagine-image-pro` | `auto` | `super` |

### Image Edit

| 模型名 | mode | tier |
| :-- | :-- | :-- |
| `grok-imagine-image-edit` | `auto` | `super` |

### Video

| 模型名 | mode | tier |
| :-- | :-- | :-- |
| `grok-imagine-video` | `auto` | `super` |

<br>

## API 一览

| 接口 | 是否鉴权 | 说明 |
| :-- | :-- | :-- |
| `GET /v1/models` | 是 | 列出当前启用模型 |
| `GET /v1/models/{model_id}` | 是 | 获取单个模型信息 |
| `POST /v1/chat/completions` | 是 | 对话 / 图像 / 视频统一入口 |
| `POST /v1/responses` | 是 | OpenAI Responses API 兼容子集 |
| `POST /v1/messages` | 是 | Anthropic Messages API 兼容接口 |
| `POST /v1/messages/count_tokens` | 是 | Anthropic Count Message Tokens 兼容接口（预估输入 token） |
| `POST /v1/images/generations` | 是 | 独立图像生成接口 |
| `POST /v1/images/edits` | 是 | 独立图像编辑接口 |
| `POST /v1/videos` | 是 | 异步视频任务创建 |
| `GET /v1/videos/{video_id}` | 是 | 查询视频任务 |
| `GET /v1/videos/{video_id}/content` | 是 | 获取最终视频文件 |
| `GET /v1/files/video?id=...` | 否 | 获取本地缓存视频 |
| `GET /v1/files/image?id=...` | 否 | 获取本地缓存图片 |

<br>

## 接口示例

> 以下示例默认使用 `http://localhost:8000` 地址。

### Cloudflare / WAF 与 OpenAI Python SDK

如果公网域名前置了 Cloudflare 或其他 WAF，可能会出现同一个 API key 用 `curl`
正常访问，但 OpenAI Python SDK 或 Python `urllib` 返回 `HTTP 403`、`error code:
1010`、`Your request was blocked` 的情况。这类响应通常由 WAF 在请求到达
grokManager 前拦截，服务端日志里不会出现对应请求。

常见触发头包括：

```text
User-Agent: OpenAI/Python ...
User-Agent: Python-urllib/...
```

客户端可覆盖请求头：

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["GROKMANAGER_API_KEY"],
    base_url="https://your-domain.example/v1",
    default_headers={
        "User-Agent": "curl/8.5.0",
        "Accept": "*/*",
    },
)

response = client.chat.completions.create(
    model="grok-4.20-auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```

`urllib` 也需要显式设置请求头：

```python
import json
import os
from urllib import request

req = request.Request(
    "https://your-domain.example/v1/models",
    headers={
        "Authorization": f"Bearer {os.environ['GROKMANAGER_API_KEY']}",
        "User-Agent": "curl/8.5.0",
        "Accept": "*/*",
    },
)

with request.urlopen(req, timeout=30) as resp:
    print(json.loads(resp.read()))
```

更稳定的做法是在 Cloudflare 里为 API 路径配置跳过 WAF / Bot 规则，例如仅对独立
API 域名和 `/v1/*` 路径放行，并保留鉴权与速率限制：

```text
(http.host eq "your-domain.example" and starts_with(http.request.uri.path, "/v1/"))
```

不要把整站关闭防护；只放行 OpenAI / Anthropic 兼容 API 路径即可。

<details>
<summary><code>GET /v1/models</code></summary>
<br>

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY"
```

<details>
<summary>字段说明</summary>
<br>

| 字段 | 位置 | 说明 |
| :-- | :-- | :-- |
| `Authorization` | Header | 当 `app.api_key` 非空时必填，格式为 `Bearer <api_key>` |

<br>
</details>

<br>
</details>

<details>
<summary><code>POST /v1/chat/completions</code></summary>
<br>

对话：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "stream": true,
    "reasoning_effort": "high",
    "deepsearch": "default",
    "messages": [
      {"role":"user","content":"你好"}
    ]
  }'
```

图像：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "stream": true,
    "messages": [
      {"role":"user","content":"一只在太空漂浮的猫"}
    ],
    "image_config": {
      "n": 2,
      "size": "1024x1024",
      "response_format": "url"
    }
  }'
```

视频：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -d '{
    "model": "grok-imagine-video",
    "stream": true,
    "messages": [
      {"role":"user","content":"霓虹雨夜街头，电影感慢镜头追拍"}
    ],
    "video_config": {
      "seconds": 10,
      "size": "1792x1024",
      "resolution_name": "720p",
      "preset": "normal"
    }
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段 | 说明 |
| :-- | :-- |
| `messages` | 支持文本与多模态内容块 |
| `stream` | 是否流式输出；不传时使用 `features.stream` 默认值 |
| `reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`；`none` 会关闭思考输出 |
| `deepsearch` | 深度搜索预设：`default`, `deeper` |
| `temperature` / `top_p` | 采样参数，默认 `0.8` / `0.95` |
| `tools` | OpenAI function tools 结构 |
| `tool_choice` | `auto`, `required` 或指定函数工具 |
| `image_config` | 图像模型参数 |
| \|_ `n` | `lite` 为 `1-4`，其他图像模型为 `1-10`，编辑模型为 `1-2` |
| \|_ `size` | `1280x720`, `720x1280`, `1792x1024`, `1024x1792`, `1024x1024` |
| \|_ `response_format` | `url`, `b64_json` |
| `video_config` | 视频模型参数 |
| \|_ `seconds` | `6`, `10`, `12`, `16`, `20` |
| \|_ `size` | `720x1280`, `1280x720`, `1024x1024`, `1024x1792`, `1792x1024` |
| \|_ `resolution_name` | `480p`, `720p` |
| \|_ `preset` | `fun`, `normal`, `spicy`, `custom` |

<br>
</details>

<br>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "input": "解释一下量子隧穿",
    "instructions": "用简洁的中文回答",
    "stream": true,
    "reasoning": {
      "effort": "high"
    }
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段 | 说明 |
| :-- | :-- |
| `model` | 模型 ID，需为已启用模型 |
| `input` | 用户输入；支持字符串或 Responses API 风格的消息数组 |
| `instructions` | 可选系统指令，会作为 system 消息注入 |
| `stream` | 是否流式输出；不传时使用 `features.stream` 默认值 |
| `reasoning` | 可选思考配置 |
| \|_ `effort` | `none` 会关闭思考输出；其他值会开启思考输出 |
| `temperature` / `top_p` | 采样参数，默认 `0.8` / `0.95` |
| `tools` / `tool_choice` | 支持函数工具；Responses API 的扁平工具格式会自动转换 |

<br>
</details>

<br>
</details>

<details>
<summary><code>POST /v1/messages</code></summary>
<br>

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "stream": true,
    "thinking": {
      "type": "enabled",
      "budget_tokens": 1024
    },
    "messages": [
      {
        "role": "user",
        "content": "用三句话解释量子隧穿"
      }
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段 | 说明 |
| :-- | :-- |
| `model` | 模型 ID，需为已启用模型 |
| `messages` | Anthropic Messages 格式消息，支持文本、图片、文档和工具结果块 |
| `system` | 可选系统提示词，支持字符串或文本块数组 |
| `stream` | 是否流式输出；不传时使用 `features.stream` 默认值 |
| `thinking` | 可选思考配置 |
| \|_ `type` | `disabled` 会关闭思考输出；其他配置会开启思考输出 |
| `max_tokens` | 接收但当前会忽略，Grok 上游不暴露该参数 |
| `tools` / `tool_choice` | 支持 Anthropic 工具格式，会转换为内部 function tools |

<br>
</details>

<br>
</details>

<details>
<summary><code>POST /v1/messages/count_tokens</code></summary>
<br>

```bash
curl http://localhost:8000/v1/messages/count_tokens \
  -H "Content-Type: application/json" \
  -H "x-api-key: $GROKMANAGER_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "grok-4.20-auto",
    "system": "You are a helpful assistant.",
    "messages": [
      {"role": "user", "content": "用三句话解释量子隧穿"}
    ]
  }'
```

请求体与 `/v1/messages` 完全一致（同时支持 `system`、`tools`、`tool_choice` 等可选字段），响应返回 `{"input_tokens": N}`，用于在调用前估算输入 token 用量。

<br>
</details>

<details>
<summary><code>POST /v1/images/generations</code></summary>
<br>

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "一只在太空漂浮的猫",
    "n": 1,
    "size": "1792x1024",
    "response_format": "url"
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段 | 说明 |
| :-- | :-- |
| `model` | 图像模型：`grok-imagine-image-lite`, `grok-imagine-image`, `grok-imagine-image-pro` |
| `prompt` | 图片生成提示词 |
| `n` | 生成数量；`lite` 为 `1-4`，其他图像模型为 `1-10` |
| `size` | 支持 `1280x720`, `720x1280`, `1792x1024`, `1024x1792`, `1024x1024` |
| `response_format` | `url` 或 `b64_json` |

<br>
</details>

<br>
</details>

<details>
<summary><code>POST /v1/images/edits</code></summary>
<br>

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -F "model=grok-imagine-image-edit" \
  -F "prompt=把这张图变清晰一些" \
  -F "image[]=@/path/to/image.png" \
  -F "n=1" \
  -F "size=1024x1024" \
  -F "response_format=url"
```

<details>
<summary>字段说明</summary>
<br>

| 字段 | 说明 |
| :-- | :-- |
| `model` | 图像编辑模型，目前为 `grok-imagine-image-edit` |
| `prompt` | 编辑指令 |
| `image[]` | 参考图片，multipart 文件字段；最多使用 5 张 |
| `n` | 生成数量，范围 `1-2` |
| `size` | 当前仅支持 `1024x1024` |
| `response_format` | `url` 或 `b64_json` |
| `mask` | 暂不支持；传入会返回校验错误 |

<br>
</details>

<br>
</details>

<details>
<summary><code>POST /v1/videos</code></summary>
<br>

```bash
curl http://localhost:8000/v1/videos \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -F "model=grok-imagine-video" \
  -F "prompt=霓虹雨夜街头，电影感慢镜头追拍" \
  -F "seconds=10" \
  -F "size=1792x1024" \
  -F "resolution_name=720p" \
  -F "preset=normal" \
  -F "input_reference[]=@/path/to/reference.png"
```

```bash
curl http://localhost:8000/v1/videos/<video_id> \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY"

curl -L http://localhost:8000/v1/videos/<video_id>/content \
  -H "Authorization: Bearer $GROKMANAGER_API_KEY" \
  -o result.mp4
```

<details>
<summary>字段说明</summary>
<br>

| 字段 | 说明 |
| :-- | :-- |
| `model` | 视频模型，目前为 `grok-imagine-video` |
| `prompt` | 视频生成提示词 |
| `seconds` | 视频长度：`6`, `10`, `12`, `16`, `20` |
| `size` | 支持 `720x1280`, `1280x720`, `1024x1024`, `1024x1792`, `1792x1024` |
| `resolution_name` | `480p` 或 `720p` |
| `preset` | `fun`, `normal`, `spicy`, `custom` |
| `input_reference[]` | 可选图生视频参考图，multipart 文件字段；最多使用前 5 张 |
| `video_id` | `POST /v1/videos` 返回的视频任务 ID，用于查询任务或下载成片 |

<br>
</details>

<br>
</details>

<br>

## 说明

当前仓库是在 `grok2api` 主服务基础上，合并 `grok-maintainer` 子工具后的新项目形态。README 已按统一仓库模式整理，但英文文档和部分脚本命名仍保留兼容层，后续可以继续逐步统一。
