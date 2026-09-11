"""Exercise the release image against the disposable local test database."""
import json
import subprocess
from dotenv import dotenv_values

env=dotenv_values('.env')
settings={
 'database_url':env['DATABASE_URL'].replace('@127.0.0.1:55432/','@postgres:5432/').rsplit('/',1)[0]+'/central_brain_test',
 'session_secret':env['SESSION_SECRET'],
 'central_brain_principals_json':env['CENTRAL_BRAIN_PRINCIPALS_JSON'],
 'library_local_path':'/tmp/library-test',
}
code='''
import json,sys,os
from central_brain.config import Settings
from central_brain.auth import AuthContext
from central_brain.repository import PostgresMemoryRepository
from central_brain.library import Library
from central_brain.library_worker import process_one
from uuid import uuid4
s=Settings(**json.load(sys.stdin));r=PostgresMemoryRepository(s.database_url);r.open()
a=AuthContext(next(p for p in s.principals().values() if 'reviewer' in p.roles))
l=Library(r,s)
n=l.upload(a,str(uuid4())+'.txt',b'Container document verification')
for _ in range(100):
 if l.read(a,n)['state']!='queued':break
 process_one(l,a)
assert l.read(a,n)['state']=='ready',l.read(a,n)
assert os.getuid()==10001
print('Non-root Linux extraction, PostgreSQL indexing and original storage passed.')
r.close()
'''
result=subprocess.run(['docker','run','--rm','-i','--network','central-brain-pilot_default',
 '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true','--tmpfs','/tmp:size=100m,mode=1777',
 'central-brain:library-20260911','python','-c',code],input=json.dumps(settings),text=True,capture_output=True)
if result.returncode:raise RuntimeError(result.stderr)
print(result.stdout)
