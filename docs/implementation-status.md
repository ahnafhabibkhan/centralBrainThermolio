# Pilot implementation status

The local implementation is on branch `codex/implement-ec2-pilot`. It replaces the starter API with a functioning review workflow and official-SDK MCP server.

Eight PostgreSQL integration tests passed locally. The local dump-and-restore exercise also passed, including matching record counts, excluded sessions, and preserved row-level security. Desktop and mobile browser checks passed after correcting the sign-in page's referrer policy. The container image built successfully, and the CloudFormation template passed local linting.

## Implemented locally

- The review website supports sign-in, proposals, approval, rejection, correction, search, individual export, and individual deletion.
- PostgreSQL applies forced workspace and private-record isolation. Runtime credentials cannot change audit rows or schema.
- Mutations and their audit events commit together. Equal concurrent submissions deduplicate, and competing corrections cannot both replace the same active memory.
- MCP exposes four assistant tools with reader and writer permissions. Review and deletion require separate human permissions.
- Production authentication validates signed OAuth access tokens and restricts assistant clients independently of their supplied scopes.
- Browser sessions use opaque cookies and encrypted server-side token storage. Forms require CSRF validation, and rendered memory text is escaped.
- The deployment package describes EC2, persistent encrypted EBS, HTTPS, Cognito, backup storage, alerts, and recovery procedures.

## Deployment-dependent validation

AWS creation, public DNS, certificates, Cognito sign-in and MFA, live ChatGPT and Claude connections, S3 backup delivery, notification delivery, host restart, and cloud recovery are not yet performed. CloudFormation linting validates structure, not permissions or live service compatibility.

The pilot has one instance and no high availability. Retrieval uses English PostgreSQL full-text search, without embeddings or inference charges. Expired records are hidden from retrieval but remain stored until explicitly deleted. Deletion applies to one record; previous versions and external copies require separate review. Imported skills are reference text, and import requires explicit approval. No existing memory or skill artifacts were automatically imported.

The OAuth implementation requires resource-bound access tokens and pre-registered clients. Provider metadata and assistant account support remain explicit acceptance gates. The application intentionally fails closed if they are incompatible.
