"""Initialize the approved stack with isolated production credentials and a release."""
import hashlib
import json
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from pilot_aws import environment


def main():
    env = environment()

    def aws(*args, payload=None):
        command = ['aws', *args, '--output', 'json']
        with tempfile.NamedTemporaryFile(mode='w', dir='work/aws-release') as request:
            if payload is not None:
                json.dump(payload, request)
                request.flush()
                command += ['--cli-input-json', 'file://' + request.name]
            result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
            if result.returncode:
                denied = re.search(r'not authorized to perform: ([\w:*]+)', result.stderr)
                raise RuntimeError(f'AWS {args[0]} {args[1]} failed with code {result.returncode}. '
                                   + ('Missing permission: ' + denied[1] if denied else 'Request details were withheld.'))
        return json.loads(result.stdout) if result.stdout.strip() else {}

    stack = aws('cloudformation', 'describe-stacks', '--stack-name', 'central-brain-pilot')['Stacks'][0]
    assert stack['StackStatus'] in {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'}
    state = {o['OutputKey']: o['OutputValue'] for o in stack['Outputs']}
    Path('work/aws-release/state.json').write_text(json.dumps(state, indent=2))
    client = aws('cognito-idp', 'describe-user-pool-client', '--user-pool-id', state['UserPoolId'],
                 '--client-id', state['WebClientId'])['UserPoolClient']
    # An existing parameter must never be overwritten with newly generated passwords.
    existing = subprocess.run(['aws', 'ssm', 'get-parameter', '--name', '/central-brain/pilot/environment'],
                              env=env, text=True, capture_output=True, check=False)
    if existing.returncode == 0:
        print('Existing production configuration retained.')
    elif 'ParameterNotFound' in existing.stderr:
        admin, runtime, migrator = [secrets.token_urlsafe(32) for _ in range(3)]
        values = {
            'APP_IMAGE': 'central-brain:aws-pilot-20260910', 'PUBLIC_URL': state['PublicUrl'],
            'POSTGRES_PASSWORD': admin,
            'LOCAL_ADMIN_URL': f'postgresql://postgres:{admin}@postgres:5432/postgres',
            'DATABASE_URL': f'postgresql://central_brain_runtime:{runtime}@postgres:5432/central_brain',
            'MIGRATION_DATABASE_URL': f'postgresql://central_brain_migrator:{migrator}@postgres:5432/central_brain',
            'SESSION_SECRET': secrets.token_urlsafe(48), 'OAUTH_ISSUER': state['OAuthIssuer'],
            'OAUTH_HOSTED_DOMAIN': 'https://thermolio-central-brain-596104703378.auth.ca-central-1.amazoncognito.com',
            'OAUTH_CLIENT_IDS': json.dumps([state['WebClientId']]), 'OAUTH_PRINCIPALS_JSON': '{}',
            'OAUTH_SCOPE_PREFIX': state['PublicUrl'] + '/mcp/',
            'OAUTH_WEB_CLIENT_ID': state['WebClientId'], 'OAUTH_WEB_CLIENT_SECRET': client['ClientSecret'],
        }
        content = ''.join(f"{key}='{value}'\n" for key, value in values.items())
        aws('ssm', 'put-parameter', payload={'Name': '/central-brain/pilot/environment',
            'Type': 'SecureString', 'Tier': 'Standard', 'Value': content,
            'Tags': [{'Key': 'Project', 'Value': 'CentralBrain'}]})
        print('Stored unique production credentials in encrypted SSM Parameter Store.')
    else:
        raise RuntimeError('Unable to check the production configuration; no secrets were changed.')
    digest = hashlib.sha256(Path('work/aws-release/pilot.tar.gz').read_bytes()).hexdigest()
    exports = {'DATABASE_VOLUME_ID': state['DatabaseVolumeId'], 'BACKUP_BUCKET': state['BackupBucket'],
               'INSTANCE_ID': state['InstanceId'], 'RELEASE_SHA256': digest}
    script = '\n'.join(f"export {k}='{v}'" for k, v in exports.items()) + '\n'
    script += Path('deploy/prepare-host.sh').read_text()
    response = aws('ssm', 'send-command', payload={'InstanceIds': [state['InstanceId']],
        'DocumentName': 'AWS-RunShellScript', 'Comment': 'Install the approved Central Brain pilot.',
        'TimeoutSeconds': 600, 'Parameters': {'commands': [script], 'executionTimeout': ['1800']}})
    Path('work/aws-release/install-command.txt').write_text(response['Command']['CommandId'])
    print('Installation command:', response['Command']['CommandId'])


if __name__ == '__main__':
    main()
