import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import dotenv_values
from fastapi import HTTPException
from fastapi.testclient import TestClient

from central_brain.api import create_app
from central_brain.auth import AuthContext, TokenAuthenticator
from central_brain.config import Principal, Settings
from central_brain.manage import seed_identity
from central_brain.models import MemoryCreate, SearchRequest
from central_brain.repository import PostgresMemoryRepository


@pytest.fixture
def pilot():
    env = dotenv_values('.env')
    if not env.get('DATABASE_URL'):
        pytest.fail('Run init-local, docker compose up, and bootstrap --database central_brain_test first.')
    database = env['DATABASE_URL'].rsplit('/', 1)[0] + '/central_brain_test'
    migration = env['MIGRATION_DATABASE_URL'].rsplit('/', 1)[0] + '/central_brain_test'
    workspace, actor, other_actor, other_workspace = [uuid4() for _ in range(4)]
    with psycopg.connect(migration) as connection:
        connection.execute('SET ROLE central_brain_owner')
        seed_identity(connection, workspace, actor, 'Integration test')
        seed_identity(connection, workspace, other_actor, 'Integration test')
        seed_identity(connection, other_workspace, uuid4(), 'Other workspace')
    common = {'workspace_id': workspace, 'actor_id': actor,
              'roles': {'reader', 'writer', 'reviewer', 'admin'}}
    owner = Principal(**common)
    assistant = owner.model_copy(update={'roles': {'reader', 'writer'}})
    outsider = owner.model_copy(update={'workspace_id': other_workspace})
    colleague = owner.model_copy(update={'actor_id': other_actor})
    principals = {'owner': owner, 'assistant': assistant, 'outsider': outsider, 'colleague': colleague}
    settings = Settings(database_url=database, session_secret='t' * 48,
                        central_brain_principals_json=json.dumps({
                            key: value.model_dump(mode='json') for key, value in principals.items()}),
                        requests_per_minute=600)
    repo = PostgresMemoryRepository(database)
    repo.open()
    with TestClient(create_app(repo, settings), base_url='http://127.0.0.1:8080') as client:
        yield SimpleNamespace(repo=repo, client=client, settings=settings,
                              auth=AuthContext(owner), migration=migration)
    repo.close()


def headers(token='owner'):
    return {'Authorization': f'Bearer {token}'}


def memory(**updates):
    return MemoryCreate(content='Always show costs in CAD.', memory_type='preference',
                        source={'kind': 'human', 'reference': 'Explicit test instruction'}, **updates)


def test_approval_lifecycle_and_roles(pilot):
    c = pilot.client
    assert c.get('/ready').status_code == 200
    assert c.get('/v1/memories').status_code == 401
    response = c.post('/v1/memories', headers=headers('assistant'), json=memory().model_dump(mode='json'))
    assert response.status_code == 201, response.text
    mid = response.json()['memory_id']
    assert c.post('/v1/memories/search', headers=headers(), json={'query': 'CAD'}).json() == []
    assert c.post(f'/v1/memories/{mid}/approve', headers=headers('assistant')).status_code == 403
    assert c.delete(f'/v1/memories/{mid}', headers=headers('assistant')).status_code == 403
    assert c.post(f'/v1/memories/{mid}/approve', headers=headers()).status_code == 200
    assert len(c.post('/v1/memories/search', headers=headers('assistant'), json={'query': 'CAD'}).json()) == 1
    assert c.post(f'/v1/memories/{mid}/reject', headers=headers()).status_code == 409
    assert c.get(f'/v1/memories/{mid}/export', headers=headers()).status_code == 200
    assert c.delete(f'/v1/memories/{mid}', headers=headers()).status_code == 200
    assert c.get(f'/v1/memories/{mid}', headers=headers()).status_code == 404
    with pilot.repo._connection(pilot.auth) as connection:
        row = connection.execute('SELECT content,source,metadata FROM central_brain.memories WHERE id=%s', (mid,)).fetchone()
        assert row == {'content': '[Deleted]', 'source': {}, 'metadata': {}}
        assert connection.execute('SELECT count(*) AS n FROM central_brain.audit_events WHERE resource_id=%s', (mid,)).fetchone()['n'] == 3
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('DELETE FROM central_brain.audit_events')


def test_tenant_privacy_and_missing_context(pilot):
    record = pilot.repo.create(pilot.auth, memory(visibility='private'))
    pilot.repo.approve(pilot.auth, record.memory_id)
    for token in ('outsider', 'colleague'):
        assert pilot.client.get(f'/v1/memories/{record.memory_id}', headers=headers(token)).status_code == 404
        assert pilot.client.post('/v1/memories/search', headers=headers(token), json={'query': 'CAD'}).json() == []
    with pilot.repo.pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM central_brain.memories').fetchone()['n'] == 0
        assert not connection.execute("SELECT rolbypassrls OR rolsuper AS unsafe FROM pg_roles WHERE rolname=current_user").fetchone()['unsafe']


def test_duplicate_writes_and_conflicting_keys(pilot):
    item = memory(confidence=0.1, dedupe_key='same-request')
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: pilot.repo.create(pilot.auth, item), range(8)))
    assert len({r.memory_id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    with pytest.raises(HTTPException) as error:
        pilot.repo.create(pilot.auth, item.model_copy(update={'content': 'Different'}))
    assert error.value.status_code == 409


def test_competing_revisions(pilot):
    original = pilot.repo.create(pilot.auth, memory())
    pilot.repo.approve(pilot.auth, original.memory_id)
    revisions = [pilot.repo.create(pilot.auth, memory().model_copy(update={'content': f'CAD revision {i}'}),
                                  original.memory_id) for i in range(2)]
    def approve(receipt):
        try:
            pilot.repo.approve(pilot.auth, receipt.memory_id)
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(approve, revisions)) == [200, 409]
    assert pilot.repo.get(pilot.auth, original.memory_id).status == 'superseded'
    assert len(pilot.repo.search(pilot.auth, SearchRequest(query='CAD'))) == 1


def test_validation_and_expiry(pilot):
    c = pilot.client
    item = memory().model_dump(mode='json')
    item['source'] = {'kind': 'human'}
    assert c.post('/v1/memories', headers=headers(), json=item).status_code == 422
    assert c.post('/v1/memories', headers=headers(), json=memory(sensitivity='restricted').model_dump(mode='json')).status_code == 403
    receipt = pilot.repo.create(pilot.auth, memory(expires_at=datetime.now(UTC) + timedelta(hours=1)))
    pilot.repo.approve(pilot.auth, receipt.memory_id)
    with pilot.repo._connection(pilot.auth) as connection:
        connection.execute("UPDATE central_brain.memories SET expires_at=now()-interval '1 second' WHERE id=%s", (receipt.memory_id,))
    assert pilot.repo.search(pilot.auth, SearchRequest(query='CAD')) == []
    assert c.post('/v1/memories', headers=headers(), content='x' * 262145).status_code == 413


def csrf(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_web_session_csrf_and_escaping(pilot):
    c = pilot.client
    page = c.get('/login')
    assert page.status_code == 200
    assert page.headers['Referrer-Policy'] == 'same-origin'
    assert c.post('/login', data={'token': 'owner', 'csrf_token': 'wrong'}).status_code == 403
    login = c.post('/login', data={'token': 'owner', 'csrf_token': csrf(page)})
    assert login.status_code == 200, login.text
    assert 'owner' not in c.cookies.get('brain_session')
    receipt = pilot.repo.create(pilot.auth, memory().model_copy(update={'content': '<script>alert(1)</script>'}))
    detail = c.get(f'/review/{receipt.memory_id}')
    assert '&lt;script&gt;' in detail.text
    assert '<script>alert' not in detail.text
    assert c.post(f'/review/{receipt.memory_id}/approve', data={'csrf_token': csrf(detail)},
                  headers={'Origin': 'https://evil.example'}).status_code == 403
    assert c.post(f'/review/{receipt.memory_id}/approve', data={'csrf_token': csrf(detail)}).status_code == 200
    page = c.get('/')
    assert c.post('/logout', data={'csrf_token': csrf(page)}).status_code == 200
    assert c.get('/', follow_redirects=False).status_code == 303


def test_mcp_tools_and_auth(pilot):
    c = pilot.client
    assert c.post('/mcp/').status_code == 401
    h = {**headers('assistant'), 'Accept': 'application/json, text/event-stream'}
    response = c.post('/mcp/', headers=h, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                   'clientInfo': {'name': 'integration-test', 'version': '1'}}})
    assert response.status_code == 200, response.text
    response = c.post('/mcp/', headers=h, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    assert {t['name'] for t in response.json()['result']['tools']} == {
        'search_memories', 'propose_memory', 'list_skills', 'get_skill'}
    response = c.post('/mcp/', headers=h, json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
        'params': {'name': 'propose_memory', 'arguments': {'memory': memory().model_dump(mode='json')}}})
    assert response.status_code == 200, response.text
    assert not response.json()['result'].get('isError'), response.text
    assert len(pilot.repo.list(pilot.auth)) == 1


def test_signed_oauth_tokens(pilot):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(database_url=pilot.settings.database_url, session_secret='t' * 48,
        environment='production', public_url='https://brain.example',
        central_brain_principals_json='{}', oauth_issuer='https://issuer.example/pool',
        oauth_client_ids=['web', 'connector'], oauth_web_client_id='web',
        oauth_principals_json=json.dumps({'person': pilot.auth.principal.model_dump(mode='json')}))
    authenticator = TokenAuthenticator(settings)
    authenticator.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key()))
    claims = {'iss': settings.oauth_issuer, 'aud': settings.public_url, 'sub': 'person',
              'client_id': 'connector', 'token_use': 'access', 'iat': datetime.now(UTC),
              'exp': datetime.now(UTC) + timedelta(minutes=5),
              'scope': 'central-brain/read central-brain/propose central-brain/review central-brain/admin'}
    def token(**updates):
        return jwt.encode({**claims, **updates}, key, algorithm='RS256')
    assert authenticator.verify(token()).principal.roles == {'reader', 'writer'}
    assert 'admin' in authenticator.verify(token(client_id='web')).principal.roles
    for changes in ({'aud': 'https://wrong.example'}, {'client_id': 'unknown'}, {'sub': 'unknown'},
                    {'token_use': 'id'}, {'exp': datetime.now(UTC) - timedelta(seconds=1)}):
        with pytest.raises(HTTPException) as error:
            authenticator.verify(token(**changes))
        assert error.value.status_code == 401
