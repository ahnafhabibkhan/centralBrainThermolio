"""Resolve pending originals by their complete, case-insensitive library path."""
from psycopg.types.json import Jsonb


def archive_names(payload):
    return [(-1, payload.get('summary_filename', 'summary.md')),
            (-2, payload.get('source_filename', 'source.md')),
            *enumerate(f['name'] for f in payload.get('files', []))]


def normalized_path(folder, name):
    return '/' + '/'.join(part.strip().lower() for part in (folder + '/' + name).strip('/').split('/'))


def finish_archive(library, auth, c, sid, payload):
    """Retain provenance without retaining inline originals or staging credentials."""
    from .archive_review import entry
    from .project_transfer import queue_cleanup
    reviews = payload.get('reviews', {})
    if any(reviews.get(str(index), 'pending') == 'pending' for index, _ in archive_names(payload)):
        return payload, 'proposed'
    retained = {key: payload[key] for key in (
        'name', 'folder_path', 'fingerprint', 'reviews', 'superseded_by',
        'summary_filename', 'source_filename', 'destination_ancestors') if key in payload}
    retained['files'] = []
    for index, original in enumerate(payload.get('files', [])):
        item = entry(payload, index)
        retained['files'].append({'name': original['name'], 'size_bytes': item['size'],
                                  'sha256': item['sha256'], 'received': item['ready']})
    if payload.get('transfer_inventory'):
        queue_cleanup(c, auth, sid, payload)
    states = set(reviews.values())
    return retained, 'approve' if 'approved' in states else ('superseded' if states == {'superseded'} else 'reject')


def reconcile_pending_uploads(library, auth, c):
    """Supersede older visible uploads while holding the workspace mutation lock."""
    library._lock(c, auth)
    rows = c.execute("SELECT id,created_at,payload,status FROM central_brain.library_suggestions "
                     "WHERE action='archive' AND (status='proposed' OR payload ? 'files') "
                     "ORDER BY created_at,id FOR UPDATE").fetchall()
    candidates = {}
    by_id = {row['id']: row for row in rows}
    for row in rows:
        payload = row['payload']
        for index, name in archive_names(payload):
            state = payload.get('reviews', {}).get(str(index), 'pending')
            if state not in {'pending', 'approved'} or (row['status'] != 'proposed' and state != 'approved'):
                continue
            key = normalized_path(payload['folder_path'], name)
            candidate = (row['created_at'], str(row['id']), index, row['id'], state)
            candidates.setdefault(key, []).append(candidate)
    # A recursive visible tree prevents a hidden ancestor from joining a shared path.
    nodes = c.execute("""WITH RECURSIVE visible AS (
        SELECT id,parent_id,name,kind,status,visibility,created_at FROM central_brain.library_nodes
        WHERE sensitivity=ANY(%s) AND visibility='workspace' AND status IN ('active','proposed')
    ), tree AS (
        SELECT n.*,ARRAY[n.name] AS parts,ARRAY[n.id] AS ancestors FROM visible n WHERE parent_id IS NULL
        UNION ALL SELECT n.*,t.parts||n.name,t.ancestors||n.id FROM visible n JOIN tree t ON n.parent_id=t.id
        WHERE t.kind='folder' AND t.status='active' AND cardinality(t.parts)<32 AND NOT n.id=ANY(t.ancestors)
    ) SELECT t.*,v.created_at AS uploaded_at,v.source_created_at,v.source_upload_id FROM tree t JOIN LATERAL (
        SELECT created_at,source_created_at,source_upload_id FROM central_brain.library_versions WHERE node_id=t.id ORDER BY version DESC LIMIT 1
    ) v ON true WHERE t.kind='file' AND (t.status='proposed' OR v.source_created_at IS NOT NULL)""", (auth.principal.sensitivities,)).fetchall()
    node_ids = {node['id'] for node in nodes}
    for node in nodes:
        if node['visibility'] != 'workspace':
            continue
        key = normalized_path('/', '/'.join(node['parts']))
        candidates.setdefault(key, []).append((node['source_created_at'] or node['uploaded_at'],
                                               str(node['source_upload_id'] or node['id']), 0, node['id'],
                                               'pending' if node['status'] == 'proposed' else 'approved'))
    changed_nodes = set()
    changed = set()
    for values in candidates.values():
        newest = max(values, key=lambda item: item[:3])
        for _, origin, index, sid, state in values:
            if state != 'pending' or origin == newest[1] or (sid, index) == (newest[3], newest[2]):
                continue
            if sid in node_ids:
                c.execute("UPDATE central_brain.library_nodes SET status='rejected' WHERE id=%s", (sid,))
                library._audit(c, auth, 'file.superseded', sid,
                               {'newer_id': str(newest[3]), 'newer_index': newest[2]})
                changed_nodes.add(sid)
                continue
            row = by_id[sid]
            if row['status'] != 'proposed':
                continue
            payload = row['payload']
            payload.setdefault('reviews', {})[str(index)] = 'superseded'
            payload.setdefault('superseded_by', {})[str(index)] = {'id': str(newest[3]), 'index': newest[2],
                                                                     'kind': 'node' if newest[3] in node_ids else 'archive_file'}
            changed.add(sid)
            # Superseded staging bytes stay reserved until the archive cleanup is queued.
            library._audit(c, auth, 'archive.file_superseded', sid,
                           {'index': index, 'newer_archive_id': str(newest[3]), 'newer_index': newest[2]})
    for sid in changed:
        payload = by_id[sid]['payload']
        if payload.get('transfer_inventory'):
            if payload.get('reserved_bytes'):
                c.execute('UPDATE central_brain.transfer_reservations SET size_bytes=%s WHERE id=%s',
                          (payload['reserved_bytes'], sid))
            else:
                c.execute('DELETE FROM central_brain.transfer_reservations WHERE id=%s', (sid,))
        payload, status = finish_archive(library, auth, c, sid, payload)
        c.execute('UPDATE central_brain.library_suggestions SET payload=%s,status=%s WHERE id=%s',
                  (Jsonb(payload), status, sid))
    return len(changed) + len(changed_nodes)
