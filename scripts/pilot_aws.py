"""Use the approved project deployment role without saving temporary credentials."""
import json
import os
import subprocess
import sys

ROLE = 'arn:aws:iam::596104703378:role/central-brain-deployer'


def environment():
    response = subprocess.run(['aws', 'sts', 'assume-role', '--role-arn', ROLE,
        '--role-session-name', 'central-brain-deployment', '--duration-seconds', '3600',
        '--output', 'json'], text=True, capture_output=True, check=True)
    credentials = json.loads(response.stdout)['Credentials']
    return {**os.environ, 'AWS_ACCESS_KEY_ID': credentials['AccessKeyId'],
            'AWS_SECRET_ACCESS_KEY': credentials['SecretAccessKey'],
            'AWS_SESSION_TOKEN': credentials['SessionToken'],
            'AWS_DEFAULT_REGION': 'ca-central-1', 'AWS_PAGER': ''}


if __name__ == '__main__':
    result = subprocess.run(['aws', *sys.argv[1:]], env=environment(), check=False)
    raise SystemExit(result.returncode)
