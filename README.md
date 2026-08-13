# Support Agent Lite

Cross-channel enterprise support agent.

User-centric and workflow-first reimplementation, derived from lessons learned in the legacy `support-agent-platform` project (available read-only in `reference/`).

## Status

Phase 0 — Architecture Bootstrap (no business functionality yet).

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app
```

## Documentation

- `docs/PRODUCT_SCOPE.md` — what the product does and does not do
- `docs/ARCHITECTURE.md` — architecture and invariants
- `docs/DOMAIN_MODEL.md` — domain entities and state machines
- `docs/GOLDEN_PATH.md` — the golden path through the system
- `docs/ACCEPTANCE_TESTS.md` — acceptance contract (AC-01..AC-10)
- `docs/DEVELOPMENT_PLAN.md` — phased development plan
- `docs/LEGACY_PORT_MAP.md` — what to port/adapt/rewrite/ignore from legacy
- `docs/reference/LEGACY_ARCHITECTURE_AUDIT.md` — legacy architecture audit (reference only)
