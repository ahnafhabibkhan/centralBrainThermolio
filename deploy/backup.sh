#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${BACKUP_BUCKET:?Set the approved backup bucket}"
: "${INSTANCE_ID:?Set the instance ID for backup monitoring}"
: "${AWS_DEFAULT_REGION:?Set the region}"
cd /opt/central-brain/deploy
mountpoint -q /srv/central-brain
mkdir -p /srv/central-brain/backup-work
exec 9>/srv/central-brain/backup-work/backup.lock
flock -n 9
archive=$(mktemp /srv/central-brain/backup-work/backup.XXXXXX)
trap 'rm -f "$archive"' EXIT
docker compose --env-file .env -f compose.yaml exec -T postgres \
  pg_dump -U postgres -d central_brain --format=custom \
  --exclude-table-data=central_brain.web_sessions > "$archive"
test -s "$archive"
key="backups/$(date -u +%Y/%m/%d/%H%M%S).dump"
aws s3 cp "$archive" "s3://$BACKUP_BUCKET/$key" --sse AES256 --only-show-errors
aws cloudwatch put-metric-data --namespace CentralBrain \
  --metric-name BackupSuccess --value 1 --unit Count \
  --dimensions "InstanceId=$INSTANCE_ID"
printf 'Backup uploaded successfully.\n'
