"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

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

  function renderLinks(elementId, entries) {
    const element = document.getElementById(elementId);
    element.innerHTML = entries.length
      ? entries.map(([label, url]) => `<a href="${escapeHtml(url)}">${escapeHtml(label)}</a>`).join("")
      : '<span class="muted">暂无</span>';
  }

  function firstFailure(item) {
    if (item.error) return item.error.message || JSON.stringify(item.error);
    const step = (item.steps || []).find((value) => !["passed", "success"].includes(value.status));
    if (step) return step.summary || step.error?.message || "步骤失败";
    const assertion = (item.assertions || []).find((value) => !value.passed);
    return assertion ? assertion.message : "";
  }

  function render(run) {
    const statusNode = document.getElementById("run-status");
    statusNode.textContent = run.status;
    statusNode.className = `status status-${run.status}`;
    document.getElementById("run-progress").textContent = `${run.cases_done}/${run.cases_total}`;
    document.getElementById("run-started").textContent = run.started_at || "-";
    document.getElementById("run-finished").textContent = run.finished_at || "-";
    const error = document.getElementById("run-error");
    error.textContent = run.error || "";
    error.classList.toggle("hidden", !run.error);
    document.getElementById("case-results").innerHTML = (run.results || []).length
      ? `<table><thead><tr><th>用例</th><th>状态</th><th>耗时</th><th>失败原因</th></tr></thead><tbody>${run.results.map((item) => `<tr><td>${escapeHtml(item.case_id)}</td><td><span class="status status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td><td>${escapeHtml(item.duration_ms)}ms</td><td>${escapeHtml(firstFailure(item))}</td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">等待执行结果。</p>';
    renderLinks("report-links", Object.entries(run.report_urls || {}));
    renderLinks("evidence-links", (run.evidence || []).map((item) => [item.path, item.url]));
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
