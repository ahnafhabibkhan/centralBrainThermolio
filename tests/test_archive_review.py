import re
from uuid import UUID
import pytest
from fastapi import HTTPException
from test_pilot import pilot
from test_library import library
from test_project_transfer import prepare, send
from central_brain.archives import approval_item, approve_batch
from central_brain.archive_review import review


def test_individual_review_ready_files_do_not_wait_for_missing_originals(pilot, library):
    a, b = b'A'*200000, b'B'*200000
    receipt = prepare(pilot, library, {'nested/a.txt':a,'nested/b.txt':b})
    sid = UUID(str(receipt['id']))
    assert send(pilot, receipt['uploads'][0], a).status_code == 200
    with library.repo._connection(pilot.auth) as c:
        items = [approval_item(library,pilot.auth,c,'archive_file',sid,i) for i in [-1,-2,0]]
        with pytest.raises(HTTPException):
            approval_item(library,pilot.auth,c,'archive_file',sid,1)
    assert approve_batch(library,pilot.auth,items) == 3
    paths = {n['path'] for n in library.snapshot(pilot.auth)}
    assert '/Projects/Upload test/nested/a.txt' in paths
    assert '/Projects/Upload test/summary.md' in paths
    assert '/Projects/Upload test/nested/b.txt' not in paths
    with pytest.raises(HTTPException):
        approve_batch(library,pilot.auth,items)
    with library.repo._connection(pilot.auth) as c:
        assert c.execute('SELECT size_bytes FROM central_brain.transfer_reservations WHERE id=%s',(sid,)).fetchone()['size_bytes'] == len(b)
        review(library,pilot.auth,sid,1,False,c,[])
    assert send(pilot,receipt['uploads'][1],b).status_code == 409
    assert '/Projects/Upload test/nested/a.txt' in {n['path'] for n in library.snapshot(pilot.auth)}


def test_workspace_has_individual_rows_and_bulk_approves_only_ready(pilot, library):
    receipt = prepare(pilot,library,{'first.txt':b'x'*200000,'second.txt':b'y'*200000})
    login = pilot.client.get('/login')
    csrf = re.search(r'name="csrf_token" value="([^"]+)"',login.text).group(1)
    pilot.client.post('/login',data={'token':'owner','csrf_token':csrf})
    page = pilot.client.get('/library')
    assert 'Approve all (2)' in page.text
    assert page.text.count('data-queue-upload') == 2
    from html import unescape
    items = unescape(re.search(r'name="items" value="([^"]+)"',page.text).group(1))
    csrf = re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
    assert pilot.client.post('/library/approve-all',data={'items':items,'csrf_token':csrf}).status_code == 200
    page = pilot.client.get('/library')
    assert 'Approve all (2)' not in page.text
    assert page.text.count('data-queue-upload') == 2


def test_single_entry_does_not_decode_other_originals(monkeypatch):
    from central_brain.archive_review import entry
    from central_brain.archives import ArchiveFile
    decoded = []
    original = ArchiveFile.data
    def record(self):
        decoded.append(self.name)
        return original(self)
    monkeypatch.setattr(ArchiveFile, 'data', record)
    payload = {'files': [{'name': f'{i}.txt', 'content': 'original', 'encoding': 'utf8'} for i in range(59)]}
    assert entry(payload, 30)['data'] == b'original'
    assert decoded == ['30.txt']


def test_bulk_json_groups_preserve_completed_approvals_and_download_original(pilot, library):
    import json
    from html import unescape
    data = b'original pdf bytes' * 10000
    receipt = prepare(pilot, library, {'Bills/original.pdf': data, 'Bills/missing.pdf': data})
    sid = str(receipt['id'])
    assert send(pilot, receipt['uploads'][0], data).status_code == 200
    login = pilot.client.get('/login')
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text)[1]
    pilot.client.post('/login', data={'token': 'owner', 'csrf_token': csrf})
    page = pilot.client.get('/library').text
    assert 'data-queue-filter="ready"' in page and 'data-queue-filter="upload"' in page
    assert f'href="/library/suggestions/{sid}"' not in page
    legacy = pilot.client.get('/library/suggestions/' + sid, follow_redirects=False)
    assert legacy.headers['location'] == '/library#approvals'
    url = f'/library/suggestions/{sid}/files/0/download'
    downloaded = pilot.client.get(url)
    assert downloaded.content == data
    assert downloaded.headers['content-disposition'].startswith('attachment;')
    assert pilot.client.get(f'/library/suggestions/{sid}/files/1/download').status_code == 409
    items = json.loads(unescape(re.search(r'name="items" value="([^"]+)"', page)[1]))
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    headers = {'Accept': 'application/json'}
    first = pilot.client.post('/library/approve-all', data={'items': json.dumps(items[:1]), 'csrf_token': csrf}, headers=headers)
    assert first.json() == {'approved': 1}
    rest = pilot.client.post('/library/approve-all', data={'items': json.dumps(items[1:]), 'csrf_token': csrf}, headers=headers)
    assert rest.json() == {'approved': 2}
    assert pilot.client.post('/library/approve-all', data={'items': json.dumps(items), 'csrf_token': csrf}, headers=headers).status_code == 409
    assert '/Projects/Upload test/Bills/original.pdf' in {n['path'] for n in library.snapshot(pilot.auth)}


def test_fifty_nine_approvals_can_complete_in_successive_groups(pilot, library):
    import json
    from html import unescape
    originals = {f'Files/{i}.txt': f'Original file {i}.'.encode() for i in range(57)}
    receipt = prepare(pilot, library, originals)
    for upload in receipt['uploads']:
        assert send(pilot, upload, originals[upload['name']]).status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', pilot.client.get('/login').text)[1]
    pilot.client.post('/login', data={'token': 'owner', 'csrf_token': csrf})
    page = pilot.client.get('/library').text
    items = json.loads(unescape(re.search(r'name="items" value="([^"]+)"', page)[1]))
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    assert len(items) == 59
    for start in range(0, len(items), 3):
        batch = items[start:start + 3]
        response = pilot.client.post('/library/approve-all',
            data={'items': json.dumps(batch), 'csrf_token': csrf}, headers={'Accept': 'application/json'})
        assert response.json() == {'approved': len(batch)}
    assert len([n for n in library.snapshot(pilot.auth) if n['kind'] == 'file']) == 59


def test_duplicate_original_reuses_exact_copy_but_does_not_overwrite(pilot, library):
    for number, data in enumerate([b'original', b'original', b'changed']):
        receipt = prepare(pilot, library, {'Files/same.txt': data}, summary_filename=f'batch-{number}.md', destination_confirmed=True)
        sid = UUID(str(receipt['id']))
        assert send(pilot, receipt['uploads'][0], data).status_code == 200
        with library.repo._connection(pilot.auth) as c:
            item = approval_item(library, pilot.auth, c, 'archive_file', sid, 0)
        if number < 2:
            assert approve_batch(library, pilot.auth, [item]) == 1
        else:
            with pytest.raises(HTTPException) as error:
                approve_batch(library, pilot.auth, [item])
            assert error.value.status_code == 409
            assert 'different contents' in error.value.detail
    files = [n for n in library.snapshot(pilot.auth) if n['kind'] == 'file']
    assert len(files) == 1
    with library.download(pilot.auth, files[0]['id'])[1] as stream:
        assert stream.read() == b'original'
