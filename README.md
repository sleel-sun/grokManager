<img alt="grokManager" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Version](https://img.shields.io/badge/version-2.0.4.rc2-111827)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](LICENSE)
[![English](https://img.shields.io/badge/English-2563EB?logo=bookstack&logoColor=white)](docs/README.en.md)


# grokManager

> [!NOTE]
> 本项目仅供学习与研究交流。请务必遵循 Grok 的使用条款及当地法律法规，不得用于非法用途。

<br>

`grokManager` 是把原 `grok2api` 网关能力和 `grok-maintainer` 浏览器养号/注册能力合并后的统一仓库。它同时覆盖两种工作模式：
- API Gateway：基于 **FastAPI** 的 Grok 网关，对外提供 OpenAI / Anthropic 兼容接口
- Account Maintainer：基于浏览器自动化的账号注册与 token 回写工具，直接并入同一代码库

核心特性：
- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos`、`/v1/videos/{video_id}`、`/v1/videos/{video_id}/content`
- Anthropic 兼容接口：`/v1/messages`
- 支持流式与非流式对话、显式思考输出、函数工具结构透传，以及统一的 token / usage 统计
- 支持多账号池、层级选号、失败反馈、额度同步与自动维护
- 支持本地缓存图片、视频与本地代理链接返回
- 支持文生图、图像编辑、文生视频、图生视频
- 内置 Admin 后台管理、Web Chat、Masonry 生图、ChatKit 语音页面
- 内置 `app/maintainer/` 子模块，支持批量注册 Grok 账号并自动导入 token 池
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

## 服务架构

```mermaid
flowchart LR
    Client["Clients\nOpenAI SDK / curl / Browser"] --> API["FastAPI App"]
    Maintainer["Maintainer\nBrowser Automation"] --> AdminAPI["Admin Token APIs"]

    subgraph Products["Products"]
        direction TB
        OpenAI["OpenAI APIs\n/v1/*"]
        Anthropic["Anthropic APIs\n/v1/messages"]
        Web["Web Products\n/admin /webui/*"]
        AdminAPI["Token Import APIs\n/admin/api/tokens\n/v1/admin/tokens"]
    end

    subgraph Control["Control"]
        direction TB
        Models["Model Registry"]
        Accounts["Account Services"]
        Proxies["Proxy Services"]
    end

    subgraph Dataplane["Dataplane"]
        direction TB
        Reverse["Reverse Protocol + Transport"]
        AccountDP["AccountDirectory"]
        ProxyDP["Proxy Runtime"]
    end

    subgraph Platform["Platform"]
        direction TB
        Tokens["Token Estimation"]
        Storage["Storage"]
        Config["Config Snapshot"]
        Auth["Auth"]
        Log["Logging"]
    end

    API --> OpenAI
    API --> Anthropic
    API --> Web
    API --> AdminAPI

    OpenAI --> Models
    OpenAI --> AccountDP
    OpenAI --> ProxyDP
    OpenAI --> Reverse
    OpenAI --> Tokens
    OpenAI --> Storage

    Anthropic --> Models
    Anthropic --> AccountDP
    Anthropic --> ProxyDP
    Anthropic --> Reverse
    Anthropic --> Tokens

    Web --> Accounts
    Web --> Config
    Web --> Auth

    Accounts --> AccountDP
    Proxies --> ProxyDP
    Models --> Reverse
```

<br>

## 快速开始

### 本地部署

```bash
git clone https://github.com/sleel-sun/grokManager.git
cd grokManager
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

### Docker Compose

```bash
git clone https://github.com/sleel-sun/grokManager.git
cd grokManager
cp .env.example .env
docker compose up -d --build
```

这套 Compose 现在会一起拉起：
- `grokmanager`：对外 API 服务
- `maintainer`：后台注册/养号服务

首次用 Compose 部署时，建议至少先在 `.env` 里设置：
- `GROK_APP_APP_KEY`
- `GROK_APP_API_KEY`
- `GROK_APP_APP_URL`
- `MAINTAINER_EMAIL_WORKER_DOMAIN`
- `MAINTAINER_EMAIL_DOMAINS`
- `MAINTAINER_EMAIL_ADMIN_PASSWORD`

如果 maintainer 相关环境变量没填完整，`maintainer` 服务会保持启动但进入等待重试，不会把整套编排打挂。

### Vercel

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/sleel-sun/grokManager&env=LOG_LEVEL,LOG_FILE_ENABLED,DATA_DIR,LOG_DIR,ACCOUNT_STORAGE,ACCOUNT_REDIS_URL,ACCOUNT_MYSQL_URL,ACCOUNT_POSTGRESQL_URL)

### Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/sleel-sun/grokManager)

### 首次启动

1. 修改 `app.app_key`
2. 设置 `app.api_key`
3. 设置 `app.app_url`（否则图片、视频的链接会 403 无权访问）

<br>

## Maintainer

仓库已内置 `grok-maintainer` 的浏览器注册工具，代码位于 `app/maintainer/`，用于批量注册 Grok 账号并自动回写 token 池。

```bash
cp maintainer.config.example.json maintainer.config.json
uv sync --extra maintainer
uv run grokmanager-maintainer --count 5
```

- 新 CLI 名称：`grokmanager-maintainer`
- 为兼容旧脚本，旧命令 `grok2api-maintainer` 仍可继续使用
- 默认输出目录：`${DATA_DIR}/maintainer/sso`
- 默认日志目录：`${LOG_DIR}/maintainer`
- 默认回写接口：`/v1/admin/tokens`，使用 `app.app_key` 作为 Bearer Token
- 兼容新后台接口：`/admin/api/tokens` 与 `/admin/api/tokens/add`

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
| Masonry | `/webui/masonry` |
| ChatKit | `/webui/chatkit` |

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
| `proxy.clearance` | `mode`, `cf_cookies`, `user_agent`, `browser`, `flaresolverr_url`, `timeout_sec`, `refresh_interval` |
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
| `grok-4.20-auto` | `auto` | `basic`，优先使用高等级账号池 |
| `grok-4.20-expert` | `expert` | `basic`，优先使用高等级账号池 |
| `grok-4.20-heavy` | `heavy` | `heavy` |
| `grok-4.3-beta` | `grok-420-computer-use-sa` | `super` |

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
