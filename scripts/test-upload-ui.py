import sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_pilot import pilot
from test_project_transfer import prepare
from central_brain.library import Library
from central_brain.project_transfer import store_original
from central_brain.api import create_app
from central_brain.repository import PostgresMemoryRepository
import uvicorn
from uuid import UUID
fixture = pilot.__wrapped__()
p = next(fixture)
p.settings.public_url = 'http://127.0.0.1:8087'
p.settings.library_local_path = str(Path('work/upload-audit/originals').resolve())
library = Library(p.repo, p.settings)
roots, _ = library.workspace_folders(p.auth)
folder = library.folder(p.auth, 'Projects')
sub = library.folder(p.auth, 'Sales and investor materials', folder)
library.upload(p.auth, 'Project notes.md', b'# Project notes\nSynthetic preview content.', sub)
library.upload(p.auth, 'Writing style.md', b'# Style\nUse clear sentences.', roots['Skills'])
prepared = prepare(p, library, {'Source files/Investor presentation.pdf': b'%PDF test' * 20000,
                              'Source files/Financial forecast.xlsx': b'xlsx original' * 15000}, destination_confirmed=True)
store_original(library, p.auth, UUID(str(prepared['id'])), 0, b'%PDF test' * 20000)
for i in range(6): library.upload(p.auth, f'Approval check {i+1}.txt', b'Synthetic approval.', proposed=True)
repo = PostgresMemoryRepository(p.settings.database_url)
repo.open()
try: uvicorn.run(create_app(repo, p.settings), host='127.0.0.1', port=8087, log_level='warning')
finally:
    repo.close()
    try: next(fixture)
    except StopIteration: pass
