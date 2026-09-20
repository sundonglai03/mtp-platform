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
    document.getElementById("case-results").innerHTML = (run.cases || []).length
      ? `<table><thead><tr><th>用例</th><th>状态</th><th>耗时</th></tr></thead><tbody>${run.cases.map((item) => `<tr><td>${escapeHtml(item.case_id)}</td><td><span class="status status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td><td>${escapeHtml(item.duration_ms)}ms</td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">等待执行结果。</p>';
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
