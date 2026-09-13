"""Bounded, resumable original transfers through the authenticated MCP connection."""
import base64
import hashlib
import time
from uuid import UUID

from fastapi import HTTPException
from psycopg.types.json import Jsonb

CHUNK_BYTES = 128 * 1024


def status(library, auth, archive_id):
    auth.require('writer')
    with library.repo._connection(auth) as c:
        row = c.execute("SELECT payload,status FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s",
                        (archive_id, auth.principal.actor_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Transfer not found.')
        p = row['payload']
        files = [{'index': i, 'name': f['name'], 'size_bytes': f['size_bytes'],
                  'received': f['received'], 'next_offset': min(len(f.get('chunks', [])) * CHUNK_BYTES, f['size_bytes'])}
                 for i, f in enumerate(p.get('files', [])) if 'received' in f]
        ready = bool(files) and all(f['received'] for f in files)
        return {'archive_id': str(archive_id), 'status': row['status'], 'ready_for_approval': ready,
                'files': files, 'chunk_bytes': CHUNK_BYTES,
                'completion_url': library.settings.public_url + '/library/suggestions/' + str(archive_id)}


def upload_chunk(library, auth, archive_id, index, offset, content):
    auth.require('writer')
    if len(content) > 174764:
        raise HTTPException(413, 'Send at most 128 KiB of original bytes in each chunk.')
    try:
        data = base64.b64decode(content, validate=True)
    except ValueError as exc:
        raise HTTPException(422, 'The chunk is not valid base64.') from exc
    if not data or len(data) > CHUNK_BYTES or offset < 0 or offset % CHUNK_BYTES:
        raise HTTPException(422, 'Use nonempty chunks aligned to 128 KiB offsets.')
    with library.repo._connection(auth) as c:
        library._lock(c, auth)
        row = c.execute("SELECT payload FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s "
                        "AND status='proposed' FOR UPDATE", (archive_id, auth.principal.actor_id)).fetchone()
        if not row or not row['payload'].get('transfer_inventory'):
            raise HTTPException(404, 'Pending project transfer not found.')
        p = row['payload']
        if not 0 <= index < len(p['files']):
            raise HTTPException(404, 'Original not found.')
        f = p['files'][index]
        if p.get('reviews', {}).get(str(index)) == 'rejected':
            raise HTTPException(409, 'This original was rejected.')
        if time.time() >= p['transfer_expires'] and not f['received']:
            raise HTTPException(409, 'The incomplete transfer expired.')
        if len(data) != min(CHUNK_BYTES, f['size_bytes'] - offset):
            raise HTTPException(422, 'Chunk size does not match the declared original.')
        chunks = f.setdefault('chunks', [])
        part = offset // CHUNK_BYTES
        digest = hashlib.sha256(data).hexdigest()
        if part < len(chunks):
            if chunks[part] != digest:
                raise HTTPException(409, 'A different chunk already occupies this offset.')
        elif part == len(chunks) and not f['received']:
            library.store.put(f['object_key'] + '_chunks/' + str(part), data)
            chunks.append(digest)
        else:
            raise HTTPException(409, 'Resume at the next_offset returned by get_chat_archive_upload_status.')
        next_offset = min(len(chunks) * CHUNK_BYTES, f['size_bytes'])
        if next_offset == f['size_bytes'] and not f['received']:
            combined = bytearray()
            for number, expected in enumerate(chunks):
                with library.store.get(f['object_key'] + '_chunks/' + str(number)) as stream:
                    raw = stream.read(CHUNK_BYTES + 1)
                if len(raw) > CHUNK_BYTES or hashlib.sha256(raw).hexdigest() != expected:
                    raise HTTPException(409, 'A stored chunk failed verification. Retry the transfer.')
                combined.extend(raw)
            if len(combined) != f['size_bytes'] or hashlib.sha256(combined).hexdigest() != f['sha256']:
                raise HTTPException(422, 'The complete file does not match the original SHA-256 checksum.')
            library.store.put(f['object_key'], bytes(combined))
            f['received'] = True
            library._audit(c, auth, 'archive.original_received', archive_id)
        c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(p), archive_id))
        return {'archive_id': str(archive_id), 'file_index': index, 'next_offset': next_offset,
                'file_received': f['received'], 'ready_for_approval': all(f['received'] for f in p['files'])}
