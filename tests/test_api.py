from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from central_brain.api import create_app
from central_brain.auth import AuthContext
from central_brain.config import Settings
from central_brain.models import Memory, MemoryCreate, SearchRequest, SearchResult, WriteReceipt

WORKSPACE_ID = uuid4()
ACTOR_ID = uuid4()


class FakeRepository:
    def __init__(self) -> None:
        self.last_limit = None

    def create(self, auth: AuthContext, item: MemoryCreate) -> WriteReceipt:
        assert auth.principal.workspace_id == str(WORKSPACE_ID)
        return WriteReceipt(memory_id=uuid4(), status="proposed", created=True)

    def search(self, auth: AuthContext, request: SearchRequest) -> list[SearchResult]:
        self.last_limit = request.limit
        memory = Memory(
            id=uuid4(), workspace_id=WORKSPACE_ID, content="Use PostgreSQL", memory_type="decision",
            status="active", sensitivity="internal", confidence=1, source={"kind": "human"},
            metadata={}, created_at=datetime.now(UTC), expires_at=None,
        )
        return [SearchResult(memory=memory, score=0.9)]

    def approve(self, auth: AuthContext, memory_id: UUID) -> WriteReceipt | None:
        return WriteReceipt(memory_id=memory_id, status="active", created=False)


def make_client(roles: list[str] | None = None) -> tuple[TestClient, FakeRepository]:
    repo = FakeRepository()
    principal = {"workspace_id": str(WORKSPACE_ID), "actor_id": str(ACTOR_ID), "roles": roles or []}
    settings = Settings(
        database_url="postgresql://unused",
        central_brain_principals_json=__import__("json").dumps({"test-token": principal}),
        max_search_results=5,
    )
    return TestClient(create_app(repo, settings)), repo


def test_requires_bearer_token() -> None:
    client, _ = make_client()
    assert client.post("/v1/memories/search", json={"query": "postgres"}).status_code == 401


def test_creates_proposed_memory() -> None:
    client, _ = make_client()
    response = client.post(
        "/v1/memories",
        headers={"Authorization": "Bearer test-token"},
        json={"content": "Use PostgreSQL", "memory_type": "decision", "source": {"kind": "human"}},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "proposed"


def test_caps_search_limit() -> None:
    client, repo = make_client()
    response = client.post(
        "/v1/memories/search",
        headers={"Authorization": "Bearer test-token"},
        json={"query": "postgres", "limit": 20},
    )
    assert response.status_code == 200
    assert repo.last_limit == 5


def test_approval_requires_reviewer_role() -> None:
    client, _ = make_client()
    memory_id = uuid4()
    assert client.post(
        f"/v1/memories/{memory_id}/approve",
        headers={"Authorization": "Bearer test-token"},
    ).status_code == 403
    reviewer, _ = make_client(["reviewer"])
    assert reviewer.post(
        f"/v1/memories/{memory_id}/approve",
        headers={"Authorization": "Bearer test-token"},
    ).status_code == 200
