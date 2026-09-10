"""Check the built image against the local development database without printing credentials."""
import json
import subprocess

from dotenv import dotenv_values

env = dotenv_values('.env')
settings = {
    'database_url': env['DATABASE_URL'].replace('@127.0.0.1:55432/', '@postgres:5432/'),
    'session_secret': env['SESSION_SECRET'],
    'central_brain_principals_json': env['CENTRAL_BRAIN_PRINCIPALS_JSON'],
}
code = '''
import json, os, sys
from fastapi.testclient import TestClient
from central_brain.api import create_app
from central_brain.config import Settings
assert os.getuid() == 10001
with TestClient(create_app(settings=Settings(**json.load(sys.stdin)))) as client:
    assert client.get('/ready').status_code == 200
    response = client.get('/login')
    assert response.status_code == 200
    assert response.headers['Referrer-Policy'] == 'same-origin'
    assert 'Local reviewer token' in response.text
print('Container readiness, packaged templates, and non-root execution passed.')
'''
result = subprocess.run([
    'docker', 'run', '--rm', '-i', '--network', 'central-brain-pilot_default',
    '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
    '--tmpfs', '/tmp:size=64m,mode=1777', 'central-brain:local', 'python', '-c', code,
], input=json.dumps(settings), text=True, capture_output=True, check=False)
if result.returncode:
    raise RuntimeError('Container verification failed: ' + result.stderr)
print(result.stdout.strip())
