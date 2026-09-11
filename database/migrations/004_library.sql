BEGIN;
CREATE TABLE IF NOT EXISTS central_brain.library_nodes (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id),
 created_by uuid NOT NULL, parent_id uuid, name text NOT NULL CHECK(length(name) BETWEEN 1 AND 240),
 kind text NOT NULL CHECK(kind IN ('folder','file','memory')),
 visibility text NOT NULL DEFAULT 'workspace' CHECK(visibility IN ('workspace','private')),
 sensitivity text NOT NULL DEFAULT 'internal',
 memory_id uuid REFERENCES central_brain.memories(id),
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','proposed','rejected')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,workspace_id),
 FOREIGN KEY(parent_id,workspace_id) REFERENCES central_brain.library_nodes(id,workspace_id),
 FOREIGN KEY(created_by,workspace_id) REFERENCES central_brain.actors(id,workspace_id)
);
CREATE INDEX IF NOT EXISTS library_parent ON central_brain.library_nodes(workspace_id,parent_id);
CREATE TABLE IF NOT EXISTS central_brain.library_versions (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, node_id uuid NOT NULL,
 version integer NOT NULL, object_key text NOT NULL UNIQUE, size bigint NOT NULL CHECK(size BETWEEN 0 AND 52428800),
 sha256 text NOT NULL, state text NOT NULL DEFAULT 'queued', error text,
 lease_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(node_id,version), FOREIGN KEY(node_id,workspace_id) REFERENCES central_brain.library_nodes(id,workspace_id)
);
CREATE TABLE IF NOT EXISTS central_brain.library_sections (
 workspace_id uuid NOT NULL, version_id uuid NOT NULL REFERENCES central_brain.library_versions(id),
 ordinal integer NOT NULL, location text NOT NULL, content text NOT NULL,
 search_document tsvector GENERATED ALWAYS AS (to_tsvector('english',content)) STORED,
 PRIMARY KEY(version_id,ordinal)
);
CREATE INDEX IF NOT EXISTS library_section_search ON central_brain.library_sections USING gin(search_document);
CREATE TABLE IF NOT EXISTS central_brain.library_suggestions (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, created_by uuid NOT NULL,
 action text NOT NULL, payload jsonb NOT NULL, status text NOT NULL DEFAULT 'proposed',
 created_at timestamptz NOT NULL DEFAULT now()
);
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['library_nodes','library_versions','library_sections','library_suggestions'] LOOP
 EXECUTE format('ALTER TABLE central_brain.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE central_brain.%I FORCE ROW LEVEL SECURITY',t);
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='central_brain' AND tablename=t) THEN
 EXECUTE format('CREATE POLICY workspace ON central_brain.%I USING(workspace_id=nullif(current_setting(''app.workspace_id'',true),'''')::uuid) WITH CHECK(workspace_id=nullif(current_setting(''app.workspace_id'',true),'''')::uuid)',t);
 END IF;
 END LOOP;
END $policies$;
DROP POLICY IF EXISTS workspace ON central_brain.library_nodes;
CREATE POLICY workspace ON central_brain.library_nodes USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 (visibility='workspace' OR created_by=nullif(current_setting('app.actor_id',true),'')::uuid)
) WITH CHECK (workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 (visibility='workspace' OR created_by=nullif(current_setting('app.actor_id',true),'')::uuid));
DROP POLICY IF EXISTS workspace ON central_brain.library_suggestions;
CREATE POLICY workspace ON central_brain.library_suggestions USING (
 workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 created_by=nullif(current_setting('app.actor_id',true),'')::uuid
) WITH CHECK (workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid AND
 created_by=nullif(current_setting('app.actor_id',true),'')::uuid);
CREATE OR REPLACE FUNCTION central_brain.project_memory_file() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO central_brain.library_nodes(id,workspace_id,created_by,name,kind,memory_id,visibility,sensitivity)
 VALUES(NEW.id,NEW.workspace_id,NEW.created_by,'Memory-'||NEW.id||'.md','memory',NEW.id,NEW.visibility,NEW.sensitivity)
 ON CONFLICT(id) DO UPDATE SET visibility=NEW.visibility,sensitivity=NEW.sensitivity;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS project_memory_file ON central_brain.memories;
CREATE TRIGGER project_memory_file AFTER INSERT OR UPDATE OF visibility,sensitivity ON central_brain.memories
 FOR EACH ROW EXECUTE FUNCTION central_brain.project_memory_file();
GRANT SELECT,INSERT,UPDATE,DELETE ON central_brain.library_nodes,central_brain.library_versions,
 central_brain.library_sections,central_brain.library_suggestions TO central_brain_runtime;
INSERT INTO central_brain.schema_migrations(version) VALUES('004_library') ON CONFLICT DO NOTHING;
COMMIT;
