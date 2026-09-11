"""Workspace document library. Object keys never come from user supplied paths."""

import hashlib
import io
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

EXTENSIONS = {".pdf", ".docx", ".xlsx", ".csv", ".md", ".txt"}


def filename(value):
    value = value.strip()
    if not value or len(value) > 240 or value in {".", ".."} or re.search(r"[/\\\x00-\x1f]", value):
        raise HTTPException(
            422, "Use a name of 1 to 240 characters without slashes or control characters."
        )
    return value


class ObjectStore:
    def __init__(self, settings):
        self.settings = settings

    def client(self):
        import boto3
        from botocore.config import Config

        return boto3.Session().client(
            "s3",
            region_name=self.settings.aws_region,
            config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
        )

    def put(self, key, data):
        if self.settings.library_bucket:
            self.client().put_object(
                Bucket=self.settings.library_bucket,
                Key=key,
                Body=data,
                ServerSideEncryption="AES256",
                ContentType="application/octet-stream",
            )
        else:
            if self.settings.environment != "local":
                raise HTTPException(503, "File storage is not configured.")
            path = Path(self.settings.library_local_path) / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def get(self, key):
        if self.settings.library_bucket:
            return self.client().get_object(Bucket=self.settings.library_bucket, Key=key)["Body"]
        return (Path(self.settings.library_local_path) / key).open("rb")

    def delete(self, key):
        if self.settings.library_bucket:
            self.client().delete_object(Bucket=self.settings.library_bucket, Key=key)
        else:
            (Path(self.settings.library_local_path) / key).unlink(missing_ok=True)


class Library:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings
        self.store = ObjectStore(settings)

    @contextmanager
    def _upload_transaction(self, auth, key):
        try:
            with self.repo._connection(auth) as c:
                yield c
        except Exception:
            # Remove an orphan original if the database transaction failed after storing it.
            try:
                self.store.delete(key)
            except Exception:
                pass
            raise

    def _get(self, c, auth, node_id, review=False):
        row = c.execute(
            "SELECT * FROM central_brain.library_nodes WHERE id=%s AND sensitivity=ANY(%s)",
            (node_id, auth.principal.sensitivities),
        ).fetchone()
        if not row or (row["status"] != "active" and not review):
            raise HTTPException(404, "File or folder not found.")
        if row["kind"] == "memory":
            memory = self.repo._get(c, auth, row["memory_id"])
            if not memory or (
                not review
                and (
                    memory["status"] != "active"
                    or (
                        memory["expires_at"]
                        and memory["expires_at"].timestamp() <= __import__("time").time()
                    )
                )
            ):
                raise HTTPException(404, "Memory not available.")
        return row

    def _parent(self, c, auth, parent):
        if parent and self._get(c, auth, parent)["kind"] != "folder":
            raise HTTPException(422, "Destination must be a folder.")

    def _unique(self, c, parent, name, exclude=None):
        if c.execute(
            "SELECT id FROM central_brain.library_nodes WHERE parent_id IS NOT DISTINCT FROM %s "
            "AND lower(name)=lower(%s) AND status<>'rejected' AND id<>%s",
            (parent, name, exclude or UUID(int=0)),
        ).fetchone():
            raise HTTPException(
                409,
                "That name already exists in this folder. Choose another name or upload a new version.",
            )

    def _lock(self, c, auth):
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("library:" + str(auth.principal.workspace_id),),
        )

    def _capacity(self, c):
        if (
            c.execute("SELECT count(*) AS n FROM central_brain.library_nodes").fetchone()["n"]
            >= 2000
        ):
            raise HTTPException(413, "The pilot supports up to 2,000 library items.")

    def _audit(self, c, auth, action, node):
        c.execute(
            "INSERT INTO central_brain.audit_events(id,workspace_id,actor_id,action,resource_type,resource_id,outcome) "
            "VALUES(%s,%s,%s,%s,'library',%s,'allowed')",
            (uuid4(), auth.principal.workspace_id, auth.principal.actor_id, action, node),
        )

    def project_memories(self, auth):
        # Backfill existing records lazily under the caller's existing row-level permissions.
        with self.repo._connection(auth) as c:
            c.execute(
                "INSERT INTO central_brain.library_nodes(id,workspace_id,created_by,name,kind,memory_id,visibility,sensitivity) "
                "SELECT id,workspace_id,created_by,'Memory-'||id||'.md','memory',id,visibility,sensitivity "
                "FROM central_brain.memories WHERE deleted_at IS NULL AND created_by IS NOT NULL ON CONFLICT DO NOTHING"
            )

    def listing(self, auth, parent=None, review=False, offset=0):
        auth.require("reviewer" if review else "reader")
        self.project_memories(auth)
        with self.repo._connection(auth) as c:
            self._parent(c, auth, parent)
            rows = c.execute(
                "SELECT n.*, v.size,v.state,v.version FROM central_brain.library_nodes n "
                "LEFT JOIN LATERAL(SELECT size,state,version FROM central_brain.library_versions WHERE node_id=n.id "
                "ORDER BY version DESC LIMIT 1) v ON true LEFT JOIN central_brain.memories m ON m.id=n.memory_id "
                "WHERE n.parent_id IS NOT DISTINCT FROM %(parent)s AND n.sensitivity=ANY(%(levels)s) "
                "AND (n.status='active' OR (%(review)s AND n.status='proposed')) "
                "AND (n.kind<>'memory' OR (m.deleted_at IS NULL AND m.status='active' AND "
                "(m.expires_at IS NULL OR m.expires_at>now()))) ORDER BY n.kind,n.name,n.id LIMIT 100 OFFSET %(offset)s",
                {
                    "parent": parent,
                    "levels": auth.principal.sensitivities,
                    "review": review,
                    "offset": max(0, offset),
                },
            ).fetchall()
            return rows

    def path(self, auth, node_id):
        with self.repo._connection(auth) as c:
            parts = []
            current = node_id
            for _ in range(32):
                if not current:
                    return "/" + "/".join(reversed(parts))
                row = self._get(c, auth, current, review="reviewer" in auth.principal.roles)
                parts.append(row["name"])
                current = row["parent_id"]
            raise HTTPException(409, "Folder nesting limit exceeded.")

    def info(self, auth, node_id, review=False):
        auth.require("reviewer" if review else "reader")
        with self.repo._connection(auth) as c:
            row = self._get(c, auth, node_id, review)
            row["versions"] = c.execute(
                "SELECT id,version,size,sha256,state,error,created_at FROM central_brain.library_versions "
                "WHERE node_id=%s ORDER BY version DESC LIMIT 100",
                (node_id,),
            ).fetchall()
            row['sheets'] = []
            if row['versions'] and row['name'].lower().endswith('.xlsx'):
                headings=c.execute("SELECT location FROM central_brain.library_sections WHERE version_id=%s "
                    "AND location LIKE 'Sheet %%' AND location NOT LIKE '%%, row %%' ORDER BY ordinal",
                    (row['versions'][0]['id'],)).fetchall()
                row['sheets']=[h['location'][6:] for h in headings]
        row["path"] = self.path(auth, node_id)
        return row

    def usage(self, auth):
        with self.repo._connection(auth) as c:
            used = c.execute(
                "SELECT coalesce(sum(size),0) AS used FROM central_brain.library_versions"
            ).fetchone()["used"]
        return {
            "used": int(used),
            "limit": self.settings.library_quota_bytes,
            "file_limit": self.settings.library_file_bytes,
        }

    def folder(self, auth, name, parent=None, connection=None):
        auth.require("reviewer")
        name = filename(name)
        from contextlib import nullcontext

        with nullcontext(connection) if connection else self.repo._connection(auth) as c:
            self._lock(c, auth)
            self._parent(c, auth, parent)
            current = parent
            for _ in range(30):
                if not current:
                    break
                current = self._get(c, auth, current)["parent_id"]
            else:
                raise HTTPException(422, "Folder nesting is limited to 30 levels.")
            self._unique(c, parent, name)
            self._capacity(c)
            node = uuid4()
            c.execute(
                "INSERT INTO central_brain.library_nodes(id,workspace_id,created_by,parent_id,name,kind) VALUES(%s,%s,%s,%s,%s,'folder')",
                (node, auth.principal.workspace_id, auth.principal.actor_id, parent, name),
            )
            self._audit(c, auth, "folder.create", node)
        return node

    def move(self, auth, node_id, name, parent=None, connection=None):
        auth.require("reviewer")
        name = filename(name)
        from contextlib import nullcontext

        with nullcontext(connection) if connection else self.repo._connection(auth) as c:
            self._lock(c, auth)
            row = self._get(c, auth, node_id, True)
            self._parent(c, auth, parent)
            if (
                row["kind"] != "folder"
                and Path(name).suffix.lower() != Path(row["name"]).suffix.lower()
            ):
                raise HTTPException(422, "Preserve the file extension when renaming.")
            current = parent
            for _ in range(31):
                if current == node_id:
                    raise HTTPException(422, "A folder cannot contain itself.")
                if not current:
                    break
                current = self._get(c, auth, current)["parent_id"]
            else:
                raise HTTPException(422, "Folder nesting limit exceeded.")
            self._unique(c, parent, name, node_id)
            c.execute(
                "UPDATE central_brain.library_nodes SET name=%s,parent_id=%s WHERE id=%s",
                (name, parent, node_id),
            )
            self._audit(c, auth, "library.move", node_id)

    def upload(
        self, auth, name, data, parent=None, node_id=None, proposed=False, visibility="workspace"
    ):
        auth.require("writer" if proposed else "reviewer")
        name = filename(name)
        if Path(name).suffix.lower() not in EXTENSIONS:
            raise HTTPException(422, "Supported formats: PDF, DOCX, XLSX, CSV, MD, TXT.")
        if visibility not in {"private", "workspace"}:
            raise HTTPException(422, "Invalid visibility.")
        if not data or len(data) > self.settings.library_file_bytes:
            raise HTTPException(413, "File must be between 1 byte and 50 MB.")
        version_id = uuid4()
        key = f"files/{auth.principal.workspace_id}/{version_id}"
        with self._upload_transaction(auth, key) as c:
            self._lock(c, auth)
            self._parent(c, auth, parent)
            used = c.execute(
                "SELECT coalesce(sum(size),0) AS used FROM central_brain.library_versions"
            ).fetchone()["used"]
            if used + len(data) > self.settings.library_quota_bytes:
                raise HTTPException(413, "The 1 GB library quota includes all versions.")
            version = 1
            if node_id:
                row = self._get(c, auth, node_id, True)
                if proposed or row["kind"] != "file" or row["status"] != "active":
                    raise HTTPException(422, "Only reviewers can version an active file.")
                if Path(name).suffix.lower() != Path(row["name"]).suffix.lower():
                    raise HTTPException(422, "A version must keep the original format.")
                version = c.execute(
                    "SELECT coalesce(max(version),0)+1 AS v FROM central_brain.library_versions WHERE node_id=%s",
                    (node_id,),
                ).fetchone()["v"]
            else:
                self._unique(c, parent, name)
                self._capacity(c)
                node_id = uuid4()
                c.execute(
                    "INSERT INTO central_brain.library_nodes(id,workspace_id,created_by,parent_id,name,kind,status,visibility) "
                    "VALUES(%s,%s,%s,%s,%s,'file',%s,%s)",
                    (
                        node_id,
                        auth.principal.workspace_id,
                        auth.principal.actor_id,
                        parent,
                        name,
                        "proposed" if proposed else "active",
                        visibility,
                    ),
                )
            c.execute(
                "INSERT INTO central_brain.library_versions(id,workspace_id,node_id,version,object_key,size,sha256) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    version_id,
                    auth.principal.workspace_id,
                    node_id,
                    version,
                    key,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                ),
            )
            # Store while the quota reservation transaction is locked. Failed storage rolls it back.
            self.store.put(key, data)
            self._audit(c, auth, "file.propose" if proposed else "file.upload", node_id)
        return node_id

    def download(self, auth, node_id, version=None):
        auth.require("reviewer")
        with self.repo._connection(auth) as c:
            row = self._get(c, auth, node_id, True)
            if row["kind"] == "memory":
                m = self.repo._get(c, auth, row["memory_id"])
                return row["name"], io.BytesIO(
                    (
                        m["content"] + "\n\nSource: " + str(m["source"].get("reference", "")) + "\n"
                    ).encode()
                )
            v = c.execute(
                "SELECT object_key FROM central_brain.library_versions WHERE node_id=%s "
                "AND (%s::int IS NULL OR version=%s) ORDER BY version DESC LIMIT 1",
                (node_id, version, version),
            ).fetchone()
            if not v:
                raise HTTPException(404, "Version not found.")
        return row["name"], self.store.get(v["object_key"])

    def read(self, auth, node_id, version=None, start=0, limit=3, review=False):
        auth.require("reviewer" if review else "reader")
        with self.repo._connection(auth) as c:
            row = self._get(c, auth, node_id, review)
            if row["kind"] == "memory":
                m = self.repo._get(c, auth, row["memory_id"])
                return {
                    "file_id": node_id,
                    "name": row["name"],
                    "sections": [
                        {
                            "ordinal": 0,
                            "location": "Memory",
                            "content": m["content"][: self.settings.max_context_chars],
                        }
                    ],
                    "source": m["source"],
                }
            v = c.execute(
                "SELECT id,version,state,error FROM central_brain.library_versions WHERE node_id=%s "
                "AND (%s::int IS NULL OR version=%s) ORDER BY version DESC LIMIT 1",
                (node_id, version, version),
            ).fetchone()
            if not v:
                raise HTTPException(404, "Version not found.")
            rows = c.execute(
                "SELECT ordinal,location,content FROM central_brain.library_sections WHERE version_id=%s "
                "AND ordinal>=%s ORDER BY ordinal LIMIT %s",
                (v["id"], max(0, start), min(max(1, limit), 3)),
            ).fetchall()
        return {
            "file_id": node_id,
            "name": row["name"],
            "version": v["version"],
            "state": v["state"],
            "error": v["error"],
            "sections": rows,
            "next_section": rows[-1]["ordinal"] + 1 if rows else None,
            "reference_material": True,
        }

    def search(self, auth, query, parent=None, limit=8):
        auth.require("reader")
        if not query.strip() or len(query) > 2000:
            raise HTTPException(422, "Enter a search query of up to 2,000 characters.")
        self.project_memories(auth)
        with self.repo._connection(auth) as c:
            self._parent(c, auth, parent)
            rows = c.execute(
                """WITH RECURSIVE paths AS (
              SELECT id,name::text AS path FROM central_brain.library_nodes WHERE parent_id IS NULL
              UNION ALL SELECT n.id,p.path||' / '||n.name FROM central_brain.library_nodes n JOIN paths p ON n.parent_id=p.id
            ), descendants AS (
              SELECT id FROM central_brain.library_nodes WHERE id=%(parent)s
              UNION ALL SELECT n.id FROM central_brain.library_nodes n JOIN descendants d ON n.parent_id=d.id
            ), q AS (SELECT websearch_to_tsquery('english',%(query)s) AS term), docs AS (
              SELECT n.id,n.name,n.parent_id,v.version,s.ordinal,s.location,s.content,
                setweight(to_tsvector('english',coalesce(p.path,n.name)),'A') || s.search_document AS document
              FROM central_brain.library_nodes n
              JOIN LATERAL(SELECT * FROM central_brain.library_versions WHERE node_id=n.id ORDER BY version DESC LIMIT 1)v ON true
              JOIN central_brain.library_sections s ON s.version_id=v.id
              LEFT JOIN paths p ON p.id=n.id
              WHERE n.status='active' AND n.sensitivity=ANY(%(levels)s)
              AND (%(parent)s::uuid IS NULL OR n.parent_id IN(SELECT id FROM descendants))
              UNION ALL
              SELECT n.id,n.name,n.parent_id,1,0,'Memory',m.content,m.search_document || setweight(to_tsvector('english',coalesce(p.path,n.name)),'A')
              FROM central_brain.library_nodes n JOIN central_brain.memories m ON m.id=n.memory_id
              LEFT JOIN paths p ON p.id=n.id
              WHERE m.status='active' AND m.deleted_at IS NULL AND (m.expires_at IS NULL OR m.expires_at>now())
              AND m.sensitivity=ANY(%(levels)s) AND (%(parent)s::uuid IS NULL OR n.parent_id IN(SELECT id FROM descendants))
            ) SELECT id,name,version,ordinal,location,left(content,1400) AS excerpt,
               ts_rank_cd(document,q.term) AS score FROM docs,q WHERE document @@ q.term
               ORDER BY score DESC,id,ordinal LIMIT %(limit)s""",
                {
                    "query": query,
                    "parent": parent,
                    "levels": auth.principal.sensitivities,
                    "limit": min(max(limit, 1), 8),
                },
            ).fetchall()
        for row in rows:
            row["path"] = self.path(auth, row["id"])
        return {"results": rows, "reference_material": True}

    def suggest(self, auth, action, payload):
        auth.require("writer")
        if action not in {"folder", "move"}:
            raise HTTPException(422, "Unsupported suggestion.")
        filename(payload.get("name", ""))
        parent = UUID(payload["parent_id"]) if payload.get("parent_id") else None
        with self.repo._connection(auth) as c:
            self._parent(c, auth, parent)
            if action == "move":
                self._get(c, auth, UUID(payload["node_id"]))
            sid = uuid4()
            c.execute(
                "INSERT INTO central_brain.library_suggestions(id,workspace_id,created_by,action,payload) VALUES(%s,%s,%s,%s,%s)",
                (sid, auth.principal.workspace_id, auth.principal.actor_id, action, Jsonb(payload)),
            )
            self._audit(c, auth, "organization.propose", sid)
        return {"id": sid, "status": "proposed"}

    def review(self, auth, node_id, approve):
        auth.require("reviewer")
        with self.repo._connection(auth) as c:
            row = self._get(c, auth, node_id, True)
            if row["status"] != "proposed":
                raise HTTPException(409, "Only pending files can be reviewed.")
            c.execute(
                "UPDATE central_brain.library_nodes SET status=%s WHERE id=%s",
                ("active" if approve else "rejected", node_id),
            )
            self._audit(c, auth, "file.approve" if approve else "file.reject", node_id)
