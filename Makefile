# Nightingale 72-hour build — developer convenience targets.
#
# Conventions (see docker-compose.yml):
#   - roles.sql + schema.sql + rls.sql are applied by `make migrate` via
#     `python -m app.db.apply_migrations` INSIDE the api container (which has
#     MIGRATION_DATABASE_URL set). They are idempotent and recorded in the
#     schema_migrations table, so re-running is a no-op.
#   - synthetic demo data is applied by `make seed` via `python -m app.seed`.

compose := docker compose

.PHONY: up down logs migrate seed test fmt api-shell db-shell

## up       Build and start the full stack (db, mock-llm, api, web).
up:
	$(compose) up --build -d
	@echo ""
	@echo "Stack started:"
	@echo "  API       http://localhost:8000   (OpenAPI docs: http://localhost:8000/docs)"
	@echo "  Web       http://localhost:5173"
	@echo "  DB        localhost:5432          (user/pass from .env)"
	@echo "  Mock LLM  http://localhost:5000"
	@echo "Next: make migrate && make seed"

## down     Stop and remove the stack (keeps the pgdata volume).
down:
	$(compose) down

## logs     Tail logs from all services.
logs:
	$(compose) logs -f --tail=200

## migrate  Apply roles.sql -> schema.sql -> rls.sql via app.db.apply_migrations.
migrate:
	$(compose) up -d api
	@$(compose) exec -T api python -m app.db.apply_migrations

## seed     Apply synthetic demo data (covering 2025-04-15 & 2026-02-06) via app.seed.
seed:
	$(compose) up -d api
	@$(compose) exec -T api python -m app.seed

## test     Run the backend pytest suite inside the api container.
test:
	$(compose) exec -T api pytest -q

## fmt      Format and lint the backend code with ruff.
fmt:
	$(compose) exec -T api ruff format .
	$(compose) exec -T api ruff check --fix .

## api-shell   Open a shell inside the api container.
api-shell:
	$(compose) exec api sh

## db-shell    Open a psql shell inside the db container.
db-shell:
	$(compose) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'
