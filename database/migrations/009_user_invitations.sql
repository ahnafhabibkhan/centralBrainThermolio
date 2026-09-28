BEGIN;

CREATE TABLE IF NOT EXISTS central_brain.oauth_identities (
    subject text PRIMARY KEY CHECK (char_length(subject) BETWEEN 1 AND 255),
    workspace_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    roles text[] NOT NULL,
    sensitivities text[] NOT NULL DEFAULT ARRAY['public', 'internal']::text[],
    invited_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (actor_id, workspace_id),
    FOREIGN KEY (actor_id, workspace_id)
        REFERENCES central_brain.actors(id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (invited_by, workspace_id)
        REFERENCES central_brain.actors(id, workspace_id),
    CHECK (roles <@ ARRAY['reader', 'writer', 'reviewer', 'admin']::text[]),
    CHECK (sensitivities <@ ARRAY['public', 'internal', 'confidential', 'restricted']::text[])
);

REVOKE ALL ON central_brain.oauth_identities FROM PUBLIC;
GRANT SELECT, INSERT ON central_brain.oauth_identities TO central_brain_runtime;

INSERT INTO central_brain.schema_migrations(version) VALUES ('009_user_invitations')
ON CONFLICT DO NOTHING;

COMMIT;
