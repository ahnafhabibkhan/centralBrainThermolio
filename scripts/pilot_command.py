"""Send a reviewed, nonsecret shell script only to this stack's instance."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pilot_aws import environment

state = json.loads(Path('work/aws-release/state.json').read_text())
payload = {'InstanceIds': [state['InstanceId']], 'DocumentName': 'AWS-RunShellScript',
           'Parameters': {'commands': [Path(sys.argv[1]).read_text()], 'executionTimeout': ['1800']}}
with tempfile.NamedTemporaryFile(mode='w', dir='work/aws-release') as request:
    json.dump(payload, request)
    request.flush()
    result = subprocess.run(['aws', 'ssm', 'send-command', '--cli-input-json', 'file://' + request.name,
                             '--query', 'Command.CommandId', '--output', 'text'],
                            env=environment(), text=True, capture_output=True, check=True)
    print(result.stdout.strip())
