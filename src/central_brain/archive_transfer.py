"""Short-lived, checksum-bound transfers from an assistant's code environment."""

import hashlib
import json
import time

import jwt
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .archives import ArchiveFile, find_folders, folder_parts, propose_archive
from .auth import AuthContext
from .config import Principal
from .library import EXTENSIONS, filename
from pathlib import Path


class OriginalManifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=240)
    size_bytes: int = Field(gt=0, le=180000)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def prepare_transfer(library, settings, auth, folder_path, summary, source_reference, files,
                     destination_confirmed=False, summary_filename='summary.md'):
    auth.require('writer')
    parts = folder_parts(folder_path)
    if parts[0].lower() == 'memories':
        raise HTTPException(422, 'Choose a category folder for original attachments.')
    if not 1 <= len(files) <= 20 or sum(f.size_bytes for f in files) > 180000:
        raise HTTPException(413, 'Use 1 to 20 originals totaling at most 180 KB for this handoff.')
    if not 1 <= len(summary) <= 2000 or not 1 <= len(source_reference) <= 1000:
        raise HTTPException(422, 'Provide a short summary and a source reference.')
    filename(summary_filename)
    if not summary_filename.lower().endswith('.md') or summary_filename.lower() == 'source.md':
        raise HTTPException(422, 'Choose a Markdown summary filename other than source.md.')
    names = {summary_filename.lower(), 'source.md' if summary_filename == 'summary.md'
             else (Path(summary_filename).stem + '-source.md').lower()}
    for item in files:
        filename(item.name)
        if Path(item.name).suffix.lower() not in EXTENSIONS or item.name.lower() in names:
            raise HTTPException(422, 'Use supported, distinct original filenames.')
        names.add(item.name.lower())
    candidates = find_folders(library, auth, ' '.join(parts))
    if candidates['matches'] and not destination_confirmed:
        return {'status': 'needs_destination_confirmation', 'stored': False, **candidates}
    now = int(time.time())
    claims = {'iss': 'central-brain:archive-transfer', 'aud': settings.public_url,
              'iat': now, 'exp': now + 900,
              'principal': auth.principal.model_copy(update={'roles': {'reader', 'writer'}}).model_dump(mode='json'),
              'archive': {'folder_path': '/'.join(parts), 'summary': summary,
                          'source_reference': source_reference, 'summary_filename': summary_filename,
                          'destination_confirmed': destination_confirmed},
              'files': [item.model_dump() for item in files]}
    return {'status': 'awaiting_transfer', 'upload_url': settings.public_url + '/archive-transfer',
            'upload_token': jwt.encode(claims, settings.session_secret, algorithm='HS256'),
            'expires_in_seconds': 900,
            'instruction': 'Use your code tool to read the original files directly and POST JSON {"files": '
                           '[{"name": "original filename", "encoding": "base64", "content": "encoded bytes"}]} '
                           'to upload_url with Authorization: Bearer <upload_token>. Keep the token out of the final '
                           'chat response. Do not transcribe binary content through model output. The exact names, '
                           'sizes and SHA-256 values must match. Success creates only a pending human approval. '
                           'If your code environment cannot reach this URL, report that limitation.'}


def receive_transfer(library, settings, token, body):
    try:
        claims = jwt.decode(token, settings.session_secret, algorithms=['HS256'],
                            audience=settings.public_url, issuer='central-brain:archive-transfer',
                            options={'require': ['exp', 'iat', 'iss', 'aud']})
    except jwt.PyJWTError as exc:
        raise HTTPException(401, 'This upload handoff is invalid or expired.') from exc
    try:
        value = json.loads(body)
        if not isinstance(value, dict) or set(value) != {'files'}:
            raise ValueError('Expected original files only.')
        files = TypeAdapter(list[ArchiveFile]).validate_python(value['files'])
        expected = {item['name']: item for item in claims['files']}
        if len(files) != len(expected) or {f.name for f in files} != set(expected):
            raise ValueError('The original inventory changed.')
        for item in files:
            data = item.data()
            if len(data) != expected[item.name]['size_bytes'] or hashlib.sha256(data).hexdigest() != expected[item.name]['sha256']:
                raise ValueError('An original did not match its checksum or size.')
    except (ValueError, KeyError, ValidationError) as exc:
        raise HTTPException(422, 'The uploaded originals do not match the approved transfer inventory.') from exc
    identity = Principal.model_validate(claims['principal'])
    registered = settings.oauth_principals() if settings.environment == 'production' else settings.principals()
    current = next((p for p in registered.values() if p.actor_id == identity.actor_id
                    and p.workspace_id == identity.workspace_id and 'writer' in p.roles), None)
    if current is None:
        raise HTTPException(403, 'The upload identity no longer has workspace write access.')
    auth = AuthContext(current.model_copy(update={'roles': {'reader', 'writer'},
                       'sensitivities': list(set(current.sensitivities) & set(identity.sensitivities))}))
    return propose_archive(library, auth, files=files, **claims['archive'])
