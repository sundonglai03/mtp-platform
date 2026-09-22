"use strict";

// 服务端存的时间戳都是 UTC ISO 字符串，展示统一交给浏览器按**本地时区**渲染。
// 服务端只输出原始值和 data-iso，避免在容器里猜时区（容器通常是 UTC）。
(function () {
  const pad = (n) => String(n).padStart(2, "0");

  function formatTime(value) {
    if (!value) return "-";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return (
      `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ` +
      `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`
    );
  }

  // 把页面里所有 data-iso 元素改写成当地时间，原始值留在 title 里备查。
  function renderTimes(root) {
    (root || document).querySelectorAll("[data-iso]").forEach((node) => {
      const iso = node.dataset.iso;
      if (!iso) return;
      node.textContent = formatTime(iso);
      node.title = iso;
    });
  }

  window.mtpTime = { formatTime, renderTimes };
  renderTimes(document);
})();
