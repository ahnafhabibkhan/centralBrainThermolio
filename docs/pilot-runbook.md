# EC2 pilot runbook

## Approval boundary

The user approved this AWS pilot on September 10, 2026, with a CAD $40 monthly project limit and a preference for free services. The infrastructure and HTTPS application are deployed. See [the live deployment guide](aws-pilot-live.md). The invitation email and intended Claude account still need to be specified. Importing memory or skills requires separate authorization.

The proposed deployment uses one `t4g.small` in `ca-central-1`, an encrypted 8 GB root disk, an encrypted 20 GB database disk, one public IPv4 address, S3 backups, Cognito, and basic alerts. It uses neither RDS nor a load balancer. Existing databases remain separate.

The project budget is USD $20, filtered by the activated `Project=CentralBrain` cost tag. An automatic budget action stops only this project's instance at USD $16. AWS billing is delayed, and retained storage and the Elastic IP continue to cost money after a stop, so this is not a guaranteed hard cap. CPU credits use standard mode to avoid surplus-credit charges. The live guide includes conservative CAD estimates and the trial's expiry date.

## 1. Review inputs and preview changes

Choose an available free hostname, such as an approved DuckDNS subdomain. Record its HTTPS origin without a trailing slash. Get the exact callback URLs from the ChatGPT and Claude connector setup screens; do not guess them. Choose the owner's sign-in email, alert email, and a unique Cognito domain prefix.

Confirm the selected VPC, subnet, and availability zone agree. Previously inspected candidates were VPC `vpc-001b383be7a969294`, subnet `subnet-058870551a5c4e29d`, and zone `ca-central-1a`. Recheck those read-only before deployment. Public outbound internet access is required for SSM, package downloads, Cognito, and certificate issuance.

Validate the template locally:

```bash
.venv/bin/python -m pip install -c constraints.txt cfn-lint
.venv/bin/cfn-lint deploy/pilot.yaml
```

For a future environment, obtain deployment authorization before creating resources. For this approved pilot, use the existing `central-brain-pilot` stack and preserve all existing parameter values during updates. Do not create a duplicate stack. The template includes EC2, an Elastic IP, EBS, S3, IAM, Cognito clients, SNS, CloudWatch alarms, and a project budget. Email subscriptions are enabled only after a recipient is supplied.

Record all stack outputs. The template intentionally does not run arbitrary install scripts or launch the application. Confirm the instance becomes an SSM managed node before continuing. Install the local Session Manager plugin if it is missing, then connect using `aws ssm start-session --target INSTANCE_ID --region ca-central-1`.

## 2. Prepare the host and database disk

On the approved Amazon Linux 2023 instance, install Docker and enable it:

```bash
sudo dnf install -y docker
sudo systemctl enable --now docker
```

Install the Docker Compose CLI plugin from an approved official release and verify its published checksum. Record the chosen version. Use an ARM64 binary. Verify `docker compose version`, `aws --version`, and `lsblk -f` before continuing. Do not run a remote installation script without inspecting it.

Find the attached 20 GB EBS disk using `lsblk` and its NVMe serial, then match that serial to the stack's `DatabaseVolumeId`. AWS device names can differ from `/dev/sdf`. Never assume the device name. If it contains a filesystem, mount and inspect it without formatting. For a verified new blank disk, formatting is destructive and belongs within the approved deployment action:

```bash
sudo mkfs.ext4 /dev/VERIFIED_DATABASE_DEVICE
sudo mkdir -p /srv/central-brain
sudo blkid /dev/VERIFIED_DATABASE_DEVICE
```

Add its filesystem UUID to `/etc/fstab` with the mount point `/srv/central-brain`, type `ext4`, and options `defaults`. Use filesystem checks `0 2`. Mount it and confirm `mountpoint /srv/central-brain` succeeds. Do not use `nofail`, which could let the database write onto the root disk when EBS is unavailable. Add a Docker systemd override with `RequiresMountsFor=/srv/central-brain` so Docker cannot start the database without the disk. Reload systemd and restart Docker before launching services.

Create `/srv/central-brain/postgres`, `/srv/central-brain/caddy-data`, and `/srv/central-brain/caddy-config`. The official database image prepares its data-directory ownership. Keep the host's credentials root-readable only.

## 3. Build and transfer an approved release

Build the image for ARM64 on the development machine. Record the Git commit, image ID, dependency constraints, and SHA-256 of the exported archive:

```bash
docker build --platform linux/arm64 -t central-brain:APPROVED_RELEASE .
docker save central-brain:APPROVED_RELEASE | gzip > work/central-brain-image.tar.gz
shasum -a 256 work/central-brain-image.tar.gz
```

After upload approval, upload the image archive and the reviewed `deploy/` files to the stack's bucket under `releases/APPROVED_RELEASE/`. The instance role may read this prefix but cannot write releases. Download them on the instance, compare the approved checksum, and load the image with `docker load`. Place deployment files at `/opt/central-brain/deploy`. Keep the previous release available for rollback.

Before deployment, pin the `postgres:17` and `caddy:2` references in the reviewed Compose file to the tested image digests for the target architecture. Mutable major-version tags are convenient locally but must not silently change a deployed release.

## 4. Configure authentication and credentials

The template creates a private Cognito user pool with software-token MFA. Create only the approved owner account after permission to send its invitation. Complete password and MFA enrollment, then record that account's immutable Cognito `sub`. Disable public self-registration.

Fetch each client secret through an authenticated AWS administration session. Store the web client secret only on the host. Enter each assistant client secret only in the corresponding connector's secure setup form. Never put secrets into conversation messages, Git, URLs, screenshots, or memories.

Create `/opt/central-brain/deploy/.env` with permissions `0600`. Generate independent URL-safe random database administrator, runtime, and migration passwords and a session secret of at least 32 characters. Set these variables:

| Variable | Value |
| --- | --- |
| `APP_IMAGE` | Set the approved local image tag or digest. |
| `PUBLIC_URL` | Set the approved `https://hostname` origin. |
| `POSTGRES_PASSWORD` | Set the new database administrator password. |
| `LOCAL_ADMIN_URL` | Use `postgresql://postgres:PASSWORD@postgres:5432/postgres`. |
| `DATABASE_URL` | Use `postgresql://central_brain_runtime:PASSWORD@postgres:5432/central_brain`. |
| `MIGRATION_DATABASE_URL` | Use `postgresql://central_brain_migrator:PASSWORD@postgres:5432/central_brain`. |
| `SESSION_SECRET` | Set the independently generated session secret. |
| `OAUTH_ISSUER` | Use the stack's Cognito issuer output. |
| `OAUTH_CLIENT_IDS` | Set a JSON array containing all three client IDs. |
| `OAUTH_WEB_CLIENT_ID` | Use the web client ID. |
| `OAUTH_WEB_CLIENT_SECRET` | Use the web client secret. |
| `OAUTH_SCOPE_PREFIX` | Use the exact MCP resource URL, including its trailing slash. |
| `OAUTH_PRINCIPALS_JSON` | Map the approved Cognito subject as shown below. |

```json
{"APPROVED_COGNITO_SUB":{"workspace_id":"a22cdb8e-6c0d-4b59-b292-a4e5593156c1","actor_id":"f1aa197b-4291-4d81-a00c-cab55a1ceff0","roles":["reader","writer","reviewer","admin"],"sensitivities":["public","internal"]}}
```

The runtime receives only its database credentials and web OAuth credentials. The separate bootstrap container receives migration credentials. Non-web OAuth clients are restricted to reader and writer roles in code even if a token unexpectedly carries broader scopes.

## 5. Start PostgreSQL, migrate, and enable HTTPS

Run these commands only on the approved host after confirming the database disk is mounted:

```bash
cd /opt/central-brain/deploy
mountpoint /srv/central-brain
sudo docker compose --env-file .env -f compose.yaml up -d --wait postgres
sudo docker compose --env-file .env -f compose.yaml run --rm bootstrap
sudo docker compose --env-file .env -f compose.yaml up -d app caddy
```

Point the approved DNS hostname to the stack's public IP. Caddy obtains and renews its HTTPS certificate using ports 80 and 443. Certificate issuance has no separate certificate fee, while EC2, IPv4, DNS-provider terms, and traffic have their own costs or conditions. Do not enable access logging of OAuth callback query strings.

Verify HTTPS certificate validity, `/health`, `/ready`, and sign-in. Confirm that ports 22, 5432, and 8080 are not publicly reachable. App and database services have no published host ports. Use SSM for administration. Test host restart before acceptance to confirm the disk mounts before Docker starts.

## 6. Verify both assistant connections

Use the public endpoint `https://hostname/mcp/`, a separate OAuth client for each assistant, and the scopes advertised at `/.well-known/oauth-protected-resource`. Each assistant app must support the configured OAuth flow and manual client credentials. Account and plan availability must be checked during setup.

The application requires a signed RS256 access token with the correct issuer, expiry, allowed client ID, allowlisted subject, `token_use=access`, and an audience matching `PUBLIC_URL` plus `/mcp/`. The authorization request must use that exact OAuth `resource`, including the trailing slash, to obtain the audience. Do not weaken token validation if a connector fails.

Use Cognito Essentials and managed login version 2 for resource-bound tokens. The resource server identifier must equal the MCP resource URL, and custom scopes must belong to that resource server. The classic hosted UI does not supply the required audience.

Check the issuer's discovery metadata and authorization flow for PKCE S256 and resource binding. Cognito-to-client discovery compatibility is a live acceptance gate, not something local signature tests establish. If an assistant rejects the metadata, cannot accept pre-registered credentials, or does not request the required audience, stop that connector rollout and design the smallest standards-compliant authorization adapter for review.

For each assistant, propose an explicitly labeled test memory with its source. Confirm it is absent from retrieval before approval. Sign in to the review page and approve it. Retrieve it from the other assistant, then reject another proposal and confirm that it remains hidden. Test correction, export, deletion, logout, expired-token rejection, and permission denial on reviewer routes. Remove test records through the review interface afterward.

The server publishes four MCP tools: `search_memories`, `propose_memory`, `list_skills`, and `get_skill`. It provides no approve or delete tool. Assistant behavior depends on tool use and instructions; it does not automatically watch every conversation.

## 7. Configure and verify backups

Create `backup.env` beside the Compose file with permissions `0600`. Set `BACKUP_BUCKET`, `INSTANCE_ID`, and `AWS_DEFAULT_REGION=ca-central-1` using stack outputs. Copy the supplied backup service and timer into `/etc/systemd/system/`, reload systemd, and enable `central-brain-backup.timer`.

Run the service once manually and verify a nonempty S3 object and a `CentralBrain/BackupSuccess` metric. Only confirm an SNS email subscription when the owner explicitly wants email alerts. This pilot uses internal AWS monitoring and has an empty alert email parameter. The CloudWatch alarm detects 26 consecutive hours without a successful backup. Daily logical backups imply up to roughly 24 hours of data loss if the instance and database disk are both lost. They are not continuous recovery.

Backups exclude session rows. S3 encrypts objects at rest and requires TLS. Backup objects expire after 30 days; old object versions expire after a further 7 days. Deleting a memory removes its content from live retrieval but does not immediately erase historical backups or copies in assistant conversations. Restoring an old backup can resurrect deleted records, so reconcile approved deletions before reconnecting clients.

Monthly, restore a selected backup into an isolated disposable PostgreSQL 17 instance. Create the owner, migration, and runtime roles first. Use `pg_restore --exit-on-error` as the isolated database administrator, preserving object ownership. Run bootstrap again to restore restricted grants, clear web sessions, and test RLS, record counts, approval behavior, and search. Never practice restoration over the live database. Record duration and outcome; the recovery-time objective remains unmeasured until an AWS restore exercise passes.

## 8. Operate and roll back

Check database-disk usage weekly with `df -h /srv/central-brain`, and inspect `docker stats`, container health, failed backups, and AWS spend. Container logs rotate at 10 MB with three files. Application logs contain request IDs and response status, not request bodies or credentials. Detailed denied-action auditing, disk alarms, and high availability are future work.

For an application-only rollback, stop the application, select the previous verified image in `.env`, and recreate the app container. Do not downgrade database schema automatically. Take a verified backup before future schema changes and document migration compatibility with both releases.

For a database recovery, stop application writes, preserve the failed disk, restore into an isolated replacement, validate permissions and content, reconcile deletions, then switch the application. This requires separate recovery authorization. Rotate credentials when exposure is suspected; removing a subject from `OAUTH_PRINCIPALS_JSON` and restarting the app revokes application access immediately. Existing JWTs otherwise remain usable until expiry even after provider-side revocation.

The database disk, backup bucket, and Cognito pool are retained if the stack is deleted. They continue to exist and may incur charges. Teardown requires an explicit decision about retention, data export, and eventual deletion.
