import re
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

EMAIL = re.compile(
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9]"
    r"(?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


@dataclass(frozen=True)
class InvitationResult:
    sent: bool
    existing: bool


class CognitoInvitationService:
    def __init__(self, settings, repository, client=None):
        self.settings = settings
        self.repository = repository
        self.client = client

    @staticmethod
    def normalize_email(value):
        email = value.strip().lower()
        if len(email) > 254 or not EMAIL.fullmatch(email):
            raise HTTPException(422, "Enter a valid email address")
        return email

    @staticmethod
    def _attributes(user):
        return {item["Name"]: item["Value"] for item in user.get("Attributes", [])}

    def invite(self, auth, value):
        auth.require("admin")
        email = self.normalize_email(value)
        client = self.client or boto3.client("cognito-idp", region_name=self.settings.aws_region)
        try:
            users = client.list_users(
                UserPoolId=self.settings.oauth_user_pool_id,
                Filter=f'email = "{email}"',
                Limit=2,
            ).get("Users", [])
            if len(users) > 1:
                raise HTTPException(409, "The email resolved to more than one account")
            created = not users
            if created:
                user = client.admin_create_user(
                    UserPoolId=self.settings.oauth_user_pool_id,
                    Username=email,
                    MessageAction="SUPPRESS",
                    DesiredDeliveryMediums=["EMAIL"],
                    UserAttributes=[
                        {"Name": "email", "Value": email},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                )["User"]
            else:
                user = users[0]
                client.admin_update_user_attributes(
                    UserPoolId=self.settings.oauth_user_pool_id,
                    Username=user["Username"],
                    UserAttributes=[{"Name": "email_verified", "Value": "true"}],
                )
            subject = self._attributes(user).get("sub")
            if not subject:
                raise HTTPException(502, "Cognito did not return an account identifier")
            self.repository.register_oauth_identity(auth, subject)
            pending = user.get("UserStatus") == "FORCE_CHANGE_PASSWORD"
            if created or pending:
                client.admin_create_user(
                    UserPoolId=self.settings.oauth_user_pool_id,
                    Username=user["Username"],
                    MessageAction="RESEND",
                    DesiredDeliveryMediums=["EMAIL"],
                )
                return InvitationResult(sent=True, existing=not created)
            return InvitationResult(sent=False, existing=True)
        except HTTPException:
            raise
        except (BotoCoreError, ClientError) as exc:
            raise HTTPException(502, "Cognito could not send the invitation") from exc
