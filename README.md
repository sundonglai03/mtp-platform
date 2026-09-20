# mtp-platform

读取规范测试用例，直接调用 SSH、MySQL、Playwright 和 HTTP 工具，最后生成证据与测试报告。既可以作为 CLI / Docker 批任务执行器，也可以作为带登录、上传、后台队列和报告下载的常驻 HTTP 服务；执行链路不依赖 MCP 协议。

## 快速开始

### 本地运行

```bash
uv sync --frozen --all-extras
uv run --frozen mtp doctor
uv run --frozen mtp validate tests/cases/valid
uv run --frozen mtp run tests/cases/valid --out artifacts/reports
```

本地启动 Web 服务：

```bash
export MTP_WEB_USERNAME=admin
export MTP_WEB_PASSWORD='替换为高强度密码'
export MTP_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
uv run --frozen mtp serve --host 0.0.0.0 --port 8080
```

### Web 常驻服务（Docker）

```bash
export MTP_WEB_USERNAME=admin
export MTP_WEB_PASSWORD='替换为高强度密码'
export MTP_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose build
docker compose up -d
docker compose ps
```

打开 `http://<服务器IP>:8080`，登录后即可上传 YAML、YML 或 JSON 用例。用户名、密码和 Session 密钥没有默认生产值；缺少其中任意一个时 Web 服务会拒绝启动。

公网部署请在前面使用 Nginx/Caddy 配置 HTTPS，并设置：

```bash
export MTP_COOKIE_SECURE=true
```

任务元数据保存在 `artifacts/mtp-platform.sqlite3`，上传文件位于 `artifacts/uploads/<run_id>/`，每次任务的证据和报告位于 `artifacts/runs/<run_id>/`。重启容器后历史任务仍可查询；中断时仍在运行的任务会标记为 `error`，排队任务会继续执行。

### 一次性 CLI（Docker）

```bash
docker compose run --rm mtp-platform doctor
docker compose run --rm mtp-platform validate /app/cases/valid
docker compose run --rm mtp-platform run /app/cases --office
```

任务完成后容器退出，报告保留在宿主机的 `artifacts/`。镜像名为 `mtp-platform:0.1.0`，容器名为 `mtp-platform`。

镜像入口会在启动时修复 `/app/artifacts` 绑定目录的所有权，然后立即降权为 `mtp`
用户执行命令。因此即使 Linux 上的 `./artifacts` 是由 root 或 Docker 自动创建，也
不需要使用 `chmod 777`，测试执行进程仍保持非 root。

## 工作流程

```text
Agent + mtp-contracts-mcp
          ↓
      规范 YAML / JSON
          ↓
      mtp-platform
          ├─ engine：编排、超时、重试、断言
          ├─ tools：直接调用被测系统
          └─ reporting：汇总证据和结果
          ↓
JSON / JUnit / HTML / Excel / Word 报告
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `mtp version` | 显示版本 |
| `mtp doctor` | 检查配置路径和工具依赖 |
| `mtp validate <路径>` | 校验一个用例或用例目录 |
| `mtp run <路径>` | 执行用例并生成报告 |
| `mtp serve` | 启动 HTTP 管理服务 |

完整参数使用 `mtp --help` 或 `mtp <子命令> --help` 查看。

## 项目结构

```text
src/mtp_platform/
├── engine/      # 编排、断言、证据和执行历史
├── tools/       # SSH、MySQL、Playwright、HTTP 直连工具
├── reporting/   # JSON、JUnit、HTML、Excel、Word 报告
├── service/     # CLI/Web 共用执行服务、SQLite 和后台任务队列
├── web/         # FastAPI、登录、页面和静态资源
└── cli.py       # `mtp` 命令入口
```

工具直接作为 Python 模块调用。这样可以减少 MCP 服务进程、网络调用、序列化和连接管理；MCP 只用于让外部 Agent 复用测试用例契约。

## 工具依赖

| extra | 能力 | 依赖 |
| --- | --- | --- |
| 无 | HTTP API | `requests`，默认安装 |
| `ssh` | 命令、上传、下载 | Paramiko |
| `mysql` | 查询和受控写入 | PyMySQL |
| `playwright` | 浏览器自动化 | Playwright + Chromium |
| `office` | Excel / Word 报告 | openpyxl、python-docx |

按需安装示例：

```bash
uv sync --frozen --extra ssh --extra mysql --extra playwright
uv run playwright install chromium
```

Docker 镜像默认包含全部 extra 和 Chromium。不需要浏览器时可以减小镜像：

```bash
docker build --build-arg MTP_INSTALL_BROWSER=0 -t mtp-platform:0.1.0 .
```

## 配置与安全

主配置文件是 `mtp_config.yaml`。常用环境变量：

| 变量 | 作用 |
| --- | --- |
| `MTP_ROOT` | 项目和相对路径的解析根目录 |
| `MTP_ARTIFACT_ROOT` | 证据、下载文件、报告和历史记录目录 |
| `PLAYWRIGHT_BROWSERS_PATH` | 共享 Playwright 浏览器内核目录 |
| `MTP_WEB_USERNAME` | Web 登录用户名，启动 Web 服务时必填 |
| `MTP_WEB_PASSWORD` | Web 登录密码，启动 Web 服务时必填 |
| `MTP_SESSION_SECRET` | Session 签名密钥，启动 Web 服务时必填 |
| `MTP_HTTP_PORT` | Compose 对外端口，默认 `8080` |
| `MTP_MAX_CONCURRENT_RUNS` | 后台任务并发数，默认 `1` |
| `MTP_MAX_UPLOAD_BYTES` | 单文件上限，默认 `2097152`（2 MiB） |
| `MTP_MAX_UPLOAD_FILES` | 单次上传文件数上限，默认 `20` |
| `MTP_COOKIE_SECURE` | 是否仅通过 HTTPS 发送 Session Cookie |

默认安全策略包括：

- SSH、MySQL 使用目标白名单；
- MySQL 写操作需要 `--allow-write`，并限制最大影响行数；
- HTTP 阻止云元数据等危险地址，并限制响应大小和重定向次数；
- Playwright 执行 JavaScript 需要用例明确设置 `allow_js: true`；
- 密码、Token、Cookie 等字段写入报告前会脱敏。

仓库配置不得写入真实凭据。账号密码由运行环境或单次测试用例注入。

## contracts 依赖

Schema、校验器和共享数据模型来自独立的 `mtp-contracts-core` 包，本项目不保存契约
副本。`pyproject.toml` 固定到 core 的 Git tag，升级契约时需要显式修改 tag 并提交
更新后的 `uv.lock`。

## HTTP 接口

除健康检查和登录外，页面、任务 API、报告和证据均需要认证；修改操作还需要 CSRF Token。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET/POST | `/login` | 登录页面和登录提交 |
| POST | `/logout` | 退出登录 |
| GET | `/` | 任务列表 |
| GET | `/runs/new` | 上传任务页面 |
| GET | `/runs/{run_id}` | 任务详情页面 |
| POST | `/api/runs` | 上传用例并创建任务 |
| GET | `/api/runs` | 查询任务列表 |
| GET | `/api/runs/{run_id}` | 查询任务状态与结果 |
| POST | `/api/runs/{run_id}/cancel` | 取消任务 |
| GET | `/api/runs/{run_id}/reports/{filename}` | 下载报告 |
| GET | `/api/runs/{run_id}/evidence/{path}` | 下载证据 |

Web 第一版面向单机单容器部署，SQLite 不用于多副本并行部署。默认一次只执行一个任务；即使提高并发数，也应先确认浏览器、目标环境和报告存储能够承受并发访问。

## 验证

```bash
uv run --frozen --extra dev pytest -q
docker compose config
```
