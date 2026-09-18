# ruff: noqa: F401, F811
import json

import pytest
from fastapi import HTTPException
from test_library import library
from test_pilot import pilot

from central_brain.auth import AuthContext
from central_brain.note_proposals import propose_note


def call_propose_file(pilot, args):
    response = pilot.client.post('/mcp/', headers={
        'Authorization':'Bearer assistant', 'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':1,'method':'tools/call',
              'params':{'name':'propose_file','arguments':args}})
    assert response.status_code == 200
    return response.json()['result']


def test_identical_pending_note_retry_returns_existing_id(pilot, library):
    first = propose_note(library, pilot.auth, 'pending.md', 'Original note', 'chat:test')
    retry = propose_note(library, pilot.auth, 'pending.md', 'Original note', 'chat:test')
    assert retry['file_id'] == first['file_id']
    assert retry['outcome'] == 'already_exists'
    assert retry['needs_approval'] and not retry['created']


def test_conflicting_pending_note_is_not_overwritten(pilot, library):
    first = propose_note(library, pilot.auth, 'pending.md', 'Original note', 'chat:test')
    retry = propose_note(library, pilot.auth, 'pending.md', 'Changed note', 'chat:test')
    assert retry['file_id'] == first['file_id']
    assert retry['outcome'] == 'content_conflict'
    assert not retry['identical_content']
    again = propose_note(library, pilot.auth, 'pending.md', 'Original note', 'chat:test')
    assert again['identical_content']


def test_pending_note_metadata_is_not_disclosed_to_another_author(pilot, library):
    propose_note(library, pilot.auth, 'pending.md', 'Original note', 'chat:test')
    other = AuthContext(pilot.settings.principals()['colleague'])
    with pytest.raises(HTTPException) as error:
        propose_note(library, other, 'pending.md', 'Original note', 'chat:test')
    assert error.value.status_code == 409


def test_connector_retry_reports_pending_instead_of_generic_conflict(pilot, library):
    args = {'name':'repeat.md','content':'Original note','source_reference':'chat:test'}
    assert not call_propose_file(pilot, args).get('isError')
    result = call_propose_file(pilot, args)
    assert not result.get('isError')
    body = json.loads(result['content'][0]['text'])
    assert body['status'] == 'proposed'
    assert body['outcome'] == 'already_exists'


def test_connector_directs_large_text_originals_to_archive_upload(pilot):
    result = call_propose_file(pilot, {
        'name':'finished.md', 'content':'x' * 20001, 'source_reference':'claude-code:test'})
    assert not result.get('isError')
    body = json.loads(result['content'][0]['text'])
    assert '20,000-character' in body['error']
    assert 'propose_chat_archive' in body['error']
    assert 'prepare_chat_archive_upload' in body['error']
