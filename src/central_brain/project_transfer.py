"""Transfer project originals separately, retaining only their inventory in the database."""
import hashlib
import time
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID, uuid4

import jwt
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .archives import folder_parts, propose_archive
from .auth import AuthContext


def reserved_bytes(c):
    return c.execute('SELECT coalesce(sum(size_bytes),0) AS n FROM central_brain.transfer_reservations').fetchone()['n']


def prepare_project(library, settings, auth, folder_path, summary, source_reference, files,
                    destination_confirmed=False, summary_filename='summary.md'):
    with library.repo._connection(auth) as c:
        library._lock(c, auth)
        return _prepare_project(library, settings, auth, folder_path, summary, source_reference,
                                files, destination_confirmed, summary_filename, c)


def _prepare_project(library, settings, auth, folder_path, summary, source_reference, files,
                     destination_confirmed, summary_filename, connection):
    from .library import EXTENSIONS
    parts = folder_parts(folder_path)
    if not 1 <= len(files) <= 100:
        raise HTTPException(413, 'Transfer up to 100 files per batch. Use further batches for the same project.')
    names = {summary_filename.lower(), ('source.md' if summary_filename == 'summary.md'
             else Path(summary_filename).stem + '-source.md').lower()}
    for f in files:
        path = folder_parts(f.name)
        if f.name != '/'.join(path) or len(parts) + len(path) > 20:
            raise HTTPException(422, 'Use relative file paths with no more than 20 total folder levels.')
        if Path(f.name).suffix.lower() not in EXTENSIONS or f.name.lower() in names:
            raise HTTPException(422, 'Use supported, distinct file paths, without reserved summary names.')
        if f.size_bytes > settings.library_file_bytes:
            raise HTTPException(413, 'Each original can be up to 100 MB.')
        names.add(f.name.lower())
    for name in names:
        if any('/'.join(name.split('/')[:i]) in names for i in range(1, len(name.split('/')))):
            raise HTTPException(422, 'A file path cannot also be a folder path.')
    # Reuse destination validation and the existing reviewable archive workflow.
    prepared = propose_archive(library, auth, folder_path, summary, source_reference, [],
                               destination_confirmed, summary_filename, connection=connection)
    if prepared.get('status') == 'needs_destination_confirmation':
        return prepared
    sid = prepared['id']
    inventory = [f.model_dump() for f in files]
    with nullcontext(connection) as c:
        library._lock(c, auth)
        row = c.execute("SELECT payload,status FROM central_brain.library_suggestions WHERE id=%s FOR UPDATE", (sid,)).fetchone()
        p = row['payload']
        if p.get('transfer_inventory') != inventory:
            if p.get('transfer_inventory') or row['status'] != 'proposed':
                raise HTTPException(409, 'Use a distinct summary filename for this new batch.')
            used = c.execute('SELECT coalesce(sum(size),0) AS n FROM central_brain.library_versions').fetchone()['n']
            total = sum(f.size_bytes for f in files)
            if used + reserved_bytes(c) + total > settings.library_quota_bytes:
                raise HTTPException(413, 'The project exceeds the remaining shared storage allowance.')
            c.execute('INSERT INTO central_brain.transfer_reservations(id,workspace_id,created_by,size_bytes) VALUES(%s,%s,%s,%s)',
                      (sid, auth.principal.workspace_id, auth.principal.actor_id, total))
            p.update(transfer_inventory=inventory, reserved_bytes=total, transfer_expires=int(time.time()) + 86400,
                     files=[{**f.model_dump(), 'object_key': f'files/{auth.principal.workspace_id}/{uuid4()}',
                             'received': False} for f in files])
            c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(p), sid))
        if row['status'] != 'proposed':
            return {'id': sid, 'status': row['status'], 'duplicate': True}
        now = int(time.time())
        if now >= p['transfer_expires']:
            # An authenticated retry renews this pending inventory without losing progress.
            p['transfer_expires'] = now + 86400
            c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(p), sid))
        uploads = []
        for i, item in enumerate(p['files']):
            if p.get('reviews', {}).get(str(i), 'pending') != 'pending':
                continue
            claims = {'iss': 'central-brain:project-transfer', 'aud': settings.public_url,
                      'iat': now, 'exp': min(now + 3600, p['transfer_expires']), 'sid': str(sid), 'index': i,
                      'actor': str(auth.principal.actor_id), 'workspace': str(auth.principal.workspace_id)}
            uploads.append({'file_index': i, 'name': item['name'], 'received': item['received'],
                            'upload_url': settings.public_url + '/archive-original',
                            'upload_token': jwt.encode(claims, settings.session_secret, algorithm='HS256')})
    return {'id': sid, 'status': 'awaiting_transfer', 'mode': 'individual_raw_files',
            'uploads': uploads, 'file_limit_bytes': settings.library_file_bytes,
            'completion_url': settings.public_url + '/library#approvals',
            'connector_fallback': 'For GitHub files, obtain fresh download_url values from the connected GitHub '
                                 'Contents API at the inventoried commit and call import_github_original for each file. '
                                 'Central Brain downloads and verifies the originals directly. '
                                 'If the application upload host is unreachable, call prepare_original_upload '
                                 'for each missing file, POST its raw bytes directly to S3 with the returned multipart '
                                 'fields, then call complete_original_upload. This avoids binary tool arguments. '
                                 'If S3 is blocked too, use the browser completion_url. For small transfers only, '
                                 'call upload_chat_archive_chunk through MCP with '
                                 'programmatically forwarded base64 chunks of 131072 raw bytes. Use '
                                 'get_chat_archive_upload_status to resume. Never transcribe binary data manually. '
                                 'If this host cannot forward file bytes to tools, open completion_url and upload '
                                 'the originals there. A transfer waiting for bytes is not ready for approval.',
            'instruction': 'Upload each missing file sequentially from your code environment with an HTTP POST '
                           'to its upload_url, Content-Type: application/octet-stream and Authorization: Bearer '
                           '<upload_token>. Send the exact raw bytes, without base64 or JSON. Check every response. '
                           'On HTTP 429, wait for Retry-After before retrying. '
                           'Tokens last one hour. Repeat the identical preparation to renew access and resume '
                           'an existing pending transfer. Received originals and partial chunks are preserved. '
                           'Use each returned file_index for S3 or chunk tools; do not renumber remaining files after review. '
                           'Each received original can be approved independently. Missing files remain pending. Preserve relative paths. '
                           'For more than 100 files, use further batches with distinct summary filenames. '
                           'Never expose tokens or claim files were saved if an upload failed.'}


def transfer_identity(settings, token):
    try:
        claims = jwt.decode(token, settings.session_secret, algorithms=['HS256'], audience=settings.public_url,
                            issuer='central-brain:project-transfer', options={'require': ['exp', 'iat', 'sid', 'index', 'actor', 'workspace']})
        UUID(claims['sid'])
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(401, 'This file transfer is invalid or expired.') from exc
    registered = settings.oauth_principals() if settings.environment == 'production' else settings.principals()
    current = next((p for p in registered.values() if str(p.actor_id) == claims['actor']
                    and str(p.workspace_id) == claims['workspace'] and 'writer' in p.roles), None)
    if current is None:
        raise HTTPException(403, 'The upload identity no longer has write access.')
    return AuthContext(current.model_copy(update={'roles': {'reader', 'writer'}})), claims


def receive_original(library, settings, token, data):
    auth, claims = transfer_identity(settings, token)
    return store_original(library, auth, UUID(claims['sid']), claims['index'], data)


def original_intact(library, item):
    """Check a received original before acknowledging a retry as already complete."""
    from botocore.exceptions import ClientError
    digest = hashlib.sha256()
    remaining = item['size_bytes'] + 1
    total = 0
    try:
        with library.store.get(item['object_key']) as stream:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
                total += len(chunk)
    except FileNotFoundError:
        return False
    except ClientError as exc:
        if exc.response['Error']['Code'] in {'NoSuchKey', '404'}:
            return False
        raise
    return total == item['size_bytes'] and digest.hexdigest() == item['sha256']


def store_original(library, auth, sid, index, data):
    auth.require('writer')
    claims = {'sid': str(sid), 'index': index}
    with library.repo._connection(auth) as c:
        library._lock(c, auth)
        row = c.execute("SELECT payload FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s "
                        "AND status='proposed' FOR UPDATE", (UUID(claims['sid']), auth.principal.actor_id)).fetchone()
        if not row:
            raise HTTPException(409, 'The archive is no longer awaiting files or approval.')
        p = row['payload']
        if not p.get('transfer_inventory') or not 0 <= index < len(p['files']):
            raise HTTPException(404, 'Original not found in this transfer.')
        item = p['files'][claims['index']]
        if p.get('reviews', {}).get(str(index)) == 'rejected':
            raise HTTPException(409, 'This original was rejected. Prepare a new proposal to upload it.')
        if len(data) != item['size_bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise HTTPException(422, 'The original does not match its declared size and SHA-256 checksum.')
        duplicate = item['received'] and original_intact(library, item)
        if not duplicate:
            library.store.put(item['object_key'], data)
            item['received'] = True
            c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(p), UUID(claims['sid'])))
            library._audit(c, auth, 'archive.original_received', UUID(claims['sid']))
        missing = [f['name'] for f in p['files'] if not f['received']]
        return {'id': claims['sid'], 'status': 'awaiting_transfer' if missing else 'proposed',
                'received': item['name'], 'duplicate': duplicate, 'remaining': missing,
                'instruction': 'Human approval is required before these files enter shared retrieval.'}


def queue_cleanup(c, auth, sid, p):
    c.execute('DELETE FROM central_brain.transfer_reservations WHERE id=%s', (sid,))
    c.execute("INSERT INTO central_brain.library_deletions "
              "(id,workspace_id,created_by,visibility,sensitivity,name,path,kind,original_status,deleted_by,object_keys) "
              "VALUES(%s,%s,%s,'private','internal',%s,%s,'transfer','proposed',%s,%s) ON CONFLICT DO NOTHING",
              (sid, auth.principal.workspace_id, auth.principal.actor_id, p['name'], p['folder_path'],
               auth.principal.actor_id, Jsonb([key for f in p['files'] for key in
                   [f['object_key']] + ([f['direct_upload_key']] if f.get('direct_upload_key') else []) + [f['object_key'] + '_chunks/' + str(i)
                    for i in range(len(f.get('chunks', [])) + 1)]])))


def expire_incomplete(library, auth):
    with library.repo._connection(auth) as c:
        library._lock(c, auth)
        rows = c.execute("SELECT id,payload FROM central_brain.library_suggestions WHERE status='proposed' "
                         "AND created_by=%s AND (payload->>'transfer_expires')::bigint < %s FOR UPDATE",
                         (auth.principal.actor_id, int(time.time()))).fetchall()
        for row in rows:
            p = row['payload']
            # Expiration limits credentials, not the user's received files or transfer progress.
            # Empty abandoned inventories can still release their reserved storage.
            if any(f['received'] or f.get('chunks') for f in p['files']):
                continue
            queue_cleanup(c, auth, row['id'], p)
            c.execute("UPDATE central_brain.library_suggestions SET status='reject',payload=%s WHERE id=%s",
                      (Jsonb({'name': p['name'], 'folder_path': p['folder_path']}), row['id']))
