#!/usr/bin/env bash
# Restore a named backup into a disposable database on the project instance.
set -euo pipefail
umask 077
: "${BACKUP_BUCKET:?Set the project backup bucket}"
: "${BACKUP_KEY:?Set the exact backup object key}"
[[ "$BACKUP_KEY" == backups/*.dump ]]
cd /opt/central-brain/deploy
scratch=$(mktemp -d)
target="central_brain_restore_$(date +%s)"
cleanup() {
  docker compose exec -T postgres dropdb -U postgres --if-exists "$target"
  rm -rf "$scratch"
}
trap cleanup EXIT
aws s3 cp "s3://$BACKUP_BUCKET/$BACKUP_KEY" "$scratch/backup.dump" --only-show-errors
docker compose exec -T postgres createdb -U postgres "$target"
docker compose exec -T postgres pg_restore -U postgres --exit-on-error -d "$target" < "$scratch/backup.dump"
docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U postgres -d "$target" <<'SQL'
DO $$
BEGIN
  IF (SELECT count(*) FROM central_brain.workspaces) < 1 THEN
    RAISE EXCEPTION 'The workspace seed was not restored';
  END IF;
  IF (SELECT count(*) FROM pg_policies WHERE schemaname='central_brain') < 5 THEN
    RAISE EXCEPTION 'Row security policies were not restored';
  END IF;
  IF (SELECT count(*) FROM central_brain.web_sessions) <> 0 THEN
    RAISE EXCEPTION 'Session credentials must not be backed up';
  END IF;
END $$;
SELECT 'Cloud backup restored with workspace data, row security policies, and no sessions.' AS result;
SQL
