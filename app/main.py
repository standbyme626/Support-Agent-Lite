"""FastAPI application bootstrap.

Phase 0: minimal startup with a health endpoint only.
"""
from fastapi import FastAPI

app = FastAPI(
    title="support-agent-lite",
    version="0.0.1",
    description="Cross-channel enterprise support agent (user-centric, workflow-first).",
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "support-agent-lite", "docs": "/docs"}
