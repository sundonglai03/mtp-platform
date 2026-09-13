# mtp-platform

**自动化测试系统**：接收已经规范化的测试用例，**直连工具**执行并生成报告。

## 职责

- 接收规范用例（由 Agent 经 `mtp-contracts-mcp` 校验、规范化后的 YAML/JSON）；
- 用 `contracts-core`（`mtp-contracts`）校验输入；
- `engine` 负责编排、重试、超时与状态管理；
- `tools` **直接调用** SSH / MySQL / Playwright / HTTP / Office 能力（**执行链路不走 MCP**）；
- 收集截图、日志、Trace 与接口响应；
- 生成 JSON / JUnit / HTML / Excel / Word 报告；
- 提供 CLI 或 HTTP API；
- 作为独立 Linux / Docker 服务部署。

## 组成

```
mtp-platform
├── mtp-contracts     （固定版本依赖，共享内核）
├── engine            （编排 / 断言 / 证据 / 台账）
├── tools             （直连工具：ssh / mysql / playwright / http / office）
├── reporting         （JSON / JUnit / HTML / Excel / Word）
├── cli               （命令行入口 `mtp`）
└── Docker / CI
```

## 为什么执行链路不用 MCP

自动化测试平台直接调用工具，减少 MCP client/server 进程管理、Streamable HTTP 网络层、
额外序列化、子进程清理与超时/连接故障，也减少部署组件。
**MCP 只作为外部复用接口保留**（`mtp-contracts-mcp`），不作为执行平台的核心依赖。

## 不包含

Agent 推理、DOCX 自然语言理解、contracts MCP 的远程服务端、外部 MCP server 源码。

## 本地构建 / 安装

```bash
# 1) 先构建上游 contracts wheel
(cd ../mtp-contracts && .venv/bin/python -m pip wheel . -w dist --no-deps)

# 2) 建 venv 并按固定版本安装
python -m venv .venv
.venv/bin/pip install -e ".[dev]" --find-links ../mtp-contracts/dist
.venv/bin/python -m pytest -q
```

## 运行

```bash
.venv/bin/mtp --help
```
