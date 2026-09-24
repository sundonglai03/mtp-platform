# mtp-platform

`mtp-platform` 是一个带登录的常驻 HTTP 测试服务。用户在浏览器上传唯一的 JSON 测试套件，后台执行后查看任务进度、每个用例结果、第一条失败原因；展开单个用例还能看到逐步骤输出、断言明细和浏览器截图。

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

一次只能上传一个 `.json` 测试套件文件，文件名可自由命名。根节点必须是对象，`cases` 必须是非空数组；每个元素都是完整测试用例，且 `id` 在套件内唯一。只要其中任意一个用例无效，任务不会创建。文件带 BOM 也能直接读；如果直接上传 `mtp-contracts-mcp` 的完整返回（含 `ok` / `suite` / `errors`），平台会自动取里面的 `suite`。

上传时按 `mtp-contracts-core` 的**共用动作目录**校验：未知 action、缺必填参数、参数类型不符、重复 id、引用不存在的步骤、引用某步骤不会返回的字段都会带字段路径报错，任一用例不合法就不会创建任务。

连接 MySQL、SSH 等外部系统所需的测试凭证随 JSON 套件传入。`secrets` 是 `{逻辑名: 实际凭证}` —— **直接写真实值，不是环境变量名**，用 `{{ secrets.xxx }}` 引用；平台不从容器环境变量补取密码，tools 每步都从步骤参数拿连接信息、不保存连接配置（mysql 步骤必须传 `args.credentials`）。任务结果、证据、SQLite 与 API 会对敏感字段名与已登记的凭证值脱敏。

注意：**上传的套件文件本身包含凭证**，它保存在服务端的 `artifacts/uploads/` 下。所以真实套件不要提交到 Git，也不要把生产口令写进去 —— 脱敏只覆盖结果与证据，覆盖不了你上传的那个文件。

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
失败会附带机器可读的原因初判（用例定义、前置条件、连接/认证、浏览器超时、断言不符或平台任务异常）和排查提示。断言不符不会直接归结为被测程序缺陷；页面会标出归属待核对的情况。

展开的用例里，**每个步骤一行、可独立展开收起**：收起时只看步骤 id、动作、状态、耗时与摘要（失败步骤默认展开），展开后看错误、完整输出、原始数据和该步骤的证据。

浏览器步骤声明 `evidence: ["screenshot"]` 保存整页 PNG，声明 `evidence: ["snapshot"]` 保存页面标题、URL 和可见文本；浏览器步骤失败时自动补截图和文本快照。ssh / mysql / http 这类步骤不再另存 stdout/stderr 文本——步骤产出在用例详情里仍然可见，只是不再落成证据文件。

截图和文本快照保存到 `artifacts/runs/<run_id>/evidence/<case_id>/<step_id>/`，详情页在对应步骤下直接预览，不需要先下载；每个证据仍保留「下载原文件」链接，接口也支持 `?inline=1` 在浏览器里直接打开（非图片的预览统一按 `text/plain` 返回，避免证据被当成可执行文档）。

SQLite 文件 `artifacts/mtp-platform.sqlite3` 是唯一的任务结果来源。它保存用例摘要（状态、耗时、断言计数、第一条失败点）、逐用例的步骤与断言明细，以及证据的路径、MIME 类型和文件大小；不嵌入图片内容。明细不随列表接口返回，只由用例详情接口按需读取，避免响应随步骤输出膨胀。
终态任务、上传的套件（可能含测试凭证）和证据按 `mtp_config.yaml` 的 `evidence.retention_days` 一起清理，默认保留 14 天。

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

## 用例级会话隔离

一次任务里所有用例**共用同一个浏览器进程**。不隔离的话，用例 A 登录（或跳过 UKey 门禁）之后，用例 B 的「打开页面」会直接落到已登录页面：B 里那段登录 / 门禁路径根本没被检验（假通过），而且 B 单独跑、换个顺序跑，行为就不一样（顺序依赖）。

所以**每个用例开始前，平台会重建浏览器会话**：关掉当前浏览上下文、保留浏览器进程（省一次约 1s 的冷启动）。cookie、localStorage、缓存、页面路由与弹窗状态都不再延续。只有**声明了 `reset_session` 的工具**会被重置 —— 目前就是浏览器；ssh / mysql 的连接不携带跨用例的身份语义，不做重建。

- 整体关掉：`runner.isolate_case_session: false`
- 单个用例明确要复用上一个用例的会话：用例里写 `"reuse_session": true`（结果里会记一条告警，便于回看）
- 隔离失败只告警、不打断用例；浏览器进程掉线时退化成完整重建

## 两层超时（用例写的 timeout 与单步看门狗）

用例里的 `args.timeout` 是**工具自己的**超时（ssh / mysql 按**秒**、playwright 按**毫秒**，与各自底层库一致），平台的单步看门狗默认 30s。两者以前各算各的：用例写 `"timeout": 240`（想让 SSH 轮询等到证书落地），30s 就被看门狗砍掉，报出来的是「步骤超时（30.0s）」——完全指不到真正原因。

现在引擎取「用例声明」（步骤 `timeout_sec` → 用例 `timeout_sec` → `runner.default_step_timeout_sec`）与「适配器声明的内部超时 + 5s 余量」的**较大值**：

- 工具只要实现 `declared_timeout_sec(action, args)`（返回**秒**）即可，ssh / mysql / playwright 已实现；老工具不实现也没关系（按 0 处理，等价旧行为）。
- 因此写用例时**只需把工具自己的超时写对**，不必再为了绕开看门狗额外加步骤级 `timeout_sec`。

## 选择器写错时，报错里会带页面真实 DOM

playwright 步骤超时（选择器没命中）时，错误详情不再只有 `Timeout 15000ms exceeded`，而是直接列出页面上的候选元素与建议选择器：

```
原选择器命中 0 个元素：button:has-text('登录')
页面上含文本「登录」的可见元素（建议改用 text=登录）：
  - input#submi  "登录"
```

意义在于：agent 看不到被测页面，人也不想手动去翻 DOM。契约层（`mtp-contracts-core`）对 `button:has-text('文字')` 这类「元素类型 + 文本」混写选择器会给出 `fragile_target` 忠告，建议改用 `text=文字`（Playwright 的文本选择器同时匹配 `<button>`、`<span>` 与 `<input type=button value=...>`）。

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
