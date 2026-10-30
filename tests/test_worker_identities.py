import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from test_library import library as library_fixture
from test_pilot import pilot as pilot_fixture

from central_brain.auth import AuthContext
from central_brain.library_worker import WorkerIdentities, process_cycle
from central_brain.manage import seed_identity

library = library_fixture
pilot = pilot_fixture


def worker_settings(*principals):
    return SimpleNamespace(
        environment="production",
        oauth_principals=lambda: {str(i): p for i, p in enumerate(principals)},
    )


def test_worker_refreshes_invites_after_startup_and_maintains_private_files(
    pilot, library, monkeypatch
):
    clock = [10.0]
    monkeypatch.setattr("central_brain.library_worker.monotonic", lambda: clock[0])
    identities = WorkerIdentities(pilot.repo, worker_settings(pilot.auth.principal))
    assert not process_cycle(library, identities)

    invited = AuthContext(pilot.repo.register_oauth_identity(pilot.auth, str(uuid4())))
    node = library.upload(invited, "Private.txt", b"Invited private report.", visibility="private")
    assert not process_cycle(library, identities)
    assert library.read(invited, node)["state"] == "queued"

    clock[0] += 30
    assert process_cycle(library, identities)
    assert library.read(invited, node)["sections"][0]["content"] == "Invited private report."
    with pytest.raises(HTTPException) as hidden:
        library.read(pilot.auth, node)
    assert hidden.value.status_code == 404
    assert all(p.roles == {"reader", "writer"} for p in identities.current())

    plan = library.deletion_plan(invited, node)
    keys = [version["object_key"] for version in plan["versions"]]
    library.delete(invited, node, plan["root"]["name"], plan["token"])
    assert all((Path(library.settings.library_local_path) / key).exists() for key in keys)
    assert process_cycle(library, identities)
    assert all(not (Path(library.settings.library_local_path) / key).exists() for key in keys)


def test_discovery_excludes_unconfigured_workspaces(pilot, library):
    workspace, actor = uuid4(), uuid4()
    with psycopg.connect(pilot.migration) as connection:
        connection.execute("SET ROLE central_brain_owner")
        seed_identity(connection, workspace, actor, "Unconfigured worker workspace")
    other_admin = AuthContext(pilot.auth.principal.model_copy(
        update={"workspace_id": workspace, "actor_id": actor}
    ))
    invited = AuthContext(pilot.repo.register_oauth_identity(other_admin, str(uuid4())))
    node = library.upload(invited, "Unrelated.txt", b"Unrelated private report.", visibility="private")
    identities = WorkerIdentities(pilot.repo, worker_settings(pilot.auth.principal))

    assert pilot.repo.workspace_oauth_principals(pilot.auth) == []
    assert {p.workspace_id for p in identities.current()} == {pilot.auth.principal.workspace_id}
    assert not process_cycle(library, identities)
    assert library.read(invited, node)["state"] == "queued"


def test_non_admin_configuration_cannot_discover_other_identities(pilot):
    invited = pilot.repo.register_oauth_identity(pilot.auth, str(uuid4()))
    non_admin = pilot.auth.principal.model_copy(update={"roles": {"reader", "writer"}})
    with pytest.raises(HTTPException) as denied:
        pilot.repo.workspace_oauth_principals(AuthContext(non_admin))
    assert denied.value.status_code == 403
    identities = WorkerIdentities(pilot.repo, worker_settings(non_admin))
    assert {p.actor_id for p in identities.current()} == {non_admin.actor_id}
    assert invited.actor_id not in {p.actor_id for p in identities.current()}


def test_configured_identity_overrides_discovered_identity_and_local_mode_is_preserved(pilot):
    invited = pilot.repo.register_oauth_identity(pilot.auth, str(uuid4()))
    configured = invited.model_copy(update={"sensitivities": ["public"]})
    settings = pilot.settings.model_copy(update={
        "central_brain_principals_json": json.dumps({
            "owner": pilot.auth.principal.model_dump(mode="json"),
            "configured": configured.model_dump(mode="json"),
            "alias": configured.model_dump(mode="json"),
        })
    })
    current = WorkerIdentities(pilot.repo, settings).current()
    assert len(current) == 2
    assert next(p for p in current if p.actor_id == invited.actor_id).sensitivities == ["public"]


def test_admin_alias_remains_discovery_anchor_after_lower_permission_alias(pilot):
    invited = pilot.repo.register_oauth_identity(pilot.auth, str(uuid4()))
    assistant = pilot.auth.principal.model_copy(update={
        "roles": {"reader", "writer"}, "sensitivities": ["public"],
    })
    settings = pilot.settings.model_copy(update={
        "central_brain_principals_json": json.dumps({
            "reviewer": pilot.auth.principal.model_dump(mode="json"),
            "assistant": assistant.model_dump(mode="json"),
        })
    })
    identities = WorkerIdentities(pilot.repo, settings)
    current = identities.current()
    assert {p.actor_id for p in current} == {pilot.auth.principal.actor_id, invited.actor_id}
    assert identities.administrators == {pilot.auth.principal.workspace_id: pilot.auth.principal}
    configured = next(p for p in current if p.actor_id == assistant.actor_id)
    assert configured.roles == {"reader", "writer"}
    assert configured.sensitivities == ["public"]


def test_refresh_drops_revoked_identities_and_retries_after_failure(pilot, monkeypatch, caplog):
    clock = [10.0]
    monkeypatch.setattr("central_brain.library_worker.monotonic", lambda: clock[0])
    invited = pilot.repo.register_oauth_identity(pilot.auth, str(uuid4()))
    identities = WorkerIdentities(pilot.repo, worker_settings(pilot.auth.principal))
    assert invited.actor_id in {p.actor_id for p in identities.current()}

    original = pilot.repo.workspace_oauth_principals
    def unavailable(auth):
        raise RuntimeError("The identity lookup is temporarily unavailable.")
    monkeypatch.setattr(pilot.repo, "workspace_oauth_principals", unavailable)
    clock[0] += 30
    assert {p.actor_id for p in identities.current()} == {pilot.auth.principal.actor_id}
    assert "Worker identity refresh failed" in caplog.text

    monkeypatch.setattr(pilot.repo, "workspace_oauth_principals", original)
    clock[0] += 30
    assert invited.actor_id in {p.actor_id for p in identities.current()}
    with psycopg.connect(pilot.migration) as connection:
        connection.execute("SET ROLE central_brain_owner")
        connection.execute(
            "UPDATE central_brain.oauth_identities SET roles=ARRAY['reader']::text[] WHERE actor_id=%s",
            (invited.actor_id,),
        )
    clock[0] += 30
    assert {p.actor_id for p in identities.current()} == {pilot.auth.principal.actor_id}


def test_discovered_administrator_does_not_expand_worker_scope(pilot):
    invited = pilot.repo.register_oauth_identity(pilot.auth, str(uuid4()))
    with psycopg.connect(pilot.migration) as connection:
        connection.execute("SET ROLE central_brain_owner")
        connection.execute(
            "UPDATE central_brain.oauth_identities SET roles=ARRAY['reader','writer','admin']::text[] "
            "WHERE actor_id=%s", (invited.actor_id,),
        )
    identities = WorkerIdentities(pilot.repo, worker_settings(pilot.auth.principal))
    assert len(identities.current()) == 2
    assert identities.administrators == {pilot.auth.principal.workspace_id: pilot.auth.principal}
    assert all("admin" not in p.roles for p in identities.current())


def test_failed_private_cleanup_does_not_starve_extraction(pilot, library, monkeypatch):
    invited = AuthContext(pilot.repo.register_oauth_identity(pilot.auth, str(uuid4())))
    removed = library.upload(invited, "Remove.txt", b"An original awaiting deletion.", visibility="private")
    plan = library.deletion_plan(invited, removed)
    library.delete(invited, removed, plan["root"]["name"], plan["token"])
    pending = library.upload(invited, "Pending.txt", b"A report awaiting extraction.", visibility="private")

    def unavailable(key):
        raise OSError("Object deletion is temporarily unavailable.")
    monkeypatch.setattr(library.store, "delete", unavailable)
    identities = WorkerIdentities(pilot.repo, worker_settings(pilot.auth.principal))
    assert process_cycle(library, identities)
    assert library.read(invited, pending)["sections"][0]["content"] == "A report awaiting extraction."
