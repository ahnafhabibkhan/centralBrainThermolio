from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status

from .auth import AuthContext, authenticate
from .config import Settings, get_settings
from .models import MemoryCreate, SearchRequest, SearchResult, WriteReceipt
from .repository import MemoryRepository, PostgresMemoryRepository


def create_app(repository: MemoryRepository | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    postgres = None if repository else PostgresMemoryRepository(settings.database_url)
    resolved_repository = repository or postgres

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if postgres:
            postgres.open()
        yield
        if postgres:
            postgres.close()

    app = FastAPI(title="Central Brain API", version="0.1.0", lifespan=lifespan)
    app.state.repository = resolved_repository
    # Authentication must use the same settings object as the application. This
    # also makes test and multi-instance app factories deterministic instead of
    # accidentally consulting the process-global settings cache.
    app.dependency_overrides[get_settings] = lambda: settings

    def get_repository(request: Request) -> MemoryRepository:
        return request.app.state.repository

    Auth = Annotated[AuthContext, Depends(authenticate)]
    Repository = Annotated[MemoryRepository, Depends(get_repository)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/memories", response_model=WriteReceipt, status_code=status.HTTP_201_CREATED)
    def create_memory(item: MemoryCreate, auth: Auth, repo: Repository) -> WriteReceipt:
        return repo.create(auth, item)

    @app.post("/v1/memories/search", response_model=list[SearchResult])
    def search_memories(request: SearchRequest, auth: Auth, repo: Repository) -> list[SearchResult]:
        request.limit = min(request.limit, settings.max_search_results)
        return repo.search(auth, request)

    @app.post("/v1/memories/{memory_id}/approve", response_model=WriteReceipt)
    def approve_memory(memory_id: UUID, auth: Auth, repo: Repository) -> WriteReceipt:
        auth.require("reviewer")
        result = repo.approve(auth, memory_id)
        if result is None:
            raise HTTPException(status_code=404, detail="proposed memory not found")
        return result

    return app
