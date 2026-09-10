-- Optional: verify pgvector support and choose the embedding dimension before use.
-- This migration uses 1536 only as an explicit starting choice; changing dimensions
-- requires a new column/table and re-embedding rather than mixing vector spaces.
BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS central_brain.memory_embeddings (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id) ON DELETE CASCADE,
    memory_id uuid NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions = 1536),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    embedding vector(1536) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (memory_id, provider, model, content_sha256),
    FOREIGN KEY (memory_id, workspace_id)
        REFERENCES central_brain.memories(id, workspace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS memory_embeddings_workspace_idx
    ON central_brain.memory_embeddings (workspace_id, model);

ALTER TABLE central_brain.memory_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.memory_embeddings FORCE ROW LEVEL SECURITY;

DO $policy$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'central_brain'
          AND tablename = 'memory_embeddings'
          AND policyname = 'memory_embeddings_workspace_isolation'
    ) THEN
        CREATE POLICY memory_embeddings_workspace_isolation
            ON central_brain.memory_embeddings
            USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
            WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);
    END IF;
END
$policy$;

COMMIT;
