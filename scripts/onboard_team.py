"""Prepare separate team identities, then send invitations after the application is ready."""

import argparse
import io
import json
import re
import subprocess
import tempfile
from uuid import UUID, uuid5

from dotenv import dotenv_values
from pilot_aws import environment

POOL = 'ca-central-1_T8tjnzjRh'
WORKSPACE = 'a22cdb8e-6c0d-4b59-b292-a4e5593156c1'
PARAMETER = '/central-brain/pilot/environment'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'invite', 'verify'])
    parser.add_argument('emails', nargs='+')
    args = parser.parse_args()
    emails = sorted(set(e.strip().lower() for e in args.emails))
    if any(not re.fullmatch(r'[a-z0-9._+%-]+@[a-z0-9.-]+\.[a-z]{2,}', e) for e in emails):
        raise ValueError('Use valid approved email addresses.')
    env = environment()

    def aws(*arguments, payload=None):
        command = ['aws', *arguments, '--output', 'json']
        with tempfile.NamedTemporaryFile(mode='w') as request:
            if payload is not None:
                json.dump(payload, request)
                request.flush()
                command += ['--cli-input-json', 'file://' + request.name]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
        if result.returncode:
            code = re.search(r'\((\w+Exception)\)', result.stderr)
            raise RuntimeError(f'{arguments[0]} {arguments[1]} failed: {code[1] if code else "request rejected"}')
        return json.loads(result.stdout) if result.stdout.strip() else {}

    parameter = aws('ssm', 'get-parameter', '--name', PARAMETER, '--with-decryption')['Parameter']
    content = parameter['Value']
    values = dotenv_values(stream=io.StringIO(content))
    principals = json.loads(values['OAUTH_PRINCIPALS_JSON'])
    for email in emails:
        matches = aws('cognito-idp', 'list-users', '--user-pool-id', POOL, '--filter', f'email = "{email}"')['Users']
        if not matches and args.phase == 'prepare':
            user = aws('cognito-idp', 'admin-create-user', payload={
                'UserPoolId': POOL, 'Username': email, 'MessageAction': 'SUPPRESS',
                'DesiredDeliveryMediums': ['EMAIL'], 'UserAttributes': [{'Name': 'email', 'Value': email}],
            })['User']
        elif len(matches) == 1:
            user = matches[0]
        else:
            raise RuntimeError(f'{email} did not resolve to exactly one account.')
        attributes = {a['Name']: a['Value'] for a in user['Attributes']}
        subject = attributes['sub']
        if args.phase == 'prepare' and subject not in principals:
            principals[subject] = {'workspace_id': WORKSPACE,
                'actor_id': str(uuid5(UUID(WORKSPACE), subject)),
                'roles': ['reader', 'writer', 'reviewer'], 'sensitivities': ['public', 'internal']}
        if subject not in principals:
            raise RuntimeError(f'{email} is not mapped to the application.')
        if args.phase == 'invite':
            if user['UserStatus'] != 'FORCE_CHANGE_PASSWORD':
                print(json.dumps({'email': email, 'status': user['UserStatus'], 'invitation': 'not required'}))
                continue
            aws('cognito-idp', 'admin-create-user', payload={
                'UserPoolId': POOL, 'Username': email, 'MessageAction': 'RESEND', 'DesiredDeliveryMediums': ['EMAIL']})
            print(json.dumps({'email': email, 'invitation': 'Cognito accepted email delivery', 'status': 'FORCE_CHANGE_PASSWORD'}))
        else:
            print(json.dumps({'email': email, 'subject': subject, 'actor_id': principals[subject]['actor_id'],
                              'status': user['UserStatus'], 'roles': principals[subject]['roles']}))
    if args.phase == 'prepare':
        replacements = {'OAUTH_PRINCIPALS_JSON': json.dumps(principals, separators=(',', ':')),
                        'LIBRARY_QUOTA_BYTES': str(100 * 1024**3)}
        lines = [line for line in content.splitlines() if line.split('=', 1)[0] not in replacements]
        lines += [f"{key}='{value}'" for key, value in replacements.items()]
        current = aws('ssm', 'get-parameter', '--name', PARAMETER)['Parameter']
        if current['Version'] != parameter['Version']:
            raise RuntimeError('Deployment settings changed. Rerun preparation to merge the updates.')
        aws('ssm', 'put-parameter', payload={'Name': PARAMETER, 'Type': 'SecureString',
                                          'Value': '\n'.join(lines)+'\n', 'Overwrite': True})
        print('Prepared separate team identities and the 100 GB allowance. Invitations have not yet been sent.')


if __name__ == '__main__':
    main()
