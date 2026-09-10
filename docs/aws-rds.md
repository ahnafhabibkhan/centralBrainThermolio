# AWS RDS PostgreSQL setup

## New RDS database or existing instance?

Prefer a **new RDS PostgreSQL instance/cluster** when data has a different owner,
security classification, lifecycle, residency requirement, or unpredictable load.
It provides clearer blast-radius, maintenance, and restore boundaries.

An **isolated database on an existing instance** is often a sound pilot choice if
the engine/version and extensions are compatible, capacity is proven, networking
and KMS controls are acceptable, and independent instance-level restore is not a
requirement. A dedicated schema alone is the weakest boundary because connection,
backup, and many administrative privileges remain shared.

## AWS console/IaC checklist

1. Select a supported PostgreSQL version and private subnets; set **Public access
   = No**.
2. Restrict the security group to the Brain API's security group on port 5432.
3. Enable KMS encryption, automated backups, deletion protection, Performance
   Insights/Database Insights as appropriate, log exports, and alarms.
4. Store generated credentials in Secrets Manager and enable rotation. Do not put
   production credentials in `.env`, source control, or model context.
5. If semantic search is planned, verify the `vector` extension and desired
   version are supported for the exact engine/version before enabling it.
6. Use RDS Proxy if connection bursts or credential rotation warrant it.

## Bootstrap roles and database

Run these commands as an RDS administrative user after replacing generated
passwords. A separate database is recommended when the existing instance is used:

```sql
CREATE ROLE central_brain_owner NOLOGIN;
CREATE ROLE central_brain_migrator LOGIN PASSWORD '<from-secrets-manager>';
CREATE ROLE central_brain_runtime LOGIN PASSWORD '<from-secrets-manager>';
GRANT central_brain_owner TO central_brain_migrator;

CREATE DATABASE central_brain OWNER central_brain_owner;
REVOKE CONNECT ON DATABASE central_brain FROM PUBLIC;
GRANT CONNECT ON DATABASE central_brain
  TO central_brain_migrator, central_brain_runtime;
```

Reconnect to `central_brain` as the migrator and apply `001_core.sql`. Afterwards:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA central_brain TO central_brain_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA central_brain
  TO central_brain_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE central_brain_owner IN SCHEMA central_brain
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO central_brain_runtime;
```

The runtime role must not own tables, create extensions, bypass RLS, or run
migrations. If using only a schema in an existing database, create it with the
dedicated owner and apply the same grants, but document the reduced isolation.

## Required validation

- Connect as the runtime role and confirm it cannot access another workspace after
  `SET LOCAL app.workspace_id` in a transaction.
- Confirm a connection without `app.workspace_id` returns no tenant rows.
- Restore a snapshot to a non-production environment and validate record counts.
- Exercise credential rotation without downtime and verify revoked credentials
  stop working.
- Configure alarms for storage, CPU, connections, replica lag (if used), failed
  logins, and backup failures.

Exact AWS features and extension versions change over time; verify them against
the current AWS RDS documentation and your organization's controls during setup.
