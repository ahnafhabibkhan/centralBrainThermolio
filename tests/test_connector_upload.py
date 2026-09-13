import base64
from uuid import UUID

import pytest
from fastapi import HTTPException

from test_pilot import pilot
from test_library import library
from test_project_transfer import prepare
from test_archives import selection
from central_brain.connector_upload import CHUNK_BYTES, status, upload_chunk
from central_brain.archives import approve_batch
from central_brain.auth import AuthContext


def test_connector_chunks_resume_integrity_and_approval(pilot, library):
    data = b'original bytes\n' * 22000
    receipt = prepare(pilot, library, {'nested/source.txt': data})
    sid = UUID(str(receipt['id']))
    assert not status(library, pilot.auth, sid)['ready_for_approval']
    colleague = AuthContext(pilot.settings.principals()['colleague'])
    with pytest.raises(HTTPException):
        status(library, colleague, sid)
    first = base64.b64encode(data[:CHUNK_BYTES]).decode()
    with pytest.raises(HTTPException):
        upload_chunk(library, colleague, sid, 0, 0, first)
    assert upload_chunk(library, pilot.auth, sid, 0, 0, first)['next_offset'] == CHUNK_BYTES
    assert upload_chunk(library, pilot.auth, sid, 0, 0, first)['next_offset'] == CHUNK_BYTES
    with pytest.raises(HTTPException):
        upload_chunk(library, pilot.auth, sid, 0, 0, base64.b64encode(b'x'*CHUNK_BYTES).decode())
    for offset in range(CHUNK_BYTES, len(data), CHUNK_BYTES):
        result = upload_chunk(library, pilot.auth, sid, 0, offset, base64.b64encode(data[offset:offset+CHUNK_BYTES]).decode())
    assert result['ready_for_approval']
    approve_batch(library, pilot.auth, selection(library, pilot.auth, [('suggestion',sid)]))
    node = next(n for n in library.snapshot(pilot.auth) if n['name']=='source.txt')
    with library.download(pilot.auth,node['id'])[1] as stream:
        assert stream.read()==data


def test_connector_rejects_wrong_complete_checksum(pilot, library):
    data=b'x'*150000
    sid=UUID(str(prepare(pilot,library,{'original.txt':data})['id']))
    upload_chunk(library,pilot.auth,sid,0,0,base64.b64encode(data[:CHUNK_BYTES]).decode())
    with pytest.raises(HTTPException) as error:
        upload_chunk(library,pilot.auth,sid,0,CHUNK_BYTES,base64.b64encode(b'y'*(len(data)-CHUNK_BYTES)).decode())
    assert error.value.status_code==422
    assert not status(library,pilot.auth,sid)['ready_for_approval']
    assert upload_chunk(library,pilot.auth,sid,0,CHUNK_BYTES,base64.b64encode(data[CHUNK_BYTES:]).decode())['ready_for_approval']


def test_originals_use_mcp_without_direct_upload_http(pilot, library):
    data=b'connector-only upload\n'*15000
    sid=str(prepare(pilot,library,{'project.txt':data})['id'])
    headers={'Authorization':'Bearer assistant','Accept':'application/json, text/event-stream'}
    for i,offset in enumerate(range(0,len(data),CHUNK_BYTES)):
        response=pilot.client.post('/mcp/',headers=headers,json={'jsonrpc':'2.0','id':i+1,'method':'tools/call',
            'params':{'name':'upload_chat_archive_chunk','arguments':{'archive_id':sid,'file_index':0,
                'offset':offset,'content_base64':base64.b64encode(data[offset:offset+CHUNK_BYTES]).decode()}}})
        assert response.status_code==200
        assert not response.json()['result'].get('isError'), response.text
    assert status(library,pilot.auth,UUID(sid))['ready_for_approval']
