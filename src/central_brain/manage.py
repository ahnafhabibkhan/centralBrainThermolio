"""Explicit operator commands. Importing this module never provisions anything."""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from dotenv import dotenv_values
from psycopg import sql

WORKSPACE = UUID("a22cdb8e-6c0d-4b59-b292-a4e5593156c1")
ACTOR = UUID("f1aa197b-4291-4d81-a00c-cab55a1ceff0")
ROOT = Path(os.environ.get("CENTRAL_BRAIN_ROOT", Path(__file__).resolve().parents[2]))


def copy_login_key():
    """Copy the local reviewer credential without printing it or altering credentials."""
    env = dotenv_values(".env")
    if env.get("ENVIRONMENT") != "local":
        raise ValueError("This command is only available for a local development setup")
    principals = json.loads(env["CENTRAL_BRAIN_PRINCIPALS_JSON"])
    token = next((key for key, value in principals.items() if "reviewer" in value["roles"]), None)
    if not token:
        raise ValueError("No local reviewer key is configured")
    if sys.platform != "darwin":
        raise ValueError("Clipboard copying requires macOS. Read the reviewer key from your local .env file.")
    subprocess.run(["/usr/bin/pbcopy"], input=token, text=True, check=True)
    print("Your local reviewer key is copied. Paste it into the sign-in page.")


def init_local():
    path = Path(".env")
    admin, runtime, migrator = [secrets.token_urlsafe(32) for _ in range(3)]
    reviewer, connector = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    common = {"workspace_id": str(WORKSPACE), "actor_id": str(ACTOR)}
    principals = {
        reviewer: {**common, "roles": ["reader", "writer", "reviewer", "admin"]},
        connector: {**common, "roles": ["reader", "writer"]},
    }
    values = {
        "ENVIRONMENT": "local", "PUBLIC_URL": "http://127.0.0.1:8080",
        "DATABASE_URL": f"postgresql://central_brain_runtime:{runtime}@127.0.0.1:55432/central_brain",
        "MIGRATION_DATABASE_URL": f"postgresql://central_brain_migrator:{migrator}@127.0.0.1:55432/central_brain",
        "LOCAL_ADMIN_URL": f"postgresql://postgres:{admin}@127.0.0.1:55432/postgres",
        "LOCAL_POSTGRES_PASSWORD": admin, "SESSION_SECRET": secrets.token_urlsafe(48),
        "CENTRAL_BRAIN_PRINCIPALS_JSON": json.dumps(principals),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        for key, value in values.items():
            stream.write(f"{key}='{value}'\n")
    print("Generated .env with unique local credentials. Existing files were not overwritten.")


def bootstrap(admin_url, runtime_url, migrator_url, database="central_brain", remote=False):
    if not remote and urlsplit(admin_url).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Remote bootstrap requires --production-approved")
    if database not in {"central_brain", "central_brain_test"}:
        raise ValueError("Unsupported database name")
    with psycopg.connect(admin_url, autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname='central_brain_owner'").fetchone():
            connection.execute("CREATE ROLE central_brain_owner NOLOGIN")
        for role, dsn in (("central_brain_runtime", runtime_url),
                          ("central_brain_migrator", migrator_url)):
            if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT").format(sql.Identifier(role)))
            connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(urlsplit(dsn).password)))
        connection.execute("GRANT central_brain_owner TO central_brain_migrator")
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            connection.execute(sql.SQL("CREATE DATABASE {} OWNER central_brain_owner").format(sql.Identifier(database)))
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO central_brain_runtime, central_brain_migrator").format(sql.Identifier(database)))
    db_url = migrator_url.rsplit("/", 1)[0] + "/" + database
    with psycopg.connect(db_url, autocommit=True) as connection:
        connection.execute("SET ROLE central_brain_owner")
        # The core migration is idempotent. Optional embeddings are deliberately not applied.
        for name in ("001_core.sql", "003_pilot.sql", "004_library.sql", "005_library_deletions.sql"):
            connection.execute((ROOT / "database/migrations" / name).read_text())
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA central_brain FROM central_brain_runtime")
        connection.execute("GRANT USAGE ON SCHEMA central_brain TO central_brain_runtime")
        connection.execute("GRANT SELECT ON ALL TABLES IN SCHEMA central_brain TO central_brain_runtime")
        connection.execute("GRANT INSERT, UPDATE ON central_brain.memories TO central_brain_runtime")
        connection.execute("GRANT INSERT ON central_brain.audit_events TO central_brain_runtime")
        connection.execute("GRANT INSERT, DELETE ON central_brain.web_sessions TO central_brain_runtime")
        connection.execute("GRANT INSERT, UPDATE, DELETE ON central_brain.library_nodes,central_brain.library_versions,central_brain.library_sections,central_brain.library_suggestions TO central_brain_runtime")
        connection.execute("GRANT INSERT, UPDATE, DELETE ON central_brain.library_deletions TO central_brain_runtime")
        with connection.transaction():
            seed_identity(connection, WORKSPACE, ACTOR, "Personal pilot")
    print(f"Applied core and pilot migrations to {database}; runtime privileges are restricted.")


def seed_identity(connection, workspace, actor, name):
    connection.execute("SELECT set_config('app.workspace_id',%s,true)", (str(workspace),))
    connection.execute("SELECT set_config('app.actor_id',%s,true)", (str(actor),))
    connection.execute("INSERT INTO central_brain.workspaces(id,name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                       (workspace, name))
    connection.execute("INSERT INTO central_brain.actors(id,workspace_id,external_ref,actor_type) "
                       "VALUES (%s,%s,%s,'human') ON CONFLICT DO NOTHING", (actor, workspace, str(actor)))


def import_skills(migrator_url, approved):
    if not approved:
        raise ValueError("Review skill files first, then pass --confirm-reviewed")
    with psycopg.connect(migrator_url) as connection:
        connection.execute("SET ROLE central_brain_owner")
        seed_identity(connection, WORKSPACE, ACTOR, "Personal pilot")
        for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
            content = path.read_text()
            digest = hashlib.sha256(content.encode()).hexdigest()
            name = path.parent.name
            row = connection.execute("INSERT INTO central_brain.skills(id,workspace_id,name,description) "
                                     "VALUES (%s,%s,%s,%s) ON CONFLICT(workspace_id,name) "
                                     "DO UPDATE SET description=excluded.description RETURNING id",
                                     (uuid4(), WORKSPACE, name, f"Reviewed {name} procedure.")).fetchone()
            connection.execute("INSERT INTO central_brain.skill_versions "
                               "(id,workspace_id,skill_id,version,definition,content_sha256,status,created_by) "
                               "VALUES (%s,%s,%s,%s,%s,%s,'active',%s) ON CONFLICT(skill_id,version) DO NOTHING",
                               (uuid4(), WORKSPACE, row[0], digest[:12], content, digest, ACTOR))
            print(f"Imported {name} version {digest[:12]}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init-local", "bootstrap", "import-skills", "copy-login-key"])
    parser.add_argument("--database", default="central_brain")
    parser.add_argument("--production-approved", action="store_true")
    parser.add_argument("--confirm-reviewed", action="store_true")
    args = parser.parse_args()
    if args.command == "copy-login-key":
        copy_login_key()
        return
    if args.command == "init-local":
        init_local()
        return
    env = {**dotenv_values(".env"), **os.environ}
    if args.command == "bootstrap":
        bootstrap(env["LOCAL_ADMIN_URL"], env["DATABASE_URL"], env["MIGRATION_DATABASE_URL"],
                  args.database, args.production_approved)
    else:
        import_skills(env["MIGRATION_DATABASE_URL"], args.confirm_reviewed)


if __name__ == "__main__":
    main()
