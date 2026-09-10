BEGIN;
ALTER TABLE central_brain.memories ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE TABLE IF NOT EXISTS central_brain.schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Keep private memories inaccessible to other actors even inside the same workspace.
DROP POLICY IF EXISTS memories_workspace_isolation ON central_brain.memories;
CREATE POLICY memories_workspace_isolation ON central_brain.memories
USING (
    workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid
    AND (visibility = 'workspace' OR created_by =
        nullif(current_setting('app.actor_id', true), '')::uuid)
)
WITH CHECK (
    workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid
    AND (visibility = 'workspace' OR created_by =
        nullif(current_setting('app.actor_id', true), '')::uuid)
);

-- Browser tokens stay server-side. Only hashes of opaque cookie IDs are stored.
CREATE TABLE IF NOT EXISTS central_brain.web_sessions (
    id_hash text PRIMARY KEY,
    access_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO central_brain.schema_migrations(version) VALUES ('003_pilot')
ON CONFLICT DO NOTHING;
COMMIT;
