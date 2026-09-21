"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
const statusLabel = {
  queued: "排队中",
  running: "执行中",
  passed: "成功",
  failed: "失败",
  error: "错误",
  cancelled: "已取消",
};

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value == null ? "" : String(value);
  return node.innerHTML;
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

  function renderEvidence(entries) {
    const element = document.getElementById("evidence-links");
    element.innerHTML = entries.length
      ? entries.map((item) => item.mime_type === "image/png"
        ? `<a href="${escapeHtml(item.url)}" target="_blank"><img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.path)}" loading="lazy">${escapeHtml(item.path)}</a>`
        : `<a href="${escapeHtml(item.url)}" download>${escapeHtml(item.path)}</a>`).join("")
      : '<span class="muted">暂无</span>';
  }

  const openCases = new Set();
  const detailCache = new Map();
  let casesSignature = "";

  function formatValue(value) {
    if (value === undefined || value === null) return "";
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }

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

  function renderCaseDetail(payload) {
    const steps = (payload.steps || []).map((step) => {
      const visual = step.status === "passed" ? "passed" : step.status === "skipped" ? "cancelled" : "failed";
      const attempts = step.attempts > 1 ? ` · 尝试 ${step.attempts} 次` : "";
      const failure = step.error?.message ? `<div class="failure-hint">${escapeHtml(step.error.message)}</div>` : "";
      const output = stepOutput(step.data);
      return `<tr>
        <td><code>${escapeHtml(step.step_id)}</code><div class="muted">${escapeHtml(step.action || "")}</div></td>
        <td><span class="status status-${visual}">${escapeHtml(step.status)}</span></td>
        <td class="muted">${escapeHtml(step.duration_ms)}ms${escapeHtml(attempts)}</td>
        <td>${escapeHtml(step.summary || "")}${failure}${output ? `<pre class="step-output">${escapeHtml(output)}</pre>` : ""}</td>
      </tr>`;
    }).join("");

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
      <h3>步骤</h3>
      ${steps ? `<div class="table-wrap"><table><thead><tr><th>步骤</th><th>状态</th><th>耗时</th><th>摘要与输出</th></tr></thead><tbody>${steps}</tbody></table></div>` : '<p class="muted">无步骤。</p>'}
      <h3>断言</h3>
      ${assertions ? `<div class="table-wrap"><table><thead><tr><th>断言</th><th>结果</th><th>说明</th><th>期望</th><th>实际</th></tr></thead><tbody>${assertions}</tbody></table></div>` : '<p class="muted">无断言。</p>'}
      ${warnings}
      ${payload.truncated ? '<p class="muted">步骤输出过大，已省略正文。</p>' : ""}`;
  }

  async function loadCaseDetail(node, caseId) {
    const body = node.querySelector(".case-detail");
    if (!body) return;
    if (detailCache.has(caseId)) {
      body.innerHTML = renderCaseDetail(detailCache.get(caseId));
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
      body.innerHTML = renderCaseDetail(payload);
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
      return `<details class="case-item" data-case-id="${escapeHtml(item.case_id)}"${openCases.has(item.case_id) ? " open" : ""}>
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
    document.getElementById("run-started").textContent = run.started_at || "-";
    document.getElementById("run-finished").textContent = run.finished_at || "-";
    const started = Date.parse(run.started_at);
    const finished = Date.parse(run.finished_at) || Date.now();
    const elapsed = Number.isNaN(started) ? "-" : `${Math.max(0, Math.floor((finished - started) / 1000))} 秒`;
    document.getElementById("run-duration").textContent = elapsed;
    const error = document.getElementById("run-error");
    error.textContent = run.first_failure?.message || "";
    error.classList.toggle("hidden", !run.first_failure);
    renderCases(run.cases || []);
    renderEvidence(run.evidence || []);
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
