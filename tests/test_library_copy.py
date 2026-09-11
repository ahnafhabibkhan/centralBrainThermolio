import hashlib
import re
from uuid import UUID

import pytest
from fastapi import HTTPException

from test_pilot import pilot
from test_library import library
from test_archives import selection
from central_brain.archives import approve_batch
from central_brain.auth import AuthContext
from central_brain.library_worker import process_one


def test_copy_preserves_original_and_context_after_source_changes_and_deletion(pilot, library):
    auth = pilot.auth
    source_folder = library.folder(auth, 'Brand')
    target = library.folder(auth, 'Proposal')
    original = b'<svg xmlns="http://www.w3.org/2000/svg"><title>Thermolio boiler</title></svg>'
    source = library.upload(auth, 'logo.svg', original, source_folder)
    assert process_one(library, auth)
    before = library.context(auth)['revision']
    copied = library.copy(auth, source, 'logo.svg', target)
    info = library.info(auth, copied)
    assert info['copied_from'] == {'file_id': str(source), 'version': 1,
                                   'sha256': hashlib.sha256(original).hexdigest()}
    assert info['path'] == '/Proposal/logo.svg' and info['versions'][0]['state'] == 'ready'
    context = library.context(auth)
    assert context['revision'] != before
    assert next(n for n in context['recent_files'] if n['id'] == str(copied))['copied_from'] == info['copied_from']
    assert library.search(auth, 'boiler', target)['results'][0]['id'] == copied
    with library.repo._connection(auth) as c:
        keys = c.execute('SELECT object_key FROM central_brain.library_versions WHERE node_id=ANY(%s)', ([source, copied],)).fetchall()
        assert len({k['object_key'] for k in keys}) == 2
    library.upload(auth, 'logo.svg', b'<svg>Updated source</svg>', node_id=source)
    assert library.info(auth, copied)['versions'][0]['version'] == 1
    plan = library.deletion_plan(auth, source)
    library.delete(auth, source, 'logo.svg', plan['token'])
    with library.download(auth, copied)[1] as stream:
        assert stream.read() == original
    assert any(d['id'] == str(source) for d in library.context(auth)['recent_deletions'])
    assert any(n['id'] == copied for n in library.snapshot(auth))


def test_copy_privacy_conflicts_and_storage_rollback(pilot, library, monkeypatch):
    auth = pilot.auth
    target = library.folder(auth, 'Project')
    source = library.upload(auth, 'Private.txt', b'Private context.', visibility='private')
    copied = library.copy(auth, source, 'Private.txt', target)
    assert library.info(auth, copied)['visibility'] == 'private'
    colleague = AuthContext(pilot.settings.principals()['colleague'])
    with pytest.raises(HTTPException):
        library.info(colleague, copied)
    with pytest.raises(HTTPException):
        library.copy(colleague, source, 'Leaked.txt', target)
    writer = AuthContext(auth.principal.model_copy(update={'roles': {'reader', 'writer'}}))
    with pytest.raises(HTTPException):
        library.copy(writer, source, 'Bypass.txt', target)
    with library.repo._connection(auth) as c:
        c.execute("UPDATE central_brain.library_nodes SET sensitivity='confidential' WHERE id=%s", (source,))
    privileged = AuthContext(auth.principal.model_copy(update={'sensitivities': ['public', 'internal', 'confidential']}))
    confidential = library.copy(privileged, source, 'Confidential.txt', target)
    assert library.info(privileged, confidential)['sensitivity'] == 'confidential'
    with pytest.raises(HTTPException):
        library.info(auth, confidential)
    with library.repo._connection(auth) as c:
        c.execute("UPDATE central_brain.library_nodes SET sensitivity='internal' WHERE id=%s", (source,))
    for name in ['Private.txt', '../Bad.txt', 'Converted.svg']:
        with pytest.raises(HTTPException):
            library.copy(auth, source, name, target)
    before = library.context(auth)['revision']
    original_put = library.store.put
    def fail_after_put(key, data):
        original_put(key, data)
        raise RuntimeError('Simulated failed storage acknowledgement.')
    monkeypatch.setattr(library.store, 'put', fail_after_put)
    with pytest.raises(RuntimeError):
        library.copy(auth, source, 'Failed.txt', target)
    assert library.context(auth)['revision'] == before
    from pathlib import Path
    assert len([p for p in Path(pilot.settings.library_local_path).rglob('*') if p.is_file()]) == 3


def test_agent_copy_proposal_freezes_source_version_and_needs_approval(pilot, library):
    auth = pilot.auth
    target = library.folder(auth, 'Destination')
    source = library.upload(auth, 'Source.txt', b'Version one.')
    writer = AuthContext(auth.principal.model_copy(update={'roles': {'reader', 'writer'}}))
    proposal = library.suggest(writer, 'copy', {'node_id': str(source), 'parent_id': str(target), 'name': 'Copy.txt'})
    assert library.listing(auth, target) == []
    library.upload(auth, 'Source.txt', b'Version two.', node_id=source)
    approve_batch(library, auth, selection(library, auth, [('suggestion', proposal['id'])]))
    copied = library.listing(auth, target)[0]
    with library.download(auth, copied['id'])[1] as stream:
        assert stream.read() == b'Version one.'
    assert copied['copied_from']['version'] == 1


def test_web_copy_uses_reviewed_version_csrf_and_independent_id(pilot, library):
    client = pilot.client
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
    client.post('/login', data={'token': 'owner', 'csrf_token': csrf})
    source = library.upload(pilot.auth, 'Source.txt', b'Original.')
    target = library.folder(pilot.auth, 'Destination')
    html = client.get(f'/library/file/{source}').text
    assert 'Copy to folder' in html
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
    data = {'name': 'Copied.txt', 'parent_id': str(target), 'version': 1,
            'source_sha256': hashlib.sha256(b'Original.').hexdigest(), 'csrf_token': csrf}
    assert client.post(f'/library/file/{source}/copy', data=dict(data, csrf_token='wrong')).status_code == 403
    assert client.post(f'/library/file/{source}/copy', data=dict(data, source_sha256='wrong')).status_code == 409
    response = client.post(f'/library/file/{source}/copy', data=data)
    assert response.status_code == 200 and 'Copy source' in response.text
    copied = UUID(str(response.url).rsplit('/', 1)[1])
    assert copied != source and library.info(pilot.auth, copied)['path'] == '/Destination/Copied.txt'
    assert 'Copied.txt' in client.get('/library').text
