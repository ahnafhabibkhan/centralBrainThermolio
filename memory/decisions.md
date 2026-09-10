# Architecture decisions

## ADR-001: PostgreSQL is the canonical store

- **Status:** proposed
- **Date:** 2026-09-10
- **Decision:** Store canonical, revisioned memory and skills in PostgreSQL. Treat
  embeddings as rebuildable indexes.
- **Reason:** PostgreSQL provides transactions, constraints, audit-friendly data,
  full-text search, and tenant isolation without coupling to a model vendor.

## ADR-002: Clients use an API rather than direct database access

- **Status:** proposed
- **Date:** 2026-09-10
- **Decision:** Model adapters call a narrow authenticated Brain API.
- **Reason:** Central policy, validation, redaction, auditing, and rate limits are
  difficult to guarantee when every client has database credentials.
