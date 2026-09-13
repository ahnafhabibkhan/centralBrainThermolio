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
    assert page.text.count('class="button small upload-action"') == 2
    from html import unescape
    items = unescape(re.search(r'name="items" value="([^"]+)"',page.text).group(1))
    csrf = re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
    assert pilot.client.post('/library/approve-all',data={'items':items,'csrf_token':csrf}).status_code == 200
    page = pilot.client.get('/library')
    assert 'Approve all (2)' not in page.text
    assert page.text.count('class="button small upload-action"') == 2
