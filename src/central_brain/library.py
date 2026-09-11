"""Workspace document library. Object keys never come from user supplied paths."""

import hashlib
import io
import json
import re
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

EXTENSIONS = {".pdf", ".docx", ".xlsx", ".csv", ".md", ".txt", ".svg", ".png", ".jpg", ".jpeg", ".webp"}


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

    def workspace_folders(self, auth):
        """Return an access-filtered tree and create the requested workspace roots."""
        auth.require("reviewer")
        with self.repo._connection(auth) as c:
            self._lock(c, auth)
            roots = {}
            for name in ("Memories", "Skills"):
                row = c.execute(
                    "SELECT id,kind FROM central_brain.library_nodes WHERE parent_id IS NULL "
                    "AND lower(name)=lower(%s) AND status='active' LIMIT 1", (name,)
                ).fetchone()
                if row and row["kind"] == "folder":
                    roots[name] = row["id"]
                elif not row:
                    roots[name] = self.folder(auth, name, connection=c)
                else:
                    raise HTTPException(409, f"Rename the root file named {name} to make room for the workspace folder.")
            if roots.get("Memories"):
                c.execute("UPDATE central_brain.library_nodes SET parent_id=%s "
                          "WHERE kind='memory' AND parent_id IS NULL AND sensitivity=ANY(%s)",
                          (roots["Memories"], auth.principal.sensitivities))
            if roots.get("Skills"):
                candidates = c.execute(
                    "SELECT id,name FROM central_brain.library_nodes WHERE kind='file' "
                    "AND status='active' AND parent_id IS NULL AND lower(name) ~ '(^|_)skill[.]md$' "
                    "AND sensitivity=ANY(%s)",
                    (auth.principal.sensitivities,),
                ).fetchall()
                for candidate in candidates:
                    collision = c.execute(
                        "SELECT 1 FROM central_brain.library_nodes WHERE parent_id=%s "
                        "AND lower(name)=lower(%s) AND status<>'rejected' LIMIT 1",
                        (roots["Skills"], candidate["name"]),
                    ).fetchone()
                    if not collision:
                        c.execute(
                            "UPDATE central_brain.library_nodes SET parent_id=%s WHERE id=%s",
                            (roots["Skills"], candidate["id"]),
                        )
                        self._audit(c, auth, "skill.auto_organize", candidate["id"])
            rows = c.execute(
                "WITH RECURSIVE tree AS (SELECT id,parent_id,name,ARRAY[name] AS parts "
                "FROM central_brain.library_nodes WHERE kind='folder' AND status='active' "
                "AND parent_id IS NULL AND sensitivity=ANY(%s) UNION ALL "
                "SELECT n.id,n.parent_id,n.name,t.parts||n.name FROM central_brain.library_nodes n "
                "JOIN tree t ON n.parent_id=t.id WHERE n.kind='folder' AND n.status='active' "
                "AND n.sensitivity=ANY(%s) AND cardinality(t.parts)<32) "
                "SELECT *,array_to_string(parts,' / ') AS path,cardinality(parts)-1 AS depth "
                "FROM tree ORDER BY parts LIMIT 2000",
                (auth.principal.sensitivities, auth.principal.sensitivities),
            ).fetchall()
        return roots, rows

    def category_folder(self, auth, name):
        auth.require("reader")
        with self.repo._connection(auth) as c:
            row = c.execute(
                "SELECT id FROM central_brain.library_nodes WHERE parent_id IS NULL "
                "AND kind='folder' AND status='active' AND lower(name)=lower(%s) LIMIT 1",
                (name,),
            ).fetchone()
        return row["id"] if row else None

    def snapshot(self, auth, review=False):
        """Read one consistent hierarchy with the caller's access and approval filters."""
        auth.require("reviewer" if review else "reader")
        self.project_memories(auth)
        with self.repo._connection(auth) as c:
            rows = c.execute(
                """WITH RECURSIVE visible AS (
                    SELECT n.*, CASE WHEN n.kind='memory' THEN m.status ELSE n.status END AS effective_status,
                        m.memory_type,md5(m.content||m.source::text||m.metadata::text) AS content_hash,m.expires_at,
                        v.version,v.size,v.state,v.sha256,
                        CASE WHEN n.kind='memory' THEN m.created_at ELSE coalesce(v.created_at,n.created_at) END AS changed_at
                    FROM central_brain.library_nodes n
                    LEFT JOIN central_brain.memories m ON m.id=n.memory_id
                    LEFT JOIN LATERAL (SELECT * FROM central_brain.library_versions WHERE node_id=n.id
                        ORDER BY version DESC LIMIT 1) v ON true
                    WHERE n.sensitivity=ANY(%(levels)s) AND
                        ((n.kind<>'memory' AND (n.status='active' OR (%(review)s AND n.status='proposed'))) OR
                         (n.kind='memory' AND m.deleted_at IS NULL AND m.sensitivity=ANY(%(levels)s)
                          AND (m.expires_at IS NULL OR m.expires_at>now())
                          AND (m.status='active' OR (%(review)s AND m.status='proposed'))))
                ), tree AS (
                    SELECT v.*,ARRAY[v.name] AS parts,ARRAY[v.id] AS ancestors FROM visible v WHERE parent_id IS NULL
                    UNION ALL SELECT v.*,t.parts||v.name,t.ancestors||v.id FROM visible v JOIN tree t ON v.parent_id=t.id
                    WHERE t.kind='folder' AND cardinality(t.parts)<32 AND NOT v.id=ANY(t.ancestors)
                ) SELECT * FROM tree ORDER BY parts,id LIMIT 2001""",
                {"levels": auth.principal.sensitivities, "review": review},
            ).fetchall()
        for row in rows:
            row["status"] = row.pop("effective_status")
            row["path"] = "/" + "/".join(row["parts"])
            row["category"] = "memory" if row["kind"] == "memory" else (
                "skill" if row["kind"] == "file" and row["parts"][0].lower() == "skills"
                and row["name"].lower().endswith(".md") else row["kind"])
        return rows

    def context(self, auth, review=False):
        """Recompute a compact workspace map after content or indexing changes."""
        nodes = self.snapshot(auth, review)
        with self.repo._connection(auth) as c:
            registered = c.execute(
                "SELECT s.name,v.version,v.content_sha256 FROM central_brain.skills s "
                "JOIN central_brain.skill_versions v ON v.skill_id=s.id AND v.workspace_id=s.workspace_id "
                "WHERE v.status='active' ORDER BY s.name,v.version"
            ).fetchall()
            suggestions = c.execute("SELECT id,action,payload,status FROM central_brain.library_suggestions "
                                    "WHERE status='proposed' ORDER BY id").fetchall() if review else []
            deletions = c.execute("SELECT id,name,nullif(path,'') AS path,kind,deleted_at FROM central_brain.library_deletions "
                                  "WHERE sensitivity=ANY(%s) AND (original_status='active' OR %s) "
                                  "AND deleted_at>now()-interval '30 days' ORDER BY deleted_at DESC,id LIMIT 21",
                                  (auth.principal.sensitivities, review)).fetchall()
        stable = json.dumps({"nodes": nodes, "registered": registered, "suggestions": suggestions,
                             "deletions": deletions}, default=str,
                            sort_keys=True, separators=(",", ":"))
        active = [n for n in nodes if n["status"] == "active"]
        return {
            "revision": hashlib.sha256(stable.encode()).hexdigest()[:16],
            "counts": {
                "files": sum(n["category"] == "file" for n in active),
                "memories": sum(n["category"] == "memory" for n in active),
                "skills": sum(n["category"] == "skill" for n in active) + len(registered),
                "pending": sum(n["status"] == "proposed" for n in nodes) + len(suggestions),
                "indexing": sum(n.get("state") in {"queued", "processing"} for n in nodes),
            },
            "folders": [{"id": str(n["id"]), "path": n["path"][:300]} for n in active
                        if n["kind"] == "folder"][:40],
            "recent_files": [{"id": str(n["id"]), "path": n["path"][:300],
                              "version": n["version"], "state": n["state"], "category": n["category"],
                              "copied_from": n["copied_from"]}
                             for n in sorted(active, key=lambda n: n["changed_at"], reverse=True)
                             if n["kind"] != "folder"][:8],
            "recent_deletions": [{**d, 'id': str(d['id']), 'deleted_at': d['deleted_at'].isoformat(),
                                  'path': d['path'][:300] if d['path'] else None} for d in deletions[:20]],
            "deletions_truncated": len(deletions) > 20,
            "deletion_window_days": 30,
            "truncated": len(nodes) > 2000 or sum(n["kind"] == "folder" for n in active) > 40,
            "reference_material": True,
        }

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

    def _deletion_plan(self, c, auth, node_id):
        auth.require('admin')
        root = self._get(c, auth, node_id, True)
        if root['kind'] == 'folder' and root['parent_id'] is None and root['name'] in {'Memories', 'Skills'}:
            raise HTTPException(422, 'Memories and Skills are permanent workspace folders. You can delete their contents.')
        rows = c.execute('''WITH RECURSIVE subtree AS (
            SELECT n.*,ARRAY[n.name] AS parts FROM central_brain.library_nodes n WHERE id=%s
            UNION ALL SELECT n.*,s.parts||n.name FROM central_brain.library_nodes n
            JOIN subtree s ON n.parent_id=s.id
        ) SELECT * FROM subtree ORDER BY id''', (node_id,)).fetchall()
        if any(r['sensitivity'] not in auth.principal.sensitivities for r in rows):
            raise HTTPException(403, 'This folder contains items outside your access. Nothing was deleted.')
        ids = [r['id'] for r in rows]
        versions = c.execute('SELECT id,node_id,version,size,sha256,object_key FROM central_brain.library_versions '
                             'WHERE node_id=ANY(%s) ORDER BY id', (ids,)).fetchall()
        memories = c.execute('SELECT id,status,deleted_at,md5(content||source::text||metadata::text) AS fingerprint '
                             'FROM central_brain.memories WHERE id=ANY(%s) ORDER BY id FOR UPDATE',
                             ([r['memory_id'] for r in rows if r['memory_id']],)).fetchall()
        path = self.path(auth, node_id)
        fingerprint = hashlib.sha256(json.dumps([path, rows, versions, memories], default=str, sort_keys=True).encode()).hexdigest()
        return {'root': root, 'path': path, 'rows': rows, 'versions': versions, 'memories': memories, 'token': fingerprint,
                'counts': {kind: sum(r['kind'] == kind for r in rows) for kind in ('folder','file','memory')},
                'version_count': len(versions), 'bytes': sum(v['size'] for v in versions)}

    def deletion_plan(self, auth, node_id):
        with self.repo._connection(auth) as c:
            self._lock(c, auth)
            plan = self._deletion_plan(c, auth, node_id)
        return plan

    def delete(self, auth, node_id, confirmation, token):
        from psycopg.errors import ForeignKeyViolation
        auth.require('admin')
        try:
            with self.repo._connection(auth) as c:
                self._lock(c, auth)
                plan = self._deletion_plan(c, auth, node_id)
                path = plan['path']
                if confirmation != plan['root']['name']:
                    raise HTTPException(422, 'Type the exact item name to confirm deletion.')
                if token != plan['token']:
                    raise HTTPException(409, 'These items changed since you opened the warning. Reopen Delete to review the latest contents.')
                ids = [r['id'] for r in plan['rows']]
                memory_status = {m['id']: m['status'] for m in plan['memories']}
                for row in plan['rows']:
                    keys = [v['object_key'] for v in plan['versions'] if v['node_id'] == row['id']]
                    item_path = path + ('/' + '/'.join(row['parts'][1:]) if len(row['parts']) > 1 else '')
                    c.execute('''INSERT INTO central_brain.library_deletions
                        (id,workspace_id,created_by,visibility,sensitivity,name,path,kind,original_status,deleted_by,object_keys)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                        (row['id'], row['workspace_id'], row['created_by'], row['visibility'], row['sensitivity'],
                         row['name'], item_path, row['kind'], memory_status.get(row['memory_id'], row['status']),
                         auth.principal.actor_id, Jsonb(keys)))
                    if row['memory_id']:
                        c.execute("UPDATE central_brain.memories SET content='[Deleted]',source='{}',metadata='{}',"
                                  "subject_ref=NULL,confidence=NULL,dedupe_key=NULL,status='archived',deleted_at=now() WHERE id=%s",
                                  (row['memory_id'],))
                    self._audit(c, auth, 'library.delete', row['id'])
                c.execute('DELETE FROM central_brain.library_sections WHERE version_id IN '
                          '(SELECT id FROM central_brain.library_versions WHERE node_id=ANY(%s))', (ids,))
                c.execute('DELETE FROM central_brain.library_versions WHERE node_id=ANY(%s)', (ids,))
                # The foreign key also protects descendants hidden by row-level access rules.
                c.execute('DELETE FROM central_brain.library_nodes WHERE id=ANY(%s)', (ids,))
                c.execute("UPDATE central_brain.library_suggestions SET status='cancelled' WHERE status='proposed' "
                          "AND (payload->>'node_id'=ANY(%s) OR payload->>'parent_id'=ANY(%s))",
                          ([str(i) for i in ids], [str(i) for i in ids]))
                return plan['root']['parent_id']
        except ForeignKeyViolation:
            raise HTTPException(409, 'This folder contains items that cannot be deleted with your access. Nothing was deleted.') from None

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
            if row["kind"] == "folder" and row["parent_id"] is None and row["name"] in {"Skills", "Memories"}:
                if parent is not None or name != row["name"]:
                    raise HTTPException(422, "Memories and Skills are fixed top-level workspace folders.")
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

    def copy(self, auth, node_id, name, parent=None, version=None, expected_sha256=None,
             connection=None, stored_keys=None):
        """Create an independent original and preserve the source's access restrictions."""
        auth.require('reviewer')
        name = filename(name)
        keys = stored_keys if stored_keys is not None else []
        try:
            with nullcontext(connection) if connection else self.repo._connection(auth) as c:
                self._lock(c, auth)
                source = self._get(c, auth, node_id)
                if source['kind'] != 'file':
                    raise HTTPException(422, 'Copy an approved uploaded file. Canonical memories remain single records.')
                if Path(name).suffix.lower() != Path(source['name']).suffix.lower():
                    raise HTTPException(422, 'Preserve the original file extension when copying.')
                self._parent(c, auth, parent)
                self._unique(c, parent, name)
                original = c.execute('SELECT * FROM central_brain.library_versions WHERE node_id=%s '
                                     'AND (%s::int IS NULL OR version=%s) ORDER BY version DESC LIMIT 1',
                                     (node_id, version, version)).fetchone()
                if not original or (expected_sha256 and original['sha256'] != expected_sha256):
                    raise HTTPException(409, 'The source version is unavailable or changed. Review the file again.')
                visibility, sensitivity = source['visibility'], source['sensitivity']
                if parent:
                    destination = self._get(c, auth, parent)
                    if destination['visibility'] == 'private':
                        visibility = 'private'
                    levels = ['public', 'internal', 'confidential', 'restricted']
                    sensitivity = max([sensitivity, destination['sensitivity']], key=levels.index)
                with self.store.get(original['object_key']) as stream:
                    data = stream.read(self.settings.library_file_bytes + 1)
                if len(data) != original['size'] or hashlib.sha256(data).hexdigest() != original['sha256']:
                    raise HTTPException(409, 'The stored original failed its integrity check. No copy was created.')
                new_id = self.upload(auth, name, data, parent, visibility=visibility, connection=c, stored_keys=keys)
                provenance = {'file_id': str(node_id), 'version': original['version'], 'sha256': original['sha256']}
                c.execute('UPDATE central_brain.library_nodes SET sensitivity=%s,copied_from=%s WHERE id=%s',
                          (sensitivity, Jsonb(provenance), new_id))
                text_usage = c.execute('SELECT coalesce(sum(length(content)),0) AS n FROM central_brain.library_sections').fetchone()['n']
                source_text = c.execute('SELECT coalesce(sum(length(content)),0) AS n FROM central_brain.library_sections '
                                        'WHERE version_id=%s', (original['id'],)).fetchone()['n']
                if original['state'] in {'ready', 'partial', 'unsearchable'} and text_usage + source_text <= 20000000:
                    new_version = c.execute('SELECT id FROM central_brain.library_versions WHERE node_id=%s', (new_id,)).fetchone()['id']
                    c.execute('INSERT INTO central_brain.library_sections(workspace_id,version_id,ordinal,location,content) '
                              'SELECT workspace_id,%s,ordinal,location,content FROM central_brain.library_sections '
                              'WHERE version_id=%s', (new_version, original['id']))
                    c.execute('UPDATE central_brain.library_versions SET state=%s,error=%s WHERE id=%s',
                              (original['state'], original['error'], new_version))
                self._audit(c, auth, 'file.copy', new_id)
            return new_id
        except Exception:
            if connection is None:
                for key in keys:
                    try:
                        self.store.delete(key)
                    except Exception:
                        pass
            raise

    def upload(
        self, auth, name, data, parent=None, node_id=None, proposed=False, visibility="workspace",
        connection=None, stored_keys=None,
    ):
        auth.require("writer" if proposed else "reviewer")
        name = filename(name)
        if Path(name).suffix.lower() not in EXTENSIONS:
            raise HTTPException(422, "Supported formats: PDF, DOCX, XLSX, CSV, MD, TXT, SVG, PNG, JPG, WEBP.")
        if visibility not in {"private", "workspace"}:
            raise HTTPException(422, "Invalid visibility.")
        if not data or len(data) > self.settings.library_file_bytes:
            raise HTTPException(413, "File must be between 1 byte and 50 MB.")
        version_id = uuid4()
        key = f"files/{auth.principal.workspace_id}/{version_id}"
        with nullcontext(connection) if connection else self._upload_transaction(auth, key) as c:
            self._lock(c, auth)
            self._parent(c, auth, parent)
            used = c.execute(
                "SELECT coalesce(sum(size),0) AS used FROM central_brain.library_versions"
            ).fetchone()["used"]
            if used + len(data) > self.settings.library_quota_bytes:
                raise HTTPException(413, "The shared file storage limit has been reached. All stored versions count toward the limit.")
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
            if stored_keys is not None:
                stored_keys.append(key)
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
        from .retrieval import search
        return search(self, auth, query, parent, limit)

    def suggest(self, auth, action, payload):
        auth.require("writer")
        if action not in {"folder", "move", "copy"}:
            raise HTTPException(422, "Unsupported suggestion.")
        filename(payload.get("name", ""))
        parent = UUID(payload["parent_id"]) if payload.get("parent_id") else None
        with self.repo._connection(auth) as c:
            self._lock(c, auth)
            self._parent(c, auth, parent)
            if action in {'move', 'copy'}:
                if not payload.get('node_id'):
                    raise HTTPException(422, 'Choose a source item.')
                source = self._get(c, auth, UUID(payload["node_id"]))
                if action == 'copy':
                    if source['kind'] != 'file':
                        raise HTTPException(422, 'Only approved uploaded files can be copied.')
                    if Path(payload['name']).suffix.lower() != Path(source['name']).suffix.lower():
                        raise HTTPException(422, 'Preserve the original file extension when copying.')
                    self._unique(c, parent, payload['name'])
                    original = c.execute('SELECT version,sha256 FROM central_brain.library_versions WHERE node_id=%s '
                                         'ORDER BY version DESC LIMIT 1', (source['id'],)).fetchone()
                    if not original:
                        raise HTTPException(409, 'The original file is unavailable.')
                    payload = dict(payload, source_version=original['version'], source_sha256=original['sha256'])
            sid = uuid4()
            c.execute(
                "INSERT INTO central_brain.library_suggestions(id,workspace_id,created_by,action,payload) VALUES(%s,%s,%s,%s,%s)",
                (sid, auth.principal.workspace_id, auth.principal.actor_id, action, Jsonb(payload)),
            )
            self._audit(c, auth, "organization.propose", sid)
        return {"id": sid, "status": "proposed"}

    def review(self, auth, node_id, approve, connection=None):
        auth.require("reviewer")
        with nullcontext(connection) if connection else self.repo._connection(auth) as c:
            self._lock(c, auth)
            row = self._get(c, auth, node_id, True)
            if row["status"] != "proposed":
                raise HTTPException(409, "Only pending files can be reviewed.")
            c.execute(
                "UPDATE central_brain.library_nodes SET status=%s WHERE id=%s",
                ("active" if approve else "rejected", node_id),
            )
            if approve and row["parent_id"] is None and re.search(r'(^|_)skill[.]md$', row["name"].lower()):
                skills = c.execute(
                    "SELECT id FROM central_brain.library_nodes WHERE parent_id IS NULL "
                    "AND kind='folder' AND status='active' AND lower(name)='skills' LIMIT 1"
                ).fetchone()
                if skills:
                    collision = c.execute(
                        "SELECT 1 FROM central_brain.library_nodes WHERE parent_id=%s AND lower(name)=lower(%s) "
                        "AND status<>'rejected' AND id<>%s LIMIT 1",
                        (skills["id"], row["name"], node_id),
                    ).fetchone()
                    if not collision:
                        c.execute(
                            "UPDATE central_brain.library_nodes SET parent_id=%s WHERE id=%s",
                            (skills["id"], node_id),
                        )
            self._audit(c, auth, "file.approve" if approve else "file.reject", node_id)
