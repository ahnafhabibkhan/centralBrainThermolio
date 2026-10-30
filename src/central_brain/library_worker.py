import json
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from time import monotonic

from .auth import AuthContext
from .config import get_settings
from .library import Library
from .repository import PostgresMemoryRepository


class WorkerIdentities:
    """Refresh invited identities within workspaces administered by configured users."""

    def __init__(self, repo, settings, refresh_seconds=30):
        self.repo = repo
        self.refresh_seconds = refresh_seconds
        principals = (
            settings.oauth_principals()
            if settings.environment == "production"
            else settings.principals()
        )
        self.administrators = {
            p.workspace_id: p for p in principals.values() if "admin" in p.roles
        }
        self.configured = {(p.workspace_id, p.actor_id): p for p in principals.values()}
        self._current = ()
        self._refresh_at = 0

    def current(self):
        now = monotonic()
        if now < self._refresh_at:
            return self._current
        principals = dict(self.configured)
        for workspace_id, administrator in self.administrators.items():
            try:
                invited = self.repo.workspace_oauth_principals(AuthContext(administrator))
                for principal in invited:
                    if principal.workspace_id == workspace_id:
                        principals.setdefault((workspace_id, principal.actor_id), principal)
            except Exception:
                logging.getLogger("central_brain").exception(
                    "Worker identity refresh failed. Configured identities remain available until retry."
                )
        self._current = tuple(
            principal.model_copy(update={"roles": {"reader", "writer"}})
            for principal in principals.values()
            if {"reader", "writer"}.issubset(principal.roles)
        )
        self._refresh_at = now + self.refresh_seconds
        return self._current


def process_cycle(library, identities):
    """Process one extraction and deletion attempt for each current worker identity."""
    busy = False
    for principal in identities.current():
        auth = AuthContext(principal)
        for operation in (purge_one, process_one):
            try:
                busy = operation(library, auth) or busy
            except Exception:
                logging.getLogger("central_brain").exception(
                    "Library maintenance failed during %s.", operation.__name__
                )
    return busy


def purge_one(library, auth):
    """Retry original-object deletion after the database removal has committed."""
    from psycopg.types.json import Jsonb

    from .project_transfer import expire_incomplete
    expire_incomplete(library, auth)
    with library.repo._connection(auth) as c:
        row = c.execute('SELECT id,object_keys FROM central_brain.library_deletions WHERE purged_at IS NULL '
                        'ORDER BY deleted_at LIMIT 1 FOR UPDATE SKIP LOCKED').fetchone()
        if not row:
            c.execute("DELETE FROM central_brain.library_deletions WHERE purged_at IS NOT NULL "
                      "AND deleted_at<now()-interval '30 days'")
            return False
        keys = row['object_keys']
        if keys:
            library.store.delete(keys[0])
            keys = keys[1:]
        c.execute('UPDATE central_brain.library_deletions SET object_keys=%s, '
                  'purged_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s',
                  (Jsonb(keys), not keys, row['id']))
    return True


def process_one(library, auth):
    with library.repo._connection(auth) as c:
        job = c.execute(
            "SELECT v.*,n.name FROM central_brain.library_versions v JOIN central_brain.library_nodes n ON n.id=v.node_id "
            "WHERE v.state='queued' OR (v.state='processing' AND v.lease_at<now()-interval '5 minutes') "
            "ORDER BY v.created_at LIMIT 1 FOR UPDATE OF v SKIP LOCKED"
        ).fetchone()
        if not job:
            return False
        c.execute(
            "UPDATE central_brain.library_versions SET state='processing',lease_at=now() WHERE id=%s",
            (job["id"],),
        )
    result = {
        "sections": [],
        "state": "failed",
        "error": "Processing timed out or exceeded available resources. The original remains downloadable.",
    }
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / ('input'+Path(job['name']).suffix.lower())
            output = Path(directory) / "output.json"
            with library.store.get(job["object_key"]) as stream, source.open("wb") as destination:
                remaining = library.settings.library_file_bytes + 1
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    destination.write(chunk)
                    remaining -= len(chunk)
                if not remaining:
                    raise ValueError("Object exceeds upload limit")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "central_brain.extract",
                    str(source),
                    Path(job["name"]).suffix.lower(),
                    str(output),
                ],
                check=True,
                timeout=60,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result = json.loads(output.read_text())
    except Exception:
        logging.getLogger("central_brain").warning(
            "Document processing failed for version %s", job["id"]
        )
    with library.repo._connection(auth) as c:
        library._lock(c, auth)
        if not c.execute('SELECT id FROM central_brain.library_versions WHERE id=%s', (job['id'],)).fetchone():
            return True
        c.execute("DELETE FROM central_brain.library_sections WHERE version_id=%s", (job["id"],))
        available = (
            20000000
            - c.execute(
                "SELECT coalesce(sum(length(content)),0) AS n FROM central_brain.library_sections"
            ).fetchone()["n"]
        )
        for index, section in enumerate(result["sections"]):
            if len(section["content"]) > available:
                result["state"] = "partial"
                result["error"] = "The pilot text index is full. The original remains downloadable."
                break
            c.execute(
                "INSERT INTO central_brain.library_sections(workspace_id,version_id,ordinal,location,content) VALUES(%s,%s,%s,%s,%s)",
                (
                    auth.principal.workspace_id,
                    job["id"],
                    index,
                    section["location"],
                    section["content"],
                ),
            )
            available -= len(section["content"])
        c.execute(
            "UPDATE central_brain.library_versions SET state=%s,error=%s WHERE id=%s",
            (result["state"], result["error"], job["id"]),
        )
    return True


def main():
    settings = get_settings()
    repo = PostgresMemoryRepository(settings.database_url)
    repo.open()
    library = Library(repo, settings)
    identities = WorkerIdentities(repo, settings)
    try:
        while True:
            if not process_cycle(library, identities):
                time.sleep(3)
    finally:
        repo.close()


if __name__ == "__main__":
    main()
