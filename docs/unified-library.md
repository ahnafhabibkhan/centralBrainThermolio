# Unified Library

The Library is the signed-in home page. Its sidebar shows the folder hierarchy, including the top-level Memories and Skills folders. Search is part of the folder view. Getting started and Sign out are header utilities.

Upload ordinary documents in the root or a project folder. Upload Markdown memories in Memories or use Upload a memory in the sidebar. Memory files must be UTF-8 .md files containing at most 20,000 characters. Every memory upload remains a proposal until a reviewer approves it.

To change a memory, download its .md file, edit it, and upload the replacement from its review page. The old approved memory remains active until the replacement is approved. The database retains the revision relationship. Memory Markdown is stored as database content and exposed as downloadable .md files in the library, rather than duplicated into S3. Existing assistant proposal tools remain compatible and produce the same Markdown representation.

Skills opens the actual Skills folder, so uploaded procedure files appear immediately and become searchable when extraction finishes. Approved imported procedures are displayed in the same view. Library files use search_library and read_file_sections. Imported registry procedures also use list_skills and get_skill. Uploading a procedure file does not execute it or automatically register it as an executable skill.

Memories and Skills are fixed top-level folder names. Existing manually organized memory files retain their destination. Root-level memory projections are grouped in Memories. Workspace row-level permissions continue to govern the tree, files, and search results.

The release requires no new AWS resources or database schema migration. Validation includes Markdown format rejection, download fidelity, replacement approval, folder navigation, Skills visibility, and library search.
