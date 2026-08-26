# --- builder: compile/install dependencies -------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir --prefix=/install .

# --- runtime: slim, non-root, healthchecked --------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SUPPORT_AGENT_DB=/app/runtime/support_agent.db

WORKDIR /app

# runtime deps only (from builder), no build toolchain
COPY --from=builder /install /usr/local

# application code + knowledge corpus (retriever loads from here)
COPY app ./app
COPY seed ./seed

RUN useradd --system --create-home --uid 10001 agent \
    && mkdir -p /app/runtime && chown -R agent:agent /app
USER agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
