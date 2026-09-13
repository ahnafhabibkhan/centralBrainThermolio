"""Scoped S3 uploads keep original bytes outside chat tool arguments."""
import base64
import time

from fastapi import HTTPException
from psycopg.types.json import Jsonb


def prepare(library, auth, archive_id, file_index):
    auth.require('writer')
    if not library.settings.library_bucket:
        raise HTTPException(409, 'Direct S3 uploads are unavailable in this environment.')
    with library.repo._connection(auth) as c:
        library._lock(c, auth)
        row = c.execute("SELECT payload FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s "
                        "AND status='proposed' FOR UPDATE", (archive_id, auth.principal.actor_id)).fetchone()
        if not row or not row['payload'].get('transfer_inventory'):
            raise HTTPException(404, 'Pending project transfer not found.')
        p = row['payload']
        if not 0 <= file_index < len(p['files']):
            raise HTTPException(404, 'Original not found.')
        f = p['files'][file_index]
        if f['received']:
            return {'received': True, 'name': f['name']}
        expiry = min(600, p['transfer_expires'] - int(time.time()))
        if expiry <= 0:
            raise HTTPException(409, 'This transfer expired. Prepare a new archive.')
        key = 'files/transfers/' + f['object_key'].rsplit('/', 1)[-1]
        checksum = base64.b64encode(bytes.fromhex(f['sha256'])).decode()
        fields = {'x-amz-server-side-encryption': 'AES256', 'x-amz-checksum-algorithm': 'SHA256',
                  'x-amz-checksum-sha256': checksum}
        receipt = library.store.client().generate_presigned_post(
            Bucket=library.settings.library_bucket, Key=key, Fields=fields,
            Conditions=[{k: v} for k, v in fields.items()] +
                       [['content-length-range', f['size_bytes'], f['size_bytes']]], ExpiresIn=expiry)
        f['direct_upload_key'] = key
        c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(p), archive_id))
        return {'name': f['name'], 'received': False, 'method': 'POST', 'url': receipt['url'],
                'fields': receipt['fields'], 'expires_in_seconds': expiry,
                'instruction': 'From code, POST multipart form data to url using every returned field unchanged, '
                               'with the original binary file as the last field named file. Do not send an '
                               'Authorization header. Do not encode the file as base64. Keep signed fields private. '
                               'After S3 returns HTTP 204, call complete_original_upload with this archive ID and '
                               'file index. If this host also blocks S3, report the restriction and use the browser '
                               'completion page. Do not keep retrying or transcribe binary chunks.'}


def complete(library, auth, archive_id, file_index):
    auth.require('writer')
    with library.repo._connection(auth) as c:
        row = c.execute("SELECT payload FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s "
                        "AND status='proposed'", (archive_id, auth.principal.actor_id)).fetchone()
        if not row or not row['payload'].get('transfer_inventory'):
            raise HTTPException(404, 'Pending project transfer not found.')
        p = row['payload']
        if not 0 <= file_index < len(p['files']):
            raise HTTPException(404, 'Original not found.')
        f = p['files'][file_index]
        if f['received']:
            return {'received': True, 'duplicate': True, 'name': f['name']}
        if int(time.time()) >= p['transfer_expires']:
            raise HTTPException(409, 'The incomplete transfer expired.')
        if not f.get('direct_upload_key'):
            raise HTTPException(409, 'Prepare a direct original upload first.')
    from botocore.exceptions import ClientError
    try:
        with library.store.get(f['direct_upload_key']) as stream:
            data = stream.read(f['size_bytes'] + 1)
    except (FileNotFoundError, ClientError) as exc:
        if isinstance(exc, ClientError) and exc.response['Error']['Code'] not in {'NoSuchKey', '404'}:
            raise
        raise HTTPException(409, 'The original has not reached S3. Complete its upload first.') from exc
    from .project_transfer import store_original
    return store_original(library, auth, archive_id, file_index, data)
