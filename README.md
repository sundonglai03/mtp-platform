# mtp-platform

`mtp-platform` 是一个带登录的常驻 HTTP 测试服务。用户在浏览器上传唯一的 JSON 测试套件，后台执行后查看任务进度、每个用例结果、第一条失败原因；展开单个用例还能看到逐步骤输出与断言明细，以及浏览器截图和步骤文本证据。

不提供 CLI；不接收 YAML/YML 测试用例；不生成 Excel、Word、HTML、JUnit 或 JSON 报告文件。

## 使用

```bash
docker compose up -d --build
```

打开 `http://<服务器IP>:8080`，使用默认账号 `admin` / `123456` 登录。容器状态和启动日志通过以下命令查看：

```bash
docker compose ps
docker compose logs mtp-platform
```

一次只能上传一个 `.json` 测试套件文件，文件名可自由命名。根节点必须是对象，`cases` 必须是非空数组；每个元素都是完整测试用例，且 `id` 在套件内唯一。只要其中任意一个用例无效，任务不会创建。

连接 MySQL、SSH 等外部系统所需的测试凭证随 JSON 套件传入；平台配置和 tools 不保存凭证。建议集中放在每个用例的 `secrets` 对象中并通过 `{{ secrets.xxx }}` 引用。任务结果和证据会对凭证字段及已登记的凭证值脱敏。

```json
{
  "cases": [
    {
      "schema_version": 1,
      "id": "login-success",
      "title": "正常登录",
      "secrets": {"ssh_password": "test-password"},
      "steps": [
        {
          "id": "open",
          "action": "playwright.navigate",
          "args": {"url": "https://example.test/login"}
        }
      ]
    }
  ]
}
```

任务详情显示状态、进度、开始/结束时间、耗时，以及每个用例的状态、耗时、断言通过数和第一条失败原因。用例行可展开，展开时按需调用 `GET /api/runs/{run_id}/cases/{case_id}` 拉取该用例的步骤（含 stdout/stderr 输出、错误、重试次数）和断言（期望值/实际值）。超长输出会截断并标注，单个用例明细过大时只保留结构、省略步骤正文。

证据保存到 `artifacts/runs/<run_id>/evidence/`：浏览器步骤失败产生的 PNG 截图和文本快照，以及 ssh / mysql / http 等**非浏览器步骤的 stdout/stderr 文本**（默认采集；若要关闭，在该步骤上写 `"evidence": []`）。详情页会预览 PNG，其他证据提供下载链接。

SQLite 文件 `artifacts/mtp-platform.sqlite3` 是唯一的任务结果来源。它保存用例摘要（状态、耗时、断言计数、第一条失败点）、逐用例的步骤与断言明细，以及证据的路径、MIME 类型和文件大小；不嵌入图片内容。明细不随列表接口返回，只由用例详情接口按需读取，避免响应随步骤输出膨胀。

## HTTP 接口

登录后可使用以下接口：

```text
GET  /login
POST /login
POST /logout

GET  /
GET  /runs/new
GET  /runs/{run_id}

POST /api/runs
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/cases/{case_id}
POST /api/runs/{run_id}/cancel
GET  /api/runs/{run_id}/evidence/{path}
```

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `MTP_WEB_USERNAME` | Web 登录用户名 |
| `MTP_WEB_PASSWORD` | Web 登录密码 |
| `MTP_SESSION_SECRET` | Session 签名密钥 |
| `MTP_ARTIFACT_ROOT` | SQLite、上传文件和证据目录 |
| `MTP_HTTP_PORT` | Compose 对外端口，默认 `8080` |
| `MTP_MAX_CONCURRENT_RUNS` | 后台任务并发数，默认 `1` |
| `MTP_MAX_UPLOAD_BYTES` | 套件文件大小上限，默认 `2097152`（2 MiB） |
| `MTP_COOKIE_SECURE` | 是否只通过 HTTPS 发送 Session Cookie |

创建任务时可以选择允许 MySQL 写操作。未勾选时，写操作会作为任务错误返回页面和 API。

## 验证

```bash
uv run --frozen --extra dev pytest -q
docker compose config
```
