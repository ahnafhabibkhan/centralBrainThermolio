# Imported fixtures are consumed by pytest through parameter names.
# ruff: noqa: F401, F811
import io

import pytest
from fastapi import HTTPException
from PIL import Image
from test_library import library
from test_pilot import pilot

from central_brain.auth import AuthContext
from central_brain.image_access import read_image
from central_brain.library_worker import process_one


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("restriction", ["private", "confidential"])
def test_hidden_ancestor_blocks_known_ids_and_mutations(pilot, library, restriction, monkeypatch):
    owner = pilot.auth
    colleague = AuthContext(pilot.settings.principals()["colleague"])
    privileged = AuthContext(owner.principal.model_copy(
        update={"sensitivities": ["public", "internal", "confidential"]}
    ))
    folder = library.folder(owner, "Shared folder")
    nested = library.folder(owner, "Nested folder", folder)
    node = library.upload(owner, "Known.txt", b"First approved version.", nested)
    library.upload(owner, "Known.txt", b"Second approved version.", node_id=node)
    pending = library.upload(owner, "Pending.txt", b"Pending original.", nested, proposed=True)
    picture = library.upload(owner, "Known.png", png_bytes(), nested)
    while process_one(library, owner):
        pass
    assert library.read(colleague, node)["sections"][0]["content"] == "Second approved version."
    deletion = library.deletion_plan(colleague, node)
    restricted = library.folder(owner, "Restricted", visibility=(
        "private" if restriction == "private" else "workspace"
    ))
    if restriction == "confidential":
        with pilot.repo._connection(owner) as connection:
            connection.execute("UPDATE central_brain.library_nodes SET sensitivity='confidential' "
                               "WHERE id=%s", (restricted,))
    library.move(privileged, folder, "Shared folder", restricted)
    assert all(row["id"] != node for row in library.snapshot(colleague))
    assert library.search(colleague, "approved")["results"] == []

    def unexpected_read(key):
        pytest.fail("An inaccessible original must not be read from storage.")

    with monkeypatch.context() as patched:
        patched.setattr(library.store, "get", unexpected_read)
        operations = [
            lambda: library.read(colleague, node),
            lambda: library.read(colleague, node, version=1),
            lambda: library.read(colleague, pending, review=True),
            lambda: library.download(colleague, node),
            lambda: library.download(colleague, node, version=1),
            lambda: library.info(colleague, node, review=True),
            lambda: read_image(library, colleague, picture),
            lambda: library.listing(colleague, nested),
            lambda: library.folder_export(colleague, nested),
            lambda: library.copy(colleague, node, "Escaped.txt"),
            lambda: library.move(colleague, node, "Escaped.txt"),
            lambda: library.upload(colleague, "Known.txt", b"Changed.", node_id=node),
            lambda: library.folder(colleague, "New folder", nested),
            lambda: library.upload(colleague, "New.txt", b"New.", nested),
            lambda: library.suggest(colleague, "folder", {"name": "New folder", "parent_id": str(nested)}),
            lambda: library.suggest(colleague, "move", {"name": "Escaped.txt", "node_id": str(node)}),
            lambda: library.review(colleague, pending, True),
            lambda: library.deletion_plan(colleague, node),
            lambda: library.delete(colleague, node, "Known.txt", deletion["token"]),
        ]
        for operation in operations:
            with pytest.raises(HTTPException) as error:
                operation()
            assert error.value.status_code == 404

    assert library.info(privileged, node)["parent_id"] == nested
    assert len(library.info(privileged, node)["versions"]) == 2
    assert library.info(privileged, pending, review=True)["status"] == "proposed"
    assert library.read(privileged, node)["sections"][0]["content"] == "Second approved version."


@pytest.mark.parametrize("parent_private", [False, True])
@pytest.mark.parametrize("status", ["active", "proposed", "rejected"])
def test_own_private_files_preserve_review_access(pilot, library, parent_private, status):
    owner = pilot.auth
    colleague = AuthContext(pilot.settings.principals()["colleague"])
    parent = library.folder(owner, "Own private folder", visibility="private") if parent_private else None
    node = library.upload(owner, "Own.txt", b"Own private content.", parent,
                          proposed=status != "active", visibility="private")
    assert process_one(library, owner)
    if status == "rejected":
        library.review(owner, node, False)
    assert library.info(owner, node, review=True)["status"] == status
    assert library.read(owner, node, review=True)["sections"][0]["content"] == "Own private content."
    with library.download(owner, node)[1] as stream:
        assert stream.read() == b"Own private content."
    if status == "active":
        assert library.read(owner, node)["sections"]
    else:
        with pytest.raises(HTTPException) as error:
            library.read(owner, node)
        assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        library.read(colleague, node, review=True)
    assert error.value.status_code == 404


@pytest.mark.parametrize("invalid_ancestor", ["cycle", "inactive", "not_folder"])
def test_invalid_ancestor_chain_denies_direct_access(pilot, library, invalid_ancestor):
    owner = pilot.auth
    root = library.folder(owner, "Root")
    nested = library.folder(owner, "Nested", root)
    node = library.upload(owner, "Known.txt", b"Approved content.", nested)
    with pilot.repo._connection(owner) as connection:
        if invalid_ancestor == "cycle":
            connection.execute("UPDATE central_brain.library_nodes SET parent_id=%s WHERE id=%s",
                               (nested, root))
        elif invalid_ancestor == "inactive":
            connection.execute("UPDATE central_brain.library_nodes SET status='rejected' WHERE id=%s", (root,))
        else:
            connection.execute("UPDATE central_brain.library_nodes SET kind='file' WHERE id=%s", (root,))
    with pytest.raises(HTTPException) as error:
        library.read(owner, node, review=True)
    assert error.value.status_code == 404


@pytest.mark.parametrize("restriction", ["private", "confidential", "proposed", "rejected"])
def test_export_visible_root_excludes_inaccessible_branch_contents(pilot, library, restriction):
    owner = pilot.auth
    colleague = AuthContext(pilot.settings.principals()["colleague"])
    root = library.folder(owner, "Root")
    branch = library.folder(owner, "Restricted branch", root)
    nested = library.folder(owner, "Nested", branch)
    library.upload(owner, "Hidden.txt", b"Restricted branch content.", nested)
    library.upload(owner, "Visible.txt", b"Accessible content.", root)
    library.upload(owner, "Pending.txt", b"Accessible proposal.", root, proposed=True)
    with pilot.repo._connection(owner) as connection:
        if restriction == "private":
            connection.execute("UPDATE central_brain.library_nodes SET visibility='private' WHERE id=%s", (branch,))
        elif restriction == "confidential":
            connection.execute("UPDATE central_brain.library_nodes SET sensitivity='confidential' WHERE id=%s", (branch,))
        else:
            connection.execute("UPDATE central_brain.library_nodes SET status=%s WHERE id=%s", (restriction, branch))
    name, entries = library.folder_export(colleague, root)
    assert name == "Root"
    paths = {entry["path"] for entry in entries}
    assert {"Root/", "Root/Visible.txt", "Root/Pending.txt"} <= paths
    assert not any("Nested" in path or path.endswith("Hidden.txt") for path in paths)
    assert ("Root/Restricted branch/" in paths) == (restriction == "proposed")
    review_paths = {row["path"] for row in library.snapshot(colleague, review=True)}
    assert not any("Nested" in path or path.endswith("Hidden.txt") for path in review_paths)


def test_image_preview_rechecks_ancestor_access_after_conversion(pilot, library, monkeypatch):
    import central_brain.image_access as access

    owner = pilot.auth
    colleague = AuthContext(pilot.settings.principals()["colleague"])
    node = library.upload(owner, "Known.png", png_bytes())
    private = library.folder(owner, "Private destination", visibility="private")
    convert = access.subprocess.run

    def move_during_conversion(*args, **kwargs):
        result = convert(*args, **kwargs)
        library.move(owner, node, "Known.png", private)
        return result

    monkeypatch.setattr(access.subprocess, "run", move_during_conversion)
    with pytest.raises(HTTPException) as error:
        read_image(library, colleague, node)
    assert error.value.status_code == 404
