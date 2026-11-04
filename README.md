# Dry Fruit House Operations Platform (demo)

Demo of an in-house platform for a 500 store dry fruit retail chain: offline-first POS, OMS and WMS, procurement
and inbound logistics, wastage, finance and tax working data, demand planning, customers, HR and platform.
All data is synthetic. Prices, costs, vendors and volumes are modeled, not real.

## Deploy on Render

This repository includes a Render Blueprint (render.yaml). Create a dedicated PostgreSQL database first
(for example, a Neon project), then create a new Blueprint Instance in Render from this repository.

- Supply the PostgreSQL connection string as DATABASE_URL when prompted. Use the direct connection string
  with SSL enabled (Neon connection strings include sslmode=require).
- Render generates DFH_SECRET; do not replace it with the development fallback.
- The Blueprint attaches a 1 GB persistent disk at /var/data. The six-month Parquet analytics dataset is
  stored there because Render's normal service filesystem is ephemeral.
- On first boot, Alembic creates the schema and the startup bootstrap seeds the synthetic dataset only when
  the database is empty. Existing non-empty databases are never reseeded. If database rows exist but the
  persistent analytics files are missing, startup fails rather than overwriting data.
- The service health check is /api/health. Demo login: ceo / demo1234.

The persistent disk requires a paid Render web-service plan. Keep this as a single instance while the disk
is attached. Use a dedicated demo database; do not point the seed process at a database containing real data.

## Run the app locally
~~~bash
make setup     # install, then seed masters and 6 months of history (about 10 seconds)
make test      # automated tests
make run       # http://localhost:8000  (login: ceo / demo1234)
make smoke     # drives the UI in Chromium and saves screenshots to /tmp/shots
~~~

## Run the foundation only
~~~bash
pip install -r requirements.txt
python -m dfh.seed.run          # masters plus 6 months of history (about 10 seconds)
pytest -q tests                 # automated tests
~~~

Output: data/dfh.db (SQLite masters, 95 tables) and data/sales_daily/ (Parquet, about 22 MB for 6 months).
Set DATABASE_URL for Postgres. Read context in docs/ first: CONTEXT.md, PLAN.md, UPDATES.md.
