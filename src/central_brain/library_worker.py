import json
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .auth import AuthContext
from .config import get_settings
from .library import Library
from .repository import PostgresMemoryRepository


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
    principals = (
        settings.oauth_principals()
        if settings.environment == "production"
        else settings.principals()
    )
    unique = {(p.workspace_id, p.actor_id): p for p in principals.values()}
    try:
        while True:
            busy = False
            for principal in unique.values():
                try:
                    busy = process_one(library, AuthContext(principal)) or busy
                except Exception:
                    logging.exception("Library queue unavailable")
            if not busy:
                time.sleep(3)
    finally:
        repo.close()


if __name__ == "__main__":
    main()
