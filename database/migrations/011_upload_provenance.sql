BEGIN;

-- Legacy versions retain NULL provenance because their creation may reflect approval time.
ALTER TABLE central_brain.library_versions ADD COLUMN IF NOT EXISTS source_created_at timestamptz;
ALTER TABLE central_brain.library_versions ADD COLUMN IF NOT EXISTS source_upload_id uuid;

INSERT INTO central_brain.schema_migrations(version) VALUES('011_upload_provenance') ON CONFLICT DO NOTHING;
COMMIT;
