# Staged implementation roadmap

The pilot includes the review website, MCP server, production OAuth validation, passing PostgreSQL integration tests, and an approved AWS deployment. See [current implementation status](implementation-status.md). User onboarding and authenticated assistant acceptance tests remain pending. The stages below preserve the broader roadmap.

Each stage has an explicit exit criterion. Start with Stage 0 rather than choosing
an embedding model or building a chat UI.

## Stage 0 — decisions and data inventory (day 1)

- Name the owner, intended users, workspaces, data classes, retention period, and
  human approval policy.
- List the exact memory types needed. Begin with preferences, facts, decisions,
  projects, and summaries; avoid raw transcript retention by default.
- Decide whether the existing RDS instance satisfies isolation, ownership,
  backup, performance, and change-control requirements.

**Exit:** an approved data inventory and threat model with one accountable owner.

## Stage 1 — database foundation

- Create a new database (strongest isolation) or dedicated schema and roles by
  following `docs/aws-rds.md`.
- Apply `001_core.sql`, create one test workspace and actor, and exercise RLS with
  two workspaces.
- Configure backups, deletion/retention jobs, monitoring, and restore testing.
- Add `002_pgvector.sql` only after checking RDS engine support and deciding an
  embedding dimension.

**Exit:** migrations run repeatably, cross-workspace tests fail closed, and a
restore drill succeeds.

## Stage 2 — minimal Brain API

- Define OpenAPI schemas and build authentication, workspace resolution, health,
  memory create/read/search, approval, and audit endpoints.
- Use parameterized queries, transaction-local `app.workspace_id`, request size
  limits, structured logs, and idempotency keys.
- Add integration tests against real PostgreSQL, including authorization,
  redaction, expiry, revision, and deletion cases.

**Exit:** the API passes contract/security tests without any model integration.

## Stage 3 — deterministic retrieval

- Implement metadata filtering and full-text ranking first.
- Create a fixed evaluation set of questions, expected memories, forbidden
  memories, and empty-result cases.
- Measure recall, precision, latency, and token use. Returning no context is valid.

**Exit:** retrieval meets written evaluation thresholds and never crosses tenants.

## Stage 4 — one provider adapter

- Connect the lower-risk provider/use case first.
- Clearly delimit retrieved memories and include provenance in the model context.
- Convert model-suggested memories into `proposed` records; do not auto-promote
  them until the evaluation demonstrates acceptable behavior.

**Exit:** an end-to-end pilot works for one workspace with reviewed writes.

## Stage 5 — second provider and portability

- Implement the same internal contract for the second provider.
- Run identical golden tests against both adapters and ensure provider IDs remain
  metadata rather than primary keys.
- Add per-provider rate limits, timeout/retry rules, and cost/latency dashboards.

**Exit:** changing provider requires configuration/adapter selection, not a schema
change.

## Stage 6 — semantic retrieval and operations

- If keyword evaluation shows a real gap, enable pgvector and generate embeddings
  asynchronously. Record provider, model, dimensions, and content hash.
- Add background jobs for expiry, re-embedding, retention, and failed-write replay.
- Run prompt-injection, poisoning, access-control, incident-response, and restore
  exercises before production rollout.

**Exit:** measured benefit justifies semantic-search cost and production runbooks
are owned and tested.

## First-week checklist

- [ ] Complete Stage 0 decisions.
- [ ] Choose new database vs dedicated schema.
- [ ] Apply migrations to a non-production instance.
- [ ] Test two-workspace isolation and backup restore.
- [ ] Draft OpenAPI and authentication flow.
- [ ] Implement proposed-memory writes and keyword retrieval.
- [ ] Build the first 20-case retrieval evaluation set.
