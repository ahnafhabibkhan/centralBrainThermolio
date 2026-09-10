import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import AuthContext, TokenAuthenticator, authenticate
from .config import Settings, get_settings
from .mcp_server import build_mcp, current_auth
from .models import (
    Memory,
    MemoryCreate,
    MemoryStatus,
    SearchRequest,
    SearchResult,
    Skill,
    WriteReceipt,
)
from .repository import PostgresMemoryRepository

logger = logging.getLogger("central_brain")


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 262144:
                return await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(repository=None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    repo = repository or PostgresMemoryRepository(settings.database_url)
    mcp = build_mcp(settings, repo)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        if repository is None:
            await run_in_threadpool(repo.open)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if repository is None:
                await run_in_threadpool(repo.close)

    app = FastAPI(title="Central Brain", version="0.2.0", lifespan=lifespan,
                  docs_url="/docs" if settings.environment == "local" else None,
                  redoc_url=None)
    app.state.repository = repo
    app.state.settings = settings
    app.state.authenticator = TokenAuthenticator(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       session_cookie="brain_session", max_age=28800, same_site="lax",
                       https_only=settings.environment == "production")
    from urllib.parse import urlsplit
    hosts = [urlsplit(settings.public_url).hostname, "127.0.0.1", "localhost"]
    if settings.environment == "local":
        hosts.append("testserver")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(BodyLimit)
    buckets = OrderedDict()

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        request_id = str(uuid4())
        key = request.client.host if request.client else "unknown"
        auth_marker = None
        if request.url.path.startswith("/mcp"):
            try:
                auth = await run_in_threadpool(
                    authenticate, request, request.headers.get("authorization")
                )
                auth.require("reader")
                auth_marker = current_auth.set(auth)
                key = str(auth.principal.actor_id)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, exc.status_code, headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="'
                    + settings.public_url + '/.well-known/oauth-protected-resource"'
                })
        try:
            if request.url.path not in {"/health", "/ready"} and not request.url.path.startswith("/static"):
                now = int(time.monotonic() // 60)
                old_window, count = buckets.get(key, (now, 0))
                count = count + 1 if old_window == now else 1
                buckets[key] = (now, count)
                buckets.move_to_end(key)
                if len(buckets) > 4096:
                    buckets.popitem(last=False)
                if count > settings.requests_per_minute:
                    return JSONResponse({"detail": "too many requests"}, 429,
                                        headers={"Retry-After": "60"})
            response = await call_next(request)
            response.headers.update({
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; "
                "img-src 'self' data:; script-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            })
            logger.info("request id=%s status=%s", request_id, response.status_code)
            return response
        finally:
            if auth_marker is not None:
                current_auth.reset(auth_marker)

    @app.exception_handler(HTTPException)
    async def errors(request, exc):
        headers = dict(exc.headers or {})
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = 'Bearer resource_metadata="' + settings.public_url \
                + '/.well-known/oauth-protected-resource"'
        return JSONResponse({"detail": exc.detail}, exc.status_code, headers=headers)

    Auth = Annotated[AuthContext, Depends(authenticate)]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        try:
            if repo.ready():
                return {"status": "ready"}
        except (DatabaseError, PoolTimeout):
            logger.warning("Database readiness check failed")
        return JSONResponse({"status": "unavailable"}, 503)

    @app.get("/.well-known/oauth-protected-resource")
    def protected_resource():
        return {
            "resource": settings.public_url,
            "authorization_servers": [settings.oauth_issuer] if settings.oauth_issuer else [],
            "scopes_supported": [settings.oauth_scope_prefix + "/" + s for s in ("read", "propose")],
        }

    @app.post("/v1/memories", response_model=WriteReceipt, status_code=201)
    def create_memory(item: MemoryCreate, auth: Auth):
        return repo.create(auth, item)

    @app.post("/v1/memories/search", response_model=list[SearchResult])
    def search_memories(item: SearchRequest, auth: Auth):
        return repo.search(auth, item.model_copy(update={
            "limit": min(item.limit, settings.max_search_results)
        }))

    @app.get("/v1/memories", response_model=list[Memory])
    def list_memories(auth: Auth, status: MemoryStatus = "proposed",
                      limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return repo.list(auth, status, limit, offset)

    @app.get("/v1/memories/{memory_id}", response_model=Memory)
    def get_memory(memory_id: UUID, auth: Auth):
        return repo.get(auth, memory_id)

    @app.post("/v1/memories/{memory_id}/approve", response_model=WriteReceipt)
    def approve(memory_id: UUID, auth: Auth):
        return repo.approve(auth, memory_id)

    @app.post("/v1/memories/{memory_id}/reject", response_model=WriteReceipt)
    def reject(memory_id: UUID, auth: Auth):
        return repo.transition(auth, memory_id, "reject")

    @app.post("/v1/memories/{memory_id}/supersede", response_model=WriteReceipt, status_code=201)
    def revise(memory_id: UUID, item: MemoryCreate, auth: Auth):
        auth.require("reviewer")
        return repo.create(auth, item, memory_id)

    @app.delete("/v1/memories/{memory_id}", response_model=WriteReceipt)
    def delete(memory_id: UUID, auth: Auth):
        return repo.transition(auth, memory_id, "delete")

    @app.get("/v1/memories/{memory_id}/export")
    def export(memory_id: UUID, auth: Auth):
        return JSONResponse(repo.get(auth, memory_id).model_dump(mode="json"), headers={
            "Content-Disposition": f'attachment; filename="memory-{memory_id}.json"'
        })

    @app.get("/v1/skills", response_model=list[Skill])
    def skills(auth: Auth):
        return repo.skills(auth)

    @app.get("/v1/skills/{name}", response_model=Skill)
    def skill(name: str, version: str, auth: Auth):
        result = repo.skills(auth, name, version)
        if not result:
            raise HTTPException(404, "skill not found")
        return result[0]

    from .web import install_web
    install_web(app, settings, repo)
    app.mount("/mcp", mcp_app)
    return app
