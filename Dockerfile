FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 先装依赖（利用层缓存）
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --frozen

# 再装本包源码
COPY src ./src
COPY scripts ./scripts
RUN uv sync --no-dev --frozen

ENV PATH="/app/.venv/bin:$PATH" \
    MTP_ARTIFACT_ROOT=/app/artifacts
RUN mkdir -p /app/artifacts

ENTRYPOINT ["mtp"]
CMD ["--help"]
