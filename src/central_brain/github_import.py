"""Receive exact GitHub originals without relaying binary data through chat."""
import http.client
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from .project_transfer import original_intact


def download_original(url, size):
    try:
        parsed = urlsplit(url)
        valid = (len(url) <= 8192 and not any(c.isspace() for c in url)
                 and parsed.scheme == 'https' and parsed.hostname == 'raw.githubusercontent.com'
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.fragment and len(parsed.path.strip('/').split('/')) >= 4)
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(422, 'Provide a fresh raw.githubusercontent.com download_url from GitHub, not a page link.')
    # A fixed HTTPS host and no redirects prevent fetching arbitrary or internal URLs.
    # http.client does not log URLs, which may contain temporary GitHub download credentials.
    connection = http.client.HTTPSConnection('raw.githubusercontent.com', timeout=20)
    deadline = time.monotonic() + 60
    try:
        target = parsed.path + ('?' + parsed.query if parsed.query else '')
        connection.request('GET', target, headers={'User-Agent': 'Central-Brain', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status != 200:
            raise HTTPException(409, 'GitHub did not provide the original. Obtain a fresh download_url with repository access and retry.')
        data = bytearray()
        while len(data) <= size:
            if time.monotonic() > deadline:
                raise HTTPException(504, 'GitHub transfer timed out. Retry this original.')
            chunk = response.read(min(65536, size + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != size:
            raise HTTPException(422, 'The GitHub original does not match its registered size.')
        return bytes(data)
    except (OSError, http.client.HTTPException):
        raise HTTPException(502, 'The GitHub original could not be downloaded. Retry with a fresh download_url.') from None
    finally:
        connection.close()


def import_original(library, auth, archive_id, file_index, download_url):
    auth.require('writer')
    with library.repo._connection(auth) as c:
        row = c.execute("SELECT payload FROM central_brain.library_suggestions WHERE id=%s AND created_by=%s "
                        "AND status='proposed'", (archive_id, auth.principal.actor_id)).fetchone()
        if not row or not row['payload'].get('transfer_inventory'):
            raise HTTPException(404, 'Pending project transfer not found.')
        payload = row['payload']
        if not 0 <= file_index < len(payload['files']):
            raise HTTPException(404, 'Original not found.')
        if payload.get('reviews', {}).get(str(file_index)) == 'rejected':
            raise HTTPException(409, 'This original was rejected.')
        item = payload['files'][file_index]
        if item['received'] and original_intact(library, item):
            return {'received': item['name'], 'duplicate': True}
        if time.time() >= payload['transfer_expires']:
            raise HTTPException(409, 'This transfer expired. Prepare the archive again.')
    data = download_original(download_url, item['size_bytes'])
    from .project_transfer import store_original
    # Rechecks ownership and review state, then verifies SHA-256 before storing.
    return store_original(library, auth, archive_id, file_index, data)
