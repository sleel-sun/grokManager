# Grok Maintainer

浏览器注册器已经并入 `grokManager` 仓库，作为可选子工具维护，不会影响 API 服务默认依赖。

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

## 配置文件

默认读取仓库根目录的 `maintainer.config.json`，也可以通过 `--config` 或环境变量 `GROK_MAINTAINER_CONFIG` 指定。

### 邮箱 Provider

`email.provider` 默认为 `worker`，继续使用现有临时邮箱 Worker。设置为 `hotmail`、`outlook` 或 `outlookmail` 时，`email.credentials_file` 每行使用以下格式：

```text
email----password----client_id----refresh_token
```

相对凭证路径按项目根目录解析。Hotmail 模式使用 Microsoft OAuth2 刷新 access token，通过 XOAUTH2 登录 Outlook IMAP，并按注册 alias 匹配收件人后提取验证码。`email.alias_mode=plus` 会先使用主地址，后续生成随机 plus alias；`alias_random_length` 和 `max_aliases_per_account` 控制 alias。凭证文件旁的隐藏 reservation 目录会原子记录已分配 alias，避免多 worker 或重启后重复使用。`poll_interval`、`recent_seconds`、`imap_hosts`、`imap_last_n` 和 `require_recipient_match` 控制轮询范围。Microsoft 返回轮换后的 refresh token 时会原子回写凭证文件并设置权限为 `0600`。

### Grok Build OAuth

`grok_build.auto_oauth_after_register` 默认开启。注册批次完成并回写 SSO 池后，maintainer 会顺序使用纯 HTTP Device Flow 为新账号生成 Grok Build OAuth，并写入 `grok_build.auth_file` 指定的聚合凭证池。`required=false` 时单个 OAuth 失败不会丢弃已注册的 SSO；`delay_sec` 可控制账号间隔，`poll_timeout_sec` 默认将 token 轮询限制为 90 秒，`proxy` 可单独指定授权代理。

### Turnstile

最终注册页出现 Turnstile 时会先自动点击验证，并通过 CDP 注入内置 `turnstilePatch/script.js`，不再依赖 Chrome 扩展加载。`web.turnstile_manual_wait_sec=0` 表示自动模式：只有真实图形桌面默认等待 180 秒供人工完成验证；Headless、Xvfb、无 `DISPLAY` 的 Linux 服务器默认不等待。设置为正数可固定等待秒数。要完全关闭人工等待，可设置 `MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC=off`。

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
- 可选：本仓库运行中的 `grokManager` 服务，用于自动回写 token
