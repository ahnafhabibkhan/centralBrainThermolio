"""Independent review of originals and generated archive documents."""
import hashlib
from pathlib import PurePosixPath
from fastapi import HTTPException
from psycopg.types.json import Jsonb


def entries(p):
    return [entry(p, i) for i in [-1, -2, *range(len(p['files']))]]


def entry(p, index):
    # Decode only the selected original. Decoding a whole project for each approval
    # made bulk review repeatedly process the same large binary files.
    if index in (-1, -2):
        name = p.get('summary_filename', 'summary.md') if index == -1 else p.get('source_filename', 'source.md')
        data = p['summary'].encode() if index == -1 else (
            '# Conversation source\n\n' + p['source_reference'] +
            '\n\n## Requested originals\n\nThis inventory does not confirm delivery or approval.\n\n' +
            '\n'.join('- ' + f['name'] for f in p['files'])).encode()
        return {'index': index, 'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                'ready': True, 'data': data, 'state': p.get('reviews', {}).get(str(index), 'pending')}
    if not isinstance(index, int) or not 0 <= index < len(p['files']):
        raise HTTPException(404, 'File proposal not found.')
    from .archives import ArchiveFile
    f = p['files'][index]
    data = None if p.get('transfer_inventory') else ArchiveFile.model_validate(f).data()
    return {'index': index, 'name': f['name'], 'size': f['size_bytes'] if data is None else len(data),
            'sha256': f['sha256'] if data is None else hashlib.sha256(data).hexdigest(),
            'ready': f.get('received', True), 'data': data,
            'state': p.get('reviews', {}).get(str(index), 'pending')}


def review(library, auth, sid, index, approve, c, stored_keys):
    from .archives import folder_parts
    from .project_transfer import queue_cleanup
    auth.require('reviewer')
    row = c.execute("SELECT payload FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s "
                    "AND action='archive' AND status='proposed' FOR UPDATE", (sid, auth.principal.actor_id)).fetchone()
    if not row:
        raise HTTPException(409, 'This archive is no longer awaiting review.')
    p = row['payload']
    item = entry(p, index)
    if item['state'] != 'pending':
        raise HTTPException(409, 'This file was already reviewed. Refresh the queue.')
    if approve and not item['ready']:
        raise HTTPException(409, 'The original bytes have not arrived. Upload this file before approving it.')
    if index >= 0 and p.get('transfer_inventory'):
        p['reserved_bytes'] = max(0, p.get('reserved_bytes', 0) - item['size'])
        c.execute('DELETE FROM central_brain.transfer_reservations WHERE id=%s', (sid,))
        if p['reserved_bytes']:
            c.execute('INSERT INTO central_brain.transfer_reservations(id,workspace_id,created_by,size_bytes) VALUES(%s,%s,%s,%s)',
                      (sid, auth.principal.workspace_id, auth.principal.actor_id, p['reserved_bytes']))
    if approve:
        from uuid import UUID
        for ancestor in p.get('destination_ancestors', []):
            library._get(c, auth, UUID(ancestor))
        parent = None
        for part in folder_parts(p['folder_path']) + folder_parts(item['name'])[:-1]:
            existing = c.execute("SELECT id,kind,visibility FROM central_brain.library_nodes WHERE parent_id IS NOT DISTINCT FROM %s "
                                 "AND lower(name)=lower(%s) AND status='active'", (parent, part)).fetchone()
            if existing:
                library._get(c, auth, existing['id'])
                if existing['kind'] != 'folder' or existing['visibility'] != 'workspace':
                    raise HTTPException(409, 'The destination conflicts with an existing item.')
                parent = existing['id']
            else:
                parent = library.folder(auth, part, parent, connection=c)
        data = item['data']
        if data is None:
            with library.store.get(p['files'][index]['object_key']) as stream:
                data = stream.read(library.settings.library_file_bytes + 1)
        if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise HTTPException(409, 'This original failed verification. Upload the correct file again.')
        name = PurePosixPath(item['name']).name
        existing = c.execute("SELECT id,kind,visibility FROM central_brain.library_nodes WHERE parent_id IS NOT DISTINCT FROM %s "
                             "AND lower(name)=lower(%s) AND status='active'", (parent, name)).fetchone()
        if existing:
            library._get(c, auth, existing['id'])
            version = c.execute('SELECT sha256,size FROM central_brain.library_versions WHERE node_id=%s ORDER BY version DESC LIMIT 1',
                                (existing['id'],)).fetchone()
            if (existing['kind'] != 'file' or existing['visibility'] != 'workspace' or not version
                    or version['sha256'] != item['sha256'] or version['size'] != item['size']):
                raise HTTPException(409, f'{name} already exists with different contents. Review or rename this proposal before approving it.')
        else:
            library.upload(auth, name, data, parent, connection=c, stored_keys=stored_keys)
    p.setdefault('reviews', {})[str(index)] = 'approved' if approve else 'rejected'
    status = 'proposed'
    if all(p['reviews'].get(str(i), 'pending') != 'pending' for i in [-1, -2, *range(len(p['files']))]):
        if p.get('transfer_inventory'):
            queue_cleanup(c, auth, sid, p)
        status = 'approve' if any(v == 'approved' for v in p['reviews'].values()) else 'reject'
        p = {k: p[k] for k in ('name', 'folder_path', 'fingerprint', 'reviews') if k in p}
    c.execute('UPDATE central_brain.library_suggestions SET payload=%s,status=%s WHERE id=%s', (Jsonb(p), status, sid))
    library._audit(c, auth, 'archive.file_approved' if approve else 'archive.file_rejected', sid)
