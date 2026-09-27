BEGIN;
ALTER TABLE central_brain.library_versions
 DROP CONSTRAINT IF EXISTS library_versions_size_check;
ALTER TABLE central_brain.library_versions
 ADD CONSTRAINT library_versions_size_check CHECK(size BETWEEN 0 AND 524288000);
INSERT INTO central_brain.schema_migrations(version)
 VALUES('008_library_file_size') ON CONFLICT DO NOTHING;
COMMIT;
