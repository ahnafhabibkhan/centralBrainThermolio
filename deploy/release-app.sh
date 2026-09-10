#!/usr/bin/env bash
# Install a verified application image on the existing pilot without database changes.
set -euo pipefail
umask 077
: "${RELEASE_IMAGE:?Set the approved image tag}"
: "${RELEASE_KEY:?Set the release object key}"
: "${RELEASE_SHA256:?Set the verified image archive digest}"
export AWS_DEFAULT_REGION=ca-central-1
cd /opt/central-brain/deploy
archive=$(mktemp /opt/central-brain/app-release.XXXXXX.tar)
trap 'rm -f "$archive"' EXIT
aws s3 cp "s3://central-brain-pilot-backups-83ve7xpsxvxs/$RELEASE_KEY" "$archive" --only-show-errors
echo "$RELEASE_SHA256  $archive" | sha256sum -c -
docker load -i "$archive"
if ! APP_IMAGE="$RELEASE_IMAGE" docker compose up -d --no-deps --wait app; then
  docker compose up -d --no-deps --wait app
  echo 'The new release failed. The previous configured image was restored.'
  exit 1
fi
echo 'The application release is healthy. Persist its image tag in encrypted configuration.'
