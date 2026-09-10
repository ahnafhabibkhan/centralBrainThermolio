# Pilot implementation status

The local implementation is on branch `codex/implement-ec2-pilot`. It replaces the starter API with a functioning review workflow and official-SDK MCP server.

Eleven PostgreSQL integration tests passed locally. Local and cloud dump-and-restore exercises passed, including excluded sessions and preserved row security. Desktop and mobile browser checks passed. The ARM container runs on AWS, and the CloudFormation stack completed successfully. See the [live deployment guide](aws-pilot-live.md).

## Implemented locally

- The review website supports sign-in, proposals, approval, rejection, correction, search, individual export, and individual deletion.
- PostgreSQL applies forced workspace and private-record isolation. Runtime credentials cannot change audit rows or schema.
- Mutations and their audit events commit together. Equal concurrent submissions deduplicate, and competing corrections cannot both replace the same active memory.
- MCP exposes four assistant tools with reader and writer permissions. Review and deletion require separate human permissions.
- Production authentication validates signed OAuth access tokens and restricts assistant clients independently of their supplied scopes.
- Browser sessions use opaque cookies and encrypted server-side token storage. Forms require CSRF validation, and rendered memory text is escaped.
- The deployment package describes EC2, persistent encrypted EBS, HTTPS, Cognito, backup storage, alerts, and recovery procedures.

## Deployment-dependent validation

AWS provisioning, HTTPS certificate validation, application readiness, OAuth discovery, the Cognito sign-in page, S3 backup delivery, an isolated cloud restore, and recovery after a full host reboot have passed. The owner invitation, immutable Cognito subject authorization, live configuration reload, and alert subscription creation also passed. ChatGPT and Claude both discovered the live OAuth configuration. The live OAuth callback and authenticated workspace access now pass. Authenticator enrollment and authenticated connector tool calls remain pending. The owner email requirement was removed because AWS monitoring is managed internally. The login flow skips the landing page, and Cognito uses managed login with matching resource-scoped permissions. Discovery success alone does not prove an authenticated connection.

The pilot has one instance and no high availability. Retrieval uses English PostgreSQL full-text search, without embeddings or inference charges. Expired records are hidden from retrieval but remain stored until explicitly deleted. Deletion applies to one record; previous versions and external copies require separate review. Imported skills are reference text, and import requires explicit approval. No existing memory or skill artifacts were automatically imported.

The OAuth implementation requires resource-bound access tokens and pre-registered clients. Provider metadata and assistant account support remain explicit acceptance gates. The application intentionally fails closed if they are incompatible.
