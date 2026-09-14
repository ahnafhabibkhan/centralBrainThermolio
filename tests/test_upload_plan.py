import pytest
from central_brain.upload_plan import plan_upload


@pytest.mark.parametrize('source', ['code_bytes', 'github_download', 'browser_download', 'text_only', 'unknown'])
def test_denied_host_never_proposes_another_transfer(source):
    result = plan_upload(source, False, True, 'https://brain.example')
    assert result['route'] == 'needs_supported_permission'
    assert not result['ready_to_prepare']
    assert not result['creates_proposals']


@pytest.mark.parametrize('source,route,ready', [
    ('code_bytes', 'raw_upload', True),
    ('github_download', 'github_import', True),
    ('browser_download', 'browser_file_picker', False),
    ('text_only', 'original_access_needed', False),
    ('unknown', 'inspect_source', False),
])
def test_routes_do_not_assume_access_or_claim_server_verification(source, route, ready):
    result = plan_upload(source, True, False, 'https://brain.example')
    assert result['route'] == route
    assert result['ready_to_prepare'] is ready
    assert not result['inventory_complete']
    assert not result['capabilities_verified_by_server']
