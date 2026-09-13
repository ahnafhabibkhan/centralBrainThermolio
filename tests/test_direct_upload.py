import base64
import hashlib
import json
from uuid import UUID

import boto3
import pytest
from botocore.config import Config
from fastapi import HTTPException

from test_pilot import pilot
from test_library import library
from test_project_transfer import prepare as project
from central_brain.direct_upload import prepare, complete
from central_brain.auth import AuthContext


def test_direct_s3_policy_completion_and_access(pilot, library, monkeypatch):
    data = b'exact large original' * 20000
    receipt = project(pilot, library, {'deck.pptx': data})
    sid = UUID(str(receipt['id']))
    client = boto3.client('s3', region_name='ca-central-1', aws_access_key_id='test',
                          aws_secret_access_key='test', config=Config(signature_version='s3v4'))
    monkeypatch.setattr(library.store, 'client', lambda: client)
    pilot.settings.library_bucket = 'test-private-bucket'
    colleague = AuthContext(pilot.settings.principals()['colleague'])
    with pytest.raises(HTTPException) as exc:
        prepare(library, colleague, sid, 0)
    assert exc.value.status_code == 404
    signed = prepare(library, pilot.auth, sid, 0)
    policy = json.loads(base64.b64decode(signed['fields']['policy']))
    assert ['content-length-range', len(data), len(data)] in policy['conditions']
    assert {'x-amz-checksum-sha256': base64.b64encode(hashlib.sha256(data).digest()).decode()} in policy['conditions']
    assert signed['fields']['key'].startswith('files/transfers/')
    assert signed['expires_in_seconds'] <= 600
    pilot.settings.library_bucket = ''
    with pytest.raises(HTTPException) as exc:
        complete(library, pilot.auth, sid, 0)
    assert exc.value.status_code == 409
    library.store.put(signed['fields']['key'], b'wrong')
    with pytest.raises(HTTPException) as exc:
        complete(library, pilot.auth, sid, 0)
    assert exc.value.status_code == 422
    library.store.put(signed['fields']['key'], data)
    with pytest.raises(HTTPException) as exc:
        complete(library, colleague, sid, 0)
    assert exc.value.status_code == 404
    assert complete(library, pilot.auth, sid, 0)['status'] == 'proposed'
    assert complete(library, pilot.auth, sid, 0)['duplicate']


def test_powerpoint_text_follows_presentation_order(tmp_path):
    import zipfile
    from central_brain.extract import extract
    path = tmp_path / 'deck.pptx'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('ppt/presentation.xml', '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId r:id="second"/><p:sldId r:id="first"/></p:sldIdLst></p:presentation>')
        z.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="first" Target="slides/slide1.xml"/><Relationship Id="second" Target="slides/slide2.xml"/></Relationships>')
        for n in [1, 2]:
            z.writestr(f'ppt/slides/slide{n}.xml', f'<slide xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Content {n}</a:t></slide>')
    result = extract(path, '.pptx')
    assert result['state'] == 'ready'
    assert result['sections'] == [{'location':'Slide 1','content':'Content 2'}, {'location':'Slide 2','content':'Content 1'}]
