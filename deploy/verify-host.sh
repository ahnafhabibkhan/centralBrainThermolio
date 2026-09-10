#!/usr/bin/env bash
set -euo pipefail
cd /opt/central-brain/deploy
mountpoint -q /srv/central-brain
findmnt -n -o TARGET,SOURCE,FSTYPE /srv/central-brain
systemctl is-active docker central-brain-backup.timer
docker compose ps --format '{{.Service}} {{.State}} {{.Health}}'
docker inspect central-brain-app-1 --format 'Application user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}'
docker compose exec -T app python - <<'PY'
import json
import os
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=5) as response:
    assert json.load(response)['status'] == 'ready'
assert len(json.loads(os.environ['OAUTH_CLIENT_IDS'])) == 3
assert not json.loads(os.environ['CENTRAL_BRAIN_PRINCIPALS_JSON'])
print('Production is ready with three OAuth clients and no static bearer tokens.')
PY
