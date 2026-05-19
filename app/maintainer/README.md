# Grok Maintainer

浏览器注册器已经并入 `grok2api` 仓库，作为可选子工具维护，不会影响 API 服务默认依赖。

## 安装

```bash
cp maintainer.config.example.json maintainer.config.json
uv sync --extra maintainer
```

## 启动

```bash
uv run grokmanager-maintainer --count 5
uv run python -m app.maintainer --count 0
```

## Compose 一体化运行

`docker-compose.yml` 已内置独立的 `maintainer` 服务。执行：

```bash
docker compose up -d --build
```

会同时启动：
- `grokmanager` API 服务
- `maintainer` 后台服务

Compose 模式下，maintainer 默认：
- 等待 `http://grokmanager:8000/health`
- 从 `.env` 中的 `MAINTAINER_*` 环境变量生成运行时配置
- 把 token 回写到 `http://grokmanager:8000/v1/admin/tokens`
- 通过 `MAINTAINER_INTERVAL_SEC` 周期性重复运行
- 默认把自动生成的配置写到容器内临时路径，不落宿主持久卷
- 未显式设置 `MAINTAINER_API_TOKEN` 时，会先复用 `GROK_APP_APP_KEY`，两者都为空则回退到默认后台密钥 `grok2api`

必填环境变量：
- `MAINTAINER_EMAIL_WORKER_DOMAIN`
- `MAINTAINER_EMAIL_DOMAINS`
- `MAINTAINER_EMAIL_ADMIN_PASSWORD`

常用可调参数：
- `MAINTAINER_COUNT`
- `MAINTAINER_INTERVAL_SEC`
- `MAINTAINER_HEADLESS`
- `MAINTAINER_USE_XVFB`

## 配置文件

默认读取仓库根目录的 `maintainer.config.json`，也可以通过 `--config` 或环境变量 `GROK_MAINTAINER_CONFIG` 指定。

### API 回写

- 兼容旧接口：`/v1/admin/tokens`
- 兼容新接口：`/admin/api/tokens`
- 追加导入：`/admin/api/tokens/add`

旧接口使用 `{"ssoBasic": [...]}` 载荷，新接口使用 Admin Key 并写入 `basic` 池。
`email.verify_ssl` 与 `api.verify_ssl` 默认开启；只有在自签名或内网环境下才建议显式关闭。

## 输出

- Token 文件：`${DATA_DIR}/maintainer/sso/`
- 运行日志：`${LOG_DIR}/maintainer/`
- 运行日志默认不再记录注册密码

## 环境要求

- Python 3.13+
- Chrome 或 Chromium
- 自建临时邮箱 Worker
- 可选：本仓库运行中的 `grok2api` 服务，用于自动回写 token
