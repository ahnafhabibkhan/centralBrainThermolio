# ruff: noqa: F401, F811
import base64
import io
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from pillow_heif import register_heif_opener
from test_library import library
from test_pilot import pilot

from central_brain.auth import AuthContext
from central_brain.image_access import read_image
from central_brain.image_preview import render


def image_bytes(format="PNG"):
    register_heif_opener()
    image = Image.new("RGB", (120, 80), "red")
    image.paste("blue", (60, 0, 120, 80))
    buffer = io.BytesIO()
    image.save(buffer, format=format)
    return buffer.getvalue()


def test_preview_orientation_crop_and_metadata(tmp_path):
    original = Image.new("RGB", (80, 40), "green")
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "Metadata must not be returned"
    source, target = tmp_path / "source.jpg", tmp_path / "preview.jpg"
    original.save(source, exif=exif)
    details = render(source, target, 512, None)
    assert (details["oriented_original_width"], details["oriented_original_height"]) == (40, 80)
    with Image.open(target) as result:
        assert result.size == (40, 80)
        assert not result.getexif()
    details = render(source, target, 512, [0, 0, 1, .5])
    assert (details["preview_width"], details["preview_height"]) == (40, 40)


@pytest.mark.parametrize("format,extension", [("PNG", ".png"), ("JPEG", ".jpeg"),
                                             ("WEBP", ".webp"), ("HEIF", ".heic")])
def test_reader_can_view_original_pixels_without_reviewer_role(pilot, library, format, extension):
    original = image_bytes(format)
    node = library.upload(pilot.auth, "asset" + extension, original)
    reader = AuthContext(pilot.settings.principals()["assistant"])
    result = read_image(library, reader, node, crop=[.5, 0, 1, 1])
    assert [r.type for r in result] == ["text", "image"]
    assert result[1].mimeType == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(result[1].data))) as image:
        assert image.size == (60, 80)
        red, _green, blue = image.getpixel((30, 40))
        assert blue > 200 and red < 30
    metadata = json.loads(result[0].text)
    assert metadata["file_id"] == str(node) and metadata["version"] == 1
    assert "object_key" not in metadata
    with library.download(pilot.auth, node)[1] as stream:
        assert stream.read() == original


def test_preview_preserves_access_and_approval_boundaries(pilot, library, monkeypatch):
    private = library.upload(pilot.auth, "private.png", image_bytes(), visibility="private")
    pending = library.upload(pilot.auth, "pending.png", image_bytes(), proposed=True)
    shared = library.upload(pilot.auth, "shared.png", image_bytes())

    def unexpected_storage_read(key):
        pytest.fail("Storage was read before authorization was established")

    monkeypatch.setattr(library.store, "get", unexpected_storage_read)
    cases = [(AuthContext(pilot.settings.principals()["colleague"]), private),
             (AuthContext(pilot.settings.principals()["outsider"]), shared),
             (pilot.auth, pending), (pilot.auth, uuid4()),
             (AuthContext(pilot.auth.principal.model_copy(update={"roles": {"writer"}})), shared),
             (AuthContext(pilot.auth.principal.model_copy(update={"sensitivities": ["public"]})), shared)]
    for auth, node in cases:
        with pytest.raises(HTTPException) as error:
            read_image(library, auth, node)
        assert error.value.status_code in {403, 404}


def test_versions_and_invalid_image_inputs(pilot, library):
    node = library.upload(pilot.auth, "image.png", image_bytes())
    library.upload(pilot.auth, "image.png", b"corrupt image", node_id=node)
    assert json.loads(read_image(library, pilot.auth, node, version=1)[0].text)["version"] == 1
    with pytest.raises(HTTPException) as error:
        read_image(library, pilot.auth, node)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        read_image(library, pilot.auth, node, version=99)
    assert error.value.status_code == 404
    text = library.upload(pilot.auth, "text.txt", b"Not an image")
    for selected, kwargs in [(text, {}), (node, {"max_edge": 9999}),
                             (node, {"crop": [0, 0, 2, 1]}),
                             (node, {"crop": [1, 0, 0, 1]}),
                             (node, {"crop": [0, 0, 1]}),
                             (node, {"crop": [0, 0, float("nan"), 1]})]:
        with pytest.raises(HTTPException) as error:
            read_image(library, pilot.auth, selected, **kwargs)
        assert error.value.status_code == 422


def test_source_size_checksum_and_busy_limits(pilot, library, monkeypatch):
    import central_brain.image_access as access

    node = library.upload(pilot.auth, "asset.png", image_bytes())
    monkeypatch.setattr(access, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(HTTPException) as error:
        read_image(library, pilot.auth, node)
    assert error.value.status_code == 413
    monkeypatch.setattr(access, "MAX_SOURCE_BYTES", 32 * 1024 * 1024)
    access._conversion_slot.acquire()
    try:
        with pytest.raises(HTTPException) as error:
            read_image(library, pilot.auth, node)
        assert error.value.status_code == 503
    finally:
        access._conversion_slot.release()
    monkeypatch.setattr(library.store, "get", lambda key: io.BytesIO(b"changed"))
    with pytest.raises(HTTPException) as error:
        read_image(library, pilot.auth, node)
    assert error.value.status_code == 409


def test_mcp_response_contains_image_block_and_rejects_unauthenticated_requests(pilot, library):
    node = library.upload(pilot.auth, "wire.png", image_bytes())
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "read_image", "arguments": {"file_id": str(node)}}}
    assert pilot.client.post("/mcp/", json=request).status_code == 401
    response = pilot.client.post("/mcp/", headers={"Authorization": "Bearer assistant",
                                 "Accept": "application/json, text/event-stream"}, json=request)
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), result
    assert [c["type"] for c in result["content"]] == ["text", "image"]
    with Image.open(io.BytesIO(base64.b64decode(result["content"][1]["data"]))) as image:
        assert image.size == (120, 80)
