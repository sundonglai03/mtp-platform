# mtp-platform

读取规范测试用例，直接调用 SSH、MySQL、Playwright 和 HTTP 工具，最后生成证据与测试报告。

它是 CLI / Docker 批任务执行器，不依赖 MCP 协议，也不是常驻 HTTP 服务。

## 快速开始

### 本地运行

```bash
uv sync --frozen --all-extras
uv run --frozen mtp doctor
uv run --frozen mtp validate tests/cases/valid
uv run --frozen mtp run tests/cases/valid --out artifacts/reports
```

### Docker

```bash
docker compose build
docker compose run --rm mtp-platform doctor
docker compose run --rm mtp-platform validate /app/cases/valid
docker compose run --rm mtp-platform run /app/cases --office
```

任务完成后容器退出，报告保留在宿主机的 `artifacts/`。镜像名为 `mtp-platform:0.1.0`，容器名为 `mtp-platform`。

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

完整参数使用 `mtp --help` 或 `mtp <子命令> --help` 查看。

## 项目结构

```text
src/mtp_platform/
├── engine/      # 编排、断言、证据和执行历史
├── tools/       # SSH、MySQL、Playwright、HTTP 直连工具
├── reporting/   # JSON、JUnit、HTML、Excel、Word 报告
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

## 当前部署边界

当前每次 `mtp run` 对应一个测试任务。若以后需要网页或 Agent 通过 HTTP 提交任务，应在本项目外层增加 API、任务队列和执行 Worker；不要把任务执行能力放进 `mtp-contracts-mcp`。

## 验证

```bash
uv run --frozen --extra dev pytest -q
docker compose config
```
