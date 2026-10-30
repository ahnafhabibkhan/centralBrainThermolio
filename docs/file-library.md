# Central Brain file library

Open `/library` after signing in. Create a folder, open it, and use **Upload file** or drag one file onto the upload card. The original remains downloadable while text extraction runs. Select **Manage** to rename or move an item, preview text, download an older original, or upload a new version.

## What is stored

Private S3 objects contain unchanged originals under generated immutable keys. PostgreSQL contains folder relationships, versions, checksums, permissions, processing status, and extracted sections. Display names do not become S3 keys. Renaming or moving a file preserves its ID and original bytes. Approved memories appear as virtual Markdown files backed by the existing memory records, so approval and deletion still apply.

Workspace files are shared with authorized identities in the workspace. Private files are accessible only to the creating Central Brain identity. ChatGPT and Claude use the identity selected during Central Brain OAuth sign-in, which can differ from their own account email. Folders are workspace organization containers; they do not broaden private file access.

Direct file access also requires an accessible path through active parent folders. Moving a shared file or its parent beneath a private or sensitivity-restricted folder removes access for identities that cannot access that path. File reads, historical versions, previews, downloads, exports, and mutations apply this restriction.

Your uploads become available immediately, with search available after processing. Assistant-created Markdown or text files require review. Folder creation and move suggestions also require review. Existing memory proposals continue through the memory approval queue.

## Pilot limits

- Originals are limited to 100 MiB each and 100 GiB per workspace, including retained versions and rejected file originals.
- The pilot allows up to 2,000 library nodes for new library operations. Existing memories remain governed by the memory service.
- Supported formats are PDF, DOCX, XLSX, CSV, Markdown, and UTF-8 plain text. Legacy DOC/XLS, images, OCR, and password-protected files are not supported for extraction.
- Each file is limited to 1 million extracted characters, 500 sections, and 500 PDF pages. Each worksheet processes up to 100 columns and 10,000 rows, subject to the section and text limits. Expanded Office archives are capped at 100 MiB.
- The workspace text index is capped at 20 million characters across retained versions. Partial extraction is clearly labeled. Files that fail extraction retain their downloadable originals.
- One worker processes jobs sequentially. Each extractor has a 60-second wall timeout, a 45-second CPU limit, and a 384 MiB address-space limit on Linux. A processing job can be reclaimed after five minutes if interrupted.

## Assistant tools

`search_library` searches indexed document text, names, folder paths, and approved memories. It returns up to eight excerpts with file IDs, paths, versions, and source locations. `list_folder` lists up to 100 items with an offset. `get_file_info` reports versions and indexed worksheet names. `read_file_sections` retrieves up to three sections. `read_spreadsheet_rows` returns at most 20 indexed rows and 16,000 content characters. Neither service automatically reads all uploaded content or captures every conversation.

`read_image` returns actual image content to vision-capable assistants over the authenticated MCP connection. It accepts an approved JPEG, PNG, WebP, or HEIC file ID and an optional version. HEIC images are converted to JPEG previews. The original bytes and checksum remain unchanged. Existing reader, workspace, private-file, sensitivity, and approval restrictions apply. No public image URL or new credential is created.

The default preview has a maximum edge of 1600 pixels. Set `max_edge` between 512 and 2400, or pass `crop: [left, top, right, bottom]` with coordinates from 0 to 1 relative to the upright original, to inspect small bill figures or labels. Source images are limited to 32 MiB and 40 million pixels. Previews are limited to 2 MiB. Conversion runs in a bounded subprocess, and concurrent callers may need to retry. Source EXIF and GPS metadata are omitted. Preview retrieval is audited. This provides visual access rather than OCR or automatic image indexing. Follow all document pagination separately.

After deployment, refresh the Central Brain connection in Claude if `read_image` is absent from its tool list. Confirm that the tool returns an image, then verify a visible detail from the image. A stored-image placeholder is not visual access.

Use `propose_file` only for a generated Markdown or text note containing at most 20,000 characters. It appends the source reference and therefore does not preserve the exact original bytes. For a finished document already present in a code filesystem, prefer `prepare_chat_archive_upload` so that Central Brain verifies its byte size and SHA-256 before preserving the raw bytes. Use `propose_chat_archive` only when exact UTF-8 content is already available inline, each original contains at most 200,000 characters, and the full proposal fits within 240 KB. Use `suggest_organization` for pending folder or move suggestions. The memory and skill tools remain available.

After deployment, refresh the Central Brain connector's tools in Claude and the plugin's information in ChatGPT. Start a fresh chat if a client retains its old tool list.

## Local operation and deployment

Apply migration `004_library.sql` using the migration role. The local bootstrap command includes it. Start the web app normally and run `PYTHONPATH=src .venv/bin/python -m central_brain.library_worker` as a separate local process. Local originals default to `work/library`; production requires `LIBRARY_BUCKET`.

Production uses the existing EC2 instance plus a private S3 bucket. The `library-worker` Compose service runs the queue. A host systemd timer renews a storage-only assumed role every 30 minutes. The containers receive a read-only credentials directory and have instance metadata access disabled. The S3 role cannot access database backups, SSM parameters, or other buckets. Objects use S3-managed AES256 encryption and the bucket denies public and non-TLS access.

The worker discovers invited identities only within workspaces administered by configured identities. It refreshes this registry between processing cycles after at least 30 seconds have elapsed. Queue operations use reader and writer roles under the existing workspace and private-record policies. Failed identity refreshes temporarily remove discovered identities until a later refresh succeeds, and failed deletion attempts do not prevent extraction. Configured identities retain precedence over database entries for the same actor.

The database was backed up before migration. Do not roll back by dropping library tables or deleting S3 objects. Restore the previous application image and Compose configuration if necessary; the additive migration remains compatible with the previous memory service. Preserve both database backups and all original objects for recovery. The application retains originals and their versions; an operator-approved retention or purge procedure is needed to reclaim quota in this pilot.

Run `PYTHONPATH=src .venv/bin/pytest -q --tb=short` for the database-backed suite. `scripts/verify-library-container.py` checks extraction in the non-root Linux release image against the local test database. Deployment validation also verifies live S3 upload, byte-identical download, background extraction, and folder-filtered search using a clearly labeled demo document.
