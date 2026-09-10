"""Invite one approved Cognito user and map the immutable subject to the pilot workspace."""
import argparse
import io
import json
import subprocess
import tempfile

from dotenv import dotenv_values
from pilot_aws import environment

WORKSPACE_ID = "a22cdb8e-6c0d-4b59-b292-a4e5593156c1"
ACTOR_ID = "f1aa197b-4291-4d81-a00c-cab55a1ceff0"
POOL_ID = "ca-central-1_T8tjnzjRh"
PARAMETER = "/central-brain/pilot/environment"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    args = parser.parse_args()
    email = args.email.strip().lower()
    if "@" not in email or any(character.isspace() for character in email):
        raise ValueError("A valid approved email address is required")
    aws_environment = environment()

    def aws(*arguments, payload=None):
        command = ["aws", *arguments, "--output", "json"]
        with tempfile.NamedTemporaryFile(mode="w") as request:
            if payload is not None:
                json.dump(payload, request)
                request.flush()
                command.extend(["--cli-input-json", "file://" + request.name])
            result = subprocess.run(command, env=aws_environment, text=True, capture_output=True,
                                    check=True)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    matches = aws("cognito-idp", "list-users", "--user-pool-id", POOL_ID,
                  "--filter", f'email = "{email}"')["Users"]
    if not matches:
        aws("cognito-idp", "admin-create-user", payload={
            "UserPoolId": POOL_ID,
            "Username": email,
            "UserAttributes": [{"Name": "email", "Value": email}],
            "DesiredDeliveryMediums": ["EMAIL"],
        })
        matches = aws("cognito-idp", "list-users", "--user-pool-id", POOL_ID,
                      "--filter", f'email = "{email}"')["Users"]
        print("Cognito invitation sent to the approved address.")
    else:
        print("Existing Cognito user retained; no duplicate invitation was sent.")
    if len(matches) != 1:
        raise RuntimeError("The approved address did not resolve to exactly one Cognito user")
    attributes = {item["Name"]: item["Value"] for item in matches[0]["Attributes"]}
    subject = attributes["sub"]

    parameter = aws("ssm", "get-parameter", "--name", PARAMETER, "--with-decryption")
    content = parameter["Parameter"]["Value"]
    values = dotenv_values(stream=io.StringIO(content))
    principals = json.loads(values.get("OAUTH_PRINCIPALS_JSON", "{}"))
    principals[subject] = {
        "workspace_id": WORKSPACE_ID,
        "actor_id": ACTOR_ID,
        "roles": ["reader", "writer", "reviewer", "admin"],
        "sensitivities": ["public", "internal"],
    }
    replacement = "OAUTH_PRINCIPALS_JSON='" + json.dumps(principals, separators=(",", ":")) + "'"
    updated = "\n".join(replacement if line.startswith("OAUTH_PRINCIPALS_JSON=") else line
                        for line in content.splitlines()) + "\n"
    aws("ssm", "put-parameter", payload={"Name": PARAMETER, "Type": "SecureString",
                                          "Value": updated, "Overwrite": True})
    print("The immutable Cognito subject is authorized for the pilot review workspace.")


if __name__ == "__main__":
    main()
