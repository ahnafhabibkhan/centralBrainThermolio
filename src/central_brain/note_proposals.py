"""Resolve repeated note proposals without overwriting or duplicating files."""
import hashlib
from fastapi import HTTPException
from .library import filename


def propose_note(library, auth, name, content, source_reference, parent=None):
    data = (content + '\n\nSource: ' + source_reference).encode()
    try:
        node = library.upload(auth, name, data, parent, proposed=True)
        return {'file_id': str(node), 'status': 'proposed'}
    except HTTPException as error:
        if error.status_code != 409:
            raise
        with library.repo._connection(auth) as c:
            # Only disclose a pending proposal to its author. Database policies also apply.
            row = c.execute(
                "SELECT n.id,n.status,v.sha256 FROM central_brain.library_nodes n "
                "JOIN LATERAL (SELECT sha256 FROM central_brain.library_versions WHERE node_id=n.id "
                "ORDER BY version DESC LIMIT 1) v ON true "
                "WHERE n.parent_id IS NOT DISTINCT FROM %s AND lower(n.name)=lower(%s) "
                "AND n.kind='file' AND n.status IN ('active','proposed') "
                "AND n.sensitivity=ANY(%s) AND (n.status='active' OR n.created_by=%s)",
                (parent, filename(name), auth.principal.sensitivities, auth.principal.actor_id),
            ).fetchone()
        if not row:
            raise error
        identical = row['sha256'] == hashlib.sha256(data).hexdigest()
        return {'file_id': str(row['id']), 'status': row['status'], 'created': False,
                'identical_content': identical, 'needs_approval': row['status'] == 'proposed',
                'outcome': 'already_exists' if identical else 'content_conflict',
                'next_action': ('The identical note already exists. Do not upload or rename it again.' if identical
                                else 'The name exists with different content or provenance. Do not overwrite or rename automatically. Review the existing file.'),
                'review_url': library.settings.public_url + '/library#approvals'}
