# MTP 测试用例 Agent 指引

调用 `mtp-contracts-mcp`，对准备提交给 `mtp-platform` 的 JSON 测试用例做唯一入口的规范校验。

## 服务

- MCP endpoint：`<MTP_CONTRACTS_MCP_URL>/mcp`
- 唯一工具：`build_suite`

## 调用规则

1. 只调用 `build_suite`，不调用或假设任何其他工具。
2. 参数必须是一个 JSON 对象，且仅按以下形状传入：

   ```json
   {
     "cases": [
       {
         "schema_version": 1,
         "id": "case-id",
         "title": "用例标题",
         "steps": [
           {
             "id": "open-page",
             "action": "playwright.navigate",
             "args": {
               "url": "http://target.example/"
             }
           }
         ]
       }
     ]
   }
   ```

3. `cases` 必须是非空 JSON 数组；每一项必须是一个 JSON 用例对象。
4. 不得传文件路径、JSON 文件内容字符串、YAML/YML、Markdown 或完整的 `{"suite": {...}}` 包装对象。
5. MCP 不生成、不猜测、不修复业务步骤；它只校验，并且仅在缺失时补 `schema_version: 1`。
6. 同一次调用中的 `case.id` 必须唯一。

## Playwright 用例规则

- `playwright.click`、`type`、`check`、`hover`、`wait_for` 的 `args.target` 必须是可执行的 CSS 或 Playwright selector。
- 禁止把自然语言描述当作 `target`。

错误示例：

```json
{
  "id": "skip-ukey-login",
  "action": "playwright.click",
  "args": {"target": "跳过"}
}
```

正确写法必须基于真实页面 DOM，例如：

```json
{
  "id": "skip-ukey-login",
  "action": "playwright.click",
  "args": {"target": "button:has-text('跳过')"}
}
```

或：

```json
{
  "id": "skip-ukey-login",
  "action": "playwright.click",
  "args": {"target": "text=跳过"}
}
```

对于复选框、表格行、菜单等，也必须给出真实 CSS/Playwright selector，不能写“某行的勾选框”“网关 bypass 设置”之类的描述。

## 处理返回值

始终将工具返回内容解析为 JSON。

成功结构：

```json
{
  "ok": true,
  "suite": {"cases": []},
  "errors": []
}
```

只有 `ok` 为 `true` 时，才能把 `suite.cases` 提交给 `mtp-platform` 执行。

失败结构：

```json
{
  "ok": false,
  "suite": null,
  "errors": [
    {
      "case_index": 0,
      "case_id": "case-id",
      "path": "steps[0].args.target",
      "code": "schema",
      "message": "..."
    }
  ]
}
```

当 `ok` 为 `false`：

1. 根据每一条 `errors` 修复本地 JSON；
2. 重新调用 `build_suite`；
3. 不得提交半成品、绕过校验或自行假定校验已通过。

每个关键 UI 操作后增加 `screenshot` 或 `snapshot`；失败步骤也须保留页面 URL、实际 selector 和截图证据，以便定位页面改版、元素缺失或被遮挡的问题。
