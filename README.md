# Nightingale 72-Hour Build

The product made by the candidate for the Nightingale internship.

A server-authoritative, deterministic care-note collaboration platform:
FastAPI + PostgreSQL (RLS) backend, a Vite + React + Tailwind PWA frontend, a
deterministic mock LLM, PHI redaction before every model call, and
provenance-first AI-scribed notes. See `docs/` for the full design package
(requirements, architecture, schema, security, plan).

---

## Features

- **Care Note timeline** — a single longitudinal note per patient: intake,
  AI-scribed consult summaries, care plans, reviews, follow-ups. Every entry
  carries its `author_role`, risk level, and provenance (who wrote what).
- **Top Card (P95 ≤ 300 ms)** — a precomputed glance per patient: recency +
  risk + entity importance (allergy boost) + open actions + learned boost, so
  the highest-signal items surface instantly.
- **AI Scribe** — paste a raw transcript; every PHI span (names, NRIC/FIN, IDs,
  phones) is deterministically redacted *before* the model sees it, and the
  summary is ingested as a `system`-authored entry with a resolvable
  provenance pointer. The redaction mask (category counts only) is persisted.
- **Collaboration** — threaded comments with `@mention` notifications and
  unread badges, tasks with assignee/priority/status/due, revision history
  with version restore, and deterministic conflict handling (a same-section
  race is never silently dropped).
- **Risk highlights** — a deterministic generator ranks risk phrases and
  freezes each span at `(entry_id, offset_start, offset_end)` so it always
  resolves. Accept / reject transitions feed the learning loop and audit log.
- **Self-learning importance** — accept/reject teaches `learning_weight` for
  the entry's extracted entities; a suggestions endpoint and the Top Card rank
  by learned importance.
- **Data decay** — stale, open-work-free entries archive to `entry_archive`
  (admin-triggered, **clinic-scoped**) and restore on demand, so the note
  stays longitudinal without accumulating dead weight.

---

## Setup & Run

### Prerequisites

- Docker (with the `docker compose` v2 plugin)

### 1. Start the stack

```bash
cp .env.example .env        # optional for local dev — safe dev defaults exist
docker compose up --build -d   # or: make up
```

This starts four services (also via `make up`):

| Service | URL | Purpose |
| --- | --- | --- |
| `db` | `localhost:5432` | PostgreSQL 16 (named volume `nightingale_pgdata`, RLS) |
| `mock-llm` | `localhost:5001` | Deterministic canned-output LLM (no live model; host port 5001 — macOS AirPlay Receiver owns 5000) |
| `api` | `localhost:8000` | FastAPI; OpenAPI docs at `/docs` |
| `web` | `localhost:5173` | Vite dev server (React + Tailwind PWA) |

The API healthcheck pings `GET /healthz`, which executes `SELECT 1` against
Postgres before the stack is considered healthy.

### 2. Migrate and seed

```bash
make migrate     # roles.sql -> schema.sql -> rls.sql, applied in order inside the api container
make seed        # python -m app.seed (synthetic demo data)
```

Migrations run inside the `api` container (not via the Postgres
`/docker-entrypoint-initdb.d` hook): `make migrate` execs
`python -m app.db.apply_migrations`, which applies `backend/app/db/roles.sql`,
`backend/app/db/schema.sql`, then `backend/app/db/rls.sql`, each idempotently,
connecting as the bootstrap superuser via `MIGRATION_DATABASE_URL` (creating
roles needs `CREATEROLE`). `make seed` execs `python -m app.seed` the same way.
The runtime API itself connects as the restricted `app_nightingale` role via
`DATABASE_URL`. The synthetic seed reproduces the demo scenarios on the fixed
dates **2025-04-15** and **2026-02-06** with all three AI-scribed note types.

### 3. Run tests and checks

```bash
make test        # pytest suite inside the api container (tests/ lives in backend/)
make fmt         # ruff format + lint
```

### 4. Useful commands

```bash
make logs        # follow service logs
make api-shell   # shell into the api container
make db-shell    # psql into the database
make down        # stop the stack (keeps the pgdata volume)
```

---

## Where redaction happens

Every outbound LLM call passes through the single chokepoint
**`backend/app/services/llm_gateway.py`** (the `LLMGateway`). It refuses to
execute without first running the deterministic regex+lexicon PHI redactor on
all system/user prompt parts and only persists de-redacted text. `LLM_MOCK=1`
(default) routes every call to the deterministic `mock-llm` service, so tests
and demos never depend on a live model.

## How RBAC is enforced

Three defense-in-depth layers, strongest first:

1. **PostgreSQL Row-Level Security** — `FORCE ROW LEVEL SECURITY` policies on
   every table (including child tables) in **`backend/app/db/rls.sql`**,
   keyed on session GUCs (`app.user_id` / `app.role` / `app.clinic_id`).
2. **API dependency guards** — `backend/app/db/session.py` `set_app_context`
   `SET ROLE`s into the role class and `SET LOCAL`s the RLS GUCs per
   transaction (wired from the verified JWT in M2); route-level `require_roles`
   guards return clean 401/403s.
3. **Service-layer scope checks** — object-scope checks (e.g. `get_patient_or_404`
   resolved inside the actor's clinic) before any query, and 404 for
   out-of-scope reads to avoid existence leaks.

Tests run against a live Postgres **as the restricted application role** — no
superuser or table-owner bypass. See `docs/SECURITY.md` and `docs/DATA_SCHEMA.md`
for the policy matrix.
