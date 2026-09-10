BEGIN;

CREATE SCHEMA IF NOT EXISTS central_brain;

CREATE TABLE IF NOT EXISTS central_brain.workspaces (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS central_brain.actors (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id) ON DELETE CASCADE,
    external_ref text NOT NULL,
    actor_type text NOT NULL CHECK (actor_type IN ('human', 'service', 'model')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, external_ref),
    UNIQUE (id, workspace_id)
);

CREATE TABLE IF NOT EXISTS central_brain.memories (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id) ON DELETE CASCADE,
    subject_ref text,
    memory_type text NOT NULL CHECK (memory_type IN
        ('preference', 'fact', 'decision', 'project', 'summary')),
    content text NOT NULL CHECK (char_length(content) BETWEEN 1 AND 20000),
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN
        ('proposed', 'active', 'rejected', 'superseded', 'archived')),
    visibility text NOT NULL DEFAULT 'workspace' CHECK (visibility IN
        ('private', 'workspace')),
    sensitivity text NOT NULL DEFAULT 'internal' CHECK (sensitivity IN
        ('public', 'internal', 'confidential', 'restricted')),
    confidence real CHECK (confidence BETWEEN 0 AND 1),
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key text,
    supersedes_id uuid,
    created_by uuid,
    approved_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz,
    expires_at timestamptz,
    search_document tsvector GENERATED ALWAYS AS
        (to_tsvector('english', coalesce(content, ''))) STORED,
    CHECK ((status <> 'active') OR (approved_at IS NOT NULL)),
    CHECK ((approved_at IS NULL) = (approved_by IS NULL)),
    UNIQUE (id, workspace_id),
    FOREIGN KEY (supersedes_id, workspace_id)
        REFERENCES central_brain.memories(id, workspace_id),
    FOREIGN KEY (created_by, workspace_id)
        REFERENCES central_brain.actors(id, workspace_id),
    FOREIGN KEY (approved_by, workspace_id)
        REFERENCES central_brain.actors(id, workspace_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS memories_workspace_dedupe_active_idx
    ON central_brain.memories (workspace_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('proposed', 'active');
CREATE INDEX IF NOT EXISTS memories_workspace_lookup_idx
    ON central_brain.memories (workspace_id, memory_type, status, created_at DESC);
CREATE INDEX IF NOT EXISTS memories_search_idx
    ON central_brain.memories USING gin (search_document);
CREATE INDEX IF NOT EXISTS memories_expiry_idx
    ON central_brain.memories (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS central_brain.skills (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id) ON DELETE CASCADE,
    name text NOT NULL,
    description text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name),
    UNIQUE (id, workspace_id)
);

CREATE TABLE IF NOT EXISTS central_brain.skill_versions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id) ON DELETE CASCADE,
    skill_id uuid NOT NULL,
    version text NOT NULL,
    definition text NOT NULL CHECK (char_length(definition) BETWEEN 1 AND 100000),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (skill_id, version),
    FOREIGN KEY (skill_id, workspace_id)
        REFERENCES central_brain.skills(id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (created_by, workspace_id)
        REFERENCES central_brain.actors(id, workspace_id)
);

CREATE TABLE IF NOT EXISTS central_brain.audit_events (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES central_brain.workspaces(id) ON DELETE RESTRICT,
    actor_id uuid,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    outcome text NOT NULL CHECK (outcome IN ('allowed', 'denied', 'failed')),
    request_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (actor_id, workspace_id)
        REFERENCES central_brain.actors(id, workspace_id)
);
CREATE INDEX IF NOT EXISTS audit_workspace_time_idx
    ON central_brain.audit_events (workspace_id, occurred_at DESC);

ALTER TABLE central_brain.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.actors ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.skills ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.skill_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE central_brain.workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE central_brain.actors FORCE ROW LEVEL SECURITY;
ALTER TABLE central_brain.memories FORCE ROW LEVEL SECURITY;
ALTER TABLE central_brain.skills FORCE ROW LEVEL SECURITY;
ALTER TABLE central_brain.skill_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE central_brain.audit_events FORCE ROW LEVEL SECURITY;

DO $policies$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['actors', 'memories', 'skills', 'skill_versions', 'audit_events']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'central_brain'
              AND tablename = table_name
              AND policyname = table_name || '_workspace_isolation'
        ) THEN
            EXECUTE format(
                'CREATE POLICY %I ON central_brain.%I USING (workspace_id = nullif(current_setting(''app.workspace_id'', true), '''')::uuid) WITH CHECK (workspace_id = nullif(current_setting(''app.workspace_id'', true), '''')::uuid)',
                table_name || '_workspace_isolation', table_name
            );
        END IF;
    END LOOP;
END
$policies$;

DO $workspace_policy$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'central_brain'
          AND tablename = 'workspaces'
          AND policyname = 'workspaces_isolation'
    ) THEN
        CREATE POLICY workspaces_isolation ON central_brain.workspaces
            USING (id = nullif(current_setting('app.workspace_id', true), '')::uuid)
            WITH CHECK (id = nullif(current_setting('app.workspace_id', true), '')::uuid);
    END IF;
END
$workspace_policy$;

COMMIT;
