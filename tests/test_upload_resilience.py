"""Failure injection and recovery checks use isolated workspaces and original bytes."""
import base64
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from test_pilot import pilot
from test_library import library
from test_project_transfer import prepare, send
from central_brain.archive_review import review
from central_brain.connector_upload import CHUNK_BYTES, status, upload_chunk
from central_brain.project_transfer import expire_incomplete, store_original
from central_brain.direct_upload import complete


def payload(library, auth, sid):
    with library.repo._connection(auth) as c:
        return c.execute('SELECT payload FROM central_brain.library_suggestions WHERE id=%s', (sid,)).fetchone()['payload']


def expire(library, auth, sid):
    p = payload(library, auth, sid)
    p['transfer_expires'] = int(time.time()) - 1
    with library.repo._connection(auth) as c:
        c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(p), sid))


@pytest.mark.parametrize('partial', [False, True])
def test_expiry_preserves_received_originals_and_chunks_and_renews(pilot, library, partial):
    data = b'original content' * 20000
    originals = {'nested/first.txt': data, 'nested/second.txt': data}
    result = prepare(pilot, library, originals)
    sid = UUID(str(result['id']))
    if partial:
        upload_chunk(library, pilot.auth, sid, 0, 0, base64.b64encode(data[:CHUNK_BYTES]).decode())
    else:
        store_original(library, pilot.auth, sid, 0, data)
    expire(library, pilot.auth, sid)
    expire_incomplete(library, pilot.auth)
    assert status(library, pilot.auth, sid)['status'] == 'proposed'
    assert status(library, pilot.auth, sid)['upload_access_expired']
    resumed = prepare(pilot, library, originals)
    assert resumed['id'] == result['id']
    assert not status(library, pilot.auth, sid)['upload_access_expired']
    if partial:
        assert status(library, pilot.auth, sid)['files'][0]['next_offset'] == CHUNK_BYTES
    for item in resumed['uploads']:
        assert send(pilot, item, data).status_code == 200
    assert status(library, pilot.auth, sid)['ready_file_count'] == 2
    with library.repo._connection(pilot.auth) as c:
        review(library, pilot.auth, sid, 0, True, c, [])
    saved = next(n for n in library.snapshot(pilot.auth) if n['name'] == 'first.txt')
    with library.download(pilot.auth, saved['id'])[1] as stream:
        assert stream.read() == data


def test_concurrent_retry_is_idempotent_and_can_switch_transport(pilot, library):
    data = b'original\x00\xff' * 30000
    sid = UUID(str(prepare(pilot, library, {'nested/data.pdf': data})['id']))
    upload_chunk(library, pilot.auth, sid, 0, 0, base64.b64encode(data[:CHUNK_BYTES]).decode())
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: store_original(library, pilot.auth, sid, 0, data), range(4)))
    assert sum(not r['duplicate'] for r in results) == 1
    state = status(library, pilot.auth, sid)
    assert state['files'][0]['next_offset'] == len(data)
    assert state['ready_file_count'] == 1
    with library.repo._connection(pilot.auth) as c:
        assert c.execute("SELECT count(*) AS n FROM central_brain.audit_events WHERE resource_id=%s AND action='archive.original_received'", (sid,)).fetchone()['n'] == 1


@pytest.mark.parametrize('damage', ['missing', 'truncated', 'wrong_same_size', 'oversized'])
def test_received_original_can_be_repaired_after_storage_damage(pilot, library, damage):
    data = b'recovery original' * 10000
    sid = UUID(str(prepare(pilot, library, {'source.pdf': data})['id']))
    store_original(library, pilot.auth, sid, 0, data)
    key = payload(library, pilot.auth, sid)['files'][0]['object_key']
    if damage == 'missing':
        library.store.delete(key)
    else:
        library.store.put(key, {'truncated': data[:10], 'wrong_same_size': b'x' * len(data), 'oversized': data + b'x'}[damage])
    result = store_original(library, pilot.auth, sid, 0, data)
    assert not result['duplicate']
    assert store_original(library, pilot.auth, sid, 0, data)['duplicate']
    with library.store.get(key) as stream:
        assert stream.read() == data


def test_rejected_files_cannot_be_completed_or_reissued(pilot, library):
    data = b'original' * 20000
    originals = {'one.txt': data, 'two.txt': data}
    sid = UUID(str(prepare(pilot, library, originals)['id']))
    store_original(library, pilot.auth, sid, 0, data)
    with library.repo._connection(pilot.auth) as c:
        review(library, pilot.auth, sid, 0, False, c, [])
    with pytest.raises(HTTPException) as error:
        complete(library, pilot.auth, sid, 0)
    assert error.value.status_code == 409
    assert status(library, pilot.auth, sid)['ready_file_count'] == 0
    assert status(library, pilot.auth, sid)['files'][0]['review_state'] == 'rejected'
    resumed = prepare(pilot, library, originals)['uploads']
    assert [(f['name'], f['file_index']) for f in resumed] == [('two.txt', 1)]


@pytest.mark.parametrize('offset,content,expected', [
    (1, b'x', 422), (-1, b'x', 422), (CHUNK_BYTES, b'x' * CHUNK_BYTES, 409),
    (0, b'', 422), (0, b'x' * (CHUNK_BYTES + 1), 422), (0, b'x', 422),
])
def test_invalid_chunks_do_not_change_resume_position(pilot, library, offset, content, expected):
    sid = UUID(str(prepare(pilot, library, {'a.txt': b'x' * (CHUNK_BYTES * 3)})['id']))
    with pytest.raises(HTTPException) as error:
        upload_chunk(library, pilot.auth, sid, 0, offset, base64.b64encode(content).decode())
    assert error.value.status_code == expected
    assert status(library, pilot.auth, sid)['files'][0]['next_offset'] == 0


@pytest.mark.parametrize('suffix', ['pdf', 'xlsx', 'docx', 'pptx', 'csv', 'md', 'txt', 'svg', 'png', 'jpg', 'jpeg', 'webp'])
def test_supported_types_preserve_bytes_names_and_paths(pilot, library, suffix):
    # Transport must preserve even an unindexable original rather than converting it to text.
    data = b'opaque original bytes\x00\xff\x80'
    name = f'R\u00e9sum\u00e9 & project/Original version 2.{suffix}'
    sid = UUID(str(prepare(pilot, library, {name: data})['id']))
    store_original(library, pilot.auth, sid, 0, data)
    with library.repo._connection(pilot.auth) as c:
        review(library, pilot.auth, sid, 0, True, c, [])
    node = next(n for n in library.snapshot(pilot.auth) if n['kind'] == 'file')
    assert node['path'].endswith('/' + name)
    actual_name, stream = library.download(pilot.auth, node['id'])
    with stream:
        assert hashlib.sha256(stream.read()).digest() == hashlib.sha256(data).digest()
    assert actual_name == name.split('/')[-1]


def test_manifest_batch_boundaries_and_collisions(pilot, library):
    originals = {f'part-{i}.txt': b'x' for i in range(100)}
    assert len(prepare(pilot, library, originals)['uploads']) == 100
    with pytest.raises(HTTPException) as error:
        prepare(pilot, library, {**originals, 'part-100.txt': b'x'}, summary_filename='second.md')
    assert error.value.status_code == 413
    for invalid in [{'nested/A.txt': b'x', 'nested/a.txt': b'x'}, {'summary.md': b'x', 'nested/a.txt': b'x'}]:
        with pytest.raises(HTTPException) as error:
            prepare(pilot, library, invalid)
        assert error.value.status_code == 422


def test_small_mcp_original_has_the_same_resumable_upload_options(pilot, library):
    data = b'%PDF-1.4\nsmall original'
    response = pilot.client.post('/mcp/', headers={
        'Authorization': 'Bearer assistant', 'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'prepare_chat_archive_upload', 'arguments': {
                'folder_path': 'Sales', 'summary': 'Original investor file.',
                'source_reference': 'Synthetic regression test', 'files': [{
                    'name': 'deck.pdf', 'size_bytes': len(data),
                    'sha256': hashlib.sha256(data).hexdigest()}]}}})
    assert response.status_code == 200
    result = response.json()['result']
    assert not result.get('isError')
    import json
    receipt = json.loads(result['content'][0]['text'])
    assert receipt['mode'] == 'individual_raw_files'
    assert receipt['completion_url'].endswith('/library#approvals')
    assert send(pilot, receipt['uploads'][0], data).status_code == 200
    assert status(library, pilot.auth, UUID(receipt['id']))['ready_file_count'] == 1


@pytest.mark.parametrize('suffix', ['pdf', 'xlsx'])
def test_indexing_failure_keeps_original_downloadable(pilot, library, suffix):
    from central_brain.library_worker import process_one
    data = b'This is deliberately not a valid document container.'
    node = library.upload(pilot.auth, f'broken.{suffix}', data)
    assert process_one(library, pilot.auth)
    assert library.read(pilot.auth, node)['state'] == 'failed'
    with library.download(pilot.auth, node)[1] as stream:
        assert stream.read() == data


def test_body_limit_handles_chunked_and_disconnected_uploads_without_delivery():
    import asyncio
    from central_brain.api import BodyLimit
    async def check(disconnect):
        delivered, messages = [], []
        async def app(scope, receive, send):
            delivered.append(True)
        count = 0
        async def receive():
            nonlocal count
            count += 1
            if disconnect and count == 2:
                return {'type': 'http.disconnect'}
            return {'type': 'http.request', 'body': b'x' * 1024 * 1024, 'more_body': True}
        async def send(message):
            messages.append(message)
        await BodyLimit(app)({'type': 'http', 'path': '/archive-original'}, receive, send)
        assert not delivered
        if disconnect:
            assert not messages
        else:
            assert messages[0]['status'] == 413
    asyncio.run(check(True))
    asyncio.run(check(False))
