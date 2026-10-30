# Library access and worker identity fixes

This release candidate prevents direct access to files beneath inaccessible parent folders and processes private files belonging to invited users. The changes require no database migration or new database privilege. Production deployment has not been performed.

## Behavior

File reads, historical versions, metadata, previews, downloads, and mutations require a complete path through accessible, active folders. Folder exports exclude restricted branches, and review snapshots stop at inactive folders. Owners retain access to their private files, and reviewers retain permitted access to proposed and rejected items.

The library worker discovers invited identities only within workspaces administered by configured users. It refreshes identities between cycles after at least 30 seconds have elapsed. Configured identities retain precedence, and queue operations use reader and writer roles under the existing row-level policies. Failed refreshes remove discovered identities until recovery. Failed deletion attempts do not prevent extraction.

## Source and build provenance

Commit `37eef52` captures the deployed canonical source, required migrations, and associated tests. The deployed-source manifest records the October 20 image identity, observed migration versions, source hashes, Python version, and dependency inventory. Duplicate source copies and macOS resource-fork files are excluded from the canonical snapshot.

Commit `585ad93` contains the access fix. Commit `b25014f` contains the worker fix. The release manifest records the application commit, pinned base image, built image ID, source hashes, and build-input hashes. All 48 canonical application files in the image matched the repaired source. Application dependency versions matched the inspected production image.

The build uses the digest-pinned Python 3.12 base in the Dockerfile and the existing dependency constraints:

```bash
docker build --platform linux/arm64 -t central-brain:access-worker-20261030 .
```

Source and dependency equivalence are verified. Bit-identical rebuilding is not asserted because build-tool metadata and image timestamps can differ.

## Validation

- All 217 Python tests passed against a disposable PostgreSQL database in the non-root Linux ARM64 image with a read-only root filesystem.
- All 27 JavaScript tests passed.
- The fixes add 24 regression cases for ancestor access, exports, invited identities, private extraction and deletion, revocation, refresh failure, and processing independence.
- Both new test files pass Ruff. Canonical-source lint findings decreased from 218 to 216, with no new diagnostics. The existing whole-repository lint gate still fails on those inherited findings.
- The disposable test database was isolated from the existing development and production databases.

## Production application

Apply the same verified image to both the API and library worker when deploying this release. Preserve the previous image and persist the new image selection in encrypted configuration. Check application readiness and worker operation after deployment. The existing application-only release helper does not update the worker automatically.

The previous production image remains `central-brain:selected-review-20261020` until a deployment is performed. No persistent local or staging environment was created for this work.
