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
# --mount=type=cache 把 uv 的下载缓存挂到宿主机的构建缓存上（默认路径就是
# /root/.cache/uv，跟随约定不自定义）：依赖层因为 uv.lock 变更 / 换构建机 /
# 清过层缓存而重建时，wheel 直接从缓存取，不再回 PyPI 下载；mtp-contracts-core
# 的 git 依赖也一并缓存在这里，不用每次重新 clone GitHub。缓存不进镜像层，
# 首次构建照常联网。
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
# 新机器首次构建仍会下载；网络瞬断时最多重试三次。
ARG MTP_INSTALL_BROWSER=1
RUN if [ "$MTP_INSTALL_BROWSER" = "1" ]; then \
      for attempt in 1 2 3; do \
        playwright install --with-deps chromium && break; \
        [ "$attempt" = 3 ] && exit 1; \
      done; \
    fi

# 再装本包源码
COPY src ./src
COPY mtp_config.yaml ./mtp_config.yaml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen \
    --extra ssh --extra mysql --extra playwright

EXPOSE 8080
CMD ["uvicorn", "mtp_platform.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
