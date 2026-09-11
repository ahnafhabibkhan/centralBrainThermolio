# Deletion and chat context

Open an item, or choose Folder settings, and select Delete. The confirmation shows the path, counts of affected folders, files and memories, original version count, and storage size. Type the exact name to confirm. Deleting a folder includes its descendants and pending proposals. Changes made after the warning invalidate the confirmation. The permanent Memories and Skills roots remain available; their contents can be deleted.

Deletion requires an administrator session and a valid CSRF token. It runs as one database transaction. A hidden descendant or another reference that prevents deletion rolls back the entire operation. Text sections and file-version records are removed, memory contents are purged, and retrieval immediately stops returning the removed items. A durable queue lets the existing worker delete original objects and retry temporary storage failures. An extraction job already in progress cannot restore a deleted version.

Access-filtered deletion metadata, including item IDs and former paths, is retained for 30 days. The context tool returns the most recent 20 notices and indicates when more exist. Private deletion metadata stays private, and unapproved proposals are excluded from the assistant's notices. Minimal audit identifiers persist. Backups follow the existing retention policy. Deleting Central Brain content cannot remove copies already downloaded or quoted in a conversation.

The workspace context is a database-backed map, not a separate AI. It contains a revision, approved item counts, folder paths, recent files, and recent deletions. The browser refreshes after its own changes and checks for changes every 20 seconds while visible. If another session deletes the current folder, the page returns to Workspace and closes unavailable item details. Assistants get fresh context when they call `get_workspace_context`. A changed revision means they should search again and stop relying on deleted IDs. This does not push updates into every open conversation.

## Saving work from a chat

Use a conversation where the Central Brain connection is available and ask the assistant to save specific material into a named project. The server instructions now recommend inspecting existing destinations and using this convention:

```text
Projects/
  Boiler upgrade/
    Chats/
      2026-09-11 Design review/
        summary.md
        decisions.md
        Files/
          specification.pdf
          costs.xlsx
Memories/
  Memory-<id>.md
Skills/
  commissioning-checklist.md
```

This is a recommended layout, not an archive already created. The current connector proposes folders and text files separately. New folders need approval before files can be proposed inside them. It can send Markdown or plain-text content of up to 20,000 characters per proposed file, with a source reference. It cannot automatically collect every attachment from a chat. Upload original PDFs, Word documents and spreadsheets through the platform, into the intended folder. Generated summaries should be clearly distinguished from original files.

A practical request is: “Use Central Brain to save a summary and the confirmed decisions from this chat under Projects / Boiler upgrade / Chats / 2026-09-11 Design review. Reuse existing folders, include this chat's source reference, and tell me which original attachments I still need to upload.” Review proposals in the top approval section. Approved files then appear in the folder map and become searchable after indexing.

Folder relationships and metadata live in PostgreSQL. Original file bytes live in private S3 objects under generated keys. The interface presents those objects in the logical folder hierarchy. GitHub contains application code, not private chat archives or uploaded documents.

A future improvement would bundle a chat summary, source manifest, folder creation and multiple text files into one reviewable archive proposal. That batch workflow is not implemented by this change.
