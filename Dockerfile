FROM python:3.12-slim

ARG UV_VERSION=0.12.5
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "uv==$UV_VERSION"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 先装依赖（利用层缓存）。
# 显式把 ssh / mysql / playwright 三个运行能力带上 ——
# 不装的话，容器里这些工具会在运行时才报「依赖缺失」。
# 想压镜像可以改成：--extra ssh --extra mysql（按需裁剪）。
#
# 依赖层只依赖 pyproject.toml / uv.lock：改 src 不会触发重装。
# --mount=type=cache 把 uv 的下载缓存（约定路径 /root/.cache/uv）挂到宿主机的
# 构建缓存上：锁文件变更导致重装时，wheel 与 mtp-contracts-core 的 git 依赖都从
# 缓存取，不再重新下载；缓存不进镜像层。需要构建机装了 buildx（已装 v0.37.1）。
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen \
    --extra ssh --extra mysql --extra playwright \
    --no-install-project

ENV PATH="/app/.venv/bin:$PATH" \
    MTP_ARTIFACT_ROOT=/app/artifacts \
    PLAYWRIGHT_BROWSERS_PATH=/app/browsers \
    PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000

# 浏览器层必须放在 COPY src 之前：业务源码变化不会迫使 Docker 再下载浏览器。
#
# 但**只要 pyproject.toml / uv.lock 变过**（改版本号、relock 都算），它上面那层
# `uv sync` 就会重建，这一层跟着重建 —— 之前每次都要重新下载 Chromium 与
# `--with-deps` 的系统依赖（实测 83s）。下面把「下载物」也挂到构建缓存上：
#
#   /root/.cache/browser-cache  Playwright 的浏览器下载缓存（跨层重建复用）
#   /var/cache/apt              apt 已下载的 deb（--with-deps 不再重下）
#   /var/lib/apt/lists          apt 索引（省掉每次 apt-get update）
#
# 缓存必须再拷进镜像：运行时要用浏览器，它不能只活在缓存挂载里（挂载不进镜像）。
# 首次构建照常联网；之后重建这一层基本只剩本地拷贝。
# 网络瞬断时最多重试三次。
ARG MTP_INSTALL_BROWSER=1
RUN --mount=type=cache,target=/root/.cache/browser-cache \
    --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt/lists \
    if [ "$MTP_INSTALL_BROWSER" = "1" ]; then \
      for attempt in 1 2 3; do \
        PLAYWRIGHT_BROWSERS_PATH=/root/.cache/browser-cache \
          playwright install --with-deps chromium && break; \
        [ "$attempt" = 3 ] && exit 1; \
      done; \
      mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"; \
      cp -a /root/.cache/browser-cache/. "$PLAYWRIGHT_BROWSERS_PATH"/; \
    fi

# 再装本包源码
COPY src ./src
COPY mtp_config.yaml ./mtp_config.yaml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen \
    --extra ssh --extra mysql --extra playwright

EXPOSE 8080
CMD ["uvicorn", "mtp_platform.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
