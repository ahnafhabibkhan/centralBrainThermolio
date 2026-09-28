from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException

from test_pilot import pilot
from central_brain.auth import AuthContext
from central_brain.config import Principal
from central_brain.invitations import CognitoInvitationService


class FakeRepository:
    def __init__(self, events):
        self.events = events

    def register_oauth_identity(self, auth, subject):
        auth.require("admin")
        self.events.append(("map", subject))


class FakeCognito:
    def __init__(self, events, users=None):
        self.events = events
        self.users = users or []

    def list_users(self, **kwargs):
        self.events.append(("list", kwargs))
        return {"Users": self.users}

    def admin_create_user(self, **kwargs):
        self.events.append(("create", kwargs))
        if kwargs.get("MessageAction") == "SUPPRESS":
            return {"User": {
                "Username": "generated-name",
                "UserStatus": "FORCE_CHANGE_PASSWORD",
                "Attributes": [{"Name": "sub", "Value": "new-subject"}],
            }}
        return {}

    def admin_update_user_attributes(self, **kwargs):
        self.events.append(("verify", kwargs))


def admin_auth(admin=True):
    roles = {"reader", "writer", "reviewer"}
    if admin:
        roles.add("admin")
    return AuthContext(Principal(workspace_id=uuid4(), actor_id=uuid4(), roles=roles))


def service(client, events):
    settings = SimpleNamespace(oauth_user_pool_id="pool", aws_region="ca-central-1")
    return CognitoInvitationService(settings, FakeRepository(events), client)


def test_new_user_is_mapped_before_invitation_is_sent():
    events = []
    result = service(FakeCognito(events), events).invite(admin_auth(), " Person@Example.com ")

    assert result.sent and not result.existing
    assert events[0][0] == "list"
    assert events[0][1]["Filter"] == 'email = "person@example.com"'
    assert events[1][1]["MessageAction"] == "SUPPRESS"
    assert {item["Name"]: item["Value"] for item in events[1][1]["UserAttributes"]}[
        "email_verified"
    ] == "true"
    assert events[2] == ("map", "new-subject")
    assert events[3][1]["MessageAction"] == "RESEND"
    assert events[3][1]["Username"] == "person@example.com"


def test_pending_user_is_verified_mapped_and_reinvited():
    events = []
    user = {"Username": "existing", "UserStatus": "FORCE_CHANGE_PASSWORD",
            "Attributes": [{"Name": "sub", "Value": "existing-subject"}]}
    result = service(FakeCognito(events, [user]), events).invite(admin_auth(), "person@example.com")

    assert result.sent and result.existing
    assert [event[0] for event in events] == ["list", "verify", "map", "create"]
    assert events[-1][1]["Username"] == "person@example.com"


def test_active_user_is_mapped_without_duplicate_email():
    events = []
    user = {"Username": "existing", "UserStatus": "CONFIRMED",
            "Attributes": [{"Name": "sub", "Value": "existing-subject"}]}
    result = service(FakeCognito(events, [user]), events).invite(admin_auth(), "person@example.com")

    assert not result.sent and result.existing
    assert [event[0] for event in events] == ["list", "verify", "map"]


@pytest.mark.parametrize("email", ["missing-at.example", "name@example", 'bad"@example.com'])
def test_invalid_email_is_rejected_before_cognito(email):
    events = []
    with pytest.raises(HTTPException) as error:
        service(FakeCognito(events), events).invite(admin_auth(), email)
    assert error.value.status_code == 422
    assert events == []


def test_non_admin_cannot_invite():
    events = []
    with pytest.raises(HTTPException) as error:
        service(FakeCognito(events), events).invite(admin_auth(False), "person@example.com")
    assert error.value.status_code == 403
    assert events == []


def test_runtime_registers_invitation_identity_without_update_privilege(pilot):
    subject = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(
            lambda _: pilot.repo.register_oauth_identity(pilot.auth, subject), range(2)
        ))
    assert identities[0] == identities[1]
    assert identities[0].roles == {'reader', 'writer', 'reviewer'}
    assert pilot.repo.oauth_principal(subject) == identities[0]
    with pilot.repo._connection(pilot.auth) as connection:
        count = connection.execute(
            "SELECT count(*) AS n FROM central_brain.audit_events "
            "WHERE action='user.invite' AND resource_id=%s", (identities[0].actor_id,),
        ).fetchone()['n']
    assert count == 1
