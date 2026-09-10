# Cloud deployment runbook

The repository is cloud-ready but deliberately does not contain account-specific
AWS identifiers. Cloud changes must be applied from an authenticated deployment
role; never paste AWS credentials into a prompt or commit them to this repository.

## Recommended first deployment

Use an existing organization-approved CI/CD pipeline to build `Dockerfile`, scan
the image, push it to Amazon ECR, and deploy it to a private ECS Fargate service.
Place an HTTPS Application Load Balancer in front of the service and allow the ECS
security group to reach RDS on port 5432. Supply `DATABASE_URL` and
`CENTRAL_BRAIN_PRINCIPALS_JSON` from Secrets Manager as ECS secret environment
variables.

Do not run database migrations automatically on every web-container startup. Run
them as a one-off, approval-gated ECS task using the migrator identity described in
`docs/aws-rds.md`; the API task must use the restricted runtime identity.

## Apply sequence

1. Confirm the AWS account, region, VPC, private subnets, hosted zone, KMS key,
   backup policy, and service owner.
2. Create or select RDS following `docs/aws-rds.md` and store migrator/runtime
   connection values in separate Secrets Manager secrets.
3. Apply `database/migrations/001_core.sql` from a one-off migration task and run
   the two-workspace isolation checks.
4. Build and scan the image:

   ```bash
   docker build --pull -t central-brain:${GIT_SHA} .
   docker run --rm central-brain:${GIT_SHA} python -m compileall -q /usr/local/lib
   ```

5. Push the immutable commit-tagged image to ECR. Do not deploy `latest`.
6. Deploy ECS with at least two tasks across availability zones, read-only root
   filesystems, no public IP, health check `/health`, deployment rollback, and
   CloudWatch logs with an explicit retention period.
7. Attach AWS WAF/rate limiting where internet exposure is required. Prefer
   private access or an authenticated API gateway for internal deployments.
8. Smoke-test authentication, create/propose/search/approve flows, RLS isolation,
   alarms, and rollback before enabling a model adapter.

## Required deployment inputs

| Input | Example | Source |
| --- | --- | --- |
| AWS account and region | organization-specific | deployment environment |
| VPC/private subnet IDs | `subnet-…` | infrastructure stack output |
| RDS endpoint/database | secret | Secrets Manager |
| runtime bearer principals | secret JSON | Secrets Manager |
| TLS certificate/hostname | organization-specific | ACM/Route 53 |
| ECR repository/image digest | immutable digest | build pipeline |

## Rollback

Roll back the ECS task definition to the previous image digest. Database changes
must be backward compatible with both application versions; use a forward-fix
migration rather than destructive automatic rollback. Restore RDS snapshots only
for disaster recovery because restoration affects all data after the snapshot.

## Why this runbook does not directly provision AWS

Applying cloud resources without the target account, region, network, DNS,
security controls, deployment role, and an explicit RDS isolation choice risks
deploying into the wrong account or exposing a database. Once those inputs are
available to the CI/CD environment, this sequence can be encoded in the
organization's Terraform/CDK stack and applied through its normal approval gates.
