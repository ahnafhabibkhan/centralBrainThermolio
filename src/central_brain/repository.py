import hashlib
import json
import math
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .auth import AuthContext
from .models import Memory, MemoryCreate, SearchRequest, SearchResult, Skill, WriteReceipt


class PostgresMemoryRepository:
    def __init__(self, database_url: str):
        self.pool = ConnectionPool(
            database_url, min_size=1, max_size=4, timeout=5, open=False,
            kwargs={"row_factory": dict_row, "connect_timeout": 5},
        )

    def open(self):
        self.pool.open(wait=True, timeout=10)

    def close(self):
        self.pool.close()

    def ready(self):
        with self.pool.connection() as connection:
            return bool(connection.execute(
                "SELECT version FROM central_brain.schema_migrations WHERE version='003_pilot'"
            ).fetchone())

    @contextmanager
    def _connection(self, auth: AuthContext):
        with self.pool.connection() as connection, connection.transaction():
            connection.execute("SELECT set_config('app.workspace_id', %s, true)",
                               (str(auth.principal.workspace_id),))
            connection.execute("SELECT set_config('app.actor_id', %s, true)",
                               (str(auth.principal.actor_id),))
            connection.execute("SET LOCAL statement_timeout='5000ms'")
            yield connection

    @staticmethod
    def _params(auth):
        return {"levels": auth.principal.sensitivities}

    def _get(self, connection, auth, memory_id, lock=False):
        return connection.execute(
            "SELECT * FROM central_brain.memories WHERE id=%(id)s AND deleted_at IS NULL "
            "AND sensitivity=ANY(%(levels)s)" + (" FOR UPDATE" if lock else ""),
            {**self._params(auth), "id": memory_id},
        ).fetchone()

    @staticmethod
    def _not_found(row):
        if row is None:
            raise HTTPException(404, "memory not found")
        return row

    @staticmethod
    def _validate_write(auth, item):
        auth.require("writer")
        if item.sensitivity not in auth.principal.sensitivities:
            raise HTTPException(403, "sensitivity is not permitted")
        if item.expires_at and item.expires_at <= datetime.now(UTC):
            raise HTTPException(422, "expires_at must be in the future")

    def create(self, auth, item: MemoryCreate, supersedes_id: UUID | None = None):
        self._validate_write(auth, item)
        values = item.model_dump(mode="json")
        canonical = {k: v for k, v in values.items() if k != "dedupe_key"}
        canonical["supersedes_id"] = str(supersedes_id) if supersedes_id else None
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        # Scope deduplication to the actor so private writes do not disclose other actors' IDs.
        key = hashlib.sha256(
            f"{auth.principal.actor_id}:{item.dedupe_key or fingerprint}".encode()
        ).hexdigest()
        with self._connection(auth) as connection:
            if supersedes_id:
                old = self._not_found(self._get(connection, auth, supersedes_id, lock=True))
                if old["status"] != "active":
                    raise HTTPException(409, "only active memories can be revised")
                if old["visibility"] != item.visibility or old["sensitivity"] != item.sensitivity:
                    raise HTTPException(422, "a revision must preserve visibility and sensitivity")
            values.update(id=uuid4(), workspace_id=auth.principal.workspace_id,
                          actor_id=auth.principal.actor_id, dedupe_key=key,
                          supersedes_id=supersedes_id,
                          source=Jsonb(values["source"]), metadata=Jsonb(values["metadata"]))
            # A transaction-scoped advisory lock serializes only equal deduplication keys.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               (str(auth.principal.workspace_id) + key,))
            existing = connection.execute(
                "SELECT * FROM central_brain.memories WHERE dedupe_key=%s "
                "AND status IN ('proposed','active') AND deleted_at IS NULL", (key,),
            ).fetchone()
            if existing:
                for field in ("content", "memory_type", "source", "metadata", "visibility",
                              "sensitivity", "supersedes_id", "expires_at", "subject_ref",
                              "confidence"):
                    expected = getattr(item, field, supersedes_id)
                    if field == "source":
                        expected = item.source.model_dump()
                    if (field == "confidence" and expected is not None and existing[field] is not None
                            and math.isclose(existing[field], expected, rel_tol=1e-6)):
                        continue
                    if existing[field] != expected:
                        raise HTTPException(409, "deduplication key already has different content")
                return WriteReceipt(memory_id=existing["id"], status=existing["status"], created=False)
            row = connection.execute(
                """INSERT INTO central_brain.memories
                (id, workspace_id, created_by, content, memory_type, subject_ref, sensitivity,
                 visibility, confidence, source, metadata, dedupe_key, expires_at, supersedes_id)
                VALUES (%(id)s, %(workspace_id)s, %(actor_id)s, %(content)s, %(memory_type)s,
                 %(subject_ref)s, %(sensitivity)s, %(visibility)s, %(confidence)s, %(source)s,
                 %(metadata)s, %(dedupe_key)s, %(expires_at)s, %(supersedes_id)s)
                RETURNING id, status""", values,
            ).fetchone()
            self._audit(connection, auth, "memory.propose", row["id"])
            return WriteReceipt(memory_id=row["id"], status=row["status"], created=True)

    def search(self, auth, request: SearchRequest):
        auth.require("reader")
        with self._connection(auth) as connection:
            rows = connection.execute(
                """SELECT *, ts_rank_cd(search_document,
                       websearch_to_tsquery('english', %(query)s)) AS score
                FROM central_brain.memories WHERE status='active' AND deleted_at IS NULL
                  AND (expires_at IS NULL OR expires_at > now())
                  AND sensitivity=ANY(%(levels)s)
                  AND (cardinality(%(types)s::text[])=0 OR memory_type=ANY(%(types)s))
                  AND search_document @@ websearch_to_tsquery('english', %(query)s)
                ORDER BY score DESC, created_at DESC, id DESC LIMIT %(limit)s""",
                {**self._params(auth), "query": request.query,
                 "types": request.memory_types, "limit": request.limit},
            ).fetchall()
        return [SearchResult(memory=Memory.model_validate(row), score=row["score"]) for row in rows]

    def list(self, auth, status="proposed", limit=25, offset=0):
        auth.require("reviewer")
        with self._connection(auth) as connection:
            rows = connection.execute(
                "SELECT * FROM central_brain.memories WHERE status=%(status)s "
                "AND deleted_at IS NULL AND sensitivity=ANY(%(levels)s) "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
                {**self._params(auth), "status": status, "limit": limit, "offset": offset},
            ).fetchall()
        return [Memory.model_validate(row) for row in rows]

    def get(self, auth, memory_id):
        auth.require("reviewer")
        with self._connection(auth) as connection:
            return Memory.model_validate(self._not_found(self._get(connection, auth, memory_id)))

    def transition(self, auth, memory_id, action):
        auth.require("admin" if action == "delete" else "reviewer")
        with self._connection(auth) as connection:
            row = self._not_found(self._get(connection, auth, memory_id, lock=True))
            if action == "delete":
                # Purge content while retaining IDs needed by revision and audit references.
                connection.execute(
                    """UPDATE central_brain.memories SET content='[Deleted]', source='{}',
                    metadata='{}', subject_ref=NULL, confidence=NULL, dedupe_key=NULL,
                    status='archived', deleted_at=now() WHERE id=%s""", (memory_id,),
                )
                status = "archived"
            else:
                if row["status"] != "proposed":
                    raise HTTPException(409, "only proposals can be reviewed")
                if action == "approve":
                    if row["expires_at"] and row["expires_at"] <= datetime.now(UTC):
                        raise HTTPException(409, "expired proposals cannot be approved")
                    if row["supersedes_id"]:
                        old = self._get(connection, auth, row["supersedes_id"], lock=True)
                        if not old or old["status"] != "active":
                            raise HTTPException(409, "the original memory is no longer active")
                        connection.execute(
                            "UPDATE central_brain.memories SET status='superseded' WHERE id=%s",
                            (row["supersedes_id"],),
                        )
                    connection.execute(
                        "UPDATE central_brain.memories SET status='active', approved_at=now(), "
                        "approved_by=%s WHERE id=%s", (auth.principal.actor_id, memory_id),
                    )
                    status = "active"
                elif action == "reject":
                    connection.execute("UPDATE central_brain.memories SET status='rejected' "
                                       "WHERE id=%s", (memory_id,))
                    status = "rejected"
                else:
                    raise ValueError("Unknown transition")
            self._audit(connection, auth, f"memory.{action}", memory_id)
        return WriteReceipt(memory_id=memory_id, status=status, created=False)

    def approve(self, auth, memory_id):
        return self.transition(auth, memory_id, "approve")

    def edit_proposal(self, auth, memory_id, item):
        auth.require("reviewer")
        self._validate_write(auth, item)
        with self._connection(auth) as connection:
            row = self._not_found(self._get(connection, auth, memory_id, lock=True))
            if row["status"] != "proposed":
                raise HTTPException(409, "only proposals can be edited in place")
            connection.execute(
                "UPDATE central_brain.memories SET content=%s, source=%s, dedupe_key=NULL WHERE id=%s",
                (item.content, Jsonb(item.source.model_dump()), memory_id),
            )
            self._audit(connection, auth, "memory.edit", memory_id)
        return WriteReceipt(memory_id=memory_id, status="proposed", created=False)

    def skills(self, auth, name=None, version=None):
        auth.require("reader")
        with self._connection(auth) as connection:
            rows = connection.execute(
                """SELECT s.name, s.description, v.version, v.definition, v.content_sha256
                FROM central_brain.skills s JOIN central_brain.skill_versions v
                ON v.skill_id=s.id AND v.workspace_id=s.workspace_id
                WHERE v.status='active' AND (%(name)s::text IS NULL OR s.name=%(name)s)
                AND (%(version)s::text IS NULL OR v.version=%(version)s)
                ORDER BY s.name, v.created_at DESC LIMIT 100""",
                {"name": name, "version": version},
            ).fetchall()
        return [Skill.model_validate(row) for row in rows]

    def session_create(self, session_id, token, expiry):
        with self.pool.connection() as connection:
            connection.execute("DELETE FROM central_brain.web_sessions WHERE expires_at<now()")
            connection.execute("INSERT INTO central_brain.web_sessions(id_hash,access_token,expires_at) "
                               "VALUES (%s,%s,to_timestamp(%s))",
                               (hashlib.sha256(session_id.encode()).hexdigest(), token, expiry))

    def session_token(self, session_id):
        with self.pool.connection() as connection:
            row = connection.execute("SELECT access_token FROM central_brain.web_sessions "
                                     "WHERE id_hash=%s AND expires_at>now()",
                                     (hashlib.sha256(session_id.encode()).hexdigest(),)).fetchone()
        return row["access_token"] if row else None

    def session_delete(self, session_id):
        with self.pool.connection() as connection:
            connection.execute("DELETE FROM central_brain.web_sessions WHERE id_hash=%s",
                               (hashlib.sha256(session_id.encode()).hexdigest(),))

    @staticmethod
    def _audit(connection, auth, action, resource_id):
        connection.execute(
            """INSERT INTO central_brain.audit_events
            (id,workspace_id,actor_id,action,resource_type,resource_id,outcome)
            VALUES (%s,%s,%s,%s,'memory',%s,'allowed')""",
            (uuid4(), auth.principal.workspace_id, auth.principal.actor_id, action, resource_id),
        )
