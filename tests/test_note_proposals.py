from test_pilot import pilot
from test_library import library
from central_brain.note_proposals import propose_note


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
    import pytest
    from fastapi import HTTPException
    from central_brain.auth import AuthContext
    propose_note(library, pilot.auth, 'pending.md', 'Original note', 'chat:test')
    other = AuthContext(pilot.settings.principals()['colleague'])
    with pytest.raises(HTTPException) as error:
        propose_note(library, other, 'pending.md', 'Original note', 'chat:test')
    assert error.value.status_code == 409


def test_connector_retry_reports_pending_instead_of_generic_conflict(pilot, library):
    args = {'name':'repeat.md','content':'Original note','source_reference':'chat:test'}
    def call():
        response = pilot.client.post('/mcp/', headers={'Authorization':'Bearer assistant','Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'propose_file','arguments':args}})
        assert response.status_code == 200
        return response.json()['result']
    assert not call().get('isError')
    result = call()
    assert not result.get('isError')
    import json
    body = json.loads(result['content'][0]['text'])
    assert body['status'] == 'proposed'
    assert body['outcome'] == 'already_exists'
