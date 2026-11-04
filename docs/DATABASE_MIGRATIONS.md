# Database migrations

DFH GPT uses Alembic with the SQLAlchemy Core metadata in `dfh/schema.py`.

## New database

Set `DATABASE_URL` to the target database and run:

```bash
pip install -r requirements.txt
alembic upgrade head
```

The initial `0001_baseline` revision creates any missing metadata tables. It is intentionally irreversible: do not use a downgrade as a schema-reset mechanism.

## Existing database

For an existing database whose schema has already been created and validated, do not replay the baseline as though it were a schema diff. First take a backup and verify the schema, then register the baseline:

```bash
alembic stamp 0001_baseline
```

This records the migration version without changing application tables.

## Future schema changes

1. Update `dfh/schema.py`.
2. Generate a candidate migration with `make revision message="describe change"`.
3. Review the generated migration carefully; autogeneration is not a substitute for review.
4. Add migration tests for both upgrade behavior and relevant data transformations.
5. Apply with `make migrate` in a controlled deployment.

Use additive, backward-compatible changes where possible. Destructive changes require an explicit data migration and a verified backup/restore plan.
