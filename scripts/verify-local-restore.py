"""Restore only the disposable test database inside the local Compose container."""
import subprocess
from uuid import uuid4

PREFIX = ['docker', 'compose', 'exec', '-T', 'postgres']


def run(*args, data=None):
    result = subprocess.run(PREFIX + list(args), input=data, capture_output=True, check=True)
    return result.stdout


def main():
    target = 'restore_test_' + uuid4().hex
    dump = run('pg_dump', '-U', 'postgres', '-d', 'central_brain_test', '--format=custom',
               '--exclude-table-data=central_brain.web_sessions')
    count_query = 'SELECT count(*) FROM central_brain.memories'
    expected = run('psql', '-U', 'postgres', '-d', 'central_brain_test', '-Atc', count_query)
    run('createdb', '-U', 'postgres', '-O', 'central_brain_owner', target)
    try:
        run('pg_restore', '-U', 'postgres', '-d', target, '--exit-on-error', data=dump)
        actual = run('psql', '-U', 'postgres', '-d', target, '-Atc', count_query)
        if actual != expected:
            raise RuntimeError('Restored record count does not match the source')
        sessions = run('psql', '-U', 'postgres', '-d', target, '-Atc',
                       'SELECT count(*) FROM central_brain.web_sessions')
        if sessions.strip() != b'0':
            raise RuntimeError('Sessions must not be included in backups')
        isolated = run('psql', '-U', 'postgres', '-d', target, '-Atc',
                       'SET ROLE central_brain_runtime; SELECT count(*) FROM central_brain.memories')
        if isolated.strip().splitlines()[-1] != b'0':
            raise RuntimeError('Restored row-level security failed')
        print('Local dump and restore passed. Record counts match, sessions are excluded, and RLS holds.')
    finally:
        run('dropdb', '-U', 'postgres', target)


if __name__ == '__main__':
    main()
