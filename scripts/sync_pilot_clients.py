"""Refresh the approved client allowlist without changing any production password."""
import json
import subprocess
import tempfile
from pathlib import Path

from pilot_aws import environment

env = environment()


def aws(*args):
    result = subprocess.run(['aws', *args, '--output', 'json'], env=env, capture_output=True,
                            text=True, check=True)
    return json.loads(result.stdout)


stack = aws('cloudformation', 'describe-stacks', '--stack-name', 'central-brain-pilot')['Stacks'][0]
assert stack['StackStatus'] == 'UPDATE_COMPLETE'
state = {o['OutputKey']: o['OutputValue'] for o in stack['Outputs']}
Path('work/aws-release/state.json').write_text(json.dumps(state, indent=2))
parameter = aws('ssm', 'get-parameter', '--name', '/central-brain/pilot/environment', '--with-decryption')
content = parameter['Parameter']['Value']
clients = [state[key] for key in ('WebClientId', 'ChatGPTClientId', 'ClaudeClientId') if key in state]
lines = content.splitlines()
updated = '\n'.join("OAUTH_CLIENT_IDS='" + json.dumps(clients) + "'" if line.startswith('OAUTH_CLIENT_IDS=')
                    else line for line in lines) + '\n'
with tempfile.NamedTemporaryFile(mode='w', dir='work/aws-release') as request:
    json.dump({'Name': '/central-brain/pilot/environment', 'Type': 'SecureString',
               'Value': updated, 'Overwrite': True}, request)
    request.flush()
    aws('ssm', 'put-parameter', '--cli-input-json', 'file://' + request.name)
print(f'Encrypted configuration now allows {len(clients)} approved OAuth clients.')
