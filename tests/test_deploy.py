from dfh.deploy import analytics_files_present


def test_analytics_files_require_sales_partitions_and_stockout_file(tmp_path):
    sales = tmp_path / "sales_daily" / "year=2026" / "month=10"
    sales.mkdir(parents=True)
    (sales / "part-000.parquet").write_bytes(b"fixture")
    assert not analytics_files_present(tmp_path)

    (tmp_path / "stockout_events.parquet").write_bytes(b"fixture")
    assert analytics_files_present(tmp_path)


def test_analytics_files_are_not_ready_when_sales_are_missing(tmp_path):
    (tmp_path / "stockout_events.parquet").write_bytes(b"fixture")
    assert not analytics_files_present(tmp_path)
