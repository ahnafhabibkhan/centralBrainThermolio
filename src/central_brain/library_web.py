from urllib.parse import quote
from uuid import UUID

from fastapi import Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .library import Library


def install_library_web(app, settings, repo, reviewer, page, check_csrf):
    library = Library(repo, settings)
    app.state.library = library

    @app.get("/library/context", include_in_schema=False)
    def workspace_context(request: Request):
        return JSONResponse(jsonable_encoder(library.context(reviewer(request), True)),
                            headers={"Cache-Control": "no-store"})

    @app.get("/library", include_in_schema=False)
    def browse(
        request: Request,
        folder: UUID | None = None,
        q: str = Query("", max_length=2000),
        offset: int = Query(0, ge=0, le=100000),
    ):
        auth = reviewer(request)
        library.project_memories(auth)
        roots, tree = library.workspace_folders(auth)
        snapshot = library.snapshot(auth, True)
        nodes = {row["id"]: dict(row, children=[]) for row in snapshot[:2000]}
        hierarchy = []
        for node in nodes.values():
            if node["parent_id"] in nodes:
                nodes[node["parent_id"]]["children"].append(node)
            elif node["parent_id"] is None:
                hierarchy.append(node)
        def sort_tree(items):
            items.sort(key=lambda n: (n["kind"] != "folder", n["name"].lower()))
            for item in items:
                sort_tree(item["children"])
        sort_tree(hierarchy)
        rows = library.listing(auth, folder, True, offset)
        location = library.info(auth, folder, True) if folder else None
        section = location["path"].strip("/").split("/")[0] if location else ""
        with repo._connection(auth) as c:
            suggestions = c.execute(
                "SELECT * FROM central_brain.library_suggestions WHERE status='proposed' ORDER BY created_at LIMIT 50"
            ).fetchall()
        suggestions = [dict(item) for item in suggestions]
        for suggestion in suggestions:
            parent_id = suggestion["payload"].get("parent_id")
            try:
                suggestion["destination"] = library.path(auth, UUID(parent_id)) if parent_id else "Workspace"
            except HTTPException:
                suggestion["destination"] = "Destination unavailable"
        return page(
            request,
            "library.html",
            title="Library",
            hierarchy=hierarchy,
            workspace_context=library.context(auth, True),
            section=section,
            skills=repo.skills(auth) if section == "Skills" else [],
            rows=rows,
            folder=location,
            usage=library.usage(auth),
            offset=offset,
            query=q,
            results=library.search(auth, q, folder)["results"] if q.strip() else [],
            suggestions=suggestions,
            pending_files=[row for row in snapshot if row["status"] == "proposed"][:100],
        )

    @app.post("/library/folders", include_in_schema=False)
    def folder_create(
        request: Request,
        name: str = Form(...),
        parent_id: str = Form(""),
        csrf_token: str = Form(...),
    ):
        auth = reviewer(request)
        check_csrf(request, csrf_token)
        node = library.folder(auth, name, UUID(parent_id) if parent_id else None)
        return RedirectResponse(f"/library?folder={node}", 303)

    @app.post("/library/upload", include_in_schema=False)
    async def upload(request: Request):
        from starlette.concurrency import run_in_threadpool

        auth = await run_in_threadpool(reviewer, request)
        async with request.form(max_files=1, max_fields=5, max_part_size=8192) as form:
            check_csrf(request, str(form.get("csrf_token", "")))
            file = form.get("file")
            if not file or not hasattr(file, "read"):
                raise HTTPException(422, "Choose a file to upload.")
            data = await file.read(settings.library_file_bytes + 1)
            parent = UUID(str(form["parent_id"])) if form.get("parent_id") else None
            node = UUID(str(form["node_id"])) if form.get("node_id") else None
            if parent and library.path(auth, parent).strip('/').split('/')[0] == "Memories":
                from .models import MemoryCreate, Source
                from .web import markdown_content
                import io
                from types import SimpleNamespace

                content = markdown_content(SimpleNamespace(filename=file.filename, file=io.BytesIO(data)))
                receipt = await run_in_threadpool(repo.create, auth, MemoryCreate(
                    content=content, memory_type="fact",
                    visibility=str(form.get("visibility", "workspace")),
                    source=Source(kind="document", reference=file.filename),
                ))
                await run_in_threadpool(library.move, auth, receipt.memory_id, f"Memory-{receipt.memory_id}.md", parent)
                return RedirectResponse(f"/review/{receipt.memory_id}", 303)
            result = await run_in_threadpool(
                library.upload,
                auth,
                file.filename,
                data,
                parent,
                node,
                False,
                str(form.get("visibility", "workspace")),
            )
        return RedirectResponse(f"/library/file/{result}", 303)

    @app.get("/library/file/{node_id}", include_in_schema=False)
    def detail(
        request: Request, node_id: UUID, version: int | None = None, start: int = Query(0, ge=0)
    ):
        auth = reviewer(request)
        node = library.info(auth, node_id, True)
        _, folders = library.workspace_folders(auth)
        return page(
            request,
            "file.html",
            title=node["name"],
            node=node,
            folders=folders,
            preview=library.read(auth, node_id, version, start, 3, True)
            if node["kind"] != "folder"
            else None,
        )

    @app.get("/library/file/{node_id}/download", include_in_schema=False)
    def download(request: Request, node_id: UUID, version: int | None = None):
        name, stream = library.download(reviewer(request), node_id, version)

        def chunks():
            try:
                while data := stream.read(65536):
                    yield data
            finally:
                stream.close()

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")},
        )

    @app.post("/library/file/{node_id}/move", include_in_schema=False)
    def move(
        request: Request,
        node_id: UUID,
        name: str = Form(...),
        parent_id: str = Form(""),
        csrf_token: str = Form(...),
    ):
        auth = reviewer(request)
        check_csrf(request, csrf_token)
        library.move(auth, node_id, name, UUID(parent_id) if parent_id else None)
        return RedirectResponse(f"/library/file/{node_id}", 303)

    @app.post("/library/file/{node_id}/review", include_in_schema=False)
    def review(
        request: Request, node_id: UUID, action: str = Form(...), csrf_token: str = Form(...)
    ):
        auth = reviewer(request)
        check_csrf(request, csrf_token)
        if action not in {"approve", "reject"}:
            raise HTTPException(422, "Unknown review action.")
        library.review(auth, node_id, action == "approve")
        return RedirectResponse("/library", 303)

    @app.post("/library/suggestions/{suggestion_id}", include_in_schema=False)
    def organization(
        request: Request, suggestion_id: UUID, action: str = Form(...), csrf_token: str = Form(...)
    ):
        auth = reviewer(request)
        check_csrf(request, csrf_token)
        if action not in {"approve", "reject"}:
            raise HTTPException(422, "Unknown review action.")
        with repo._connection(auth) as c:
            row = c.execute(
                "SELECT * FROM central_brain.library_suggestions WHERE id=%s AND status='proposed' FOR UPDATE",
                (suggestion_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Suggestion not found.")
            p = row["payload"]
            parent = UUID(p["parent_id"]) if p.get("parent_id") else None
            if action == "approve":
                if row["action"] == "folder":
                    existing = c.execute("SELECT id FROM central_brain.library_nodes WHERE kind='folder' "
                                         "AND status='active' AND parent_id IS NOT DISTINCT FROM %s "
                                         "AND lower(name)=lower(%s)", (parent, p["name"])).fetchone()
                    if not existing:
                        library.folder(auth, p["name"], parent, connection=c)
                else:
                    library.move(auth, UUID(p["node_id"]), p["name"], parent, connection=c)
            c.execute(
                "UPDATE central_brain.library_suggestions SET status=%s WHERE id=%s",
                (action, suggestion_id),
            )
            library._audit(c, auth, "organization." + action, suggestion_id)
        return RedirectResponse("/library", 303)
