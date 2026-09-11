import asyncio
import json
import re
from uuid import uuid4

from central_brain.auth import AuthContext
from central_brain.library import Library
from central_brain.library_worker import process_one
from central_brain.retrieval import task_context
from test_pilot import pilot


def test_copy_grouping_before_limit_and_access_filters(pilot, tmp_path):
    library = Library(pilot.repo, pilot.settings)
    pilot.settings.library_local_path = str(tmp_path)
    auth = pilot.auth
    a = library.folder(auth, 'Public documents')
    source = library.upload(auth, 'Heating.md', b'Boiler maintenance requires annual inspection.', a)
    assert process_one(library, auth)
    for i in range(12):
        library.copy(auth, source, f'Heating-{i}.md', a)
    private = library.copy(auth, source, 'Private.md', library.folder(auth, 'Private'))
    with pilot.repo._connection(auth) as c:
        c.execute("UPDATE central_brain.library_nodes SET visibility='private' WHERE id=%s", (private,))
    other = library.upload(auth, 'Different.md', b'Boiler replacement has a separate capital budget.', a)
    assert process_one(library, auth)
    colleague = AuthContext(auth.principal.model_copy(update={'actor_id': uuid4()}))
    results = library.search(colleague, 'boiler')['results']
    assert len(results) == 2
    group = next(x for x in results if x['duplicate_count'])
    assert group['duplicate_count'] == 12
    assert len(group['also_in']) == 10 and group['duplicate_locations_truncated']
    assert 'Private' not in json.dumps(results, default=str)
    assert other in [x['id'] for x in results]
    assert len(library.search(auth, 'Heating-3.md')['results']) == 1
    login = pilot.client.get('/login').text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login)[1]
    pilot.client.post('/login', data={'token': 'owner', 'csrf_token': csrf})
    page = pilot.client.get('/library?q=boiler')
    assert page.status_code == 200 and 'Also available in' in page.text
    from central_brain.mcp_server import build_mcp
    names = [tool.name for tool in asyncio.run(build_mcp(pilot.settings, pilot.repo).list_tools())]
    assert 'get_context_for_task' in names


def test_task_refresh_copy_move_delete_and_filename_lookup(pilot, tmp_path):
    library = Library(pilot.repo, pilot.settings)
    pilot.settings.library_local_path = str(tmp_path)
    auth = pilot.auth
    file = library.upload(auth, 'BrandLogo.png', b'opaque test image')
    first = task_context(library, auth, 'BrandLogo')
    assert first['results'][0]['id'] == str(file)
    assert first['results'][0]['location'] == 'File details'
    assert first['changed'] and not first['refresh_required']
    same = task_context(library, auth, 'BrandLogo', known_revision=first['context_revision'])
    assert not same['changed'] and 'folders' not in same['context']
    folder = library.folder(auth, 'Brand')
    library.move(auth, file, 'BrandLogo.png', folder)
    moved = task_context(library, auth, 'BrandLogo', known_revision=first['context_revision'])
    assert moved['changed'] and moved['results'][0]['path'] == '/Brand/BrandLogo.png'
    plan = library.deletion_plan(auth, file)
    library.delete(auth, file, 'BrandLogo.png', plan['token'])
    deleted = task_context(library, auth, 'BrandLogo', known_revision=moved['context_revision'])
    assert deleted['changed'] and not deleted['results']
    assert deleted['context']['recent_deletions'][0]['id'] == str(file)


def test_changed_during_retrieval_fails_closed_and_output_bound(pilot, tmp_path, monkeypatch):
    library = Library(pilot.repo, pilot.settings)
    pilot.settings.library_local_path = str(tmp_path)
    for i in range(8):
        library.upload(pilot.auth, f'{i}.md', ('Boiler '+str(i)+' x'*1200).encode())
        process_one(library, pilot.auth)
    pilot.settings.max_context_chars = 1000
    result = task_context(library, pilot.auth, 'boiler')
    assert len(json.dumps(result)) <= 1000 and result['truncated']
    revisions = iter([{'revision': str(i)} for i in range(4)])
    monkeypatch.setattr(library, 'context', lambda auth: next(revisions))
    changed = task_context(library, pilot.auth, 'boiler')
    assert changed['refresh_required'] and not changed['results']
