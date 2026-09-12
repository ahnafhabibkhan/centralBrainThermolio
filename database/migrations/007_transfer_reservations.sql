BEGIN;
CREATE TABLE IF NOT EXISTS central_brain.transfer_reservations (
 id uuid PRIMARY KEY REFERENCES central_brain.library_suggestions(id) ON DELETE CASCADE,
 workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id),
 created_by uuid NOT NULL,
 size_bytes bigint NOT NULL CHECK(size_bytes > 0)
);
ALTER TABLE central_brain.transfer_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.transfer_reservations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workspace ON central_brain.transfer_reservations;
CREATE POLICY workspace ON central_brain.transfer_reservations FOR SELECT USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid
);
DROP POLICY IF EXISTS author_insert ON central_brain.transfer_reservations;
CREATE POLICY author_insert ON central_brain.transfer_reservations FOR INSERT WITH CHECK (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 created_by=nullif(current_setting('app.actor_id',true),'')::uuid
);
DROP POLICY IF EXISTS author_delete ON central_brain.transfer_reservations;
CREATE POLICY author_delete ON central_brain.transfer_reservations FOR DELETE USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 created_by=nullif(current_setting('app.actor_id',true),'')::uuid
);
GRANT SELECT,INSERT,DELETE ON central_brain.transfer_reservations TO central_brain_runtime;
INSERT INTO central_brain.schema_migrations(version) VALUES('007_transfer_reservations') ON CONFLICT DO NOTHING;
COMMIT;
