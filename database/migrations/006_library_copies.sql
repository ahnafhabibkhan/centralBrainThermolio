BEGIN;
ALTER TABLE central_brain.library_nodes ADD COLUMN IF NOT EXISTS copied_from jsonb NOT NULL DEFAULT '{}';
INSERT INTO central_brain.schema_migrations(version) VALUES('006_library_copies') ON CONFLICT DO NOTHING;
COMMIT;
