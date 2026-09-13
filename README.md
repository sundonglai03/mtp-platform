# mtp-platform

**自动化测试系统**：接收已经规范化的测试用例，**直连工具**执行并生成报告。

## 职责

- 接收规范用例（由 Agent 经 `mtp-contracts-mcp` 校验、规范化后的 YAML/JSON）；
- 用内联的 **contracts-core** 校验输入；
- `engine` 负责编排、重试、超时与状态管理；
- `tools` **直接调用** SSH / MySQL / Playwright / HTTP / Office 能力（**执行链路不走 MCP**）；
- 收集截图、日志、Trace 与接口响应；
- 生成 JSON / JUnit / HTML / Excel / Word 报告；
- 提供 CLI / HTTP API；
- 作为独立 Linux / Docker 服务部署。

## 组成

```
mtp-platform
├── contracts/        contracts-core 内联副本（自动生成，勿手改）
├── engine/           编排 / 断言 / 证据 / 台账        （待实现）
├── tools/            直连工具：ssh / mysql / playwright / http / office（待实现）
├── reporting/        JSON / JUnit / HTML / Excel / Word（待实现）
└── cli.py            命令行入口 `mtp`
```

## 为什么执行链路不用 MCP

直接调用工具，减少 MCP client/server 进程管理、Streamable HTTP 网络层、额外序列化、
子进程清理与超时/连接故障，也减少部署组件。**MCP 只作为外部复用接口保留**
（见 `mtp-contracts-mcp`），不作为执行平台的核心依赖。

## contracts-core 内联（必读）

contracts-core 在本项目内是一份**内联副本**（`src/mtp_platform/contracts/`），与
`mtp-contracts-mcp` 里的那份**逐字节一致**（副本内部全部使用相对导入，不含包名）。

- **要改 contracts**：改本项目里的 `src/mtp_platform/contracts/`（这是唯一事实来源），
  然后运行同步脚本推到另一个项目：
  ```bash
  uv run python scripts/sync_contracts.py
  ```
- 副本带 `_sync_manifest.json`；`tests/test_contracts_integrity.py` 会在副本被手改或忘记同步时失败。

## 本地开发（uv）

```bash
uv sync --extra dev          # 建 .venv 并安装（含 dev 依赖）
uv run pytest -q             # 跑测试
uv run mtp version
uv run mtp validate tests/cases/valid
```

## 容器部署（独立 compose）

```bash
docker compose build
docker compose run --rm mtp-platform validate /app/cases/valid
```

产物默认落在 `artifacts/`（`MTP_ARTIFACT_ROOT` 可改）。两个系统**各自独立**部署，
不共用 compose。
