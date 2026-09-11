import base64
import json
import re
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from test_pilot import pilot, memory
from test_library import library
from central_brain.archives import ArchiveFile, approval_item, approve_batch, find_folders, propose_archive
from central_brain.auth import AuthContext
from central_brain.library_worker import process_one

SVG = '<svg xmlns="http://www.w3.org/2000/svg"><title>Thermolio test logo</title><path d="M0 0h10v10z"/></svg>\n'


def selection(lib, auth, entries):
    with lib.repo._connection(auth) as c:
        return [approval_item(lib, auth, c, kind, uid) for kind, uid in entries]


def test_archive_originals_folder_context_and_duplicate(pilot, library):
    assistant = AuthContext(pilot.auth.principal.model_copy(update={'roles': {'reader', 'writer'}}))
    before = library.context(pilot.auth)['revision']
    png = b'\x89PNG\r\n\x1a\noriginal-test-bytes'
    files = [ArchiveFile(name='logo.svg', content=SVG),
             ArchiveFile(name='logo.png', content=base64.b64encode(png).decode(), encoding='base64')]
    proposal = propose_archive(library, assistant, 'Brand/Thermolio/Logo', 'Use the approved logo.', 'Test conversation', files)
    assert proposal['status'] == 'proposed'
    assert not library.snapshot(assistant)
    assert library.context(assistant)['revision'] == before
    assert propose_archive(library, assistant, 'Brand/Thermolio/Logo', 'Use the approved logo.', 'Test conversation', files)['duplicate']
    items = selection(library, pilot.auth, [('suggestion', proposal['id'])])
    with pytest.raises(HTTPException) as error:
        approve_batch(library, assistant, items)
    assert error.value.status_code == 403
    approve_batch(library, pilot.auth, items)
    nodes = library.snapshot(assistant)
    assert {n['path'] for n in nodes} == {'/Brand', '/Brand/Thermolio', '/Brand/Thermolio/Logo',
        '/Brand/Thermolio/Logo/summary.md', '/Brand/Thermolio/Logo/source.md',
        '/Brand/Thermolio/Logo/logo.svg', '/Brand/Thermolio/Logo/logo.png'}
    for name, data in [('logo.svg', SVG.encode()), ('logo.png', png)]:
        node = next(n for n in nodes if n['name'] == name)
        with library.download(pilot.auth, node['id'])[1] as stream:
            assert stream.read() == data
    while process_one(library, pilot.auth):
        pass
    assert library.context(assistant)['revision'] != before
    assert any(hit['name'] == 'logo.svg' for hit in library.search(assistant, 'Thermolio')['results'])
    assert find_folders(library, assistant, 'logo assets')['matches']
    reuse = propose_archive(library, assistant, 'Brand/Thermolio/Another chat', 'Other chat.', 'Source', [])
    assert reuse['status'] == 'needs_destination_confirmation' and not reuse['stored']
    next_proposal = propose_archive(library, assistant, 'Brand/Thermolio/Another chat', 'Other chat.', 'Source', [], True)
    approve_batch(library, pilot.auth, selection(library, pilot.auth, [('suggestion', next_proposal['id'])]))
    assert len([n for n in library.snapshot(assistant) if n['path'] == '/Brand/Thermolio']) == 1
    addition = propose_archive(library, assistant, 'Brand/Thermolio/Logo', 'A later conversation.',
                               'Second source', [], True, 'follow-up.md')
    approve_batch(library, pilot.auth, selection(library, pilot.auth, [('suggestion', addition['id'])]))
    assert '/Brand/Thermolio/Logo/follow-up.md' in [n['path'] for n in library.snapshot(assistant)]
    with library.repo._connection(pilot.auth) as c:
        payload = c.execute('SELECT payload FROM central_brain.library_suggestions WHERE id=%s', (proposal['id'],)).fetchone()['payload']
    assert 'files' not in payload and 'summary' not in payload


def test_bulk_approves_only_shown_items_and_rejects_stale_batch(pilot, library):
    first = pilot.repo.create(pilot.auth, memory())
    file = library.upload(pilot.auth, 'Pending.txt', b'Pending original.', proposed=True)
    items = selection(library, pilot.auth, [('node', first.memory_id), ('node', file)])
    later = pilot.repo.create(pilot.auth, memory().model_copy(update={'content':'A later proposal.'}))
    pilot.repo.edit_proposal(pilot.auth, first.memory_id, memory().model_copy(update={'content':'Changed since shown.'}))
    with pytest.raises(HTTPException) as error:
        approve_batch(library, pilot.auth, items)
    assert error.value.status_code == 409
    assert library.info(pilot.auth, file, True)['status'] == 'proposed'
    items = selection(library, pilot.auth, [('node', first.memory_id), ('node', file)])
    assert approve_batch(library, pilot.auth, items) == 2
    assert pilot.repo.get(pilot.auth, later.memory_id).status == 'proposed'
    assert pilot.repo.get(pilot.auth, first.memory_id).status == 'active'


def test_archive_and_bulk_rollback_after_storage_failure(pilot, library, monkeypatch):
    memory_id = pilot.repo.create(pilot.auth, memory()).memory_id
    proposal = propose_archive(library, pilot.auth, 'Failure test', 'Summary.', 'Source', [ArchiveFile(name='logo.svg', content=SVG)])
    items = selection(library, pilot.auth, [('node', memory_id), ('suggestion', proposal['id'])])
    original_put = library.store.put
    attempts = []
    def failing_put(key, data):
        attempts.append(key)
        if len(attempts) == 2:
            raise RuntimeError('Synthetic storage failure')
        original_put(key, data)
    monkeypatch.setattr(library.store, 'put', failing_put)
    with pytest.raises(RuntimeError):
        approve_batch(library, pilot.auth, items)
    assert pilot.repo.get(pilot.auth, memory_id).status == 'proposed'
    assert not [n for n in library.snapshot(pilot.auth, True) if n['kind'] == 'folder']
    assert library.usage(pilot.auth)['used'] == 0
    from pathlib import Path
    assert not [p for p in Path(pilot.settings.library_local_path).rglob('*') if p.is_file()]


def test_archive_limits_privacy_conflicts_and_web_batch(pilot, library):
    with pytest.raises(HTTPException):
        propose_archive(library, pilot.auth, '../Escape', 'Summary', 'Source', [])
    with pytest.raises(HTTPException):
        propose_archive(library, pilot.auth, 'Assets', 'Summary', 'Source', [ArchiveFile(name='a.svg', content='bad base64!', encoding='base64')])
    with pytest.raises(HTTPException):
        propose_archive(library, pilot.auth, 'Assets', 'Summary', 'Source', [ArchiveFile(name='summary.md', content='Duplicate')])
    private_auth = AuthContext(pilot.auth.principal.model_copy(update={'actor_id': uuid4()}))
    proposal = propose_archive(library, pilot.auth, 'Assets', 'Summary <script>bad()</script>', 'Source', [ArchiveFile(name='logo.svg', content=SVG)])
    with pytest.raises(HTTPException):
        selection(library, private_auth, [('suggestion', proposal['id'])])
    client = pilot.client
    login_csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
    assert client.post('/login', data={'token':'owner','csrf_token':login_csrf}, follow_redirects=False).status_code == 303
    page = client.get('/library').text
    import html
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    items = html.unescape(re.search(r'name="items" value="([^"]+)"', page)[1])
    preview = client.get(f"/library/suggestions/{proposal['id']}")
    assert '&lt;script&gt;bad()&lt;/script&gt;' in preview.text
    assert 'logo.svg' in preview.text
    assert client.post('/library/approve-all', data={'items':items,'csrf_token':'bad'}).status_code == 403
    result = client.post('/library/approve-all', data={'items':items,'csrf_token':csrf})
    assert result.status_code == 200
    assert 'No approvals are waiting' in result.text
    assert '/Assets/logo.svg' in [n['path'] for n in library.snapshot(pilot.auth)]
    second = propose_archive(library, pilot.auth, 'Assets', 'Other summary', 'Source', [], True)
    with pytest.raises(HTTPException) as error:
        approve_batch(library, pilot.auth, selection(library, pilot.auth, [('suggestion', second['id'])]))
    assert error.value.status_code == 409
