"""Permission-filtered retrieval with independent copy locations and freshness checks."""

import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder


def bounded(payload, budget):
    """Keep the tool envelope bounded and explicitly mark omitted context and locations."""
    payload = jsonable_encoder(payload)
    payload['truncated'] = False
    def size():
        return len(json.dumps(payload, ensure_ascii=True))
    context = payload.get('context', {})
    for field in ['folders', 'recent_files', 'recent_deletions']:
        while size() > budget and context.get(field):
            context[field].pop()
            context['truncated'] = payload['truncated'] = True
            if field == 'recent_deletions':
                context['deletions_truncated'] = True
    for result in reversed(payload['results']):
        while size() > budget and result['also_in']:
            result['also_in'].pop()
            result['duplicate_locations_truncated'] = payload['truncated'] = True
        if size() > budget:
            result['excerpt'] = result['excerpt'][:250]
            payload['truncated'] = True
    while size() > budget and payload['results']:
        payload['results'].pop()
        payload['truncated'] = True
    if payload['truncated']:
        payload['guidance'] = 'Results are bounded. Narrow the folder or query, and read cited files for details.'
    return payload


def search(library, auth, query, parent=None, limit=8):
    auth.require('reader')
    if not query.strip() or len(query) > 2000:
        raise HTTPException(422, 'Enter a search query of up to 2,000 characters.')
    library.project_memories(auth)
    with library.repo._connection(auth) as c:
        library._parent(c, auth, parent)
        rows = c.execute(
            """WITH RECURSIVE paths AS (
                SELECT id,ARRAY[id] AS ancestors,('/'||name)::text AS path
                FROM central_brain.library_nodes WHERE parent_id IS NULL
                AND status='active' AND sensitivity=ANY(%(levels)s)
                UNION ALL SELECT n.id,p.ancestors||n.id,p.path||'/'||n.name
                FROM central_brain.library_nodes n JOIN paths p ON n.parent_id=p.id
                WHERE n.status='active' AND n.sensitivity=ANY(%(levels)s)
                AND cardinality(p.ancestors)<32 AND NOT n.id=ANY(p.ancestors)
            ), q AS (SELECT websearch_to_tsquery('english',%(query)s) AS term), docs AS (
                SELECT n.id,n.name,p.path,v.version,coalesce(s.ordinal,0) AS ordinal,
                    coalesce(s.location,'File details') AS location,coalesce(s.content,'') AS content,
                    v.sha256||':'||lower(substring(n.name from '[.][^.]+$'))||':'||
                        coalesce(s.ordinal,0)::text||':'||md5(coalesce(s.content,'')) AS duplicate_key,
                    setweight(to_tsvector('english',p.path),'A')||
                        coalesce(s.search_document,''::tsvector) AS document
                FROM central_brain.library_nodes n JOIN paths p ON p.id=n.id
                JOIN LATERAL (SELECT * FROM central_brain.library_versions WHERE node_id=n.id
                    ORDER BY version DESC LIMIT 1) v ON true
                LEFT JOIN central_brain.library_sections s ON s.version_id=v.id
                WHERE n.kind='file' AND (%(parent)s::uuid IS NULL OR %(parent)s=ANY(p.ancestors))
                UNION ALL
                SELECT n.id,n.name,p.path,1,0,'Memory',m.content,n.id::text,
                    m.search_document||setweight(to_tsvector('english',p.path),'A')
                FROM central_brain.library_nodes n JOIN paths p ON p.id=n.id
                JOIN central_brain.memories m ON m.id=n.memory_id
                WHERE m.status='active' AND m.deleted_at IS NULL
                AND (m.expires_at IS NULL OR m.expires_at>now())
                AND m.sensitivity=ANY(%(levels)s)
                AND (%(parent)s::uuid IS NULL OR %(parent)s=ANY(p.ancestors))
            ), matches AS (
                SELECT docs.*,ts_rank_cd(document,q.term)+
                    CASE WHEN strpos(lower(path),lower(%(query)s))>0 THEN 1 ELSE 0 END AS score
                FROM docs,q WHERE document@@q.term OR strpos(lower(path),lower(%(query)s))>0
            ), ranked AS (
                SELECT *,row_number() OVER (PARTITION BY duplicate_key ORDER BY score DESC,path,id) AS position
                FROM matches
            ) SELECT r.id,r.name,r.path,r.version,r.ordinal,r.location,left(r.content,1400) AS excerpt,r.score,
                (SELECT count(*) FROM docs d WHERE d.duplicate_key=r.duplicate_key AND d.id<>r.id) AS duplicate_count,
                coalesce((SELECT jsonb_agg(x) FROM (
                    SELECT d.id,d.path,d.version FROM docs d
                    WHERE d.duplicate_key=r.duplicate_key AND d.id<>r.id ORDER BY d.path,d.id LIMIT 10
                ) x),'[]'::jsonb) AS also_in
            FROM ranked r WHERE position=1 ORDER BY score DESC,path,id,ordinal LIMIT %(limit)s""",
            {'levels': auth.principal.sensitivities, 'query': query, 'parent': parent,
             'limit': min(max(limit, 1), 8)},
        ).fetchall()
    for row in rows:
        row['duplicate_locations_truncated'] = row['duplicate_count'] > len(row['also_in'])
    return {'results': rows, 'reference_material': True, 'duplicate_policy': 'exact_bytes_and_section'}


def task_context(library, auth, query, parent=None, known_revision=None, limit=6):
    """Fetch current context and results, retrying once if the library changes during retrieval."""
    auth.require('reader')
    for _ in range(2):
        before = library.context(auth)
        result = search(library, auth, query, parent, limit)
        after = library.context(auth)
        if before['revision'] == after['revision']:
            changed = known_revision != after['revision']
            return bounded({
                **result, 'context_revision': after['revision'], 'changed': changed,
                'context': after if changed else {'revision': after['revision'], 'counts': after['counts']},
                'refresh_required': False,
                'cache_action': 'replace_previous_results' if changed else 'use_current_results',
                'guidance': 'Cite paths and versions. Changes are pulled on tool calls, not pushed into chats.',
            }, library.settings.max_context_chars)
    return {'results': [], 'context_revision': None, 'changed': True, 'refresh_required': True,
            'cache_action': 'discard_previous_results', 'reference_material': True,
            'guidance': 'The library changed during retrieval. Call again before using results.'}
