import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from test_library import library
from test_pilot import pilot, memory
from central_brain.auth import AuthContext
from central_brain.library_worker import process_one, purge_one


def remove(library, auth, node):
    plan = library.deletion_plan(auth, node)
    return library.delete(auth, node, plan['root']['name'], plan['token'])


def test_recursive_deletion_context_and_original_cleanup(pilot, library, monkeypatch):
    auth = pilot.auth
    folder = library.folder(auth, 'Chat archive')
    nested = library.folder(auth, 'Files', folder)
    node = library.upload(auth, 'Report.txt', b'Original boiler report.', nested)
    library.upload(auth, 'Report.txt', b'Updated boiler report.', node_id=node)
    record = pilot.repo.create(auth, memory())
    pilot.repo.approve(auth, record.memory_id)
    library.move(auth, record.memory_id, 'Decision.md', folder)
    while process_one(library, auth):
        pass
    plan = library.deletion_plan(auth, folder)
    assert plan['counts'] == {'folder': 2, 'file': 1, 'memory': 1}
    assert plan['version_count'] == 2
    keys = [v['object_key'] for v in plan['versions']]
    before = library.context(auth)['revision']
    assert remove(library, auth, folder) is None
    assert not library.search(auth, 'boiler')['results']
    for item in [folder, nested, node, record.memory_id]:
        with pytest.raises(HTTPException):
            library.info(auth, item, True)
    assert not library.snapshot(auth)
    context = library.context(auth)
    assert context['revision'] != before
    assert {d['id'] for d in context['recent_deletions']} == {str(i) for i in [folder, nested, node, record.memory_id]}
    assert any(d['path'] == '/Chat archive/Files/Report.txt' for d in context['recent_deletions'])
    assert library.usage(auth)['used'] == 0
    original_delete = library.store.delete
    def fail(key):
        raise OSError('Temporary storage failure')
    monkeypatch.setattr(library.store, 'delete', fail)
    with pytest.raises(OSError):
        while purge_one(library, auth):
            pass
    assert all((Path(library.settings.library_local_path) / key).exists() for key in keys)
    monkeypatch.setattr(library.store, 'delete', original_delete)
    while purge_one(library, auth):
        pass
    assert all(not (Path(library.settings.library_local_path) / key).exists() for key in keys)
    assert library.context(auth)['revision'] == context['revision']


def test_confirmation_changes_and_protected_roots(pilot, library):
    auth = pilot.auth
    roots, _ = library.workspace_folders(auth)
    for node in roots.values():
        with pytest.raises(HTTPException, match='permanent workspace folders'):
            library.deletion_plan(auth, node)
    node = library.folder(auth, 'Review first')
    plan = library.deletion_plan(auth, node)
    with pytest.raises(HTTPException) as wrong:
        library.delete(auth, node, 'wrong', plan['token'])
    assert wrong.value.status_code == 422
    library.upload(auth, 'New.txt', b'Arrived after the warning.', node)
    with pytest.raises(HTTPException) as stale:
        library.delete(auth, node, 'Review first', plan['token'])
    assert stale.value.status_code == 409
    assert len(library.listing(auth, node)) == 1


def test_hidden_descendant_rolls_back_everything(pilot, library):
    auth = pilot.auth
    colleague = AuthContext(pilot.settings.principals()['colleague'])
    folder = library.folder(auth, 'Shared folder')
    public = library.upload(auth, 'Public.txt', b'Keep this content.', folder)
    private = library.upload(colleague, 'Private.txt', b'Private original.', folder, visibility='private')
    plan = library.deletion_plan(auth, folder)
    assert plan['counts']['file'] == 1
    with pytest.raises(HTTPException) as denied:
        library.delete(auth, folder, 'Shared folder', plan['token'])
    assert denied.value.status_code == 409
    assert library.info(auth, public)['name'] == 'Public.txt'
    assert library.info(colleague, private)['name'] == 'Private.txt'
    assert not library.context(auth)['recent_deletions']
    assert remove(library, colleague, folder) is None


def test_deletion_notices_respect_access_and_approval(pilot, library):
    auth = pilot.auth
    colleague = AuthContext(pilot.settings.principals()['colleague'])
    reader = AuthContext(pilot.settings.principals()['assistant'])
    before = library.context(colleague)['revision']
    private = library.upload(auth, 'Private.txt', b'Private text.', visibility='private')
    with pytest.raises(HTTPException) as denied:
        library.deletion_plan(reader, private)
    assert denied.value.status_code == 403
    remove(library, auth, private)
    assert not library.context(colleague)['recent_deletions']
    assert library.context(colleague)['revision'] == before
    proposed = library.upload(auth, 'Unapproved.txt', b'Unapproved text.', proposed=True)
    remove(library, auth, proposed)
    assert str(proposed) not in {d['id'] for d in library.context(reader)['recent_deletions']}
    assert str(proposed) in {d['id'] for d in library.context(auth, True)['recent_deletions']}
    record = pilot.repo.create(auth, memory())
    pilot.repo.approve(auth, record.memory_id)
    pilot.repo.transition(auth, record.memory_id, 'delete')
    assert str(record.memory_id) in {d['id'] for d in library.context(reader)['recent_deletions']}


def test_indexing_cannot_restore_deleted_file(pilot, library, monkeypatch):
    import central_brain.library_worker as worker
    node = library.upload(pilot.auth, 'Indexing.txt', b'Never restore this content.')
    original_run = worker.subprocess.run
    def delete_during_extraction(*args, **kwargs):
        remove(library, pilot.auth, node)
        return original_run(*args, **kwargs)
    monkeypatch.setattr(worker.subprocess, 'run', delete_during_extraction)
    assert process_one(library, pilot.auth)
    assert not library.search(pilot.auth, 'restore')['results']
    with library.repo._connection(pilot.auth) as c:
        assert c.execute('SELECT count(*) AS n FROM central_brain.library_sections').fetchone()['n'] == 0


def test_web_delete_requires_csrf_and_confirmation(pilot, library):
    client = pilot.client
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
    client.post('/login', data={'token':'owner','csrf_token':csrf})
    node = library.upload(pilot.auth, 'Delete me.txt', b'Test document.')
    url = f'/library/file/{node}/delete'
    warning = client.get(url).text
    assert 'cannot be undone' in warning and 'Delete me.txt' in warning
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', warning)[1]
    token = re.search(r'name="plan_token" value="([^"]+)"', warning)[1]
    data = {'csrf_token':'wrong','confirmation':'Delete me.txt','plan_token':token}
    assert client.post(url, data=data).status_code == 403
    data['csrf_token'] = csrf
    data['confirmation'] = 'wrong'
    assert client.post(url, data=data).status_code == 422
    data['confirmation'] = 'Delete me.txt'
    assert client.post(url, data=data, follow_redirects=False).status_code == 303
    assert client.get(f'/library/file/{node}/download').status_code == 404
