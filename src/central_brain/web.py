import base64
import hashlib
import logging
import secrets
import time
from pathlib import Path
from uuid import UUID

from authlib.integrations.starlette_client import OAuth
from cryptography.fernet import Fernet, InvalidToken
from fastapi import File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .models import MemoryCreate, Source

logger = logging.getLogger("central_brain")


def markdown_content(file):
    if not file.filename or Path(file.filename).suffix.lower() != ".md":
        raise HTTPException(422, "Memories must be uploaded as .md files.")
    data = file.file.read(80001)
    if len(data) > 80000:
        raise HTTPException(413, "Memory files must contain at most 20,000 characters.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "Save the Markdown file as UTF-8 text.") from None
    if not content.strip() or len(content) > 20000 or "\x00" in content:
        raise HTTPException(422, "Provide a Markdown file with 1 to 20,000 text characters.")
    return content


def install_web(app, settings, repo):
    assets = Path(__file__).parent
    templates = Jinja2Templates(directory=assets / "templates")
    app.mount("/static", StaticFiles(directory=assets / "static"), name="static")
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.session_secret.encode()).digest()))
    oauth = OAuth()
    if settings.oauth_issuer:
        oauth.register(
            name="cognito", client_id=settings.oauth_web_client_id,
            client_secret=settings.oauth_web_client_secret or None,
            server_metadata_url=settings.oauth_issuer + "/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid " + " ".join(settings.oauth_scope_prefix + "/" + s
                                              for s in ("read", "propose", "review", "admin")),
                "code_challenge_method": "S256",
            },
        )

    def csrf(request):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    def check_csrf(request, value):
        if not secrets.compare_digest(request.session.get("csrf", ""), value) or not value:
            raise HTTPException(403, "invalid form token; reload the page")
        origin = request.headers.get("origin")
        if origin and origin != settings.public_url:
            raise HTTPException(403, "invalid form origin")

    def reviewer(request):
        session_id = request.session.get("sid")
        encrypted = repo.session_token(session_id) if session_id else None
        if not encrypted:
            raise HTTPException(303, "sign in", headers={"Location": "/login"})
        try:
            auth = app.state.authenticator.verify(cipher.decrypt(encrypted.encode()).decode())
            auth.require("reviewer")
            return auth
        except (HTTPException, InvalidToken):
            request.session.clear()
            raise HTTPException(303, "sign in", headers={"Location": "/login"}) from None

    def page(request, template, **context):
        if request.session.get("sid") and hasattr(app.state, "library"):
            auth = reviewer(request)
            app.state.library.project_memories(auth)
            roots, tree = app.state.library.workspace_folders(auth)
            context.update(library_roots=roots, folder_tree=tree)
        return templates.TemplateResponse(request=request, name=template, context={
            "csrf": csrf(request), "local": settings.environment == "local", **context,
        })

    def establish(request, token, expires):
        auth = app.state.authenticator.verify(token)
        auth.require("reviewer")
        if old := request.session.get("sid"):
            repo.session_delete(old)
        request.session.clear()
        session_id = secrets.token_urlsafe(32)
        repo.session_create(session_id, cipher.encrypt(token.encode()).decode(), expires)
        request.session["sid"] = session_id
        csrf(request)

    from .library_web import install_library_web
    install_library_web(app, settings, repo, reviewer, page, check_csrf)

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request, signed_out: bool = False):
        if request.session.get("sid"):
            try:
                reviewer(request)
            except HTTPException:
                pass
            else:
                return RedirectResponse("/", 303)
        if settings.oauth_issuer and not signed_out:
            return RedirectResponse("/auth/login", 303)
        return page(request, "login.html", title="Welcome back")

    @app.get("/getting-started", include_in_schema=False)
    def getting_started(request: Request):
        from .manage import ROOT
        return page(request, "getting-started.html", title="Getting started", project_path=str(ROOT))

    @app.post("/login", include_in_schema=False)
    def local_login(request: Request, token: str = Form(...), csrf_token: str = Form(...)):
        if settings.environment != "local":
            raise HTTPException(404, "not found")
        check_csrf(request, csrf_token)
        try:
            establish(request, token, time.time() + 28800)
        except HTTPException:
            return page(request, "login.html", title="Welcome back", error="The access key is invalid. Copy your reviewer key and try again.")
        return RedirectResponse("/", 303)

    @app.get("/auth/login", include_in_schema=False)
    async def oauth_login(request: Request):
        if not settings.oauth_issuer:
            return RedirectResponse("/login", 303)
        return await oauth.cognito.authorize_redirect(
            request, settings.public_url + "/auth/callback", resource=settings.oauth_resource,
        )

    @app.get("/auth/callback", include_in_schema=False)
    async def oauth_callback(request: Request):
        if not settings.oauth_issuer:
            raise HTTPException(404, "not found")
        stage = "token exchange"
        try:
            token = await oauth.cognito.authorize_access_token(request, resource=settings.oauth_resource)
            stage = "workspace session"
            await run_in_threadpool(establish, request, token["access_token"],
                                    min(token.get("expires_at", time.time()), time.time() + 3600))
        except Exception as exc:  # noqa: BLE001  OAuth failures must not expose credentials.
            logger.warning("OAuth callback failed at %s: %s (cause: %s)", stage,
                           type(exc).__name__, type(exc.__cause__).__name__)
            request.session.clear()
            return page(request, "login.html", title="Sign-in needs attention",
                        error="Sign-in failed. Check the approved account and OAuth configuration.")
        return RedirectResponse("/", 303)

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request, csrf_token: str = Form(...)):
        check_csrf(request, csrf_token)
        if sid := request.session.get("sid"):
            repo.session_delete(sid)
        request.session.clear()
        return RedirectResponse("/login?signed_out=true", 303)

    @app.get("/", include_in_schema=False)
    def dashboard(request: Request, view: str | None = None, offset: int = 0):
        auth = reviewer(request)
        if view is None:
            return RedirectResponse("/library", 303)
        if view not in {"proposed", "active", "rejected", "superseded", "archived"}:
            raise HTTPException(400, "invalid view")
        if offset < 0 or offset > 100000:
            raise HTTPException(400, "invalid offset")
        rows = repo.list(auth, view, 25, offset)
        return page(request, "dashboard.html", title="Your shared memory", view=view,
                    memories=rows, offset=offset, workspace=str(auth.principal.workspace_id)[:8])

    @app.get("/new", include_in_schema=False)
    def new_page(request: Request):
        reviewer(request)
        return page(request, "edit.html", title="Add a memory", memory=None)

    @app.post("/new", include_in_schema=False)
    def new_memory(request: Request, file: UploadFile = File(...), memory_type: str = Form(...),
                   source_reference: str = Form(...), csrf_token: str = Form(...)):
        auth = reviewer(request)
        check_csrf(request, csrf_token)
        content = markdown_content(file)
        try:
            item = MemoryCreate(content=content, memory_type=memory_type,
                                source=Source(kind="human", reference=source_reference))
        except ValidationError:
            raise HTTPException(422, "Provide valid content, type, and source") from None
        receipt = repo.create(auth, item)
        return RedirectResponse(f"/review/{receipt.memory_id}", 303)

    @app.get("/review/{memory_id}", include_in_schema=False)
    def detail(request: Request, memory_id: UUID):
        auth = reviewer(request)
        return page(request, "detail.html", title="Review a memory", memory=repo.get(auth, memory_id))

    @app.get("/review/{memory_id}/edit", include_in_schema=False)
    def edit_page(request: Request, memory_id: UUID):
        memory = repo.get(reviewer(request), memory_id)
        if memory.status not in {"proposed", "active"}:
            raise HTTPException(409, "only proposed or active memories can be edited")
        return page(request, "edit.html", title="Edit a memory", memory=memory)

    @app.post("/review/{memory_id}/edit", include_in_schema=False)
    def edit(request: Request, memory_id: UUID, file: UploadFile = File(...),
             source_reference: str = Form(...), csrf_token: str = Form(...)):
        auth = reviewer(request)
        check_csrf(request, csrf_token)
        content = markdown_content(file)
        original = repo.get(auth, memory_id)
        try:
            item = MemoryCreate(
                content=content, memory_type=original.memory_type,
                sensitivity=original.sensitivity, visibility=original.visibility,
                source=Source(kind=original.source.get("kind", "human"), reference=source_reference),
                metadata=original.metadata, expires_at=original.expires_at,
                subject_ref=original.subject_ref, confidence=original.confidence,
            )
        except ValidationError:
            raise HTTPException(422, "Provide valid content and source") from None
        if original.status == "proposed":
            receipt = repo.edit_proposal(auth, memory_id, item)
        else:
            receipt = repo.create(auth, item, memory_id)
        return RedirectResponse(f"/review/{receipt.memory_id}", 303)

    @app.post("/review/{memory_id}/{action}", include_in_schema=False)
    def review_action(request: Request, memory_id: UUID, action: str,
                      csrf_token: str = Form(...)):
        auth = reviewer(request)
        check_csrf(request, csrf_token)
        if action not in {"approve", "reject", "delete"}:
            raise HTTPException(404, "not found")
        repo.transition(auth, memory_id, action)
        return RedirectResponse("/?view=proposed", 303)

    @app.get("/review/{memory_id}/export", include_in_schema=False)
    def export(request: Request, memory_id: UUID):
        memory = repo.get(reviewer(request), memory_id)
        return Response(memory.content, media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="memory-{memory_id}.md"'
        })

    @app.get("/skills", include_in_schema=False)
    def skills_page(request: Request):
        roots, _ = app.state.library.workspace_folders(reviewer(request))
        return RedirectResponse(f"/library?folder={roots['Skills']}", 303)

    @app.get("/search", include_in_schema=False)
    def search_page(request: Request, q: str = Query("", max_length=2000)):
        from .models import SearchRequest
        auth = reviewer(request)
        rows = repo.search(auth, SearchRequest(query=q, limit=20)) if q.strip() else []
        return page(request, "search.html", title="Find a memory", query=q, results=rows)
