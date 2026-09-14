# Upload reliability review

This review was completed on September 14, 2026. It covers the website, MCP connector, raw original uploads, direct S3 uploads, GitHub imports, processing, and individual and bulk approvals.

## Changes

- Every new MCP original transfer now uses the same resumable inventory, including small PDFs and spreadsheets. The S3, raw upload, chunk, and browser completion options therefore work consistently across file sizes.
- Received originals and partial chunks survive transfer expiry. Repeating the identical preparation with current authorization renews access to the pending transfer and preserves its progress. Empty abandoned transfers can still expire and release reserved storage.
- Retrying a received original verifies its stored size and SHA-256. A missing, truncated, oversized, or changed stored copy can be repaired using the exact original. A storage outage is not mistaken for a missing file.
- Transfer status distinguishes pending, approved, and rejected files and reports counts of ready and missing originals. Switching from chunks to a whole-file transfer reports the correct completed offset.
- Resumed targets include their original file index. Reviewing a file does not shift the indices used by S3 or chunk uploads for the remaining files.
- Rejected files cannot receive a new upload target or be acknowledged as successfully completed through the S3 completion action.
- Browser upload and approval errors explain interrupted connections, unconfirmed server responses, rate limits, and expired sessions. After an ambiguous result, the user is told to refresh the queue before retrying. Confirmed approvals remain preserved.

The source-download guidance deployed earlier remains in place. An assistant should check an authorized source file viewer's Download action when the project API supplies only extracted text. It must not silently substitute extracted text for an original.

## Verification

The full Python integration suite passed **100 tests**, including 31 added regression cases. Tests use isolated database workspaces and synthetic originals, not the business team's approval queue.

| Area | Verified behavior |
| --- | --- |
| Original preservation | All 12 supported extensions retain their original bytes, Unicode filenames, spaces, and subfolder paths. Valid PDF and XLSX fixtures also complete transfer, approval, and download without conversion. |
| Size and capacity | An exact 100 MiB original passes the HTTP transfer path. Oversized files, capacity exhaustion, invalid nesting, filename collisions, and batches above 100 files are rejected. |
| Interrupted transfers | Partial chunks can resume. Wrong offsets, incorrect lengths, wrong hashes, conflicting chunks, and checksum failures cannot mark a file received. |
| Expiry and recovery | Received files and partial chunks survive cleanup. Renewing the same pending inventory retains its identity and progress. Empty abandoned transfers release reserved storage. |
| Duplicate delivery | Concurrent retries produce one received event. A completed transfer can be retried without duplicating the original. Switching transfer methods preserves the exact file. |
| Storage failure | Simulated storage failures leave the transfer retryable. Missing and corrupted stored originals can be restored. Approval rollback preserves database consistency. |
| Direct S3 | Signed policies bind the upload to the expected size and checksum. Completion rejects missing or incorrect objects and unauthorized callers. |
| GitHub | Downloads preserve original bytes. Wrong sizes and hashes, expired or denied URLs, redirects, and unsafe hosts cannot become received originals. Temporary URL credentials are not stored in proposal payloads. |
| Access | Authentication, role checks, CSRF, revoked upload identities, private files, and workspace isolation remain enforced. |
| Approvals | Each received file can be approved independently. Missing originals are excluded. Stale or conflicting proposals cannot overwrite different contents. A 59-item queue completes in successive groups. |
| Request bodies | Chunked requests without a Content-Length header are bounded. A disconnected request does not reach the upload handler. |
| Processing | Extraction failure does not destroy the original. Malformed PDF and XLSX containers remain downloadable. Deletion cannot be reversed by an indexing worker. |

The automated browser run passed the following checks:

- Both themes fit 320, 360, 390, 768, and 1440 pixel viewports, including all three approval filters.
- The theme toggle works by keyboard, survives reload, synchronizes across tabs, follows the initial system preference, and still works when browser storage is blocked.
- Incorrect originals remain unapproved. The correct original can be uploaded after a failed attempt and moves into the ready queue.
- Simulated network interruption, HTML gateway errors, rate limits, and expired sessions produce actionable messages and re-enable controls.
- When an approval commits but its response is lost, the refreshed queue removes the completed items. A retry approves only the remaining items.
- The approval panel remains visible when empty, mobile dialogs fit the viewport, and the browser reports no JavaScript errors.

Desktop and mobile screenshots were inspected in both themes.

Live deployment checks also passed an exact 17,940,000-byte direct S3 upload, rejection of an incorrect S3 upload, duplicate completion, renewal after expiry, and a 168,000-byte upload through the public HTTPS endpoint. The downloaded originals matched their expected hashes. Public theme assets matched the deployed source. Only the temporary test transfer was rejected and purged afterward; business files were not approved or modified.

## Reproducing the checks

Use the configured local test database, with all migrations applied. Run from the repository root:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q
```

For browser checks, start the synthetic preview in one terminal:

```sh
PYTHONPATH=src .venv/bin/python scripts/test-upload-ui.py
```

With Playwright available to Node, run in another terminal:

```sh
node scripts/test-upload-ui.cjs
```

Restart the preview before repeating the browser suite because the test deliberately approves its own synthetic files. Its URL is fixed to localhost. Screenshots are written under `work/upload-audit`.

## Boundaries

These tests cover the implemented upload paths and injected failures. They do not prove that every future provider outage, browser behavior, or source API change is impossible.

Central Brain cannot obtain original bytes that an external provider does not expose to the current assistant. It also cannot override ChatGPT or Claude's tool permissions or network restrictions. Such files must remain visibly incomplete until an authorized download, import, or browser upload delivers and verifies the original. A filename, extracted text, or a source-chat reference is not proof that an original was received.

File storage and text indexing are separate. Scanned, encrypted, malformed, or unsupported documents may not be searchable even when their original bytes are safely stored. The existing upload allowance remains 100 MiB per file and 100 GiB of shared storage, including versions and pending reservations. Pending transfers that contain progress retain their reservations until reviewed or rejected.
