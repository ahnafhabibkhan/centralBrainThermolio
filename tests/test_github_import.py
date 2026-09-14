import io
from uuid import UUID

import pytest
from fastapi import HTTPException

from test_pilot import pilot
from test_library import library
from test_project_transfer import prepare
from central_brain.auth import AuthContext
from central_brain.github_import import download_original, import_original


def fake_github(monkeypatch, data, status=200):
    requests = []
    class Connection:
        def __init__(self, host, timeout):
            assert host == 'raw.githubusercontent.com'
        def request(self, method, target, headers):
            requests.append(target)
        def getresponse(self):
            stream = io.BytesIO(data)
            stream.status = status
            return stream
        def close(self):
            pass
    monkeypatch.setattr('central_brain.github_import.http.client.HTTPSConnection', Connection)
    return requests


def test_github_original_preserves_bytes_and_rejects_wrong_content(pilot, library, monkeypatch):
    data = b'%PDF-1.4\nexact original binary\x00\xff'
    sid = UUID(str(prepare(pilot, library, {'Bills/January.pdf': data})['id']))
    url = 'https://raw.githubusercontent.com/team/repo/commit/Bills/January.pdf?token=temporary'
    requests = fake_github(monkeypatch, data)
    colleague = AuthContext(pilot.settings.principals()['colleague'])
    with pytest.raises(HTTPException) as error:
        import_original(library, colleague, sid, 0, url)
    assert error.value.status_code == 404
    assert not requests
    fake_github(monkeypatch, b'x' * len(data))
    with pytest.raises(HTTPException) as error:
        import_original(library, pilot.auth, sid, 0, url)
    assert error.value.status_code == 422
    requests = fake_github(monkeypatch, data)
    result = import_original(library, pilot.auth, sid, 0, url)
    assert result['received'] == 'Bills/January.pdf'
    assert import_original(library, pilot.auth, sid, 0, url)['duplicate']
    assert len(requests) == 1
    with library.repo._connection(pilot.auth) as c:
        row = c.execute('SELECT payload,status FROM central_brain.library_suggestions WHERE id=%s', (sid,)).fetchone()
    assert row['status'] == 'proposed'
    assert 'temporary' not in str(row['payload'])
    with library.store.get(row['payload']['files'][0]['object_key']) as stream:
        assert stream.read() == data


@pytest.mark.parametrize('url', [
    'http://raw.githubusercontent.com/o/r/ref/a.pdf',
    'https://127.0.0.1/o/r/ref/a.pdf',
    'https://raw.githubusercontent.com.evil.example/o/r/ref/a.pdf',
    'https://raw.githubusercontent.com:8443/o/r/ref/a.pdf',
    'https://secret@raw.githubusercontent.com/o/r/ref/a.pdf',
    'https://raw.githubusercontent.com/o/r/ref/a.pdf#fragment',
])
def test_github_download_rejects_unsafe_hosts(url):
    with pytest.raises(HTTPException) as error:
        download_original(url, 10)
    assert error.value.status_code == 422


@pytest.mark.parametrize('status', [302, 403, 404])
def test_github_does_not_follow_redirects_or_leak_private_url(monkeypatch, status):
    requests = fake_github(monkeypatch, b'', status)
    with pytest.raises(HTTPException) as error:
        download_original('https://raw.githubusercontent.com/o/r/ref/a.pdf?token=secret', 10)
    assert error.value.status_code == 409
    assert 'secret' not in str(error.value)
    assert len(requests) == 1


def test_github_download_is_size_bounded(monkeypatch):
    fake_github(monkeypatch, b'x' * 100)
    with pytest.raises(HTTPException) as error:
        download_original('https://raw.githubusercontent.com/o/r/ref/a.pdf', 10)
    assert error.value.status_code == 422


@pytest.mark.parametrize('statuses', [[503, 200], [429, 200], [500, 502, 200]])
def test_github_retries_transient_responses(monkeypatch, statuses):
    calls = []
    sleeps = []
    class Connection:
        def __init__(self, host, timeout):
            pass
        def request(self, *args, **kwargs):
            calls.append(1)
        def getresponse(self):
            response = io.BytesIO(b'original')
            response.status = statuses[len(calls)-1]
            response.getheader = lambda name: '1'
            return response
        def close(self):
            pass
    monkeypatch.setattr('central_brain.github_import.http.client.HTTPSConnection', Connection)
    monkeypatch.setattr('central_brain.github_import.time.sleep', sleeps.append)
    assert download_original('https://raw.githubusercontent.com/o/r/ref/a.md?token=secret',8) == b'original'
    assert len(calls) == len(statuses)
    assert len(sleeps) == len(statuses)-1


def test_github_connection_retries_are_bounded(monkeypatch):
    calls = []
    class Connection:
        def __init__(self, host, timeout):
            pass
        def request(self, *args, **kwargs):
            calls.append(1)
            raise OSError('private token must not appear')
        def close(self):
            pass
    monkeypatch.setattr('central_brain.github_import.http.client.HTTPSConnection', Connection)
    monkeypatch.setattr('central_brain.github_import.time.sleep', lambda _: None)
    with pytest.raises(HTTPException) as error:
        download_original('https://raw.githubusercontent.com/o/r/ref/a.md?token=secret',8)
    assert len(calls) == 3
    assert error.value.status_code == 502
    assert 'secret' not in str(error.value)


def test_long_rate_limit_does_not_retry_early(monkeypatch):
    calls = []
    class Connection:
        def __init__(self, host, timeout):
            pass
        def request(self, *args, **kwargs):
            calls.append(1)
        def getresponse(self):
            response = io.BytesIO(b'')
            response.status = 429
            response.getheader = lambda name: '120'
            return response
        def close(self):
            pass
    monkeypatch.setattr('central_brain.github_import.http.client.HTTPSConnection', Connection)
    with pytest.raises(HTTPException) as error:
        download_original('https://raw.githubusercontent.com/o/r/ref/a.md',8)
    assert error.value.status_code == 429
    assert error.value.headers['Retry-After'] == '120'
    assert len(calls) == 1
