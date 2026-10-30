import hashlib
import json
import re
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from test_pilot import pilot
from test_library import library
from test_project_transfer import prepare, send
from central_brain.archives import approval_item, approve_batch, review_suggestion
from central_brain.auth import AuthContext
from central_brain.connector_upload import status
from central_brain.project_transfer import store_original


def colleague(pilot, **changes):
    return AuthContext(pilot.settings.principals()['colleague'].model_copy(update=changes))


def login(pilot, token):
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', pilot.client.get('/login').text)[1]
    pilot.client.post('/login', data={'token': token, 'csrf_token': csrf})
    return pilot.client.get('/library')


def test_another_reviewer_in_same_workspace_can_preview_and_partially_review(pilot, library):
    originals = {'Files/received.txt': b'Exact original.', 'Files/missing.txt': b'Later original.'}
    receipt = prepare(pilot, library, originals)
    sid = receipt['id']
    assert send(pilot, receipt['uploads'][0], originals['Files/received.txt']).status_code == 200
    reviewer = colleague(pilot)
    page = login(pilot, 'colleague')
    assert page.status_code == 200
    assert 'Files/received.txt' in page.text and 'Files/missing.txt' in page.text
    assert pilot.client.get('/library/context').json()['counts']['pending'] == 1
    download = pilot.client.get(f'/library/suggestions/{sid}/files/0/download')
    assert download.content == originals['Files/received.txt']
    assert hashlib.sha256(download.content).digest() == hashlib.sha256(originals['Files/received.txt']).digest()
    assert pilot.client.get(f'/library/suggestions/{sid}/files/1/download').status_code == 409
    with pytest.raises(HTTPException) as denied:
        status(library, reviewer, sid)
    assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        store_original(library, reviewer, sid, 1, originals['Files/missing.txt'])
    assert denied.value.status_code == 409
    items = page.context['approval_items']
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    response = pilot.client.post('/library/approve-all',
        data={'items': json.dumps(items), 'csrf_token': csrf}, headers={'Accept': 'application/json'})
    assert response.json() == {'approved': 3}
    with pilot.repo._connection(pilot.auth) as c:
        reservation = c.execute('SELECT created_by,size_bytes FROM central_brain.transfer_reservations WHERE id=%s',
                                (sid,)).fetchone()
        assert reservation == {'created_by': pilot.auth.principal.actor_id,
                               'size_bytes': len(originals['Files/missing.txt'])}
        assert c.execute('SELECT created_by FROM central_brain.library_suggestions WHERE id=%s',
                         (sid,)).fetchone()['created_by'] == pilot.auth.principal.actor_id
    assert status(library, pilot.auth, sid)['files'][0]['review_state'] == 'approved'
    assert send(pilot, receipt['uploads'][1], originals['Files/missing.txt']).status_code == 200
    with library.repo._connection(reviewer) as c:
        fingerprint = approval_item(library, reviewer, c, 'archive_file', sid, 1)['fingerprint']
    response = pilot.client.post(f'/library/suggestions/{sid}/files/1/review',
        data={'action': 'reject', 'csrf_token': csrf, 'fingerprint': fingerprint},
        headers={'Accept': 'application/json'})
    assert response.json() == {'status': 'rejected'}
    with pilot.repo._connection(pilot.auth) as c:
        assert not c.execute('SELECT id FROM central_brain.transfer_reservations WHERE id=%s', (sid,)).fetchone()
        events = c.execute("SELECT actor_id FROM central_brain.audit_events WHERE resource_id=%s "
                           "AND action IN ('archive.file_approved','archive.file_rejected')", (sid,)).fetchall()
        assert len(events) == 4
        assert all(event['actor_id'] == reviewer.principal.actor_id for event in events)
    paths = {node['path'] for node in library.snapshot(pilot.auth)}
    assert '/Projects/Upload test/Files/received.txt' in paths
    assert '/Projects/Upload test/Files/missing.txt' not in paths


@pytest.mark.parametrize('approve', [True, False])
def test_another_reviewer_can_finish_whole_archive_without_leaking_reservation(pilot, library, approve):
    receipt = prepare(pilot, library, {'Files/original.txt': b'Exact bytes.'})
    assert send(pilot, receipt['uploads'][0], b'Exact bytes.').status_code == 200
    reviewer = colleague(pilot)
    with pilot.repo._connection(reviewer) as c:
        review_suggestion(library, reviewer, receipt['id'], approve, c, [])
    with pilot.repo._connection(pilot.auth) as c:
        row = c.execute('SELECT created_by,status FROM central_brain.library_suggestions WHERE id=%s',
                        (receipt['id'],)).fetchone()
        assert row == {'created_by': pilot.auth.principal.actor_id, 'status': 'approve' if approve else 'reject'}
        assert not c.execute('SELECT id FROM central_brain.transfer_reservations WHERE id=%s',
                             (receipt['id'],)).fetchone()


@pytest.mark.parametrize('identity', ['writer', 'public_reviewer', 'other_workspace'])
def test_archive_review_does_not_cross_role_sensitivity_or_workspace_boundaries(pilot, library, identity):
    receipt = prepare(pilot, library, {'Files/original.txt': b'Exact bytes.'})
    sid = receipt['id']
    if identity == 'writer':
        auth = colleague(pilot, roles={'reader', 'writer'})
    elif identity == 'public_reviewer':
        auth = colleague(pilot, sensitivities=['public'])
    else:
        auth = AuthContext(pilot.settings.principals()['outsider'])
    with pilot.repo._connection(auth) as c:
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s', (sid,)).fetchone()
        assert c.execute("UPDATE central_brain.library_suggestions SET status='reject' WHERE id=%s",
                         (sid,)).rowcount == 0
        assert c.execute('UPDATE central_brain.transfer_reservations SET size_bytes=1 WHERE id=%s',
                         (sid,)).rowcount == 0
        assert c.execute('DELETE FROM central_brain.transfer_reservations WHERE id=%s', (sid,)).rowcount == 0
        with pytest.raises(HTTPException):
            approval_item(library, auth, c, 'archive_file', sid, -1)
    with pytest.raises(HTTPException):
        approve_batch(library, auth, [{'kind': 'archive_file', 'id': str(sid), 'index': -1, 'fingerprint': 'x'}])
    if identity != 'writer':
        assert library.context(auth, True)['counts']['pending'] == 0
    with pilot.repo._connection(pilot.auth) as c:
        assert c.execute("SELECT status FROM central_brain.library_suggestions WHERE id=%s", (sid,)).fetchone()['status'] == 'proposed'


def test_other_suggestions_and_author_only_inserts_remain_private(pilot, library):
    folder = library.folder(pilot.auth, 'Private plans', visibility='private')
    suggestion = library.suggest(pilot.auth, 'folder', {'name': 'Private child', 'parent_id': str(folder)})
    reviewer = colleague(pilot)
    with pilot.repo._connection(reviewer) as c:
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s',
                             (suggestion['id'],)).fetchone()
        assert c.execute("UPDATE central_brain.library_suggestions SET status='reject' WHERE id=%s",
                         (suggestion['id'],)).rowcount == 0
    assert 'Private child' not in login(pilot, 'colleague').text
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pilot.repo._connection(reviewer) as c:
            c.execute("INSERT INTO central_brain.library_suggestions(id,workspace_id,created_by,action,payload) "
                      "VALUES(%s,%s,%s,'archive',%s)",
                      (uuid4(), reviewer.principal.workspace_id, pilot.auth.principal.actor_id, Jsonb({})))


@pytest.mark.parametrize('restricted', ['private', 'sensitivity'])
def test_archive_preview_requires_access_to_destination_ancestors(pilot, library, restricted):
    folder = library.folder(pilot.auth, 'Projects', visibility='private' if restricted == 'private' else 'workspace')
    receipt = prepare(pilot, library, {'Files/original.txt': b'Exact bytes.'}, destination_confirmed=True)
    if restricted == 'sensitivity':
        with pilot.repo._connection(pilot.auth) as c:
            c.execute("UPDATE central_brain.library_nodes SET sensitivity='confidential' WHERE id=%s", (folder,))
    reviewer = colleague(pilot)
    with pilot.repo._connection(reviewer) as c:
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s',
                             (receipt['id'],)).fetchone()
    page = login(pilot, 'colleague')
    assert 'Files/original.txt' not in page.text
    assert pilot.client.get(f"/library/suggestions/{receipt['id']}/files/-1/download").status_code == 404
    assert library.context(reviewer, True)['counts']['pending'] == 0


@pytest.mark.parametrize('restricted', ['private', 'sensitivity'])
def test_actual_destination_blocks_legacy_archives_with_empty_ranked_ancestors(pilot, library, restricted):
    folder = library.folder(pilot.auth, 'Projects', visibility='private' if restricted == 'private' else 'workspace')
    for number in range(21):
        library.folder(pilot.auth, f'Projects Upload test example {number}')
    data = b'Private project original.'
    receipt = prepare(pilot, library, {'Files/original.txt': data}, destination_confirmed=True)
    assert send(pilot, receipt['uploads'][0], data).status_code == 200
    with pilot.repo._connection(pilot.auth) as c:
        payload = c.execute('SELECT payload FROM central_brain.library_suggestions WHERE id=%s',
                            (receipt['id'],)).fetchone()['payload']
        assert str(folder) in payload['destination_ancestors']
        assert not any(item['id'] == str(folder) for item in payload['existing_folders'])
        payload['destination_ancestors'] = []
        c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s',
                  (Jsonb(payload), receipt['id']))
        if restricted == 'sensitivity':
            c.execute("UPDATE central_brain.library_nodes SET sensitivity='confidential' WHERE id=%s", (folder,))
    reviewer = colleague(pilot)
    with pilot.repo._connection(reviewer) as c:
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s',
                             (receipt['id'],)).fetchone()
        assert c.execute('SELECT central_brain.archive_path_reviewable(%s,%s) AS allowed',
                         (reviewer.principal.workspace_id, Jsonb(payload))).fetchone()['allowed'] is False
        if restricted == 'private':
            assert not c.execute('SELECT id FROM central_brain.library_nodes WHERE id=%s', (folder,)).fetchone()
        assert not c.execute("SELECT pg_has_role(current_user,'central_brain_owner','USAGE') AS owner").fetchone()['owner']
    assert 'Files/original.txt' not in login(pilot, 'colleague').text
    assert pilot.client.get(f"/library/suggestions/{receipt['id']}/files/0/download").status_code == 404
    assert library.context(reviewer, True)['counts']['pending'] == 0


@pytest.mark.parametrize('restricted', ['private', 'sensitivity'])
def test_recorded_destination_moved_below_restricted_parent_stays_hidden(pilot, library, restricted):
    folder = library.folder(pilot.auth, 'Projects')
    receipt = prepare(pilot, library, {'Files/original.txt': b'Exact bytes.'}, destination_confirmed=True)
    parent = library.folder(pilot.auth, 'Hidden parent', visibility='private' if restricted == 'private' else 'workspace')
    library.move(pilot.auth, folder, 'Projects', parent)
    if restricted == 'sensitivity':
        with pilot.repo._connection(pilot.auth) as c:
            c.execute("UPDATE central_brain.library_nodes SET sensitivity='confidential' WHERE id=%s", (parent,))
    reviewer = colleague(pilot)
    with pilot.repo._connection(reviewer) as c:
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s',
                             (receipt['id'],)).fetchone()
    assert 'Files/original.txt' not in login(pilot, 'colleague').text
    assert pilot.client.get(f"/library/suggestions/{receipt['id']}/files/-1/download").status_code == 404


def test_original_subfolder_must_also_be_shared_and_accessible(pilot, library):
    project = library.folder(pilot.auth, 'Projects')
    destination = library.folder(pilot.auth, 'Upload test', project)
    hidden = library.folder(pilot.auth, 'Files', destination, visibility='private')
    receipt = prepare(pilot, library, {'Files/original.txt': b'Exact bytes.'}, destination_confirmed=True)
    reviewer = colleague(pilot)
    with pilot.repo._connection(reviewer) as c:
        assert not c.execute('SELECT id FROM central_brain.library_nodes WHERE id=%s', (hidden,)).fetchone()
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s',
                             (receipt['id'],)).fetchone()
    assert 'Files/original.txt' not in login(pilot, 'colleague').text


def test_existing_shared_destination_and_original_subfolder_remain_reviewable(pilot, library):
    project = library.folder(pilot.auth, 'Projects')
    destination = library.folder(pilot.auth, 'Upload test', project)
    library.folder(pilot.auth, 'Files', destination)
    receipt = prepare(pilot, library, {'Files/original.txt': b'Exact bytes.'}, destination_confirmed=True)
    reviewer = colleague(pilot)
    with pilot.repo._connection(reviewer) as c:
        assert c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s',
                         (receipt['id'],)).fetchone()
    assert 'Files/original.txt' in login(pilot, 'colleague').text
