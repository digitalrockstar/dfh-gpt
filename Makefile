setup:
	pip install -r requirements.txt
	python -m dfh.seed.run

test:
	python -m pytest -q tests

run:
	uvicorn dfh.app:get_app --factory --reload --port 8000

smoke:
	python scripts/smoke.py

smoke-pos:
	python scripts/smoke_pos.py

smoke-crm:
	python scripts/smoke_crm.py

# Apply versioned schema migrations (set DATABASE_URL for the target database).
migrate:
	alembic upgrade head

# Generate a migration after updating dfh/schema.py; pass message="...".
revision:
	alembic revision --autogenerate -m "$(message)"
