"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

// 耗时只信服务端算好的 duration_ms：以前是拿浏览器的 Date.now() 去减服务器给的
// started_at，两台机器时钟一有偏差，任务刚起来就会显示好几秒。
// 这里保留一个基准值，运行中的任务在两次轮询之间用本地时间差平滑推进。
let durationBaseline = {ms: null, at: 0};

function formatDuration(running) {
  if (durationBaseline.ms === null) return "-";
  const elapsed = durationBaseline.ms + (running ? performance.now() - durationBaseline.at : 0);
  const seconds = Math.floor(Math.max(0, elapsed) / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

// 服务端存的是 UTC，展示统一转成本地时区；原始值放在 title 里备查
function formatTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ` +
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`
  );
}
const statusLabel = {
  queued: "排队中",
  running: "执行中",
  passed: "成功",
  failed: "失败",
  error: "错误",
  cancelled: "已取消",
};
const stepStatusLabel = {
  pending: "待执行",
  running: "执行中",
  passed: "通过",
  failed: "失败",
  error: "错误",
  skipped: "跳过",
  cancelled: "已取消",
};
const phaseLabel = {
  fixtures: "前置数据",
  preconditions: "前置条件",
  steps: "步骤",
  postconditions: "后置清理",
  cleanup: "资源清理",
};
const kindLabel = {
  screenshot: "截图",
  snapshot: "页面快照",
  console: "Console 日志",
  network: "Network 日志",
  output: "步骤输出",
  text: "文本",
};
const IMAGE_MIME = /^image\/(png|jpe?g|gif|webp|bmp)$/;
const TEXT_MIME = /^(text\/|application\/(json|xml|x-ndjson|x-yaml|yaml|x-www-form-urlencoded))/;
const TEXT_EXT = /\.(txt|log|json|md|markdown|xml|ya?ml|csv|ndjson)$/i;
// 预览只渲染前 2 万字符：步骤输出动辄十万字符，整段塞进 DOM 会把页面拖死。
const PREVIEW_TEXT_LIMIT = 20000;
// 输出类字段已经单独渲染过，原始数据里不再重复一遍。
const OUTPUT_KEYS = ["command", "stdout", "stderr", "text", "rows", "exit_code"];

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value == null ? "" : String(value);
  return node.innerHTML;
}

// 注意：escapeHtml 走 innerHTML 序列化，不会转义引号，不能用在属性值上。
function escapeAttr(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatValue(value) {
  if (value === undefined || value === null) return "";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function statusVisual(status) {
  if (status === "passed") return "passed";
  if (status === "failed" || status === "error") return "failed";
  if (status === "skipped") return "cancelled";
  if (status === "pending") return "queued";
  if (status === "running") return "running";
  return "cancelled";
}

const runForm = document.getElementById("run-form");
if (runForm) {
  runForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = runForm.querySelector('button[type="submit"]');
    const error = document.getElementById("form-error");
    button.disabled = true;
    error.classList.add("hidden");
    try {
      const response = await fetch(runForm.action, {method: "POST", body: new FormData(runForm)});
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
      window.location.assign(body.url);
    } catch (exception) {
      error.textContent = exception.message;
      error.classList.remove("hidden");
      button.disabled = false;
    }
  });
}

const detail = document.getElementById("run-detail");
if (detail) {
  const runId = detail.dataset.runId;
  const terminal = new Set(["passed", "failed", "error", "cancelled"]);

  const openCases = new Set();
  const openSteps = new Set();
  const openEvidence = new Set();
  const detailCache = new Map();
  const previewCache = new Map();
  let casesSignature = "";

  // ---- 证据 ---------------------------------------------------------------
  function isImage(item) {
    return IMAGE_MIME.test(item.mime_type || "");
  }

  function isText(item) {
    const mime = item.mime_type || "";
    if (TEXT_MIME.test(mime)) return true;
    if (mime && mime !== "application/octet-stream") return false;
    return TEXT_EXT.test(item.path || "");
  }

  // 预览链接：inline=1 让浏览器直接渲染，而不是弹下载。
  function previewUrl(url) {
    return `${url}${url.includes("?") ? "&" : "?"}inline=1`;
  }

  function evidenceKey(item) {
    return item.path || item.url || formatValue(item);
  }

  function evidenceName(item) {
    const parts = String(item.path || "").split("/");
    return parts[parts.length - 1] || item.path || "证据";
  }

  // 新的任务明细里后端会直接给 url；历史任务的明细只有 path，
  // 这里兜底拼出可访问地址，否则会渲染成「该证据没有可访问的文件」。
  function evidenceUrl(item) {
    if (item.url) return item.url;
    const path = item.path || "";
    if (!path) return "";
    const encoded = path.split("/").map(encodeURIComponent).join("/");
    return `/api/runs/${encodeURIComponent(runId)}/evidence/${encoded}`;
  }

  function renderEvidenceItem(item) {
    const key = evidenceKey(item);
    const label = kindLabel[item.kind] || item.kind || "证据";
    const size = item.bytes ?? item.size ?? 0;
    return `<details class="evidence-item" data-key="${escapeAttr(key)}" data-url="${escapeAttr(evidenceUrl(item))}" data-mime="${escapeAttr(item.mime_type || "")}" data-file="${escapeAttr(item.path || "")}"${openEvidence.has(key) ? " open" : ""}>
      <summary>
        <span class="evidence-kind">${escapeHtml(label)}</span>
        <code>${escapeHtml(evidenceName(item))}</code>
        <span class="muted">${escapeHtml(formatBytes(size))}</span>
        <span class="muted">${escapeHtml(item.mime_type || "")}</span>
      </summary>
      <div class="evidence-body"><p class="muted">展开后加载预览…</p></div>
    </details>`;
  }

  function bindEvidenceItem(node) {
    const load = () => loadEvidence(node);
    node.addEventListener("toggle", () => {
      if (node.open) {
        openEvidence.add(node.dataset.key);
        load();
      } else {
        openEvidence.delete(node.dataset.key);
      }
    });
    if (node.open) load();
  }

  async function loadEvidence(node) {
    const body = node.querySelector(".evidence-body");
    if (!body) return;
    const key = node.dataset.key;
    if (previewCache.has(key)) {
      body.innerHTML = previewCache.get(key);
      return;
    }
    const url = node.dataset.url;
    const file = node.dataset.file;
    if (!url) {
      body.innerHTML = '<p class="muted">该证据没有可访问的文件。</p>';
      return;
    }
    const actions = `<p class="evidence-actions"><a href="${escapeAttr(url)}" download>下载原文件</a> · <a href="${escapeAttr(previewUrl(url))}" target="_blank" rel="noopener">新标签打开</a></p>`;
    const item = {mime_type: node.dataset.mime, path: file};

    if (isImage(item)) {
      // 图片直接内联，不需要额外请求；点击在新标签看原图。
      body.innerHTML = `<a href="${escapeAttr(previewUrl(url))}" target="_blank" rel="noopener"><img class="evidence-image" src="${escapeAttr(url)}" alt="${escapeAttr(file)}" loading="lazy"></a>${actions}`;
      previewCache.set(key, body.innerHTML);
      return;
    }
    if (!isText(item)) {
      body.innerHTML = `<p class="muted">该类型暂不支持内联预览。</p>${actions}`;
      previewCache.set(key, body.innerHTML);
      return;
    }

    body.innerHTML = '<p class="muted">正在加载预览…</p>';
    try {
      const response = await fetch(previewUrl(url));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const text = await response.text();
      const shown = text.length > PREVIEW_TEXT_LIMIT
        ? `${text.slice(0, PREVIEW_TEXT_LIMIT)}\n…（预览截断，全文 ${text.length} 字符，可下载查看）`
        : text;
      body.innerHTML = `<pre class="evidence-text">${escapeHtml(shown)}</pre>${actions}`;
      previewCache.set(key, body.innerHTML);
    } catch (exception) {
      body.innerHTML = `<p class="muted">预览加载失败：${escapeHtml(exception.message)}</p>${actions}`;
    }
  }

  // ---- 步骤与断言 ---------------------------------------------------------
  function stepOutput(data) {
    if (!data || typeof data !== "object") return "";
    const parts = [];
    if (data.command) parts.push(`$ ${data.command}`);
    ["stdout", "stderr", "text"].forEach((key) => {
      if (data[key]) parts.push(`--- ${key} ---\n${data[key]}`);
    });
    if (Array.isArray(data.rows)) parts.push(`--- rows ---\n${JSON.stringify(data.rows, null, 2)}`);
    if (data.exit_code !== undefined && data.exit_code !== null) parts.push(`[exit] ${data.exit_code}`);
    return parts.join("\n\n");
  }

  function stepExtraData(data) {
    if (!data || typeof data !== "object") return "";
    const rest = {};
    Object.keys(data).forEach((key) => {
      if (OUTPUT_KEYS.includes(key)) return;
      const value = data[key];
      if (value === null || value === undefined || value === "") return;
      rest[key] = value;
    });
    return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : "";
  }

  function stepKey(caseId, step, index) {
    return [caseId, step.phase || "steps", step.index ?? index, step.step_id].join(":");
  }

  function renderStep(caseId, step, index) {
    const visual = statusVisual(step.status);
    const key = stepKey(caseId, step, index);
    const attempts = step.attempts > 1 ? ` · 尝试 ${step.attempts} 次` : "";
    const phase = step.phase && step.phase !== "steps"
      ? `<span class="phase-tag">${escapeHtml(phaseLabel[step.phase] || step.phase)}</span>`
      : "";
    const failure = step.error?.message
      ? `<div class="alert error compact">${escapeHtml(step.error.message)}</div>`
      : "";
    const output = stepOutput(step.data);
    const extra = stepExtraData(step.data);
    const evidence = step.evidence || [];
    const evidenceBlock = evidence.length
      ? `<h4>本步骤证据（${evidence.length}）</h4><div class="evidence-list">${evidence.map(renderEvidenceItem).join("")}</div>`
      : "";
    return `<details class="step-item status-border-${visual}" data-step-key="${escapeAttr(key)}"${openSteps.has(key) ? " open" : ""}>
      <summary>
        <span class="step-index">${escapeHtml(String((Number(step.index) || index) + 1))}</span>
        <code class="step-id">${escapeHtml(step.step_id)}</code>
        <span class="step-action">${escapeHtml(step.action || "")}</span>
        ${phase}
        <span class="status status-${visual}">${escapeHtml(stepStatusLabel[step.status] || step.status)}</span>
        <span class="muted">${escapeHtml(step.duration_ms ?? 0)}ms${escapeHtml(attempts)}</span>
        ${evidence.length ? `<span class="muted">证据 ${evidence.length}</span>` : ""}
        <span class="step-summary">${escapeHtml(step.summary || "")}</span>
      </summary>
      <div class="step-detail">
        ${failure}
        <p class="muted step-times">${escapeHtml(formatTime(step.started_at))} → ${escapeHtml(formatTime(step.finished_at))}</p>
        ${output ? `<pre class="step-output">${escapeHtml(output)}</pre>` : ""}
        ${extra ? `<details class="raw-json"><summary>步骤原始数据</summary><pre class="step-output">${escapeHtml(extra)}</pre></details>` : ""}
        ${evidenceBlock}
      </div>
    </details>`;
  }

  function renderSteps(caseId, steps, emptyText) {
    if (!steps.length) return `<p class="muted">${escapeHtml(emptyText)}</p>`;
    return `<div class="step-list">${steps.map((step, index) => renderStep(caseId, step, index)).join("")}</div>`;
  }

  function renderCaseDetail(payload, caseId) {
    const steps = payload.steps || [];
    const cleanup = payload.cleanup || [];
    const assertions = (payload.assertions || []).map((item) => `<tr>
      <td><code>${escapeHtml(item.id)}</code><div class="muted">${escapeHtml(item.type || "")}</div></td>
      <td><span class="status status-${item.passed ? "passed" : "failed"}">${item.passed ? "通过" : "失败"}</span></td>
      <td>${escapeHtml(item.description || "")}</td>
      <td><code class="assertion-actual">${escapeHtml(formatValue(item.expected))}</code></td>
      <td><code class="assertion-actual">${escapeHtml(formatValue(item.actual))}</code></td>
    </tr>`).join("");

    const warnings = (payload.warnings || []).length
      ? `<h3>告警</h3><ul>${payload.warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
      : "";

    return `${payload.error?.message ? `<div class="alert error">${escapeHtml(payload.error.message)}</div>` : ""}
      <h3>步骤（${steps.length}）</h3>
      ${renderSteps(caseId, steps, "无步骤。")}
      <h3>断言</h3>
      ${assertions ? `<div class="table-wrap"><table><thead><tr><th>断言</th><th>结果</th><th>说明</th><th>期望</th><th>实际</th></tr></thead><tbody>${assertions}</tbody></table></div>` : '<p class="muted">无断言。</p>'}
      ${cleanup.length ? `<h3>资源清理（${cleanup.length}）</h3>${renderSteps(caseId, cleanup, "无清理步骤。")}` : ""}
      ${warnings}
      ${payload.truncated ? '<p class="muted">步骤输出过大，已省略正文，完整内容见证据文件。</p>' : ""}`;
  }

  function bindCaseDetail(body, caseId) {
    body.querySelectorAll("details.step-item").forEach((node) => {
      node.addEventListener("toggle", () => {
        if (node.open) {
          openSteps.add(node.dataset.stepKey);
        } else {
          openSteps.delete(node.dataset.stepKey);
        }
      });
    });
    body.querySelectorAll("details.evidence-item").forEach(bindEvidenceItem);
  }

  function paintCaseDetail(body, payload, caseId) {
    body.innerHTML = renderCaseDetail(payload, caseId);
    bindCaseDetail(body, caseId);
  }

  async function loadCaseDetail(node, caseId) {
    const body = node.querySelector(".case-detail");
    if (!body) return;
    if (detailCache.has(caseId)) {
      paintCaseDetail(body, detailCache.get(caseId), caseId);
      return;
    }
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}`);
      if (!response.ok) {
        body.innerHTML = '<p class="muted">该用例暂无详情，任务可能还在执行。</p>';
        return;
      }
      const payload = await response.json();
      detailCache.set(caseId, payload);
      paintCaseDetail(body, payload, caseId);
    } catch (exception) {
      body.innerHTML = `<p class="muted">详情加载失败：${escapeHtml(exception.message)}</p>`;
    }
  }

  function renderCases(cases) {
    const signature = JSON.stringify(cases);
    if (signature === casesSignature) return;
    casesSignature = signature;
    const container = document.getElementById("case-results");
    if (!cases.length) {
      container.innerHTML = '<p class="muted">等待执行结果。</p>';
      return;
    }
    container.innerHTML = cases.map((item) => {
      const total = item.counts?.assertions_total || 0;
      const failed = item.counts?.assertions_failed || 0;
      const failure = item.first_failure;
      const hint = failure
        ? `<span class="failure-hint">${escapeHtml(failure.step_id ? `${failure.step_id}: ` : "")}${escapeHtml(failure.message)}</span>`
        : "";
      return `<details class="case-item" data-case-id="${escapeAttr(item.case_id)}"${openCases.has(item.case_id) ? " open" : ""}>
        <summary>
          <span class="case-id">${escapeHtml(item.case_id)}</span>
          <span class="status status-${escapeHtml(item.status)}">${escapeHtml(statusLabel[item.status] || item.status)}</span>
          <span class="muted">${escapeHtml(item.duration_ms)}ms</span>
          ${total ? `<span class="muted">断言 ${total - failed}/${total}</span>` : ""}
          ${hint}
        </summary>
        <div class="case-detail muted">展开查看步骤与断言明细。</div>
      </details>`;
    }).join("");
    container.querySelectorAll("details.case-item").forEach((node) => {
      node.addEventListener("toggle", () => {
        if (node.open) {
          openCases.add(node.dataset.caseId);
          loadCaseDetail(node, node.dataset.caseId);
        } else {
          openCases.delete(node.dataset.caseId);
        }
      });
      if (node.open) loadCaseDetail(node, node.dataset.caseId);
    });
  }

  function render(run) {
    const statusNode = document.getElementById("run-status");
    statusNode.textContent = statusLabel[run.status] || run.status;
    statusNode.className = `status status-${run.status}`;
    document.getElementById("run-progress").textContent = `${run.cases_done}/${run.cases_total}`;
    const done = terminal.has(run.status);
    document.getElementById("run-started").textContent = formatTime(run.started_at);
    document.getElementById("run-finished").textContent = formatTime(run.finished_at);
    if (typeof run.duration_ms === "number") {
      durationBaseline = {ms: run.duration_ms, at: performance.now()};
    }
    const durationNode = document.getElementById("run-duration");
    durationNode.textContent = formatDuration(!done);
    durationNode.title = run.started_at
      ? `服务端计时：started_at=${run.started_at} finished_at=${run.finished_at || "(未结束)"}`
      : "";
    const error = document.getElementById("run-error");
    error.textContent = run.first_failure?.message || "";
    error.classList.toggle("hidden", !run.first_failure);
    renderCases(run.cases || []);
    document.getElementById("cancel-run").disabled = terminal.has(run.status);
    return terminal.has(run.status);
  }

  async function refresh() {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    if (!response.ok) return true;
    return render(await response.json());
  }

  document.getElementById("cancel-run").addEventListener("click", async () => {
    await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      headers: {"X-CSRF-Token": csrf},
    });
    await refresh();
  });

  refresh().then((done) => {
    if (done) return;
    const timer = setInterval(async () => {
      if (await refresh()) clearInterval(timer);
    }, 2000);
  });
}
