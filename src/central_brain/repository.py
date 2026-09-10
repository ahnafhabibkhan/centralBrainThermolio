from contextlib import contextmanager
from typing import Iterator, Protocol
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .auth import AuthContext
from .models import Memory, MemoryCreate, SearchRequest, SearchResult, WriteReceipt


class MemoryRepository(Protocol):
    def create(self, auth: AuthContext, item: MemoryCreate) -> WriteReceipt: ...
    def search(self, auth: AuthContext, request: SearchRequest) -> list[SearchResult]: ...
    def approve(self, auth: AuthContext, memory_id: UUID) -> WriteReceipt | None: ...


class PostgresMemoryRepository:
    def __init__(self, database_url: str) -> None:
        self.pool = ConnectionPool(database_url, min_size=0, open=False)

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def _connection(self, auth: AuthContext) -> Iterator:
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.workspace_id', %s, true)",
                    (auth.principal.workspace_id,),
                )
                yield connection

    def create(self, auth: AuthContext, item: MemoryCreate) -> WriteReceipt:
        memory_id = uuid4()
        values = item.model_dump(mode="json")
        values["source"] = Jsonb(values["source"])
        values["metadata"] = Jsonb(values["metadata"])
        with self._connection(auth) as connection:
            row = connection.cursor(row_factory=dict_row).execute(
                """
                INSERT INTO central_brain.memories
                    (id, workspace_id, created_by, content, memory_type, subject_ref,
                     sensitivity, confidence, source, metadata, dedupe_key, expires_at)
                VALUES (%(id)s, %(workspace_id)s, %(actor_id)s, %(content)s,
                        %(memory_type)s, %(subject_ref)s, %(sensitivity)s, %(confidence)s,
                        %(source)s, %(metadata)s, %(dedupe_key)s, %(expires_at)s)
                ON CONFLICT (workspace_id, dedupe_key)
                    WHERE dedupe_key IS NOT NULL AND status IN ('proposed', 'active')
                DO NOTHING
                RETURNING id, status
                """,
                {
                    **values,
                    "id": memory_id,
                    "workspace_id": auth.principal.workspace_id,
                    "actor_id": auth.principal.actor_id,
                },
            ).fetchone()
            if row:
                self._audit(connection, auth, "memory.create", row["id"], "allowed")
                return WriteReceipt(memory_id=row["id"], status=row["status"], created=True)
            existing = connection.cursor(row_factory=dict_row).execute(
                """SELECT id, status FROM central_brain.memories
                   WHERE dedupe_key = %s AND status IN ('proposed', 'active')""",
                (item.dedupe_key,),
            ).fetchone()
            self._audit(connection, auth, "memory.deduplicate", existing["id"], "allowed")
            return WriteReceipt(memory_id=existing["id"], status=existing["status"], created=False)

    def search(self, auth: AuthContext, request: SearchRequest) -> list[SearchResult]:
        filters = "AND memory_type = ANY(%(types)s)" if request.memory_types else ""
        with self._connection(auth) as connection:
            rows = connection.cursor(row_factory=dict_row).execute(
                f"""SELECT *, ts_rank_cd(search_document, websearch_to_tsquery('english', %(query)s)) AS score
                    FROM central_brain.memories
                    WHERE status = 'active'
                      AND (expires_at IS NULL OR expires_at > now())
                      AND search_document @@ websearch_to_tsquery('english', %(query)s)
                      {filters}
                    ORDER BY score DESC, created_at DESC
                    LIMIT %(limit)s""",  # noqa: S608 - only a fixed internal clause is interpolated
                {"query": request.query, "types": request.memory_types, "limit": request.limit},
            ).fetchall()
        return [SearchResult(memory=Memory.model_validate(row), score=row["score"]) for row in rows]

    def approve(self, auth: AuthContext, memory_id: UUID) -> WriteReceipt | None:
        with self._connection(auth) as connection:
            row = connection.cursor(row_factory=dict_row).execute(
                """UPDATE central_brain.memories
                   SET status = 'active', approved_by = %s, approved_at = now()
                   WHERE id = %s AND status = 'proposed'
                   RETURNING id, status""",
                (auth.principal.actor_id, memory_id),
            ).fetchone()
            if row:
                self._audit(connection, auth, "memory.approve", row["id"], "allowed")
        return WriteReceipt(memory_id=row["id"], status=row["status"], created=False) if row else None

    @staticmethod
    def _audit(connection, auth: AuthContext, action: str, resource_id: UUID, outcome: str) -> None:
        connection.execute(
            """INSERT INTO central_brain.audit_events
                   (id, workspace_id, actor_id, action, resource_type, resource_id, outcome)
               VALUES (%s, %s, %s, %s, 'memory', %s, %s)""",
            (
                uuid4(), auth.principal.workspace_id, auth.principal.actor_id,
                action, resource_id, outcome,
            ),
        )
