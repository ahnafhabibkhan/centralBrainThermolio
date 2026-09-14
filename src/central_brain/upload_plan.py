"""Choose a transfer route before reserving storage or creating proposals."""
from typing import Literal

SourceAccess = Literal['code_bytes', 'github_download', 'browser_download', 'text_only', 'unknown']


def plan_upload(source_access: SourceAccess, transfer_permitted: bool,
                inventory_complete: bool, public_url: str) -> dict:
    result = {
        'source_access': source_access,
        'inventory_complete': inventory_complete,
        'creates_proposals': False,
        'capabilities_verified_by_server': False,
        'completion_url': public_url + '/library#approvals',
        'guidance': 'This plan uses capabilities reported by the host, not a server probe. '
                    'Verify one original through the chosen route before starting remaining batches. '
                    'Reuse existing transfers and reconcile received, missing and inaccessible originals. '
                    'Never report a whole project complete without its full inventory.',
    }
    if not transfer_permitted:
        return {**result, 'ready_to_prepare': False, 'route': 'needs_supported_permission',
                'next_action': 'The host has not permitted this transfer. Use its supported permission '
                               'flow. Do not retry a denied action through another host, script or encoding '
                               'to evade the denial. Do not create another empty transfer.'}
    routes = {
        'code_bytes': ('raw_upload', 'Prepare checksum-bound originals, upload exact bytes from code, and verify receipt.'),
        'github_download': ('github_import', 'Obtain fresh authorized GitHub download URLs at the inventoried commit. '
                            'Prepare the inventory and import each original through import_github_original.'),
        'browser_download': ('browser_file_picker', 'Use the source viewer Download action and a supported browser '
                             'file picker to upload the downloaded original. Check that the host supports both '
                             'steps before preparation. Do not POST signed-in page data to another origin with '
                             'a page script. In the workspace, Finish multiple uploads accepts matching originals '
                             'together and preserves received files. This route can require the user to sign in '
                             'or select files if the host cannot operate downloads and file pickers.'),
        'text_only': ('original_access_needed', 'Extracted text is not an original. Check the source file viewer '
                     'for a supported Download action or an authorized source connector. Do not request another '
                     'local folder search based on an old failed search, and do not create a transfer yet.'),
        'unknown': ('inspect_source', 'Inspect the source and available host tools first. Do not create upload '
                    'proposals until an original-file access and transfer route is established.'),
    }
    route, action = routes[source_access]
    return {**result, 'ready_to_prepare': source_access in {'code_bytes', 'github_download'},
            'route': route, 'next_action': action}
