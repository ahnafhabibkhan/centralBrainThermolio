BEGIN;
CREATE TABLE IF NOT EXISTS central_brain.library_deletions (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id),
 created_by uuid NOT NULL, visibility text NOT NULL, sensitivity text NOT NULL,
 name text NOT NULL, path text NOT NULL, kind text NOT NULL, original_status text NOT NULL,
 deleted_by uuid NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now(),
 object_keys jsonb NOT NULL DEFAULT '[]', purged_at timestamptz
);
ALTER TABLE central_brain.library_deletions ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.library_deletions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workspace ON central_brain.library_deletions;
CREATE POLICY workspace ON central_brain.library_deletions USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 (visibility='workspace' OR created_by=nullif(current_setting('app.actor_id',true),'')::uuid)
) WITH CHECK (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 (visibility='workspace' OR created_by=nullif(current_setting('app.actor_id',true),'')::uuid)
);
CREATE INDEX IF NOT EXISTS library_deletions_recent ON central_brain.library_deletions(workspace_id,deleted_at DESC);
GRANT SELECT,INSERT,UPDATE,DELETE ON central_brain.library_deletions TO central_brain_runtime;
INSERT INTO central_brain.schema_migrations(version) VALUES('005_library_deletions') ON CONFLICT DO NOTHING;
COMMIT;
