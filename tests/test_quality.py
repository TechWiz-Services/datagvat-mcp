from datetime import date

from conftest import DATASET, DATASET_3
from datagvat_mcp.quality import assess_quality

TODAY = date(2026, 11, 11)


def test_complete_dataset_scores_high():
    q = assess_quality(DATASET, today=TODAY)
    assert q["score"] >= 90 and q["rating"] == "good"
    assert q["formats"] == ["CSV", "PDF"]
    assert q["last_modified"] == "2025-11-20"
    assert {c["check"] for c in q["checks"]} >= {"licenses", "machine_readable", "up_to_date"}


def test_sparse_dataset_scores_low_with_reasons():
    q = assess_quality(DATASET_3, today=TODAY)
    assert q["rating"] == "poor"
    failed = {c["check"] for c in q["checks"] if not c["passed"]}
    assert {"description", "keywords", "licenses", "contact_point", "open_format", "up_to_date"} <= failed
    assert "machine_readable" not in failed  # XLSX is machine-readable, just not open
    assert q["missing"]


def test_stale_dataset_and_unknown_frequency():
    d = dict(DATASET, modified="2020-01-01", accrual_periodicity={"label": "unknown",
             "resource": "http://publications.europa.eu/resource/authority/frequency/UNKNOWN"})
    q = assess_quality(d, today=TODAY)
    failed = {c["check"] for c in q["checks"] if not c["passed"]}
    assert {"up_to_date", "update_frequency"} <= failed


def test_score_is_percentage():
    for d in (DATASET, DATASET_3, {}):
        assert 0 <= assess_quality(d, today=TODAY)["score"] <= 100
