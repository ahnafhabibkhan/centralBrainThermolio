"""Local trial cache. Derived labels never modify authoritative library records."""

import hashlib
import json

CATEGORIES = {'brand', 'finance', 'operations', 'projects', 'people', 'security', 'other'}


def reconcile(documents, previous, generate, model_id, scope):
    """Process changed records in a complete, access-filtered snapshot and drop absent records.

    Callers must supply the entire current snapshot for this scope, not one search page.
    The model receives bounded text only and has no tools or access to the authoritative store.
    """
    if len({d['id'] for d in documents}) != len(documents):
        raise ValueError('Document IDs must be unique.')
    old = previous.get('entries', {}) if previous.get('scope') == scope and previous.get('model') == model_id else {}
    entries, processed, rejected = {}, 0, 0
    for document in documents:
        fingerprint = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        if old.get(document['id'], {}).get('fingerprint') == fingerprint:
            entries[document['id']] = old[document['id']]
            continue
        processed += 1
        raw = generate(document['text'][:2000])
        try:
            result = json.loads(raw)
            if result.get('category') not in CATEGORIES or not isinstance(result.get('evidence'), str):
                raise ValueError('Invalid derived label.')
            quote = result['evidence']
            if not 3 <= len(quote) <= 240 or quote not in document['text'][:2000]:
                raise ValueError('Evidence must be an exact source excerpt.')
            label = {'category': result['category'], 'evidence': quote}
        except (ValueError, TypeError, AttributeError):
            rejected += 1
            label = {'category': 'other', 'evidence': '', 'needs_review': True}
        entries[document['id']] = {'fingerprint': fingerprint, 'version': document['version'],
                                   'derived': label, 'reference_material': True}
    return {'scope': scope, 'model': model_id, 'entries': entries}, {
        'processed': processed, 'reused': len(documents) - processed,
        'removed': len(set(old) - set(entries)), 'rejected': rejected,
    }
