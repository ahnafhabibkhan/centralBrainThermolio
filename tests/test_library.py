import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from test_pilot import pilot, memory
from central_brain.auth import AuthContext
from central_brain.extract import extract
from central_brain.library import Library
from central_brain.library_worker import process_one


@pytest.fixture
def library(pilot, tmp_path):
    pilot.settings.library_local_path = str(tmp_path)
    return Library(pilot.repo, pilot.settings)


def test_upload_version_search_and_original(pilot, library):
    auth = pilot.auth
    folder = library.folder(auth, "180 Bloor")
    original = b"Heating decision: install efficient boiler controls."
    node = library.upload(auth, "Decision.txt", original, folder)
    assert library.read(auth, node)["state"] == "queued"
    assert process_one(library, auth)
    hit = library.search(auth, "boiler", folder)["results"][0]
    assert hit["id"] == node and hit["path"] == "/180 Bloor/Decision.txt"
    name, stream = library.download(auth, node)
    with stream:
        assert stream.read() == original
    library.upload(auth, "Decision.txt", b"Cooling upgrade approved.", node_id=node)
    assert not library.search(auth, "boiler")["results"]
    assert process_one(library, auth)
    assert library.search(auth, "cooling")["results"][0]["version"] == 2
    assert library.read(auth, node, version=1)["sections"][0]["content"] == original.decode()
    assert library.usage(auth)["used"] == len(original) + len(b"Cooling upgrade approved.")
    library.move(auth, node, "Decision-renamed.txt", None)
    assert library.info(auth, node)["path"] == "/Decision-renamed.txt"
    assert not library.search(auth, "cooling", folder)["results"]


def test_private_proposals_and_memory_projection(pilot, library):
    auth = pilot.auth
    other = AuthContext(auth.principal.model_copy(update={"actor_id": uuid4()}))
    node = library.upload(auth, "Private.txt", b"Secret building notes.", visibility="private")
    assert process_one(library, auth)
    assert library.listing(other) == []
    assert library.search(other, "building")["results"] == []
    with pytest.raises(HTTPException):
        library.read(other, node)
    proposed = library.upload(auth, "Proposal.md", b"Proposed preference.", proposed=True)
    assert process_one(library, auth)
    with pytest.raises(HTTPException):
        library.read(auth, proposed)
    assert library.search(auth, "preference")["results"] == []
    library.review(auth, proposed, True)
    assert library.search(auth, "preference")["results"]
    item = pilot.repo.create(auth, memory())
    with pytest.raises(HTTPException):
        library.read(auth, item.memory_id)
    pilot.repo.approve(auth, item.memory_id)
    assert library.read(auth, item.memory_id)["sections"]
    library.move(auth, item.memory_id, "Preference.md", None)
    pilot.repo.transition(auth, item.memory_id, "delete")
    with pytest.raises(HTTPException):
        library.read(auth, item.memory_id)


def test_limits_cycles_and_quota_race(pilot, library):
    auth = pilot.auth
    a = library.folder(auth, "A")
    b = library.folder(auth, "B", a)
    with pytest.raises(HTTPException):
        library.move(auth, a, "A", b)
    with pytest.raises(HTTPException):
        library.folder(auth, "A")
    with pytest.raises(HTTPException):
        library.upload(auth, "../escape.txt", b"x")
    with pytest.raises(HTTPException):
        library.upload(auth, "macro.xlsm", b"x")
    pilot.settings.library_file_bytes = 10
    with pytest.raises(HTTPException):
        library.upload(auth, "big.txt", b"x" * 11)
    pilot.settings.library_quota_bytes = 10

    def put(index):
        try:
            library.upload(auth, f"{index}.txt", b"x" * 8)
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(put, range(2))) == 1


def test_extract_formats_and_bounds(tmp_path):
    text = tmp_path / "text"
    text.write_text("x" * 1001000)
    assert extract(text, ".txt")["state"] == "partial"
    text.write_text("name,value\nboiler,42\n")
    assert extract(text, ".csv")["sections"][1]["location"] == "Row 2"
    doc = tmp_path / "doc.docx"
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Heating decision</w:t></w:r></w:p></w:body></w:document>',
        )
    assert extract(doc, ".docx")["sections"][0]["content"] == "Heating decision"
    sheet = tmp_path / "sheet.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Costs"
    ws.append(["Equipment", "CAD"])
    ws.append(["Boiler", 42])
    wb.save(sheet)
    assert any(s["location"] == "Sheet Costs, row 2" for s in extract(sheet, ".xlsx")["sections"])
    from pypdf import PdfWriter

    pdf = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    assert extract(pdf, ".pdf")["state"] == "unsearchable"


def test_web_upload_csrf_preview_and_download(pilot, library):
    client = pilot.client
    login = client.get("/login").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login)[1]
    client.post("/login", data={"token": "owner", "csrf_token": csrf})
    html = client.get("/library").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
    assert (
        client.post(
            "/library/upload",
            files={"file": ("Sample.txt", b"Boiler test")},
            data={"csrf_token": "wrong"},
        ).status_code
        == 403
    )
    response = client.post(
        "/library/upload",
        files={"file": ("Sample.txt", b"Boiler test")},
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    url = response.headers["location"]
    assert client.get(url).status_code == 200
    assert client.get(url + "/download").content == b"Boiler test"


def test_spreadsheet_mcp_bounds_and_sheet_names(pilot,library):
    import io
    wb=Workbook();wb.active.title='Costs'
    for sheet in [wb.active,wb.create_sheet('Forecast')]:
        for n in range(25):sheet.append(['Heating',n])
    output=io.BytesIO();wb.save(output)
    node=library.upload(pilot.auth,'Forecast.xlsx',output.getvalue())
    assert process_one(library,pilot.auth)
    assert library.info(pilot.auth,node)['sheets']==['Costs','Forecast']
    response=pilot.client.post('/mcp/',headers={'Authorization':'Bearer assistant','Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':22,'method':'tools/call','params':{'name':'read_spreadsheet_rows','arguments':{'file_id':str(node),'first_row':1,'last_row':100}}})
    assert response.status_code==200
    result=response.json()['result']
    assert not result.get('isError'),result
    import json
    payload=json.loads(result['content'][0]['text'])
    assert len(payload['rows'])==20
