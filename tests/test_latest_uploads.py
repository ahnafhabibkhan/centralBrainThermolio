# Imported fixtures are consumed by pytest through parameter names.
# ruff: noqa: F401, F811
import base64
import hashlib
from uuid import UUID

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from test_library import library
from test_pilot import pilot
from test_project_transfer import send

from central_brain.archive_transfer import OriginalManifest
from central_brain.archives import (
    ArchiveFile,
    approval_item,
    approve_batch,
    propose_archive,
    review_batch,
    review_suggestion,
)
from central_brain.auth import AuthContext
from central_brain.connector_upload import upload_chunk
from central_brain.pending_uploads import reconcile_pending_uploads
from central_brain.project_transfer import prepare_project


def prepare(pilot, library, originals, **kwargs):
    inventory = [OriginalManifest(name=name, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                 for name, data in originals.items()]
    return prepare_project(library, pilot.settings, pilot.auth, 'Projects/Upload test', 'Project originals.',
                           'Integration test', inventory, **kwargs)


def token(pilot, library, sid, index=0, kind='archive_file', **kwargs):
    with pilot.repo._connection(pilot.auth) as c:
        return approval_item(library, pilot.auth, c, kind, UUID(str(sid)), index if kind == 'archive_file' else None, **kwargs)


def payload(pilot, sid):
    with pilot.repo._connection(pilot.auth) as c:
        return c.execute('SELECT status,payload FROM central_brain.library_suggestions WHERE id=%s', (sid,)).fetchone()


def test_newer_missing_original_supersedes_old_ready_file_and_generated_notes(pilot, library):
    old = prepare(pilot, library, {'Files/same.txt': b'Old.'})
    assert send(pilot, old['uploads'][0], b'Old.').status_code == 200
    stale = token(pilot, library, old['id'])
    newer = prepare(pilot, library, {'Files/same.txt': b'New.'})
    assert newer['id'] != old['id']
    prior = payload(pilot, old['id'])
    assert prior['status'] == 'superseded'
    assert set(prior['payload']['reviews'].values()) == {'superseded'}
    assert prior['payload']['files'][0]['sha256'] == hashlib.sha256(b'Old.').hexdigest()
    assert 'object_key' not in prior['payload']['files'][0]
    assert 'summary' not in prior['payload']
    with pytest.raises(HTTPException) as stale_error:
        approve_batch(library, pilot.auth, [stale])
    assert stale_error.value.status_code == 409
    with pytest.raises(HTTPException):
        token(pilot, library, newer['id'])
    assert send(pilot, old['uploads'][0], b'Old.').status_code == 409
    missing = token(pilot, library, newer['id'], require_ready=False)
    assert review_batch(library, pilot.auth, [missing], approve=False) == 1
    assert library.snapshot(pilot.auth) == []


def test_newest_rule_uses_complete_case_insensitive_destination_path(pilot, library):
    old = prepare(pilot, library, {'One/same.txt': b'Old.', 'Two/same.txt': b'Other.'}, summary_filename='old.md')
    for upload in old['uploads']:
        assert send(pilot, upload, b'Old.' if upload['file_index'] == 0 else b'Other.').status_code == 200
    new = prepare(pilot, library, {'one/SAME.txt': b'New.'}, summary_filename='new.md')
    assert payload(pilot, old['id'])['payload']['reviews']['0'] == 'superseded'
    assert token(pilot, library, old['id'], 1)['index'] == 1
    assert send(pilot, new['uploads'][0], b'New.').status_code == 200
    assert approve_batch(library, pilot.auth, [token(pilot, library, old['id'], 1), token(pilot, library, new['id'])]) == 2
    paths = {row['path'] for row in library.snapshot(pilot.auth)}
    assert '/Projects/Upload test/Two/same.txt' in paths
    assert '/Projects/Upload test/one/SAME.txt' in paths


def test_newest_replacement_retains_active_identity_original_bytes_and_history(pilot, library):
    ids = []
    for number, data in enumerate([b'Original.', b'Changed.', b'Changed.']):
        prepared = prepare(pilot, library, {'Files/same.txt': data}, summary_filename=f'batch-{number}.md', destination_confirmed=True)
        assert send(pilot, prepared['uploads'][0], data).status_code == 200
        assert approve_batch(library, pilot.auth, [token(pilot, library, prepared['id'])]) == 1
        ids.append(next(row['id'] for row in library.snapshot(pilot.auth) if row['name'] == 'same.txt'))
    assert len(set(ids)) == 1
    with pilot.repo._connection(pilot.auth) as c:
        versions = c.execute('SELECT version,sha256 FROM central_brain.library_versions WHERE node_id=%s ORDER BY version', (ids[0],)).fetchall()
    assert versions == [{'version': i + 1, 'sha256': hashlib.sha256(data).hexdigest()} for i, data in enumerate([b'Original.', b'Changed.'])]
    for number, data in [(1, b'Original.'), (2, b'Changed.')]:
        with library.download(pilot.auth, ids[0], number)[1] as stream:
            assert stream.read() == data


def test_finalized_newer_archive_provenance_blocks_legacy_older_pending(pilot, library):
    old = prepare(pilot, library, {'same.txt': b'Old.'}, summary_filename='old.md')
    assert send(pilot, old['uploads'][0], b'Old.').status_code == 200
    old_payload = payload(pilot, old['id'])['payload']
    new = prepare(pilot, library, {'same.txt': b'New.'}, summary_filename='new.md')
    assert send(pilot, new['uploads'][0], b'New.').status_code == 200
    with pilot.repo._connection(pilot.auth) as c:
        review_suggestion(library, pilot.auth, new['id'], True, c, [])
        # Model a legacy proposal that was never reconciled before the newer approval.
        c.execute("UPDATE central_brain.library_suggestions SET payload=%s,status='proposed' WHERE id=%s", (Jsonb(old_payload), old['id']))
        reconcile_pending_uploads(library, pilot.auth, c)
    assert payload(pilot, old['id'])['payload']['reviews']['0'] == 'superseded'
    assert payload(pilot, new['id'])['status'] == 'approve'


def test_cross_actor_reconciliation_remains_within_visible_workspace_paths(pilot, library):
    old = prepare(pilot, library, {'same.txt': b'Old.'}, summary_filename='old.md')
    other = AuthContext(pilot.settings.principals()['colleague'])
    new = propose_archive(library, other, 'Projects/Upload test', 'New summary.', 'New source.',
                          [ArchiveFile(name='same.txt', content='New.')], True, 'new.md')
    with pilot.repo._connection(other) as c:
        reconcile_pending_uploads(library, other, c)
    assert payload(pilot, old['id'])['payload']['reviews']['0'] == 'superseded'
    outsider = AuthContext(pilot.settings.principals()['outsider'])
    with pilot.repo._connection(outsider) as c:
        assert reconcile_pending_uploads(library, outsider, c) == 0
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s', (new['id'],)).fetchone()
    private = library.folder(pilot.auth, 'Private', visibility='private')
    secret = propose_archive(library, pilot.auth, 'Private', 'Private summary.', 'Source.', [ArchiveFile(name='secret.txt', content='Secret.')], True)
    with pilot.repo._connection(other) as c:
        assert not c.execute('SELECT id FROM central_brain.library_suggestions WHERE id=%s', (secret['id'],)).fetchone()
        assert not c.execute('SELECT id FROM central_brain.library_nodes WHERE id=%s', (private,)).fetchone()


def test_repeated_direct_pending_upload_invalidates_stale_token(pilot, library):
    node = library.upload(pilot.auth, 'same.txt', b'Old.', proposed=True)
    stale = token(pilot, library, node, kind='node')
    assert library.upload(pilot.auth, 'same.txt', b'New.', proposed=True) == node
    with pytest.raises(HTTPException):
        approve_batch(library, pilot.auth, [stale])
    assert approve_batch(library, pilot.auth, [token(pilot, library, node, kind='node')]) == 1
    with library.download(pilot.auth, node)[1] as stream:
        assert stream.read() == b'New.'


def test_direct_pending_replacement_does_not_expose_unapproved_bytes(pilot, library):
    active = library.upload(pilot.auth, 'same.txt', b'Approved.')
    pending = library.upload(pilot.auth, 'same.txt', b'Proposed.', proposed=True)
    assert active != pending
    with library.download(pilot.auth, active)[1] as stream:
        assert stream.read() == b'Approved.'
    assert approve_batch(library, pilot.auth, [token(pilot, library, pending, kind='node')]) == 1
    assert [n['id'] for n in library.snapshot(pilot.auth) if n['name'] == 'same.txt'] == [active]
    with library.download(pilot.auth, active)[1] as stream:
        assert stream.read() == b'Proposed.'
    with library.download(pilot.auth, active, 1)[1] as stream:
        assert stream.read() == b'Approved.'


@pytest.mark.parametrize('newer_kind', ['archive', 'direct'])
def test_latest_upload_rule_crosses_archive_and_direct_sources(pilot, library, newer_kind):
    project = library.folder(pilot.auth, 'Project')
    def archive():
        return propose_archive(library, pilot.auth, 'Project', 'Summary.', 'Source.', [ArchiveFile(name='same.txt', content='Archive.')], True)
    if newer_kind == 'archive':
        node = library.upload(pilot.auth, 'same.txt', b'Direct.', project, proposed=True)
        proposal = archive()
        with pytest.raises(HTTPException):
            token(pilot, library, node, kind='node')
        assert token(pilot, library, proposal['id'])['index'] == 0
    else:
        proposal = archive()
        node = library.upload(pilot.auth, 'same.txt', b'Direct.', project, proposed=True)
        assert payload(pilot, proposal['id'])['payload']['reviews']['0'] == 'superseded'
        assert token(pilot, library, node, kind='node')['id'] == str(node)


def test_superseded_partial_transfer_cannot_resume_chunk_upload(pilot, library):
    old = prepare(pilot, library, {'same.txt': b'Old.', 'other.txt': b'Other.'}, summary_filename='old.md')
    prepare(pilot, library, {'same.txt': b'New.'}, summary_filename='new.md')
    with pytest.raises(HTTPException) as error:
        upload_chunk(library, pilot.auth, old['id'], 0, 0, base64.b64encode(b'Old.').decode())
    assert error.value.status_code == 409


@pytest.mark.parametrize('update_active', [False, True])
def test_newer_active_upload_supersedes_an_older_pending_archive(pilot, library, update_active):
    folder = library.folder(pilot.auth, 'Project')
    active = library.upload(pilot.auth, 'same.txt', b'Initial.', folder) if update_active else None
    old = propose_archive(library, pilot.auth, 'Project', 'Summary.', 'Source.',
                          [ArchiveFile(name='same.txt', content='Older pending.')], True)
    stale = token(pilot, library, old['id'])
    active = library.upload(pilot.auth, 'same.txt', b'Latest direct.', folder, node_id=active)
    with pytest.raises(HTTPException):
        approve_batch(library, pilot.auth, [stale])
    assert payload(pilot, old['id'])['payload']['reviews']['0'] == 'superseded'
    with library.download(pilot.auth, active)[1] as stream:
        assert stream.read() == b'Latest direct.'


def test_legacy_active_version_does_not_override_proposal_time(pilot, library):
    folder = library.folder(pilot.auth, 'Project')
    old = propose_archive(library, pilot.auth, 'Project', 'Summary.', 'Source.',
                          [ArchiveFile(name='same.txt', content='Pending replacement.')], True)
    old_payload = payload(pilot, old['id'])['payload']
    active = library.upload(pilot.auth, 'same.txt', b'Legacy active.', folder)
    with pilot.repo._connection(pilot.auth) as c:
        c.execute('UPDATE central_brain.library_versions SET source_created_at=NULL,source_upload_id=NULL WHERE node_id=%s', (active,))
        c.execute("UPDATE central_brain.library_suggestions SET payload=%s,status='proposed' WHERE id=%s", (Jsonb(old_payload), old['id']))
    assert approve_batch(library, pilot.auth, [token(pilot, library, old['id'])]) == 1
    with library.download(pilot.auth, active)[1] as stream:
        assert stream.read() == b'Pending replacement.'


def test_partial_supersession_keeps_storage_reserved_and_receipts_only_require_pending(pilot, library):
    old = prepare(pilot, library, {'same.txt': b'Old.', 'other.txt': b'Other.'}, summary_filename='old.md')
    prepare(pilot, library, {'same.txt': b'New.'}, summary_filename='new.md')
    with pilot.repo._connection(pilot.auth) as c:
        reservation = c.execute('SELECT size_bytes FROM central_brain.transfer_reservations WHERE id=%s', (old['id'],)).fetchone()
    assert reservation['size_bytes'] == len(b'Old.Other.')
    result = send(pilot, old['uploads'][1], b'Other.')
    assert result.status_code == 200 and result.json()['remaining'] == []


def test_expired_legacy_resume_keeps_reconciled_states_and_only_pending_upload_tokens(pilot, library):
    old = prepare(pilot, library, {'same.txt': b'Old.', 'other.txt': b'Other.'}, summary_filename='old.md')
    old_payload = payload(pilot, old['id'])['payload']
    prepare(pilot, library, {'same.txt': b'New.'}, summary_filename='new.md')
    old_payload['transfer_expires'] = 1
    with pilot.repo._connection(pilot.auth) as c:
        c.execute('UPDATE central_brain.library_suggestions SET payload=%s WHERE id=%s', (Jsonb(old_payload), old['id']))
    resumed = prepare(pilot, library, {'same.txt': b'Old.', 'other.txt': b'Other.'}, summary_filename='old.md')
    assert [item['name'] for item in resumed['uploads']] == ['other.txt']
    assert payload(pilot, old['id'])['payload']['reviews']['0'] == 'superseded'


def test_whole_archive_and_per_file_replacement_share_same_review_rules(pilot, library):
    for content in ['Original.', 'Replacement.']:
        proposal = propose_archive(library, pilot.auth, 'Project', 'Summary ' + content, 'Source.',
                                   [ArchiveFile(name='same.txt', content=content)], True)
        with pilot.repo._connection(pilot.auth) as c:
            review_suggestion(library, pilot.auth, proposal['id'], True, c, [])
    files = [node for node in library.snapshot(pilot.auth) if node['name'] == 'same.txt']
    assert len(files) == 1
    with library.download(pilot.auth, files[0]['id'], 1)[1] as stream:
        assert stream.read() == b'Original.'
    with library.download(pilot.auth, files[0]['id'])[1] as stream:
        assert stream.read() == b'Replacement.'


def test_identical_direct_approval_retains_newest_provenance_without_duplicate_version(pilot, library):
    folder = library.folder(pilot.auth, 'Project')
    active = library.upload(pilot.auth, 'same.txt', b'Identical.', folder)
    old = propose_archive(library, pilot.auth, 'Project', 'Summary.', 'Source.',
                          [ArchiveFile(name='same.txt', content='Stale content.')], True)
    old_payload = payload(pilot, old['id'])['payload']
    pending = library.upload(pilot.auth, 'same.txt', b'Identical.', folder, proposed=True)
    assert approve_batch(library, pilot.auth, [token(pilot, library, pending, kind='node')]) == 1
    with pilot.repo._connection(pilot.auth) as c:
        c.execute("UPDATE central_brain.library_suggestions SET payload=%s,status='proposed' WHERE id=%s", (Jsonb(old_payload), old['id']))
        reconcile_pending_uploads(library, pilot.auth, c)
        assert c.execute('SELECT count(*) AS n FROM central_brain.library_versions WHERE node_id=%s', (active,)).fetchone()['n'] == 1
    assert payload(pilot, old['id'])['payload']['reviews']['0'] == 'superseded'
