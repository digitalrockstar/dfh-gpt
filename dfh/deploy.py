"""Render startup bootstrap for the synthetic demo dataset.

The database and Parquet analytics files must both survive restarts. Render's
Blueprint attaches a persistent disk for Parquet; DATABASE_URL points to a
dedicated PostgreSQL database. Existing non-empty databases are never reseeded.
"""
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

from .db import get_engine
from .schema import metadata


def database_has_rows(engine) -> bool:
    """Return True if any application table contains data."""
    with engine.connect() as connection:
        for table in metadata.sorted_tables:
            if connection.execute(select(table).limit(1)).first() is not None:
                return True
    return False


def analytics_files_present(data_dir: str | Path) -> bool:
    """Check the two Parquet inputs required by Analytics at app startup."""
    root = Path(data_dir)
    return (
        any(root.glob("sales_daily/**/*.parquet"))
        and (root / "stockout_events.parquet").is_file()
    )


def bootstrap() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must point to a dedicated PostgreSQL demo database")

    data_dir = Path(os.getenv("DFH_DATA", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    engine = get_engine(database_url)
    try:
        populated = database_has_rows(engine)
    finally:
        engine.dispose()

    if not populated:
        # seed.run refuses to layer fixtures over any non-empty database.
        subprocess.run(
            [
                sys.executable,
                "-m",
                "dfh.seed.run",
                "--db",
                database_url,
                "--data",
                str(data_dir),
            ],
            check=True,
        )
    elif not analytics_files_present(data_dir):
        raise RuntimeError(
            "The demo database already contains records but its persistent "
            "Parquet analytics files are missing. Refusing to reseed or overwrite "
            "the database; restore the DFH_DATA disk backup."
        )

    if not analytics_files_present(data_dir):
        raise RuntimeError("Demo seed completed without the required Parquet analytics files")


def main() -> None:
    bootstrap()
    port = os.getenv("PORT", "10000")
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "dfh.app:get_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
    )


if __name__ == "__main__":
    main()
