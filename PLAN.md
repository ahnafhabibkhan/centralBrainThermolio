# Central Brain delivery plan

The EC2 pilot is now implemented locally. See [the current implementation status](docs/implementation-status.md) and [the EC2 pilot runbook](docs/pilot-runbook.md). The original planning notes below describe the earlier starter state and alternatives, not current deployment instructions.

This short plan is intentionally at the repository root so it is easy to locate.
The detailed design remains under `docs/`.

## Current state

- PostgreSQL schema, tenant row-level security, audit tables, and optional vector
  indexing are defined.
- A minimal authenticated FastAPI service supports proposed memory writes, keyword
  retrieval, and reviewer approval.
- Portable memory files and memory retrieval/write/maintenance skills exist.
- The service has a non-root container definition and an AWS deployment runbook.
- No AWS resources have been applied from this checkout because it has no AWS
  deployment credentials or account/network configuration.

## Execution order

### 1. Confirm ownership and data policy

Decide the service owner, AWS account and region, data classifications, retention
period, workspace model, reviewer role, and what must never be stored. Do this
before provisioning infrastructure.

**Done when:** the data inventory and threat model have named approvers.

### 2. Select the PostgreSQL boundary

Use a new RDS instance/cluster for the cleanest operational and security boundary.
For a small pilot, use a separate database on an existing compatible RDS instance.
Use only a separate schema when shared backup, restore, and administration are
explicitly acceptable.

**Done when:** the choice, owner, capacity, extensions, backup, and restore impact
are documented.

### 3. Provision a non-production database

Use private subnets, restricted security groups, KMS encryption, Secrets Manager,
automated backups, deletion protection, and distinct migration/runtime roles.
Apply `database/migrations/001_core.sql`; defer pgvector until keyword retrieval has
been measured.

**Done when:** a restore test passes and attempts to read another workspace fail.

### 4. Validate the API locally and in CI

Install the project with `python -m pip install -e '.[dev]'`, run `pytest`, start
the API through the documented Uvicorn factory, and exercise authentication,
write, search, approval, expiry, deduplication, and cross-workspace cases against
real PostgreSQL.

**Done when:** unit, integration, migration, and tenant-isolation tests pass in CI.

### 5. Deploy the Brain API to AWS

Build and scan the container, push an immutable image digest to ECR, and deploy at
least two private ECS Fargate tasks behind HTTPS. Inject database and principal
configuration from Secrets Manager. Run migrations as a separate approval-gated
task, not during API startup.

**Done when:** health checks, logs, alarms, rollback, credential rotation, and the
end-to-end memory lifecycle pass in the non-production environment.

### 6. Connect one model provider

Implement one adapter that sends bounded, cited memory to the selected model and
turns model-suggested writes into `proposed` records. Retrieved memory must be
treated as untrusted reference data, never as executable instructions.

**Done when:** a golden evaluation set demonstrates useful retrieval without
cross-workspace disclosure or automatic unreviewed writes.

### 7. Add the second provider

Implement the same internal adapter contract for the second provider and run the
same evaluation set. Provider-specific identifiers stay in metadata and never
become canonical database identifiers.

**Done when:** switching providers needs configuration, not a database migration.

### 8. Add semantic retrieval only if justified

If keyword evaluation reveals a measured recall gap, apply the optional pgvector
migration and generate embeddings asynchronously. Record the provider, model,
dimensions, and content hash so embeddings remain rebuildable.

**Done when:** semantic retrieval improves the written evaluation enough to
justify its cost and operational complexity.

## Inputs required before cloud application

1. Git repository URL and authenticated push access.
2. AWS account ID, region, and an authenticated deployment role.
3. VPC and private subnet IDs plus security-group ownership.
4. New-versus-existing RDS decision and, when existing, its endpoint and engine
   version supplied through an approved secret channel.
5. DNS name and ACM certificate, or a decision to keep the service private.
6. KMS key, backup/retention requirements, and CloudWatch log-retention period.

Never place credentials or secret values in an issue, chat message, pull request,
or committed environment file.
