"""Receive exact GitHub originals without relaying binary data through chat."""
import http.client
import time
import math
from email.utils import parsedate_to_datetime
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
    target = parsed.path + ('?' + parsed.query if parsed.query else '')
    for attempt in range(3):
        connection = http.client.HTTPSConnection('raw.githubusercontent.com', timeout=20)
        deadline = time.monotonic() + 60
        try:
            connection.request('GET', target, headers={'User-Agent': 'Central-Brain', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (429, 500, 502, 503, 504):
                retry_after = response.getheader('Retry-After') if hasattr(response, 'getheader') else None
                try:
                    delay = max(0, int(retry_after)) if retry_after else 2 ** attempt
                except ValueError:
                    try:
                        delay = max(0, math.ceil(parsedate_to_datetime(retry_after).timestamp() - time.time()))
                    except (TypeError, ValueError, OverflowError):
                        delay = 60
                if attempt < 2 and delay <= 5:
                    connection.close()
                    time.sleep(delay)
                    continue
                raise HTTPException(429 if response.status == 429 else 503,
                                    f'GitHub is temporarily unavailable or rate limited. Retry only this file after {delay} seconds; received files are preserved.',
                                    headers={'Retry-After': str(delay)})
            if response.status in (401, 403, 404):
                raise HTTPException(409, 'GitHub could not authorize or locate this original. The download link may have expired, or repository access, path or commit may be wrong. Obtain a fresh download_url for this exact file and commit immediately before importing it. Retry only this file; received files are preserved.')
            if response.status != 200:
                raise HTTPException(409, 'GitHub returned an unsupported response. Obtain a fresh raw download_url for this exact file and commit. Redirects are not followed.')
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
            if attempt == 2:
                raise HTTPException(502, 'The GitHub connection failed after three attempts. Retry only this original with a fresh download_url; received files are preserved.') from None
            connection.close()
            time.sleep(2 ** attempt)
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
