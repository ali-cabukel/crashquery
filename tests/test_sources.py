from crashquery.ingest.sources import candidate_csv_urls, canonical_column, csv_url


def test_canonical_column_aliases() -> None:
    assert canonical_column("collision_index") == "accident_index"
    assert canonical_column("COLLISION_YEAR") == "accident_year"
    assert canonical_column("casualty_severity") == "casualty_severity"


def test_candidate_urls_include_accident_fallback() -> None:
    urls = candidate_csv_urls("collision", 2019)
    assert csv_url("collision", 2019) in urls
    assert any("accident-2019" in url for url in urls)
    assert len(candidate_csv_urls("vehicle", 2019)) == 1
