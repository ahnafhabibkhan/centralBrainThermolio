# Central Brain

Central Brain stores approved memories and reviewed procedures for connected assistants. It does not run a language model or capture conversations in the background. An assistant proposes a useful memory, you review it on the webpage, and connected assistants can retrieve it after approval.

The local pilot includes a FastAPI application, a review website, PostgreSQL persistence, an official-SDK MCP endpoint, and production OAuth validation. AWS configuration is prepared but has not been deployed. Live ChatGPT, Claude, Cognito, DNS, and HTTPS integration still require deployment and acceptance testing.

## Run locally

Use Python 3.11 or later and Docker with Compose. Run these commands from this repository:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e '.[dev]'
.venv/bin/python -m central_brain.manage init-local
docker compose up -d --wait
.venv/bin/python -m central_brain.manage bootstrap
.venv/bin/uvicorn central_brain.api:create_app --app-dir src --factory --host 127.0.0.1 --port 8080 --no-access-log
```

Skip `init-local` when `.env` already exists. It intentionally refuses to overwrite credentials. Open [the local review page](http://127.0.0.1:8080). The reviewer token is the key whose roles include `reviewer` in `CENTRAL_BRAIN_PRINCIPALS_JSON` inside `.env`. On macOS, copy it directly to the clipboard without printing it:

```bash
.venv/bin/python src/central_brain/manage.py copy-login-key
```

Paste the token into the sign-in form. Select **Add a memory**, supply a concise fact and its source, and submit it. It remains in **Needs review** until you approve it. Search only returns approved, unexpired records. Editing an approved record creates a proposal; the original stays active until the correction is approved.

The [in-app getting-started guide](http://127.0.0.1:8080/getting-started) explains this workflow step by step. The internal interface follows the [Thermolio brand reference](docs/brand-reference.md) and uses a locally served official logo.

The other generated token has only `reader` and `writer` roles. Use it for local MCP tests at `http://127.0.0.1:8080/mcp/`. Normal cloud assistant apps cannot access this loopback endpoint. Production uses separate OAuth clients and HTTPS.

## Verify the implementation

```bash
.venv/bin/python -m central_brain.manage bootstrap --database central_brain_test
.venv/bin/pytest -q
.venv/bin/ruff check src tests
docker build -t central-brain:local .
.venv/bin/python scripts/verify-container.py
.venv/bin/python scripts/verify-local-restore.py
```

The tests use real PostgreSQL with restricted runtime credentials. They exercise approval, deletion, audit permissions, tenant and private-record isolation, concurrent duplicate writes, competing corrections, expiry, browser sessions, CSRF, escaping, MCP tool calls, and signed OAuth access tokens. They never connect to AWS. Dependency deprecation warnings do not indicate test failures.

## Deployment and operation

Follow [the EC2 pilot runbook](docs/pilot-runbook.md) after reviewing the configuration and receiving explicit deployment approval. It covers the database disk, HTTPS, Cognito, SSM, backups, restoration, connector acceptance tests, and rollback. The [implementation status](docs/implementation-status.md) distinguishes completed local work from deployment-dependent checks.

| Location | Purpose |
| --- | --- |
| `src/central_brain/` | The API, review website, MCP tools, and authentication live here. |
| `database/migrations/` | Core tables and forced row-level security are defined here. |
| `deploy/` | Reviewed CloudFormation, production Compose, Caddy, and backup files live here. |
| `tests/` | Real database integration and authorization tests live here. |
| `skills/` | Candidate procedures await explicit review and import. |
| `memory/` | Planning artifacts remain separate from runtime memories. |

No memory or skill files are imported automatically. Review candidate skill files before running `python -m central_brain.manage import-skills --confirm-reviewed` with migration credentials. Skills remain reference material, and the server never executes them.

Use `docker compose stop` to stop the local database without removing its data. Do not commit `.env`, credentials, database dumps, or runtime memories. Earlier architecture documents describe alternatives; the EC2 pilot runbook is the current deployment path.
