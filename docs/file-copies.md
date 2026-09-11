# Copy files between folders

Open an approved uploaded file and expand **Copy to folder**. Choose the destination, keep or edit the filename, and select **Copy file**. The original stays in its current folder. When viewing an older version, the form names and copies that specific version. Existing destination files are never overwritten.

Each copy receives a separate file ID, version history, and stored original. It retains the source visibility and sensitivity, or adopts stricter destination restrictions. It consumes storage within the existing quota. Editing or deleting the source does not change the copy. This operation applies to uploaded files, including Markdown documents and skills. Canonical memory records remain single approved records and do not gain independent copies that could bypass their expiry or approval state.

The `copied_from` metadata records the source file ID, exact version, and checksum. It appears in file information, folder listings, and recent context entries. The workspace revision changes immediately. Where the original already has extracted text, the copy reuses that index within the text quota. Otherwise, the normal indexing worker processes it.

Assistants can call `suggest_organization` with `action="copy"`, a source `file_id`, destination `folder_id`, and filename. This creates a human-reviewed proposal tied to the source version at proposal time. The approval row links to that version. Individual approval and Approve all both support copies. Refresh the connector tool list and start a fresh chat if its cached tool descriptions do not include this action.

The source reference records the copy's origin. It does not mean the copied file stays synchronized with its source. Agents should retrieve the current workspace revision and file version when checking for changes.
