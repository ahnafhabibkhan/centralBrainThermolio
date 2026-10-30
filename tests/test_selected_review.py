# ruff: noqa: F401, F811
import hashlib
import json
import re
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_library import library
from test_pilot import csrf, memory, pilot, production_settings

from central_brain.api import create_app
from central_brain.archive_transfer import OriginalManifest, prepare_transfer
from central_brain.archives import ArchiveFile, approval_item, propose_archive
from central_brain.manage import seed_identity


def sign_in(pilot, token='owner'):
    login = pilot.client.get('/login')
    response = pilot.client.post('/login', data={'token': token, 'csrf_token': csrf(login)},
                                 follow_redirects=False)
    assert response.status_code == 303
    page = pilot.client.get('/library')
    assert page.status_code == 200
    return csrf(page)


def tokens(library, auth, selections):
    with library.repo._connection(auth) as c:
        return [approval_item(library, auth, c, kind, uid, index, require_ready=False)
                for kind, uid, index in selections]


def submit(pilot, selected, csrf_token, action='approve', **kwargs):
    return pilot.client.post('/library/review-selected', data={
        'items': json.dumps(selected), 'action': action, 'csrf_token': csrf_token,
    }, headers={'Accept': 'application/json'}, **kwargs)


def test_selected_review_approves_only_checked_snapshot(pilot, library):
    first = library.upload(pilot.auth, 'first.txt', b'First original.', proposed=True)
    unchecked = library.upload(pilot.auth, 'unchecked.txt', b'Unchecked original.', proposed=True)
    token = sign_in(pilot)
    selected = tokens(library, pilot.auth, [('node', first, None)])
    later = library.upload(pilot.auth, 'later.txt', b'New arrival.', proposed=True)
    response = submit(pilot, selected, token)
    assert response.status_code == 200 and response.json() == {'approved': 1}
    assert response.headers['Cache-Control'] == 'no-store'
    assert library.info(pilot.auth, first)['status'] == 'active'
    assert library.info(pilot.auth, unchecked, True)['status'] == 'proposed'
    assert library.info(pilot.auth, later, True)['status'] == 'proposed'


def test_selected_review_rejects_missing_original_and_pending_memory(pilot, library):
    content = b'Original bytes.' * 20000
    manifest = [OriginalManifest(name='missing.txt', size_bytes=len(content),
                                sha256=hashlib.sha256(content).hexdigest())]
    receipt = prepare_transfer(library, pilot.settings, pilot.auth, 'Projects/Missing',
                               'Summary.', 'Test source.', manifest)
    sid = UUID(str(receipt['id']))
    mid = pilot.repo.create(pilot.auth, memory()).memory_id
    token = sign_in(pilot)
    selected = tokens(library, pilot.auth, [('archive_file', sid, 0), ('node', mid, None)])
    blocked = submit(pilot, selected, token)
    assert blocked.status_code == 409
    assert pilot.repo.get(pilot.auth, mid).status == 'proposed'
    rejected = submit(pilot, selected, token, 'reject')
    assert rejected.status_code == 200 and rejected.json() == {'rejected': 2}
    assert pilot.repo.get(pilot.auth, mid).status == 'rejected'
    with library.repo._connection(pilot.auth) as c:
        payload = c.execute('SELECT payload FROM central_brain.library_suggestions WHERE id=%s',
                            (sid,)).fetchone()['payload']
        assert payload['reviews']['0'] == 'rejected'
        assert not c.execute('SELECT id FROM central_brain.transfer_reservations WHERE id=%s',
                             (sid,)).fetchone()


@pytest.mark.parametrize('action', ['approve', 'reject'])
def test_selected_review_stale_fingerprint_rolls_back_entire_group(pilot, library, action):
    mid = pilot.repo.create(pilot.auth, memory()).memory_id
    node = library.upload(pilot.auth, 'unchanged.txt', b'Unchanged original.', proposed=True)
    token = sign_in(pilot)
    selected = tokens(library, pilot.auth, [('node', node, None), ('node', mid, None)])
    pilot.repo.edit_proposal(pilot.auth, mid, memory().model_copy(update={'content': 'Changed.'}))
    response = submit(pilot, selected, token, action)
    assert response.status_code == 409
    assert library.info(pilot.auth, node, True)['status'] == 'proposed'
    assert pilot.repo.get(pilot.auth, mid).status == 'proposed'


def test_selected_review_preserves_session_csrf_origin_and_workspace_access(pilot, library):
    node = library.upload(pilot.auth, 'private.txt', b'Private original.', proposed=True,
                          visibility='private')
    selected = tokens(library, pilot.auth, [('node', node, None)])
    assert submit(pilot, selected, 'unused', follow_redirects=False).status_code == 303
    login = pilot.client.get('/login')
    denied = pilot.client.post('/login', data={'token': 'assistant', 'csrf_token': csrf(login)},
                              follow_redirects=False)
    assert denied.status_code in {200, 403}
    assert submit(pilot, selected, 'unused', follow_redirects=False).status_code == 303
    token = sign_in(pilot)
    assert submit(pilot, selected, 'wrong').status_code == 403
    response = pilot.client.post('/library/review-selected', data={
        'items': json.dumps(selected), 'action': 'approve', 'csrf_token': token,
    }, headers={'Origin': 'https://untrusted.example'})
    assert response.status_code == 403
    colleague_token = sign_in(pilot, 'colleague')
    assert submit(pilot, selected, colleague_token).status_code in {404, 409}
    principals = pilot.settings.principals()
    outsider = principals['outsider'].model_copy(update={'actor_id': uuid4()})
    principals['outsider'] = outsider
    pilot.settings.central_brain_principals_json = json.dumps({
        name: principal.model_dump(mode='json') for name, principal in principals.items()
    })
    with psycopg.connect(pilot.migration) as c:
        c.execute('SET ROLE central_brain_owner')
        seed_identity(c, outsider.workspace_id, outsider.actor_id, 'Other workspace')
    outsider_token = sign_in(pilot, 'outsider')
    assert submit(pilot, selected, outsider_token).status_code in {404, 409}
    assert library.info(pilot.auth, node, True)['status'] == 'proposed'


@pytest.mark.parametrize('action', ['approve', 'reject'])
def test_individual_review_rejects_old_page_after_replacement(pilot, library, action):
    node = library.upload(pilot.auth, 'same.txt', b'Old original.', proposed=True)
    token = sign_in(pilot)
    old_page = pilot.client.get(f'/library/file/{node}')
    fingerprint = old_page.context['review_fingerprint']
    updated = library.upload(pilot.auth, 'same.txt', b'New original.', proposed=True)
    assert updated == node
    stale = pilot.client.post(f'/library/file/{node}/review', data={
        'action': action, 'csrf_token': token, 'fingerprint': fingerprint,
    }, follow_redirects=False)
    assert stale.status_code == 409
    assert library.info(pilot.auth, node, True)['status'] == 'proposed'
    historical = pilot.client.get(f'/library/file/{node}?version=1')
    assert historical.context['review_fingerprint'] is None
    assert 'Approve file' not in historical.text
    current = pilot.client.get(f'/library/file/{node}')
    accepted = pilot.client.post(f'/library/file/{node}/review', data={
        'action': action, 'csrf_token': token,
        'fingerprint': current.context['review_fingerprint'],
    }, follow_redirects=False)
    assert accepted.status_code == 303
    assert library.info(pilot.auth, node, True)['status'] == ('active' if action == 'approve' else 'rejected')


def test_selected_review_validates_action_selection_and_bound(pilot, library):
    node = library.upload(pilot.auth, 'pending.txt', b'Pending original.', proposed=True)
    token = sign_in(pilot)
    selected = tokens(library, pilot.auth, [('node', node, None)])
    assert submit(pilot, selected, token, 'delete').status_code == 422
    assert submit(pilot, [], token).status_code == 422
    assert submit(pilot, selected * 2, token).status_code == 422
    assert submit(pilot, selected * 151, token).status_code == 422
    invalid = dict(selected[0], unexpected=True)
    assert submit(pilot, [invalid], token).status_code == 422
    response = pilot.client.post('/library/review-selected', data={
        'items': 'x' * 40001, 'action': 'reject', 'csrf_token': token,
    })
    assert response.status_code == 413
    assert library.info(pilot.auth, node, True)['status'] == 'proposed'


def test_review_queue_has_tokens_for_every_displayed_row_and_no_older_duplicate(pilot, library):
    older = propose_archive(library, pilot.auth, 'Projects/Latest', 'Old summary.', 'Source.',
                            [ArchiveFile(name='same.txt', content='Old original.')])
    newer = propose_archive(library, pilot.auth, 'Projects/Latest', 'New summary.', 'Source.',
                            [ArchiveFile(name='same.txt', content='New original.')], True,
                            summary_filename='new.md')
    sign_in(pilot)
    page = pilot.client.get('/library')
    keys = re.findall(r'data-review-key="([^"]+)"', page.text)
    expected = {f"{item['kind']}:{item['id']}:{item.get('index', '')}"
                for item in page.context['review_items']}
    assert set(keys) == expected
    assert len(keys) == len(expected)
    assert f"archive_file:{older['id']}:0" not in expected
    assert f"archive_file:{newer['id']}:0" in expected


def test_review_queue_limits_rows_and_tokens_together(pilot, library):
    for batch, count in enumerate((100, 60)):
        manifest = [OriginalManifest(name=f'file-{batch}-{i}.txt', size_bytes=1,
                                    sha256=hashlib.sha256(b'x').hexdigest()) for i in range(count)]
        prepare_transfer(library, pilot.settings, pilot.auth, 'Projects/Large queue',
                         'Summary.', 'Source.', manifest, True, summary_filename=f'batch-{batch}.md')
    sign_in(pilot)
    page = pilot.client.get('/library')
    keys = re.findall(r'data-review-key="([^"]+)"', page.text)
    expected = {f"{item['kind']}:{item['id']}:{item.get('index', '')}"
                for item in page.context['review_items']}
    assert len(keys) == len(expected) == 150
    assert set(keys) == expected
    assert page.context['review_queue_truncated']


def test_selected_review_and_legacy_approval_share_bounded_rate_allowance(pilot, monkeypatch):
    monkeypatch.setattr('central_brain.api.monotonic', lambda: 1000.0)
    monkeypatch.setattr('central_brain.api.MIN_LIBRARY_REVIEW_REQUESTS_PER_MINUTE', 3)
    app = create_app(pilot.repo, production_settings(pilot, requests_per_minute=2))
    with TestClient(app, base_url='https://brain.example', follow_redirects=False) as client:
        assert client.get('/').status_code == 303
        assert client.get('/').status_code == 303
        assert client.get('/').status_code == 429
        for path in ['/library/approve-all', '/library/review-selected', '/library/review-selected']:
            response = client.post(path, data={'items': '[]', 'action': 'reject', 'csrf_token': 'x'})
            assert response.status_code == 303 and response.headers['location'] == '/login'
        limited = client.post('/library/review-selected', data={
            'items': '[]', 'action': 'reject', 'csrf_token': 'x',
        })
        assert limited.status_code == 429 and limited.headers['Retry-After'] == '20'
