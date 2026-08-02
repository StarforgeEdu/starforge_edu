.PHONY: up down migrate makemigrations seed test lint format typecheck schema check

# Bring up the local infra (Postgres + Redis + MinIO) only.
up:
	docker compose -f docker/docker-compose.yml up -d postgres redis minio

down:
	docker compose -f docker/docker-compose.yml down

# Migrate shared (public) AND every tenant schema.
migrate:
	uv run python manage.py migrate_schemas --shared
	uv run python manage.py migrate_schemas --tenant

makemigrations:
	uv run python manage.py makemigrations

seed:
	STARFORGE_ALLOW_LOCAL_DEMO_SEED=1 uv run python scripts/seed_dev.py

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy apps core infrastructure config

# Generate + validate the OpenAPI schema (the frontend contract).
schema:
	uv run python scripts/export_openapi.py --validate

# Everything CI runs, locally.
check: lint typecheck test schema
