# Chat archives and bulk approval

The library has an Approve all button above the approval table. It approves only the proposals shown in that page snapshot, including memories, files, folder suggestions, and chat archives. A changed proposal or failed operation stops the entire batch. New arrivals are not silently included. The button remains visible but disabled when the queue is empty.

A chat archive is one proposal containing a folder path, a conversation summary, a source reference, and the original files actually transferred by the assistant. Approval creates missing folders and all files together. Existing folders are reused; existing files are not overwritten. When adding another conversation to the same folder, choose a distinct summary_filename such as logo-follow-up.md. Its companion source becomes logo-follow-up-source.md. A separate conversation subfolder is also supported.

For a logo conversation, the resulting arrangement can be:

```text
Brand/
  Thermolio/
    Logo/
      summary.md
      source.md
      thermolio-horizontal.svg
      thermolio-stacked.svg
      thermolio-mark.svg
      thermolio-horizontal-ink.png
```

The source file lists the exact transferred filenames, sizes, and SHA-256 checksums. Original SVG text is preserved without appending Markdown or source comments. PNG and other binary files are decoded from supplied base64 bytes. SVG is displayed as escaped source text and downloaded as an attachment, never executed inline in the library.

## Assistant workflow

1. The assistant inventories the original attachments that it can actually read in the current conversation.
2. It calls `find_folders` with the project and category. Related brand, logo, identity, and asset folder names are matched together. Results are limited to accessible folders.
3. If related folders exist, the assistant shows their paths and asks whether to reuse one or select a separate destination.
4. For small text originals, it calls `propose_chat_archive` with the agreed folder path, summary, source, and exact original contents. For binary files or opaque metadata, it calls `prepare_chat_archive_upload` with filenames, sizes, and SHA-256 checksums. Its code environment then sends the actual bytes directly using the returned 15-minute upload handoff. The server verifies every checksum before creating a proposal. Both tools return a destination-confirmation response if matching folders exist and confirmation was omitted.
5. A reviewer inspects the archive in the approval panel and approves it, individually or with Approve all.
6. The folder tree and context revision update immediately. File contents become searchable after the existing indexing worker processes them.

## Limits

The connector accepts up to 20 originals and a total encoded proposal payload of 240 KB. The direct handoff accepts up to 180 KB of raw originals, subject to the same encoded limit, and requires a code environment that can reach Central Brain over HTTPS. Its token permits only the declared files and destination, expires after 15 minutes, and cannot approve anything. The website accepts individual originals up to 50 MB within the existing library quota. Supported formats are PDF, DOCX, XLSX, CSV, MD, TXT, SVG, PNG, JPG, JPEG, and WEBP. Image files are stored and discoverable by path, but image text recognition is not enabled.

A chat attachment link does not transfer the file itself. If a host does not expose original bytes, or an archive exceeds the connector limit, the assistant must identify the missing files and ask for a website upload. It must not claim that a summary, a link, or a regenerated export is the original attachment.

Memories remain Markdown. Saving a memory alone does not create a category folder or copy conversation attachments. The archive tool supplies that separate workflow. Once an archive is approved or rejected, its original proposal content is cleared so it does not retain a second copy outside library deletion controls.

## Verification

The automated suite covers exact SVG and binary preservation, approval visibility, related-folder confirmation, folder reuse, duplicate proposals, stale batches, proposals arriving after page load, permission checks, source escaping, CSRF protection, conflicts, and rollback after a storage failure. It also verifies handoff expiry, checksum rejection, revoked access, and the requirement for a separate human approval. A mobile browser check covers archive inspection in the existing page, bulk approval, the empty queue, and the resulting folder tree.
