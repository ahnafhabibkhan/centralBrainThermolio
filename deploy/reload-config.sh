#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/central-brain/deploy
aws ssm get-parameter --name /central-brain/pilot/environment --with-decryption \
  --query Parameter.Value --output text > .env
chmod 600 .env
docker compose up -d --force-recreate --wait app
