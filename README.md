# Central Brain

A model-agnostic persistence layer for sharing durable memory and reusable skills
between Claude, ChatGPT, and future assistants. The brain is deliberately **not**
an autonomous agent: clients retrieve context, ask a model to reason, and write
back only reviewed, provenance-rich memories.

> The design PDF referenced in the original request was not available inside this
> repository. This starter kit therefore treats it as background context—not as
> executable instructions—and uses conservative, vendor-neutral boundaries.

## Start here

1. Read the concise [delivery plan](PLAN.md), then the detailed
   [architecture and trust boundaries](docs/architecture.md).
2. Choose a fresh PostgreSQL database or an isolated schema in an existing RDS
   instance using the [RDS guide](docs/aws-rds.md).
3. Copy `.env.example` to `.env` and replace the example principal token and IDs.
4. Apply `database/migrations/001_core.sql`; optionally apply
   `002_pgvector.sql` if `pgvector` is approved and available.
5. Populate the Markdown files under `memory/` with reviewed facts only.
6. Add or adapt reusable procedures under `skills/`.
7. Implement a thin API by following [the staged roadmap](docs/roadmap.md). Do
   not give model clients direct database credentials.

For local PostgreSQL:

```bash
cp .env.example .env
docker compose up -d
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f database/migrations/001_core.sql
python -m pip install -e '.[dev]'
uvicorn central_brain.api:create_app --factory --reload
```

## Repository map

| Path | Purpose |
| --- | --- |
| `database/migrations/` | Idempotent PostgreSQL schema and optional vector search |
| `docs/` | Architecture, staged delivery plan, RDS setup, and safety model |
| `memory/` | Human-readable, version-controlled seed memory |
| `skills/` | Model-neutral procedures with explicit inputs and outputs |
| `src/central_brain/` | Authenticated Brain API and PostgreSQL repository |
| `tests/` | API authorization, lifecycle, and retrieval-limit tests |

See the [cloud deployment runbook](docs/cloud-deployment.md) for an approval-gated
ECR/ECS/RDS rollout sequence and the inputs required before applying AWS changes.

## Core principles

- **Provider adapters, not provider assumptions.** Normalize model messages and
  tool results at the API boundary.
- **Tenant isolation first.** Every persisted brain object belongs to a workspace;
  PostgreSQL row-level security provides defense in depth.
- **Provenance over confidence.** Memories record where they came from, when they
  expire, and whether a human verified them.
- **Retrieval is bounded.** Filter by workspace, visibility, status, and type
  before semantic or text ranking.
- **Writes are controlled.** Proposed memories are validated, deduplicated, and
  optionally approved before becoming active.
- **Secrets never become memory.** Keep credentials in a secret manager and
  redact sensitive input before persistence.

The Markdown memory files are seed/configuration artifacts. Runtime conversation
history and user data belong in PostgreSQL and should not be committed.
