#!/usr/bin/env bash
# Run as root through SSM on the project's instance only.
set -euo pipefail
umask 077
: "${DATABASE_VOLUME_ID:?Set the exact newly created EBS volume ID}"
: "${BACKUP_BUCKET:?Set the project bucket}"
: "${INSTANCE_ID:?Set the project instance ID}"
: "${RELEASE_SHA256:?Set the verified release archive digest}"
export AWS_DEFAULT_REGION=ca-central-1

dnf install -y docker
install -d /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-aarch64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
echo '732e3a84c1a0f67256ce80bc2598a24546b10ca05f9faa97efceb1171ece2ef7  /usr/local/lib/docker/cli-plugins/docker-compose' | sha256sum -c -
chmod 755 /usr/local/lib/docker/cli-plugins/docker-compose

# Match the AWS volume serial before touching a disk. Refuse ambiguous matches.
serial="${DATABASE_VOLUME_ID//-/}"
mapfile -t disks < <(lsblk -dn -o PATH,SERIAL | awk -v serial="$serial" '$2 == serial {print $1}')
[[ ${#disks[@]} -eq 1 ]] || { echo 'The expected database volume was not uniquely found.'; exit 1; }
device="${disks[0]}"
if ! blkid "$device" >/dev/null 2>&1; then
  [[ $(lsblk -nr -o TYPE "$device" | wc -l) -eq 1 ]]
  [[ -z $(wipefs --noheadings --output TYPE "$device") ]]
  mkfs.ext4 -L central-brain "$device"
fi
[[ $(blkid -s TYPE -o value "$device") == ext4 ]]
uuid=$(blkid -s UUID -o value "$device")
install -d /srv/central-brain
if ! grep -q "UUID=$uuid " /etc/fstab; then
  echo "UUID=$uuid /srv/central-brain ext4 defaults,noatime 0 2" >> /etc/fstab
fi
mountpoint -q /srv/central-brain || mount /srv/central-brain
[[ $(findmnt -n -o UUID /srv/central-brain) == "$uuid" ]]
install -d /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/central-brain.conf <<'UNIT'
[Unit]
RequiresMountsFor=/srv/central-brain
UNIT
systemctl daemon-reload
systemctl enable --now docker
install -d -m 700 /opt/central-brain /opt/central-brain/deploy
aws s3 cp "s3://$BACKUP_BUCKET/releases/pilot.tar.gz" /opt/central-brain/release.tar.gz --only-show-errors
echo "$RELEASE_SHA256  /opt/central-brain/release.tar.gz" | sha256sum -c -
tar -xzf /opt/central-brain/release.tar.gz -C /opt/central-brain
docker load -i /opt/central-brain/app-image.tar
rm /opt/central-brain/app-image.tar /opt/central-brain/release.tar.gz
aws ssm get-parameter --name /central-brain/pilot/environment --with-decryption \
  --query Parameter.Value --output text > /opt/central-brain/deploy/.env
chmod 600 /opt/central-brain/deploy/.env
cd /opt/central-brain/deploy
printf 'BACKUP_BUCKET=%s\nINSTANCE_ID=%s\nAWS_DEFAULT_REGION=ca-central-1\n' "$BACKUP_BUCKET" "$INSTANCE_ID" > backup.env
docker compose up -d --wait postgres
docker compose --profile operations run --rm bootstrap
docker compose up -d --wait app caddy
chmod 755 /opt/central-brain/deploy/backup.sh
install -m 644 central-brain-backup.service central-brain-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now central-brain-backup.timer
systemctl start central-brain-backup.service
docker compose ps --format json
