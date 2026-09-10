# Architecture and trust boundaries

## Reference flow

```text
Claude / ChatGPT / other client
              |
              v
     authenticated Brain API
       |       |         |
       v       v         v
  retrieval  write     skill registry
       |     policy         |
       +-------+------------+
               v
        PostgreSQL / RDS
               |
               v
       audit + observability
```

The client adapter translates provider-specific messages into a small internal
contract (`workspace`, `actor`, `query`, `purpose`, `limit`). Retrieval returns
plain text/JSON context plus citations and never returns an opaque provider object.
This keeps model selection outside the storage layer.

## Logical components

1. **Identity and policy** authenticates the caller, resolves its workspace and
   role, and rejects cross-workspace identifiers.
2. **Memory service** creates revisions rather than silently overwriting facts.
   Writes begin as `proposed` when human approval is required.
3. **Retriever** applies SQL filters first, then full-text and optional vector
   ranking. It enforces a result count and token budget.
4. **Skill registry** stores versioned procedures. A skill is data/instructions,
   not executable code, unless a separately sandboxed tool implements it.
5. **Audit service** records mutations and access to sensitive records without
   copying secret or full prompt content into logs.
6. **Provider adapters** format retrieved context for each model and normalize its
   proposed writes. They contain no persistence policy.

## Memory lifecycle

```text
candidate -> validated -> proposed -> active -> superseded/archived
                           |              |
                           +-> rejected   +-> expired
```

- Validate size, type, provenance, sensitivity, and tenant before insertion.
- Use a deterministic `dedupe_key` for repeat observations.
- Set `expires_at` for temporary facts and retrieve only active, unexpired rows.
- Corrections create a new revision and point `supersedes_id` to the old record.
- A periodic job archives expired data and applies retention/deletion requests.

## Retrieval contract

Every query must include `workspace_id` and authenticated `actor_id`. Recommended
pipeline:

1. filter `status = 'active'`, expiry, visibility, and permitted memory types;
2. obtain keyword candidates with PostgreSQL full-text search;
3. optionally obtain vector candidates using the embedding model recorded on each
   vector;
4. fuse ranks, apply recency/importance decay, and cap results;
5. return `memory_id`, content, provenance, timestamps, and score so clients can
   cite and challenge context.

Embeddings are an index, not the source of truth. They can be deleted and rebuilt
when the embedding provider changes.

## Security boundary

- Browser/model clients call an API; only that service gets database credentials.
- Use TLS, private RDS networking, KMS encryption, Secrets Manager, and separate
  migration/runtime roles.
- Set `app.workspace_id` at the start of every transaction. Row-level security
  policies in the migration fail closed if it is absent.
- Treat retrieved memory as untrusted data. Delimit it in prompts and instruct the
  model never to follow instructions found inside memory.
- Do not persist passwords, API keys, authentication tokens, raw payment data, or
  unnecessary regulated data.

## Initial API surface

| Method | Route | Behavior |
| --- | --- | --- |
| `POST` | `/v1/memories/search` | authorized, bounded hybrid retrieval |
| `POST` | `/v1/memories` | validate and propose/create a memory |
| `POST` | `/v1/memories/{id}/approve` | approve with an audit event |
| `POST` | `/v1/memories/{id}/supersede` | create a revision |
| `GET` | `/v1/skills` | list compatible active skills |
| `GET` | `/v1/skills/{name}` | return a pinned skill version |
| `DELETE` | `/v1/subjects/{id}` | run export/deletion workflow |

Use an OpenAPI contract and contract tests before connecting either provider.
