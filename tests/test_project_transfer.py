import hashlib
import time
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from psycopg.types.json import Jsonb

from test_pilot import pilot
from test_library import library
from test_archives import selection
from central_brain.archive_transfer import OriginalManifest, prepare_transfer
from central_brain.archives import approve_batch, review_suggestion
from central_brain.library_worker import purge_one
from central_brain.auth import AuthContext


def prepare(pilot, library, originals, **kw):
    inventory = [OriginalManifest(name=name, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                 for name, data in originals.items()]
    return prepare_transfer(library, pilot.settings, pilot.auth, 'Projects/Upload test',
                            'Project originals.', 'Integration test', inventory, **kw)


def send(pilot, upload, data):
    return pilot.client.post(upload['upload_url'], content=data,
                             headers={'Authorization': 'Bearer ' + upload['upload_token'],
                                      'Content-Type': 'application/octet-stream'})


def test_project_transfer_large_files_subfolders_resume_and_approval(pilot, library):
    originals = {'Assets/logo.svg': b'<svg>' + b' ' * 1100000 + b'</svg>',
                 **{f'Documents/File-{i}.txt': b'exact project original' for i in range(25)}}
    prepared = prepare(pilot, library, originals)
    sid = UUID(str(prepared['id']))
    assert len(prepared['uploads']) == 26
    assert library.usage(pilot.auth)['used'] == sum(map(len, originals.values()))
    with pytest.raises(HTTPException) as exc:
        approve_batch(library, pilot.auth, selection(library, pilot.auth, [('suggestion', sid)]))
    assert exc.value.status_code == 409
    assert not library.snapshot(pilot.auth)
    assert send(pilot, prepared['uploads'][0], b'wrong').status_code == 422
    for upload in prepared['uploads']:
        result = send(pilot, upload, originals[upload['name']])
        assert result.status_code == 200, result.text
    assert result.json()['status'] == 'proposed'
    assert send(pilot, prepared['uploads'][0], originals['Assets/logo.svg']).json()['duplicate']
    resumed = prepare(pilot, library, originals)
    assert all(u['received'] for u in resumed['uploads'])
    with library.repo._connection(pilot.auth) as c:
        payload = c.execute('SELECT payload FROM central_brain.library_suggestions WHERE id=%s', (sid,)).fetchone()['payload']
        assert all('content' not in f for f in payload['files'])
        keys = [f['object_key'] for f in payload['files']]
    approve_batch(library, pilot.auth, selection(library, pilot.auth, [('suggestion', sid)]))
    nodes = library.snapshot(pilot.auth)
    for path, data in originals.items():
        node = next(n for n in nodes if n['path'] == '/Projects/Upload test/' + path)
        with library.download(pilot.auth, node['id'])[1] as stream:
            assert stream.read() == data
    for _ in keys:
        assert purge_one(library, pilot.auth)
    assert all(not (__import__('pathlib').Path(pilot.settings.library_local_path) / k).exists() for k in keys)
    assert send(pilot, prepared['uploads'][0], originals['Assets/logo.svg']).status_code == 409


def test_project_quota_revocation_reject_and_expiry(pilot, library):
    original = {'big.txt': b'x' * 200000}
    pilot.settings.library_quota_bytes = 199999
    with pytest.raises(HTTPException):
        prepare(pilot, library, original)
    with library.repo._connection(pilot.auth) as c:
        assert c.execute('SELECT count(*) AS n FROM central_brain.library_suggestions').fetchone()['n'] == 0
    pilot.settings.library_quota_bytes = 400000
    prepared = prepare(pilot, library, original)
    sid = UUID(str(prepared['id']))
    colleague = AuthContext(pilot.settings.principals()['colleague'])
    assert library.usage(colleague)['used'] == 200000
    with pytest.raises(HTTPException):
        library.upload(colleague, 'colleague.txt', b'x' * 200001)
    with pytest.raises(HTTPException):
        library.upload(pilot.auth, 'other.txt', b'x' * 200001)
    assert send(pilot, prepared['uploads'][0], original['big.txt']).status_code == 200
    with library.repo._connection(pilot.auth) as c:
        review_suggestion(library, pilot.auth, sid, False, c, [])
    assert library.usage(pilot.auth)['used'] == 0
    assert purge_one(library, pilot.auth)
    prepared = prepare(pilot, library, original, summary_filename='retry.md')
    sid = UUID(str(prepared['id']))
    with library.repo._connection(pilot.auth) as c:
        p = c.execute('SELECT payload FROM central_brain.library_suggestions WHERE id=%s', (sid,)).fetchone()['payload']
        p['transfer_expires'] = int(time.time()) - 1
        c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(p), sid))
    purge_one(library, pilot.auth)
    assert library.usage(pilot.auth)['used'] == 0
    prepared = prepare(pilot, library, original, summary_filename='revoked.md')
    pilot.settings.central_brain_principals_json = '{}'
    assert send(pilot, prepared['uploads'][0], original['big.txt']).status_code == 403


def test_project_path_and_size_limits(pilot, library):
    for path in ['../file.txt', '/file.txt', 'a//file.txt']:
        with pytest.raises(HTTPException):
            prepare(pilot, library, {path: b'x'})
    with pytest.raises(HTTPException):
        prepare(pilot, library, {'a.txt': b'x', 'a.txt/child.txt': b'x'})
    with pytest.raises(ValidationError):
        OriginalManifest(name='large.pdf', size_bytes=52428801, sha256='a' * 64)
    assert OriginalManifest(name='large.pdf', size_bytes=52428800, sha256='a' * 64)


def test_maximum_original_over_http_and_storage_failure_retry(pilot, library, monkeypatch):
    from central_brain.library import ObjectStore
    data = b'x' * (50 * 1024 * 1024)
    prepared = prepare(pilot, library, {'maximum.txt': data})
    upload = prepared['uploads'][0]
    assert pilot.client.post('/archive-original', content=b'x').status_code == 401
    real_put = ObjectStore.put
    def fail_once(self, key, content):
        raise HTTPException(503, 'Simulated storage outage.')
    monkeypatch.setattr(ObjectStore, 'put', fail_once)
    assert send(pilot, upload, data).status_code == 503
    monkeypatch.setattr(ObjectStore, 'put', real_put)
    assert send(pilot, upload, data).json()['status'] == 'proposed'
    assert send(pilot, upload, data + b'x').status_code == 422
    with library.repo._connection(pilot.auth) as c:
        review_suggestion(library, pilot.auth, UUID(str(prepared['id'])), False, c, [])
    assert purge_one(library, pilot.auth)
