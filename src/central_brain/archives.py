"""Reviewable chat archives and bounded, atomic approval batches."""

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from .library import EXTENSIONS, filename


class ArchiveFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=200000)
    encoding: Literal["utf8", "base64"] = "utf8"

    def data(self):
        filename(self.name)
        if Path(self.name).suffix.lower() not in EXTENSIONS:
            raise HTTPException(422, "This original file format is not supported.")
        try:
            return self.content.encode("utf-8") if self.encoding == "utf8" else base64.b64decode(self.content, validate=True)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, "The file content is not valid for its declared encoding.") from exc


def folder_parts(path):
    parts = path.strip("/").split("/")
    if not 1 <= len(parts) <= 20:
        raise HTTPException(422, "Choose a folder path with 1 to 20 levels.")
    return [filename(part) for part in parts]


def find_folders(library, auth, category):
    words = set(re.findall(r"[a-z0-9]+", category.lower()))
    if words & {"logo", "logos", "brand", "branding", "identity", "assets"}:
        words |= {"logo", "logos", "brand", "branding", "identity", "assets"}
    matches = []
    for node in library.snapshot(auth):
        if node["kind"] != "folder":
            continue
        overlap = words & set(re.findall(r"[a-z0-9]+", node["path"].lower()))
        if overlap:
            matches.append({"id": str(node["id"]), "path": node["path"], "matching_terms": sorted(overlap)})
    matches.sort(key=lambda item: (-len(item["matching_terms"]), item["path"].lower()))
    return {"matches": matches[:20], "truncated": len(matches) > 20,
            "instruction": "Show matching folders and ask whether to reuse one before proposing a destination."}


def propose_archive(library, auth, folder_path, summary, source_reference, files, destination_confirmed=False,
                    summary_filename='summary.md'):
    auth.require("writer")
    parts = folder_parts(folder_path)
    if parts[0].lower() == "memories":
        raise HTTPException(422, "Choose a project or category folder for originals. Memories contains Markdown memories.")
    if not 1 <= len(summary) <= 20000 or not 1 <= len(source_reference) <= 1000 or len(files) > 20:
        raise HTTPException(422, "Provide a summary up to 20,000 characters, a source, and at most 20 originals.")
    summary_filename = filename(summary_filename)
    if not summary_filename.lower().endswith('.md') or summary_filename.lower() == 'source.md':
        raise HTTPException(422, 'Choose a Markdown summary filename other than source.md.')
    source_filename = 'source.md' if summary_filename == 'summary.md' else filename(Path(summary_filename).stem + '-source.md')
    payload = {"name": parts[-1], "folder_path": "/" + "/".join(parts), "summary": summary,
               "summary_filename": summary_filename, "source_filename": source_filename,
               "source_reference": source_reference, "files": [item.model_dump() for item in files]}
    if len(json.dumps(payload).encode()) > 240000:
        raise HTTPException(413, "A chat archive supports 240 KB per proposal. Upload larger originals through the website.")
    names = {summary_filename.lower(), source_filename.lower()}
    for item in files:
        if item.name.lower() in names:
            raise HTTPException(422, "Use distinct file names. The summary and source filenames are reserved for the conversation.")
        names.add(item.name.lower())
        if not item.data():
            raise HTTPException(422, "Original files cannot be empty.")
    candidates = find_folders(library, auth, " ".join(parts))
    if candidates["matches"] and not destination_confirmed:
        return {"status": "needs_destination_confirmation", "requested_path": payload["folder_path"],
                **candidates, "stored": False}
    payload["existing_folders"] = candidates["matches"]
    payload["destination_ancestors"] = [n['id'] for n in candidates['matches']
                                        if payload['folder_path'].lower() == n['path'].lower()
                                        or payload['folder_path'].lower().startswith(n['path'].lower() + '/')]
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with library.repo._connection(auth) as c:
        library._lock(c, auth)
        old = c.execute("SELECT id,status FROM central_brain.library_suggestions WHERE action='archive' "
                        "AND created_by=%s AND payload->>'fingerprint'=%s AND status<>'reject' LIMIT 1", (auth.principal.actor_id, fingerprint)).fetchone()
        if old:
            return {"id": old["id"], "status": old["status"], "duplicate": True, "path": payload["folder_path"]}
        payload["fingerprint"] = fingerprint
        usage = c.execute("SELECT count(*) AS n,coalesce(sum(octet_length(payload::text)),0) AS bytes "
                          "FROM central_brain.library_suggestions WHERE status='proposed'").fetchone()
        if usage['n'] >= 200 or usage['bytes'] + len(json.dumps(payload).encode()) > 10000000:
            raise HTTPException(413, "Review existing proposals before submitting more archives.")
        sid = uuid4()
        c.execute("INSERT INTO central_brain.library_suggestions(id,workspace_id,created_by,action,payload) "
                  "VALUES(%s,%s,%s,'archive',%s)",
                  (sid, auth.principal.workspace_id, auth.principal.actor_id, Jsonb(payload)))
        library._audit(c, auth, "archive.propose", sid)
    return {"id": sid, "status": "proposed", "path": payload["folder_path"],
            "files": [summary_filename, source_filename] + [f.name for f in files],
            "instruction": "The folder and listed files will be created together after human approval. Only listed originals were transferred."}


def review_suggestion(library, auth, sid, approve, c, stored_keys):
    auth.require("reviewer")
    row = c.execute("SELECT * FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s AND status='proposed' FOR UPDATE", (sid, auth.principal.actor_id)).fetchone()
    if not row:
        raise HTTPException(409, "This suggestion is no longer awaiting approval.")
    p = row["payload"]
    if approve:
        parent = UUID(p["parent_id"]) if p.get("parent_id") else None
        if row["action"] == "archive":
            for ancestor_id in p.get('destination_ancestors', []):
                library._get(c, auth, UUID(ancestor_id))
            for part in folder_parts(p["folder_path"]):
                existing = c.execute("SELECT id,kind,visibility,sensitivity FROM central_brain.library_nodes WHERE parent_id IS NOT DISTINCT FROM %s "
                                     "AND lower(name)=lower(%s) AND status='active'", (parent, part)).fetchone()
                if existing:
                    library._get(c, auth, existing["id"])
                    if existing["kind"] != "folder" or existing["visibility"] != "workspace":
                        raise HTTPException(409, "The archive path conflicts with an existing item. Choose a shared folder.")
                    parent = existing["id"]
                else:
                    parent = library.folder(auth, part, parent, connection=c)
            originals = [ArchiveFile.model_validate(f) for f in p["files"]]
            manifest = "# Conversation source\n\n" + p["source_reference"] + "\n\n## Transferred originals\n\n"
            for item in originals:
                data = item.data()
                manifest += f"- {item.name}: {len(data)} bytes; SHA-256 {hashlib.sha256(data).hexdigest()}.\n"
            if not originals:
                manifest += "No original attachments were transferred.\n"
            entries = [(p.get('summary_filename', 'summary.md'), p["summary"].encode()),
                       (p.get('source_filename', 'source.md'), manifest.encode())]
            entries += [(item.name, item.data()) for item in originals]
            for name, data in entries:
                library.upload(auth, name, data, parent, connection=c, stored_keys=stored_keys)
        elif row["action"] == "folder":
            existing = c.execute("SELECT id FROM central_brain.library_nodes WHERE kind='folder' AND status='active' "
                                 "AND parent_id IS NOT DISTINCT FROM %s AND lower(name)=lower(%s)", (parent, p["name"])).fetchone()
            if existing:
                library._get(c, auth, existing["id"])
            else:
                library.folder(auth, p["name"], parent, connection=c)
        elif row["action"] == "move":
            library.move(auth, UUID(p["node_id"]), p["name"], parent, connection=c)
        else:
            raise HTTPException(422, "Unknown organization action.")
    action = "approve" if approve else "reject"
    if row['action'] == 'archive':
        # Originals now live in the library, or were rejected. Keep no second content copy here.
        p = {key: p[key] for key in ('name', 'folder_path', 'fingerprint') if key in p}
        c.execute("UPDATE central_brain.library_suggestions SET status=%s,payload=%s WHERE id=%s", (action, Jsonb(p), sid))
    else:
        c.execute("UPDATE central_brain.library_suggestions SET status=%s WHERE id=%s", (action, sid))
    library._audit(c, auth, "organization." + action, sid)


def approval_item(library, auth, c, kind, node_id):
    if kind == "suggestion":
        row = c.execute("SELECT * FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s AND status='proposed' FOR UPDATE", (node_id, auth.principal.actor_id)).fetchone()
        if not row:
            raise HTTPException(409, "The approval queue changed. Refresh and review it again.")
        value = dict(row)
    elif kind == "node":
        row = library._get(c, auth, node_id, True)
        value = dict(row)
        if row["kind"] == "memory":
            value["memory"] = library.repo._get(c, auth, row["memory_id"], lock=True)
            status = value["memory"]["status"]
        else:
            status = row["status"]
            value["versions"] = c.execute("SELECT id,version,sha256 FROM central_brain.library_versions WHERE node_id=%s ORDER BY version", (node_id,)).fetchall()
        if status != "proposed":
            raise HTTPException(409, "The approval queue changed. Refresh and review it again.")
    else:
        raise HTTPException(422, "Unknown approval type.")
    return {"kind": kind, "id": str(node_id), "fingerprint": hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()}


def approve_batch(library, auth, items):
    auth.require("reviewer")
    if not 1 <= len(items) <= 150 or len({(i['kind'], i['id']) for i in items}) != len(items):
        raise HTTPException(422, "Choose between 1 and 150 distinct pending approvals.")
    stored_keys = []
    try:
        with library.repo._connection(auth) as c:
            library._lock(c, auth)
            for item in sorted(items, key=lambda i: i["id"]):
                current = approval_item(library, auth, c, item["kind"], UUID(item["id"]))
                if current != item:
                    raise HTTPException(409, "A proposal changed. Refresh and review the queue before approving it.")
            for item in items:
                node_id = UUID(item["id"])
                if item["kind"] == "suggestion":
                    review_suggestion(library, auth, node_id, True, c, stored_keys)
                else:
                    node = library._get(c, auth, node_id, True)
                    if node["kind"] == "memory":
                        library.repo.transition(auth, node["memory_id"], "approve", connection=c)
                    else:
                        library.review(auth, node_id, True, connection=c)
    except Exception:
        for key in stored_keys:
            try:
                library.store.delete(key)
            except Exception:
                pass
        raise
    return len(items)
