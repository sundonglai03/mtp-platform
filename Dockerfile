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
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --frozen \
    --extra ssh --extra mysql --extra playwright \
    --no-install-project

# 再装本包源码
COPY src ./src
COPY mtp_config.yaml ./mtp_config.yaml
RUN uv sync --no-dev --frozen \
    --extra ssh --extra mysql --extra playwright

ENV PATH="/app/.venv/bin:$PATH" \
    MTP_ARTIFACT_ROOT=/app/artifacts \
    PLAYWRIGHT_BROWSERS_PATH=/app/browsers

# 浏览器内核（只有浏览器用例需要；约 300MB）。
# 不需要就 `--build-arg MTP_INSTALL_BROWSER=0`，镜像会小很多。
ARG MTP_INSTALL_BROWSER=1
RUN if [ "$MTP_INSTALL_BROWSER" = "1" ]; then \
      for attempt in 1 2 3; do \
        playwright install --with-deps chromium && break; \
        [ "$attempt" = 3 ] && exit 1; \
      done; \
    fi

EXPOSE 8080
CMD ["uvicorn", "mtp_platform.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
