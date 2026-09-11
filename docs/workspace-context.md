# Persistent workspace and context

The signed-in Library now operates on one page. Expanding folders reveals nested files with connecting branch lines. Selecting a folder updates its contents in place. File details, memory history, editing and Getting started open in a dialog. Sign out remains in the header.

The approvals table is always present, including when its queue is empty. It shows accessible proposed files, memories and organization changes across the workspace, rather than only the selected folder. Uploaded human documents become available immediately. Assistant proposals still require approval.

Markdown files under Skills are discoverable through both the library tools and list_skills/get_skill. For old approved root files named SKILL.md or ending in _SKILL.md, folder initialization moves them into Skills when the destination has no naming collision. Approval also applies this convention. Other procedures can be uploaded directly into Skills. Classification follows the filename and folder, rather than using a paid AI classifier.

get_workspace_context returns an access-filtered revision, counts, folder map and recent files. The revision reflects versions, memory contents and metadata, approval changes, and extraction state. It is recalculated from current database records. The browser checks every 20 seconds while visible and updates after its own writes. Unfinished forms are preserved. Assistants receive current information when they call the tools; existing conversation text is not rewritten or pushed into their apps.

Context excludes inaccessible, expired and unapproved content from assistant retrieval. Reviewers can see their accessible pending queue. Large maps are explicitly marked as truncated. Search still returns bounded excerpts, and exact skill versions retain their source paths and truncation flags.

The supplied thermolio-doc-style skill informs the typography, charcoal text, thin rules, muted captions and green table headers. User-requested folder controls and the existing Thermolio header remain part of the web interface. The uploaded original is stored unchanged in private S3 and indexed as a reference document. Its instructions are not executed by the server.

Validation covers Markdown memory replacements, approval transitions, skill placement and retrieval, original download fidelity, context changes after indexing and versions, and privacy filtering. No new AWS service is required.
